#!/bin/bash
# The smb-guard A-suite: the filer spike's A1-A13, ported to pass/fail regression tests of the
# production guard (edge/crates/omnifile-smbguard) under the production systemd drop-ins
# (edge/systemd). Linux >= 6.14 (pre-content fanotify), root, systemd, xfsprogs, attr, samba +
# smbclient (A11). Runs in the dev edge VM and on the ubuntu-26.04 GitHub runner.
#
#   sudo bash run.sh --bin-dir <dir with omnifile-smbguard-harness, smbguard-tool, omnifile-guardctl>
#   sudo bash run.sh --bin-dir ... --only a01,a06
#   sudo bash run.sh --cleanup-only
#
# Uses only $SG_ROOT (default /srv/omni-guard): a sparse xfs loop image, the test unit
# omnifile-smbguard-test.service and a test smbd on port 4450. Env: SG_REEXEC=1 also runs
# systemctl daemon-reexec in A6 (CI only); SG_FLOOD=<n> changes the A8 reader count (17,000);
# SG_BUDGET_SCALE=<x> multiplies every check's time budget (an emulated guest: release.sh
# --budget-scale).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ONLY=
BIN_DIR=
CLEANUP_ONLY=0
KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --bin-dir) BIN_DIR=$(cd "$2" && pwd); shift ;;
    --only) ONLY=$2; shift ;;
    --cleanup-only) CLEANUP_ONLY=1 ;;
    --keep) KEEP=1 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
if [ -n "$BIN_DIR" ]; then export SG_BIN=$BIN_DIR; fi
. "$HERE/lib.sh"
if [ "$CLEANUP_ONLY" = 1 ]; then suite_cleanup; exit 0; fi
# Signal the guard only through guard_pid (lib.sh): `kill ... "$(mainpid)"` can become kill 0.
if grep -nE 'kill +-[A-Z0-9]+ +"?\$\(mainpid\)' "$HERE"/*.sh; then echo "kill of \$(mainpid): use guard_pid" >&2; exit 2; fi
for b in "$H" "$T" "$CTL"; do [ -x "$b" ] || { echo "missing $b (build: cargo build --release -p omnifile-smbguard)" >&2; exit 2; }; done

CHECKS="a01_a04_policies:300 a05_refusals:60 a06_fdstore:600 a07_interlock:180 a08_flood:1800 \
a09_ordering:180 a10_paths:400 a11_a12_samba:400 a13_overhead:300 x01_otrunc_fence:420 \
x02_rewind_dirs:300 x03_prevver_open:300 x04_chunk_map:300"
trap 'suite_cleanup' EXIT
log "kernel $(uname -r); systemd $(systemctl --version | head -n 1)"
setup_volume
rm -f "$RESULTS"/*.md "$RESULTS"/*.title "$STATE/recalls.log" "$STATE/overwritten.log" "$STATE/modified.log" \
  "$STATE/populates.log"
suite_cleanup
install_units
harness_args ""
policy wait; delay 0; rate 0
start_guard || exit 1
FAILED=()
SUMMARY=$RESULTS/summary.md
{ echo "# smb-guard A-suite"; echo; echo "kernel \`$(uname -r)\`, $(systemctl --version | head -n 1)"; echo; } > "$SUMMARY"
for c in $CHECKS; do
  name=${c%%:*}; budget=$(awk -v b="${c##*:}" -v s="${SG_BUDGET_SCALE:-1}" 'BEGIN { printf "%d", b * s }')
  if [ -n "$ONLY" ] && ! grep -qw "${name%%_*}" <<< "${ONLY//,/ }"; then continue; fi
  active || { start_guard || true; }
  log "--- $name (budget ${budget}s)"
  CHECK=$name timeout -s KILL "$budget" bash "$HERE/$name.sh"; rc=$?
  pkill -9 -x smbguard-tool 2> /dev/null
  restore_unit
  if [ $rc = 0 ]; then st=PASS; else st="FAIL (rc $rc)"; FAILED+=("$name"); fi
  { echo "## $name: $st"; echo; cat "$RESULTS/$name.title" 2> /dev/null; cat "$RESULTS/$name.md" 2> /dev/null; echo; } >> "$SUMMARY"
  log "--- $name: $st"
done
[ "$KEEP" = 1 ] && trap - EXIT
echo
if [ ${#FAILED[@]} -gt 0 ]; then log "FAILED: ${FAILED[*]}"; exit 1; fi
log "all checks passed"
