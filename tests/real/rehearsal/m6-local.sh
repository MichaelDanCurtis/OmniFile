#!/usr/bin/env bash
# The M6 exit rehearsed on this Mac, before it runs on real Azure: the arm64 appliance under
# HVF (WP2 builds it), the local service stack (dev/m4/stack.py: api, web with its dev OIDC
# issuer, worker, relay, sim storage) and the shared omnifile-dc VM (Samba AD, OMNI.TEST),
# driven through the same public interfaces as tests/real/harness/m6exit.py: NFSv3, NFSv4 and
# SMB from this Mac, and the portal's REST API.
#
#   tests/real/rehearsal/m6-local.sh [--check]     what is there and what is missing (default;
#                                                  starts nothing)
#   tests/real/rehearsal/m6-local.sh --run [--work DIR] [--image FILE]
#
# Status: PENDING ON WP2. The run needs WP2's arm64 build of the appliance and HVF support in
# appliance/tests/vm.sh; until both are on main, --run refuses before it starts anything.
#
# What --run does, in order (each step names what it proves):
#   1. limactl start omnifile-dc (shared and read-only: the appliance only joins it).
#   2. dev/m4/stack.py up --work DIR, with OMNIFILE_DATABASE_URL naming a local Postgres.
#   3. An enrolment code from the portal API (POST /v1/edges as the dev ArchiveAdmin).
#   4. The appliance booted under HVF (qemu-system-aarch64 -accel hvf) on a fresh overlay
#      of the image, with the setup answers as the systemd credential omnifile.setup (the
#      wizard's unattended path: join OMNI.TEST, enrol with the code, private networking
#      off for the sim), the join password as omnifile.join-password, on vmnet-shared so it
#      reaches omnifile-dc's 192.168.104.0/24 (VERIFY: Lima's user-v2 network from a
#      vmnet guest; else socket_vmnet).
#   5. The M6 exit steps against it: NFSv3 and NFSv4 writes (mount_nfs), an SMB write as
#      geo-writer (mount_smbfs with a Kerberos ticket from omnifile-dc), uploaded and stubbed
#      (the portal API and blocks on the share), read back over all three, a pin to Archive,
#      a High recall and its read-back (the sim's clock advanced by POST /_sim/advance), and
#      WORM on the sim's WORM layout.
#   6. Teardown: the appliance's qemu killed and its overlay removed, the stack down. The DC
#      VM is left as it was found (started only if it was stopped, then stopped again).
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
MODE=check WORK=${TMPDIR:-/tmp}/omnifile-m6-rehearsal IMAGE=''
while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check ;;
    --run) MODE=run ;;
    --work) WORK=$2; shift ;;
    --image) IMAGE=$2; shift ;;
    *) echo "usage: m6-local.sh [--check | --run [--work DIR] [--image FILE]]" >&2; exit 2 ;;
  esac
  shift
done

missing=()
have() { command -v "$1" >/dev/null 2>&1; }
if [ -z "$IMAGE" ]; then
  IMAGE=$(ls -1 "$REPO"/appliance/build/out/omnifile-edge_*arm64*.qcow2 \
    "$REPO"/appliance/build/out/omnifile-edge_*aarch64*.raw 2>/dev/null | tail -1 || true)
fi
[ -n "$IMAGE" ] && [ -f "$IMAGE" ] || missing+=("the arm64 appliance image (WP2: appliance/build.sh --arch arm64)")
grep -q -i 'hvf' "$REPO/appliance/tests/vm.sh" 2>/dev/null || missing+=("HVF support in appliance/tests/vm.sh (WP2)")
have qemu-system-aarch64 || missing+=("qemu-system-aarch64 (brew install qemu)")
have limactl || missing+=("limactl (Lima)")
[ -f "$REPO/dev/m4/stack.py" ] || missing+=("dev/m4/stack.py")
[ -x "$REPO/service/.venv/bin/python" ] || missing+=("the service venv (uv sync --project service)")
[ -n "${OMNIFILE_DATABASE_URL:-}" ] || missing+=("OMNIFILE_DATABASE_URL (a local Postgres 16+ for the stack)")
if have limactl; then
  dc=$(limactl list --format '{{.Name}} {{.Status}}' 2>/dev/null | awk '$1=="omnifile-dc" {print $2}')
  [ -n "$dc" ] || missing+=("the omnifile-dc Lima VM (dev/up.sh)")
fi

echo "M6 local rehearsal: image=${IMAGE:-none} work=$WORK dc=${dc:-absent}"
if [ ${#missing[@]} -gt 0 ]; then
  echo "pending: missing"
  printf '  - %s\n' "${missing[@]}"
fi
if [ "$MODE" = check ]; then
  [ ${#missing[@]} -eq 0 ] && echo "ready: run with --run"
  exit 0
fi
if [ ${#missing[@]} -gt 0 ]; then
  echo "refusing to start: the rehearsal is pending on the items above" >&2
  exit 3
fi

# ------------------------------------------------------------------- the run
mkdir -p "$WORK"
started_dc=0
cleanup() {
  [ -f "$WORK/qemu.pid" ] && kill "$(cat "$WORK/qemu.pid")" 2>/dev/null || true
  rm -f "$WORK/edge.qcow2"
  "$REPO/service/.venv/bin/python" "$REPO/dev/m4/stack.py" down --work "$WORK/stack" || true
  if [ "$started_dc" = 1 ]; then limactl stop omnifile-dc || true; fi
}
trap cleanup EXIT
if [ "$dc" != Running ]; then
  limactl start omnifile-dc
  started_dc=1
fi
"$REPO/service/.venv/bin/python" "$REPO/dev/m4/stack.py" up --work "$WORK/stack"
"$REPO/service/.venv/bin/python" "$HERE/m6_local.py" --work "$WORK" --image "$IMAGE"
