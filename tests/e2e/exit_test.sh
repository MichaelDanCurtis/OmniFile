#!/usr/bin/env bash
# M1 exit test (docs/BUILD-PLAN.md M1, docs/milestones/M1.md), on the edge
# machine itself, as root: the dev VM (tests/e2e/run.sh drives it from the
# Mac) or the e2e CI runner (.github/workflows/e2e.yml).
#
# Needs, installed beforehand:
#   dev/scripts/install-ganesha.sh   Ganesha V15.6 + FSAL_OMNI + its real unit
#   dev/scripts/install-edge.sh      omnifile-edge + its unit + /etc/omnifile/edge.toml
#   dev/scripts/install-service.sh   Postgres, sim facade, enrol, api, worker
#   an xfs volume at /srv/omnifile/data
#
# What it proves (each line of the results file is one check):
#   1. enrolment over TLS; the edge materialises both shares from the feed;
#   2. Ganesha runs under nfs-ganesha-omni.service with the unit's
#      capabilities (CAP_SYS_ADMIN, no CAP_SYS_MODULE/RAWIO): not privileged;
#   3. files copied in over NFSv3, 4.1 and 4.2 are detected, uploaded and
#      committed; each blob exists in sim with the size and CRC64-NVME of the
#      source, plain bytes (same SHA-256), POSIX metadata in the edge format,
#      and no explicit access tier;
#   3b. an upload the outbox holds (Blob refusing commits) survives a daemon
#      SIGKILL: the restarted daemon replays the same op_id, one version;
#   4. the "stub after N minutes" policy (N = 0) stubs them: marker PARTIAL
#      at headers + 1 MiB, holes punched, disk usage dropped, catalog `stub`;
#   5. reads back byte-identical over NFSv3, 4.1 and 4.2 on the `wait` share
#      and on the `failfast` share;
#   6. daemon killed (SIGKILL) in the middle of a fill with two readers
#      waiting: systemd restarts it, Ganesha stays up, no reader ever sees a
#      zero block, both end byte-identical;
#   7. fill fence (fsal-socket.md 6.1): after a fill, truncate and cp over the
#      file proceed at once; after a failed fill (sim outage) the read fails
#      (EIO) and truncate proceeds; after a daemon kill mid-fill, truncate
#      proceeds once the new daemon has resynced;
#   8. failfast + backend outage: EIO, then the same read succeeds once the
#      backend is back.
# Every commit must be verified by the service, never on the edge's word:
# verified_by = upload_block_crc (a backend that lists block CRCs) or
# azure_read_crc (the verify queue read the blob back; the sim, like Azure,
# lists none). Archived-data failfast, with the export's
# Policy rendered from the catalog, and the daemon held down (not restarted)
# while NFSv3/4.1/4.2 readers probe an unfilled range, are in m2_exit_test.sh
# (steps 9 and 11), which runs on the same stack.
#
# Environment: E2E_RESULTS (results file), E2E_REV (commit, for the results;
# the dev VM can't read a worktree's .git), E2E_BIG_MIB (default 256),
# E2E_MIB (default 96), E2E_KEEP=1 (leave mounts and services up).
set -uo pipefail
# bash reads a script as it runs: run from a private copy, so editing the
# file during a run cannot change the run.
if [ -z "${E2E_COPY:-}" ]; then
  copy=$(mktemp /tmp/omnifile-e2e-XXXXXX.sh)
  cp "$0" "$copy"
  E2E_COPY=1 E2E_SELF_SHA=$(sha256sum "$0" | cut -d' ' -f1) \
    E2E_HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) exec bash "$copy" "$@"
fi

