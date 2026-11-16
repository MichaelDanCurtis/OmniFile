#!/usr/bin/env bash
# A local line-rate ceiling (docs/BUILD-PLAN.md M7, "200 GB SEG-Y at line rate", whose real
# run is pilot 2 at a client site): one large file through the whole data path on the dev
# edge VM, as root, each stage timed on its own:
#   1. written over NFSv4.1 (hard mount, 8 MiB writes, then fsync): the landing zone;
#   2. uploaded: the daemon's quiet period, hash and upload to the sim, until the catalog has
#      the committed version;
#   3. stubbed (stub after 0 minutes, from then on);
#   4. recalled: the caches dropped, the stub read whole over NFSv4.1 with O_DIRECT, the read
#      waiting on the fill from the sim; then the local copy's SHA-256 checked;
#   5. the file deleted.
#
#   sudo tests/scale-edge/line_rate.sh [--gib 10]
#
# Run after tests/chaos/storage.sh on: the data volume is then a loop file on the repo
# mount (virtiofs, on the Mac's external SSD), and the figures include that. The sim is a
# Python process that holds an uploaded blob in RAM, about twice during the commit; its
# peak RSS is recorded. Results: tests/e2e/results/scale-edge-linerate.md.
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
GIB=10
RESULTS=$REPO/tests/e2e/results/scale-edge-linerate.md
while [ $# -gt 0 ]; do
  case "$1" in
    --gib) GIB=$2; shift ;;
    --results) RESULTS=$2; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
WORK=/var/tmp/omnifile-linerate
MNT=/mnt/omnifile-linerate
rm -rf "$WORK"
install -d -m 0755 "$WORK"
# shellcheck source=../chaos/lib/devstack.sh
. "$REPO/tests/chaos/lib/devstack.sh"
SIZE=$((GIB << 30))
NAME=linerate.bin
say() { printf '[line-rate %s] %s\n' "$(date +%T)" "$*"; }
now() { date +%s.%N; }
mbps() { awk -v b="$1" -v s="$2" 'BEGIN { if (s > 0) printf "%.0f", b / s / 1e6; else print "-" }'; }
secs() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.1f", b - a }'; }
ROWS=()
row() { ROWS+=("| $1 | $2 | $3 | $4 |"); say "$1: $2 s, $3 MB/s ($4)"; }

cleanup() {
  [ -n "${MW:-}" ] && kill "$MW" 2>/dev/null
  setpriv --reuid=20002 --regid=10000 --clear-groups rm -f "$MNT/$NAME" 2>/dev/null
  timeout 30 umount "$MNT" 2>/dev/null || umount -f -l "$MNT" 2>/dev/null
  rm -f "$DATA/wait/$NAME"
  worker_env_clear
  systemctl restart omnifile-dev-worker 2>/dev/null
  systemctl stop nfs-ganesha-omni 2>/dev/null
}
trap cleanup EXIT

say "stack: reset and seed; Ganesha on the appliance's base config; no stubbing yet"
stack_reset_seed || exit 1
worker_env OMNIFILE_STUB_AFTER_MINUTES=525600 OMNIFILE_WORKER_MAX_SWEEP_S=5
ganesha_conf_appliance
systemctl stop nfs-ganesha-omni 2>/dev/null
ganesha_start_wait "$(date +%s)" || { say "Ganesha did not start"; exit 1; }
install -d "$MNT"
mount -t nfs -o vers=4.1,proto=tcp,hard,timeo=50 127.0.0.1:/e2e/wait "$MNT" || exit 1
avail=$(df -B1 --output=avail "$DATA" | tail -1)
[ "$avail" -gt $((SIZE + (4 << 30))) ] || { say "not enough room on $DATA ($avail bytes)"; exit 1; }

# The sim's resident memory, and the VM's free memory, every 2 s.
SIMPID=$(systemctl show -p MainPID --value omnifile-dev-sim)
( while :; do
    echo "$(ps -o rss= -p "$SIMPID" 2>/dev/null | tr -d ' ') $(awk '/MemAvailable/ {print $2}' /proc/meminfo)" >>"$WORK/mem.txt"
    sleep 2
  done ) &
MW=$!

