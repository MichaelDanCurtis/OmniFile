#!/usr/bin/env bash
# M3 exit test (docs/BUILD-PLAN.md M3, docs/milestones/M3.md), on the edge
# machine itself, as root, against sim (two sim_http facades) on a shared
# virtual clock, with the real edge (daemon, Ganesha + FSAL_OMNI, Samba).
#
#   tests/e2e/run.sh --m3          from the Mac (installs, then runs this)
#   tests/e2e/run.sh --m3-test     this test only
#
# Virtual time: sim_http (both facades), the api and the worker share one
# OffsetClock file (OMNIFILE_DEV_CLOCK_FILE, libfaketime's "+<seconds>" form),
# and Postgres runs under libfaketime reading the same file, so the catalog's
# now() moves too. `m3.py advance` (POST /_sim/advance) moves them all;
# archive days and 15-hour rehydrates pass in seconds. The edge keeps real
# time (it only ever sees the service's times through the API).
#
# What it proves (each results line is one check):
#   1. a file goes hot -> cool -> cold -> archive on its policy's days; on the
#      two-account layout the archive step copies into the archive account and
#      deletes the original; stub_at=archive makes it a stub at that moment;
#   2. a stub read triggers a real rehydrate (Copy Blob into the recall
#      container, Standard, 15 h on the virtual clock): wait readers (NFS
#      v4.2/v3, SMB) block until it completes and get byte-identical bytes
#      whose CRC64-NVME matches the catalog; failfast readers get EIO /
#      NT_STATUS at once, the recall is queued, and a later read succeeds;
#      a file replaced while a reader waits on its recall: the reader is
#      released with the new bytes, the recall ends `superseded` (not failed),
#      its note goes, and the rehydrate is left to finish; a file deleted
#      while a reader waits: the reader gets an error, not a wait with no end
#      (control-api.md 4.4, migration 0059);
#   3. a tier pin to hot (override_archive) keeps a file hot and un-stubbed
#      past its archive day; once it expires the file archives;
#   4. Smart is one way: a Smart file pinned to Cool never returns to Smart,
#      and a move back to Smart is refused; Smart files archive on the day;
#   5. a WORM share (azure_worm, one account): WORM enabled and auto-locked;
#      an overwrite makes a new version; the old version can't be deleted
#      (backend and catalog); archive happens in place; rewind-depth pruning
#      leaves a retained version alone and prunes it once retention ends;
#   6. the High-priority budget (10 GiB/h per account): synthetic 6 GiB recalls
#      beyond an hour of pipeline wait in OmniFile's queue with an ETA, and are
#      admitted as it drains; a 12 GiB recall gets no promised time;
#   7. change-feed sync lag is measured and reported.
#
# Environment: E2E_RESULTS, E2E_REV / E2E_HEAD, E2E_MIB (default 16), E2E_KEEP=1.
set -uo pipefail
if [ -z "${E2E_COPY:-}" ]; then
  copy=$(mktemp /tmp/omnifile-m3-XXXXXX.sh)
  cp "$0" "$copy"
  E2E_COPY=1 E2E_SELF_SHA=$(sha256sum "$0" | cut -d' ' -f1) \
    E2E_HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) exec bash "$copy" "$@"
fi

