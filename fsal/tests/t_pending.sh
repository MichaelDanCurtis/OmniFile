#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# The pending table's limits (docs/contracts/fsal-socket.md 6.1 and 6.2; M1
# review nits):
#  - at 10^6 entries the oldest entries without parked reads are evicted, so
#    a new read still gets an entry (here: it parks and is served) rather
#    than being answered without one;
#  - a FETCHING entry is the fill fence until DONE, FAILED or CANCELLED
#    replaces it, or a resync drops it, whatever its valid_ms: it is neither
#    swept (60 s after its expiry) nor evicted.
. "$(dirname "$0")/lib.sh"
setup_volumes
ganesha_start || exit 1
daemon_start --fetch-delay 1
mount_v 4.2 failfast "$MNT/f"
mount_v 4.2 park "$MNT/p"
fid=9800

# A fill fence whose valid_ms (100 ms) runs out long before the fill ends.
fid=$((fid + 1))
ffid=$fid
mkstub "$DATA/failfast/fx$ffid.segy" $ffid $SIZE $RES
t0=$(date +%s)
out=$(daemon_ctl '{"cmd": "fetching", "path": "'"$DATA/failfast/fx$ffid.segy"'", "valid_ms": 100}')
jtest "$out" 'd["acked"]' || fail "fence INVALIDATE not acked: $out"

# --- the cap: 10^6 entries, then a read that must park --------------------------
out=$(daemon_ctl '{"cmd": "flood", "n": 1000000}')
say "flood: $out"
sleep 3
fid=$((fid + 1))
mkstub "$DATA/park/cap$fid.segy" $fid $SIZE $RES --online
r=$(timeout 30 python3 "$T/reader.py" one "$MNT/p/cap$fid.segy" "$BACKEND/$fid" --start $((8 * MiB)))
if jtest "$r" 'd.get("ok")' && grep -q 'pending table at its cap of 1000000 entries: evicted' "$LOG"; then
	pass "a full pending table (10^6) evicts its oldest entry: an ONLINE read still parks and gets correct bytes"
else
	fail "read with a full pending table: $r; $(grep 'pending table' "$LOG" | tail -2)"
fi

# --- FETCHING outlives valid_ms + 60 s and the eviction above -------------------
left=$((t0 + 63 - $(date +%s)))
[ $left -gt 0 ] && { say "waiting ${left} s for the 60 s sweep"; sleep $left; }
timeout -k 2 4 truncate -s 0 "$MNT/f/fx$ffid.segy"
rc=$?
if [ $rc = 124 ] && [ "$(stat -c %s "$DATA/failfast/fx$ffid.segy")" = $SIZE ]; then
	pass "a FETCHING entry 60 s past its valid_ms (and through a table eviction) still fences truncate (DELAY)"
else
	fail "FETCHING entry swept or evicted: truncate rc $rc, size $(stat -c %s "$DATA/failfast/fx$ffid.segy")"
fi
daemon_ctl '{"cmd": "done", "path": "'"$DATA/failfast/fx$ffid.segy"'"}' >/dev/null
if timeout 20 truncate -s 0 "$MNT/f/fx$ffid.segy" && [ "$(stat -c %s "$DATA/failfast/fx$ffid.segy")" = 0 ]; then
	pass "the fence lifts on DONE: truncate goes through"
else
	fail "truncate after DONE"
fi
ganesha_alive || fail "ganesha.nfsd died"
finish
