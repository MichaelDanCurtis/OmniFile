#!/usr/bin/env bash
# Run the OmniFile service side for the M1 end-to-end test on the edge machine
# itself (dev VM or the e2e CI runner): Postgres 17, the sim Azure Blob HTTP
# facade, `enrol`, `api` and `worker`, each as a systemd unit. Runs as root.
#
#   install-service.sh            # installs, generates PKI, migrates, (re)starts
#
# Environment:
#   OMNIFILE_DATABASE_URL  use this Postgres (CI: a postgres:17 service
#                          container) instead of installing postgresql-17
#   REPO                   the repository (default: this script's ../..)
#
# Everything generated (dev CA, server certificate, tokens, the sim's key) is
# under /etc/omnifile-dev and /var/lib/omnifile-dev, never in the repository.
# Ports, all on 127.0.0.1: sim 10000 (http, two-account layout), sim-worm 10001
# (http, WORM layout), api 8443 (mTLS), enrol 8444 (TLS).
# Both sims, the api and the worker share one virtual clock file,
# $VAR/clock-offset ("+<seconds>", normally +0): POST /_sim/advance moves it
# (tests/e2e/m3_exit_test.sh; dev mode only).
# The worker is installed but not started: tests/e2e starts it when it wants
# the "stub after N minutes" policy to run (N = OMNIFILE_STUB_AFTER_MINUTES, 0).
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ETC=/etc/omnifile-dev
VAR=/var/lib/omnifile-dev
VENV=/opt/omnifile-service/venv
PKI=$ETC/pki
UNITS=(omnifile-dev-sim omnifile-dev-sim-worm omnifile-dev-enrol omnifile-dev-api omnifile-dev-worker)

log() { printf '[install-service] %s\n' "$*"; }
die() { printf '[install-service] ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "must run as root"
export DEBIAN_FRONTEND=noninteractive
install -d -m 0755 "$ETC" "$VAR"
install -d -m 0700 "$PKI"

# 1. Postgres 17 ------------------------------------------------------------------
if [ -z "${OMNIFILE_DATABASE_URL:-}" ]; then
  if ! dpkg-query -W -f='${Status}' postgresql-17 2>/dev/null | grep -q 'install ok installed'; then
    log "installing postgresql-17 (PGDG)"
    apt-get install -y -qq postgresql-common >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y -qq postgresql-common >/dev/null; }
    /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y >/dev/null 2>&1 \
      || die "could not add the PGDG repository"
    apt-get install -y -qq -o DPkg::Lock::Timeout=600 postgresql-17 >/dev/null
  fi
  systemctl enable --now postgresql >/dev/null
  pw_file=$ETC/db-password
  [ -s "$pw_file" ] || (umask 077; head -c 24 /dev/urandom | base64 | tr -d '/+=' >"$pw_file")
  pw=$(cat "$pw_file")
  cluster_port=$(pg_lsclusters -h | awk '$1 == "17" {print $3; exit}')
  [ -n "$cluster_port" ] || die "no postgresql 17 cluster"
  sudo -u postgres psql -qAt -p "$cluster_port" <<SQL >/dev/null
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'omnifile') THEN
    CREATE ROLE omnifile LOGIN PASSWORD '$pw';
  ELSE
    ALTER ROLE omnifile PASSWORD '$pw';
  END IF;
END \$\$;
SQL
  sudo -u postgres psql -qAt -p "$cluster_port" -c "SELECT 1 FROM pg_database WHERE datname = 'omnifile'" | grep -q 1 \
    || sudo -u postgres createdb -p "$cluster_port" -O omnifile omnifile
  OMNIFILE_DATABASE_URL="postgresql://omnifile:$pw@127.0.0.1:$cluster_port/omnifile"
fi

# libfaketime: the M3 test runs Postgres on the shared virtual clock.
if ! dpkg-query -W -f='${Status}' libfaketime 2>/dev/null | grep -q 'install ok installed'; then
  apt-get install -y -qq -o DPkg::Lock::Timeout=600 libfaketime >/dev/null 2>&1 \
    || { apt-get update -qq; apt-get install -y -qq libfaketime >/dev/null; }
fi

