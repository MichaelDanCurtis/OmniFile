"""The `verify` queue: read a committed upload back and check its CRC64-NVME.

Upload `complete` (control-api.md section 4.2) checks the committed block
list, but Azure's Get Block List has no per-block CRC, so the CRCs it would
compare are the edge's own word. The api then commits the version with
`verified = false` and sends ``{"location_id"}`` here in the same
transaction. This handler reads the pinned blob version back through the
backend, computes CRC64-NVME over every byte (the decision-24 verifier
pattern: an in-Azure read, never the edge's claim) and, only if it equals
the catalog's value and the size matches, sets ``verified = true``
(``verified_by = azure_read_crc``). Nothing may be stubbed before that
(`api/stubs.py`, `worker/stubs.py` and the policy engine all require
`verified`).

A mismatch is audited (``location.verify`` / deny) and logged; the location
stays unverified, so the edge keeps the bytes. Re-uploading it is the
reconciler's job: for an annexed file the annex shows a problem and its edge is
asked to reconcile at once (`annex/readback.py`).

The read of a big file takes hours, so it runs with no transaction open (`outside`):
this handler then runs again and reads its row afresh. A message that dead-letters
changes nothing about the location, which stays unverified: `redrive.py` sends it
again, and in the end records a deny and keeps the dead letter, so the alert stays.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import timedelta

import psycopg

from ..annex.readback import copy_refused
from ..backends.base import (
    BackendError,
    NotRestored,
    ObjectNotFound,
    ObjectRef,
    Throttled,
)
from ..backends.sim.crc64 import crc64nvme, to_bytes
from .backends import BackendRegistry
from .jobs import QueueJob
from .queue import ConsumerConfig, Message, Permanent, Retry, outside, throttled

log = logging.getLogger(__name__)

QUEUE = "verify"
VERIFIED_BY = "azure_read_crc"
READ_ERRORS_KEY = "_read_errors"
READ_ERROR_DELAY = 30.0
READ_ERROR_MAX_DELAY = 900.0
#: About a day of provider errors.
MAX_READ_ERRORS = 100

LOCATION_SQL = """
SELECT l.location_id, l.backend_id, l.container, l.object_key, l.backend_version_id,
       l.size, l.crc64nvme, l.state, l.verified, l.file_id, l.version_id
  FROM catalog.location l
 WHERE l.location_id = %s
