"""`POST /internal/v1/teams/intents`: a Teams card button click, forwarded by the relay
(docs/contracts/teams.md section 4; control-api.md section 13).

The relay has checked the Bot Framework token, the tenant, the intent's signature and that
the clicker is the recipient. None of that is trusted here; in order:

1. body schema; `activity_time` and `received_at` within 10 minutes of our clock;
2. the intent (Ed25519, public keys only), as the relay checks it;
3. `intent_id` and `action` equal the intent's `jti` and `act`;
4. the tenant is ours;
5. the clicker is the intent's `sub`;
6. a `teams_intent` row matches (action, file, recipient), not revoked;
7. it is consumed once (`used_at`);
8. the clicker can still read the file (decision 28);
9. the action, in the transaction of step 7, with a `notify` message 6 s later so the
   worker's full card lands after this reply.

The reply carries a card without `Action.Execute` buttons (`notify.cards.render(..., None)`):
`api` holds no signing key. Every refusal is audited as `teams.intent.refused`.

Status codes are chosen for the relay (`relay/src/omnifile_relay/sink.py`): never 401 (it
would treat that as transient), 409 for a repeat, 400/403/410/422 for refusals.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from ..notify import cards, facts
from ..notify.intents import Intent, IntentRefused, IntentVerifier, load_public_keys
from .clientauth import RelayAuthenticator
from .context import Services, WindowLimiter
from .data import readable_location
from .db import id_str, set_origin
from .idempotency import Result, request_key, run_idempotent
from .problems import ApiError
from .recalls import ACTIVE, online_location, promise
from .validate import bad, json_body

log = logging.getLogger("omnifile_service.api.teams")

PATH = "/internal/v1/teams/intents"
PRINCIPAL = "relay"
FIELDS = (
    "intent",
    "intent_id",
    "action",
    "clicker_aad_object_id",
    "tenant_id",
    "activity_time",
    "received_at",
)
MAX_INTENT_CHARS = 4096
CLOCK_WINDOW = timedelta(minutes=10)
NOTIFY_DELAY_S = 6
#: Decision 33: a Teams "Keep online" is a 14-day hold. Fixed, not a setting.
KEEP_ONLINE_DAYS = 14
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass
class TeamsSettings:
    tenant_id: str
    verifier: IntentVerifier
    portal_url: str | None = None
    #: SHA-256 (lowercase hex) of the relay certificates allowed to call; None accepts any
    #: certificate with the relay SAN, which `from_env` permits only in dev mode.
    relay_cert_sha256: frozenset[str] | None = None
    #: More than 10 clicks per minute for one recipient is `429 throttled` (per replica).
    clicks: WindowLimiter = field(default_factory=lambda: WindowLimiter(10, 60.0))

    def __post_init__(self) -> None:
        self.tenant_id = self.tenant_id.lower()
        if not _GUID.match(self.tenant_id):
            raise ValueError("OMNIFILE_TENANT_ID must be a GUID")
        if self.relay_cert_sha256 is not None:
            pins = frozenset(h.strip().lower().replace(":", "") for h in self.relay_cert_sha256)
            if not pins or not all(_SHA256.match(h) for h in pins):
                raise ValueError("relay certificate pins must be SHA-256 hex fingerprints")
            self.relay_cert_sha256 = pins

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TeamsSettings | None:
        """None when Teams isn't configured (no public keys or tenant)."""
        env = dict(os.environ if env is None else env)
        keys = env.get("OMNIFILE_TEAMS_INTENT_PUBLIC_KEYS")
        tenant = env.get("OMNIFILE_TENANT_ID")
        if not keys or not tenant:
            return None
        raw_pins = env.get("OMNIFILE_TEAMS_RELAY_CERT_SHA256", "")
        pins = frozenset(p for p in raw_pins.split(",") if p.strip())
        if not pins and env.get("OMNIFILE_DEV_MODE") != "1":
            raise ValueError(
                "OMNIFILE_TEAMS_RELAY_CERT_SHA256 is required (any relay certificate is"
                " accepted only with OMNIFILE_DEV_MODE=1)"
            )
        return cls(
            tenant_id=tenant,
            verifier=IntentVerifier(load_public_keys(keys)),
            portal_url=env.get("OMNIFILE_PORTAL_URL") or None,
            relay_cert_sha256=pins or None,
        )


