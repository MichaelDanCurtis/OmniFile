#!/usr/bin/env bash
# tests/e2e/nfs_grace_test.sh on the dev edge VM, the server on the same machine, as root:
# the M1 stack reset and seeded, Ganesha on the APPLIANCE's base config
# (edge/config/ganesha/omnifile-base.conf), then the grace test once per variant:
#
#   restart  systemctl restart nfs-ganesha-omni (a clean stop and start, as an update does)
#   kill     SIGKILL; systemd starts it again (Restart=on-failure)
#   nlm-off  the same base with Enable_NLM = false, NFSv4 only (--no-nlm): the comparison
#            for the owner's NLM decision (grace then ends once every client reclaimed)
#
#   sudo tests/chaos/grace_dev.sh [restart,kill,nlm-off]
# Results: tests/e2e/results/grace-<variant>.md.
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
WORK=/var/tmp/omnifile-grace-dev
VARIANTS=${1:-restart,kill,nlm-off}
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
install -d -m 0755 "$WORK"
# shellcheck source=lib/devstack.sh
. "$HERE/lib/devstack.sh"
RC=0
LOGCMD='journalctl -q -o short-unix -u nfs-ganesha-omni --since @{since}'
REV=${E2E_REV:-unknown}

cleanup() {
  worker_env_clear
  nlm_server_off
  systemctl restart omnifile-dev-worker 2>/dev/null
}
trap cleanup EXIT

ds_log "reset and seed the M1 stack"
stack_reset_seed || { ds_log "stack reset failed"; exit 1; }
# Files the test holds open must stay whole: no "stub after N minutes" while it runs.
worker_env OMNIFILE_STUB_AFTER_MINUTES=525600

run_variant() { # name base-file nlm(1|0) restart-cmd label
  local name=$1 base=$2 nlm=$3 cmd=$4 label=$5 t
  systemctl stop nfs-ganesha-omni 2>/dev/null
  if [ "$nlm" = 1 ]; then nlm_server_on; else nlm_server_off; fi
  ganesha_conf_appliance "$base"
  # Clients of earlier runs (their namespaces are gone) would each hold grace open until
  # it times out: Ganesha waits for every client it knows of to reclaim.
  rm -rf /var/lib/nfs/ganesha/v4recov /var/lib/nfs/ganesha/v4old
  t=$(date +%s)
  ganesha_start_wait "$t" || { ds_log "$name: Ganesha did not start or leave grace"; RC=1; return; }
  ds_log "$name: Ganesha up, out of grace; running the grace test"
  local extra=()
  [ "$nlm" = 1 ] || extra=(--no-nlm)
  "$REPO/tests/e2e/nfs_grace_test.sh" --server 127.0.0.1 --restart-cmd "$cmd" \
    --server-log-cmd "$LOGCMD" --results "$REPO/tests/e2e/results/grace-$name.md" \
    --label "$label (rev $REV)" "${extra[@]}" || RC=1
}

IFS=, read -r -a vs <<<"$VARIANTS"
for v in "${vs[@]}"; do
  case "$v" in
    restart)
      run_variant restart "$REPO/edge/config/ganesha/omnifile-base.conf" 1 \
        'systemctl restart nfs-ganesha-omni' \
        "Dev VM. The appliance's base config as committed; Ganesha restarted cleanly." ;;
    kill)
      run_variant kill "$REPO/edge/config/ganesha/omnifile-base.conf" 1 \
        'systemctl kill -s KILL nfs-ganesha-omni; sleep 1; for i in $(seq 1 60); do systemctl is-active -q nfs-ganesha-omni && exit 0; sleep 0.5; done; exit 1' \
        "Dev VM. The appliance's base config as committed; Ganesha killed with SIGKILL, restarted by systemd." ;;
    nlm-off)
      sed 's/^\(\s*Enable_NLM = \)true;/\1false;/' "$REPO/edge/config/ganesha/omnifile-base.conf" >"$WORK/base-nlm-off.conf"
      run_variant nlm-off "$WORK/base-nlm-off.conf" 0 \
        'systemctl restart nfs-ganesha-omni' \
        "Dev VM. The appliance's base config with Enable_NLM = false (the comparison); Ganesha restarted cleanly; NFSv4 only." ;;
    *) echo "unknown variant $v" >&2; RC=2 ;;
  esac
done
systemctl stop nfs-ganesha-omni
exit $RC
