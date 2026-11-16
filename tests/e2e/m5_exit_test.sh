#!/usr/bin/env bash
# M5 exit test, NFS half (docs/BUILD-PLAN.md M5, docs/milestones/M5.md): files
# that still live in S3 are served over NFS by the same share, from wherever
# the bytes are. Runs on the M1 stack (exit_test.sh's header lists what to
# install), as root, with the sim facade hosting its S3 sim (`--s3`, as
# dev/scripts/install-service.sh starts it). The service half is
# service/tests/api/test_m5_exit.py.
#
# NFS reads go through libnfs (fsal/tests/nfsc.py), not the kernel client:
# it never retries, so each JUKEBOX / DELAY the share answers is seen, and it
# runs where the kernel has no NFS client (a container).
#
# What it proves (each line of the results file is one check):
#   1. a bucket with a Standard and an archived object, registered as a
#      source of the `wait` share's dataset (the partner API's
#      POST /v1/sources does the same), is seeded by the worker from a
#      listing: both files are in the catalog with their S3 locations, and
#      the bytes are nowhere in Azure;
#   2. the edge materialises both as placeholders (no data on disk) and
#      both are listed over NFSv3 and NFSv4;
#   3. a read of the Standard object over NFSv3 first answers JUKEBOX ("not
#      yet"), never ENOENT; the recall is filled from S3 through a presigned
#      URL (the only place the bytes are); the read then returns the S3
#      object's bytes, byte-identical, no zero blocks, and so does NFSv4;
#   4. the filled file is a whole local copy (CRC-64/NVME of the object)
#      and the catalog never saw an upload: still one location, on S3;
#   5. the archived object answers JUKEBOX / DELAY and starts an S3 restore
#      (recall `rehydrating`); once the restore completes (the sim's clock
#      moves past it) the same read returns its bytes over NFSv3 and v4;
#   6. SMB Previous Versions (rewind trees): a standalone smbd with the
#      stanza omniacl renders lists a folder of today's label, which the
#      daemon's smb-guard fills first (it was unfilled), and reads a file
#      from it; placeholders show as offline to SMB clients, filled files
#      don't. Needs smbd, smbclient and the omniacl example;
#   7. "Previous Versions -> Open" on a file in a subfolder of an unfilled
#      label: NOT_FOUND through shadow_copy2 alone; with vfs_omnifile_prevver
#      (samba-vfs/, built by samba-vfs/build.sh or $OMNIFILE_PREVVER_SO) in
#      Ubuntu's smbd, the file's bytes, with the label and folder filled;
#   9. shares added and removed in the catalog are rendered by the daemon and
#      served by Ganesha (M6);
#  10. users' renames, moves, deletes and chowns, over NFS and from a shell,
#      reach the catalog: a renamed file keeps its id, deleted ones stay gone;
#  11. with the control API down, reads of S3-only bytes fail closed (never
#      bytes, never ENOENT, nothing written), and succeed once it is back.
#  12. network loss: every packet from the edge daemon to the backends and
#      the control API is dropped, so its requests hang rather than fail.
#      NFS calls still answer at once, reads of S3-only bytes fail closed, a
#      file written meanwhile is kept. The link then comes back as through
#      a stateful firewall that forgot the open flows (they stay dropped),
#      and the read returns the bytes and the write is uploaded and
#      committed, with the daemon never restarted. Needs nft.
#  13. the daemon killed (SIGKILL) in the middle of a whole-file fill from S3
#      (a restored Glacier object: online objects take the chunk path since
#      decision 44), with an NFSv3 and an NFSv4 reader waiting: no reader ever
#      gets bytes before the whole-file CRC matched, nor an error; systemd
#      restarts the daemon (Ganesha stays up), the fill resumes where it
#      recorded its progress (trusted.omnifile.fetched) instead of from 0, and
#      both readers get the object's bytes.
#  14. range reads of a large S3-resident object (decision 44): ranges at
#      random offsets are byte-identical and fetch only the chunks they touch
#      (the sim counts the bytes sent), the file stays a placeholder with a
#      chunk map, never filled whole; after a SIGKILL of the daemon the same
#      ranges are served from the chunks already on disk.
#  15. a migration of a prefix (POST /v1/locations' request_location, the
#      worker's sim copier): a user deletes a file over NFS; an NFSv4 reader
#      holds a file open; a user reads a Glacier object no copy has reached:
#      the restore is staged (an S3 copy into OmniFile's staging bucket), a
#      staging run copies it into Azure, it is verified and its bytes served;
#      the prefix cuts over using that copy, with no rerun of the whole
#      prefix, and the staging object is deleted. The held fd reads the whole
#      file across the cutover, byte-identical, same inode; an object first
#      read after it comes from Azure; the cutover report lists the deleted
#      file with who deleted it (the NFS user's identity, claimed).
#  16. no write reached a source bucket in the whole run (decision 40: the
#      sim's per-bucket counters of PUT, COPY, DELETE, tagging, retention and
#      bucket policy requests by OmniFile); its only S3 writes were to its
#      staging bucket.
#
# Environment: E2E_RESULTS (results file), E2E_REV (commit, for the
# results), E2E_KEEP=1 (leave services up), E2E_IO_FLUSHER_FAIL_OK=1 (a
# container without CAP_SYS_RESOURCE: Ganesha's allow_set_io_flusher_fail),
# OMNIFILE_PREVVER_SO (vfs_omnifile_prevver for step 8; default
# samba-vfs/build/, then the one dev/scripts/install-e2e-tools.sh staged).
set -uo pipefail
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
WORK=/var/tmp/omnifile-e2e-m5
RESULTS=${E2E_RESULTS:-$HERE/results/m5-latest.md}
WUID=${E2E_UID:-20002}
WGID=${E2E_GID:-10000}
OWNERS_SID=S-1-5-21-1000-2000-3000-512
# libnfs as the writer (the shares squash root), from a world-readable copy.
NFSC="setpriv --reuid=$WUID --regid=$WGID --clear-groups python3 $WORK/nfsc.py"
STD_SIZE=$((8 << 20 | 12345)) # not a multiple of a block or a range
GL_SIZE=$((3 << 20))
NFAIL=0
NPASS=0
T0=$(date +%s)
GANESHA_STARTS=1

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
set -a
# shellcheck disable=SC1091
. /etc/omnifile-dev/service.env
set +a
grep -q -- '--s3' /etc/systemd/system/omnifile-dev-sim.service ||
  { echo "the sim facade has no S3 sim: rerun dev/scripts/install-service.sh" >&2; exit 2; }
command -v setpriv >/dev/null && python3 -c 'import ctypes.util, sys; sys.exit(not ctypes.util.find_library("nfs"))' ||
  { echo "needs setpriv and libnfs (apt install libnfs-utils)" >&2; exit 2; }
mkdir -p "$(dirname "$RESULTS")"
install -m 0755 -d "$WORK"
install -m 0644 "$REPO/fsal/tests/nfsc.py" "$WORK/nfsc.py"
: >"$WORK/checks.txt"

say() { printf '[m5 %4ss] %s\n' "$(($(date +%s) - T0))" "$*"; }
pass() { say "PASS: $*"; echo "| PASS | $* |" >>"$WORK/checks.txt"; NPASS=$((NPASS + 1)); }
fail() { say "FAIL: $*"; echo "| **FAIL** | $* |" >>"$WORK/checks.txt"; NFAIL=$((NFAIL + 1)); }
check() { # check "description" command...
  local d=$1; shift
  if "$@"; then pass "$d"; else fail "$d"; fi
}
# need: a check the rest of the test depends on; on failure, stop here.
need() {
  local d=$1; shift
  if "$@"; then pass "$d"; else fail "$d"; finish; fi
}
finish() {
  # Step 9 starts Ganesha once more, on purpose, with the daemon's exports.
  check "Ganesha never restarted" bash -c "[ \$(journalctl -q -o cat -u nfs-ganesha-omni --since @$T0 | grep -c 'NFS SERVER INITIALIZED') -le $GANESHA_STARTS ]"
  {
    echo "# M5 exit test (NFS half): $(date -u +%FT%TZ)"
    echo
    echo "rev ${E2E_REV:-$(cd "$REPO" && git rev-parse --short HEAD 2>/dev/null || echo unknown)}, kernel $(uname -r), script ${E2E_SELF_SHA:-?}"
    # What actually ran: a module or daemon left from another tree fails checks for
    # reasons this tree doesn't have.
    echo
    echo "daemon $(sha256sum /usr/local/bin/omnifile-edge 2>/dev/null | cut -c1-16), FSAL_OMNI $(sha256sum /opt/ganesha/lib/ganesha/libfsalomni.so 2>/dev/null | cut -c1-16), $(smbd --version 2>/dev/null)"
    echo
    echo "| result | check |"
    echo "|---|---|"
    cat "$WORK/checks.txt"
    echo
    echo "$NPASS passed, $NFAIL failed"
  } >"$RESULTS"
  say "$NPASS passed, $NFAIL failed ($RESULTS)"
  [ "$NFAIL" -eq 0 ]
  exit
}
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
export_of() { if [ "$1" = 3 ]; then echo "$DATA/wait"; else echo /e2e/wait; fi; }
nfs() { $NFSC "$1" "$(export_of "$1")" "${@:2}"; } # nfs VERSION OP PATH...
# read_until VERSION FILE SECONDS: retry the read while the share says "not
# yet"; prints every distinct answer, the last one being the final result.
read_until() {
  local v=$1 f=$2 t=$3 end=$(($(date +%s) + $3)) r last=""
  while :; do
    r=$(nfs "$v" sum "$f")
    [ "$r" != "$last" ] && echo "$r"
    last=$r
    jt "$r" 'd["ok"]' && return 0
    [ "$(date +%s)" -ge "$end" ] && return 1
    sleep 1
  done
}
# "Not yet": NFSv4 DELAY is EAGAIN in libnfs; NFSv3 JUKEBOX comes back as
# EFAULT with the raw status (10008 = 0x2718, little-endian) in the message.
NOT_YET="not d['ok'] and 'step' not in d and (d.get('name') == 'EAGAIN' or chr(0x18) + chr(0x27) in d.get('error', ''))"

