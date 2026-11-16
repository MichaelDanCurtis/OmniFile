#!/usr/bin/env bash
# The one release version: the repository's VERSION file, written into every manifest and lockfile
# (release/stamp.py has the list), or checked.
#
#   release/stamp.sh                 write VERSION's version everywhere
#   release/stamp.sh --version 0.2.0 set VERSION to 0.2.0 first
#   release/stamp.sh --check         every file, cargo metadata, uv's installed __version__ agree; exit 1 if not
#   release/stamp.sh --check --no-tools [--edge-bin edge/target/release/omnifile-edge]
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stamp.py" "$@"
