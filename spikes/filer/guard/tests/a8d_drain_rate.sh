#!/bin/bash
# A8d: drain rate vs number of blocked readers (all answers wake every waiter on the group's
# single access_waitq -> O(N) wakeups per response).
. "$(dirname "$0")/lib.sh"
release_unit; stop_plain; policy wait; delay 0
for N in 250 1000 4000; do
  mkstubs >/dev/null; dropcache; : > $LOGS/guard-plain.log
  # warm the stub fully first so every event is a pure "allow" (no fill cost at all)
  start_plain; $T check $DATA/seis/line_2g.segy --seed 2 >/dev/null
  touch $STATE/pause; stop_plain; start_plain --pause-file $STATE/pause
  ( ulimit -n 65536; $T flood $DATA/seis/line_2g.segy $N 64K 8M --seed 2 --timeout 300 ) > $LOGS/a8d.out &
  F=$!
  until grep -q "started=$N " $LOGS/a8d.out; do sleep 0.5; done; sleep 2
  t0=$(date +%s.%N); rm -f $STATE/pause; wait $F; t1=$(date +%s.%N)
  echo "N=$N blocked readers (pure allow): drained in $(echo "$t1 - $t0" | bc) s -> $(echo "$N / ($t1 - $t0)" | bc) answers/s; $(tail -1 $LOGS/a8d.out)"
  stop_plain
done
