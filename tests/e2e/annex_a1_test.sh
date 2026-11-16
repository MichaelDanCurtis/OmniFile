#!/usr/bin/env bash
# Annex phase A1 exit test (docs/design/annex.md section 10.1): a Copy annex
# on a test Samba server and on a test NFS server. Runs as root on a Linux
# machine joined to the dev AD (the Lima VM omnifile-acl), self-contained: it
# starts its own Postgres, sim Blob facade, enrol, api and worker on
# loopback ports, its own NAS servers, and the annex units as transient
# systemd units, and tears all of it down at the end (E2E_KEEP=1 keeps it).
#
# The two NAS:
# - SMB: smbd on 127.0.0.2:4450, joined to the dev AD as its own computer
#   account (A1NAS; the join is kept in $E2E_EXT/smb-state). The annex mounts
#   it read-only with Kerberos. The edge's computer account gets its ticket,
#   (the users' SMB mount is geo-writer's, with Kerberos too),
#   but a Samba server with idmap_ad can't map a computer account without a
#   uidNumber (the shared dev DC isn't changed for it), so the SMB annex uses
#   the credential mode `service_keytab`: an AD user's keytab, made from the
#   dev AD's env file.
# - NFS: NFS-Ganesha (FSAL_VFS) in its own network namespace (10.214.0.2),
#   NFSv3, `sec=sys` (the host-trusted fallback: an nfs/ SPN would need a
#   change to the shared DC).
# Both export xfs images mounted `strictatime`: every read moves a file's
# atime, the annex's reads included (a read, not a write, but an
# accessed_age rule on the NAS sees it). The test's own reads use O_NOATIME,
# and the "nothing written" digest leaves atimes out; a separate check shows
# the annex's reads moving them, so the check is live.
# The test changes files the way users do: over the NAS's own protocol (an
# SMB mount as another AD user, an NFS mount), never behind the server's
# back, where its caches would hide the change from every client.
#
# What it proves, for each annex (each line of the results file is a check):
#   1. the mount is read-only (the mount event; a write in the unit's mount
#      namespace fails with EROFS), with the expected security;
#   2. the baseline copies every file, each verified by the in-Azure CRC read
#      (verified_by azure_read_crc), the copy's CRC-64/NVME equal to the
#      NAS file's; baseline files are not arrivals;
#   3. a file changed on the NAS is copied again as a new version, the old
#      one kept; a new file arrives and is copied;
#   4. nothing was written to the NAS: a digest of the whole export (names,
#      sizes, bytes, mode, owner, mtime, ctime) is unchanged across every
#      stretch where only the annex touched it; atimes moved only by its reads;
#   5. a file deleted on the NAS is a tombstone, its copy kept;
#   6. reconciliation catches a change the quick scans missed (a rewrite in
#      place, full scans off) and an arrival whose directory mtime was put
#      back: drift rows, then copies;
#   7. the unit killed (SIGKILL) mid-copy of a large file restarts and
#      completes it: one committed version, no duplicate, CRC equal.
#
# Then phase A3.4, replaces at the origin (RP0-RP7, stage `replace`, a34.md): an
# older version replaces a verified file under its own name; a change, a legal hold
# or a write in the grace keeps the file there; a write after the link keeps it under
# a visible -replaced- name; a kill at each step resumes and deletes once.
#
# Then phase A2.2, removals (R0-R12, wp14-a2.md "What writes to the filer"):
# files the user drops under lts/rm/ move to Azure (Move now for that folder),
# with the shortest grace an annex can have (1 minute; the service keeps it
# too): no read-write mount before removals are
# live; a removal hides, waits, deletes and leaves a pointer, after an
# in-Azure read; a pointer is never overwritten; a change or a rename back in
# the grace, a hard link, an alternate data stream (SMB) and a change between
# the ticket and the hide each cancel it; a pause keeps a hidden file hidden
# and unmounts the read-write mount; the unit killed before and after each of
# W1-W4 resumes and removes once; and the whole export shows exactly the
# expected changes. The ticket's expiry and the sharing violation of an SMB
# open are covered by the service's and the edge's own tests.
#
# Environment:
#   E2E_EXT        persistent scratch (default /mnt/ext/wp14): the venv, the
#                  NAS images, the test server's AD join
#   E2E_ANNEX_BIN  omnifile-annex (default $E2E_EXT/target/debug/omnifile-annex)
#   E2E_EDGE_BIN   omnifile-edge, for `enrol` (default next to it)
#   E2E_PY         the service's Python (default $E2E_EXT/venv/bin/python)
#   AD_ENV_FILE    the dev AD's secrets (default /work/dev/.secrets/ad.env)
#   E2E_RESULTS, E2E_REV, E2E_KEEP=1, E2E_BIG_MIB (default 400)
set -uo pipefail
if [ -z "${E2E_COPY:-}" ]; then
  copy=$(mktemp /tmp/omnifile-annex-a1-XXXXXX.sh)
  cp "$0" "$copy"
  E2E_COPY=1 E2E_SELF_SHA=$(sha256sum "$0" | cut -d' ' -f1) \
    E2E_HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) exec bash "$copy" "$@"
fi

