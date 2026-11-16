#!/bin/bash
# A13 (clean numbers): warm-cache sequential read of a resident 4 GiB file, 5 runs per cell, median.
. "$(dirname "$0")/lib.sh"
release_unit; stop_plain
F=$DATA/res/big.bin; mkdir -p $DATA/res
rm -f $F; $T gen $F 4G --seed 9
setfattr -n user.omnifile.state -v resident $F
setfattr -n user.omnifile.resident_bytes -v $(stat -c %s $F) $F
setfattr -n user.omnifile.key -v big.bin $F
sync; sleep 5
cat $F > /dev/null   # warm
med() { sort -n | awk '{a[NR]=$1} END {print a[int((NR+1)/2)]}'; }
rate() { dd if=$F of=/dev/null bs=$1 2>&1 | tail -1 | sed 's/.*s, //' | awk '{v=$1; u=$2; if(u=="GB/s") v*=1000; if(u=="kB/s") v/=1000; print v}'; }
cell() { for i in 1 2 3 4 5; do rate $1; done | med; }
row() { printf "%-34s %10s %10s %10s\n" "$1" "$(cell 1M)" "$(cell 64K)" "$(cell 4K)"; }
printf "%-34s %10s %10s %10s   (MB/s, median of 5, warm page cache)\n" mode bs=1M bs=64K bs=4K
row "no pre-content group"
: > $LOGS/guard-plain.log; start_plain; row "sb mark, no ignore mark"; stop_plain
: > $LOGS/guard-plain.log; start_plain --ignore-filled; cat $F >/dev/null; row "sb mark + inode IGNORE mark"
grep -c "event fd\|ignore mark on" $LOGS/guard-plain.log >/dev/null; stop_plain
row "no pre-content group (again)"
rm -f $F
