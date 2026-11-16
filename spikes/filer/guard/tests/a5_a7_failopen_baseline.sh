#!/bin/bash
# A5 + A7 (plain process, no fd holder): what readers see when the group dies.
. "$(dirname "$0")/lib.sh"
release_unit
stop_plain; : > $LOGS/guard-plain.log
mkstubs; dropcache

say "A5a: kill -9 while a reader is blocked (event READ by daemon, fill in progress)"
policy wait; delay 30000
start_plain
$T check $DATA/seis/line_2g.segy --off 100M --len 1M --seed 2 > $LOGS/a5a.out &
R=$!; sleep 2
echo "reader state before kill: $(cat /proc/$R/stat 2>/dev/null | awk '{print $3}') wchan=$(cat /proc/$R/wchan 2>/dev/null)"
grep -c "^B " $STATE/journal | sed 's/^/journal B records: /'
kill -9 $GPID; wait $R; cat $LOGS/a5a.out

say "A5b: kill -9 while idle, then read"
mkstubs; dropcache; delay 0
start_plain; kill -9 $GPID; sleep 0.5
$T check $DATA/seis/line_2g.segy --off 200M --len 1M --seed 2

say "A7a: group released with events still UNREAD in the notification queue (daemon paused)"
mkstubs; dropcache
touch $STATE/pause
start_plain --pause-file $STATE/pause
$T check $DATA/seis/line_2g.segy --off 300M --len 1M --seed 2 > $LOGS/a7a.out &
R=$!; sleep 2
echo "reader state: $(awk '{print $3}' /proc/$R/stat) wchan=$(cat /proc/$R/wchan)"
kill -9 $GPID; wait $R; cat $LOGS/a7a.out; rm -f $STATE/pause

say "A7b: group released with events READ but not answered (daemon stalled)"
start_plain --stall
$T check $DATA/seis/line_2g.segy --off 400M --len 1M --seed 2 > $LOGS/a7b.out &
R=$!; sleep 2
grep STALL $LOGS/guard-plain.log | tail -1
kill -9 $GPID; wait $R; cat $LOGS/a7b.out

say "A7c: same, but the group fd is closed by a clean exit (SIGTERM)"
start_plain --stall
$T check $DATA/seis/line_2g.segy --off 500M --len 1M --seed 2 > $LOGS/a7c.out &
R=$!; sleep 2
kill -TERM $GPID; wait $R; cat $LOGS/a7c.out
policy wait; delay 0
