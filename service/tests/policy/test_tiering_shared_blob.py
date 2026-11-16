"""A blob with more than one catalog reference, moved for all of them.

`api.data.commit_dedupe` makes a second file's location point at a blob that
already has one. Set Blob Tier changes the blob, so it changes the tier of
every reference. These run the real dedupe, scheduler, move consumer and
recall path against Postgres 17 and sim.

A file that differs from the others now gets its own copy instead (decision 66,
`test_own_copy.py`), and only a move to Archive may move a blob over one that can't.
So these tests are about how a move reaches every row, when the files agree or the move
is to Archive, and what the rest do when they can't move: every world here has copies
failing unless it asks otherwise, and `move_message` starts where a move starts once
its copy has failed.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from omnifile_service import metrics
from omnifile_service.api.data import commit_dedupe, find_duplicate
from omnifile_service.api.recalls import online_location
from omnifile_service.api.validate import rfc3339
from omnifile_service.backends.base import (
    BackendKind,
    ChangeKind,
    ChangeRecord,
    LogicalTier,
    NotRestored,
)
from omnifile_service.backends.base import LogicalTier as T
from omnifile_service.backends.sim.faults import Op
from omnifile_service.catalog.privileges import as_app
from omnifile_service.policy import scheduler
from omnifile_service.web.entries import decorate
from omnifile_service.worker import tiering
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.changefeed_sync import (
    apply_record,
    register_container,
    sync_container,
)
from omnifile_service.worker.queue import Consumer, ConsumerConfig, Defer, Message, Retry
from omnifile_service.worker.tiering import handle_move, handle_restore_budgeted
from omnifile_service.worker.tiering_jobs import tiering_jobs

from . import support
from .support import (
    Stored,
    World,
    drain_retention,
    drain_verify,
    drain_work,
    primary,
    row,
    run,
    store,
    tick,
)

BLAKE3 = b"\x07" * 32
MINUTE = timedelta(minutes=1)
DAY = timedelta(days=1)
DAY_AND_A_BIT = DAY + timedelta(minutes=1)
POLICY = {"mode": "scheduled", "warm_day": 30, "cold_day": 90, "archive_day": 180}
#: Layouts where every move, Archive included, is Set Blob Tier in place: a
#: single account, and a WORM share (decision 31).
IN_PLACE = {
    "single_account": {"layout": "azure_worm"},
    "worm_share": {"layout": "azure_worm", "worm_days": 3650},
}


def make_world(*args: Any, copies: bool = False, **kwargs: Any) -> World:
    """A world where no copy can be made, unless `copies`: the backstop's."""
    w = support.make_world(*args, **kwargs)
    if not copies:
        ever = timedelta(days=36500)
        w.sim.faults.outage(w.clock.now() - DAY, w.clock.now() + ever, ops={Op.COPY_OBJECT})
    return w


def advance_to(w: World, n: float) -> None:
    w.clock.set(w.day(n))


async def _dedupe(
    w: World, source: Stored, name: str, parent: int, *, blake3: bytes = BLAKE3
) -> tuple[int, int, int]:
    """The upload path's dedupe, as `api.data.start_upload` runs it."""
    url = as_app(w.url, "api")
    async with await psycopg.AsyncConnection.connect(url, row_factory=dict_row) as db:
        await db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        size = (
            await (
                await db.execute(
                    "UPDATE catalog.file SET blake3 = %s WHERE file_id = %s RETURNING size",
                    (blake3, source.file_id),
                )
            ).fetchone()
        )["size"]
        f = await (
            await db.execute(
                "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
                " resident_bytes) VALUES (%s, %s, %s, 'file', 'uploading', %s, %s) RETURNING *",
                (w.dataset_id, parent, name, size, size),
            )
        ).fetchone()
        dup = await find_duplicate(db, w.dataset_id, blake3, size)
        assert dup is not None and dup["object_key"] == source.ref.key
        ctx = SimpleNamespace(edge_id=w.edge_id)
        vid = await commit_dedupe(
            db, ctx, f, dup, f"op-{f['file_id']}", size, 1, blake3, dup["crc64nvme"]
        )
        lid = (
            await (
                await db.execute(
                    "SELECT location_id FROM catalog.location WHERE version_id = %s", (vid,)
                )
            ).fetchone()
        )["location_id"]
        await db.commit()
    return f["file_id"], vid, lid


def dedupe(c: psycopg.Connection, w: World, source: Stored, name: str) -> Stored:
    """A second file with the same bytes, referencing `source`'s blob, written
    at the virtual now (the commit itself stamps the wall clock)."""
    return dedupe_with(c, w, source, name)


def dedupe_with(
    c: psycopg.Connection, w: World, source: Stored, name: str, *, blake3: bytes = BLAKE3
) -> Stored:
    fid, vid, lid = run(_dedupe(w, source, name, w.root_id, blake3=blake3))
    with c.transaction():
        c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        c.execute(
            "UPDATE catalog.file SET written_at = %s WHERE file_id = %s", (w.clock.now(), fid)
        )
    return Stored(fid, vid, lid, source.ref)


def blob_rows(c: psycopg.Connection, s: Stored) -> list[dict]:
    cur = c.execute(
        "SELECT location_id, file_id, role, state, logical_tier, storage_class, tier_changed_at,"
        " explicit_tier_set FROM catalog.location WHERE backend_id = %s AND container = %s"
        " AND object_key = %s AND backend_version_id IS NOT DISTINCT FROM %s"
        " ORDER BY location_id",
        (s.ref.backend_id, s.ref.container, s.ref.key, s.ref.version_id),
    )
    return [dict(zip([d.name for d in cur.description], r, strict=True)) for r in cur.fetchall()]


def assert_true_to_the_blob(c: psycopg.Connection, w: World, s: Stored) -> str:
    """Every committed reference shows the blob's real class. Returns it."""
    native = run(w.sim.head(s.ref)).native_class
    rows = [r for r in blob_rows(c, s) if r["state"] == "committed"]
    assert len(rows) >= 2
    assert {r["storage_class"] for r in rows} == {native}, rows
    return native


def audits(c: psycopg.Connection, file_id: int, action: str = "tier.move") -> list[dict]:
    cur = c.execute(
        "SELECT dataset_id, version_id, details FROM catalog.audit_log"
        " WHERE file_id = %s AND action = %s ORDER BY audit_id",
        (file_id, action),
    )
    return [dict(zip([d.name for d in cur.description], r, strict=True)) for r in cur.fetchall()]


def held_reports(c: psycopg.Connection, file_id: int) -> list[dict]:
    return [
        x
        for x in audits(c, file_id, "tier.move.refused")
        if x["details"]["why"] == "held_back_shared_blob"
    ]


def file_row(c: psycopg.Connection, file_id: int) -> dict:
    r = row(c, "SELECT * FROM catalog.file WHERE file_id = %s", file_id)
    assert r is not None
    return r


def pin(c: psycopg.Connection, w: World, file_id: int, tier: str) -> None:
    with c.transaction():
        c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        c.execute(
            "INSERT INTO catalog.tier_pin (dataset_id, file_id, logical_tier, until,"
            " set_by_identity_id, set_at, override_archive)"
            " VALUES (%s, %s, %s, NULL, %s, %s, true)",
            (w.dataset_id, file_id, tier, w.identity_id, w.clock.now()),
        )


def its_date(c: psycopg.Connection, w: World, *stored: Stored) -> None:
    """These files' archive dates are today, so a move of theirs to Archive is the date's,
    which may move a shared blob for everyone (decision 66). A pin to Archive may not."""
    with c.transaction():
        c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        c.execute(
            "UPDATE catalog.file SET archive_at = %s WHERE file_id = ANY(%s)",
            (w.clock.now().date(), [x.file_id for x in stored]),
        )


def agree(c: psycopg.Connection, w: World, tier: str, *stored: Stored) -> None:
    """These files want `tier` too, so a move to it is for all of them. Only a move to
    Archive may move a blob over a file that differs (decision 66), so tests of how a move
    reaches every row use files that agree."""
    with c.transaction():
        c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        c.execute(
            "UPDATE catalog.tier_pin SET released_at = %s, released_by_identity_id = %s"
            " WHERE file_id = ANY(%s) AND released_at IS NULL",
            (w.clock.now(), w.identity_id, [s.file_id for s in stored]),
        )
    for s in stored:
        pin(c, w, s.file_id, tier)


def move_message(s: Stored, tier: str, phase: str = "inplace", msg_id: int = 1) -> Message:
    now = None
    return Message(
        msg_id,
        1,
        now,  # type: ignore[arg-type]
        now,  # type: ignore[arg-type]
        {
            "kind": "move",
            "file_id": str(s.file_id),
            "version_id": str(s.version_id),
            "location_id": str(s.location_id),
            "tier": tier,
            "phase": phase,
            "no_own_copy": True,
        },
    )


async def _worker(w: World) -> psycopg.AsyncConnection:
    # A lock these tests get wrong fails the test rather than hanging it.
    return await psycopg.AsyncConnection.connect(
        as_app(w.url, "worker"), autocommit=True, options="-c lock_timeout=20000"
    )


async def _run_move(w: World, msg: Message, registry: BackendRegistry | None = None) -> bool:
    """One attempt, one transaction, as `Consumer` runs it (Retry commits).
    True if the message came back to the queue."""
    conn = await _worker(w)
    try:
        async with conn.transaction():
            try:
                await handle_move(msg, conn, registry=registry or w.registry, now=w.clock.now())
            except (Retry, Defer):
                return True
        return False
    finally:
        await conn.close()


async def _online(w: World, s: Stored) -> dict | None:
    url = as_app(w.url, "api")
    async with await psycopg.AsyncConnection.connect(url, row_factory=dict_row) as db:
        return await online_location(db, s.file_id, s.version_id)


async def _drain(gen: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in gen])


def make_stub(c: psycopg.Connection, s: Stored) -> None:
    """The edge punched the file: reads now go through a recall or a credential."""
    with c.transaction():
        c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        c.execute(
            "UPDATE catalog.file SET state = 'stub', resident_bytes = 0 WHERE file_id = %s",
            (s.file_id,),
        )


# ---------------------------------------------------------------------------
# The reported failure, and the two layouts' claims
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kw", IN_PLACE.values(), ids=IN_PLACE.keys())
def test_in_place_archive_reaches_every_reference(
    db: psycopg.Connection, policy_db_url: str, kw: dict[str, Any]
) -> None:
    """A's move to Archive is B's too: B must say so, and B's read must get a
    recall rather than a read credential for a blob that can't be read."""
    w = make_world(db, policy_db_url, policy=POLICY, **kw)
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 100)
    b = dedupe(db, w, a, "b.segy")
    make_stub(db, b)
    tick(w)
    advance_to(w, 180)
    res = tick(w)
    assert (a.file_id, "archive") in res.moves

    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert assert_true_to_the_blob(db, w, a) == "Archive"
    moved = primary(db, b.file_id)
    assert moved["location_id"] == b.location_id
    assert moved["logical_tier"] == "archive"
    assert moved["tier_changed_at"] == w.day(180)
    assert moved["explicit_tier_set"]
    # The file's summary, which the portal and the edge read, follows.
    assert file_row(db, b.file_id)["remote_tier"] == "archive"
    # No read credential for a blob nobody can read: a recall is needed.
    assert run(_online(w, b)) is None
    # The blob is what it is, so the bytes could not have been read.
    with pytest.raises(NotRestored):
        run(_drain(w.sim.get_range(a.ref, 0, 16)))


