#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# The spike's threading finding, as a regression test: with 32 readers stuck
# on stubs of the wait share and the daemon HUNG, reads on an unrelated
# export must not slow down. The spike's synchronous FSAL fell from ~18,000
# to 768 reads in 8 s (max 1.8 s); the production FSAL never waits on the
# daemon in a Ganesha worker.
. "$(dirname "$0")/lib.sh"
N=${N:-32}
setup_volumes
ganesha_start || exit 1
python3 -c 'open("'"$PLAIN"'/lat.bin","wb").write(bytes(range(256))*(256*1024))'
for i in $(seq 1 "$N"); do mkstub "$DATA/wait/t$i.segy" $((6000 + i)) $SIZE $RES; done
mount_v 4.2 wait "$MNT/tw"
mount_v 4.2 plain "$MNT/tp"
lat() {
	python3 - "$MNT/tp/lat.bin" <<'PY'
import json, mmap, os, sys, time
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECT)
b = mmap.mmap(-1, 1 << 20)
L = []
end = time.monotonic() + 8
while time.monotonic() < end:
    for off in range(0, 64 << 20, 1 << 20):
        t = time.monotonic()
        os.preadv(fd, [b], off)
        L.append(time.monotonic() - t)
L.sort()
print(json.dumps({"reads": len(L), "p50_ms": round(1e3 * L[len(L) // 2], 1),
                  "p99_ms": round(1e3 * L[int(len(L) * .99)], 1), "max_ms": round(1e3 * L[-1], 1)}))
PY
}
daemon_stop
base=$(lat)
say "no stuck readers:            $base"
for mode in answering hung-hello hung; do
	case $mode in
	answering) daemon_start --recall-delay 600 ;;
	hung-hello) daemon_start --mode hang-hello ;;
	hung) daemon_start --mode hang ;;
	esac
	pids=
	for i in $(seq 1 "$N"); do
		timeout 25 cat "$MNT/tw/t$i.segy" >/dev/null 2>&1 &
		pids="$pids $!"
	done
	sleep 3
	r=$(lat)
	say "daemon $mode, $N stuck readers: $r; ganesha threads=$(ls /proc/"$(ganesha_pid)"/task | wc -l)"
	if python3 -c 'import json,sys; b=json.loads(sys.argv[1]); r=json.loads(sys.argv[2]); sys.exit(0 if r["reads"] >= b["reads"] / 3 and r["max_ms"] < 500 else 1)' "$base" "$r"; then
		pass "daemon $mode with $N stuck readers: unrelated export keeps $(echo "$r" | python3 -c 'import json,sys;print(json.load(sys.stdin)["reads"])') reads in 8 s (baseline $(echo "$base" | python3 -c 'import json,sys;print(json.load(sys.stdin)["reads"])')), max $(echo "$r" | python3 -c 'import json,sys;print(json.load(sys.stdin)["max_ms"])') ms"
	else
		fail "daemon $mode: unrelated export degraded: $r vs baseline $base"
	fi
	kill $pids 2>/dev/null
	wait $pids 2>/dev/null
	daemon_stop
	sleep 1
done
ganesha_alive || fail "ganesha.nfsd died"
finish
