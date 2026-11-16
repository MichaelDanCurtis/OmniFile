"""The worker's notification work (teams.md sections 3, 6, 7 and 8).

`Notifier.handle_recall` is the `notify` consumer: each message `{"recall_id"[, "reason"]}`
means "bring every card of this recall up to date", and is idempotent. Per recipient:

1. the Graph identity is fresh enough (user 24 h, groups 1 h), or the recipient is skipped;
2. the recipient can still read the file, or gets one last "no access" card;
3. coalescing: a card goes out only if what it shows changed, or a click asked for it, and
   at most once per 10 s per notification;
4. the bot's conversation exists (proactive install through Graph), or the tenant blocked
   it and the recipient gets email;
5. fresh intents are minted (one `teams_intent` row per button) and the card is sent, or
   updated in place; the previous card version's unused intents are revoked;
6. failures: 429 waits `Retry-After`; 5xx and timeouts back off 10 s .. 15 min; after 1 h,
   email. 403/404: the conversation is cleared and re-created once, then email.

Everything runs in the consumer's transaction, so the catalog rows and the ack commit
together; HTTP calls have short timeouts. Nothing here logs tokens, intents, card bodies,
names, mail addresses or objectIds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ..worker.queue import Message, Permanent, Retry, outside
from . import cards, facts
from .botconnector import BotConnector, BotError
from .cards import Audience, CardState
from .graph import GraphClient, GraphError, group_sids_for
from .intents import IntentSigner

log = logging.getLogger("omnifile_service.notify")

ACTOR = "system:notify"
USER_FRESH = timedelta(hours=24)
GROUPS_FRESH = facts.GROUPS_FRESH
NOT_FOUND_RECHECK = timedelta(hours=24)
GRAPH_ERROR_BACKOFF = timedelta(minutes=5)
COALESCE = timedelta(seconds=10)
FALLBACK_AFTER = timedelta(hours=1)
BACKOFF_MIN = 10.0
BACKOFF_MAX = 15 * 60.0
GROUP_AUDIENCE_CAP = 50
#: `worker.tiering`'s message kind, for a pin a forced Archive overrode.
PIN_OVERRIDDEN = "pin_overridden"
NOTICE_GIVES_UP = timedelta(days=1)


class Rate:
    """At most `per_second` events per key in any second (per worker process)."""

    def __init__(self, per_second: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_second = per_second
        self.clock = clock
        self._seen: dict[str, deque[float]] = {}

    async def wait(self, key: str) -> None:
        q = self._seen.setdefault(key, deque())
        while True:
            now = self.clock()
            while q and q[0] <= now - 1.0:
                q.popleft()
            if len(q) < self.per_second:
                q.append(now)
                return
            await asyncio.sleep(max(0.01, q[0] + 1.0 - now))


@dataclass
class NotifyConfig:
    tenant_id: str
    teams_app_id: str
    service_url: str
    portal_url: str | None = None
    sender: str | None = None  # the sendMail mailbox
    #: Teams' published limits, as assumed (VERIFY on a real tenant).
    per_conversation_per_second: int = 7
    per_bot_per_second: int = 50


@dataclass
class Notifier:
    config: NotifyConfig
    graph: GraphClient
    bot: BotConnector
    signer: IntentSigner | None
    #: Intents carry real time (the relay checks them against its own clock), even when the
    #: worker runs on a virtual clock.
    wall_clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _conv_rate: Rate = field(init=False)
    _bot_rate: Rate = field(init=False)

    def __post_init__(self) -> None:
        self.config.tenant_id = self.config.tenant_id.lower()
        self._conv_rate = Rate(self.config.per_conversation_per_second)
        self._bot_rate = Rate(self.config.per_bot_per_second)

    # =========================================================================
    # The notify consumer
    # =========================================================================

    async def handle(self, msg: Message, conn: psycopg.AsyncConnection, now: datetime) -> None:
        if msg.body.get("kind") == PIN_OVERRIDDEN:
            await self.handle_pin_overridden(msg, conn, now)
        else:
            await self.handle_recall(msg, conn, now)

    async def handle_pin_overridden(
        self, msg: Message, conn: psycopg.AsyncConnection, now: datetime
    ) -> None:
        """One notice to whoever set a pin that a forced Archive overrode. It has no state of
        its own, so a crash just after the send sends it again. The calls run with no
        transaction open, and the audit follows them."""
        try:
            file_id = int(str(msg.body["file_id"]))
            identity_id = int(str(msg.body["identity_id"]))
            tier = str(msg.body["tier"])
        except (KeyError, ValueError):
            raise Permanent("a pin notice needs a file, an identity and a tier") from None
        since = datetime.fromisoformat(str(msg.body.get("since") or now.isoformat()))
        outcome, delay = await outside(
            msg, "notice",
            lambda: self._deliver_pin_notice(conn, file_id, identity_id, tier, now, since),
        )  # fmt: skip
        if delay is not None:
            body = {k: v for k, v in msg.body.items() if not k.startswith("_")}
            raise Retry(max(1.0, delay), {**body, "since": since.isoformat()})
        row = await facts.one(
            conn,
            "SELECT dataset_id, file_id, content_version_id AS version_id FROM catalog.file"
            " WHERE file_id = %s",
            (file_id,),
        )
        if row is not None:
            await self._audit(conn, f"pin.notice.{outcome}", row,
                              {"identity_id": str(identity_id), "tier": tier})  # fmt: skip

    async def _deliver_pin_notice(
        self, conn, file_id: int, identity_id: int, tier: str, now: datetime, since: datetime
    ) -> tuple[str, float | None]:
        """How it went: `teams`, `email` or `skipped` and no delay, or a delay to try again."""
        f = await facts.one(
            conn,
            "SELECT f.dataset_id, p.body FROM catalog.file f LEFT JOIN catalog.policy p"
            " ON p.dataset_id = f.dataset_id AND p.state = 'active' WHERE f.file_id = %s",
            (file_id,),
        )
        if f is None:
            return "skipped", None
        status = await self.resolve_identity(conn, identity_id, now)
        if not await facts.can_read(conn, file_id, identity_id):
            return "skipped", None  # no card about a file this person can't read
        settings = facts.notify_settings(f["body"])
        share, path = await facts.share_and_path(conn, f["dataset_id"], file_id)
        name = (share or {}).get("name")
        ident = await facts.one(
            conn,
            "SELECT entra_object_id, mail FROM catalog.identity WHERE identity_id = %s",
            (identity_id,),
        )
        overdue = now - since >= FALLBACK_AFTER

        async def by_email() -> tuple[str, float | None]:
            if (
                not settings.get("email_fallback", True)
                or not (ident or {}).get("mail")
                or not self.config.sender
            ):
                return "skipped", None
            subject, body = cards.pin_overridden_email(path, name, tier)
            try:
                await self.graph.send_mail(self.config.sender, ident["mail"], subject, body)
            except GraphError as exc:
                if now - since >= NOTICE_GIVES_UP:
                    return "skipped", None
                return "", exc.retry_after or BACKOFF_MAX
            return "email", None

        if status == "error" and not (ident or {}).get("entra_object_id"):
            return ("", GRAPH_ERROR_BACKOFF.total_seconds()) if not overdue else await by_email()
        if not ident or ident["entra_object_id"] is None:
            return await by_email()
        oid = str(ident["entra_object_id"]).lower()
        card = cards.pin_overridden_card(path, name, tier, self.config.portal_url)
        summary = cards.pin_overridden_text(path, name, tier)[0]
        for fresh_install in (False, True):
            conv = await self._conversation(conn, identity_id, oid, now)
            if conv is None:
                return await by_email()
            if isinstance(conv, float):
                return ("", conv) if not overdue else await by_email()
            try:
                await self._bot_rate.wait("bot")
                await self._conv_rate.wait(conv["conversation_id"])
                await self.bot.send(conv["service_url"], conv["conversation_id"], card, summary)
            except BotError as exc:
                if exc.gone and not fresh_install:
                    await conn.execute(
                        "DELETE FROM catalog.teams_conversation WHERE identity_id = %s",
                        (identity_id,),
                    )
                    continue
                if exc.gone or overdue:
                    return await by_email()
                return "", exc.retry_after or BACKOFF_MIN
            return "teams", None
        return await by_email()

    async def handle_recall(
        self, msg: Message, conn: psycopg.AsyncConnection, now: datetime
    ) -> None:
        try:
            recall_id = int(str(msg.body["recall_id"]))
        except (KeyError, ValueError):
            raise Permanent("notify message needs a recall_id") from None
        reason = msg.body.get("reason")
        r = await facts.recall_row(conn, recall_id)
        if r is None:
            raise Permanent(f"no recall {recall_id}")
        settings = facts.notify_settings(r["policy"])
        await self._recipients(conn, r, settings, now)
        notes = await facts.rows(
            conn,
            """
            SELECT n.*, i.sid, i.kind, i.entra_object_id, i.mail, i.graph_state
              FROM catalog.notification n
              JOIN catalog.identity i ON i.identity_id = n.identity_id
             WHERE n.recall_id = %s AND n.channel = 'teams'
             ORDER BY n.notification_id
               FOR UPDATE OF n
            """,
            (recall_id,),
        )
        delays: list[float] = []
        for n in notes:
            delay = await self._notification(conn, r, n, settings, now, reason == "click")
            if delay is not None:
                delays.append(delay)
        if delays:
            raise Retry(max(1.0, min(delays)), {"recall_id": str(recall_id)})

    async def _recipients(self, conn, r: dict, settings: dict, now: datetime) -> None:
        """Requesters always (if the policy says so); owners and watchers at `requested`."""
        if settings.get("requester", True):
            await conn.execute(
                """
                INSERT INTO catalog.notification (recall_id, identity_id, channel, audience)
                SELECT DISTINCT %(r)s, q.identity_id, 'teams', 'requester'
                  FROM catalog.recall_requester q
                 WHERE q.recall_id = %(r)s AND q.identity_id IS NOT NULL
                ON CONFLICT (recall_id, identity_id, channel) DO NOTHING
                """,
                {"r": r["recall_id"]},
            )
        anonymous = await facts.one(
            conn,
            "SELECT count(*) AS n FROM catalog.recall_requester"
            " WHERE recall_id = %s AND identity_id IS NULL AND app_id IS NULL",
            (r["recall_id"],),
        )
        if anonymous and anonymous["n"]:
            await self._skip_once(conn, r, None, "no_identity")
        if r["state"] != "requested":
            return
        groups: list[tuple[str, str]] = []
        if settings.get("owners"):
            share, _ = await facts.share_and_path(conn, r["dataset_id"], r["file_id"])
            if share and share.get("owners_group_sid"):
                groups.append(("owner", share["owners_group_sid"]))
        if isinstance(settings.get("watchers_group_sid"), str):
            groups.append(("watcher", settings["watchers_group_sid"]))
        if not groups:
            return
        done = await facts.one(
            conn,
            "SELECT 1 AS x FROM catalog.notification WHERE recall_id = %s"
            " AND audience IN ('owner', 'watcher') LIMIT 1",
            (r["recall_id"],),
        )
        if done is not None:
            return
        members: list[tuple[str, Any]] = []
        for audience, group_sid in groups:
            oid = await self._group_object_id(conn, group_sid)
            if oid is None:
                continue
            try:
                users = await self.graph.group_members(oid, GROUP_AUDIENCE_CAP)
            except GraphError as exc:
                log.warning("group expansion failed", extra={"reason": exc.code})
                return
            members += [(audience, u) for u in users]
        if len(members) > GROUP_AUDIENCE_CAP:
            await self._audit(
                conn,
                "notify.skipped",
                r,
                {"recall_id": str(r["recall_id"]), "reason": "audience_over_cap"},
            )
            return
        for audience, u in members:
            if not u.sid:
                continue
            ident = await self._upsert_user(conn, u, now)
            if ident is None:
                continue
            await conn.execute(
                "INSERT INTO catalog.notification (recall_id, identity_id, channel, audience)"
                " VALUES (%s, %s, 'teams', %s)"
                " ON CONFLICT (recall_id, identity_id, channel) DO NOTHING",
                (r["recall_id"], ident, audience),
            )

    async def _group_object_id(self, conn, group_sid: str) -> str | None:
        row = await facts.one(
            conn,
            "SELECT entra_object_id FROM catalog.identity WHERE sid = %s AND kind = 'group'",
            (group_sid,),
        )
        return str(row["entra_object_id"]) if row and row["entra_object_id"] else None

    async def _upsert_user(self, conn, u, now: datetime) -> int | None:
        try:
            async with conn.transaction():
                row = await facts.one(
                    conn,
                    """
                    INSERT INTO catalog.identity (sid, kind, entra_object_id, display_name, upn,
                                                  mail, graph_state, graph_resolved_at)
                    VALUES (%s, 'user', %s, %s, %s, %s, 'resolved', %s)
                    ON CONFLICT (sid) DO UPDATE SET
                        entra_object_id = EXCLUDED.entra_object_id,
                        display_name = EXCLUDED.display_name, upn = EXCLUDED.upn,
                        mail = EXCLUDED.mail, graph_state = 'resolved',
                        graph_resolved_at = EXCLUDED.graph_resolved_at, updated_at = now()
                    RETURNING identity_id
                    """,
                    (u.sid, u.object_id, u.display_name, u.upn, u.mail, now),
                )
        except psycopg.errors.UniqueViolation:
            return None  # another identity holds that objectId; leave it to resolution
        return row["identity_id"] if row else None

    # =========================================================================
    # One recipient
    # =========================================================================

    async def _notification(
        self, conn, r: dict, n: dict, settings: dict, now: datetime, click: bool
    ) -> float | None:
        """Bring one notification up to date. Returns a delay if it must be tried again."""
        if n["state"] == "skipped":
            return None
        audience = Audience(n["audience"])
        if audience is not Audience.REQUESTER and n["card_version"] > 0 and not n["tracking"]:
            return None  # owners and watchers: one card, then only if they press Track
        status = await self.resolve_identity(conn, n["identity_id"], now)
        if status == "not_found":
            return await self._skip(conn, r, n, "not_in_entra")
        ident = await facts.one(
            conn,
            "SELECT entra_object_id, mail FROM catalog.identity WHERE identity_id = %s",
            (n["identity_id"],),
        )
        if not ident or ident["entra_object_id"] is None:
            if status == "error":
                return GRAPH_ERROR_BACKOFF.total_seconds()
            return await self._skip(conn, r, n, "not_in_entra")
        oid = str(ident["entra_object_id"]).lower()
        readable = await facts.can_read(conn, r["file_id"], n["identity_id"])
        state = await facts.card_state(
            conn,
            r["recall_id"],
            n["identity_id"],
            now=now,
            portal_url=self.config.portal_url,
            tracking=bool(n["tracking"]) and audience is not Audience.REQUESTER,
            no_access=not readable,
        )
        if state is None:
            return None
        if n["state"] == "fell_back":
            return await self._email(conn, r, n, ident, state, settings, now)
        if not readable and n["teams_activity_id"] is None:
            return await self._skip(conn, r, n, "no_access")
        if state.key == n["last_card_key"] and not click:
            return None
        if n["last_sent_at"] is not None and now - n["last_sent_at"] < COALESCE:
            wait = n["last_sent_at"] + COALESCE
            await self._set(conn, n, next_attempt_at=wait)
            return (wait - now).total_seconds()
        if n["next_attempt_at"] is not None and n["next_attempt_at"] > now and not click:
            return (n["next_attempt_at"] - now).total_seconds()
        return await self._send_card(conn, r, n, oid, state, audience, settings, now)

    async def _send_card(
        self,
        conn,
        r: dict,
        n: dict,
        oid: str,
        state: CardState,
        audience: Audience,
        settings: dict,
        now: datetime,
    ) -> float | None:
        reinstalled = False
        while True:
            conv = await self._conversation(conn, n["identity_id"], oid, now)
            if conv is None:  # blocked by tenant policy, or no Teams licence
                return await self._fall_back(conn, r, n, state, settings, now, "blocked")
            if isinstance(conv, float):
                return conv
            minted = self._mint(state, audience, oid, r["file_id"])
            card = cards.render(state, audience, {a: m.token for a, m in minted.items()})
            summary = cards.plain_title(state)
            conv_id = n["teams_conversation_id"] or conv["conversation_id"]
            try:
                await self._bot_rate.wait("bot")
                await self._conv_rate.wait(conv_id)
                if n["teams_activity_id"]:
                    await self.bot.update(
                        conv["service_url"], conv_id, n["teams_activity_id"], card, summary
                    )
                    activity_id, event = n["teams_activity_id"], "teams.card.updated"
                else:
                    activity_id = await self.bot.send(conv["service_url"], conv_id, card, summary)
                    event = "teams.card.sent"
            except BotError as exc:
                if exc.gone and not reinstalled:
                    reinstalled = True
                    await conn.execute(
                        "DELETE FROM catalog.teams_conversation WHERE identity_id = %s",
                        (n["identity_id"],),
                    )
                    n = await self._set(conn, n, teams_activity_id=None, teams_conversation_id=None)
                    continue
                return await self._bot_failure(conn, r, n, state, settings, now, exc)
            break
        version = n["card_version"] + 1
        for action, m in minted.items():
            await conn.execute(
                "INSERT INTO catalog.teams_intent (intent_id, notification_id, action, file_id,"
                " version_id, recipient_identity_id, issued_at, expires_at, card_version)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    m.intent_id,
                    n["notification_id"],
                    action,
                    r["file_id"],
                    r["version_id"],
                    n["identity_id"],
                    m.issued_at,
                    m.expires_at,
                    version,
                ),
            )
        await conn.execute(
            "UPDATE catalog.teams_intent SET revoked_at = now()"
            " WHERE notification_id = %s AND used_at IS NULL AND revoked_at IS NULL"
            "   AND card_version IS DISTINCT FROM %s",
            (n["notification_id"], version),
        )
        await self._set(
            conn,
            n,
            state="skipped" if state.no_access else "sent",
            teams_conversation_id=conv_id,
            teams_activity_id=activity_id,
            card_version=version,
            last_card_state=state.state,
            last_card_key=state.key,
            last_sent_at=now,
            next_attempt_at=None,
            failing_since=None,
            last_error=None,
        )
        await self._audit(
            conn,
            event,
            r,
            {
                "recall_id": str(r["recall_id"]),
                "notification_id": str(n["notification_id"]),
                "state": state.state,
                "audience": audience.value,
                "card_version": version,
            },
        )
        return None

    def _mint(self, state: CardState, audience: Audience, oid: str, file_id: int) -> dict:
        if self.signer is None:
            return {}
        wall = self.wall_clock()
        return {
            action: self.signer.mint(
                action=action,
                file_id=file_id,
                recipient=oid,
                tenant=self.config.tenant_id,
                now=wall,
            )
            for action in cards.actions_needing_intents(state, audience)
        }

    async def _bot_failure(
        self, conn, r: dict, n: dict, state: CardState, settings: dict, now: datetime, exc
    ) -> float | None:
        attempts = n["attempts"] + 1
        if exc.gone:  # second 403/404 after a fresh install
            await self._audit_failed(conn, r, n, exc.code)
            return await self._fall_back(conn, r, n, state, settings, now, exc.code)
        if exc.throttled:
            delay = exc.retry_after if exc.retry_after is not None else 1.0
            await self._set(
                conn,
                n,
                attempts=attempts,
                last_error=exc.code,
                next_attempt_at=now + timedelta(seconds=delay),
            )
            return delay
        since = n["failing_since"] or now
        await self._audit_failed(conn, r, n, exc.code)
        if now - since >= FALLBACK_AFTER:
            return await self._fall_back(conn, r, n, state, settings, now, exc.code)
        delay = min(BACKOFF_MAX, BACKOFF_MIN * 2 ** max(0, attempts - 1))
        await self._set(
            conn,
            n,
            state="failed",
            attempts=attempts,
            last_error=exc.code,
            failing_since=since,
            next_attempt_at=now + timedelta(seconds=delay),
        )
        return delay

    async def _audit_failed(self, conn, r: dict, n: dict, code: str) -> None:
        await self._audit(
            conn,
            "teams.card.failed",
            r,
            {
                "recall_id": str(r["recall_id"]),
                "notification_id": str(n["notification_id"]),
                "error": code,
            },
        )

    # --- conversation (teams.md 6.2) -----------------------------------------

    async def _conversation(self, conn, identity_id: int, oid: str, now: datetime):
        """The active conversation row; None if blocked; a float delay on a transient error."""
        row = await facts.one(
            conn,
            "SELECT * FROM catalog.teams_conversation WHERE identity_id = %s FOR UPDATE",
            (identity_id,),
        )
        if row is not None:
            return None if row["state"] == "blocked" else row
        try:
            await self.graph.install_app(oid, self.config.teams_app_id)
            install_id = await self.graph.installation_id(oid, self.config.teams_app_id)
            if install_id is None:
                raise GraphError("not_installed")
            chat = await self.graph.chat_id(oid, install_id)
        except GraphError as exc:
            if exc.status == 403:
                await conn.execute(
                    "INSERT INTO catalog.teams_conversation (identity_id, state, last_error)"
                    " VALUES (%s, 'blocked', %s)",
                    (identity_id, exc.code),
                )
                return None
            log.warning("teams install failed", extra={"reason": exc.code})
            return exc.retry_after or BACKOFF_MIN
        return await facts.one(
            conn,
            "INSERT INTO catalog.teams_conversation (identity_id, state, conversation_id,"
            " service_url, install_id, installed_at) VALUES (%s, 'active', %s, %s, %s, %s)"
            " RETURNING *",
            (identity_id, chat, self.config.service_url, install_id, now),
        )

    # --- email (teams.md 8) ----------------------------------------------------

    async def _fall_back(
        self, conn, r: dict, n: dict, state: CardState, settings: dict, now: datetime, why: str
    ) -> float | None:
        n = await self._set(conn, n, state="fell_back", last_error=why, next_attempt_at=None)
        ident = await facts.one(
            conn, "SELECT mail FROM catalog.identity WHERE identity_id = %s", (n["identity_id"],)
        )
        return await self._email(conn, r, n, ident or {}, state, settings, now)

    async def _email(
        self, conn, r: dict, n: dict, ident: dict, state: CardState, settings: dict, now: datetime
    ) -> float | None:
        if not settings.get("email_fallback", True) or state.no_access:
            return None
        e = await facts.one(
            conn,
            "INSERT INTO catalog.notification (recall_id, identity_id, channel, audience)"
            " VALUES (%s, %s, 'email', %s)"
            " ON CONFLICT (recall_id, identity_id, channel) DO UPDATE SET updated_at = now()"
            " RETURNING *",
            (r["recall_id"], n["identity_id"], n["audience"]),
        )
        assert e is not None
        if e["state"] == "skipped":
            return None
        final = state.state in ("available", "failed", "superseded")
        sent = e["last_card_state"]
        if final:
            if sent in ("available", "failed", "superseded"):
                return None
        elif state.state in cards.ACTIVE:
            if sent is not None:
                return None
        else:
            return None  # cancelled: no mail
        if e["next_attempt_at"] is not None and e["next_attempt_at"] > now:
            return (e["next_attempt_at"] - now).total_seconds()
        if not ident.get("mail") or not self.config.sender:
            await self._set(conn, e, state="skipped", last_error="no_mail")
            await self._skip_once(conn, r, n["identity_id"], "no_mail")
            return None
        subject, body = cards.email(state)
        try:
            await self.graph.send_mail(self.config.sender, ident["mail"], subject, body)
        except GraphError as exc:
            attempts = e["attempts"] + 1
            delay = (
                exc.retry_after
                if exc.retry_after is not None
                else min(BACKOFF_MAX, BACKOFF_MIN * 2 ** (attempts - 1))
            )
            await self._set(
                conn,
                e,
                state="failed",
                attempts=attempts,
                last_error=exc.code,
                next_attempt_at=now + timedelta(seconds=delay),
            )
            return delay
        await self._set(
            conn,
            e,
            state="sent",
            last_card_state=state.state,
            last_sent_at=now,
            next_attempt_at=None,
            last_error=None,
        )
        await self._audit(
            conn,
            "email.sent",
            r,
            {"recall_id": str(r["recall_id"]), "notification_id": str(e["notification_id"])},
        )
        return None

    # --- identity resolution (teams.md 7) -------------------------------------

    async def resolve_identity(
        self, conn, identity_id: int, now: datetime, *, force: bool = False
    ) -> str:
        """Refresh the user (24 h) and their groups (1 h) from Graph.

        Returns `resolved`, `not_found`, `error` (cached data, if any, is kept) or `busy`
        (another worker holds the row)."""
        row = await facts.one(
            conn,
            "SELECT identity_id, sid, kind, entra_object_id, graph_state, graph_resolved_at,"
            "       graph_next_attempt_at, groups_resolved_at"
            "  FROM catalog.identity WHERE identity_id = %s FOR UPDATE SKIP LOCKED",
            (identity_id,),
        )
        if row is None:
            return "busy"
        if row["kind"] != "user":
            return row["graph_state"] or "not_found"
        state = row["graph_state"]
        stale = row["graph_resolved_at"] is None or now - row["graph_resolved_at"] >= USER_FRESH
        waiting = row["graph_next_attempt_at"] is not None and row["graph_next_attempt_at"] > now
        if force or (stale and not waiting):
            state = await self._resolve_user(conn, row, now)
        if state != "resolved":
            return state or "error"
        groups_stale = (
            row["groups_resolved_at"] is None or now - row["groups_resolved_at"] >= GROUPS_FRESH
        )
        if force or groups_stale:
            oid = await facts.one(
                conn,
                "SELECT entra_object_id FROM catalog.identity WHERE identity_id = %s",
                (identity_id,),
            )
            try:
                found = await self.graph.transitive_group_sids(str(oid["entra_object_id"]))
            except GraphError as exc:
                log.warning("group refresh failed", extra={"reason": exc.code})
                return "resolved"  # the cached groups stand
            await conn.execute(
                "UPDATE catalog.identity SET group_sids = %s::catalog.sid[],"
                " groups_resolved_at = %s, groups_source = 'graph', updated_at = now()"
                " WHERE identity_id = %s",
                (group_sids_for(row["sid"], found), now, identity_id),
            )
        return "resolved"

    async def _resolve_user(self, conn, row: dict, now: datetime) -> str:
        iid = row["identity_id"]
        try:
            user = await self.graph.user_by_sid(row["sid"])
        except GraphError as exc:
            await conn.execute(
                "UPDATE catalog.identity SET graph_error = %s, graph_next_attempt_at = %s,"
                " graph_state = CASE WHEN graph_state = 'resolved' THEN graph_state"
                "                    ELSE 'error' END, updated_at = now()"
                " WHERE identity_id = %s",
                (exc.code, now + GRAPH_ERROR_BACKOFF, iid),
            )
            return "resolved" if row["graph_state"] == "resolved" else "error"
        if user is None:
            await conn.execute(
                "UPDATE catalog.identity SET graph_state = 'not_found', graph_error = NULL,"
                " graph_resolved_at = %s, graph_next_attempt_at = %s, updated_at = now()"
                " WHERE identity_id = %s",
                (now, now + NOT_FOUND_RECHECK, iid),
            )
            return "not_found"
        try:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE catalog.identity SET entra_object_id = %s, display_name = %s,"
                    " upn = %s, mail = %s, graph_state = 'resolved', graph_error = NULL,"
                    " graph_resolved_at = %s, graph_next_attempt_at = NULL, updated_at = now()"
                    " WHERE identity_id = %s",
                    (user.object_id, user.display_name, user.upn, user.mail, now, iid),
                )
        except psycopg.errors.UniqueViolation:
            await conn.execute(
                "UPDATE catalog.identity SET graph_state = 'error',"
                " graph_error = 'object_id_conflict', graph_next_attempt_at = %s"
                " WHERE identity_id = %s",
                (now + NOT_FOUND_RECHECK, iid),
            )
            return "error"
        return "resolved"

    async def handle_identity_resolve(
        self, msg: Message, conn: psycopg.AsyncConnection, now: datetime
    ) -> None:
        """`identity_resolve` {"identity_id"}: from `web` (group overage) and new SIDs."""
        try:
            identity_id = int(str(msg.body["identity_id"]))
        except (KeyError, ValueError):
            raise Permanent("identity_resolve needs an identity_id") from None
        status = await self.resolve_identity(conn, identity_id, now, force=True)
        if status == "busy":
            raise Retry(5.0)

    async def sync_group_directory(self, conn: psycopg.AsyncConnection, now: datetime) -> int:
        """Nightly: synced AD groups' names and SIDs, for the portal's group picker."""
        count = 0
        async with conn.transaction():
            async for g in self.graph.synced_groups():
                sid = g.get("onPremisesSecurityIdentifier")
                gid = g.get("id")
                if not isinstance(sid, str) or not isinstance(gid, str):
                    continue
                try:
                    async with conn.transaction():
                        await conn.execute(
                            """
                            INSERT INTO catalog.identity (sid, kind, entra_object_id,
                                display_name, sam_account_name, graph_state, graph_resolved_at)
                            VALUES (%s, 'group', %s, %s, %s, 'resolved', %s)
                            ON CONFLICT (sid) DO UPDATE SET
                                entra_object_id = EXCLUDED.entra_object_id,
                                display_name = EXCLUDED.display_name,
                                sam_account_name = EXCLUDED.sam_account_name,
                                graph_state = 'resolved',
                                graph_resolved_at = EXCLUDED.graph_resolved_at,
                                updated_at = now()
                             WHERE catalog.identity.kind = 'group'
                            """,
                            (
                                sid,
                                gid.lower(),
                                g.get("displayName"),
                                g.get("onPremisesSamAccountName"),
                                now,
                            ),
                        )
                except (psycopg.errors.UniqueViolation, psycopg.errors.CheckViolation):
                    continue
                count += 1
        return count

    async def sweep_due(self, conn: psycopg.AsyncConnection, now: datetime) -> int:
        """Re-send `notify` for recalls whose notifications are due and have no message
        waiting (a lost or dead-lettered one)."""
        async with conn.transaction():
            due = await facts.rows(
                conn,
                """
                SELECT DISTINCT n.recall_id FROM catalog.notification n
                 WHERE n.next_attempt_at IS NOT NULL
                   AND n.next_attempt_at < %s - interval '2 minutes'
                   AND n.state IN ('queued', 'sent', 'failed')
                   AND NOT EXISTS (SELECT 1 FROM pgmq.q_notify q
                                    WHERE q.message->>'recall_id' = n.recall_id::text)
                 LIMIT 500
                """,
                (now,),
            )
            for d in due:
                await conn.execute(
                    "SELECT pgmq.send('notify', %s::jsonb)",
                    (json.dumps({"recall_id": str(d["recall_id"])}),),
                )
        return len(due)

    # --- small helpers ---------------------------------------------------------

    async def _skip(self, conn, r: dict, n: dict, reason: str) -> None:
        await self._set(conn, n, state="skipped", last_error=reason, next_attempt_at=None)
        await self._skip_once(conn, r, n["identity_id"], reason)
        return None

    async def _skip_once(self, conn, r: dict, identity_id: int | None, reason: str) -> None:
        seen = await facts.one(
            conn,
            "SELECT 1 AS x FROM catalog.audit_log WHERE file_id = %s AND action = 'notify.skipped'"
            "   AND details->>'recall_id' = %s AND details->>'reason' = %s"
            "   AND (details->>'identity_id') IS NOT DISTINCT FROM %s LIMIT 1",
            (
                r["file_id"],
                str(r["recall_id"]),
                reason,
                None if identity_id is None else str(identity_id),
            ),
        )
        if seen is None:
            details = {"recall_id": str(r["recall_id"]), "reason": reason}
            if identity_id is not None:
                details["identity_id"] = str(identity_id)
            await self._audit(conn, "notify.skipped", r, details)

    async def _set(self, conn, n: dict, **values: Any) -> dict:
        cols = ", ".join(f"{k} = %({k})s" for k in values)
        row = await facts.one(
            conn,
            f"UPDATE catalog.notification SET {cols}, updated_at = now()"
            " WHERE notification_id = %(nid)s RETURNING *",
            {**values, "nid": n["notification_id"]},
        )
        return {**n, **(row or {})}

    async def _audit(self, conn, action: str, r: dict, details: dict) -> None:
        await conn.execute(
            "INSERT INTO catalog.audit_log (actor, action, decision, dataset_id, file_id,"
            " version_id, details) VALUES (%s, %s, 'info', %s, %s, %s, %s::jsonb)",
            (ACTOR, action, r["dataset_id"], r["file_id"], r["version_id"], json.dumps(details)),
        )
        log.info(
            action,
            extra={
                "event": action,
                "recall_id": details.get("recall_id"),
                "notification_id": details.get("notification_id"),
                "reason": details.get("reason") or details.get("error"),
            },
        )
