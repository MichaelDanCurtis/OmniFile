"""Keep `s3_metadata` sources current from their S3 Metadata journal table
(docs/design/catalog-seeding.md section 8; sources/metadata_s3.py).

Once a source is seeded, each pass reads the journal rows under its prefix
after its cursor (`catalog.source.journal`), in order, and applies each as
the change record an S3 event would be (`changefeed_sync.apply_record`): a
`CREATE` or `DELETE` is a prompt, and the key's current object is HEADed
(`sources/follow.py`); an `UPDATE_METADATA` sets the location's storage
class. The first pass starts at the source's `snapshot_at`, so changes made
while it was seeding are applied too.

The bucket's EventBridge events are still read (`changefeed_sync`): restores
come only from there, and an object change that arrives both ways is
applied twice, harmlessly (both HEAD the same object). Nothing is LISTed and
nothing is written to the bucket or the tables (decision 40).

**At least once.** Rows are applied in batches; each batch commits with the
cursor after its last row, so a crash replays at most one batch.
"""

from __future__ import annotations

import logging
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..backends.base import BackendError
from ..retention.common import native_to_logical, set_actor
from ..sources import metadata_s3
from ..sources.follow import heads_to_follow
from .backends import BackendRegistry
from .changefeed_sync import _skip_record, apply_record

log = logging.getLogger(__name__)

JOURNAL_ACTOR = "system:journal_sync"
#: Rows read per pass, and applied per transaction.
READ_LIMIT = 5000
BATCH = 200


async def follow_journal(
    conn: psycopg.AsyncConnection,
    registry: BackendRegistry,
    source_id: int,
    now: datetime,
    *,
    limit: int = READ_LIMIT,
    batch: int = BATCH,
) -> int:
    """One pass of a source's journal on an autocommit connection. Returns
    the rows applied."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT backend_id, container, prefix, inventory, journal, state, snapshot_at"
        "  FROM catalog.source WHERE source_id = %s",
        (source_id,),
    )
    src = await cur.fetchone()
    if (
        src is None
        or src["state"] != "seeded"
        or (src["inventory"] or {}).get("kind") != metadata_s3.KIND
    ):
        return 0
    backend = registry.get(src["backend_id"])
    journal = metadata_s3.MetadataJournal(
        backend, src["container"], src["prefix"], src["inventory"]
    )
    got = await journal.read(src["journal"], src["snapshot_at"], limit)
    tiers = native_to_logical(backend.capabilities())
    expected = src["journal"]
    applied = 0
    chunks = [got.rows[i : i + batch] for i in range(0, len(got.rows), batch)] or [[]]
    for n, chunk in enumerate(chunks):
        last = n == len(chunks) - 1
        new = got.end if last else got.cursor_after(chunk[-1])
        if not chunk and new == expected:
            break
        # Before anything is written, so no transaction id waits on the provider.
        heads = await heads_to_follow(
            conn, backend, [jr.record for jr in chunk if jr.record is not None]
        )
        async with conn.transaction():
            await set_actor(conn, JOURNAL_ACTOR)
            await cur.execute(
                "SELECT journal FROM catalog.source WHERE source_id = %s"
                "   AND state = 'seeded' FOR UPDATE SKIP LOCKED",
                (source_id,),
            )
            row = await cur.fetchone()
            if row is None or row["journal"] != expected:
                return applied  # another worker has it, or moved it on
            for jr in chunk:
                if jr.record is None:
                    continue
                try:
                    async with conn.transaction():  # a savepoint per row
                        await apply_record(conn, backend, jr.record, now, tiers, heads)
                except BackendError:
                    raise  # transient (a HEAD failed): the batch is read again
                except Exception as e:  # noqa: BLE001 - recorded as drift, not retried forever
                    await _skip_record(conn, src["backend_id"], jr.record, e, now)
                applied += 1
            await conn.execute(
                "UPDATE catalog.source SET journal = %s, updated_at = %s WHERE source_id = %s",
                (Jsonb(new), now, source_id),
            )
        expected = new
    return applied


async def journal_all(
    conn: psycopg.AsyncConnection, registry: BackendRegistry, now: datetime
) -> int:
    """One pass over every seeded `s3_metadata` source (the periodic sweep)."""
    cur = await conn.execute(
        "SELECT source_id FROM catalog.source WHERE state = 'seeded'"
        "   AND inventory->>'kind' = %s ORDER BY source_id",
        (metadata_s3.KIND,),
    )
    total = 0
    for (source_id,) in await cur.fetchall():
        try:
            total += await follow_journal(conn, registry, source_id, now)
        except Exception as e:  # noqa: BLE001 - one source must not stop the others
            log.warning("journal of source %s: %s", source_id, e)
            await conn.execute(
                "UPDATE catalog.source SET last_error = %s, updated_at = %s WHERE source_id = %s",
                (f"journal: {type(e).__name__}: {e}"[:2000], now, source_id),
            )
        else:
            await conn.execute(
                "UPDATE catalog.source SET last_error = NULL WHERE source_id = %s"
                "   AND last_error LIKE 'journal:%%'",
                (source_id,),
            )
    return total


__all__ = ["follow_journal", "journal_all"]
