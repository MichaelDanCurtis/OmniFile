#!/bin/bash
# M2 additions: O_TRUNC overwrites (no pre-content event) clear stale stub state; a fill racing
# an O_TRUNC never leaves backend bytes in the user's file; bad markers fail closed.
. "$(dirname "$0")/lib.sh"
begin "X1: O_TRUNC, the fill fence, parallel writers, PARTIAL writes, held cap, bad markers"
mkdir -p "$DATA/seis"
policy wait; delay 0

section ": > stub (O_TRUNC, nothing else): the watcher clears the marker"
mkstub seis/o1.segy 64M 1M 801; dropcache
: > "$DATA/seis/o1.segy"
wait_until 10 bash -c "! $T marker $DATA/seis/o1.segy | grep -q Marker" && pass "marker removed" \
  || fail "stale marker left: $($T marker "$DATA/seis/o1.segy")"
expect "size" "$(stat -c %s "$DATA/seis/o1.segy")" 0
grep -q "OVERWRITTEN .*file_id=801 " "$STATE/overwritten.log" && pass "reported to the broker" || fail "not reported"

section "O_TRUNC + write over a stub: the user's bytes, no stub state"
mkstub seis/o2.segy 64M 1M 802; dropcache
"$T" owrite "$DATA/seis/o2.segy" 3M --seed 9802; expect "overwrite" "$?" 0
read_now 20 "$DATA/seis/o2.segy" --seed 9802; expect "new content" "$VERDICT" OK
expect "size" "$(stat -c %s "$DATA/seis/o2.segy")" 3145728
"$T" marker "$DATA/seis/o2.segy" | grep -q Unmanaged && pass "no marker" || fail "marker left: $($T marker "$DATA/seis/o2.segy")"

section "an O_TRUNC racing a running fill: no backend byte survives in the new file"
rate 32
mkstub seis/o3.segy 512M 1M 803; dropcache
start_reader fill 120 check "$DATA/seis/o3.segy" --off 500M --len 1M --seed 803; R=$RPID
sleep 3
"$T" owrite "$DATA/seis/o3.segy" 1M --seed 9803; expect "overwrite during the fill" "$?" 0
sleep 2
rate 0
read_now 20 "$DATA/seis/o3.segy" --seed 9803; expect "file holds only the new bytes" "$VERDICT" OK
expect "size is the new size" "$(stat -c %s "$DATA/seis/o3.segy")" 1048576
evidence "$(grep "file_id=803 " "$STATE/overwritten.log" | tail -n 1)"
kill -9 "$R" 2> /dev/null; wait "$R" 2> /dev/null
expect "marker" "$("$T" marker "$DATA/seis/o3.segy" | grep -c Unmanaged)" 1

section "a corrupt marker fails closed"
mkstub seis/b.segy 64M 1M 804; dropcache
setfattr -n trusted.omnifile -v "0x$(printf '0%.0s' $(seq 128))" "$DATA/seis/b.segy"
read_now 10 "$DATA/seis/b.segy" --off 20M --len 1M --seed 804
expect "read past the prefix" "$VERDICT" ERROR
read_now 10 "$DATA/seis/b.segy" --off 0 --len 4096 --seed 804
expect "even the prefix" "$VERDICT" ERROR
section "B1: parallel writers on one O_TRUNC open while the clean-up runs (smbd aio)"
# The clean-up removes the marker, waits out any fill, then truncates to 0. A second
# writer must not be allowed in that window (the file looks unmanaged), or the
# truncate wipes its bytes. quiesce_delay_ms stretches the window to 3 s.
echo 3000 > "$STATE/quiesce_delay_ms"
mkstub seis/o4.segy 64M 1M 805; dropcache
out=$(timeout -s KILL 60 "$T" parwrite "$DATA/seis/o4.segy" 1M 2M 1M --seed 9805 --gap-ms 800 2>&1); rc=$?
evidence "$out"
echo 0 > "$STATE/quiesce_delay_ms"
expect "both writers succeeded" "$rc" 0
expect "size holds both writes" "$(stat -c %s "$DATA/seis/o4.segy")" 3145728
read_now 20 "$DATA/seis/o4.segy" --off 0 --len 1M --seed 9805
grep -q "OK 1048576 bytes" <<< "$OUT" && pass "writer 1's bytes" || fail "writer 1's bytes lost: $OUT"
read_now 20 "$DATA/seis/o4.segy" --off 2M --len 1M --seed 9805
grep -q "OK 1048576 bytes" <<< "$OUT" && pass "writer 2's bytes" || fail "writer 2's bytes wiped by the clean-up: $OUT"

