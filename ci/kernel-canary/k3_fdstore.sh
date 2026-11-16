#!/bin/bash
# K3: the fail-closed design. The guard runs under systemd with the spike's unit
# (spikes/filer/guard/systemd/omni-guard-a.service): the fanotify group lives in the unit's fd
# store (FileDescriptorStorePreserve=yes, unit enabled), Restart=always, RestartMode=direct.
# kill -9 of the guard while a reader is blocked must never let zeros through: the group
# survives in the fd store, the restarted guard reuses it, answers its predecessor's journalled
# event, and the reader gets correct bytes.
. "$(dirname "$0")/lib.sh"
begin "K3 fail-closed guard: systemd fd store survives kill -9"
F=$DATA/seis/line.segy
SPIKE_UNIT=$CANARY_DIR/../../spikes/filer/guard/systemd/$UNIT.service

mainpid() { systemctl show -p MainPID --value "$UNIT"; }
nstore() { systemctl show -p NFileDescriptorStore --value "$UNIT"; }
running_new() { local p; p=$(mainpid); [ "$p" != 0 ] && [ "$p" != "$1" ] && [ "$(systemctl is-active "$UNIT")" = active ]; }
# kill_guard: SIGKILL the main process and wait until systemd has a new one running.
kill_guard() {
  local old; old=$(mainpid)
  kill -9 "$old"
  if wait_until 30 running_new "$old"; then log "killed guard $old, new MainPID $(mainpid)"
  else fail "guard did not come back within 30 s after kill -9 of $old"; fi
}

section "install the spike's guard unit"
mkstubs; policy wait; delay 0
: > "$STATE/guard.env"
cp "$SPIKE_UNIT" "/etc/systemd/system/$UNIT.service"
mkdir -p "/etc/systemd/system/$UNIT.service.d"
cp "$CANARY_DIR/check-mount.sh" "$A/check-mount.sh"
cat > "/etc/systemd/system/$UNIT.service.d/canary.conf" << EOF
# kernel canary drop-in: never arm the filesystem mark unless the xfs loop volume is mounted
# (an unmounted /srv/omni-a/data would mark the root filesystem), and do not let the
# deliberate kills of this test hit the start limit.
[Unit]
RequiresMountsFor=$DATA
StartLimitIntervalSec=0
[Service]
ExecStartPre=/bin/bash $A/check-mount.sh $DATA $A/fs-uuid
EOF
systemctl daemon-reload
systemctl enable "$UNIT" > /dev/null 2>&1
T0=$(date '+%Y-%m-%d %H:%M:%S')
if ! systemctl start "$UNIT"; then
  fail "guard unit failed to start"; journalctl -u "$UNIT" -n 30 --no-pager; end_check
fi
props=$(systemctl show -p FileDescriptorStorePreserve -p Restart -p RestartMode -p UnitFileState -p NFileDescriptorStore -p Type "$UNIT" | sort | tr '\n' ' ')
evidence "unit: $props"
expect "FileDescriptorStorePreserve" "$(systemctl show -p FileDescriptorStorePreserve --value "$UNIT")" yes
expect "Restart" "$(systemctl show -p Restart --value "$UNIT")" always
expect "RestartMode" "$(systemctl show -p RestartMode --value "$UNIT")" direct
expect "UnitFileState (must stay enabled or stop garbage-collects the store)" "$(systemctl show -p UnitFileState --value "$UNIT")" enabled
expect "group fd in the fd store" "$(nstore)" 1

section "K3a kill -9 while a reader is blocked in a fill"
delay 8000
start_reader a 90 check "$F" --off 100M --len 1M --seed 2
R=$RPID
wait_until 10 grep -q '^B ' "$STATE/journal" || fail "event never journalled"
evidence "journal before the kill: $(grep '^B ' "$STATE/journal" | tail -n 1 | cut -c 1-80)..."
st=$(reader_state $R); expect "reader blocked before the kill" "${st%% *}" D
kill_guard
finish_reader a $R
expect "reader blocked across kill -9 + restart" "$VERDICT" OK "a zero read here is silent corruption"
evidence "$OUT"

section "K3b kill -9 while idle, read at once (no daemon until systemd restarts it)"
delay 0
old=$(mainpid); kill -9 "$old"
read_now 60 "$F" --off 700M --len 1M --seed 2
expect "read issued right after the kill" "$VERDICT" OK
wait_until 30 running_new "$old" || fail "guard did not come back after kill -9 of $old"

section "K3c four kills in a row during a stream of reads"
delay 300
SL=$LOGS/$CHECK-stream.out
( for i in $(seq 0 29); do
    timeout -s KILL 60 "$T" check "$F" --off $((800 + i * 16))M --len 64K --seed 2; echo "rc=$?"
  done ) > "$SL" 2>&1 &
S=$!
for _ in 1 2 3 4; do sleep 2; kill_guard; done
wait $S
ok=$(grep -c ': OK ' "$SL"); zeros=$(grep -c 'ZEROS' "$SL"); mism=$(grep -c 'MISMATCH' "$SL")
errs=$(grep -c ': ERROR ' "$SL"); tmo=$(grep -cE '^rc=(124|137)$' "$SL")
evidence "30 reads, 4 kills: $ok OK, $zeros ZEROS, $mism MISMATCH, $errs ERROR, $tmo TIMEOUT"
expect "zero reads" "$zeros" 0 "fail-OPEN: silent corruption"
expect "mismatched reads" "$mism" 0
expect "reads that never returned" "$tmo" 0
if [ "$errs" -gt 0 ]; then warn "$errs read(s) got EIO (unjournalled event denied by the restart sweep: fail-closed)"; fi
expect "group fd still in the fd store" "$(nstore)" 1
evidence "NRestarts=$(systemctl show -p NRestarts --value "$UNIT")"

section "guard log (journald)"
jl=$(journalctl -u "$UNIT" --since "$T0" -o cat --no-pager)
n_created=$(grep -c "created group fd" <<< "$jl"); n_reused=$(grep -c "REUSING fanotify group" <<< "$jl")
evidence "group created $n_created time(s), reused from the fd store $n_reused time(s)"
expect "group created exactly once" "$n_created" 1
if [ "$n_reused" -ge 6 ]; then pass "every restart reused the stored group ($n_reused)"; else fail "only $n_reused restarts reused the stored group"; fi
grep -m 2 -E "ORPHAN event|orphan fd=" <<< "$jl" | cut -c 1-160 | while read -r l; do evidence "$l"; done
grep -q "check-mount: $DATA is xfs" <<< "$jl" && evidence "$(grep -m 1 "check-mount:" <<< "$jl")"

delay 0
end_check
