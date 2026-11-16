#!/usr/bin/env bash
# Run each built image with the command its app runs in Azure, against Postgres, and check it is up.
#
#   deploy/images/smoke.sh [--only api|enrol|web|worker|verifier|relay]... [--lock FILE]
#                          [--database-url URL] [--remove]
#
# Arguments:
#   --only APP          smoke only this app (repeatable). Default: every app whose image the lock
#                       names and Docker has. api needs enrol (it gets its certificate there)
#   --lock FILE         default deploy/images/images.lock.json (from build.sh)
#   --database-url URL  a Postgres server to make a throwaway database on (default the shared
#                       test server, postgresql://postgres:omnifile@127.0.0.1:55433/postgres)
#   --remove            remove each image's local tag once its apps are done (disk)
#
# Each app runs as Container Apps runs it: `docker run --entrypoint` with the command from the
# compiled deploy/azure/main.bicep (appCommands), so a wrong entrypoint fails here. Settings are
# dev ones (a dev CA made now, dev tokens, the dev sign-in issuer), never an account's.
#   api       direct TLS (the default, threat model T5): its server certificate from enrol; then
#             an edge enrols, calls api over mutual TLS, and a forged X-Forwarded-Client-Cert
#             is refused (smoke_probe.py t5)
#   enrol     healthy, and signs api's certificate
#   web       healthy with the dev issuer
#   worker    still running after 30 s, nothing on stderr that ends it
#   verifier  one pass over the (empty) verify queue: exits 0 within 3 minutes, with its summary line
#   relay     healthy, with a relay certificate from the dev CA
# "Healthy" is the image's own HEALTHCHECK (GET /healthz on 127.0.0.1:8080).
#
# Output: one "ok"/"FAIL" line per check, the last log lines of a failed container; exit 1 on
# any failure. It removes its containers, its network and its database on exit.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
lock="$here/images.lock.json"
db_url="postgresql://postgres:omnifile@127.0.0.1:55433/postgres"
only=()
remove=0

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --only) [ $# -ge 2 ] || usage; only+=("$2"); shift 2 ;;
    --lock) [ $# -ge 2 ] || usage; lock="$2"; shift 2 ;;
    --database-url) [ $# -ge 2 ] || usage; db_url="$2"; shift 2 ;;
    --remove) remove=1; shift ;;
    -h|--help) usage ;;
    *) echo "smoke.sh: unknown argument $1" >&2; usage ;;
  esac
done
[ -f "$lock" ] || { echo "smoke.sh: no $lock (run build.sh)" >&2; exit 2; }
command -v bicep >/dev/null || { echo "smoke.sh: needs the Bicep CLI (the commands come from main.bicep)" >&2; exit 2; }

py() { uv --directory "$repo/service" run --quiet python "$@"; }
probe() { py "$here/smoke_probe.py" "$@"; }

run_id="omnifile-smoke-$$"
work="$(mktemp -d)"
net="$run_id"
containers=()
smoke_db=""
container_db=""
fail=0
cleanup() {
  for c in "${containers[@]+"${containers[@]}"}"; do docker rm -f "$c" >/dev/null 2>&1 || true; done
  docker network rm "$net" >/dev/null 2>&1 || true
  [ -z "$smoke_db" ] || probe db-drop "$smoke_db" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

# The commands, from the template.
bicep build "$repo/deploy/azure/main.bicep" --outfile "$work/main.json" >/dev/null
command_of() {
  python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["variables"]["appCommands"][sys.argv[2]]))' \
    "$work/main.json" "$1"
}
tag_of() {  # the local tag of the image an app runs, or nothing
  python3 -c 'import json,sys
l = json.load(open(sys.argv[1])); i = l["images"].get(l["apps"][sys.argv[2]])
print(i["local_tag"] if i else "")' "$lock" "$1"
}
have() { local t; t="$(tag_of "$1")"; [ -n "$t" ] && docker image inspect "$t" >/dev/null 2>&1; }

