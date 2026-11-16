#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# Parked reads (contract 6.3): a read of an ONLINE (not archived) range is
# parked in the FSAL without holding a Ganesha worker, and completes with
# correct bytes once the daemon has fetched the range, on NFSv3, 4.0, 4.1 and
# 4.2, even on a failfast share. Past park_ms it answers DELAY.
. "$(dirname "$0")/lib.sh"
setup_volumes
ganesha_start || exit 1
fid=7000
daemon_start --fetch-delay 1
for v in 3 4.0 4.1 4.2; do
	fid=$((fid + 1))
	mkstub "$DATA/park/p$fid.segy" $fid $SIZE $RES --online
	mount_v "$v" park "$MNT/p"
	t=$(now)
	r=$(timeout 30 python3 "$T/reader.py" one "$MNT/p/p$fid.segy" "$BACKEND/$fid" --start $((8 * MiB)))
	np=$(daemon_count "$t" 'm.get("type") == "READ_INTENT" and m.get("parked") == 1')
	if jtest "$r" 'd.get("ok") and d["s"] < 4'; then
		pass "v$v: ONLINE range on a failfast share parked and served correct bytes in $(echo "$r" | python3 -c 'import json,sys;print(json.load(sys.stdin)["s"])') s (no EIO, no 5 s v3 retry)"
	else
		fail "v$v: parked read: $r"
	fi
	[ "$np" -ge 1 ] && pass "v$v: READ_INTENT said parked=1" || fail "v$v: no parked READ_INTENT"
	umount -f -l "$MNT/p"
done
grep -q 'parked read completes: resume' "$LOG" && pass "parked reads resumed through fsal_resume" ||
	fail "no parked-read resume logged"

# park_ms: a 1.5 s park on a 4 s fetch answers DELAY, the client retries,
# and the read still ends with correct bytes; on NFSv4.0, 4.1 and 4.2 (v3
# would retry a JUKEBOX only after 5 s).
PARK_MS=1500 ganesha_start || exit 1
daemon_start --fetch-delay 4
for v in 4.0 4.1 4.2; do
	fid=$((fid + 1))
	mkstub "$DATA/park/pt$fid.segy" $fid $SIZE $RES --online
	mount_v "$v" park "$MNT/p"
	nd=$(grep 'parked read completes:' "$LOG" | grep -vc 'resume')
	r=$(timeout 30 python3 "$T/reader.py" one "$MNT/p/pt$fid.segy" "$BACKEND/$fid" --start $((8 * MiB)))
	if jtest "$r" 'd.get("ok")' && [ "$(grep 'parked read completes:' "$LOG" | grep -vc 'resume')" -gt "$nd" ]; then
		pass "v$v: park_ms 1500 on a 4 s fetch: DELAY at park_ms, then correct bytes ($(echo "$r" | python3 -c 'import json,sys;print(json.load(sys.stdin)["s"])') s)"
	else
		fail "v$v: park_ms expiry: $r; $(grep 'parked read completes' "$LOG" | tail -3)"
	fi
	umount -f -l "$MNT/p"
done

# Without the PARK feature an ONLINE range gets the policy default (EIO).
daemon_start --fetch-delay 1 --no-park
for v in 4.1 4.2; do
	fid=$((fid + 1))
	mkstub "$DATA/park/np$fid.segy" $fid $SIZE $RES --online
	mount_v "$v" park "$MNT/p"
	r=$(reader one "$MNT/p/np$fid.segy" "$BACKEND/$fid" --start $((8 * MiB)))
	jtest "$r" 'd.get("errno") == "EIO"' && pass "v$v: PARK not negotiated: ONLINE range on failfast answers EIO" ||
		fail "v$v: no-park: $r"
	umount -f -l "$MNT/p"
done
# A parked read whose daemon disappears completes fail-closed at once, on
# every NFS version.
for v in 3 4.0 4.1 4.2; do
	daemon_start --fetch-delay 30
	fid=$((fid + 1))
	mkstub "$DATA/park/dd$fid.segy" $fid $SIZE $RES --online
	mount_v "$v" park "$MNT/p"
	timeout 20 python3 "$T/reader.py" one "$MNT/p/dd$fid.segy" "$BACKEND/$fid" --start $((8 * MiB)) >"$B/dd.out" &
	rp=$!
	sleep 1
	daemon_stop
	wait $rp
	r=$(cat "$B/dd.out")
	jtest "$r" 'd.get("errno") == "EIO" and d["s"] < 3' && pass "v$v: daemon gone while a read was parked: EIO at once, no zeros" ||
		fail "v$v: parked read on disconnect: $r"
	umount -f -l "$MNT/p"
done
ganesha_alive || fail "ganesha.nfsd died"
finish
