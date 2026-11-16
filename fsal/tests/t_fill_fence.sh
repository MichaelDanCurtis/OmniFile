#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# The fill fence (docs/contracts/fsal-socket.md 6.1): a truncate to 0 of a
# PARTIAL file (SETATTR size 0, or OPEN with truncate) answers DELAY while
# (1) the daemon is filling it (a FETCHING entry), (2) the FSAL is
# disconnected, or (3) it is between HELLO_ACK and RESYNC_END. Otherwise it
# truncates and drops the marker. End to end: a user's new file written over
# a stub that is being filled never ends up mixed with backend bytes.
. "$(dirname "$0")/lib.sh"
setup_volumes
ganesha_start || exit 1
mount_v 4.2 failfast "$MNT/f"
mount_v 3 failfast "$MNT/f3"
fid=9500
has_marker() { getfattr -n trusted.omnifile "$1" >/dev/null 2>&1; }
# trunc <how> <file> <timeout>: 0 done, 124 still blocked (DELAY retried)
trunc() {
	case $1 in
	setattr) timeout "$3" truncate -s 0 "$2" ;;
	open) timeout "$3" python3 -c 'import sys; open(sys.argv[1], "wb").close()' "$2" ;;
	esac
}

# --- (1) FETCHING entry: SETATTR and OPEN-truncate wait, then go through ---
daemon_start --recall-delay 600
for how in setattr open; do
	for v in 4.2 3; do
		fid=$((fid + 1))
		mkstub "$DATA/failfast/ff$fid.segy" $fid $SIZE $RES
		m="$MNT/f"
		[ $v = 3 ] && m="$MNT/f3"
		out=$(daemon_ctl '{"cmd": "fetching", "path": "'"$DATA/failfast/ff$fid.segy"'"}')
		jtest "$out" 'd["acked"]' || fail "fence INVALIDATE not acked: $out"
		trunc $how "$m/ff$fid.segy" 4
		rc=$?
		if [ $rc = 124 ] && [ "$(stat -c %s "$DATA/failfast/ff$fid.segy")" = $SIZE ] &&
			has_marker "$DATA/failfast/ff$fid.segy"; then
			pass "v$v $how-truncate of a PARTIAL file being filled: DELAY (blocked 4 s), file and marker untouched"
		else
			fail "v$v $how-truncate during FETCHING: rc $rc, size $(stat -c %s "$DATA/failfast/ff$fid.segy")"
		fi
		trunc $how "$m/ff$fid.segy" 20 &
		tp=$!
		sleep 1
		daemon_ctl '{"cmd": "done", "path": "'"$DATA/failfast/ff$fid.segy"'"}' >/dev/null
		wait $tp
		rc=$?
		if [ $rc = 0 ] && [ "$(stat -c %s "$DATA/failfast/ff$fid.segy")" = 0 ] &&
			! has_marker "$DATA/failfast/ff$fid.segy"; then
			pass "v$v $how-truncate goes through once the fill is DONE (size 0, marker removed)"
		else
			fail "v$v $how-truncate after DONE: rc $rc, size $(stat -c %s "$DATA/failfast/ff$fid.segy")"
		fi
	done
done
# a FETCHING entry fences only its own file
fid=$((fid + 1))
mkstub "$DATA/failfast/fa$fid.segy" $fid $SIZE $RES
mkstub "$DATA/failfast/fb$((fid + 1)).segy" $((fid + 1)) $SIZE $RES
daemon_ctl '{"cmd": "fetching", "path": "'"$DATA/failfast/fa$fid.segy"'"}' >/dev/null
trunc setattr "$MNT/f/fb$((fid + 1)).segy" 4 && ! has_marker "$DATA/failfast/fb$((fid + 1)).segy" &&
	pass "a PARTIAL file nobody is filling truncates at once while another file is fenced" ||
	fail "unrelated PARTIAL file truncate was fenced"
fid=$((fid + 1))

# --- (2) disconnected: PARTIAL waits, RESIDENT and plain files do not -------
daemon_stop
sleep 0.5
fid=$((fid + 1))
mkstub "$DATA/failfast/d$fid.segy" $fid $SIZE $RES
mkstub "$DATA/failfast/r$((fid + 1)).segy" $((fid + 1)) $SIZE $SIZE --full
echo plain >"$DATA/failfast/plain-fence.txt"
trunc setattr "$MNT/f/r$((fid + 1)).segy" 4 && trunc setattr "$MNT/f/plain-fence.txt" 4 &&
	pass "disconnected: RESIDENT and unmanaged files still truncate" ||
	fail "disconnected: RESIDENT/unmanaged truncate blocked"
trunc open "$MNT/f/d$fid.segy" 20 &
tp=$!
sleep 4
if kill -0 $tp 2>/dev/null && [ "$(stat -c %s "$DATA/failfast/d$fid.segy")" = $SIZE ]; then
	pass "disconnected: OPEN-truncate of a PARTIAL file waits (DELAY)"
else
	fail "disconnected: PARTIAL truncate did not wait"
fi
daemon_start --recall-delay 600
wait $tp
[ $? = 0 ] && [ "$(stat -c %s "$DATA/failfast/d$fid.segy")" = 0 ] &&
	pass "disconnected: the waiting truncate completes once the daemon is back and resynced" ||
	fail "disconnected: truncate did not complete after reconnect"

# --- (3) between HELLO_ACK and RESYNC_END -----------------------------------
fid=$((fid + 2))
mkstub "$DATA/failfast/rs$fid.segy" $fid $SIZE $RES
daemon_stop
daemon_start --recall-delay 600 --resync-delay 4
t0=$(now)
trunc setattr "$MNT/f/rs$fid.segy" 20
rc=$?
dt=$(python3 -c "import time; print(round(time.time() - $t0, 1))")
if [ $rc = 0 ] && python3 -c "import sys; sys.exit(0 if $dt >= 2.0 else 1)" &&
	grep -q 'resync in progress (fill fence' "$LOG"; then
	pass "resync window: truncate of a PARTIAL file waited ${dt} s for RESYNC_END, then went through"
else
	fail "resync window: rc $rc after ${dt} s"
fi

# --- end to end: a new file written over a stub while it is being filled ---
daemon_start --recall-delay 0.3 --fill-hold 3
fid=$((fid + 1))
mkstub "$DATA/failfast/e$fid.segy" $fid $SIZE $RES
reader one "$MNT/f/e$fid.segy" "$BACKEND/$fid" --start $((8 * MiB)) >/dev/null # queues the recall
sleep 1.2 # the fence is up, the fill is holding
head -c 3000001 /dev/urandom >"$B/new-e.bin"
t0=$(now)
if timeout 30 cp "$B/new-e.bin" "$MNT/f/e$fid.segy"; then
	dt=$(python3 -c "import time; print(round(time.time() - $t0, 1))")
	sleep 0.5
	if cmp -s "$B/new-e.bin" "$DATA/failfast/e$fid.segy" && ! has_marker "$DATA/failfast/e$fid.segy"; then
		pass "cp over a stub during its fill waited ${dt} s for the fill, then wrote exactly the new bytes (no backend bytes mixed in, no marker)"
	else
		fail "cp over a filling stub: content $(cmp "$B/new-e.bin" "$DATA/failfast/e$fid.segy" 2>&1 | head -1), size $(stat -c %s "$DATA/failfast/e$fid.segy")"
	fi
else
	fail "cp over a filling stub failed"
fi
ganesha_alive || fail "ganesha.nfsd died"
finish
