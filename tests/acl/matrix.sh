#!/usr/bin/env bash
# AD RBAC permission matrix (decision 28, docs/design/access-control.md,
# "Tests that prove it"). Runs as root INSIDE the omnifile-acl VM
# (tests/acl/vm/up.sh), against the dev AD in omnifile-dc:
#
#   limactl shell --workdir / omnifile-acl -- sudo /work/tests/acl/matrix.sh [phase...]
#
# Phases (default: all but revoke):
#   setup     share tree, root ACL via omniacl, resident files and stubs
#   servers   smb.conf + Ganesha exports rendered by omniacl, smbd, fake daemon, Ganesha
#   tickets   Kerberos tickets for every test user (passwords from ad.env, never printed)
#   nfs       NFS sec=sys + Manage_Gids, and NFS krb5p: users x {resident, stub, recalling}
#   smb       SMB (smbclient + CIFS krb5 mounts): users x {resident, stub*}
#   explorer  smbcacls edits (Explorer's path) enforced over NFS; drift scan
#   identity  SMB pid -> session -> SID (omniacl resolve-smb), NFS uid -> SID
#   acls      ACLs unchanged by stub + recall (on-disk and snapshot hash)
#   revoke    see tests/acl/revoke.sh (run on the Mac: it changes AD on omnifile-dc)
#   down      unmount, stop servers
#
# SMB reads of stubs go through the production smb-guard library
# (omnifile-smbguard-harness, failfast, fills disabled) on this VM's volume
# while the smb phase runs: a permitted read fails with an NT_STATUS error and
# starts a recall from the user's smbd, a denied one never reaches the guard.
# Set SMBGUARD_BIN to the directory holding omnifile-smbguard-harness (default:
# build it with cargo into /var/tmp/acl-matrix/target); without it the five
# rows are PENDING. The integrated daemon's SMB path (SID attribution, wait and
# failfast, kill -9) is tests/e2e/m2_exit_test.sh on the edge VM.
#
# What is NOT exercised here, and why:
#   - The real daemon: FSAL_OMNI talks to fsal/tests/fake_daemon.py, which
#     plays the daemon's recall role (READ_INTENT -> recall -> fill).
#
# Results: /var/tmp/acl-matrix/results.tsv (cell, expected, actual, verdict).
set -uo pipefail

ENV_FILE=${AD_ENV_FILE:-/work/dev/.secrets/ad.env}
FSAL_T=/work/fsal/tests
TPL=/work/edge/config
DATA=/srv/acl/m
SHARE=$DATA/proj
BACKEND=/srv/acl/backend
RUN=/var/tmp/acl-matrix
MNT=/mnt/acl
SOCK=/run/omnifile/fsal.sock
CTL=/run/omnifile/fake-ctl.sock
DLOG=$RUN/daemon.log
GLOG=$RUN/ganesha.log
RES=$RUN/results.tsv
FQDN=acl1.omni.test
T=${OP_TIMEOUT:-25}
OMNIACL=${OMNIACL:-/usr/local/bin/omniacl}

# uid of each test user (dev/README.md seed data)
declare -A UID_OF=([geo-reader]=20001 [geo-writer]=20002 [geo-none]=20003 [geo-owner]=20005 [geo-nested]=20006)
USERS=(geo-owner geo-writer geo-reader geo-nested geo-none)
# What each user may do on the share (Owners rwx, Writers rwx, Readers r-x;
# geo-nested is in Proj-Readers through Proj-Readers-Nested).
declare -A MAY_READ=([geo-owner]=OK [geo-writer]=OK [geo-reader]=OK [geo-nested]=OK [geo-none]=DENIED)
declare -A MAY_WRITE=([geo-owner]=OK [geo-writer]=OK [geo-reader]=DENIED [geo-nested]=DENIED [geo-none]=DENIED)

