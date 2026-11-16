"""uvicorn HTTP/1.1 protocol that exposes the TLS client certificate to the app.

uvicorn verifies the client certificate (`ssl_cert_reqs=CERT_REQUIRED`,
`ssl_ca_certs=<OmniFile CA>`), but does not pass it to the ASGI app. This
subclass adds the ASGI TLS extension to every request scope on the connection:
`scope["extensions"]["tls"] = {"client_cert_chain": [<leaf PEM>], ...}`
(https://asgi.readthedocs.io/en/latest/specs/tls.html). Python's `ssl` gives
only the leaf, which is all `clientauth.DirectTls` reads.

This is how `api` runs in Azure by default (direct TLS behind a TCP ingress,
threat model T5), and in dev and tests. Only with forwarded mode, which the
template no longer offers, would an ingress terminate TLS and the app read
`X-Forwarded-Client-Cert` instead.
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

from uvicorn.protocols.http.h11_impl import H11Protocol


class ClientCertH11Protocol(H11Protocol):
    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        ssl_object = transport.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
        if not der:
            return
        tls = {
            "client_cert_chain": [ssl.DER_cert_to_PEM_cert(der)],
            "client_cert_name": None,
            "client_cert_error": None,
            "tls_version": None,
            "cipher_suite": None,
        }
        inner = self.app

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope.get("type") in ("http", "websocket"):
                scope.setdefault("extensions", {})["tls"] = tls
            await inner(scope, receive, send)

        self.app = app  # type: ignore[assignment]
