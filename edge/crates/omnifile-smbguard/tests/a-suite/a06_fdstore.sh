#!/bin/bash
# A6: the group outlives the process (systemd fd store), the journal lets a restarted guard
# answer its predecessor's events, and the sweep denies the ones it cannot account for.
. "$(dirname "$0")/lib.sh"
begin "A6: kill -9, restarts and the fd store"
F=$DATA/seis/a6.segy
mkdir -p "$DATA/seis"
policy wait; delay 0
T0=$(since_mark)

section "unit properties (production drop-in)"
expect "Type" "$(systemctl show -p Type --value "$UNIT")" notify
expect "FileDescriptorStorePreserve" "$(systemctl show -p FileDescriptorStorePreserve --value "$UNIT")" yes
expect "Restart" "$(systemctl show -p Restart --value "$UNIT")" always
expect "RestartMode" "$(systemctl show -p RestartMode --value "$UNIT")" direct
expect "PrivateMounts" "$(systemctl show -p PrivateMounts --value "$UNIT")" yes
expect "LimitNOFILE" "$(systemctl show -p LimitNOFILE --value "$UNIT")" 1048576
expect "UnitFileState" "$(systemctl show -p UnitFileState --value "$UNIT")" enabled
expect "pre-content + notification groups in the fd store" "$(nstore)" 2

section "A6a kill -9 while a reader is blocked in a fill"
mkstub seis/a6.segy 2G 4M 201; dropcache
delay 8000
start_reader a 90 check "$F" --off 100M --len 1M --seed 201
R=$RPID
wait_until 10 grep -q '^B ' "$JOURNAL" || fail "event never journalled"
evidence "journal: $(grep '^B ' "$JOURNAL" | tail -n 1 | cut -c 1-90)"
sleep 1; st=$(reader_state $R); expect "reader blocked before the kill" "${st%% *}" D
kill_guard
finish_reader a $R
expect "reader across kill -9 + restart" "$VERDICT" OK "a zero read here is silent corruption"

section "A6b kill -9 while idle, read at once"
delay 0
mkstub seis/a6b.segy 256M 1M 202; dropcache
if old=$(guard_pid); then kill -9 "$old"; else fail "no guard to kill (MainPID $(mainpid))"; fi
read_now 60 "$DATA/seis/a6b.segy" --off 100M --len 1M --seed 202
expect "read issued right after the kill" "$VERDICT" OK
wait_until 30 running_new "$old" || fail "guard did not come back"

section "A6c ten kills, 1 s apart, during a stream of 61 reads"
mkstub seis/a6c.segy 2G 4M 203; dropcache
delay 300
SL=$LOGS/$CHECK-stream.out
( for i in $(seq 0 60); do
    timeout -s KILL 90 "$T" check "$DATA/seis/a6c.segy" --off $((200 + i * 16))M --len 64K --seed 203; echo "rc=$?"
  done ) > "$SL" 2>&1 &
S=$!
for _ in $(seq 1 10); do sleep 1; kill_guard; done
wait $S
ok=$(grep -c ': OK ' "$SL"); zeros=$(grep -c 'ZEROS' "$SL"); mism=$(grep -c 'MISMATCH' "$SL")
errs=$(grep -c ': ERROR ' "$SL"); tmo=$(grep -cE '^rc=(124|137)$' "$SL")
evidence "61 reads, 10 kills: $ok OK, $zeros ZEROS, $mism MISMATCH, $errs ERROR, $tmo TIMEOUT"
expect "zero reads" "$zeros" 0 "fail-OPEN"
expect "mismatched reads" "$mism" 0
expect "reads that never returned" "$tmo" 0
[ "$errs" -gt 0 ] && warn "$errs read(s) got EIO (unjournalled event denied by the sweep: fail-closed)"
expect "group still in the fd store" "$(nstore)" 2

section "A6d restart and daemon-reload with a reader waiting"
delay 4000
mkstub seis/a6d.segy 256M 1M 204; dropcache
start_reader d1 90 check "$DATA/seis/a6d.segy" --off 100M --len 1M --seed 204; R=$RPID
sleep 1; systemctl restart "$UNIT"
finish_reader d1 $R; expect "reader across systemctl restart" "$VERDICT" OK
mkstub seis/a6d2.segy 256M 1M 205; dropcache
start_reader d2 90 check "$DATA/seis/a6d2.segy" --off 100M --len 1M --seed 205; R=$RPID
sleep 1; systemctl daemon-reload
finish_reader d2 $R; expect "reader across daemon-reload" "$VERDICT" OK
if [ "${SG_REEXEC:-0}" = 1 ]; then
  mkstub seis/a6d3.segy 256M 1M 206; dropcache
  start_reader d3 90 check "$DATA/seis/a6d3.segy" --off 100M --len 1M --seed 206; R=$RPID
  sleep 1; systemctl daemon-reexec
  finish_reader d3 $R; expect "reader across daemon-reexec" "$VERDICT" OK
