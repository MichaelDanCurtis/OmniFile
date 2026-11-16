"""The built SPA (portal/dist) and the security headers on every response
(portal-api.md 2.1).

- A path with a file extension is served from the build directory if it is
  there (never outside it), else `404`.
- Any other `GET` returns `index.html`, so client-side routes work.
- Hashed assets (`/assets/...`) are cached for a year; `/v1` and `/auth` are
  `no-store`.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .problems import not_found

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;"
    " connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"strict-transport-security", b"max-age=31536000"),
    (b"x-frame-options", b"DENY"),
]
API_PREFIXES = ("/v1", "/auth", "/dev-idp", "/healthz")


class SecurityHeaders:
    """Pure ASGI middleware: the headers above, and Cache-Control by path."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if path.startswith(("/v1", "/auth", "/dev-idp")):
            cache = b"no-store"
        elif path.startswith("/assets/"):
            cache = b"public, max-age=31536000, immutable"
        else:
            cache = b"no-cache"

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in {h for h, _ in SECURITY_HEADERS} | {b"cache-control"}
                ]
                headers += SECURITY_HEADERS
                headers.append((b"cache-control", cache))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)


class Spa:
    def __init__(self, directory: Path | None) -> None:
        self.dir = directory.resolve() if directory else None

    async def __call__(self, request: Request) -> Response:
        path = request.url.path
        if path.startswith(API_PREFIXES) and not path.startswith("/healthz"):
            raise not_found("endpoint")
        if self.dir is None or not self.dir.is_dir():
            raise not_found("portal build")
        last = path.rsplit("/", 1)[-1]
        if "." in last:
            target = (self.dir / path.lstrip("/")).resolve()
            if not target.is_relative_to(self.dir) or not target.is_file():
                raise not_found("file")
            media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return FileResponse(target, media_type=media)
        index = self.dir / "index.html"
        if not index.is_file():
            raise not_found("portal build")
        return FileResponse(index, media_type="text/html; charset=utf-8")
