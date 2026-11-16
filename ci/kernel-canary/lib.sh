# shellcheck shell=bash
# Shared helpers for the kernel canary. Sourced by run.sh and every k*.sh script; run as root.
#
# Layout (the same as the filer spike, so the spike's systemd unit is used verbatim):
#   /srv/omni-a/data            xfs on a loop image: the ONLY filesystem that is ever marked
#   /srv/omni-a/state           guard state files (policy, delay_ms, journal, pause)
#   /srv/omni-a/logs            guard and reader logs (uploaded as a CI artifact)
#   /srv/omni-a/results         per-check evidence (markdown), assembled into the job summary
#   /srv/omni-a/target/release  omnifile-guard and omnitool from spikes/filer/guard
set -u

A=/srv/omni-a
DATA=$A/data
STATE=$A/state
LOGS=$A/logs
BACKEND=$A/backend
RESULTS=$A/results
BIN=$A/target/release
GUARD=$BIN/omnifile-guard
T=$BIN/omnitool
UNIT=omni-guard-a
CANARY_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CHECK=${CHECK:-$(basename "$0" .sh)}
FAILS=0
WARNS=0
GPID=
GSTART=0

mkdir -p "$STATE" "$LOGS" "$BACKEND" "$RESULTS"

# ---- reporting -------------------------------------------------------------------------------
log() { echo "[$(date +%T)] $*"; }
md() { echo "$*" >> "$RESULTS/$CHECK.md"; }
section() { echo; echo "=== $* ==="; md ""; md "**$***"; md ""; }
evidence() { echo "    evidence: $*"; md "- $*"; }
pass() { echo "    PASS: $*"; md "- PASS: $*"; }
warn() { echo "    WARN: $*"; md "- WARN: $*"; WARNS=$((WARNS + 1)); }
fail() { echo "    FAIL: $*"; md "- **FAIL: $***"; FAILS=$((FAILS + 1)); }
# expect <what> <got> <want> [hint shown on failure]
expect() {
  if [ "$2" = "$3" ]; then pass "$1: $2"; else fail "$1: expected $3, got $2${4:+ -- $4}"; fi
}
begin() { : > "$RESULTS/$CHECK.md"; echo "$1" > "$RESULTS/$CHECK.title"; log "$CHECK: $1"; }
end_check() {
  if [ "$FAILS" -gt 0 ]; then log "$CHECK: $FAILS failure(s)"; exit 1; fi
  log "$CHECK: all assertions passed ($WARNS warning(s))"; exit 0
}

# ---- small utilities ---------------------------------------------------------------------------
# scaled <seconds>: a time limit times CANARY_BUDGET_SCALE (1; more for an emulated guest, which
# appliance/release.sh --budget-scale sets).
scaled() { awk -v n="$1" -v s="${CANARY_BUDGET_SCALE:-1}" 'BEGIN { printf "%d", n * s }'; }
# wait_until <seconds> <command...>: poll every 0.2 s; returns 1 on timeout.
wait_until() {
  local deadline=$(($(date +%s) + $1)); shift
  until "$@"; do [ "$(date +%s)" -ge "$deadline" ] && return 1; sleep 0.2; done
}
policy() { echo "$1" > "$STATE/policy"; }
delay() { echo "$1" > "$STATE/delay_ms"; }
dropcache() { sync; echo 3 > /proc/sys/vm/drop_caches; }

# omnitool exit status -> verdict. 124/137: killed by our timeout (a reader that never returned).
verdict() {
  case "$1" in
    0) echo OK ;; 2) echo ZEROS ;; 3) echo MISMATCH ;; 4) echo ERROR ;;
    124 | 137) echo TIMEOUT ;; *) echo "EXIT$1" ;;
  esac
}

# read_now <timeout-s> <omnitool check args...>: synchronous verified read. Sets OUT RC VERDICT.
read_now() {
  local to=$1; shift
  OUT=$(timeout -s KILL "$to" "$T" check "$@" 2>&1); RC=$?
  VERDICT=$(verdict $RC)
  echo "    omnitool: ${OUT:-<no output>} [$VERDICT]"
}

