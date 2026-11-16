#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# The marker cache against INVALIDATE and size changes (M1 review B3, S4, S7,
# S11, S12; docs/contracts/fsal-socket.md 5.5, 6.1, 6.5, 8). Ganesha runs
# the module under test relinked with a shim (slowxattr.c, lib.sh
# build_slow_module) that can hold each marker read for a few hundred ms, so
# the races are deterministic:
#  B3  a marker read that saw RESIDENT before an INVALIDATE is never cached
#      after it (SEEK, which also pins now): no zeros after the stub;
#  S4  an INVALIDATE for an unknown fsid is acked only after pinned I/O drains;
#  S7  a RESIDENT file appended over NFS stays readable (all local);
#  S12 a read past a cached PARTIAL size re-reads the marker: a file the
#      daemon grew (replace_content) gives EIO, not its new holes;
#  S11 a truncate to 0 of a file whose marker cannot be read answers DELAY.
. "$(dirname "$0")/lib.sh"
setup_volumes
build_slow_module || { fail "cannot build the module with the slowxattr shim"; finish; }
slow_marker off
PLUGINS_DIR=$SLOW_PLUGINS ganesha_start || exit 1
daemon_start --recall-delay 600
mount_v 4.2 failfast "$MNT/f"
mount_v 3 failfast "$MNT/f3"
fid=9700
direct() { reader one "$1" "$2" --start "$3" --direct; }

# Whether an INVALIDATE waited for a held marker read is decided from
# timestamps, not from a fixed ack_ms: on a loaded host the test's own
# processes can start a few hundred ms late, so a threshold reads a correct
# wait as "did not wait" (seen once in seven runs at load 50 on 16 cores: a
# 600 ms hold, ack after 117 ms). The read is held 700 ms, under the fake
# daemon's 1 s ack timeout. The slowxattr shim logs each hold's start and
# end; the fake daemon reports when it sent the INVALIDATE and got the ack.
# The INVALIDATE goes out once a hold has started, not after a fixed sleep.
HOLDS=$RUN/test-slow-marker.log
hold_reset() { rm -f "$HOLDS"; w=""; }
# wait_hold: wait (up to 10 s) until a marker read is being held
wait_hold() {
	for _ in $(seq 1 200); do
		grep -q '^H ' "$HOLDS" 2>/dev/null && return 0
		sleep 0.05
	done
	return 1
}
# acked_after_hold '<daemon answer>': succeeds if the INVALIDATE was sent at
# least 50 ms before a held marker read ended and acked no earlier than that
# end. An INVALIDATE that does not wait is acked at once, before it. Prints
# what it saw.
acked_after_hold() {
	python3 - "$HOLDS" "$1" <<'PY'
import json, sys
d = json.loads(sys.argv[2])
start, holds = {}, []
for line in open(sys.argv[1]):
    k, tid, t = line.split()
    if k == "H":
        start[tid] = float(t)
    elif tid in start:
        holds.append((start.pop(tid), float(t)))
ends = [e for s, e in holds if s <= d["t_sent"] and e - d["t_sent"] >= 0.05]
if not ends:
    print("inconclusive: no marker read was held when the INVALIDATE was sent (holds %s, sent %.3f)" % (holds, d["t_sent"]))
    sys.exit(1)
gap = (d["t_ack"] - min(ends)) * 1000
if gap < 0:
    print("acked %.1f ms before the held read ended" % -gap)
    sys.exit(1)
print("sent %.0f ms before the held read ended, acked %.1f ms after it" % ((min(ends) - d["t_sent"]) * 1000, gap))
PY
}

# --- B3: a RESIDENT marker read before the stub's INVALIDATE ----------------
# SEEK reads the (uncached) marker as RESIDENT and is held there; the daemon
# narrows, INVALIDATEs and punches meanwhile. Before the fix SEEK took no pin
# (the ack came at once, VFS then reported the punched hole) and cached its
# RESIDENT marker after the INVALIDATE, so the next read served zeros.
fid=$((fid + 1))
mkstub "$DATA/failfast/b3_$fid.segy" $fid $SIZE $SIZE --full
hold_reset
slow_marker 700
python3 "$T/reader.py" seek "$MNT/f/b3_$fid.segy" >"$B/b3seek.out" &
sp=$!
wait_hold || say "no marker read held after 10 s"
out=$(daemon_ctl '{"cmd": "stub", "path": "'"$DATA/failfast/b3_$fid.segy"'", "resident": '$RES'}')
wait $sp
slow_marker off
s=$(cat "$B/b3seek.out")
r=$(direct "$MNT/f/b3_$fid.segy" "$BACKEND/$fid" $((8 * MiB)))
say "stub $out; seek $s; read $r"
if jtest "$out" 'd.get("punched")' && w=$(acked_after_hold "$out"); then
	pass "B3: the stub's INVALIDATE waited for the pinned SEEK before the punch ($w)"
else
	fail "B3: INVALIDATE did not wait for SEEK: $out ${w:-}"
fi
if jtest "$s" 'd.get("hole_from_8M") == '$SIZE; then
	pass "B3: SEEK_HOLE during the stub reported no hole inside the file"
