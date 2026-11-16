#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# B5 (filer spike, ported): sequential reads of a resident file cost the same
# through FSAL_OMNI as through upstream FSAL_NULL and plain FSAL_VFS, and
# never contact the daemon. Containers on a laptop are noisy, so the bar is
# loose: OMNI must reach half of FSAL_NULL's throughput.
. "$(dirname "$0")/lib.sh"
setup_volumes
ganesha_start || exit 1
daemon_start
SZ=$((512 * MiB))
fid=5001
mkstub "$DATA/wait/res.segy" $fid $SZ $SZ --full
cp "$BACKEND/$fid" "$DATA/null/res.segy"
cp "$BACKEND/$fid" "$PLAIN/res.segy"
mount_v 4.2 wait "$MNT/o"
mount_v 4.2 null "$MNT/n"
mount_v 4.2 plain "$MNT/p"
t=$(now)
best() { # best <dir> <direct?> -> best MB/s of 3 runs
	local i r mbs best=0
	for i in 1 2 3; do
		sync
		echo 3 >/proc/sys/vm/drop_caches 2>/dev/null
		if [ "$2" = direct ]; then
			r=$(reader seq "$1/res.segy" "$BACKEND/$fid" --direct)
		else
			r=$(reader seq "$1/res.segy" "$BACKEND/$fid")
		fi
		jtest "$r" 'd["correct"]' || { fail "B5: read through $1 incorrect: $r"; echo 0; return; }
		mbs=$(echo "$r" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(int(d["bytes"] / max(d["total_s"], 0.01) / 1e6))')
		[ "$mbs" -gt "$best" ] && best=$mbs
	done
	echo $best
}
for mode in buffered direct; do
	o=$(best "$MNT/o" $mode)
	n=$(best "$MNT/n" $mode)
	p=$(best "$MNT/p" $mode)
	say "B5 $mode: OMNI $o MB/s, NULL $n MB/s, VFS $p MB/s"
	if [ "$o" -ge $((n / 2)) ]; then
		pass "B5 $mode: OMNI ($o MB/s) within noise of FSAL_NULL ($n MB/s) and VFS ($p MB/s)"
	else
		fail "B5 $mode: OMNI $o MB/s vs NULL $n MB/s"
	fi
done
n=$(daemon_count "$t" 'm.get("type") == "READ_INTENT"')
[ "$n" = 0 ] && pass "B5: the daemon was never contacted for resident reads" ||
	fail "B5: $n READ_INTENTs for a resident file"
finish
