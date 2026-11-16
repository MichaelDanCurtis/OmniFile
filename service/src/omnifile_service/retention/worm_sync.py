"""Backend side of versioning and WORM: what the `retention` queue and the
retention sweeps do with the provider.

- `sync_share_policy`: bring the share's container policy in line with the
  catalog (unlocked during the validation window, locked after, extended
  when retention grows, removed if the admin cancelled in the window).
- `stamp_worm`: give each committed copy of a retained or held version its
  per-version retention (locked once the share is), its legal hold, and the
  state index tags (`omnifile_worm`, `omnifile_hold`,
  `omnifile_retain_until`).
- `retire_replaced_secondaries`: what a split leaves behind, once nothing protects it.
- `delete_blob`: the lifecycle-equivalent delete of a pruned version's
  bytes. A blob shared with a live location (whole-file dedupe) is kept.
  The original of an archive move is deleted only after the copy that
  replaces it passes the archive-copy check (`_replacement_refuses`). Deleters
  of a blob take turns on its advisory lock, which a dedupe in flight holds
  shared: a row lock would take a transaction id, and one held across the delete
  stops the edge feed for every edge.
- `expire_recall_copies`: a WORM recall copies the archived version into the
  non-WORM recall container; the copy is deleted after the user's window.
  The archived original is never touched (decision 31).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ..backends.base import (
    BackendError,
    Immutable,
    ObjectNotFound,
    ObjectRef,
    StorageBackend,
    UnsupportedOperation,
)
from ..backends.copy_check import copy_mismatch
from ..catalog.blob_lock import blob_lock_key
from ..catalog.source_readonly import location_on_source, on_source
from ..worker.backends import BackendRegistry
from ..worker.queue import Defer, Permanent, Retry, send
from .common import TAG_HOLD, TAG_RETAIN, TAG_STATE, TAG_WORM, data_container, set_actor
from .policy_admin import ContainerPolicyAdmin, policy_admin_for

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StampResult:
    stamped: int = 0
    unsupported: int = 0
    failed: int = 0


def _ref(backend_id: int, container: str, key: str, version: str | None) -> ObjectRef:
    return ObjectRef(backend_id, container, key, version)


async def _merge_tags(
    backend: StorageBackend, ref: ObjectRef, tags: dict[str, str],
    *, unless_set: dict[str, str] | None = None,
) -> None:  # fmt: skip
    """`unless_set` only fills a gap: a row with no retention says nothing about a blob
    another row's retention is on. A later retain-until tag stands, as the blob's date does."""
    current = dict(await backend.get_tags(ref))
    merged = {**(unless_set or {}), **current, **tags}
    if TAG_RETAIN in tags and current.get(TAG_RETAIN, "") > tags[TAG_RETAIN]:
        merged[TAG_RETAIN] = current[TAG_RETAIN]
    if merged != current:
        await backend.set_tags(ref, merged)


async def _another_hold(conn: psycopg.AsyncConnection, lid: int, ref: ObjectRef) -> bool:
    """Another file on this blob is on legal hold."""
    cur = await conn.execute(
        """
        SELECT 1 FROM catalog.location o
          JOIN catalog.file_version v ON v.version_id = o.version_id
         WHERE o.backend_id = %s AND o.container = %s AND o.object_key = %s
           AND o.backend_version_id IS NOT DISTINCT FROM %s
           AND o.location_id <> %s AND o.state = 'committed' AND v.legal_hold
         LIMIT 1
        """,
        (ref.backend_id, ref.container, ref.key, ref.version_id, lid),
    )
    return await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Container policy
# ---------------------------------------------------------------------------


#: How soon a share's policy sync looks again when another is running.
POLICY_BUSY_SECONDS = 30.0
#: How soon a blob's delete looks again when a move or a dedupe of it is running.
BLOB_BUSY_SECONDS = 30.0


async def sync_share_policy(
    conn: psycopg.AsyncConnection,
    registry: BackendRegistry,
    share_id: int,
    now: datetime,
    *,
    admin_for=policy_admin_for,
) -> str:
    """Make the backend container policy match the share. Returns the
    policy state now recorded.

    The container policy covers the whole container, so it is only set for
    a share that exports the whole dataset. A share on a sub-folder relies
    on per-version retention alone (stamped by `stamp_worm`).
    """
    async with conn.transaction():
        # Syncers of one share take turns without a row lock, which would take a
        # transaction id and hold it across the provider call.
        cur = await conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext(%s))", (f"omnifile:worm-policy:{share_id}",)
        )
        (mine,) = await cur.fetchone()  # type: ignore[misc]
        if not mine:
            raise Retry(POLICY_BUSY_SECONDS)
        cur = await conn.execute(
            """
            SELECT s.worm_enabled, s.worm_mode, s.worm_retention_days, s.worm_policy_state,
                   s.worm_policy_days, s.root_dir_id, d.backend_id, b.config,
                   (s.root_dir_id IS NULL OR s.root_dir_id = r.file_id) AS whole
              FROM catalog.share s
              JOIN catalog.dataset d ON d.dataset_id = s.dataset_id
              JOIN catalog.backend b ON b.backend_id = d.backend_id
              JOIN catalog.file r ON r.dataset_id = d.dataset_id AND r.parent_id IS NULL
             WHERE s.share_id = %s
            """,
            (share_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise Permanent(f"no share {share_id}")
        (enabled, mode, days, state, pdays, _, backend_id, config, whole) = row
        want = "none" if not enabled else ("locked" if mode == "locked" else "unlocked")
        want_days = days if enabled else None
        if (state, pdays) == (want, want_days):
            return state
        if whole:
            container = data_container(config or {})
            try:
                admin: ContainerPolicyAdmin = admin_for(registry.get(backend_id), config or {})
                if want == "none":
                    if state == "unlocked":
                        await admin.delete_unlocked(container)
                elif want == "unlocked":
                    await admin.set_unlocked(container, days)
                elif state == "locked":
                    await admin.extend(container, days)
                else:
                    await admin.lock(container, days)
            except UnsupportedOperation as e:
                # No management settings for the binding (or no such policy
                # on it): retry hourly; per-version retention still protects
                # every version.
                log.warning("share %s: container policy not applied: %s", share_id, e)
                raise Retry(3600) from e
            except Immutable as e:
                # The provider's locked policy says more than the catalog (it
                # only ever grows): a person looks; the versions stay retained.
                log.error("share %s: container policy disagrees with the catalog: %s",
                          share_id, e)  # fmt: skip
                raise Retry(3600) from e
            except BackendError as e:
                log.warning("share %s: container policy not applied yet: %s", share_id, e)
                raise Retry(300) from e
        # The share may have changed during the call: this records only what was done.
        cur = await conn.execute(
            "SELECT worm_enabled, worm_mode, worm_retention_days FROM catalog.share"
            " WHERE share_id = %s FOR UPDATE",
            (share_id,),
        )
        if await cur.fetchone() != (enabled, mode, days):
            raise Retry(0)
        await conn.execute(
            "UPDATE catalog.share SET worm_policy_state = %s, worm_policy_days = %s,"
            " worm_policy_synced_at = %s WHERE share_id = %s",
            (want, want_days, now, share_id),
        )
        return want


async def stale_policy_shares(conn: psycopg.AsyncConnection) -> list[int]:
    cur = await conn.execute(
        """
        SELECT share_id FROM catalog.share
         WHERE (worm_enabled AND (worm_policy_state <> CASE worm_mode WHEN 'locked' THEN 'locked'
                                                               ELSE 'unlocked' END
                                  OR worm_policy_days IS DISTINCT FROM worm_retention_days))
            OR (NOT worm_enabled AND worm_policy_state <> 'none')
         ORDER BY share_id
        """
    )
    return [r[0] for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Per-version retention, legal hold and tags
# ---------------------------------------------------------------------------

_TO_STAMP = f"""
SELECT l.location_id, l.backend_id, l.container, l.object_key, l.backend_version_id,
       l.worm_stamped_until, l.worm_stamped_locked, l.worm_stamped_hold,
       v.retention_until, v.legal_hold, catalog.worm_locked(v.dataset_id, v.parent_id),
       {on_source("l")}
  FROM catalog.location l
  JOIN catalog.file_version v ON v.version_id = l.version_id
 WHERE l.worm_sync_needed
   AND (%s::bigint[] IS NULL OR l.location_id = ANY(%s::bigint[]))
 ORDER BY l.location_id
 LIMIT %s
