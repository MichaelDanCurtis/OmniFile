"""The portal's worker jobs (M4; docs/contracts/portal-api.md 5.4).

Queue ``portal``:
- ``{"kind": "download", "download_id"}``: mint a 10-minute read URL for one
  blob version (a user-delegation SAS on Azure), on the private endpoint, and
  record it on the `download_request`. `web` has no storage role, so it
  can't mint one itself.
- ``{"kind": "annex_placement", "placement_id"}``: a recall of an annexed
  file into an OmniFile share (`share_placements.py`, annex phase A3.2).
- ``{"kind": "version_restore", "restore_id"}``: restore an old version,
  server side (`copy_object`), as a new current version of the file
  (``target = overwrite``) or as a new file next to it (``restored``,
  ``restored_timestamped``; decision 34). Never a destructive rollback, so
  WORM holds. An archived version is recalled first (started here if
  nobody started it). The edge needs no new code: a content-version change
  from the feed goes through the narrowing handshake.

Sweep ``portal_sweep`` (hourly): expired sessions, sign-ins and download URLs;
old finished downloads and restores.

VERIFY on real Azure: the SAS carries no `rscd` (Content-Disposition) yet,
because `StorageBackend.edge_credential` has no parameter for it; the
browser names the file from the URL.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row

from ..backends.base import BackendError, EdgeAccess, NotRestored, ObjectRef, Throttled
from ..retention.common import data_container
from .jobs import Job, QueueJob, SweepJob
from .queue import Message, Permanent, Retry, outside, send

if TYPE_CHECKING:
    from .backends import BackendRegistry
    from .runner import Worker

log = logging.getLogger(__name__)

QUEUE = "portal"
ACTOR = "system:portal_jobs"
DOWNLOAD_TTL = timedelta(minutes=10)
WAIT_RECALL = 60.0
#: A server-side copy has no time promise, and its read-back reads the whole file.
COPY_HIDE_SECONDS = 6 * 3600
SWEEP_EVERY = timedelta(hours=1)

#: The readable copy of a version: the primary location, or the restored
#: copy of an archived one while its restore is live.
SOURCE_SQL = """
SELECT v.version_id, v.file_id, v.kind, v.state AS version_state, v.size, v.mtime_ns,
       l.version_id AS content_id,
       v.blake3, v.crc64nvme, v.uid, v.gid, v.mode,
       l.location_id, l.backend_id, l.container, l.object_key, l.backend_version_id,
       l.logical_tier,
       r.state AS restore_state, r.expires_at AS restore_expires_at,
       t.location_id AS t_location_id, t.container AS t_container,
       t.object_key AS t_object_key, t.backend_version_id AS t_backend_version_id
  FROM catalog.file_version v
  JOIN catalog.location l
    ON l.version_id = CASE WHEN v.kind = 'content' THEN v.version_id ELSE v.content_version_id END
   AND l.role = 'primary' AND l.state = 'committed'
  LEFT JOIN LATERAL (
        SELECT * FROM catalog.restore
         WHERE location_id = l.location_id AND state <> 'cancelled'
         ORDER BY requested_at DESC, restore_id DESC LIMIT 1) r ON true
  LEFT JOIN catalog.location t ON t.location_id = r.target_location_id AND t.state = 'committed'
 WHERE v.version_id = %s
