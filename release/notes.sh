#!/usr/bin/env bash
# Release notes from the merged pull requests between two tags or commits (local git only, grouped by area),
# with the security and known-issues sections (docs/release/notes-template.md). release/notes.py has the details.
#
#   release/notes.sh v0.1.0 HEAD [--version 0.2.0] [--security build/scan/scan.md] [--out NOTES.md]
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/notes.py" "$@"
