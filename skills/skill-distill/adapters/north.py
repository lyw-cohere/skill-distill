#!/usr/bin/env python3
"""Run one North student trial (Python standard library only).

Configure NORTH_BASE_URL and either NORTH_TOKEN_FILE or NORTH_TOKEN/NORTH_JWT.
Keep credentials in the inherited environment, not distill.py --env/run.json.
For example, export NORTH_BASE_URL=https://your-north and NORTH_TOKEN_FILE=/tmp/token,
then use distill.py init ... --student north:north-mini-beta.
STUDENT_ARGS accepts the options shown by: python3 adapters/north.py --help.

Each trial creates a conversation, stages regular workspace files and the frozen
.skill tree under /mnt/data/artifacts/skill-distill, runs the original prompt,
and downloads artifacts back to WORKSPACE. A temporary private agent supplies the
sandbox tool by default; --agent-id uses an existing agent without changing it.
The trace holds raw SSE events, conversation.json, and run metadata for checks.
Files are staged as a private, uniquely named North skill. Remote resources created
by this adapter are deleted afterward unless --keep-remote.

Requires North's v2 conversation API, private skills API and sandbox tool. Symlinks
are rejected; git metadata and ignored dependencies are not staged. Install needed
dependencies inside the sandbox. API downloads preserve bytes, not new file modes.
External MCP state is task-specific: isolate it in --setup and check it in checks.sh.
"""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


class NorthError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the bearer token to a redirected host.


class NorthClient:
    def __init__(self, base_url, token, timeout, organization_id="default"):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"))):
            raise NorthError("Use an HTTPS North URL (HTTP is allowed only for localhost)")
        self.base = base_url.rstrip("/")
        if not self.base.endswith("/api"):
            self.base += "/api"
        self.token, self.timeout = token, timeout
        self.organization_id = organization_id
        self.opener = urllib.request.build_opener(NoRedirect())

    def open(self, method, path, *, payload=None, data=None, content_type=None):
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json",
                   "Organization-Id": self.organization_id}
        if payload is not None:
            data = json.dumps(payload).encode()
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            raise NorthError(f"{method} {path.split('?')[0]}: HTTP {error.code}", error.code) from None
        except (OSError, urllib.error.URLError):
            raise NorthError(f"{method} {path.split('?')[0]}: connection failed or timed out") from None

    def json(self, method, path, payload=None):
        with self.open(method, path, payload=payload) as response:
            body = response.read()
        try:
            return json.loads(body) if body else None
        except ValueError:
            raise NorthError(f"{method} {path.split('?')[0]}: invalid JSON") from None

    def upload_skill(self, files):
        boundary = uuid.uuid4().hex
        parts = []
        for name, content in files.items():
            if any(char in name for char in ('"', '\r', '\n', '\\')):
                raise NorthError("Workspace filename cannot be represented in a skill upload")
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                          f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
                         + content + b"\r\n")
        data = b"".join(parts) + f"--{boundary}--\r\n".encode()
        with self.open("POST", "/internal/v1/skills",
                       data=data, content_type="multipart/form-data; boundary=" + boundary) as response:
            return json.load(response)


def sse_events(response):
    """Decode SSE frames, including comments, CRLF and multiline data."""
    data = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                value = "\n".join(data)
                data = []
                if value == "[DONE]":
                    continue
                event = json.loads(value)
                if not isinstance(event, dict):
                    raise NorthError("North returned an invalid SSE event")
                yield event
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        raise NorthError("North stream ended inside an SSE frame")