else
	fail "B3: SEEK reported a hole that is remote data: $s"
fi
if jtest "$r" 'd.get("errno") == "EIO"'; then
	pass "B3: after the stub, the punched range fails closed (EIO): no stale RESIDENT marker cached"
else
	fail "B3: read after the stub: $r (zeros mean a stale cached marker)"
fi

# --- S4: INVALIDATE for an unknown fsid drains pinned I/O ---------------------
fid=$((fid + 1))
mkstub "$DATA/failfast/s4_$fid.segy" $fid $SIZE $SIZE --full
hold_reset
slow_marker 700
direct "$MNT/f/s4_$fid.segy" "$BACKEND/$fid" $((8 * MiB)) >"$B/s4read.out" &
rp=$!
wait_hold || say "no marker read held after 10 s"
out=$(daemon_ctl '{"cmd": "invalidate", "path": "'"$DATA/failfast/s4_$fid.segy"'", "fsid": 4660}')
wait $rp
slow_marker off
r=$(cat "$B/s4read.out")
if jtest "$out" 'd.get("acked")' && w=$(acked_after_hold "$out") && jtest "$r" 'd.get("ok")'; then
	pass "S4: INVALIDATE with an unknown fsid acked only after the pinned read drained ($w)"
else
	fail "S4: unknown-fsid INVALIDATE: $out, read $r ${w:-}"
fi
grep -q 'INVALIDATE for unknown fsid 0000000000001234.*drained pinned I/O' "$LOG" &&
	pass "S4: logged as a fsid mismatch" || fail "S4: unknown fsid not logged"

# --- S7: a RESIDENT file appended over NFS ------------------------------------
fid=$((fid + 1))
mkstub "$DATA/failfast/s7_$fid.segy" $fid $SIZE $SIZE --full
head -c 8192 /dev/urandom >"$B/s7tail.bin"
cat "$B/s7tail.bin" >>"$MNT/f/s7_$fid.segy"
# the next read must not come from the cache filled before the append, as
# after any INVALIDATE, reconnect or handle release
out=$(daemon_ctl '{"cmd": "invalidate", "path": "'"$DATA/failfast/s7_$fid.segy"'", "what": 2}')
jtest "$out" 'd.get("acked")' || fail "S7: INVALIDATE not acked: $out"
r=$(direct "$MNT/f/s7_$fid.segy" "$BACKEND/$fid" $((8 * MiB)))
tail_ok=$(python3 - "$MNT/f/s7_$fid.segy" "$B/s7tail.bin" $SIZE <<'PY'
import mmap, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECT)
buf = mmap.mmap(-1, 1 << 20)
try:
    n = os.preadv(fd, [buf], int(sys.argv[3]))
    print(bytes(buf[:n]) == open(sys.argv[2], "rb").read())
except OSError as e:
    print("errno", e.errno)
PY
)
if jtest "$r" 'd.get("ok")' && [ "$tail_ok" = True ]; then
	pass "S7: a RESIDENT file appended over NFS reads correctly (old bytes and the new tail): all local"
else
	fail "S7: appended RESIDENT file: read $r, tail $tail_ok"
fi

# --- S12: a read past the cached PARTIAL size after the file grew ------------
# As replace_content does: resize first; the new marker and its INVALIDATE
# (not acknowledged) have not arrived yet.
fid=$((fid + 1))
mkstub "$DATA/failfast/s12_$fid.segy" $fid $SIZE $RES
r0=$(direct "$MNT/f/s12_$fid.segy" "$BACKEND/$fid" 0) # caches the PARTIAL marker
truncate -s $((2 * SIZE)) "$DATA/failfast/s12_$fid.segy"
r=$(direct "$MNT/f/s12_$fid.segy" "$BACKEND/$fid" $((SIZE + 8 * MiB)))
if jtest "$r0" 'd.get("ok")' && jtest "$r" 'd.get("errno") == "EIO"'; then
	pass "S12: a read past a cached PARTIAL size of a grown file re-reads the marker and fails closed (EIO), no zeros"
else
	fail "S12: header $r0, read past the old size $r"
fi

# --- S11: truncate to 0 with an unreadable marker ------------------------------
for v in 4.2 3; do
	m="$MNT/f"
	[ $v = 3 ] && m="$MNT/f3"
	fid=$((fid + 1))
	mkstub "$DATA/failfast/s11_$fid.segy" $fid $SIZE $RES --bad-crc
	timeout -k 2 4 truncate -s 0 "$m/s11_$fid.segy"
	rc=$?
	if [ $rc = 124 ] && [ "$(stat -c %s "$DATA/failfast/s11_$fid.segy")" = $SIZE ] &&
		timeout 5 stat "$m/s11_$fid.segy" >/dev/null; then
		pass "S11: v$v truncate to 0 of a file with an unreadable marker answers DELAY (blocked 4 s, file untouched)"
	else
		fail "S11: v$v truncate with a bad marker: rc $rc, size $(stat -c %s "$DATA/failfast/s11_$fid.segy")"
	fi
done

ganesha_alive || fail "ganesha.nfsd died"
finish