"""


def readable_ref(src: dict[str, Any], now: datetime) -> ObjectRef | None:
    """Where the bytes can be read now, or None (archived, no live restore)."""
    if src["logical_tier"] != "archive":
        return ObjectRef(src["backend_id"], src["container"], src["object_key"],
                         src["backend_version_id"])  # fmt: skip
    live = src["restore_state"] == "completed" and (
        src["restore_expires_at"] is None or src["restore_expires_at"] > now
    )
    if not live:
        return None
    if src["t_location_id"] is not None:
        return ObjectRef(src["backend_id"], src["t_container"], src["t_object_key"],
                         src["t_backend_version_id"])  # fmt: skip
    return ObjectRef(src["backend_id"], src["container"], src["object_key"],
                     src["backend_version_id"])  # fmt: skip


async def _one(cur, sql: str, params: Any) -> dict[str, Any] | None:
    await cur.execute(sql, params)
    return await cur.fetchone()


async def _set_actor(conn: psycopg.AsyncConnection) -> None:
    await conn.execute("SELECT set_config('omnifile.actor', %s, true)", (ACTOR,))


# --- downloads ---------------------------------------------------------------------------


async def handle_download(
    conn: psycopg.AsyncConnection,
    registry: BackendRegistry,
    download_id: int,
    now: datetime,
    msg: Message | None = None,
) -> str:
    """Returns the download's new state."""
    cur = conn.cursor(row_factory=dict_row)
    d = await _one(cur, "SELECT * FROM catalog.download_request WHERE download_id = %s"
                        " FOR UPDATE", (download_id,))  # fmt: skip
    if d is None or d["state"] != "preparing":
        return "gone" if d is None else d["state"]
    src = await _one(cur, SOURCE_SQL, (d["version_id"],))
    if src is None:
        state, url, expires, failure = "failed", None, None, "This version's bytes are gone."
    elif (ref := readable_ref(src, now)) is None:
        state, url, expires, failure = "needs_recall", None, None, None
    else:
        try:
            cred = await outside(
                msg,
                "credential",
                lambda: registry.get(ref.backend_id).edge_credential(
                    ref, EdgeAccess.READ, DOWNLOAD_TTL
                ),
            )
        except Throttled as e:
            raise Retry(e.retry_after.total_seconds() if e.retry_after else 10.0) from e
        except BackendError as e:
            log.warning("download %s: %s", download_id, e)
            cred = None
        if cred is None or cred.url is None:
            state, url, expires, failure = (
                "failed", None, None, "A download link could not be made for this file.",
            )  # fmt: skip
        else:
            state, url, expires, failure = "ready", cred.url, cred.expires_at, None
    await conn.execute(
        "UPDATE catalog.download_request SET state = %s, url = %s, expires_at = %s,"
        " failure = %s, updated_at = %s WHERE download_id = %s",
        (state, url, expires, failure, now, download_id),
    )
    return state


# --- version restores --------------------------------------------------------------------
#
# Order matters (the consumer commits the handler's writes when it raises
# `Retry`, worker/queue.py): nothing about the new version is written until
# the bytes are in place.
#   1. the source must be readable (else start or join its recall, and wait);
#   2. the new name, if any, must be free;
#   3. copy the bytes to the restore's own key (`<prefix>restores/<restore_id>`)
#      and record that copy on the restore (state `copying`), so a retry
#      resumes there instead of copying again;
#   4. HEAD the copy, then write the version (and the file, for `restored*`),
#      its location, and `done`, in one go.
# The copy and the HEAD run `outside` the transaction: the handler lets its row
# locks go first, and comes back through every check above with them taken again.


async def _fail(conn, restore_id: int, why: str, now: datetime) -> str:
    await conn.execute(
        "UPDATE catalog.version_restore SET state = 'failed', failure = %s, updated_at = %s"
        " WHERE restore_id = %s",
        (why, now, restore_id),
    )
    return "failed"


async def _start_recall(cur, conn, r: dict[str, Any], src: dict[str, Any], now: datetime):
    """Start or join the recall of the version's bytes (one active per
    content version), as the restore's requester. Returns the recall id.

    The restore wants this version, whatever the file holds by then. While it
    waits, the catalog counts it on the recall (`held_by_jobs`, migration
    0059), so the recall is not ended if the file is replaced or deleted. That
    matters for a restore of the file's current version under another name.
    A recall that is joined is locked: a commit that would end it waits for
    this transaction, and then sees the restore."""
    content_id = src["content_id"]
    row = await _one(
        cur,
        "INSERT INTO catalog.recall (file_id, version_id, edge_id, priority, requested_at,"
        " updated_at) SELECT %s, %s, d.edge_id, 'standard', %s, %s FROM catalog.dataset d"
        " WHERE d.dataset_id = %s"
        " ON CONFLICT (file_id, version_id)"
        "   WHERE state IN ('requested', 'rehydrating', 'copying', 'downloading')"
        " DO NOTHING RETURNING recall_id",
        (r["file_id"], content_id, now, now, r["dataset_id"]),
    )
    if row is None:
        row = await _one(
            cur,
            "SELECT recall_id FROM catalog.recall WHERE file_id = %s AND version_id = %s"
            " AND state IN ('requested', 'rehydrating', 'copying', 'downloading') FOR UPDATE",
            (r["file_id"], content_id),
        )
        if row is None:
            raise Retry(5.0)  # finished in between: look again
    else:
        await conn.execute(
            "SELECT pgmq.send('restores', %s::jsonb)",
            (json.dumps({"recall_id": str(row["recall_id"])}),),
        )
    await conn.execute(
        "INSERT INTO catalog.recall_requester (recall_id, identity_id, via, proof, requested_at)"
        " VALUES (%s, %s, 'portal', 'proven', %s) ON CONFLICT DO NOTHING",
        (row["recall_id"], r["requested_by_identity_id"], now),
    )
    return row["recall_id"]