"""


async def _source_only(conn: psycopg.AsyncConnection, lid: int, now: datetime) -> None:
    """A copy in a migration source: the catalog alone records its
    retention and hold (the source is read-only to OmniFile, so nothing is
    stamped on the object), and the flag is cleared."""
    async with conn.transaction():
        await conn.execute(
            "UPDATE catalog.location SET worm_sync_needed = false, worm_synced_at = %s"
            " WHERE location_id = %s",
            (now, lid),
        )


async def stamp_worm(
    conn: psycopg.AsyncConnection, registry: BackendRegistry, now: datetime, *, limit: int = 500,
    only: list[int] | None = None,
) -> StampResult:  # fmt: skip
    """Stamp one batch of locations whose WORM state the backend lacks, or just
    `only`. Makes its provider calls with no transaction of its own open.

    A blob can have several references, so **a row's dates never take the blob's back.**
    A retention that has ended is never stamped: it can't protect anything and could
    shorten another file's, or fail for ever on a locked blob, so the flag just clears. A
    date earlier than the blob already carries leaves the blob's. A hold cleared on one
    file stays while another file on the blob is held, and no row marks such a blob as
    unheld. If a hold is set in the catalog just after the check, its own row's flag is
    already up, and that row's stamp follows under this same lock."""
    cur = await conn.execute(_TO_STAMP, (only, only, limit))
    rows = await cur.fetchall()
    stamped = unsupported = failed = 0
    for row in rows:
        lid, bid, container, key = row[:4]
        if row[-1]:  # in a migration source
            await _source_only(conn, lid, now)
            unsupported += 1
            continue
        try:
            backend = registry.get(bid)
        except KeyError:
            log.warning("location %s: no binding for backend %s", lid, bid)
            failed += 1
            continue
        # Stamps of one blob take turns, so a check of its other rows still holds when the
        # provider hears of it. A session lock takes no id, and no transaction stays open
        # across the calls. A move or dedupe of the blob holds the same lock: then the
        # flag stays, and the next sweep gets it.
        lock = blob_lock_key(bid, container, key)
        got = await conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (lock,))
        (mine,) = await got.fetchone()  # type: ignore[misc]
        if not mine:
            continue
        try:
            outcome = await _stamp_one(conn, backend, row, now)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock,))
        if outcome == "stamped":
            stamped += 1
        elif outcome == "unsupported":
            unsupported += 1
        elif outcome != "nothing":
            failed += 1
    return StampResult(stamped, unsupported, failed)


