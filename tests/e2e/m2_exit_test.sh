#!/usr/bin/env bash
# M2 exit test (docs/BUILD-PLAN.md M2, docs/milestones/M2.md), on the edge
# machine itself, as root, against the dev AD: the integrated daemon (smb-guard
# + AD identity inside omnifile-edge), Samba and Ganesha under their real units.
#
#   tests/e2e/run.sh --m2          from the Mac (installs, then runs this)
#
# Needs, installed beforehand: install-ganesha.sh, install-edge.sh (daemon, the
# smb-guard drop-ins, omniacl), install-service.sh, install-samba.sh (smb.conf
# from the product template, nfs/ keytab), and dev/.secrets/ad.env.
#
# What it proves (each line of the results file is one check):
#   1. the daemon arms the smb-guard first thing (fd store, READY, private fill
#      mount) under its hardened unit; smbd and Ganesha are bound to it;
#   2. shares, exports and their open policy are rendered from the catalog
#      (omniacl), not written by hand; share roots carry the AD-group ACL;
#   3. files written over SMB and NFS are committed, stubbed, then archived;
#   4. SMB failfast: a read of an archived stub fails at once (an NT_STATUS
#      error, never zeros) and the recall is attributed to the AD user's SID,
#      proven; SMB wait: the read waits, then returns the right bytes once the
#      recall is filled (smbclient, and a Kerberos CIFS mount as geo-reader);
#      a user outside the share's groups is denied and starts no recall;
#   5. NFS failfast (Policy from the catalog's on_open=fail): an archived stub
#      fails with EIO at once over NFSv3, 4.1 and 4.2, while the wait export
#      blocks and then returns the right bytes;
#   6. kill -9 of the daemon (and so the guard) in the middle of SMB reads,
#      three times: smbd keeps its pid, no reader sees zeros, bytes identical;
#      the online fetch is reported and the catalog says resident;
#   7. the daemon held down (stopped, not restarted): SMB, local and NFS
#      (v3, 4.1, 4.2) readers of an unfilled range wait (or fail), never read
#      zeros; released, they finish with the right bytes;
#   8. a Kerberos NFS mount (sec=krb5p) as geo-reader works against the dev AD,
#      and its recall is attributed to geo-reader, proven;
#   9. a flood of local readers of one archived stub through the integrated
#      daemon (E2E_FLOOD, default 2000) all wait and all read the right bytes;
#      local readers are reported as via=local.
#
# Environment: E2E_RESULTS, E2E_REV / E2E_HEAD (commit, for the results),
# E2E_MIB (default 32), E2E_FLOOD (default 2000), E2E_KEEP=1.
set -uo pipefail
if [ -z "${E2E_COPY:-}" ]; then
  copy=$(mktemp /tmp/omnifile-m2-XXXXXX.sh)
  cp "$0" "$copy"
  E2E_COPY=1 E2E_SELF_SHA=$(sha256sum "$0" | cut -d' ' -f1) \
    E2E_HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) exec bash "$copy" "$@"
fi

