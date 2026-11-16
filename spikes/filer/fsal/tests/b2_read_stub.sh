#!/bin/bash
# B2: NFS reads of a stub, per NFS version, for wait / failfast / waitquick.
# Usage: b2_read_stub.sh [versions...]   (default: 3 4.0 4.1 4.2)
. /work/spikes/filer/fsal/tests/lib.sh
T=$SPK/tests
VERS=${@:-3 4.0 4.1 4.2}
now() { python3 -c 'import time;print(time.time())'; }
fresh() { # fresh <share> <name>
	mkstub $B/data/$1/$2 $1-$2 $SIZE $RES
}
ganesha_start >/dev/null || exit 1
for v in $VERS; do
	tag=$(echo $v | tr -d .)
	echo "=== NFS v$v"
	# --- header reads never contact the daemon -------------------------
	daemon_start --recall-delay 10
	fresh wait hdr$tag.segy
	mount_v $v /omni/wait $MNT/w$tag
	t=$(now)
	echo -n "hdr O_DIRECT 4MiB:        "; python3 $T/reader.py seq $MNT/w$tag/hdr$tag.segy $B/backend/wait-hdr$tag.segy --direct --count $RES
	sleep 1; echo -n "  daemon: "; python3 $T/dstats.py $t
	umount -f -l $MNT/w$tag; mount_v $v /omni/wait $MNT/w$tag
	t=$(now)
	echo -n "hdr buffered 1MiB:        "; python3 $T/reader.py seq $MNT/w$tag/hdr$tag.segy $B/backend/wait-hdr$tag.segy --count $((1024*1024))
	sleep 2; echo -n "  daemon: "; python3 $T/dstats.py $t
	umount -f -l $MNT/w$tag; mount_v $v /omni/wait $MNT/w$tag
	t=$(now)
	echo -n "hdr buffered 4MiB (all):  "; python3 $T/reader.py seq $MNT/w$tag/hdr$tag.segy $B/backend/wait-hdr$tag.segy --count $RES
	sleep 2; echo -n "  daemon: "; python3 $T/dstats.py $t
	umount -f -l $MNT/w$tag
	# --- wait: whole-file sequential read ------------------------------
	daemon_start --recall-delay 10
	fresh wait w$tag.segy
	mount_v $v /omni/wait $MNT/w$tag
	ls -l $MNT/w$tag/w$tag.segy | sed 's/^/  ls: /'
	t=$(now)
	echo -n "wait seq read (recall 10s): "; timeout 120 python3 $T/reader.py seq $MNT/w$tag/w$tag.segy $B/backend/wait-w$tag.segy
	echo -n "  daemon: "; python3 $T/dstats.py $t
	umount -f -l $MNT/w$tag
	# --- wait: is a blocked reader killable? ---------------------------
	daemon_start --recall-delay 30
	fresh wait k$tag.segy
	mount_v $v /omni/wait $MNT/w$tag
	python3 $T/reader.py seq $MNT/w$tag/k$tag.segy $B/backend/wait-k$tag.segy --start $((8*1024*1024)) > /dev/null 2>&1 &
	rp=$!; sleep 3
	echo "  blocked reader state: $(awk '{print $3}' /proc/$rp/stat 2>/dev/null) wchan=$(cat /proc/$rp/wchan 2>/dev/null)"
	kill -INT $rp; sleep 1
	if kill -0 $rp 2>/dev/null; then kill -KILL $rp; sleep 2
		if kill -0 $rp 2>/dev/null; then echo "  SIGINT+SIGKILL: still alive after 2s"; else echo "  SIGINT ignored; SIGKILL killed it"; fi
	else echo "  SIGINT killed it"; fi
	umount -f -l $MNT/w$tag
	# --- failfast -------------------------------------------------------
	daemon_start --recall-delay 6
	fresh failfast f$tag.segy
	mount_v $v /omni/failfast $MNT/f$tag
	t=$(now)
	echo -n "failfast pread@8MiB x3 (gap 4s), then fresh open: "
	python3 $T/reader.py one $MNT/f$tag/f$tag.segy $B/backend/failfast-f$tag.segy --start $((8*1024*1024)) --repeat 3 --gap 4
	echo -n "  daemon: "; python3 $T/dstats.py $t
	fresh failfast g$tag.segy
	t=$(now)
	echo -n "failfast dd bs=1M skip=8: "; dd if=$MNT/f$tag/g$tag.segy of=/dev/null bs=1M skip=8 count=1 2>&1 | head -1
	umount -f -l $MNT/f$tag
	# --- waitquick:5000 -------------------------------------------------
	for rd in 3 10; do
		daemon_start --recall-delay $rd
		fresh quick q$rd$tag.segy
		mount_v $v /omni/quick $MNT/q$tag
		t=$(now)
		echo -n "waitquick:5000, recall ${rd}s: "; timeout 60 python3 $T/reader.py one $MNT/q$tag/q$rd$tag.segy $B/backend/quick-q$rd$tag.segy --start $((8*1024*1024))
		echo -n "  daemon: "; python3 $T/dstats.py $t
		umount -f -l $MNT/q$tag
	done
done
daemon_stop