def teams_settings(app) -> TeamsSettings:
    """`app.state.teams`, read from the environment on first use."""
    settings = getattr(app.state, "teams", None)
    if settings is None:
        try:
            settings = TeamsSettings.from_env()
        except ValueError as exc:
            log.error("Teams settings are invalid: %s", exc)
            raise ApiError(
                "unavailable", "Teams intents are misconfigured", retry_after_s=60
            ) from None
        if settings is None:
            raise ApiError("unavailable", "Teams intents are not configured", retry_after_s=60)
        app.state.teams = settings
    return settings


# --- the endpoint ----------------------------------------------------------------


async def consume_intent(request: Request) -> Response:
    rid = uuid.uuid4().hex
    svc: Services = request.app.state.svc
    started = time.monotonic()
    try:
        teams = teams_settings(request.app)
        RelayAuthenticator(
            svc.auth.source, svc.auth.ca, allowed=teams.relay_cert_sha256
        ).authenticate(request)
        key = request_key(request.headers)
        raw, body = await json_body(request)
        if body.get("intent_id") != key:
            raise bad("Idempotency-Key must be the intent_id")
        # Steps 1-5 run on every request, before the idempotency store: their answers
        # depend on the clicker and the clock, not only on the intent, so they are never
        # stored, and a stored reply is only ever given to the intent's own recipient.
        intent = await _precheck(svc, teams, body)
        # teams.md 4.4: the stored reply is keyed on the intent alone, so a second relay
        # replica forwarding the same click (another received_at) gets the first reply.
        resp = await run_idempotent(
            svc.catalog,
            PRINCIPAL,
            key,
            "POST",
            PATH,
            json.dumps(body["intent"]).encode(),
            lambda: _handle(svc, teams, intent),
            request_id=rid,
        )
        _log(rid, started, resp.status_code, "accepted" if resp.status_code < 300 else "refused")
        return resp
    except ApiError as exc:
        _log(rid, started, exc.status, "refused", exc.code)
        return exc.response(rid)
    except Exception:
        log.exception("unhandled error", extra={"request_id": rid})
        return ApiError("internal", "unexpected error").response(rid)


