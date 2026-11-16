#!/usr/bin/env bash
# Build the omnifile-edge daemon (release) on Linux, as an ordinary user, with
# omnifile-guardctl, smbguard-tool and the omniacl CLI next to it
# (release/ and release/examples/). Prints the daemon's path on the last line.
#
#   dev/scripts/build-edge.sh            # in the dev VM: limactl shell omnifile-edge -- bash -lc /work/dev/scripts/build-edge.sh
#
# The build runs from a private copy of edge/ whose workspace lists only
# omnifile-edge, omnifile-smbguard and omnifile-acl (path dependencies come along). Other tracks add crates
# to edge/Cargo.toml while they are still half-written; the copy keeps this
# build independent of them, and keeps target/ off the virtiofs mount.
# Cargo.lock is copied, so the dependency versions are the repository's (cargo only drops the entries of the left-out crates).
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
WORK="${EDGE_BUILD_DIR:-$HOME/.cache/omnifile/edge-src}"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$HOME/.cache/omnifile/edge-target}"
# shellcheck disable=SC1091
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
command -v cargo >/dev/null || { echo "cargo not found" >&2; exit 1; }

mkdir -p "$WORK"
rsync -a --delete --exclude target --exclude '.git' "$REPO/edge/" "$WORK/"
python3 - "$WORK/Cargo.toml" <<'EOF'
import re, sys
p = sys.argv[1]
s = open(p).read()
s = re.sub(r"members\s*=\s*\[[^\]]*\]", 'members = ["crates/omnifile-edge", "crates/omnifile-smbguard", "crates/omnifile-acl"]', s, count=1, flags=re.S)
open(p, "w").write(s)
EOF
# The daemon (with the smb-guard inside), the guard's ops interlock and test
# tool, and the ACL CLI the permission matrix and e2e use.
(cd "$WORK" && cargo build --release -p omnifile-edge -p omnifile-smbguard --bins >&2)
(cd "$WORK" && cargo build --release -p omnifile-acl --example omniacl >&2)
echo "$CARGO_TARGET_DIR/release/omnifile-edge"