log() { printf '[matrix] %s\n' "$*"; }
die() { printf '[matrix] ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run as root"
mkdir -p "$RUN" "$MNT"

record() { # cell expected actual
  local v
  if [ "$2" = "$3" ]; then v=PASS; elif [ "$2" = PENDING ]; then v=PENDING; else v=FAIL; fi
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$v" >>"$RES"
  printf '  %-58s expect %-8s got %-8s %s\n' "$1" "$2" "$3" "$v"
}
finding() { printf '%s\t%s\t%s\tFINDING\n' "$1" "$2" "$3" >>"$RES"; printf '  %-58s expect %-8s got %-8s FINDING\n' "$1" "$2" "$3"; }
pending() { printf '%s\t-\t-\tPENDING: %s\n' "$1" "$2" >>"$RES"; printf '  %-58s PENDING (%s)\n' "$1" "$2"; }

# as <user> [--groups g1,g2] cmd...: run with the user's uid, gid 10000 and NO
# supplementary groups unless given (so NFS sec=sys sends none and the server
# must look them up: Manage_Gids), and the user's Kerberos cache.
as() {
  local u=$1; shift
  local g=(--clear-groups)
  if [ "${1:-}" = --groups ]; then g=(--groups "$2"); shift 2; fi
  setpriv --reuid="${UID_OF[$u]}" --regid=10000 "${g[@]}" --inh-caps=-all -- \
    env KRB5CCNAME="FILE:/tmp/krb5cc_${UID_OF[$u]}" HOME=/tmp "$@"
}

# classify <rc> <stderr>: OK / DENIED / EIO / TIMEOUT / ERR
classify() {
  local rc=$1 err=$2
  if [ "$rc" -eq 0 ]; then echo OK
  elif [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then echo TIMEOUT
  elif grep -qiE 'permission denied|NT_STATUS_ACCESS_DENIED|access denied|operation not permitted' <<<"$err"; then echo DENIED
  elif grep -qiE 'input/output error|I/O error|NT_STATUS_IO' <<<"$err"; then echo EIO
  else printf '%s rc=%s %s\n' "$(date +%T)" "$rc" "$err" >>"$RUN/errors.log"; echo "ERR"; fi
}

fs_read() { # user path [extra as-args]
  local u=$1 p=$2; shift 2
  local err rc
  err=$(as "$u" "$@" timeout -s KILL "$T" dd if="$p" of=/dev/null bs=64k count=32 status=none 2>&1); rc=$?
  classify "$rc" "$err"
}
fs_write() { # user path: append one line
  local u=$1 p=$2 err rc
  err=$(as "$u" timeout -s KILL "$T" sh -c "echo omni >> '$p'" 2>&1); rc=$?
  classify "$rc" "$err"
}
fs_create() { # user dir
  local u=$1 d=$2 err rc
  err=$(as "$u" timeout -s KILL "$T" sh -c "echo new > '$d/new-$u-$RANDOM.txt'" 2>&1); rc=$?
  classify "$rc" "$err"
}

intents_for() { # uid -> number of READ_INTENTs from that uid so far
  python3 - "$1" "$DLOG" <<'EOF'
import json, sys
uid = int(sys.argv[1]); n = 0
for l in open(sys.argv[2]):
    try: m = json.loads(l)
    except Exception: continue
    if m.get("type") == "READ_INTENT" or "ident" in m and m.get("op"):
        if m.get("ident", {}).get("uid") == uid: n += 1
print(n)
EOF
}

# ---------------------------------------------------------------------------
phase_setup() {
  log "setup: share tree at $SHARE"
  umount -fl "$MNT"/* 2>/dev/null
  rm -rf "$DATA" "$BACKEND"
  install -d -m 0755 "$DATA" "$BACKEND"
  install -d -m 0755 "$SHARE"
  "$OMNIACL" apply-root "$SHARE" 30003 30002 30001 >"$RUN/root-acl.txt" || die "apply-root"
  log "root ACL: $(cat "$RUN/root-acl.txt")"
  # Files as the daemon would materialise them: created in the share (they
  # inherit the default ACL), owned by geo-owner.
  echo "resident bytes" >"$SHARE/resident.txt"
  echo "owners only" >"$SHARE/owners-only.txt"
  setfacl -x g:30001,g:30002 "$SHARE/owners-only.txt"
  local id=100 u
  for u in "${USERS[@]}"; do
    # stub-*: ARCHIVED, 4 KiB resident of 1 MiB (failfast export)
    # rec-*:  ARCHIVED (wait export: the read waits through the recall)
    id=$((id + 1)); python3 "$FSAL_T/mkstub.py" --backend "$BACKEND" "$SHARE/stub-$u.segy" $id 1048576 4096
    id=$((id + 1)); python3 "$FSAL_T/mkstub.py" --backend "$BACKEND" "$SHARE/rec-$u.segy" $id 1048576 4096
    id=$((id + 1)); python3 "$FSAL_T/mkstub.py" --backend "$BACKEND" "$SHARE/krb-$u.segy" $id 1048576 4096
    id=$((id + 1)); python3 "$FSAL_T/mkstub.py" --backend "$BACKEND" "$SHARE/smbstub-$u.segy" $id 1048576 4096
  done
  chown -R 20005:10000 "$SHARE"/*
  # mkstub.py (like the daemon's materializer) builds placeholders in a way
  # that does not inherit the share's default ACL and then chmods 0644, which
  # opens `other` and narrows the mask. The product must give a placeholder
  # the ACL the catalog supplies (control API §12 item 6); emulate that: the ACL a
  # file created in the share inherits.
  local f
  for f in "$SHARE"/*.segy; do
    getfacl -dcpn "$SHARE" | sed -e 's/^default://' | setfacl --set-file=- "$f"
    setfacl -m u::rw-,m::rw-,o::--- "$f"
  done
  for f in "$SHARE"/*; do getfacl -cpn "$f" 2>/dev/null | sort; done | sha256sum >"$RUN/acl-before.sum"
  ( cd "$SHARE" && for f in *; do printf '%s ' "$f"; getfacl -cpn "$f" | tr '\n' ' '; echo; done ) >"$RUN/acl-before.txt"
  "$OMNIACL" scan "$SHARE" "$RUN/scan-state.json" >"$RUN/scan-initial.json" || die "scan"
  log "scan: $(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d["entries_seen"],"entries,",d["distinct_acls"],"distinct ACLs, findings",d["findings"])' "$RUN/scan-initial.json")"
  "$OMNIACL" check-root "$SHARE" 30003 30002 30001
}

phase_servers() {
  log "servers: rendering config with omniacl"
  set -a; . "$ENV_FILE"; set +a
  local d=S-1-5-21-3190051465-787089565-3716134449
  d=$(wbinfo -n proj-owners | cut -d' ' -f1); d=${d%-*}
  local own=$d-1105 wri=$d-1104 rea=$d-1103 ip
  ip=$(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -1)
  "$OMNIACL" render proj "$SHARE" 10 smb - - "$own" "$wri" "$rea" failfast >"$RUN/render-smb.txt" || die "render smb"
  "$OMNIACL" render proj-sys "$SHARE" 11 nfs3,nfs4 sys "127.0.0.1,$ip" "$own" "$wri" "$rea" failfast >"$RUN/render-sys.txt" || die "render sys"
  "$OMNIACL" render proj-wait "$SHARE" 12 nfs4 sys "127.0.0.1,$ip" "$own" "$wri" "$rea" wait >"$RUN/render-wait.txt" || die "render wait"
  "$OMNIACL" render proj-krb "$SHARE" 13 nfs4 krb5,krb5i,krb5p - "$own" "$wri" "$rea" wait >"$RUN/render-krb.txt" || die "render krb"
  # A wildcard host-trusted share must be refused.
  if "$OMNIACL" render bad "$SHARE" 14 nfs4 sys '*' "$own" - - wait >/dev/null 2>"$RUN/render-bad.txt"; then
    record "config: host-trusted share with Clients=*" REFUSED ACCEPTED
  else
    record "config: host-trusted share with Clients=*" REFUSED REFUSED
  fi
  sed -n '/^#--- smb/,/^#--- ganesha/p' "$RUN/render-smb.txt" | grep -v '^#---' >/etc/samba/omnifile-shares.conf
  for f in sys wait krb; do sed -n '/^#--- ganesha/,$p' "$RUN/render-$f.txt" | grep -v '^#---'; done >/etc/ganesha/omnifile-exports.conf
  sed -e "s/@WORKGROUP@/$AD_DOMAIN/g" -e "s/@REALM@/$AD_REALM/g" -e "s/@NETBIOS_NAME@/ACL1/g" \
      -e "s/@IDMAP_RANGE@/10000-999999/g" -e "s/@AUDIT_FACILITY@/local5/g" \
      "$TPL/samba/smb.conf.tmpl" >/etc/samba/smb.conf
  testparm -s >"$RUN/testparm.txt" 2>&1 || die "testparm failed"
  if grep -qi acl_xattr "$RUN/testparm.txt"; then record "smb.conf: no vfs_acl_xattr" ABSENT PRESENT; else record "smb.conf: no vfs_acl_xattr" ABSENT ABSENT; fi
  sed -e "s#@NFS_DOMAIN@#omni.test#g" -e "s#@KEYTAB@#/etc/krb5.keytab#g" -e "s#@FSAL_SOCKET@#$SOCK#g" \
      "$TPL/ganesha/ganesha.conf.tmpl" >/etc/ganesha/ganesha.conf
  systemctl restart winbind; systemctl restart smbd || die "smbd"
  # fake daemon (the recall side of the FSAL contract)
  pkill -f fake_daemon.py; pkill -x ganesha.nfsd; sleep 1
  install -d /run/omnifile; : >"$DLOG"
  setsid python3 "$FSAL_T/fake_daemon.py" --root "$SHARE" --backend "$BACKEND" --socket "$SOCK" \
    --ctl "$CTL" --log "$DLOG" --recall-delay 2 --fetch-delay 0.5 >"$RUN/daemon.out" 2>&1 </dev/null &
  for _ in $(seq 1 20); do [ -S "$SOCK" ] && break; sleep 0.25; done
  pgrep -x rpcbind >/dev/null || rpcbind -w
  systemctl start rpc-gssd 2>/dev/null
  : >"$GLOG"
  setsid /opt/ganesha/bin/ganesha.nfsd -F -L "$GLOG" -f /etc/ganesha/ganesha.conf -N NIV_EVENT \
    >"$RUN/ganesha.out" 2>&1 </dev/null &
  for _ in $(seq 1 60); do grep -q 'NFS SERVER INITIALIZED' "$GLOG" && break; sleep 0.5; done
  grep -q 'NFS SERVER INITIALIZED' "$GLOG" || { tail -30 "$GLOG"; die "ganesha did not start"; }
  grep -E 'CRIT|MAJ' "$GLOG" | grep -v DBUS | head -5
  # NFSv4 opens are refused during the grace period after a (re)start.
  log "waiting for Ganesha to leave its grace period"
  for _ in $(seq 1 120); do grep -q 'NOT IN GRACE' "$GLOG" && break; sleep 1; done
  log "servers up: smbd $(systemctl is-active smbd), ganesha $(pgrep -x ganesha.nfsd), daemon $(pgrep -f fake_daemon.py | head -1)"
}

phase_tickets() {
  log "tickets: kinit each test user (password from ad.env via stdin)"
  set -a; . "$ENV_FILE"; set +a
  local u var pw
  for u in "${USERS[@]}"; do
    var="AD_PASSWORD_$(printf '%s' "$u" | tr 'a-z-' 'A-Z_')"
    pw="${!var:-}"; [ -n "$pw" ] || die "$var missing"
    rm -f "/tmp/krb5cc_${UID_OF[$u]}"
    printf '%s\n' "$pw" | as "$u" kinit "$u@OMNI.TEST" >/dev/null 2>"$RUN/kinit-$u.err" \
      || die "kinit $u failed: $(cat "$RUN/kinit-$u.err")"
  done
  # smbclient/smbcacls authenticate with NTLMv2 against the DC from a 0600
  # auth file: smbclient 4.23 here would not use an MIT ccache
  # (--use-krb5-ccache ignored, "No password for user principal"). The CIFS
  # kernel mounts below use Kerberos (sec=krb5).
  umask 077; install -d -m 700 /root/acl-auth
  for u in "${USERS[@]}"; do
    var="AD_PASSWORD_$(printf '%s' "$u" | tr 'a-z-' 'A-Z_')"
    printf 'username = %s\npassword = %s\ndomain = OMNI\n' "$u" "${!var}" >"/root/acl-auth/$u"
  done
  umask 022
  unset pw
  log "tickets ok"
}

mount_nfs() { # export mountpoint sec
  install -d "$2"; mountpoint -q "$2" && return 0
  mount -t nfs -o "vers=4.2,sec=$3,noac,lookupcache=none,soft,timeo=600,retrans=2" "$FQDN:/$1" "$2"
}

phase_nfs() {
  mount_nfs proj-sys "$MNT/sys" sys || die "mount sys"
  mount_nfs proj-wait "$MNT/wait" sys || die "mount wait"
  log "NFS sec=sys (Manage_Gids: clients send no supplementary groups)"
  local u got n0 n1
  for u in "${USERS[@]}"; do
    record "nfs-sys $u resident read" "${MAY_READ[$u]}" "$(fs_read "$u" "$MNT/sys/resident.txt")"
    record "nfs-sys $u resident write" "${MAY_WRITE[$u]}" "$(fs_write "$u" "$MNT/sys/resident.txt")"
    record "nfs-sys $u create file" "${MAY_WRITE[$u]}" "$(fs_create "$u" "$MNT/sys")"
    # stub, failfast: a permitted read fails fast (EIO) and starts a recall
    # attributed to the user; a denied one never reaches the guard.
    n0=$(intents_for "${UID_OF[$u]}")
    got=$(fs_read "$u" "$MNT/sys/stub-$u.segy")
    n1=$(intents_for "${UID_OF[$u]}")
    if [ "${MAY_READ[$u]}" = OK ]; then
      record "nfs-sys $u stub read (failfast)" EIO "$got"
      record "nfs-sys $u stub recall requested as uid ${UID_OF[$u]}" yes "$([ "$n1" -gt "$n0" ] && echo yes || echo no)"
    else
      record "nfs-sys $u stub read (failfast)" DENIED "$got"
      record "nfs-sys $u stub: no recall" yes "$([ "$n1" -eq "$n0" ] && echo yes || echo no)"
    fi
    record "nfs-sys $u stub write" "$([ "${MAY_WRITE[$u]}" = OK ] && echo EIO || echo DENIED)" "$(fs_write "$u" "$MNT/sys/stub-$u.segy")"
    # recalling, wait: the permitted read waits through the recall and gets
    # the right bytes.
    got=$(fs_read "$u" "$MNT/wait/rec-$u.segy")
    record "nfs-sys $u recalling read (wait)" "${MAY_READ[$u]}" "$got"
    if [ "$got" = OK ]; then
      local fid
      fid=$(getfattr --only-values -n trusted.omnifile "$SHARE/rec-$u.segy" 2>/dev/null | python3 -c 'import sys,struct;b=sys.stdin.buffer.read();print(struct.unpack("<Q",b[8:16])[0])' 2>/dev/null)
      record "nfs-sys $u recalled bytes correct" yes "$(cmp -s "$SHARE/rec-$u.segy" "$BACKEND/$fid" && echo yes || echo no)"
    fi
  done
  # Claimed groups are ignored: geo-none claims Owners, Writers, Readers.
  record "nfs-sys geo-none claiming Owners/Writers/Readers gids: read" DENIED \
    "$(fs_read geo-none "$MNT/sys/resident.txt" --groups 30003,30002,30001)"
  # Root is squashed on a host-trusted export.
  local err rc
  err=$(timeout "$T" dd if="$MNT/sys/resident.txt" of=/dev/null status=none 2>&1); rc=$?
  record "nfs-sys root (Root_Squash) read" DENIED "$(classify $rc "$err")"
  # Ganesha's POSIX->NFSv4 ACL mapping adds DENY GROUP@ for rights `other`
  # has and `group::` lacks, before the named-group ALLOWs. Every AD user is
  # in the owning group (Domain Users), so a chmod that opens `other` (cp -p,
  # tar, rsync -p of a 0644 file) locks named-group members out over NFS,
  # while the kernel (local, SMB) still lets them in. The edge normalises
  # (access-control.md §1.1, omnifile_acl::normalize): other::---, mask =
  # the group class.
  cp -p "$SHARE/resident.txt" "$SHARE/chmod644.txt"; chmod 644 "$SHARE/chmod644.txt"
  local k; k=$(as geo-reader --groups 10000,30001 dd if="$SHARE/chmod644.txt" of=/dev/null status=none 2>/dev/null && echo OK || echo DENIED)
  finding "chmod 644, not normalised: geo-reader kernel vs NFS (Ganesha)" "$k" "$(fs_read geo-reader "$MNT/sys/chmod644.txt")"
  record "normalise: omniacl normalize repairs the chmod'ed file" "normalized=1" "$("$OMNIACL" normalize "$SHARE/chmod644.txt")"
  sleep "${ATTR_SETTLE:-12}"
  record "normalise: geo-reader reads chmod644.txt over NFS" OK "$(fs_read geo-reader "$MNT/sys/chmod644.txt")"
  record "normalise: geo-writer writes chmod644.txt over NFS (mask restored)" OK "$(fs_write geo-writer "$MNT/sys/chmod644.txt")"
  record "normalise: geo-none still denied" DENIED "$(fs_read geo-none "$MNT/sys/chmod644.txt")"
  record "normalise: other::--- after repair" "other::---" "$(getfacl -cpn "$SHARE/chmod644.txt" | grep '^other')"

  log "NFS sec=krb5p (proven)"
  if mount_nfs proj-krb "$MNT/krb" krb5p 2>"$RUN/mount-krb.err"; then
    for u in "${USERS[@]}"; do
      record "nfs-krb5p $u resident read" "${MAY_READ[$u]}" "$(fs_read "$u" "$MNT/krb/resident.txt")"
      record "nfs-krb5p $u resident write" "${MAY_WRITE[$u]}" "$(fs_write "$u" "$MNT/krb/resident.txt")"
      got=$(fs_read "$u" "$MNT/krb/krb-$u.segy")
      record "nfs-krb5p $u recalling read (wait)" "${MAY_READ[$u]}" "$got"
    done
    # What FSAL_OMNI reported for the krb5p recalls (fsal-socket.md §4.2).
    local seen
    seen=$(python3 - "$DLOG" <<'EOF2'
import json, sys
secs, princ = set(), set()
for l in open(sys.argv[1]):
    try: m = json.loads(l)
    except Exception: continue
    i = m.get("ident") or {}
    if m.get("type") == "READ_INTENT" and i.get("uid") == 20001 and i.get("sec", 0) >= 2:
        secs.add(i["sec"]); princ.add(m.get("principal", ""))
print(",".join(map(str, sorted(secs))) or "none", "|".join(sorted(princ)) or "empty")
EOF2
)
    record "nfs-krb5p recall from geo-reader reported as Kerberos (proven)" yes "$([ "${seen%% *}" != none ] && echo yes || echo no)"
    record "nfs-krb5p service level in identity.sec (4=krb5p)" 4 "${seen%% *}"
    record "nfs-krb5p principal in READ_INTENT" "geo-reader@OMNI.TEST" "${seen#* }"
  else
    pending "nfs-krb5p (all cells)" "mount failed: $(tr '\n' ' ' <"$RUN/mount-krb.err")"
  fi
}

smb() { # user command...: smbclient as the user; the output, then "__rc=<exit status>"
  local u=$1 out rc; shift
  out=$(timeout -s KILL "$T" smbclient "//$FQDN/proj" -A "/root/acl-auth/$u" "$@" 2>&1); rc=$?
  printf '%s\n__rc=%s\n' "$out" "$rc"
}
smb_rc() { sed -n 's/^__rc=//p' <<<"$1" | tail -n 1; }
smb_class_io() { # output -> OK/DENIED/EIO/TIMEOUT/ERR (a guard denial is an I/O error)
  local rc; rc=$(smb_rc "$1")
  if [ "$rc" = 124 ] || [ "$rc" = 137 ]; then echo TIMEOUT
  elif grep -qiE 'NT_STATUS_ACCESS_DENIED|NT_STATUS_BAD_NETWORK_NAME|NT_STATUS_OBJECT_NAME_NOT_FOUND' <<<"$1"; then echo DENIED
  elif grep -qE 'NT_STATUS_(IO_DEVICE_ERROR|UNEXPECTED_IO_ERROR|DATA_ERROR|FILE_IS_OFFLINE|INVALID_DEVICE_REQUEST|IO_)' <<<"$1"; then echo EIO
  elif grep -q 'NT_STATUS_' <<<"$1" || [ "${rc:-1}" != 0 ]; then printf '%s smb-io: %s\n' "$(date +%T)" "$1" >>"$RUN/errors.log"; echo "ERR"
  else echo OK; fi
}
smb_class() { # output -> OK/DENIED/TIMEOUT/ERR; OK needs exit status 0
  local rc; rc=$(smb_rc "$1")
  if [ "$rc" = 124 ] || [ "$rc" = 137 ]; then echo TIMEOUT
  elif grep -qiE 'NT_STATUS_ACCESS_DENIED|NT_STATUS_BAD_NETWORK_NAME|NT_STATUS_OBJECT_NAME_NOT_FOUND' <<<"$1"; then echo DENIED
  elif grep -q 'NT_STATUS_' <<<"$1" || [ "${rc:-1}" != 0 ]; then printf '%s smb: %s\n' "$(date +%T)" "$1" >>"$RUN/errors.log"; echo "ERR"
  else echo OK; fi
}

# The smb-guard on /srv/acl for the smb phase (see the header).
GUARD_STATE=$RUN/guard
GUARD_PID=
guard_bin() {
  if [ -n "${SMBGUARD_BIN:-}" ]; then echo "$SMBGUARD_BIN"; return; fi
  local u=${SUDO_USER:-}
  [ -n "$u" ] && [ "$u" != root ] || return 1
  sudo -u "$u" -H bash -lc "cd /work/edge && CARGO_TARGET_DIR=$RUN/target cargo build --release -q -p omnifile-smbguard --bin omnifile-smbguard-harness" >&2 || return 1
  echo "$RUN/target/release"
}
guard_start() {
  local bin uuid
  bin=$(guard_bin) || return 1
  [ -x "$bin/omnifile-smbguard-harness" ] || return 1
  uuid=$(blkid -c /dev/null -s UUID -o value "$(findmnt -n -o SOURCE --mountpoint /srv/acl)")
  rm -rf "$GUARD_STATE"; install -d "$GUARD_STATE"
  echo failfast >"$GUARD_STATE/policy"
  : >"$GUARD_STATE/fail" # fills fail: the stubs' bytes are never touched here
  setsid "$bin/omnifile-smbguard-harness" --data /srv/acl --uuid "$uuid" --fill-mount /run/acl-guard-fill \
    --private-bind --state "$GUARD_STATE" --no-fdstore >"$RUN/guard.log" 2>&1 </dev/null &
  GUARD_PID=$!
  for _ in $(seq 1 50); do grep -q 'harness running' "$RUN/guard.log" && break; sleep 0.2; done
  grep -q 'harness running' "$RUN/guard.log" || { tail -5 "$RUN/guard.log" >&2; kill "$GUARD_PID"; GUARD_PID=; return 1; }
  # smbd must open the files after the group exists (spike A9).
  systemctl restart smbd
}
guard_stop() { [ -n "$GUARD_PID" ] && kill "$GUARD_PID" 2>/dev/null; GUARD_PID=; }

phase_smb() {
  log "SMB (smbclient, NTLMv2 against the DC)"
  local u out guard=0 n0 n1
  if guard_start; then guard=1; log "smb-guard (production library, failfast) armed on /srv/acl"; fi
  for u in "${USERS[@]}"; do
    out=$(smb "$u" -c 'get resident.txt /dev/null'); record "smb-client $u resident read" "${MAY_READ[$u]}" "$(smb_class "$out")"
    out=$(smb "$u" -c "put /etc/hostname smb-new-$u.txt"); record "smb-client $u create file" "${MAY_WRITE[$u]}" "$(smb_class "$out")"
    # Opening a stub is a permission check; its read is the smb-guard's.
    out=$(smb "$u" -c "open smbstub-$u.segy"); record "smb-client $u stub open" "${MAY_READ[$u]}" "$(smb_class "$out")"
    if [ $guard = 1 ]; then
      n0=$(grep -c "path=.*/smbstub-$u.segy" "$GUARD_STATE/recalls.log" 2>/dev/null)
      out=$(smb "$u" -c "get smbstub-$u.segy $RUN/smbstub-$u.got")
      n1=$(grep -c "path=.*/smbstub-$u.segy" "$GUARD_STATE/recalls.log" 2>/dev/null)
      if [ "${MAY_READ[$u]}" = OK ]; then
        record "smb $u stub read (smb-guard, failfast)" EIO "$(smb_class_io "$out")"
        record "smb $u stub recall requested by smbd" yes "$([ "${n1:-0}" -gt "${n0:-0}" ] && grep "path=.*/smbstub-$u.segy" "$GUARD_STATE/recalls.log" | tail -1 | grep -q 'comm=smbd' && echo yes || echo no)"
        record "smb $u stub read: no zeros kept" yes "$(! [ -s "$RUN/smbstub-$u.got" ] || ! cmp -s <(head -c 1048576 /dev/zero) <(tail -c +4097 "$RUN/smbstub-$u.got" | head -c 1048576) && echo yes || echo no)"
      else
        record "smb $u stub read (smb-guard, failfast)" DENIED "$(smb_class_io "$out")"
        record "smb $u stub: no recall" yes "$([ "${n1:-0}" -eq "${n0:-0}" ] && echo yes || echo no)"
      fi
    else
      pending "smb $u stub/recalling read" "no omnifile-smbguard-harness (set SMBGUARD_BIN or install cargo)"
    fi
  done
  guard_stop
  [ $guard = 1 ] && systemctl restart smbd
  # Access-based enumeration: hidden share for non-members, hidden files for non-readers.
  out=$(timeout "$T" smbclient -L "//$FQDN" -A /root/acl-auth/geo-none 2>&1)
  record "smb geo-none: share 'proj' listed (access based share enum)" no "$(grep -qE '^\s+proj\s' <<<"$out" && echo yes || echo no)"
  out=$(timeout "$T" smbclient -L "//$FQDN" -A /root/acl-auth/geo-reader 2>&1)
  record "smb geo-reader: share 'proj' listed" yes "$(grep -qE '^\s+proj\s' <<<"$out" && echo yes || echo no)"
  out=$(smb geo-reader -c ls); record "smb geo-reader: owners-only.txt listed (hide unreadable)" no "$(grep -q owners-only <<<"$out" && echo yes || echo no)"
  out=$(smb geo-owner -c ls); record "smb geo-owner: owners-only.txt listed" yes "$(grep -q owners-only <<<"$out" && echo yes || echo no)"

  log "SMB (CIFS kernel mounts, sec=krb5, one per user)"
  for u in "${USERS[@]}"; do
    local m="$MNT/cifs-$u"; install -d "$m"
    umount -l "$m" 2>/dev/null
    if mount -t cifs "//$FQDN/proj" "$m" -o "sec=krb5,cruid=${UID_OF[$u]},uid=${UID_OF[$u]},noperm,cache=none,vers=3.1.1" 2>"$RUN/cifs-$u.err"; then
      record "smb-cifs $u mount" "${MAY_READ[$u]}" OK
      record "smb-cifs $u resident read" "${MAY_READ[$u]}" "$(fs_read "$u" "$m/resident.txt")"
      record "smb-cifs $u create file" "${MAY_WRITE[$u]}" "$(fs_create "$u" "$m")"
      umount -l "$m"
    else
      record "smb-cifs $u mount" "${MAY_READ[$u]}" "$(grep -qiE 'permission denied|access denied|error\(13\)' "$RUN/cifs-$u.err" && echo DENIED || echo ERR)"
    fi
  done
}

phase_explorer() {
  log "Explorer-style ACL edits over SMB (smbcacls), enforced over NFS"
  mount_nfs proj-sys "$MNT/sys" sys
  local out f=explorer.txt readers_ace
  cacls() { # output, then "__rc=<exit status>" (smb_class checks it)
    local out rc
    out=$(timeout -s KILL "$T" smbcacls "//$FQDN/proj" "$f" -A /root/acl-auth/geo-owner "$@" 2>&1); rc=$?
    printf '%s\n__rc=%s\n' "$out" "$rc"
  }
  rm -f "$SHARE/$f"
  smb geo-owner -c "put /etc/hostname $f" >/dev/null
  # Edits made over SMB reach NFS within Ganesha's Attr_Expiration_Time
  # (omnifile_acl::config::ATTR_EXPIRATION_S = 10 s).
  local settle=${ATTR_SETTLE:-12}
  record "explorer: geo-reader reads $f over NFS (inherited Readers)" OK "$(fs_read geo-reader "$MNT/sys/$f")"
  cacls >"$RUN/explorer-ntacl-initial.txt"
  readers_ace=$(grep -i '^ACL:OMNI\\proj-readers:' "$RUN/explorer-ntacl-initial.txt" | head -1)
  out=$(cacls -D "$readers_ace")
  record "explorer: geo-owner removes the Proj-Readers ACE (smbcacls -D)" OK "$(smb_class "$out")"
  sleep "$settle"
  record "explorer: geo-reader reads $f over NFS after removal" DENIED "$(fs_read geo-reader "$MNT/sys/$f")"
  record "explorer: geo-nested reads $f over NFS after removal" DENIED "$(fs_read geo-nested "$MNT/sys/$f")"
  record "explorer: geo-writer still reads $f over NFS" OK "$(fs_read geo-writer "$MNT/sys/$f")"
  out=$(cacls -a 'ACL:OMNI\geo-reader:ALLOWED/0x0/READ')
  record "explorer: geo-owner grants geo-reader READ (smbcacls -a)" OK "$(smb_class "$out")"
  sleep "$settle"
  getfacl -cpn "$SHARE/$f" >"$RUN/explorer-after.acl"
  record "explorer: grant stored as POSIX entry user:20001" yes "$(grep -q '^user:20001:r' "$RUN/explorer-after.acl" && echo yes || echo no)"
  record "explorer: geo-reader reads $f over NFS after the grant" OK "$(fs_read geo-reader "$MNT/sys/$f")"
  record "explorer: geo-nested still denied over NFS" DENIED "$(fs_read geo-nested "$MNT/sys/$f")"
  record "explorer: no security.NTACL written" absent "$(getfattr -n security.NTACL "$SHARE/$f" >/dev/null 2>&1 && echo present || echo absent)"
  "$OMNIACL" scan "$SHARE" "$RUN/scan-state.json" >"$RUN/scan-explorer.json"
  record "explorer: drift scan reports $f changed" yes "$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print("yes" if any(c["path"].endswith("/explorer.txt") for c in d["changed"]) else "no")' "$RUN/scan-explorer.json")"
  record "explorer: share root still clean" "clean=true" "$("$OMNIACL" check-root "$SHARE" 30003 30002 30001 | cut -d' ' -f1)"
  record "explorer: scan findings (security.NTACL)" "[]" "$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1]))["findings"]))' "$RUN/scan-explorer.json")"
}

phase_identity() {
  log "identity: SMB pid -> session -> SID; NFS uid -> SID"
  local m="$MNT/cifs-id" pid out
  install -d "$m"; umount -l "$m" 2>/dev/null
  mount -t cifs "//$FQDN/proj" "$m" -o "sec=krb5,cruid=20006,uid=20006,noperm,cache=none,vers=3.1.1" || { pending identity "cifs mount failed"; return; }
  as geo-nested cat "$m/resident.txt" >/dev/null
  smbstatus --processes --json >"$RUN/smbstatus.json" 2>/dev/null
  pid=$(ss -tnpH state established '( sport = :445 )' | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)
  out=$("$OMNIACL" resolve-smb "$pid")
  echo "    smbd pid $pid -> $out"
  record "identity: smbd pid -> geo-nested SID, proven" yes \
    "$(grep -q '"sid":"S-1-5-21-[0-9-]*-1113".*"via":"smb".*"proof":"proven"' <<<"$out" && echo yes || echo no)"
  umount -l "$m"
  sleep 1
  out=$("$OMNIACL" resolve-smb "$pid")
  record "identity: same pid after disconnect is not attributed" yes "$(grep -q '"sid"' <<<"$out" && echo no || echo yes)"
  out=$("$OMNIACL" resolve-nfs 20001 sys 10.1.2.3 2>/dev/null)
  record "identity: NFS sys uid 20001 -> SID, claimed" yes "$(grep -q '"sid":"S-1-5-21-[0-9-]*-1108".*"proof":"claimed"' <<<"$out" && echo yes || echo no)"
  out=$("$OMNIACL" resolve-nfs 20001 krb5p 2>/dev/null)
  record "identity: NFS krb5p uid 20001 -> SID, proven" yes "$(grep -q '"proof":"proven"' <<<"$out" && echo yes || echo no)"
}

phase_acls() {
  log "ACLs unchanged by stub and recall"
  # Every stub/rec/krb file that was recalled (filled) or left as a stub keeps
  # the ACL it had at setup.
  ( cd "$SHARE" && for f in *; do case "$f" in stub-*|rec-*|krb-*|smbstub-*) printf '%s ' "$f"; getfacl -cpn "$f" | tr '\n' ' '; echo;; esac; done ) >"$RUN/acl-after.txt"
  local diffs
  diffs=$(grep -E '^(stub-|rec-|krb-|smbstub-)' "$RUN/acl-before.txt" | diff - "$RUN/acl-after.txt" | grep -c '^[<>]')
  record "acls: stub/recalled files' ACLs identical to setup" 0 "$diffs"
  local filled=0 f
  for f in "$SHARE"/rec-*.segy; do
    st=$(getfattr --only-values -n trusted.omnifile "$f" 2>/dev/null | python3 -c 'import sys;b=sys.stdin.buffer.read();print(b[5] if len(b)>5 else -1)')
    [ "$st" != -1 ] && filled=$((filled + 1))
  done
  echo "    (recall files with markers: $filled)"
}

phase_revoke() {
  # The AD change runs on omnifile-dc, so this phase is driven from the Mac:
  die "run tests/acl/revoke.sh on the Mac (it drives omnifile-dc and this VM)"
}

phase_down() {
  umount -fl "$MNT"/* 2>/dev/null
  pkill -x ganesha.nfsd; pkill -f fake_daemon.py; systemctl stop smbd
  log "down"
}

: >"$RES"
phases=("$@"); [ ${#phases[@]} -gt 0 ] || phases=(setup servers tickets nfs smb explorer identity acls)
for p in "${phases[@]}"; do "phase_$p"; done
echo
awk -F'\t' '{c[$4 ~ /^PENDING/ ? "PENDING" : $4]++} END {for (k in c) printf "%s=%d ", k, c[k]; print ""}' "$RES"