HERE=${E2E_HERE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
REPO=$(cd "$HERE/../.." && pwd)
PY=/opt/omnifile-service/venv/bin/python
E2E="$PY $HERE/e2e.py"
DATA=/srv/omnifile/data
MNT=/mnt/omnifile-e2e
WORK=/var/tmp/omnifile-e2e
# compare/flood as an unprivileged user: the system python3 (stdlib only).
# Run from a world-readable copy in $WORK (made below): the repo may sit under a
# home the readers cannot traverse, as on GitHub runners where /home/runner is 0750.
E2EU="python3 $WORK/e2e.py"
RESULTS=${E2E_RESULTS:-$HERE/results/latest.md}
BIG_MIB=${E2E_BIG_MIB:-256}
MIB=${E2E_MIB:-96}
# The writer: the shares squash root (sec=sys), so files are written, read back,
# truncated and overwritten as an ordinary user (geo-writer in the dev AD; any
# uid works). The edge normalises `other` away (access-control.md 1.1), so a
# squashed root can't read them either.
WUID=${E2E_UID:-20002}
WGID=${E2E_GID:-10000}
AS_WRITER="setpriv --reuid=$WUID --regid=$WGID --clear-groups"
# SEG-Y headers + 1 MiB (worker DEFAULT_RESIDENT_BYTES), rounded up to a page by
# the edge (pre-content events are page-granular).
RESIDENT=$(( (3600 + 1048576 + 4095) / 4096 * 4096 ))
VERSIONS=(3 4.1 4.2)
SHARES=(wait failfast)
NFAIL=0
NPASS=0
T0=$(date +%s)

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
set -a
# shellcheck disable=SC1091
. /etc/omnifile-dev/service.env
set +a
mkdir -p "$(dirname "$RESULTS")" "$WORK" "$MNT"
install -m 0755 -d "$WORK"
install -m 0644 "$HERE/e2e.py" "$WORK/e2e.py"
: >"$WORK/checks.txt"

since() { awk -v a="$1" -v b="$(date +%s%N)" 'BEGIN { printf "%.2f", (b - a) / 1e9 }'; }
say() { printf '[e2e %4ss] %s\n' "$(($(date +%s) - T0))" "$*"; }
pass() { say "PASS: $*"; echo "| PASS | $* |" >>"$WORK/checks.txt"; NPASS=$((NPASS + 1)); }
fail() { say "FAIL: $*"; echo "| **FAIL** | $* |" >>"$WORK/checks.txt"; NFAIL=$((NFAIL + 1)); }
check() { # check "description" command...
  local d=$1; shift
  if "$@"; then pass "$d"; else fail "$d"; fi
}
j() { "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); v=eval(sys.argv[2]); print(v if not isinstance(v,(dict,list)) else json.dumps(v))' "$1" "$2"; }
jt() { "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if eval(sys.argv[2]) else 1)' "$1" "$2"; }
# wait_for SECONDS command... : poll every 0.5 s
wait_for() {
  local t=$1; shift
  local end=$(($(date +%s) + t))
  while [ "$(date +%s)" -lt "$end" ]; do
    "$@" && return 0
    sleep 0.5
  done
  "$@"
}
# The worker verifies committed uploads by reading them back (worker/verify.py:
# Azure lists no block CRCs) and runs the "stub after N minutes" policy. While
# commits are checked it runs with stubbing pushed out; then N = 0.
worker_verify_only() {
  mkdir -p /run/systemd/system/omnifile-dev-worker.service.d
  echo OMNIFILE_STUB_AFTER_MINUTES=525600 >/run/omnifile-e2e-worker.env
  printf '[Service]\nEnvironmentFile=/run/omnifile-e2e-worker.env\n' \
    >/run/systemd/system/omnifile-dev-worker.service.d/90-e2e-verify-only.conf
  systemctl daemon-reload
  systemctl restart omnifile-dev-worker
}
worker_stub_now() {
  rm -f /run/systemd/system/omnifile-dev-worker.service.d/90-e2e-verify-only.conf /run/omnifile-e2e-worker.env
  systemctl daemon-reload
  systemctl restart omnifile-dev-worker
}
mnt_of() { echo "$MNT/$1-v${2//./}"; } # share version
ganesha_pid() { systemctl show -p MainPID --value nfs-ganesha-omni; }
edge_pid() { systemctl show -p MainPID --value omnifile-edge; }
journal_since() { journalctl -q --no-pager -o cat -u "$1" --since "@$T0" 2>/dev/null; }

umount_all() {
  local m ms
  ms=$(findmnt -rn -o TARGET | grep "^$MNT/" || true)
  [ -n "$ms" ] || return 0
  # Hard mounts of a server that is gone hang umount in the kernel (D state):
  # make sure Ganesha is there to answer before unmounting.
  systemctl is-active -q nfs-ganesha-omni || { systemctl start nfs-ganesha-omni; sleep 3; }
  for m in $ms; do
    timeout 20 umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || true
  done
}
mount_all() {
  local s v m src opts
  for s in "${SHARES[@]}"; do
    for v in "${VERSIONS[@]}"; do
      m=$(mnt_of "$s" "$v")
      mkdir -p "$m"
      if [ "$v" = 3 ]; then
        src="127.0.0.1:$DATA/$s"; opts="vers=3,proto=tcp,nolock,hard,timeo=50"
      else
        src="127.0.0.1:/e2e/$s"; opts="vers=$v,proto=tcp,hard,timeo=50"
      fi
      mount -t nfs -o "$opts" "$src" "$m" || return 1
    done
  done
}
remount_cold() { umount_all; sync; echo 3 >/proc/sys/vm/drop_caches; mount_all; }

cleanup() {
  [ "${E2E_KEEP:-0}" = 1 ] && return
  umount_all
  # Never leave stubbing off for the next lane: the drop-in is in /run until a reboot.
  worker_stub_now
}
trap cleanup EXIT
# So an interrupt goes through `cleanup`, with the status a signal gives (as m5_exit_test.sh).
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------------------------
say "1. reset: services, catalog, sim, edge state"
umount_all
systemctl stop nfs-ganesha-omni omnifile-edge omnifile-dev-worker 2>/dev/null
# Release the smb-guard's group (`omnifile-guardctl clean`): it outlives the daemon in the fd
# store, and its marks on unfilled rewind directories would hold the rm below.
systemctl clean --what=fdstore omnifile-edge 2>/dev/null
rm -rf /var/lib/omnifile/replica.db* /var/lib/omnifile/identity /var/lib/omnifile/outbox
install -d -m 0700 /var/lib/omnifile/identity /var/lib/omnifile/outbox
rm -rf "${DATA:?}/wait" "${DATA:?}/failfast" "${DATA:?}/.omni"
$E2E reset-db >/dev/null || { fail "catalog reset"; exit 1; }
rm -f /var/lib/omnifile-dev/sim-key.json
systemctl restart omnifile-dev-sim omnifile-dev-enrol
systemctl restart omnifile-dev-api
wait_for 30 test -s /var/lib/omnifile-dev/sim-key.json
wait_for 30 bash -c '(exec 3<>/dev/tcp/127.0.0.1/8443) 2>/dev/null && (exec 3<>/dev/tcp/127.0.0.1/8444) 2>/dev/null'

say "2. seed the catalog and enrol the edge"
seed=$($E2E seed --enrol-url https://127.0.0.1:8444 --server-ca /etc/omnifile-dev/pki/ca.crt \
  --bundle-out "$WORK/bundle.txt" --uid "$WUID" --gid "$WGID") || { fail "seed"; exit 1; }
say "seeded: $seed"
if /usr/local/bin/omnifile-edge --config /etc/omnifile/edge.toml enrol "$WORK/bundle.txt" >"$WORK/enrol.out" 2>&1; then
  pass "enrolment over TLS with the pinned CA: $(grep '^enrolled:' "$WORK/enrol.out")"
else
  fail "enrolment: $(tail -3 "$WORK/enrol.out")"; exit 1
fi
rm -f "$WORK/bundle.txt"

say "3. start the daemon; the feed materialises the shares"
systemctl start omnifile-edge
check "omnifile-edge.service active (startup checks: xfs, CAP_SYS_ADMIN)" wait_for 30 systemctl is-active -q omnifile-edge
check "share roots materialised from the feed (wait, failfast)" wait_for 60 test -d "$DATA/wait" -a -d "$DATA/failfast"
check "share roots carry the catalog's owner and mode ($WUID:$WGID 0755)" \
  bash -c "[ \"\$(stat -c '%u:%g %a' $DATA/wait $DATA/failfast | sort -u)\" = '$WUID:$WGID 755' ]"

say "4. start Ganesha under its real unit"
cat >/etc/ganesha/ganesha.conf <<EOF
# Written by tests/e2e/exit_test.sh: the installed base (dev/edge/ganesha-base.conf,
# including MDCACHE Close_Fast) plus one OMNI export per share.
%include "/etc/ganesha/omnifile-base.conf"
# M1 exports are sec=sys only; Kerberos (the nfs/ keytab from the AD join) is
# M2. Without this, Ganesha's default Active_krb5 logs a startup CRIT on a
# machine with no keytab (a bare CI runner).
NFS_KRB5 { Active_krb5 = false; }
EXPORT_DEFAULTS { Access_Type = RW; Squash = Root_Squash; SecType = sys; Protocols = 3, 4; Transports = TCP; }
EXPORT {
  Export_Id = 11; Path = $DATA/wait; Pseudo = /e2e/wait;
  FSAL { Name = OMNI; Policy = "wait"; FSAL { Name = VFS; } }
}
EXPORT {
  Export_Id = 12; Path = $DATA/failfast; Pseudo = /e2e/failfast;
  FSAL { Name = OMNI; Policy = "failfast"; FSAL { Name = VFS; } }
}
EOF
G0=$(date +%s)
systemctl start nfs-ganesha-omni
check "nfs-ganesha-omni.service active" wait_for 30 systemctl is-active -q nfs-ganesha-omni
check "Ganesha initialised" wait_for 30 bash -c "journalctl -q -o cat -u nfs-ganesha-omni --since @$T0 | grep -q 'NFS SERVER INITIALIZED'"
check "FSAL_OMNI connected to the daemon" wait_for 30 bash -c "journalctl -q -o cat -u nfs-ganesha-omni --since @$T0 | grep -q 'connected to the OmniFile daemon'"
gpid=$(ganesha_pid)
capeff=$(awk '/^CapEff/ {print $2}' "/proc/$gpid/status")
capbnd=$(awk '/^CapBnd/ {print $2}' "/proc/$gpid/status")
cap_has() { (( (16#$1 >> $2) & 1 )); }
if cap_has "$capeff" 21 && ! cap_has "$capbnd" 16 && ! cap_has "$capbnd" 17 && ! cap_has "$capbnd" 19; then
  pass "Ganesha runs with the unit's capability set: CAP_SYS_ADMIN effective; no CAP_SYS_MODULE/RAWIO/PTRACE in its bounding set (CapEff=$capeff CapBnd=$capbnd); not --privileged"
else
  fail "Ganesha capabilities CapEff=$capeff CapBnd=$capbnd"
fi
# A CRIT here would be FSAL_OMNI without CAP_SYS_ADMIN, or D-Bus refusing
# Ganesha. ("disconnected from the OmniFile daemon" is CRIT by design, logged
# when the daemon goes away; it belongs to steps 10 and 13, not here.)
check "no CRIT in the Ganesha log at startup (FSAL_OMNI has CAP_SYS_ADMIN; D-Bus policy installed)" \
  bash -c "! journalctl -q -o cat -u nfs-ganesha-omni --since @$G0 | grep ':CRIT' | grep -v 'disconnected from the OmniFile daemon'"
check "NFSv3, v4.1 and v4.2 mounts of both exports" mount_all

# ---------------------------------------------------------------------------------
say "5. copy pattern files in over every NFS version"
declare -A SRC SIZE
add_file() { # share name mib seed version
  local s=$1 n=$2 mib=$3 seed=$4 v=$5 src="$WORK/src-$1-$2"
  $E2E pattern "$src" $((mib * 1048576)) "$seed" >/dev/null
  SRC[$s/$n]=$src; SIZE[$s/$n]=$((mib * 1048576))
  local t
  t=$(date +%s%N)
  if $AS_WRITER cp "$src" "$(mnt_of "$s" "$v")/$n" && $AS_WRITER sync -f "$(mnt_of "$s" "$v")/$n"; then
    pass "wrote $s/$n ($mib MiB) over NFSv$v in $(since "$t") s"
  else
    fail "write $s/$n over NFSv$v"
  fi
}
FILES=()
seedn=1
for s in "${SHARES[@]}"; do
  for v in "${VERSIONS[@]}"; do
    n="line_v${v//./}.segy"
    mib=$MIB; [ "$s/$v" = wait/4.2 ] && mib=$BIG_MIB
    add_file "$s" "$n" "$mib" $seedn "$v"; FILES+=("$s/$n"); seedn=$((seedn + 1))
  done
done
add_file wait kill.segy "$MIB" 101 4.2; FILES+=(wait/kill.segy)
add_file wait fence-fail.segy 48 102 4.2; FILES+=(wait/fence-fail.segy)
add_file wait fence-kill.segy 48 103 4.2; FILES+=(wait/fence-kill.segy)
add_file failfast outage.segy 48 104 4.2; FILES+=(failfast/outage.segy)

say "6. wait for detect -> upload -> commit (and the service's CRC verification), then check every blob in sim"
worker_verify_only
committed() { # share/name
  local f=$1 row mk
  row=$($E2E file "${f%%/*}" "${f#*/}") || return 1
  jt "$row" 'd.get("state") == "resident" and d.get("location_state") == "committed" and d.get("verified") and d.get("verified_by") in ("upload_block_crc", "azure_read_crc")' || return 1
  mk=$($E2E local "$DATA/$f") || return 1
  jt "$mk" "d['marker'] and d['marker']['state'] == 'RESIDENT' and d['marker']['version_id'] == $(j "$row" 'd["content_version_id"]')"
}
for f in "${FILES[@]}"; do
  t=$(date +%s)
  if wait_for 240 committed "$f"; then
    row=$($E2E file "${f%%/*}" "${f#*/}")
    pass "$f committed in $(($(date +%s) - t)) s after the copy (file_id $(j "$row" 'd["file_id"]'), version $(j "$row" 'd["content_version_id"]'), verified_by $(j "$row" 'd["verified_by"]'))"
  else
    fail "$f not committed within 240 s (catalog: $($E2E file "${f%%/*}" "${f#*/}"); local: $($E2E local "$DATA/$f"))"
  fi
done
for f in "${FILES[@]}"; do
  row=$($E2E file "${f%%/*}" "${f#*/}")
  blob=$($E2E blob "${f%%/*}" "${f#*/}")
  src=$($E2E local "${SRC[$f]}" --hash)
  want_crc=$(j "$src" 'd["crc64nvme"]'); want_sha=$(j "$src" 'd["sha256"]')
  if jt "$blob" "d.get('exists') and d['size'] == ${SIZE[$f]} and d['crc64nvme'] == '$want_crc' and d['sha256'] == '$want_sha'" \
    && [ "$(j "$row" 'd["crc64nvme"]')" = "$want_crc" ]; then
    pass "$f: blob $(j "$blob" 'd["object_key"]') in sim, ${SIZE[$f]} bytes, CRC64-NVME $want_crc = source = catalog, SHA-256 equal (plain bytes)"
  else
    fail "$f: blob $blob vs source crc $want_crc sha $want_sha, catalog crc $(j "$row" 'd.get("crc64nvme")')"
  fi
  if jt "$blob" "d['metadata'].get('file_owner') == '$WUID' and d['metadata'].get('file_group') == '$WGID' and d['metadata'].get('file_permissions') == '0640' and not d.get('access_tier_inferred') in ('false', 'False')"; then
    [ "$f" = "${FILES[0]}" ] && pass "POSIX metadata in the edge format (file_owner=$WUID file_group=$WGID file_permissions=0640: written 0644, 'other' normalised away by the edge, access-control.md 1.1); no explicit access tier sent (tier $(j "$blob" 'd.get("access_tier")'), inferred $(j "$blob" 'd.get("access_tier_inferred")'))"
  else
    fail "$f: blob metadata/tier $(j "$blob" '{"m": d.get("metadata"), "t": d.get("access_tier"), "i": d.get("access_tier_inferred")}')"
  fi
done

say "6b. an upload held in the outbox survives a daemon restart"
f=wait/outbox.segy
OUTBOX=/var/lib/omnifile/outbox/ops
$E2E fault '{"op": "put", "outage_s": 600}' >/dev/null
add_file wait outbox.segy 48 105 4.2; FILES+=("$f")
held() { compgen -G "$OUTBOX/*.json" >/dev/null && journalctl -q -o cat -u omnifile-edge --since "@$T0" | grep -q 'upload held'; }
if wait_for 180 held; then
  op_id=$("$PY" -c 'import glob,json; print(" ".join(json.load(open(p))["upload"]["op_id"] for p in glob.glob("'"$OUTBOX"'/*.json")))')
  pass "upload of $f held in the outbox while Blob refuses Put Block List (op $op_id recorded, clone kept: $(ls "$DATA/.omni/commit" | wc -l) clone(s))"
else
  op_id=""
  fail "upload of $f never held in the outbox: $(ls "$OUTBOX" 2>&1 | head -3)"
fi
systemctl kill -s KILL omnifile-edge
$E2E fault '{"clear": true}' >/dev/null
say "killed the daemon with the op in flight; Blob is back"
wait_for 30 systemctl is-active -q omnifile-edge
if wait_for 120 committed "$f"; then
  v=$($E2E versions wait outbox.segy)
  if jt "$v" "len(d['versions']) == 1 and d['versions'][0]['edge_op_id'] == '$op_id'"; then
    pass "after the restart the outbox replayed op $op_id: $f committed as exactly one version ($(j "$v" 'd["versions"][0]["version_id"]')), marked resident"
  else
    fail "$f versions after the replay: $v (held op $op_id)"
  fi
  blob=$($E2E blob wait outbox.segy)
  src=$($E2E local "${SRC[$f]}" --hash)
  jt "$blob" "d.get('exists') and d['crc64nvme'] == '$(j "$src" 'd["crc64nvme"]')' and d['size'] == ${SIZE[$f]}" \
    && pass "$f: replayed blob has the source's size and CRC64-NVME" || fail "$f replayed blob: $blob"
else
  fail "$f not committed after the restart: $($E2E file wait outbox.segy)"
fi
check "outbox drained and commit clones deleted" wait_for 30 bash -c "! compgen -G '$OUTBOX/*.json' >/dev/null && [ -z \"\$(ls -A $DATA/.omni/commit)\" ]"

# ---------------------------------------------------------------------------------
say "7. the 'stub after N minutes' policy (N = OMNIFILE_STUB_AFTER_MINUTES = $OMNIFILE_STUB_AFTER_MINUTES)"
stubbed() { # the last state seen goes to $WORK/stubbed-last.txt, for diagnosis
  local f=$1 row mk
  row=$($E2E file "${f%%/*}" "${f#*/}") || return 1
  mk=$($E2E local "$DATA/$f") || return 1
  printf 'catalog %s\nlocal %s\n' "$row" "$mk" >"$WORK/stubbed-last.txt"
  jt "$row" 'd.get("state") == "stub"' || return 1
  jt "$mk" "d['marker'] and d['marker']['state'] == 'PARTIAL' and d['marker']['resident'] == $RESIDENT and $RESIDENT <= d['first_hole'] <= $RESIDENT + 4096 and d['data_after_hole'] is None and d['allocated'] <= $RESIDENT + 4 * 1048576"
}
worker_stub_now
t=$(date +%s)
for f in "${FILES[@]}"; do
  t1=$(date +%s)
  stubbed "$f" || cp "$WORK/stubbed-last.txt" "$WORK/stubbed-first-miss.txt" 2>/dev/null
  if wait_for 180 stubbed "$f"; then
    mk=$($E2E local "$DATA/$f")
    [ $(($(date +%s) - t1)) -gt 20 ] && say "slow stub check for $f; first miss: $(tr '\n' ' ' <"$WORK/stubbed-first-miss.txt" | cut -c1-600)"
    pass "$f stubbed: marker PARTIAL, resident $RESIDENT (headers + 1 MiB, page-aligned), holes from $(j "$mk" 'd["first_hole"]') to EOF (no data extent after it), disk usage dropped to $(j "$mk" 'd["allocated"]') of ${SIZE[$f]} bytes, catalog state stub"
  else
    fail "$f not stubbed (catalog: $($E2E file "${f%%/*}" "${f#*/}"); local: $($E2E local "$DATA/$f"))"
  fi
done
say "all stubbed $(($(date +%s) - t)) s after the worker started"
systemctl stop omnifile-dev-worker
df_after=$(df -B1 --output=used "$DATA" | tail -1)
say "data volume used after stubbing: $df_after bytes"

# ---------------------------------------------------------------------------------
say "8. read back, cold client cache, every NFS version, both policies"
remount_cold || fail "remount"
for s in "${SHARES[@]}"; do
  for v in "${VERSIONS[@]}"; do
    f="$s/line_v${v//./}.segy"
    r=$($AS_WRITER $E2EU compare "$(mnt_of "$s" "$v")/${f#*/}" "${SRC[$f]}")
    if jt "$r" 'd["identical"] and d["zero_blocks"] == 0'; then
      pass "$f read back over NFSv$v ($s policy): byte-identical, ${SIZE[$f]} bytes in $(j "$r" 'd["seconds"]') s (recall fill inside the read)"
    else
      fail "$f over NFSv$v: $r"
    fi
    mk=$($E2E local "$DATA/$f")
    jt "$mk" "d['marker']['state'] == 'RESIDENT'" || fail "$f not RESIDENT after the fill: $mk"
  done
done

# ---------------------------------------------------------------------------------
say "9. fill fence after a completed fill: truncate and cp over the filled file"
f=wait/line_v42.segy
t=$(date +%s%N)
if timeout 20 $AS_WRITER truncate -s 0 "$(mnt_of wait 4.2)/line_v42.segy"; then
  pass "truncate -s 0 over NFSv4.2 of $f right after its fill: $(since "$t") s (no lingering DELAY)"
else
  fail "truncate after a completed fill hung or failed"
fi
f=wait/line_v41.segy
head -c 1000000 "${SRC[wait/kill.segy]}" >"$WORK/new.bin"
t=$(date +%s%N)
if timeout 20 $AS_WRITER cp "$WORK/new.bin" "$(mnt_of wait 4.1)/line_v41.segy" && cmp -s "$WORK/new.bin" "$DATA/$f"; then
  pass "cp of new bytes over the just-filled $f (NFSv4.1): $(since "$t") s, exactly the new bytes on disk"
else
  fail "cp over a just-filled file hung, failed or left other bytes"
fi

# ---------------------------------------------------------------------------------
say "10. kill the daemon in the middle of a fill, with two readers waiting"
f=wait/kill.segy
$E2E fault '{"op": "get_range", "latency_ms": 400}' >/dev/null
$AS_WRITER $E2EU compare "$(mnt_of wait 4.2)/kill.segy" "${SRC[$f]}" >"$WORK/kill-r1.json" &
r1=$!
$AS_WRITER $E2EU compare "$(mnt_of wait 3)/kill.segy" "${SRC[$f]}" >"$WORK/kill-r2.json" &
r2=$!
filling() { local mk; mk=$($E2E local "$DATA/$f"); jt "$mk" "d['marker'] and d['marker']['resident'] > $RESIDENT and d['marker']['resident'] < d['size']"; }
if wait_for 60 filling; then
  pass "fill of $f under way: $(j "$($E2E local "$DATA/$f")" 'd["marker"]["resident"]') of ${SIZE[$f]} bytes local"
else
  fail "fill of $f never seen in progress: $($E2E local "$DATA/$f")"
fi
gpid_before=$(ganesha_pid); epid_before=$(edge_pid)
nr_before=$(systemctl show -p NRestarts --value omnifile-edge)
kill_ts=$(date +%s)
systemctl kill -s KILL omnifile-edge
say "killed omnifile-edge (pid $epid_before) with SIGKILL"
sleep 3
$E2E fault '{"clear": true}' >/dev/null
if wait_for 30 bash -c "[ \"\$(systemctl show -p NRestarts --value omnifile-edge)\" -gt $nr_before ] && systemctl is-active -q omnifile-edge"; then
  pass "systemd restarted the daemon (pid $epid_before -> $(edge_pid), RestartMode=direct)"
else
  fail "daemon not restarted"
fi
if [ "$(ganesha_pid)" = "$gpid_before" ] && systemctl is-active -q nfs-ganesha-omni; then
  pass "Ganesha stayed up through the daemon crash (same pid $gpid_before; BindsTo does not fire on a direct restart)"
else
  fail "Ganesha restarted or stopped with the daemon (pid $gpid_before -> $(ganesha_pid))"
fi
ok=1
for r in $r1 $r2; do
  waited=0
  while kill -0 "$r" 2>/dev/null && [ $waited -lt 300 ]; do sleep 1; waited=$((waited + 1)); done
  kill -0 "$r" 2>/dev/null && { kill "$r"; ok=0; }
  wait "$r" 2>/dev/null
done
for n in 1 2; do
  r=$(cat "$WORK/kill-r$n.json" 2>/dev/null || echo '{}')
  if jt "$r" 'd.get("identical") and d.get("zero_blocks") == 0'; then
    pass "reader $n ($([ $n = 1 ] && echo NFSv4.2 || echo NFSv3)) across the daemon kill: byte-identical, no zero block, no error ($(j "$r" 'd["seconds"]') s)"
  else
    fail "reader $n across the daemon kill: $r"; ok=0
  fi
done
[ $ok = 1 ] && pass "no reader saw zeros while the daemon was dead (fail closed)"
check "the restarted daemon took the FSAL connection back (fsal hello after the kill)" bash -c "journalctl -q -o cat -u omnifile-edge --since @$kill_ts | grep -q 'fsal hello'"

# ---------------------------------------------------------------------------------
say "11. fill fence after a failed fill (sim outage on get_range)"
f=wait/fence-fail.segy
$E2E fault '{"op": "get_range", "outage_s": 600}' >/dev/null
t=$(date +%s)
r=$(timeout 240 $AS_WRITER $E2EU compare "$(mnt_of wait 4.2)/fence-fail.segy" "${SRC[$f]}")
rc=$?
if [ $rc -eq 124 ]; then
  fail "read of $f during a backend outage hung for 240 s"
elif jt "$r" 'd["error"] and "errno 5" in d["error"] and d["zero_blocks"] == 0'; then
  pass "read of $f during a backend outage failed with EIO after $(($(date +%s) - t)) s (fill FAILED; wait share), no zeros"
else
  fail "read of $f during a backend outage: $r"
fi
t=$(date +%s%N)
if timeout 20 $AS_WRITER truncate -s 0 "$(mnt_of wait 4.2)/fence-fail.segy"; then
  pass "truncate -s 0 of $f after its fill failed: $(since "$t") s (fence lifted by FAILED)"
else
  fail "truncate after a failed fill hung (fence never lifted)"
fi
$E2E fault '{"clear": true}' >/dev/null

say "12. failfast + backend outage: EIO, then the same read succeeds"
f=failfast/outage.segy
$E2E fault '{"op": "get_range", "outage_s": 600}' >/dev/null
t=$(date +%s)
r=$(timeout 240 $AS_WRITER $E2EU compare "$(mnt_of failfast 4.2)/outage.segy" "${SRC[$f]}")
if jt "$r" 'd["error"] and "errno 5" in d["error"] and d["zero_blocks"] == 0'; then
  pass "failfast read of $f during the outage: EIO after $(($(date +%s) - t)) s, no zeros"
else
  fail "failfast read during the outage: $r"
fi
$E2E fault '{"clear": true}' >/dev/null
sleep 6 # the FAILED answer lasts recall_valid_ms; the daemon then forgets it
r=$(timeout 240 $AS_WRITER $E2EU compare "$(mnt_of failfast 4.2)/outage.segy" "${SRC[$f]}")
if jt "$r" 'd["identical"]'; then
  pass "failfast read of $f after the outage: byte-identical ($(j "$r" 'd["seconds"]') s)"
else
  fail "failfast read after the outage: $r"
fi

say "13. fill fence across a daemon kill mid-fill"
f=wait/fence-kill.segy
$E2E fault '{"op": "get_range", "latency_ms": 500}' >/dev/null
$AS_WRITER $E2EU compare "$(mnt_of wait 4.2)/fence-kill.segy" "${SRC[$f]}" >"$WORK/fence-kill.json" &
rk=$!
if wait_for 60 bash -c "$E2E local $DATA/$f | $PY -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d[\"marker\"][\"resident\"] > $RESIDENT else 1)'"; then
  pass "fill of $f under way"
else
  fail "fill of $f never started"
fi
kill -9 $rk 2>/dev/null; wait $rk 2>/dev/null
systemctl kill -s KILL omnifile-edge
$E2E fault '{"clear": true}' >/dev/null
wait_for 30 systemctl is-active -q omnifile-edge
t=$(date +%s%N)
if timeout 30 $AS_WRITER truncate -s 0 "$(mnt_of wait 4.2)/fence-kill.segy"; then
  pass "truncate -s 0 of $f after a daemon kill mid-fill: $(since "$t") s after the restart (resync dropped FETCHING)"
else
  fail "truncate after a daemon kill mid-fill hung (FETCHING survived the resync)"
fi
check "$f is empty on disk after the truncate" bash -c "[ \$(stat -c %s $DATA/$f) -eq 0 ]"

# ---------------------------------------------------------------------------------
say "14. disk usage of a file still stubbed (outbox.segy)"
# The stub punch also frees xfs's post-EOF preallocation (Ganesha's
# open/write/close pattern defeats xfs's trim on close), so the space is back
# when the stub is made, not at xfs's eofblocks scan minutes later.
mk=$($E2E local "$DATA/wait/outbox.segy")
check "disk usage of wait/outbox.segy stayed at the resident prefix: allocated $(j "$mk" 'd["allocated"]') of ${SIZE[wait/outbox.segy]} bytes; volume used $(df -B1 --output=used "$DATA" | tail -1) bytes" \
  jt "$mk" "d['allocated'] <= $RESIDENT + 4 * 1048576"

say "15. logs"
check "no panic in the daemon journal" bash -c "! journalctl -q -o cat -u omnifile-edge --since @$T0 | grep -q 'panicked'"
check "no malformed FSAL message in the daemon journal (NS_HINT and friends decode)" bash -c "! journalctl -q -o cat -u omnifile-edge --since @$T0 | grep -q 'malformed message'"
check "Ganesha still active at the end" systemctl is-active -q nfs-ganesha-omni

# ---------------------------------------------------------------------------------
{
  echo "# M1 exit test results"
  echo
  echo "- When: $(date -u +%Y-%m-%dT%H:%M:%SZ), $(($(date +%s) - T0)) s"
  echo "- Where: $(hostname), $(. /etc/os-release; echo "$PRETTY_NAME"), kernel $(uname -r), $(nproc) CPUs"
  echo "- Repository: HEAD ${E2E_HEAD:-unknown} (${E2E_REV:-$(git -c safe.directory='*' -C "$REPO" describe --always --dirty 2>/dev/null || echo unknown)})"
  echo "- Test script: tests/e2e/exit_test.sh sha256 ${E2E_SELF_SHA:-unknown}"
  echo "- Ganesha: $(/opt/ganesha/bin/ganesha.nfsd -v 2>&1 | head -1); FSAL_OMNI sha256 $(sha256sum /opt/ganesha/lib/ganesha/libfsalomni.so | cut -c1-16)"
  echo "- Daemon: $(/usr/local/bin/omnifile-edge --version), sha256 $(sha256sum /usr/local/bin/omnifile-edge | cut -c1-16)"
  echo "- Postgres: $("$PY" -c 'import os,psycopg; print(psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"]).execute("show server_version").fetchone()[0])' 2>/dev/null)"
  echo "- Files: ${#FILES[@]} (largest ${BIG_MIB} MiB, others ${MIB} or 48 MiB), seeded pseudo-random"
  echo "- Result: **$NPASS passed, $NFAIL failed**"
  echo
  echo "| | Check |"
  echo "|---|---|"
  cat "$WORK/checks.txt"
  echo
  echo "## Notes"
  echo
  echo "- **failfast on hot data waits.** M1 has no Archive tier, so every stub is ONLINE (readable by range GET). The daemon answers DELAY (or parks the read) for ONLINE data even on failfast shares, because a fetch takes seconds (crates/omnifile-edge/src/core.rs, on_read_intent); failfast's immediate EIO is for archived data (M3). EIO on failfast is exercised here through a backend outage (step 12): the fill fails after the Blob client's retries (about 40 s), the read gets EIO, and the same read succeeds once Blob is back."
  echo "- **Ganesha runs with MDCACHE Close_Fast** (dev/edge/ganesha-base.conf). Without it Ganesha's fd cache keeps an NFSv3 writer's fd open, the stub handshake's write-lease check refuses the file, and files written over NFSv3 never stub."
  echo "- **One machine.** The service (Postgres 17, sim Blob facade, enrol, api, worker) runs on the edge machine itself, over 127.0.0.1; shares are sec=sys with root squashed, files are written as uid $WUID."
  echo "- **Recall here means an online fetch.** M1 reads hot data back by range GET into the stub (no rehydrate); the catalog keeps the file in state stub after an online fetch, since the service is not told (recall_filled is for archive recalls)."
} >"$RESULTS"
say "results: $RESULTS ($NPASS passed, $NFAIL failed)"
rm -f "$WORK"/src-* "$WORK/new.bin"
[ "$NFAIL" -eq 0 ]