else
  warn "daemon-reexec skipped (shared VM); SG_REEXEC=1 runs it (CI)"
fi
expect "group still in the fd store" "$(nstore)" 2

section "A6e stop with the unit enabled: the reader waits through the stop"
delay 0
mkstub seis/a6e.segy 256M 1M 207; dropcache
systemctl stop "$UNIT"
expect "store kept across stop" "$(nstore)" 2
guard_pid > /dev/null && fail "guard_pid returned MainPID $(mainpid) for a stopped unit" \
  || pass "guard_pid refuses MainPID 0 (kill -9 0 would kill this check)"
start_reader e 90 check "$DATA/seis/a6e.segy" --off 100M --len 1M --seed 207; R=$RPID
sleep 3; st=$(reader_state $R); expect "reader blocked while no guard runs" "${st%% *}" D
start_guard
finish_reader e $R; expect "reader after start" "$VERDICT" OK

section "A6f an event read but never journalled is denied by the sweep"
harness_args "--test-no-journal"; restart_guard
delay 8000
mkstub seis/a6f.segy 256M 1M 208; dropcache
start_reader f 60 check "$DATA/seis/a6f.segy" --off 100M --len 1M --seed 208; R=$RPID
sleep 2
harness_args ""
kill_guard
finish_reader f $R
expect "unjournalled event after the kill" "$VERDICT" ERROR "must be EIO, never zeros"
delay 0
read_now 60 "$DATA/seis/a6f.segy" --off 100M --len 1M --seed 208
expect "retry" "$VERDICT" OK

section "A6g crash loop to the start limit keeps the store"
mkdir -p "/etc/systemd/system/$UNIT.service.d"
printf '[Unit]\nStartLimitIntervalSec=60\nStartLimitBurst=3\n' > "/etc/systemd/system/$UNIT.service.d/99-startlimit.conf"
systemctl daemon-reload
mkstub seis/a6g.segy 256M 1M 209; dropcache
# Each kill waits for a new, active guard; MainPID is read once per kill (guard_pid), since it
# drops to 0 between a stale read and the next one while systemd restarts the unit.
prev=
for _ in $(seq 1 8); do
  wait_until 5 running_new "$prev" || break
  prev=$(guard_pid) || break
  kill -9 "$prev" 2> /dev/null
done
wait_until 15 bash -c "[ \"\$(systemctl is-active $UNIT)\" = failed ]" && pass "unit failed at the start limit" \
  || warn "unit state $(systemctl is-active "$UNIT") (start limit not reached)"
expect "store kept while failed" "$(nstore)" 2
start_reader g 90 check "$DATA/seis/a6g.segy" --off 100M --len 1M --seed 209; R=$RPID
sleep 2; st=$(reader_state $R); expect "reader blocked (fail closed) while failed" "${st%% *}" D
rm -f "/etc/systemd/system/$UNIT.service.d/99-startlimit.conf"; systemctl daemon-reload
systemctl reset-failed "$UNIT"; start_guard
finish_reader g $R; expect "reader after reset-failed + start" "$VERDICT" OK

section "guard log"
jl=$(glog "$T0")
n_created=$(grep -c "created a new fanotify pre-content group" <<< "$jl")
n_reused=$(grep -c "REUSING the fanotify group" <<< "$jl")
evidence "group created $n_created time(s) in this check, reused $n_reused time(s)"
expect "no new group in this check" "$n_created" 0
expect_ge "restarts that reused the stored group" "$n_reused" 15
grep -m 2 "ORPHAN event" <<< "$jl" | cut -c 1-200 | while read -r l; do evidence "$l"; done
grep -q "ORPHAN event" <<< "$jl" && pass "orphan events were answered by successors" || fail "no orphan answered"
n_busy=$(grep -c "orphan event denied (fd number busy)" <<< "$jl")
expect "orphans denied because their fd number was taken (N1: reserved first)" "$n_busy" 0
grep -q "sweep: unjournalled event denied" <<< "$jl" && pass "sweep denied an unjournalled event" \
  || fail "sweep never denied anything"
end_check