def _log(rid: str, started: float, status: int, outcome: str, reason: str = "") -> None:
    # No tokens, objectIds, names or paths (teams.md section 9).
    log.info(
        "teams intent",
        extra={
            "event": "teams_intent",
            "request_id": rid,
            "status": status,
            "outcome": outcome,
            "reason": reason,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )


# --- checks ------------------------------------------------------------------------


def _time(body: dict[str, Any], name: str, now: datetime) -> datetime:
    value = body.get(name)
    if not isinstance(value, str) or len(value) > 64:
        raise bad(f"{name} must be an RFC 3339 time")
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise bad(f"{name} must be an RFC 3339 time") from None
    if t.tzinfo is None or "T" not in value:
        raise bad(f"{name} must be an RFC 3339 time with an offset")
    if abs(now - t) > CLOCK_WINDOW:
        raise bad(f"{name} is more than 10 minutes from the service's clock")
    return t


def check_body(body: dict[str, Any], now: datetime) -> None:
    """Step 1: schema `TeamsIntentForward`, and the two times."""
    extra = set(body) - set(FIELDS)
    if extra:
        raise bad(f"unexpected field {sorted(extra)[0]}")
    for name in FIELDS:
        if not isinstance(body.get(name), str):
            raise bad(f"{name} is required and must be a string")
    if len(body["intent"]) > MAX_INTENT_CHARS:
        raise bad("intent is too long")
    for name in ("intent_id", "clicker_aad_object_id", "tenant_id"):
        if not _GUID.match(body[name]):
            raise bad(f"{name} must be a UUID")
    if body["action"] not in ("recall", "keep_online", "track"):
        raise bad("action must be recall, keep_online or track")
    _time(body, "activity_time", now)
    _time(body, "received_at", now)


def check_intent(body: dict[str, Any], teams: TeamsSettings, now: datetime) -> Intent:
    """Steps 2-5."""
    try:
        intent = teams.verifier.verify(body["intent"], now)
    except IntentRefused as exc:
        raise ApiError(exc.code, exc.reason) from None
    if intent.intent_id != body["intent_id"].lower() or intent.action != body["action"]:
        raise ApiError("intent_mismatch", "intent_id or action differ from the intent")
    if intent.tenant != teams.tenant_id or body["tenant_id"].lower() != teams.tenant_id:
        raise ApiError("intent_invalid", "wrong tenant")
    if body["clicker_aad_object_id"].lower() != intent.recipient:
        raise ApiError("not_intended_recipient", "the clicker is not the card's recipient")
    return intent


@dataclass
class _Click:
    """What the refusal audit needs, filled in as the checks go."""

    intent_id: str | None = None
    action: str | None = None
    identity_id: int | None = None
    sid: str | None = None
    file_id: int | None = None
    dataset_id: int | None = None
    #: Step 8 found the clicker's cached groups too old to act on.
    stale_groups: bool = False


def _click_of(body: dict[str, Any]) -> _Click:
    return _Click(
        intent_id=body.get("intent_id") if isinstance(body.get("intent_id"), str) else None,
        action=body.get("action") if isinstance(body.get("action"), str) else None,
    )


async def _precheck(svc: Services, teams: TeamsSettings, body: dict[str, Any]) -> Intent:
    """Steps 1-5 and the per-recipient rate. Refusals are audited and answered, not stored."""
    click = _click_of(body)
    try:
        # The wall clock, not the service clock: the activity and receipt stamps come from
        # Teams and the relay, and the worker mints iat/exp on the wall clock (teams.md
        # section 2). They differ only on the dev virtual clock, where the service clock
        # can be days ahead and every click would otherwise look stale or expired.
        now = svc.wall_clock()
        check_body(body, now)
        intent = check_intent(body, teams, now)
        click.file_id = intent.file_id
        if not teams.clicks.allow(intent.recipient):
            raise ApiError("throttled", "too many clicks", retry_after_s=10)
        return intent
    except ApiError as exc:
        await _audit_refusal(svc, click, exc)
        raise


async def _handle(svc: Services, teams: TeamsSettings, intent: Intent) -> Result:
    """Steps 6-9, inside the idempotency store."""
    click = _Click(intent.intent_id, intent.action, file_id=intent.file_id)
    try:
        return await _act(svc, teams, intent, click, svc.clock())
    except ApiError as exc:
        if click.stale_groups:
            await _refresh_groups(svc, click.identity_id)
        if exc.code not in ("unavailable", "internal", "request_in_progress"):
            await _audit_refusal(svc, click, exc)
        raise


async def _refresh_groups(svc: Services, identity_id: int | None) -> None:
    """Ask the worker to refresh the clicker's groups from Graph (teams.md section 7)."""
    try:
        async with svc.catalog.tx(PRINCIPAL) as db:
            await db.execute(
                "SELECT pgmq.send('identity_resolve', %s::jsonb)",
                (json.dumps({"identity_id": str(identity_id)}),),
            )
    except ApiError:
        log.warning("could not queue a group refresh", extra={"event": "teams_intent"})


async def _audit_refusal(svc: Services, click: _Click, exc: ApiError) -> None:
    actor = f"user:{click.identity_id}" if click.identity_id is not None else PRINCIPAL
    details = {"intent_id": click.intent_id, "action": click.action, "reason": exc.code}
    if exc.detail:
        details["detail"] = exc.detail[:200]
    try:
        async with svc.catalog.tx(actor) as db:
            await db.execute(
                "INSERT INTO catalog.audit_log (actor, actor_sid, proof, action, decision,"
                " dataset_id, file_id, details)"
                " VALUES (%s, %s, %s, 'teams.intent.refused', 'deny', %s, %s, %s::jsonb)",
                (
                    actor,
                    click.sid,
                    "proven" if click.identity_id is not None else None,
                    click.dataset_id,
                    click.file_id if click.dataset_id is not None else None,
                    json.dumps(details),
                ),
            )
    except ApiError:
        log.warning("could not audit a refused intent", extra={"event": "teams_intent"})


INTENT_ROW = """
SELECT t.intent_id, t.notification_id, t.action, t.file_id, t.version_id,
       t.recipient_identity_id, t.used_at, t.revoked_at,
       i.sid, i.entra_object_id, i.groups_resolved_at, n.recall_id, n.audience, n.tracking,
       n.teams_conversation_id, n.teams_activity_id, f.dataset_id
  FROM catalog.teams_intent t
  JOIN catalog.identity i ON i.identity_id = t.recipient_identity_id
  JOIN catalog.notification n ON n.notification_id = t.notification_id
  JOIN catalog.file f ON f.file_id = t.file_id
 WHERE t.intent_id = %s
   FOR UPDATE OF t
"""


async def _act(
    svc: Services,
    teams: TeamsSettings,
    intent: Intent,
    click: _Click,
    now: datetime,
) -> Result:
    async with svc.catalog.tx(PRINCIPAL) as db:
        row = await facts.one(db, INTENT_ROW, (intent.intent_id,))
        # Step 6: the row must say what the token says.
        if (
            row is None
            or row["action"] != intent.action
            or row["file_id"] != intent.file_id
            or row["entra_object_id"] is None
            or str(row["entra_object_id"]).lower() != intent.recipient
        ):
            raise ApiError("intent_invalid", "no matching intent")
        click.identity_id = row["recipient_identity_id"]
        click.sid = row["sid"]
        click.dataset_id = row["dataset_id"]
        if row["revoked_at"] is not None:
            raise ApiError("intent_invalid", "a newer card replaced this one")
        # Step 7: consume once.
        used = await facts.one(
            db,
            "UPDATE catalog.teams_intent SET used_at = now(), used_by_object_id = %s"
            " WHERE intent_id = %s AND used_at IS NULL AND revoked_at IS NULL"
            " RETURNING intent_id",
            (intent.recipient, intent.intent_id),
        )
        if used is None:
            raise ApiError("intent_already_used", "this button was already used")
        identity_id = row["recipient_identity_id"]
        await set_origin(db, f"user:{identity_id}")
        # Step 8: decision 28, with cached groups no older than GROUPS_FRESH. A card stays
        # clickable for the intent's 7 days, so older groups could let a user removed from a
        # group still act. They are refreshed instead (identity_resolve, after this
        # transaction rolls back), and the click is answered 503: the relay releases it and
        # the user clicks again a minute later. Real elapsed time, so the wall clock.
        resolved = row["groups_resolved_at"]
        if resolved is None or svc.wall_clock() - resolved >= facts.GROUPS_FRESH:
            click.stale_groups = True
            raise ApiError(
                "unavailable", "the clicker's AD groups are being refreshed", retry_after_s=60
            )
        if not await facts.can_read(db, row["file_id"], identity_id):
            raise ApiError("forbidden", "the clicker can no longer read the file")
        # Step 9.
        if intent.action == "recall":
            out = await _recall(db, row, identity_id, now)
        elif intent.action == "keep_online":
            out = await _keep_online(db, row, identity_id, teams, now)
        else:
            out = await _track(db, row)
        recall_id = out["recall_id"]
        await db.execute(
            "UPDATE catalog.teams_intent SET result = %s WHERE intent_id = %s",
            (out["result"], intent.intent_id),
        )
        await db.execute(
            "SELECT pgmq.send('notify', %s::jsonb, %s)",
            (json.dumps({"recall_id": str(recall_id), "reason": "click"}), NOTIFY_DELAY_S),
        )
        details = {"intent_id": intent.intent_id, "action": intent.action}
        details.update({k: str(v) for k, v in out.items() if k.endswith("_id")})
        await db.execute(
            "INSERT INTO catalog.audit_log (actor, actor_sid, proof, action, decision,"
            " dataset_id, file_id, version_id, details)"
            " VALUES (%s, %s, 'proven', 'teams.intent.accepted', 'allow', %s, %s, %s, %s::jsonb)",
            (
                f"user:{identity_id}",
                row["sid"],
                row["dataset_id"],
                row["file_id"],
                row["version_id"],
                json.dumps(details),
            ),
        )
        audience = "requester" if intent.action == "recall" else row["audience"]
        tracking = intent.action == "track" or bool(row["tracking"])
        state = await facts.card_state(
            db,
            recall_id,
            identity_id,
            now=now,
            portal_url=teams.portal_url,
            tracking=tracking if audience != "requester" else False,
        )
    reply: dict[str, Any] = {"result": out["result"]}
    if intent.action == "recall":
        reply["recall_id"] = id_str(recall_id)
    if state is not None:
        reply["card"] = cards.render(state, audience, None)
    return Result(200, reply)


async def _recall(db, row: dict, identity_id: int, now: datetime) -> dict[str, Any]:
    """Start or join a recall of the intent's version, with the clicker as a requester."""
    file_id, version_id = row["file_id"], row["version_id"]
    f = await facts.one(
        db,
        "SELECT f.file_id, f.dataset_id, f.size, f.deleted_at, f.state, f.content_version_id,"
        "       d.edge_id"
        "  FROM catalog.file f JOIN catalog.dataset d ON d.dataset_id = f.dataset_id"
        " WHERE f.file_id = %s",
        (file_id,),
    )
    if f is None or f["deleted_at"] is not None:
        raise ApiError("action_not_allowed", "the file is gone")
    try:
        primary = await readable_location(db, file_id, version_id)
    except ApiError:
        raise ApiError("action_not_allowed", "that version has no stored bytes") from None
    # Nothing to recall when the bytes are readable now (not archived, or a live restored
    # copy) or the edge holds this version whole: portal-api's `recall_not_needed`, here a
    # refusal the relay shows as such (409 would read as "already have this request").
    resident = f["state"] == "resident" and f["content_version_id"] == version_id
    if resident or await online_location(db, file_id, version_id) is not None:
        raise ApiError("action_not_allowed", "the file is online; no recall is needed")
    size_row = await facts.one(
        db, "SELECT size FROM catalog.file_version WHERE version_id = %s", (version_id,)
    )
    size = (size_row or {}).get("size") or f["size"]
    eta, _ = promise(
        primary["kind"],
        primary["config"] or {},
        size,
        now,
        native_class=primary.get("storage_class"),
    )
    created = await facts.one(
        db,
        "INSERT INTO catalog.recall (file_id, version_id, edge_id, eta)"
        " VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (file_id, version_id)"
        "   WHERE state IN ('requested', 'rehydrating', 'copying', 'downloading')"
        " DO NOTHING RETURNING recall_id",
        (file_id, version_id, f["edge_id"], eta),
    )
    if created is None:
        joined = await facts.one(
            db,
            "SELECT recall_id FROM catalog.recall"
            " WHERE file_id = %s AND version_id = %s AND state = ANY(%s)",
            (file_id, version_id, list(ACTIVE)),
        )
        if joined is None:  # finished between the insert and the select
            raise ApiError("request_in_progress", "retry", retry_after_s=1)
        recall_id = joined["recall_id"]
    else:
        recall_id = created["recall_id"]
        await db.execute(
            "SELECT pgmq.send('restores', %s::jsonb)", (json.dumps({"recall_id": str(recall_id)}),)
        )
    await db.execute(
        "INSERT INTO catalog.recall_requester (recall_id, identity_id, via, proof)"
        " VALUES (%s, %s, 'teams', 'proven') ON CONFLICT DO NOTHING",
        (recall_id, identity_id),
    )
    if recall_id != row["recall_id"]:
        # The clicked card follows the new recall: its notification takes over the
        # activity, and the old card's other buttons stop working.
        await db.execute(
            "INSERT INTO catalog.notification (recall_id, identity_id, channel, audience,"
            " teams_conversation_id, teams_activity_id)"
            " VALUES (%s, %s, 'teams', 'requester', %s, %s)"
            " ON CONFLICT (recall_id, identity_id, channel) DO UPDATE SET"
            "   teams_conversation_id = coalesce(catalog.notification.teams_conversation_id,"
            "                                    EXCLUDED.teams_conversation_id),"
            "   teams_activity_id = coalesce(catalog.notification.teams_activity_id,"
            "                                EXCLUDED.teams_activity_id)",
            (recall_id, identity_id, row["teams_conversation_id"], row["teams_activity_id"]),
        )
        await db.execute(
            "UPDATE catalog.teams_intent SET revoked_at = now()"
            " WHERE notification_id = %s AND used_at IS NULL AND revoked_at IS NULL",
            (row["notification_id"],),
        )
    await db.execute(
        "INSERT INTO catalog.audit_log (actor, actor_sid, proof, action, decision, dataset_id,"
        " file_id, version_id, details)"
        " VALUES (%s, %s, 'proven', 'recall.request', 'allow', %s, %s, %s, %s::jsonb)",
        (
            f"user:{identity_id}",
            row["sid"],
            f["dataset_id"],
            file_id,
            version_id,
            json.dumps({"recall_id": str(recall_id), "via": "teams"}),
        ),
    )
    return {"result": "queued", "recall_id": recall_id}


async def _keep_online(
    db, row: dict, identity_id: int, teams: TeamsSettings, now: datetime
) -> dict[str, Any]:
    """A keep_online hold on that one file; the clicker's own active hold is extended,
    never shortened. The hold trigger audits it (hold.set / hold.change)."""
    until = now + timedelta(days=KEEP_ONLINE_DAYS)
    existing = await facts.one(
        db,
        "SELECT hold_id, until FROM catalog.hold"
        " WHERE file_id = %s AND kind = 'keep_online' AND released_at IS NULL"
        "   AND set_by_identity_id = %s AND (until IS NULL OR until > %s)"
        " ORDER BY until DESC NULLS FIRST LIMIT 1 FOR UPDATE",
        (row["file_id"], identity_id, now),
    )
    if existing is not None:
        hold_id = existing["hold_id"]
        if existing["until"] is not None and existing["until"] < until:
            await db.execute(
                "UPDATE catalog.hold SET until = %s WHERE hold_id = %s", (until, hold_id)
            )
    else:
        created = await facts.one(
            db,
            "INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_identity_id,"
            " until) VALUES (%s, %s, 'keep_online', 'Teams', %s, %s) RETURNING hold_id",
            (row["dataset_id"], row["file_id"], identity_id, until),
        )
        hold_id = created["hold_id"]  # type: ignore[index]
    return {"result": "done", "recall_id": row["recall_id"], "hold_id": hold_id}


async def _track(db, row: dict) -> dict[str, Any]:
    state = await facts.one(
        db, "SELECT state FROM catalog.recall WHERE recall_id = %s", (row["recall_id"],)
    )
    if state is None or state["state"] not in ACTIVE:
        raise ApiError("action_not_allowed", "the recall has finished")
    await db.execute(
        "UPDATE catalog.notification SET tracking = true, updated_at = now()"
        " WHERE notification_id = %s",
        (row["notification_id"],),
    )
    return {"result": "done", "recall_id": row["recall_id"]}
