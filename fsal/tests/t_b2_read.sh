#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# B2 (filer spike, ported): NFS reads of a stub per NFS version, for the wait,
# failfast and waitquick policies, plus header reads, readahead and the
# retry storm. Never zeros; wait gets correct bytes; failfast gets EIO fast.
#   t_b2_read.sh [versions...]   default: 3 4.0 4.1 4.2
. "$(dirname "$0")/lib.sh"
VERS=${*:-3 4.0 4.1 4.2}
setup_volumes
ganesha_start || exit 1

fid=1000
for v in $VERS; do
	tag=$(echo "$v" | tr -d .)
	say "=== NFSv$v"
	fid=$((fid + 10))

	# --- header reads never contact the daemon ---------------------------
	daemon_start --recall-delay 3 || fail "v$v: daemon start"
	mkstub "$DATA/wait/hdr$tag.segy" $fid $SIZE $RES
	mount_v "$v" wait "$MNT/w$tag"
	t=$(now)
	r=$(reader seq "$MNT/w$tag/hdr$tag.segy" "$BACKEND/$fid" --direct --count $RES)
	sleep 1
	n=$(daemon_count "$t" 'm.get("type") == "READ_INTENT"')
	if jtest "$r" 'd["correct"]' && [ "$n" = 0 ]; then
		pass "v$v: O_DIRECT read of the 4 MiB resident header is correct, daemon not asked"
	else
		fail "v$v: O_DIRECT header read: $r, READ_INTENTs=$n"
	fi
	umount -f -l "$MNT/w$tag"
	mount_v "$v" wait "$MNT/w$tag"
	t=$(now)
	r=$(reader seq "$MNT/w$tag/hdr$tag.segy" "$BACKEND/$fid" --count $MiB)
	sleep 1.5
	n=$(daemon_count "$t" 'm.get("type") == "READ_INTENT"')
	if jtest "$r" 'd["correct"]' && [ "$n" = 0 ]; then
		pass "v$v: buffered 1 MiB header read is correct, daemon not asked"
	else
		fail "v$v: buffered 1 MiB header read: $r, READ_INTENTs=$n"
	fi
	umount -f -l "$MNT/w$tag"
	mount_v "$v" wait "$MNT/w$tag"
	t=$(now)
	r=$(reader seq "$MNT/w$tag/hdr$tag.segy" "$BACKEND/$fid" --count $RES)
	sleep 1.5
	n=$(daemon_count "$t" 'm.get("type") == "READ_INTENT" and m["off"] >= '$RES)
	jtest "$r" 'd["correct"]' || fail "v$v: buffered 4 MiB header read: $r"
	# Spike finding, kept visible: client readahead past the header asks
	# for the next rsize, which starts a recall.
	say "v$v: buffered read of the whole 4 MiB header -> $n READ_INTENT(s) past it (client readahead; spike finding)"
	umount -f -l "$MNT/w$tag"

	# --- wait: whole-file sequential read through a 3 s recall -------------
	fid=$((fid + 1))
	daemon_start --recall-delay 3
	mkstub "$DATA/wait/w$tag.segy" $fid $SIZE $RES
	mount_v "$v" wait "$MNT/w$tag"
	t=$(now)
	r=$(timeout 90 python3 "$T/reader.py" seq "$MNT/w$tag/w$tag.segy" "$BACKEND/$fid")
	n=$(daemon_count "$t" 'm.get("type") == "READ_INTENT"')
	if jtest "$r" 'd["correct"] and not d["zeros"]'; then
		pass "v$v: wait share, whole-file read through a 3 s recall: correct bytes (total $(echo "$r" | python3 -c 'import json,sys;print(json.load(sys.stdin)["total_s"])') s)"
	else
		fail "v$v: wait share whole-file read: $r"
	fi
	# The pending table answers retries from memory: the spike sent one
	# request per client retry (~180 for a 10 s recall on NFSv4).
	if [ "$n" -le 12 ]; then
		pass "v$v: $n READ_INTENT(s) for the whole wait (retries answered from the pending table)"
	else
		fail "v$v: $n READ_INTENTs for one waiting reader (retry storm)"
	fi
	umount -f -l "$MNT/w$tag"

	# --- failfast: EIO at once, correct after the recall ----------------------
	fid=$((fid + 1))
	daemon_start --recall-delay 2
	mkstub "$DATA/failfast/f$tag.segy" $fid $SIZE $RES
	mount_v "$v" failfast "$MNT/f$tag"
	r=$(reader one "$MNT/f$tag/f$tag.segy" "$BACKEND/$fid" --start $((8 * MiB)))
	if jtest "$r" 'd.get("errno") == "EIO" and d["s"] < 0.5'; then
		pass "v$v: failfast read of a stub range: EIO in $(echo "$r" | python3 -c 'import json,sys;print(json.load(sys.stdin)["s"])') s"
	else
		fail "v$v: failfast read: $r"
	fi
	sleep 3.5
	r=$(reader one "$MNT/f$tag/f$tag.segy" "$BACKEND/$fid" --start $((8 * MiB)))
	if jtest "$r" 'd.get("ok")'; then
		pass "v$v: failfast: the same range reads correctly once the queued recall is done"
	else
		fail "v$v: failfast after recall: $r"
	fi
	umount -f -l "$MNT/f$tag"

	# --- waitquick:5000 -----------------------------------------------------
	for rd in 2 20; do
		fid=$((fid + 1))
		daemon_start --recall-delay $rd
		mkstub "$DATA/quick/q$rd$tag.segy" $fid $SIZE $RES
		mount_v "$v" quick "$MNT/q$tag"
		r=$(timeout 60 python3 "$T/reader.py" one "$MNT/q$tag/q$rd$tag.segy" "$BACKEND/$fid" --start $((8 * MiB)))
		if [ $rd = 2 ]; then
			if jtest "$r" 'd.get("ok")'; then
				pass "v$v: waitquick:5000 with a 2 s recall waits and reads correctly ($(echo "$r" | python3 -c 'import json,sys;print(json.load(sys.stdin)["s"])') s)"
			else
				fail "v$v: waitquick 2 s recall: $r"
			fi
		else
			if jtest "$r" 'd.get("errno") == "EIO"'; then
				pass "v$v: waitquick:5000 with a 20 s recall answers EIO"
			else
				fail "v$v: waitquick 20 s recall: $r"
			fi
		fi
		umount -f -l "$MNT/q$tag"
	done
done
ganesha_alive || fail "ganesha.nfsd died"
finish
