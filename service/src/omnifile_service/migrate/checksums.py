"""Expected checksums for copies of S3 objects that have none stored
(docs/design/migration-engine.md section 5).

S3 stores a full-object CRC64NVME for uploads that carried one and adds one
to uploads that carried none [V]; objects written before then have none, so
`verify_copy` can't compare their copies. For those, one S3 Batch Operations
Compute checksum job at a time per migration prefix computes it at rest (any
class, no restore; up to about a week for Glacier) [V]:

- `plan_checksums`: S3 objects whose copies wait (a pending copy location)
  and that have no CRC64NVME, not asked about before;
- `checksum_progress`: when the job is Complete, its report's values go on
  the S3 locations (`verified_by = 's3_batch_checksum'`: S3 computed them
  itself, from the bytes at rest) and the waiting copies are queued for
  verification again. Objects the job couldn't checksum (SSE-C) are named
  in the prefix's `last_error` and stay unverified.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..backends.base import ObjectRef
from ..worker.queue import Message, send
from .calls import provider
from .opsbuckets import batch_writes_source

log = logging.getLogger("omnifile_service.migrate")

CHECKSUM_MAX = 100_000
FINAL = frozenset({"Complete", "Failed", "Cancelled"})
#: Failed jobs per prefix before its objects are left unverified.
JOB_ATTEMPTS = 3
#: The call whose answer must go on record at once (`engine.migrate_step`).
CHECKSUM_TAG = "checksum_job"
#: A job's report: up to `CHECKSUM_MAX` rows, read whole.
REPORT_SECONDS = 900.0

_WAITING = """
SELECT DISTINCT s.location_id, s.container, s.object_key, s.backend_version_id
  FROM catalog.location t
  JOIN catalog.location s ON s.version_id = t.version_id AND s.backend_id = %(sb)s
                         AND s.container = %(sc)s AND s.state = 'committed'
 WHERE t.backend_id = %(tb)s AND t.container = %(tc)s AND t.state = 'pending'
   AND t.object_key COLLATE "C" LIKE %(tlike)s
   AND s.crc64nvme IS NULL
"""


def _params(m: Mapping[str, Any], tlike: str) -> dict[str, Any]:
    return {"sb": m["src_backend_id"], "sc": m["src_container"], "tb": m["to_backend_id"],
            "tc": m["to_container"], "tlike": tlike}  # fmt: skip


async def plan_checksums(
    conn: psycopg.AsyncConnection,
    backend: Any,
    m: Mapping[str, Any],
    tlike: str,
    now: datetime,
    msg: Message | None = None,
) -> int | None:
    """Start a checksum job if the prefix has none running and copies wait on
    objects with no checksum. Returns the job's catalog id."""
    if getattr(getattr(backend, "config", None), "batch", None) is None:
        return None
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT 1 FROM catalog.checksum_job WHERE migration_id = %s AND state = 'running'",
        (m["migration_id"],),
    )
    if await cur.fetchone() is not None:
        return None
    # A job still running under a cancelled prefix has nobody to read its report, so its
    # objects are asked about again: their copies could never be checked otherwise.
    await cur.execute(
        _WAITING
        + """
           AND NOT EXISTS (SELECT 1 FROM catalog.checksum_job_object o
                             JOIN catalog.checksum_job j USING (checksum_job_id)
                             JOIN catalog.migration_prefix p ON p.migration_id = j.migration_id
                            WHERE o.location_id = s.location_id
                              AND NOT (j.state = 'running' AND p.state = 'cancelled'))
         ORDER BY s.location_id
         LIMIT %(max)s
        """,
        {**_params(m, tlike), "max": CHECKSUM_MAX},
    )
    rows = one_kind(await cur.fetchall())
    if not rows:
        return None
    if await batch_writes_source(conn, backend, m, now):
        return None
    await cur.execute(
        "SELECT count(*) AS n FROM catalog.checksum_job WHERE migration_id = %s",
        (m["migration_id"],),
    )
    n = int((await cur.fetchone())["n"])  # type: ignore[index]
    refs = [ObjectRef(m["src_backend_id"], r["container"], r["object_key"],
                      r["backend_version_id"]) for r in rows]  # fmt: skip
    name = job_name("checksum", m["migration_id"], n + 1, refs)

    # The job is created before anything is written, and its name (the request
    # token) is the same on a retry over the same objects: a lost CreateJob
    # response finds the same job, and a failed call leaves no row behind.
    async def create() -> tuple[str, str, list[Any]]:
        return await backend.batch_checksum(refs, name=name), name, rows

    made = await provider(msg, CHECKSUM_TAG, create)
    return await record_checksum_job(conn, m, now, made)


async def record_checksum_job(
    conn: psycopg.AsyncConnection, m: Mapping[str, Any], now: datetime, made: Any
) -> int:
    """A job that was created: the objects it was asked about, whatever the catalog says now."""
    remote, name, rows = made
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "INSERT INTO catalog.checksum_job"
        " (migration_id, backend_id, name, job_id, objects, requested_at)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING checksum_job_id",
        (m["migration_id"], m["src_backend_id"], name, remote, len(rows), now),
    )
    job = await cur.fetchone()
    assert job is not None
    jid = int(job["checksum_job_id"])
    await cur.execute(
        "INSERT INTO catalog.checksum_job_object (checksum_job_id, location_id)"
        " SELECT %s, unnest(%s::bigint[])",
        (jid, [r["location_id"] for r in rows]),
    )
    log.info("checksum job", extra={"checksum_job_id": jid, "objects": len(rows)})
    return jid


