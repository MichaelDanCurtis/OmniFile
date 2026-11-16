# Shared helpers for the FSAL_OMNI spike tests. Run as root inside the VM.
SPK=/work/spikes/filer/fsal
B=/srv/omni-b
MNT=$B/mnt
LOG=$B/ganesha.log
DLOG=$B/daemon.log
PIDF=$B/ganesha.pid

ganesha_start() {
	pkill -x ganesha.nfsd 2>/dev/null; sleep 1
	cp $SPK/ganesha-omni.conf $B/ganesha.conf
	: > $LOG
	LD_LIBRARY_PATH=/opt/ganesha/lib /opt/ganesha/bin/ganesha.nfsd \
		-f $B/ganesha.conf -L $LOG -p $PIDF -N NIV_EVENT
	for i in $(seq 1 30); do
		ss -ltn | grep -q ':2049 ' && return 0; sleep 0.5
	done
	echo "ganesha did not start"; tail -30 $LOG; return 1
}
ganesha_stop() { pkill -x ganesha.nfsd; sleep 1; }

daemon_start() { # args passed to fake_daemon.py
	daemon_stop
	setsid nohup python3 $SPK/fake_daemon.py "$@" >> $B/daemon.stdout 2>&1 < /dev/null &
	disown; echo $! > $B/daemon.pid
	for i in $(seq 1 20); do [ -S /run/omnifile/fsal.sock ] && return 0; sleep 0.2; done
	echo "daemon did not start"; return 1
}
daemon_stop() {
	[ -f $B/daemon.pid ] && kill -9 $(cat $B/daemon.pid) 2>/dev/null
	rm -f $B/daemon.pid /run/omnifile/fsal.sock
	sleep 0.3
}

# mount_v <vers> <export-pseudo> <dir>   vers: 3 | 4.0 | 4.1 | 4.2
mount_v() {
	mkdir -p $3
	umount -f -l $3 2>/dev/null
	if [ "$1" = 3 ]; then
		# NFSv3 mounts the export Path, not the NFSv4 Pseudo path
		local p=$2
		case $p in /omni/*) p=$B/data/${p#/omni/};; /plain) p=$B/plain;; /null) p=$B/data/null;; esac
		mount -t nfs -o vers=3,proto=tcp,nolock,mountproto=tcp,port=2049,actimeo=1 127.0.0.1:$p $3
	else
		mount -t nfs -o vers=$1,proto=tcp,actimeo=1 127.0.0.1:$2 $3
	fi
}
umount_all() { for d in $MNT/*; do umount -f -l $d 2>/dev/null; done; }

# stub geometry: 64 MiB file, 4 MiB resident headers
SIZE=$((64*1024*1024)); RES=$((4*1024*1024))
mkstub() { python3 $SPK/mkstub.py "$@"; }
