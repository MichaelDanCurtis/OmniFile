"""Request plumbing shared by every edge endpoint.

`edge_endpoint` wraps a handler with, in order:
1. the client certificate -> active edge, and `{edge_id}` must match it;
2. the `OmniFile-Edge-Version` gate (`426` below `min_edge_version`), except
   for heartbeat, certificate renewal and the feed (section 7);
3. for POST: `Idempotency-Key`, the JSON body, and the idempotency store;
4. errors as problem details; anything unexpected is a logged `500 internal`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import re
import secrets
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from .clientauth import EdgeAuthenticator, EdgeIdentity
from .dataplane import DataPlanes
from .db import Catalog
from .idempotency import Result, request_key, run_idempotent
from .problems import ApiError
from .signclient import SignClient
from .validate import json_body, parse_id

log = logging.getLogger("omnifile_service.api")


@dataclass
class ApiSettings:
    min_edge_version: str = "0.0.0"
    api_url: str | None = None
    #: Key for stub leases (HMAC). Every api replica must share it.
    lease_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    #: Long-poll granularity for the feed.
    feed_poll_seconds: float = 0.25
    #: Bytes kept resident when a stub has no explicit resident size.
    default_resident_bytes: int = 4 * 1024 * 1024


class WindowLimiter:
    """At most `limit` events per key in any `window` seconds (per replica)."""

    def __init__(
        self, limit: int, window: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit, self.window, self.clock = limit, window, clock
        self._events: dict[Any, deque[float]] = {}

    def allow(self, key: Any) -> bool:
        """Record an event for `key` if under the limit; False if over it."""
        q = self._events.setdefault(key, deque())
        now = self.clock()
        while q and q[0] <= now - self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self._events) > 100_000:  # bound memory
            self._events = {key: q}
        return True


#: New (never-seen) SIDs an edge may introduce through recall requesters:
#: each one becomes a catalog identity and, later, directory and Graph lookups.
NEW_SIDS_PER_EDGE = 30
NEW_SIDS_WINDOW_S = 600.0


@dataclass
class Services:
    catalog: Catalog
    auth: EdgeAuthenticator
    planes: DataPlanes
    signer: SignClient | None
    settings: ApiSettings
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    #: Wall-clock time, for stamps made outside the service: Teams' and the relay's click
    #: times and the intents' iat/exp (teams.md section 2). Never the dev virtual clock,
    #: which moves `clock` only.
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    #: Extra async closers run when the app shuts down.
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    #: Rate of new requester SIDs per edge (section 4.4).
    new_sids: WindowLimiter = field(
        default_factory=lambda: WindowLimiter(NEW_SIDS_PER_EDGE, NEW_SIDS_WINDOW_S)
    )


@dataclass
class Ctx:
    request: Request
    svc: Services
    edge: EdgeIdentity
    body: dict[str, Any]
    idempotency_key: str | None
    request_id: str

    @property
    def edge_id(self) -> int:
        return self.edge.edge_id

    @property
    def now(self) -> datetime:
        """The service's clock (the dev virtual clock in a simulation)."""
        return self.svc.clock()

    def path_id(self, name: str) -> int:
        return parse_id(self.request.path_params[name], name)


