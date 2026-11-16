#!/usr/bin/env bash
# The chaos soak on the dev edge VM, as root (tests/chaos/soak.py has the faults, the load
# and the invariants):
#
#   sudo tests/chaos/soak.sh --duration 1800 --each 2     # a short run, to check the harness
#   sudo tests/chaos/soak.sh                              # 6 h 15 min, every fault 12 times
#   sudo tests/chaos/soak.sh --seed 1234 ...              # replay a run's schedule and choices
#   touch /run/omnifile-soak.stop                         # stop early (the report says ABORTED)
#
# Set-up: tests/chaos/storage.sh on first (the soak writes for hours: the data volume on the
# repo mount, Postgres and the journal in RAM). Then the M2 stack: the catalog reset and
# seeded with the AD shares, the edge enrolled, Ganesha on the APPLIANCE's base config
# (edge/config/ganesha/omnifile-base.conf: grace and NLM on) with the wait and failfast
# exports and NLM's server side (devstack.sh nlm_server_on), smbd with the smbwait share
# rendered by omniacl, the worker stubbing at once (stub after 0 minutes) and polling
# recalls every 2 s; NFSv3 (nolock), 4.1 and 4.2 mounts of the wait share.
# Needs omnifile-dc up (AD users geo-reader, geo-writer) and dev/.secrets/ad.env.
#
# Results: tests/e2e/results/chaos-<label>.md; the event log, schedule and summary in
# /work/dev/.artifacts/wp3/soak-<seed>/ (git-ignored), with the run's journal
# (journal.log.gz) and, for a run with violations, the catalog's rows behind them
# (violations-catalog.json): storage.sh keeps the journal and Postgres in RAM, so they
# are saved there when the run ends, before anything stops.
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
WORK=/var/tmp/omnifile-soak
MNT=/mnt/omnifile-soak
DATA=/srv/omnifile/data
ENV_FILE=${AD_ENV_FILE:-$REPO/dev/.secrets/ad.env}
LABEL=soak
ARGS=()
SEED=""
while [ $# -gt 0 ]; do
  case "$1" in
    --label) LABEL=$2; shift ;;
    --seed) SEED=$2; ARGS+=(--seed "$2"); shift ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done
[ -n "$SEED" ] || { SEED=$(od -An -N4 -tu4 /dev/urandom | tr -d ' '); ARGS+=(--seed "$SEED"); }
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
case "$(findmnt -n -o SOURCE --mountpoint $DATA)" in
  /dev/loop*) ;;
  *) echo "no data volume at $DATA" >&2; exit 2 ;;
esac
losetup -n -O BACK-FILE "$(findmnt -n -o SOURCE --mountpoint $DATA)" | grep -q '^/work/' ||
  echo "WARNING: the data volume is on the VM's own disk; run tests/chaos/storage.sh on first" >&2
rm -rf "$WORK"
install -d -m 0755 "$WORK" "$WORK/smbget" "$WORK/smbput"
install -m 0644 "$HERE/soak_io.py" "$WORK/soak_io.py"
OUT=/work/dev/.artifacts/wp3/soak-$SEED
# shellcheck source=lib/devstack.sh
. "$HERE/lib/devstack.sh"
say() { printf '[soak-setup %s] %s\n' "$(date +%T)" "$*"; }

umount_all() {
  local m
  for m in $(findmnt -rn -o TARGET | grep "^$MNT/" | sort -r); do
    timeout 20 umount "$m" 2>/dev/null || umount -f -l "$m" 2>/dev/null
  done
}
cleanup() {
  umount_all
  nft delete table inet omnifile_chaos 2>/dev/null
  rm -f "$DATA/.chaos-fill" /run/omnifile-soak.stop
  $E2E fault '{"clear": true}' >/dev/null 2>&1
  worker_env_clear
  systemctl restart omnifile-dev-worker 2>/dev/null
  systemctl stop smbd nfs-ganesha-omni 2>/dev/null
  nlm_server_off
  rm -rf /root/chaos-auth
}
trap cleanup EXIT

