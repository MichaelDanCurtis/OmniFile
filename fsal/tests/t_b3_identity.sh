#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# B3 (filer spike, ported): the caller's identity reaches the daemon with
# every READ_INTENT: uid, gid, NFS version, client address (an IPv4-mapped
# address sent as family 4), sec flavour, and the v4 client id.
. "$(dirname "$0")/lib.sh"
setup_volumes
ganesha_start || exit 1
U=2001
G=3001
daemon_start --recall-delay 30
mkdir -p "$DATA/failfast/id"
chown $U:$G "$DATA/failfast/id"
fid=3000
for v in 3 4.0 4.1 4.2; do
	fid=$((fid + 1))
	mkstub "$DATA/failfast/id/i$fid.segy" $fid $SIZE $RES
	mount_v "$v" failfast "$MNT/i"
	setpriv --reuid $U --regid $G --clear-groups -- \
		python3 "$T/reader.py" one "$MNT/i/id/i$fid.segy" "$BACKEND/$fid" --start $((8 * MiB)) >/dev/null
	sleep 0.3
	m=$(daemon_last 'm.get("type") == "READ_INTENT" and m["obj"]["file_id"] == '$fid)
	want_minor=${v#*.}
	[ "$v" = 3 ] && want="3.0" || want="4.$want_minor"
	if jtest "$m" 'd["ident"]["uid"] == '$U' and d["ident"]["gid"] == '$G' and d["ident"]["nfs"] == "'$want'" and d["ident"]["addr"] == "127.0.0.1" and d["ident"]["sec"] == 1 and d["principal"] == "" and d["ident"]["origin"] == "request" and d["policy"] == "failfast" and d["op"] == "read" and d["off"] == 8 << 20 and d["version"] == 7 and d["obj"]["export_id"] == 102'; then
		pass "v$v: READ_INTENT carries uid=$U gid=$G nfs=$want client=127.0.0.1 sec=sys origin=request"
	else
		fail "v$v: identity $m"
	fi
	if [ "$v" != 3 ] && [ "$v" != 4.0 ]; then
		jtest "$m" 'd["ident"]["clientid"] != 0' && pass "v$v: NFSv4 client id sent" ||
			fail "v$v: no client id: $m"
	fi
	# obj_ref keys: the daemon's own fsid / ino / generation (contract 4.1)
	ino=$(stat -c %i "$DATA/failfast/id/i$fid.segy")
	exp=$(python3 -c 'import sys; sys.path.insert(0,"'"$T"'"); import omni_proto as P; print(P.fsid_of("'"$DATA"'"), P.ino_gen("'"$DATA/failfast/id/i$fid.segy"'"))')
	set -- $exp
	if jtest "$m" 'd["obj"]["ino"] == '$ino' and d["obj"]["fsid"] == '$1' and d["obj"]["gen"] == '$2' and d["obj"]["file_id"] == '$fid; then
		pass "v$v: obj_ref fsid/ino/ino_gen match statfs f_fsid, st_ino and FS_IOC_GETVERSION"
	else
		fail "v$v: obj_ref $m (want fsid $1 ino $ino gen $2)"
	fi
	umount -f -l "$MNT/i"
done
finish
