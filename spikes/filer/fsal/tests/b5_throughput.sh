#!/bin/bash
# B5: sequential read of a 2 GiB resident file: FSAL_OMNI vs FSAL_NULL vs plain FSAL_VFS.
# Client and server share one 8 GB VM (and another agent's workload), so wall-clock
# throughput is noisy. We therefore also record Ganesha's CPU time per GiB served,
# and count the extra syscalls per READ with strace.
. /work/spikes/filer/fsal/tests/lib.sh
SZ=$((2*1024*1024*1024)); RUNS=${RUNS:-5}
mkdir -p $B/data/null
[ -f $B/data/wait/big2.bin ] || mkstub $B/data/wait/big2.bin wait-big2 $SZ 0 --resident-file
[ -f $B/data/null/big2.bin ] || cp --sparse=never $B/backend/wait-big2 $B/data/null/big2.bin
[ -f $B/plain/big2.bin ] || cp --sparse=never $B/backend/wait-big2 $B/plain/big2.bin
ganesha_start >/dev/null || exit 1
daemon_start --recall-delay 3   # running, but a resident file must never contact it
GP=$(cat $PIDF)
cpu() { awk '{print $14+$15}' /proc/$GP/stat; }   # utime+stime, clock ticks
TCK=$(getconf CLK_TCK)
t0=$(python3 -c 'import time;print(time.time())')
echo "loadavg before: $(cat /proc/loadavg)"
for v in 3 4.2; do
	for run in $(seq 1 $RUNS); do
		for e in /omni/wait /null /plain; do
			case $e in /omni/wait) f=$B/data/wait/big2.bin;; /null) f=$B/data/null/big2.bin;; /plain) f=$B/plain/big2.bin;; esac
			cat $f > /dev/null
			d=$MNT/b5$(echo $e | tr / _); mount_v $v $e $d
			for mode in buffered direct; do
				fl=; [ $mode = direct ] && fl=iflag=direct
				c0=$(cpu)
				s=$(dd if=$d/big2.bin of=/dev/null bs=1M $fl 2>&1 | tail -1 | sed -E 's/.*copied, ([0-9.]+) s.*/\1/')
				c1=$(cpu)
				python3 -c "print('B5 v$v run$run $e $mode MBps=%.0f ganesha_cpu_s_per_GiB=%.3f' % ($SZ/1e6/$s, ($c1-$c0)/$TCK/2))"
			done
			umount -f -l $d
		done
	done
done
echo "loadavg after: $(cat /proc/loadavg)"
echo -n "daemon requests during B5: "; python3 $SPK/tests/dstats.py $t0
# syscalls Ganesha makes per 1 MiB READ (O_DIRECT client reads of 256 MiB)
for e in /omni/wait /plain; do
	d=$MNT/b5s; mount_v 4.2 $e $d
	case $e in /omni/wait) f=big2.bin;; /plain) f=big2.bin;; esac
	timeout 60 strace -f -c -p $GP -o $B/strace$(echo $e | tr / _).txt & sp=$!; sleep 1
	dd if=$d/$f of=/dev/null bs=1M count=256 iflag=direct 2>/dev/null
	sleep 1; kill -INT $sp; wait $sp 2>/dev/null
	echo "strace -c for 256 x 1 MiB READ via $e:"
	grep -E "openat|open_by_handle|fgetxattr|getxattr|close|pread|preadv|read |sendmsg|writev|total" $B/strace$(echo $e | tr / _).txt | awk '{print "   ", $4, $NF}'
	umount -f -l $d
done
daemon_stop
