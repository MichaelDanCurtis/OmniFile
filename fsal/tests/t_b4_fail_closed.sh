#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# B4 (filer spike, ported) and the fail-closed table of
# docs/contracts/fsal-socket.md section 7: whatever goes wrong, a read of a
# non-local range answers EIO (failfast) or DELAY (wait) and NEVER zeros;
# resident data, headers, ls and stat keep working; immutable files read.
. "$(dirname "$0")/lib.sh"
setup_volumes
ganesha_start || exit 1
# failfast and wait mounts on every NFS version (mount dirs f<tag>, w<tag>)
VERS="4.2 4.1 4.0 3"
for v in $VERS; do
	tag=$(echo "$v" | tr -d .)
	mount_v "$v" failfast "$MNT/f$tag"
	mount_v "$v" wait "$MNT/w$tag"
done
fid=4000

# expect_closed <label> : failfast EIO quickly, wait blocks (DELAY) without
# zeros (timeout fires, the mount still answers), on NFSv3, 4.0, 4.1 and 4.2;
# headers still read.
expect_closed() {
	local label=$1 r v tag
	fid=$((fid + 1))
	mkstub "$DATA/failfast/c$fid.segy" $fid $SIZE $RES
	for v in $VERS; do
		tag=$(echo "$v" | tr -d .)
		r=$(reader one "$MNT/f$tag/c$fid.segy" "$BACKEND/$fid" --start $((8 * MiB)))
		if jtest "$r" 'd.get("errno") == "EIO" and d["s"] < 1.0'; then
			pass "$label: failfast v$v read -> EIO in $(echo "$r" | python3 -c 'import json,sys;print(json.load(sys.stdin)["s"])') s"
		else
			fail "$label: failfast v$v read: $r"
		fi
	done
	r=$(reader one "$MNT/f42/c$fid.segy" "$BACKEND/$fid" --start 0)
	jtest "$r" 'd.get("ok")' && pass "$label: resident header still reads correctly" ||
		fail "$label: header read: $r"
	fid=$((fid + 1))
	mkstub "$DATA/wait/c$fid.segy" $fid $SIZE $RES
	# the wait reads run side by side: each must still be blocked at 3 s
	for v in $VERS; do
		tag=$(echo "$v" | tr -d .)
		blocked_read 3 "$MNT/w$tag/c$fid.segy" "$BACKEND/$fid" $((8 * MiB)) >"$B/blk.$tag" &
	done
	wait
	for v in $VERS; do
		tag=$(echo "$v" | tr -d .)
		if [ ! -s "$B/blk.$tag" ]; then
			pass "$label: wait v$v read blocks (DELAY), no zeros, mount alive"
		else
			fail "$label: wait v$v read: $(cat "$B/blk.$tag")"
		fi
	done
}

# --- socket missing --------------------------------------------------------
daemon_stop
sleep 0.5
expect_closed "socket missing"
# a wait reader blocked on a missing daemon completes once it is back
fid=$((fid + 1))
mkstub "$DATA/wait/back$fid.segy" $fid $SIZE $RES
timeout 60 python3 "$T/reader.py" one "$MNT/w42/back$fid.segy" "$BACKEND/$fid" --start $((8 * MiB)) >"$B/back.out" &
rp=$!
sleep 3
daemon_start --recall-delay 1
wait $rp
r=$(cat "$B/back.out")
jtest "$r" 'd.get("ok")' && pass "a wait reader blocked while the daemon was down gets correct bytes after it returns ($(echo "$r" | python3 -c 'import json,sys;print(json.load(sys.stdin)["s"])') s)" ||
	fail "wait reader across a daemon restart: $r"

# --- stale socket (daemon killed -9; socket file left behind) --------------
daemon_stop
python3 -c 'import socket; s=socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET); s.bind("'"$SOCK"'")'
sleep 0.5
expect_closed "stale socket (ECONNREFUSED)"
rm -f "$SOCK"

# --- hung daemon: accepts, never answers HELLO ------------------------------
daemon_start --mode hang-hello
expect_closed "hung daemon (no HELLO_ACK)"
grep -q 'no HELLO_ACK within 1 s' "$LOG" && pass "hung daemon: the FSAL gives up on HELLO after 1 s and retries" ||
	fail "hung daemon: no HELLO timeout logged"

# --- hung daemon: answers HELLO, then nothing --------------------------------
daemon_start --mode hang
sleep 0.3
expect_closed "hung daemon (after HELLO)"
sleep 3.5
grep -q 'no traffic for 3 s' "$LOG" && pass "hung daemon: disconnected after 3 s without traffic (ping timeout)" ||
	fail "hung daemon: no liveness disconnect logged"