async def _stamp_one(
    conn: psycopg.AsyncConnection, backend: StorageBackend, row: tuple, now: datetime
) -> str:
    """One location's retention, hold and tags, under its blob's lock."""
    (lid, bid, container, key, bver, s_until, s_locked, s_hold, until, hold, locked, _) = row
    ref = _ref(bid, container, key, bver)
    new_until, new_locked, new_hold = s_until, s_locked, s_hold
    outcome = "stamped"
    want_locked = bool(locked) or bool(s_locked)
    expired = until is not None and until <= now
    try:
        if (
            until is not None
            and not expired
            and (s_until is None or until > s_until or (want_locked and not s_locked))
        ):
            try:
                blob_until = (await backend.head(ref)).retention_until
                keep = until if blob_until is None else max(until, blob_until)
                if keep != blob_until or want_locked:
                    await backend.set_retention(ref, keep, locked=want_locked)
                new_until, new_locked = until, want_locked
            except UnsupportedOperation:
                outcome = "unsupported"  # no WORM here: the catalog alone guards it
        shared_hold = not hold and await _another_hold(conn, lid, ref)
        if outcome == "stamped" and bool(hold) != bool(s_hold):
            try:
                if not shared_hold:
                    await backend.set_legal_hold(ref, bool(hold))
                new_hold = bool(hold)
            except UnsupportedOperation:
                outcome = "unsupported"
        tags = {TAG_HOLD: "1" if hold or shared_hold else "0"}
        if until is not None and not expired:
            tags[TAG_WORM] = "locked" if want_locked else "unlocked"
            tags[TAG_RETAIN] = until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        await _merge_tags(backend, ref, tags, unless_set={TAG_WORM: "none"})
        if expired and outcome == "stamped":
            outcome = "nothing"
    except ObjectNotFound:
        # Reconcile reports the missing blob; retrying here can't help.
        log.warning("location %s: %s/%s is missing", lid, container, key)
        outcome = "missing"
    except Exception as e:  # noqa: BLE001 - one bad blob must not stop the batch
        log.warning("location %s: WORM stamp failed: %s", lid, e)
        return "failed"
    async with conn.transaction():
        # Clear the flag only if the version still wants what was stamped;
        # a retention extended meanwhile keeps it raised.
        await conn.execute(
            """
            UPDATE catalog.location l
               SET worm_stamped_until = %(until)s, worm_stamped_locked = %(locked)s,
                   worm_stamped_hold = %(hold)s, worm_synced_at = %(now)s,
                   worm_sync_needed = NOT EXISTS (
                       SELECT 1 FROM catalog.file_version v
                        WHERE v.version_id = l.version_id
                          AND v.retention_until IS NOT DISTINCT FROM %(want_until)s
                          AND v.legal_hold = %(want_hold)s)
             WHERE l.location_id = %(lid)s
            """,
            {"until": new_until, "locked": new_locked, "hold": new_hold, "now": now,
             "want_until": until, "want_hold": hold, "lid": lid},
        )  # fmt: skip
    return outcome