# start_reader <name> <timeout-s> <omnitool args...>: background reader. Sets RPID (the timeout
# wrapper). The reader is SIGKILLed at the timeout: a stub read blocked in fanotify sleeps
# TASK_KILLABLE (state D) and ignores everything but a fatal signal.
start_reader() {
  local name=$1 to=$2; shift 2
  timeout -s KILL "$to" "$T" "$@" > "$LOGS/$CHECK-$name.out" 2>&1 &
  RPID=$!
}
# finish_reader <name> <pid>: wait for it. Sets OUT RC VERDICT.
finish_reader() {
  wait "$2"; RC=$?
  VERDICT=$(verdict $RC)
  OUT=$(tail -n 1 "$LOGS/$CHECK-$1.out" 2>/dev/null)
  echo "    reader $1: ${OUT:-<no output>} [$VERDICT]"
}
# reader_state <timeout-wrapper-pid>: "<state> <wchan>" of the omnitool process under it.
reader_state() {
  local c
  c=$(pgrep -P "$1" -x omnitool | head -n 1)
  [ -n "$c" ] || { echo "gone -"; return; }
  echo "$(awk '{print $3}' "/proc/$c/stat" 2>/dev/null) $(cat "/proc/$c/wchan" 2>/dev/null)"
}
# elapsed seconds from an omnitool report line ("... in 3.012s" / "after 0.001s")
secs_of() { sed -nE 's/.*(in|after) ([0-9.]+)s.*/\2/p' <<< "$1"; }
# field <name> <flood progress line>: value of name=<n>
field() { sed -nE "s/.*[ ]$1=([0-9]+).*/\1/p" <<< " $2"; }

# ---- the data volume -------------------------------------------------------------------------
# Never arm a FAN_MARK_FILESYSTEM mark on anything but the canary's xfs loop volume: the spike
# showed that marking an unmounted path marks the ROOT filesystem.
assert_data_mount() { bash "$CANARY_DIR/check-mount.sh" "$DATA" "$A/fs-uuid"; }

# fresh synthetic stubs (pattern known to omnitool and the guard; no backend files needed).
# Only call while no group exists or a guard is answering: with an unanswered group every write
# to the volume blocks.
mkstubs() {
  rm -rf "$DATA/seis"; mkdir -p "$DATA/seis"
  "$T" mkstub "$DATA/seis/small.segy" 64M 1M small --synthetic --seed 1 > /dev/null
  "$T" mkstub "$DATA/seis/line.segy" 2G 4M line --synthetic --seed 2 > /dev/null
  dropcache
}

# ---- the guard as a plain process (no fd holder: the group dies with it) ----------------------
start_plain() {
  stop_plain
  if ! assert_data_mount; then fail "data volume check failed: refusing to arm a filesystem mark"; return 1; fi
  GSTART=$((GSTART + 1))
  GLOG=$LOGS/$CHECK-guard-$GSTART.log
  ( ulimit -n 1048576 2>/dev/null || ulimit -n 65536; exec "$GUARD" --fill-mount "$A/.fill" --private-ns "$@" ) >> "$GLOG" 2>&1 &
  GPID=$!
  if ! wait_until 15 grep -q "guard armed" "$GLOG"; then
    fail "guard did not arm within 15 s (log: $GLOG)"; tail -n 20 "$GLOG"; return 1
  fi
  log "plain guard pid=$GPID armed ($*); log $GLOG"
}
stop_plain() {
  { pkill -9 -x omnifile-guard && sleep 0.5; [ -n "$GPID" ] && wait "$GPID"; } 2> /dev/null
  GPID=
}
# kill_plain: SIGKILL the plain guard, which releases its group (and reap it quietly).
kill_plain() {
  { kill -9 "$GPID"; wait "$GPID"; } 2> /dev/null
  GPID=
}

# ---- cleanup: leave no group, no blocked reader, no unit ---------------------------------------
# Order matters: kill readers first (SIGKILL cancels their pending events), then release the
# group. Releasing a group ALLOWs everything pending, so readers left behind would read zeros.
canary_cleanup() {
  pkill -9 -x omnitool 2>/dev/null
  if [ -e "/etc/systemd/system/$UNIT.service" ]; then
    systemctl disable --now "$UNIT" > /dev/null 2>&1
    systemctl clean --what=fdstore "$UNIT" > /dev/null 2>&1
    rm -rf "/etc/systemd/system/$UNIT.service" "/etc/systemd/system/$UNIT.service.d"
    systemctl daemon-reload
    systemctl reset-failed "$UNIT" > /dev/null 2>&1
  fi
  pkill -9 -x omnifile-guard 2>/dev/null
  if [ -f "$A/max_queued_events.orig" ]; then
    sysctl -q -w fs.fanotify.max_queued_events="$(cat "$A/max_queued_events.orig")"
    rm -f "$A/max_queued_events.orig"
  fi
  rm -f "$STATE/pause" "$STATE/go" "$STATE/guard.env"
  policy wait; delay 0
  sleep 0.5
  local left
  left=$(pgrep -x 'omnitool|omnifile-guard' | tr '\n' ' ')
  [ -z "$left" ] || echo "cleanup: processes still present: $left"
}
