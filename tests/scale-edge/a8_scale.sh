#!/usr/bin/env bash
# A8 at scale (risk R6, the smb-guard's drain rate): the A-suite's flood check
# (edge/crates/omnifile-smbguard/tests/a-suite/a08_flood.sh, run unchanged through its
# run.sh) at 4,000, 8,000 and 17,000 concurrent readers of an unfilled stub, as root on the
# dev edge VM. Each run: N readers block on a stopped guard (none may get zeros), the guard
# is resumed, and the drain is timed; then A8b (descriptor exhaustion, 3,000 readers at
# LimitNOFILE=1024). The limits a large flood needs are raised first and recorded.
#
#   sudo tests/scale-edge/a8_scale.sh [--readers 4000,8000,17000] [--bin-dir DIR]
#
# The suite's volume is a sparse xfs image in RAM (tmpfs, 8 GiB) instead of the VM's disk.
# Results: tests/e2e/results/scale-edge-a8.md.
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
SUITE=$REPO/edge/crates/omnifile-smbguard/tests/a-suite
READERS=4000,8000,17000
BIN_DIR=""
RESULTS=$REPO/tests/e2e/results/scale-edge-a8.md
SG_RAM=/srv/omni-guard-ram
while [ $# -gt 0 ]; do
  case "$1" in
    --readers) READERS=$2; shift ;;
    --bin-dir) BIN_DIR=$2; shift ;;
    --results) RESULTS=$2; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
if [ -z "$BIN_DIR" ]; then # what dev/scripts/install-edge.sh built, newest first
  BIN_DIR=$(ls -dt /home/*/.cache/omnifile/edge-target/release 2>/dev/null |
    while read -r d; do [ -x "$d/omnifile-smbguard-harness" ] && { echo "$d"; break; }; done)
fi
[ -x "$BIN_DIR/omnifile-smbguard-harness" ] || { echo "no omnifile-smbguard-harness in '$BIN_DIR'" >&2; exit 2; }
LOG=/var/tmp/omnifile-a8
rm -rf "$LOG"
install -d "$LOG"
say() { printf '[a8 %s] %s\n' "$(date +%T)" "$*"; }

say "limits: before"
before="pid_max $(sysctl -n kernel.pid_max), threads-max $(sysctl -n kernel.threads-max), max_map_count $(sysctl -n vm.max_map_count), nr_open $(sysctl -n fs.nr_open), nofile $(ulimit -Sn)/$(ulimit -Hn), nproc $(ulimit -u), MemTotal $(awk '/MemTotal/ {print int($2/1048576)}' /proc/meminfo) GiB"
say "$before"
# Each reader is a thread blocked in read() with the file open: a pid, a task, a stack
# mapping (two with its guard page) and a descriptor. 17,000 of them need room above that.
sysctl -q -w kernel.pid_max=4194304
[ "$(sysctl -n kernel.threads-max)" -lt 200000 ] && sysctl -q -w kernel.threads-max=200000
[ "$(sysctl -n vm.max_map_count)" -lt 262144 ] && sysctl -q -w vm.max_map_count=262144
ulimit -Hn 1048576 && ulimit -Sn 1048576
ulimit -u unlimited
after="pid_max $(sysctl -n kernel.pid_max), threads-max $(sysctl -n kernel.threads-max), max_map_count $(sysctl -n vm.max_map_count), nofile $(ulimit -Sn)/$(ulimit -Hn), nproc $(ulimit -u)"
say "limits: after: $after"

install -d "$SG_RAM"
mountpoint -q "$SG_RAM" || mount -t tmpfs -o size=8G tmpfs "$SG_RAM" || exit 1
cleanup() {
  SG_ROOT=$SG_RAM bash "$SUITE/run.sh" --cleanup-only >/dev/null 2>&1
  umount "$SG_RAM/data" 2>/dev/null
  umount "$SG_RAM" 2>/dev/null
}
trap cleanup EXIT

# Free memory sampled while a flood runs: what 17,000 blocked readers cost.
memwatch() { while :; do awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo >>"$1"; sleep 1; done; }
ROWS=()
RC=0
IFS=, read -r -a ns <<<"$READERS"
for n in "${ns[@]}"; do
  say "A8 with $n readers"
  : >"$LOG/mem-$n.txt"
  memwatch "$LOG/mem-$n.txt" &
  mw=$!
  t=$(date +%s)
  SG_ROOT=$SG_RAM SG_FLOOD=$n bash "$SUITE/run.sh" --bin-dir "$BIN_DIR" --only a08 >"$LOG/run-$n.log" 2>&1
  rc=$?
  kill "$mw" 2>/dev/null
  took=$(($(date +%s) - t))
  md=$SG_RAM/results/a08_flood.md
  cp "$md" "$LOG/a08-$n.md" 2>/dev/null
  # "drained in S s: <the flood tool's last line>" and the tool's key=value counters.
  drain=$(grep -m1 -o 'drained in [0-9]* s' "$md" 2>/dev/null | grep -o '[0-9]*')
  flood=$(grep -m1 'drained in' "$md" 2>/dev/null)
  kv() { grep -o "$1=[0-9]*" <<<"$flood" | head -n 1 | cut -d= -f2; }
  ok=$(kv completed_ok) zeros=$(kv completed_ZEROS) errs=$(kv errors)
  stopped=$(grep -m1 'readers that got zeros while the guard was stopped' "$md" 2>/dev/null | grep -o '[0-9]*$')
  a8b=$(sed -n '/A8b/,$p' "$md" 2>/dev/null | grep -cE '^- \*\*FAIL')
  minmem=$(sort -n "$LOG/mem-$n.txt" | head -n 1)
  rate=$(awk -v n="${ok:-0}" -v s="${drain:-0}" 'BEGIN { if (s > 0) printf "%.0f", n / s; else print "-" }')
  st=PASS
  [ "$rc" -eq 0 ] || { st="FAIL (rc $rc)"; RC=1; }
  say "$n readers: $st; zeros while stopped ${stopped:-?}, after the drain ${zeros:-?}; ok ${ok:-?}; drained in ${drain:-?} s (${rate}/s); A8b failures $a8b; MemAvailable min ${minmem:-?} MiB; ${took} s"
  ROWS+=("| $n | $st | ${stopped:-?} | ${zeros:-?} | ${ok:-?} | ${errs:-?} | ${drain:-?} | $rate | $([ "$a8b" = 0 ] && echo PASS || echo "FAIL ($a8b)") | ${minmem:-?} | $took |")
done

{
  echo "# A8 at scale (smb-guard drain rate, risk R6): $(date -u +%FT%TZ)"
  echo
  echo "kernel \`$(uname -r)\`, $(nproc) vCPUs, $(awk '/MemTotal/ {print int($2/1048576)}' /proc/meminfo) GiB; the A-suite's a08_flood.sh, unchanged (run.sh --only a08, SG_FLOOD=N), binaries from $BIN_DIR; its volume an xfs image on tmpfs."
  echo
  echo "Limits before: $before."
  echo
  echo "Limits raised to: $after."
  echo
  echo "| readers | A8 | zeros while the guard was stopped | zeros after the drain | completed OK | errors | drain (s) | answers/s | A8b (3,000 readers, LimitNOFILE=1024) | MemAvailable min (MiB) | run (s) |"
  echo "|---|---|---|---|---|---|---|---|---|---|---|"
  printf '%s\n' "${ROWS[@]}"
  echo
  echo "The drain is timed by a08_flood.sh from SIGCONT of the guard to the flood tool's exit, to the second."
} >"$RESULTS"
say "results: $RESULTS"
exit $RC