# 2. Python environment ----------------------------------------------------------------
if ! command -v uv >/dev/null; then
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
fi
log "syncing the service environment into $VENV"
# A private environment: never the repository's service/.venv (on the dev VM
# that is the Mac's, through the shared mount).
# Non-editable, and rebuilt from the checkout on every run: the installed units
# must not depend on the checkout they were installed from. An editable install
# leaves a .pth pointing into it, so deleting a work checkout (or a worktree
# whose /work mount moved on) broke every dev unit at start. uv reinstalls a
# non-editable local project only when pyproject.toml changes, so force it.
UV_PROJECT_ENVIRONMENT=$VENV UV_PYTHON_PREFERENCE=only-managed \
  uv sync --quiet --frozen --no-dev --no-editable --reinstall-package omnifile-service \
  --python 3.12 --project "$REPO/service"
# Prove it: nothing in the environment may point back into a checkout.
if "$VENV/bin/python" -I - <<'PY'
import importlib.util, pathlib, sys
spec = importlib.util.find_spec("omnifile_service")
where = pathlib.Path(spec.origin).resolve()
venv = pathlib.Path(sys.prefix).resolve()
sys.exit(0 if venv in where.parents else 1)
PY
then :; else die "omnifile_service is imported from outside $VENV (an editable install?)"; fi
PY=$VENV/bin/python

# 3. PKI: the OmniFile dev CA signs edge certificates and the service's TLS
#    certificates. The edge pins the CA it sees in the server's chain.
if [ ! -s "$PKI/ca.key" ]; then
  log "generating the dev CA"
  "$PY" -m omnifile_service.enrol init-dev-ca "$PKI/ca.key" "$PKI/ca.crt" >/dev/null
fi
if [ ! -s "$PKI/server.crt" ]; then
  log "issuing the service TLS certificate (127.0.0.1, localhost)"
  openssl ecparam -name prime256v1 -genkey -noout -out "$PKI/server.key" 2>/dev/null
  openssl req -new -key "$PKI/server.key" -subj "/CN=omnifile-service-dev" -out "$PKI/server.csr" 2>/dev/null
  printf 'subjectAltName=IP:127.0.0.1,DNS:localhost\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature\nbasicConstraints=critical,CA:FALSE\n' >"$PKI/server.ext"
  openssl x509 -req -in "$PKI/server.csr" -CA "$PKI/ca.crt" -CAkey "$PKI/ca.key" -CAcreateserial \
    -days 365 -sha256 -extfile "$PKI/server.ext" -out "$PKI/server.crt" 2>/dev/null
  cat "$PKI/server.crt" "$PKI/ca.crt" >"$PKI/server-chain.pem"
  rm -f "$PKI/server.csr" "$PKI/server.ext"
