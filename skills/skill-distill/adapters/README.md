# Harness adapters

Each adapter runs one headless agent session and exits. `distill.py trial` uses a tmux window when available,
or a detached process otherwise.

The North adapter also accepts `--turns N` for long tasks that must resume in
the same conversation, sandbox and external workspace. Later turns continue
from the existing files and live external state, do not rerun bootstrap, and
verify completed-looking work rather than trusting a prior reply; the default
remains one.

For trials whose setup creates an isolated external fixture, pass a small
relative `--instruction-file` from `WORKSPACE`. Its contents are added to the
North system instruction on every turn, so a fresh resource ID can override a
stale ID that must remain in the verbatim reference prompt.

Environment passed in:

| var | meaning |
|---|---|
| `WORKSPACE` | fresh copy of the starting state; the agent's cwd |
| `PROMPT_FILE` | the original task prompt plus the line that points at the skill |
| `MODEL` | model id in the harness's own syntax |
| `TRACE_DIR` | where the adapter must leave `session.jsonl` (any format `extract_session.py` reads) |
| `STUDENT_ARGS` | extra flags, appended verbatim (provider config, thinking level, ...) |

Contract: write the harness's session log to `$TRACE_DIR/session.jsonl`, stream human-readable output to
stdout, and exit with the harness's exit code. Turn off the user's global context files and skills where
the harness allows it, so the only instructions the student gets are the prompt and the skill under test.

Adding a harness: copy `omp.sh`, change the command, and check that `extract_session.py` reads the log
(add a parser there if it doesn't).