if [ ${#only[@]} -eq 0 ]; then
  for app in enrol api web worker verifier relay; do have "$app" && only+=("$app"); done
fi
[ ${#only[@]} -gt 0 ] || { echo "smoke.sh: no built image to smoke" >&2; exit 2; }
wants() { local a; for a in "${only[@]}"; do [ "$a" = "$1" ] && return 0; done; return 1; }
if wants api && ! wants enrol; then only=(enrol "${only[@]}"); fi
for app in "${only[@]}"; do
  have "$app" || { echo "FAIL $app: its image ($(tag_of "$app")) is not built"; exit 1; }
done

probe pki "$work/pki" >/dev/null
docker network create "$net" >/dev/null
token="smoke-$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
api_name="api.smoke.test"

needs_db=0
for app in "${only[@]}"; do [ "$app" = relay ] || needs_db=1; done
if [ "$needs_db" = 1 ]; then
  smoke_db="$(probe db-create "$db_url")"
  # The containers reach the host's Postgres through Docker Desktop's host name.
  container_db="$(printf '%s' "$smoke_db" | sed -E 's/(host=|@)(127\.0\.0\.1|localhost)/\1host.docker.internal/')"
  migrator="$(tag_of worker)"; docker image inspect "$migrator" >/dev/null 2>&1 || migrator="$(tag_of web)"
  docker run --rm --platform linux/amd64 --network "$net" --add-host host.docker.internal:host-gateway \
    -e OMNIFILE_DATABASE_URL="$container_db" --entrypoint python "$migrator" \
    -m omnifile_service.catalog.migrate >"$work/migrate.log" 2>&1 \
    || { echo "FAIL catalog migrations"; tail -20 "$work/migrate.log"; exit 1; }
  echo "ok   catalog migrated in a throwaway database"
fi

start() {  # app, then docker run options; the command comes from main.bicep
  local app="$1"; shift
  local name="$run_id-$app" tag cmd=()
  tag="$(tag_of "$app")"
  while IFS= read -r line; do cmd+=("$line"); done < <(command_of "$app")
  docker run -d --name "$name" --network "$net" --network-alias "$app" --platform linux/amd64 \
    --add-host host.docker.internal:host-gateway -v "$work/pki:/smoke:ro" "$@" \
    --entrypoint "${cmd[0]}" "$tag" "${cmd[@]:1}" >/dev/null
  containers+=("$name")
  echo "     $app: ${cmd[*]} ($tag)"
}

healthy() {  # app, seconds
  local name="$run_id-$1" deadline=$((SECONDS + $2)) status
  while [ $SECONDS -lt $deadline ]; do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}} {{.State.Status}}' "$name" 2>/dev/null || echo "gone gone")"
    case "$status" in
      "healthy running") echo "ok   $1 healthy"; return 0 ;;
      *exited*|*dead*|gone*) break ;;
    esac
    sleep 2
  done
  echo "FAIL $1: $status"; docker logs --tail 30 "$name" 2>&1 | sed 's/^/     /'; fail=1; return 1
}

stays_up() {  # app, seconds: no health check (no HTTP), so: still running, never restarted
  local name="$run_id-$1"
  sleep "$2"
  if [ "$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null)" = running ]; then
    echo "ok   $1 running after $2 s"
  else
    echo "FAIL $1 stopped"; docker logs --tail 30 "$name" 2>&1 | sed 's/^/     /'; fail=1
  fi
}

exits_cleanly() {  # app, seconds: a run-to-completion job, so: exits 0, with its summary
  local name="$run_id-$1" deadline=$((SECONDS + $2)) status=""
  while [ $SECONDS -lt $deadline ]; do
    status="$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$name" 2>/dev/null || echo "gone 1")"
    case "$status" in exited*|dead*|gone*) break ;; esac
    sleep 2
  done
  if [ "$status" = "exited 0" ] && docker logs "$name" 2>&1 | grep -q "verifier pass:"; then
    echo "ok   $1 ran one pass and exited 0"
  else
    echo "FAIL $1: $status"; docker logs --tail 30 "$name" 2>&1 | sed 's/^/     /'; fail=1
  fi
}