cleanup() {
  # Steps 6, 13 and 15 move the virtual clock days ahead: never leave it there, however this
  # ends, or the next lane on this VM runs in the future and its SAS tokens look expired. The
  # sim and enrol issued keys on the virtual clock, so they restart, as at the start of a run.
  echo "+0" >/var/lib/omnifile-dev/clock-offset
  systemctl restart omnifile-dev-sim omnifile-dev-enrol 2>/dev/null
  # Step 11 stops the control API: never leave it down. It reads the clock too.
  systemctl restart omnifile-dev-api 2>/dev/null
  # Step 12 drops the edge's packets: never leave the rule.
  nft delete table inet omnifile_e2e 2>/dev/null
  [ "${E2E_KEEP:-0}" = 1 ] && return
  systemctl stop nfs-ganesha-omni 2>/dev/null
  # Step 9 turned share rendering on: back to the installed config.
  [ -f "$WORK/edge.toml.orig" ] && install -m 0644 "$WORK/edge.toml.orig" /etc/omnifile/edge.toml
  rm -f /run/systemd/system/omnifile-dev-worker.service.d/90-e2e-m5.conf
  systemctl daemon-reload
}
trap cleanup EXIT
# So an interrupt goes through `cleanup`, with the status a signal gives.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------------------------
say "1. reset: services, catalog, sim, edge state"
systemctl stop nfs-ganesha-omni omnifile-edge omnifile-dev-worker 2>/dev/null
# Release the smb-guard's group (`omnifile-guardctl clean`): it outlives the daemon in the fd
# store, and its marks on unfilled rewind directories would hold the rm below.
systemctl clean --what=fdstore omnifile-edge 2>/dev/null
rm -rf /var/lib/omnifile/replica.db* /var/lib/omnifile/identity /var/lib/omnifile/outbox
install -d -m 0700 /var/lib/omnifile/identity /var/lib/omnifile/outbox
rm -rf "${DATA:?}/wait" "${DATA:?}/failfast" "${DATA:?}/.omni"
$E2E reset-db >/dev/null || { fail "catalog reset"; exit 1; }
rm -f /var/lib/omnifile-dev/sim-key.json
# The virtual clock back to real time: steps 6, 13 and 15 move it days ahead, and a
# rerun would otherwise start where the last run left it.
echo "+0" >/var/lib/omnifile-dev/clock-offset
systemctl restart omnifile-dev-sim omnifile-dev-enrol
systemctl restart omnifile-dev-api
wait_for 30 test -s /var/lib/omnifile-dev/sim-key.json
wait_for 30 bash -c '(exec 3<>/dev/tcp/127.0.0.1/8443) 2>/dev/null && (exec 3<>/dev/tcp/127.0.0.1/8444) 2>/dev/null'

say "2. seed the catalog and the S3 backend, enrol the edge, start it and Ganesha"
seed=$($E2E seed --enrol-url https://127.0.0.1:8444 --server-ca /etc/omnifile-dev/pki/ca.crt \
  --bundle-out "$WORK/bundle.txt" --uid "$WUID" --gid "$WGID") || { fail "seed"; finish; }
# The S3 side of the migration: a backend row (the worker reads them at
# start) and its bucket. Seeded files take the dataset's POSIX defaults.
s3=$($E2E s3-setup wait --uid "$WUID" --gid "$WGID") || { fail "s3 setup"; finish; }
say "s3: $s3"
S3_ID=$(j "$s3" 'd["backend_id"]')
DS=$(j "$s3" 'd["dataset_id"]')
/usr/local/bin/omnifile-edge --config /etc/omnifile/edge.toml enrol "$WORK/bundle.txt" >"$WORK/enrol.out" 2>&1 ||
  { fail "enrolment: $(tail -3 "$WORK/enrol.out")"; finish; }
rm -f "$WORK/bundle.txt"
# Virtual hours pass in seconds: the restore is polled every 2 s (dev only). Step 15's
# migration is copied by the worker's sim copier (Storage Mover's semantics over the
# sims; dev only). Its jobs live in the worker's memory: the worker must not restart
# during step 15.
mkdir -p /run/systemd/system/omnifile-dev-worker.service.d
printf '# tests/e2e/m5_exit_test.sh\n[Service]\nEnvironment=OMNIFILE_RECALL_POLL_S=2\nEnvironment=OMNIFILE_STORAGE_MOVER=sim\n' \
  >/run/systemd/system/omnifile-dev-worker.service.d/90-e2e-m5.conf
systemctl daemon-reload
systemctl restart omnifile-dev-worker
systemctl start omnifile-edge
need "omnifile-edge.service active" wait_for 30 systemctl is-active -q omnifile-edge
need "share root materialised from the feed" wait_for 60 test -d "$DATA/wait"
base=/etc/ganesha/omnifile-base.conf
if [ "${E2E_IO_FLUSHER_FAIL_OK:-0}" = 1 ]; then
  sed 's/^\(\s*Enable_RQUOTA = false;\)$/\1\n\tallow_set_io_flusher_fail = true;/' "$base" >"$WORK/ganesha-base.conf"
  base=$WORK/ganesha-base.conf
fi
cat >/etc/ganesha/ganesha.conf <<EOF
# Written by tests/e2e/m5_exit_test.sh (as exit_test.sh, the wait share only).
%include "$base"
NFS_KRB5 { Active_krb5 = false; }
EXPORT_DEFAULTS { Access_Type = RW; Squash = Root_Squash; SecType = sys; Protocols = 3, 4; Transports = TCP; }
EXPORT {
  Export_Id = 11; Path = $DATA/wait; Pseudo = /e2e/wait;
  FSAL { Name = OMNI; Policy = "wait"; FSAL { Name = VFS; } }
}
EOF
systemctl start nfs-ganesha-omni
need "Ganesha up, FSAL_OMNI connected to the daemon" wait_for 30 bash -c \
  "journalctl -q -o cat -u nfs-ganesha-omni --since @$T0 | grep -q 'connected to the OmniFile daemon'"

# ---------------------------------------------------------------------------------
say "3. a bucket with a Standard and an archived object, seeded as a source"
std=$($E2E s3-put std.segy "$STD_SIZE" 51) || { fail "put std.segy"; finish; }
gl=$($E2E s3-put gl.segy "$GL_SIZE" 52 --archive) || { fail "put gl.segy"; finish; }
STD_SHA=$(j "$std" 'd["sha256"]')
GL_SHA=$(j "$gl" 'd["sha256"]')
reg=$("$PY" -m omnifile_service.sources register --dataset "$DS" --backend "$S3_ID" \
  --bucket e2e-bucket --owners "$OWNERS_SID" 2>&1) || { fail "source register: $reg"; finish; }
SRC=$(j "$reg" 'd["source_id"]')
need "the worker seeds the source from a listing" \
  wait_for 90 bash -c "$E2E source $SRC | grep -q '\"state\": \"seeded\"'"
say "source: $($E2E source "$SRC")"
for f in std.segy gl.segy; do
  row=$($E2E file wait "$f")
  say "$f: $row"
  check "$f in the catalog at the share root, its primary copy on S3 (backend $S3_ID)" \
    jt "$row" "d.get('backend_id') == $S3_ID and d['container'] == 'e2e-bucket' and d['object_key'] == '$f'"
  check "$f: one copy, the S3 object (nothing in Azure)" \
    jt "$($E2E locations wait "$f")" "[(l['backend_id'], l['role']) for l in d['locations']] == [($S3_ID, 'primary')]"
done

say "4. placeholders on the edge, listed over NFS"
need "both materialised as placeholders" \
  wait_for 60 test -f "$DATA/wait/std.segy" -a -f "$DATA/wait/gl.segy"
loc=$($E2E local "$DATA/wait/std.segy")
say "std.segy on disk: $loc"
check "std.segy: full size, a stub marker, no data on disk" \
  jt "$loc" "d['size'] == $STD_SIZE and d['marker'] is not None and d['allocated'] == 0"
