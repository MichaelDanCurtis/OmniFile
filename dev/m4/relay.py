"""The real Teams relay (relay/src, unchanged) for the M4 dev stack.

    uv run --project relay python dev/m4/relay.py --port P --fakes-url http://127.0.0.1:<bot port>

Exactly what `omnifile_relay.main` builds (settings from OMNIFILE_RELAY_*, the mutual-TLS
sink to `api`), with one difference: the relay's `http_client`, which it uses only for
Microsoft's Bot Framework OpenID metadata and JWKS, sends requests for
https://login.botframework.com/... to the fake Teams in dev/m4/fakes.py instead
(<fakes-url>/_teams/...). The relay's code and default settings are unchanged, so it
validates the fake's JWTs exactly as it would Microsoft's. Dev and tests only.
"""

from __future__ import annotations

import argparse
import os

import httpx
import uvicorn
from omnifile_relay import jsonlog
from omnifile_relay.app import create_app
from omnifile_relay.config import ConfigError, from_env
from omnifile_relay.sink import HttpsMtlsSink

LOGIN_HOST = "login.botframework.com"


class FakeLogin(httpx.AsyncBaseTransport):
    """https://login.botframework.com/<path> -> <fakes>/_teams/<path>; nothing else is sent."""

    def __init__(self, fakes_url: str) -> None:
        self.base = httpx.URL(fakes_url.rstrip("/") + "/_teams/")
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https" or request.url.host != LOGIN_HOST:
            return httpx.Response(403, text="dev relay: only login.botframework.com is faked")
        target = self.base.join(request.url.raw_path.decode().lstrip("/"))
        forwarded = httpx.Request(request.method, target, headers={"accept": "application/json"})
        resp = await self.inner.handle_async_request(forwarded)
        await resp.aread()
        return httpx.Response(
            resp.status_code, content=resp.content, headers={"content-type": "application/json"}
        )

    async def aclose(self) -> None:
        await self.inner.aclose()


def build(fakes_url: str):
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
    http = httpx.AsyncClient(
        transport=FakeLogin(fakes_url), trust_env=False, follow_redirects=False
    )
    return create_app(settings, sink=sink, http_client=http)


def main() -> None:
    p = argparse.ArgumentParser(prog="dev/m4/relay.py")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--fakes-url", required=True, help="dev/m4/fakes.py's bot port")
    args = p.parse_args()
    if os.environ.get("OMNIFILE_DEV_MODE") != "1":
        raise SystemExit("dev/m4/relay.py fakes Microsoft's JWKS: it needs OMNIFILE_DEV_MODE=1")
    jsonlog.configure(os.environ.get("OMNIFILE_RELAY_LOG_LEVEL", "INFO"))
    # The same server options as omnifile_relay.main.
    uvicorn.run(
        build(args.fakes_url),
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        server_header=False,
        date_header=False,
        access_log=False,
        limit_concurrency=200,
        timeout_keep_alive=5,
        log_config=None,
    )


if __name__ == "__main__":
    main()