def job_name(kind: str, migration_id: int, n: int, refs: Sequence[ObjectRef]) -> str:
    """A Batch job's name, and its request token. It names the objects too. A retry over
    the same objects finds the same job. A retry after a crash, once the catalog has
    moved, asks for other objects, and S3 refuses a token used with other parameters."""
    digest = hashlib.sha256()
    for r in refs:
        digest.update(f"{r.container}\0{r.key}\0{r.version_id}\n".encode())
    return f"omnifile-{kind}-{migration_id}-{n}-{digest.hexdigest()[:12]}"


def one_kind(rows: list[Any]) -> list[Any]:
    """A job's manifest names a version for every object or for none (S3
    Batch Operations), so a job takes objects of one kind: those with a
    version id, or (in a later job) those without."""
    if not rows:
        return rows
    versioned = rows[0]["backend_version_id"] is not None
    return [r for r in rows if (r["backend_version_id"] is not None) == versioned]


async def checksum_progress(
    conn: psycopg.AsyncConnection,
    backend: Any,
    m: Mapping[str, Any],
    now: datetime,
    msg: Message | None = None,
) -> str | None:
    """The running checksum job after this look: None (none), 'running' or
    'done' (its values applied, the waiting copies queued again)."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT * FROM catalog.checksum_job WHERE migration_id = %s AND state = 'running'"
        " FOR UPDATE",
        (m["migration_id"],),
    )
    j = await cur.fetchone()
    if j is None or j["job_id"] is None:
        return None
    status = await provider(msg, f"job:{j['job_id']}", lambda: backend.batch_job(j["job_id"]))
    if status["status"] not in FINAL:
        return "running"
    values = {}
    if status["status"] == "Complete":
        values = await provider(
            msg,
            f"report:{j['job_id']}",
            lambda: backend.batch_checksums(j["job_id"], name=j["name"]),
            seconds=REPORT_SECONDS,
        )
    await cur.execute(
        "SELECT l.location_id, l.version_id, l.container, l.object_key, l.backend_version_id"
        "  FROM catalog.checksum_job_object o JOIN catalog.location l USING (location_id)"
        " WHERE o.checksum_job_id = %s",
        (j["checksum_job_id"],),
    )
    found = 0
    for r in await cur.fetchall():
        crc = values.get((r["container"], r["object_key"], r["backend_version_id"]))
        if crc is None and r["backend_version_id"] is None:
            crc = values.get((r["container"], r["object_key"], "null"))
        if crc is None:
            continue
        found += 1
        await conn.execute(
            "UPDATE catalog.location SET crc64nvme = %s, verified = true, verified_at = %s,"
            " verified_by = 's3_batch_checksum' WHERE location_id = %s AND crc64nvme IS NULL",
            (crc, now, r["location_id"]),
        )
        # And on the file, if it has none: the edge can then read the S3
        # object too (every fill is checked against the file's CRC).
        await conn.execute(
            "UPDATE catalog.file SET crc64nvme = %s"
            " WHERE content_version_id = %s AND crc64nvme IS NULL",
            (crc, r["version_id"]),
        )
        tcur = await conn.execute(
            "SELECT t.location_id FROM catalog.location t JOIN catalog.location s"
            "    ON s.version_id = t.version_id"
            " WHERE s.location_id = %s AND t.backend_id = %s AND t.state = 'pending'",
            (r["location_id"], m["to_backend_id"]),
        )
        for (tid,) in await tcur.fetchall():
            body = {"location_id": int(tid), "migration_id": int(m["migration_id"])}
            await send(conn, "migrate_verify", body)
    done = status["status"] == "Complete"
    await cur.execute(
        "UPDATE catalog.checksum_job SET state = %s, found = %s, finished_at = %s, failure = %s"
        " WHERE checksum_job_id = %s",
        ("done" if done else "failed", found, now, None if done else f"job {status['status']}",
         j["checksum_job_id"]),
    )  # fmt: skip
    error = None
    if not done:
        await cur.execute(
            "SELECT count(*) AS n FROM catalog.checksum_job"
            " WHERE migration_id = %s AND state = 'failed'",
            (m["migration_id"],),
        )
        failed = int((await cur.fetchone())["n"])  # type: ignore[index]
        if failed < JOB_ATTEMPTS:
            # Its objects may be asked about again by the next job.
            await conn.execute(
                "DELETE FROM catalog.checksum_job_object WHERE checksum_job_id = %s",
                (j["checksum_job_id"],),
            )
        error = f"checksum job {j['job_id']} {status['status']} ({failed} of {JOB_ATTEMPTS})"
    elif j["objects"] > found:
        error = (f"{j['objects'] - found} objects have no CRC64NVME S3 can compute (SSE-C?);"
                 " their copies stay unverified")  # fmt: skip
    if error is not None:
        await conn.execute(
            "UPDATE catalog.migration_prefix SET last_error = %s, updated_at = %s"
            " WHERE migration_id = %s",
            (error, now, m["migration_id"]),
        )
    return "done"


__all__ = [
    "CHECKSUM_TAG",
    "checksum_progress",
    "job_name",
    "one_kind",
    "plan_checksums",
    "record_checksum_job",
]
