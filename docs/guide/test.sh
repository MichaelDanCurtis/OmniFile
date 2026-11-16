#!/usr/bin/env bash
# The customer guide's tests: the lint (green on the guide, red on the broken fixture) and every tool
# the guide runs, against fakes. Offline; needs python3, the Bicep CLI (or az) and openssl.
#
#   docs/guide/test.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here/../.."
python3 -m unittest discover -s docs/guide/lint -p 'test_*.py' "$@"