# ---------------------------------------------------------------------------
# Deleting pruned bytes
# ---------------------------------------------------------------------------


async def delete_blob(
    conn: psycopg.AsyncConnection, registry: BackendRegistry, location_id: int, now: datetime
) -> str:
    """Delete one `deleting` location's bytes and mark it `deleted`.

    Runs inside the consumer's transaction. Returns what happened:
    ``deleted``, ``shared`` (another live location uses the blob; only the
    row is retired), ``gone`` (already absent), ``retained_at_source`` (a
    migration source's object, never deleted: the row is `committed` again),
    ``refused_<why>`` (the primary copy of a live version whose replacement
    failed the archive-copy check: the row is `committed` again) or ``noop``.

    Nothing here writes or locks a row before the provider calls, so the
    transaction has no id across them. Only this function moves a `deleting` row
    back, and it does that under the blob's lock.
    """
    location = """
        SELECT backend_id, container, object_key, backend_version_id, state
          FROM catalog.location WHERE location_id = %s
    """
    cur = await conn.execute(location, (location_id,))
    row = await cur.fetchone()
    if row is None:
        raise Permanent(f"no location {location_id}")
    bid, container, key, bver, state = row
    if state != "deleting":
        return "noop"
    # A dedupe in flight holds this lock shared, so once this has it, the check for
    # other references below can't miss one.
    cur = await conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext(%s))", (blob_lock_key(bid, container, key),)
    )
    (mine,) = await cur.fetchone()  # type: ignore[misc]
    if not mine:
        raise Defer(BLOB_BUSY_SECONDS)
    cur = await conn.execute(location, (location_id,))
    row = await cur.fetchone()  # what the last holder left
    if row is None or row[4] != "deleting":
        return "noop"
    await set_actor(conn, "system:retention_delete")
    if await location_on_source(conn, location_id):
        # Queued before its bucket became a source (or by a bug): the
        # client's object is never deleted. The bytes are still there.
        log.error("location %s: refused to delete s3://%s/%s, a migration source",
                  location_id, container, key)  # fmt: skip
        await conn.execute(
            "UPDATE catalog.location SET state = 'committed' WHERE location_id = %s",
            (location_id,),
        )
        await conn.execute(
            "SELECT catalog.audit('location.delete.refused', f.dataset_id, NULL, l.file_id,"
            " l.version_id, %s) FROM catalog.location l JOIN catalog.file f USING (file_id)"
            " WHERE l.location_id = %s",
            (Jsonb({"location_id": location_id, "why": "on_source"}), location_id),
        )
        return "retained_at_source"
    refused = await _replacement_refuses(conn, registry, location_id, now)
    if refused is not None:
        return refused
    cur = await conn.execute(
        """
        SELECT 1 FROM catalog.location
         WHERE backend_id = %s AND container = %s AND object_key = %s
           AND (backend_version_id IS NOT DISTINCT FROM %s OR %s::text IS NULL
                OR backend_version_id IS NULL)
           AND location_id <> %s AND state IN ('pending', 'committed')
         LIMIT 1
        """,
        (bid, container, key, bver, bver, location_id),
    )
    if await cur.fetchone() is not None:
        outcome = "shared"
    else:
        backend = registry.get(bid)
        ref = _ref(bid, container, key, bver)
        outcome = "deleted"
        try:
            try:
                await _merge_tags(backend, ref, {TAG_STATE: "pruned"})
            except (UnsupportedOperation, Immutable):
                pass
            await backend.delete(ref)
            if bver is None:
                # Unversioned ref on a versioned container: the delete made a
                # tombstone; remove the versions too, so the bytes go.
                for v in await backend.list_versions(ref):
                    if v.ref.version_id is not None:
                        try:
                            await backend.delete(v.ref)
                        except ObjectNotFound:
                            pass
        except ObjectNotFound:
            outcome = "gone"
        except Immutable as e:
            # The provider still retains it (a hold or retention the catalog
            # didn't know): come back when it ends.
            delay = timedelta(days=1)
            try:
                head = await registry.get(bid).head(ref)
                if head.retention_until is not None and not head.legal_hold:
                    delay = max(timedelta(hours=1), head.retention_until - now)
            except Exception:  # noqa: BLE001
                pass
            log.warning("location %s still immutable at the provider: %s", location_id, e)
            raise Retry(delay.total_seconds()) from e
    await conn.execute(
        "UPDATE catalog.location SET state = 'deleted' WHERE location_id = %s"
        " AND state = 'deleting'",
        (location_id,),
    )
    return outcome


