#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# B2x (filer spike, ported) and the other data paths R1 section 6 guards,
# over NFSv4.2: SEEK, server-side COPY (guarded, with the opener's identity),
# CLONE (still NOTSUPP), mmap, WRITE, SETATTR size, ALLOCATE, and truncate to 0.
. "$(dirname "$0")/lib.sh"
setup_volumes
ganesha_start || exit 1
U=2001
G=3001
as_user() { setpriv --reuid $U --regid $G --clear-groups -- "$@"; }
# check_prefix <file> <backend>: every byte present matches, no zero blocks
check_prefix() {
	python3 - "$1" "$2" <<'PY'
import json, sys
a, b = open(sys.argv[1], "rb").read(), open(sys.argv[2], "rb").read()
bad = [o for o in range(0, len(a), 4096) if a[o:o+4096] != b[o:o+4096]]
zero = [o for o in bad if a[o:o+4096].count(0) > 2048]
print(json.dumps({"len": len(a), "bad": len(bad), "zeros": len(zero)}))
PY
}

daemon_start --recall-delay 3
mkdir -p "$DATA/wait/u" "$DATA/failfast/u"
chown $U:$G "$DATA/wait/u" "$DATA/failfast/u"
mount_v 4.2 wait "$MNT/w"
mount_v 4.2 failfast "$MNT/f"

# --- SEEK: a stub is all data ---------------------------------------------
mkstub "$DATA/wait/seek.segy" 2001 $SIZE $RES
t=$(now)
r=$(reader seek "$MNT/w/seek.segy")
n=$(daemon_count "$t" 'm.get("type") == "READ_INTENT"')
if jtest "$r" 'd["hole_from_0"] == d["size"] and d["data_from_8M"] == 8 << 20' && [ "$n" = 0 ]; then
	pass "SEEK on a stub reports the whole file as data, and starts no recall"
else
	fail "SEEK on a stub: $r (READ_INTENTs $n)"
fi

# --- COPY of a stub on a wait share: correct bytes, identity from OPEN ----
mkstub "$DATA/wait/u/csrc.segy" 2002 $SIZE $RES
chown $U:$G "$DATA/wait/u/csrc.segy"
t=$(now)
cops=$(awk -v m="$MNT/w" '$1=="device"{x=($5==m)} x&&$1=="COPY:"{print $2}' /proc/self/mountstats)
if as_user timeout 60 cp "$MNT/w/u/csrc.segy" "$MNT/w/u/cdst.segy"; then
	r=$(check_prefix "$DATA/wait/u/cdst.segy" "$BACKEND/2002")
	if jtest "$r" 'd["len"] == '$SIZE' and d["bad"] == 0'; then
		pass "COPY of a stub on a wait share waits for the recall and copies correct bytes"
	else
		fail "COPY on wait: destination $r"
	fi
else
	fail "COPY on wait: cp failed"
fi
cops2=$(awk -v m="$MNT/w" '$1=="device"{x=($5==m)} x&&$1=="COPY:"{print $2}' /proc/self/mountstats)
[ "${cops2:-0}" -gt "${cops:-0}" ] || fail "cp did not use server-side COPY ($cops -> $cops2)"
m=$(daemon_last 'm.get("type") == "READ_INTENT" and m.get("op") == "copy_src"')
if jtest "$m" 'd.get("ident", {}).get("uid") == '$U' and d["ident"]["gid"] == '$G' and d["ident"]["origin"] == "open_stateid" and d["ident"]["nfs"] == "4.2"'; then
	pass "COPY's recall carries the opener's identity: uid $U gid $G origin=open_stateid (spike B3 gap closed)"
else
	fail "COPY identity: $m"
fi

# --- COPY on failfast: EIO, and no zeros in the destination ---------------
mkstub "$DATA/failfast/u/csrc.segy" 2003 $SIZE $RES
chown $U:$G "$DATA/failfast/u/csrc.segy"
if as_user timeout 30 cp "$MNT/f/u/csrc.segy" "$MNT/f/u/cdst.segy" 2>"$B/cp.err"; then
	fail "COPY of a stub on failfast succeeded before any recall"
else
	r=$(check_prefix "$DATA/failfast/u/cdst.segy" "$BACKEND/2003" 2>/dev/null || echo '{"len":0,"bad":0,"zeros":0}')
	if jtest "$r" 'd["zeros"] == 0 and d["bad"] == 0'; then
		pass "COPY of a stub on failfast fails ($(tr -d '\n' <"$B/cp.err" | cut -c1-60)); destination holds no zeros ($r)"
	else
		fail "COPY on failfast left bad bytes: $r"
	fi
