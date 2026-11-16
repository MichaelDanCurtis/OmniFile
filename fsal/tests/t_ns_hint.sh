#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# NS_HINT (contract 5.4): every user mkdir, rename, owner change, unlink and
# rmdir over NFSv3, 4.0, 4.1 and 4.2 reaches the daemon with the caller's
# identity, the names and the inode keys, so the edge can say who made each
# change (control-api 4.1 `actor`). A truncate's SETATTR says SIZE, an owner
# change OWNER. Hard links are refused (decision 42): LINK answers NOTSUPP
# over every version, nothing is linked and no hint is sent. Never blocking:
# with the daemon away every op succeeds at once. Ported from line B's
# t_ns_change.sh (PR #10, d981ee9, 81fa187) onto the existing NS_HINT.
. "$(dirname "$0")/lib.sh"
setup_volumes
ganesha_start || exit 1
daemon_start || exit 1
U=4321 # an ordinary NFS user (sec=sys: the uid is the client's claim)
as() { setpriv --reuid $U --regid $U --clear-groups "$@"; }

# check_hints <since> <vers> <dir inode> <file inode> -> PASS/FAIL lines
check_hints() {
	python3 - "$DLOG" "$1" "$2" "$U" "$3" "$4" <<'PY'
import json, sys
log, since, vers, uid = sys.argv[1], float(sys.argv[2]), sys.argv[3], int(sys.argv[4])
dino, fino = int(sys.argv[5]), int(sys.argv[6])
ms = [json.loads(l) for l in open(log)]
ms = [m for m in ms if m.get("t", 0) >= since and m.get("type") == "NS_HINT"]
NAMES = {1: "create", 2: "unlink", 3: "rename", 4: "setattr", 5: "setacl", 6: "link",
         7: "mkdir", 8: "rmdir"}
got = [(NAMES.get(m["op"], m["op"]), m["name"], m["new_name"]) for m in ms]
want = [("mkdir", "d", ""), ("rename", "f", "g"), ("setattr", "", ""),
        ("unlink", "g", ""), ("rmdir", "d", "")]
if got != want:
    print("FAIL hints in order: got %r" % got); sys.exit()
print("PASS mkdir, rename, setattr (owner), unlink and rmdir, in order; no LINK (refused)")
nfs = "3.0" if vers == "3" else vers
bad = [m for m in ms if m["ident"]["uid"] != uid or m["ident"]["nfs"] != nfs
       or m["ident"]["origin"] != "request" or m["ident"]["sec"] != 1
       or m["ident"]["addr"] != "127.0.0.1"]
print(("FAIL identity: %r" % [m["ident"] for m in bad]) if bad else
      "PASS every hint carries the caller (uid %d, NFS %s, sec=sys, its address)" % (uid, nfs))
mk, rn, sa, ul, rd = ms
print("PASS mkdir names the new directory's inode" if mk["target"]["ino"] == dino
      and mk["target"]["fsid"] != 0 and mk["parent"]["ino"] != 0 else "FAIL mkdir: %r" % mk)
print("PASS rename names the file's inode and its old directory" if rn["target"]["ino"] == fino
      and rn["parent"]["ino"] == dino else "FAIL rename: %r" % rn)
print("PASS the owner change says OWNER (what 2), not a truncate" if sa["what"] == 2
      and sa["target"]["ino"] == fino else "FAIL setattr: %r" % sa)
print("PASS unlink and rmdir name their objects" if ul["target"]["ino"] == fino
      and ul["parent"]["ino"] == dino and rd["target"]["ino"] == dino
      else "FAIL unlink/rmdir: %r %r" % (ul, rd))
PY
}

for v in 3 4.0 4.1 4.2; do
	mount_v "$v" wait "$MNT/w" || { fail "v$v: mount"; continue; }
	W=$MNT/w/ns-$v
	mkdir -p "$W" && chown $U:$U "$W"
	since=$(now)
	as mkdir "$W/d" && as sh -c "echo x >'$W/d/f'" && as mv "$W/d/f" "$W/d/g" &&
		as chown $U:$U "$W/d/g" || fail "v$v: an operation failed"
	dino=$(stat -c %i "$DATA/wait/ns-$v/d")
	fino=$(stat -c %i "$DATA/wait/ns-$v/d/g")
	# decision 42: a hard link is refused with "not supported" (NFSv3
	# NFS3ERR_NOTSUPP; NFSv4 NFS4ERR_NOTSUPP, which the Linux client may show
	# as errno 524), and nothing is linked
	out=$(as ln "$W/d/g" "$W/d/h" 2>&1) && fail "v$v: ln SUCCEEDED (hard links must be refused)"
	if echo "$out" | grep -qiE 'not supported|error 524' && [ ! -e "$DATA/wait/ns-$v/d/h" ] &&
		[ "$(stat -c %h "$DATA/wait/ns-$v/d/g")" = 1 ]; then
		pass "v$v: ln (hard link) answers not supported and links nothing ($out)"
	else
		fail "v$v: ln: '$out', h exists: $([ -e "$DATA/wait/ns-$v/d/h" ] && echo yes || echo no)"
	fi
	as rm "$W/d/g" && as rmdir "$W/d" || fail "v$v: rm/rmdir failed"
	sleep 0.5
	while read -r st msg; do
		if [ "$st" = PASS ]; then pass "v$v: $msg"; else fail "v$v: $msg"; fi
	done < <(check_hints "$since" "$v" "$dino" "$fino")
	umount -f -l "$MNT/w"
done

mount_v 4.2 wait "$MNT/w"
echo something >"$DATA/wait/tr.txt"

# --- daemon away: the ops succeed at once (hints are dropped, never queued) ----
daemon_stop
sleep 0.3
t0=$(now)
mkdir "$MNT/w/away1" || fail "mkdir with the daemon away failed"
ta=$(now)
mv "$MNT/w/away1" "$MNT/w/away2" || fail "rename with the daemon away failed"
tb=$(now)
rmdir "$MNT/w/away2" || fail "rmdir with the daemon away failed"
t1=$(now)
say "daemon away: mkdir $(python3 -c "print(round($ta - $t0, 2))") s, rename $(python3 -c "print(round($tb - $ta, 2))") s, rmdir $(python3 -c "print(round($t1 - $tb, 2))") s"
python3 -c "import sys; sys.exit(0 if $t1 - $t0 < 3 else 1)" &&
	pass "with the daemon away, mkdir, rename and rmdir succeed at once (never blocked)" ||
	fail "ops took $(python3 -c "print($t1 - $t0)") s"
out=$(ln "$MNT/w/tr.txt" "$MNT/w/tr3.txt" 2>&1)
[ ! -e "$DATA/wait/tr3.txt" ] && pass "LINK is refused with the daemon away too" ||
	fail "ln with the daemon away linked: $out"
umount -f -l "$MNT/w"
daemon_start || exit 1
ganesha_alive || fail "ganesha.nfsd died"
finish
