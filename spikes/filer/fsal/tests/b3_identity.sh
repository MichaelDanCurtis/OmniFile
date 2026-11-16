#!/bin/bash
# B3: caller uid/gid and client IP reach the FSAL with sec=sys.
# Reads run as uid 2001 / gid 3001 over the VM's non-loopback address.
. /work/spikes/filer/fsal/tests/lib.sh
T=$SPK/tests
IP=$(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -1)
now() { python3 -c 'import time;print(time.time())'; }
ganesha_start >/dev/null || exit 1
daemon_start --recall-delay 3
echo "server/client address used: $IP"
for v in 3 4.2; do
	tag=$(echo $v | tr -d .)
	mkstub $B/data/wait/id$tag.segy wait-id$tag $SIZE $RES
	mkdir -p $MNT/id$tag; umount -f -l $MNT/id$tag 2>/dev/null
	if [ $v = 3 ]; then
		mount -t nfs -o vers=3,nolock,sec=sys $IP:$B/data/wait $MNT/id$tag
	else
		mount -t nfs -o vers=4.2,sec=sys $IP:/omni/wait $MNT/id$tag
	fi
	t=$(now)
	echo -n "v$v read as uid 2001: "
	setpriv --reuid=2001 --regid=3001 --clear-groups python3 $T/reader.py one $MNT/id$tag/id$tag.segy $B/backend/wait-id$tag --start $((8*1024*1024)) --bs 65536
	echo -n "  daemon saw: "; python3 $T/dstats.py $t
	echo "  ganesha log:"; grep "OMNI read" $LOG | grep "id$tag.segy" | tail -2 | sed 's/.*OMNI/    OMNI/'
	umount -f -l $MNT/id$tag
done
# identity in a server-side COPY (NFSv4.2), as uid 2001
mkstub $B/data/wait/idcp.segy wait-idcp $SIZE $RES
chown 2001:3001 $B/data/wait; mount -t nfs -o vers=4.2 $IP:/omni/wait $MNT/id42
t=$(now)
setpriv --reuid=2001 --regid=3001 --clear-groups python3 -c "
import os
fi=os.open('$MNT/id42/idcp.segy',os.O_RDONLY); fo=os.open('$MNT/id42/idcp.copy',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o644)
n=0
while n < $SIZE:
    c=os.copy_file_range(fi,fo,$SIZE-n)
    if c==0: break
    n+=c
print('COPY as uid 2001 copied', n)"
echo -n "  daemon saw (COPY): "; python3 $T/dstats.py $t
umount -f -l $MNT/id42; chown root:root $B/data/wait
daemon_stop
