"""Dead letters: the messages the worker gave up on (``<queue>_dlq``).

The `dead-letters` alert fires while any queue here is not empty. ``list`` shows what
is in them. ``resend`` puts a message back on its queue with its counts cleared, so it
has its full number of tries. ``drop`` takes one out without running it. Neither
destroys it: both archive it to ``pgmq.a_<queue>_dlq``, and both are audited, one row
for each message, with who signed in.

``drop`` clears the alert, so it refuses, without ``--force``, a message nothing would
send again: it says what would be stranded, and changes nothing. ``--all`` needs
``--yes``, since it acts on messages nobody has read.

The worker re-sends `restores`, `verify` and `retention` dead letters by itself, for a
while (`worker/redrive.py`), so one here may be waiting for that, or it may have
``gave_up`` set, which means only a person will do anything about it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..worker.queue import dlq_name
from ..worker.redrive import GAVE_UP_KEY, _int, clean_body
from .backends import Refused, _audit

RESEND_WARNING = (
    "A resent message runs its handler again, with all its tries. One that fails every"
    " time comes back here after about a minute and a half, having repeated whatever it"
    " does at the provider each try. Find out why it failed first (the error above, and the"
    " worker's log). A message for work that has ended since (a failed recall, say) does"
    " nothing."
)


@dataclass(frozen=True)
class Letter:
    queue: str
    msg_id: int
    dead_at: datetime
    failures: int
    error: str
    gave_up: bool
    message: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "queue": self.queue,
            "msg_id": str(self.msg_id),
            "dead_at": self.dead_at.isoformat(),
            "failures": self.failures,
            "error": self.error,
            "gave_up": self.gave_up,
            "message": self.message,
        }


def queues(conn: psycopg.Connection) -> list[str]:
    """Every queue with a dead-letter queue, as its plain name."""
    rows = conn.execute(
        "SELECT queue_name FROM pgmq.meta WHERE queue_name LIKE '%\\_dlq' ORDER BY 1"
    ).fetchall()
    return [r[0].removesuffix("_dlq") for r in rows]


def _known(conn: psycopg.Connection, queue: str) -> str:
    name = queue.removesuffix("_dlq")
    if name not in queues(conn):
        raise Refused(f"no dead-letter queue for {queue!r}; there are: {', '.join(queues(conn))}")
    return name


def _letter(queue: str, row: Mapping[str, Any]) -> Letter:
    # A malformed letter is exactly what an operator has to be able to read and remove.
    m = row["message"] if isinstance(row["message"], dict) else {}
    body = m.get("message")
    failures = m.get("failures")
    return Letter(
        queue, row["msg_id"], row["enqueued_at"], failures if isinstance(failures, int) else 0,
        str(m.get("error") or ""), bool(m.get(GAVE_UP_KEY)),
        dict(body) if isinstance(body, dict) else {},
    )  # fmt: skip


def letters(
    conn: psycopg.Connection, queue: str | None = None, msg_ids: Sequence[int] | None = None
) -> list[Letter]:
    names = [_known(conn, queue)] if queue is not None else queues(conn)
    out: list[Letter] = []
    cur = conn.cursor(row_factory=dict_row)
    for name in names:
        query = sql.SQL("SELECT msg_id, enqueued_at, message FROM pgmq.{}").format(
            sql.Identifier(f"q_{dlq_name(name)}")
        )
        if msg_ids is not None:
            query += sql.SQL(" WHERE msg_id = ANY(%s)")
        query += sql.SQL(" ORDER BY msg_id")
        cur.execute(query, (list(msg_ids),) if msg_ids is not None else None)
        out += [_letter(name, r) for r in cur.fetchall()]
    return out


#: Queues with nothing to send a message again, and what stops without it.
_NOT_SENT_AGAIN = {
    "migrate": "the migration prefix stops in its state",
    "migrate_verify": "the copy stays pending, so its migration prefix never reaches verified",
    "seed": "the source stays seeding",
    "snapshot_export": "the export stays running until it expires",
    "portal": "the version restore, download or placement never finishes",
    "cloud_task": "the Settings task stays pending",
    "settings_test": "the Settings test stays pending",
}


def stranded(conn: psycopg.Connection, x: Letter) -> str | None:
    """What dropping this message would leave undone for good, with no alert and nothing
    to send it again. None if nothing: its work has ended, a sweep derives it, or it names
    nothing that could exist."""
    body = x.message
    if x.queue == "verify":
        lid = _int(body.get("location_id"))
        if lid is None:
            return None
        removal = body.get("purpose") == "removal"
        row = conn.execute(
            "SELECT 1 FROM catalog.location WHERE location_id = %s"
            " AND state = 'committed' AND (NOT verified OR %s)",
            (lid, removal),
        ).fetchone()
        if row is None:
            return None
        if removal:
            return f"location {lid} isn't read again, so the NAS removal waiting on it never runs"
        return (
            f"location {lid} stays unverified: its file never becomes a stub, and the edge"
            " keeps its bytes"
        )
    if x.queue == "retention" and body.get("op") == "delete_blob":
        lid = _int(body.get("location_id"))
        if lid is None:
            return None
        row = conn.execute(
            "SELECT 1 FROM catalog.location WHERE location_id = %s AND state = 'deleting'", (lid,)
        ).fetchone()
        return (
            None
            if row is None
            else f"location {lid} stays deleting, and its bytes stay, and are billed"
        )
    if x.queue == "retention" and body.get("op") == "annex_purge":
        annex, file_id = _int(body.get("annex_id")), _int(body.get("file_id"))
        if annex is None or file_id is None:
            return None
        row = conn.execute(
            "SELECT 1 FROM catalog.annex_file WHERE annex_id = %s AND file_id = %s"
            " AND purge_requested_at IS NOT NULL AND purged_at IS NULL",
            (annex, file_id),
        ).fetchone()
        return None if row is None else f"file {file_id}'s annex purge never runs"
    return _NOT_SENT_AGAIN.get(x.queue)


def _take(conn: psycopg.Connection, letter: Letter) -> bool:
    row = conn.execute(
        "SELECT pgmq.archive(%s::text, %s::bigint)", (dlq_name(letter.queue), letter.msg_id)
    ).fetchone()
    return bool(row and row[0])


def _chosen(
    conn: psycopg.Connection, queue: str, msg_ids: Sequence[int], everything: bool, yes: bool
) -> list[Letter]:
    if not everything and not msg_ids:
        raise Refused("name messages with --msg-id, or all of the queue's with --all")
    if everything and msg_ids:
        raise Refused("--all or --msg-id, not both")
    found = letters(conn, queue, None if everything else msg_ids)
    if everything and not yes:
        raise Refused(
            f"--all would act on {len(found)} message(s) in"
            f" {dlq_name(queue.removesuffix('_dlq'))}. Read them with list, then add --yes"
        )
    missing = set(msg_ids) - {x.msg_id for x in found}
    if missing:
        raise Refused(f"no message {sorted(missing)} in {dlq_name(queue.removesuffix('_dlq'))}")
    return found


def resend(
    conn: psycopg.Connection,
    queue: str,
    msg_ids: Sequence[int] = (),
    *,
    everything: bool = False,
    yes: bool = False,
    signed_in: Mapping[str, str] | None = None,
) -> list[Letter]:
    """Put them back on their queue, each in one transaction with its audit row."""
    done: list[Letter] = []
    for x in _chosen(conn, queue, msg_ids, everything, yes):
        with conn.transaction():
            if not _take(conn, x):
                continue  # the worker's sweep got there first
            conn.execute(
                "SELECT pgmq.send(%s::text, %s::jsonb)", (x.queue, Jsonb(clean_body(x.message)))
            )
            with conn.cursor() as cur:
                _audit(cur, "dead_letter.resend", _details(x, signed_in))
        done.append(x)
    return done


def drop(
    conn: psycopg.Connection,
    queue: str,
    msg_ids: Sequence[int] = (),
    *,
    everything: bool = False,
    yes: bool = False,
    force: bool = False,
    signed_in: Mapping[str, str] | None = None,
) -> list[tuple[Letter, str | None]]:
    """Take them out without running them, each with what that strands. They stay in the
    queue's archive table. Refuses, changing nothing, if any would strand work, unless
    `force`."""
    chosen = [(x, stranded(conn, x)) for x in _chosen(conn, queue, msg_ids, everything, yes)]
    lost = [(x, why) for x, why in chosen if why]
    if lost and not force:
        lines = "; ".join(f"message {x.msg_id}: {why}" for x, why in lost[:10])
        more = f"; and {len(lost) - 10} more" if len(lost) > 10 else ""
        raise Refused(
            f"dropping clears the alert, and nothing would send these again: {lines}{more}."
            " Fix the cause and resend, or add --force to strand them"
        )
    done: list[tuple[Letter, str | None]] = []
    for x, why in chosen:
        with conn.transaction():
            if not _take(conn, x):
                continue
            with conn.cursor() as cur:
                details = _details(x, signed_in)
                if why:
                    details["stranded"] = why
                _audit(cur, "dead_letter.drop", details)
        done.append((x, why))
    return done


def _details(x: Letter, signed_in: Mapping[str, str] | None) -> dict[str, Any]:
    details = {"queue": x.queue, "msg_id": str(x.msg_id), "error": x.error[:500],
               "message": clean_body(x.message)}  # fmt: skip
    if signed_in:
        details["entra"] = dict(signed_in)
    return details
