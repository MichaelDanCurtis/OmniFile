"""What a dead letter would lose, and how it ends.

A message goes to ``<queue>_dlq`` after `ConsumerConfig.max_failures` failures in
about a minute and a half. Some queues have a sweep that derives the work again
(`tiering`, `notify`, stubs, the CRL). These three had none, so a provider's bad
afternoon lost the work for good, and nothing told anyone but the `dead-letters`
alert. Once a minute this sends them again, further apart each time, and ends them:

- **`restores`:** the recall stays active and every later read joins it
  (`api/recalls.py`), so the user waits for ever. It is sent again, and its promised
  time is cleared, since it no longer means anything (anti-pattern 5: no new one is
  given). After `RedriveConfig.tries` it fails with a reason the portal and Teams
  show, and the user can recall again, unless the provider isn't answering at all
  (see below). A recall that is active with no message and no dead letter (a message
  lost some other way, say purged) is sent again too.
- **`verify`:** the location stays unverified, so nothing is stubbed and the edge
  keeps the bytes. A lost read-back can't make a file verified, but it does stick it
  there. It is sent again. After `tries`, unless the provider isn't answering (below),
  the failure is recorded as a `location.verify` deny, which an archive move acts on
  (`tiering._verifying` drops the copy and keeps the original); an upload stays on the
  edge, and the portal says so on the file (`Entry.read_back_failed_at`). The dead
  letter stays, so the alert does.
- **`retention`:**
  - `delete_blob`: the row stays `deleting` and the bytes stay, and are billed. No
    data is at risk, so it is sent again, and after `tries` audited and left in the
    queue for a person.
  - `annex_purge`: an ArchiveAdmin's purge would never happen (`purge_due` skips one
    already requested). Handled the same way.
  - `worm_policy`: loses nothing, since the `worm_policy` sweep does it again anyway.

**An outage must not fail every recall in the fleet, or deny every read-back.** Before
a recall or a read-back gives up, its own object is asked for, with no transaction open.
An answer (or "not found") means the provider is up and something is wrong with this
one. No answer means an outage: it keeps waiting at the longest interval, for up to
`RedriveConfig.outage_limit`, after which it ends like any other, so nothing waits for
ever. A provider that answers HEAD but refuses every restore (a role lost its
permission, say) fails its recalls after `tries`, which is right: someone has to act.
At most `batch` dead letters are handled per queue per sweep, so a mass failure
reaches users' Teams at that rate.

**An outage that has just ended isn't the file's fault either.** Every try so far may
have failed during it. So once the provider answers at the bound, the message gets one
more try (``_last_try``), and only if that fails does it end.

A dead letter is archived (`pgmq.archive`) when it is sent again, so the queue's depth
and the alert say what is waiting now. Each message counts its re-sends in its own
body (``_redrives``); a successful answer from the provider clears the count
(`recalls._keep_polling`), and so does an operator's resend (`admin dead-letters`).

Times here are the database's, as a dead letter's are, not the worker's virtual clock.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..annex.readback import copy_refused
from ..backends.base import ObjectNotFound, ObjectRef
from .backends import BackendRegistry
from .jobs import Job, SweepJob
from .queue import CONFLICTS_KEY, ERROR_KEY, FAILURES_KEY, THROTTLED_SINCE_KEY, dlq_name, send
from .recalls import ACTIVE, _notify, _set_recall
from .recalls import QUEUE as RESTORES
from .verify import QUEUE as VERIFY
from .verify import READ_ERRORS_KEY

if TYPE_CHECKING:
    from .runner import Worker

log = logging.getLogger(__name__)

RETENTION = "retention"
ACTOR = "system:redrive"
EVERY = timedelta(minutes=1)
REDRIVES_KEY = "_redrives"
FIRST_DEAD_KEY = "_first_dead_at"
#: In a dead letter: nothing more will be tried; a person reads it.
GAVE_UP_KEY = "gave_up"
LAST_TRY_KEY = "_last_try"
#: In a migrate_verify message: since when an outage has held its last try.
HELD_SINCE_KEY = "_held_since"
#: A recall newer than this may have a message on its way (a test's or a tool's row).
LOST_GRACE = timedelta(minutes=2)
#: What the portal shows. Never the provider's text, and never a time.
GAVE_UP_REASON = (
    "The storage service kept failing, so this recall was stopped. Recall the file again to retry."
)


@dataclass(frozen=True)
class RedriveConfig:
    #: The wait before the first re-send doubles each time, up to `longest`.
    first: timedelta = timedelta(minutes=5)
    longest: timedelta = timedelta(hours=1)
    #: Re-sends before giving up. At these waits a recall or a read-back fails about
    #: 5 h 25 min after it started failing: 8 rounds (the first delivery, these 6, a last
    #: try) of 75 s each, and 5, 10, 20 and 40 minutes then four hours between them.
    #: A delete has no last try: 7 rounds, about 4 h 25 min.
    tries: int = 6
    #: How long a provider that answers nothing can keep a recall waiting.
    outage_limit: timedelta = timedelta(days=3)
    probe_timeout: float = 60.0
    #: Dead letters handled per queue, per sweep.
    batch: int = 100


@dataclass(frozen=True)
class Dead:
    msg_id: int
    at: datetime
    error: str
    #: The message as it was when it died.
    body: dict[str, Any]

    @property
    def redrives(self) -> int:
        return int(self.body.get(REDRIVES_KEY, 0))

    @property
    def first_dead_at(self) -> datetime:
        raw = self.body.get(FIRST_DEAD_KEY)
        return datetime.fromisoformat(raw) if raw else self.at


def clean_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """A dead message's body with every count gone: a person's resend, so it has all its
    tries again, with its read errors and its time throttled forgotten."""
    gone = (FAILURES_KEY, ERROR_KEY, CONFLICTS_KEY, REDRIVES_KEY, FIRST_DEAD_KEY,
            READ_ERRORS_KEY, THROTTLED_SINCE_KEY, LAST_TRY_KEY, HELD_SINCE_KEY)  # fmt: skip
    return {k: v for k, v in body.items() if k not in gone}


def _still_counting(body: Mapping[str, Any]) -> dict[str, Any]:
    """This sweep's own re-send keeps the read errors and the time throttled. If the
    provider is still out, the message comes straight back here, on this sweep's
    schedule, rather than looping unseen for another hour or another day."""
    gone = (FAILURES_KEY, ERROR_KEY, CONFLICTS_KEY, LAST_TRY_KEY)
    return {k: v for k, v in body.items() if k not in gone}


def _dead_letter(msg_id: int, at: datetime, letter: Any) -> Dead:
    """A letter that isn't what the queue writes has an empty body, which each queue's sweep
    gives up on, so one bad row can't stop the sweep for the rest."""
    m = letter if isinstance(letter, dict) else {}
    body = m.get("message")
    return Dead(msg_id, at, str(m.get("error") or ""), dict(body) if isinstance(body, dict) else {})


