"""Request plumbing for `web`: who is calling, CSRF, rate limits, idempotency,
cursors and error handling.

`endpoint(handler, ...)` wraps a handler with, in order:
1. authentication: a bearer app token (where the route allows one), else the
   session cookie (`401 unauthenticated` without either);
2. for a session's unsafe method: `X-CSRF-Token` and `Origin` (`403 csrf_failed`);
3. the app-role gate: a signed-in user with no app role gets `403 role_required`
   on every `/v1` route except `/v1/me`;
4. the per-principal rate limit (`429 throttled`);
5. for POST, PUT and PATCH: the JSON body (1 MiB); for POST under /v1 except
   the previews, `Idempotency-Key` and the idempotency store;
6. errors as problem details; anything unexpected is a logged `500 internal`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import tuple_row
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..api.clientauth import forwarded_client_ip
from ..api.db import Catalog
from ..api.idempotency import request_key, run_idempotent
from ..api.problems import PROBLEM_JSON, ApiError
from ..api.validate import json_body, parse_id
from ..apiclients.keys import in_query
from .config import WebSettings
from .prices import PriceDisplay, has_money, scrub
from .problems import WebError, invalid

log = logging.getLogger("omnifile_service.web")

MAX_BODY = 1024 * 1024  # portal-api.md section 1
SESSION_COOKIE = "__Host-omnifile_session"
LOGIN_COOKIE = "__Host-omnifile_login"
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: The caller's address, for audit rows written during this request.
CLIENT_ADDR: ContextVar[str | None] = ContextVar("omnifile_web_client_addr", default=None)


@dataclass
class Principal:
    """The caller: a signed-in user (session) or a partner application (bearer)."""

    kind: str  # "user" | "app"
    roles: frozenset[str]
    object_id: str | None = None  # Entra oid (users)
    identity_id: int | None = None
    sid: str | None = None
    display_name: str | None = None
    upn: str | None = None
    groups: tuple[str, ...] = ()
    groups_source: str = "none"
    groups_resolved_at: datetime | None = None
    app_id: str | None = None  # azp (apps)
    session: dict[str, Any] | None = None
    #: Apps: the registered API client (apiclients/registry.py) and its owner.
    api_client: dict[str, Any] | None = None
    #: Apps signed in with an OmniFile API key: its public id.
    key_id: str | None = None

    @property
    def is_app(self) -> bool:
        return self.kind == "app"

    @property
    def idem_principal(self) -> str:
        """The idempotency and rate-limit scope (section 4.2): user:<oid>,
        app:<client id>, or key:<key id> for an API key (limits are per key)."""
        if self.key_id is not None:
            return f"key:{self.key_id}"
        return f"app:{self.app_id}" if self.is_app else f"user:{self.object_id}"

    @property
    def actor(self) -> str:
        """The audit actor (section 3.4): user:<identity_id> or app:<client id>."""
        if self.is_app:
            return f"app:{self.app_id}"
        if self.identity_id is not None:
            return f"user:{self.identity_id}"
        return f"user:entra:{self.object_id}"

    def has(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)

    @property
    def all_sids(self) -> list[str]:
        """The user's SID and group SIDs (share-role checks)."""
        return ([self.sid] if self.sid else []) + list(self.groups)


@dataclass
class WebResult:
    """A handler's answer. Duck-types `api.idempotency.Result` (status, body,
    response), so idempotent routes store and replay it."""

    status: int
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def response(self, replayed: bool = False) -> Response:
        headers = dict(self.headers)
        if replayed:
            headers["Idempotent-Replayed"] = "true"
        if self.body is None:
            return Response(status_code=self.status, headers=headers)
        media = PROBLEM_JSON if self.status >= 400 else "application/json"
        return JSONResponse(self.body, status_code=self.status, headers=headers, media_type=media)


class TokenBuckets:
    """Per-principal token buckets (section 4.4), per replica."""

    def __init__(self, rate: float, burst: float, clock: Callable[[], float] = time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self._b: dict[str, tuple[float, float]] = {}

    def take(self, key: str) -> float | None:
        """None if allowed, else the seconds until a token is free."""
        now = self.clock()
        tokens, at = self._b.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - at) * self.rate)
        if tokens < 1:
            self._b[key] = (tokens, now)
            return (1 - tokens) / self.rate
        self._b[key] = (tokens - 1, now)
        if len(self._b) > 100_000:
            self._b = {key: self._b[key]}
        return None