@pytest.mark.parametrize("kw", IN_PLACE.values(), ids=IN_PLACE.keys())
def test_the_other_reference_recalls_and_reads(
    db: psycopg.Connection, policy_db_url: str, kw: dict[str, Any]
) -> None:
    """The way out works: B's recall rehydrates a copy of the shared blob and
    B reads it; A's archived blob stays where it is. A's own recall joins."""
    w = make_world(db, policy_db_url, policy=POLICY, **kw)
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 100)
    b = dedupe(db, w, a, "b.segy")
    make_stub(db, a)
    make_stub(db, b)
    tick(w)
    advance_to(w, 180)
    tick(w)
    assert run(_online(w, b)) is None

    async def recall(s: Stored) -> int:
        conn = await _worker(w)
        try:
            async with conn.transaction():
                await conn.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
                cur = await conn.execute(
                    "INSERT INTO catalog.recall (file_id, version_id, edge_id)"
                    " VALUES (%s, %s, %s) RETURNING recall_id",
                    (s.file_id, s.version_id, w.edge_id),
                )
                rid = (await cur.fetchone())[0]  # type: ignore[index]
            await poll(rid)
            return rid
        finally:
            await conn.close()

    async def poll(rid: int) -> None:
        conn = await _worker(w)
        try:
            async with conn.transaction():
                try:
                    await handle_restore_budgeted(
                        Message(1, 1, None, None, {"recall_id": str(rid)}),  # type: ignore[arg-type]
                        conn,
                        registry=w.registry,
                        now=w.clock.now(),
                    )
                except Retry:
                    pass
        finally:
            await conn.close()

    rb = run(recall(b))
    ra = run(recall(a))
    state = "SELECT state FROM catalog.recall WHERE recall_id = %s"
    assert row(db, state, rb) == {"state": "rehydrating"}
    assert row(db, state, ra) == {"state": "rehydrating"}
    w.clock.set(w.clock.now() + timedelta(days=2))
    run(poll(rb))
    run(poll(ra))
    assert row(db, state, rb) == {"state": "downloading"}
    assert row(db, state, ra) == {"state": "downloading"}

    loc = run(_online(w, b))
    assert loc is not None and loc["role"] == "recall_copy"
    ref = type(a.ref)(w.backend_id, loc["container"], loc["object_key"], loc["backend_version_id"])
    assert run(_drain(w.sim.get_range(ref, 0, 16))) == b"x" * 16
    # The archived blob stayed archived, for both.
    assert assert_true_to_the_blob(db, w, a) == "Archive"
    assert primary(db, b.file_id)["logical_tier"] == "archive"


def test_two_account_archive_copy_leaves_the_other_reference_alone(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Confirms the claim: on `two_account` Archive is a copy, so the shared
    blob stays in the live account, B's row stays true and readable, and the
    delete of A's original only retires A's row."""
    w = make_world(db, policy_db_url, policy=POLICY, copies=True)
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 100)
    tick(w)
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    advance_to(w, 180)
    tick(w)
    moved = primary(db, a.file_id)
    assert (moved["container"], moved["logical_tier"]) == ("archive/data", "archive")
    assert drain_retention(w) == ["shared"]
    # The blob is where it was, online, in the tier B's row says, and B reads it.
    head = run(w.sim.head(a.ref))
    assert head.logical_tier is not T.ARCHIVE
    assert primary(db, b.file_id)["storage_class"] == head.native_class
    assert run(_online(w, b)) is not None
    assert run(_drain(w.sim.get_range(a.ref, 0, 16))) == b"x" * 16
    # B archives on its own date; only then is the last reference gone.
    advance_to(w, 280)
    tick(w)
    assert primary(db, b.file_id)["logical_tier"] == "archive"
    assert drain_retention(w) == ["deleted"]


def test_two_account_in_place_moves_reach_every_reference(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The same layout is not safe below Archive: hot, cool and cold are Set
    Blob Tier on the live account, Smart's one-way flag included. Only A moves
    here, so B's row can only be right if A's move wrote it. B wants the same."""
    w = make_world(db, policy_db_url, policy=POLICY)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    assert primary(db, b.file_id)["logical_tier"] == "smart"
    assert not primary(db, b.file_id)["explicit_tier_set"]
    agree(db, w, "hot", b)
    run(_run_move(w, move_message(a, "hot")))  # out of the Smart upload default
    assert assert_true_to_the_blob(db, w, a) == "Hot"
    assert primary(db, b.file_id)["explicit_tier_set"]
    agree(db, w, "warm", b)
    run(_run_move(w, move_message(a, "warm")))
    assert assert_true_to_the_blob(db, w, a) == "Cool"
    agree(db, w, "cold", b)
    run(_run_move(w, move_message(a, "cold")))
    assert assert_true_to_the_blob(db, w, a) == "Cold"


# ---------------------------------------------------------------------------
# Which rows are the blob
# ---------------------------------------------------------------------------


def test_only_rows_of_this_blob_change(db: psycopg.Connection, policy_db_url: str) -> None:
    """Same key, other version; same key, other container; same container and
    key, other backend; and a reference that isn't committed: all left alone."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    # A later version of a's blob name, on a versioned container.
    other_version = store(db, w, "a-later.segy", key="a.segy", data=b"z" * 4096)
    assert other_version.ref.version_id != a.ref.version_id
    # The same name in another backend's world.
    w2 = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    other_backend = store(db, w2, "a.segy")

    def clone(
        state: str, *, container: str | None = None, key: str | None = None, role: str = "secondary"
    ) -> int:
        with db.transaction():
            db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            r = db.execute(
                "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
                " object_key, backend_version_id, role, state, logical_tier, storage_class, size,"
                " committed_at) SELECT file_id, version_id, backend_id, %s, %s,"
                " backend_version_id, %s, %s, logical_tier, storage_class, size, committed_at"
                " FROM catalog.location WHERE location_id = %s RETURNING location_id",
                (container or a.ref.container, key or a.ref.key, role, state, a.location_id),
            ).fetchone()
        assert r is not None
        return r[0]

    other_container = clone("committed", container="other/data")
    other_key = clone("committed", key="not-a.segy")
    deleting = clone("deleting")
    pending = clone("pending")
    agree(db, w, "cold", b)
    run(_run_move(w, move_message(a, "cold")))

    assert run(w.sim.head(a.ref)).native_class == "Cold"
    tier = "SELECT logical_tier::text AS t FROM catalog.location WHERE location_id = %s"
    assert row(db, tier, a.location_id) == {"t": "cold"}
    assert row(db, tier, b.location_id) == {"t": "cold"}
    for untouched in (
        other_version.location_id,
        other_backend.location_id,
        other_container,
        other_key,
        deleting,
        pending,
    ):
        assert row(db, tier, untouched) == {"t": "hot"}, untouched
    assert run(w.sim.head(other_version.ref)).native_class == "Hot"
    assert run(w2.sim.head(other_backend.ref)).native_class == "Hot"
    assert audits(db, other_version.file_id) == []
    assert audits(db, other_backend.file_id) == []


def test_a_copy_that_mints_a_version_leaves_the_pinned_one(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """S3's class change is a CopyObject onto itself: a new version in the new
    class, and the version B is pinned to keeps its class. B's row was true;
    it stays as it was."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="s3", copies=True)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")

    registry = BackendRegistry.from_mapping({w.backend_id: _CopiesInPlace(w.sim)})  # type: ignore[dict-item]
    run(_run_move(w, move_message(a, "cold"), registry))

    moved = primary(db, a.file_id)
    assert moved["logical_tier"] == "cold"
    assert moved["backend_version_id"] != a.ref.version_id
    assert run(w.sim.head(a.ref)).logical_tier is T.HOT  # the old version, B's
    assert primary(db, b.file_id)["logical_tier"] == "hot"
    assert primary(db, b.file_id)["backend_version_id"] == a.ref.version_id
    assert audits(db, b.file_id) == []


def test_a_superseded_reference_follows_but_not_the_files_tier(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """B once had these bytes and has since been overwritten: its old version
    shows the blob's tier, and B's current tier stays its current version's."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    its_date(db, w, a)
    # B's next version, in a blob of its own.
    newer = run(w.sim.put("data", "b-new", _bytes(b"y" * 4096), None, {}))
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (vid,) = db.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
            " size) VALUES (%s, %s, %s, 'b.segy', 'content', 'pending', 4096)"
            " RETURNING version_id",
            (b.file_id, w.dataset_id, w.root_id),
        ).fetchone()
        db.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
            " backend_version_id, role, state, logical_tier, storage_class, size, committed_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'primary', 'committed', 'hot', 'Hot', 4096, %s)",
            (b.file_id, vid, w.backend_id, newer.container, newer.key, newer.version_id,
             w.clock.now()),
        )  # fmt: skip
        db.execute(
            "UPDATE catalog.file_version SET state = 'committed', committed_at = now()"
            " WHERE version_id = %s",
            (vid,),
        )
    run(_run_move(w, move_message(a, "archive")))

    old = row(
        db, "SELECT logical_tier::text AS t FROM catalog.location WHERE location_id = %s",
        b.location_id,
    )  # fmt: skip
    assert old == {"t": "archive"}
    assert file_row(db, b.file_id)["remote_tier"] == "hot"
    assert primary(db, b.file_id)["version_id"] == vid


def test_a_reference_in_another_dataset(db: psycopg.Connection, policy_db_url: str) -> None:
    """Dedupe stays in one dataset today, but a blob is a blob: a row in
    another dataset is told the truth, under its own dataset's audit and feed,
    with nothing in it that names the mover's file."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a-secret-name.segy")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (edge2,) = db.execute(
            "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
            " VALUES (%s, %s, now() + interval '1 day') RETURNING edge_id",
            (f"edge2-{w.dataset_id}", (b"%032d" % w.dataset_id)),
        ).fetchone()
        (ds2,) = db.execute(
            "INSERT INTO catalog.dataset (name, backend_id, edge_id, storage_layout)"
            " VALUES (%s, %s, %s, 'single_account') RETURNING dataset_id",
            (f"ds2-{w.dataset_id}", w.backend_id, edge2),
        ).fetchone()
        (root2,) = db.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
            " VALUES (%s, NULL, '', 'dir') RETURNING file_id",
            (ds2,),
        ).fetchone()
        (fid,) = db.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
            " resident_bytes) VALUES (%s, %s, 'mine.segy', 'file', 'stub', 4096, 0)"
            " RETURNING file_id",
            (ds2, root2),
        ).fetchone()
        (vid,) = db.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
            " size) VALUES (%s, %s, %s, 'mine.segy', 'content', 'pending', 4096)"
            " RETURNING version_id",
            (fid, ds2, root2),
        ).fetchone()
        (lid,) = db.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
            " backend_version_id, role, state, logical_tier, storage_class, size, committed_at)"
            " SELECT %s, %s, backend_id, container, object_key, backend_version_id, 'primary',"
            " 'committed', logical_tier, storage_class, size, committed_at FROM catalog.location"
            " WHERE location_id = %s RETURNING location_id",
            (fid, vid, a.location_id),
        ).fetchone()
        db.execute(
            "UPDATE catalog.file_version SET state = 'committed', committed_at = now()"
            " WHERE version_id = %s",
            (vid,),
        )
    (before,) = db.execute(
        "SELECT count(*) FROM catalog.feed_event WHERE dataset_id = %s AND entity = 'file'"
        " AND entity_id = %s",
        (ds2, fid),
    ).fetchone()

    run(_run_move(w, move_message(a, "archive")))

    assert row(
        db, "SELECT logical_tier::text AS t FROM catalog.location WHERE location_id = %s", lid
    ) == {"t": "archive"}
    assert file_row(db, fid)["remote_tier"] == "archive"
    # Its edge hears of it, like any change to one of its files.
    (after,) = db.execute(
        "SELECT count(*) FROM catalog.feed_event WHERE dataset_id = %s AND entity = 'file'"
        " AND entity_id = %s",
        (ds2, fid),
    ).fetchone()
    assert after == before + 1
    # Its audit is its own dataset's, and doesn't name the mover's file.
    got = audits(db, fid)
    assert [a_["dataset_id"] for a_ in got] == [ds2]
    assert got[0]["version_id"] == vid
    assert got[0]["details"] == {"from": "hot", "to": "archive", "how": "shared_blob"}
    assert "secret" not in str(got[0])