# 8 MiB writes; every MiB carries its own index, so a misplaced range is caught.
cat >"$WORK/gen.py" <<'EOF'
import hashlib, os, sys, time
MiB = 1 << 20
path, size, mode, basefile = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
base = bytearray(open(basefile, "rb").read())
def chunks(base):
    for i in range(size // MiB):
        base[:8] = i.to_bytes(8, "little")
        yield bytes(base)
if mode == "write":
    t = time.time()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    buf = []
    for c in chunks(base):
        buf.append(c)
        if len(buf) == 8:
            os.write(fd, b"".join(buf)); buf = []
    if buf:
        os.write(fd, b"".join(buf))
    t1 = time.time()
    os.fsync(fd)
    os.close(fd)
    print(f"{t} {t1} {time.time()}")
else:  # sha256 of what was written
    h = hashlib.sha256()
    for c in chunks(base):
        h.update(c)
    print(h.hexdigest())
EOF
say "1. write $GIB GiB over NFSv4.1"
head -c 1048576 /dev/urandom >"$WORK/base"
chmod 0644 "$WORK/base" "$WORK/gen.py"
read -r tw0 tw1 tw2 < <(setpriv --reuid=20002 --regid=10000 --clear-groups python3 "$WORK/gen.py" "$MNT/$NAME" "$SIZE" write "$WORK/base")
WANT=$(python3 "$WORK/gen.py" - "$SIZE" sha "$WORK/base")
row "NFSv4.1 write, to the last write() returning" "$(secs "$tw0" "$tw1")" "$(mbps "$SIZE" "$(secs "$tw0" "$tw1")")" "client-side, the page cache absorbing"
row "NFSv4.1 write, through fsync" "$(secs "$tw0" "$tw2")" "$(mbps "$SIZE" "$(secs "$tw0" "$tw2")")" "on the edge's data volume"

say "2. upload: until the catalog has the committed version"
# The whole file: the daemon may commit snapshots of it while it is still being written.
committed() { $E2E file wait "$NAME" | $PY -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if (d.get("content_version_id") or 0) > 0 and d.get("location_state") == "committed" and d.get("size") == int(sys.argv[1]) else 1)' "$SIZE"; }
ds_wait 3600 committed || { say "never committed"; exit 1; }
tc=$(now)
row "upload: fsync to committed in the catalog (quiet period 3 s, hash, upload, commit)" "$(secs "$tw2" "$tc")" "$(mbps "$SIZE" "$(secs "$tw2" "$tc")")" "edge to the sim"
verified() { $E2E file wait "$NAME" | grep -q '"verified": true'; }
ds_wait 3600 verified && tv=$(now) && row "verified by the worker (the sim's bytes read back)" "$(secs "$tc" "$tv")" "$(mbps "$SIZE" "$(secs "$tc" "$tv")")" "service side"
# Every content version the daemon committed for the file, with its size and time.
VERSIONS=$($PY - "$NAME" <<'PYEOF'
import os, sys, psycopg
with psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"]) as c:
    rows = c.execute("SELECT v.size, to_char(v.created_at, 'HH24:MI:SS.MS') FROM catalog.file_version v"
                     " JOIN catalog.file f USING (file_id) WHERE f.name = %s AND v.kind = 'content'"
                     " AND v.state = 'committed' ORDER BY v.seq", (sys.argv[1],)).fetchall()
print("; ".join(f"{s} bytes at {t}" for s, t in rows))
PYEOF
)
say "content versions committed: $VERSIONS"
blob=$($E2E blob wait "$NAME")
grep -q "\"sha256\": \"$WANT\"" <<<"$blob" && say "the sim's blob has the file's SHA-256" || { say "blob differs: $blob"; exit 1; }

say "3. stub it"
worker_env OMNIFILE_STUB_AFTER_MINUTES=0 OMNIFILE_WORKER_MAX_SWEEP_S=5
stubbed() { $E2E local "$DATA/wait/$NAME" | $PY -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d["marker"] and d["marker"]["state"] == "PARTIAL" and d["allocated"] < 64 << 20 else 1)'; }
ds_wait 1800 stubbed || { say "never stubbed"; exit 1; }
say "stub: $($E2E local "$DATA/wait/$NAME")"

say "4. recall: read the stub whole over NFSv4.1 (O_DIRECT), waiting on the fill"
sync; echo 3 >/proc/sys/vm/drop_caches
tr0=$(now)
setpriv --reuid=20002 --regid=10000 --clear-groups dd if="$MNT/$NAME" of=/dev/null bs=8M iflag=direct status=none ||
  { say "the read failed"; exit 1; }
tr1=$(now)
row "recall: NFSv4.1 read of the stub, the fill from the sim behind it" "$(secs "$tr0" "$tr1")" "$(mbps "$SIZE" "$(secs "$tr0" "$tr1")")" "sim to the edge to the client"
got=$(sha256sum "$DATA/wait/$NAME" | cut -d' ' -f1)
[ "$got" = "$WANT" ] && say "the filled file has the right SHA-256" || { say "filled file differs: $got"; exit 1; }
sync; echo 3 >/proc/sys/vm/drop_caches
th0=$(now)
setpriv --reuid=20002 --regid=10000 --clear-groups dd if="$MNT/$NAME" of=/dev/null bs=8M iflag=direct status=none
th1=$(now)
row "the same read once filled (resident)" "$(secs "$th0" "$th1")" "$(mbps "$SIZE" "$(secs "$th0" "$th1")")" "the volume through Ganesha"
kill "$MW" 2>/dev/null
MW=""
simpeak=$(awk '{print $1}' "$WORK/mem.txt" | sort -n | tail -1)
memmin=$(awk '{print $2}' "$WORK/mem.txt" | sort -n | head -1)

say "5. delete the file"
setpriv --reuid=20002 --regid=10000 --clear-groups rm -f "$MNT/$NAME"
{
  echo "# Line-rate ceiling, $GIB GiB: $(date -u +%FT%TZ)"
  echo
  echo "kernel \`$(uname -r)\`, $(nproc) vCPUs, $(awk '/MemTotal/ {print int($2/1048576)}' /proc/meminfo) GiB; daemon $(sha256sum /usr/local/bin/omnifile-edge | cut -c1-16); NFS client and server on the same VM (127.0.0.1); the data volume a loop file on $(losetup -n -O BACK-FILE "$(findmnt -n -o SOURCE --mountpoint "$DATA")") (virtiofs to the Mac's external SSD); the backend the dev sim (Python, in RAM)."
  echo
  echo "| stage | seconds | MB/s | path |"
  echo "|---|---|---|---|"
  printf '%s\n' "${ROWS[@]}"
  echo
  echo "Content versions the daemon committed for the file, in order: $VERSIONS. The write ran from $(date -d @"${tw0%.*}" +%T) to $(date -d @"${tw2%.*}" +%T): any version smaller than the file is a snapshot taken while it was still being written."
  echo
  echo "Bytes: $SIZE ($GIB GiB); SHA-256 $WANT, checked in the sim's blob and in the filled file. The sim's RSS peaked at $((simpeak / 1024)) MiB; the VM's MemAvailable fell to $((memmin / 1024)) MiB. The file was deleted afterwards."
} >"$RESULTS"
say "results: $RESULTS"
