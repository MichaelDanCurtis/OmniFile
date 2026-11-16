"""What an app serves while its deployment isn't configured yet (two-phase deployment).

- `api` and `enrol` before the edge CA exists (deploy/azure/README.md, step 7). An operator
  makes the CA certificate inside enrol's container (`init-kv-ca`), which needs the container
  running.
- `web` before its Entra app registration exists (step 6, `deploy/entra/register.sh`).

So instead of exiting, and being restarted in a loop, the app starts this. It logs one line
saying what is missing. `/healthz` answers `503` with the reason, so the replica is **not
ready**; the platform's liveness probe is a TCP one (`deploy/azure/modules/app.bicep`), so it
is not restarted either. Every other path answers `503 unavailable`. Nothing is
authenticated or signed.
"""

from __future__ import annotations

import json
import logging

from ..api.problems import ApiError

log = logging.getLogger(__name__)


def create_waiting_app(app_name: str, reason: str):
    """An ASGI app that waits for configuration (`reason` is logged and shown on /healthz)."""
    log.warning("%s is not ready and is waiting: %s", app_name, reason)
    health = json.dumps({"status": "waiting", "app": app_name, "reason": reason}).encode()
    waiting = ApiError(
        "unavailable", f"{app_name} is not configured yet: {reason}", retry_after_s=60
    )

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
        if scope["path"] == "/healthz" and scope["method"] == "GET":
            body, ctype = health, b"application/json"
        else:
            body, ctype = json.dumps(waiting.body()).encode(), b"application/problem+json"
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", ctype),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", b"60"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app