def workspace_files(workspace):
    result = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                            cwd=workspace, capture_output=True, check=True)
    names = set(os.fsdecode(name) for name in result.stdout.split(b"\0") if name)
    names.update(path.relative_to(workspace).as_posix() for path in (workspace / ".skill").rglob("*")
                 if not path.is_dir() or path.is_symlink())
    if not any(name.startswith(".skill/") and name.endswith("/SKILL.md") for name in names):
        raise NorthError("WORKSPACE must contain the frozen .skill/<name>/SKILL.md")
    files = []
    for name in sorted(names):
        path = workspace / name
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != workspace.parent):
            raise NorthError(f"Sandbox staging does not support symlinks: {name}")
        if not path.is_file():
            continue
        if path.stat().st_size > 25 * 1024 * 1024:
            raise NorthError(f"Sandbox upload exceeds 25 MiB: {name}")
        files.append((name, path, path.stat().st_mode & 0o777))
    if len(files) > 1000:
        raise NorthError("Sandbox staging exceeds 1000 files; use a smaller starting state")
    return files


def skill_package(workspace, files):
    skills = list((workspace / ".skill").glob("*/SKILL.md"))
    if len(skills) != 1:
        raise NorthError("Expected exactly one frozen student skill")
    original = skills[0].parent.name
    runtime_name = "distill-" + uuid.uuid4().hex[:16]
    markdown = skills[0].read_text()
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---(?:\n|$)", markdown, re.S)
    if not frontmatter:
        raise NorthError("The student skill needs YAML frontmatter")
    front, count = re.subn(r"^name:.*$", "name: " + runtime_name, frontmatter.group(1), flags=re.M)
    if count != 1:
        raise NorthError("The student skill needs exactly one frontmatter name")
    package = {"SKILL.md": ("---\n" + front + "\n---\n" + markdown[frontmatter.end():]).encode(),
               "original-skill.md": skills[0].read_bytes()}
    for name, path, _ in files:
        if name.startswith(".skill/" + original + "/"):
            relative = name[len(".skill/" + original + "/"):]
            if relative == "SKILL.md":
                continue
            if relative in ("original-skill.md", "workspace") or relative.startswith("workspace/"):
                raise NorthError("Student skill uses a path reserved for workspace staging")
            package[relative] = path.read_bytes()
        elif not name.startswith(".skill/"):
            package["workspace/" + name] = path.read_bytes()
    return original, runtime_name, package


def collect_files(client, conversation_id, workspace, trace_dir, files):
    remote = []
    next_token = None
    seen_tokens = set()
    while True:
        query = {"sandbox_session_id": conversation_id, "only_list_current_folder": "false",
                 "only_list_files": "true", "path_prefix": "skill-distill/"}
        if next_token:
            query["next_token"] = next_token
        result = client.json("GET", "/internal/v1/sandbox/output_files?" + urllib.parse.urlencode(query))
        for item in result["data"]:
            if item["type"] != "FILE":
                continue
            name = item["path"]
            parts = PurePosixPath(name).parts
            if (not name.startswith("skill-distill/") or ".." in parts or "\\" in name
                    or "\0" in name or name != PurePosixPath(name).as_posix()):
                raise NorthError("Sandbox returned an unsafe artifact path")
            relative = name[len("skill-distill/"):]
            if not relative or relative.endswith("/"):
                raise NorthError("Sandbox returned an unsafe artifact path")
            if PurePosixPath(relative).parts[0] in (".git", ".skill"):
                continue
            target = workspace / relative
            if not target.resolve().is_relative_to(workspace):
                raise NorthError("Sandbox artifact escapes WORKSPACE")
            remote.append((name, relative))
        next_token = result.get("next_token")
        if not next_token:
            break
        if next_token in seen_tokens:
            raise NorthError("Sandbox repeated its pagination token")
        seen_tokens.add(next_token)
    modes = {name: mode for name, _, mode in files}
    returned = {relative for _, relative in remote}
    if len(returned) != len(remote):
        raise NorthError("Sandbox returned duplicate artifact paths")
    # Finish all downloads before replacing local files; a failed transfer must not
    # leave a mixture that looks like a completed trial.
    with tempfile.TemporaryDirectory(dir=trace_dir) as temp:
        staged = Path(temp)
        for name, relative in remote:
            query = urllib.parse.urlencode({"sandbox_session_id": conversation_id, "file_path": name})
            with client.open("GET", "/internal/v1/sandbox/output_files/download?" + query) as response:
                content = response.read()
            dest = staged / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
            dest.chmod(modes.get(relative, 0o644))
        for name, path, _ in files:
            if not name.startswith(".skill/") and name not in returned:
                path.unlink(missing_ok=True)
        for _, relative in remote:
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged / relative, target)
    (trace_dir / "artifacts.json").write_text(json.dumps([relative for _, relative in remote], indent=2))


