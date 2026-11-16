"""The relay's ASGI app: ``POST /api/messages`` and ``GET /healthz``.

Order of checks for ``POST /api/messages`` (cheapest first, nothing forwarded until all pass):

1. per-IP rate limit, body size and content type;
2. ``serviceUrl`` on the allowlist (the relay never calls it, but refuses foreign hosts);
3. Bot Framework JWT (``botauth``), including serviceUrl claim, channel endorsement and tenant;
4. the activity's tenant is ours (single-tenant bot);
5. only ``invoke`` / ``adaptiveCard/action`` with a manual trigger does anything;
6. per-user rate limit;
7. the signed intent: signature, expiry, type, audience;
8. the clicker is the intent's recipient, the tenant matches, the verb matches the action;
9. the intent id has not been used (replay);
10. forward inward within Teams' 5-second limit, then reply with an updated card.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import cards
from .botauth import AuthError, BotKeyCache, BotTokenValidator, service_url_allowed
from .config import Settings
from .intent import DATA_KEY, IntentError, IntentVerifier
from .ratelimit import RateLimiter
from .replay import MemoryReplayStore, ReplayStore, ReplayStoreFull
from .sink import Forward, IntentSink, SinkDuplicate, SinkError, SinkRejected

log = logging.getLogger("omnifile_relay.app")


class _TooLarge(Exception):
    pass


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise _TooLarge()
        except ValueError:
            raise _TooLarge() from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _TooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


def _tenant_of(activity: dict) -> set[str]:
    found: set[str] = set()
    conv = activity.get("conversation")
    if isinstance(conv, dict) and isinstance(conv.get("tenantId"), str):
        found.add(conv["tenantId"].lower())
    cd = activity.get("channelData")
    if isinstance(cd, dict) and isinstance(cd.get("tenant"), dict):
        tid = cd["tenant"].get("id")
        if isinstance(tid, str):
            found.add(tid.lower())
    return found


def _activity_time(activity: dict) -> str | None:
    """The activity's ``timestamp`` (set by the channel) as RFC 3339 UTC, or None."""
    raw = activity.get("timestamp")
    if not isinstance(raw, str) or len(raw) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def create_app(
    settings: Settings,
    *,
    sink: IntentSink,
    http_client: httpx.AsyncClient | None = None,
    replay: ReplayStore | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Starlette:
    """Build the app. ``http_client`` is used only for Microsoft's OpenID/JWKS documents."""
    owns_http = http_client is None
    http = http_client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
    keys = BotKeyCache(settings, http, clock=monotonic)
    validator = BotTokenValidator(settings, keys)
    intents = IntentVerifier(settings)
    used = replay or MemoryReplayStore()
    ip_limiter = RateLimiter(settings.per_ip_rate, settings.per_ip_burst, clock=monotonic)
    user_limiter = RateLimiter(settings.per_user_rate, settings.per_user_burst, clock=monotonic)

    def outcome(rid: str, started: float, status: int, result: str, reason: str, **kw) -> None:
        log.info(
            "request",
            extra={
                "event": "messages",
                "request_id": rid,
                "status": status,
                "outcome": result,
                "reason": reason,
                "latency_ms": round((monotonic() - started) * 1000, 1),
                **kw,
            },
        )

    def invoke_reply(body: dict) -> JSONResponse:
        # Universal Actions: HTTP 200 always; the real result is body.statusCode.
        return JSONResponse(body, status_code=200)

    async def messages(request: Request) -> Response:
        rid = uuid.uuid4().hex
        started = monotonic()
        client = request.client.host if request.client else "unknown"
        if not ip_limiter.allow(client):
            outcome(rid, started, 429, "rejected", "rate_limited_ip")
            return Response(status_code=429, headers={"Retry-After": "1"})
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            outcome(rid, started, 415, "rejected", "content_type")
            return Response(status_code=415)
        try:
            # A deadline on the whole body read, so a slow sender can't hold a worker.
            async with asyncio.timeout(settings.body_read_timeout_seconds):
                raw = await _read_body(request, settings.max_body_bytes)
        except _TooLarge:
            outcome(rid, started, 413, "rejected", "body_too_large")
            return Response(status_code=413)
        except TimeoutError:
            outcome(rid, started, 408, "rejected", "body_read_timeout")
            return Response(status_code=408)
        try:
            activity = json.loads(raw)
        except (ValueError, RecursionError):
            # RecursionError: deeply nested JSON ("[[[[..."), which fits in 64 KiB.
            activity = None
        if not isinstance(activity, dict):
            outcome(rid, started, 400, "rejected", "bad_json")
            return Response(status_code=400)

        if not service_url_allowed(activity.get("serviceUrl"), settings.service_url_hosts):
            outcome(rid, started, 403, "rejected", "service_url_not_allowed")
            return Response(status_code=403)

        try:
            await validator.validate(request.headers.get("authorization"), activity)
        except AuthError as exc:
            outcome(rid, started, exc.status, "rejected", exc.reason)
            return Response(status_code=exc.status)

        tenants = _tenant_of(activity)
        if not tenants or tenants != {settings.tenant_id}:
            outcome(rid, started, 403, "rejected", "wrong_tenant")
            return Response(status_code=403)

        # Authenticated, from our tenant: the per-address bucket guards only
        # against unauthenticated floods. Every Teams request comes from a
        # few Bot Connector addresses, so a user scripting messages at the
        # bot must not use up everyone's clicks; the per-user bucket applies
        # instead, to every kind of activity (M7 review).
        ip_limiter.refund(client)
        sender = activity.get("from")
        clicker = sender.get("aadObjectId") if isinstance(sender, dict) else None
        if isinstance(clicker, str) and clicker:
            clicker = clicker.lower()
            if not user_limiter.allow(clicker):
                outcome(rid, started, 429, "rejected", "rate_limited_user")
                return Response(status_code=429, headers={"Retry-After": "2"})

        if activity.get("type") != "invoke":
            # Messages, installs, conversation updates: acknowledged, never forwarded.
            outcome(rid, started, 200, "ignored", "not_invoke")
            return Response(status_code=200)
        if activity.get("name") != "adaptiveCard/action":
            outcome(rid, started, 501, "ignored", "unsupported_invoke")
            return Response(status_code=501)

        if not isinstance(clicker, str) or not clicker:
            outcome(rid, started, 200, "rejected", "no_clicker")
            return invoke_reply(cards.error_response(400, "invalid"))

        value = activity.get("value")
        action = value.get("action") if isinstance(value, dict) else None
        if (
            not isinstance(action, dict)
            or action.get("type") != "Action.Execute"
            or value.get("trigger", "manual") != "manual"
        ):
            outcome(rid, started, 200, "rejected", "not_manual_execute")
            return invoke_reply(cards.error_response(400, "invalid"))
        data = action.get("data")
        token = data.get(DATA_KEY) if isinstance(data, dict) else None

        try:
            intent = intents.verify(token)
        except IntentError as exc:
            code = "intent_expired" if exc.reason == "intent_expired" else "invalid"
            outcome(rid, started, 200, "rejected", exc.reason)
            return invoke_reply(cards.error_response(400, code))

        log_kw = {"intent_id": intent.intent_id, "action": intent.action}
        if intent.recipient != clicker:
            outcome(rid, started, 200, "rejected", "wrong_recipient", **log_kw)
            return invoke_reply(cards.error_response(403, "wrong_recipient"))
        if intent.tenant != settings.tenant_id:
            outcome(rid, started, 200, "rejected", "intent_wrong_tenant", **log_kw)
            return invoke_reply(cards.error_response(400, "invalid"))
        if action.get("verb") != intent.action:
            outcome(rid, started, 200, "rejected", "verb_mismatch", **log_kw)
            return invoke_reply(cards.error_response(400, "invalid"))

        activity_time = _activity_time(activity)
        if activity_time is None:
            outcome(rid, started, 200, "rejected", "bad_activity_timestamp", **log_kw)
            return invoke_reply(cards.error_response(400, "invalid"))

        try:
            fresh = used.reserve(intent.intent_id, intent.expires_at)
        except ReplayStoreFull:
            # Not attacker-reachable (it takes that many valid clicks by their
            # own recipients), but never silent: the click isn't forwarded.
            outcome(rid, started, 200, "error", "replay_store_full", **log_kw)
            return invoke_reply(cards.error_response(500, "unavailable"))
        if not fresh:
            # Teams retries a slow invoke twice; a double click lands here too. Not forwarded.
            outcome(rid, started, 200, "duplicate", "replay", **log_kw)
            return invoke_reply(cards.message_response(cards.ERROR_TEXT["already_received"]))

        forward = Forward(
            intent_token=intent.token,
            intent_id=intent.intent_id,
            action=intent.action,
            clicker_object_id=clicker,
            tenant_id=settings.tenant_id,
            activity_time=activity_time,
            received_at=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )
        try:
            reply = await asyncio.wait_for(
                sink.submit(forward), timeout=settings.forward_timeout_seconds
            )
        except SinkDuplicate:
            outcome(rid, started, 200, "duplicate", "service_duplicate", **log_kw)
            return invoke_reply(cards.message_response(cards.ERROR_TEXT["already_received"]))
        except SinkRejected:
            outcome(rid, started, 200, "rejected", "service_rejected", **log_kw)
            return invoke_reply(cards.error_response(403, "rejected"))
        except (SinkError, TimeoutError):
            used.release(intent.intent_id)
            outcome(rid, started, 200, "error", "service_unavailable", **log_kw)
            return invoke_reply(cards.error_response(500, "unavailable"))

        outcome(rid, started, 200, "accepted", "forwarded", **log_kw)
        if reply.card is not None:
            return invoke_reply(cards.card_response(reply.card))
        if reply.message is not None:
            return invoke_reply(cards.message_response(reply.message))
        return invoke_reply(cards.card_response(cards.done_card(intent.action)))

    async def healthz(_: Request) -> Response:
        # Liveness only: never depends on Microsoft or the service being reachable.
        return JSONResponse({"status": "ok", "signing_keys_loaded": keys.loaded})

    @asynccontextmanager
    async def lifespan(_: Starlette):
        try:
            yield
        finally:
            await sink.aclose()
            if owns_http:
                await http.aclose()

    return Starlette(
        routes=[
            Route("/api/messages", messages, methods=["POST"]),
            Route("/healthz", healthz, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