async def _due(conn: psycopg.AsyncConnection, queue: str, cfg: RedriveConfig) -> list[Dead]:
    cur = await conn.execute(
        sql.SQL(
            """
            SELECT msg_id, enqueued_at, message FROM pgmq.{dlq}
             WHERE message->>%(gave_up)s IS NULL
               AND enqueued_at + least(
                     %(longest)s::interval,
                     %(first)s::interval * power(2, least(
                        coalesce((message->'message'->>%(key)s)::int, 0), 20)))
                   <= now()
             ORDER BY msg_id LIMIT %(batch)s
            """
        ).format(dlq=sql.Identifier(f"q_{dlq_name(queue)}")),
        {"gave_up": GAVE_UP_KEY, "longest": cfg.longest, "first": cfg.first, "key": REDRIVES_KEY,
         "batch": cfg.batch},
    )  # fmt: skip
    return [_dead_letter(*r) for r in await cur.fetchall()]


async def _archive(conn: psycopg.AsyncConnection, queue: str, d: Dead) -> bool:
    """False if another worker's sweep took it first."""
    cur = await conn.execute(
        "SELECT pgmq.archive(%s::text, %s::bigint)", (dlq_name(queue), d.msg_id)
    )
    row = await cur.fetchone()
    return bool(row and row[0])


async def _resend(
    conn: psycopg.AsyncConnection, queue: str, d: Dead, count: int, *, last_try: bool = False
) -> bool:
    """Send it again, in one transaction with taking the dead letter."""
    body = {**_still_counting(d.body), REDRIVES_KEY: count,
            FIRST_DEAD_KEY: d.first_dead_at.isoformat()}  # fmt: skip
    if last_try:
        body[LAST_TRY_KEY] = True
    async with conn.transaction():
        if not await _archive(conn, queue, d):
            return False
        await send(conn, queue, body)
    log.warning("queue %s: dead letter %s sent again (%d of the bound): %s", queue, d.msg_id, count,
                d.error[:200])  # fmt: skip
    return True