HERE=${E2E_HERE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
REPO=$(cd "$HERE/../.." && pwd)
PY=/opt/omnifile-service/venv/bin/python
E2E="$PY $HERE/e2e.py"
M3="$PY $HERE/m3.py"
DATA=/srv/omnifile/data
MNT=/mnt/omnifile-m3
WORK=/var/tmp/omnifile-m3
# Unprivileged readers run e2e.py from a world-readable copy in $WORK (made below): the repo may sit under a
# home the readers cannot traverse, as on GitHub runners where /home/runner is 0750.
E2EU="python3 $WORK/e2e.py"
VAR=/var/lib/omnifile-dev
CLOCK_FILE=$VAR/clock-offset
PG_ENV=/etc/postgresql/17/main/environment
RESULTS=${E2E_RESULTS:-$HERE/results/m3-latest.md}
MIB=${E2E_MIB:-16}
ENV_FILE=${AD_ENV_FILE:-/work/dev/.secrets/ad.env}
OMNIACL=/usr/local/bin/omniacl
FQDN=edge1.omni.test
SHARES=(wait failfast smbwait smbfail smart worm)
declare -A UID_OF=([geo-reader]=20001 [geo-writer]=20002 [geo-none]=20003 [geo-owner]=20005)
NFAIL=0
NPASS=0
T0=$(date +%s)
DAY=86400

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
set -a
# shellcheck disable=SC1091
. /etc/omnifile-dev/service.env
set +a
mkdir -p "$(dirname "$RESULTS")" "$WORK" "$MNT"
install -m 0755 -d "$WORK"
install -m 0644 "$HERE/e2e.py" "$WORK/e2e.py"
: >"$WORK/checks.txt"
: >"$WORK/notes.txt"

since() { awk -v a="$1" -v b="$(date +%s%N)" 'BEGIN { printf "%.2f", (b - a) / 1e9 }'; }
say() { printf '[m3 %4ss] %s\n' "$(($(date +%s) - T0))" "$*"; }
pass() { say "PASS: $*"; echo "| PASS | $* |" >>"$WORK/checks.txt"; NPASS=$((NPASS + 1)); }
fail() { say "FAIL: $*"; echo "| **FAIL** | $* |" >>"$WORK/checks.txt"; NFAIL=$((NFAIL + 1)); }
note() { echo "$*" >>"$WORK/notes.txt"; }
check() { local d=$1; shift; if "$@"; then pass "$d"; else fail "$d"; fi; }
j() { "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); v=eval(sys.argv[2]); print(v if not isinstance(v,(dict,list)) else json.dumps(v, default=str))' "$1" "$2"; }
jt() { "$PY" -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if eval(sys.argv[2]) else 1)' "$1" "$2"; }
wait_for() {
  local t=$1; shift
  local end=$(($(date +%s) + t))
  while [ "$(date +%s)" -lt "$end" ]; do "$@" && return 0; sleep 1; done
  "$@"
}
as() {
  local u=$1; shift
  setpriv --reuid="${UID_OF[$u]}" --regid=10000 --clear-groups --inh-caps=-all -- \
    env KRB5CCNAME="FILE:/tmp/krb5cc_${UID_OF[$u]}" HOME=/tmp "$@"
}
writer() { setpriv --reuid=20002 --regid=10000 --clear-groups "$@"; }
smb() { # user share command
  timeout -s KILL "${SMB_T:-60}" smbclient "//$FQDN/$2" -A "/root/m3-auth/$1" -t "${SMB_T:-60}" -c "$3" 2>&1
}
VDAYS=0 # virtual time advanced, in seconds
VBASE=0 # VDAYS when the files were written: policy days count from there
adv() { # seconds
  local r; r=$($M3 advance "$1") || { fail "advance $1 s"; return 1; }
  VDAYS=$((VDAYS + ${1%.*}))
  sleep 2 # libfaketime (Postgres) re-reads the clock file at most once a second
  say "virtual clock +$1 s (total +$((VDAYS / 3600)) h): $(j "$r" 'd["now"]')"
}
adv_to() { # seconds after the writes (plus the real time since, which the clock also counts)
  local want=$((VBASE + $1))
  [ "$want" -gt "$VDAYS" ] && adv $((want - VDAYS))
  return 0
}
MARGIN=1800 # files commit over a few real minutes after VBASE

umount_all() {
  local m
  for m in $(findmnt -rn -o TARGET | grep "^$MNT/" | sort -r); do
    timeout 20 umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || true
  done
}
pg_faketime_off() {
  if [ -f "$PG_ENV.m3-orig" ]; then
    mv -f "$PG_ENV.m3-orig" "$PG_ENV"
    systemctl restart postgresql@17-main
  fi
}
worker_dropin_off() {
  rm -f /run/systemd/system/omnifile-dev-worker.service.d/90-e2e-m3.conf
  systemctl daemon-reload
}
cleanup() {
  [ "${E2E_KEEP:-0}" = 1 ] && return
  umount_all
  systemctl stop omnifile-dev-worker 2>/dev/null
  worker_dropin_off
  pg_faketime_off
  # Back to real time: the facades issued keys on the virtual clock, so they restart.
  echo "+0" >"$CLOCK_FILE"
  systemctl restart omnifile-dev-sim omnifile-dev-sim-worm 2>/dev/null
  systemctl restart omnifile-dev-api 2>/dev/null
}
trap cleanup EXIT
# So an interrupt goes through `cleanup`, with the status a signal gives (as m5_exit_test.sh).
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------------------------
say "0. reset: services, catalog, both sims, edge state; Postgres on the shared clock"
umount_all
systemctl stop smbd nfs-ganesha-omni omnifile-edge omnifile-dev-worker 2>/dev/null
# Release the smb-guard's group (`omnifile-guardctl clean`): it outlives the daemon in the fd
# store, and its marks on unfilled rewind directories would hold the rm below.
systemctl clean --what=fdstore omnifile-edge 2>/dev/null
rm -rf /var/lib/omnifile/replica.db* /var/lib/omnifile/identity /var/lib/omnifile/outbox
install -d -m 0700 /var/lib/omnifile/identity /var/lib/omnifile/outbox
for s in "${SHARES[@]}" wait failfast smbwait smbfail krb; do rm -rf "${DATA:?}/$s"; done
rm -rf "${DATA:?}/.omni"
echo "+0" >"$CLOCK_FILE"; chmod 0644 "$CLOCK_FILE"
if ! dpkg-query -W -f='${Status}' libfaketime 2>/dev/null | grep -q 'install ok installed'; then
  apt-get install -y -qq libfaketime >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y -qq libfaketime >/dev/null; }
fi
FAKELIB=$(ls /usr/lib/*/faketime/libfaketime.so.1 2>/dev/null | head -1)
[ -n "$FAKELIB" ] || { fail "libfaketime not found"; exit 1; }
[ -f "$PG_ENV.m3-orig" ] || cp "$PG_ENV" "$PG_ENV.m3-orig"
cp "$PG_ENV.m3-orig" "$PG_ENV"
cat >>"$PG_ENV" <<EOF
# tests/e2e/m3_exit_test.sh: the catalog's now() follows the shared virtual clock.
LD_PRELOAD = '$FAKELIB'
FAKETIME_TIMESTAMP_FILE = '$CLOCK_FILE'
FAKETIME_CACHE_DURATION = '1'
FAKETIME_DONT_FAKE_MONOTONIC = '1'
DONT_FAKE_MONOTONIC = '1'
EOF
systemctl restart postgresql@17-main
wait_for 60 bash -c "$PY -c 'import os, psycopg; psycopg.connect(os.environ[\"OMNIFILE_DATABASE_URL\"]).close()' 2>/dev/null" \
  || { fail "Postgres did not come back under libfaketime"; exit 1; }
$E2E reset-db >/dev/null || { fail "catalog reset"; exit 1; }
rm -f "$VAR/sim-key.json" "$VAR/sim-worm-key.json"
systemctl restart omnifile-dev-sim omnifile-dev-sim-worm omnifile-dev-enrol
systemctl restart omnifile-dev-api
wait_for 30 test -s "$VAR/sim-key.json" -a -s "$VAR/sim-worm-key.json"
wait_for 30 bash -c '(exec 3<>/dev/tcp/127.0.0.1/8443) 2>/dev/null && (exec 3<>/dev/tcp/127.0.0.1/8444) 2>/dev/null'
c=$($M3 clock)
jt "$c" 'abs(d["db_offset_s"]) < 5 and d["offset_s"] == 0' && pass "shared clock at +0: sim, api/worker file and Postgres (libfaketime) agree with the wall clock" || fail "clock at start: $c"
probe=$($M3 advance 3600)
c=$($M3 clock)
sleep 2; c=$($M3 clock)
if jt "$c" 'abs(d["db_offset_s"] - 3600) < 5 and d["offset_s"] == 3600 and d["sim"] >= d["db"][:16]'; then
  pass "one /_sim/advance moves everything: +1 h on both facades, the shared file and Postgres now() (db offset $(j "$c" 'd["db_offset_s"]') s)"
else
  fail "advance probe: $c"
fi
VDAYS=3600

say "1. AD: groups, SMB auth files"
OWN=$(wbinfo -n proj-owners | cut -d' ' -f1)
WRI=$(wbinfo -n proj-writers | cut -d' ' -f1)
REA=$(wbinfo -n proj-readers | cut -d' ' -f1)
declare -A SID_OF
for u in "${!UID_OF[@]}"; do SID_OF[$u]=$(wbinfo -n "$u" | cut -d' ' -f1); done
check "winbind resolves the share groups and users" test -n "$OWN" -a -n "$WRI" -a -n "$REA" -a -n "${SID_OF[geo-reader]}"
set -a; . "$ENV_FILE"; set +a
umask 077; install -d -m 700 /root/m3-auth
for u in "${!UID_OF[@]}"; do
  var="AD_PASSWORD_$(printf '%s' "$u" | tr 'a-z-' 'A-Z_')"
  printf 'username = %s\npassword = %s\ndomain = OMNI\n' "$u" "${!var}" >"/root/m3-auth/$u"
done
umask 022
unset AD_ADMIN_PASSWORD

say "2. seed: two backends (two-account, WORM), six shares with M3 policies; enrol"
ip=$(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -1)
seed=$($M3 seed --enrol-url https://127.0.0.1:8444 --server-ca /etc/omnifile-dev/pki/ca.crt \
  --bundle-out "$WORK/bundle.txt" --uid 20002 --gid 10000 \
  --owners-sid "$OWN" --writers-sid "$WRI" --readers-sid "$REA" --nfs-client "$ip") \
  || { fail "seed"; exit 1; }
say "seeded: $seed"
note "- Policies: scheduled \`$(j "$seed" 'd["shares"]["wait"]["policy"]')\`; smart \`$(j "$seed" 'd["shares"]["smart"]["policy"]')\`; WORM share \`$(j "$seed" 'd["shares"]["worm"]["policy"]')\`."
if /usr/local/bin/omnifile-edge --config /etc/omnifile/edge.toml enrol "$WORK/bundle.txt" >"$WORK/enrol.out" 2>&1; then
  pass "enrolment: $(grep '^enrolled:' "$WORK/enrol.out")"
else
  fail "enrolment: $(tail -3 "$WORK/enrol.out")"; exit 1
fi
rm -f "$WORK/bundle.txt"

say "3. daemon, shares rendered from the catalog, Ganesha and smbd"
systemctl start omnifile-edge
check "omnifile-edge active" wait_for 30 systemctl is-active -q omnifile-edge
check "share roots materialised (${SHARES[*]})" wait_for 60 bash -c \
  "for s in ${SHARES[*]}; do test -d $DATA/\$s || exit 1; done"
for s in smbwait smbfail; do "$OMNIACL" apply-root "$DATA/$s" 30003 30002 30001 >"$WORK/root-$s.txt" || fail "apply-root $s"; done
shares=$($E2E shares)
: >"$WORK/shares-smb.conf"; : >"$WORK/exports.conf"
eid=20
for name in $(j "$shares" '" ".join(s["name"] for s in d["shares"])'); do
  row=$(j "$shares" "[s for s in d['shares'] if s['name'] == '$name'][0]")
  case "$(j "$row" 'd["on_open"]')" in wait) pol=wait ;; fail) pol=failfast ;; auto) pol="waitquick:$(( $(j "$row" 'd["wait_threshold_minutes"] or 0') * 60000 ))" ;; esac
  protos=$(j "$row" '",".join(d["protocols"])')
  secs=$(j "$row" '",".join(d["nfs_sec"]) or "-"')
  clients=$(j "$row" '",".join(d["nfs_clients"]) or "-"')
  eid=$((eid + 1))
  if "$OMNIACL" render "$name" "$DATA/$name" "$eid" "$protos" "$secs" "$clients" \
      "$(j "$row" 'd["owners_group_sid"]')" "$(j "$row" 'd["writers_group_sid"] or "-"')" \
      "$(j "$row" 'd["readers_group_sid"] or "-"')" "$pol" >"$WORK/render-$name.txt" 2>&1; then
    sed -n '/^#--- smb/,/^#--- ganesha/p' "$WORK/render-$name.txt" | grep -v '^#---' >>"$WORK/shares-smb.conf"
    sed -n '/^#--- ganesha/,$p' "$WORK/render-$name.txt" | grep -v '^#---' >>"$WORK/exports.conf"
  else
    fail "render $name: $(cat "$WORK/render-$name.txt")"
  fi
done
install -m 0644 "$WORK/shares-smb.conf" /etc/samba/omnifile-shares.conf
install -m 0644 "$WORK/exports.conf" /etc/ganesha/omnifile-exports.conf
sed -e "s#@NFS_DOMAIN@#omni.test#g" -e "s#@KEYTAB@#/etc/krb5.keytab#g" -e "s#@FSAL_SOCKET@#/run/omnifile/fsal.sock#g" \
  "$REPO/edge/config/ganesha/ganesha.conf.tmpl" >/etc/ganesha/ganesha.conf
G0=$(date +%s)
systemctl start nfs-ganesha-omni smbd
check "nfs-ganesha-omni and smbd active" wait_for 30 bash -c 'systemctl is-active -q nfs-ganesha-omni && systemctl is-active -q smbd'
check "FSAL_OMNI connected" wait_for 30 bash -c "journalctl -q -o cat -u nfs-ganesha-omni --since @$G0 | grep -q 'connected to the OmniFile daemon'"
wait_for 120 bash -c "journalctl -q -o cat -u nfs-ganesha-omni --since @$G0 | grep -q 'NOT IN GRACE'" || true
nfs_mount() { # export version mountpoint
  install -d "$3"; mountpoint -q "$3" && return 0
  if [ "$2" = 3 ]; then
    mount -t nfs -o "vers=3,proto=tcp,nolock,hard,timeo=50" "127.0.0.1:$DATA/$1" "$3"
  else
    mount -t nfs -o "vers=$2,proto=tcp,hard,timeo=50" "127.0.0.1:/$1" "$3"
  fi
}
for s in wait failfast smart worm; do nfs_mount "$s" 4.2 "$MNT/$s" || fail "mount $s v4.2"; done
nfs_mount failfast 3 "$MNT/failfast-v3" || fail "mount failfast v3"
nfs_mount wait 3 "$MNT/wait-v3" || fail "mount wait v3"

say "4. WORM on the worm share (typed confirmation, 60-minute window), rewind depth 1 day"
r=$($M3 worm-enable worm 4 60) && pass "WORM enabled on share worm: 4-day retention, lock deadline $(j "$r" 'd["lock_deadline"]')" || fail "WORM enable: $r"
r=$($M3 rewind worm 1)
jt "$r" 'd["rewind_depth_days"] == 1 and d["worm_mode"] == "unlocked_test"' && pass "rewind depth 1 day (Owners, within the ArchiveAdmin maximum); WORM in its validation window" || fail "rewind/worm state: $r"

say "5. the worker: tiering, retention, change feed, reconcile, verify; 2 s sweeps and polls"
mkdir -p /run/systemd/system/omnifile-dev-worker.service.d
cat >/run/systemd/system/omnifile-dev-worker.service.d/90-e2e-m3.conf <<'EOF'
# tests/e2e/m3_exit_test.sh: virtual days pass in seconds.
[Service]
Environment=OMNIFILE_WORKER_MAX_SWEEP_S=2
Environment=OMNIFILE_RECALL_POLL_S=2
EOF
systemctl daemon-reload
W0=$(date +%s)
systemctl restart omnifile-dev-worker
check "worker active on the dev clock, with the M3 jobs" wait_for 30 bash -c \
  "journalctl -q -o cat -u omnifile-dev-worker --since @$W0 | grep -q 'dev clock'"

# ---------------------------------------------------------------------------------
say "6. write files over NFS and SMB; commit and verify"
declare -A SRC
n=300
VBASE=$VDAYS
mk() { n=$((n + 1)); SRC[$1]="$WORK/src-${1//\//-}"; $E2E pattern "${SRC[$1]}" $((MIB * 1048576)) "$n" >/dev/null; }
NFS_FILES=(wait/w.segy wait/pin.segy failfast/ff.segy failfast/ff3.segy smart/s.segy smart/pinned.segy worm/v.segy wait/sup.segy wait/del.segy wait/held.segy)
SMB_FILES=(smbwait/a.segy smbfail/b.segy)
for f in "${NFS_FILES[@]}"; do
  mk "$f"
  writer cp "${SRC[$f]}" "$MNT/${f%%/*}/${f#*/}" && writer sync -f "$MNT/${f%%/*}/${f#*/}" \
    && pass "wrote $f over NFSv4.2 as geo-writer" || fail "write $f"
done
for f in "${SMB_FILES[@]}"; do
  mk "$f"
  out=$(smb geo-writer "${f%%/*}" "put ${SRC[$f]} ${f#*/}")
  grep -q NT_STATUS <<<"$out" && fail "SMB put $f: $out" || pass "wrote $f over SMB as geo-writer (NTLMv2 against the DC)"
done
ALL=("${NFS_FILES[@]}" "${SMB_FILES[@]}")
committed() {
  local row; row=$($E2E file "${1%%/*}" "${1#*/}") || return 1
  jt "$row" 'd.get("location_state") == "committed" and d.get("verified")'
}
for f in "${ALL[@]}"; do
  if wait_for 240 committed "$f"; then
    row=$($E2E file "${f%%/*}" "${f#*/}")
    pass "$f committed and verified by the worker's read-back ($(j "$row" 'd["verified_by"]'), CRC64 $(j "$row" 'd["crc64nvme"]'))"
  else
    fail "$f not committed: $($E2E file "${f%%/*}" "${f#*/}")"
  fi
done
tier_is() { # share name tier [container-prefix]
  local t; t=$($M3 tier "$1" "$2") || return 1
  jt "$t" "d['primary'] and d['primary']['logical_tier'] == '$3' and d['primary']['sim'].get('tier') == '$3'${4:+ and d['primary']['container'].startswith('$4')}"
}
check "smart/s.segy lands in Smart on the live (ZRS) account: no explicit tier sent at upload" tier_is smart s.segy smart live/
check "wait/w.segy (scheduled mode) is set Hot explicitly: Smart must not cool it before the warm day" wait_for 60 tier_is wait w.segy hot live/
check "WORM uploads land Hot in the WORM account" tier_is worm v.segy hot worm/

say "7. WORM: overwrite makes a new version; the old one can't be destroyed"
mk worm/v2.segy
SRC[worm/v.segy.1]=${SRC[worm/v.segy]}
SRC[worm/v.segy]=${SRC[worm/v2.segy]}
writer cp "${SRC[worm/v.segy]}" "$MNT/worm/v.segy" && writer sync -f "$MNT/worm/v.segy" || fail "overwrite worm/v.segy"
two_versions() { local v; v=$($M3 versions worm v.segy); jt "$v" 'len([x for x in d["versions"] if x["state"] == "committed"]) == 2 and d["versions"][-1]["version_id"] == d["content_version_id"]'; }
if wait_for 240 two_versions && wait_for 240 committed worm/v.segy; then
  v=$($M3 versions worm v.segy)
  pass "overwrite of worm/v.segy committed as a new version ($(j "$v" '[x["version_id"] for x in d["versions"]]')); both blobs kept in sim: $(j "$v" '[x.get("sim", {}).get("exists") for x in d["versions"]]')"
  jt "$v" 'all(x["retention_until"] for x in d["versions"])' && pass "every committed version has retention_until = commit + 4 days ($(j "$v" '[x["retention_until"][:19] for x in d["versions"]]'))" || fail "retention: $v"
else
  fail "worm overwrite: $($M3 versions worm v.segy)"
fi
stamped() { local v; v=$($M3 versions worm v.segy); jt "$v" 'all(x.get("sim", {}).get("retention_until") for x in d["versions"])'; }
check "per-version retention stamped on the WORM blobs (worm_stamp sweep: Azure version-level immutability)" wait_for 60 stamped
r=$($M3 delete-old worm v.segy)
if jt "$r" 'd["backend_delete"]["refused"] and d["backend_delete"]["error"] == "Immutable" and all(d[k]["refused"] and d[k]["sqlstate"] == "OF002" for k in ("catalog_delete_version", "catalog_prune_version", "catalog_delete_location"))'; then
  pass "the old version $(j "$r" 'd["version_id"]') can't be destroyed: sim delete -> Immutable; catalog delete, prune, and location delete -> OF002"
else
  fail "old WORM version: $r"
fi

say "8. pins; the High-priority budget; WORM auto-lock"
r=$($M3 pin wait pin.segy hot 84 --override-archive) && pass "pin wait/pin.segy to hot for 84 h, override_archive (Owner's explicit choice past the 72 h archive day)" || fail "pin: $r"
r=$($M3 pin smart pinned.segy warm 30) && pass "pin smart/pinned.segy to cool (warm) for 30 h" || fail "pin: $r"
r=$($M3 budget-seed 6 6 6 12)
say "budget recalls: $r"
budget_settled() {
  local b; b=$($M3 budget)
  jt "$b" 'd["recalls"][0]["state"] == "rehydrating" and d["recalls"][3]["state"] == "rehydrating" and all(r["state"] == "requested" and r["eta"] for r in d["recalls"][1:3])'
}
if wait_for 60 budget_settled; then
  b=$($M3 budget)
  pass "10 GiB/h budget: the first 6 GiB High recall is admitted (restore speed $(j "$b" 'd["recalls"][0]["speed"]'), ETA $(j "$b" 'd["recalls"][0]["eta"][11:19]')); the 2nd and 3rd wait in OmniFile's queue (no restore yet) with ETAs $(j "$b" '[r["eta"][11:19] for r in d["recalls"][1:3]]')"
  jt "$b" 'd["recalls"][3]["eta"] is None and d["recalls"][3]["priority"] == "standard"' \
    && pass "a 12 GiB recall has no promised time (none above 10 GB), at Standard" || fail "12 GiB recall: $b"
else
  fail "budget: $($M3 budget)"
fi
note "- Budget, before the hour passed: \`$($M3 budget | $PY -c 'import json,sys; d=json.load(sys.stdin); print([(r["recall_id"], r["size"]//(1<<30), r["priority"], r["state"], r["speed"], (r["eta"] or "none")[11:19]) for r in d["recalls"]])')\`"
adv 3660 # past the WORM window and an hour of priority pipeline
locked() { local w; w=$($M3 worm worm); jt "$w" 'd["worm_mode"] == "locked"'; }
check "the WORM share auto-locked when its validation window ended (one-way from now)" wait_for 60 locked
admitted2() { local b; b=$($M3 budget); jt "$b" 'd["recalls"][1]["state"] == "rehydrating" and d["recalls"][1]["speed"] == "high"'; }
if wait_for 60 admitted2; then
  b=$($M3 budget)
  pass "an hour later the pipeline drained: the 2nd 6 GiB recall is admitted at High; the 3rd: $(j "$b" 'd["recalls"][2]["state"]') (speed $(j "$b" 'd["recalls"][2]["speed"]'))"
else
  fail "budget after an hour: $($M3 budget)"
fi

# ---------------------------------------------------------------------------------
say "9. day 1: scheduled files go Hot -> Cool (in place); Smart stays Smart"
adv_to $((DAY + MARGIN))
check "wait/w.segy is Cool (Set Blob Tier in place on the live account)" wait_for 60 tier_is wait w.segy warm live/
check "smbwait/a.segy (written over SMB) is Cool" wait_for 60 tier_is smbwait a.segy warm live/
check "worm/v.segy is Cool, in place in the WORM account" wait_for 60 tier_is worm v.segy warm worm/
check "smart/s.segy is still Smart (Microsoft moves it; OmniFile waits for the archive day)" tier_is smart s.segy smart live/
pin_moved_cool() { local t; t=$($M3 tier smart pinned.segy); jt "$t" '["smart", "warm"] in d["moves"] and d["next_at_is_pin_until"]'; }
check "smart/pinned.segy is Cool while its pin lasts (explicit tier)" wait_for 60 tier_is smart pinned.segy warm live/
check "smart/pinned.segy: the scheduler moved it Smart -> Cool for the pin (audit tier.move) and will look again when the pin ends" wait_for 60 pin_moved_cool
# The pinned-hot file sat in the same dataset as wait/w.segy, which moved to
# Cool: the scheduler evaluated it on day 1 and held it for the pin.
pin_held_hot() { local t; t=$($M3 tier wait pin.segy); jt "$t" 'd["primary"]["logical_tier"] == "hot" and not any(m[1] in ("warm", "cold", "archive") for m in d["moves"]) and d["next_at_is_pin_until"]'; }
check "wait/pin.segy (pinned hot) is Hot, not Cool" wait_for 60 tier_is wait pin.segy hot live/
check "wait/pin.segy: evaluated on day 1 and held for the pin (no move off Hot; next look at the pin's end, 84 h)" wait_for 60 pin_held_hot

say "10. day 2: Cool -> Cold; the expired Smart pin leaves the file Cool, never Smart"
adv_to $((2 * DAY + MARGIN))
check "wait/w.segy is Cold" wait_for 60 tier_is wait w.segy cold live/
check "failfast/ff.segy is Cold" wait_for 60 tier_is failfast ff.segy cold live/
# After the pin's end the scheduler looks again (its next look moves past the
# pin's end, to the archive day) and leaves the file Cool: no move to Smart.
pin_reevaluated() { local t; t=$($M3 tier smart pinned.segy); jt "$t" 'd["next_at_after_pin_until"]'; }
check "smart/pinned.segy re-evaluated after its pin ended (next look moved past the pin's end, to the archive day)" wait_for 60 pin_reevaluated
t=$($M3 tier smart pinned.segy)
jt "$t" 'd["primary"]["logical_tier"] == "warm" and d["primary"]["explicit_tier_set"] and d["primary"]["sim"]["tier"] == "warm" and not any(m[1] == "smart" for m in d["moves"])' \
  && pass "smart/pinned.segy after its pin expired: still Cool, explicit tier (one way: never back to Smart)" || fail "pinned smart file: $t"
fid=$(j "$t" 'd["file_id"]')
$M3 move smart pinned.segy smart >/dev/null
refused() { local a; a=$($M3 audit tier.move.refused "$fid"); jt "$a" 'd["n"] >= 1 and any(r["details"].get("why") in ("smart_one_way",) or "Smart" in str(r["details"].get("why")) for r in d["rows"])'; }
check "a move back to Smart is refused and audited (tier.move.refused)" wait_for 30 refused
check "smart/pinned.segy still Cool in sim after the refused move" tier_is smart pinned.segy warm live/

say "11. day 3: archive (copy into the archive account, then delete); stub_at=archive"
adv_to $((3 * DAY + MARGIN))
archived_copy() { # share name
  local t; t=$($M3 tier "$1" "$2") || return 1
  jt "$t" 'd["primary"]["container"].startswith("archive/") and d["primary"]["logical_tier"] == "archive" and d["primary"]["sim"]["tier"] == "archive" and d["primary"]["sim"]["restore_state"] == "archived" and any(l["state"] == "deleted" and l["container"].startswith("live/") and l["sim"].get("error") == "ObjectNotFound" for l in d["locations"])'
}
for f in wait/w.segy wait/sup.segy wait/del.segy wait/held.segy failfast/ff.segy failfast/ff3.segy smbwait/a.segy smbfail/b.segy smart/s.segy smart/pinned.segy; do
  check "$f archived on day 3: copied into archive/data at Archive, the live original deleted (gone from sim)" wait_for 90 archived_copy "${f%%/*}" "${f#*/}"
done
archived_inplace() {
  local t; t=$($M3 tier worm v.segy) || return 1
  jt "$t" 'd["primary"]["container"] == "worm/data" and d["primary"]["logical_tier"] == "archive" and d["primary"]["sim"]["tier"] == "archive" and len([l for l in d["locations"] if l["version_id"] == d["primary"]["version_id"]]) == 1'
}
check "worm/v.segy archived in place (Set Blob Tier, same WORM blob; no copy, no delete)" wait_for 90 archived_inplace
stubbed_archived() { # share name
  local row mk; row=$($E2E file "$1" "$2") || return 1
  mk=$($E2E local "$DATA/$1/$2") || return 1
  jt "$row" 'd.get("state") == "stub"' && jt "$mk" "d['marker'] and d['marker']['state'] == 'PARTIAL' and (d['marker']['flags'] & 2) and not (d['marker']['flags'] & 1)"
}
for f in wait/w.segy wait/sup.segy wait/del.segy wait/held.segy failfast/ff.segy failfast/ff3.segy smbwait/a.segy smbfail/b.segy smart/s.segy worm/v.segy; do
  check "$f is a stub after archive (stub_at=archive; marker PARTIAL, ARCHIVED)" wait_for 120 stubbed_archived "${f%%/*}" "${f#*/}"
done
t=$($M3 tier wait pin.segy); row=$($E2E file wait pin.segy)
jt "$t" 'd["primary"]["logical_tier"] == "hot" and d["primary"]["container"].startswith("live/")' && jt "$row" 'd["state"] == "resident"' \
  && pass "wait/pin.segy on day 3: Hot, resident, not archived (the hot pin overrides the archive day)" || fail "pinned file on day 3: $t $row"
v=$($M3 versions worm v.segy)
jt "$v" 'd["versions"][0]["state"] == "committed" and d["versions"][0]["sim"]["exists"]' \
  && pass "the superseded WORM version is beyond rewind depth + grace (1 + 1 days) but still retained, so not pruned" || fail "WORM old version on day 3: $v"
sync; echo 3 >/proc/sys/vm/drop_caches

# ---------------------------------------------------------------------------------
say "12. failfast: an archived stub fails at once; the recall is queued"
for f in failfast/ff.segy failfast/ff3.segy; do
  m=$MNT/failfast; [ "$f" = failfast/ff3.segy ] && m=$MNT/failfast-v3
  ver=4.2; [ "$f" = failfast/ff3.segy ] && ver=3
  t=$(date +%s%N)
  r=$(writer timeout 60 $E2EU compare "$m/${f#*/}" "${SRC[$f]}")
  if jt "$r" 'd["error"] and "errno 5" in d["error"] and d["zero_blocks"] == 0' && awk "BEGIN{exit !($(since "$t") < 10)}"; then
    pass "NFSv$ver read of archived $f: EIO in $(since "$t") s, no zeros"
  else
    fail "NFSv$ver failfast read of $f: $r ($(since "$t") s)"
  fi
done
t=$(date +%s%N)
out=$(SMB_T=30 smb geo-reader smbfail "get b.segy $WORK/got-b.bin")
secs=$(since "$t")
if grep -q 'NT_STATUS_IO_DEVICE_ERROR' <<<"$out" && awk "BEGIN{exit !($secs < 10)}"; then
  pass "SMB read of archived smbfail/b.segy as geo-reader: NT_STATUS_IO_DEVICE_ERROR in $secs s"
else
  fail "SMB failfast read: $out ($secs s)"
fi
rehydrating() { # share name
  local r; r=$($M3 recall "$1" "$2"); jt "$r" 'd.get("state") == "rehydrating" and d["speed"] == "standard" and d["mechanism"] == "azure_copy_rehydrate" and (d["target_container"] or "").endswith("/recall") and d["eta"]'
}
for f in failfast/ff.segy smbfail/b.segy; do
  if wait_for 30 rehydrating "${f%%/*}" "${f#*/}"; then
    r=$($M3 recall "${f%%/*}" "${f#*/}")
    pass "$f: recall queued and the worker started a Copy Blob rehydrate (Standard) into $(j "$r" 'd["target_container"]'); ETA $(j "$r" 'd["eta"][:19]') (15 h on the virtual clock)"
  else
    fail "$f recall: $($M3 recall "${f%%/*}" "${f#*/}")"
  fi
done
check "a .RECALLING.txt note next to failfast/ff.segy" wait_for 10 test -s "$DATA/failfast/ff.segy.RECALLING.txt"

say "13. wait: readers block until the rehydrate completes (virtual 15 h), then read the right bytes"
writer timeout 900 $E2EU compare "$MNT/wait/w.segy" "${SRC[wait/w.segy]}" >"$WORK/wait-nfs.json" &
p1=$!
writer timeout 900 $E2EU compare "$MNT/smart/s.segy" "${SRC[smart/s.segy]}" >"$WORK/wait-smart.json" &
p2=$!
writer timeout 900 $E2EU compare "$MNT/worm/v.segy" "${SRC[worm/v.segy]}" >"$WORK/wait-worm.json" &
p3=$!
rm -f "$WORK/got-a.bin"
( SMB_T=900 smb geo-reader smbwait "get a.segy $WORK/got-a.bin" >"$WORK/wait-smb.out" ) &
p4=$!
# Three more waiting readers, whose files change under them (steps 13b and 13c).
rm -f "$WORK/got-sup.bin" "$WORK/got-del.bin" "$WORK/got-held.bin" "$WORK"/got-*.rc
( writer timeout 900 cat "$MNT/wait/held.segy" >"$WORK/got-held.bin" 2>"$WORK/got-held.err"; echo $? >"$WORK/got-held.rc" ) &
p7=$!
( writer timeout 900 cat "$MNT/wait/sup.segy" >"$WORK/got-sup.bin" 2>"$WORK/got-sup.err"; echo $? >"$WORK/got-sup.rc" ) &
p5=$!
( writer timeout 900 cat "$MNT/wait/del.segy" >"$WORK/got-del.bin" 2>"$WORK/got-del.err"; echo $? >"$WORK/got-del.rc" ) &
p6=$!
sleep 10
alive=0; for p in $p1 $p2 $p3 $p4; do kill -0 $p 2>/dev/null && alive=$((alive + 1)); done
[ $alive = 4 ] && pass "4 readers (NFSv4.2 wait, smart, WORM, SMB smbclient) all waiting after 10 s: the rehydrate is running, no error, no zeros" \
  || fail "only $alive of 4 wait readers still waiting"
for f in wait/w.segy smart/s.segy worm/v.segy smbwait/a.segy; do
  check "$f: recall rehydrating (the read started it)" wait_for 30 rehydrating "${f%%/*}" "${f#*/}"
done
r=$($M3 recall worm v.segy)
jt "$r" '(d["target_container"] or "") == "worm/recall"' && pass "the WORM file's rehydrate copies into the non-WORM recall container (worm/recall); the archived original is untouched" || fail "WORM recall target: $r"

say "13b. a file replaced, and a file deleted, while a reader waits on its recall"
verified_back_now() { # share name
  local row loc; row=$($E2E file "$1" "$2"); loc=$($E2E local "$DATA/$1/$2" --hash)
  jt "$row" 'd["state"] == "resident"' && [ "$(j "$loc" 'd["crc64nvme"]')" = "$(j "$row" 'd["crc64nvme"]')" ]
}
superseded() { # share name reason recall_id (by id: a deleted file has no name to find it by)
  local r; r=$($M3 recall "$1" "$2" --id "$4")
  jt "$r" "d.get('recall_id') == $4 and d['state'] == 'superseded' and d['superseded_reason'] == '$3' and d['failure'] is None and d['completed_at']"
}
for f in wait/sup.segy wait/del.segy; do
  check "$f: recall rehydrating (the read started it)" wait_for 30 rehydrating "${f%%/*}" "${f#*/}"
  check "a .RECALLING.txt note next to $f" wait_for 10 test -s "$DATA/$f.RECALLING.txt"
done
kill -0 $p5 2>/dev/null && kill -0 $p6 2>/dev/null && pass "both readers are waiting (NFSv4.2, wait share)" || fail "a reader of sup.segy or del.segy is not waiting"
SUP_RID=$(j "$($M3 recall wait sup.segy)" 'd["recall_id"]')
DEL_RID=$(j "$($M3 recall wait del.segy)" 'd["recall_id"]')
SUP_RESIDENT=$(j "$($E2E local "$DATA/wait/sup.segy")" 'd["marker"]["resident"]')
DEL_RESIDENT=$(j "$($E2E local "$DATA/wait/del.segy")" 'd["marker"]["resident"]')
EDGE_T=$(date +%s)
# Replaced in place: cp over it truncates it, as a new export does. From another client (the
# NFSv3 mount): on one Linux NFS client a truncate waits for a read of the same file that
# is in flight, so the same mount would only show the client waiting on itself.
mk wait/sup-new.segy
t=$(date +%s%N)
writer timeout 120 cp "${SRC[wait/sup-new.segy]}" "$MNT/wait-v3/sup.segy" && writer timeout 60 sync -f "$MNT/wait-v3/sup.segy" \
  && pass "replaced wait/sup.segy from another client (NFSv3) while its recall rehydrates; the write did not wait for the recall ($(since "$t") s)" \
  || fail "replace wait/sup.segy from the NFSv3 mount"
if wait_for 120 test -s "$WORK/got-sup.rc"; then
  # The reader was part-way through: it had read the stub's resident header (the old
  # version's, served at once) and was waiting for the rest. It goes on into the new
  # file, as a reader of any file replaced under it does. What matters here: it is
  # released, with no error, nothing of the old version past its header, and no byte
  # that is in neither file.
  # It is released at the writer's truncate, so it may read while the other client is
  # still writing. Then it gets the file as it is at that instant: short, or with zeros
  # where the writes have not landed. Any reader of a file that another NFS client is
  # rewriting can get that, stub or not (wp24d.md has the probe), so it is said, not
  # failed. The stub's own holes are gone by then: the FSAL removes the marker only
  # together with the truncate to 0.
  mix=$($M3 readmix "$WORK/got-sup.bin" "${SRC[wait/sup-new.segy]}" "${SRC[wait/sup.segy]}")
  if [ "$(cat "$WORK/got-sup.rc")" = 0 ] && jt "$mix" "d['other'] == 0 and d['old_end'] <= $SUP_RESIDENT" \
    && jt "$mix" "d['zeros'] == 0 and d['mixed'] == 0 and d['new'] > 0 and d['bytes'] == d['new_bytes']"; then
    pass "the waiting reader of wait/sup.segy was released $(since "$t") s after the replace, with no error and no zeros: after the $(j "$mix" 'd["old_end"]') bytes of resident header it had already read (the old version's), every byte is the new file's ($(j "$mix" 'd["new"]') blocks of 64 KiB)"
  elif [ "$(cat "$WORK/got-sup.rc")" = 0 ] && jt "$mix" "d['other'] == 0 and d['old_end'] <= $SUP_RESIDENT"; then
    pass "the waiting reader of wait/sup.segy was released during the replace, while the other client was still writing, with no error: after the $(j "$mix" 'd["old_end"]') bytes of resident header it had already read (the old version's), nothing more of the old version and no byte that is in neither file; it read $(j "$mix" 'd["bytes"]') of $(j "$mix" 'd["new_bytes"]') bytes, $(j "$mix" 'd["new"]') blocks of the new file's and $(j "$mix" 'd["zeros"] + d["mixed"]') with zeros where its writes had not landed yet"
  else
    fail "the waiting reader of the replaced file: exit $(cat "$WORK/got-sup.rc"), $mix, $(head -c 200 "$WORK/got-sup.err")"
  fi
else
  fail "the reader of the replaced file is still waiting after 120 s"
  kill $p5 2>/dev/null
fi
r=$(writer timeout 120 $E2EU compare "$MNT/wait/sup.segy" "${SRC[wait/sup-new.segy]}")
jt "$r" 'd.get("identical") and d.get("zero_blocks") == 0' && pass "a new read of wait/sup.segy gets the whole new file, byte-identical" || fail "read after the replace: $r"
if wait_for 180 superseded wait sup.segy replaced "$SUP_RID"; then
  r=$($M3 recall wait sup.segy)
  pass "wait/sup.segy: its recall ended superseded (replaced) when the new version was committed, not failed; no failure text; ended by a $(j "$r" 'd["superseded_by_kind"]') version"
else
  fail "the replaced file's recall: $($M3 recall wait sup.segy)"
fi
check "the .RECALLING.txt note next to wait/sup.segy is gone" wait_for 30 test ! -e "$DATA/wait/sup.segy.RECALLING.txt"
r=$($M3 recall wait sup.segy)
jt "$r" 'd["restore_state"] == "in_progress" and d["target_state"] == "pending"' \
  && pass "the old version's rehydrate is left running (restore in_progress): not aborted, and not polled for this recall any more" || fail "the superseded recall's restore: $r"
check "wait/sup.segy holds the new version: resident, local CRC64-NVME = the catalog's" wait_for 120 verified_back_now wait sup.segy
# Deleted on the edge's own volume (a local process), while the NFS reader holds it open.
t=$(date +%s%N)
rm -f "$DATA/wait/del.segy" && pass "deleted wait/del.segy on the edge while its recall rehydrates" || fail "delete wait/del.segy"
if wait_for 180 superseded wait del.segy deleted "$DEL_RID"; then
  pass "wait/del.segy: its recall ended superseded (deleted) with the tombstone, not failed"
else
  fail "the deleted file's recall: $($M3 recall wait del.segy --id "$DEL_RID")"
fi
check "the .RECALLING.txt note next to where wait/del.segy was is gone" wait_for 30 test ! -e "$DATA/wait/del.segy.RECALLING.txt"
if wait_for 180 test -s "$WORK/got-del.rc"; then
  # It had read the resident header before it waited; after the delete nothing more can come.
  n=$(stat -c %s "$WORK/got-del.bin")
  [ "$(cat "$WORK/got-del.rc")" != 0 ] && [ "$n" -le "$DEL_RESIDENT" ] && cmp -s -n "$n" "$WORK/got-del.bin" "${SRC[wait/del.segy]}" \
    && pass "the waiting reader of the deleted file was released $(since "$t") s after the delete, with an error ($(head -c 120 "$WORK/got-del.err" | tr '\n' ' ')) and nothing past the $n bytes of resident header it had already read: not a wait with no end, no zeros" \
    || fail "the reader of the deleted file: exit $(cat "$WORK/got-del.rc"), $n bytes (resident $DEL_RESIDENT), $(head -c 120 "$WORK/got-del.err")"
else
  fail "the reader of the deleted file is still waiting after 180 s"
  kill $p6 2>/dev/null
fi
n=$(journalctl -q -o cat -u omnifile-edge --since "@$EDGE_T" | grep -c 'recall fill failed: reporting' || true)
[ "$n" = 0 ] && pass "the edge reported no failed fill for either recall" || fail "$n failed fills reported by the edge"

say "13c. a file deleted while a reader waits, and a version restore holds its recall"
# Decision 34: an owner restores the file's current version under another name. The
# restore is of that version, so it holds the recall; the delete does not end it, and
# the restore closes it `available` when it is done. The reader still holds the deleted
# file open on a `wait` share: it must end with an error then, not wait for good.
check "wait/held.segy: recall rehydrating (the read started it)" wait_for 30 rehydrating wait held.segy
kill -0 $p7 2>/dev/null && pass "its reader is waiting (NFSv4.2, wait share)" || fail "the reader of held.segy is not waiting"
HELD_RID=$(j "$($M3 recall wait held.segy)" 'd["recall_id"]')
HELD_RESIDENT=$(j "$($E2E local "$DATA/wait/held.segy")" 'd["marker"]["resident"]')
vr=$($M3 restore-copy wait held.segy)
HELD_VR=$(j "$vr" 'd["restore_id"]')
r=$($M3 recall wait held.segy --id "$HELD_RID")
jt "$vr" "d['recall_id'] == $HELD_RID" && jt "$r" 'd["state"] == "rehydrating" and d["held_by_jobs"] == 1 and not d["pinned"]' \
  && pass "a restore of wait/held.segy's current version as $(j "$vr" 'd["name"]') joined the reader's recall and holds it (held_by_jobs 1)" \
  || fail "the restore and its recall: $vr $r"
rm -f "$DATA/wait/held.segy" && pass "deleted wait/held.segy on the edge while its recall rehydrates" || fail "delete wait/held.segy"
held_past_delete() {
  local r; r=$($M3 recall wait held.segy --id "$HELD_RID")
  jt "$r" 'd["file_deleted"] and d["state"] == "rehydrating" and d["held_by_jobs"] == 1 and d["superseded_reason"] is None'
}
check "the tombstone is committed and the held recall goes on (rehydrating, not superseded)" wait_for 180 held_past_delete
sleep 5
kill -0 $p7 2>/dev/null && [ ! -s "$WORK/got-held.rc" ] \
  && pass "the reader of the deleted file still waits: its recall is still running, for the restore" \
  || fail "the reader of held.segy ended before its recall did: exit $(cat "$WORK/got-held.rc" 2>/dev/null), $(head -c 120 "$WORK/got-held.err")"

R0=$(date +%s)
adv $((15 * 3600 + 300))
for p in $p1 $p2 $p3 $p4; do wait $p; done
say "readers released $(($(date +%s) - R0)) s after the clock passed the rehydrate"
for x in wait-nfs:wait/w.segy wait-smart:smart/s.segy wait-worm:worm/v.segy; do
  r=$(cat "$WORK/${x%%:*}.json" 2>/dev/null || echo '{}')
  jt "$r" 'd.get("identical") and d.get("zero_blocks") == 0' && pass "${x#*:}: the waiting reader got byte-identical bytes after the rehydrate ($(j "$r" 'd["seconds"]') s)" || fail "${x#*:} wait read: $r"
done
[ -f "$WORK/got-a.bin" ] && cmp -s "$WORK/got-a.bin" "${SRC[smbwait/a.segy]}" \
  && pass "smbwait/a.segy: the waiting SMB reader got byte-identical bytes" || fail "SMB wait read: $(tail -2 "$WORK/wait-smb.out")"
verified_back() { # share name
  local row loc; row=$($E2E file "$1" "$2"); loc=$($E2E local "$DATA/$1/$2" --hash)
  jt "$row" 'd["state"] == "resident"' && [ "$(j "$loc" 'd["crc64nvme"]')" = "$(j "$row" 'd["crc64nvme"]')" ]
}
for f in wait/w.segy smart/s.segy worm/v.segy smbwait/a.segy; do
  if wait_for 60 verified_back "${f%%/*}" "${f#*/}"; then
    pass "$f back on the edge: resident, local CRC64-NVME $(j "$($E2E file "${f%%/*}" "${f#*/}")" 'd["crc64nvme"]') = the catalog's (verified at upload)"
  else
    fail "$f after recall: $($E2E file "${f%%/*}" "${f#*/}") $($E2E local "$DATA/$f" --hash)"
  fi
  r=$($M3 recall "${f%%/*}" "${f#*/}")
  jt "$r" 'd["state"] == "available" and d["restore_state"] == "completed" and d["target_state"] == "committed"' \
    && pass "$f recall available; restore completed; recall copy committed" || fail "$f recall end state: $r"
done

say "14. failfast, later: once the recall completed the same reads succeed"
for f in failfast/ff.segy smbfail/b.segy; do
  check "$f recall available (the edge filled it after the rehydrate)" wait_for 60 bash -c "$M3 recall ${f%%/*} ${f#*/} | grep -q '\"state\": \"available\"'"
done
# The dev stack stubs a recalled file again 0.2 to 3.4 s after its fill (the policy says a
# file in Archive is a stub; its recalled copy stays online, so a read fills it again).
# But the edge keeps the finished recall's entry for 30 s (recall_valid_ms), and while it
# does, a read of the file it has stubbed again is refused (EIO; fail closed) instead of
# filled. Step 13's checks have a minute for that (wait_for 60), and usually take it.
# These two reads had none: when the stubbing came late (3.2 s), step 13 was done before
# it and they ran inside the 30 s. 3 of 8 runs failed here on #96's branch, and then 3
# of 6 when the reads waited only for the stubbing; on main 1 run of 5 came within a
# second of it (wp24d.md). So they get the same minute.
ff_read() { r=$(writer timeout 60 $E2EU compare "$MNT/failfast/ff.segy" "${SRC[failfast/ff.segy]}"); jt "$r" 'd["identical"]'; }
wait_for 60 ff_read && pass "NFS read of failfast/ff.segy after the recall: byte-identical" || fail "later failfast read: $r"
smb_read() { rm -f "$WORK/got-b.bin"; out=$(SMB_T=60 smb geo-reader smbfail "get b.segy $WORK/got-b.bin"); cmp -s "$WORK/got-b.bin" "${SRC[smbfail/b.segy]}"; }
wait_for 60 smb_read && pass "SMB read of smbfail/b.segy after the recall: byte-identical" || fail "later SMB read: $out"
r=$($M3 recall failfast ff3.segy)
jt "$r" 'd["state"] in ("rehydrating", "downloading", "available")' && pass "failfast/ff3.segy (NFSv3 failfast read) also has its recall: $(j "$r" 'd["state"]')" || fail "ff3 recall: $r"

say "13b and 13c, after the rehydrate"
# Here, after steps 13 and 14, not between them. The dev stack stubs a recalled file
# again a second or two after its fill (OMNIFILE_STUB_AFTER_MINUTES=0), and steps 13
# and 14 check their files inside that time. Anything put before them makes them late:
# with these checks up there, step 13 or step 14 failed in two runs (wp24d.md).
# 13b, after the old version's rehydrate has run out: nothing came back over the new file.
restore_closed() { local r; r=$($M3 recall wait sup.segy); jt "$r" 'd["state"] == "superseded" and d["restore_state"] == "completed" and d["target_state"] == "committed"'; }
check "wait/sup.segy: the old version's rehydrate finished and the change feed closed it (restore completed, its copy committed for the 7-day window); the recall is still superseded" wait_for 120 restore_closed
r=$(writer timeout 120 $E2EU compare "$MNT/wait/sup.segy" "${SRC[wait/sup-new.segy]}")
jt "$r" 'd.get("identical") and d.get("zero_blocks") == 0' && pass "wait/sup.segy still holds the new file after the old version's rehydrate finished" || fail "wait/sup.segy after the old rehydrate: $r"
# 13c: the restore finishes, closes its recall, and the reader ends.
restore_done() { local r; r=$($M3 restore-state "$HELD_VR"); jt "$r" 'd["state"] == "done" and d["result_name"] == d["name"]'; }
if wait_for 240 restore_done; then
  pass "the restore of wait/held.segy's version is done: $(j "$($M3 restore-state "$HELD_VR")" 'd["result_name"]') exists, though the file itself was deleted meanwhile"
else
  fail "the restore of held.segy: $($M3 restore-state "$HELD_VR")"
fi
held_closed() { local r; r=$($M3 recall wait held.segy --id "$HELD_RID"); jt "$r" 'd["state"] == "available" and d["held_by_jobs"] == 0 and d["failure"] is None'; }
check "its recall was closed available by the restore (not left for the idle rule to fail), and is no longer held" wait_for 60 held_closed
if wait_for 180 test -s "$WORK/got-held.rc"; then
  n=$(stat -c %s "$WORK/got-held.bin")
  [ "$(cat "$WORK/got-held.rc")" != 0 ] && [ "$n" -le "$HELD_RESIDENT" ] && cmp -s -n "$n" "$WORK/got-held.bin" "${SRC[wait/held.segy]}" \
    && pass "the reader that held the deleted file open was released when the recall closed, with an error ($(head -c 120 "$WORK/got-held.err" | tr '\n' ' ')) and nothing past the $n bytes of resident header it had already read: not a wait with no end, no zeros" \
    || fail "the reader of the deleted, held file: exit $(cat "$WORK/got-held.rc"), $n bytes (resident $HELD_RESIDENT), $(head -c 120 "$WORK/got-held.err")"
else
  fail "the reader of the deleted file whose recall closed available is still waiting after 180 s"
  kill $p7 2>/dev/null
fi
check "no .RECALLING.txt note is left where wait/held.segy was" wait_for 30 test ! -e "$DATA/wait/held.segy.RECALLING.txt"
r=$(writer timeout 180 $E2EU compare "$MNT/wait/held-restored.segy" "${SRC[wait/held.segy]}")
jt "$r" 'd.get("identical") and d.get("zero_blocks") == 0' && pass "wait/held-restored.segy reads byte-identical to the deleted file's version" || fail "read of the restored copy: $r"

say "15. the pin expired (84 h): the pinned file archives"
check "wait/pin.segy archived after its hot pin expired" wait_for 90 archived_copy wait pin.segy
check "wait/pin.segy then stubbed" wait_for 120 stubbed_archived wait pin.segy

say "16. WORM retention ends (day 4): rewind-depth pruning removes the old version"
adv_to $((4 * DAY + MARGIN))
pruned() { local v; v=$($M3 versions worm v.segy); jt "$v" 'd["versions"][0]["state"] == "pruned" and not d["versions"][0]["sim"]["exists"]'; }
if wait_for 90 pruned; then
  v=$($M3 versions worm v.segy)
  pass "after retention ended the old version $(j "$v" 'd["versions"][0]["version_id"]') was pruned (catalog: pruned) and its blob deleted from the WORM account; the current version stays ($(j "$v" 'd["versions"][-1]["state"]'))"
else
  fail "WORM prune after retention: $($M3 versions worm v.segy)"
fi

say "17. change-feed lag"
mk wait/probe.segy
writer cp "${SRC[wait/probe.segy]}" "$MNT/wait/probe.segy" && writer sync -f "$MNT/wait/probe.segy"
wait_for 240 committed wait/probe.segy || fail "probe not committed"
sleep 8
l=$($M3 lag)
say "lag: $l"
live=$(j "$l" '[c for c in d["cursors"] if c["container"] == "live/data"][0]')
if jt "$live" 'd["caught_up"] and d["last_lag_seconds"] is not None and d["last_lag_seconds"] < 30 and not d["last_error"]'; then
  pass "change-feed sync of live/data caught up; steady-state lag $(j "$live" 'round(d["last_lag_seconds"], 1)') s (sim feed_lag 0 + the 2 s sync interval)"
else
  fail "change-feed lag live/data: $live"
fi
jt "$l" 'all(not c["last_error"] for c in d["cursors"]) and sum(c["records_applied"] for c in d["cursors"]) > 0' \
  && pass "change feed followed on $(j "$l" 'len(d["cursors"])') containers, $(j "$l" 'sum(c["records_applied"] for c in d["cursors"])') records applied, no errors" || fail "change feed cursors: $l"
note "- Change-feed lag per container (samples include virtual-clock jumps, so their max is the jump, not provider lag):"
note "$(j "$l" 'd["samples"]' | $PY -c 'import json,sys; [print(f"  - {s["container"]}: {s["records"]} records in {s["pages"]} pages, max {s["max_lag_s"]:.0f} s, median page max {s["p50_page_max_s"]:.1f} s") for s in json.load(sys.stdin)]')"
note "  - Steady state after the probe write, live/data: last lag $(j "$live" 'round(d["last_lag_seconds"], 1)') s."

say "18. logs"
check "no panic in the daemon journal" bash -c "! journalctl -q -o cat -u omnifile-edge --since @$T0 | grep -q 'panicked'"
check "no traceback in the worker or api journals" bash -c "! journalctl -q -o cat -u omnifile-dev-worker -u omnifile-dev-api --since @$T0 | grep -q 'Traceback'"
dlq=$($M3 dlq)
jt "$dlq" 'not d["dead"]' && pass "no dead-lettered worker messages" || fail "dead letters: $dlq"

# ---------------------------------------------------------------------------------
{
  echo "# M3 exit test results"
  echo
  echo "- When: $(date -u +%Y-%m-%dT%H:%M:%SZ), $(($(date +%s) - T0)) s wall, $((VDAYS / 3600)) h virtual"
  echo "- Where: $(hostname), $(. /etc/os-release; echo "$PRETTY_NAME"), kernel $(uname -r), $(nproc) CPUs"
  echo "- Repository: HEAD ${E2E_HEAD:-unknown} (${E2E_REV:-unknown})"
  echo "- Test script: tests/e2e/m3_exit_test.sh sha256 ${E2E_SELF_SHA:-unknown}"
  echo "- Daemon: $(/usr/local/bin/omnifile-edge --version), sha256 $(sha256sum /usr/local/bin/omnifile-edge | cut -c1-16)"
  echo "- Samba: $(smbd --version); Ganesha: $(/opt/ganesha/bin/ganesha.nfsd -v 2>&1 | head -1); Postgres under $(basename "$FAKELIB")"
  echo "- Backends: sim_http two-account (:10000, live ZRS Smart + archive LRS) and WORM (:10001, one LRS account, version-level WORM), one shared virtual clock"
  echo "- Files: ${MIB} MiB, seeded pseudo-random"
  echo "- Result: **$NPASS passed, $NFAIL failed**"
  echo
  echo "| | Check |"
  echo "|---|---|"
  cat "$WORK/checks.txt"
  echo
  echo "## Notes"
  echo
  cat "$WORK/notes.txt"
  echo "- **Virtual time.** One OffsetClock file is the clock of both sim facades, the api and the worker, and (through libfaketime) of Postgres; \`/_sim/advance\` moves them together. The edge keeps real time. Worker sweeps and restore polls run every 2 s (\`OMNIFILE_WORKER_MAX_SWEEP_S\`, \`OMNIFILE_RECALL_POLL_S\`, dev only)."
  echo "- **Sim, not Azure.** Rehydrate times are Microsoft's stated upper bounds on the virtual clock (Standard 15 h; High 1 h floor at 10 GiB/h per account). Real recall times and real change-feed lag are pilot 4 (docs/milestones/real-azure-checks.md)."
} >"$RESULTS"
say "results: $RESULTS ($NPASS passed, $NFAIL failed)"
rm -f "$WORK"/src-* "$WORK"/got-*
[ "$NFAIL" -eq 0 ]
