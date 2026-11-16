# shellcheck shell=bash
# Shared helpers for the smb-guard A-suite. Sourced by run.sh and every a*.sh; run as root.
#
# The suite runs the production guard library (omnifile-smbguard-harness) under a test unit
# that carries the production drop-in edge/systemd/omnifile-edge.service.d/10-smbguard.conf
# verbatim, with paths substituted. Nothing outside these paths is touched:
#   $A/xfs.img, $A/data   the test volume (xfs on a loop device): the ONLY filesystem marked
#   $A/state              harness state files (policy, delay_ms, rate_mbps, fail, ctl/)
#   $A/logs, $A/results   evidence
#   /etc/systemd/system/omnifile-smbguard-test{,-smbd}.service(.d)
set -u

SUITE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$SUITE/../../../../.." && pwd)
A=${SG_ROOT:-/srv/omni-guard}
DATA=$A/data
STATE=$A/state
LOGS=$A/logs
RESULTS=$A/results
FILL=/run/omni-guard-fill
BIN=${SG_BIN:-$A/target/release}
H=$BIN/omnifile-smbguard-harness
T=$BIN/smbguard-tool
CTL=$BIN/omnifile-guardctl
UNIT=omnifile-smbguard-test
SMBU=omnifile-smbguard-test-smbd
JOURNAL=/run/$UNIT/journal
SMBPORT=4450
CHECK=${CHECK:-$(basename "$0" .sh)}
FAILS=0
WARNS=0

mkdir -p "$STATE" "$LOGS" "$RESULTS"

# ---- reporting ---------------------------------------------------------------------------------
log() { echo "[$(date +%T)] $*"; }
md() { echo "$*" >> "$RESULTS/$CHECK.md"; }
section() { echo; echo "=== $* ==="; md ""; md "**$***"; md ""; }
evidence() { echo "    evidence: $*"; md "- $*"; }
pass() { echo "    PASS: $*"; md "- PASS: $*"; }
warn() { echo "    WARN: $*"; md "- WARN: $*"; WARNS=$((WARNS + 1)); }
fail() { echo "    FAIL: $*"; md "- **FAIL: $***"; FAILS=$((FAILS + 1)); }
expect() { if [ "$2" = "$3" ]; then pass "$1: $2"; else fail "$1: expected $3, got $2${4:+ -- $4}"; fi; }
# expect_le <what> <got> <max>, numbers (floats allowed)
expect_le() { if awk "BEGIN{exit !($2 <= $3)}"; then pass "$1: $2 <= $3"; else fail "$1: $2 > $3"; fi; }
expect_ge() { if awk "BEGIN{exit !($2 >= $3)}"; then pass "$1: $2 >= $3"; else fail "$1: $2 < $3"; fi; }
begin() { : > "$RESULTS/$CHECK.md"; echo "$1" > "$RESULTS/$CHECK.title"; log "$CHECK: $1"; }
end_check() {
  if [ "$FAILS" -gt 0 ]; then log "$CHECK: $FAILS failure(s)"; exit 1; fi
  log "$CHECK: all assertions passed ($WARNS warning(s))"; exit 0
}

# ---- utilities ---------------------------------------------------------------------------------
# scaled <seconds>: a time limit times SG_BUDGET_SCALE (1; more for an emulated guest, which
# appliance/release.sh --budget-scale sets).
scaled() { awk -v n="$1" -v s="${SG_BUDGET_SCALE:-1}" 'BEGIN { printf "%d", n * s }'; }
wait_until() {
  local deadline=$(($(date +%s) + $1)); shift
  until "$@"; do [ "$(date +%s)" -ge "$deadline" ] && return 1; sleep 0.2; done
}
policy() { echo "$1" > "$STATE/policy"; }
delay() { echo "$1" > "$STATE/delay_ms"; }
rate() { echo "$1" > "$STATE/rate_mbps"; }
dropcache() { sync; echo 3 > /proc/sys/vm/drop_caches; }
verdict() {
  case "$1" in
    0) echo OK ;; 2) echo ZEROS ;; 3) echo MISMATCH ;; 4) echo ERROR ;;
    124 | 137) echo TIMEOUT ;; *) echo "EXIT$1" ;;
  esac
}
# read_now <timeout-s> <tool check args...>: sets OUT RC VERDICT
read_now() {
  local to=$1; shift
  OUT=$(timeout -s KILL "$to" "$T" check "$@" 2>&1); RC=$?
  VERDICT=$(verdict $RC)
  echo "    tool: ${OUT:-<no output>} [$VERDICT]"
}
# start_reader <name> <timeout-s> <tool args...>: sets RPID
start_reader() {
  local name=$1 to=$2; shift 2
  timeout -s KILL "$to" "$T" "$@" > "$LOGS/$CHECK-$name.out" 2>&1 &
  RPID=$!
}
finish_reader() {
  wait "$2"; RC=$?
  VERDICT=$(verdict $RC)
  OUT=$(tail -n 1 "$LOGS/$CHECK-$1.out" 2>/dev/null)
  echo "    reader $1: ${OUT:-<no output>} [$VERDICT]"
}
reader_state() {
  local c
  c=$(pgrep -P "$1" -x smbguard-tool | head -n 1)
  [ -n "$c" ] || { echo "gone -"; return; }
  echo "$(awk '{print $3}' "/proc/$c/stat" 2>/dev/null) $(cat "/proc/$c/wchan" 2>/dev/null)"
}
secs_of() { sed -nE 's/.*(in|after) ([0-9.]+)s.*/\2/p' <<< "$1"; }
field() { sed -nE "s/.*[ ]$1=([0-9]+).*/\1/p" <<< " $2"; }
marker_state() { "$T" marker "$1" 2>/dev/null | sed -nE 's/.*state: ([A-Za-z]+).*/\1/p'; }
recalls() { cat "$STATE/recalls.log" 2> /dev/null | wc -l; }