async def _give_up(conn: psycopg.AsyncConnection, queue: str, d: Dead) -> None:
    """Leave it in the queue, so the alert stays, and don't look at it again."""
    await conn.execute(
        sql.SQL("UPDATE pgmq.{} SET message = message || %s WHERE msg_id = %s").format(
            sql.Identifier(f"q_{dlq_name(queue)}")
        ),
        (Jsonb({GAVE_UP_KEY: True}), d.msg_id),
    )


async def _audit(
    conn: psycopg.AsyncConnection, action: str, decision: str, *,
    dataset_id: int | None = None, file_id: int | None = None, version_id: int | None = None,
    details: dict[str, Any],
) -> None:  # fmt: skip
    await conn.execute(
        "INSERT INTO catalog.audit_log (actor, action, decision, dataset_id, file_id, version_id,"
        " details) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (ACTOR, action, decision, dataset_id, file_id, version_id, Jsonb(details)),
    )


def _int(raw: Any) -> int | None:
    """An id from a message, or None: a letter can be malformed. One too big for a row is
    still an int, and just matches nothing."""
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return None


# --- recalls ------------------------------------------------------------------------


_RECALL_SQL = """
SELECT r.recall_id, r.state, r.eta, r.file_id, r.version_id, f.dataset_id,
       l.backend_id, l.container, l.object_key, l.backend_version_id,
       EXISTS (SELECT 1 FROM pgmq.q_restores q
                WHERE q.message->>'recall_id' = r.recall_id::text) AS live
  FROM catalog.recall r
  JOIN catalog.file f ON f.file_id = r.file_id
  LEFT JOIN catalog.location l
         ON l.version_id = r.version_id AND l.role = 'primary' AND l.state = 'committed'
 WHERE r.recall_id = %s
"""

_LOST_SQL = """
SELECT r.recall_id
  FROM catalog.recall r
 WHERE r.state = ANY(%(active)s)
   AND r.requested_at < now() - %(grace)s::interval
   AND NOT EXISTS (SELECT 1 FROM pgmq.q_restores q
                    WHERE q.message->>'recall_id' = r.recall_id::text)
   AND NOT EXISTS (SELECT 1 FROM pgmq.q_restores_dlq q
                    WHERE q.message->'message'->>'recall_id' = r.recall_id::text)
 ORDER BY r.recall_id LIMIT %(batch)s
"""


@dataclass
class Probe:
    """Is the provider answering? Asks for the object itself, with no transaction open.
    A backend that gave no answer isn't asked again this sweep: a hanging provider must
    not cost `probe_timeout` for every recall."""

    registry: BackendRegistry
    cfg: RedriveConfig
    down: set[int] = field(default_factory=set)
    #: What came back from a backend counted as down: often not silence, but a refusal.
    why: dict[int, str] = field(default_factory=dict)

    async def no_answer(self, row: Mapping[str, Any]) -> bool:
        backend_id = row["backend_id"]
        if backend_id is None:
            return False
        if backend_id in self.down:
            return True
        ref = ObjectRef(backend_id, row["container"], row["object_key"],
                        row["backend_version_id"])  # fmt: skip
        try:
            await asyncio.wait_for(self.registry.get(backend_id).head(ref), self.cfg.probe_timeout)
        except ObjectNotFound:
            return False  # an answer: this object is gone, so waiting won't bring it back
        except KeyError:
            return False  # no binding for it: no wait helps
        except Exception as e:  # noqa: BLE001 - any other outcome is no answer
            log.warning("backend %s gave no answer: %s", backend_id, e)
            self.down.add(backend_id)
            self.why[backend_id] = f"{type(e).__name__}: {e}"
            return True
        return False

    async def hold(self, d: Dead, row: Mapping[str, Any]) -> bool:
        """True: an outage, so this keeps waiting. Not for ever: `outage_limit`."""
        age = datetime.now(UTC) - d.first_dead_at
        return age < self.cfg.outage_limit and await self.no_answer(row)


