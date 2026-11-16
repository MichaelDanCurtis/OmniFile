#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# The stub handshake (docs/design/fsal-namespace.md 5.1) against the FSAL:
# the daemon narrows the marker, sends INVALIDATE with ACK_REQUIRED, and
# punches only after INVALIDATE_ACK. The FSAL must drop its cached marker and
# drain I/O that saw the old one before acking, so a read never sees a hole
# that the cache still called resident: the result is EIO, never zeros, even
# with a reader hammering the file while it is stubbed.
. "$(dirname "$0")/lib.sh"
setup_volumes
ganesha_start || exit 1
daemon_start --recall-delay 60
mount_v 4.2 failfast "$MNT/f"

fid=8001
mkstub "$DATA/failfast/inv.segy" $fid $SIZE $SIZE --full
r=$(reader one "$MNT/f/inv.segy" "$BACKEND/$fid" --start $((8 * MiB)))
jtest "$r" 'd.get("ok")' || fail "resident read before stubbing: $r"
out=$(daemon_ctl '{"cmd": "stub", "path": "'"$DATA/failfast/inv.segy"'", "resident": '$RES'}')
if jtest "$out" 'd.get("acked") and d.get("punched")'; then
	pass "stub handshake: INVALIDATE acked by the FSAL in $(echo "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin)["ack_ms"])') ms, holes punched"
else
	fail "stub handshake: $out"
fi
r=$(reader one "$MNT/f/inv.segy" "$BACKEND/$fid" --start $((8 * MiB)))
r0=$(reader one "$MNT/f/inv.segy" "$BACKEND/$fid" --start 0)
if jtest "$r" 'd.get("errno") == "EIO"' && jtest "$r0" 'd.get("ok")'; then
	pass "after stubbing, the cached RESIDENT marker is gone: punched range -> EIO, header still reads"
else
	fail "after stubbing: punched range $r, header $r0"
fi

# A reader hammering the file (O_DIRECT, so every read reaches the server)
# while it is stubbed: every read is correct or EIO; no zeros.
fid=8002
mkstub "$DATA/failfast/inv2.segy" $fid $SIZE $SIZE --full
python3 - "$MNT/f/inv2.segy" "$BACKEND/$fid" >"$B/hammer.out" <<'PY' &
import errno, json, mmap, os, sys, time
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECT)
want = open(sys.argv[2], "rb").read()
buf = mmap.mmap(-1, 1 << 20)
ok = eio = bad = zeros = 0
end = time.monotonic() + 4
while time.monotonic() < end:
    for off in range(0, 64 << 20, 1 << 20):
        try:
            n = os.preadv(fd, [buf], off)
            if bytes(buf[:n]) == want[off:off + n]:
                ok += 1
            else:
                bad += 1
                zeros += bytes(buf[:n]).count(0) > n // 2
        except OSError as e:
            if e.errno == errno.EIO:
                eio += 1
            else:
                bad += 1
print(json.dumps({"ok": ok, "eio": eio, "bad": bad, "zeros": zeros}))
PY
hp=$!
sleep 1
out=$(daemon_ctl '{"cmd": "stub", "path": "'"$DATA/failfast/inv2.segy"'", "resident": '$RES'}')
wait $hp
h=$(cat "$B/hammer.out")
if jtest "$out" 'd.get("punched")' && jtest "$h" 'd["bad"] == 0 and d["zeros"] == 0 and d["ok"] > 0 and d["eio"] > 0'; then
	pass "stubbing under a concurrent O_DIRECT reader: $h (no wrong bytes, no zeros)"
else
	fail "stubbing under load: handshake $out, reader $h"
fi
ganesha_alive || fail "ganesha.nfsd died"
finish
