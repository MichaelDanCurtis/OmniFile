#!/usr/bin/env bash
# The M4 parts of the dev stack on the edge machine (dev VM), after install-service.sh:
# Teams through the fakes and the real relay, and the portal (`web`), as systemd units next
# to omnifile-dev-api and omnifile-dev-worker. Runs as root.
#
#   install-m4.sh          # keys and certificates, fakes seed from AD, units, drop-ins, restart
#
# What it adds (all on 127.0.0.1; generated files under /etc/omnifile-dev/m4):
#   omnifile-dev-fakes  :18460 fake Graph + Entra token endpoint, :18461 fake Bot Connector,
#                       fake Teams JWKS and /_fake controls (dev/m4/fakes.py). The fake
#                       Graph knows the dev domain's real users and groups by SID (wbinfo),
#                       so a real SMB or NFS read by an AD user gets that user a card.
#   omnifile-dev-relay  :18462 the relay from relay/ (dev/m4/relay.py), mutual TLS to api with
#                       a relay certificate from the dev CA, pinned in api
#   omnifile-dev-web    :18445 web over TLS, dev OIDC issuer (the AD users, by SID), portal/dist
#   drop-ins            api: the intent public key, tenant, relay pin;
#                       worker: Teams and Graph through the fakes, the intent signing key
# The process-based equivalent for a Mac or CI runner is dev/m4/stack.py.
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ETC=/etc/omnifile-dev
M4=$ETC/m4
PKI=$ETC/pki
VENV=/opt/omnifile-service/venv
RELAY_VENV=/opt/omnifile-relay/venv
PY=$VENV/bin/python

log() { printf '[install-m4] %s\n' "$*"; }
die() { printf '[install-m4] ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "must run as root"
[ -x "$PY" ] && [ -s "$ETC/service.env" ] || die "run install-service.sh first"
[ -f "$REPO/portal/dist/index.html" ] || die "portal/dist is missing: build the portal on the Mac (npm run build)"
install -d -m 0700 "$M4"

log "syncing the relay environment into $RELAY_VENV"
# Non-editable (and reinstalled every run), as install-service.sh does for the
# service: the units must not depend on the checkout they were installed from.
UV_PROJECT_ENVIRONMENT=$RELAY_VENV UV_PYTHON_PREFERENCE=only-managed \
  uv sync --quiet --frozen --no-dev --no-editable --reinstall-package omnifile-relay \
  --python 3.12 --project "$REPO/relay"

log "keys, certificates, secrets and the fakes' seed (AD users and groups from winbind)"
"$PY" - "$REPO" "$M4" "$PKI" <<'EOF'
import asyncio, base64, hashlib, ipaddress, json, secrets, subprocess, sys, uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

repo, m4, pki = (Path(a) for a in sys.argv[1:])
sys.path.insert(0, str(repo / "dev" / "m4"))
import world
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from omnifile_service.enrol.ca import FileCASigner

def secret(name):
    p = m4 / f"{name}.secret"
    if not p.exists():
        p.write_text(secrets.token_urlsafe(24)); p.chmod(0o600)
    return p.read_text()

intent = m4 / "intent.pem"
if not intent.exists():
    intent.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    intent.chmod(0o600)
key = serialization.load_pem_private_key(intent.read_bytes(), None)
raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
public = json.dumps({world.INTENT_KID: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()})

relay_crt, relay_key = m4 / "relay.crt", m4 / "relay.key"
if not relay_crt.exists():
    ca = FileCASigner.from_files(pki / "ca.key", pki / "ca.crt")
    k = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    b = (x509.CertificateBuilder()
         .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "omnifile-relay")]))
         .issuer_name(ca.ca_certificate.subject).public_key(k.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=90))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
         .add_extension(x509.SubjectAlternativeName(
             [x509.UniformResourceIdentifier("urn:omnifile:relay")]), critical=False))
    cert = asyncio.run(ca.sign(b))
    relay_key.write_bytes(k.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    relay_key.chmod(0o600)
    relay_crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + (pki / "ca.crt").read_bytes())
leaf = x509.load_pem_x509_certificates(relay_crt.read_bytes())[0]
pin = leaf.fingerprint(hashes.SHA256()).hex()

# The dev domain's people, by their real SIDs (dev/scripts/dc-provision.sh seeds them).
def sid_of(name):
    out = subprocess.run(["wbinfo", "--name-to-sid", name], capture_output=True, text=True)
    return out.stdout.split()[0] if out.returncode == 0 and out.stdout else None