#: A live version's primary copy being replaced, and its replacement.
_REPLACED = """
SELECT l.role, l.backend_id, l.container, l.object_key, l.backend_version_id,
       v.state, v.kind, v.size, v.crc64nvme, f.dataset_id, l.file_id, l.version_id,
       r.location_id, r.backend_id, r.container, r.object_key, r.backend_version_id,
       r.size, r.crc64nvme, r.md5, r.verified
  FROM catalog.location l
  JOIN catalog.file_version v ON v.version_id = l.version_id
  JOIN catalog.file f ON f.file_id = l.file_id
  LEFT JOIN catalog.location r
         ON r.version_id = l.version_id AND r.role = 'primary' AND r.state = 'committed'
        AND r.location_id <> l.location_id
 WHERE l.location_id = %s
"""
#: How long the file waits before the scheduler tries its move again.
REPLACEMENT_RETRY = timedelta(days=1)


#: A version's primary now, and whether it was read back.
_PRIMARY_NOW = """
SELECT location_id, verified FROM catalog.location
 WHERE version_id = %s AND role = 'primary' AND state = 'committed'
"""


async def _good_bytes(conn: psycopg.AsyncConnection, location_id: int, file_id: int) -> bool:
    """A copy that was read back and never failed a check: neither its read-back
    (`tiering._verify_denied`) nor the archive-copy check (`_replacement_refuses`)."""
    cur = await conn.execute(
        "SELECT verified_at IS NOT NULL AND verified_by IS NOT NULL"
        "   AND NOT EXISTS (SELECT 1 FROM catalog.audit_log a WHERE a.file_id = %s"
        "                    AND ((a.action = 'location.verify' AND a.decision = 'deny'"
        "                          AND a.details->>'location_id' = %s)"
        "                      OR (a.action = 'location.delete.refused'"
        "                          AND a.details->>'replacement_location_id' = %s)))"
        "  FROM catalog.location WHERE location_id = %s",
        (file_id, str(location_id), str(location_id), location_id),
    )
    got = await cur.fetchone()
    return bool(got and got[0])


