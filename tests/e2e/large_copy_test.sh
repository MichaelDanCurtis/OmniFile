#!/usr/bin/env bash
# #54 (no partial versions) on the dev edge VM, as root: large NFS copies that
# go quiet partway through, and survive a Ganesha restart, against what
# docs/design/edge-metadata.md promises ("A quiet file is not always a
# finished one", and "The NFSv3 limit").
#
#   sudo /work/tests/e2e/large_copy_test.sh
#
# Each copy is one file of E2E_LARGE_MIB (default 4096) written by an ordinary
# user through the `wait` share, from one open fd:
#   - at a quarter, the writer pauses, its fd still open: a writeback stall;
#   - at half, Ganesha restarts (systemctl restart): its exit closes the fds, the
#     NFSv4 client reclaims its OPEN (or opens again: the dev Ganesha runs
#     graceless), the NFSv3 client simply retries;
#   - the copy then finishes and the fd is closed.
# The cases (Q is the edge's commit quiet_s, 3 s in dev):
#   - NFSv4.1, a pause of E2E_PAUSE_S (default 10, over 2 x Q): the OPEN holds
#     the commit, so exactly one version, the whole file;
#   - NFSv3, a pause of Q + 1 s (under 2 x Q): the size-and-mtime check holds
#     it, so exactly one version, the whole file;
#   - NFSv3, a pause of E2E_V3_LONG_PAUSE_S (default 30, over 2 x Q): the
#     documented NFSv3 limit. NFSv3 has no open state, so the file as it was
#     in the pause is committed as a version (smaller than the whole file),
#     then the finished file as the next one: exactly two versions, the last
#     the whole file.
# Every final version's blob is the source, byte for byte; no daemon panic.
# Results: tests/e2e/results/large-copy-latest.md. Steps 1-4 are the M1 exit
# test's (tests/e2e/exit_test.sh): reset, seed and enrol, daemon, Ganesha.
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PY=/opt/omnifile-service/venv/bin/python
E2E="$PY $HERE/e2e.py"
DATA=/srv/omnifile/data
MNT=/mnt/omnifile-e2e
WORK=/var/tmp/omnifile-e2e
RESULTS=${E2E_RESULTS:-$HERE/results/large-copy-latest.md}
LARGE_MIB=${E2E_LARGE_MIB:-4096}
PAUSE_S=${E2E_PAUSE_S:-10}
# The edge's commit quiet period (edge.toml [commit] quiet_s; the daemon's
# default is 10 s).
QUIET_S=$(awk '/^\[/{c=($0=="[commit]")} c && $1=="quiet_s"{print $3; exit}' /etc/omnifile/edge.toml 2>/dev/null)
QUIET_S=${QUIET_S:-10}
V3_SHORT_PAUSE_S=$((QUIET_S + 1))
V3_LONG_PAUSE_S=${E2E_V3_LONG_PAUSE_S:-30}
LIMIT="docs/design/edge-metadata.md, \"The NFSv3 limit\""
WUID=${E2E_UID:-20002}
WGID=${E2E_GID:-10000}
AS_WRITER="setpriv --reuid=$WUID --regid=$WGID --clear-groups"
NFAIL=0
NPASS=0
T0=$(date +%s)

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
set -a
# shellcheck disable=SC1091
. /etc/omnifile-dev/service.env
set +a
mkdir -p "$(dirname "$RESULTS")" "$WORK" "$MNT"
install -m 0644 "$HERE/e2e.py" "$WORK/e2e.py"
: >"$WORK/checks.txt"

