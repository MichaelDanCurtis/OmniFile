"""Process entry point: ``omnifile-relay`` (used by the Containerfile).

Before the deployment's second phase the relay has no client certificate (Key Vault secrets,
deploy/azure/README.md step 8) and perhaps no intent keys. Then it starts **not ready**
instead of exiting and being restarted in a loop: it logs one line saying what is missing,
`/healthz` answers 503 with it (the readiness probe; liveness is the port), and
`/api/messages` answers 503, which Teams retries.
"""

from __future__ import annotations

import json
import logging
import os

import uvicorn

from . import jsonlog
from .app import create_app
from .config import ConfigError, from_env
from .sink import HttpsMtlsSink


def build_app():
    settings = from_env()
    missing = [
        name
        for name, value in (
            ("OMNIFILE_RELAY_SERVICE_BASE_URL", settings.service_base_url),
            ("OMNIFILE_RELAY_SERVICE_CLIENT_CERT", settings.service_client_cert),
            ("OMNIFILE_RELAY_SERVICE_CLIENT_KEY", settings.service_client_key),
            ("OMNIFILE_RELAY_SERVICE_CA_BUNDLE", settings.service_ca_bundle),
        )
        if not value
    ]
    if missing:
        raise ConfigError("missing: " + ", ".join(missing))
    sink = HttpsMtlsSink(
        settings.service_base_url,  # type: ignore[arg-type]
        client_cert=settings.service_client_cert,  # type: ignore[arg-type]
        client_key=settings.service_client_key,  # type: ignore[arg-type]
        ca_bundle=settings.service_ca_bundle,  # type: ignore[arg-type]
        timeout=settings.forward_timeout_seconds,
    )
    return create_app(settings, sink=sink)


log = logging.getLogger("omnifile_relay")


def waiting_app(reason: str):
    """An ASGI app for a relay that isn't configured yet: 503 everywhere, with `reason`."""
    health = json.dumps({"status": "waiting", "reason": reason}).encode()

    async def app(scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(health)).encode()),
                    (b"retry-after", b"60"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": health})

    return app


def build_or_wait():
    """The relay, or, when its settings are incomplete, the waiting app (one log line)."""
    try:
        return build_app()
    except ConfigError as exc:
        reason = f"{exc} (deploy/azure/README.md, steps 7 and 8)"
        log.warning("relay is not ready and is waiting: %s", reason)
        return waiting_app(reason)


def main() -> None:
    jsonlog.configure(os.environ.get("OMNIFILE_RELAY_LOG_LEVEL", "INFO"))
    uvicorn.run(
        build_or_wait(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        # Trust X-Forwarded-For only from the ingress subnet (CIDR), never "*".
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("OMNIFILE_RELAY_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        server_header=False,
        date_header=False,
        access_log=False,
        limit_concurrency=int(os.environ.get("OMNIFILE_RELAY_MAX_CONCURRENCY", "200")),
        timeout_keep_alive=5,
        log_config=None,
    )


if __name__ == "__main__":
    main()
