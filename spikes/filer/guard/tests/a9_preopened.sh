#!/bin/bash
# A9: files opened before the mark is armed.
. "$(dirname "$0")/lib.sh"
release_unit; stop_plain; : > $LOGS/guard-plain.log
policy wait; delay 0; mkstubs; dropcache; rm -f $STATE/go

say "A9a: fd opened with NO group on the filesystem, guard armed afterwards, then read via that fd"
$T delayed $DATA/seis/line_2g.segy $STATE/go 100M 1M --seed 2 > $LOGS/a9a.out &
R=$!; sleep 0.5
start_plain
touch $STATE/go; wait $R; cat $LOGS/a9a.out; rm -f $STATE/go
echo "same range through a NEW open after arming:"
$T check $DATA/seis/line_2g.segy --off 101M --len 1M --seed 2

say "A9b: fd opened while group #1 armed; group #1 dies (plain kill), group #2 armed; read via the old fd"
$T delayed $DATA/seis/line_2g.segy $STATE/go 300M 1M --seed 2 > $LOGS/a9b.out &
R=$!; sleep 0.5
stop_plain; start_plain
touch $STATE/go; wait $R; cat $LOGS/a9b.out; rm -f $STATE/go

say "A9c: fd opened while armed; group dies; read via the old fd with NO group at all"
$T delayed $DATA/seis/line_2g.segy $STATE/go 500M 1M --seed 2 > $LOGS/a9c.out &
R=$!; sleep 0.5
stop_plain
touch $STATE/go; wait $R; cat $LOGS/a9c.out; rm -f $STATE/go
