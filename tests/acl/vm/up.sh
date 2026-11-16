#!/usr/bin/env bash
# Create/start the permission-matrix VM (omnifile-acl) and join it to the dev AD.
# Needs omnifile-dc running and dev/.secrets/ad.env (dev/up.sh makes both).
# Idempotent. Never touches omnifile-edge or omnifile-dc's configuration; the
# join adds one computer account (ACL1) to OMNI.TEST.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
VM="${ACL_VM:-omnifile-acl}"

[ -r "$REPO/dev/.secrets/ad.env" ] || { echo "dev/.secrets/ad.env missing: run dev/up.sh first" >&2; exit 1; }
status="$(limactl list --format '{{.Status}}' "$VM" 2>/dev/null || true)"
case "$status" in
  Running) echo "[acl-vm] $VM already running" ;;
  Stopped) limactl start --tty=false "$VM" ;;
  "")
    repo_json="${REPO//\\/\\\\}"; repo_json="${repo_json//\"/\\\"}"
    limactl start --tty=false --name="$VM" \
      --set ".mounts = [{\"location\": \"$repo_json\", \"mountPoint\": \"/work\", \"writable\": false}]" \
      "$HERE/omnifile-acl.yaml" ;;
  *) echo "$VM is in state '$status'" >&2; exit 1 ;;
esac
limactl shell --workdir / "$VM" -- sudo /work/tests/acl/vm/provision.sh