Handler = Callable[[Ctx], Awaitable[Result]]

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def version_tuple(v: str) -> tuple[int, int, int] | None:
    m = _SEMVER.match(v.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def check_edge_version(request: Request, minimum: str) -> None:
    sent = request.headers.get("omnifile-edge-version")
    if sent is None:
        return
    have, need = version_tuple(sent), version_tuple(minimum)
    if have is None:
        raise ApiError("invalid_request", "OmniFile-Edge-Version must be a semantic version")
    if need is not None and have < need:
        raise ApiError("edge_upgrade_required", f"this edge is {sent}; the minimum is {minimum}")


def edge_endpoint(handler: Handler, *, gate_version: bool = True) -> Callable[[Request], Any]:
    async def endpoint(request: Request) -> Response:
        rid = uuid.uuid4().hex
        svc: Services = request.app.state.svc
        try:
            edge_id = parse_id(request.path_params["edge_id"], "edge_id")
            edge = await svc.auth.authenticate(request, edge_id)
            if gate_version:
                check_edge_version(request, svc.settings.min_edge_version)
            if request.method == "POST":
                key = request_key(request.headers)
                raw, body = await json_body(request)
                ctx = Ctx(request, svc, edge, body, key, rid)
                return await run_idempotent(
                    svc.catalog,
                    edge.principal,
                    key,
                    "POST",
                    request.url.path,
                    raw,
                    lambda: handler(ctx),
                    request_id=rid,
                )
            ctx = Ctx(request, svc, edge, {}, None, rid)
            return (await handler(ctx)).response()
        except ApiError as exc:
            return exc.response(rid)
        except Exception:
            log.exception("unhandled error", extra={"request_id": rid})
            return ApiError("internal", "unexpected error").response(rid)

    return endpoint


# --- stub leases -------------------------------------------------------------
#
# A lease is a string `<expiry>.<mac>`: the expiry in minutes since
# 2026-01-01 UTC (decimal) and a 128-bit HMAC-SHA256 tag (32 hex digits) over
# (edge, file, version, resident_bytes, expiry). The catalog has no lease
# table and needs none: `commit` re-runs every `prepare` check, so the lease
# only bounds the time between them and binds the resident size the service
# chose. The key (`OMNIFILE_API_LEASE_KEY`) is at least 32 bytes.

_LEASE_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_MAC_BYTES = 16  # 128-bit tag
MIN_LEASE_KEY_BYTES = 32
_LEASE = re.compile(r"^([0-9]{1,9})\.([0-9a-f]{32})$")


def _lease_mac(key: bytes, edge_id: int, file_id: int, version_id: int, resident: int, exp: int):
    msg = f"stub-lease:{edge_id}:{file_id}:{version_id}:{resident}:{exp}".encode()
    return hmac.new(key, msg, hashlib.sha256).digest()[:_MAC_BYTES]


def make_lease(
    key: bytes, edge_id: int, file_id: int, version_id: int, resident: int, expires: datetime
) -> tuple[str, datetime]:
    """The lease id, and its actual expiry (`expires` rounded up to a minute)."""
    exp = -int(-(expires - _LEASE_EPOCH).total_seconds() // 60)
    if not 0 < exp < 10**9:
        raise ApiError("internal", "lease expiry out of range")
    mac = _lease_mac(key, edge_id, file_id, version_id, resident, exp)
    return f"{exp}.{mac.hex()}", _LEASE_EPOCH + timedelta(minutes=exp)


def check_lease(
    key: bytes, lease_id: str, edge_id: int, file_id: int, version_id: int, resident: int, now
) -> None:
    m = _LEASE.match(lease_id) if isinstance(lease_id, str) else None
    if m is None:
        raise ApiError("stub_not_allowed", "malformed lease")
    exp = int(m[1])
    if not hmac.compare_digest(
        bytes.fromhex(m[2]), _lease_mac(key, edge_id, file_id, version_id, resident, exp)
    ):
        raise ApiError("stub_not_allowed", "lease does not match this file, version and size")
    expires = _LEASE_EPOCH.timestamp() + exp * 60
    if now.timestamp() >= expires:
        raise ApiError("stub_not_allowed", "lease expired")


def lease_key_from_env(value: str | None) -> bytes | None:
    """The shared lease key: base64 of at least 32 bytes. Raises ValueError."""
    if not value:
        return None
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("must be base64") from None
    if len(key) < MIN_LEASE_KEY_BYTES:
        raise ValueError(f"must decode to at least {MIN_LEASE_KEY_BYTES} bytes")
    return key