async def _name_taken(cur, parent_id: int, name: str) -> bool:
    row = await _one(
        cur,
        "SELECT 1 AS x FROM catalog.file WHERE parent_id = %s AND name = %s AND deleted_at IS NULL",
        (parent_id, name),
    )
    return row is not None


async def handle_restore(
    conn: psycopg.AsyncConnection,
    registry: BackendRegistry,
    restore_id: int,
    now: datetime,
    msg: Message | None = None,
) -> str:
    """Returns the restore's new state. Raises `Retry` while it waits."""
    cur = conn.cursor(row_factory=dict_row)
    r = await _one(cur, "SELECT * FROM catalog.version_restore WHERE restore_id = %s FOR UPDATE",
                   (restore_id,))  # fmt: skip
    if r is None or r["state"] in ("done", "failed"):
        return "gone" if r is None else r["state"]
    await _set_actor(conn)
    src = await _one(cur, SOURCE_SQL, (r["version_id"],))
    if src is None or src["version_state"] != "committed" or src["kind"] == "tombstone":
        return await _fail(conn, restore_id, "This version can no longer be restored.", now)
    f = await _one(cur, "SELECT * FROM catalog.file WHERE file_id = %s FOR UPDATE",
                   (r["file_id"],))  # fmt: skip
    ds = await _one(
        cur,
        "SELECT d.*, b.config AS backend_config FROM catalog.dataset d"
        " JOIN catalog.backend b ON b.backend_id = d.backend_id WHERE d.dataset_id = %s",
        (r["dataset_id"],),
    )
    assert f is not None and ds is not None
    if r["target"] == "overwrite" and f["deleted_at"] is not None:
        return await _fail(conn, restore_id, "The file was deleted; restore it as a new file.",
                           now)  # fmt: skip
    if r["target"] != "overwrite":
        parent = await _one(cur, "SELECT kind, deleted_at FROM catalog.file WHERE file_id = %s",
                            (f["parent_id"],))  # fmt: skip
        if parent is None or parent["kind"] != "dir" or parent["deleted_at"] is not None:
            return await _fail(conn, restore_id, "The folder it was in no longer exists.", now)
        if await _name_taken(cur, f["parent_id"], r["name"]):
            return await _fail(conn, restore_id, f"name_taken: {r['name']!r} already exists.",
                               now)  # fmt: skip

    if r["copy_key"] is None:
        ref = readable_ref(src, now)
        if ref is None:
            recall_id = r["recall_id"]
            if recall_id is not None:
                rc = await _one(cur, "SELECT state FROM catalog.recall WHERE recall_id = %s",
                                (recall_id,))  # fmt: skip
                if rc is not None and rc["state"] in ("failed", "cancelled"):
                    return await _fail(conn, restore_id, "The recall of this version failed.",
                                       now)  # fmt: skip
                # `superseded`: ended because the file was replaced or deleted. A
                # restore holds its recall, so it should not get here (migration
                # 0059); if it has, the version is still wanted: recall it again,
                # rather than wait on a recall that will never bring it.
                if rc is not None and rc["state"] in ("available", "superseded"):
                    recall_id = None  # its copy expired since, or it was ended: recall again
            if recall_id is None:
                recall_id = await _start_recall(cur, conn, r, src, now)
            await conn.execute(
                "UPDATE catalog.version_restore SET state = 'waiting_recall', recall_id = %s,"
                " updated_at = %s WHERE restore_id = %s",
                (recall_id, now, restore_id),
            )
            raise Retry(WAIT_RECALL)
        backend = registry.get(ds["backend_id"])
        container = data_container(ds["backend_config"] or {})
        try:
            new = await outside(
                msg,
                "copy",
                lambda: backend.copy_object(
                    ref, container, f"{ds['key_prefix']}restores/{restore_id}"
                ),
                hide_for=COPY_HIDE_SECONDS,
            )
        except Throttled as e:
            raise Retry(e.retry_after.total_seconds() if e.retry_after else 30.0) from e
        except NotRestored:
            raise Retry(WAIT_RECALL) from None
        await conn.execute(
            "UPDATE catalog.version_restore SET state = 'copying', copy_container = %s,"
            " copy_key = %s, copy_backend_version_id = %s, updated_at = %s WHERE restore_id = %s",
            (new.container, new.key, new.version_id, now, restore_id),
        )
    else:
        backend = registry.get(ds["backend_id"])
        new = ObjectRef(ds["backend_id"], r["copy_container"], r["copy_key"],
                        r["copy_backend_version_id"])  # fmt: skip
    try:
        head = await outside(msg, "head", lambda: backend.head(new))
    except Throttled as e:
        raise Retry(e.retry_after.total_seconds() if e.retry_after else 30.0) from e

    # The bytes are in place: write the version, in one go.
    size = int(src["size"] or 0)
    if r["target"] == "overwrite":
        target_file = f
    else:
        target_file = await _one(
            cur,
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
            " resident_bytes, mtime_ns, uid, gid, mode, owner_sid, acl_id, origin, written_at)"
            " VALUES (%s, %s, %s, 'file', %s, %s, 0, %s, %s, %s, %s, %s, %s, 'service', %s)"
            " ON CONFLICT (parent_id, name) WHERE deleted_at IS NULL DO NOTHING RETURNING *",
            (f["dataset_id"], f["parent_id"], r["name"], "stub" if size > 0 else "resident",
             size, src["mtime_ns"], src["uid"], src["gid"], src["mode"], f["owner_sid"],
             f["acl_id"], now),
        )  # fmt: skip
        if target_file is None:
            return await _fail(conn, restore_id, f"name_taken: {r['name']!r} already exists.",
                               now)  # fmt: skip
    tid = target_file["file_id"]
    v = await _one(
        cur,
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
        " size, mtime_ns, blake3, crc64nvme, uid, gid, mode, acl_id)"
        " VALUES (%s, %s, %s, %s, 'content', 'pending', %s, %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING version_id",
        (tid, target_file["dataset_id"], target_file["parent_id"], target_file["name"],
         src["size"], src["mtime_ns"], src["blake3"], src["crc64nvme"], src["uid"], src["gid"],
         src["mode"], target_file["acl_id"]),
    )  # fmt: skip
    assert v is not None
    vid = v["version_id"]
    loc = await _one(
        cur,
        "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
        " backend_version_id, role, state, logical_tier, storage_class, size, crc64nvme,"
        " committed_at, tier_changed_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'primary', 'committed', %s, %s, %s, %s, %s, %s)"
        " RETURNING location_id",
        (tid, vid, ds["backend_id"], new.container, new.key, new.version_id,
         str(head.logical_tier), head.native_class, src["size"], src["crc64nvme"], now, now),
    )  # fmt: skip
    assert loc is not None
    await conn.execute(
        "UPDATE catalog.file_version SET state = 'committed', committed_at = %s"
        " WHERE version_id = %s",
        (now, vid),
    )
    await conn.execute(
        "UPDATE catalog.file SET size = %s, mtime_ns = %s, blake3 = %s, crc64nvme = %s,"
        " state = %s, resident_bytes = 0, stub_version_id = NULL, stub_resident_bytes = NULL,"
        " written_at = %s WHERE file_id = %s",
        (size, src["mtime_ns"], src["blake3"], src["crc64nvme"],
         "stub" if size > 0 else "resident", now, tid),
    )  # fmt: skip
    if src["crc64nvme"] is not None:
        await conn.execute(
            "SELECT pgmq.send('verify', %s::jsonb)",
            (json.dumps({"location_id": str(loc["location_id"])}),),
        )
    await conn.execute(
        "UPDATE catalog.version_restore SET state = 'done', result_file_id = %s,"
        " result_version_id = %s, updated_at = %s WHERE restore_id = %s",
        (tid, vid, now, restore_id),
    )
    # The recall that brought the old version's bytes back has done its job.
    # No edge fills a version for a restore, so it would otherwise stay
    # `downloading` for good (#55 review). One of the file's current content,
    # which an edge is filling, is left to the edge. A deleted file keeps its
    # content version, but no edge fills it: left alone, its recall would be
    # failed by the idle rule an hour later, with a failure card (#96 review).
    # In whatever active state the recall is: the restore reads the bytes as
    # soon as the change feed has committed the recall's copy, which can be
    # before the recall's own poll has marked it `downloading` (M3 step 13c
    # found the recall marked `downloading` 0.7 s after the restore was done,
    # and left so). The provider's restore under it is closed by the change
    # feed, as for any recall that has ended.
    # From here on the restore no longer holds the recall, so a later replace
    # or delete of the file ends it as any other.
    # A pinned recall is a rewind reader's: an edge fills its label, so it is
    # left to the edge. Closed here before the edge had `downloading` and its
    # credential, the label was never filled and the reader had to ask again.
    if r["recall_id"] is not None:
        closed = await _one(
            cur,
            "UPDATE catalog.recall rc SET state = 'available', completed_at = %s, updated_at = %s"
            " FROM catalog.file f WHERE rc.recall_id = %s"
            " AND rc.state IN ('requested', 'rehydrating', 'copying', 'downloading')"
            " AND NOT rc.pinned"
            " AND f.file_id = rc.file_id"
            " AND (f.content_version_id IS DISTINCT FROM rc.version_id"
            "      OR f.deleted_at IS NOT NULL)"
            " RETURNING rc.recall_id",
            (now, now, r["recall_id"]),
        )
        if closed is not None:
            await conn.execute(
                "SELECT pgmq.send('notify', %s::jsonb)",
                (json.dumps({"recall_id": str(closed["recall_id"])}),),
            )
    await conn.execute(
        "SELECT catalog.audit('version.restore.done', %s, NULL, %s, %s, %s::jsonb)",
        (r["dataset_id"], tid, vid,
         json.dumps({"restore_id": str(restore_id), "from_version": str(r["version_id"]),
                     "target": r["target"], "name": target_file["name"],
                     "origin": "service"})),
    )  # fmt: skip
    return "done"