# ---- the test volume ---------------------------------------------------------------------------
setup_volume() {
  mkdir -p "$DATA"
  if ! mountpoint -q "$DATA"; then
    [ -f "$A/xfs.img" ] || { truncate -s 300G "$A/xfs.img"; mkfs.xfs -q -f "$A/xfs.img"; }
    mount -o loop "$A/xfs.img" "$DATA"
  fi
  blkid -c /dev/null -s UUID -o value "$(findmnt -n -o SOURCE --mountpoint "$DATA")" > "$A/fs-uuid"
  case "$DATA" in /srv/omnifile/*) echo "refusing: $DATA is the dev appliance volume"; exit 1 ;; esac
  bash "$REPO/ci/kernel-canary/check-mount.sh" "$DATA" "$A/fs-uuid"
}
UUID() { cat "$A/fs-uuid"; }

# mkstub <name> <size> <resident> <seed> [--online]
mkstub() { "$T" mkstub "$DATA/$1" "$2" "$3" --seed "$4" "${@:5}" > /dev/null; }

# ---- the guard under its test unit -------------------------------------------------------------
install_units() {
  local u=/etc/systemd/system/$UNIT.service
  cat > "$u" << EOF
[Unit]
Description=smb-guard A-suite: the guard library under the production drop-in
[Service]
EnvironmentFile=-$STATE/harness.env
ExecStartPre=/bin/bash $REPO/ci/kernel-canary/check-mount.sh $DATA $A/fs-uuid
ExecStart=$H --data $DATA --uuid $(UUID) --fill-mount $FILL --state $STATE --journal $JOURNAL \$HARNESS_ARGS
[Install]
WantedBy=multi-user.target
EOF
  mkdir -p "$u.d"
  sed -e "s#/srv/omnifile/data#$DATA#g" -e "s#/run/omnifile-fill#$FILL#g" \
      -e "s#^RuntimeDirectory=.*#RuntimeDirectory=$UNIT#" \
      -e "s#^Before=.*#Before=$SMBU.service#" \
      "$REPO/edge/systemd/omnifile-edge.service.d/10-smbguard.conf" > "$u.d/10-smbguard.conf"
  # A test smbd: its own config, port, state; guest share on the test volume. The production
  # smbd drop-in, with the unit name substituted.
  mkdir -p "$STATE/samba/private" "$STATE/samba/lock" "$STATE/samba/state" "$STATE/samba/cache" "$STATE/samba/pid"
  cat > "$STATE/samba/smb.conf" << EOF
[global]
  server role = standalone server
  security = user
  map to guest = Bad User
  guest account = nobody
  smb ports = $SMBPORT
  bind interfaces only = yes
  interfaces = lo
  private dir = $STATE/samba/private
  lock directory = $STATE/samba/lock
  state directory = $STATE/samba/state
  cache directory = $STATE/samba/cache
  pid directory = $STATE/samba/pid
  ncalrpc dir = $STATE/samba/lock/ncalrpc
  log file = $LOGS/smbd-test.log
  disable spoolss = yes
  load printers = no
  kernel oplocks = no
  smb2 leases = no
[sg]
  path = $DATA/smb
  guest ok = yes
  read only = no
  force user = root
EOF
  cat > "/etc/systemd/system/$SMBU.service" << EOF
[Unit]
Description=smb-guard A-suite: test smbd on port $SMBPORT
[Service]
Type=simple
ExecStart=/usr/sbin/smbd --foreground --no-process-group --configfile=$STATE/samba/smb.conf
EOF
  mkdir -p "/etc/systemd/system/$SMBU.service.d"
  sed -e "s#omnifile-edge.service#$UNIT.service#g" "$REPO/edge/systemd/smbd.service.d/10-omnifile-guard.conf" \
    > "/etc/systemd/system/$SMBU.service.d/10-omnifile-guard.conf"
  systemctl daemon-reload
  systemctl enable "$UNIT" > /dev/null 2>&1
}

# smb_listening: the test smbd accepts TCP on $SMBPORT. smbd can take ~20 s to listen when
# winbind's domain (the dev DC) is unreachable.
smb_listening() { grep -qiE "^ *[0-9]+: [0-9A-F]+:$(printf '%04X' "$SMBPORT") [0-9A-F]+:0000 0A" /proc/net/tcp /proc/net/tcp6; }

harness_args() { echo "HARNESS_ARGS=$*" > "$STATE/harness.env"; }
mainpid() { systemctl show -p MainPID --value "$UNIT"; }
# guard_pid: the guard's MainPID, read once, or fail. Signal the guard only through this: MainPID
# is 0 while the unit restarts (RestartSec=1) or has failed, and `kill -9 0` signals this whole
# process group, the check and its `timeout` included (a06 died that way, rc 137, in CI).
guard_pid() {
  local p; p=$(mainpid)
  case "$p" in '' | 0 | 1 | *[!0-9]*) return 1 ;; esac
  echo "$p"
}
nstore() { systemctl show -p NFileDescriptorStore --value "$UNIT"; }
active() { [ "$(systemctl is-active "$UNIT")" = active ]; }
running_new() { local p; p=$(mainpid); [ "$p" != 0 ] && [ "$p" != "$1" ] && active; }
start_guard() {
  systemctl start "$UNIT" || { fail "guard unit failed to start"; journalctl -u "$UNIT" -n 30 --no-pager; return 1; }
  wait_until 20 active || { fail "guard not active"; return 1; }
}
restart_guard() { systemctl restart "$UNIT"; wait_until 20 active; }
kill_guard() {
  local old
  old=$(guard_pid) || { fail "no guard to kill (MainPID $(mainpid))"; return 1; }
  kill -9 "$old"
  if wait_until 30 running_new "$old"; then log "killed guard $old, new MainPID $(mainpid)"
  else fail "guard did not come back within 30 s after kill -9 of $old"; fi
}
# fdinfo of the guard's pre-content group (class bits 0x8).
group_fdinfo() {
  local f fl
  for f in /proc/"$(mainpid)"/fdinfo/*; do
    fl=$(sed -n 's/^fanotify flags:\([0-9a-f]*\).*/\1/p' "$f" 2>/dev/null)
    [ -n "$fl" ] && [ $(( 0x$fl & 0xc )) = 8 ] && { cat "$f"; return; }
  done
}
# A journal cursor: glog <cursor> prints the guard's log lines after it.
since_mark() { journalctl -u "$UNIT" -n 1 --show-cursor -o cat --no-pager 2> /dev/null | sed -n 's/^-- cursor: //p'; }
glog() {
  if [ -n "$1" ]; then journalctl -u "$UNIT" --after-cursor "$1" -o cat --no-pager
  else journalctl -u "$UNIT" -o cat --no-pager; fi
}