def run(args):
    workspace = Path(os.environ["WORKSPACE"]).resolve()
    trace_dir = Path(os.environ["TRACE_DIR"]).resolve()
    trace_dir.mkdir(parents=True, exist_ok=True)
    prompt = Path(os.environ["PROMPT_FILE"]).read_text()
    if not args.model:
        raise NorthError("Set MODEL to the North student model")
    token = (Path(args.token_file).read_text().strip() if args.token_file else
             os.environ.get("NORTH_TOKEN") or os.environ.get("NORTH_JWT"))
    if not token or "\n" in token or "\r" in token:
        raise NorthError("Set NORTH_TOKEN, NORTH_JWT or NORTH_TOKEN_FILE to a valid bearer token")
    client = NorthClient(args.base_url, token, args.timeout, args.organization_id)
    files = workspace_files(workspace)
    original_skill, runtime_skill, package = skill_package(workspace, files)
    agent_id = args.agent_id
    conversation_id = None
    skill_id = None
    owned_agent = not agent_id
    metadata = {"model": args.model, "provider_instance": args.provider_instance}
    try:
        if owned_agent:
            agent = client.json("POST", "/v2/agents", {"name": "skill-distill-" + uuid.uuid4().hex[:12],
                                "model": args.model, "visibility": "private", "instructions": ""})
            agent_id = agent["id"]
        conversation = client.json("POST", "/v2/conversations", {"agent_id": agent_id, "title": "skill-distill trial"})
        conversation_id = conversation["id"]
        metadata.update(agent_id=agent_id, conversation_id=conversation_id)
        (trace_dir / "north.json").write_text(json.dumps(metadata, indent=2))
        print(f"North conversation: {conversation_id}", flush=True)
        skill = client.upload_skill(package)
        skill_id = skill["id"]
        metadata.update(skill_id=skill_id, skill_name=runtime_skill)
        try:
            client.json("PUT", f"/internal/v1/skills/{skill_id}/enablement", {"enabled": True})
        except NorthError as error:
            # Older deployments mount selected skills without an enablement toggle.
            if error.status != 404:
                raise
        root = "/mnt/data/artifacts/skill-distill"
        source = "/mnt/skills/" + runtime_skill
        frozen = root + "/.skill/" + original_skill
        bootstrap = ["mkdir -p " + shlex.quote(frozen),
                     "cp -a " + shlex.quote(source + "/.") + " " + shlex.quote(frozen + "/"),
                     "cp " + shlex.quote(source + "/original-skill.md") + " " + shlex.quote(frozen + "/SKILL.md")]
        if any(not name.startswith(".skill/") for name, _, _ in files):
            bootstrap.append("cp -a " + shlex.quote(source + "/workspace/.") + " " + shlex.quote(root + "/"))
        bootstrap += ["cd " + shlex.quote(root), "git init -q",
                     "printf '.skill/\\n' >> .git/info/exclude"]
        bootstrap += [f"chmod -- {mode:o} {shlex.quote(name)}" for name, _, mode in files if mode & 0o111]
        bootstrap += ["git add -A", "git -c commit.gpgsign=false -c user.name=skill-distill -c user.email=skill-distill@localhost "
                      "commit -q --allow-empty -m start"]
        instruction = (f"Your trial workspace is {root}. Before the task, run this setup command:\n"
                       + " && ".join(bootstrap) + f"\nRun subsequent shell commands from {root}. "
                       "The frozen task skill is in .skill/ as the user prompt specifies. "
                       "Keep task files in this workspace so they can be collected. "
                       f"Use only the trial skill {source}; ignore unrelated skills under /mnt/skills.")
        request = {"agent": {"id": agent_id}, "conversation": {"id": conversation_id},
                   "model": args.model, "stream": True, "messages": [{"role": "user", "content": prompt}],
                   "instruction": instruction, "use_memory": False,
                   "skills": {"selection": "selected", "skill_ids": [skill_id]}}
        if owned_agent:
            request["tools"] = [{"type": "north_tool", "north_tool": {"name": "sandbox"}}]
            request["libraries"] = []
        if args.provider_instance:
            request["provider_instance"] = args.provider_instance
        completed = False
        with (trace_dir / "session.jsonl").open("w") as trace:
            trace.write(json.dumps({"type": "north-session", "prompt": prompt, **metadata}) + "\n")
            trace.flush()
            with client.open("POST", "/internal/v2/chat", payload=request) as response:
                for event in sse_events(response):
                    trace.write(json.dumps(event) + "\n")
                    trace.flush()
                    event_type = event.get("type")
                    if event_type == "content-delta":
                        content = event.get("delta", {}).get("message", {}).get("content")
                        text = content if isinstance(content, str) else (content or {}).get("text", "")
                        print(text, end="", flush=True)
                    elif event_type == "tool-call-start":
                        call = event.get("delta", {}).get("message", {}).get("tool_calls") or {}
                        print("\nTool: " + call.get("function", {}).get("name", ""), flush=True)
                    elif event_type == "stream-end":
                        delta = event.get("delta") or {}
                        metadata["finish_reason"] = delta.get("finish_reason", "COMPLETE")
                        if delta.get("error") or metadata["finish_reason"] != "COMPLETE":
                            raise NorthError("North trial ended with " + metadata["finish_reason"])
                        completed = True
                        break
                    elif event_type in ("error", "tool-elicitation-request"):
                        raise NorthError("North trial requires intervention: " + event_type)
        if not completed:
            raise NorthError("North stream ended before stream-end")
        conversation = client.json("GET", f"/internal/v2/conversations/{conversation_id}?include_debug_info=false")
        (trace_dir / "conversation.json").write_text(json.dumps(conversation, indent=2))
        collect_files(client, conversation_id, workspace, trace_dir, files)
        metadata["complete"] = True
        print("\nNorth trial complete", flush=True)
    finally:
        if not args.keep_remote:
            for path in ([f"/v1/conversations/{conversation_id}"] if conversation_id else []) + (
                    [f"/v2/agents/{agent_id}"] if owned_agent and agent_id else []) + (
                    [f"/internal/v1/skills/{skill_id}"] if skill_id else []):
                try:
                    client.json("DELETE", path)
                except NorthError as error:
                    print("North cleanup: " + str(error), file=sys.stderr)
                    metadata.setdefault("cleanup_errors", []).append(str(error))
        (trace_dir / "north.json").write_text(json.dumps(metadata, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get("NORTH_BASE_URL", ""))
    parser.add_argument("--token-file", default=os.environ.get("NORTH_TOKEN_FILE"))
    parser.add_argument("--model", default=os.environ.get("MODEL", ""))
    parser.add_argument("--provider-instance", default=os.environ.get("NORTH_PROVIDER_INSTANCE"))
    parser.add_argument("--organization-id", default=os.environ.get("NORTH_ORG_ID", "default"))
    parser.add_argument("--agent-id", default=os.environ.get("NORTH_AGENT_ID"),
                        help="Existing student agent (must include sandbox); its configuration is retained")
    parser.add_argument("--timeout", type=float, default=300, help="Per-request socket timeout in seconds")
    parser.add_argument("--keep-remote", action="store_true", help="Keep the trial conversation and temporary agent")
    args = parser.parse_args(shlex.split(os.environ.get("STUDENT_ARGS", "")) + sys.argv[1:])
    def interrupted(signum, frame):
        raise NorthError("North trial interrupted or timed out")
    for name in ("SIGINT", "SIGTERM", "SIGALRM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupted)
    try:
        run(args)
        return 0
    except (NorthError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        # Do not echo response bodies, environment values or credential contents.
        message = str(error) if isinstance(error, NorthError) else type(error).__name__
        print("North adapter: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