# --- sweep -----------------------------------------------------------------------------------


async def sweep(conn: psycopg.AsyncConnection, now: datetime) -> int:
    done = 0
    # Sessions and pending sign-ins belong to `web`, which stamps them on the wall clock
    # (it has no dev virtual clock). Judge them on the wall clock too: a worker running
    # days ahead on the dev clock would otherwise sign everyone out.
    wall = datetime.now(UTC)
    async with conn.transaction():
        for sql, params in (
            ("DELETE FROM catalog.web_session WHERE expires_at <= %s OR idle_expires_at <= %s",
             (wall, wall)),
            ("DELETE FROM catalog.oidc_login WHERE expires_at <= %s", (wall,)),
            ("UPDATE catalog.download_request SET state = 'expired', url = NULL, updated_at = %s"
             " WHERE url IS NOT NULL AND expires_at <= %s", (now, now)),
            ("DELETE FROM catalog.download_request WHERE state IN ('expired', 'failed',"
             " 'needs_recall') AND updated_at < %s", (now - timedelta(days=1),)),
            ("DELETE FROM catalog.version_restore WHERE state IN ('done', 'failed')"
             " AND updated_at < %s", (now - timedelta(days=30),)),
        ):  # fmt: skip
            cur = await conn.execute(sql, params)
            done += cur.rowcount
    return done


