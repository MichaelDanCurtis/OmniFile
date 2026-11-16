#!/bin/bash
# Threading: a hung daemon holds a Ganesha RPC thread for Timeout_Ms per stub READ.
# Measure O_DIRECT 1 MiB read latency on the plain export while N readers are stuck
# on stubs of the wait share (NFSv4.2), with the daemon answering (baseline) or hung.
. /work/spikes/filer/fsal/tests/lib.sh
N=${N:-32}
ganesha_start >/dev/null || exit 1
GP=$(cat $PIDF)
python3 - <<PY
open("$B/plain/lat.bin","wb").write(bytes(range(256))*(256*1024))
PY
for i in $(seq 1 $N); do [ -f $B/data/wait/t$i.segy ] || mkstub $B/data/wait/t$i.segy wait-t$i $SIZE $RES; done
mount_v 4.2 /omni/wait $MNT/tw; mount_v 4.2 /plain $MNT/tp
lat() {
	python3 - <<PY
import os, time, mmap, statistics
fd=os.open("$MNT/tp/lat.bin", os.O_RDONLY|os.O_DIRECT); b=mmap.mmap(-1,1<<20); L=[]
t_end=time.monotonic()+8
while time.monotonic()<t_end:
    for off in range(0, 64<<20, 1<<20):
        t=time.monotonic(); os.preadv(fd,[b],off); L.append(time.monotonic()-t)
L.sort(); print("reads=%d p50=%.1fms p99=%.1fms max=%.1fms" % (len(L), 1e3*L[len(L)//2], 1e3*L[int(len(L)*.99)], 1e3*L[-1]))
PY
}
for mode in answering hung; do
	for i in $(seq 1 $N); do mkstub $B/data/wait/t$i.segy wait-t$i $SIZE $RES; done
	if [ $mode = hung ]; then daemon_start --hang; else daemon_start --recall-delay 600; fi
	for i in $(seq 1 $N); do timeout 20 cat $MNT/tw/t$i.segy > /dev/null 2>&1 & done
	sleep 3
	echo "daemon $mode, $N stuck readers: ganesha threads=$(ls /proc/$GP/task | wc -l); plain-export latency: $(lat)"
	pkill -f "cat $MNT/tw/" ; daemon_stop; sleep 3
done
daemon_stop; echo "no stuck readers: $(lat)"
umount_all
