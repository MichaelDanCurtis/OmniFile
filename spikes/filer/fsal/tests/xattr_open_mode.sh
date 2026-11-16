#!/bin/bash
# Finding: FSAL_VFS getextattr_value_by_name() opens the file O_RDWR to read an
# xattr. On an immutable (chattr +i) file that open fails with EPERM, so the
# guard cannot tell whether the file is a stub and fails closed - for a plain,
# non-stub file. wait share: DELAY forever; failfast share: EIO.
. /work/spikes/filer/fsal/tests/lib.sh
ganesha_start >/dev/null || exit 1
for s in failfast wait; do
	echo "plain text, not a stub" > $B/data/$s/imm.txt; chattr +i $B/data/$s/imm.txt
	mount_v 4.2 /omni/$s $MNT/im$s
	echo -n "$s share, immutable non-stub file: "
	timeout 10 cat $MNT/im$s/imm.txt 2>&1; rc=$?; [ $rc = 124 ] && echo "(still blocked after 10 s)"
	grep "imm.txt" $LOG | tail -1 | sed 's/.*OMNI/  OMNI/'
	umount -f -l $MNT/im$s; chattr -i $B/data/$s/imm.txt; rm -f $B/data/$s/imm.txt
done
