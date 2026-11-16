#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# Ganesha under the production unit's capabilities, not --privileged.
# The capability set is read from fsal/systemd/nfs-ganesha-omni.service and
# applied with setpriv (bounding set + no_new_privs), so this proves what the
# unit grants:
#  1. with the unit's set, managed files work (header local, stub range
#     guarded, recall served);
#  2. with the same set minus CAP_SYS_ADMIN, the FSAL logs a CRIT at export
#     creation and every read of a managed file FAILS CLOSED, never zeros:
#     without CAP_SYS_ADMIN trusted.omnifile reads as "no such attribute".
. "$(dirname "$0")/lib.sh"
UNIT=$T/../systemd/nfs-ganesha-omni.service
setup_volumes
caps=$(sed -n 's/^CapabilityBoundingSet=//p' "$UNIT")
[ -n "$caps" ] || { fail "no CapabilityBoundingSet in $UNIT"; finish; }
grep -q '^NoNewPrivileges=yes' "$UNIT" && nnp=--no-new-privs || nnp=
bset() { # bset [cap to drop] -> setpriv --bounding-set argument
	local c out=-all
	for c in $caps; do
		c=$(echo "${c#CAP_}" | tr 'A-Z' 'a-z')
		[ "$c" = "${1:-}" ] && continue
		out="$out,+$c"
	done
	echo "$out"
}
say "unit capabilities: $caps"

# --- 1. the unit's capability set ----------------------------------------------
ganesha_start setpriv --bounding-set "$(bset)" $nnp -- || { fail "Ganesha does not start with the unit's capabilities"; finish; }
eff=$(awk '/^CapEff:/ {print $2}' /proc/"$(ganesha_pid)"/status)
say "ganesha.nfsd CapEff=$eff"
daemon_start --recall-delay 2
fid=9001
mkstub "$DATA/failfast/cap$fid.segy" $fid $SIZE $RES
mkstub "$DATA/wait/cap$((fid + 1)).segy" $((fid + 1)) $SIZE $RES
head -c 3000000 /dev/urandom >"$DATA/failfast/capplain.bin"
mount_v 4.2 failfast "$MNT/f"
mount_v 4.2 wait "$MNT/w"
if grep -q 'does NOT have CAP_SYS_ADMIN' "$LOG"; then
	fail "unit caps: FSAL_OMNI reports CAP_SYS_ADMIN missing"
else
	pass "unit caps: Ganesha runs with the unit's bounding set; CAP_SYS_ADMIN present"
fi
r0=$(reader one "$MNT/f/cap$fid.segy" "$BACKEND/$fid" --start 0)
r=$(reader one "$MNT/f/cap$fid.segy" "$BACKEND/$fid" --start $((8 * MiB)))
rw=$(timeout 30 python3 "$T/reader.py" one "$MNT/w/cap$((fid + 1)).segy" "$BACKEND/$((fid + 1))" --start $((8 * MiB)))
if jtest "$r0" 'd.get("ok")' && jtest "$r" 'd.get("errno") == "EIO"' && jtest "$rw" 'd.get("ok")' &&
	cmp -s "$DATA/failfast/capplain.bin" "$MNT/f/capplain.bin"; then
	pass "unit caps: header reads, stub range guarded (failfast EIO), wait recall served, plain file reads"
else
	fail "unit caps: header $r0, failfast $r, wait $rw"
fi
umount_all

# --- 2. the same set without CAP_SYS_ADMIN ----------------------------------------
ganesha_start setpriv --bounding-set "$(bset sys_admin)" $nnp -- || { fail "Ganesha does not start without CAP_SYS_ADMIN"; finish; }
eff=$(awk '/^CapEff:/ {print $2}' /proc/"$(ganesha_pid)"/status)
say "ganesha.nfsd CapEff=$eff (no CAP_SYS_ADMIN)"
daemon_start --recall-delay 2
mount_v 4.2 failfast "$MNT/f"
mount_v 4.2 wait "$MNT/w"
if grep -q 'FSAL :CRIT :FSAL_OMNI: ganesha.nfsd does NOT have CAP_SYS_ADMIN at export creation' "$LOG"; then
	pass "no CAP_SYS_ADMIN: loud CRIT at export creation"
else
	fail "no CAP_SYS_ADMIN: no CRIT at startup"
fi
fid=9011
mkstub "$DATA/failfast/nc$fid.segy" $fid $SIZE $RES
mkstub "$DATA/wait/nc$((fid + 1)).segy" $((fid + 1)) $SIZE $RES
r0=$(reader one "$MNT/f/nc$fid.segy" "$BACKEND/$fid" --start 0)
r=$(reader one "$MNT/f/nc$fid.segy" "$BACKEND/$fid" --start $((8 * MiB)))
rw=$(blocked_read 4 "$MNT/w/nc$((fid + 1)).segy" "$BACKEND/$((fid + 1))" $((8 * MiB))) && rw=blocked
if jtest "$r0" 'd.get("errno") == "EIO"' && jtest "$r" 'd.get("errno") == "EIO" and not d.get("zeros")' &&
	[ "$rw" = blocked ]; then
	pass "no CAP_SYS_ADMIN: managed file reads fail closed (failfast EIO, header too; wait blocks), never zeros"
else
	fail "no CAP_SYS_ADMIN: header $r0, failfast $r, wait $rw"
fi
if ! cmp -s "$DATA/failfast/capplain.bin" "$MNT/f/capplain.bin" 2>/dev/null; then
	pass "no CAP_SYS_ADMIN: ordinary files fail closed too (markers cannot be read, so nothing is trusted)"
else
	fail "no CAP_SYS_ADMIN: an unmanaged file was served without a marker check"
fi
ls "$MNT/f" >/dev/null && [ "$(stat -c %s "$MNT/f/nc$fid.segy")" = $SIZE ] &&
	pass "no CAP_SYS_ADMIN: ls and stat still work" || fail "no CAP_SYS_ADMIN: ls/stat"
grep -q 'FAILS CLOSED: no CAP_SYS_ADMIN' "$LOG" && pass "no CAP_SYS_ADMIN: per-read CRIT logged (rate limited)" ||
	fail "no CAP_SYS_ADMIN: no per-read CRIT"
ganesha_alive || fail "ganesha.nfsd died"
finish
