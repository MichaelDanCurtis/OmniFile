#!/bin/bash
# A8: more than 16384 pending permission events, with and without FAN_UNLIMITED_QUEUE.
# The guard is paused (holds the group, reads nothing) = "daemon dead, fd held by systemd".
. "$(dirname "$0")/lib.sh"
N=${N:-17000}
release_unit
stop_plain; : > $LOGS/guard-plain.log
policy wait; delay 0
echo "fs.fanotify.max_queued_events=$(cat /proc/sys/fs/fanotify/max_queued_events)"
run() {  # $1 = extra guard args, $2 = guard nofile limit
  mkstubs >/dev/null; dropcache
  touch $STATE/pause
  ( ulimit -n $2; start_plain --pause-file $STATE/pause $1 )
  GPID=$(pgrep -x omnifile-guard)
  ( ulimit -n 65536; $T flood $DATA/seis/line_2g.segy $N 64K 8M --seed 2 --timeout ${FT:-25} ) > $LOGS/a8.out &
  F=$!
  sleep 22; echo "while paused:"; tail -1 $LOGS/a8.out
  rm -f $STATE/pause; echo "guard resumed"
  wait $F; tail -1 $LOGS/a8.out
  grep -E "OVERFLOW|failed|EMFILE" $LOGS/guard-plain.log | sort | uniq -c | head -5
  kill -9 $GPID; sleep 0.5
}
say "A8a: WITHOUT FAN_UNLIMITED_QUEUE, $N readers"
run --no-unlimited 65536
say "A8b: WITH FAN_UNLIMITED_QUEUE, $N readers (guard nofile 65536)"
: > $LOGS/guard-plain.log
run "" 65536
say "A8c: WITH FAN_UNLIMITED_QUEUE, guard nofile limit 1024 (event fds exhausted on drain)"
: > $LOGS/guard-plain.log
run "" 1024