async def _secondary_refuses(
    conn: psycopg.AsyncConnection, registry: BackendRegistry, location_id: int, dataset_id: int,
    file_id: int, version_id: int, vsize: int | None, vcrc: bytes | None, primary: tuple,
) -> str | None:  # fmt: skip
    """A secondary may go only while its version's primary is there, with nothing in its
    properties that disagrees: an automatic delete must not take the last copy. Otherwise the
    row stays, committed, the refusal is audited, and a person looks. None to go on.

    What disagrees refuses: a primary that's missing, or whose size, CRC or MD5 differs. A
    primary never read back, with no CRC or MD5 in its properties (Azure's case), refuses
    only when this secondary is the good bytes: read back (`verified_at`, `verified_by`) and
    never denied. A copy that failed its own check never is, and must go
    (`test_a_refused_delete_ends_and_never_makes_the_bad_copy_primary`).

    The primary's blob is locked for the decision, as its swaps lock it, and the primary is
    read again under that lock after the provider call: if it changed meanwhile, this comes
    back later rather than decide on what it was."""
    rid, rbid, rcont, rkey, rver, rsize, rcrc, rmd5, rverified = primary
    why: str | None = "no_primary"
    if rid is not None:
        cur = await conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext(%s))", (blob_lock_key(rbid, rcont, rkey),)
        )
        (mine,) = await cur.fetchone()  # type: ignore[misc]
        if not mine:
            raise Defer(BLOB_BUSY_SECONDS)
        try:
            head = await registry.get(rbid).head(_ref(rbid, rcont, rkey, rver))
        except ObjectNotFound:
            head = None
        cur = await conn.execute(_PRIMARY_NOW, (version_id,))
        now_primary = await cur.fetchone()
        if now_primary is None or tuple(now_primary) != (rid, rverified):
            raise Defer(BLOB_BUSY_SECONDS)  # swapped while this looked: decide afresh
        if head is None:
            why = "primary_missing"
        else:
            bad = copy_mismatch(size=rsize if rsize is not None else vsize,
                                crc64nvme=rcrc if rcrc is not None else vcrc, md5=rmd5,
                                copy=head, read_back=True)  # fmt: skip
            why = f"primary_{bad}" if bad is not None else None
            if why is None and not rverified and await _good_bytes(conn, location_id, file_id):
                why = "primary_unverified"
    if why is None:
        return None
    log.error("location %s: refused to delete a secondary: %s", location_id, why)
    await conn.execute(
        "UPDATE catalog.location SET state = 'committed',"
        " verified = (verified_at IS NOT NULL AND verified_by IS NOT NULL"
        "             AND crc64nvme IS NOT NULL)"
        " WHERE location_id = %s AND state = 'deleting'",
        (location_id,),
    )
    await conn.execute(
        "SELECT catalog.audit('location.delete.refused', %s, NULL, %s, %s, %s)",
        (dataset_id, file_id, version_id, Jsonb({"location_id": location_id, "why": why})),
    )
    return f"refused_{why}"


