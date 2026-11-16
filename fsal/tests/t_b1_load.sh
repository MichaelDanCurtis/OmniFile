#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# B1 (filer spike, ported): Ganesha loads FSAL_OMNI with the guard, every
# OMNI export is created with its policy, the module connects to the daemon
# with the HELLO handshake, and plain files round-trip over every version.
. "$(dirname "$0")/lib.sh"
setup_volumes
ganesha_start || exit 1
t=$(now)
daemon_start
for e in "101: policy=wait" "102: policy=failfast" "103: policy=waitquick:5000" "104: policy=failfast"; do
	grep -q "FSAL_OMNI export $e park_ms=5000" "$LOG" && pass "export $e created" || fail "export $e missing"
done
if grep -E ':(FSAL|CONFIG|EXPORT) :(MAJ|FATAL) ' "$LOG" | grep -q .; then
	fail "FSAL/CONFIG/EXPORT errors in the Ganesha log"
fi
m=$(daemon_last 'm.get("type") == "HELLO"')
if jtest "$m" 'd["magic"] == 0x53464D4F and d["major"] == 0 and d["minor"] == 2 and d["features"] & 1 and d["features"] & 2 and "15.6" in d["ganesha"]'; then
	pass "HELLO: magic OMFS, protocol 0.2, PARK and CHUNK_MAP offered, Ganesha $(echo "$m" | python3 -c 'import json,sys;print(json.load(sys.stdin)["ganesha"])')"
else
	fail "HELLO: $m"
fi
ganesha_connected && pass "FSAL connected (HELLO_ACK)" || fail "FSAL did not log the connection"
sleep 2.5
n=$(daemon_count "$t" 'm.get("type") == "PING"')
[ "$n" -ge 1 ] && pass "FSAL pings an idle connection ($n PINGs in 2.5 s)" || fail "no PING from the FSAL"
t=$(now)
daemon_start --keepalive
sleep 1.5
n=$(daemon_count "$t" 'm.get("type") == "PONG"')
[ "$n" -ge 1 ] && pass "FSAL answers the daemon's PINGs ($n PONGs)" || fail "no PONG from the FSAL"
# A daemon whose disk stalls (every message's log write held 4 s, like the
# writeback and swap stalls of a loaded host) still answers HELLO and PING at
# once (contract 5.6), so the FSAL never sees 3 s without traffic and never
# fails closed on a daemon that is only slow.
up=yes
daemon_start --log-stall 4 || up=no
nd=$(grep -c -E 'disconnected from|no HELLO_ACK' "$LOG")
sleep 7
if [ $up = yes ] && [ "$(grep -c -E 'disconnected from|no HELLO_ACK' "$LOG")" = "$nd" ]; then
	pass "a daemon with 4 s disk stalls answers HELLO and PING at once: connected, and not dropped in 7 s"
else
	fail "a daemon with 4 s disk stalls: connected=$up, $(grep -E 'disconnected from|no HELLO_ACK' "$LOG" | tail -1)"
fi
daemon_start
for v in 3 4.0 4.1 4.2; do
	mount_v "$v" wait "$MNT/b1" || { fail "v$v mount"; continue; }
	head -c 1048577 /dev/urandom >"$B/b1.bin"
	cp "$B/b1.bin" "$MNT/b1/b1-$v.bin" && sync &&
		cmp -s "$B/b1.bin" "$DATA/wait/b1-$v.bin" && cmp -s "$B/b1.bin" "$MNT/b1/b1-$v.bin" &&
		pass "v$v: plain file write/read round trip through the guard" || fail "v$v: round trip"
	rm -f "$MNT/b1/b1-$v.bin"
	umount -f -l "$MNT/b1"
done
# POSIX ACLs are enforced over NFS through FSAL_OMNI: a named entry grants
# a user that the mode bits deny (decision 28; Ganesha built with
# ENABLE_VFS_POSIX_ACL, fsal/build/Containerfile).
echo "acl granted" >"$DATA/wait/acl.txt"
chown 0:0 "$DATA/wait/acl.txt"
chmod 0600 "$DATA/wait/acl.txt"
echo "acl denied" >"$DATA/wait/noacl.txt"
chmod 0600 "$DATA/wait/noacl.txt"
setfacl -m u:2001:r,g:3005:r "$DATA/wait/acl.txt"
for v in 3 4.2; do
	mount_v "$v" wait "$MNT/acl" || { fail "v$v ACL mount"; continue; }
	got=$(setpriv --reuid 2001 --regid 3001 --clear-groups -- cat "$MNT/acl/acl.txt" 2>&1)
	gotg=$(setpriv --reuid 2002 --regid 3005 --clear-groups -- cat "$MNT/acl/acl.txt" 2>&1)
	if [ "$got" = "acl granted" ] && [ "$gotg" = "acl granted" ] &&
		! setpriv --reuid 2001 --regid 3001 --clear-groups -- cat "$MNT/acl/noacl.txt" >/dev/null 2>&1 &&
		! setpriv --reuid 2003 --regid 3001 --clear-groups -- cat "$MNT/acl/acl.txt" >/dev/null 2>&1; then
		pass "v$v: POSIX ACL named user and named group entries grant read on a 0600 root file; others denied"
	else
		fail "v$v: POSIX ACL not enforced: user '$got', group '$gotg'"
	fi
	umount -f -l "$MNT/acl"
done
readelf -d "$GANESHA_PREFIX/lib/ganesha/libfsalomni.so" | grep -q libjemalloc && pass "module links jemalloc" ||
	fail "module not linked with jemalloc"
finish
