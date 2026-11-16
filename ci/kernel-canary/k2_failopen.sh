#!/bin/bash
# K2: the KNOWN fail-open behaviours of fanotify (filer spike A7, A8, A9), asserted as the
# expected current behaviour. They are why the product needs the fd store, FAN_UNLIMITED_QUEUE
# and the start ordering. If one of these changes (for better or worse), this check fails so a
# human reviews the stub guard design against the new kernel.
#   K2a release with events queued (unread)        -> pending readers are ALLOWed: zeros
#   K2b release with events read but unanswered    -> pending readers are ALLOWed: zeros
#   K2c no FAN_UNLIMITED_QUEUE, queue overflow     -> the overflowing readers are ALLOWed: zeros
#   K2d fd opened before any group existed         -> unguarded for its lifetime: zeros
. "$(dirname "$0")/lib.sh"
begin "K2 known fail-open facts (expected current behaviour; a change needs review)"
F=$DATA/seis/line.segy
CHANGED="kernel behaviour CHANGED: review the stub guard's fail-closed design (docs/pilots/filer-spike-results-A.md)"

mkstubs; policy wait; delay 0

section "K2a group released with a permission event still queued (daemon paused)"
touch "$STATE/pause"
start_plain --pause-file "$STATE/pause" || end_check
start_reader a 60 check "$F" --off 300M --len 1M --seed 2
R=$RPID; sleep 2
st=$(reader_state $R); evidence "reader before release: state=${st%% *} wchan=${st#* }"
expect "reader blocked before release" "${st%% *}" D
kill_plain
finish_reader a $R
expect "reader after the last group fd closed" "$VERDICT" ZEROS "$CHANGED"
rm -f "$STATE/pause"

section "K2b group released with a permission event read but not answered (daemon stalled)"
start_plain --stall || end_check
start_reader b 60 check "$F" --off 400M --len 1M --seed 2
R=$RPID
if wait_until 10 grep -q "STALL: read event" "$GLOG"; then evidence "$(grep -m 1 'STALL: read event' "$GLOG" | cut -c 1-140)"
else fail "the stalled guard never read the event"; fi
kill_plain
finish_reader b $R
expect "reader after the last group fd closed" "$VERDICT" ZEROS "$CHANGED"

section "K2c queue overflow without FAN_UNLIMITED_QUEUE"
# fs.fanotify.max_queued_events is read when the group is created. Lowered here so the overflow
# needs 1,500 readers instead of 17,000; K4 floods past the default 16,384 with the flag set.
Q=${K2_QUEUE:-1024}; N=${K2_READERS:-1500}
sysctl -n fs.fanotify.max_queued_events > "$A/max_queued_events.orig"
mkstubs
sysctl -q -w fs.fanotify.max_queued_events="$Q"
touch "$STATE/pause"
start_plain --pause-file "$STATE/pause" --no-unlimited || end_check
sysctl -q -w fs.fanotify.max_queued_events="$(cat "$A/max_queued_events.orig")"; rm -f "$A/max_queued_events.orig"
evidence "group created with max_queued_events=$Q, no FAN_UNLIMITED_QUEUE; $N readers of distinct holes"
FL=$LOGS/$CHECK-flood.out
( ulimit -n 65536; exec timeout -s KILL 150 "$T" flood "$F" "$N" 64K 8M --seed 2 --timeout 120 ) > "$FL" 2>&1 &
FP=$!
settled() { local l; l=$(grep '^t=' "$FL" | tail -n 1); [ "$(field started "$l")" = "$N" ] && [ "$(field still_blocked "$l")" = "$Q" ]; }
wait_until 60 settled
l=$(grep '^t=' "$FL" | tail -n 1)
evidence "while paused: $l"
expect "readers that got zeros at once (N - queue limit)" "$(field completed_ZEROS "$l")" "$((N - Q))" "$CHANGED"
expect "readers blocked (= queue limit)" "$(field still_blocked "$l")" "$Q" "$CHANGED"
rm -f "$STATE/pause"
wait $FP
l=$(grep '^t=' "$FL" | tail -n 1)
evidence "after the guard drained the queue: $l"
expect "queued readers answered with correct bytes" "$(field completed_ok "$l")" "$Q"
if grep -q "FAN_Q_OVERFLOW" "$GLOG"; then pass "guard received FAN_Q_OVERFLOW"; else fail "no FAN_Q_OVERFLOW event: $CHANGED"; fi
stop_plain

section "K2d fd opened while the filesystem had no pre-content group"
mkstubs
rm -f "$STATE/go"
start_reader d 60 delayed "$F" "$STATE/go" 1500M 1M --seed 2
R=$RPID
wait_until 10 grep -q "opened" "$LOGS/$CHECK-d.out" || fail "pre-opening reader did not start"
start_plain || end_check
touch "$STATE/go"
finish_reader d $R
expect "read through the fd opened before the group existed" "$VERDICT" ZEROS "$CHANGED"
read_now 30 "$F" --off 1501M --len 1M --seed 2
expect "same file through a NEW open after arming (guard works)" "$VERDICT" OK
rm -f "$STATE/go"
stop_plain
end_check