port_of() { docker port "$run_id-$1" "$2" | head -1 | sed 's/.*://'; }

for app in "${only[@]}"; do
  case "$app" in
    enrol)
      start enrol -p 127.0.0.1::8080 -e OMNIFILE_DATABASE_URL="$container_db" -e OMNIFILE_DEV_MODE=1 \
        -e OMNIFILE_DEV_CA_KEY=/smoke/ca.key -e OMNIFILE_DEV_CA_CERT=/smoke/ca.pem \
        -e OMNIFILE_ENROL_DEV_TOKEN="$token" -e OMNIFILE_ENROL_API_SERVER_NAMES="$api_name" \
        -e OMNIFILE_API_URL="https://$api_name:8443"
      healthy enrol 180 || true ;;
    api)
      start api -p 127.0.0.1::8443 -e OMNIFILE_DATABASE_URL="$container_db" -e OMNIFILE_DEV_MODE=1 \
        -e OMNIFILE_CA_CERT=/smoke/ca.pem -e OMNIFILE_API_CLIENT_AUTH=direct_tls -e PORT=8443 \
        -e OMNIFILE_API_SERVER_CERT_URL=http://enrol:8080/internal/v0/sign \
        -e OMNIFILE_ENROL_SIGN_URL=http://enrol:8080/internal/v0/sign \
        -e OMNIFILE_ENROL_DEV_TOKEN="$token" -e OMNIFILE_API_URL="https://$api_name:8443"
      if healthy api 180; then
        if probe t5 --db "$smoke_db" --enrol "http://127.0.0.1:$(port_of enrol 8080)" \
          --api "127.0.0.1:$(port_of api 8443)" --name "$api_name" --ca "$work/pki/ca.pem"; then :; else
          fail=1; docker logs --tail 30 "$run_id-api" 2>&1 | sed 's/^/     /'
        fi
      fi ;;
    web)
      start web -e OMNIFILE_DATABASE_URL="$container_db" -e OMNIFILE_DEV_MODE=1 -e OMNIFILE_WEB_OIDC=dev \
        -e OMNIFILE_WEB_ORIGIN=https://portal.smoke.test \
        -e OMNIFILE_WEB_TENANT_ID=00000000-0000-0000-0000-00000000000a \
        -e OMNIFILE_WEB_CLIENT_ID=00000000-0000-0000-0000-00000000000b
      healthy web 180 || true ;;
    worker)
      start worker --no-healthcheck -e OMNIFILE_DATABASE_URL="$container_db"
      stays_up worker 30 ;;
    verifier)
      start verifier --no-healthcheck -e OMNIFILE_DATABASE_URL="$container_db"
      exits_cleanly verifier 180 ;;
    relay)
      start relay -e OMNIFILE_RELAY_BOT_APP_ID=00000000-0000-0000-0000-00000000000c \
        -e OMNIFILE_RELAY_TENANT_ID=00000000-0000-0000-0000-00000000000a \
        -e OMNIFILE_RELAY_INTENT_PUBLIC_KEYS="$(cat "$work/pki/intent-keys.json")" \
        -e OMNIFILE_RELAY_SERVICE_BASE_URL="https://$api_name:8443" \
        -e OMNIFILE_RELAY_SERVICE_CLIENT_CERT=/smoke/relay.pem \
        -e OMNIFILE_RELAY_SERVICE_CLIENT_KEY=/smoke/relay.key \
        -e OMNIFILE_RELAY_SERVICE_CA_BUNDLE=/smoke/ca.pem -e PORT=8080
      healthy relay 120 || true ;;
    *) echo "smoke.sh: no app called $app" >&2; exit 2 ;;
  esac
done

if [ "$remove" = 1 ]; then
  for c in "${containers[@]+"${containers[@]}"}"; do docker rm -f "$c" >/dev/null 2>&1 || true; done
  containers=()
  for app in "${only[@]}"; do
    t="$(tag_of "$app")"
    if docker image inspect "$t" >/dev/null 2>&1; then docker image rm "$t" >/dev/null && echo "     removed $t"; fi
  done
fi
exit "$fail"