HERE=${E2E_HERE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
REPO=$(cd "$HERE/../.." && pwd)
EXT=${E2E_EXT:-/mnt/ext/wp14}
ANNEX=${E2E_ANNEX_BIN:-$EXT/target/debug/omnifile-annex}
EDGE=${E2E_EDGE_BIN:-$(dirname "$ANNEX")/omnifile-edge}
PY=${E2E_PY:-$EXT/venv/bin/python}
# Not under /tmp or /var/tmp: the annex units have PrivateTmp=yes.
WORK=/srv/omnifile-annex-a1
RESULTS=${E2E_RESULTS:-$HERE/results/annex-a1-latest.md}
AD_ENV=${AD_ENV_FILE:-/work/dev/.secrets/ad.env}
BIG_MIB=${E2E_BIG_MIB:-400}
PG_PORT=55461 SIM_PORT=55471 API_PORT=55472 ENROL_PORT=55473
NS=wp14nas NAS_IP=10.214.0.2 EDGE_IP=10.214.0.1
SMB_IP=127.0.0.2 SMB_PORT=4450 SMB_NAME=A1NAS SMB_USER=geo-reader
HANG_S=90
NPASS=0 NFAIL=0 T0=$(date +%s)
PIDS=()

# E2E_ONLY names the stages to run, for working on one: never a green exit test.
STAGES="smb nfs root v4 placer replace hang stuck"
for s in ${E2E_ONLY:-}; do
  [[ " $STAGES " == *" $s "* ]] || { echo "E2E_ONLY: no stage $s (there's: $STAGES)" >&2; exit 2; }
done
if [ -n "${E2E_ONLY:-}" ] && [ -z "$(echo $E2E_ONLY)" ]; then
  echo "E2E_ONLY names no stage" >&2
  exit 2
fi
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
for t in "$ANNEX" "$EDGE" "$PY"; do [ -x "$t" ] || { echo "missing $t" >&2; exit 2; }; done
for c in smbd net mount.cifs kinit ktutil ip nsenter setpriv mkfs.xfs systemd-run; do
  command -v "$c" >/dev/null || { echo "needs $c" >&2; exit 2; }
done
[ -x /opt/ganesha/bin/ganesha.nfsd ] || { echo "needs NFS-Ganesha in /opt/ganesha" >&2; exit 2; }
[ -r "$AD_ENV" ] || { echo "needs the dev AD env file $AD_ENV" >&2; exit 2; }
PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
[ -x "$PGBIN/postgres" ] || { echo "needs a PostgreSQL server" >&2; exit 2; }

say() { printf '[annex-a1 %4ss] %s\n' "$(($(date +%s) - T0))" "$*"; }
pass() { say "PASS: $*"; echo "| PASS | $* |" >>"$WORK/checks.txt"; NPASS=$((NPASS + 1)); }
fail() { say "FAIL: $*"; echo "| **FAIL** | $* |" >>"$WORK/checks.txt"; NFAIL=$((NFAIL + 1)); }
check() { local d=$1; shift; if "$@"; then pass "$d"; else fail "$d"; fi; }
need() { local d=$1; shift; if "$@"; then pass "$d"; else fail "$d"; finish; fi; }
j() { "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); v=eval(sys.argv[2]); print(v if not isinstance(v,(dict,list)) else json.dumps(v))' "$1" "$2"; }
jt() { "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if eval(sys.argv[2]) else 1)' "$1" "$2"; }
wait_for() {
  local t=$1; shift
  local end=$(($(date +%s) + t))
  while [ "$(date +%s)" -lt "$end" ]; do
    "$@" && return 0
    sleep 1
  done
  "$@"
}
A1() { timeout 300 "$PY" "$HERE/annex_a1.py" "$@"; }

keep_logs() { # E2E_LOGS: what a failed run needs read, somewhere the next run won't wipe
  [ -n "${E2E_LOGS:-}" ] || return 0
  rm -rf "$E2E_LOGS"; mkdir -p "$E2E_LOGS"
  cp -a "$WORK/log/." "$E2E_LOGS/" 2>/dev/null
  cp -a "$WORK/checks.txt" "$E2E_LOGS/" 2>/dev/null
  journalctl --since "@$T0" -u 'omnifile-annex-a1-*' --no-pager -o short-iso >"$E2E_LOGS/units.journal" 2>/dev/null
}
cleanup() {
  keep_logs
  ip netns exec "$NS" iptables -D INPUT -p tcp --dport 2049 -j DROP 2>/dev/null
  [ -f "$WORK/ganesha.pid" ] && kill -CONT "$(cat "$WORK/ganesha.pid")" 2>/dev/null
  for u in $(systemctl list-units --plain --no-legend 'omnifile-annex-a1-*' 2>/dev/null | awk '{print $1}'); do
    systemctl stop "$u" 2>/dev/null
  done
  systemctl reset-failed 'omnifile-annex-a1-*' 2>/dev/null
  rm -f "$WORK/writer.pw"
  [ "${E2E_KEEP:-0}" = 1 ] && return
  pkill -f "$WORK/smb/smb.conf" 2>/dev/null
  [ -f "$WORK/ganesha.pid" ] && kill "$(cat "$WORK/ganesha.pid")" 2>/dev/null
  ip netns pids "$NS" 2>/dev/null | xargs -r kill 2>/dev/null
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
  [ -d "$WORK/pg/data" ] && setpriv --reuid=nobody --regid=nogroup --clear-groups \
    "$PGBIN/pg_ctl" -D "$WORK/pg/data" -m fast stop >/dev/null 2>&1
  sleep 1
  for m in "$WORK"/w/* "$WORK"/nas/*; do umount -l "$m" 2>/dev/null; done
  ip netns del "$NS" 2>/dev/null
  ip link del wp14h 2>/dev/null
}
trap cleanup EXIT

setup_failed() { # a setup step failed: a failure, whatever passed before it
  NFAIL=$((NFAIL + 1))
  say "FAIL: setup failed, near line ${BASH_LINENO[0]}"
  echo "| FAIL | setup, near line ${BASH_LINENO[0]} |" >>"$WORK/checks.txt"
  finish
}
finish() {
  {
    echo "# Annex A1 exit test: $(date -u +%FT%TZ)"
    echo
    echo "rev ${E2E_REV:-unknown}, kernel $(uname -r), script ${E2E_SELF_SHA:-?}"
    echo
    echo "omnifile-annex $(sha256sum "$ANNEX" | cut -c1-16), $(smbd --version), $(/opt/ganesha/bin/ganesha.nfsd -v 2>&1 | head -1)"
    echo
    echo "| result | check |"
    echo "|---|---|"
    cat "$WORK/checks.txt"
    echo
    echo "$NPASS passed, $NFAIL failed"
    if [ -n "${E2E_ONLY:-}" ]; then
      echo
      echo "PARTIAL RUN: only the stages $E2E_ONLY (E2E_ONLY). This is not the exit test."
    fi
  } >"$RESULTS"
  say "$NPASS passed, $NFAIL failed ($RESULTS)${E2E_ONLY:+ PARTIAL RUN: only $E2E_ONLY}"
  [ "$NFAIL" -eq 0 ] || exit 1
  # A partial run can't look green.
  [ -z "${E2E_ONLY:-}" ] || exit 3
  exit 0
}

# --- 0. clean slate ------------------------------------------------------------------
cleanup
rm -rf "$WORK"
install -d -m 0755 "$WORK" "$WORK/nas" "$WORK/annex" "$WORK/mnt" "$WORK/log"
mkdir -p "$(dirname "$RESULTS")"
: >"$WORK/checks.txt"

# --- 1. the service, on loopback -----------------------------------------------------------
say "starting Postgres, the sim facade, enrol, api and worker"
install -d -o nobody -g nogroup -m 0700 "$WORK/pg"
asnobody() { setpriv --reuid=nobody --regid=nogroup --clear-groups "$@"; }
asnobody "$PGBIN/initdb" -D "$WORK/pg/data" -U postgres --auth=trust >/dev/null || setup_failed
asnobody "$PGBIN/pg_ctl" -D "$WORK/pg/data" -l "$WORK/pg/log" -w \
  -o "-c listen_addresses=127.0.0.1 -p $PG_PORT -c unix_socket_directories=$WORK/pg -c max_connections=200 -c fsync=off" \
  start >/dev/null || setup_failed
export OMNIFILE_DATABASE_URL="postgresql://postgres@127.0.0.1:$PG_PORT/postgres"
PKI=$WORK/pki
install -d -m 0700 "$PKI"
"$PY" -m omnifile_service.enrol init-dev-ca "$PKI/ca.key" "$PKI/ca.crt" >/dev/null || setup_failed
openssl ecparam -name prime256v1 -genkey -noout -out "$PKI/server.key" 2>/dev/null
openssl req -new -key "$PKI/server.key" -subj "/CN=omnifile-annex-a1" -out "$PKI/server.csr" 2>/dev/null
printf 'subjectAltName=IP:127.0.0.1,DNS:localhost\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature\nbasicConstraints=critical,CA:FALSE\n' >"$PKI/server.ext"
openssl x509 -req -in "$PKI/server.csr" -CA "$PKI/ca.crt" -CAkey "$PKI/ca.key" -CAcreateserial \
  -days 2 -sha256 -extfile "$PKI/server.ext" -out "$PKI/server.crt" 2>/dev/null
cat "$PKI/server.crt" "$PKI/ca.crt" >"$PKI/server-chain.pem"
secret() { head -c 32 /dev/urandom | base64 | tr -d '\n'; }
export OMNIFILE_CA_CERT=$PKI/ca.crt OMNIFILE_DEV_CA_KEY=$PKI/ca.key OMNIFILE_DEV_CA_CERT=$PKI/ca.crt
export OMNIFILE_API_CLIENT_AUTH=direct_tls OMNIFILE_API_TLS_CERT=$PKI/server-chain.pem
export OMNIFILE_API_TLS_KEY=$PKI/server.key OMNIFILE_API_URL=https://127.0.0.1:$API_PORT
export OMNIFILE_API_HOST=127.0.0.1 OMNIFILE_ENROL_HOST=127.0.0.1 OMNIFILE_DEV_MODE=1
export OMNIFILE_ENROL_DEV_TOKEN=$(secret) OMNIFILE_API_LEASE_KEY=$(secret)
export OMNIFILE_ENROL_SIGN_URL=https://127.0.0.1:$ENROL_PORT/internal/v0/sign
export OMNIFILE_API_HEALTH_PORT=55474 PYTHONUNBUFFERED=1
export E2E_SIM_URL=http://127.0.0.1:$SIM_PORT E2E_SIM_KEY=$WORK/sim-key.json
"$PY" -m omnifile_service.catalog.migrate >/dev/null || setup_failed
(cd "$WORK" && exec "$PY" -m omnifile_service.backends.sim_http --host 127.0.0.1 --port $SIM_PORT \
  --key-file "$E2E_SIM_KEY" >"$WORK/log/sim.log" 2>&1) & PIDS+=($!)
wait_for 20 test -s "$E2E_SIM_KEY" || setup_failed
(cd "$WORK" && PORT=$ENROL_PORT exec "$PY" -m uvicorn --factory omnifile_service.enrol.__main__:build \
  --host 127.0.0.1 --port $ENROL_PORT --ssl-certfile "$PKI/server-chain.pem" \
  --ssl-keyfile "$PKI/server.key" --no-server-header >"$WORK/log/enrol.log" 2>&1) & PIDS+=($!)
SEED=$(A1 seed --bundle-out "$WORK/bundle" --enrol-url https://127.0.0.1:$ENROL_PORT --server-ca "$PKI/ca.crt") || setup_failed
(cd "$WORK" && PORT=$API_PORT exec "$PY" -m omnifile_service.api >"$WORK/log/api.log" 2>&1) & PIDS+=($!)
(cd "$WORK" && exec "$PY" -m omnifile_service.worker >"$WORK/log/worker.log" 2>&1) & PIDS+=($!)
up() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
need "service up: api :$API_PORT, enrol :$ENROL_PORT, sim :$SIM_PORT" wait_for 60 bash -c "$(declare -f up); up $API_PORT && up $ENROL_PORT"

# --- 2. the edge's identity and config ------------------------------------------------------------
SMB_SID=$(wbinfo -n "OMNI\\$SMB_USER" 2>/dev/null | awk '{print $1}')
# A3.3's restore-rights account, not the read account (R4), with its own uid's
# ticket cache (cruid).
RESTORE_USER=geo-owner
RESTORE_SID=$(wbinfo -n "OMNI\\$RESTORE_USER" 2>/dev/null | awk '{print $1}')
RESTORE_UID=$(id -u "$RESTORE_USER" 2>/dev/null)
[ -n "$SMB_SID" ] && [ -n "$RESTORE_SID" ] && [ -n "$RESTORE_UID" ] || { echo "needs $SMB_USER and $RESTORE_USER through winbind" >&2; exit 2; }
cat >"$WORK/edge.toml" <<EOF
# Annex A1 exit test (tests/e2e/annex_a1_test.sh): only [service] and [annex] matter.
data_root = "$WORK/unused"
replica_path = "$WORK/unused/replica.db"
socket_path = "$WORK/unused/fsal.sock"
backend = "service"
[log]
format = "text"
filter = "info"
[service]
identity_dir = "$WORK/identity"
outbox_dir = "$WORK/outbox"
key_protection = "file"
[annex]
state_dir = "$WORK/annex"
mount_root = "$WORK/mnt"
tick_s = 1
hang_s = $HANG_S
poll_s = 5
quiet_s = 3
quick_scan_s = 4
# Full scans and reconciliation only when the test asks (SIGHUP, SIGUSR2).
full_scan_s = 86400
reconcile_s = 864000
smb_options = "port=$SMB_PORT,ip=$SMB_IP"
nfs_options = "port=2049,mountport=20048,mountproto=tcp,nolock"
service_keytab = "$WORK/annex.keytab"
service_principal = "$SMB_USER@OMNI.TEST"
# A3.3: the placement mount's account, and the staging folder's DACL naming
# it (full) and the read account (read and traverse).
restore_keytab = "$WORK/restore.keytab"
restore_principal = "$RESTORE_USER@OMNI.TEST"
restore_uid = $RESTORE_UID
placement_sid = "$RESTORE_SID"
read_sid = "$SMB_SID"
EOF
install -d -m 0700 "$WORK/identity" "$WORK/outbox"
need "the edge enrols" "$EDGE" --config "$WORK/edge.toml" enrol "$WORK/bundle"

# --- 3. the NAS servers --------------------------------------------------------------------------
image() { # image NAME MiB -> mounted xfs at $WORK/nas/NAME, strictatime
  local img=$EXT/nas/$1.img
  install -d "$EXT/nas" "$WORK/nas/$1"
  rm -f "$img"
  truncate -s "${2}M" "$img" && mkfs.xfs -q -f "$img" && mount -o loop,strictatime "$img" "$WORK/nas/$1"
}
need "the NAS volumes (xfs, strictatime)" bash -c "$(declare -f image); EXT=$EXT WORK=$WORK image smb $((BIG_MIB + 600)) && EXT=$EXT WORK=$WORK image nfs $((BIG_MIB + 600))"
populate() { # the files present when the annex starts
  local r=$1
  mkdir -p "$r/lts/surveys/2026/raw" "$r/lts/docs" "$r/elsewhere" "$r/lts/rm"
  "$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); from annex_a1_files import write_all; write_all(sys.argv[2])' "$HERE" "$r/lts"
  echo "not annexed" >"$r/elsewhere/outside.txt"
  chmod -R a+rX "$r"
  chmod 0777 "$r/lts/rm" # removals (A2.2): the annex's account deletes there
}
populate "$WORK/nas/smb"
populate "$WORK/nas/nfs"
chmod -R a+w "$WORK/nas/smb/lts" # the writer is an ordinary AD user

# A third NAS volume, for an annex at the export's root (the staging folder
# then sits inside what the annex scans).
need "the export-root volume" bash -c "$(declare -f image); EXT=$EXT WORK=$WORK image rootx 400"
mkdir -p "$WORK/nas/rootx/docs" "$WORK/nas/rootx/elsewhere/restored"
echo "at the export root" >"$WORK/nas/rootx/docs/x.bin"
echo "beside it" >"$WORK/nas/rootx/docs/y.bin"
chmod -R a+rX "$WORK/nas/rootx"
chmod 0777 "$WORK/nas/rootx" "$WORK/nas/rootx/elsewhere" "$WORK/nas/rootx/elsewhere/restored"

# And a fourth, served over NFSv4 with its ACLs shown (FSAL_VFS derives them from
# the mode and any POSIX ACL; not a real NAS's, and not knfsd's).
need "the NFSv4 volume" bash -c "$(declare -f image); EXT=$EXT WORK=$WORK image v4x 400"
mkdir -p "$WORK/nas/v4x/lts/docs" "$WORK/nas/v4x/elsewhere/restored"
echo "over NFSv4" >"$WORK/nas/v4x/lts/docs/x.bin"
echo "killed at secured" >"$WORK/nas/v4x/lts/docs/y.bin"
echo "root's" >"$WORK/nas/v4x/lts/docs/z.bin"
chmod -R a+rX "$WORK/nas/v4x"
# Someone's, for P5 to put back. z.bin stays root's: uid 0 has no SID.
chown 20002:20001 "$WORK/nas/v4x/lts/docs/x.bin" "$WORK/nas/v4x/lts/docs/y.bin"
chmod 0777 "$WORK/nas/v4x" "$WORK/nas/v4x/elsewhere" "$WORK/nas/v4x/elsewhere/restored"

# SMB: the test server joined as A1NAS; its own dirs, its own keytab.
SMBW=$WORK/smb SMBP=$EXT/smb-state
install -d "$SMBW/run" "$SMBW/lock" "$SMBW/state" "$SMBW/cache" "$SMBP/private"
cat >"$SMBW/smb.conf" <<EOF
[global]
  workgroup = OMNI
  realm = OMNI.TEST
  netbios name = $SMB_NAME
  security = ads
  kerberos method = secrets and keytab
  dedicated keytab file = $SMBP/krb5.keytab
  server role = member server
  interfaces = $SMB_IP/8
  bind interfaces only = yes
  smb ports = $SMB_PORT
  pid directory = $SMBW/run
  lock directory = $SMBW/lock
  state directory = $SMBW/state
  cache directory = $SMBW/cache
  private dir = $SMBP/private
  ncalrpc dir = $SMBW/run/ncalrpc
  machine password timeout = 0
  winbind use default domain = yes
  idmap config * : backend = tdb
  idmap config * : range = 3000-7999
  idmap config OMNI : backend = ad
  idmap config OMNI : schema_mode = rfc2307
  idmap config OMNI : range = 10000-999999
  idmap config OMNI : unix_nss_info = yes
  log file = $SMBW/log.%m
  disable netbios = yes
  load printers = no
  # A server that never writes on a read: no DOS attributes stored. Streams
  # (A2.2's alternate-data-stream gate) are kept in xattrs, written only when
  # a client writes a stream.
  store dos attributes = no
  ea support = yes
  vfs objects = streams_xattr
  map archive = no
  server min protocol = SMB2_10
[annex]
  path = $WORK/nas/smb
  read only = no
EOF
set -a; . "$AD_ENV"; set +a
if ! net ads testjoin -s "$SMBW/smb.conf" >/dev/null 2>&1; then
  # `net ads join` writes the system keytab whatever the config says: keep
  # this machine's own keytab safe and give the test server its own.
  cp -p /etc/krb5.keytab "$WORK/host.keytab.bak"
  KRB5_KTNAME=FILE:$SMBP/krb5.keytab net ads join -s "$SMBW/smb.conf" -U "$AD_ADMIN_USER%$AD_ADMIN_PASSWORD" \
    --no-dns-updates "dnshostname=${SMB_NAME,,}.omni.test" >"$WORK/log/join.log" 2>&1
  cmp -s /etc/krb5.keytab "$WORK/host.keytab.bak" || {
    cp -p /etc/krb5.keytab "$SMBP/krb5.keytab"; cp -p "$WORK/host.keytab.bak" /etc/krb5.keytab; }
fi
need "the test Samba server is joined to the dev AD ($SMB_NAME)" net ads testjoin -s "$SMBW/smb.conf"
smbd -D -s "$SMBW/smb.conf"
# The restore-rights account's privileges on this server (the setup guide's
# step on a real NAS), which set a placed file's owner back (A3.3 P5). Samba
# wants both: SeTakeOwnership to open for WRITE_OWNER, SeRestore to give the
# file to someone else.
need "the restore account holds its privileges on the test server" \
  net sam rights grant "OMNI\\$RESTORE_USER" SeRestorePrivilege SeTakeOwnershipPrivilege -s "$SMBW/smb.conf"
# The SMB annex's service account: a keytab from the dev AD's password.
pwvar="AD_PASSWORD_$(printf '%s' "$SMB_USER" | tr 'a-z-' 'A-Z_')"
printf 'addent -password -p %s@OMNI.TEST -k 1 -e aes256-cts-hmac-sha1-96\n%s\nwkt %s\nquit\n' \
  "$SMB_USER" "${!pwvar}" "$WORK/annex.keytab" | ktutil >/dev/null 2>&1
chmod 600 "$WORK/annex.keytab"
rpw="AD_PASSWORD_$(printf '%s' "$RESTORE_USER" | tr 'a-z-' 'A-Z_')"
printf 'addent -password -p %s@OMNI.TEST -k 1 -e aes256-cts-hmac-sha1-96\n%s\nwkt %s\nquit\n' \
  "$RESTORE_USER" "${!rpw}" "$WORK/restore.keytab" | ktutil >/dev/null 2>&1
chmod 600 "$WORK/restore.keytab"
# The users' side: another AD user writes over SMB (as people do), so the
# server breaks the annex client's leases, as a real NAS does.
wpw="AD_PASSWORD_GEO_WRITER"
(umask 077; printf '%s\n' "${!wpw}" >"$WORK/writer.pw")
unset AD_ADMIN_PASSWORD "$pwvar" "$wpw" "$rpw"
need "the SMB server listens on $SMB_IP:$SMB_PORT" wait_for 20 bash -c "(exec 3<>/dev/tcp/$SMB_IP/$SMB_PORT) 2>/dev/null"

# NFS: Ganesha (FSAL_VFS) in its own network namespace.
ip netns add "$NS"
ip link add wp14h type veth peer name wp14n
ip link set wp14n netns "$NS"
ip addr add "$EDGE_IP/30" dev wp14h && ip link set wp14h up
ip netns exec "$NS" ip addr add "$NAS_IP/30" dev wp14n
ip netns exec "$NS" ip link set wp14n up
ip netns exec "$NS" ip link set lo up
install -d "$WORK/ganesha/recovery"
cat >"$WORK/ganesha.conf" <<EOF
NFS_CORE_PARAM {
  NFS_Port = 2049; MNT_Port = 20048; Protocols = 3, 4; Bind_addr = $NAS_IP;
  Enable_NLM = false; Enable_RQUOTA = false; Plugins_Dir = "/opt/ganesha/lib/ganesha";
}
NFSV4 { Minor_Versions = 1, 2; Graceless = true; RecoveryBackend = fs; RecoveryRoot = "$WORK/ganesha/recovery"; }
EXPORT {
  Export_Id = 7; Path = "$WORK/nas/nfs"; Pseudo = "/annex"; Protocols = 3, 4;
  Transports = TCP; Access_Type = NONE; Squash = No_Root_Squash; SecType = sys;
  FSAL { Name = VFS; }
  CLIENT { Clients = $EDGE_IP; Access_Type = RW; }
}
EXPORT {
  Export_Id = 8; Path = "$WORK/nas/rootx"; Pseudo = "/rootx"; Protocols = 3, 4;
  Transports = TCP; Access_Type = NONE; Squash = No_Root_Squash; SecType = sys;
  FSAL { Name = VFS; }
  CLIENT { Clients = $EDGE_IP; Access_Type = RW; }
}
EXPORT {
  Export_Id = 9; Path = "$WORK/nas/v4x"; Pseudo = "/v4x"; Protocols = 4;
  Transports = TCP; Access_Type = NONE; Squash = No_Root_Squash; SecType = sys; Disable_ACL = false;
  FSAL { Name = VFS; }
  CLIENT { Clients = $EDGE_IP; Access_Type = RW; }
}
LOG { Default_Log_Level = WARN; }
EOF
ip netns exec "$NS" unshare -m --propagation private bash -c "
  mount -t tmpfs tmpfs /run && mkdir -p /run/rpcbind && (rpcbind -w || true)
  LD_LIBRARY_PATH=/opt/ganesha/lib exec /opt/ganesha/bin/ganesha.nfsd -F -f '$WORK/ganesha.conf' \
    -L '$WORK/log/ganesha.log' -p '$WORK/ganesha.pid' -N NIV_WARN" >"$WORK/log/ganesha.out" 2>&1 &
PIDS+=($!)
need "the NFS server listens on $NAS_IP:2049" wait_for 30 bash -c "(exec 3<>/dev/tcp/$NAS_IP/2049) 2>/dev/null"

# The users' mounts: every change the test makes goes through the NAS's own
# protocol, never behind the server's back (its caches would hide it).
install -d "$WORK/w/smb" "$WORK/w/nfs"
# The SMB one lives in its own mount namespace with its own /tmp, so geo-writer's
# Kerberos ticket (where cifs.upcall looks: root's default cache) never touches
# this machine's: the test writes there through nsenter.
unshare -m --propagation private sh -c "mount -t tmpfs tmpfs /tmp && kinit geo-writer@OMNI.TEST <'$WORK/writer.pw' >/dev/null &&
  mount -t cifs //${SMB_NAME,,}.omni.test/annex '$WORK/w/smb' -o rw,sec=krb5,cruid=0,port=$SMB_PORT,ip=$SMB_IP,cache=none,actimeo=0,nosharesock &&
  touch /tmp/ready && exec sleep infinity" >"$WORK/log/writer.log" 2>&1 &
WPID=$!
PIDS+=("$WPID")
need "the users' SMB mount (geo-writer, read-write, Kerberos)" wait_for 30 nsenter -t "$WPID" -m test -e /tmp/ready
rm -f "$WORK/writer.pw"
need "the users' NFS mount (read-write)" mount -t nfs "$NAS_IP:$WORK/nas/nfs" "$WORK/w/nfs" \
  -o "rw,vers=3,port=2049,mountport=20048,mountproto=tcp,nolock,actimeo=0"

# --- 4. the annexes --------------------------------------------------------------------------------
SMB_A=$(A1 annex --name smb --protocol smb --host "${SMB_NAME,,}.omni.test" --export annex --path lts) || setup_failed
NFS_A=$(A1 annex --name nfs --protocol nfs3 --host "$NAS_IP" --export "$WORK/nas/nfs" --path lts --sec-sys) || setup_failed
ROOT_A=$(A1 annex --name rootx --protocol nfs3 --host "$NAS_IP" --export "$WORK/nas/rootx" --path "" --sec-sys) || setup_failed
V4_A=$(A1 annex --name v4x --protocol nfs4 --host "$NAS_IP" --export /v4x --path lts --sec-sys) || setup_failed
SMB_ID=$(j "$SMB_A" 'd["annex_id"]'); NFS_ID=$(j "$NFS_A" 'd["annex_id"]'); ROOT_ID=$(j "$ROOT_A" 'd["annex_id"]'); V4_ID=$(j "$V4_A" 'd["annex_id"]')
"$PY" - "$SMB_ID" <<'PY' || setup_failed
import os, sys, psycopg
with psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"], autocommit=True) as c:
    c.execute("UPDATE catalog.filer SET credential_mode = 'service_keytab'"
              " WHERE filer_id = (SELECT filer_id FROM catalog.annex WHERE annex_id = %s)",
              (int(sys.argv[1]),))
PY
# Registering the placement account before its edge has said anything: refused.
r=$(A1 register-placer "$SMB_ID" "$RESTORE_SID")
check "smb: registering before the edge has reported is refused, and says so" jt "$r" "d['status'] == 400 and 'no_report' in d['detail']"

unit() { echo "omnifile-annex-a1-$1"; }
# The shipped unit's capabilities, read from it, so this test can't pass on
# something the unit can't do.
UNIT_FILE=$HERE/../../edge/crates/omnifile-edge/systemd/omnifile-annex@.service
UNIT_CAPS=$(sed -n 's/^CapabilityBoundingSet=//p' "$UNIT_FILE")
[ -n "$UNIT_CAPS" ] || { echo "no CapabilityBoundingSet in $UNIT_FILE" >&2; exit 2; }
start_unit() { # start_unit ID [VAR=VALUE...]: the unit's settings, as omnifile-annex@.service has them
  local id=$1 envs=() e; shift
  for e in "$@"; do envs+=(-E "$e"); done
  timeout 180 systemctl stop "$(unit "$id")" 2>/dev/null
  systemctl reset-failed "$(unit "$id")" 2>/dev/null
  systemd-run --quiet --unit="$(unit "$id")" -p PrivateMounts=yes -p PrivateTmp=yes \
    -p "CapabilityBoundingSet=$UNIT_CAPS" -p NoNewPrivileges=yes \
    -p Restart=always -p RestartSec=1 -p StartLimitIntervalSec=0 -E OMNIFILE_BLOB_ALLOW_HTTP=1 \
    "${envs[@]}" "$ANNEX" --config "$WORK/edge.toml" run --annex "$id"
}
status() { "$ANNEX" --config "$WORK/edge.toml" status --annex "$1"; }
handles() { # handles ID SIG: the unit's daemon catches SIG (so it's up, not mid-start)
  local pid n m; pid=$(systemctl show -p MainPID --value "$(unit "$1")")
  [ "${pid:-0}" -gt 0 ] || return 1
  n=$(kill -l "${2#SIG}") && m=$(awk '/^SigCgt:/ {print $2}' "/proc/$pid/status" 2>/dev/null) && [ -n "$m" ] || return 1
  (( (16#$m >> (n - 1)) & 1 ))
}
nudge() { # SIGUSR1 quick, SIGHUP full, SIGUSR2 reconcile; to the daemon alone, once it's up
  wait_for 60 handles "$1" "$2" || say "nudge: $(unit "$1") doesn't catch $2 yet"
  systemctl kill --kill-whom=main -s "$2" "$(unit "$1")"
}
copies_why() { # copies_why ID ROOT LABEL: if a copy check failed, what it saw
  copies_ok "$1" "$2" && return 0
  say "$3: $(A1 copies "$1" "$2" | cut -c1-1500)"
  say "$3: read-backs refused: $(A1 verify-denies | cut -c1-1500)"
}
copies_ok() { # copies_ok ID ROOT: every NAS file copied, CRC equal, verified in Azure
  local r; r=$(A1 copies "$1" "$2")
  jt "$r" 'd["files"] > 0 and d["matched"] == d["files"] == d["verified"] and not d["extra"]'
}
quiet() { # the annex has nothing queued or in flight
  local s; s=$(status "$1")
  jt "$s" 'd["outbox"] == 0 and not d["copy_jobs"] and d["states"]["new"] == 0 and d["states"]["reported"] == 0 and d["states"]["ready"] == 0'
}
tree() { A1 tree "$1" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["digest"])'; }
read_set_ready() { # read_set_ready ID: the edge read every folder above the files, the export's root too
  "$PY" - "$1" <<'PY'
import os, sys, psycopg
with psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"]) as c:
    r = c.execute("SELECT a.folders_read_at IS NOT NULL AND EXISTS (SELECT 1"
                  "  FROM catalog.nas_folder_acl n JOIN catalog.acl s ON s.acl_id = n.acl_id"
                  " WHERE n.filer_id = a.filer_id AND n.path = '' AND s.search_sids IS NOT NULL)"
                  "  FROM catalog.annex a WHERE a.annex_id = %s", (int(sys.argv[1]),)).fetchone()[0]
sys.exit(0 if r else 1)
PY
}
reads_as() { # reads_as ID docs|others SID: may that user read every live file there (decision 63)?
  "$PY" - "$1" "$2" "$3" <<'PY'
import os, sys, psycopg
docs = "af.path LIKE 'docs/%%'" if sys.argv[2] == "docs" else "af.path NOT LIKE 'docs/%%'"
with psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"]) as c:
    r = c.execute(
        "SELECT bool_and(catalog.can_read_file(a.read_sids, a.deny_sids, a.deny_first,"
        "                f.file_id, %s, '{}')),"
        "       bool_and(catalog.acl_can_read(a.read_sids, a.deny_sids, %s, '{}', a.deny_first))"
        "  FROM catalog.annex_file af JOIN catalog.file f ON f.file_id = af.file_id"
        "  JOIN catalog.acl a ON a.acl_id = f.acl_id"
        f" WHERE af.annex_id = %s AND af.gone_since IS NULL AND {docs}",
        (sys.argv[3], sys.argv[3], int(sys.argv[1]))).fetchone()
print(f"effective={r[0]} captured={r[1]}")
sys.exit(0 if r[0] else 1)
PY
}

# --- 5. each annex -------------------------------------------------------------------------------------
suite() { # suite NAME ID ROOT SEC USERS: ROOT is the export on the server (digests,
  # CRCs); USERS is the users' mount of it, where the test changes files.
  local name=$1 id=$2 root=$3 sec=$4 w=$5 before after r pid
  # As a user: in the SMB writer's namespace, or here for NFS.
  W() { if [ "$name" = smb ]; then nsenter -t "$WPID" -m "$@"; else "$@"; fi; }
  say "== $name annex $id"
  before=$(tree "$root")
  A1 tree "$root" >"$WORK/$name-atimes-before.json"
  start_unit "$id"
  need "$name: the unit mounts the NAS and reports it" wait_for 60 bash -c \
    "$(declare -f A1 jt); PY=$PY HERE=$HERE; r=\$(A1 events $id); jt \"\$r\" 'd[\"health\"] is not None and d[\"health\"].get(\"ok\")'"
  r=$(A1 events "$id")
  check "$name: 1. the mount is read-only, sec=$sec" jt "$r" "d['health']['read_only'] is True and d['health']['sec'] == '$sec'"
  pid=$(systemctl show -p MainPID --value "$(unit "$id")")
  check "$name: 1. a write in the unit's mount namespace fails (EROFS)" bash -c \
    "! nsenter -t $pid -m touch '$WORK/mnt/$id/mnt/lts/probe' 2>$WORK/log/$name-erofs.txt && grep -qi 'read-only' $WORK/log/$name-erofs.txt"
  check "$name: 2. the baseline copies every file, verified in Azure, CRC equal" wait_for 240 copies_ok "$id" "$root/lts"
  r=$(A1 events "$id")
  check "$name: 2. the baseline is counted, not arrivals" jt "$r" "d['baseline_files'] == 6 and d['events'].get('arrived', 0) == 0 and d['state'] == 'active'"
  # A2.1: a dry run of Archive after 30 days, answered by the unit over its
  # listing of the real NAS. Read-only: the digest below covers it.
  r=$(A1 dry-run "$id" --days 30 --wait 60)
  say "$name: dry run: $(printf '%s' "$r" | cut -c1-300)"
  check "$name: A2.1 a dry run is answered over the listing: 6 files copied, 6 leaving on day 30" \
    jt "$r" "d['state'] == 'done' and d['report']['copied']['files'] == 6 and d['report']['removals']['files'] == 6 and d['report']['removals']['now']['files'] == 0"
  r=$(A1 atimes-moved "$root" "$WORK/$name-atimes-before.json")
  say "$name: atimes moved by the baseline's reads: $r"
  check "$name: 4. the annex's reads move atime on a strictatime NAS (a read, not a write)" \
    jt "$r" "d['moved'] >= 1"
  wait_for 30 quiet "$id"
  after=$(tree "$root")
  check "$name: 4. nothing written to the NAS during the baseline (tree digest)" test "$before" = "$after"

  # Decision 63: who may read a file takes every folder above it into account, up to
  # the export's root. The fixture's files and folders are a+rX: anyone reads them.
  local other=S-1-5-21-63-63-63-4242
  check "$name: 63. the edge has read every folder above the files, the export's root too" \
    wait_for 60 read_set_ready "$id"
  check "$name: 63. so a file anyone may read is readable by anyone" reads_as "$id" docs "$other"
  # A folder that keeps them out, on the server, read at a full scan. Over NFS only:
  # the annex reads as root there (sec=sys, no root squash), so a 0700 folder keeps
  # out the users but not the annex. Over SMB it would keep out the annex's own
  # account too, and its files would look gone. A full scan is asked for until one
  # sees the change past the mount's attribute cache.
  # Decision 64: every mount says whether the server shows its ACLs; these do.
  r=$(A1 events "$id")
  check "$name: 64. the mount report says the server shows its ACLs" \
    jt "$r" "d['health'].get('acls_shown') is True"
  # Decision 65: the share lets in only a group the reader isn't in. No scan needed:
  # it is the filer's answer, through PATCH /v1/filers, by a freshly signed-in admin.
  r=$(A1 answer-access "$id" --groups S-1-5-21-65-65-65-512)
  check "$name: 65. an ArchiveAdmin says the share lets in only one AD group" \
    jt "$r" "d['status'] == 200 and d['access']['limited'] is True"
  check "$name: 65. so a reader outside it can't read what the files let anyone read" \
    bash -c "$(declare -f reads_as); PY=$PY; ! reads_as $id docs $other"
  r=$(A1 answer-access "$id")
  check "$name: 65. answered again: the share limits nothing" \
    jt "$r" "d['status'] == 200 and d['access']['limited'] is False and d['access']['confirmed_at']"
  check "$name: 65. and the files are theirs again" reads_as "$id" docs "$other"
  if [ "$name" = nfs ]; then
    full_until() { # full_until COND...: SIGHUP every 10 s until COND holds, 3 minutes at most
      local i
      for i in $(seq 1 18); do
        nudge "$id" SIGHUP
        sleep 10
        "$@" && return 0
      done
      return 1
    }
    chmod 0700 "$root/lts/docs"
    check "$name: 63. a 0700 folder keeps them out of its files, though the files say 0644" \
      full_until bash -c "$(declare -f reads_as); PY=$PY; ! reads_as $id docs $other"
    check "$name: 63. and only of its own: the other folders' files are still theirs" \
      reads_as "$id" others "$other"
    chmod 0755 "$root/lts/docs"
    check "$name: 63. opened again, the files are theirs again" \
      full_until bash -c "$(declare -f reads_as); PY=$PY; reads_as $id docs $other"
    wait_for 30 quiet "$id"
  fi

  # 3. a change and an arrival
  W "$PY" -c 'import sys; open(sys.argv[1], "ab").write(b"appended on the NAS\n")' "$w/lts/docs/notes.txt"
  W sh -c "echo 'a new file' >'$w/lts/docs/new.txt'"
  # Past the clients' attribute cache (actimeo=1), as any scan after a write.
  sleep 2
  nudge "$id" SIGHUP
  check "$name: 3. the change and the arrival are copied" wait_for 120 bash -c \
    "$(declare -f A1 jt copies_ok); PY=$PY HERE=$HERE; copies_ok $id $root/lts"
  r=$(A1 versions "$id" docs/notes.txt)
  check "$name: 3. the changed file has two committed versions" jt "$r" 'd["committed"] == 2 and d["crcs"][0] != d["crcs"][1]'
  r=$(A1 events "$id")
  check "$name: 3. one arrival and one change on the timeline" jt "$r" "d['events'].get('arrived') == 1 and d['events'].get('changed') == 1"
  wait_for 30 quiet "$id"
  before=$(tree "$root")

  # 5. a delete
  W rm "$w/lts/surveys/2026/raw/line_1043.segy"
  sleep 2
  nudge "$id" SIGUSR1
  check "$name: 5. a NAS delete is a tombstone and the copy is kept" wait_for 60 bash -c \
    "$(declare -f A1 jt); PY=$PY HERE=$HERE; r=\$(A1 gone $id surveys/2026/raw/line_1043.segy); jt \"\$r\" 'd[\"found\"] and d[\"gone\"] and d[\"deleted\"] and d[\"kept_copies\"] == 1 and d[\"tombstones\"] == 1'"
  wait_for 30 quiet "$id"

  # 6. what the quick scans miss: a rewrite in place, and an arrival whose
  # directory mtime is put back. Full scans are off; reconciliation finds both.
  # Directories changed in the last 10 s are read again by every quick scan
  # (their listing may be the client's cache): let them settle first.
  sleep 12
  before=$(tree "$root")
  local d=$w/lts/surveys/2026/raw m
  m=$(W stat -c %y "$d")
  W "$PY" -c 'import sys; f=open(sys.argv[1], "r+b"); f.seek(4000); f.write(b"\x7f" * 64)' "$d/line_1042.segy"
  W sh -c "echo 'slipped in' >'$d/hidden.txt'"
  W touch -d "$m" "$d"
  nudge "$id" SIGUSR1
  sleep 8
  r=$(A1 versions "$id" surveys/2026/raw/line_1042.segy)
  check "$name: 6. the quick scans missed both" jt "$r" 'd["committed"] == 1'
  nudge "$id" SIGUSR2
  check "$name: 6. reconciliation records the missed change and arrival" wait_for 60 bash -c \
    "$(declare -f A1 jt); PY=$PY HERE=$HERE; r=\$(A1 drift $id); jt \"\$r\" 'sorted(x[\"kind\"] for x in d[\"drift\"]) == [\"changed_no_event\", \"missed_arrival\"]'"
  check "$name: 6. then both are copied, verified" wait_for 120 bash -c \
    "$(declare -f A1 jt copies_ok); PY=$PY HERE=$HERE; copies_ok $id $root/lts"
  copies_why "$id" "$root/lts" "$name: 6."
  r=$(A1 drift "$id")
  check "$name: 6. the drift rows say what fixed them" jt "$r" 'all(x["resolved_by"] for x in d["drift"])'
  wait_for 30 quiet "$id"

  # 7. SIGKILL mid-copy of a large file
  W "$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); from annex_a1_files import big; big(sys.argv[2], int(sys.argv[3]))' "$HERE" "$w/lts/big/line_big.segy" "$BIG_MIB"
  before=$(tree "$root")
  sleep 2
  nudge "$id" SIGHUP
  if wait_for 120 bash -c "$(declare -f status jt); ANNEX=$ANNEX WORK=$WORK PY=$PY; s=\$(status $id); jt \"\$s\" 'd[\"copy_jobs\"]'"; then
    systemctl kill -s SIGKILL "$(unit "$id")"
    pass "$name: 7. killed mid-copy (copy journalled)"
  else
    fail "$name: 7. the large copy never started"
  fi
  check "$name: 7. the unit restarts and completes the copy" wait_for 300 bash -c \
    "$(declare -f A1 jt copies_ok); PY=$PY HERE=$HERE; copies_ok $id $root/lts"
  copies_why "$id" "$root/lts" "$name: 7."
  r=$(A1 versions "$id" big/line_big.segy)
  check "$name: 7. one committed version, no duplicate commit" jt "$r" 'd["committed"] == 1'
  check "$name: 7. restarted by systemd" test "$(systemctl show -p NRestarts --value "$(unit "$id")")" -ge 1
  wait_for 30 quiet "$id"
  after=$(tree "$root")
  check "$name: 4. nothing written to the NAS while copying and after the kill (tree digest)" test "$before" = "$after"
  systemctl stop "$(unit "$id")"
}

# --- 6. removals (phase A2.2) --------------------------------------------------------------------
rwmounted() { # the read-write mount, in the unit's mount namespace
  local pid; pid=$(systemctl show -p MainPID --value "$(unit "$1")")
  [ "${pid:-0}" -gt 0 ] && nsenter -t "$pid" -m mountpoint -q "$WORK/mnt/$1/rw"
}
norw() { ! rwmounted "$1"; }
rstate() { # rstate ID PATH STATE...: PATH's latest removal is in one of STATES
  local r id=$1 p=$2; shift 2; r=$(A1 removals "$id")
  "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]).get(sys.argv[2], {}); sys.exit(0 if d.get("state") in sys.argv[3:] else 1)' "$r" "$p" "$@"
}
rget() { A1 removals "$1" | "$PY" -c 'import json,sys; d=json.load(sys.stdin).get(sys.argv[1], {}); print(eval(sys.argv[2]))' "$2" "$3"; }
reason_is() { # reason_is ID PATH REASON...: the latest removal's reason is one of them
  local got id=$1 p=$2; shift 2; got=$(rget "$id" "$p" 'd.get("reason")')
  local x; for x in "$@"; do [ "$got" = "$x" ] && return 0; done; return 1
}
removed_once() { # removed_once ID PATH ROOT: done, one removal, one W2, its pointer, the file gone
  rstate "$1" "$2" done &&
    test "$(rget "$1" "$2" 'd["removals"] == 1 and d.get("events", {}).get("removed") == 1')" = True &&
    test -e "$3/lts/$2.omnifile.txt" && ! test -e "$3/lts/$2"
}
note_kept() { rstate "$1" rm/b.bin done && test "$(cat "$2")" = 'my own note'; }
restored() { # restored ID PATH ROOT HIDDEN: cancelled, back under its name
  rstate "$1" "$2" cancelled && test -e "$3/lts/$2" && ! test -e "$3/lts/$(dirname "$2")/$4"
}
waits_hidden() { test -e "$3/lts/rm/$4" && rstate "$1" "$2" hidden; }
removals_suite() { # removals_suite NAME ID ROOT USERS
  local name=$1 id=$2 root=$3 w=$4 h pt r want kept=() pointers=()
  W() { if [ "$name" = smb ]; then nsenter -t "$WPID" -m "$@"; else "$@"; fi; }
  drop() { W sh -c "printf '%s\n' '$2' >'$w/lts/rm/$1'"; } # a user drops a file under rm/
  say "== $name annex $id: removals (A2.2)"
  A1 snapshot "$root" "$WORK/$name-before-removals.json" >/dev/null
  start_unit "$id"
  wait_for 60 quiet "$id"
  check "$name: R0. no read-write mount while removals aren't live (Copy rules)" norw "$id"
  A1 go-live "$id" >/dev/null
  check "$name: R1. the read-write mount once removals are live" wait_for 30 rwmounted "$id"

  drop a.bin "moved"
  check "$name: R2. a removal hides, waits its grace, deletes, leaves its pointer" wait_for 240 removed_once "$id" rm/a.bin "$root"
  check "$name: R2. its copy was read back in Azure first" test "$(rget "$id" rm/a.bin 'd["read_in_azure"]')" = True
  check "$name: R2. the pointer's text" grep -q 'moved to Azure by OmniFile' "$root/lts/rm/a.bin.omnifile.txt"
  pointers+=(rm/a.bin.omnifile.txt)

  W sh -c "echo 'my own note' >'$w/lts/rm/b.bin.omnifile.txt'"
  kept+=(rm/b.bin.omnifile.txt)
  drop b.bin "pointer name taken"
  check "$name: R3. W4 never overwrites: removed, the note there is the user's" wait_for 240 note_kept "$id" "$root/lts/rm/b.bin.omnifile.txt"

  drop c.bin "changed in its grace"
  wait_for 240 rstate "$id" rm/c.bin hidden
  h=$(rget "$id" rm/c.bin 'd["hidden_name"]')
  W sh -c "echo 'more' >>'$w/lts/rm/$h'"
  check "$name: R4. a change in the grace cancels it, and W3 brings it back" wait_for 120 restored "$id" rm/c.bin "$root" "$h"
  check "$name: R4. ... as changed in its grace" reason_is "$id" rm/c.bin changed_in_grace
  # Changed, it is copied again, and Move now would move it after that copy:
  # the user takes it out of rm/ to keep it.
  W mv "$w/lts/rm/c.bin" "$w/lts/docs/c.bin"
  kept+=(docs/c.bin)

  # A same-size rewrite with its mtime put back (cp -p, touch -d): the ctime
  # W1 left (and over SMB the bytes, read again) still tell.
  drop s.bin "same size, then"
  wait_for 240 rstate "$id" rm/s.bin hidden
  h=$(rget "$id" rm/s.bin 'd["hidden_name"]')
  m=$(W stat -c %y "$w/lts/rm/$h")
  W sh -c "printf '%s\n' 'SAME SIZE, THEN' >'$w/lts/rm/$h' && touch -d '$m' '$w/lts/rm/$h'"
  check "$name: R4b. a same-size rewrite with its mtime put back cancels it, put back" wait_for 120 restored "$id" rm/s.bin "$root" "$h"
  check "$name: R4b. ... as changed in its grace" reason_is "$id" rm/s.bin changed_in_grace crc_mismatch_in_grace
  W mv "$w/lts/rm/s.bin" "$w/lts/docs/s.bin" # kept (see R4)
  kept+=(docs/s.bin)

  drop d.bin "renamed back"
  wait_for 240 rstate "$id" rm/d.bin hidden
  h=$(rget "$id" rm/d.bin 'd["hidden_name"]')
  W mv "$w/lts/rm/$h" "$w/lts/rm/d.bin"
  check "$name: R5. a rename back in the grace cancels it, the file kept" wait_for 120 restored "$id" rm/d.bin "$root" "$h"
  check "$name: R5. ... as renamed back" reason_is "$id" rm/d.bin renamed_back
  kept+=(rm/d.bin)

  drop e.bin "hard linked"
  # Over NFS through the protocol (the server's attribute cache would hide a
  # link made behind its back); Samba reports a server-side link at once.
  if [ "$name" = smb ]; then ln "$root/lts/rm/e.bin" "$root/lts/docs/e-link.bin"
  else W ln "$w/lts/rm/e.bin" "$w/lts/docs/e-link.bin"; fi
  check "$name: R6. a hard-linked file is never removed" wait_for 240 rstate "$id" rm/e.bin cancelled
  check "$name: R6. ... both names kept" bash -c "test -e '$root/lts/rm/e.bin' && test -e '$root/lts/docs/e-link.bin'"
  check "$name: R6. ... as hard linked (before W1, or in its grace)" reason_is "$id" rm/e.bin hard_linked hard_linked_in_grace
  kept+=(rm/e.bin docs/e-link.bin)

  if [ "$name" = smb ]; then
    W sh -c "printf 'with a stream\n' >/tmp/ads.txt && printf 'the stream\n' >/tmp/stream.txt && smbclient //${SMB_NAME,,}.omni.test/annex -p $SMB_PORT -I $SMB_IP --use-kerberos=required -c 'put /tmp/ads.txt lts/rm/f.bin; put /tmp/stream.txt lts/rm/f.bin:extra' >$WORK/log/smbclient.txt 2>&1"
    check "$name: R7. a file with an alternate data stream is never removed" wait_for 240 rstate "$id" rm/f.bin cancelled
    check "$name: R7. ... as having a stream" reason_is "$id" rm/f.bin alternate_data_stream
    kept+=(rm/f.bin)
  fi

  drop g.bin "paused"
  wait_for 240 rstate "$id" rm/g.bin hidden
  h=$(rget "$id" rm/g.bin 'd["hidden_name"]')
  A1 pause "$id" on >/dev/null
  check "$name: R8. paused: the read-write mount goes" wait_for 30 norw "$id"
  sleep 70
  check "$name: R8. paused, past its grace: the hidden file waits, nothing deleted" waits_hidden "$id" rm/g.bin "$root" "$h"
  A1 pause "$id" off >/dev/null
  check "$name: R8. resumed: mounted again, and removed after a new grace" wait_for 240 removed_once "$id" rm/g.bin "$root"
  pointers+=(rm/g.bin.omnifile.txt)

  start_unit "$id" OMNIFILE_ANNEX_HOLD_BEFORE_W1=10
  drop h.bin "changed before its hide"
  wait_for 240 rstate "$id" rm/h.bin ticketed
  W sh -c "echo 'more' >>'$w/lts/rm/h.bin'"
  check "$name: R9. a change between the ticket and W1 cancels it before any write" wait_for 60 rstate "$id" rm/h.bin cancelled
  check "$name: R9. ... as changed" reason_is "$id" rm/h.bin changed
  W mv "$w/lts/rm/h.bin" "$w/lts/docs/h.bin" # kept (see R4)
  kept+=(docs/h.bin)

  # The unit killed before and after each write: it resumes from its journal.
  for pt in before_w1 after_w1 before_w2 after_w2 before_w4 after_w4; do
    start_unit "$id" OMNIFILE_ANNEX_CRASH_AT=$pt
    drop "k-$pt.bin" "killed $pt"
    check "$name: R10. killed $pt: resumed, removed once, pointer once" wait_for 240 removed_once "$id" "rm/k-$pt.bin" "$root"
    check "$name: R10. killed $pt: it did crash there, and systemd restarted it" bash -c \
      "test -e '$WORK/annex/$id/crashed-$pt' && test \"\$(systemctl show -p NRestarts --value $(unit "$id"))\" -ge 1"
    pointers+=("rm/k-$pt.bin.omnifile.txt")
  done
  for pt in before_w3 after_w3; do
    start_unit "$id" OMNIFILE_ANNEX_CRASH_AT=$pt
    drop "c-$pt.bin" "cancelled, killed $pt"
    wait_for 240 rstate "$id" "rm/c-$pt.bin" hidden
    h=$(rget "$id" "rm/c-$pt.bin" 'd["hidden_name"]')
    W sh -c "echo 'more' >>'$w/lts/rm/$h'"
    check "$name: R10. killed $pt: the cancel resumes, the file back once" wait_for 240 restored "$id" "rm/c-$pt.bin" "$root" "$h"
    check "$name: R10. killed $pt: it did crash there" test -e "$WORK/annex/$id/crashed-$pt"
    W mv "$w/lts/rm/c-$pt.bin" "$w/lts/docs/c-$pt.bin" # kept (see R4)
    kept+=("docs/c-$pt.bin")
  done
  start_unit "$id"
  wait_for 60 quiet "$id"

  # R11: the export changed exactly as expected, and nothing else did.
  A1 snapshot "$root" "$WORK/$name-after-removals.json" >/dev/null
  r=$(A1 diff "$WORK/$name-before-removals.json" "$WORK/$name-after-removals.json")
  want=$("$PY" -c 'import json,sys; print(json.dumps(sorted("lts/" + p for p in sys.argv[1:])))' "${kept[@]}" "${pointers[@]}")
  say "$name: changes on the NAS: $r"
  check "$name: R11. the NAS shows exactly the expected changes, nothing else" \
    jt "$r" "d['removed'] == [] and d['changed'] == [] and d['added'] == $want"
  r=$(A1 removals "$id")
  check "$name: R12. no file removed twice, every W2 audited" "$PY" - "$r" "$id" <<'PY'
import json, os, sys, psycopg
d = json.loads(sys.argv[1])
assert all(v.get("events", {}).get("removed", 0) <= 1 for v in d.values()), d
with psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"]) as c:
    n = c.execute("SELECT count(*) FROM catalog.audit_log l JOIN catalog.annex_removal r"
                  " ON r.file_id = l.file_id WHERE l.action = 'annex.removal.deleted'"
                  " AND r.annex_id = %s AND r.state = 'done'", (int(sys.argv[2]),)).fetchone()[0]
done = sum(1 for v in d.values() if v.get("state") == "done")
assert n >= done > 0, (n, done)
PY
  systemctl stop "$(unit "$id")"
}

# --- 7. placements (phase A3.3) ---------------------------------------------------------------
plmounted() { # the placement mount, in the unit's mount namespace
  local pid; pid=$(systemctl show -p MainPID --value "$(unit "$1")")
  [ "${pid:-0}" -gt 0 ] && nsenter -t "$pid" -m mountpoint -q "$WORK/mnt/$1/pl"
}
nopl() { ! plmounted "$1"; }
pget() { A1 placements "$1" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); p=d["by_path"].get(sys.argv[1], {}); print(eval(sys.argv[2]))' "$2" "$3"; }
pstate() { test "$(pget "$1" "$2" 'p.get("state")')" = "$3"; }
rmeta() { # rmeta ID KEY: a value from the annex's journal
  "$PY" -c 'import sqlite3,sys; r=sqlite3.connect(sys.argv[1]).execute("SELECT value FROM meta WHERE key = ?", (sys.argv[2],)).fetchone(); print(r[0] if r else "")' "$WORK/annex/$1/annex.db" "$2"
}
stg_rel() { # the annex's staging folder, as its journal has it (the name has a random token)
  "$PY" - "$WORK/annex/$1/annex.db" <<'PY'
import sqlite3, sys
r = sqlite3.connect(sys.argv[1]).execute("SELECT value FROM meta WHERE key = 'staging_rel'").fetchone()
print(r[0] if r else "")
PY
}
folder_acls() { # folder_acls ID [PATH]: the edge has read the folders' ACLs, the return location's too
  "$PY" - "$1" "${2:-elsewhere/restored/rm}" <<'PY'
import os, sys, psycopg
with psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"]) as c:
    n = c.execute("SELECT count(*) FROM catalog.nas_folder_acl n JOIN catalog.annex a"
                  " ON a.filer_id = n.filer_id WHERE a.annex_id = %s"
                  " AND n.path = %s AND n.acl_id IS NOT NULL",
                  (int(sys.argv[1]), sys.argv[2])).fetchone()[0]
sys.exit(0 if n >= 1 else 1)
PY
}
reported() { # the filer's edge has told the service its placement account
  "$PY" - "$1" <<'PY'
import os, sys, psycopg
with psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"]) as c:
    r = c.execute("SELECT f.placement_account FROM catalog.filer f JOIN catalog.annex a"
                  " ON a.filer_id = f.filer_id WHERE a.annex_id = %s", (int(sys.argv[1]),)).fetchone()
sys.exit(0 if r and r[0] and r[0].get("sid") else 1)
PY
}
register_placer() { # register_placer ID: the setup step, the real report and the real route
  local id=$1 r
  check "smb: the edge reports the account it holds, from winbind" wait_for 60 reported "$id"
  r=$(A1 filer-report "$id")
  say "smb: the edge reports: $r"
  check "smb: ... the restore account, as a user" jt "$r" "d['sid'] == '$RESTORE_SID' and d['kind'] == 'user' and d['name'] == '$RESTORE_USER@OMNI.TEST' and d['can_register']"
  r=$(A1 register-placer "$id" "$SMB_SID")
  check "smb: a SID the edge doesn't report is refused, and says so" jt "$r" "d['status'] == 400 and 'different_sid' in d['detail']"
  r=$(A1 register-placer "$id" "$RESTORE_SID")
  say "smb: registered: $(printf '%s' "$r" | cut -c1-200)"
  check "smb: an ArchiveAdmin registers what the edge reports, through PATCH /v1/filers" jt "$r" "d['status'] == 200 and d['placement_sid'] == '$RESTORE_SID'"
}
placements_suite() { # placements_suite NAME ID ROOT USERS
  local name=$1 id=$2 root=$3 w=$4 r pt f want
  W() { if [ "$name" = smb ]; then nsenter -t "$WPID" -m "$@"; else "$@"; fi; }
  # A folder's mode, as the NAS's clients see it: over NFS through the users'
  # mount (Ganesha caches what it serves, so a change made under it isn't
  # seen for a while); over SMB on the server (cifs ignores a chmod).
  setmode() { if [ "$name" = nfs ]; then chmod "$@" "$w/$_rel"; else chmod "$@" "$root/$_rel"; fi; }
  say "== $name annex $id: placements (A3.3)"
  # The setup guide's grant to the restore group: create in the export's root,
  # for the staging folder (here, the root open to all: a lab NAS).
  chmod 0777 "$root"
  # The restore folder, which users (and so a requester) may add files to.
  # (Over SMB on the server: the writer can't add to a root-owned `elsewhere`.)
  if [ "$name" = nfs ]; then W mkdir -p "$w/elsewhere/restored/rm"; else mkdir -p "$root/elsewhere/restored/rm"; fi
  _rel=elsewhere setmode -R 0777
  A1 returns "$id" >/dev/null
  start_unit "$id"
  wait_for 60 quiet "$id"
  if [ "$name" = smb ]; then register_placer "$id"; fi
  check "$name: P0. no placement mount while none is in hand" nopl "$id"
  nudge "$id" SIGHUP
  check "$name: P0. a full scan reads the folders' ACLs, the return location's too" wait_for 60 folder_acls "$id"
  A1 snapshot "$root" "$WORK/$name-before-placements.json" >/dev/null
  # P6 below takes the pointer on what W4 recorded, not on its text.
  check "$name: P0. W4 recorded the pointer's inode and hash" "$PY" - "$WORK/annex/$id/annex.db" <<'PY'
import sqlite3, sys
r = sqlite3.connect(sys.argv[1]).execute(
    "SELECT pointer_ino, pointer_sha FROM removal WHERE path = 'rm/a.bin'"
    " AND pointer_ino IS NOT NULL").fetchone()
sys.exit(0 if r and r[1] else 1)
PY
  local tcpd=
  if [ "$name" = smb ]; then # R3: what the placer's links ask the server
    command -v tcpdump >/dev/null || { echo "needs tcpdump" >&2; finish; }
    tcpdump -i lo -s 0 -U -w - "tcp port $SMB_PORT" >"$WORK/smb-links.pcap" 2>"$WORK/log/tcpdump.log" &
    tcpd=$!
    PIDS+=("$tcpd")
    sleep 2
  fi

  A1 place "$id" rm/a.bin >/dev/null
  check "$name: P1. a removed file is placed back where it came from" wait_for 180 pstate "$id" rm/a.bin placed
  check "$name: P1. ... its own bytes, under its own name" bash -c "test \"\$(cat '$root/lts/rm/a.bin')\" = moved"
  check "$name: P1. ... its pointer gone (the one W4 recorded)" test ! -e "$root/lts/rm/a.bin.omnifile.txt"
  check "$name: P1. ... returned, and held" test "$(pget "$id" rm/a.bin 'p.get("held")')" = True
  if [ "$name" = nfs ]; then
    check "$name: P1. ... its own mode back" test "$(stat -c %a "$root/lts/rm/a.bin")" = 644
  fi
  sleep 70
  check "$name: P2. held: a Move-now folder doesn't send it out again" bash -c \
    "test -e '$root/lts/rm/a.bin' && test ! -e '$root/lts/rm/a.bin.omnifile.txt'"

  W sh -c "echo mine >'$w/elsewhere/restored/rm/g.bin'"
  A1 place "$id" rm/g.bin >/dev/null
  check "$name: P3. into a restore folder, its name taken: a -restored name, nothing replaced" \
    wait_for 180 pstate "$id" rm/g.bin placed
  check "$name: P3. ... the user's file untouched, the recalled one beside it" bash -c \
    "test \"\$(cat '$root/elsewhere/restored/rm/g.bin')\" = mine && test \"\$(cat '$root/elsewhere/restored/rm/g-restored.bin')\" = paused"
  if [ "$name" = smb ]; then
    # a.bin was the writer's, as g.bin is: its owner set back over SMB.
    check "$name: P1. ... its owner back (the writer's, by the restore privilege)" \
      test "$(stat -c %u "$root/lts/rm/a.bin" 2>/dev/null || echo missing-a)" = "$(stat -c %u "$root/elsewhere/restored/rm/g.bin" 2>/dev/null || echo missing-g)"
    check "$name: P1. ... and the placement account holds nothing on it" bash -c \
      "test -e '$root/lts/rm/a.bin' && ! getfacl -n '$root/lts/rm/a.bin' 2>/dev/null | grep -q '^user:$RESTORE_UID:'"
  fi

  # A new subfolder of the restore folder: its parent's ACL, so the requester
  # may add a file to it, and `go` says so from the folder as it is then.
  local want_docs; want_docs=$(cat "$root/lts/docs/h.bin")
  A1 place "$id" docs/h.bin >/dev/null
  check "$name: P3b. into a folder made for it, which lets in whoever its parent does" \
    wait_for 180 pstate "$id" docs/h.bin placed
  check "$name: P3b. ... its bytes" bash -c "test \"\$(cat '$root/elsewhere/restored/docs/h.bin')\" = '$want_docs'"

  local body
  for pt in created secured linked; do
    case $pt in created) f=k-before_w1.bin ;; secured) f=k-after_w1.bin ;; linked) f=k-before_w2.bin ;; esac
    body="killed ${f#k-}"; body=${body%.bin}
    start_unit "$id" OMNIFILE_ANNEX_CRASH_AT=place_$pt
    A1 place "$id" "rm/$f" >/dev/null
    check "$name: P4. killed at $pt: resumed, placed once, whole" wait_for 180 bash -c \
      "$(declare -f A1 pget pstate); PY=$PY HERE=$HERE; pstate $id rm/$f placed && test \"\$(cat '$root/elsewhere/restored/rm/$f')\" = '$body' && test ! -e '$root/elsewhere/restored/rm/${f%.bin}-restored.bin'"
    check "$name: P4. killed at $pt: it did crash there" test -e "$WORK/annex/$id/crashed-place_$pt"
  done
  start_unit "$id"
  if [ -n "$tcpd" ]; then
    kill "$tcpd"
    wait "$tcpd" 2>/dev/null
    r=$(A1 smb-links "$WORK/smb-links.pcap")
    say "$name: link requests on the wire: $r"
    check "$name: R3. the placer's links reach the server, each with ReplaceIfExists = 0" \
      jt "$r" "any(l['name'].endswith('a.bin') for l in d['links']) and all(l['replace_if_exists'] == 0 for l in d['links'])"
    check "$name: P5. its opens to set the owner ask for backup intent (the daemon's own uid)" \
      jt "$r" "d['owner_opens'] and all(o['backup_intent'] for o in d['owner_opens'])"
  fi

  _rel=elsewhere/restored/rm setmode 0555
  nudge "$id" SIGHUP
  sleep 15
  A1 place "$id" rm/k-after_w2.bin >/dev/null
  check "$name: P5. a folder the requester can't add files to: refused, nothing written" \
    wait_for 120 pstate "$id" rm/k-after_w2.bin failed
  check "$name: P5. ... as access denied" test "$(pget "$id" rm/k-after_w2.bin 'p.get("reason")')" = access_denied
  _rel=elsewhere/restored/rm setmode 0777

  # A full NAS: less room than the file needs, on the server's own volume.
  local free; free=$(df --output=avail -B1M "$root" | tail -1 | tr -d ' ')
  fallocate -l "$((free - BIG_MIB / 2))M" "$root/.a1-filler"
  A1 place "$id" big/line_big.segy >/dev/null
  check "$name: P5b. a full NAS: it fails cleanly" wait_for 180 pstate "$id" big/line_big.segy failed
  check "$name: P5b. ... as no space" test "$(pget "$id" big/line_big.segy 'p.get("reason")')" = no_space
  check "$name: P5b. ... nothing under the name, its new folder gone" test ! -e "$root/elsewhere/restored/big"
  rm -f "$root/.a1-filler"

  check "$name: P6. no placement mount once none is in hand" wait_for 60 nopl "$id"
  local stg; stg=$(stg_rel "$id")
  check "$name: P6. the staging folder has a name with its token" bash -c "[[ '$stg' =~ ^\.omnifile-staging-$id-[0-9a-f]+\$ ]]"
  check "$name: P6. the staging folder is empty" bash -c "test -d '$root/$stg' && test -z \"\$(ls -A '$root/$stg')\""
  if [ "$name" = nfs ]; then
    check "$name: P6. ... and locked down (0700)" test "$(stat -c %a "$root/$stg")" = 700
  fi
  A1 snapshot "$root" "$WORK/$name-after-placements.json" >/dev/null
  r=$(A1 diff "$WORK/$name-before-placements.json" "$WORK/$name-after-placements.json")
  want='["elsewhere/restored/docs/h.bin", "elsewhere/restored/rm/g-restored.bin", "elsewhere/restored/rm/g.bin", "elsewhere/restored/rm/k-after_w1.bin", "elsewhere/restored/rm/k-before_w1.bin", "elsewhere/restored/rm/k-before_w2.bin", "lts/rm/a.bin"]'
  say "$name: changes on the NAS: $r"
  check "$name: P7. the NAS shows exactly the expected changes, nothing else" \
    jt "$r" "d['added'] == $want and d['removed'] == ['lts/rm/a.bin.omnifile.txt'] and d['changed'] == []"
  systemctl stop "$(unit "$id")"
}

# An annex at the export's root: the staging folder sits inside what it scans,
# and every other annex here is at `lts`. A staged file held there through a
# full scan and a reconciliation never reaches the catalog (F2 lived here).
export_root_suite() { # export_root_suite ID ROOT
  local id=$1 root=$2 r stg
  say "== export-root annex $id"
  A1 returns "$id" >/dev/null
  start_unit "$id" OMNIFILE_ANNEX_HOLD_AT=place_secured
  check "root: 1. the baseline copies the export's files" wait_for 120 copies_ok "$id" "$root"
  nudge "$id" SIGHUP
  check "root: 1. a full scan reads the return location's ACL" wait_for 60 folder_acls "$id" elsewhere/restored
  A1 place "$id" docs/x.bin >/dev/null
  staged() { stg=$(stg_rel "$1"); [ -n "$stg" ] && [ -n "$(ls -A "$root/$stg" 2>/dev/null)" ]; }
  jstate() { "$PY" -c 'import sqlite3,sys; r=sqlite3.connect(sys.argv[1]).execute("SELECT state FROM placement LIMIT 1").fetchone(); print(r[0] if r else "")' "$WORK/annex/$1/annex.db"; }
  isheld() { test "$(jstate "$1")" = secured && staged "$1"; }
  check "root: 2. a whole staged file is held in the staging folder, inside the annex" wait_for 120 isheld "$id"
  local before; before=$(rmeta "$id" last_reconcile)
  nudge "$id" SIGHUP
  sleep 8
  nudge "$id" SIGUSR2
  ran() { test "$(rmeta "$1" last_reconcile)" != "$2"; }
  check "root: 3. the reconciliation ran" wait_for 60 ran "$id" "$before"
  wait_for 60 quiet "$id"
  check "root: 3. ... and is still there after the full scan and the reconciliation" isheld "$id"
  r=$(A1 staging-rows "$id")
  say "root: staging rows: $r"
  check "root: 3. nothing of the staging folder reached the journal or the catalog" jt "$r" 'd["files"] == 0 and d["events"] == 0'
  check "root: 3. ... nor the scan's own table" "$PY" - "$WORK/annex/$id/annex.db" <<'PY'
import sqlite3, sys
n = sqlite3.connect(sys.argv[1]).execute("SELECT count(*) FROM entry WHERE path LIKE '.omnifile-staging%'").fetchone()[0]
sys.exit(0 if n == 0 else 1)
PY
  start_unit "$id"
  check "root: 4. resumed: placed" wait_for 240 pstate "$id" docs/x.bin placed
  check "root: 4. ... its bytes, and the staging folder empty" bash -c \
    "test \"\$(cat '$root/elsewhere/restored/docs/x.bin')\" = 'at the export root' && test -z \"\$(ls -A '$root/$(stg_rel "$id")')\""
  systemctl stop "$(unit "$id")"
}

# A placement over NFSv4, on the one server the lab can serve it with: the ACLs
# are read and set as NFSv4 ACLs, the lock is read back as one (F9).
v4_suite() { # v4_suite ID ROOT
  local id=$1 root=$2 r pid stg
  say "== NFSv4 annex $id"
  A1 returns "$id" >/dev/null
  start_unit "$id"
  check "v4: 1. the baseline copies the files" wait_for 300 copies_ok "$id" "$root/lts"
  pid=$(systemctl show -p MainPID --value "$(unit "$id")")
  check "v4: 1. the read-only mount is NFSv4.2" bash -c "nsenter -t $pid -m grep ' $WORK/mnt/$id/mnt ' /proc/self/mounts | grep -q 'nfs4 ro,.*vers=4.2'"
  r=$("$PY" - "$id" <<'PY'
import json, os, sys, psycopg
with psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"]) as c:
    rows = c.execute(
        "SELECT a.raw_kind, cardinality(a.read_sids) FROM catalog.annex_file af"
        " JOIN catalog.file f USING (file_id) JOIN catalog.acl a ON a.acl_id = f.acl_id"
        " WHERE af.annex_id = %s", (int(sys.argv[1]),)).fetchall()
print(json.dumps([list(x) for x in rows]))
PY
)
  say "v4: the catalog's ACLs: $r"
  check "v4: 1. the catalog holds an NFSv4 ACL for each, with a read set" jt "$r" 'd and all(k == "nfs4" and n > 0 for k, n in d)'
  nudge "$id" SIGHUP
  check "v4: 1. a full scan reads the return location's ACL" wait_for 60 folder_acls "$id" elsewhere/restored
  A1 snapshot "$root" "$WORK/v4-before.json" >/dev/null
  A1 place "$id" docs/x.bin >/dev/null
  check "v4: 2. placed" wait_for 180 pstate "$id" docs/x.bin placed
  check "v4: 2. ... its bytes" bash -c "test \"\$(cat '$root/elsewhere/restored/docs/x.bin')\" = 'over NFSv4'"
  check "v4: 2. ... its mode" test "$(stat -c %a "$root/elsewhere/restored/docs/x.bin")" = 644
  check "v4: 2. ... its owner and group back" test "$(stat -c %u:%g "$root/elsewhere/restored/docs/x.bin")" = 20002:20001
  start_unit "$id" OMNIFILE_ANNEX_CRASH_AT=place_secured
  A1 place "$id" docs/y.bin >/dev/null
  check "v4: 3. killed at secured: resumed, placed once, whole" wait_for 180 bash -c \
    "$(declare -f A1 pget pstate); PY=$PY HERE=$HERE; pstate $id docs/y.bin placed && test \"\$(cat '$root/elsewhere/restored/docs/y.bin')\" = 'killed at secured'"
  check "v4: 3. it did crash there" test -e "$WORK/annex/$id/crashed-place_secured"
  start_unit "$id"
  # OWNER@ with no SID holds WRITE_ACL: anyone could be that. Refused away from
  # its origin, with nothing left behind (decision 51).
  A1 place "$id" docs/z.bin >/dev/null
  check "v4: 3b. a file whose owner has no SID isn't placed elsewhere" wait_for 120 pstate "$id" docs/z.bin failed
  check "v4: 3b. ... as a mismatch" test "$(pget "$id" docs/z.bin 'p.get("reason")')" = acl_mismatch
  check "v4: 4. no placement mount once none is in hand" wait_for 60 nopl "$id"
  stg=$(stg_rel "$id")
  check "v4: 4. the staging folder is empty, and its mode 0700 on the server" bash -c \
    "test -z \"\$(ls -A '$root/$stg')\" && test \"\$(stat -c %a '$root/$stg')\" = 700"
  A1 snapshot "$root" "$WORK/v4-after.json" >/dev/null
  r=$(A1 diff "$WORK/v4-before.json" "$WORK/v4-after.json")
  say "v4: changes on the NAS: $r"
  check "v4: 4. the NAS shows exactly the expected changes" jt "$r" \
    "d['added'] == ['elsewhere/restored/docs/x.bin', 'elsewhere/restored/docs/y.bin'] and d['removed'] == [] and d['changed'] == []"
  systemctl stop "$(unit "$id")"
}

# A NAS that stops answering: NFS mounts are `hard`, so a call into it blocks in
# the kernel, and only the watchdog gets the unit out. The rule goes in this
# test's own namespace, and comes out again.
block_nfs() { ip netns exec "$NS" iptables -I INPUT -p tcp --dport 2049 -j DROP; }
unblock_nfs() { ip netns exec "$NS" iptables -D INPUT -p tcp --dport 2049 -j DROP; }
# The other way a NAS goes quiet: up, connected, and answering nothing.
freeze_nas() { kill -STOP "$(cat "$WORK/ganesha.pid")"; }
thaw_nas() { kill -CONT "$(cat "$WORK/ganesha.pid")"; }
upid() { systemctl show -p MainPID --value "$(unit "$1")"; }
restarted() { local p; p=$(upid "$1"); [ "${p:-0}" -gt 0 ] && [ "$p" != "$2" ]; }
mount_says() { # mount_says ID TEXT AFTER: a `mount` report since event AFTER has this error
  jt "$(A1 mount-reports "$1" --after "$3")" "any('$2' in (e or '') for e in d)"
}
unit_shows() { # what the unit looks like under systemd, and what its processes are doing
  local u p; u=$(unit "$1")
  say "$2: $(systemctl show -p ActiveState -p SubState -p MainPID -p Result "$u" | tr '\n' ' ')"
  for p in $(cat "/sys/fs/cgroup/system.slice/$u.service/cgroup.procs" 2>/dev/null); do
    say "$2: pid $p: $(ps -L -o stat=,wchan:28= -p "$p" 2>/dev/null | sort | uniq -c | tr -s ' ' | tr '\n' ';')"
  done
}
mount_up() { # mount_up ID AFTER: a good `mount` report since event AFTER, and the signals caught
  jt "$(A1 mount-reports "$1" --after "$2")" 'any(e is None for e in d)' && handles "$1" SIGHUP
}
started_up() { # started_up ID [VAR=VALUE...]: start it, and wait until it has mounted
  local id=$1 ev; shift
  ev=$(A1 last-event "$id")
  start_unit "$id" "$@"
  wait_for 240 mount_up "$id" "$ev"
}
jrow() { # jrow ID PLACEMENT: its state and tries in the edge's journal
  "$PY" - "$WORK/annex/$1/annex.db" "$2" <<'PY'
import sqlite3, sys
r = sqlite3.connect(sys.argv[1]).execute(
    "SELECT state, attempts FROM placement WHERE placement_id = ?", (int(sys.argv[2]),)).fetchone()
print(" ".join(map(str, r)) if r else "")
PY
}
jtried() { local r; r=$(jrow "$1" "$2"); [ "${r#* }" != "" ] && [ "${r#* }" != "$r" ] && [ "${r#* }" -ge 1 ]; }
jfailed() { local r; r=$(jrow "$1" "$2"); [ "${r%% *}" = failed ]; }
jat() { local r; r=$(jrow "$1" "$2"); [ "${r%% *}" = "$3" ]; }
hung_problem() { jt "$(A1 mount-reports "$1" --after "$2" --problems)" 'len(d) > 0'; }
hang_copied() { # the stage's own files: each copied, CRC equal, verified in Azure
  local r; r=$(A1 copies "$1" "$2" --only docs/hang-)
  jt "$r" 'd["files"] == 3 and d["matched"] == d["files"] == d["verified"] and not d["extra"]'
}
hang_suite() { # hang_suite ID ROOT USERS
  local id=$1 root=$2 w=$3 pid a b c since gone ev
  say "== nfs annex $id: a NAS that stops answering"
  chmod 0777 "$root"
  mkdir -p "$w/elsewhere/restored"
  chmod -R 0777 "$w/elsewhere" 2>/dev/null
  [[ " ${E2E_ONLY:-nfs} " == *" nfs "* ]] || A1 returns "$id" >/dev/null
  printf 'cancelled in the outage\n' >"$w/lts/docs/hang-a.bin"
  printf 'waited out the outage\n' >"$w/lts/docs/hang-b.bin"
  printf 'staged when it began\n' >"$w/lts/docs/hang-c.bin"
  start_unit "$id"
  # After the suites before it the annex holds pointers and removed files, so only these.
  check "hang: 1. the files are copied" wait_for 180 hang_copied "$id" "$root/lts"
  nudge "$id" SIGHUP
  check "hang: 1. a full scan reads the return location's ACL" wait_for 60 folder_acls "$id" elsewhere/restored
  wait_for 60 quiet "$id"

  gone=$(A1 gone-count "$id")
  ev=$(A1 last-event "$id")
  pid=$(upid "$id")
  since=$(date '+%F %T')
  block_nfs
  nudge "$id" SIGHUP # a full scan walks into it
  check "hang: 2. a scan caught in it: the unit doesn't sit there, it restarts" \
    wait_for $((HANG_S + 60)) restarted "$id" "$pid"
  journal_alert() { journalctl -u "$(unit "$1")" --since "$since" --no-pager | grep -q 'ALERT annex: the NAS'; }
  check "hang: 2. ... with an ALERT" wait_for 20 journal_alert "$id"
  check "hang: 3. the service is told" wait_for 60 mount_says "$id" "has not answered" "$ev"
  check "hang: 3. ... as a problem an admin sees" wait_for 60 hung_problem "$id" "$ev"
  check "hang: 3. ... and that it can't mount, in the filer's health" wait_for 300 mount_says "$id" "mount.nfs" "$ev"
  a=$(A1 place "$id" docs/hang-a.bin | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["placement_id"])')
  b=$(A1 place "$id" docs/hang-b.bin | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["placement_id"])')
  check "hang: 4. with no mount it still asks, and the tries count" wait_for 420 jtried "$id" "$b"
  A1 cancel-placement "$a" >/dev/null
  # At the bound, and its report: each try a mount that takes two minutes to fail and
  # a minute's wait, or two of those for the last backoff. It took about 1330 s.
  check "hang: 4. a cancel is heard with the NAS still away" wait_for 1500 jfailed "$id" "$a"
  check "hang: 4. stopping the unit works, mid-outage" timeout 150 systemctl stop "$(unit "$id")"
  ev=$(A1 last-event "$id")
  start_unit "$id"
  sleep 5
  unblock_nfs
  check "hang: 5. the rule is out" bash -c "! ip netns exec $NS iptables -C INPUT -p tcp --dport 2049 -j DROP 2>/dev/null"
  check "hang: 5. back: mounted again" wait_for 420 mount_up "$id" "$ev"
  check "hang: 5. a placement that waited out the outage finishes" wait_for 420 pstate "$id" docs/hang-b.bin placed
  check "hang: 5. ... whole" test "$(cat "$root/elsewhere/restored/docs/hang-b.bin")" = 'waited out the outage'
  check "hang: 5. the cancelled one left no file" test ! -e "$root/elsewhere/restored/docs/hang-a.bin"
  nudge "$id" SIGHUP
  sleep 8
  wait_for 120 quiet "$id"
  check "hang: 5. no file is taken for gone" test "$(A1 gone-count "$id")" = "$gone"
  check "hang: 5. its files are still copied" hang_copied "$id" "$root/lts"

  # A whole file staged, and the NAS goes quiet with the placement in hand.
  started_up "$id" OMNIFILE_ANNEX_HOLD_AT=place_secured
  c=$(A1 place "$id" docs/hang-c.bin | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["placement_id"])')
  check "hang: 6. held at secured" wait_for 180 jat "$id" "$c" secured
  pid=$(upid "$id")
  block_nfs
  check "hang: 6. the placement path too: it restarts" wait_for $((HANG_S + 60)) restarted "$id" "$pid"
  start_unit "$id"
  sleep 5
  unblock_nfs
  check "hang: 6. resumed: placed" wait_for 420 pstate "$id" docs/hang-c.bin placed
  check "hang: 6. ... whole" test "$(cat "$root/elsewhere/restored/docs/hang-c.bin")" = 'staged when it began'
  systemctl stop "$(unit "$id")"
  check "hang: the firewall rule is gone" bash -c "! ip netns exec $NS iptables -C INPUT -p tcp --dport 2049 -j DROP 2>/dev/null"
}
# What the kernel does with a dying process depends on what it has open, and how
# the NAS went quiet: the service must be told all the same, whichever, and a
# restart must get a new process going.
hang_stuck_case() { # hang_stuck_case LABEL HOW ID ROOT [PLACE]: HOW is drop or freeze
  local label=$1 how=$2 id=$3 root=$4 place=${5:-} ev pid p t0 back
  ev=$(A1 last-event "$id")
  pid=$(upid "$id")
  if [ -n "$place" ]; then
    p=$(A1 place "$id" "$place" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["placement_id"])')
    check "hang: $label: its fill has begun" wait_for 120 jat "$id" "$p" filling
    sleep 6 # three chunks in: dirty pages, and a writable descriptor
  fi
  if [ "$how" = drop ]; then block_nfs; else freeze_nas; fi
  if [ -z "$place" ]; then nudge "$id" SIGHUP; fi
  t0=$(date +%s)
  check "hang: $label: the service is told, whatever the process can do" \
    wait_for $((HANG_S + 60)) mount_says "$id" "has not answered" "$ev"
  say "hang: $label: told after $(( $(date +%s) - t0 )) s"
  check "hang: $label: ... as a problem an admin sees" wait_for 20 hung_problem "$id" "$ev"
  sleep 40
  say "hang: $label: main pid was $pid"
  unit_shows "$id" "hang: $label, after its exit"
  say "hang: $label: restarting it, with the NAS still quiet"
  pid=$(upid "$id")
  t0=$(date +%s)
  timeout 300 systemctl restart "$(unit "$id")"
  say "hang: $label: restart returned $? after $(( $(date +%s) - t0 )) s"
  unit_shows "$id" "hang: $label, after a restart"
  check "hang: $label: a restart gets a new process going" wait_for 240 restarted "$id" "$pid"
  check "hang: $label: ... which says it can't mount" wait_for 300 mount_says "$id" "mount" "$ev"
  back=$(A1 last-event "$id")
  if [ "$how" = drop ]; then unblock_nfs; else thaw_nas; fi
  check "hang: $label: back: mounted again" wait_for 600 mount_up "$id" "$back"
  if [ -n "$place" ]; then
    check "hang: $label: the placement finishes" wait_for 600 pstate "$id" "$place" placed
    check "hang: $label: ... whole" cmp "$root/lts/$place" "$root/elsewhere/restored/$place"
  fi
  unit_shows "$id" "hang: $label, afterwards"
  timeout 180 systemctl stop "$(unit "$id")"
}
hang_file() { # hang_file ROOT NAME MIB
  head -c $(($3 * 1024 * 1024)) /dev/urandom >"$1/lts/docs/$2"
  chown 20002:20001 "$1/lts/docs/$2"
  chmod 0644 "$1/lts/docs/$2"
}
hang_cases() { # hang_cases NFS_ID NFS_ROOT V4_ID V4_ROOT
  local nid=$1 nroot=$2 vid=$3 vroot=$4
  say "== a NAS that stops answering, with the process in other waits"
  chmod 0777 "$nroot" "$vroot" 2>/dev/null
  mkdir -p "$nroot/elsewhere/restored" "$vroot/elsewhere/restored"
  chmod -R 0777 "$nroot/elsewhere" "$vroot/elsewhere" 2>/dev/null
  [[ " ${E2E_ONLY:-nfs} " == *" nfs "* ]] || [[ " ${E2E_ONLY:-} " == *" hang "* ]] || A1 returns "$nid" >/dev/null
  [[ " ${E2E_ONLY:-v4} " == *" v4 "* ]] || A1 returns "$vid" >/dev/null
  # fills take a while here: two seconds a chunk.
  hang_file "$nroot" hang-drop.bin 60
  hang_file "$nroot" hang-freeze.bin 60
  start_unit "$nid" OMNIFILE_ANNEX_FILL_PAUSE_MS=2000
  fill_copied() { jt "$(A1 copies "$1" "$2" --only docs/hang-"$3")" 'd["files"] == 1 and d["matched"] == 1 and d["verified"] == 1'; }
  nudge "$nid" SIGHUP
  check "hang: v3: the files are copied" wait_for 300 bash -c "$(declare -f A1 jt fill_copied); PY=$PY HERE=$HERE; fill_copied $nid $nroot/lts drop && fill_copied $nid $nroot/lts freeze"
  check "hang: v3: a full scan reads the return location's ACL" wait_for 60 folder_acls "$nid" elsewhere/restored
  wait_for 60 quiet "$nid"
  hang_stuck_case "v3 fill, dropped" drop "$nid" "$nroot" docs/hang-drop.bin
  started_up "$nid" OMNIFILE_ANNEX_FILL_PAUSE_MS=2000
  hang_stuck_case "v3 fill, frozen" freeze "$nid" "$nroot" docs/hang-freeze.bin

  # v4: a scan with nothing open, then a fill with a writable descriptor.
  hang_file "$vroot" hang-drop.bin 40
  hang_file "$vroot" hang-freeze.bin 40
  start_unit "$vid" OMNIFILE_ANNEX_FILL_PAUSE_MS=2000
  nudge "$vid" SIGHUP
  check "hang: v4: the files are copied" wait_for 300 bash -c "$(declare -f A1 jt fill_copied); PY=$PY HERE=$HERE; fill_copied $vid $vroot/lts drop && fill_copied $vid $vroot/lts freeze"
  check "hang: v4: a full scan reads the return location's ACL" wait_for 60 folder_acls "$vid" elsewhere/restored
  wait_for 60 quiet "$vid"
  hang_stuck_case "v4 scan, dropped" drop "$vid" "$vroot"
  started_up "$vid" OMNIFILE_ANNEX_FILL_PAUSE_MS=2000
  hang_stuck_case "v4 scan, frozen" freeze "$vid" "$vroot"
  started_up "$vid" OMNIFILE_ANNEX_FILL_PAUSE_MS=2000
  hang_stuck_case "v4 fill, dropped" drop "$vid" "$vroot" docs/hang-drop.bin
  started_up "$vid" OMNIFILE_ANNEX_FILL_PAUSE_MS=2000
  hang_stuck_case "v4 fill, frozen" freeze "$vid" "$vroot" docs/hang-freeze.bin
  check "hang: the firewall rule is gone, after them" bash -c "! ip netns exec $NS iptables -C INPUT -p tcp --dport 2049 -j DROP 2>/dev/null"
}
# Phase A3.4 (a34.md; decisions 82 to 85): with overwrite_keep_version, a file comes
# back to its own name in its original folder and replaces the file there, which must
# be exactly the annex's verified copy. The old file is hidden, waits the annex's grace
# (1 minute here, the shortest), the new one is linked, and only then is the old one
# deleted. Each file under lts/rp has two versions copied; the older one is placed.
hidden_at() { ls -A "$1" 2>/dev/null | grep -q "^\.omnifile-replacing-[0-9]*-$2\$"; }
rp_ok() { # rp_ok ID F OUTCOME: placed with this outcome
  pstate "$1" "rp/$2" placed && test "$(pget "$1" "rp/$2" 'p.get("outcome")')" = "$3"
}
replace_suite() { # replace_suite NAME ID ROOT USERS
  local name=$1 id=$2 root=$3 w=$4 r f at d
  W() { if [ "$name" = smb ]; then nsenter -t "$WPID" -m "$@"; else "$@"; fi; }
  say "== $name annex $id: replaces at the origin (A3.4)"
  chmod 0777 "$root"
  A1 returns-replace "$id" >/dev/null
  start_unit "$id"
  wait_for 60 quiet "$id"
  if [ "$name" = smb ] && [ -n "${E2E_ONLY:-}" ] && ! on placer; then register_placer "$id"; fi
  # Two versions of each file, both copied and read back in Azure.
  local files="a b c e legal k-replace_hiding k-replace_hidden k-replace_grace_passed k-replace_deleting"
  W mkdir -p "$w/lts/rp"
  # The setup guide's grant: the restore group may add and remove files where a file
  # may be replaced (as the removal group may where files leave). Here, open to all.
  if [ "$name" = nfs ]; then chmod 0777 "$w/lts/rp"; else chmod 0777 "$root/lts/rp"; fi
  for f in $files; do W sh -c "echo 'one: $f' >'$w/lts/rp/$f.bin'"; done
  nudge "$id" SIGHUP
  for f in $files; do wait_for 240 A1 copied "$id" "rp/$f.bin" 1 || say "$name: rp/$f.bin not copied once"; done
  sleep 2 # a later mtime
  for f in $files; do W sh -c "echo 'two, the version on the NAS: $f' >'$w/lts/rp/$f.bin'"; done
  nudge "$id" SIGHUP
  local all=0
  for f in $files; do wait_for 240 A1 copied "$id" "rp/$f.bin" 2 && all=$((all + 1)); done
  check "$name: RP0. each file has two versions, the current one verified and read back" test "$all" -eq 9
  A1 snapshot "$root" "$WORK/$name-before-replaces.json" >/dev/null

  A1 place "$id" rp/a.bin --older >/dev/null
  check "$name: RP1. the old file is hidden, and the name empty for the grace" \
    wait_for 120 bash -c "$(declare -f hidden_at); hidden_at '$root/lts/rp' a.bin && test ! -e '$root/lts/rp/a.bin'"
  check "$name: RP1. ... then the older version takes its own name, the old one deleted" \
    wait_for 240 rp_ok "$id" a.bin replaced
  check "$name: RP1. ... its bytes" test "$(cat "$root/lts/rp/a.bin")" = "one: a"
  check "$name: RP1. ... no hidden file left" bash -c "$(declare -f hidden_at); ! hidden_at '$root/lts/rp' a.bin"
  r=$(A1 heads "$id" rp/a.bin)
  check "$name: RP1. ... a new head that references the placed version's blob; the replaced one kept" \
    jt "$r" "len(d) >= 3 and d[2]['key'] == d[0]['key'] and not d[1]['head'] and not d[0]['head']"
  check "$name: RP1. ... the replaced version is named on the placement" \
    test "$(pget "$id" rp/a.bin 'p.get("replaced_version_id")')" = "$(j "$r" 'd[1]["version_id"]')"

  # Changed on the NAS after its ticket, right before the hide: never hidden. (Held
  # there, so a quick scan can't copy the change first and make it the copy again.)
  start_unit "$id" OMNIFILE_ANNEX_HOLD_AT=place_replace_start
  A1 place "$id" rp/b.bin --older >/dev/null
  wait_for 120 jat "$id" "$(pget "$id" rp/b.bin 'p.get("placement_id")')" replace_start
  W sh -c "echo 'a late line' >>'$w/lts/rp/b.bin'"
  start_unit "$id"
  check "$name: RP2. a file changed since its copy is never replaced: a -restored name" \
    wait_for 300 rp_ok "$id" b.bin overwrite_refused_changed
  check "$name: RP2. ... the user's file untouched, the older version beside it" bash -c \
    "grep -q 'a late line' '$root/lts/rp/b.bin' && test \"\$(cat '$root/lts/rp/b-restored.bin')\" = 'one: b'"
  A1 legal-hold "$id" rp/legal.bin >/dev/null
  A1 place "$id" rp/legal.bin --older >/dev/null
  check "$name: RP2. under a legal hold: never replaced, a -restored name" \
    wait_for 180 rp_ok "$id" legal.bin overwrite_refused_legal_hold
  check "$name: RP2. ... the file there untouched" \
    test "$(cat "$root/lts/rp/legal.bin")" = "two, the version on the NAS: legal"

  # A write to the hidden file in the grace: it goes back under its own name.
  A1 place "$id" rp/c.bin --older >/dev/null
  wait_for 120 bash -c "$(declare -f hidden_at); hidden_at '$root/lts/rp' c.bin"
  d=$(ls -A "$root/lts/rp" | grep '^\.omnifile-replacing-[0-9]*-c\.bin$')
  W sh -c "echo 'written in the grace' >>'$w/lts/rp/$d'"
  check "$name: RP3. a write in the grace: the old file back under its own name, with it" \
    wait_for 240 rp_ok "$id" c.bin overwrite_refused_changed_in_grace
  check "$name: RP3. ... its write there, the older version at a -restored name" bash -c \
    "grep -q 'written in the grace' '$root/lts/rp/c.bin' && test \"\$(cat '$root/lts/rp/c-restored.bin')\" = 'one: c'"

  # A write after the new file has its name and before the old one is deleted: the
  # old file kept, visible, as -replaced-<time>, and reported.
  start_unit "$id" OMNIFILE_ANNEX_HOLD_AT=place_linked
  A1 place "$id" rp/e.bin --older >/dev/null
  check "$name: RP4. held after the link: the new file has the name, the old one hidden" \
    wait_for 240 bash -c "$(declare -f hidden_at); hidden_at '$root/lts/rp' e.bin && test \"\$(cat '$root/lts/rp/e.bin' 2>/dev/null)\" = 'one: e'"
  d=$(ls -A "$root/lts/rp" | grep '^\.omnifile-replacing-[0-9]*-e\.bin$')
  W sh -c "echo 'written before the delete' >>'$w/lts/rp/$d'"
  start_unit "$id"
  check "$name: RP4. ... the old file kept under a visible -replaced- name, nothing deleted" \
    wait_for 300 rp_ok "$id" e.bin replaced_old_kept
  at=$(pget "$id" rp/e.bin 'p.get("kept_as") or ""')
  check "$name: RP4. ... its write there" bash -c "[[ '$at' == e-replaced-*Z.bin ]] && grep -q 'written before the delete' '$root/lts/rp/$at'"
  check "$name: RP4. ... and a Problem names both" "$PY" - "$id" "$at" <<'PY'
import os, sys, psycopg
with psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"]) as c:
    n = c.execute("SELECT count(*) FROM catalog.annex_event WHERE annex_id = %s AND kind = 'problem'"
                  " AND details->>'reason' = 'replace_kept_aside'"
                  " AND details->>'kept_as' LIKE %s", (int(sys.argv[1]), "%" + sys.argv[2])).fetchone()[0]
sys.exit(0 if n == 1 else 1)
PY

  # Killed at each step: it resumes, replaces once, and never deletes twice.
  for at in replace_hiding replace_hidden replace_grace_passed replace_deleting; do
    f=k-$at.bin
    start_unit "$id" OMNIFILE_ANNEX_CRASH_AT=place_$at
    A1 place "$id" "rp/$f" --older >/dev/null
    check "$name: RP5. killed at $at: resumed, replaced once" wait_for 300 rp_ok "$id" "$f" replaced
    check "$name: RP5. killed at $at: its bytes, no hidden file" bash -c \
      "$(declare -f hidden_at); test \"\$(cat '$root/lts/rp/$f')\" = 'one: ${f%.bin}' && ! hidden_at '$root/lts/rp' $f"
    check "$name: RP5. killed at $at: it did crash there" test -e "$WORK/annex/$id/crashed-place_$at"
  done
  start_unit "$id"
  check "$name: RP6. no placement mount once none is in hand" wait_for 60 nopl "$id"
  A1 snapshot "$root" "$WORK/$name-after-replaces.json" >/dev/null
  r=$(A1 diff "$WORK/$name-before-replaces.json" "$WORK/$name-after-replaces.json")
  say "$name: changes on the NAS: $r"
  at=$(pget "$id" rp/e.bin 'p.get("kept_as") or ""')
  want_added="sorted(['lts/rp/b-restored.bin', 'lts/rp/c-restored.bin', 'lts/rp/$at', 'lts/rp/legal-restored.bin'])"
  check "$name: RP7. the NAS shows exactly the expected changes, nothing else" jt "$r" \
    "d['added'] == $want_added and d['removed'] == [] and sorted(d['changed']) == sorted(['lts/rp/a.bin', 'lts/rp/b.bin', 'lts/rp/c.bin', 'lts/rp/e.bin'] + ['lts/rp/k-%s.bin' % s for s in ('replace_hiding', 'replace_hidden', 'replace_grace_passed', 'replace_deleting')])"
  systemctl stop "$(unit "$id")"
}
on() { [ -z "${E2E_ONLY:-}" ] || [[ " $E2E_ONLY " == *" $1 "* ]]; }
if on smb; then suite smb "$SMB_ID" "$WORK/nas/smb" krb5 "$WORK/w/smb"; fi
if on nfs; then suite nfs "$NFS_ID" "$WORK/nas/nfs" sys "$WORK/w/nfs"; fi
if on smb; then removals_suite smb "$SMB_ID" "$WORK/nas/smb" "$WORK/w/smb"; fi
if on nfs; then removals_suite nfs "$NFS_ID" "$WORK/nas/nfs" "$WORK/w/nfs"; fi
if on smb; then placements_suite smb "$SMB_ID" "$WORK/nas/smb" "$WORK/w/smb"; fi
if on nfs; then placements_suite nfs "$NFS_ID" "$WORK/nas/nfs" "$WORK/w/nfs"; fi
if on replace; then replace_suite smb "$SMB_ID" "$WORK/nas/smb" "$WORK/w/smb"; fi
if on replace; then replace_suite nfs "$NFS_ID" "$WORK/nas/nfs" "$WORK/w/nfs"; fi
if on root; then export_root_suite "$ROOT_ID" "$WORK/nas/rootx"; fi
if on v4; then v4_suite "$V4_ID" "$WORK/nas/v4x"; fi
if on hang; then hang_suite "$NFS_ID" "$WORK/nas/nfs" "$WORK/w/nfs"; fi
if on stuck; then hang_cases "$NFS_ID" "$WORK/nas/nfs" "$V4_ID" "$WORK/nas/v4x"; fi
# A stage of its own only when asked for: the SMB placements run this in a full run.
if [ -n "${E2E_ONLY:-}" ] && on placer; then
  start_unit "$SMB_ID"
  register_placer "$SMB_ID"
  systemctl stop "$(unit "$SMB_ID")"
fi
if on smb; then
r=$("$PY" - "$SMB_ID" <<'PY'
import json, os, sys, psycopg
with psycopg.connect(os.environ["OMNIFILE_DATABASE_URL"]) as c:
    rows = c.execute(
        "SELECT a.raw_kind, a.deny_first, cardinality(a.read_sids) FROM catalog.annex_file af"
        " JOIN catalog.file f USING (file_id) JOIN catalog.acl a ON a.acl_id = f.acl_id"
        " WHERE af.annex_id = %s", (int(sys.argv[1]),)).fetchall()
print(json.dumps([list(x) for x in rows]))
PY
)
check "smb: the NT security descriptor of every copied file is in the catalog, with a read set" \
  jt "$r" 'd and all(k == "ntfs_sd" and first and n > 0 for k, first, n in d)'
r=$(A1 acl-matches-mode "$SMB_ID" "$WORK/nas/smb/lts")
say "smb: read sets against the server's modes: $r"
check "smb: each file's read set is what its mode grants, as Samba's descriptor says" \
  jt "$r" 'd["files"] >= 6 and not d["problems"]'
fi
finish