# --- protocol version mismatch ------------------------------------------------
t=$(now)
daemon_start --mode bad-version
expect_closed "protocol major mismatch"
n=$(daemon_count "$t" 'm.get("type") == "ERROR" and m.get("code") == 1')
[ "$n" -ge 1 ] && pass "version mismatch: the FSAL sent ERROR 1 and closed" || fail "version mismatch: no ERROR 1"
grep -q 'refusing it' "$LOG" && pass "version mismatch logged at CRIT" || fail "version mismatch not logged"

# --- malformed message ----------------------------------------------------------
t=$(now)
daemon_start --mode malformed
sleep 0.5
n=$(daemon_count "$t" 'm.get("type") == "ERROR" and m.get("code") == 3')
[ "$n" -ge 1 ] && pass "malformed message: the FSAL sent ERROR 3 and closed" || fail "malformed: no ERROR 3"
expect_closed "malformed message from the daemon"

# --- unknown decision code, and RECHECK that lies -----------------------------------
daemon_start --mode decision:9
expect_closed "unknown READ_DECISION code"
daemon_start --mode decision:1
expect_closed "RECHECK while the marker does not cover the range"

# --- bad markers: never asked, BAD_MARKER reported ------------------------------------
daemon_start --recall-delay 30
for kind in "--bad-crc:2" "--bad-magic:1" "--marker-size=$((32 * MiB)):4"; do
	opt=${kind%:*}
	reason=${kind##*:}
	fid=$((fid + 1))
	mkstub "$DATA/failfast/bad$fid.segy" $fid $SIZE $RES $opt
	t=$(now)
	r0=$(reader one "$MNT/f42/bad$fid.segy" "$BACKEND/$fid" --start 0)
	r=$(reader one "$MNT/f42/bad$fid.segy" "$BACKEND/$fid" --start $((8 * MiB)))
	sleep 0.3
	ni=$(daemon_count "$t" 'm.get("type") == "READ_INTENT"')
	nb=$(daemon_count "$t" 'm.get("type") == "BAD_MARKER" and m.get("reason") == '$reason)
	if jtest "$r" 'd.get("errno") == "EIO"' && jtest "$r0" 'd.get("errno") == "EIO"' && [ "$ni" = 0 ] && [ "$nb" -ge 1 ]; then
		pass "marker $opt: every read fails closed (EIO, even the header), daemon not asked, BAD_MARKER reason $reason sent"
	else
		fail "marker $opt: header $r0, read $r, READ_INTENTs $ni, BAD_MARKER($reason) $nb"
	fi
done

# --- ordinary files, ls and stat never need the daemon ---------------------------------
daemon_stop
head -c 5000000 /dev/urandom >"$DATA/failfast/plain.bin"
if cmp -s "$DATA/failfast/plain.bin" "$MNT/f42/plain.bin"; then
	pass "an ordinary (unmanaged) file reads correctly with no daemon"
else
	fail "unmanaged file read with no daemon"
fi
if ls -l "$MNT/f42/" >/dev/null && stat "$MNT/f42/c4001.segy" >/dev/null && [ "$(stat -c %s "$MNT/f42/c4001.segy")" = $SIZE ]; then
	pass "ls and stat of stubs work with no daemon (size intact, no ENOENT)"
else
	fail "ls/stat with no daemon"
fi

# --- immutable files (spike bug: O_RDWR xattr open made them unreadable) -------------
daemon_start --recall-delay 30
for s in failfast wait; do
	echo "plain text, not a stub" >"$DATA/$s/imm.txt"
	chattr +i "$DATA/$s/imm.txt"
done
fid=$((fid + 1))
mkstub "$DATA/failfast/immstub.segy" $fid $SIZE $RES
chattr +i "$DATA/failfast/immstub.segy"
mount_v 4.2 failfast "$MNT/f"
mount_v 4.2 wait "$MNT/w"
for s in f w; do
	if [ "$(timeout 10 cat "$MNT/$s/imm.txt" 2>&1)" = "plain text, not a stub" ]; then
		pass "immutable (chattr +i) ordinary file reads through FSAL_OMNI ($s share)"
	else
		fail "immutable file on $s share unreadable"
	fi
done
r=$(reader one "$MNT/f/immstub.segy" "$BACKEND/$fid" --start 0)
r2=$(reader one "$MNT/f/immstub.segy" "$BACKEND/$fid" --start $((8 * MiB)))
if jtest "$r" 'd.get("ok")' && jtest "$r2" 'd.get("errno") == "EIO"'; then
	pass "immutable stub: header reads, stub range fails closed (marker read without O_RDWR)"
else
	fail "immutable stub: header $r, stub range $r2"
fi
for s in failfast wait; do chattr -i "$DATA/$s/imm.txt"; done
chattr -i "$DATA/failfast/immstub.segy"

# --- fail-closed warnings are rate limited ------------------------------------------
n=$(grep -c 'FSAL_OMNI fail-closed' "$LOG" || true)
say "fail-closed WARN lines in the whole run: $n (rate limited to 1/s per export)"
ganesha_alive || fail "ganesha.nfsd died"
finish