"""


async def read_crc(backend, ref: ObjectRef, size: int) -> tuple[int, int]:
    """(bytes read, CRC64-NVME) of the object, streamed."""
    crc, n = 0, 0
    async for chunk in backend.get_range(ref, 0, size + 1):  # +1: notice a longer blob
        crc = crc64nvme(chunk, crc)
        n += len(chunk)
    return n, crc


async def handle_verify(
    msg: Message,
    conn: psycopg.AsyncConnection,
    *,
    registry: BackendRegistry,
    actor: str = "system:worker",
) -> None:
    try:
        location_id = int(msg.body["location_id"])
    except (KeyError, TypeError, ValueError):
        raise Permanent(f"bad verify message {dict(msg.body)!r}") from None
    row = await (await conn.execute(LOCATION_SQL, (location_id,))).fetchone()
    if row is None:
        log.info("verify %s: no such location", location_id)
        return
    (_, backend_id, container, key, bversion, size, crc, state, verified, file_id, vid) = row
    # A removal from a NAS (annex A2.2), and a replace at the origin (A3.4), read a
    # verified copy again: the bytes Azure holds now, not only the bytes it was sent
    # (annex.md section 3.3).
    purpose = msg.body.get("purpose")
    again = purpose in ("removal", "replace")
    if state != "committed" or (verified and not again):
        return
    if crc is None or size is None:
        raise Permanent(f"location {location_id} has no CRC or size to check against")
    backend = registry.get(backend_id)
    if not hasattr(backend, "get_range"):
        raise Permanent(f"backend {backend_id} cannot read objects back")
    ref = ObjectRef(backend_id, container, key, bversion)
    try:
        n, actual = await outside(msg, "read_crc", lambda: read_crc(backend, ref, int(size)))
    except Throttled as exc:
        floor = max(exc.retry_after or timedelta(0), timedelta(seconds=5))
        raise throttled(msg, exc, floor if exc.retry_after else None) from exc
    except (ObjectNotFound, NotRestored) as exc:
        why = f"{type(exc).__name__}: {exc}"
        await _deny(conn, row, why, actor, "missing")
        if not again:
            await copy_refused(conn, location_id, file_id, vid, why, "missing")
        return
    except BackendError as exc:
        # An outage: wait it out, further apart each time. Not for ever, or a blob that
        # always errors would stay stuck with no word: a fresh message each try keeps
        # even the queue-age alert quiet. Past this it dead-letters like anything else.
        errors = int(msg.body.get(READ_ERRORS_KEY, 0)) + 1
        if errors >= MAX_READ_ERRORS:
            raise
        delay = min(READ_ERROR_MAX_DELAY, READ_ERROR_DELAY * 2 ** (errors - 1))
        raise Retry(delay, {**msg.body, READ_ERRORS_KEY: errors}) from exc
    if n != size or to_bytes(actual) != bytes(crc):
        why = (
            f"read {n} bytes, CRC64-NVME {actual:016x}; catalog says"
            f" {size} bytes, {bytes(crc).hex()}"
        )
        await _deny(conn, row, why, actor, "mismatch")
        if again:
            await _removal_copy_mismatch(conn, row, why, str(purpose))
        else:
            await copy_refused(conn, location_id, file_id, vid, why, "mismatch")
        return
    await conn.execute(
        "UPDATE catalog.location SET verified = true, verified_at = now(), verified_by = %s"
        " WHERE location_id = %s AND state = 'committed' AND NOT verified AND crc64nvme = %s",
        (VERIFIED_BY, location_id, bytes(crc)),
    )
    await conn.execute(
        "UPDATE catalog.location SET azure_read_at = now()"
        " WHERE location_id = %s AND state = 'committed' AND crc64nvme = %s",
        (location_id, bytes(crc)),
    )
    log.info("verify %s (file %s, version %s): ok", location_id, file_id, vid)


async def _removal_copy_mismatch(
    conn: psycopg.AsyncConnection, row, why: str, purpose: str = "removal"
) -> None:
    """A copy read again before a NAS removal or replace doesn't match what was copied:
    the removal waits for good (no fresh read, no ticket), and the annex's
    Owners see it on its Problems list. An alert, too: Azure holds other
    bytes than were sent."""
    file_id, version_id = row[9], row[10]
    log.error("ALERT annex copy mismatch before a %s: file %s version %s: %s",
              purpose, file_id, version_id, why)  # fmt: skip
    await conn.execute(
        "INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, version_id, details)"
        " SELECT af.annex_id, 'copy_mismatch', af.path, af.file_id, %s, %s::jsonb"
        "   FROM catalog.annex_file af WHERE af.file_id = %s",
        (version_id, json.dumps({"reason": "copy_mismatch", "detail": why[:900],
                                 "purpose": purpose}), file_id),
    )  # fmt: skip


async def _deny(conn: psycopg.AsyncConnection, row, why: str, actor: str, cause: str) -> None:
    location_id, *_rest = row
    file_id, version_id = row[9], row[10]
    log.error("verify %s (file %s, version %s) FAILED: %s", location_id, file_id, version_id, why)
    await conn.execute(
        "INSERT INTO catalog.audit_log (actor, action, decision, file_id, version_id, details)"
        " VALUES (%s, 'location.verify', 'deny', %s, %s, %s::jsonb)",
        (
            actor,
            file_id,
            version_id,
            json.dumps({"location_id": str(location_id), "reason": why, "cause": cause}),
        ),
    )


#: A read-back reads a whole file (a 184 GB SEG-Y takes a long time), so a message stays
#: hidden this long and is read one at a time: otherwise it would come back to another reader
#: mid-read and be read again in full (review of #34). The worker and the verifier job use it.
VISIBILITY_S = 2 * 60 * 60


def verify_jobs(
    registry: BackendRegistry,
    base: ConsumerConfig | None = None,
    actor: str = "system:worker",
) -> list[QueueJob]:
    async def handler(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_verify(msg, conn, registry=registry, actor=actor)

    config = replace(base or ConsumerConfig(), visibility_timeout=VISIBILITY_S, batch=1)
    return [QueueJob(QUEUE, handler, config)]
