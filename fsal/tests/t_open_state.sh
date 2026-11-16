#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# OPEN_STATE_QUERY (docs/contracts/fsal-socket.md 5.5). Before it commits a
# file, the daemon asks the FSAL whether an NFSv4 client still holds it open
# for write: a client copying a large file can go quiet for longer than the
# commit quiet period (a writeback stall) while its OPEN stays in place, and
# a version committed then is a partial one. The answer counts the NFSv4
# share states open for write on the file:
#  - an NFSv4.0/4.1/4.2 write open counts 1 while it is held and 0 once it is
#    closed;
#  - a read-only open counts 0;
#  - an OPEN_DOWNGRADE (the writer closes, a reader of the same owner stays)
#    drops the count to 0, and an upgrade raises it; a reader that joins a
#    writer (an upgrade with only READ) keeps it;
#  - NFSv3 has no open state, so a v3 writer counts 0 (the daemon's growth
#    checks cover it);
#  - a file nobody opened counts 0, and the FSAL answers at once.
. "$(dirname "$0")/lib.sh"
setup_volumes
ganesha_start || exit 1
daemon_start
mount_v 4.2 failfast "$MNT/f42"
mount_v 4.1 failfast "$MNT/f41"
mount_v 4.0 failfast "$MNT/f40"
mount_v 3 failfast "$MNT/f3"

q() { daemon_ctl '{"cmd": "open_state", "path": "'"$DATA/failfast/$1"'"}'; }
# until_count <name> <n>: poll up to 3 s for write_opens == n (a client sends
# CLOSE and OPEN_DOWNGRADE asynchronously after close(2))
until_count() {
	local i out
	for i in $(seq 1 30); do
		out=$(q "$1")
		jtest "$out" "d['answered'] and d['write_opens'] == $2" && { echo "$out"; return 0; }
		sleep 0.1
	done
	echo "$out"
	return 1
}
# hold <mode> <file> <ready> <release>: open the file (w, r, rw), write a
# little if writable, print to <ready>, keep it open until <release> exists
hold() {
	python3 - "$@" <<'PY' &
import os, sys, time
mode, path, ready, release = sys.argv[1:5]
flags = {"w": os.O_WRONLY | os.O_CREAT, "r": os.O_RDONLY, "rw": os.O_RDWR | os.O_CREAT}[mode]
fd = os.open(path, flags, 0o644)
if mode != "r":
    os.write(fd, b"x" * 65536)
    os.fsync(fd)
open(ready, "w").write("1")
while not os.path.exists(release):
    time.sleep(0.05)
os.close(fd)
PY
}
wait_file() { local i; for i in $(seq 1 50); do [ -e "$1" ] && return 0; sleep 0.1; done; return 1; }

# --- a write open counts while held, per NFSv4 minor version ------------------
for v in 4.2 4.1 4.0; do
	m="$MNT/f${v/./}"
	n="w$v.dat"
	rm -f "$B/ready" "$B/release"
	hold w "$m/$n" "$B/ready" "$B/release"
	hp=$!
	wait_file "$B/ready"
	held=$(until_count "$n" 1)
	rc1=$?
	touch "$B/release"
	wait $hp
	closed=$(until_count "$n" 0)
	rc2=$?
	if [ $rc1 = 0 ] && [ $rc2 = 0 ]; then
		pass "v$v write open: 1 while held, 0 after close ($held -> $closed)"
	else
		fail "v$v write open: held $held, closed $closed"
	fi
done

# --- a read-only open counts 0 --------------------------------------------------
echo data >"$DATA/failfast/r.dat"
rm -f "$B/ready" "$B/release"
hold r "$MNT/f42/r.dat" "$B/ready" "$B/release"
hp=$!
wait_file "$B/ready"
sleep 0.3
out=$(q r.dat)
touch "$B/release"
wait $hp
if jtest "$out" 'd["answered"] and d["write_opens"] == 0'; then
	pass "v4.2 read-only open: 0 ($out)"
else
	fail "v4.2 read-only open: $out"
fi

# --- upgrade and downgrade of one open owner's state ----------------------------
# A reader opens first; a writer of the same user upgrades the state (OPEN
# with more access, reopen2); closing the writer downgrades it (OPEN_DOWNGRADE,
# reopen2 again) while the reader holds on.
echo data >"$DATA/failfast/ud.dat"
rm -f "$B/ready" "$B/release" "$B/ready2" "$B/release2"
hold r "$MNT/f42/ud.dat" "$B/ready" "$B/release"
rp=$!
wait_file "$B/ready"
hold w "$MNT/f42/ud.dat" "$B/ready2" "$B/release2"
wp=$!
wait_file "$B/ready2"
up=$(until_count ud.dat 1)
rc1=$?
touch "$B/release2"
wait $wp
down=$(until_count ud.dat 0)
rc2=$?
touch "$B/release"
wait $rp
if [ $rc1 = 0 ] && [ $rc2 = 0 ]; then
	pass "v4.2 upgrade to write then downgrade with a reader still open: $up -> $down"
else
	fail "v4.2 upgrade/downgrade: upgraded $up, downgraded $down"
fi

# --- a reader joins a writer: still open for write (#54 review, B1) ----------
# The same open owner: the reader's OPEN upgrades the writer's state, and
# Ganesha calls reopen2 with only the reader's access, ORing it into the
# state's share_access afterwards. The count follows share_access, so the
# writer is not dropped; closing the writer downgrades the state to read.
echo data >"$DATA/failfast/wr.dat"
rm -f "$B/ready" "$B/release" "$B/ready2" "$B/release2"
hold w "$MNT/f42/wr.dat" "$B/ready" "$B/release"
wp=$!
wait_file "$B/ready"
first=$(until_count wr.dat 1)
rc0=$?
hold r "$MNT/f42/wr.dat" "$B/ready2" "$B/release2"
rp=$!
wait_file "$B/ready2"
sleep 0.5
joined=$(q wr.dat)
touch "$B/release"
wait $wp
down=$(until_count wr.dat 0)
rc2=$?
touch "$B/release2"
wait $rp
if [ $rc0 = 0 ] && jtest "$joined" 'd["answered"] and d["write_opens"] == 1' && [ $rc2 = 0 ]; then
	pass "v4.2 a reader joins a writer: still 1 ($first -> $joined), 0 once the writer closes ($down)"
else
	fail "v4.2 writer then reader: writer $first, with the reader $joined, writer closed $down"
fi

# --- NFSv3 has no open state ----------------------------------------------------
rm -f "$B/ready" "$B/release"
hold w "$MNT/f3/v3.dat" "$B/ready" "$B/release"
hp=$!
wait_file "$B/ready"
out=$(q v3.dat)
touch "$B/release"
wait $hp
if jtest "$out" 'd["answered"] and d["write_opens"] == 0'; then
	pass "v3 writer: no open state, 0 (the daemon's growth checks cover v3): $out"
else
	fail "v3 writer: $out"
fi

# --- a file nobody opened, answered at once -------------------------------------
echo data >"$DATA/failfast/idle.dat"
t0=$(now)
out=$(q idle.dat)
t1=$(now)
if jtest "$out" 'd["answered"] and d["write_opens"] == 0' &&
	python3 -c "import sys; sys.exit(0 if $t1 - $t0 < 0.5 else 1)"; then
	pass "a file nobody opened: 0, answered at once: $out"
else
	fail "idle file: $out in $(python3 -c "print(round($t1 - $t0, 3))") s"
fi
ganesha_alive || fail "ganesha.nfsd died"
finish
