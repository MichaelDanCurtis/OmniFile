# shellcheck shell=bash
# Shared by tests/chaos/*.sh and tests/scale-edge/*.sh on the dev edge VM (dev/README.md,
# "The M1 stack"), as root. Sourced, never run. Expects $WORK and $REPO.
#
#   stack_reset_seed [--m2]   services stopped, catalog, sim and edge state reset, the e2e
#                             shares seeded (wait, failfast; with --m2 also smbwait, smbfail,
#                             krb against the dev AD), the edge enrolled and started
#   ganesha_conf_appliance [base] [extra]
#                             /etc/ganesha/ganesha.conf: the APPLIANCE's base config
#                             (edge/config/ganesha/omnifile-base.conf, or [base]) and the wait
#                             and failfast exports, like exit_test.sh; [extra] is appended
#   nlm_server_on / nlm_server_off
#                             what NFSv3 locking needs next to Ganesha: rpc.statd running, and
#                             a runtime drop-in that sends SM_NOTIFY (sm-notify -f) each time
#                             Ganesha starts, before it unmonitors every client
#   worker_env K=V ...        the dev worker's environment (a runtime drop-in), restarted;
#                             it first removes the e2e lanes' verify-only drop-in, whose
#                             EnvironmentFile= would override these Environment= settings
#   worker_effective K        the running worker's own value of K (its process environment)
set -u
PY=${PY:-/opt/omnifile-service/venv/bin/python}
E2E="$PY $REPO/tests/e2e/e2e.py"
DATA=/srv/omnifile/data
GANESHA_NLM_DROPIN=/run/systemd/system/nfs-ganesha-omni.service.d/60-omnifile-nlm.conf
WORKER_DROPIN=${WORKER_DROPIN:-/run/systemd/system/omnifile-dev-worker.service.d/90-omnifile-chaos.conf}
# The e2e lanes' "verify only, never stub" drop-in (exit_test.sh, m2_exit_test.sh,
# large_copy_test.sh): an EnvironmentFile= with OMNIFILE_STUB_AFTER_MINUTES=525600.
E2E_VERIFY_DROPIN=${E2E_VERIFY_DROPIN:-/run/systemd/system/omnifile-dev-worker.service.d/90-e2e-verify-only.conf}
E2E_WORKER_ENV=${E2E_WORKER_ENV:-/run/omnifile-e2e-worker.env}

ds_log() { printf '[stack %s] %s\n' "$(date +%T)" "$*"; }
ds_wait() { # SECONDS command...
  local end=$(($(date +%s) + $1)); shift
  while [ "$(date +%s)" -lt "$end" ]; do "$@" && return 0; sleep 0.5; done
  "$@"
}
api_up() { (exec 3<>/dev/tcp/127.0.0.1/8443) 2>/dev/null && (exec 3<>/dev/tcp/127.0.0.1/8444) 2>/dev/null; }

stack_reset_seed() {
  local m2=0
  [ "${1:-}" = --m2 ] && m2=1
  set -a
  # shellcheck disable=SC1091
  . /etc/omnifile-dev/service.env
  set +a
  systemctl stop smbd nfs-ganesha-omni omnifile-edge omnifile-dev-worker 2>/dev/null
  systemctl clean --what=fdstore omnifile-edge 2>/dev/null
  rm -rf /var/lib/omnifile/replica.db* /var/lib/omnifile/identity /var/lib/omnifile/outbox
  install -d -m 0700 /var/lib/omnifile/identity /var/lib/omnifile/outbox
  local s
  for s in wait failfast smbwait smbfail krb; do rm -rf "${DATA:?}/$s"; done
  rm -rf "${DATA:?}/.omni"
  $E2E reset-db >/dev/null || return 1
  rm -f /var/lib/omnifile-dev/sim-key.json
  echo "+0" >/var/lib/omnifile-dev/clock-offset
  systemctl restart omnifile-dev-sim omnifile-dev-enrol
  systemctl restart omnifile-dev-api
  ds_wait 30 test -s /var/lib/omnifile-dev/sim-key.json || return 1
  ds_wait 30 api_up || return 1
  local extra=()
  if [ "$m2" = 1 ]; then
    OWN=$(wbinfo -n proj-owners | cut -d' ' -f1)
    WRI=$(wbinfo -n proj-writers | cut -d' ' -f1)
    REA=$(wbinfo -n proj-readers | cut -d' ' -f1)
    [ -n "$OWN" ] && [ -n "$WRI" ] && [ -n "$REA" ] || { ds_log "winbind cannot resolve the share groups (is omnifile-dc up?)"; return 1; }
    extra=(--m2 --owners-sid "$OWN" --writers-sid "$WRI" --readers-sid "$REA")
  fi
  # shellcheck disable=SC2034 # the seed answer, for callers
  SEEDED=$($E2E seed --enrol-url https://127.0.0.1:8444 --server-ca /etc/omnifile-dev/pki/ca.crt \
    --bundle-out "$WORK/bundle.txt" --uid 20002 --gid 10000 "${extra[@]}") || return 1
  /usr/local/bin/omnifile-edge --config /etc/omnifile/edge.toml enrol "$WORK/bundle.txt" >"$WORK/enrol.out" 2>&1 ||
    { ds_log "enrolment failed: $(tail -3 "$WORK/enrol.out")"; return 1; }
  rm -f "$WORK/bundle.txt"
  systemctl start omnifile-edge
  ds_wait 30 systemctl is-active -q omnifile-edge || return 1
  local want="wait failfast"
  [ "$m2" = 1 ] && want="$want smbwait smbfail krb"
  for s in $want; do ds_wait 60 test -d "$DATA/$s" || return 1; done
}

