#!/usr/bin/env bash
# North REST API. MODEL is a North model name; STUDENT_ARGS is parsed by north.py.
set -euo pipefail
exec python3 "$(dirname "$0")/north.py"