fi
chmod 600 "$PKI"/*.key

# 4. Settings ----------------------------------------------------------------------------
secret() { head -c 32 /dev/urandom | base64 | tr -d '\n'; }
env_file=$ETC/service.env
if [ ! -s "$env_file" ] || ! grep -q "^OMNIFILE_DATABASE_URL=$OMNIFILE_DATABASE_URL\$" "$env_file"; then
  (umask 077; cat >"$env_file" <<EOF
OMNIFILE_DATABASE_URL=$OMNIFILE_DATABASE_URL
OMNIFILE_CA_CERT=$PKI/ca.crt
OMNIFILE_DEV_CA_KEY=$PKI/ca.key
OMNIFILE_DEV_CA_CERT=$PKI/ca.crt
OMNIFILE_API_CLIENT_AUTH=direct_tls
OMNIFILE_API_TLS_CERT=$PKI/server-chain.pem
OMNIFILE_API_TLS_KEY=$PKI/server.key
OMNIFILE_API_URL=https://127.0.0.1:8443
OMNIFILE_API_HOST=127.0.0.1
OMNIFILE_ENROL_HOST=127.0.0.1
OMNIFILE_DEV_MODE=1
OMNIFILE_ENROL_DEV_TOKEN=$(secret)
OMNIFILE_API_LEASE_KEY=$(secret)
OMNIFILE_STUB_AFTER_MINUTES=0
PYTHONUNBUFFERED=1
EOF
  )
fi
# The dev sign token is refused without dev mode (M1 security review); older env files lack it.
grep -q '^OMNIFILE_DEV_MODE=' "$env_file" || echo 'OMNIFILE_DEV_MODE=1' >>"$env_file"
# The shared virtual clock (backends/sim/clock.py OffsetClock), +0 = real time.
grep -q '^OMNIFILE_DEV_CLOCK_FILE=' "$env_file" || echo "OMNIFILE_DEV_CLOCK_FILE=$VAR/clock-offset" >>"$env_file"
[ -s "$VAR/clock-offset" ] || echo '+0' >"$VAR/clock-offset"
chmod 0644 "$VAR/clock-offset"

# 5. Units ---------------------------------------------------------------------------------
unit() { # name description exec [extra [Service] lines]
  local name=$1 desc=$2 exec=$3 extra=${4:-}
  cat >"/etc/systemd/system/$name.service" <<EOF
# Generated by dev/scripts/install-service.sh (dev and e2e only).
[Unit]
Description=$desc
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$env_file
WorkingDirectory=$VAR
ExecStart=$exec
Restart=on-failure
RestartSec=2
$extra
EOF
}
# --s3: also an S3-flavoured sim, for S3-resident files (M5, tests/e2e/m5_exit_test.sh).
unit omnifile-dev-sim "OmniFile sim Azure Blob facade (dev)" \
  "$PY -m omnifile_service.backends.sim_http --host 127.0.0.1 --port 10000 --key-file $VAR/sim-key.json --clock offset --clock-file $VAR/clock-offset --admin --s3"
unit omnifile-dev-sim-worm "OmniFile sim Azure Blob facade, WORM layout (dev)" \
  "$PY -m omnifile_service.backends.sim_http --host 127.0.0.1 --port 10001 --layout worm --key-file $VAR/sim-worm-key.json --clock offset --clock-file $VAR/clock-offset --admin"
unit omnifile-dev-enrol "OmniFile enrol (dev CA, TLS)" \
  "$PY -m uvicorn --factory omnifile_service.enrol.__main__:build --host 127.0.0.1 --port 8444 --ssl-certfile $PKI/server-chain.pem --ssl-keyfile $PKI/server.key --no-server-header" \
  "Environment=PORT=8444"
unit omnifile-dev-api "OmniFile api (mTLS, dev)" "$PY -m omnifile_service.api" \
  "Environment=PORT=8443
# The api reads the sim's key file once; it must start after the facade.
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 50); do [ -s $VAR/sim-key.json ] && exit 0; sleep 0.2; done; exit 1'"
unit omnifile-dev-worker "OmniFile worker (dev)" "$PY -m omnifile_service.worker"
# The facade must come up before the api reads its key, and restart with it.
sed -i 's/^After=network-online.target postgresql.service$/After=network-online.target postgresql.service omnifile-dev-sim.service\nBindsTo=omnifile-dev-sim.service/' \
  /etc/systemd/system/omnifile-dev-api.service
systemctl daemon-reload

# 6. Migrate, then (re)start ----------------------------------------------------------------
log "applying catalog migrations"
OMNIFILE_DATABASE_URL=$OMNIFILE_DATABASE_URL "$PY" -m omnifile_service.catalog.migrate
systemctl stop omnifile-dev-worker 2>/dev/null || true
rm -f "$VAR/sim-key.json" "$VAR/sim-worm-key.json"
systemctl enable omnifile-dev-sim-worm >/dev/null 2>&1 || true
systemctl restart omnifile-dev-sim omnifile-dev-sim-worm omnifile-dev-enrol
systemctl restart omnifile-dev-api
for i in $(seq 1 60); do
  ok=0
  for port in 8443 8444; do
    (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && ok=$((ok + 1))
  done
  [ "$ok" -eq 2 ] && [ -s "$VAR/sim-key.json" ] && [ -s "$VAR/sim-worm-key.json" ] && break
  sleep 0.5
  [ "$i" -lt 60 ] || { systemctl --no-pager status "${UNITS[@]}" | tail -40 >&2; die "service did not come up"; }
done
log "service up: sim :10000, sim-worm :10001, api :8443, enrol :8444 (env $env_file)"
