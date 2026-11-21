"""Exercise the adapter through its shell entry point and an HTTP North fixture.

These tests protect the trial contract: frozen input reaches an isolated session,
outputs and tool traces return to the evaluator, and broken streams cannot pass.
"""
from email.parser import BytesParser
from email.policy import default
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "skills/skill-distill/adapters/north.sh"
EXTRACT = ROOT / "skills/skill-distill/scripts/extract_session.py"


class NorthFixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_request(self):
        server = self.server
        path = urlsplit(self.path).path
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Authorization") != "Bearer fixture-secret":
            self.reply({"error": "wrong token"}, 401)
            return
        server.requests.append((self.command, path, body, dict(self.headers)))
        if self.command == "DELETE":
            self.reply(None)
        elif path == "/api/v2/agents":
            server.counter += 1
            self.reply({"id": f"agent-{server.counter}"})
        elif path == "/api/v2/conversations":
            self.reply({"id": f"conversation-{server.counter}"})
        elif path == "/api/internal/v1/skills":
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
            server.package = {p.get_filename(): p.get_payload(decode=True) for p in message.iter_parts()}
            self.reply({"id": f"skill-{server.counter}"})
        elif path.endswith("/enablement"):
            self.reply({"enabled": True}, 404 if server.failure == "legacy" else 200)
        elif path == "/api/internal/v2/chat":
            if server.failure == "http":
                self.reply({"error": "fixture-secret must not be echoed"}, 403)
                return
            server.chat = json.loads(body)
            # Materialize the uploaded skill exactly as North's mount would, then
            # run the actual adapter-generated bootstrap before returning model events.
            runtime_name = server.package["SKILL.md"].decode().split("name: ", 1)[1].splitlines()[0]
            mount = server.temp / "skills" / runtime_name
            for name, data in server.package.items():
                target = mount / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            sandbox = server.temp / f"conversation-{server.counter}" / "artifacts"
            instruction = server.chat["instruction"]
            command = instruction.split("setup command:\n", 1)[1].split("\n", 1)[0]
            command = command.replace("/mnt/skills", str(server.temp / "skills"))
            command = command.replace("/mnt/data/artifacts", str(sandbox))
            subprocess.run(command, shell=True, check=True, capture_output=True)
            result = sandbox / "skill-distill" / "result.txt"
            result.write_text("10\n")
            (sandbox / "skill-distill" / "obsolete.txt").unlink()
            server.remote = {p.relative_to(sandbox).as_posix(): p.read_bytes()
                             for p in sandbox.rglob("*") if p.is_file() and ".git" not in p.parts}
            events = [
                {"type": "stream-start", "conversation_id": f"conversation-{server.counter}"},
                {"type": "message-start"},
                {"type": "tool-call-start", "index": 0, "delta": {"message": {"tool_calls": {
                    "tool_call_id": "call-1", "function": {"name": "run_command", "arguments": ""}}}}},
                {"type": "tool-call-delta", "index": 0, "delta": {"message": {"tool_calls": {
                    "function": {"arguments": '{"command":"cat '}}}}},
                {"type": "tool-call-delta", "index": 0, "delta": {"message": {"tool_calls": {
                    "function": {"arguments": 'result.txt"}'}}}}},
                {"type": "tool-call-end", "index": 0},
                {"type": "message-end"},
                {"type": "message-start", "tool_call_id": "call-1", "delta": {"message": {"role": "tool"}}},
                {"type": "content-delta", "tool_call_id": "call-1", "delta": {"message": {"role": "tool", "content": {
                    "type": "document", "document": {"data": {"output": "10\n", "exit_code": 0, "success": True}}}}}},
                {"type": "message-end", "tool_call_id": "call-1", "delta": {"finish_reason": "COMPLETE"}},
                {"type": "message-start"},
                {"type": "content-delta", "delta": {"message": {"content": {"type": "text", "text": "DO"}}}},
                {"type": "content-delta", "delta": {"message": {"content": {"type": "text", "text": "NE"}}}},
                {"type": "message-end"},
                {"type": "stream-end", "delta": {"finish_reason": "COMPLETE"}},
            ]
            if server.failure == "truncated":
                events.pop()
            elif server.failure == "error":
                events[-1]["delta"] = {"finish_reason": "ERROR", "error": {"message": "fixture-secret"}}
            elif server.failure == "tool-error":
                for event in events:
                    if event.get("type") == "content-delta" and event.get("tool_call_id"):
                        event["delta"]["message"]["content"]["document"]["data"].update(exit_code=1, success=False)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b": ping\r\n\r\n")
            for event in events:
                # Pretty JSON is legal multiline SSE data.
                self.wfile.write(b"".join(b"data: " + line + b"\r\n" for line in json.dumps(event, indent=2).encode().splitlines()) + b"\r\n")
        elif path.startswith("/api/internal/v2/conversations/"):
            self.reply({"id": f"conversation-{server.counter}", "messages": []})
        elif path == "/api/internal/v1/sandbox/output_files":
            query = parse_qs(urlsplit(self.path).query)
            names = sorted(server.remote)
            # Force pagination to verify all artifacts are collected.
            if "next_token" not in query:
                names, token = names[:1], "page-2"
            else:
                names, token = names[1:], None
            if server.failure == "traversal":
                names = ["skill-distill/../../outside.txt"]
                token = None
            self.reply({"data": [{"path": n, "type": "FILE"} for n in names], "next_token": token})
        elif path == "/api/internal/v1/sandbox/output_files/download":
            name = parse_qs(urlsplit(self.path).query)["file_path"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(server.remote[name])
        else:
            self.reply({"error": "unexpected request"}, 404)

    do_POST = handle_request
    do_PUT = handle_request
    do_GET = handle_request
    do_DELETE = handle_request


class NorthAdapterTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.temp = Path(self.directory.name)
        self.workspace = self.temp / "workspace"
        self.trace = self.temp / "trace"
        self.skill = self.workspace / ".skill" / "sum-csv"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: sum-csv\ndescription: Sum CSV values.\n---\nRead and sum numbers.csv.\n")
        (self.workspace / "numbers.csv").write_text("value\n2\n3\n5\n")
        (self.workspace / "obsolete.txt").write_text("Remove this file during the task.\n")
        (self.workspace / "helper.sh").write_text("#!/bin/sh\ncat numbers.csv\n")
        (self.workspace / "helper.sh").chmod(0o755)
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)
        (self.workspace / ".git/info/exclude").write_text(".skill/\n")
        self.prompt = self.temp / "prompt.md"
        self.prompt.write_text("Sum numbers.csv.\n\nA skill is at `.skill/sum-csv/SKILL.md`.\n")
        self.token = self.temp / "token file"
        self.token.write_text("fixture-secret\n")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), NorthFixture)
        self.server.temp = self.temp / "remote"
        self.server.requests, self.server.counter, self.server.failure = [], 0, None
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def adapter(self, extra=""):
        env = {key: value for key, value in os.environ.items() if not key.startswith("NORTH_")}
        env.update(WORKSPACE=str(self.workspace), TRACE_DIR=str(self.trace), PROMPT_FILE=str(self.prompt),
                   MODEL="north-mini-beta", NORTH_BASE_URL=f"http://127.0.0.1:{self.server.server_port}/api/",
                   NORTH_TOKEN_FILE=str(self.token), STUDENT_ARGS=extra)
        return subprocess.run(["bash", str(ADAPTER)], env=env, capture_output=True, text=True, timeout=15)

    def test_trial_returns_files_and_parseable_trace_and_cleans_its_resources(self):
        result = self.adapter()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.workspace / "result.txt").read_text(), "10\n")
        self.assertFalse((self.workspace / "obsolete.txt").exists())
        self.assertEqual(self.server.chat["messages"][0]["content"], self.prompt.read_text())
        self.assertEqual(self.server.package["original-skill.md"], (self.skill / "SKILL.md").read_bytes())
        self.assertEqual(self.server.package["workspace/numbers.csv"], (self.workspace / "numbers.csv").read_bytes())
        self.assertEqual((self.workspace / "helper.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.server.chat["skills"]["skill_ids"], ["skill-1"])
        self.assertFalse(self.server.chat["use_memory"])
        summary = self.temp / "summary.json"
        extraction = subprocess.run(["python3", str(EXTRACT), str(self.trace / "session.jsonl"),
                                     "--json", str(summary)], capture_output=True, text=True)
        self.assertEqual(extraction.returncode, 0, extraction.stderr)
        parsed = json.loads(summary.read_text())
        self.assertEqual(parsed["task"], self.prompt.read_text().strip())
        self.assertEqual((parsed["final"], parsed["tool_calls"], parsed["tool_errors"]), ("DONE", 1, 0))
        self.assertIn("cat result.txt", " ".join(parsed["steps"]))
        deleted = [path for method, path, _, _ in self.server.requests if method == "DELETE"]
        self.assertEqual(deleted, ["/api/v1/conversations/conversation-1", "/api/v2/agents/agent-1", "/api/internal/v1/skills/skill-1"])
        for path in self.trace.iterdir():
            self.assertNotIn("fixture-secret", path.read_text())

    def test_broken_streams_fail_and_retain_partial_trace(self):
        for failure in ("truncated", "error", "http"):
            with self.subTest(failure=failure):
                self.server.failure = failure
                result = self.adapter()
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("fixture-secret", result.stderr)
                metadata = json.loads((self.trace / "north.json").read_text())
                self.assertFalse(metadata.get("complete", False))
                self.assertTrue((self.trace / "session.jsonl").exists())
                self.assertFalse((self.workspace / "result.txt").exists())

    def test_tool_failures_are_visible_to_the_evaluator(self):
        self.server.failure = "tool-error"
        result = self.adapter()
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = self.temp / "summary.json"
        subprocess.run(["python3", str(EXTRACT), str(self.trace / "session.jsonl"), "--json", str(summary)],
                       check=True, capture_output=True)
        parsed = json.loads(summary.read_text())
        self.assertEqual(parsed["tool_errors"], 1)
        self.assertIn("ERROR", " ".join(parsed["steps"]))

    def test_parallel_tool_results_are_attached_to_the_correct_calls(self):
        rows = [{"type": "north-session", "prompt": "Run two tools."}, {"type": "message-start"}]
        for index, name in enumerate(("alpha", "beta")):
            rows += [{"type": "tool-call-start", "index": index, "delta": {"message": {"tool_calls": {
                "tool_call_id": name, "function": {"name": name, "arguments": "{}"}}}}},
                {"type": "tool-call-end", "index": index}]
        for name in ("beta", "alpha"):
            rows += [{"type": "message-start", "tool_call_id": name, "delta": {"message": {"role": "tool"}}},
                     {"type": "content-delta", "tool_call_id": name, "delta": {"message": {"role": "tool", "content": {
                         "type": "document", "document": {"data": {"output": name + "-response", "success": True}}}}}},
                     {"type": "message-end", "tool_call_id": name}]
        trace = self.temp / "parallel.jsonl"
        trace.write_text("".join(json.dumps(row) + "\n" for row in rows))
        summary = self.temp / "summary.json"
        subprocess.run(["python3", str(EXTRACT), str(trace), "--json", str(summary)], check=True, capture_output=True)
        steps = json.loads(summary.read_text())["steps"]
        for name in ("alpha", "beta"):
            step = next(step for step in steps if step.startswith("`" + name + "`"))
            self.assertIn(name + "-response", step)
            self.assertNotIn(("beta" if name == "alpha" else "alpha") + "-response", step)

    def test_trials_have_distinct_conversations_and_skill_mounts(self):
        first = self.adapter()
        self.assertEqual(first.returncode, 0, first.stderr)
        one = json.loads((self.trace / "north.json").read_text())
        (self.workspace / "obsolete.txt").write_text("Remove this file during the task.\n")
        before = len(self.server.requests)
        second = self.adapter("--keep-remote")
        self.assertEqual(second.returncode, 0, second.stderr)
        two = json.loads((self.trace / "north.json").read_text())
        self.assertNotEqual(one["conversation_id"], two["conversation_id"])
        self.assertNotEqual(one["skill_name"], two["skill_name"])
        self.assertFalse(any(method == "DELETE" for method, _, _, _ in self.server.requests[before:]))

    def test_existing_agent_is_retained_and_provider_is_forwarded(self):
        result = self.adapter("--agent-id configured-agent --provider-instance local-provider")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.chat["agent"], {"id": "configured-agent"})
        self.assertEqual(self.server.chat["provider_instance"], "local-provider")
        self.assertNotIn("tools", self.server.chat)  # Retain its MCP/sandbox tool configuration.
        self.assertNotIn("libraries", self.server.chat)
        self.assertFalse(any(path in ("/api/v2/agents", "/api/v2/agents/configured-agent")
                             for _, path, _, _ in self.server.requests))
        self.assertEqual([path for method, path, _, _ in self.server.requests if method == "DELETE"],
                         ["/api/v1/conversations/conversation-0", "/api/internal/v1/skills/skill-0"])

    def test_legacy_north_without_skill_enablement_still_runs(self):
        self.server.failure = "legacy"
        result = self.adapter()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.workspace / "result.txt").read_text(), "10\n")

    def test_download_paths_cannot_escape_the_trial_workspace(self):
        self.server.failure = "traversal"
        result = self.adapter()
        self.assertEqual(result.returncode, 1)
        self.assertIn("unsafe artifact path", result.stderr)
        self.assertFalse((self.temp / "outside.txt").exists())

    def test_symlink_input_is_rejected_before_remote_resources_are_created(self):
        (self.workspace / "linked-token").symlink_to(self.token)
        result = self.adapter()
        self.assertEqual(result.returncode, 1)
        self.assertIn("symlinks", result.stderr)
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