async def _send_lost(conn: psycopg.AsyncConnection, cfg: RedriveConfig) -> int:
    cur = await conn.execute(
        _LOST_SQL, {"active": list(ACTIVE), "grace": LOST_GRACE, "batch": cfg.batch}
    )
    sent = 0
    for (recall_id,) in await cur.fetchall():
        async with conn.transaction():
            # Looked at again with the row held: the API or a re-send may have got there.
            got = await conn.execute(
                "SELECT state FROM catalog.recall WHERE recall_id = %s FOR UPDATE SKIP LOCKED",
                (recall_id,),
            )
            row = await got.fetchone()
            live = await conn.execute(
                "SELECT 1 FROM pgmq.q_restores WHERE message->>'recall_id' = %s LIMIT 1",
                (str(recall_id),),
            )
            if row is None or row[0] not in ACTIVE or await live.fetchone() is not None:
                continue
            await send(conn, RESTORES, {"recall_id": str(recall_id)})
        log.warning("recall %s is active with no message: sent again", recall_id)
        sent += 1
    return sent


async def _fail_recall(
    conn: psycopg.AsyncConnection, row: dict[str, Any], d: Dead, now: datetime, cfg: RedriveConfig
) -> bool:
    recall_id = row["recall_id"]
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT state FROM catalog.recall WHERE recall_id = %s FOR UPDATE", (recall_id,)
        )
        got = await cur.fetchone()
        if got is None or got[0] not in ACTIVE:
            return False
        await _set_recall(conn, recall_id, "failed", failure=GAVE_UP_REASON, now=now)
        await _notify(conn, recall_id)
        await _audit(
            conn, "recall.gave_up", "info", dataset_id=row["dataset_id"], file_id=row["file_id"],
            version_id=row["version_id"],
            details={"recall_id": str(recall_id), "redrives": d.redrives, "error": d.error[:500]},
        )  # fmt: skip
        await _give_up(conn, RESTORES, d)
    log.error("recall %s failed after %d re-sends: %s", recall_id, d.redrives, d.error[:300])
    return True


async def redrive_recalls(
    conn: psycopg.AsyncConnection, registry: BackendRegistry, now: datetime, cfg: RedriveConfig
) -> int:
    done = await _send_lost(conn, cfg)
    cur = conn.cursor(row_factory=dict_row)
    probe = Probe(registry, cfg)
    for d in await _due(conn, RESTORES, cfg):
        recall_id = _int(d.body.get("recall_id"))
        row = None
        if recall_id is not None:
            await cur.execute(_RECALL_SQL, (recall_id,))
            row = await cur.fetchone()
        if recall_id is None:
            await _give_up(conn, RESTORES, d)  # malformed: a person reads it
            continue
        if row is None or row["state"] not in ACTIVE or row["live"]:
            # Nobody is waiting on this message, or another is at work for them.
            await _archive(conn, RESTORES, d)
            continue
        if d.redrives < cfg.tries:
            async with conn.transaction():
                if await _resend(conn, RESTORES, d, d.redrives + 1):
                    # The promised time no longer means anything. No new one is made up.
                    cleared = await conn.execute(
                        "UPDATE catalog.recall SET eta = NULL, updated_at = %s"
                        " WHERE recall_id = %s AND eta IS NOT NULL",
                        (now, recall_id),
                    )
                    if cleared.rowcount:
                        await _notify(conn, recall_id)
                    await _audit(
                        conn, "recall.redrive", "info", dataset_id=row["dataset_id"],
                        file_id=row["file_id"], version_id=row["version_id"],
                        details={"recall_id": str(recall_id), "redrives": d.redrives + 1,
                                 "error": d.error[:500]},
                    )  # fmt: skip
                    done += 1
            continue
        if await probe.hold(d, row):
            # An outage: this isn't the recall's fault. Keep it waiting.
            done += await _resend(conn, RESTORES, d, d.redrives)
            continue
        if not d.body.get(LAST_TRY_KEY):
            done += await _resend(conn, RESTORES, d, d.redrives, last_try=True)
            continue
        done += await _fail_recall(conn, row, d, now, cfg)
    return done


# --- verify ---------------------------------------------------------------------------


