#!/bin/bash
# A6: guard under systemd with the fd store. kill -9 must never let zeros through.
. "$(dirname "$0")/lib.sh"
U=omni-guard-a
stop_plain
systemctl stop $U 2>/dev/null; systemctl clean --what=fdstore $U 2>/dev/null
: > $STATE/guard.env
mkstubs; dropcache; policy wait; delay 0
jmark() { date '+%Y-%m-%d %H:%M:%S'; }
mainpid() { systemctl show -p MainPID --value $U; }
nstore() { systemctl show -p NFileDescriptorStore --value $U; }
T0=$(jmark)
systemctl start $U
echo "started: MainPID=$(mainpid) NFileDescriptorStore=$(nstore)"

say "A6a: kill -9 while a reader is blocked in a fill (orphan answered from the journal)"
delay 15000
$T check $DATA/seis/line_2g.segy --off 100M --len 1M --seed 2 > $LOGS/a6a.out &
R1=$!; sleep 2
cat $STATE/journal
P=$(mainpid); kill -9 $P; echo "killed $P at $(date +%T.%N)"
sleep 0.5
say "A6b: new reader while NO daemon is running (group held only by systemd)"
delay 1000
$T check $DATA/seis/line_2g.segy --off 300M --len 1M --seed 2 > $LOGS/a6b.out &
R2=$!; sleep 1
echo "R1 state=$(awk '{print $3}' /proc/$R1/stat 2>/dev/null) R2 state=$(awk '{print $3}' /proc/$R2/stat 2>/dev/null) wchan=$(cat /proc/$R2/wchan 2>/dev/null)"
wait $R1; cat $LOGS/a6a.out
wait $R2; cat $LOGS/a6b.out
echo "after restart: MainPID=$(mainpid) NFileDescriptorStore=$(nstore)"

say "A6c: 'systemctl stop' (fd preserved), reader waits 20 s, then start"
delay 0
systemctl stop $U; echo "stopped: state=$(systemctl is-active $U) NFileDescriptorStore=$(nstore)"
$T check $DATA/seis/line_2g.segy --off 500M --len 1M --seed 2 > $LOGS/a6c.out &
R3=$!; sleep 20
echo "after 20 s: reader state=$(awk '{print $3}' /proc/$R3/stat 2>/dev/null) wchan=$(cat /proc/$R3/wchan 2>/dev/null)"
systemctl start $U; wait $R3; cat $LOGS/a6c.out

say "A6d: kill -9 while idle, read immediately"
P=$(mainpid); kill -9 $P
$T check $DATA/seis/line_2g.segy --off 700M --len 1M --seed 2

say "A6e: repeated kill -9 during a stream of reads (10 kills, 1 s apart)"
delay 300
( for i in $(seq 0 60); do $T check $DATA/seis/line_2g.segy --off $((800 + i*16))M --len 64K --seed 2; done ) > $LOGS/a6e.out 2>&1 &
S=$!
for k in $(seq 1 10); do sleep 1; P=$(mainpid); [ "$P" != 0 ] && kill -9 $P; done
wait $S
echo "stream results: $(grep -c ': OK' $LOGS/a6e.out) OK, $(grep -c ZEROS $LOGS/a6e.out) ZEROS, $(grep -c ERROR $LOGS/a6e.out) ERROR"
grep -v ': OK' $LOGS/a6e.out | head

say "A6f: unjournalled orphan (predecessor crashed before journalling): --no-journal, kill, sweep"
systemctl stop $U; echo 'GUARD_ARGS=--no-journal' > $STATE/guard.env; systemctl start $U
delay 20000
$T check $DATA/seis/line_2g.segy --off 1800M --len 1M --seed 2 > $LOGS/a6f.out &
R=$!; sleep 2
: > $STATE/guard.env
P=$(mainpid); kill -9 $P; wait $R; cat $LOGS/a6f.out
echo "retry after sweep:"; delay 0; $T check $DATA/seis/line_2g.segy --off 1800M --len 1M --seed 2

say "guard journal for this test"
journalctl -u $U --since "$T0" -o cat | grep -E "REUSING|ORPHAN|orphan|sweep|READY|pushed|created group|fd store|filled|systemd passed" | head -60
delay 0