# The suite's own guard group, released only with no reader left (killing readers first
# cancels their events; releasing a group ALLOWs everything pending).
release_guard() {
  pkill -9 -x smbguard-tool 2>/dev/null
  systemctl stop "$SMBU" 2>/dev/null
  systemctl stop "$UNIT" 2>/dev/null
  systemctl clean --what=fdstore "$UNIT" 2>/dev/null
  rm -f "$JOURNAL"
}

# restore_unit: undo what a check killed mid-way may have left on the test unit (a06's start-limit
# drop-in, a failed state), so one failure cannot cascade into the next check.
restore_unit() {
  if compgen -G "/etc/systemd/system/$UNIT.service.d/99-*.conf" > /dev/null; then
    rm -f "/etc/systemd/system/$UNIT.service.d/"99-*.conf
    systemctl daemon-reload
  fi
  systemctl reset-failed "$UNIT" > /dev/null 2>&1
  harness_args ""
}

suite_cleanup() {
  release_guard
  systemctl disable "$UNIT" > /dev/null 2>&1
  rm -rf "/etc/systemd/system/$UNIT.service" "/etc/systemd/system/$UNIT.service.d" \
    "/etc/systemd/system/$SMBU.service" "/etc/systemd/system/$SMBU.service.d" \
    "/usr/lib/systemd/system/multi-user.target.wants/$UNIT.service" \
    "/run/systemd/system/multi-user.target.wants/$UNIT.service" 2> /dev/null
  systemctl daemon-reload
  systemctl reset-failed "$UNIT" "$SMBU" > /dev/null 2>&1
  pkill -9 -x omnifile-smbguard-harness 2>/dev/null
  rm -f "$STATE/harness.env" "$STATE/fail" "$STATE/quiesce_delay_ms"
  policy wait; delay 0; rate 0
}
