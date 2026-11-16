#!/usr/bin/env bash
# One step of the M6 exit, on the test VM (the client's Linux machine), as root:
#   exit.sh STEP   (stdin: {"step", "facts", "secrets_file"} from tests/real/harness/m6exit.py)
# Prints one JSON line: {"ok": true|false, "facts": {...}, ...}. The steps are in exit_steps.py.
set -euo pipefail
exec /srv/ofreal/venv/bin/python /srv/ofreal/vm/exit_steps.py "$@"
