#!/bin/bash
# A1-A4: resident header read, wait, failfast, waitquick. Plain guard (no systemd).
. "$(dirname "$0")/lib.sh"
release_unit; stop_plain
: > $LOGS/guard-plain.log
policy wait; delay 0
mkstubs
dropcache
start_plain

say "baseline: without a guard the stub body reads as zeros (what we must prevent)"
release_unit; stop_plain
$T check $DATA/seis/small.segy --off 8M --len 1M --seed 1
start_plain

say "A1: resident header range (first 1 MiB), expect no fill"
before=$(grep -c "filled" $LOGS/guard-plain.log)
$T check $DATA/seis/small.segy --off 0 --len 1M --seed 1
$T check $DATA/seis/small.segy --off 4096 --len 3600 --seed 1
after=$(grep -c "filled" $LOGS/guard-plain.log)
echo "fill log lines before=$before after=$after (equal => served locally)"

say "A2: read past headers, policy wait, backend delay 2000 ms"
policy wait; delay 2000
$T check $DATA/seis/small.segy --off 20M --len 2M --seed 1
grep "filled" $LOGS/guard-plain.log | tail -1
say "A2b: whole-file read (cat-like) of the 2G stub with wait, delay 0"
delay 0
$T check $DATA/seis/line_2g.segy --seed 2
xa $DATA/seis/line_2g.segy

say "A3: failfast"
mkstubs; dropcache
policy failfast; delay 3000
$T check $DATA/seis/small.segy --off 30M --len 1M --seed 1
tail -2 $STATE/recalls.log
echo "retry immediately:"; $T check $DATA/seis/small.segy --off 30M --len 1M --seed 1
echo "retry after the background recall (4s):"; sleep 4; $T check $DATA/seis/small.segy --off 30M --len 1M --seed 1
echo "cat of a failfast stub:"; cat $DATA/seis/small.segy > /dev/null; echo "cat exit=$?"

say "A4: waitquick:1500 with backend delay 500 ms (under) and 3000 ms (over)"
mkstubs; dropcache
policy waitquick:1500; delay 500
$T check $DATA/seis/small.segy --off 10M --len 1M --seed 1
delay 3000
$T check $DATA/seis/small.segy --off 40M --len 1M --seed 1
grep waitquick $LOGS/guard-plain.log | tail -2
policy wait; delay 0
release_unit; stop_plain
