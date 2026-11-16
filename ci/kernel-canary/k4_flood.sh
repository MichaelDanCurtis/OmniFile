#!/bin/bash
# K4: with FAN_UNLIMITED_QUEUE, more pending permission events than the default queue limit
# (fs.fanotify.max_queued_events = 16,384) all stay blocked: no reader gets zeros. The guard is
# paused (holds the group, reads nothing), which is what "daemon dead, group held" looks like.
# Then it resumes and drains; the drain rate is recorded (risk R6: O(N) wake-ups) but only a
# zero read fails the check.
. "$(dirname "$0")/lib.sh"
begin "K4 FAN_UNLIMITED_QUEUE: a flood above 16,384 pending readers gets no zeros"
F=$DATA/seis/line.segy
N=${K4_READERS:-17000}
DRAIN_BUDGET=${K4_DRAIN_BUDGET:-$(scaled 420)}

# room for N reader threads (a disposable runner; the values are not restored)
[ "$(sysctl -n kernel.pid_max)" -ge $((N * 3)) ] || sysctl -q -w kernel.pid_max=4194304
[ "$(sysctl -n kernel.threads-max)" -ge $((N * 3)) ] || sysctl -q -w kernel.threads-max=$((N * 4))
limit=$(sysctl -n fs.fanotify.max_queued_events)
evidence "fs.fanotify.max_queued_events=$limit; readers N=$N; kernel.pid_max=$(sysctl -n kernel.pid_max), kernel.threads-max=$(sysctl -n kernel.threads-max)"
if [ "$N" -le "$limit" ]; then fail "N=$N does not exceed the queue limit $limit: the check would prove nothing"; end_check; fi

mkstubs; policy wait; delay 0
touch "$STATE/pause"
start_plain --pause-file "$STATE/pause" || end_check
grep -m 1 "created group" "$GLOG" | cut -c 1-140 | while read -r l; do evidence "$l"; done

section "K4a $N readers of distinct holes while the guard reads nothing"
FL=$LOGS/$CHECK-flood.out
# Each reader is a thread. The job's own cgroup (the runner service) has a TasksMax far below
# N, and exhausting it starves the runner itself, so the flood runs in its own transient scope.
systemd-run --scope --quiet --unit="canary-flood-$$" -p TasksMax=infinity -- \
  bash -c "ulimit -n 65536; exec timeout -s KILL $((DRAIN_BUDGET + 300)) $T flood $F $N 64K 8M --seed 2 --timeout $((DRAIN_BUDGET + 240))" > "$FL" 2>&1 &
FP=$!
wait_until 10 systemctl is-active --quiet "canary-flood-$$.scope" && evidence "flood scope: TasksMax=$(systemctl show -p TasksMax --value "canary-flood-$$.scope")"
last() { grep '^t=' "$FL" | tail -n 1; }
all_started() { [ "$(field started "$(last)")" = "$N" ]; }
wait_until "$(scaled 180)" all_started || fail "only $(field started "$(last)") of $N readers started within $(scaled 180) s"
sleep 4   # let the last progress line settle
l=$(last)
evidence "paused: $l"
expect "readers that got zeros" "$(field completed_ZEROS "$l")" 0 "queue overflow ALLOWed reads despite FAN_UNLIMITED_QUEUE"
expect "readers that completed while nobody answered" "$(field completed_ok "$l")" 0
expect "readers blocked" "$(field still_blocked "$l")" "$N"
if grep -q "FAN_Q_OVERFLOW" "$GLOG"; then fail "FAN_Q_OVERFLOW seen with FAN_UNLIMITED_QUEUE"; else pass "no FAN_Q_OVERFLOW"; fi

section "K4b resume the guard and drain"
rm -f "$STATE/pause"
t0=$(date +%s)
done_all() { local x; x=$(last); [ $(($(field completed_ok "$x") + $(field completed_ZEROS "$x") + $(field errors "$x"))) -ge "$N" ]; }
wait_until "$DRAIN_BUDGET" done_all
dt=$(($(date +%s) - t0)); sleep 3
l=$(last)
evidence "after ${dt} s of drain: $l"
z=$(field completed_ZEROS "$l"); ok=$(field completed_ok "$l"); e=$(field errors "$l")
expect "readers that got zeros at any point" "$z" 0 "fail-OPEN under load"
if [ "$e" -gt 0 ]; then warn "$e reader(s) got an error (fail-closed)"; fi
if [ $((ok + z + e)) -ge "$N" ]; then
  evidence "drained $N events in ${dt} s (about $((N / (dt > 0 ? dt : 1))) answers/s; R6 tracks the O(N) wake-up)"
else
  warn "drain incomplete after ${DRAIN_BUDGET} s: $ok of $N answered; remaining readers killed (SIGKILL cancels their events)"
fi
{ pkill -9 -x omnitool; wait $FP; } 2> /dev/null
stop_plain
end_check