section "B2: a write into a PARTIAL file's local prefix is reported to the daemon"
mkstub seis/m.segy 64M 1M 806; dropcache
"$T" pwrite "$DATA/seis/m.segy" 4096 4096 --seed 806; expect "write into the prefix" "$?" 0
wait_until 10 grep -q "MODIFIED .*file_id=806 " "$STATE/modified.log" && pass "partial_modified called" \
  || fail "write into a PARTIAL file not reported"
mkstub seis/m2.segy 64M 1M 807; dropcache
# mkstub's own writes (before it set the marker) may still be in the watcher's queue and be
# reported once the marker exists: a harmless false positive. Let them drain first.
sleep 2; sed -i '/file_id=807 /d' "$STATE/modified.log" 2> /dev/null
read_now 30 "$DATA/seis/m2.segy" --seed 807; expect "fill of another stub" "$VERDICT" OK
sleep 1
grep -q "file_id=807 " "$STATE/modified.log" 2> /dev/null && fail "the daemon's own fill was reported as a modification" \
  || pass "fills through the fill mount are not reported"

section "a placeholder (nothing resident) is not reported as written"
# The daemon sizes a new placeholder before setting its marker, as mkstub does: that
# FS_MODIFY reaches the watcher once the marker exists. With no local prefix it can't be
# a write the guard let through (any write waits for a fill first), so it is not reported:
# reporting it would fill the file whole and commit it as a new version (M5 e2e test).
mkstub seis/p.segy 64M 0 809; dropcache
sleep 2
grep -q "file_id=809 " "$STATE/modified.log" 2> /dev/null && fail "a new placeholder was reported as a modification" \
  || pass "a new placeholder is not reported"
read_now 30 "$DATA/seis/p.segy" --seed 809; expect "and it still fills on a read" "$VERDICT" OK

section "B2 across a restart: a PARTIAL write while the guard is down is still reported"
# The notification group lives in the fd store with the pre-content group: the write waits
# for the guard (pre-content event), and its FS_MODIFY is queued for the next instance.
expect "both groups in the fd store" "$(nstore)" 2
mkstub seis/m3.segy 64M 1M 808; dropcache
sleep 2; sed -i '/file_id=808 /d' "$STATE/modified.log" 2> /dev/null
C=$(since_mark)
systemctl stop "$UNIT"
start_reader w3 60 pwrite "$DATA/seis/m3.segy" 8192 4096 --seed 808; R=$RPID
sleep 2; st=$(reader_state $R); expect "write waits while no guard runs" "${st%% *}" D
start_guard
finish_reader w3 $R; expect "write after restart" "$RC" 0
wait_until 10 grep -q "MODIFIED .*file_id=808 " "$STATE/modified.log" && pass "reported after the restart" \
  || fail "write made while the guard was down never reported"
glog "$C" | grep -q "REUSING the notification group" && pass "notification group reused from the fd store" \
  || fail "notification group not reused (a write landing in the gap would be lost)"

section "N7: held events have their own cap (here 2)"
harness_args "--max-held 2"; restart_guard
policy wait; delay 20000
P=()
for i in 1 2 3 4; do
  mkstub "seis/h$i.segy" 64M 1M $((810 + i)); dropcache
  start_reader "h$i" 60 check "$DATA/seis/h$i.segy" --off 20M --len 1M --seed $((810 + i)); P+=("$RPID")
  sleep 0.7
done
sleep 2
blocked=0
for p in "${P[@]}"; do st=$(reader_state "$p"); [ "${st%% *}" = D ] && blocked=$((blocked + 1)); done
errs=0
for i in 1 2 3 4; do grep -q "ERROR errno=5" "$LOGS/$CHECK-h$i.out" 2> /dev/null && errs=$((errs + 1)); done
expect "readers held" "$blocked" 2
expect "readers over the cap denied with EIO" "$errs" 2
read_now 10 "$DATA/seis/h1.segy" --off 0 --len 1M --seed 811
expect "a resident read still passes at once" "$VERDICT" OK
for p in "${P[@]}"; do kill -9 "$p" 2> /dev/null; done
pkill -9 -x smbguard-tool 2> /dev/null; wait 2> /dev/null
delay 0; harness_args ""; restart_guard
end_check