HERE=${E2E_HERE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
REPO=$(cd "$HERE/../.." && pwd)
PY=/opt/omnifile-service/venv/bin/python
E2E="$PY $HERE/e2e.py"
DATA=/srv/omnifile/data
MNT=/mnt/omnifile-m2
WORK=/var/tmp/omnifile-m2
# compare/flood as an unprivileged user: the system python3 (stdlib only).
# Run from a world-readable copy in $WORK (made below): the repo may sit under a
# home the readers cannot traverse, as on GitHub runners where /home/runner is 0750.
E2EU="python3 $WORK/e2e.py"
RESULTS=${E2E_RESULTS:-$HERE/results/m2-latest.md}
MIB=${E2E_MIB:-32}
FLOOD=${E2E_FLOOD:-2000}
ENV_FILE=${AD_ENV_FILE:-/work/dev/.secrets/ad.env}
OMNIACL=/usr/local/bin/omniacl
FQDN=edge1.omni.test
RESIDENT=$(( (3600 + 1048576 + 4095) / 4096 * 4096 )) # headers + 1 MiB, page-aligned by the edge
declare -A UID_OF=([geo-reader]=20001 [geo-writer]=20002 [geo-none]=20003 [geo-owner]=20005 [geo-nested]=20006)
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
say() { printf '[m2 %4ss] %s\n' "$(($(date +%s) - T0))" "$*"; }
pass() { say "PASS: $*"; echo "| PASS | $* |" >>"$WORK/checks.txt"; NPASS=$((NPASS + 1)); }
fail() { say "FAIL: $*"; echo "| **FAIL** | $* |" >>"$WORK/checks.txt"; NFAIL=$((NFAIL + 1)); }
check() { local d=$1; shift; if "$@"; then pass "$d"; else fail "$d"; fi; }
j() { "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); v=eval(sys.argv[2]); print(v if not isinstance(v,(dict,list)) else json.dumps(v))' "$1" "$2"; }
jt() { "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if eval(sys.argv[2]) else 1)' "$1" "$2"; }
wait_for() {
  local t=$1; shift
  local end=$(($(date +%s) + t))
  while [ "$(date +%s)" -lt "$end" ]; do "$@" && return 0; sleep 0.5; done
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
edge_pid() { systemctl show -p MainPID --value omnifile-edge; }
smbd_pid() { systemctl show -p MainPID --value smbd; }
ganesha_pid() { systemctl show -p MainPID --value nfs-ganesha-omni; }
# as <user> cmd...: the user's uid, Domain Users, the user's Kerberos cache.
as() {
  local u=$1; shift
  setpriv --reuid="${UID_OF[$u]}" --regid=10000 --clear-groups --inh-caps=-all -- \
    env KRB5CCNAME="FILE:/tmp/krb5cc_${UID_OF[$u]}" HOME=/tmp "$@"
}
smb() { # user share command
  # -t: smbclient's own per-request timeout (default 20 s) must outlast a held read.
  timeout -s KILL "${SMB_T:-60}" smbclient "//$FQDN/$2" -A "/root/m2-auth/$1" -t "${SMB_T:-60}" -c "$3" 2>&1
}

umount_all() {
  local m
  for m in $(findmnt -rn -o TARGET | grep "^$MNT/" | sort -r); do
    timeout 20 umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || true
  done
}
release_hold() {
  rm -f /run/omnifile-e2e-hold /run/systemd/system/omnifile-edge.service.d/99-e2e-hold.conf
  systemctl daemon-reload
}
cleanup() {
  release_hold
  [ "${E2E_KEEP:-0}" = 1 ] && return
  umount_all
  $E2E fault '{"clear": true}' >/dev/null 2>&1
  # Never leave stubbing off for the next lane: the drop-in is in /run until a reboot.
  worker_stub_now
}
trap cleanup EXIT
# So an interrupt goes through `cleanup`, with the status a signal gives (as m5_exit_test.sh).
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------------------------
say "0. reset: services, catalog, sim, edge state"
umount_all
release_hold
systemctl stop smbd nfs-ganesha-omni omnifile-edge omnifile-dev-worker 2>/dev/null
# Release the smb-guard's group (`omnifile-guardctl clean`): it outlives the daemon in the fd
# store, and its marks on unfilled rewind directories would hold the rm below.
systemctl clean --what=fdstore omnifile-edge 2>/dev/null
rm -rf /var/lib/omnifile/replica.db* /var/lib/omnifile/identity /var/lib/omnifile/outbox
install -d -m 0700 /var/lib/omnifile/identity /var/lib/omnifile/outbox
for s in wait failfast smbwait smbfail krb; do rm -rf "${DATA:?}/$s"; done
rm -rf "${DATA:?}/.omni"
$E2E reset-db >/dev/null || { fail "catalog reset"; exit 1; }
rm -f /var/lib/omnifile-dev/sim-key.json
systemctl restart omnifile-dev-sim omnifile-dev-enrol
systemctl restart omnifile-dev-api
wait_for 30 test -s /var/lib/omnifile-dev/sim-key.json
wait_for 30 bash -c '(exec 3<>/dev/tcp/127.0.0.1/8443) 2>/dev/null && (exec 3<>/dev/tcp/127.0.0.1/8444) 2>/dev/null'

say "1. AD: groups, tickets, SMB auth files"
dom=$(wbinfo -n proj-owners | cut -d' ' -f1); dom=${dom%-*}
OWN=$(wbinfo -n proj-owners | cut -d' ' -f1)
WRI=$(wbinfo -n proj-writers | cut -d' ' -f1)
REA=$(wbinfo -n proj-readers | cut -d' ' -f1)
declare -A SID_OF
for u in "${!UID_OF[@]}"; do SID_OF[$u]=$(wbinfo -n "$u" | cut -d' ' -f1); done
check "winbind resolves the share groups and users (Proj-Owners $OWN, geo-reader ${SID_OF[geo-reader]})" \
  test -n "$OWN" -a -n "$WRI" -a -n "$REA" -a -n "${SID_OF[geo-reader]}"
set -a; . "$ENV_FILE"; set +a
umask 077; install -d -m 700 /root/m2-auth
for u in "${!UID_OF[@]}"; do
  var="AD_PASSWORD_$(printf '%s' "$u" | tr 'a-z-' 'A-Z_')"
  printf 'username = %s\npassword = %s\ndomain = OMNI\n' "$u" "${!var}" >"/root/m2-auth/$u"
  rm -f "/tmp/krb5cc_${UID_OF[$u]}"
  printf '%s\n' "${!var}" | as "$u" kinit "$u@OMNI.TEST" >/dev/null 2>&1 || fail "kinit $u"
done
umask 022
unset AD_ADMIN_PASSWORD

say "2. seed the catalog (NFS sys, SMB and krb5 shares) and enrol"
ip=$(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -1)
seed=$($E2E seed --enrol-url https://127.0.0.1:8444 --server-ca /etc/omnifile-dev/pki/ca.crt \
  --bundle-out "$WORK/bundle.txt" --uid 20002 --gid 10000 --m2 \
  --owners-sid "$OWN" --writers-sid "$WRI" --readers-sid "$REA" --nfs-client "$ip") \
  || { fail "seed"; exit 1; }
say "seeded: $seed"
if /usr/local/bin/omnifile-edge --config /etc/omnifile/edge.toml enrol "$WORK/bundle.txt" >"$WORK/enrol.out" 2>&1; then
  pass "enrolment: $(grep '^enrolled:' "$WORK/enrol.out")"
else
  fail "enrolment: $(tail -3 "$WORK/enrol.out")"; exit 1
fi
rm -f "$WORK/bundle.txt"

say "3. the daemon arms the smb-guard first"
nstore0=$(systemctl show -p NFileDescriptorStore --value omnifile-edge)
systemctl start omnifile-edge
check "omnifile-edge active (Type=notify: READY only once the guard is armed)" wait_for 30 systemctl is-active -q omnifile-edge
check "the guard's fanotify group is in the unit's fd store (was $nstore0)" \
  bash -c "[ \"\$(systemctl show -p NFileDescriptorStore --value omnifile-edge)\" -ge 1 ]"
check "guard armed on the data volume, private fill mount verified, bound to the daemon" wait_for 15 bash -c \
  "journalctl -q -o cat -u omnifile-edge --since @$T0 | grep -q 'private fill mount verified' && journalctl -q -o cat -u omnifile-edge --since @$T0 | grep -q 'smb-guard bound to the daemon'"
epid=$(edge_pid)
capbnd=$(awk '/^CapBnd/ {print $2}' "/proc/$epid/status")
nnp=$(awk '/^NoNewPrivs/ {print $2}' "/proc/$epid/status")
cap_has() { (( (16#$1 >> $2) & 1 )); }
if cap_has "$capbnd" 21 && ! cap_has "$capbnd" 16 && ! cap_has "$capbnd" 17 && ! cap_has "$capbnd" 6 \
   && ! cap_has "$capbnd" 7 && ! cap_has "$capbnd" 12 && ! cap_has "$capbnd" 27 && ! cap_has "$capbnd" 5 \
   && [ "$nnp" = 1 ] && grep -q '^Seccomp:[[:space:]]*2' "/proc/$epid/status"; then
  pass "daemon confined: CapBnd=$capbnd (SYS_ADMIN, DAC_*, FOWNER, CHOWN, FSETID, SYS_PTRACE, LEASE; no KILL/SYS_MODULE/RAWIO/SETUID/SETGID/NET_ADMIN/MKNOD), NoNewPrivs=1, seccomp filter on (no @mount/@debug/@module/@raw-io)"
else
  fail "daemon capabilities CapBnd=$capbnd NoNewPrivs=$nnp"
fi
check "share roots materialised (wait failfast smbwait smbfail krb)" wait_for 60 bash -c \
  "for s in wait failfast smbwait smbfail krb; do test -d $DATA/\$s || exit 1; done"
for s in smbwait smbfail krb; do
  "$OMNIACL" apply-root "$DATA/$s" 30003 30002 30001 >"$WORK/root-$s.txt" || fail "apply-root $s"
done
check "AD-group ACL on the proven shares' roots (Owners rwx, Writers rwx, Readers r-x, default ACL)" bash -c \
  "getfacl -cpn $DATA/smbwait | grep -q '^group:30001:r-x' && getfacl -cpn $DATA/smbwait | grep -q '^default:group:30002:rwx'"

say "4. render Samba shares and Ganesha exports from the catalog"
shares=$($E2E shares)
: >"$WORK/shares-smb.conf"; : >"$WORK/exports.conf"
eid=20
for name in $(j "$shares" '" ".join(s["name"] for s in d["shares"])'); do
  row=$(j "$shares" "[s for s in d['shares'] if s['name'] == '$name'][0]")
  oo=$(j "$row" 'd["on_open"]')
  case "$oo" in
    wait) pol=wait ;; fail) pol=failfast ;;
    auto) pol="waitquick:$(( $(j "$row" 'd["wait_threshold_minutes"] or 0') * 60000 ))" ;;
  esac
  protos=$(j "$row" '",".join("nfs3" if p=="nfs3" else "nfs4" if p=="nfs4" else "smb" for p in d["protocols"])')
  secs=$(j "$row" '",".join(d["nfs_sec"]) or "-"')
  clients=$(j "$row" '",".join(d["nfs_clients"]) or "-"')
  wri=$(j "$row" 'd["writers_group_sid"] or "-"'); rea=$(j "$row" 'd["readers_group_sid"] or "-"')
  eid=$((eid + 1))
  if "$OMNIACL" render "$name" "$DATA/$name" "$eid" "$protos" "$secs" "$clients" \
      "$(j "$row" 'd["owners_group_sid"]')" "$wri" "$rea" "$pol" >"$WORK/render-$name.txt" 2>&1; then
    sed -n '/^#--- smb/,/^#--- ganesha/p' "$WORK/render-$name.txt" | grep -v '^#---' >>"$WORK/shares-smb.conf"
    sed -n '/^#--- ganesha/,$p' "$WORK/render-$name.txt" | grep -v '^#---' >>"$WORK/exports.conf"
    say "share $name: on_open=$oo -> policy $pol, protocols $protos, sec $secs"
  else
    fail "render $name: $(cat "$WORK/render-$name.txt")"
  fi
done
check "every export's FSAL Policy comes from the catalog's on_open (failfast: Policy = \"failfast\")" \
  bash -c "grep -A12 'Pseudo = \"/failfast\"' $WORK/exports.conf | grep -q 'Policy = \"failfast\"' && grep -A12 'Pseudo = \"/wait\"' $WORK/exports.conf | grep -q 'Policy = \"wait\"'"
install -m 0644 "$WORK/shares-smb.conf" /etc/samba/omnifile-shares.conf
install -m 0644 "$WORK/exports.conf" /etc/ganesha/omnifile-exports.conf
sed -e "s#@NFS_DOMAIN@#omni.test#g" -e "s#@KEYTAB@#/etc/krb5.keytab#g" -e "s#@FSAL_SOCKET@#/run/omnifile/fsal.sock#g" \
  "$REPO/edge/config/ganesha/ganesha.conf.tmpl" >/etc/ganesha/ganesha.conf
check "smb.conf parses and loads no vfs_acl_xattr" bash -c "testparm -s >$WORK/testparm.txt 2>&1 && ! grep -qi acl_xattr $WORK/testparm.txt"

say "5. start Ganesha and smbd (both bound to the guard unit)"
G0=$(date +%s)
systemctl start nfs-ganesha-omni smbd
check "nfs-ganesha-omni and smbd active" wait_for 30 bash -c 'systemctl is-active -q nfs-ganesha-omni && systemctl is-active -q smbd'
check "smbd and Ganesha BindsTo= the daemon" bash -c \
  "systemctl show -p BindsTo --value smbd | grep -q omnifile-edge && systemctl show -p BindsTo --value nfs-ganesha-omni | grep -q omnifile-edge"
check "FSAL_OMNI connected" wait_for 30 bash -c "journalctl -q -o cat -u nfs-ganesha-omni --since @$G0 | grep -q 'connected to the OmniFile daemon'"
wait_for 120 bash -c "journalctl -q -o cat -u nfs-ganesha-omni --since @$G0 | grep -q 'NOT IN GRACE'" || true

nfs_mount() { # export version mountpoint [sec]
  install -d "$3"; mountpoint -q "$3" && return 0
  if [ "$2" = 3 ]; then
    mount -t nfs -o "vers=3,proto=tcp,nolock,hard,timeo=50" "127.0.0.1:$DATA/$1" "$3"
  else
    mount -t nfs -o "vers=$2,proto=tcp,hard,timeo=50,sec=${4:-sys}" "${5:-127.0.0.1}:/$1" "$3"
  fi
}
for v in 3 4.1 4.2; do
  for s in wait failfast; do nfs_mount "$s" "$v" "$MNT/$s-v${v//./}" || fail "mount $s v$v"; done
done

# ---------------------------------------------------------------------------------
say "6. write files over SMB and NFS; commit; stub; archive"
declare -A SRC
mk() { # name seed mib
  SRC[$1]="$WORK/src-${1//\//-}"
  $E2E pattern "${SRC[$1]}" $(($3 * 1048576)) "$2" >/dev/null
}
smb_put() { # user share name
  local out; out=$(smb "$1" "$2" "put ${SRC[$2/$3]} $3")
  if grep -q NT_STATUS <<<"$out"; then fail "SMB put $2/$3 as $1: $out"; else pass "wrote $2/$3 over SMB as $1 (smbclient, NTLMv2 against the DC)"; fi
}
worker_verify_only
FILES=()
n=200
for f in smbfail/a.segy smbwait/a.segy smbwait/b.segy smbwait/kill.segy smbwait/hold.segy smbwait/flood.segy smbfail/nested.segy; do
  n=$((n + 1)); mk "$f" $n "$MIB"; smb_put geo-writer "${f%%/*}" "${f#*/}"; FILES+=("$f")
done
for f in failfast/ff.segy wait/w.segy wait/hold.segy; do
  n=$((n + 1)); mk "$f" $n "$MIB"
  # Both as the writer: root is squashed, and the share root has no `other`.
  setpriv --reuid=20002 --regid=10000 --clear-groups cp "${SRC[$f]}" "$MNT/${f%%/*}-v42/${f#*/}" \
    && setpriv --reuid=20002 --regid=10000 --clear-groups sync -f "$MNT/${f%%/*}-v42/${f#*/}" && pass "wrote $f over NFSv4.2 as geo-writer" || fail "write $f"
  FILES+=("$f")
done
committed() {
  local row; row=$($E2E file "${1%%/*}" "${1#*/}") || return 1
  jt "$row" 'd.get("state") == "resident" and d.get("location_state") == "committed" and d.get("verified")'
}
for f in "${FILES[@]}"; do
  if wait_for 240 committed "$f"; then
    row=$($E2E file "${f%%/*}" "${f#*/}")
    if jt "$row" 'd.get("verified_by") in ("upload_block_crc", "azure_read_crc")'; then
      pass "$f committed; verified by the service ($(j "$row" 'd["verified_by"]'))"
    else
      fail "$f committed but verified_by=$(j "$row" 'd.get("verified_by")')"
    fi
  else
    fail "$f not committed: $($E2E file "${f%%/*}" "${f#*/}")"
  fi
done
check "SMB-written files carry the share's AD-group ACL, other::--- (normalised)" bash -c \
  "getfacl -cpn $DATA/smbwait/a.segy | grep -q '^group:30001:r' && getfacl -cpn $DATA/smbwait/a.segy | grep -q '^other::---'"
stubbed() {
  local row mk; row=$($E2E file "${1%%/*}" "${1#*/}") || return 1
  mk=$($E2E local "$DATA/$1") || return 1
  jt "$row" 'd.get("state") == "stub"' && jt "$mk" "d['marker'] and d['marker']['state'] == 'PARTIAL' and d['marker']['resident'] == $RESIDENT"
}
worker_stub_now
for f in "${FILES[@]}"; do
  wait_for 180 stubbed "$f" && pass "$f stubbed (PARTIAL, $RESIDENT bytes resident)" || fail "$f not stubbed: $($E2E local "$DATA/$f")"
done
systemctl stop omnifile-dev-worker
archived() { local mk; mk=$($E2E local "$DATA/$1"); jt "$mk" "d['marker'] and (d['marker']['flags'] & 2) and not (d['marker']['flags'] & 1)"; }
for f in smbfail/a.segy smbwait/a.segy smbwait/b.segy smbwait/flood.segy smbfail/nested.segy failfast/ff.segy wait/w.segy; do
  $E2E archive "${f%%/*}" "${f#*/}" >/dev/null
done
for f in smbfail/a.segy smbwait/a.segy smbwait/b.segy smbwait/flood.segy smbfail/nested.segy failfast/ff.segy wait/w.segy; do
  wait_for 60 archived "$f" && pass "$f archived: marker ARCHIVED, not ONLINE (from the feed)" || fail "$f marker not ARCHIVED: $($E2E local "$DATA/$f")"
done
sync; echo 3 >/proc/sys/vm/drop_caches

# ---------------------------------------------------------------------------------
say "7. SMB failfast: an archived stub fails at once, attributed to the AD user"
t=$(date +%s%N)
out=$(SMB_T=30 smb geo-reader smbfail "get a.segy $WORK/got-ff.bin")
secs=$(since "$t")
# The guard's EIO reaches an SMB client as NT_STATUS_IO_DEVICE_ERROR (Samba's
# map of EIO); any other status (access denied, a timeout) is a failure here.
if grep -q 'NT_STATUS_IO_DEVICE_ERROR' <<<"$out"; then
  if awk "BEGIN{exit !($secs < 10)}"; then
    pass "SMB read of archived smbfail/a.segy as geo-reader failed in $secs s: $(grep -o 'NT_STATUS_[A-Z_]*' <<<"$out" | head -1) (failfast)"
  else
    fail "SMB failfast read took $secs s: $out"
  fi
else
  fail "SMB failfast read did not fail: $out"
fi
if [ -f "$WORK/got-ff.bin" ] && [ -s "$WORK/got-ff.bin" ]; then
  r=$($E2E compare "$WORK/got-ff.bin" "${SRC[smbfail/a.segy]}")
  jt "$r" 'd["zero_blocks"] == 0' && pass "no zero block in what smbclient kept" || fail "smbclient kept zeros: $r"
fi
rq() { # share name user via proof
  local r; r=$($E2E recall "$1" "$2")
  jt "$r" "any(q.get('sid') == '${SID_OF[$3]}' and q['via'] == '$4' and q['proof'] == '$5' for q in d.get('requesters', []))"
}
check "recall of smbfail/a.segy attributed to geo-reader (${SID_OF[geo-reader]}), via smb, proven" wait_for 20 rq smbfail a.segy geo-reader smb proven
check "a .RECALLING.txt note next to smbfail/a.segy" wait_for 10 test -s "$DATA/smbfail/a.segy.RECALLING.txt"
out=$(SMB_T=30 smb geo-none smbfail "get a.segy /dev/null")
# valid users keeps geo-none out of the share at tree connect.
if grep -qE 'NT_STATUS_(ACCESS_DENIED|BAD_NETWORK_NAME)' <<<"$out"; then
  pass "geo-none (no share group) is denied over SMB: $(grep -o 'NT_STATUS_[A-Z_]*' <<<"$out" | head -1)"
else
  fail "geo-none over SMB: $out"
fi
r=$($E2E recall smbfail a.segy)
check "geo-none started no recall and is not a requester" jt "$r" "not any(q.get('sid') == '${SID_OF[geo-none]}' for q in d.get('requesters', []))"

say "7b. Kerberos CIFS mounts: geo-nested on failfast, geo-reader on wait"
cifs_mount() { # user share mountpoint
  install -d "$3"; mountpoint -q "$3" && return 0
  # nosharesock: one connection (and SMB session) per mount. Without it the
  # Linux CIFS client reuses another user's krb5 session to the same server,
  # and the server, rightly, sees that user.
  mount -t cifs "//$FQDN/$2" "$3" -o "sec=krb5,cruid=${UID_OF[$1]},uid=${UID_OF[$1]},noperm,cache=none,vers=3.1.1,actimeo=0,nosharesock"
}
if cifs_mount geo-nested smbfail "$MNT/cifs-nested-fail" 2>"$WORK/cifs-nested.err"; then
  pass "CIFS mount of smbfail as geo-nested (sec=krb5, nested group Proj-Readers-Nested)"
  t=$(date +%s%N)
  r=$(as geo-nested timeout 30 $E2EU compare "$MNT/cifs-nested-fail/nested.segy" "${SRC[smbfail/nested.segy]}")
  if jt "$r" 'd["error"] and "errno 5" in d["error"] and d["zero_blocks"] == 0'; then
    pass "CIFS (krb5) read of archived smbfail/nested.segy as geo-nested: EIO after $(since "$t") s, no zeros"
  else
    fail "CIFS failfast read as geo-nested: $r"
  fi
  check "recall of smbfail/nested.segy attributed to geo-nested, via smb, proven" wait_for 20 rq smbfail nested.segy geo-nested smb proven
else
  fail "CIFS mount as geo-nested: $(cat "$WORK/cifs-nested.err")"
fi

say "8. SMB wait: the read waits, then returns the right bytes"
rm -f "$WORK/got-w.bin"
( SMB_T=600 smb geo-reader smbwait "get a.segy $WORK/got-w.bin" >"$WORK/smb-wait.out" ) &
sw=$!
cifs_mount geo-reader smbwait "$MNT/cifs-reader-wait" 2>"$WORK/cifs-reader.err" || fail "CIFS mount as geo-reader: $(cat "$WORK/cifs-reader.err")"
as geo-reader timeout 600 $E2EU compare "$MNT/cifs-reader-wait/b.segy" "${SRC[smbwait/b.segy]}" >"$WORK/cifs-wait.json" &
cw=$!
sleep 8
if kill -0 $sw 2>/dev/null && kill -0 $cw 2>/dev/null; then
  pass "both SMB readers (smbclient, CIFS krb5) still waiting after 8 s (wait policy: the read is held, no error, no zeros)"
else
  fail "an SMB wait reader finished early: smbclient $(cat "$WORK/smb-wait.out" | tail -2); cifs $(cat "$WORK/cifs-wait.json")"
fi
check "recall of smbwait/a.segy attributed to geo-reader, via smb, proven" wait_for 20 rq smbwait a.segy geo-reader smb proven
check "recall of smbwait/b.segy attributed to geo-reader (CIFS krb5), via smb, proven" wait_for 20 rq smbwait b.segy geo-reader smb proven
for f in a.segy b.segy; do $E2E advance smbwait "$f" downloading >/dev/null; done
say "recalls moved to downloading (the worker's restore, done by hand)"
wait $sw; wait $cw
if [ -f "$WORK/got-w.bin" ] && cmp -s "$WORK/got-w.bin" "${SRC[smbwait/a.segy]}"; then
  pass "smbclient get of smbwait/a.segy completed with the source's bytes after the fill"
else
  fail "smbclient wait read: $(tail -2 "$WORK/smb-wait.out")"
fi
r=$(cat "$WORK/cifs-wait.json" 2>/dev/null || echo '{}')
jt "$r" 'd.get("identical") and d.get("zero_blocks") == 0' \
  && pass "CIFS (krb5) read of smbwait/b.segy as geo-reader: byte-identical after the fill ($(j "$r" 'd["seconds"]') s)" \
  || fail "CIFS wait read: $r"
recall_done() { local r; r=$($E2E recall "$1" "$2"); jt "$r" "d.get('state') == 'available'"; }
check "recall of smbwait/a.segy available (the edge reported the fill)" wait_for 60 recall_done smbwait a.segy
check "catalog says resident for smbwait/a.segy after the recall fill" wait_for 30 bash -c "$E2E file smbwait a.segy | grep -q '\"state\": \"resident\"'"

say "8b. an Explorer ACL edit (smbcacls over SMB) reaches the catalog's ACL snapshot"
has_acl_sid() { # share name sid -> the catalog ACL names it on an access entry
  local r; r=$($E2E acl "$1" "$2")
  jt "$r" "any(e.get('sid') == '$3' and e['scope'] == 'access' for e in d.get('entries', []))"
}
if has_acl_sid smbwait a.segy "${SID_OF[geo-none]}"; then fail "geo-none already in smbwait/a.segy's catalog ACL before the edit"; else pass "before the edit: geo-none is not in smbwait/a.segy's catalog ACL"; fi
out=$(timeout 30 smbcacls "//$FQDN/smbwait" a.segy -A /root/m2-auth/geo-writer -a 'ACL:OMNI\geo-none:ALLOWED/0x0/READ' 2>&1)
if grep -q NT_STATUS <<<"$out"; then
  fail "smbcacls -a as geo-writer: $out"
else
  pass "geo-writer (the file's owner: POSIX lets only the owner change an ACL) granted geo-none READ on smbwait/a.segy with smbcacls (Explorer's path)"
fi
check "the POSIX ACL now names uid 20003 (no security.NTACL)" bash -c \
  "getfacl -cpn $DATA/smbwait/a.segy | grep -q '^user:20003:r' && ! getfattr -n security.NTACL $DATA/smbwait/a.segy >/dev/null 2>&1"
t=$(date +%s)
if wait_for 120 has_acl_sid smbwait a.segy "${SID_OF[geo-none]}"; then
  r=$($E2E acl smbwait a.segy)
  pass "the edge's ACL scan reported the edit: catalog ACL $(j "$r" 'd["sha256"][:12]') names geo-none (user, access, $(j "$r" '[e["perms"] for e in d["entries"] if e.get("sid") == "'"${SID_OF[geo-none]}"'"][0]')) after $(($(date +%s) - t)) s; read_sids include it: $(jt "$r" "'${SID_OF[geo-none]}' in d['read_sids']" && echo yes || echo no)"
else
  fail "catalog ACL of smbwait/a.segy never named geo-none: $($E2E acl smbwait a.segy)"
fi
check "an acl-scan was recorded for the dataset" bash -c \
  "journalctl -q -o cat -u omnifile-edge --since @$T0 | grep -q 'ACL changes reported'"

# ---------------------------------------------------------------------------------
say "9. NFS failfast (Policy from on_open=fail): EIO at once; the wait export blocks"
for v in 3 4.1 4.2; do
  t=$(date +%s%N)
  r=$(setpriv --reuid=20002 --regid=10000 --clear-groups timeout 60 $E2EU compare "$MNT/failfast-v${v//./}/ff.segy" "${SRC[failfast/ff.segy]}")
  if jt "$r" 'd["error"] and "errno 5" in d["error"] and d["zero_blocks"] == 0' && awk "BEGIN{exit !($(since "$t") < 10)}"; then
    pass "NFSv$v read of archived failfast/ff.segy: EIO in $(since "$t") s, no zeros"
  else
    fail "NFSv$v failfast read: $r ($(since "$t") s)"
  fi
done
for v in 3 4.1 4.2; do
  setpriv --reuid=20002 --regid=10000 --clear-groups timeout 600 $E2EU compare "$MNT/wait-v${v//./}/w.segy" "${SRC[wait/w.segy]}" >"$WORK/nfs-wait-$v.json" &
  eval "nw_${v//./}=$!"
done
sleep 8
alive=0; for v in 3 41 42; do eval "p=\$nw_$v"; kill -0 "$p" 2>/dev/null && alive=$((alive + 1)); done
[ $alive = 3 ] && pass "NFSv3, 4.1 and 4.2 readers of archived wait/w.segy all still waiting after 8 s" || fail "only $alive of 3 NFS wait readers were waiting"
$E2E advance wait w.segy downloading >/dev/null
for v in 3 41 42; do eval "p=\$nw_$v"; wait "$p"; done
for v in 3 4.1 4.2; do
  r=$(cat "$WORK/nfs-wait-$v.json" 2>/dev/null || echo '{}')
  jt "$r" 'd.get("identical") and d.get("zero_blocks") == 0' && pass "NFSv$v wait read of wait/w.segy byte-identical after the fill" || fail "NFSv$v wait read: $r"
done

# ---------------------------------------------------------------------------------
say "10. kill -9 the daemon (and so the guard) mid-SMB-read, three times"
# 4 s per 8 MiB range: the 32 MiB fill takes about 16 s, longer than the
# three kills below. (The marker only widens every 64 MiB, so the fill's
# progress can't be watched on the marker: the readers' being blocked is.)
$E2E fault '{"op": "get_range", "latency_ms": 4000}' >/dev/null
cifs_mount geo-reader smbwait "$MNT/cifs-reader-wait" 2>/dev/null
spid=$(smbd_pid); gpid=$(ganesha_pid)
as geo-reader timeout 900 $E2EU compare "$MNT/cifs-reader-wait/kill.segy" "${SRC[smbwait/kill.segy]}" >"$WORK/kill-cifs.json" &
k1=$!
( SMB_T=900 smb geo-writer smbwait "get kill.segy $WORK/got-kill.bin" >"$WORK/kill-smb.out" ) &
k2=$!
sleep 4
fetching() { journalctl -q -o cat -u omnifile-edge --since "@$T0" | grep -q "smb-guard: read of non-local bytes.*\"file_id\":$(j "$($E2E file smbwait kill.segy)" 'd["file_id"]'),"; }
check "the SMB readers' online fetch of smbwait/kill.segy started (smb-guard asked the daemon)" fetching
kill -0 $k1 2>/dev/null && kill -0 $k2 2>/dev/null \
  && pass "both SMB readers (CIFS krb5, smbclient) still waiting on the fill when the first kill lands" \
  || fail "an SMB reader finished before the kills"
for i in 1 2 3; do
  before=$(edge_pid)
  nr=$(systemctl show -p NRestarts --value omnifile-edge)
  systemctl kill -s KILL omnifile-edge
  wait_for 30 bash -c "[ \"\$(systemctl show -p NRestarts --value omnifile-edge)\" -gt $nr ] && systemctl is-active -q omnifile-edge" \
    && pass "kill $i: daemon (with the guard) restarted, pid $before -> $(edge_pid); group reused from the fd store" \
    || fail "kill $i: daemon not back"
  sleep 2
done
$E2E fault '{"clear": true}' >/dev/null
wait $k1; wait $k2
[ "$(smbd_pid)" = "$spid" ] && pass "smbd kept its pid $spid through three daemon kills (RestartMode=direct; BindsTo not fired)" || fail "smbd restarted ($spid -> $(smbd_pid))"
[ "$(ganesha_pid)" = "$gpid" ] && pass "Ganesha kept its pid through the kills" || fail "Ganesha restarted"
r=$(cat "$WORK/kill-cifs.json" 2>/dev/null || echo '{}')
jt "$r" 'd.get("identical") and d.get("zero_blocks") == 0' && pass "CIFS reader across three daemon kills: byte-identical, no zero block ($(j "$r" 'd["seconds"]') s)" || fail "CIFS reader across kills: $r"
if [ -f "$WORK/got-kill.bin" ] && cmp -s "$WORK/got-kill.bin" "${SRC[smbwait/kill.segy]}"; then
  pass "smbclient reader across three daemon kills: byte-identical"
else
  r=$($E2E compare "$WORK/got-kill.bin" "${SRC[smbwait/kill.segy]}" 2>/dev/null)
  fail "smbclient reader across kills: $(tail -2 "$WORK/kill-smb.out") $r"
fi
check "journal: ORPHAN events from a killed instance were answered by the next (or none were pending)" bash -c \
  "! journalctl -q -o cat -u omnifile-edge --since @$T0 | grep -q 'event dropped unanswered'"
check "after the online fetch the catalog says resident (files/{id}/filled)" wait_for 60 bash -c \
  "$E2E file smbwait kill.segy | grep -q '\"state\": \"resident\"'"

# ---------------------------------------------------------------------------------
say "11. the daemon held down: readers wait (or fail), never read zeros"
mkdir -p /run/systemd/system/omnifile-edge.service.d
cat >/run/systemd/system/omnifile-edge.service.d/99-e2e-hold.conf <<'EOF'
# tests/e2e/m2_exit_test.sh: keep a restarted daemon from starting while
# /run/omnifile-e2e-hold exists. The unit stays "activating" (RestartMode=direct),
# so smbd and Ganesha (BindsTo=) stay up and the guard's group stays stored.
[Service]
ExecStartPre=/bin/sh -c 'while [ -e /run/omnifile-e2e-hold ]; do sleep 0.2; done'
TimeoutStartSec=infinity
EOF
systemctl daemon-reload
touch /run/omnifile-e2e-hold
sync; echo 3 >/proc/sys/vm/drop_caches
systemctl kill -s KILL omnifile-edge
wait_for 10 bash -c '[ "$(systemctl show -p SubState --value omnifile-edge)" = start-pre ]' \
  && pass "daemon killed and held down (unit activating/start-pre, no daemon process)" || fail "hold: $(systemctl show -p ActiveState,SubState omnifile-edge | tr '\n' ' ')"
H0=$(date +%s)
as geo-reader timeout 300 $E2EU compare "$MNT/cifs-reader-wait/hold.segy" "${SRC[smbwait/hold.segy]}" >"$WORK/hold-cifs.json" &
h_cifs=$!
timeout 300 $E2EU compare "$DATA/smbwait/hold.segy" "${SRC[smbwait/hold.segy]}" >"$WORK/hold-local.json" &
h_local=$!
for v in 3 4.1 4.2; do
  setpriv --reuid=20002 --regid=10000 --clear-groups timeout 300 $E2EU compare "$MNT/wait-v${v//./}/hold.segy" "${SRC[wait/hold.segy]}" >"$WORK/hold-nfs-$v.json" &
  eval "h_nfs_${v//./}=$!"
done
sleep 12
held=0; done_early=""
for p in h_cifs h_local h_nfs_3 h_nfs_41 h_nfs_42; do
  if kill -0 "${!p}" 2>/dev/null; then held=$((held + 1)); else done_early="$done_early $p"; fi
done
[ "$(ganesha_pid)" = "$gpid" ] && [ "$(smbd_pid)" = "$spid" ] && pass "smbd and Ganesha stayed up while the daemon was held down" || fail "servers changed during hold"
for f in hold-cifs hold-local hold-nfs-3 hold-nfs-4.1 hold-nfs-4.2; do
  if [ -s "$WORK/$f.json" ]; then
    r=$(cat "$WORK/$f.json")
    jt "$r" 'd["zero_blocks"] == 0 and not d["identical"]' && pass "$f finished during the hold without zeros: $(j "$r" 'd["error"]') (fail closed)" \
      || fail "$f during the hold: $r"
  fi
done
pass "$held of 5 readers (CIFS, local, NFSv3, 4.1, 4.2) blocked for 12 s with no daemon (none read zeros)${done_early:+; finished early:$done_early}"
release_hold
H1=$(date +%s)
check "daemon back after the hold ($((H1 - H0)) s held)" wait_for 30 systemctl is-active -q omnifile-edge
for p in h_cifs h_local h_nfs_3 h_nfs_41 h_nfs_42; do wait "${!p}"; done
for f in hold-cifs hold-local hold-nfs-3 hold-nfs-4.1 hold-nfs-4.2; do
  r=$(cat "$WORK/$f.json" 2>/dev/null || echo '{}')
  if jt "$r" 'd.get("identical") and d.get("zero_blocks") == 0'; then
    pass "$f after the hold: byte-identical ($(j "$r" 'd["seconds"]') s)"
  elif jt "$r" 'd.get("zero_blocks") == 0 and d.get("error")'; then
    pass "$f: failed closed during the hold ($(j "$r" 'd["error"]')), no zeros"
  else
    fail "$f after the hold: $r"
  fi
done

# ---------------------------------------------------------------------------------
say "12. Kerberos NFS: mount sec=krb5p as geo-reader against the dev AD"
install -d "$MNT/krb"
if as geo-reader true && mount -t nfs -o "vers=4.2,proto=tcp,sec=krb5p,hard,timeo=50" "$FQDN:/krb" "$MNT/krb" 2>"$WORK/krb-mount.err"; then
  pass "NFSv4.2 mount of /krb with sec=krb5p (nfs/$FQDN from the AD keytab)"
  n=$((n + 1)); mk krb/k.segy $n 8
  if as geo-writer sh -c "cp ${SRC[krb/k.segy]} $MNT/krb/k.segy" 2>"$WORK/krb-write.err"; then
    pass "geo-writer wrote krb/k.segy over NFS krb5p"
  else
    fail "krb5p write as geo-writer: $(cat "$WORK/krb-write.err")"
  fi
  r=$(as geo-reader timeout 60 $E2EU compare "$MNT/krb/k.segy" "${SRC[krb/k.segy]}")
  jt "$r" 'd["identical"]' && pass "geo-reader reads krb/k.segy over NFS krb5p: byte-identical" || fail "krb5p read as geo-reader: $r"
  r=$(as geo-none timeout 30 $E2EU compare "$MNT/krb/k.segy" "${SRC[krb/k.segy]}")
  jt "$r" '"errno 13" in (d["error"] or "")' && pass "geo-none denied over NFS krb5p (EACCES)" || fail "krb5p read as geo-none: $r"
  worker_verify_only
  wait_for 240 committed krb/k.segy && pass "krb/k.segy committed" || fail "krb/k.segy not committed"
  worker_stub_now
  wait_for 180 stubbed krb/k.segy && pass "krb/k.segy stubbed" || fail "krb/k.segy not stubbed"
  systemctl stop omnifile-dev-worker
  $E2E archive krb k.segy >/dev/null
  wait_for 60 archived krb/k.segy || fail "krb/k.segy not archived"
  umount "$MNT/krb"; sync; echo 3 >/proc/sys/vm/drop_caches
  mount -t nfs -o "vers=4.2,proto=tcp,sec=krb5p,hard,timeo=50" "$FQDN:/krb" "$MNT/krb"
  as geo-reader timeout 600 $E2EU compare "$MNT/krb/k.segy" "${SRC[krb/k.segy]}" >"$WORK/krb-wait.json" &
  kw=$!
  check "recall of krb/k.segy attributed to geo-reader, via nfs, sec krb5p, proven" wait_for 30 bash -c \
    "$E2E recall krb k.segy | $PY -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(q.get(\"sid\") == \"${SID_OF[geo-reader]}\" and q[\"via\"] == \"nfs\" and q[\"proof\"] == \"proven\" and q[\"sec\"] == \"krb5p\" for q in d.get(\"requesters\", [])) else 1)'"
  $E2E advance krb k.segy downloading >/dev/null
  wait $kw
  r=$(cat "$WORK/krb-wait.json" 2>/dev/null || echo '{}')
  jt "$r" 'd.get("identical")' && pass "krb5p wait read of archived krb/k.segy: byte-identical after the fill" || fail "krb5p wait read: $r"
  umount "$MNT/krb"
else
  fail "NFS krb5p mount: $(cat "$WORK/krb-mount.err")"
fi

# ---------------------------------------------------------------------------------
say "13. flood: $FLOOD local readers of one archived stub through the integrated daemon"
sync; echo 3 >/proc/sys/vm/drop_caches
# Every reader holds a descriptor while it waits.
ulimit -n 65536
timeout 900 $E2E flood "$DATA/smbwait/flood.segy" "${SRC[smbwait/flood.segy]}" "$FLOOD" 600 >"$WORK/flood.json" &
fl=$!
check "local readers' recall reported via=local (process on the edge), claimed" wait_for 60 bash -c \
  "$E2E recall smbwait flood.segy | $PY -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(q[\"via\"] == \"local\" for q in d.get(\"requesters\", [])) else 1)'"
sleep 5
kill -0 $fl 2>/dev/null && pass "all $FLOOD readers held while the recall is pending" || fail "flood finished before the recall: $(cat "$WORK/flood.json")"
F0=$(date +%s)
$E2E advance smbwait flood.segy downloading >/dev/null
wait $fl
r=$(cat "$WORK/flood.json" 2>/dev/null || echo '{}')
if jt "$r" "d.get('identical') == $FLOOD and d.get('zeros') == 0 and d.get('unfinished') == 0"; then
  pass "flood: $FLOOD of $FLOOD readers read the source's bytes, 0 zeros, 0 errors; drained $(($(date +%s) - F0)) s after the recall moved on"
else
  fail "flood: $r"
fi

say "14. logs"
check "no panic in the daemon journal" bash -c "! journalctl -q -o cat -u omnifile-edge --since @$T0 | grep -q 'panicked'"
check "smbd still active, guard still armed" bash -c "systemctl is-active -q smbd && systemctl is-active -q omnifile-edge"

# ---------------------------------------------------------------------------------
{
  echo "# M2 exit test results"
  echo
  echo "- When: $(date -u +%Y-%m-%dT%H:%M:%SZ), $(($(date +%s) - T0)) s"
  echo "- Where: $(hostname), $(. /etc/os-release; echo "$PRETTY_NAME"), kernel $(uname -r), $(nproc) CPUs"
  echo "- Repository: HEAD ${E2E_HEAD:-unknown} (${E2E_REV:-unknown})"
  echo "- Test script: tests/e2e/m2_exit_test.sh sha256 ${E2E_SELF_SHA:-unknown}"
  echo "- Daemon: $(/usr/local/bin/omnifile-edge --version), sha256 $(sha256sum /usr/local/bin/omnifile-edge | cut -c1-16)"
  echo "- Samba: $(smbd --version); Ganesha: $(/opt/ganesha/bin/ganesha.nfsd -v 2>&1 | head -1)"
  echo "- AD: realm OMNI.TEST (omnifile-dc); files ${MIB} MiB, seeded pseudo-random; flood $FLOOD readers"
  echo "- Result: **$NPASS passed, $NFAIL failed**"
  echo
  echo "| | Check |"
  echo "|---|---|"
  cat "$WORK/checks.txt"
  echo
  echo "## Notes"
  echo
  echo "- **Recall progress is moved by hand.** The worker's restore (M3) is not exercised: the test sets the recall to \`downloading\` in the catalog, as the worker would once the restore completes. Everything after that (feed, read credential, fill through the private mount, \`filled\`, the held reads completing) is the product's."
  echo "- **Archive is the catalog's tier only.** The sim keeps serving the bytes; the marker's ARCHIVED flag and the recall flow are real."
} >"$RESULTS"
say "results: $RESULTS ($NPASS passed, $NFAIL failed)"
rm -f "$WORK"/src-* "$WORK"/got-*
[ "$NFAIL" -eq 0 ]
