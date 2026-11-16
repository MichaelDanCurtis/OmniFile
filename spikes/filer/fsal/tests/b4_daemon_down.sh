#!/bin/bash
# B4: the daemon is unreachable -> FSAL_OMNI fails closed, never serves the hole.
. /work/spikes/filer/fsal/tests/lib.sh
T=$SPK/tests
now() { python3 -c 'import time;print(time.time())'; }
lg() { grep "OMNI read" $LOG | tail -${2:-1} | sed 's/.*OMNI/    OMNI/' | cut -c 1-${1:-400}; }
ganesha_start >/dev/null || exit 1
mkstub $B/data/wait/r.segy wait-r $SIZE $RES --resident-file
for v in 3 4.2; do
	tag=$(echo $v | tr -d .)
	echo "=== NFS v$v"
	mkstub $B/data/wait/d$tag.segy wait-d$tag $SIZE $RES
	mkstub $B/data/failfast/d$tag.segy failfast-d$tag $SIZE $RES
	mount_v $v /omni/wait $MNT/w$tag; mount_v $v /omni/failfast $MNT/f$tag
	# 1. no daemon at all (socket path missing -> ENOENT)
	daemon_stop
	echo "-- daemon stopped, socket removed"
	echo -n "  ls/stat still work: "; stat -c "%n size=%s" $MNT/w$tag/d$tag.segy
	echo -n "  resident file, full read: "; python3 $T/reader.py seq $MNT/w$tag/r.segy $B/backend/wait-r
	echo -n "  stub header (resident range, O_DIRECT): "; python3 $T/reader.py seq $MNT/w$tag/d$tag.segy $B/backend/wait-d$tag --direct --count $RES
	echo -n "  failfast stub @8MiB: "; python3 $T/reader.py one $MNT/f$tag/d$tag.segy $B/backend/failfast-d$tag --start $((8*1024*1024))
	lg 200
	echo "  wait stub @8MiB: reader started in background; daemon comes back after 12s"
	python3 $T/reader.py seq $MNT/w$tag/d$tag.segy $B/backend/wait-d$tag --start $((8*1024*1024)) > $B/b4.out 2>&1 &
	rp=$!; sleep 12
	echo "    after 12s reader state=$(awk '{print $3}' /proc/$rp/stat 2>/dev/null) (still blocked, no bytes returned); log:"
	lg 200; echo "    fail-closed log lines so far: $(grep -c "unreachable" $LOG)"
	daemon_start --recall-delay 3
	wait $rp; echo -n "    reader result after daemon restart: "; cat $B/b4.out
	# 2. daemon killed -9 but socket file left behind (ECONNREFUSED)
	mkstub $B/data/failfast/e$tag.segy failfast-e$tag $SIZE $RES
	kill -9 $(cat $B/daemon.pid); sleep 0.5; ls -l /run/omnifile/fsal.sock | sed 's/^/  stale socket: /'
	echo -n "  failfast stub, daemon SIGKILLed (stale socket): "; python3 $T/reader.py one $MNT/f$tag/e$tag.segy $B/backend/failfast-e$tag --start $((8*1024*1024))
	lg 200
	# 3. daemon accepts but never answers (timeout 2000 ms)
	daemon_start --hang
	mkstub $B/data/failfast/h$tag.segy failfast-h$tag $SIZE $RES
	echo -n "  failfast stub, daemon hung: "; python3 $T/reader.py one $MNT/f$tag/h$tag.segy $B/backend/failfast-h$tag --start $((8*1024*1024))
	lg 200
	daemon_stop
	# 4. unknown stub state -> fail closed without asking
	mkstub $B/data/failfast/x$tag.segy failfast-x$tag $SIZE $RES
	setfattr -n user.omnifile.state -v garbage $B/data/failfast/x$tag.segy
	echo -n "  failfast, state=garbage: "; python3 $T/reader.py one $MNT/f$tag/x$tag.segy $B/backend/failfast-x$tag --start 0 --bs 4096
	grep "cannot read stub xattrs" $LOG | tail -1 | sed 's/.*OMNI/    OMNI/'
	umount_all
done