ganesha_conf_appliance() {
  local base=${1:-$REPO/edge/config/ganesha/omnifile-base.conf} more=${2:-}
  install -m 0644 "$base" /etc/ganesha/omnifile-appliance-base.conf
  cat >/etc/ganesha/ganesha.conf <<EOF
# Written by tests/chaos/lib/devstack.sh: the APPLIANCE's base config
# ($base), and the e2e exports (as tests/e2e/exit_test.sh writes them).
%include "/etc/ganesha/omnifile-appliance-base.conf"
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
$more
EOF
}

# NFSv3 locks (NLM) need, next to Ganesha (docs/milestones/results/wp3.md, "NLM"):
# - rpc.statd: Ganesha registers each locking client with it (SM_MON), and refuses
#   NLM locks (NLM4_DENIED_NOLOCKS) when it cannot;
# - sm-notify -f each time Ganesha starts, before Ganesha's first act (SM_UNMON_ALL,
#   which makes statd forget every client): it tells the clients that held locks that
#   the server restarted, and their lockd reclaims them in grace. Without it a Ganesha
#   restart loses every NFSv3 lock silently, and after grace another client gets it.
#   It must run with full privileges ("+"), not in Ganesha's capability set.
nlm_server_on() {
  systemctl start rpcbind.socket rpc-statd || return 1
  install -d "$(dirname "$GANESHA_NLM_DROPIN")"
  cat >"$GANESHA_NLM_DROPIN" <<'EOF'
# tests/chaos/lib/devstack.sh nlm_server_on (the appliance needs the same, persistently)
[Unit]
Wants=rpc-statd.service
After=rpc-statd.service
[Service]
# "+": outside the unit's sandbox. sm-notify drops its own privileges
# (PR_CAPBSET_DROP, then the statd user); under Ganesha's CapabilityBoundingSet
# (no CAP_SETPCAP) that fails, and the backgrounded sm-notify exits having moved
# every client's record to sm.bak and told none of them (measured).
ExecStartPre=-+/usr/sbin/sm-notify -f
EOF
  # Records left by the grace test's earlier client namespaces, which are gone.
  rm -f /var/lib/nfs/sm/10.231.0.* /var/lib/nfs/sm.bak/10.231.0.*
  systemctl daemon-reload
}
nlm_server_off() {
  rm -f "$GANESHA_NLM_DROPIN"
  systemctl daemon-reload
}

worker_env() {
  # systemd lets an EnvironmentFile= override Environment=, so a verify-only drop-in a lane
  # left behind would win over these settings: the 2026-11-08 regression's soak stubbed
  # nothing for 6 hours because large_copy_test.sh, run before it, left one.
  rm -f "$E2E_VERIFY_DROPIN" "$E2E_WORKER_ENV"
  install -d "$(dirname "$WORKER_DROPIN")"
  {
    echo "# tests/chaos/lib/devstack.sh worker_env"
    echo "[Service]"
    local kv
    for kv in "$@"; do echo "Environment=$kv"; done
  } >"$WORKER_DROPIN"
  systemctl daemon-reload
  systemctl restart omnifile-dev-worker
}
# What the running dev worker really has for K, from its process's environment: the drop-ins'
# Environment= and EnvironmentFile= as systemd combined them. Empty when unset or not running.
# Right after a restart the unit's main process is still systemd's own child, about to exec
# the worker, and its environment is not the worker's yet: read at once, K looked unset and
# the soak refused to start (2026-11-11). So an empty answer is looked for again, for up to
# WORKER_EFFECTIVE_WAIT_S seconds (10).
worker_effective() {
  local pid v end
  end=$(($(date +%s) + ${WORKER_EFFECTIVE_WAIT_S:-10}))
  while :; do
    pid=$(systemctl show -p MainPID --value omnifile-dev-worker 2>/dev/null)
    if [ -n "$pid" ] && [ "$pid" != 0 ]; then
      v=$(tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null | sed -n "s/^$1=//p" | tail -n 1)
      if [ -n "$v" ]; then
        echo "$v"
        return 0
      fi
    fi
    [ "$(date +%s)" -lt "$end" ] || return 0
    sleep 0.2
  done
}
worker_env_clear() {
  rm -f "$WORKER_DROPIN"
  systemctl daemon-reload
}

ganesha_start_wait() { # SINCE: started, connected to the daemon, out of grace
  systemctl start nfs-ganesha-omni || return 1
  ds_wait 30 bash -c "journalctl -q -o cat -u nfs-ganesha-omni --since @$1 | grep -q 'connected to the OmniFile daemon'" || return 1
  ds_wait 150 bash -c "journalctl -q -o cat -u nfs-ganesha-omni --since @$1 | grep -q 'NOT IN GRACE'"
}