async def _replacement_refuses(
    conn: psycopg.AsyncConnection, registry: BackendRegistry, location_id: int, now: datetime
) -> str | None:
    """The archive-copy integrity check (real-azure-checks.md, "Archive copy
    integrity"). Deleting the primary copy of a version that is still live
    is the last step of a copy-and-delete move (`worker/tiering.py`): the
    version's new primary is the copy, in the archive account and already in
    Archive, where it can't be read back. Before the original goes, the
    copy's properties are read from the provider and compared with the
    catalog by the one rule every such delete uses (`backends/copy_check`):
    its length, its CRC64-NVME where the provider keeps a whole-object one
    (S3; Azure's Get Blob Properties has none [V]), and its Content-MD5 where
    both sides have one. A copy whose properties carry no checksum must have
    passed the in-Azure CRC64 read-back before it went to Archive
    (`verified`).

    A mismatch, an unverifiable copy, or no copy at all blocks the delete:
    the original is the primary again, the copy is demoted to a secondary
    and dropped through this same queue (a secondary's delete is never a
    replacement's, so it goes without a check of its own), the refusal is
    audited and the file is looked at again later. Returns the outcome, or
    None to go on with the delete."""
    cur = await conn.execute(_REPLACED, (location_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    (role, bid, container, key, bver, vstate, vkind, vsize, vcrc, dataset_id, file_id, vid,
     rid, rbid, rcont, rkey, rver, rsize, rcrc, rmd5, rverified) = row  # fmt: skip
    if vstate != "committed" or vkind != "content" or role not in ("primary", "secondary"):
        return None  # a pruned version, a recall copy: nothing replaces them
    if role == "secondary":
        return await _secondary_refuses(
            conn, registry, location_id, dataset_id, file_id, vid, vsize, vcrc,
            (rid, rbid, rcont, rkey, rver, rsize, rcrc, rmd5, rverified),
        )  # fmt: skip
    original = _ref(bid, container, key, bver)
    try:
        await registry.get(bid).head(original)
    except ObjectNotFound:
        return None  # already gone: nothing left to protect
    why: str | None = None
    seen: dict[str, Any] = {}
    if rid is None:
        why = "no_replacement"
    else:
        try:
            head = await registry.get(rbid).head(_ref(rbid, rcont, rkey, rver))
        except ObjectNotFound:
            head = None
        want_size = rsize if rsize is not None else vsize
        want_crc = rcrc if rcrc is not None else vcrc
        if head is None:
            why = "replacement_missing"
        else:
            seen = {"size": head.size, "crc64nvme": head.crc64nvme.hex() if head.crc64nvme
                    else None, "md5": head.md5.hex() if head.md5 else None}  # fmt: skip
            bad = copy_mismatch(size=want_size, crc64nvme=want_crc, md5=rmd5, copy=head,
                                read_back=bool(rverified))  # fmt: skip
            why = f"replacement_{bad}" if bad is not None else None
    if why is None:
        return None
    log.error("location %s: refused to delete %s/%s: %s (copy %s, seen %s)",
              location_id, container, key, why, rid, seen)  # fmt: skip
    await conn.execute(
        # Demoted as well as dropped: the copy failed the check, so it is
        # never the record again, and its own delete (a secondary's) is not a
        # replacement's, so it can't refuse in turn and swap them back.
        "UPDATE catalog.location SET state = 'deleting', verified = false, role = 'secondary'"
        " WHERE location_id = %s AND state = 'committed'",
        (rid,),
    )
    await conn.execute(
        "UPDATE catalog.location SET state = 'committed',"
        " verified = (verified_at IS NOT NULL AND verified_by IS NOT NULL"
        "             AND crc64nvme IS NOT NULL)"
        " WHERE location_id = %s AND state = 'deleting'",
        (location_id,),
    )
    if rid is not None:
        await send(conn, "retention", {"op": "delete_blob", "location_id": str(rid)})
    await conn.execute(
        "SELECT catalog.audit('location.delete.refused', %s, NULL, %s, %s, %s)",
        (dataset_id, file_id, vid,
         Jsonb({"location_id": location_id, "why": why, "replacement_location_id": rid,
                "expected": {"size": rsize if rsize is not None else vsize,
                             "crc64nvme": (rcrc or vcrc).hex() if (rcrc or vcrc) else None,
                             "md5": rmd5.hex() if rmd5 else None},
                "observed": seen})),
    )  # fmt: skip
    await conn.execute(
        "UPDATE catalog.file SET next_action = coalesce(next_action, 'archive'),"
        " next_action_at = %s WHERE file_id = %s",
        (now + REPLACEMENT_RETRY, file_id),
    )
    return "refused_" + why


# ---------------------------------------------------------------------------
# Recall copies (non-WORM recall container)
# ---------------------------------------------------------------------------


async def expire_recall_copies(
    conn: psycopg.AsyncConnection,
    now: datetime,
    *,
    window: timedelta = timedelta(days=7),
    limit: int = 200,
) -> int:
    """Queue the delete of recall copies whose restore finished more than `window`
    ago and that no active recall still needs. Returns how many. The `retention`
    consumer deletes them (`delete_blob`): a row marked `deleting` here takes a
    transaction id, which must not wait on the provider."""
    cur = await conn.execute(
        """
        SELECT l.location_id
          FROM catalog.location l
          JOIN catalog.restore s ON s.target_location_id = l.location_id
         WHERE l.role = 'recall_copy' AND l.state = 'committed'
           AND s.state IN ('completed', 'expired') AND s.completed_at < %s
           AND NOT EXISTS (SELECT 1 FROM catalog.recall r
                            WHERE r.restore_id = s.restore_id
                              AND r.state IN ('requested', 'rehydrating', 'copying',
                                              'downloading'))
         ORDER BY l.location_id
         LIMIT %s
        """,
        (now - window, limit),
    )
    done = 0
    for (lid,) in await cur.fetchall():
        async with conn.transaction():
            await set_actor(conn, "system:recall_copy_expiry")
            r = await conn.execute(
                "UPDATE catalog.location SET state = 'deleting', verified = false"
                " WHERE location_id = %s AND state = 'committed'",
                (lid,),
            )
            if r.rowcount == 0:
                continue
            await send(conn, "retention", {"op": "delete_blob", "location_id": str(lid)})
            done += 1
    return done


# ---------------------------------------------------------------------------
# What a split leaves behind (decision 66)
# ---------------------------------------------------------------------------

#: Same container as the version's verified primary, under another name: a split's old
#: row, kept while its version was retained, or a copy whose message is gone. A copy in
#: flight still has its message. An archive copy is in another container, and a
#: migration's source or copy on another backend, so neither is here. A row gets its
#: grace from its tier clock, so the next move can still pick up a copy left behind.
#: One page of secondaries, by the partial index (0049). It is the page that bounds a
#: batch, not what it finds: most secondaries can be retained for years.
_SECONDARY_PAGE = """
SELECT location_id FROM catalog.location
 WHERE role = 'secondary' AND state IN ('pending', 'committed') AND location_id > %(after)s
 ORDER BY location_id
 LIMIT %(limit)s
"""
_LEFT_BEHIND = f"""
SELECT s.location_id
  FROM catalog.location s
  JOIN catalog.file_version v ON v.version_id = s.version_id
  JOIN catalog.location p ON p.version_id = s.version_id AND p.role = 'primary'
       AND p.state = 'committed' AND p.verified AND p.location_id <> s.location_id
 WHERE s.role = 'secondary' AND s.state IN ('pending', 'committed')
   AND s.location_id = ANY(%(page)s::bigint[])
   AND s.backend_id = p.backend_id AND s.container = p.container
   AND s.object_key <> p.object_key
   AND NOT v.legal_hold
   AND (v.retention_until IS NULL
        OR (v.retention_until <= %(now)s::timestamptz AND v.retention_until <= now()))
   AND NOT {on_source("s")}
   AND (%(datasets)s::bigint[] IS NULL OR v.dataset_id = ANY(%(datasets)s::bigint[]))
   AND coalesce(s.tier_changed_at, '-infinity'::timestamptz) < %(now)s::timestamptz - %(grace)s
   AND NOT EXISTS (SELECT 1 FROM pgmq.q_tiering q
                    WHERE q.message->>'target_location_id' = s.location_id::text)
 ORDER BY s.location_id
"""
#: Long enough for the sweep that follows a dead letter to pick the copy up again.
LEFT_BEHIND_GRACE = timedelta(days=1)
#: A batch, so one message's transaction stays short. The next follows at once.
LEFT_BEHIND_BATCH = 500


async def retire_replaced_secondaries(
    conn: psycopg.AsyncConnection, now: datetime, *, limit: int = LEFT_BEHIND_BATCH,
    after: int = 0, dataset_ids: list[int] | None = None,
) -> tuple[int, int | None]:  # fmt: skip
    """Retire what prune never reaches, since it leaves a current version alone: one page of
    secondaries, from `after`. Each row goes through `delete_blob`, which keeps the bytes
    while another row uses them, and checks the primary first. Returns how many, and where
    the next page starts, or None when this was the last."""
    cur = await conn.execute(_SECONDARY_PAGE, {"after": after, "limit": limit})
    page = [r[0] for r in await cur.fetchall()]
    if not page:
        return 0, None
    cur = await conn.execute(
        _LEFT_BEHIND,
        {"now": now, "grace": LEFT_BEHIND_GRACE, "datasets": dataset_ids, "page": page},
    )
    found = await cur.fetchall()
    done = 0
    for (location_id,) in found:
        async with conn.transaction():
            await set_actor(conn, "system:retention_delete")
            got = await conn.execute(
                "UPDATE catalog.location SET state = 'deleting', verified = false,"
                " committed_at = coalesce(committed_at, %s)"
                " WHERE location_id = %s AND role = 'secondary'"
                " AND state IN ('pending', 'committed')",
                (now, location_id),
            )
            if got.rowcount:
                await send(
                    conn, "retention", {"op": "delete_blob", "location_id": str(location_id)}
                )
                done += 1
    return done, (page[-1] if len(page) == limit else None)