def test_the_moved_files_own_audit_is_unchanged(db: psycopg.Connection, policy_db_url: str) -> None:
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    agree(db, w, "cold", b)
    run(_run_move(w, move_message(a, "cold")))
    assert [x["details"] for x in audits(db, a.file_id)] == [
        {"from": "hot", "to": "cold", "how": "in_place"}
    ]
    assert [x["details"] for x in audits(db, b.file_id)] == [
        {"from": "hot", "to": "cold", "how": "shared_blob"}
    ]
    # Only the mover is looked at again: B's plan is B's business (see below).
    assert file_row(db, a.file_id)["next_action_at"] == w.clock.now()
    assert file_row(db, b.file_id)["next_action_at"] is None


# ---------------------------------------------------------------------------
# Different wishes for one blob: the catalog stays true, and nobody decides
# ---------------------------------------------------------------------------


def test_younger_reference_never_rehydrates_the_archived_blob(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """B's schedule says Cool at its day 30, after A archived the blob. With
    the truth in B's row the schedule sees Archive and moves nothing; with
    `hot` there it would Set Blob Tier the blob out of Archive for everyone,
    paying the early-deletion fee."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 170)
    dedupe(db, w, a, "b.segy")
    tick(w)
    advance_to(w, 180)
    tick(w)
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    fees = len(w.sim.early_deletion_fees)
    advance_to(w, 200)  # B's day 30
    res = tick(w)
    assert res.moves == []
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert len(w.sim.early_deletion_fees) == fees
    assert assert_true_to_the_blob(db, w, a) == "Archive"


def test_a_move_queued_before_the_blob_archived_does_not_pull_it_back(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Both moves were queued in one sweep: A to Archive and B to Cold. A lands
    first. Leaving Archive is never a policy step (`engine.evaluate`), so B's
    is refused, audited, and the blob stays."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    its_date(db, w, a)
    run(_run_move(w, move_message(a, "archive")))
    run(_run_move(w, move_message(b, "cold")))
    head = run(w.sim.head(a.ref))
    assert (head.native_class, head.logical_tier) == ("Archive", T.ARCHIVE)
    assert assert_true_to_the_blob(db, w, a) == "Archive"
    assert [x["details"] for x in audits(db, b.file_id, "tier.move.refused")] == [
        {"to": "cold", "why": "archived"}
    ]
    assert file_row(db, b.file_id)["next_action_at"] == w.clock.now() + timedelta(days=1)


def test_a_hot_pin_on_the_other_reference_cannot_pull_back_archive(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """B is pinned hot and A's date comes. On an in-place layout A wins, as it
    does today, and B's file is archived against its pin. The catalog says so.
    The engine never moves a blob out of Archive, so there is no fight: a stub
    gets one recall for the pin, and the blob stays."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 170)
    b = dedupe(db, w, a, "b.segy")
    make_stub(db, b)
    tick(w)
    pin(db, w, b.file_id, "hot")
    advance_to(w, 180)
    res = tick(w)
    assert (a.file_id, "archive") in res.moves
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert primary(db, b.file_id)["logical_tier"] == "archive"
    assert len(res.recalls) == 1  # B's pin asks for its bytes back
    advance_to(w, 181)
    res = tick(w)
    assert res.moves == []
    assert run(w.sim.head(a.ref)).native_class == "Archive"


def test_a_warmer_pin_keeps_the_blob_where_it_is(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A's schedule wants the blob Cold and B's pin wants it Hot. Neither can get its own
    copy here. Cold is no date, so nothing moves: B's pin stays true and no fee is billed.
    Was: the blob went Cold, and B's pin brought it back a day later for one fee."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 80)
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, b.file_id, "hot")
    for day in (90, 91, 92):
        advance_to(w, day)
        tick(w)
        assert assert_true_to_the_blob(db, w, a) == "Hot"
    assert w.sim.early_deletion_fees == []


def test_two_pins_that_disagree_stay_true(db: psycopg.Connection, policy_db_url: str) -> None:
    """A pinned cold and B pinned hot want one blob in two tiers, and neither can get
    its own copy (decision 66). The blob stays, and the catalog agrees with it."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "hot")
    for n in range(1, 4):
        advance_to(w, n)
        tick(w)
        assert_true_to_the_blob(db, w, a)


# ---------------------------------------------------------------------------
# The change feed, and two workers
# ---------------------------------------------------------------------------


def test_the_feed_applies_to_every_reference_as_it_already_did(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """`BlobTierChanged` matches rows on exactly this blob, so a tier changed
    outside OmniFile (a lifecycle rule, the portal) reaches them all."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    at = w.clock.now() + timedelta(hours=1)
    run(_apply(w, ChangeRecord(ChangeKind.TIER_CHANGED, a.ref, at, native_class="Cool")))
    for s in (a, b):
        assert primary(db, s.file_id)["logical_tier"] == "warm"


def test_an_old_feed_record_does_not_undo_a_move_on_the_other_reference(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A to Cold at t1, then B to Hot a day later. A replayed t1 record (feeds
    lag) must not make A's row say Cold: its clock moved to t2 with the blob."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    agree(db, w, "cold", b)
    run(_run_move(w, move_message(a, "cold")))
    t1 = w.clock.now()
    t2 = t1 + DAY_AND_A_BIT
    w.clock.set(t2)
    agree(db, w, "hot", a)
    run(_run_move(w, move_message(b, "hot")))
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    run(_apply(w, ChangeRecord(ChangeKind.TIER_CHANGED, a.ref, t1, native_class="Cold")))
    assert assert_true_to_the_blob(db, w, a) == "Hot"
    for s in (a, b):
        assert primary(db, s.file_id)["tier_changed_at"] == t2


async def _apply(w: World, rec: ChangeRecord) -> None:
    conn = await _worker(w)
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            await apply_record(conn, w.sim, rec, w.clock.now())
    finally:
        await conn.close()


class _Paused:
    """sim whose first Set Blob Tier has reached the blob but not yet answered,
    which is when a worker holds no row locks."""

    def __init__(self, sim: Any) -> None:
        self._sim = sim
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)

    async def set_class(self, ref: Any, tier: LogicalTier, **kw: Any) -> Any:
        self.calls.append(str(tier))
        got = await self._sim.set_class(ref, tier, **kw)
        if len(self.calls) == 1:
            self.reached.set()
            await self.release.wait()
        return got


def test_two_workers_on_one_blob_leave_the_rows_agreeing(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Worker 1 sets the blob Cold and stalls before it commits. Worker 2, on
    another reference, would set it Cool and commit first; then worker 1 would
    write Cold over every row while the blob is Cool. The blob's lock sends
    worker 2 back to the queue at once, so its batch isn't held up and the
    provider's order and the commit order are one. Its next attempt is a day
    on, so the warmer move isn't held back as well."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    paused = _Paused(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]
    second = move_message(b, "warm", msg_id=2)
    agree(db, w, "cold", b)

    async def race() -> None:
        first = asyncio.create_task(_run_move(w, move_message(a, "cold"), registry))
        await asyncio.wait_for(paused.reached.wait(), 10)
        assert await asyncio.wait_for(_run_move(w, second, registry), 5) is True
        assert paused.calls == ["cold"], "the second worker reached the blob while it was busy"
        paused.release.set()
        assert await asyncio.wait_for(first, 10) is False

    run(race())
    w.clock.set(w.clock.now() + DAY_AND_A_BIT)
    agree(db, w, "warm", a)
    assert run(_run_move(w, second, registry)) is False
    assert paused.calls == ["cold", "warm"]
    assert assert_true_to_the_blob(db, w, a) == "Cool"


def test_a_move_the_other_reference_already_made_is_a_no_op(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Both were queued for Cold. The second finds the blob there: no second
    provider call, no second audit, and the minimum-duration clock keeps the
    time the blob really moved."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    t1 = w.clock.now()
    agree(db, w, "warm", b)
    run(_run_move(w, move_message(a, "warm")))
    w.clock.set(t1 + timedelta(hours=1))
    run(_run_move(w, move_message(b, "warm")))
    for s in (a, b):
        assert primary(db, s.file_id)["tier_changed_at"] == t1
    assert len(audits(db, a.file_id)) == 1
    assert len(audits(db, b.file_id)) == 1
    # B is still looked at: its own plan (a stub, say) follows the move it asked for.
    assert file_row(db, b.file_id)["next_action_at"] == w.clock.now()


def test_a_move_waiting_on_the_blob_sees_what_the_holder_did(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Worker 2 read B's row as `hot` before it asked for the blob, and worker 1
    archived the blob just before it got its turn. It must look again once it
    holds the lock, or it would pull the blob out of Archive."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    its_date(db, w, a)
    paused = _Paused(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]

    async def race() -> None:
        first = asyncio.create_task(_run_move(w, move_message(a, "archive"), registry))
        await asyncio.wait_for(paused.reached.wait(), 10)
        second = move_message(b, "cold", msg_id=2)
        assert await asyncio.wait_for(_run_move(w, second, registry), 5) is True
        paused.release.set()
        await asyncio.wait_for(first, 10)
        assert await _run_move(w, second, registry) is False

    run(race())
    assert paused.calls == ["archive"]
    assert assert_true_to_the_blob(db, w, a) == "Archive"


def test_a_move_that_finished_just_before_the_lock_is_seen(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The first read of B's row and the lock are two statements. A's move can
    commit between them, so the row is read again once the lock is held."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    its_date(db, w, a)

    class MovesFirst:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        def __getattr__(self, name: str) -> Any:
            return getattr(self._conn, name)

        async def execute(self, sql_: str, *args: Any) -> Any:
            if "pg_try_advisory_xact_lock" in sql_:
                await _run_move(w, move_message(a, "archive"))
            return await self._conn.execute(sql_, *args)

    async def go() -> None:
        conn = await _worker(w)
        try:
            async with conn.transaction():
                await handle_move(
                    move_message(b, "cold", msg_id=2),
                    MovesFirst(conn),  # type: ignore[arg-type]
                    registry=w.registry,
                    now=w.clock.now(),
                )
        finally:
            await conn.close()

    run(go())
    assert assert_true_to_the_blob(db, w, a) == "Archive"


def test_a_feed_record_that_got_there_first_stands(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The feed can deliver the worker's own change before the worker commits.
    A row it already made true keeps the feed's time and gets no audit: nothing
    happened to that reference that the feed hadn't already recorded."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    paused = _Paused(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]
    seen = w.clock.now() + timedelta(minutes=1)
    agree(db, w, "cold", b)

    async def race() -> None:
        worker = asyncio.create_task(_run_move(w, move_message(a, "cold"), registry))
        await asyncio.wait_for(paused.reached.wait(), 10)
        await _apply(w, ChangeRecord(ChangeKind.TIER_CHANGED, a.ref, seen, native_class="Cold"))
        w.clock.set(seen + timedelta(minutes=1))
        paused.release.set()
        await asyncio.wait_for(worker, 10)

    run(race())
    assert assert_true_to_the_blob(db, w, a) == "Cold"
    assert primary(db, b.file_id)["tier_changed_at"] == seen
    assert audits(db, b.file_id) == []
    assert len(audits(db, a.file_id)) == 1


def test_a_feed_page_in_flight_is_waited_for(db: psycopg.Connection, policy_db_url: str) -> None:
    """The feed has the rows locked and hasn't committed. The worker must wait
    and read what the feed wrote, not decide from what it saw before."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    paused = _Paused(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]
    seen = w.clock.now() + timedelta(minutes=1)
    agree(db, w, "cold", b)

    async def race() -> None:
        worker = asyncio.create_task(_run_move(w, move_message(a, "cold"), registry))
        await asyncio.wait_for(paused.reached.wait(), 10)
        feed = await _worker(w)
        try:
            async with feed.transaction():
                await feed.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
                rec = ChangeRecord(ChangeKind.TIER_CHANGED, a.ref, seen, native_class="Cold")
                await apply_record(feed, w.sim, rec, w.clock.now())
                w.clock.set(seen + timedelta(minutes=1))
                paused.release.set()
                await asyncio.sleep(0.5)  # the worker reaches the rows the feed holds
        finally:
            await feed.close()
        await asyncio.wait_for(worker, 10)

    run(race())
    assert assert_true_to_the_blob(db, w, a) == "Cold"
    assert primary(db, b.file_id)["tier_changed_at"] == seen
    assert audits(db, b.file_id) == []


def test_a_copy_placed_under_another_name_is_a_reference_too(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The row shape PR #70's `Batch.adopt_placed` writes: an annexed, remote
    file whose location copies the source's backend, container, key and version.
    Matching on the blob covers it with nothing added for annexes."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (fid,) = db.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
            " resident_bytes, origin) VALUES (%s, %s, 'placed.segy', 'file', 'remote', 4096, 0,"
            " 'annex') RETURNING file_id",
            (w.dataset_id, w.root_id),
        ).fetchone()
        (vid,) = db.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
            " size) VALUES (%s, %s, %s, 'placed.segy', 'content', 'pending', 4096)"
            " RETURNING version_id",
            (fid, w.dataset_id, w.root_id),
        ).fetchone()
        (lid,) = db.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
            " backend_version_id, role, state, logical_tier, storage_class, size, crc64nvme, etag,"
            " verified, verified_at, verified_by, committed_at)"
            " SELECT %s, %s, backend_id, container, object_key, backend_version_id, 'primary',"
            " 'committed', logical_tier, storage_class, %s, crc64nvme, etag, verified,"
            " verified_at, verified_by, now() FROM catalog.location WHERE location_id = %s"
            " RETURNING location_id",
            (fid, vid, 4096, a.location_id),
        ).fetchone()
        db.execute(
            "UPDATE catalog.file_version SET state = 'committed', committed_at = now()"
            " WHERE version_id = %s",
            (vid,),
        )
    its_date(db, w, a)
    run(_run_move(w, move_message(a, "archive")))
    assert assert_true_to_the_blob(db, w, a) == "Archive"
    assert row(
        db, "SELECT logical_tier::text AS t FROM catalog.location WHERE location_id = %s", lid
    ) == {"t": "archive"}
    assert file_row(db, fid)["remote_tier"] == "archive"
    assert file_row(db, fid)["state"] == "remote"


async def _bytes(data: bytes) -> AsyncIterator[bytes]:
    yield data


# ---------------------------------------------------------------------------
# A move that may not move the blob, and has no copy of its own, waits a day
# ---------------------------------------------------------------------------


async def _sweep_once(w: World) -> scheduler.SweepResult:
    """One sweep and its work, as the worker runs them each minute."""
    conn = await _worker(w)
    try:
        res = await scheduler.sweep(conn, w.clock.now(), dataset_ids=[w.dataset_id])
        await drain_work(conn, w, w.clock.now())
        return res
    finally:
        await conn.close()


async def _follow(w: World, container: str, *, first: bool = False) -> None:
    conn = await _worker(w)
    try:
        if first:
            async with conn.transaction():
                await register_container(conn, w.backend_id, container)
        await sync_container(conn, w.registry, w.backend_id, container, w.clock.now())
    finally:
        await conn.close()


def blob_moves(c: psycopg.Connection, w: World) -> int:
    """Real Set Blob Tier calls that changed the catalog. The scheduler's lists
    also count a move it queued and the guard then held."""
    (n,) = c.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE dataset_id = %s AND action = 'tier.move'"
        " AND details->>'how' = 'in_place'",
        (w.dataset_id,),
    ).fetchone()
    return n


def sweeps(
    c: psycopg.Connection, w: World, n: int, *, every: timedelta = MINUTE,
    feed: int | None = None, container: str = "",
) -> int:  # fmt: skip
    """`n` sweeps; the feed applied every `feed` sweeps (None: the feed is down).
    Returns the blob moves they made."""
    before = blob_moves(c, w)
    for i in range(1, n + 1):
        w.clock.set(w.clock.now() + every)
        run(_sweep_once(w))
        if feed is not None and i % feed == 0:
            run(_follow(w, container))
    return blob_moves(c, w) - before


def overwrite(db: psycopg.Connection, w: World, s: Stored, name: str) -> None:
    """A new version of `s`, in a blob of its own: its old one is superseded,
    and `scheduler.sweep_versions` tiers it from then on."""
    newer = run(w.sim.put("data", f"{name}-new", _bytes(b"y" * 4096), None, {}))
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (vid,) = db.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
            " size) VALUES (%s, %s, %s, %s, 'content', 'pending', 4096) RETURNING version_id",
            (s.file_id, w.dataset_id, w.root_id, name),
        ).fetchone()
        db.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
            " backend_version_id, role, state, logical_tier, storage_class, size, committed_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'primary', 'committed', 'hot', 'Hot', 4096, %s)",
            (s.file_id, vid, w.backend_id, newer.container, newer.key, newer.version_id,
             w.clock.now()),
        )  # fmt: skip
        db.execute(
            "UPDATE catalog.file_version SET state = 'committed', committed_at = %s"
            " WHERE version_id = %s",
            (w.clock.now(), vid),
        )


FEEDS = {"feed_down": None, "feed_lags_5_minutes": 5}


@pytest.mark.parametrize("feed", FEEDS.values(), ids=FEEDS.keys())
def test_two_pins_do_not_loop(db: psycopg.Connection, policy_db_url: str, feed: int | None) -> None:
    """Without #71's guard: one move on every sweep, 60 here, and a fee on every other one,
    whatever the feed does. With #71's: one move, and a fee a day. Now neither pin can move
    the blob over the other."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    run(_follow(w, a.ref.container, first=True))
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "hot")
    assert sweeps(db, w, 60, feed=feed, container=a.ref.container) == 0
    assert w.sim.early_deletion_fees == []
    assert assert_true_to_the_blob(db, w, a) == "Hot"
    # A has been told once, and nothing is queued for it meanwhile.
    assert len(held_reports(db, a.file_id)) == 1
    w.clock.set(w.clock.now() + MINUTE)
    assert run(_sweep_once(w)).moves == []


@pytest.mark.parametrize("layout", ["azure_worm", "azure_two_account"])
@pytest.mark.parametrize("feed", FEEDS.values(), ids=FEEDS.keys())
def test_one_pin_and_an_overwritten_original_do_not_loop(
    db: psycopg.Connection, policy_db_url: str, layout: str, feed: int | None
) -> None:
    """It only takes one pin. A file is copied (a dedupe), the copy pinned hot
    and the original overwritten: `sweep_versions` pushes the old version colder
    every sweep and the pin pushes the blob back. Without the guard: 60 moves
    and 30 fees in 60 sweeps. Now the old version waits for its own copy, and only A's
    new blob moves."""
    w = make_world(db, policy_db_url, policy=POLICY, layout=layout)
    a = store(db, w, "a.segy")
    tick(w)
    b = dedupe(db, w, a, "b.segy")
    pin(db, w, b.file_id, "hot")
    tick(w)
    run(_follow(w, a.ref.container, first=True))
    overwrite(db, w, a, "a.segy")
    advance_to(w, 40)
    fees = len(w.sim.early_deletion_fees)
    tries = w.sim.faults.calls[Op.COPY_OBJECT]
    assert sweeps(db, w, 60, feed=feed, container=a.ref.container) == 1  # A's new blob
    assert len(w.sim.early_deletion_fees) == fees
    assert assert_true_to_the_blob(db, w, a) == "Hot"
    # It tried once in the hour, not on every sweep.
    assert w.sim.faults.calls[Op.COPY_OBJECT] - tries == 3


def test_folder_pins_over_many_duplicates_do_not_fan_out(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Two folders pinned against each other, three duplicate pairs across them.
    Without the guard: a move per pair on every sweep."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (cold_dir,) = db.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, %s,"
            " 'cold', 'dir') RETURNING file_id",
            (w.dataset_id, w.root_id),
        ).fetchone()
        (hot_dir,) = db.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, %s,"
            " 'hot', 'dir') RETURNING file_id",
            (w.dataset_id, w.root_id),
        ).fetchone()
    pairs = []
    for i in range(3):
        a = store(db, w, f"{i}.segy", parent=cold_dir, data=bytes([i]) * 4096)
        fid, vid, lid = run(_dedupe(w, a, f"{i}.segy", hot_dir, blake3=bytes([i]) * 32))
        pairs.append((a, Stored(fid, vid, lid, a.ref)))
    tick(w)
    pin(db, w, cold_dir, "cold")
    pin(db, w, hot_dir, "hot")
    assert sweeps(db, w, 20) == 0
    for a, _ in pairs:
        assert assert_true_to_the_blob(db, w, a) == "Hot"


def test_two_pins_over_days_leave_the_blob_and_keep_saying_so(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Was: they alternated, at most 11 moves and a fee a day. Now five days of hourly
    sweeps move nothing and bill nothing, and it is reported each day, so the alert keeps
    firing. Each day's retry tries the copy three times, which bills nothing."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "hot")
    moves = 0
    for _ in range(120):  # five days of hourly sweeps
        moves += sweeps(db, w, 1, every=timedelta(hours=1))
        assert assert_true_to_the_blob(db, w, a) == "Hot"
    assert moves == 0
    assert w.sim.early_deletion_fees == []
    assert 5 <= len(held_reports(db, a.file_id)) <= 6
    assert w.sim.faults.calls[Op.COPY_OBJECT] <= 18


def test_a_waiting_move_comes_back_by_itself(db: psycopg.Connection, policy_db_url: str) -> None:
    """Acknowledged, never left in the queue: the file is looked at when the wait ends and
    tries again then."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    t1 = w.clock.now()
    pin(db, w, b.file_id, "cold")
    assert run(_run_move(w, move_message(b, "cold"))) is False  # no Retry
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert file_row(db, b.file_id)["next_action_at"] == t1 + DAY
    w.clock.set(t1 + DAY - MINUTE)
    assert run(_sweep_once(w)).moves == []  # not on every sweep before then
    w.clock.set(t1 + DAY)
    assert run(_sweep_once(w)).moves == [(b.file_id, "cold")]
    assert file_row(db, b.file_id)["next_action_at"] == t1 + 2 * DAY
    assert assert_true_to_the_blob(db, w, a) == "Hot"


def test_a_pin_released_during_the_hold_moves_nothing(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Nothing is left queued to fire later: the scheduler decides afresh."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "hot")
    assert sweeps(db, w, 3) == 0
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.tier_pin SET released_at = %s, released_by_identity_id = %s"
            " WHERE file_id = %s",
            (w.clock.now(), w.identity_id, a.file_id),
        )
    w.clock.set(w.clock.now() + DAY_AND_A_BIT)
    assert run(_sweep_once(w)).moves == []
    assert run(w.sim.head(a.ref)).native_class == "Hot"


def test_a_held_file_is_still_looked_at_on_its_archive_date(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A move to Archive doesn't wait, so a date inside the wait is not missed."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    t1 = w.clock.now()  # noon
    midnight = datetime.combine((t1 + DAY).date(), datetime.min.time(), tzinfo=UTC)
    assert t1 < midnight < t1 + DAY
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.file SET archive_at = %s WHERE file_id = %s",
            (midnight.date(), b.file_id),
        )
    run(_run_move(w, move_message(b, "cold")))
    assert file_row(db, b.file_id)["next_action_at"] == midnight


def test_a_hold_is_reported_once_a_day_per_blob(db: psycopg.Connection, policy_db_url: str) -> None:
    """One audit row and one alert, in the file's own dataset, whichever file
    of the blob is waiting, and again the next day if it is still going on."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    c = dedupe(db, w, a, "c.segy")
    unrelated = store(db, w, "unrelated.segy", data=b"u" * 4096)
    other = dedupe_with(db, w, unrelated, "other.segy", blake3=b"\x09" * 32)
    alerts = []
    real = metrics.tier_held_back
    metrics.tier_held_back = lambda d, f: alerts.append((d, f))  # type: ignore[assignment]
    try:
        for s in (b, b, c, b):
            assert run(_run_move(w, move_message(s, "cold"))) is False
        got = audits(db, b.file_id, "tier.move.refused")
        assert len(got) == 1
        assert audits(db, c.file_id, "tier.move.refused") == []
        assert alerts == [(w.dataset_id, b.file_id)]
        d = got[0]["details"]
        assert got[0]["dataset_id"] == w.dataset_id and got[0]["version_id"] == b.version_id
        assert d["to"] == "cold" and d["why"] == "held_back_shared_blob"
        assert d["until"] == (w.clock.now() + DAY).isoformat()
        assert d["location_id"] == str(b.location_id)
        assert "a.segy" not in str(got[0])
        # Another blob's wait is its own: the first is no reason to stay quiet.
        run(_run_move(w, move_message(other, "cold")))
        assert alerts[1:] == [(w.dataset_id, other.file_id)]
        # A day on it is still going on: a second report, so the alert stays up.
        w.clock.set(w.clock.now() + DAY_AND_A_BIT)
        run(_run_move(w, move_message(b, "cold")))
        assert len(audits(db, b.file_id, "tier.move.refused")) == 2
    finally:
        metrics.tier_held_back = real  # type: ignore[assignment]


def test_a_single_reference_is_never_held(db: psycopg.Connection, policy_db_url: str) -> None:
    """Exactly as before: both ways, in the same tick, on any layout, even with an old
    reference that is on its way out, and with copies failing, since none is needed."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.location SET state = 'deleting', verified = false"
            " WHERE location_id = %s",
            (b.location_id,),
        )
    run(_run_move(w, move_message(a, "cold")))
    run(_run_move(w, move_message(a, "hot")))
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert primary(db, a.file_id)["logical_tier"] == "hot"
    assert audits(db, a.file_id, "tier.move.refused") == []


def test_files_that_agree_move_with_copies_failing(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Exactly as before: no copy needed, none tried, in place for all of them."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    agree(db, w, "warm", a, b)
    run(_run_move(w, move_message(a, "warm")))
    agree(db, w, "cold", a, b)
    run(_run_move(w, move_message(b, "cold")))
    agree(db, w, "archive", a, b)
    run(_run_move(w, move_message(a, "archive")))
    assert assert_true_to_the_blob(db, w, a) == "Archive"
    assert audits(db, b.file_id, "tier.move.refused") == []
    assert w.sim.faults.calls[Op.COPY_OBJECT] == 0


def test_a_recall_for_a_pin_is_never_held(db: psycopg.Connection, policy_db_url: str) -> None:
    """A warmer pin on an archived stub is a recall, not a move: it starts in the
    sweep right after the blob archived, and waits for nothing."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    its_date(db, w, a)
    make_stub(db, b)
    run(_run_move(w, move_message(a, "archive")))
    pin(db, w, b.file_id, "hot")
    w.clock.set(w.clock.now() + MINUTE)
    res = run(_sweep_once(w))
    assert res.moves == [] and len(res.recalls) == 1
    assert run(w.sim.head(a.ref)).native_class == "Archive"


def test_a_warmer_move_waits_too(db: psycopg.Connection, policy_db_url: str) -> None:
    """Warmer or colder is no difference: it is a move for a file that differs."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    run(_run_move(w, move_message(a, "cold")))
    b = dedupe(db, w, a, "b.segy")
    pin(db, w, a.file_id, "cold")
    run(_run_move(w, move_message(b, "hot")))
    assert run(w.sim.head(a.ref)).native_class == "Cold"
    assert w.sim.early_deletion_fees == []
    assert len(held_reports(db, b.file_id)) == 1


def test_a_scheduler_candidate_is_filtered_only_while_it_waits(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A pin's move is let through until its wait is reported, then not at all, then
    again a day on. A pin that agrees with the blob isn't a candidate at all."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, b.file_id, "cold")
    w.clock.set(w.clock.now() + MINUTE)
    assert run(_sweep_once(w)).moves == [(b.file_id, "cold")]
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    moved = w.clock.now()
    assert len(held_reports(db, b.file_id)) == 1
    for n in (2, 3):
        w.clock.set(moved + n * MINUTE)
        assert run(_sweep_once(w)).evaluated == 0
    w.clock.set(moved + DAY_AND_A_BIT)
    assert run(_sweep_once(w)).moves == [(b.file_id, "cold")]
    assert run(w.sim.head(a.ref)).native_class == "Hot"


# ---------------------------------------------------------------------------
# Lock order, and references that appear during a move
# ---------------------------------------------------------------------------


def _is_locked(c: psycopg.Connection, sql_: str, value: int) -> bool:
    try:
        with c.transaction():
            c.execute(sql_ + " FOR UPDATE NOWAIT", (value,))
        return False
    except psycopg.errors.LockNotAvailable:
        return True


LOCK_FILE = "SELECT 1 FROM catalog.file WHERE file_id = %s"
LOCK_LOCATION = "SELECT 1 FROM catalog.location WHERE location_id = %s"


def test_a_blobs_files_are_locked_in_file_order(db: psycopg.Connection, policy_db_url: str) -> None:
    """The blob's first row belongs to the higher file. Taken in row order, two
    files sharing two blobs in opposite orders could deadlock (a row's update
    takes its file too). Blocked on the higher file, a mover must already hold
    the lower one."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    low = store(db, w, "low.segy", data=b"l" * 4096)
    high = store(db, w, "high.segy", data=b"h" * 4096)
    # low's next version, on high's blob: a later row, of a lower file.
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (vid,) = db.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
            " size) VALUES (%s, %s, %s, 'low.segy', 'content', 'pending', 4096)"
            " RETURNING version_id",
            (low.file_id, w.dataset_id, w.root_id),
        ).fetchone()
        db.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
            " backend_version_id, role, state, logical_tier, storage_class, size, committed_at)"
            " SELECT %s, %s, backend_id, container, object_key, backend_version_id, 'primary',"
            " 'committed', logical_tier, storage_class, size, committed_at FROM catalog.location"
            " WHERE location_id = %s",
            (low.file_id, vid, high.location_id),
        )
        db.execute(
            "UPDATE catalog.file_version SET state = 'committed', committed_at = now()"
            " WHERE version_id = %s",
            (vid,),
        )
    assert low.file_id < high.file_id
    agree(db, w, "cold", low, high)

    async def race() -> None:
        holder = await _worker(w)
        try:
            async with holder.transaction():
                await holder.execute(LOCK_FILE + " FOR UPDATE", (high.file_id,))
                worker = asyncio.create_task(_run_move(w, move_message(high, "cold")))
                await asyncio.sleep(0.7)  # the worker reaches the file this holds
                assert await asyncio.to_thread(_probe_file, w, low.file_id)
        finally:
            await holder.close()
        await asyncio.wait_for(worker, 10)

    run(race())
    assert run(w.sim.head(high.ref)).native_class == "Cold"


def _probe_file(w: World, file_id: int) -> bool:
    with psycopg.connect(w.url, autocommit=True) as c:
        return _is_locked(c, LOCK_FILE, file_id)


def _probe_location(w: World, location_id: int) -> bool:
    with psycopg.connect(w.url, autocommit=True) as c:
        return _is_locked(c, LOCK_LOCATION, location_id)


def test_a_blobs_rows_are_locked_in_row_order(db: psycopg.Connection, policy_db_url: str) -> None:
    """The feed takes these rows in `location_id` order too. Blocked on the
    higher row, a mover must already hold the lower one."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    agree(db, w, "cold", b)

    async def race() -> None:
        holder = await _worker(w)
        try:
            async with holder.transaction():
                await holder.execute(LOCK_LOCATION + " FOR UPDATE", (b.location_id,))
                worker = asyncio.create_task(_run_move(w, move_message(a, "cold")))
                await asyncio.sleep(0.7)
                assert await asyncio.to_thread(_probe_location, w, a.location_id)
        finally:
            await holder.close()
        await asyncio.wait_for(worker, 10)

    run(race())
    assert assert_true_to_the_blob(db, w, a) == "Cold"


def test_a_mover_never_holds_a_higher_row_while_waiting_for_a_lower_one(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The feed takes these rows lowest first, so a mover that wrote its own
    higher row and then wanted a lower one could deadlock with it."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    agree(db, w, "cold", a)

    async def race() -> None:
        holder = await _worker(w)
        try:
            async with holder.transaction():
                await holder.execute(LOCK_LOCATION + " FOR UPDATE", (a.location_id,))
                worker = asyncio.create_task(_run_move(w, move_message(b, "cold")))
                await asyncio.sleep(0.7)
                assert not await asyncio.to_thread(_probe_location, w, b.location_id)
        finally:
            await holder.close()
        await asyncio.wait_for(worker, 10)

    run(race())
    assert assert_true_to_the_blob(db, w, a) == "Cold"


def test_a_recall_for_a_pin_isnt_filtered_by_a_reported_hold(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """B's hold was reported, then A archived the blob. B's pin now means a
    recall, which starts at once, not when the hold would have ended."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    its_date(db, w, a)
    run(_run_move(w, move_message(b, "cold")))
    assert len(audits(db, b.file_id, "tier.move.refused")) == 1
    make_stub(db, b)
    run(_run_move(w, move_message(a, "archive")))
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.file SET next_action = 'archive', next_action_at = %s"
            " WHERE file_id = %s",
            (w.clock.now() + DAY, b.file_id),
        )
    pin(db, w, b.file_id, "hot")
    w.clock.set(w.clock.now() + MINUTE)
    assert len(run(_sweep_once(w)).recalls) == 1


def test_a_pin_to_archive_waits_too(db: psycopg.Connection, policy_db_url: str) -> None:
    """C's pin wants Archive, and C can't get its own copy. A pin is a person's choice about
    their own file, so it doesn't take A and B offline: it waits like any other move, and the
    scheduler stays away for the day."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    c = dedupe(db, w, a, "c.segy")
    tick(w)
    pin(db, w, c.file_id, "archive")
    w.clock.set(w.clock.now() + MINUTE)
    assert run(_sweep_once(w)).moves == [(c.file_id, "archive")]
    assert assert_true_to_the_blob(db, w, a) == "Hot"
    assert len(held_reports(db, c.file_id)) == 1
    w.clock.set(w.clock.now() + MINUTE)
    assert run(_sweep_once(w)).moves == []
    for s_ in (a, b):
        assert not [x for x in audits(db, s_.file_id) if x["details"].get("forced")]


def test_a_report_a_day_old_lets_another_pin_try(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Past a day from the report, a new pin on C is looked at, and if the others agree
    with it by then, it moves."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    c = dedupe(db, w, a, "c.segy")
    t0 = w.clock.now()
    run(_run_move(w, move_message(b, "cold")))
    assert len(audits(db, b.file_id, "tier.move.refused")) == 1
    agree(db, w, "cold", a, b)
    w.clock.set(t0 + timedelta(hours=25))
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.file SET next_action = 'archive', next_action_at = %s"
            " WHERE file_id = ANY(%s)",
            (w.clock.now() + timedelta(days=30), [a.file_id, b.file_id, c.file_id]),
        )
    pin(db, w, c.file_id, "cold")
    assert (c.file_id, "cold") in run(_sweep_once(w)).moves
    assert assert_true_to_the_blob(db, w, a) == "Cold"


class _CopiesInPlace:
    """sim with S3's class change: a CopyObject onto itself."""

    kind = BackendKind.S3

    def __init__(self, sim: Any) -> None:
        self._sim = sim

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)

    async def set_class(self, ref: Any, tier: LogicalTier, **_: Any) -> Any:
        return await self._sim.copy_object(ref, ref.container, ref.key, tier)


def test_a_minted_version_on_rows_with_no_version_reaches_them(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Rows with no version id mean the object's current version, which is the
    one the copy just made. So they did change, and must say so."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="s3", copies=True)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.location SET backend_version_id = NULL WHERE location_id = ANY(%s)",
            ([a.location_id, b.location_id],),
        )
    a = Stored(
        a.file_id,
        a.version_id,
        a.location_id,
        type(a.ref)(w.backend_id, a.ref.container, a.ref.key, None),
    )
    registry = BackendRegistry.from_mapping({w.backend_id: _CopiesInPlace(w.sim)})  # type: ignore[dict-item]
    run(_run_move(w, move_message(a, "cold"), registry))
    assert primary(db, a.file_id)["backend_version_id"] is not None
    assert primary(db, b.file_id)["logical_tier"] == "cold"
    assert primary(db, b.file_id)["backend_version_id"] is None
    assert run(w.sim.head(a.ref)).logical_tier is T.COLD


async def _find(w: World, db_wrap: Any = None) -> dict | None:
    url = as_app(w.url, "api")
    async with await psycopg.AsyncConnection.connect(url, row_factory=dict_row) as db:
        return await find_duplicate(db_wrap(db) if db_wrap else db, w.dataset_id, BLAKE3, 4096)


def _with_blake3(db: psycopg.Connection, s: Stored) -> None:
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file SET blake3 = %s WHERE file_id = %s", (BLAKE3, s.file_id))


def test_a_dedupe_doesnt_use_a_blob_being_moved(db: psycopg.Connection, policy_db_url: str) -> None:
    """Its row would copy the old tier, and the mover's update may already have
    run. It uploads its own copy instead; once the move is over it dedupes."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    _with_blake3(db, a)
    paused = _Paused(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]

    async def race() -> None:
        worker = asyncio.create_task(_run_move(w, move_message(a, "cold"), registry))
        await asyncio.wait_for(paused.reached.wait(), 10)
        assert await _find(w) is None
        paused.release.set()
        await asyncio.wait_for(worker, 10)
        got = await _find(w)
        assert got is not None and got["logical_tier"] == "cold"

    run(race())


def test_a_move_waits_for_a_dedupe_in_flight_and_then_sees_its_row(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The dedupe holds the blob's lock shared until it commits, so the mover
    can't start until the new row exists and is written with the rest. A share
    lock on the row wouldn't do: the mover's statement snapshot couldn't see it."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    _with_blake3(db, a)

    async def race() -> None:
        url = as_app(w.url, "api")
        async with await psycopg.AsyncConnection.connect(url, row_factory=dict_row) as api:
            assert await find_duplicate(api, w.dataset_id, BLAKE3, 4096) is not None
            assert await _run_move(w, move_message(a, "cold")) is True  # back to the queue
            await api.rollback()
        assert await _run_move(w, move_message(a, "cold")) is False

    run(race())
    assert run(w.sim.head(a.ref)).native_class == "Cold"


def test_a_dedupe_rereads_the_row_once_it_has_the_lock(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A move can finish between finding the row and taking the lock."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    _with_blake3(db, a)

    class MovesFirst:
        def __init__(self, db: Any) -> None:
            self._db = db

        async def execute(self, sql_: str, *args: Any) -> Any:
            if "pg_try_advisory_xact_lock_shared" in sql_:
                await _run_move(w, move_message(a, "archive"))
            return await self._db.execute(sql_, *args)

    assert run(_find(w, MovesFirst)) is None  # archived now, so not a duplicate


# ---------------------------------------------------------------------------
# The feed against a mover, and a holder that outlasts its visibility timeout
# ---------------------------------------------------------------------------


def test_the_feed_and_a_mover_on_one_blob_cannot_deadlock(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The feed takes a blob's rows, then its files through the update's trigger.
    A mover taking files first would hold what the feed wants and want what the
    feed holds, and Postgres would abort one, the mover after its provider call.
    Here the feed has the rows when the mover goes on, and writes once the mover
    has had time to reach its locks."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    agree(db, w, "cold", dedupe(db, w, a, "b.segy"))
    paused = _Paused(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]
    seen = w.clock.now() + MINUTE

    class WritesLate:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        def __getattr__(self, name: str) -> Any:
            return getattr(self._conn, name)

        async def execute(self, sql_: str, *args: Any) -> Any:
            if sql_.startswith("UPDATE catalog.location") and not paused.release.is_set():
                paused.release.set()
                await asyncio.sleep(0.7)
            return await self._conn.execute(sql_, *args)

    async def race() -> None:
        worker = asyncio.create_task(_run_move(w, move_message(a, "cold"), registry))
        await asyncio.wait_for(paused.reached.wait(), 10)
        feed = await _worker(w)
        try:
            async with feed.transaction():
                await feed.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
                rec = ChangeRecord(ChangeKind.TIER_CHANGED, a.ref, seen, native_class="Cold")
                await apply_record(WritesLate(feed), w.sim, rec, w.clock.now())  # type: ignore[arg-type]
        finally:
            await feed.close()
        await asyncio.wait_for(worker, 30)

    run(race())
    assert assert_true_to_the_blob(db, w, a) == "Cold"


def _consumer(w: World, registry: BackendRegistry, *, vt: int = 60) -> Consumer:
    async def handler(msg: Message, c: psycopg.AsyncConnection) -> None:
        await handle_move(msg, c, registry=registry, now=w.clock.now())

    async def connect() -> psycopg.AsyncConnection:
        return await _worker(w)

    return Consumer(connect, "tiering", handler, ConsumerConfig(visibility_timeout=vt, batch=1))


def _queue_move(db: psycopg.Connection, s: Stored, tier: str, phase: str = "inplace") -> int:
    body = dict(move_message(s, tier, phase).body)
    (mid,) = db.execute("SELECT pgmq.send('tiering'::text, %s::jsonb)", (Jsonb(body),)).fetchone()
    return mid


def test_a_holder_outlasting_its_visibility_timeout_keeps_its_message(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A slow class change runs past the time its message is invisible for, and a
    second consumer reads the message. The blob's lock stops it running the move.
    It only defers the message, promptly, so the holder still finishes it: the
    holder's record stands, blob Cold and row cold, not blob Cold and row hot with
    the move run again, minting another S3 version each time."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    agree(db, w, "cold", b)
    paused = _Paused(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]
    mid = _queue_move(db, a, "cold")

    async def race() -> None:
        holder = _consumer(w, registry, vt=1)
        second = _consumer(w, registry)
        try:
            worker = asyncio.create_task(holder.run_once())
            await asyncio.wait_for(paused.reached.wait(), 10)
            await asyncio.sleep(1.5)  # the holder's visibility timeout lapses
            assert await asyncio.wait_for(second.run_once(), 5) == 1
            assert second.stats.retried == 1 and second.stats.lost == 0
            got = await asyncio.to_thread(_message_row, w, mid)
            assert got is not None and got[0] == 2  # the same message, deferred
            paused.release.set()
            await asyncio.wait_for(worker, 20)
            assert holder.stats.acked == 1 and holder.stats.lost == 0
        finally:
            await holder.close()
            await second.close()

    run(race())
    assert paused.calls == ["cold"]
    assert assert_true_to_the_blob(db, w, a) == "Cold"
    assert primary(db, b.file_id)["logical_tier"] == "cold"
    (left,) = db.execute("SELECT count(*) FROM pgmq.q_tiering").fetchone()
    assert left == 0


def _feed_events(w: World, file_id: int) -> list[int]:
    """What an edge could read now, as `api.feed` reads it, below the horizon. The
    real horizon (`catalog.feed_horizon`) is the oldest open id in the whole
    cluster, so any session on a shared server can hold it back, in any database.
    This one is worked out from this database's sessions alone: another database
    can't hide an event from this test, and a session in this one, the worker's,
    still does."""
    with psycopg.connect(w.url, autocommit=True) as c:
        (horizon,) = c.execute(
            """
            SELECT coalesce(
                (SELECT min(backend_xid::text::bigint) FROM pg_stat_activity
                  WHERE datname = current_database() AND backend_xid IS NOT NULL
                    AND pid <> pg_backend_pid()),
                pg_snapshot_xmax(pg_current_snapshot())::text::bigint)::text
            """
        ).fetchone()
        rows_ = c.execute(
            "SELECT entity_id FROM catalog.feed_read('0'::xid8, 0, %s::xid8, %s, %s, 1000)"
            " WHERE entity = 'file'",
            (horizon, w.edge_id, [w.dataset_id]),
        ).fetchall()
    return [r[0] for r in rows_ if r[0] == file_id]


def _touch_file(w: World, file_id: int) -> None:
    with psycopg.connect(w.url, autocommit=True) as c:
        with c.transaction():
            c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            c.execute("UPDATE catalog.file SET mode = 416 WHERE file_id = %s", (file_id,))


def _xids_held(w: World) -> int:
    with psycopg.connect(w.url, autocommit=True) as c:
        (n,) = c.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            " AND backend_xid IS NOT NULL AND pid <> pg_backend_pid()"
        ).fetchone()
    return n


def test_an_edge_reads_a_feed_event_committed_during_a_provider_call(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The feed's horizon is the oldest open transaction id, for every edge. A
    worker holding one across a provider call would keep every edge's events
    unread until the call ends, so it holds none: not for the blob's lock, the
    row reads, the guard's check, or anything else before the call."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    agree(db, w, "cold", dedupe(db, w, a, "b.segy"))
    other = store(db, w, "other.segy", data=b"o" * 4096)
    paused = _Paused(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]
    _queue_move(db, a, "cold")
    # Another database on this server, holding an id throughout, as a concurrent
    # suite on a shared server does: it must neither fail this test nor pass it.
    params = conninfo_to_dict(w.url)
    params["dbname"] = "postgres"
    elsewhere = psycopg.connect(make_conninfo(**params))
    elsewhere.execute("SELECT pg_current_xact_id()")

    async def race() -> None:
        holder = _consumer(w, registry)
        try:
            worker = asyncio.create_task(holder.run_once())
            await asyncio.wait_for(paused.reached.wait(), 10)
            assert await asyncio.to_thread(_xids_held, w) == 0
            await asyncio.to_thread(_touch_file, w, other.file_id)
            assert await asyncio.to_thread(_feed_events, w, other.file_id), (
                "a feed event committed during the provider call wasn't readable"
            )
            paused.release.set()
            await asyncio.wait_for(worker, 20)
        finally:
            paused.release.set()
            await holder.close()

    try:
        run(race())
    finally:
        elsewhere.close()
    assert assert_true_to_the_blob(db, w, a) == "Cold"


def test_a_move_on_a_busy_blob_is_deferred_with_its_message(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Another file's move of the blob is running. This message stays as it is,
    hidden for a while, and its read count goes on counting towards the bound."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    agree(db, w, "warm", b)
    paused = _Paused(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]
    first = _queue_move(db, a, "warm")

    async def race() -> None:
        holder = _consumer(w, registry)
        other = _consumer(w, registry)
        try:
            worker = asyncio.create_task(holder.run_once())
            await asyncio.wait_for(paused.reached.wait(), 10)
            second = await asyncio.to_thread(_queue_on_a_new_connection, w, b, "cold")
            assert await other.run_once() == 1
            assert other.stats.retried == 1 and other.stats.dead == 0
            got = await asyncio.to_thread(_message_row, w, second)
            assert got is not None and got[0] == 1 and got[1] > timedelta(seconds=20)
            paused.release.set()
            await asyncio.wait_for(worker, 20)
            agree(db, w, "cold", a)
            await asyncio.to_thread(_show_message, w, second)
            assert await other.run_once() == 1
        finally:
            await holder.close()
            await other.close()

    run(race())
    assert first is not None
    assert paused.calls == ["warm", "cold"]
    assert assert_true_to_the_blob(db, w, a) == "Cold"


def _queue_on_a_new_connection(w: World, s: Stored, tier: str) -> int:
    with psycopg.connect(w.url, autocommit=True) as c:
        return _queue_move(c, s, tier)


def _message_row(w: World, msg_id: int) -> tuple | None:
    with psycopg.connect(w.url, autocommit=True) as c:
        return c.execute(
            "SELECT read_ct, vt - now() FROM pgmq.q_tiering WHERE msg_id = %s", (msg_id,)
        ).fetchone()


def _show_message(w: World, msg_id: int, *, read_ct: int | None = None) -> None:
    with psycopg.connect(w.url, autocommit=True) as c:
        c.execute(
            "UPDATE pgmq.q_tiering SET vt = now(), read_ct = coalesce(%s, read_ct)"
            " WHERE msg_id = %s",
            (read_ct, msg_id),
        )


def test_a_move_still_blocked_at_the_read_bound_is_handed_back(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A copy can outlast twenty looks. The other move isn't a failure, so it isn't
    a dead letter: it leaves the queue and its file is looked at again in an hour."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    agree(db, w, "warm", b)
    paused = _Paused(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]
    _queue_move(db, a, "warm")

    async def race() -> None:
        holder = _consumer(w, registry)
        other = _consumer(w, registry)
        try:
            worker = asyncio.create_task(holder.run_once())
            await asyncio.wait_for(paused.reached.wait(), 10)
            second = await asyncio.to_thread(_queue_on_a_new_connection, w, b, "cold")
            max_reads = ConsumerConfig().max_reads
            await asyncio.to_thread(_show_message, w, second, read_ct=max_reads - 1)
            assert await other.run_once() == 1
            assert other.stats.acked == 1 and other.stats.dead == 0
            assert await asyncio.to_thread(_message_row, w, second) is None
            paused.release.set()
            await asyncio.wait_for(worker, 20)
        finally:
            await holder.close()
            await other.close()

    run(race())
    assert paused.calls == ["warm"]
    assert file_row(db, b.file_id)["next_action_at"] == w.clock.now() + timedelta(hours=1)
    (dead,) = db.execute("SELECT count(*) FROM pgmq.q_tiering_dlq").fetchone()
    assert dead == 0


# ---------------------------------------------------------------------------
# What the API says about a held move
# ---------------------------------------------------------------------------


async def _entry(w: World, s: Stored, inherit: tuple[int, ...] = ()) -> dict:
    url = as_app(w.url, "web")
    async with await psycopg.AsyncConnection.connect(url, row_factory=dict_row) as db:
        f = await (
            await db.execute("SELECT * FROM catalog.file WHERE file_id = %s", (s.file_id,))
        ).fetchone()
        [got] = await decorate(db, [f], list(inherit), w.clock.now())
    return got


def _held_world(db: psycopg.Connection, policy_db_url: str) -> tuple[World, Stored, Stored]:
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a-secret.segy")
    b = dedupe(db, w, a, "b.segy")
    return w, a, b


def test_the_api_says_a_pins_move_is_held_and_nothing_false(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """While held, the scheduler's fields only say when it looks again, under a
    label that would read "Moves to Archive". The API gives the hold instead,
    and names no other file. Once the hold is over, it is gone."""
    w, a, b = _held_world(db, policy_db_url)
    t1 = w.clock.now()
    pin(db, w, b.file_id, "cold")
    run(_run_move(w, move_message(b, "cold")))
    assert file_row(db, b.file_id)["next_action"] == "archive"
    got = run(_entry(w, b))
    assert got["move_hold"] == {"to": "cold", "until": rfc3339(t1 + DAY)}
    assert got["next_action"] is None and got["next_action_at"] is None
    assert "secret" not in str(got)
    w.clock.set(t1 + DAY)
    got = run(_entry(w, b))
    assert got["move_hold"] is None
    assert got["next_action"] == "archive"


def test_the_api_follows_a_folders_pin_to_a_held_move(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, _, b = _held_world(db, policy_db_url)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (folder,) = db.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, %s, 'd',"
            " 'dir') RETURNING file_id",
            (w.dataset_id, w.root_id),
        ).fetchone()
    pin(db, w, folder, "cold")
    run(_run_move(w, move_message(b, "cold")))
    assert run(_entry(w, b, (folder,)))["move_hold"]["to"] == "cold"


def test_the_api_shows_no_hold_when_nothing_is_held(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """No pin; a pin to the tier it is in; one reference; an archived blob,
    where a pin means a recall; and a file past its archive date, unless the pin
    overrides it."""
    w, a, b = _held_world(db, policy_db_url)
    assert run(_entry(w, b))["move_hold"] is None
    pin(db, w, b.file_id, "hot")
    assert run(_entry(w, b))["move_hold"] is None
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.tier_pin SET logical_tier = 'cold', override_archive = false"
                   " WHERE file_id = %s", (b.file_id,))  # fmt: skip
    run(_run_move(w, move_message(b, "cold")))
    assert run(_entry(w, b))["move_hold"] is not None
    # The other reference is on its way out: nothing is shared any more.
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.location SET state = 'deleting', verified = false"
                   " WHERE location_id = %s", (a.location_id,))  # fmt: skip
    assert run(_entry(w, b))["move_hold"] is None
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.location SET state = 'committed'"
                   " WHERE location_id = %s", (a.location_id,))  # fmt: skip

    lone = store(db, w, "lone.segy", data=b"l" * 4096)
    run(_run_move(w, move_message(lone, "cold")))
    pin(db, w, lone.file_id, "hot")
    assert run(_entry(w, lone))["move_hold"] is None

    its_date(db, w, a)
    run(_run_move(w, move_message(a, "archive")))
    assert run(_entry(w, b))["move_hold"] is None


def test_the_api_shows_no_hold_without_an_active_tiering_policy(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Under no policy the scheduler looks at nothing, so no move is waiting."""
    w = make_world(db, policy_db_url, policy=None, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    run(_run_move(w, move_message(a, "cold")))
    pin(db, w, b.file_id, "hot")
    assert run(_entry(w, b))["move_hold"] is None


def test_the_api_shows_a_hold_on_the_archive_date_until_its_exact_time(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A pin without `override_archive` wins until the file's exact archive time,
    which can be later on its archive date. Past it the date wins, and no other
    move is waiting."""
    w, a, b = _held_world(db, policy_db_url)
    t1 = w.clock.now()  # noon
    written = t1 - timedelta(days=POLICY["archive_day"]) + timedelta(hours=6)  # archives at 18:00
    pin(db, w, b.file_id, "cold")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.tier_pin SET override_archive = false WHERE file_id = %s",
                   (b.file_id,))  # fmt: skip
        db.execute("UPDATE catalog.file SET written_at = %s, archive_at = %s WHERE file_id = %s",
                   (written, t1.date(), b.file_id))  # fmt: skip
    run(_run_move(w, move_message(b, "cold")))
    assert run(_entry(w, b))["move_hold"] is not None
    w.clock.set(t1 + timedelta(hours=7))
    assert run(_entry(w, b))["move_hold"] is None
    # A pin that overrides the archive date still wants its tier.
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.tier_pin SET override_archive = true WHERE file_id = %s", (b.file_id,)
        )
    assert run(_entry(w, b))["move_hold"] is not None


def test_a_provider_call_that_never_returns_gives_up_the_blob(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a bound it would hold the blob's lock for ever. With one, it is a
    failure like any other: rolled back and retried, and the next attempt works."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    agree(db, w, "cold", b)
    monkeypatch.setattr(tiering, "MOVE_CALL_TIMEOUT", 0.2)

    class NeverReturns:
        def __init__(self, sim: Any) -> None:
            self._sim = sim

        def __getattr__(self, name: str) -> Any:
            return getattr(self._sim, name)

        async def set_class(self, ref: Any, tier: LogicalTier, **_: Any) -> Any:
            await asyncio.Event().wait()

    stuck = BackendRegistry.from_mapping({w.backend_id: NeverReturns(w.sim)})  # type: ignore[dict-item]
    mid = _queue_move(db, a, "cold")

    async def go() -> None:
        c = _consumer(w, stuck)
        try:
            assert await asyncio.wait_for(c.run_once(), 5) == 1
            assert c.stats.failed == 1 and c.stats.acked == 0
        finally:
            await c.close()

    run(go())
    assert primary(db, b.file_id)["logical_tier"] == "hot"
    (left,) = db.execute("SELECT count(*) FROM pgmq.q_tiering").fetchone()
    assert left == 1
    assert mid is not None
    db.execute("UPDATE pgmq.q_tiering SET vt = now()")

    async def again() -> None:
        c = _consumer(w, w.registry)
        try:
            assert await c.run_once() == 1
        finally:
            await c.close()

    run(again())
    assert assert_true_to_the_blob(db, w, a) == "Cold"


def test_the_tiering_consumer_outlasts_its_longest_call(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Its message isn't read again while a live holder works, and nothing waits
    in a batch behind a long copy. The other settings stay the worker's."""
    base = ConsumerConfig(max_failures=7)
    worker = SimpleNamespace(consumer_config=base, registry=None, clock=None, recall_config=None)
    job = next(j for j in tiering_jobs(worker) if getattr(j, "queue", None) == "tiering")  # type: ignore[arg-type]
    assert tiering.MOVE_CALL_TIMEOUT < job.config.visibility_timeout <= 600
    assert tiering.LONG_CALL_TIMEOUT < tiering.LONG_VISIBILITY_SECONDS
    assert job.config.batch == 1
    assert job.config.max_failures == 7


def test_a_deadlock_victim_retries_soon_and_ends_in_agreement(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The mover reaches the provider, then loses a deadlock on the catalog. It
    rolls back, so the blob has moved and the row hasn't. It backs off and is
    retried in seconds, not counted as a failure, and the retry brings the row into
    line with the blob."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    agree(db, w, "cold", b)
    paused = _Paused(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]
    _queue_move(db, a, "cold")

    def block(event_lock: Any, event_go: Any) -> None:
        """Hold a file the mover wants, then want a row the mover holds. Slow to
        notice, so the mover is the one Postgres aborts."""
        with psycopg.connect(w.url) as c:
            c.execute("SET LOCAL deadlock_timeout = '10s'")
            c.execute("SELECT 1 FROM catalog.file WHERE file_id = %s FOR UPDATE", (b.file_id,))
            event_lock.set()
            event_go.wait(10)
            c.execute("SELECT 1 FROM catalog.location WHERE location_id = %s FOR UPDATE",
                      (a.location_id,))  # fmt: skip
            c.rollback()

    async def race() -> None:
        locked, go = threading.Event(), threading.Event()
        holder = _consumer(w, registry)
        other = asyncio.create_task(asyncio.to_thread(block, locked, go))
        try:
            worker = asyncio.create_task(holder.run_once())
            await asyncio.wait_for(paused.reached.wait(), 10)
            await asyncio.to_thread(locked.wait, 10)
            paused.release.set()
            await asyncio.sleep(0.5)  # the mover holds its rows and waits for the file
            go.set()
            assert await asyncio.wait_for(worker, 20) == 1
            await asyncio.wait_for(other, 20)
            assert holder.stats.retried == 1 and holder.stats.dead == 0
            assert holder.stats.acked == 0
            assert await asyncio.to_thread(_hidden_for, w) < timedelta(seconds=60)
        finally:
            go.set()
            paused.release.set()
            await holder.close()

    run(race())
    assert run(w.sim.head(a.ref)).native_class == "Cold"
    assert primary(db, a.file_id)["logical_tier"] == "hot"  # rolled back
    db.execute("UPDATE pgmq.q_tiering SET vt = now()")

    async def again() -> None:
        c = _consumer(w, w.registry)
        try:
            assert await c.run_once() == 1
            assert c.stats.acked == 1
        finally:
            await c.close()

    run(again())
    assert assert_true_to_the_blob(db, w, a) == "Cold"


def test_a_crashed_worker_holds_a_file_up_for_minutes_not_hours(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A deploy or a restart can kill a worker in the middle of any move. Until its
    message shows again the sweep skips the file, so a pin's recall waits too."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    its_date(db, w, a)
    make_stub(db, b)
    run(_run_move(w, move_message(a, "archive")))
    pin(db, w, b.file_id, "hot")
    worker = SimpleNamespace(consumer_config=ConsumerConfig(), registry=None, clock=None,
                             recall_config=None)  # fmt: skip
    job = next(j for j in tiering_jobs(worker) if getattr(j, "queue", None) == "tiering")  # type: ignore[arg-type]
    assert job.config.visibility_timeout <= 600
    # The worker read this message and died.
    mid = _queue_move(db, b, "cold")
    db.execute(
        "SELECT * FROM pgmq.read('tiering'::text, %s::integer, 1)",
        (job.config.visibility_timeout,),
    ).fetchall()
    w.clock.set(w.clock.now() + MINUTE)

    async def sweep_alone() -> scheduler.SweepResult:
        conn = await _worker(w)
        try:
            return await scheduler.sweep(conn, w.clock.now(), dataset_ids=[w.dataset_id])
        finally:
            await conn.close()

    assert run(sweep_alone()).recalls == []  # skipped while the message exists
    # It shows again and finishes, here as a refusal, and the sweep goes on.
    _show_message(w, mid)

    async def after_the_crash() -> None:
        c = _consumer(w, w.registry)
        try:
            assert await c.run_once() == 1
        finally:
            await c.close()

    run(after_the_crash())
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.file SET next_action_at = %s WHERE file_id = %s",
            (w.clock.now(), b.file_id),
        )
    assert len(run(sweep_alone()).recalls) == 1


class _SlowCopy:
    """sim as S3 or Azure with a call that stalls until released."""

    def __init__(self, sim: Any, kind: BackendKind) -> None:
        self._sim = sim
        self.kind = kind
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)

    async def set_class(self, ref: Any, tier: LogicalTier, **kw: Any) -> Any:
        got = await self._sim.set_class(ref, tier, **kw)
        self.reached.set()
        await self.release.wait()
        return got

    async def copy_object(self, *args: Any, **kw: Any) -> Any:
        got = await self._sim.copy_object(*args, **kw)
        self.reached.set()
        await self.release.wait()
        return got


def _hidden_for(w: World) -> timedelta:
    with psycopg.connect(w.url, autocommit=True) as c:
        (left,) = c.execute("SELECT max(vt) - now() FROM pgmq.q_tiering").fetchone()
    return left


def _run_stalled(w: World, slow: _SlowCopy, *, reads: int = 1) -> tuple[timedelta, int]:
    """Run the queue until the call stalls. What's the message's hidden time, and
    how many transaction ids are held, while it is in flight?"""
    registry = BackendRegistry.from_mapping({w.backend_id: slow})  # type: ignore[dict-item]

    async def go() -> tuple[timedelta, int]:
        c = _consumer(w, registry, vt=tiering.MOVE_VISIBILITY_SECONDS)
        try:
            for _ in range(reads - 1):
                await asyncio.wait_for(c.run_once(), 10)
            worker = asyncio.create_task(c.run_once())
            await asyncio.wait_for(slow.reached.wait(), 10)
            hidden = await asyncio.to_thread(_hidden_for, w)
            xids = await asyncio.to_thread(_xids_held, w)
            slow.release.set()
            await asyncio.wait_for(worker, 20)
            assert c.stats.lost == 0
            return hidden, xids
        finally:
            slow.release.set()
            await c.close()

    return run(go())


GB2 = 2 * 1024**3


def test_a_big_s3_class_change_asks_for_more_time_first(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It can run for hours. The longer timeout is committed before the call, so no
    transaction id is held across it, and the move finishes, past the ordinary
    call's bound."""
    monkeypatch.setattr(tiering, "MOVE_CALL_TIMEOUT", 0.001)
    w = make_world(db, policy_db_url, policy=POLICY, layout="s3", copies=True)
    a = store(db, w, "a.segy", size=GB2)
    _queue_move(db, a, "cold")
    hidden, xids = _run_stalled(w, _SlowCopy(w.sim, BackendKind.S3))
    assert hidden > timedelta(hours=6)
    assert xids == 0
    assert primary(db, a.file_id)["logical_tier"] == "cold"
    (left,) = db.execute("SELECT count(*) FROM pgmq.q_tiering").fetchone()
    assert left == 0


def test_a_small_s3_class_change_and_any_set_blob_tier_stay_short(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The long wait is only where it's needed: a crash in these holds the file up
    for minutes."""
    w = make_world(db, policy_db_url, policy=POLICY, layout="s3", copies=True)
    small = store(db, w, "small.segy", size=1024)
    _queue_move(db, small, "cold")
    hidden, _ = _run_stalled(w, _SlowCopy(w.sim, BackendKind.S3))
    assert hidden <= timedelta(seconds=tiering.MOVE_VISIBILITY_SECONDS)

    w2 = make_world(db, policy_db_url, policy=POLICY, layout="azure_worm")
    big = store(db, w2, "big.segy", size=GB2)
    _queue_move(db, big, "cold")
    hidden, _ = _run_stalled(w2, _SlowCopy(w2.sim, BackendKind.AZURE_BLOB))
    assert hidden <= timedelta(seconds=tiering.MOVE_VISIBILITY_SECONDS)


def test_every_archive_copy_asks_for_more_time_and_gets_its_long_bound(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Azure's Copy Blob into the archive account has no time promise at any size.
    A small one past the ordinary bound would fail and start again over a copy
    still pending. It runs past that bound here, and finishes."""
    monkeypatch.setattr(tiering, "MOVE_CALL_TIMEOUT", 0.001)
    w = make_world(db, policy_db_url, policy=POLICY, copies=True)
    small = store(db, w, "small.segy", data=b"s" * 100)
    _queue_move(db, small, "archive", "plan")
    hidden, xids = _run_stalled(w, _SlowCopy(w.sim, BackendKind.AZURE_BLOB), reads=2)
    assert hidden > timedelta(hours=6)
    assert xids == 0
    # The copy is recorded: past the call, on to its read-back.
    (state,) = db.execute(
        "SELECT state FROM catalog.location WHERE version_id = %s AND role = 'secondary'",
        (small.version_id,),
    ).fetchone()
    assert state == "committed"


class _Stuck:
    """sim with one call that never returns."""

    def __init__(self, sim: Any, stuck: str) -> None:
        self._sim = sim
        self._stuck = stuck

    def __getattr__(self, name: str) -> Any:
        if name == self._stuck:

            async def never(*_: Any, **__: Any) -> Any:
                await asyncio.Event().wait()

            return never
        return getattr(self._sim, name)


async def _once(c: Consumer) -> int:
    return await asyncio.wait_for(c.run_once(), 5)


def test_an_archive_copy_that_never_returns_gives_up(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure like any other: rolled back, the pending row left as it was."""
    monkeypatch.setattr(tiering, "LONG_CALL_TIMEOUT", 0.2)
    w = make_world(db, policy_db_url, policy=POLICY)
    a = store(db, w, "a.segy")
    stuck = BackendRegistry.from_mapping({w.backend_id: _Stuck(w.sim, "copy_object")})  # type: ignore[dict-item]
    _queue_move(db, a, "archive", "plan")

    async def go() -> None:
        c = _consumer(w, stuck)
        try:
            assert await _once(c) == 1  # plan
            assert await _once(c) == 1  # the copy
            assert c.stats.failed == 1 and c.stats.acked == 0
        finally:
            await c.close()

    run(go())
    (state,) = db.execute(
        "SELECT state FROM catalog.location WHERE version_id = %s AND role = 'secondary'",
        (a.version_id,),
    ).fetchone()
    assert state == "pending"
    assert primary(db, a.file_id)["container"] == a.ref.container


def test_set_blob_tier_on_the_archive_copy_that_never_returns_gives_up(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tiering, "MOVE_CALL_TIMEOUT", 0.2)
    w = make_world(db, policy_db_url, policy=POLICY, copies=True)
    a = store(db, w, "a.segy")
    stuck = BackendRegistry.from_mapping({w.backend_id: _Stuck(w.sim, "set_class")})  # type: ignore[dict-item]
    _queue_move(db, a, "archive", "plan")

    async def go() -> None:
        c = _consumer(w, stuck)
        try:
            assert await _once(c) == 1  # plan
            assert await _once(c) == 1  # the copy
            await drain_verify(w)
            await asyncio.to_thread(_show_all, w)
            assert await _once(c) == 1  # the read-back is in
            assert c.stats.failed == 0
            assert await _once(c) == 1  # Set Blob Tier on the copy
            assert c.stats.failed == 1 and c.stats.acked == 0
        finally:
            await c.close()

    run(go())
    assert primary(db, a.file_id)["container"] == a.ref.container


def _show_all(w: World) -> None:
    with psycopg.connect(w.url, autocommit=True) as c:
        c.execute("UPDATE pgmq.q_tiering SET vt = now()")
