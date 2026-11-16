#!/bin/bash
# B2 extras (NFSv4.2): SEEK on stubs, server-side COPY, mmap, attributes.
. /work/spikes/filer/fsal/tests/lib.sh
T=$SPK/tests
now() { python3 -c 'import time;print(time.time())'; }
ganesha_start >/dev/null || exit 1
daemon_start --recall-delay 8
mkstub $B/data/wait/seek.segy wait-seek $SIZE $RES
# the same sparse layout, but on the plain VFS export, to show what an unguarded server says
python3 - <<PY
import os
src="/srv/omni-b/backend/wait-seek"; dst="/srv/omni-b/plain/sparse.segy"
with open(src,"rb") as fi, open(dst,"wb") as fo:
    fo.write(fi.read($RES)); fo.truncate($SIZE)
PY
mount_v 4.2 /omni/wait $MNT/w42; mount_v 4.2 /plain $MNT/p42
t=$(now)
echo -n "SEEK via FSAL_OMNI (stub):     "; python3 $T/reader.py seek $MNT/w42/seek.segy
echo -n "SEEK via plain VFS (same holes): "; python3 $T/reader.py seek $MNT/p42/sparse.segy
echo -n "  daemon (SEEK must not recall): "; python3 $T/dstats.py $t
grep "OMNI seek" $LOG | tail -3 | cut -c 100-
echo "--- attributes of a stub through the FSAL"
stat -c "  stat: size=%s blocks=%b mtime=%y" $MNT/w42/seek.segy
du -k $MNT/w42/seek.segy | sed 's/^/  du -k: /'
ls -l $MNT/w42 | sed 's/^/  ls: /'
echo "--- server-side COPY (copy_file_range) of a stub, wait share"
t=$(now)
python3 - <<PY
import os, time, errno
src="$MNT/w42/seek.segy"; dst="$MNT/w42/seek.copy"
fi=os.open(src,os.O_RDONLY); fo=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o644)
t=time.monotonic(); n=0
try:
    while True:
        c=os.copy_file_range(fi,fo,$SIZE-n)
        if c==0: break
        n+=c
    print("  copy_file_range copied", n, "in", round(time.monotonic()-t,2), "s")
except OSError as e:
    print("  copy_file_range error", errno.errorcode[e.errno], "after", round(time.monotonic()-t,2), "s, copied", n)
PY
cmp $MNT/w42/seek.copy $B/backend/wait-seek && echo "  copy == backend (correct bytes)" || echo "  COPY MISMATCH"
echo -n "  daemon: "; python3 $T/dstats.py $t
grep -c "COPY\|copy" $LOG >/dev/null
echo "--- server-side COPY of a stub, failfast share"
daemon_start --recall-delay 30
mkstub $B/data/failfast/cp.segy failfast-cp $SIZE $RES
mount_v 4.2 /omni/failfast $MNT/f42
t=$(now)
python3 - <<PY
import os, time, errno
src="$MNT/f42/cp.segy"; dst="$MNT/f42/cp.copy"
fi=os.open(src,os.O_RDONLY); fo=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o644)
t=time.monotonic(); n=0
try:
    while True:
        c=os.copy_file_range(fi,fo,$SIZE-n)
        if c==0: break
        n+=c
    print("  copy_file_range copied", n, "in", round(time.monotonic()-t,2), "s")
except OSError as e:
    print("  copy_file_range error", errno.errorcode[e.errno], "after", round(time.monotonic()-t,2), "s, copied", n)
PY
python3 - <<PY
d=open("$B/data/failfast/cp.copy","rb").read()
import sys
z=sum(1 for i in range($RES, len(d), 4096) if d[i:i+4096]==bytes(4096))
print("  destination size", len(d), "zero 4K blocks past headers:", z)
PY
echo -n "  daemon: "; python3 $T/dstats.py $t
echo "--- mmap of a stub past the headers, failfast share"
mkstub $B/data/failfast/mm.segy failfast-mm $SIZE $RES
python3 - <<PY
import mmap, os, signal, sys, time, subprocess
code = '''
import mmap, os
fd=os.open("$MNT/f42/mm.segy", os.O_RDONLY)
m=mmap.mmap(fd, 0, prot=mmap.PROT_READ)
print("header byte", m[0]); print("stub byte", m[8*1024*1024])
'''
r=subprocess.run([sys.executable,"-c",code],capture_output=True,text=True,timeout=60)
print("  exit", r.returncode, "(-7 = SIGBUS)", r.stdout.strip().replace("\n","; "), r.stderr.strip()[-120:])
PY
umount_all; daemon_stop