for v in 3 4; do
  ls_out=$(nfs "$v" ls /)
  check "NFSv$v lists both ($ls_out)" jt "$ls_out" "d['ok'] and {'std.segy', 'gl.segy'} <= set(d['names'])"
done

# ---------------------------------------------------------------------------------
say "5. read the Standard object over NFSv3: filled from S3"
# An online object: the read is held while the fill runs, or answered
# "not yet" and retried. Never ENOENT.
answers=$(read_until 3 /std.segy 120)
final=$(tail -1 <<<"$answers")
say "NFSv3 answers: $(tr '\n' ' ' <<<"$answers")"
check "NFSv3 read: the S3 object's bytes, byte-identical, no zero blocks" \
  jt "$final" "d['ok'] and d['bytes'] == $STD_SIZE and d['sha256'] == '$STD_SHA' and d['zero_blocks'] == 0"
# Held while it fills: the first read already returns the bytes (a fill
# refused for want of a CRC would fail it first).
check "the first read returned them (no error answer before)" test "$(wc -l <<<"$answers")" = 1
r4=$(nfs 4 sum /std.segy)
check "NFSv4 read: the same bytes" jt "$r4" "d['ok'] and d['sha256'] == '$STD_SHA'"
check "an online object needs no restore: no recall" jt "$($E2E recall wait std.segy)" "d == {}"
loc=$($E2E local "$DATA/wait/std.segy" --hash)
check "std.segy is a whole local copy: the object's CRC-64/NVME" \
  jt "$loc" "d['crc64nvme'] == '$(j "$std" 'd["crc64nvme"]')' and d['sha256'] == '$STD_SHA'"
row=$($E2E file wait std.segy)
check "the catalog saw no upload: the primary copy is still the S3 object" \
  jt "$row" "d['backend_id'] == $S3_ID and d['backend_version_id'] == '$(j "$std" 'd["version_id"]')'"
locs=$($E2E locations wait std.segy)
check "after the read, still one copy, on S3, with the CRC S3 stores for it" \
  jt "$locs" "[(l['backend_id'], l['crc64nvme']) for l in d['locations']] == [($S3_ID, '$(j "$std" 'd["crc64nvme"]')')]"

# ---------------------------------------------------------------------------------
say "6. the archived object: an S3 restore, then the same read"
first=$(nfs 3 sum /gl.segy)
say "first read: $first"
check "gl.segy (archived): NFSv3 says 'not yet' (JUKEBOX), not ENOENT" jt "$first" "$NOT_YET"
check "an S3 restore started (recall rehydrating)" \
  wait_for 60 bash -c "$E2E recall wait gl.segy | grep -q '\"state\": \"rehydrating\"'"
r4=$(nfs 4 sum /gl.segy)
check "NFSv4 says 'not yet' (DELAY) while it restores" jt "$r4" "$NOT_YET"
# The sim restores in its documented time: move every clock past it.
"$PY" - <<'EOF'
import httpx
r = httpx.post("http://127.0.0.1:10000/_sim/advance", json={"seconds": 48 * 3600}, trust_env=False)
r.raise_for_status()
EOF
answers=$(read_until 3 /gl.segy 180)
final=$(tail -1 <<<"$answers")
say "NFSv3 answers: $(tr '\n' ' ' <<<"$answers")"
check "after the restore, NFSv3 reads gl.segy's bytes" \
  jt "$final" "d['ok'] and d['bytes'] == $GL_SIZE and d['sha256'] == '$GL_SHA' and d['zero_blocks'] == 0"
r4=$(nfs 4 sum /gl.segy)
check "and NFSv4" jt "$r4" "d['ok'] and d['sha256'] == '$GL_SHA'"

# ---------------------------------------------------------------------------------
say "7. SMB Previous Versions: smbd lists a folder of an unfilled label, which fills first"
# The share's rewind trees (fsal-namespace.md section 7), served by a standalone smbd with
# the stanza ShareConfig::render_smb emits (vfs_shadow_copy2 over the daemon's rewind area).
# smbd opens a previous version's folder only to list it; the daemon's smb-guard holds that
# open while the folder fills from the service's as-of listing, so it never lists empty.
OMNIACL=""
for b in "$REPO/edge/target/release/examples/omniacl" "$REPO/edge/target/debug/examples/omniacl"; do
  [ -x "$b" ] && { OMNIACL=$b; break; }
done
# Or the one dev/scripts/install-edge.sh installed with the daemon.
[ -z "$OMNIACL" ] && OMNIACL=$(command -v omniacl || true)
SNAP=$DATA/.omni/snap/wait
LABEL=$(date -u +%Y-%m-%dT0000Z)
GMT=$(date -u +@GMT-%Y.%m.%d-00.00.00)
SMBUSER=omnim5e2e
if ! command -v smbd >/dev/null || ! command -v smbclient >/dev/null || ! command -v smbpasswd >/dev/null || [ -z "$OMNIACL" ]; then
  fail "step 7 needs smbd, smbclient, smbpasswd and omniacl (cargo build -p omnifile-acl --example omniacl)"
  finish
fi
rw=$($E2E rewind wait 30) && jt "$rw" "d['updated'] == 1" || { fail "rewind depth: $rw"; finish; }
# A file in a subfolder, for step 8: written over NFS, committed, then backdated with the rest.
D_TXT="a file in a subfolder, from before the labels"
nfs 4 mkdir /deep >/dev/null; nfs 4 creat /deep/d.txt >/dev/null; nfs 4 write /deep/d.txt "$D_TXT" >/dev/null
d_committed() { jt "$($E2E path wait deep/d.txt)" "(d.get('content_version_id') or 0) > 0"; }
need "deep/d.txt committed" wait_for 90 d_committed
# The seeded versions predate today's 00:00 label.
say "backdated: $($E2E backdate wait 72)"
label_unfilled() { jt "$($E2E marker "$SNAP/$LABEL")" "d.get('flags', 0) & 8"; }
need "the edge made today's label ($LABEL), unfilled" wait_for 120 label_unfilled
mkdir -p "$WORK/smb"/{run,lock,state,cache,private}
OMNIACL_SMB_PREVVER=off "$OMNIACL" render wait "$DATA/wait" 11 nfs4,smb krb5 - "$OWNERS_SID" - - wait "$SNAP" |
  awk '/^#--- smb/{f=1;next} /^#--- /{f=0} f' >"$WORK/smb/share.conf"
check "the rendered stanza serves the rewind area (shadow:snapdir)" grep -q "shadow:snapdir = $SNAP" "$WORK/smb/share.conf"
# The share's AD groups become one local user with the files' uid.
sed -i -E "s/^(\s*(valid users|write list|read list) = ).*/\1$SMBUSER/" "$WORK/smb/share.conf"
# With no winbind here, the rewind trees' ACLs (the share's AD groups) map to owner-only
# (root): the user acts as root on this share. Access over SMB is the M2 permission matrix's.
echo "    admin users = $SMBUSER" >>"$WORK/smb/share.conf"
id "$SMBUSER" >/dev/null 2>&1 || useradd -M -o -u "$WUID" -s /usr/sbin/nologin "$SMBUSER"
cat >"$WORK/smb/smb.conf" <<EOF
[global]
    security = user
    server role = standalone server
    map to guest = never
    smb ports = 4455
    disable netbios = yes
    server min protocol = SMB2_10
    pid directory = $WORK/smb/run
    lock directory = $WORK/smb/lock
    state directory = $WORK/smb/state
    cache directory = $WORK/smb/cache
    private dir = $WORK/smb/private
    ncalrpc dir = $WORK/smb/run/ncalrpc
    passdb backend = tdbsam:$WORK/smb/private/passdb.tdb
    log file = $WORK/smb/smbd.log