class DictRows:
    """`conn`, whose `execute()` returns dict rows: each call runs on a new
    `conn.cursor(row_factory=dict_row)`, as `conn.execute()` runs on a new cursor. Everything
    else is the connection's."""

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn

    async def execute(self, query: Any, params: Any = None, **kwargs: Any) -> psycopg.AsyncCursor:
        return await self._conn.cursor(row_factory=dict_row).execute(query, params, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


async def handle_recall_batch(conn: psycopg.AsyncConnection, batch_id: int, now: datetime) -> bool:
    """One step of a partner's recall batch (portal-api.md 5.19): plan a
    chunk of files, or start a chunk of recalls, then queue the next step.
    Returns True while there is more to do."""
    from ..web import recall_batches  # the planning rules live with the route

    await _set_actor(conn)
    # Dict rows through cursors of their own, never by changing the connection's row_factory: a
    # pooled connection (worker/pool.py) is shared, and changing it there would outlast this step.
    more = await recall_batches.step(DictRows(conn), batch_id, now)
    if more:
        await send(conn, QUEUE, {"kind": "recall_batch", "batch_id": str(batch_id)})
    return more


def portal_jobs(worker: Worker) -> list[Job]:
    async def handle(msg: Message, conn: psycopg.AsyncConnection) -> None:
        kind = msg.body.get("kind")
        try:
            if kind == "download":
                await handle_download(
                    conn, worker.registry, int(msg.body["download_id"]), worker.clock.now(), msg
                )
            elif kind == "version_restore":
                await handle_restore(
                    conn, worker.registry, int(msg.body["restore_id"]), worker.clock.now(), msg
                )
            elif kind == "recall_batch":
                await handle_recall_batch(conn, int(msg.body["batch_id"]), worker.clock.now())
            elif kind == "annex_placement":
                from .share_placements import handle_share_placement

                await handle_share_placement(
                    conn, worker.registry, int(msg.body["placement_id"]), worker.clock.now(), msg
                )
            else:
                raise Permanent(f"unknown portal job {kind!r}")
        except (KeyError, TypeError, ValueError) as e:
            raise Permanent(f"malformed portal message {dict(msg.body)!r}: {e}") from None

    return [QueueJob(QUEUE, handle), SweepJob("portal_sweep", SWEEP_EVERY, sweep)]
