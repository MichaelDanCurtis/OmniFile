#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# Truncate to 0 next to the smb-guard (docs/contracts/fsal-socket.md 6.1,
# "Truncate to 0"; M2 integration). The smb-guard's fanotify group on the
# same filesystem sees Ganesha's VFS truncate as a pre-content event, and for
# a file that still has its marker it waits for a fill: after a FAILED fill,
# forever. So the FSAL removes the marker BEFORE the truncate, under the fill
# fence:
#  - everywhere: inotify shows the marker removal (IN_ATTRIB) before the
#    truncate (IN_MODIFY), for SETATTR and OPEN-with-truncate, NFSv4.2 and v3;
#  - where the kernel has pre-content events (FANOTIFY_ACCESS_PERMISSIONS,
#    Linux >= 6.14): with a pre-content group that never answers for marked
#    files (prewatch.py guard), the truncate of a stub whose fill FAILED
#    completes. Elsewhere this part is skipped with a NOTE.
. "$(dirname "$0")/lib.sh"
setup_volumes
ganesha_start || exit 1
daemon_start --recall-delay 600
mount_v 4.2 failfast "$MNT/f"
mount_v 3 failfast "$MNT/f3"
fid=9900
has_marker() { getfattr -n trusted.omnifile "$1" >/dev/null 2>&1; }
trunc() { # trunc <how> <file> <timeout>
	case $1 in
	setattr) timeout -k 2 "$3" truncate -s 0 "$2" ;;
	open) timeout -k 2 "$3" python3 -c 'import sys; open(sys.argv[1], "wb").close()' "$2" ;;
	esac
}
wait_line() { # wait_line <file> : until the helper printed its first line
	local i
	for i in $(seq 1 50); do [ -s "$1" ] && return 0; sleep 0.1; done
	return 1
}
# stub_failed <name>: a PARTIAL stub whose fill FAILED (a FAILED entry, no fence)
stub_failed() {
	fid=$((fid + 1))
	mkstub "$DATA/failfast/$1" $fid $SIZE $RES
	daemon_ctl '{"cmd": "failed", "path": "'"$DATA/failfast/$1"'", "valid_ms": 60000}' >/dev/null
}

# --- the marker goes before the truncate --------------------------------------
for how in setattr open; do
	for v in 4.2 3; do
		m="$MNT/f"
		[ $v = 3 ] && m="$MNT/f3"
		stub_failed "o$how$v.segy"
		f="$DATA/failfast/o$how$v.segy"
		: >"$B/order.out"
		python3 "$T/prewatch.py" order "$f" 3 >"$B/order.out" &
		op=$!
		wait_line "$B/order.out"
		trunc $how "$m/o$how$v.segy" 10
		rc=$?
		wait $op
		ev=$(tail -n 1 "$B/order.out")
		if [ $rc = 0 ] && [ "$(stat -c %s "$f")" = 0 ] && ! has_marker "$f" &&
			jtest "$ev" 'd["events"][:1] == ["A"] and any("M" in e for e in d["events"][1:])'; then
			pass "v$v $how-truncate of a stub after a FAILED fill: marker removed (IN_ATTRIB) before the truncate (IN_MODIFY): $ev"
		else
			fail "v$v $how-truncate order: rc $rc, size $(stat -c %s "$f"), inotify $ev"
		fi
	done
done

# --- with a pre-content group that waits on marked files ------------------------
: >"$B/guard.out"
python3 "$T/prewatch.py" guard "$DATA" >"$B/guard.out" 2>&1 &
gp=$!
wait_line "$B/guard.out"
if grep -q '^ready' "$B/guard.out"; then
	for how in setattr open; do
		for v in 4.2 3; do
			m="$MNT/f"
			[ $v = 3 ] && m="$MNT/f3"
			stub_failed "g$how$v.segy"
			f="$DATA/failfast/g$how$v.segy"
			trunc $how "$m/g$how$v.segy" 10
			rc=$?
			if [ $rc = 0 ] && [ "$(stat -c %s "$f")" = 0 ] && ! has_marker "$f"; then
				pass "v$v $how-truncate with a pre-content group armed (smb-guard stand-in) completes after a FAILED fill"
			else
				fail "v$v $how-truncate under the pre-content group: rc $rc (124 = hung on a held event), size $(stat -c %s "$f"); $(tail -n 2 "$B/guard.out")"
			fi
		done
	done
else
	echo "NOTE $TNAME: this kernel has no fanotify pre-content events ($(head -n 1 "$B/guard.out")); the smb-guard part runs only where they exist (CI, the appliance)" >>"$RESULTS"
	say "pre-content events unavailable: $(head -n 1 "$B/guard.out")"
fi
kill "$gp" 2>/dev/null
wait "$gp" 2>/dev/null
ganesha_alive || fail "ganesha.nfsd died"
finish