EOF
cat "$WORK/smb/share.conf" >>"$WORK/smb/smb.conf"
(echo pw; echo pw) | smbpasswd -c "$WORK/smb/smb.conf" -s -a "$SMBUSER" >/dev/null
smbd -D -s "$WORK/smb/smb.conf"
SMBC() { timeout 120 smbclient -s "$WORK/smb/smb.conf" -U "$SMBUSER%pw" //127.0.0.1/wait -p 4455 -m SMB3 -t 100 -c "$1" 2>&1; }
need "smbd answers" wait_for 30 bash -c "smbclient -s $WORK/smb/smb.conf -U $SMBUSER%pw //127.0.0.1/wait -p 4455 -m SMB3 -c ls >/dev/null 2>&1"
listing=$(SMBC "ls $GMT/*")
say "SMB listing of $GMT: $(grep -E 'segy|NT_STATUS' <<<"$listing" | tr -s ' ' | tr '\n' ';')"
check "the previous version's folder lists both files" \
  bash -c "grep -q ' std.segy ' <<<'$listing' && grep -q ' gl.segy ' <<<'$listing'"
# Placeholders are offline to SMB clients (FILE_ATTRIBUTE_OFFLINE, which keeps Explorer
# and the indexer from opening them); a filled file is not (omnifile_marker::dosattr).
# smbclient's ls prints the offline attribute as "o".
attrs_of() { awk -v f="$2" '$1 == f { print $2 }' <<<"$1"; }
gl_attrs=$(attrs_of "$listing" gl.segy)
check "a placeholder in the label is offline to SMB clients (attributes $gl_attrs)" \
  bash -c "[[ '$gl_attrs' == *o* ]]"
live=$(SMBC "ls std.segy")
std_attrs=$(attrs_of "$live" std.segy)
check "and a filled file is not (std.segy: $std_attrs)" bash -c "[[ -n '$std_attrs' && '$std_attrs' != *o* ]]"
check "the label was filled for that open (no longer unfilled)" \
  jt "$($E2E marker "$SNAP/$LABEL")" "d['flags'] & 8 == 0"
check "by the smb-guard, for smbd's open" bash -c \
  "journalctl -q -o cat -u omnifile-edge --since @$T0 | grep -q 'unfilled rewind directory opened; filling it'"
rm -f "$WORK/smb/std.prev"
got=$(SMBC "get $GMT/std.segy $WORK/smb/std.prev")
check "a file read from the previous version has its bytes" \
  bash -c "[ \"\$(sha256sum <$WORK/smb/std.prev | cut -d' ' -f1)\" = '$STD_SHA' ]"

# ---------------------------------------------------------------------------------
say "8. Previous Versions -> Open: a file in a subfolder of an unfilled label (vfs_omnifile_prevver)"
# A file opened directly by its @GMT path opens no directory, so step 7's mechanism never
# fires: shadow_copy2 alone answers NOT_FOUND. vfs_omnifile_prevver (samba-vfs/, GPL-3,
# decision 36) opens the path's folders under the label first, and the guard fills them.
YLABEL=$(date -u -d yesterday +%Y-%m-%dT0000Z)
YGMT=$(date -u -d yesterday +@GMT-%Y.%m.%d-00.00.00)
SO=${OMNIFILE_PREVVER_SO:-$REPO/samba-vfs/build/omnifile_prevver.so}
# On the dev VM, dev/scripts/install-e2e-tools.sh builds it for this host's smbd.
STAGED_SO=/usr/local/lib/omnifile/samba-vfs/omnifile_prevver.so
[ -z "${OMNIFILE_PREVVER_SO:-}" ] && [ ! -f "$SO" ] && [ -f "$STAGED_SO" ] && SO=$STAGED_SO
smbd_stop() {
  local p; p=$(cat "$WORK/smb/run/smbd.pid" 2>/dev/null) || return 0
  kill "$p" 2>/dev/null
  for _ in $(seq 1 75); do kill -0 "$p" 2>/dev/null || return 0; sleep 0.2; done
}
y_unfilled() { jt "$($E2E marker "$SNAP/$YLABEL")" "d.get('flags', 0) & 8"; }
check "yesterday's label ($YLABEL) is unfilled" y_unfilled
rm -f "$WORK/smb/d.prev"
got=$(SMBC "get $YGMT/deep/d.txt $WORK/smb/d.prev")
say "without the module: $(grep -m1 -oE 'NT_STATUS_[A-Z_]+' <<<"$got")"
check "without the module, opening it directly is NOT_FOUND (the gap)" bash -c "grep -qE 'NT_STATUS_OBJECT_(PATH|NAME)_NOT_FOUND' <<<'$got'"
check "and the label stays unfilled" y_unfilled
smbd_stop
if [ ! -f "$SO" ]; then
  fail "vfs_omnifile_prevver not built: $SO (samba-vfs/build.sh, or dev/scripts/install-e2e-tools.sh)"
else
  "$REPO/samba-vfs/install.sh" "$SO" >/dev/null
  "$OMNIACL" render wait "$DATA/wait" 11 nfs4,smb krb5 - "$OWNERS_SID" - - wait "$SNAP" |
    awk '/^#--- smb/{f=1;next} /^#--- /{f=0} f' >"$WORK/smb/share.conf"
  check "omniacl names the installed module before shadow_copy2" \
    grep -q "vfs objects = full_audit omnifile_prevver shadow_copy2" "$WORK/smb/share.conf"
  sed -i -E "s/^(\s*(valid users|write list|read list) = ).*/\1$SMBUSER/" "$WORK/smb/share.conf"
  echo "    admin users = $SMBUSER" >>"$WORK/smb/share.conf"
  sed -i '/^\[wait\]/,$d' "$WORK/smb/smb.conf"
  sed -i '/^# share wait/d' "$WORK/smb/smb.conf"
  cat "$WORK/smb/share.conf" >>"$WORK/smb/smb.conf"
  smbd -D -s "$WORK/smb/smb.conf"
  need "smbd answers (Ubuntu's smbd, with the module)" wait_for 30 bash -c "smbclient -s $WORK/smb/smb.conf -U $SMBUSER%pw //127.0.0.1/wait -p 4455 -m SMB3 -c ls >/dev/null 2>&1"
  n0=$(journalctl -q -o cat -u omnifile-edge --since "@$T0" | grep -c 'unfilled rewind directory opened; filling it')
  rm -f "$WORK/smb/d.prev"
  got=$(SMBC "get $YGMT/deep/d.txt $WORK/smb/d.prev")
  say "with the module: $(tail -n 1 <<<"$got")"
  check "opened directly, the file has its bytes from that version" bash -c "[ \"\$(cat $WORK/smb/d.prev 2>/dev/null)\" = '$D_TXT' ]"
  check "the label was filled for it" jt "$($E2E marker "$SNAP/$YLABEL")" "d['flags'] & 8 == 0"
  check "and its subfolder deep" jt "$($E2E marker "$SNAP/$YLABEL/deep")" "d['flags'] & 8 == 0"
  n1=$(journalctl -q -o cat -u omnifile-edge --since "@$T0" | grep -c 'unfilled rewind directory opened; filling it')
  check "by the smb-guard, for smbd's opens ($((n1 - n0)) fills)" test "$((n1 - n0))" -ge 2
  smbd_stop
  "$REPO/samba-vfs/install.sh" --uninstall >/dev/null
fi
userdel "$SMBUSER" 2>/dev/null

# ---------------------------------------------------------------------------------
say "9. shares added and removed in the catalog: the daemon renders them, Ganesha serves them (M6)"
# docs/design/share-provisioning.md. The M1 stack runs no portal: the rows are the
# ones POST /v1/shares and DELETE /v1/shares make (e2e.py add-share, remove-share).
# The daemon renders every share's EXPORT block into the file ganesha.conf now
# includes, and reloads Ganesha through its real unit.
RENDERED=/var/lib/omnifile/shares
install -d -m 0755 "$RENDERED"
: >"$RENDERED/ganesha.conf"
: >"$RENDERED/smb.conf"
cp /etc/omnifile/edge.toml "$WORK/edge.toml.orig"
{
  echo
  echo "[shares]"
  echo "# tests/e2e/m5_exit_test.sh step 9. No winbind on this stack: no root ACLs."
  echo "render = true"
  echo 'smb_prevver = "off"'
  echo "root_acls = false"
  echo 'reload = ["systemctl", "try-reload-or-restart", "nfs-ganesha-omni.service"]'
} >>/etc/omnifile/edge.toml
{
  echo "# Written by tests/e2e/m5_exit_test.sh step 9: the exports are the daemon's."
  echo "%include \"$base\""
  echo "NFS_KRB5 { Active_krb5 = false; }"
  echo "%include \"$RENDERED/ganesha.conf\""
} >/etc/ganesha/ganesha.conf
T9=$(date +%s)
GANESHA_STARTS=2
systemctl restart omnifile-edge
need "omnifile-edge.service active with rendering on" wait_for 30 systemctl is-active -q omnifile-edge
systemctl start nfs-ganesha-omni
check "the seeded share is rendered by the daemon" wait_for 30 grep -q 'Pseudo = "/wait";' "$RENDERED/ganesha.conf"
check "Ganesha up with the daemon's exports" wait_for 30 bash -c \
  "journalctl -q -o cat -u nfs-ganesha-omni --since @$T9 | grep -q 'connected to the OmniFile daemon'"
served() { $NFSC "$1" "$2" ls / 2>/dev/null | grep -q '"ok": true'; } # served VERSION EXPORT
check "the rendered export serves the seeded share (v4)" wait_for 30 served 4 /wait

new=$($E2E add-share fresh --uid "$WUID" --gid "$WGID") || fail "add-share"
say "added: $new"
check "a new share is rendered with its export id" wait_for 60 grep -q \
  "Export_Id = $(j "$new" 'd["nfs_export_id"]');" "$RENDERED/ganesha.conf"
check "the daemon reloaded Ganesha for it" wait_for 30 bash -c \
  "journalctl -q -o cat -u omnifile-edge --since @$T9 | grep -q 'share config changed: reloading'"
check "Ganesha serves the new share over NFSv4" wait_for 30 served 4 /fresh
check "and over NFSv3" wait_for 30 served 3 "$DATA/fresh"
check "the seeded share kept serving through the reload" served 4 /wait

removed=$($E2E remove-share fresh) || fail "remove-share"
say "removed: $removed"
check "a removed share is no longer rendered" wait_for 60 bash -c \
  "! grep -q 'Pseudo = \"/fresh\";' $RENDERED/ganesha.conf"
check "Ganesha no longer serves it" wait_for 30 bash -c "! $NFSC 4 /fresh ls / 2>/dev/null | grep -q '\"ok\": true'"
check "its folder stays on the edge" test -d "$DATA/fresh"
check "the seeded share still serves" served 4 /wait
check "Ganesha stayed up through the reloads" systemctl is-active -q nfs-ganesha-omni

# ---------------------------------------------------------------------------------
say "10. users' renames, moves, deletes and chowns reach the catalog (namespace-sync.md)"
# Over NFS (the daemon's rendered export) and from a local shell: the daemon's namespace
# watcher marks the folders, reconciles them against the replica and sends rename, unlink
# and setattr ops. The catalog keeps each file's id through its renames.
ns() { $NFSC 4 /wait "$@"; }
cat_path() { $E2E path wait "$1"; }
same_id() { jt "$(cat_path "$1")" "d.get('file_id') == $2"; } # same_id PATH FILE_ID
gone() { jt "$(cat_path "$1")" "d == {}"; }
ns mkdir /ns >/dev/null; ns creat /ns/a.txt >/dev/null; ns write /ns/a.txt "namespace sync" >/dev/null
a_committed() { jt "$(cat_path ns/a.txt)" "(d.get('content_version_id') or 0) > 0"; }
need "ns/a.txt committed" wait_for 90 a_committed
A=$(j "$(cat_path ns/a.txt)" 'd["file_id"]')
r=$(ns rename /ns/a.txt /ns/b.txt)
check "NFS rename ($r): the catalog has ns/b.txt, the same file ($A)" wait_for 30 same_id ns/b.txt "$A"
check "and no ns/a.txt" gone ns/a.txt
ns mkdir /ns2 >/dev/null
r=$(ns rename /ns/b.txt /ns2/b.txt)
check "NFS move to another folder ($r): ns2/b.txt, the same file" wait_for 30 same_id ns2/b.txt "$A"
r=$(ns rename /ns2 /ns3)
check "a folder renamed ($r): ns3/b.txt, the same file" wait_for 30 same_id ns3/b.txt "$A"
check "and no ns2" gone ns2
chown 20077:10077 "$DATA/wait/ns3/b.txt"
check "a local chown: the catalog has its new owner" wait_for 30 bash -c \
  "$E2E path wait ns3/b.txt | grep -q '\"uid\": 20077, \"gid\": 10077'"
r=$(ns unlink /ns3/b.txt)
check "NFS unlink ($r): ns3/b.txt is deleted in the catalog" wait_for 30 gone ns3/b.txt
r=$(ns rmdir /ns3)
check "NFS rmdir ($r): ns3 is deleted too" wait_for 30 gone ns3
ns creat /ns/c.txt >/dev/null; ns write /ns/c.txt "in a folder removed whole" >/dev/null
c_committed() { jt "$(cat_path ns/c.txt)" "(d.get('content_version_id') or 0) > 0"; }
need "ns/c.txt committed" wait_for 90 c_committed
rm -rf "$DATA/wait/ns"
check "a folder removed with its file from a shell: both deleted in the catalog" \
  wait_for 30 bash -c "$E2E path wait ns | grep -qx '{}'"
sleep 5
check "nothing deleted comes back on the edge (the tombstones' echoes are skipped)" \
  bash -c "[ ! -e $DATA/wait/ns ] && [ ! -e $DATA/wait/ns3 ] && [ ! -e $DATA/wait/ns2 ]"
ob=$($E2E outbox)
say "outbox: $ob"
check "every namespace op acknowledged: none queued, sent or refused" \
  jt "$ob" "not [k for k in d if k.split(':')[1] in ('rename', 'unlink', 'owner') and not k.startswith('acked:')]"

# ---------------------------------------------------------------------------------
say "11. a service outage: reads fail closed, and the recall resumes once it is back (M7 chaos)"
# The control API is down (the daemon can get no read credential): a read of a file whose
# bytes are only in S3 must answer "not yet" or EIO, never ENOENT and never zeros, and the
# placeholder must stay empty. Once the API is back, the same read returns the bytes.
CH_SIZE=$((2 << 20 | 777))
ch=$($E2E s3-put c.segy "$CH_SIZE" 53 --bucket e2e-chaos) || { fail "put chaos/c.segy"; finish; }
CH_SHA=$(j "$ch" 'd["sha256"]')
NL_SIZE=$((3 << 20 | 4321))
nl=$($E2E s3-put n.segy "$NL_SIZE" 61 --bucket e2e-chaos) || { fail "put chaos/n.segy"; finish; }
NL_SHA=$(j "$nl" 'd["sha256"]')
# Step 13's: 33 ranges of 4 MiB, so a fill records its progress (every 64 MiB) once
# or twice before it ends. Archived: online objects are read by chunk (decision 44),
# a restored one is filled whole.
K_SIZE=$((132 << 20 | 999))
k=$($E2E s3-put k.segy "$K_SIZE" 62 --bucket e2e-chaos --archive) || { fail "put chaos/k.segy"; finish; }
K_SHA=$(j "$k" 'd["sha256"]')
# Step 14's: a large online object, read by range (40 chunks of 4 MiB, the last short).
R_SIZE=$((160 << 20 | 4321))
R_SEED=63
$E2E s3-put r.segy "$R_SIZE" "$R_SEED" --bucket e2e-chaos >/dev/null || { fail "put chaos/r.segy"; finish; }
reg=$("$PY" -m omnifile_service.sources register --dataset "$DS" --backend "$S3_ID" \
  --bucket e2e-chaos --path chaos --owners "$OWNERS_SID" 2>&1) || { fail "source register: $reg"; finish; }
SRC2=$(j "$reg" 'd["source_id"]')
need "the worker seeds the second source" \
  wait_for 90 bash -c "$E2E source $SRC2 | grep -q '\"state\": \"seeded\"'"
need "chaos/c.segy is a placeholder on the edge" wait_for 60 test -f "$DATA/wait/chaos/c.segy"
systemctl stop omnifile-dev-api
need "the control API is down" bash -c "! (exec 3<>/dev/tcp/127.0.0.1/8443) 2>/dev/null"
T11=$(date +%s)
bad=""
answers=""
while [ $(($(date +%s) - T11)) -lt 20 ]; do
  for v in 3 4; do
    if [ "$v" = 3 ]; then r=$($NFSC 3 "$DATA/wait" sum /chaos/c.segy); else r=$($NFSC 4 /wait sum /chaos/c.segy); fi
    answers="$answers v$v:$(j "$r" 'd.get("name") or ("ok" if d["ok"] else "err")')"
    jt "$r" "$NOT_YET" || bad="$bad $r"
  done
  sleep 2
done
say "answers while the API was down: $(tr ' ' '\n' <<<"${answers# }" | sort | uniq -c | tr -s ' \n' ' ')"
check "while the API is down, every read answers \"not yet\" (never bytes, ENOENT or another error)" test -z "$bad"
loc=$($E2E local "$DATA/wait/chaos/c.segy")
check "and the placeholder holds no data (nothing was written)" \
  jt "$loc" "d['size'] == $CH_SIZE and d['marker'] is not None and d['allocated'] == 0"
systemctl start omnifile-dev-api
need "the control API is back" wait_for 30 bash -c "(exec 3<>/dev/tcp/127.0.0.1/8443) 2>/dev/null"
answers=$(read_until 3 /chaos/c.segy 180)
final=$(tail -1 <<<"$answers")
check "once it is back, the same read returns the object's bytes, no zero blocks" \
  jt "$final" "d['ok'] and d['bytes'] == $CH_SIZE and d['sha256'] == '$CH_SHA' and d['zero_blocks'] == 0"
r4=$($NFSC 4 /wait sum /chaos/c.segy)
check "and over NFSv4" jt "$r4" "d['ok'] and d['sha256'] == '$CH_SHA'"
check "the edge daemon stayed up through the outage" systemctl is-active -q omnifile-edge

# ---------------------------------------------------------------------------------
say "12. network loss: the edge's packets dropped, not refused (M7 chaos)"
# Step 11's outage refuses connections, so every request fails at once. Here every packet
# from the edge daemon (its cgroup, so the service and the sim are untouched) to the sim
# (Blob and S3) and to the control API vanishes, as when a site's link drops: connections
# already open stall, and new ones hang in SYN_SENT. Nothing may wait on them for ever.
need "nft is installed" bash -c "command -v nft >/dev/null"
need "chaos/n.segy is a placeholder on the edge" wait_for 60 test -f "$DATA/wait/chaos/n.segy"
edge_pid() { systemctl show -p MainPID --value omnifile-edge; }
EPID=$(edge_pid)
cg=$(sed -n 's/^0:://p' "/proc/$EPID/cgroup"); cg=${cg#/}
# The edge's flows to the API and the sim are marked on the way out and dropped on the way
# in (loopback). Marking without dropping in OUTPUT lets conntrack confirm each flow, so
# the mark stays on it; a packet dropped in OUTPUT would never have its flow confirmed.
nft add table inet omnifile_e2e &&
  nft add chain inet omnifile_e2e out '{ type filter hook output priority 0; }' &&
  nft add chain inet omnifile_e2e in '{ type filter hook input priority 0; }' &&
  nft add rule inet omnifile_e2e out socket cgroupv2 level "$(awk -F/ '{print NF}' <<<"$cg")" \
    "$cg" tcp dport '{ 8443, 10000 }' ct mark set 0x12 &&
  nft add rule inet omnifile_e2e in ct mark 0x12 drop
need "the edge's packets to the API and the sim are dropped" \
  bash -c "nft list table inet omnifile_e2e | grep -q 'dport { 8443, 10000 } ct mark set 0x00000012' &&
    nft list chain inet omnifile_e2e in | grep -q 'ct mark 0x00000012 drop'"
need "the API still answers others (only the edge is cut off)" \
  bash -c "(exec 3<>/dev/tcp/127.0.0.1/8443) 2>/dev/null"
# On a `wait` share the FSAL parks a read for up to Park_Ms (the export renders none,
# so Ganesha's default of 5,000 ms) before answering "not yet"; a call that waited on a
# hung connection instead would take the edge's 10 s connect timeout or longer.
CALL_MAX_MS=$((5000 + 2000))
# Since step 9 the share's NFSv4 export is the daemon's rendering, at /wait.
nfs12() { if [ "$1" = 3 ]; then $NFSC 3 "$DATA/wait" "${@:2}"; else $NFSC 4 /wait "${@:2}"; fi; }
T12=$(date +%s)
W_TXT="written on the edge while its link was down"
nfs12 3 creat /nl-w.txt >/dev/null
wr=$(nfs12 3 write /nl-w.txt "$W_TXT")
check "a write over NFS succeeds while the link is down" jt "$wr" "d['ok']"
bad=""
slow=""
times=""
answers=""
while [ $(($(date +%s) - T12)) -lt 45 ]; do
  for v in 3 4; do
    t=$(date +%s%N)
    r=$(nfs12 "$v" sum /chaos/n.segy)
    ms=$((($(date +%s%N) - t) / 1000000))
    times="$times $ms"
    [ "$ms" -gt "$CALL_MAX_MS" ] && slow="$slow v$v:${ms}ms"
    answers="$answers v$v:$(j "$r" 'd.get("name") or ("ok" if d["ok"] else "err")')"
    jt "$r" "$NOT_YET" || bad="$bad $r"
  done
  sleep 2
done
say "answers while the link was down: $(tr ' ' '\n' <<<"${answers# }" | sort | uniq -c | tr -s ' \n' ' ')"
check "while the link is down, every read answers \"not yet\" (never bytes, ENOENT or another error)" test -z "$bad"
say "each call, ms (libnfs mount and process start included):$times"
check "and every NFS call answers within Park_Ms + 2 s (nothing waits on a hung connection)" test -z "$slow"
loc=$($E2E local "$DATA/wait/chaos/n.segy")
check "and the placeholder holds no data" \
  jt "$loc" "d['size'] == $NL_SIZE and d['marker'] is not None and d['allocated'] == 0"
check "and nl-w.txt is not committed yet (nothing reached the service)" \
  jt "$($E2E path wait nl-w.txt)" "not (d.get('content_version_id') or 0)"
# The link comes back, but as through a stateful firewall or NAT that forgot the flows
# open during the outage: their packets keep vanishing (no RST), and only new
# connections pass. A request left on such a flow waits until TCP gives up (about 15
# minutes) unless the edge's own timeout ends it first.
# Kernels without CONFIG_NF_CONNTRACK_PROCFS (Ubuntu's) have no /proc/net/nf_conntrack:
# the conntrack tool reads the table over netlink (dev/scripts/install-e2e-tools.sh).
if [ -r /proc/net/nf_conntrack ]; then
  marked=$(grep -c 'mark=18 ' /proc/net/nf_conntrack)
elif command -v conntrack >/dev/null; then
  marked=$(conntrack -L -m 0x12 2>/dev/null | wc -l)
else
  marked=""
  fail "counting the outage's flows needs /proc/net/nf_conntrack or the conntrack tool (apt install conntrack)"
fi
say "flows marked during the outage: ${marked:-?}"
check "the outage held some of the edge's flows (the firewall has something to forget)" \
  test "${marked:-0}" -gt 0
nft flush chain inet omnifile_e2e out
need "only the flows from the outage are still dropped" \
  bash -c "nft list chain inet omnifile_e2e in | grep -q 'ct mark 0x00000012 drop' &&
    ! nft list chain inet omnifile_e2e out | grep -q 'mark set'"
T12B=$(date +%s)
answers=$(read_until 3 /chaos/n.segy 240)  # NFSv3: the same path as before step 9
final=$(tail -1 <<<"$answers")
check "once the link is back, the same read returns the object's bytes, no zero blocks ($(($(date +%s) - T12B)) s)" \
  jt "$final" "d['ok'] and d['bytes'] == $NL_SIZE and d['sha256'] == '$NL_SHA' and d['zero_blocks'] == 0"
w_committed() { jt "$($E2E path wait nl-w.txt)" "(d.get('content_version_id') or 0) > 0 and d['size'] == ${#W_TXT}"; }
check "the file written meanwhile is uploaded and committed ($(wait_for 240 w_committed; echo $(($(date +%s) - T12B))) s after)" \
  w_committed
# The feed is one long poll at a time: one left hanging on a dropped flow would cut the
# edge off from every later catalog change. A share added now must still reach it.
late=$($E2E add-share after-loss --uid "$WUID" --gid "$WGID") || fail "add-share after-loss"
check "a catalog change made after the outage still reaches the edge (the feed resumed)" \
  wait_for 120 grep -q "Export_Id = $(j "$late" 'd["nfs_export_id"]');" "$RENDERED/ganesha.conf"
$E2E remove-share after-loss >/dev/null || fail "remove-share after-loss"
check "the edge daemon was never restarted (same process throughout)" test "$(edge_pid)" = "$EPID"
nft delete table inet omnifile_e2e

# ---------------------------------------------------------------------------------
say "13. the daemon killed in the middle of a whole-file fill from S3, readers waiting (M7 chaos)"
# An S3 range isn't verified on arrival, so a whole-file fill from S3 exposes nothing
# before the whole-file CRC matches; it records how far it wrote every 64 MiB, after an
# fsync, and a later attempt resumes there (stub/src/fill.rs). Each 4 MiB range is slowed
# to 400 ms so the kill lands mid-fill. Online objects are read by chunk (step 14,
# decision 44); a recall of an archived object is filled whole, so k.segy is archived:
# the readers' first reads start its restore, which the sim's clock then completes.
need "chaos/k.segy is a placeholder on the edge" wait_for 60 test -f "$DATA/wait/chaos/k.segy"
MIB64=$((64 << 20))
served() { j "$($E2E s3-served)" 'd.get("e2e-chaos/k.segy", 0)'; }
S13_0=$(served)
$E2E fault '{"backend": "s3", "op": "get_range", "latency_ms": 400}' >/dev/null
# read13 VERSION FILE SECONDS: read_until through the share's current exports.
read13() {
  local v=$1 f=$2 end=$(($(date +%s) + $3)) r last=""
  while :; do
    r=$(nfs12 "$v" sum "$f")
    [ "$r" != "$last" ] && echo "$r"
    last=$r
    jt "$r" 'd["ok"]' && return 0
    [ "$(date +%s)" -ge "$end" ] && return 1
    sleep 1
  done
}
read13 3 /chaos/k.segy 300 >"$WORK/k-r3.txt" &
r3=$!
read13 4 /chaos/k.segy 300 >"$WORK/k-r4.txt" &
r4=$!
need "k.segy (archived): the readers started an S3 restore (recall rehydrating)" \
  wait_for 60 bash -c "$E2E recall wait chaos/k.segy | grep -q '\"state\": \"rehydrating\"'"
"$PY" - <<'EOF'
import httpx
r = httpx.post("http://127.0.0.1:10000/_sim/advance", json={"seconds": 48 * 3600}, trust_env=False)
r.raise_for_status()
EOF
recorded() { jt "$($E2E local "$DATA/wait/chaos/k.segy")" "(d['fetched_to'] or 0) >= $MIB64"; }
need "the restore completed and the fill from S3 recorded its first 64 MiB" wait_for 180 recorded
loc=$($E2E local "$DATA/wait/chaos/k.segy")
F13=$(j "$loc" 'd["fetched_to"]')
check "unverified bytes are never readable: $((F13 >> 20)) MiB on disk, the marker still says 0 resident" \
  jt "$loc" "d['allocated'] >= $F13 and d['marker']['state'] == 'PARTIAL' and d['marker']['resident'] == 0"
gpid() { systemctl show -p MainPID --value nfs-ganesha-omni; }
G13=$(gpid)
E13=$(edge_pid)
NR13=$(systemctl show -p NRestarts --value omnifile-edge)
systemctl kill -s KILL omnifile-edge
say "killed omnifile-edge (pid $E13) with SIGKILL, $((F13 >> 20)) MiB recorded"
check "systemd restarted the daemon" wait_for 30 bash -c \
  "[ \$(systemctl show -p NRestarts --value omnifile-edge) -gt $NR13 ] && systemctl is-active -q omnifile-edge"
check "Ganesha stayed up through the daemon kill (same pid $G13)" test "$(gpid)" = "$G13"
for r in $r3 $r4; do wait "$r"; done
$E2E fault '{"backend": "s3", "clear": true}' >/dev/null
for v in 3 4; do
  answers=$(cat "$WORK/k-r$v.txt")
  final=$(tail -1 <<<"$answers")
  say "NFSv$v answers: $(j "$(head -1 <<<"$answers")" 'd.get("name") or d.get("ok")') ... ($(wc -l <<<"$answers") distinct)"
  check "NFSv$v reader across the kill: the object's bytes, byte-identical, no zero block" \
    jt "$final" "d['ok'] and d['bytes'] == $K_SIZE and d['sha256'] == '$K_SHA' and d['zero_blocks'] == 0"
  bad=$(head -n -1 <<<"$answers" | while read -r r; do jt "$r" "$NOT_YET" || echo "$r"; done)
  check "and every answer before it was \"not yet\" (never bytes early, ENOENT or another error)" test -z "$bad"
done
# A fill started over from 0 would send at least the object plus what the first attempt
# fetched (at least F13); one resumed at F13 sends less than the object plus F13.
S13=$(($(served) - S13_0))
say "S3 bytes sent for k.segy: $S13 (object $K_SIZE, resumed at $F13)"
check "the fill resumed where it recorded its progress, not from 0 ($((S13 >> 20)) MiB fetched for a $((K_SIZE >> 20)) MiB object)" \
  test "$S13" -lt $((K_SIZE + F13))
loc=$($E2E local "$DATA/wait/chaos/k.segy" --hash)
check "k.segy is a whole local copy now, its progress record gone" \
  jt "$loc" "d['marker']['state'] == 'RESIDENT' and d['fetched_to'] is None and d['crc64nvme'] == '$(j "$k" 'd["crc64nvme"]')'"

# ---------------------------------------------------------------------------------
say "14. range reads of a large S3-resident object: only the chunks they touch (decision 44)"
# An ONLINE placeholder read through an FSAL that negotiated chunk maps (socket v0.2) is
# fetched chunk by chunk into the placeholder (the daemon's [cache]), never filled whole
# first: a reader of the first megabyte of a 184 GB file must not wait for 184 GB.
need "chaos/r.segy is a placeholder on the edge" wait_for 60 test -f "$DATA/wait/chaos/r.segy"
C4=$((4 << 20))
# Ten ranges, 1 byte to 1 MiB, in ten of the 40 chunks (one across a chunk boundary,
# one at the end of the file), none in the first: 11 chunks touched.
RANGES="$((3 * C4 + 12345)):4096,$((7 * C4 + 999)):65536,$((11 * C4)):1048576"
RANGES="$RANGES,$((16 * C4 - 2048)):8192,$((19 * C4 + 77777)):300000,$((23 * C4 + 1)):1"
RANGES="$RANGES,$((27 * C4 + 4000000)):100000,$((31 * C4 + 500)):1048576"
RANGES="$RANGES,$((35 * C4 + 3333)):40000,$((R_SIZE - 5000)):5000"
WANT14=$(j "$($E2E ranges "$R_SIZE" "$R_SEED" "$RANGES")" 'd["sha256"]')
r_served() { j "$($E2E s3-served)" 'd.get("e2e-chaos/r.segy", 0)'; }
S14_0=$(r_served)
r=$(nfs12 4 ranges /chaos/r.segy "$RANGES" 60)
say "NFSv4 ranges: $(j "$r" '[(x["bytes"], x["not_yet"], x["s"]) for x in d.get("ranges", [])] if d["ok"] else d')"
check "10 ranges at random offsets of a $((R_SIZE >> 20)) MiB S3-resident object over NFSv4: byte-identical" \
  jt "$r" "d['ok'] and [x['sha256'] for x in d['ranges']] == $WANT14"
S14=$(($(r_served) - S14_0))
check "only the chunks they touch came from S3: $((S14 >> 20)) MiB of $((R_SIZE >> 20)) MiB (11 chunks of 4 MiB, and the header prefix)" \
  test "$S14" -gt 0 -a "$S14" -le $((14 * C4)) -a "$S14" -lt $((R_SIZE / 2))
loc=$($E2E local "$DATA/wait/chaos/r.segy")
say "r.segy on disk: $loc"
check "r.segy stays a placeholder with a chunk map ($(j "$loc" 'd["chunks_mapped"]') chunks local), never filled whole" \
  jt "$loc" "d['marker']['state'] == 'PARTIAL' and d['marker']['chunk_map'] and 11 <= (d['chunks_mapped'] or 0) <= 14 and d['allocated'] <= 14 * $C4"
# same14 ANSWER: the right bytes, and no more from S3 than the first read fetched.
same14() { jt "$1" "d['ok'] and [x['sha256'] for x in d['ranges']] == $WANT14" && [ "$(r_served)" -eq $((S14_0 + S14)) ]; }
r=$(nfs12 3 ranges /chaos/r.segy "$RANGES" 60)
check "the same ranges over NFSv3: the same bytes, from the chunks now local (no S3 bytes)" same14 "$r"
# The chunks are durable: bytes, fdatasync, map, then marker. A daemon killed and
# restarted serves them again without fetching them.
G14=$(gpid)
NR14=$(systemctl show -p NRestarts --value omnifile-edge)
T14=$(date +%s)
systemctl kill -s KILL omnifile-edge
need "systemd restarted the daemon after a SIGKILL" wait_for 30 bash -c \
  "[ \$(systemctl show -p NRestarts --value omnifile-edge) -gt $NR14 ] && systemctl is-active -q omnifile-edge"
wait_for 30 bash -c "journalctl -q -o cat -u nfs-ganesha-omni --since @$T14 | grep -q 'connected to the OmniFile daemon'" || true
r=$(nfs12 4 ranges /chaos/r.segy "$RANGES" 60)
check "after the kill, the same ranges read the same bytes from the chunks on disk (no S3 bytes)" same14 "$r"
check "Ganesha stayed up through the kill (same pid $G14)" test "$(gpid)" = "$G14"

# ---------------------------------------------------------------------------------
say "15. a migration: a user's recall staged, an fd held across the cutover, the deleter named"
# docs/design/migration-engine.md. A third bucket, its objects under the key prefix mig/,
# is a source at the share's mig/ folder; request_location (what POST /v1/locations
# runs) moves that prefix to the dataset's own backend. The worker copies with its sim
# Storage Mover (Additive runs between the sims; archived objects are `unsupported`).
BIG_SIZE=$((96 << 20 | 555))
big=$($E2E s3-put mig/big.segy "$BIG_SIZE" 71 --bucket e2e-mig) || { fail "put mig/big.segy"; finish; }
BIG_SHA=$(j "$big" 'd["sha256"]')
G_SIZE=$((2 << 20 | 99))
g=$($E2E s3-put mig/g.segy "$G_SIZE" 72 --bucket e2e-mig --archive) || { fail "put mig/g.segy"; finish; }
G_SHA=$(j "$g" 'd["sha256"]')
P_SIZE=$((1 << 20 | 7))
p=$($E2E s3-put mig/plain.segy "$P_SIZE" 73 --bucket e2e-mig) || { fail "put mig/plain.segy"; finish; }
P_SHA=$(j "$p" 'd["sha256"]')
$E2E s3-put mig/del.segy $((1 << 20 | 3)) 74 --bucket e2e-mig >/dev/null || { fail "put mig/del.segy"; finish; }
reg=$("$PY" -m omnifile_service.sources register --dataset "$DS" --backend "$S3_ID" \
  --bucket e2e-mig --prefix mig/ --path mig --owners "$OWNERS_SID" 2>&1) || { fail "source register: $reg"; finish; }
SRC3=$(j "$reg" 'd["source_id"]')
# On S3, registering a source starts its bucket's change feed at the snapshot; the
# sim can't start one at a time, so it is followed from now (the listing's snapshot).
# A prefix cuts over only once the feed has been read past its copy's start.
say "change feed: $($E2E follow e2e-mig)"
need "the worker seeds the third source (e2e-mig/mig/ at the share's mig/)" \
  wait_for 90 bash -c "$E2E source $SRC3 | grep -q '\"state\": \"seeded\"'"
need "its four objects are placeholders on the edge" wait_for 60 bash -c \
  "for f in big g plain del; do test -f $DATA/wait/mig/\$f.segy || exit 1; done"
# Who deleted it: the edge reports the NFS caller (uid, and its SID where winbind has
# one); the service names the user from its identity cache, which the directory sync
# fills from AD. Here the user's entry is written as that sync would: with winbind's SID
# for the uid on a joined edge (the dev VM), else a SID of the test domain.
WSID=$(wbinfo --uid-to-sid "$WUID" 2>/dev/null || true)
ident=$($E2E identity "$WUID" ${WSID:+--sid "$WSID"})
say "identity for uid $WUID: $ident"
WID=$(j "$ident" 'd["identity_id"]')
D_ID=$(j "$($E2E path wait mig/del.segy)" 'd["file_id"]')
r=$(nfs12 4 unlink /mig/del.segy)
check "a user deletes the S3-resident mig/del.segy over NFSv4 ($r): a tombstone in the catalog" \
  wait_for 30 bash -c "$E2E path wait mig/del.segy | grep -qx '{}'"
# An NFSv4 reader opens big.segy, reads its first 4 MiB, and keeps the file open until
# the prefix has cut over, then reads the rest through the same open.
install -d -m 1777 "$WORK/held"
rm -f "$WORK/held/go" "$WORK/held/go.started" "$WORK/held/big.json"
INO_BIG=$(stat -c %i "$DATA/wait/mig/big.segy")
$NFSC 4 /wait held /mig/big.segy $((4 << 20)) "$WORK/held/go" 900 >"$WORK/held/big.json" 2>&1 &
HPID=$!
need "an NFSv4 reader holds mig/big.segy open, its first 4 MiB read" wait_for 120 test -s "$WORK/held/go.started"
mig=$($E2E migrate wait mig/) || { fail "migrate mig/: $mig"; finish; }
MID=$(j "$mig" 'd["migration_id"]')
mstate() { j "$($E2E migration "$MID")" 'd["state"]'; }
copies() { $E2E copies wait "$1"; }
# The copier's first run copies the online objects; g.segy is archived (unsupported).
g_uncopied() { jt "$(copies mig/g.segy)" "[l['backend_id'] for l in d['locations']] == [$S3_ID]"; }
need "migration $MID of mig/: the first run copied the online objects, and verifies them" \
  wait_for 240 bash -c "[ \"\$($E2E migration $MID | $PY -c 'import json,sys; print(json.load(sys.stdin)[\"state\"])')\" = verifying ]"
check "and g.segy (Glacier) has no copy yet: only its S3 object" g_uncopied
RUN0=$(j "$($E2E migration "$MID")" 'd["last_run_id"]')
say "migration $MID: $($E2E migration "$MID")"
# A user reads g.segy while no copy has reached it (migration-engine.md section 4).
first=$(nfs12 3 sum /mig/g.segy)
check "a user's read of mig/g.segy: 'not yet' (JUKEBOX), never ENOENT" jt "$first" "$NOT_YET"
grec() { $E2E recall wait mig/g.segy; }
check "its S3 restore serves the migration too (a user recall, migrate_on_complete)" wait_for 60 bash -c \
  "$E2E recall wait mig/g.segy | $PY -c 'import json,sys; d=json.load(sys.stdin); x=d.get(\"restore\") or {}; sys.exit(0 if d.get(\"state\") == \"rehydrating\" and x.get(\"triggered_by\") == \"user_recall\" and x.get(\"migrate_on_complete\") else 1)'"
# The sim restores from Glacier in 5 h (Standard): move the clocks an hour at a time,
# never 6 h past the run (that would start the prefix's next run, migration-engine.md
# section 3), until the restore is done.
for _ in 1 2 3 4 5 6; do
  jt "$(grec)" "d['state'] not in ('requested', 'rehydrating')" && break
  "$PY" -c 'import httpx; httpx.post("http://127.0.0.1:10000/_sim/advance", json={"seconds": 3600}, trust_env=False).raise_for_status()'
  sleep 4
done
say "g.segy's recall: $(grec)"
answers=$(read13 3 /mig/g.segy 180)
final=$(tail -1 <<<"$answers")
check "once restored, the user's read returns g.segy's bytes, no zero blocks" \
  jt "$final" "d['ok'] and d['bytes'] == $G_SIZE and d['sha256'] == '$G_SHA' and d['zero_blocks'] == 0"
g_staged() { jt "$($E2E migration "$MID")" "any(s['source_key'] == 'mig/g.segy' and s['bucket'] == 'e2e-ops' and s['object_key'].startswith('omnifile-staging/$MID/') and s['triggered_by'] == 'user_recall' for s in d['staging'])"; }
check "the restore was staged: an S3 copy of the restored object into OmniFile's staging bucket (e2e-ops), never the source bucket" \
  wait_for 180 g_staged
g_moved() { jt "$($E2E migration "$MID")" "any(s['source_key'] == 'mig/g.segy' and s['staging_run_id'] and s['run_state'] == 'done' for s in d['staging'])"; }
check "a staging run copied it into Azure" wait_for 180 g_moved
g_verified() { jt "$(copies mig/g.segy)" "any(l['backend_id'] != $S3_ID and l['state'] == 'committed' and l['verified'] for l in d['locations'])"; }
check "its copy in Azure is verified (a CRC read in Azure against the S3 object's own)" wait_for 180 g_verified
is_cut_over() { [ "$(mstate)" = cut_over ]; }
check "migration $MID cut over" wait_for 600 is_cut_over
m=$($E2E migration "$MID")
say "migration $MID: $m"
check "with no rerun of the whole prefix: the copier's last run is still the first ($RUN0)" \
  jt "$m" "d['last_run_id'] == '$RUN0' and d['files_total'] == d['files_verified'] == 3"
gc=$(copies mig/g.segy)
check "the cutover used the staged copy: g.segy's primary is its verified copy in Azure, the S3 object secondary" \
  jt "$gc" "any(l['role'] == 'primary' and l['backend_id'] != $S3_ID and l['verified'] for l in d['locations']) and any(l['role'] == 'secondary' and l['backend_id'] == $S3_ID for l in d['locations'])"
check "and the staging object was deleted once its copy was verified" \
  jt "$m" "[s['state'] for s in d['staging'] if s['source_key'] == 'mig/g.segy'] == ['removed']"
touch "$WORK/held/go"
wait "$HPID"
r=$(cat "$WORK/held/big.json")
say "held reader: $(j "$r" '{k: v for k, v in d.items() if k != "sha256"}')"
check "the fd opened before the cutover read all of big.segy through it, byte-identical, no zero blocks" \
  jt "$r" "d['ok'] and d['bytes'] == $BIG_SIZE and d['sha256'] == '$BIG_SHA' and d['zero_blocks'] == 0"
check "and the file kept its inode on the edge ($INO_BIG)" test "$(stat -c %i "$DATA/wait/mig/big.segy")" = "$INO_BIG"
p_served() { j "$($E2E s3-served)" 'd.get("e2e-mig/mig/plain.segy", 0)'; }
P0=$(p_served)
r=$(nfs12 4 sum /mig/plain.segy)
from_azure() { jt "$1" "d['ok'] and d['sha256'] == '$P_SHA'" && [ "$(p_served)" = "$P0" ]; }
check "an object first read after the cutover comes from Azure: byte-identical, no S3 bytes" from_azure "$r"
rep=$($E2E cutover-report wait mig/)
say "cutover report: $rep"
acts=$($E2E actors "$D_ID")
tomb=$(j "$acts" '[v for v in d["versions"] if v["kind"] == "tombstone"][-1]')
say "del.segy's tombstone: $tomb"
check "the cutover report lists mig/del.segy (its S3 object, never deleted by OmniFile)" \
  jt "$rep" "[(i['source']['container'], i['source']['key']) for i in d['items'] if i['path'].endswith('/mig/del.segy')] == [('e2e-mig', 'mig/del.segy')]"
deleter() {
  jt "$tomb" "d['actor_via'] == 'nfs' and d['actor_uid'] == $WUID and d['actor_identity_id'] == $WID" &&
    jt "$rep" "[(i['deleted_by']['identity_id'], i['deleted_by']['proof']) for i in d['items'] if i['path'].endswith('/mig/del.segy')] == [('$WID', 'claimed')]"
}
check "and who deleted it: the NFS user's identity ($WID: uid $WUID${WSID:+, $WSID}, via nfs), claimed (sec=sys)" deleter

# ---------------------------------------------------------------------------------
say "16. no write reached a source bucket in the whole run (decision 40)"
reqs=$($E2E s3-requests)
say "S3 requests by OmniFile: $(j "$reqs" 'd["omnifile"]')"
check "OmniFile never wrote a source bucket (e2e-bucket, e2e-chaos, e2e-mig: no PUT, COPY, DELETE, tagging, retention or policy request)" \
  jt "$reqs" "not [(b, w) for b in ('e2e-bucket', 'e2e-chaos', 'e2e-mig') for w in d['writes'] if d['omnifile'].get(b, {}).get(w)]"
check "its only S3 writes went to its staging bucket: the staged copy in, then deleted ($(j "$reqs" 'd["omnifile"].get("e2e-ops")'))" \
  jt "$reqs" "{b for b, ops in d['omnifile'].items() if any(ops.get(w) for w in d['writes'])} == {'e2e-ops'} and d['omnifile']['e2e-ops'].get('COPY', 0) >= 1 and d['omnifile']['e2e-ops'].get('DELETE', 0) >= 1"
check "and its restores and staging copy read the sources (RESTORE, COPY_FROM on e2e-mig)" \
  jt "$reqs" "d['omnifile']['e2e-mig'].get('RESTORE', 0) >= 1 and d['omnifile']['e2e-mig'].get('COPY_FROM', 0) >= 1"

finish