async def redrive_verify(
    conn: psycopg.AsyncConnection, registry: BackendRegistry, cfg: RedriveConfig
) -> int:
    done = 0
    probe = Probe(registry, cfg)
    for d in await _due(conn, VERIFY, cfg):
        location_id = _int(d.body.get("location_id"))
        if location_id is None:
            await _give_up(conn, VERIFY, d)
            continue
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT state, verified, file_id, version_id, backend_id, container, object_key,"
            " backend_version_id FROM catalog.location WHERE location_id = %s",
            (location_id,),
        )
        row = await cur.fetchone()
        live = await conn.execute(
            "SELECT 1 FROM pgmq.q_verify WHERE message->>'location_id' = %s LIMIT 1",
            (str(location_id),),
        )
        again = d.body.get("purpose") == "removal"
        if (
            row is None
            or row["state"] != "committed"
            or (row["verified"] and not again)
            or await live.fetchone() is not None
        ):
            await _archive(conn, VERIFY, d)  # nothing is waiting on this read-back
            continue
        if d.redrives < cfg.tries:
            done += await _resend(conn, VERIFY, d, d.redrives + 1)
            continue
        if await probe.hold(d, row):
            # An outage: nothing is wrong with this file. Denying every read-back would
            # drop every archive copy, and leave every upload for a person to resend.
            done += await _resend(conn, VERIFY, d, d.redrives)
            continue
        if not d.body.get(LAST_TRY_KEY):
            done += await _resend(conn, VERIFY, d, d.redrives, last_try=True)
            continue
        # The location stays unverified: nothing is stubbed, and the edge keeps the bytes.
        rounds = d.redrives + 2  # the first delivery, the re-sends, the last try
        why = f"the read-back kept failing, {rounds} rounds: {d.error}"[:1000]
        log.error("verify %s gave up: %s", location_id, why)
        async with conn.transaction():
            await _audit(
                conn, "location.verify", "deny", file_id=row["file_id"],
                version_id=row["version_id"],
                details={"location_id": str(location_id), "reason": why, "cause": "gave_up"},
            )  # fmt: skip
            if not again:
                await copy_refused(conn, location_id, row["file_id"], row["version_id"], why,
                                   "gave_up")  # fmt: skip
            await _give_up(conn, VERIFY, d)
        done += 1
    return done


# --- retention ------------------------------------------------------------------------


async def _retention_waiting(conn: psycopg.AsyncConnection, body: Mapping[str, Any]) -> bool | None:
    """Is the work still wanted? None: a message this sweep doesn't understand."""
    op = body.get("op")
    if op == "worm_policy":
        return False  # the `worm_policy` sweep does this anyway
    if op == "delete_blob" and (lid := _int(body.get("location_id"))) is not None:
        cur = await conn.execute(
            "SELECT 1 FROM catalog.location WHERE location_id = %s AND state = 'deleting'", (lid,)
        )
        return await cur.fetchone() is not None
    if op == "annex_purge":
        annex, file_id = _int(body.get("annex_id")), _int(body.get("file_id"))
        if annex is None or file_id is None:
            return None
        cur = await conn.execute(
            "SELECT 1 FROM catalog.annex_file WHERE annex_id = %s AND file_id = %s"
            " AND purge_requested_at IS NOT NULL AND purged_at IS NULL",
            (annex, file_id),
        )
        return await cur.fetchone() is not None
    return None


async def redrive_retention(conn: psycopg.AsyncConnection, cfg: RedriveConfig) -> int:
    done = 0
    for d in await _due(conn, RETENTION, cfg):
        waiting = await _retention_waiting(conn, d.body)
        if waiting is None:
            await _give_up(conn, RETENTION, d)
        elif not waiting:
            await _archive(conn, RETENTION, d)
        elif d.redrives < cfg.tries:
            done += await _resend(conn, RETENTION, d, d.redrives + 1)
        else:
            log.error("retention message %s gave up: %s", d.msg_id, d.error[:300])
            async with conn.transaction():
                await _audit(
                    conn, "retention.gave_up", "info",
                    details={"message": clean_body(d.body), "redrives": d.redrives,
                             "error": d.error[:500]},
                )  # fmt: skip
                await _give_up(conn, RETENTION, d)
            done += 1
    return done


# --- the sweep ------------------------------------------------------------------------


async def redrive(
    conn: psycopg.AsyncConnection,
    registry: BackendRegistry,
    now: datetime,
    cfg: RedriveConfig | None = None,
) -> int:
    """One pass. Returns how much it did. `conn` is autocommit: no transaction is open
    during `_provider_down`."""
    cfg = cfg or RedriveConfig()
    return (
        await redrive_recalls(conn, registry, now, cfg)
        + await redrive_verify(conn, registry, cfg)
        + await redrive_retention(conn, cfg)
    )


def redrive_jobs(worker: Worker, config: RedriveConfig | None = None) -> list[Job]:
    async def sweep(conn: psycopg.AsyncConnection, now: datetime) -> int:
        return await redrive(conn, worker.registry, now, config)

    return [SweepJob("dead_letter_redrive", EVERY, sweep)]