oid = lambda n: str(uuid.uuid5(uuid.NAMESPACE_URL, f"omnifile-dev/{n}"))
groups = {g: sid_of(f"OMNI\\{g}") for g in
          ("Proj-Readers", "Proj-Writers", "Proj-Owners", "OmniFile-Admins", "Proj-Readers-Nested")}
member = {"geo-reader": ["Proj-Readers"], "geo-writer": ["Proj-Writers"], "geo-none": [],
          "omni-admin": ["OmniFile-Admins"], "geo-owner": ["Proj-Owners"],
          "geo-nested": ["Proj-Readers-Nested"]}
users = []
for u, gs in member.items():
    s = sid_of(f"OMNI\\{u}")
    if s:
        users.append({"id": oid(u), "sid": s, "displayName": u, "userPrincipalName": f"{u}@omni.test",
                      "mail": f"{u}@omni.test", "groups": [oid(g) for g in gs]})
graph = {"users": users,
         "groups": [{"id": oid(g), "sid": s, "displayName": g, "samAccountName": g,
                     "groups": [oid("Proj-Readers")] if g == "Proj-Readers-Nested" else []}
                    for g, s in groups.items() if s],
         "blocked": []}
(m4 / "fakes-seed.json").write_text(json.dumps({
    "tenant": world.TENANT, "bot_app_id": world.BOT_APP_ID, "bot_secret": secret("bot"),
    "graph_client_id": world.GRAPH_CLIENT_ID, "graph_secret": secret("graph"), "graph": graph},
    indent=1))
roles = {"omni-admin": ["ArchiveAdmin"]}
(m4 / "dev-idp-users.json").write_text(json.dumps([
    {"key": u["displayName"], "oid": u["id"], "name": u["displayName"],
     "upn": u["userPrincipalName"], "onprem_sid": u["sid"],
     "groups": [groups[g] for g in member[u["displayName"]] if groups.get(g)]
               + ([groups["Proj-Readers"]] if u["displayName"] == "geo-nested" else []),
     "roles": roles.get(u["displayName"], ["User"])} for u in users], indent=1))
cursor = base64.b64encode(hashlib.sha256(secret("cursor").encode()).digest()).decode()
(m4 / "m4.env").write_text("\n".join([
    f"OMNIFILE_TENANT_ID={world.TENANT}",
    f"OMNIFILE_TEAMS_INTENT_PUBLIC_KEYS={public}",
    f"OMNIFILE_TEAMS_RELAY_CERT_SHA256={pin}",
    f"OMNIFILE_RELAY_INTENT_PUBLIC_KEYS={public}",
    f"OMNIFILE_WEB_CURSOR_KEY={cursor}",
]) + "\n")
(m4 / "m4.env").chmod(0o600)
print(f"{len(users)} users, {len([s for s in groups.values() if s])} groups; relay pin {pin[:16]}…")
EOF

log "units and drop-ins"
PORTAL_URL=https://localhost:18445
unit() { # name description exec env-lines
  cat >"/etc/systemd/system/$1.service" <<EOF
# Generated by dev/scripts/install-m4.sh (dev and e2e only).
[Unit]
Description=$2
After=network-online.target omnifile-dev-api.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ETC/service.env
EnvironmentFile=$M4/m4.env
WorkingDirectory=/var/lib/omnifile-dev
ExecStart=$3
Restart=on-failure
RestartSec=2
${4:-}
EOF
}
unit omnifile-dev-fakes "OmniFile fake Graph, Bot Connector and Teams (dev)" \
  "$PY $REPO/dev/m4/fakes.py --seed $M4/fakes-seed.json --graph-port 18460 --bot-port 18461 --relay-url http://127.0.0.1:18462"
unit omnifile-dev-relay "OmniFile Teams relay, real code, fake Bot Framework keys (dev)" \
  "$RELAY_VENV/bin/python $REPO/dev/m4/relay.py --port 18462 --fakes-url http://127.0.0.1:18461" \
  "Environment=OMNIFILE_RELAY_BOT_APP_ID=$(cd "$REPO/dev/m4" && "$PY" -c 'import world; print(world.BOT_APP_ID)')