@dataclass
class WebServices:
    catalog: Catalog
    settings: WebSettings
    auth: Any  # auth.Authenticator
    preview: Any  # preview.PreviewPort
    limiter: TokenBuckets | None = None
    preview_limiter: TokenBuckets | None = None
    login_limiter: TokenBuckets | None = None
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    #: Settings' live checks (settings/ports.py); None: each says it isn't configured.
    probes: Any = None
    #: The environment Settings reads the deployment's names from (never secrets).
    settings_env: dict[str, str] = field(default_factory=dict)
    #: Connect Azure: an access token -> an `settings.arm.Arm` that uses it for
    #: one request. None: `HttpArm` over the authenticator's HTTP client.
    arm_for: Callable[[str], Any] | None = None
    #: Who sees prices (prices.py), cached per replica.
    prices: Any = None

    def __post_init__(self) -> None:
        s = self.settings
        if self.prices is None:
            self.prices = PriceDisplay()
        if self.limiter is None:
            self.limiter = TokenBuckets(s.rate_per_s, s.rate_burst)
        if self.preview_limiter is None:
            self.preview_limiter = TokenBuckets(s.preview_rate_per_s, s.preview_rate_per_s * 2)
        if self.login_limiter is None:
            self.login_limiter = TokenBuckets(s.login_rate_per_s, s.login_burst)

    def now(self) -> datetime:
        """The service clock for catalog times (`WebSettings.service_clock`)."""
        s = self.settings
        return (s.service_clock or s.clock)()


@dataclass
class Ctx:
    request: Request
    svc: WebServices
    who: Principal
    body: dict[str, Any]
    request_id: str

    @property
    def now(self) -> datetime:
        return self.svc.now()

    def path_id(self, name: str) -> int:
        return parse_id(self.request.path_params[name], name)

    def query(self, name: str) -> str | None:
        return self.request.query_params.get(name)

    def query_id(self, name: str) -> int | None:
        v = self.request.query_params.get(name)
        return None if v is None else parse_id(v, name)

    def limit(self, default: int = 100, maximum: int = 500) -> int:
        raw = self.request.query_params.get("limit")
        if raw is None:
            return default
        if not raw.isdigit() or not 1 <= int(raw) <= maximum:
            raise invalid("limit", f"must be an integer from 1 to {maximum}")
        return int(raw)

    def cursor(self, kind: str) -> list[Any] | None:
        raw = self.request.query_params.get("cursor")
        s = self.svc.settings
        return None if raw is None else read_cursor(s.cursor_key, raw, kind, s.clock())

    def next_cursor(self, kind: str, values: list[Any]) -> str:
        # Cursor lifetimes are wall-clock, like sessions, never the service
        # clock: in dev that one can be days ahead (WebSettings.service_clock).
        s = self.svc.settings
        return make_cursor(s.cursor_key, kind, values, s.clock())


Handler = Callable[[Ctx], Awaitable[WebResult | Response]]


# --- cursors (section 4.1) ----------------------------------------------------

CURSOR_TTL_S = 3600


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_cursor(key: bytes, kind: str, values: list[Any], now: datetime) -> str:
    payload = _b64(json.dumps({"k": kind, "v": values, "t": int(now.timestamp())}).encode())
    mac = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest()[:16])
    return f"{payload}.{mac}"


def read_cursor(key: bytes, raw: str, kind: str, now: datetime | None = None) -> list[Any]:
    try:
        payload, mac = raw.split(".", 1)
        want = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest()[:16])
        if not hmac.compare_digest(mac, want):
            raise ValueError("mac")
        data = json.loads(_unb64(payload))
        if data["k"] != kind or not isinstance(data["v"], list):
            raise ValueError("kind")
        issued = int(data["t"])
    except (ValueError, KeyError, TypeError):
        raise invalid("cursor", "not a cursor from this listing") from None
    now = now or datetime.now(UTC)
    if now.timestamp() - issued > CURSOR_TTL_S:
        raise WebError("cursor_expired", "cursors are valid for one hour")
    return data["v"]


def page(items: list[Any], next_cursor: str | None) -> dict[str, Any]:
    return {"items": items, "next_cursor": next_cursor}


# --- catalog helpers -----------------------------------------------------------


@contextmanager
def tuple_rows(conn: psycopg.AsyncConnection) -> Iterator[psycopg.AsyncConnection]:
    """The retention and policy modules read rows as tuples; the pool's
    connections return dicts. Swap for the duration of the call."""
    saved = conn.row_factory
    conn.row_factory = tuple_row  # type: ignore[assignment]
    try:
        yield conn
    finally:
        conn.row_factory = saved  # type: ignore[assignment]


async def audit(
    db,
    who: Principal,
    action: str,
    *,
    dataset_id: int | None = None,
    share_id: int | None = None,
    file_id: int | None = None,
    version_id: int | None = None,
    decision: str = "allow",
    details: dict[str, Any] | None = None,
    client_addr: str | None = None,
) -> None:
    """One audit row with the caller's SID and proof (section 3.4). An API
    client's call also records the client and its owner (section 5.21), and
    the public id of the key it used, never the key."""
    if who.is_app and who.api_client is not None:
        details = {**(details or {}), "api_client": {
            "api_client_id": str(who.api_client["api_client_id"]),
            "owner_identity_id": str(who.api_client["owner_identity_id"]),
            **({"key_id": who.key_id} if who.key_id else {}),
        }}  # fmt: skip
    await db.execute(
        "INSERT INTO catalog.audit_log (actor, actor_sid, proof, action, decision, dataset_id,"
        " share_id, file_id, version_id, client_addr, details)"
        " VALUES (%s, %s, 'proven', %s, %s, %s, %s, %s, %s, %s::inet, %s::jsonb)",
        (
            who.actor,
            None if who.is_app else who.sid,
            action,
            decision,
            dataset_id,
            share_id,
            file_id,
            version_id,
            client_addr or CLIENT_ADDR.get(),
            json.dumps(details or {}, default=str),
        ),
    )