fi

# --- CLONE stays NOTSUPP, also for a stub source --------------------------
mkstub "$DATA/wait/clone.segy" 2004 $SIZE $RES
if out=$(cp --reflink=always "$MNT/w/clone.segy" "$MNT/w/clone2.segy" 2>&1); then
	fail "cp --reflink=always of a stub SUCCEEDED (Ganesha accepts CLONE now)"
elif printf '%s' "$out" | grep -q 'Operation not supported' && [ ! -e "$DATA/wait/clone2.segy" ]; then
	pass "CLONE of a stub is refused with EOPNOTSUPP and leaves no destination"
else
	fail "CLONE of a stub: $out"
fi

# --- mmap past the header on failfast: SIGBUS, never zeros ----------------
mkstub "$DATA/failfast/mm.segy" 2005 $SIZE $RES
python3 - "$MNT/f/mm.segy" <<'PY'
import mmap, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
m = mmap.mmap(fd, 0, prot=mmap.PROT_READ)
b = m[8 << 20: (8 << 20) + 4096]
print("zeros" if b.count(0) > 2048 else "data")
PY
rc=$?
if [ $rc = 135 ] || [ $rc = $((128 + 7)) ] || [ $rc = -7 ]; then
	pass "mmap read past the header on failfast gets SIGBUS"
else
	fail "mmap past the header on failfast: exit $rc"
fi

# --- WRITE, SETATTR size and ALLOCATE on a stub need a fill first ---------
mkstub "$DATA/failfast/w.segy" 2006 $SIZE $RES
before=$(getfattr -n trusted.omnifile -e hex "$DATA/failfast/w.segy" 2>/dev/null | tail -1)
python3 - "$MNT/f/w.segy" <<'PY' >"$B/w.out" 2>&1
import os, sys
fd = os.open(sys.argv[1], os.O_WRONLY)
try:
    os.pwrite(fd, b"X" * 4096, 8 << 20)
    os.fsync(fd)
    print("written")
except OSError as e:
    print("error", e.errno)
PY
if grep -q "error 5" "$B/w.out"; then
	pass "WRITE into a stub on failfast fails with EIO (needs a full fill first)"
else
	fail "WRITE into a stub: $(cat "$B/w.out")"
fi
if truncate -s $MiB "$MNT/f/w.segy" 2>/dev/null; then
	fail "truncate to 1 MiB of a stub succeeded"
else
	pass "SETATTR size (truncate -s 1M) of a stub on failfast is refused"
fi
if command -v fallocate >/dev/null; then
	if fallocate -o $((8 * MiB)) -l 4096 "$MNT/f/w.segy" 2>/dev/null; then
		fail "ALLOCATE on a stub succeeded"
	else
		pass "ALLOCATE on a stub on failfast is refused"
	fi
fi
after=$(getfattr -n trusted.omnifile -e hex "$DATA/failfast/w.segy" 2>/dev/null | tail -1)
r=$(check_prefix "$DATA/failfast/w.segy" "$BACKEND/2006")
if [ "$before" = "$after" ] && [ "$(stat -c %s "$DATA/failfast/w.segy")" = $SIZE ]; then
	pass "the refused stub kept its size and marker"
else
	fail "the stub changed: size $(stat -c %s "$DATA/failfast/w.segy"), marker $before -> $after"
fi

# --- truncate to 0 is allowed: cp a new file over a stub ------------------
mkstub "$DATA/failfast/over.segy" 2007 $SIZE $RES
head -c 3000000 /dev/urandom >"$B/new.bin"
t=$(now)
if cp "$B/new.bin" "$MNT/f/over.segy" && cmp -s "$B/new.bin" "$MNT/f/over.segy" &&
	cmp -s "$B/new.bin" "$DATA/failfast/over.segy"; then
	if getfattr -n trusted.omnifile "$DATA/failfast/over.segy" >/dev/null 2>&1; then
		fail "cp over a stub: the file still carries a marker"
	else
		pass "cp of a new file over a stub works (truncate to 0, marker removed, new bytes intact)"
	fi
else
	fail "cp of a new file over a stub failed"
fi
sleep 0.5
n=$(daemon_count "$t" 'm.get("type") == "NS_HINT" and m.get("op") == 4 and m.get("what") == 1')
[ "$n" -ge 1 ] && pass "the daemon was told (NS_HINT SETATTR, what SIZE)" || fail "no NS_HINT SETATTR SIZE after truncate to 0"

ganesha_alive || fail "ganesha.nfsd died"
finish