Environment=OMNIFILE_RELAY_TENANT_ID=$(cd "$REPO/dev/m4" && "$PY" -c 'import world; print(world.TENANT)')
Environment=OMNIFILE_RELAY_SERVICE_BASE_URL=https://127.0.0.1:8443
Environment=OMNIFILE_RELAY_SERVICE_CLIENT_CERT=$M4/relay.crt
Environment=OMNIFILE_RELAY_SERVICE_CLIENT_KEY=$M4/relay.key
Environment=OMNIFILE_RELAY_SERVICE_CA_BUNDLE=$PKI/ca.crt"
unit omnifile-dev-web "OmniFile web: portal and partner API, dev OIDC issuer (dev)" \
  "$PY -m uvicorn --factory omnifile_service.web.__main__:build --host 127.0.0.1 --port 18445 --ssl-certfile $PKI/server-chain.pem --ssl-keyfile $PKI/server.key --http h11 --no-proxy-headers --no-server-header" \
  "Environment=OMNIFILE_WEB_ORIGIN=$PORTAL_URL
Environment=OMNIFILE_WEB_TENANT_ID=$(cd "$REPO/dev/m4" && "$PY" -c 'import world; print(world.TENANT)')
Environment=OMNIFILE_WEB_CLIENT_ID=omnifile-portal-dev
Environment=OMNIFILE_WEB_OIDC=dev
Environment=OMNIFILE_WEB_STATIC_DIR=$REPO/portal/dist
Environment=OMNIFILE_DEV_IDP_USERS=$M4/dev-idp-users.json"
for u in api worker; do install -d "/etc/systemd/system/omnifile-dev-$u.service.d"; done
cat >/etc/systemd/system/omnifile-dev-api.service.d/20-m4-teams.conf <<EOF
# Generated by dev/scripts/install-m4.sh: Teams intents from the dev relay.
[Service]
EnvironmentFile=$M4/m4.env
Environment=OMNIFILE_PORTAL_URL=$PORTAL_URL
EOF
cat >/etc/systemd/system/omnifile-dev-worker.service.d/20-m4-teams.conf <<EOF
# Generated by dev/scripts/install-m4.sh: Teams and Graph through the fakes.
[Service]
EnvironmentFile=$M4/m4.env
Environment=OMNIFILE_TEAMS_BOT_APP_ID=$(cd "$REPO/dev/m4" && "$PY" -c 'import world; print(world.BOT_APP_ID)')
Environment=OMNIFILE_TEAMS_APP_ID=$(cd "$REPO/dev/m4" && "$PY" -c 'import world; print(world.TEAMS_APP_ID)')
Environment=OMNIFILE_TEAMS_BOT_CREDENTIAL=$M4/bot.secret
Environment=OMNIFILE_TEAMS_SERVICE_URL=http://127.0.0.1:18461/
Environment=OMNIFILE_TEAMS_INTENT_SIGNING_KEY=$M4/intent.pem
Environment=OMNIFILE_TEAMS_INTENT_KID=intent-dev
Environment=OMNIFILE_PORTAL_URL=$PORTAL_URL
Environment=OMNIFILE_NOTIFY_SENDER=omnifile@omni.test
Environment=OMNIFILE_GRAPH_CREDENTIAL=$M4/graph.secret
Environment=OMNIFILE_GRAPH_CLIENT_ID=$(cd "$REPO/dev/m4" && "$PY" -c 'import world; print(world.GRAPH_CLIENT_ID)')
Environment=OMNIFILE_GRAPH_URL=http://127.0.0.1:18460/v1.0
Environment=OMNIFILE_ENTRA_AUTHORITY=http://127.0.0.1:18460
Environment=OMNIFILE_BOTCONNECTOR_TOKEN_URL=http://127.0.0.1:18461/token
EOF
systemctl daemon-reload
systemctl restart omnifile-dev-fakes omnifile-dev-api omnifile-dev-relay omnifile-dev-web
if systemctl is-active -q omnifile-dev-worker; then systemctl restart omnifile-dev-worker; fi
for i in $(seq 1 60); do
  ok=0
  for port in 8443 18445 18460 18461 18462; do (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && ok=$((ok + 1)); done
  [ "$ok" -eq 5 ] && break
  sleep 0.5
  [ "$i" -lt 60 ] || { systemctl --no-pager status omnifile-dev-fakes omnifile-dev-relay omnifile-dev-web | tail -40 >&2; die "M4 units did not come up"; }
done
log "up: web :18445, fake Graph :18460, fake Bot Connector :18461, relay :18462 (Teams via $M4)"
