"""The M4 dev stack on one machine, as processes (no edge, no kernel features):

    python dev/m4/stack.py up   [--work DIR]   PKI, keys, migrate, start everything
    python dev/m4/stack.py down [--work DIR]   stop everything this stack started
    python dev/m4/stack.py env  [--work DIR]   print stack.json (URLs, files, people)

Runs with the service's Python (service/.venv: `uv sync --project service`). The relay
runs from relay/ in its own environment (relay/.venv). Postgres is not started here:
OMNIFILE_DATABASE_URL names it (CI: a postgres:17 service container; locally any
Postgres 16+). Everything generated lives in the work directory (default
$TMPDIR/omnifile-m4), never in the repository.

| Process | Port (M4_PORT_BASE + n) | What |
|---|---|---|
| sim     | +0  (18400) | sim_http, two-account layout, on the shared virtual clock, /_sim admin |
| sim-worm| +1  (18401) | sim_http, WORM layout (one account, version-level WORM), same clock |
| api     | +43 (18443) | `api`, direct mutual TLS (dev CA), Teams intents (relay pinned) |
| web     | +45 (18445) | `web` over TLS, dev OIDC issuer at /dev-idp, serves portal/dist |
| worker  | -           | the worker: restores, notify (Teams via the fakes), portal jobs |
| graph   | +60 (18460) | fake Microsoft Graph and Entra token endpoint (dev/m4/fakes.py) |
| bot     | +61 (18461) | fake Bot Connector, fake Teams' JWKS and the /_fake controls |
| relay   | +62 (18462) | the real relay (relay/src), mutual TLS to api (dev/m4/relay.py) |

The virtual clock (OMNIFILE_DEV_CLOCK_FILE, `+<seconds>`) is shared by sim, api, the
worker and web's catalog times (WORM deadlines, pins, holds), so a deadline web sets is
judged on the clock the worker sweeps with: POST <sim>/_sim/advance moves them all. Web's
sessions and sign-in, the relay and Postgres keep real time (intents and sessions are
wall-clock by design, teams.md section 2).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import world  # noqa: E402

PROCS = ("sim", "sim-worm", "api", "worker", "web", "fakes", "relay")


def work_dir(arg: str | None) -> Path:
    return Path(arg or os.environ.get("M4_WORK") or Path(tempfile.gettempdir()) / "omnifile-m4")


def ports() -> dict[str, int]:
    base = int(os.environ.get("M4_PORT_BASE", "18400"))
    return {
        "sim": base,
        "sim-worm": base + 1,
        "api": base + 43,
        "web": base + 45,
        "graph": base + 60,
        "bot": base + 61,
        "relay": base + 62,
    }


def service_python() -> str:
    return os.environ.get("M4_SERVICE_PY") or str(REPO / "service" / ".venv" / "bin" / "python")


def relay_python() -> str:
    return os.environ.get("M4_RELAY_PY") or str(REPO / "relay" / ".venv" / "bin" / "python")


# --- PKI and keys -----------------------------------------------------------------------


def _pki(work: Path) -> dict[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    from omnifile_service.enrol.ca import FileCASigner

    pki = work / "pki"
    pki.mkdir(parents=True, exist_ok=True, mode=0o700)
    ca_key, ca_crt = pki / "ca.key", pki / "ca.crt"
    if not ca_key.exists():
        FileCASigner.generate("OmniFile M4 dev CA").write(ca_key, ca_crt)
    ca = FileCASigner.from_files(ca_key, ca_crt)

    def issue(name: str, sans: list, eku) -> tuple[Path, Path]:
        key_path, crt_path = pki / f"{name}.key", pki / f"{name}.crt"
        if crt_path.exists():
            return key_path, crt_path
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"omnifile-{name}")]))
            .issuer_name(ca.ca_certificate.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            # RFC 5280 key identifiers: a strict verifier (VERIFY_X509_STRICT, the
            # default for newer Pythons) refuses a leaf without an AKI.
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    ca.ca_certificate.public_key()  # type: ignore[arg-type]
                ),
                critical=False,
            )
        )
        import asyncio

        cert = asyncio.run(ca.sign(builder))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        key_path.chmod(0o600)
        pem = cert.public_bytes(serialization.Encoding.PEM)
        # The chain, leaf then CA (the edge and the relay pin the CA from it).
        crt_path.write_bytes(pem + ca_crt.read_bytes())
        (pki / f"{name}.sha256").write_text(cert.fingerprint(hashes.SHA256()).hex())
        return key_path, crt_path

    server = issue(
        "server",
        [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")],
        ExtendedKeyUsageOID.SERVER_AUTH,
    )
    relay = issue(
        "relay",
        [x509.UniformResourceIdentifier("urn:omnifile:relay")],
        ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    return {
        "ca_key": str(ca_key),
        "ca_crt": str(ca_crt),
        "server_key": str(server[0]),
        "server_crt": str(server[1]),
        "relay_key": str(relay[0]),
        "relay_crt": str(relay[1]),
        "relay_sha256": (pki / "relay.sha256").read_text(),
    }


def _intent_key(work: Path) -> dict[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = work / "pki" / "intent.pem"
    if not path.exists():
        key = Ed25519PrivateKey.generate()
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        path.chmod(0o600)
    key = serialization.load_pem_private_key(path.read_bytes(), None)
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,  # type: ignore[union-attr]
        serialization.PublicFormat.Raw,
    )
    public = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return {"intent_key": str(path), "intent_public_keys": json.dumps({world.INTENT_KID: public})}


def _secret_file(work: Path, name: str) -> tuple[str, str]:
    path = work / "pki" / f"{name}.secret"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
    return str(path), path.read_text()


# --- processes ----------------------------------------------------------------------------


def _wait_port(port: int, name: str, proc: subprocess.Popen, log: Path, timeout: float = 60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise SystemExit(
                f"{name} exited ({proc.returncode}); see {log}:\n" + log.read_text()[-3000:]
            )
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise SystemExit(f"{name} did not listen on :{port}; see {log}")


def _start(work: Path, name: str, argv: list[str], env: dict[str, str]) -> subprocess.Popen:
    log = work / "logs" / f"{name}.log"
    log.parent.mkdir(exist_ok=True)
    fh = open(log, "ab")  # noqa: SIM115 - the child keeps it
    full = {k: v for k, v in os.environ.items() if not k.startswith("OMNIFILE_")}
    full.update(env)
    full.setdefault("PYTHONUNBUFFERED", "1")
    proc = subprocess.Popen(
        argv, cwd=work, env=full, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True
    )
    (work / "pids").mkdir(exist_ok=True)
    (work / "pids" / name).write_text(str(proc.pid))
    return proc


def _stop(work: Path, name: str) -> None:
    pidf = work / "pids" / name
    if not pidf.exists():
        return
    pid = int(pidf.read_text())
    for sig, wait in ((signal.SIGTERM, 8.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            break
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            continue
        break
    pidf.unlink(missing_ok=True)


def _seed_backend(
    db_url: str,
    sim_url: str,
    key_file: str,
    name: str = "m4-sim",
    container: str = "live/data",
    layout: str = "azure_two_account",
) -> int:
    """A sim backend row, before api and the worker start (they load backends once)."""
    import psycopg
    from psycopg.types.json import Jsonb

    config = {
        "data_plane": "sim_http",
        "url": sim_url,
        "key_file": key_file,
        "container": container,
        "layout": layout,
    }
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("SELECT set_config('omnifile.actor', 'dev:m4-stack', false)")
        row = c.execute(
            "SELECT backend_id FROM catalog.backend WHERE name = %s", (name,)
        ).fetchone()
        if row:
            c.execute(
                "UPDATE catalog.backend SET config = %s WHERE backend_id = %s",
                (Jsonb(config), row[0]),
            )
            return row[0]
        (bid,) = c.execute(
            "INSERT INTO catalog.backend (name, kind, config, private_network)"
            " VALUES (%s, 'sim', %s, false) RETURNING backend_id",
            (name, Jsonb(config)),
        ).fetchone()
        return bid


def up(args: argparse.Namespace) -> None:
    work = work_dir(args.work)
    work.mkdir(parents=True, exist_ok=True)
    db_url = os.environ.get("OMNIFILE_DATABASE_URL")
    if not db_url:
        raise SystemExit("OMNIFILE_DATABASE_URL is required (a Postgres 16+ database)")
    down(args, quiet=True)
    p = ports()
    pki = _pki(work)
    keys = _intent_key(work)
    bot_secret_file, bot_secret = _secret_file(work, "bot")
    graph_secret_file, graph_secret = _secret_file(work, "graph")
    clock_file = work / "clock-offset"
    clock_file.write_text("+0\n")
    sim_key = work / "sim-key.json"
    sim_key.unlink(missing_ok=True)
    worm_key = work / "sim-worm-key.json"
    worm_key.unlink(missing_ok=True)
    py, rpy = service_python(), relay_python()
    url = {k: f"http://127.0.0.1:{v}" for k, v in p.items()}
    url["api"] = f"https://127.0.0.1:{p['api']}"
    url["web"] = f"https://localhost:{p['web']}"
    static = Path(os.environ.get("M4_STATIC_DIR") or REPO / "portal" / "dist")
    if not (static / "index.html").exists():
        raise SystemExit(f"{static}/index.html is missing: build the portal (npm run build)")

    common = {"OMNIFILE_DATABASE_URL": db_url, "OMNIFILE_DEV_MODE": "1"}
    clocked = {**common, "OMNIFILE_DEV_CLOCK_FILE": str(clock_file)}

    # 1. sim, migrations, the backend row.
    sim = _start(
        work,
        "sim",
        [
            py,
            "-m",
            "omnifile_service.backends.sim_http",
            "--host",
            "127.0.0.1",
            "--port",
            str(p["sim"]),
            "--key-file",
            str(sim_key),
            "--clock",
            "offset",
            "--clock-file",
            str(clock_file),
            "--admin",
        ],
        common,
    )
    worm = _start(
        work,
        "sim-worm",
        [
            py,
            "-m",
            "omnifile_service.backends.sim_http",
            "--host",
            "127.0.0.1",
            "--port",
            str(p["sim-worm"]),
            "--layout",
            "worm",
            "--key-file",
            str(worm_key),
            "--clock",
            "offset",
            "--clock-file",
            str(clock_file),
            "--admin",
        ],
        common,
    )
    _wait_port(p["sim"], "sim", sim, work / "logs" / "sim.log")
    _wait_port(p["sim-worm"], "sim-worm", worm, work / "logs" / "sim-worm.log")
    subprocess.run(
        [py, "-m", "omnifile_service.catalog.migrate"],
        check=True,
        env={**os.environ, **common},
        stdout=subprocess.DEVNULL,
    )
    backend_id = _seed_backend(db_url, url["sim"], str(sim_key))
    worm_backend_id = _seed_backend(
        db_url, url["sim-worm"], str(worm_key), "m4-sim-worm", "worm/data", "azure_worm"
    )

    # 2. The fakes (their seed: the people of world.py).
    fakes_seed = work / "fakes-seed.json"
    fakes_seed.write_text(
        json.dumps(
            {
                "tenant": world.TENANT,
                "bot_app_id": world.BOT_APP_ID,
                "bot_secret": bot_secret,
                "graph_client_id": world.GRAPH_CLIENT_ID,
                "graph_secret": graph_secret,
                "graph": world.graph_seed(),
            },
            indent=1,
        )
    )
    fakes = _start(
        work,
        "fakes",
        [
            py,
            str(HERE / "fakes.py"),
            "--seed",
            str(fakes_seed),
            "--graph-port",
            str(p["graph"]),
            "--bot-port",
            str(p["bot"]),
            "--relay-url",
            url["relay"],
        ],
        common,
    )

    # 3. api (direct mutual TLS), with the Teams intents endpoint; the relay pinned.
    api_env = {
        **clocked,
        "OMNIFILE_CA_CERT": pki["ca_crt"],
        "OMNIFILE_API_CLIENT_AUTH": "direct_tls",
        "OMNIFILE_API_TLS_CERT": pki["server_crt"],
        "OMNIFILE_API_TLS_KEY": pki["server_key"],
        "OMNIFILE_API_HOST": "127.0.0.1",
        "PORT": str(p["api"]),
        # The plain-HTTP /healthz defaults to 8080; keep it off a port another stack may hold.
        "OMNIFILE_API_HEALTH_PORT": str(p["api"] + 1),
        "OMNIFILE_API_URL": url["api"],
        "OMNIFILE_API_LEASE_KEY": base64.b64encode(hashlib.sha256(b"m4-dev").digest()).decode(),
        "OMNIFILE_TENANT_ID": world.TENANT,
        "OMNIFILE_TEAMS_INTENT_PUBLIC_KEYS": keys["intent_public_keys"],
        "OMNIFILE_TEAMS_RELAY_CERT_SHA256": pki["relay_sha256"],
        "OMNIFILE_PORTAL_URL": url["web"],
    }
    api = _start(work, "api", [py, "-m", "omnifile_service.api"], api_env)

    # 4. worker: Teams through the fakes, on the virtual clock, short dev polls.
    worker_env = {
        **clocked,
        "OMNIFILE_STUB_AFTER_MINUTES": "1000000",
        "OMNIFILE_WORKER_MAX_SWEEP_S": "2",
        "OMNIFILE_RECALL_POLL_S": "2",
        "OMNIFILE_TENANT_ID": world.TENANT,
        "OMNIFILE_TEAMS_BOT_APP_ID": world.BOT_APP_ID,
        "OMNIFILE_TEAMS_APP_ID": world.TEAMS_APP_ID,
        "OMNIFILE_TEAMS_BOT_CREDENTIAL": bot_secret_file,
        "OMNIFILE_TEAMS_SERVICE_URL": url["bot"] + "/",
        "OMNIFILE_TEAMS_INTENT_SIGNING_KEY": keys["intent_key"],
        "OMNIFILE_TEAMS_INTENT_KID": world.INTENT_KID,
        "OMNIFILE_PORTAL_URL": url["web"],
        "OMNIFILE_NOTIFY_SENDER": world.SENDER,
        "OMNIFILE_GRAPH_CREDENTIAL": graph_secret_file,
        "OMNIFILE_GRAPH_CLIENT_ID": world.GRAPH_CLIENT_ID,
        "OMNIFILE_GRAPH_URL": url["graph"] + "/v1.0",
        "OMNIFILE_ENTRA_AUTHORITY": url["graph"],
        "OMNIFILE_BOTCONNECTOR_TOKEN_URL": url["bot"] + "/token",
    }
    worker = _start(work, "worker", [py, "-m", "omnifile_service.worker"], worker_env)

    # 5. web over TLS, dev issuer, the built SPA.
    users, apps = work / "dev-idp-users.json", work / "dev-idp-apps.json"
    users.write_text(json.dumps(world.dev_idp_users(), indent=1))
    partner_secret_file, partner_secret = _secret_file(work, "partner")
    apps.write_text(
        json.dumps(
            [
                {
                    "client_id": world.PARTNER_APP,
                    "secret": partner_secret,
                    "roles": list(world.PARTNER_ROLES),
                }
            ]
        )
    )
    web_env = {
        **clocked,
        "OMNIFILE_WEB_ORIGIN": url["web"],
        "OMNIFILE_WEB_TENANT_ID": world.TENANT,
        "OMNIFILE_WEB_CLIENT_ID": world.PORTAL_CLIENT_ID,
        "OMNIFILE_WEB_API_AUDIENCES": world.API_AUDIENCE,
        "OMNIFILE_WEB_OIDC": "dev",
        "OMNIFILE_WEB_STATIC_DIR": str(static),
        "OMNIFILE_DEV_IDP_USERS": str(users),
        "OMNIFILE_DEV_IDP_APPS": str(apps),
        "OMNIFILE_WEB_CURSOR_KEY": base64.b64encode(hashlib.sha256(b"m4-cursor").digest()).decode(),
    }
    web = _start(
        work,
        "web",
        [
            py,
            "-m",
            "uvicorn",
            "--factory",
            "omnifile_service.web.__main__:build",
            "--host",
            "127.0.0.1",
            "--port",
            str(p["web"]),
            "--ssl-certfile",
            pki["server_crt"],
            "--ssl-keyfile",
            pki["server_key"],
            "--http",
            "h11",
            "--no-proxy-headers",
            "--no-server-header",
        ],
        web_env,
    )

    # 6. The real relay, mutual TLS to api with its pinned certificate.
    relay_env = {
        "OMNIFILE_DEV_MODE": "1",
        "OMNIFILE_RELAY_BOT_APP_ID": world.BOT_APP_ID,
        "OMNIFILE_RELAY_TENANT_ID": world.TENANT,
        "OMNIFILE_RELAY_INTENT_PUBLIC_KEYS": keys["intent_public_keys"],
        "OMNIFILE_RELAY_SERVICE_BASE_URL": url["api"],
        "OMNIFILE_RELAY_SERVICE_CLIENT_CERT": pki["relay_crt"],
        "OMNIFILE_RELAY_SERVICE_CLIENT_KEY": pki["relay_key"],
        "OMNIFILE_RELAY_SERVICE_CA_BUNDLE": pki["ca_crt"],
    }
    relay = _start(
        work,
        "relay",
        [rpy, str(HERE / "relay.py"), "--port", str(p["relay"]), "--fakes-url", url["bot"]],
        relay_env,
    )

    for name, proc in (("fakes", fakes), ("api", api), ("web", web), ("relay", relay)):
        port = {"fakes": p["bot"], "api": p["api"], "web": p["web"], "relay": p["relay"]}[name]
        _wait_port(port, name, proc, work / "logs" / f"{name}.log")
    time.sleep(1.0)
    if worker.poll() is not None:
        raise SystemExit("worker exited:\n" + (work / "logs" / "worker.log").read_text()[-3000:])
    state = {
        "work": str(work),
        "db_url": db_url,
        "urls": url,
        "ports": p,
        "pki": pki,
        "intent_key": keys["intent_key"],
        "intent_kid": world.INTENT_KID,
        "clock_file": str(clock_file),
        "sim_key": str(sim_key),
        "backend_id": backend_id,
        "worm_backend_id": worm_backend_id,
        "sim_worm_key": str(worm_key),
        "partner": {"client_id": world.PARTNER_APP, "secret_file": partner_secret_file},
        "tenant": world.TENANT,
        "logs": str(work / "logs"),
    }
    (work / "stack.json").write_text(json.dumps(state, indent=1))
    print(json.dumps(state, indent=1))


def down(args: argparse.Namespace, quiet: bool = False) -> None:
    work = work_dir(args.work)
    for name in reversed(PROCS):
        _stop(work, name)
    if not quiet:
        print(f"stopped the M4 stack in {work}")


def env(args: argparse.Namespace) -> None:
    print((work_dir(args.work) / "stack.json").read_text())


def main() -> None:
    p = argparse.ArgumentParser(prog="dev/m4/stack.py")
    p.add_argument("command", choices=["up", "down", "env"])
    p.add_argument("--work")
    args = p.parse_args()
    {"up": up, "down": down, "env": env}[args.command](args)


if __name__ == "__main__":
    main()