def client_addr(request: Request, trusted_ingress: tuple[Any, ...] = ()) -> str | None:
    """The caller's address: the peer, or behind the trusted ingress the
    address it appended to X-Forwarded-For (M7 review)."""
    ip = forwarded_client_ip(
        request.client.host if request.client else None,
        request.headers.getlist("x-forwarded-for"),
        trusted_ingress,
    )
    return str(ip) if ip is not None else None


# --- the wrapper ----------------------------------------------------------------


def check_csrf(request: Request, who: Principal, settings: WebSettings) -> None:
    session = who.session or {}
    token = request.headers.get("x-csrf-token")
    want = session.get("csrf_token")
    if not token or not want or not hmac.compare_digest(token.encode(), want.encode()):
        raise WebError("csrf_failed", "X-CSRF-Token is missing or wrong")
    if request.headers.get("origin") != settings.origin:
        raise WebError("csrf_failed", "Origin is missing or not the portal's")


def endpoint(
    handler: Handler,
    *,
    bearer: bool = False,
    session: bool = True,
    needs_role: bool = True,
    idempotent: bool | None = None,
    preview: bool = False,
) -> Callable[[Request], Awaitable[Response]]:
    """`idempotent=None`: POST routes are idempotent unless `preview`."""

    async def run(request: Request) -> Response:
        rid = uuid.uuid4().hex
        svc: WebServices = request.app.state.svc
        CLIENT_ADDR.set(client_addr(request, svc.settings.trusted_ingress))
        try:
            if in_query(request.url.query):
                # Never read from there (section 2.2); the access log redacts it.
                raise WebError(
                    "invalid_request",
                    "an API key goes only in the Authorization header, never in the URL;"
                    " revoke this one, since the URL may have been recorded",
                )
            who = await svc.auth.principal(request, bearer=bearer, session=session)
            if not who.is_app and request.method in UNSAFE:
                check_csrf(request, who, svc.settings)
            if needs_role and not who.is_app and not who.roles:
                raise WebError("role_required", "no OmniFile app role is assigned to you")
            limiter = svc.preview_limiter if preview else svc.limiter
            assert limiter is not None
            wait = limiter.take(who.idem_principal)
            if wait is not None:
                raise WebError(
                    "throttled", "too many requests", retry_after_s=max(1, int(wait + 0.999))
                )
            body: dict[str, Any] = {}
            raw = b""
            if request.method in ("POST", "PUT", "PATCH"):
                raw, body = await json_body(request, MAX_BODY)
            ctx = Ctx(request, svc, who, body, rid)
            is_idem = (
                (request.method == "POST" and not preview) if idempotent is None else idempotent
            )

            async def priced() -> WebResult | Response:
                try:
                    result = await handler(ctx)
                except WebError as exc:
                    # A refusal can carry amounts too: policy_invalid's checks[].fee.
                    if await _prices_hidden(ctx, exc.extensions):
                        exc.extensions = scrub(exc.extensions)
                    raise
                return await without_hidden_prices(ctx, result)

            if is_idem:
                key = request_key(request.headers)
                return await run_idempotent(
                    svc.catalog,
                    who.idem_principal,
                    key,
                    request.method,
                    request.url.path,
                    raw,
                    priced,  # type: ignore[arg-type]
                    request_id=rid,
                )
            result = await priced()
            return result if isinstance(result, Response) else result.response()
        except ApiError as exc:
            return exc.response(rid)
        except Exception:
            log.exception("unhandled error", extra={"request_id": rid})
            return ApiError("internal", "unexpected error").response(rid)

    return run


async def without_hidden_prices(ctx: Ctx, result: WebResult | Response) -> WebResult | Response:
    """Every amount removed from an answer to a caller who may not see prices
    (prices.py; Settings, Prices and alerts)."""
    if (
        isinstance(result, WebResult)
        and result.body is not None
        and result.status < 400
        and await _prices_hidden(ctx, result.body)
    ):
        result.body = scrub(result.body)
    return result


async def _prices_hidden(ctx: Ctx, body: Any) -> bool:
    """`body` holds an amount the caller may not see."""
    return (
        not ctx.who.is_app
        and has_money(body)
        and not await ctx.svc.prices.visible(ctx.svc.catalog, ctx.who)
    )


def etag(version: int) -> str:
    return f'"{version}"'


def if_match(request: Request, current: int) -> None:
    """Section 4.3: `428 precondition_required` without If-Match, `412
    precondition_failed` when stale."""
    sent = request.headers.get("if-match")
    if sent is None:
        raise WebError("precondition_required", "If-Match is required")
    if sent.strip() != etag(current):
        raise WebError("precondition_failed", "the resource changed; read it again")