say "0. the stack: reset, seed the AD shares, enrol, start the daemon"
umount_all
stack_reset_seed --m2 || { say "stack reset failed"; exit 1; }
for s in smbwait smbfail krb; do
  omniacl apply-root "$DATA/$s" 30003 30002 30001 >/dev/null || { say "apply-root $s"; exit 1; }
done

say "1. AD credentials for smbclient (geo-reader, geo-writer)"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
umask 077
install -d -m 0700 /root/chaos-auth
for u in geo-reader geo-writer; do
  var="AD_PASSWORD_$(printf '%s' "$u" | tr 'a-z-' 'A-Z_')"
  printf 'username = %s\npassword = %s\ndomain = OMNI\n' "$u" "${!var}" >"/root/chaos-auth/$u"
done
umask 022
unset AD_ADMIN_PASSWORD AD_PASSWORD_GEO_READER AD_PASSWORD_GEO_WRITER AD_PASSWORD_GEO_NONE \
  AD_PASSWORD_OMNI_ADMIN AD_PASSWORD_GEO_OWNER AD_PASSWORD_GEO_NESTED

say "2. smbd: the smbwait share, rendered by omniacl from the catalog"
row=$($E2E shares | $PY -c 'import json,sys; print(json.dumps([s for s in json.load(sys.stdin)["shares"] if s["name"] == "smbwait"][0]))')
pj() { $PY -c 'import json,sys; d=json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$row" "$1"; }
omniacl render smbwait "$DATA/smbwait" 23 smb - - "$(pj 'd["owners_group_sid"]')" \
  "$(pj 'd["writers_group_sid"]')" "$(pj 'd["readers_group_sid"]')" wait >"$WORK/render.txt" 2>&1 ||
  { say "render: $(cat "$WORK/render.txt")"; exit 1; }
sed -n '/^#--- smb/,/^#--- ganesha/p' "$WORK/render.txt" | grep -v '^#---' >/etc/samba/omnifile-shares.conf
testparm -s >/dev/null 2>&1 || { say "smb.conf does not parse"; exit 1; }

say "3. Ganesha on the appliance's base config, NLM's server side; the worker stubs at once"
nlm_server_on
ganesha_conf_appliance
# worker_env first removes any verify-only drop-in a lane left (its EnvironmentFile= would
# override this; the 2026-11-08 regression's soak stubbed nothing for 6 hours because of one).
worker_env OMNIFILE_STUB_AFTER_MINUTES=0 OMNIFILE_RECALL_POLL_S=2 OMNIFILE_WORKER_MAX_SWEEP_S=10
stub_after=$(worker_effective OMNIFILE_STUB_AFTER_MINUTES)
say "the worker's effective OMNIFILE_STUB_AFTER_MINUTES: ${stub_after:-unset}"
[ "$stub_after" = 0 ] || { say "the worker won't stub at once (want 0): the soak would exercise no stubs"; exit 1; }
T=$(date +%s)
ganesha_start_wait "$T" || { say "Ganesha did not start or leave grace"; exit 1; }
systemctl start smbd
ds_wait 30 systemctl is-active -q smbd || { say "smbd did not start"; exit 1; }

say "4. NFS mounts of the wait share: v3 (nolock: no lockd beside Ganesha's NLM), 4.1, 4.2"
for v in v3 v41 v42; do
  install -d "$MNT/$v"
  case $v in
    v3) mount -t nfs -o vers=3,proto=tcp,hard,timeo=50,nolock "127.0.0.1:$DATA/wait" "$MNT/$v" ;;
    v41) mount -t nfs -o vers=4.1,proto=tcp,hard,timeo=50 127.0.0.1:/e2e/wait "$MNT/$v" ;;
    v42) mount -t nfs -o vers=4.2,proto=tcp,hard,timeo=50 127.0.0.1:/e2e/wait "$MNT/$v" ;;
  esac || { say "mount $v"; exit 1; }
done

say "5. the soak (seed $SEED; out $OUT)"
$PY "$HERE/soak.py" "${ARGS[@]}" --out "$OUT" --label "$LABEL" \
  --report "$REPO/tests/e2e/results/chaos-$LABEL.md"
rc=$?
say "soak exited $rc"
exit $rc