say() { printf '[big %4ss] %s\n' "$(($(date +%s) - T0))" "$*"; }
pass() { say "PASS: $*"; echo "| PASS | $* |" >>"$WORK/checks.txt"; NPASS=$((NPASS + 1)); }
fail() { say "FAIL: $*"; echo "| **FAIL** | $* |" >>"$WORK/checks.txt"; NFAIL=$((NFAIL + 1)); }
check() { local d=$1; shift; if "$@"; then pass "$d"; else fail "$d"; fi; }
j() { "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); v=eval(sys.argv[2]); print(v if not isinstance(v,(dict,list)) else json.dumps(v))' "$1" "$2"; }
jt() { "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if eval(sys.argv[2]) else 1)' "$1" "$2"; }
wait_for() {
  local t=$1; shift
  local end=$(($(date +%s) + t))
  while [ "$(date +%s)" -lt "$end" ]; do
    "$@" && return 0
    sleep 0.5
  done
  "$@"
}
umount_all() {
  local m
  for m in $(findmnt -rn -o TARGET | grep "^$MNT/" || true); do
    timeout 20 umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || true
  done
}
# The verify-only drop-in below would keep stubbing off for every later lane until a reboot
# (it is in /run), as M1 and M2 remove theirs: it goes on any exit.
worker_stub_again() {
  rm -f /run/systemd/system/omnifile-dev-worker.service.d/90-e2e-verify-only.conf /run/omnifile-e2e-worker.env
  systemctl daemon-reload
  systemctl try-restart omnifile-dev-worker 2>/dev/null || true
}
cleanup() {
  umount_all
  worker_stub_again
}
trap cleanup EXIT
# So an interrupt goes through `cleanup`, with the status a signal gives (as m5_exit_test.sh).
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

say "1. reset: services, catalog, sim, edge state"
systemctl is-active -q nfs-ganesha-omni && umount_all
systemctl stop nfs-ganesha-omni omnifile-edge omnifile-dev-worker 2>/dev/null
systemctl clean --what=fdstore omnifile-edge 2>/dev/null
rm -rf /var/lib/omnifile/replica.db* /var/lib/omnifile/identity /var/lib/omnifile/outbox
install -d -m 0700 /var/lib/omnifile/identity /var/lib/omnifile/outbox
rm -rf "${DATA:?}/wait" "${DATA:?}/failfast" "${DATA:?}/.omni"
$E2E reset-db >/dev/null || { fail "catalog reset"; exit 1; }
rm -f /var/lib/omnifile-dev/sim-key.json
systemctl restart omnifile-dev-sim omnifile-dev-enrol omnifile-dev-api
wait_for 30 test -s /var/lib/omnifile-dev/sim-key.json
wait_for 30 bash -c '(exec 3<>/dev/tcp/127.0.0.1/8443) 2>/dev/null && (exec 3<>/dev/tcp/127.0.0.1/8444) 2>/dev/null'

say "2. seed the catalog and enrol the edge"
$E2E seed --enrol-url https://127.0.0.1:8444 --server-ca /etc/omnifile-dev/pki/ca.crt \
  --bundle-out "$WORK/bundle.txt" --uid "$WUID" --gid "$WGID" >/dev/null || { fail "seed"; exit 1; }
/usr/local/bin/omnifile-edge --config /etc/omnifile/edge.toml enrol "$WORK/bundle.txt" >"$WORK/enrol.out" 2>&1 ||
  { fail "enrolment: $(tail -3 "$WORK/enrol.out")"; exit 1; }
rm -f "$WORK/bundle.txt"
# The worker verifies uploads by reading them back; never stub here.
mkdir -p /run/systemd/system/omnifile-dev-worker.service.d
echo OMNIFILE_STUB_AFTER_MINUTES=525600 >/run/omnifile-e2e-worker.env
printf '[Service]\nEnvironmentFile=/run/omnifile-e2e-worker.env\n' \
  >/run/systemd/system/omnifile-dev-worker.service.d/90-e2e-verify-only.conf
systemctl daemon-reload
systemctl restart omnifile-dev-worker

say "3. the daemon; the feed materialises the shares"
systemctl start omnifile-edge
check "share roots materialised" wait_for 60 test -d "$DATA/wait"

say "4. Ganesha"
cat >/etc/ganesha/ganesha.conf <<EOF
# Written by tests/e2e/large_copy_test.sh.
%include "/etc/ganesha/omnifile-base.conf"
NFS_KRB5 { Active_krb5 = false; }
EXPORT_DEFAULTS { Access_Type = RW; Squash = Root_Squash; SecType = sys; Protocols = 3, 4; Transports = TCP; }
EXPORT {
  Export_Id = 11; Path = $DATA/wait; Pseudo = /e2e/wait;
  FSAL { Name = OMNI; Policy = "wait"; FSAL { Name = VFS; } }
}
EOF
systemctl start nfs-ganesha-omni
check "Ganesha up, FSAL_OMNI connected" wait_for 30 bash -c \
  "journalctl -q -o cat -u nfs-ganesha-omni --since @$T0 | grep -q 'connected to the OmniFile daemon'"
check "the FSAL negotiated OPEN_STATE (feature bit 5)" wait_for 30 bash -c \
  "journalctl -q -o cat -u omnifile-edge --since @$T0 | grep 'fsal hello' | grep -Eq 'features[=\":]+(3[2-9]|[4-9][0-9]|[1-9][0-9]{2,})'"

# The writer: one open fd, a pause at a quarter, progress in $2.progress.
cat >"$WORK/slow_copy.py" <<'PY'
import os, sys, time
src, dst, pause = sys.argv[1], sys.argv[2], float(sys.argv[3])
size = os.stat(src).st_size
prog = dst.rsplit("/", 1)[-1]
progress = sys.argv[4]
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
done, paused = 0, False
with open(src, "rb") as s:
    while True:
        b = s.read(4 << 20)
        if not b:
            break
        os.write(fd, b)
        done += len(b)
        # Every 64 MiB on the server: the progress is the server's, not the
        # client's page cache, so the restart lands mid-transfer.
        if done % (64 << 20) == 0:
            os.fsync(fd)
        with open(progress, "w") as p:
            p.write(str(done * 100 // size))
        if not paused and done >= size // 4:
            paused = True
            time.sleep(pause)
os.close(fd)
with open(progress, "w") as p:
    p.write("closed")
PY
chmod 0644 "$WORK/slow_copy.py"

run_copy() { # vers name seed pause expect (one | partial)
  local v=$1 name=$2 seed=$3 pause=$4 expect=$5 m="$MNT/wait-v${1//./}" src="$WORK/big-$2"
  mkdir -p "$m"
  if [ "$v" = 3 ]; then
    mount -t nfs -o vers=3,proto=tcp,nolock,hard,timeo=50 "127.0.0.1:$DATA/wait" "$m" || { fail "v$v mount"; return; }
  else
    mount -t nfs -o "vers=$v,proto=tcp,hard,timeo=50" "127.0.0.1:/e2e/wait" "$m" || { fail "v$v mount"; return; }
  fi
  local s
  s=$($E2E pattern "$src" $((LARGE_MIB << 20)) "$seed") || { fail "source"; return; }
  chmod 0644 "$src"
  local want_crc want_size
  want_crc=$(j "$s" 'd["crc64nvme"]'); want_size=$(j "$s" 'd["size"]')
  install -d -m 1777 "$WORK/progress"
  local progress="$WORK/progress/$name"
  rm -f "$progress"
  say "v$v: copying ${LARGE_MIB} MiB into wait/$name (pause ${pause}s at 25% with quiet_s ${QUIET_S}, Ganesha restart at 50%)"
  $AS_WRITER python3 "$WORK/slow_copy.py" "$src" "$m/$name" "$pause" "$progress" &
  local wp=$!
  wait_for 1200 bash -c "p=\$(cat '$progress' 2>/dev/null); [ \"\$p\" = closed ] || [ \"\${p:-0}\" -ge 50 ]"
  local g0
  g0=$(date +%s)
  systemctl restart nfs-ganesha-omni
  wait_for 60 bash -c "journalctl -q -o cat -u nfs-ganesha-omni --since @$g0 | grep -q 'connected to the OmniFile daemon'"
  say "v$v: Ganesha restarted at $(cat "$progress")% (back in $(($(date +%s) - g0)) s)"
  wait $wp
  local rc=$?
  check "v$v: the copy finished through the restart (writer exit $rc)" [ "$rc" = 0 ]
  committed() {
    local row
    row=$($E2E file wait "$name") || return 1
    jt "$row" "d.get('state') == 'resident' and d.get('location_state') == 'committed' and d.get('crc64nvme') == '$want_crc'"
  }
  if wait_for 600 committed; then
    pass "v$v: wait/$name committed with the whole file's CRC-64/NVME ($want_crc, $want_size bytes)"
  else
    fail "v$v: wait/$name not committed whole: $($E2E file wait "$name")"
  fi
  # Give a late commit time to show.
  sleep 20
  local vs
  vs=$($E2E versions wait "$name")
  if [ "$expect" = one ]; then
    check "v$v, a ${pause} s pause: exactly one committed content version (no partial one): $vs" \
      jt "$vs" "len(d['versions']) == 1 and d['versions'][0]['crc64nvme'] == '$want_crc'"
  else
    # NFSv3 has no open state: a pause over 2 x quiet_s commits the file as it
    # was then. The documented limit, not a regression.
    check "v$v, a ${pause} s pause (over 2 x quiet_s = $((2 * QUIET_S)) s): the documented extra partial version ($LIMIT), then the whole file: $vs" \
      jt "$vs" "len(d['versions']) == 2 and 0 < d['versions'][0]['size'] < $want_size and d['versions'][-1]['size'] == $want_size and d['versions'][-1]['crc64nvme'] == '$want_crc'"
  fi
  local blob
  blob=$($E2E blob wait "$name")
  check "v$v: its blob is the source, byte for byte" jt "$blob" "d.get('exists') and d['size'] == $want_size and d['crc64nvme'] == '$want_crc'"
  umount "$m" 2>/dev/null || umount -l "$m"
  rm -f "$src"
}

say "5. NFSv4.1, a ${PAUSE_S} s pause: the OPEN holds the commit"
run_copy 4.1 big-v41.segy 541 "$PAUSE_S" one
say "6. NFSv3, a ${V3_SHORT_PAUSE_S} s pause (under 2 x quiet_s): the size-and-mtime check holds it"
run_copy 3 big-v3.segy 503 "$V3_SHORT_PAUSE_S" one
say "6b. NFSv3, a ${V3_LONG_PAUSE_S} s pause (over 2 x quiet_s): $LIMIT"
run_copy 3 big-v3-long.segy 507 "$V3_LONG_PAUSE_S" partial

say "7. logs"
check "no panic in the daemon journal" bash -c \
  "! journalctl -q -o cat -u omnifile-edge --since @$T0 | grep -q 'panicked'"
waits=$(journalctl -q -o cat -u omnifile-edge --since @$T0 | grep -c 'waiting for the writer\|still not committed' || true)
say "commits held for the writer (debug lines are off at info): $waits"

{
  echo "# #54 large-copy test, $(date -u +%FT%TZ)"
  echo
  echo "- rev: ${E2E_REV:-unknown}; ${LARGE_MIB} MiB per copy, a Ganesha restart at 50%; quiet_s ${QUIET_S}"
  echo "- pauses at 25%: NFSv4.1 ${PAUSE_S} s (one version); NFSv3 ${V3_SHORT_PAUSE_S} s (one version) and ${V3_LONG_PAUSE_S} s (the extra partial version of $LIMIT)"
  echo "- $NPASS passed, $NFAIL failed"
  echo
  echo "| | check |"
  echo "|---|---|"
  cat "$WORK/checks.txt"
} >"$RESULTS"
say "results: $RESULTS ($NPASS passed, $NFAIL failed)"
[ "$NFAIL" = 0 ]
