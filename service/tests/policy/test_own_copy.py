"""Decision 66: a file whose plan differs from the others on its blob gets its own.

These run the real dedupe, scheduler, move consumer, read-back, WORM stamping and
`delete_blob` against Postgres 17 and sim. The only wrappers stop a sim call part-way,
to stand for a crash or to hold a race open. `test_tiering_shared_blob.py` covers what
is left when the copy can't be made.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from omnifile_service import metrics
from omnifile_service.api.data import find_duplicate
from omnifile_service.backends.base import BackendKind, ObjectNotFound, Throttled
from omnifile_service.backends.base import LogicalTier as T
from omnifile_service.backends.sim.faults import CopyFault, Op
from omnifile_service.catalog.privileges import as_app
from omnifile_service.policy import scheduler
from omnifile_service.policy.engine import (
    FileFacts,
    Pin,
    old_version_wants_same_tier,
    same_tier_wanted,
)
from omnifile_service.policy.model import Policy
from omnifile_service.reconcile.run import reconcile_container
from omnifile_service.retention.worm_sync import retire_replaced_secondaries, stamp_worm
from omnifile_service.worker import portal_jobs, tiering
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.queue import ConsumerConfig

from .support import (
    Stored,
    World,
    drain_retention,
    drain_verify,
    drain_work,
    make_world,
    primary,
    row,
    run,
    store,
    tick,
)
from .test_tiering_shared_blob import (
    _CopiesInPlace,
    _dedupe,
    _drain,
    _entry,
    _follow,
    _hidden_for,
    _sweep_once,
    _worker,
    _xids_held,
    audits,
    blob_moves,
    dedupe,
    dedupe_with,
    file_row,
    held_reports,
    make_stub,
    overwrite,
    pin,
)

POLICY = {"mode": "scheduled", "warm_day": 30, "cold_day": 90, "archive_day": 180}
SMART = {"mode": "smart", "archive_day": 180}
DAY = timedelta(days=1)
MINUTE = timedelta(minutes=1)
WORM_SHARE = {"layout": "azure_worm", "worm_days": 3650}
IN_PLACE = {"single_account": {"layout": "azure_worm"}, "worm_share": WORM_SHARE}
PARSED = Policy.from_body(POLICY)
T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)


def copies(w: World) -> int:
    return w.sim.faults.calls[Op.COPY_OBJECT]


def advance_to(w: World, n: float) -> None:
    w.clock.set(w.day(n))


def world(db: psycopg.Connection, url: str, **kw: Any) -> World:
    return make_world(db, url, **{"policy": POLICY, "layout": "azure_worm", **kw})


def reachable(db: psycopg.Connection, w: World, since: datetime | None = None) -> None:
    """Edges have heartbeated without a break since `since`, up to now (migration 0040)."""
    with db.transaction():
        db.execute(
            "INSERT INTO catalog.heartbeat_window (id, started_at, last_at) VALUES (true, %s, %s)"
            " ON CONFLICT (id) DO UPDATE SET started_at = excluded.started_at,"
            " last_at = excluded.last_at",
            (since or w.t0 - DAY, w.clock.now()),
        )


def retained(db: psycopg.Connection, s: Stored) -> bool:
    (until,) = db.execute(
        "SELECT retention_until FROM catalog.file_version WHERE version_id = %s", (s.version_id,)
    ).fetchone()
    return until is not None


def read(w: World, loc: dict) -> bytes:
    ref = _ref(loc)
    return run(_drain(w.sim.get_range(ref, 0, int(loc["size"]))))


def _ref(loc: dict) -> Any:
    from omnifile_service.backends.base import ObjectRef

    return ObjectRef(loc["backend_id"], loc["container"], loc["object_key"],
                     loc["backend_version_id"])  # fmt: skip


def locations(db: psycopg.Connection, file_id: int) -> list[dict]:
    cur = db.execute(
        "SELECT * FROM catalog.location WHERE file_id = %s ORDER BY location_id", (file_id,)
    )
    return [dict(zip([d.name for d in cur.description], r, strict=True)) for r in cur.fetchall()]


def split_pair(db: psycopg.Connection, url: str, **kw: Any) -> tuple[World, Stored, Stored]:
    """A Hot blob with two references, and a third day of no plan to move."""
    w = world(db, url, **kw)
    a = store(db, w, "a.segy")
    tick(w)
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    return w, a, b


def same_bytes(db: psycopg.Connection, blake3: bytes, *stored: Stored) -> None:
    """What the upload path records, which `store` leaves out."""
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        for s in stored:
            db.execute(
                "UPDATE catalog.file_version SET blake3 = %s WHERE version_id = %s",
                (blake3, s.version_id),
            )
            db.execute(
                "UPDATE catalog.file SET blake3 = %s WHERE file_id = %s", (blake3, s.file_id)
            )


async def _steps(w: World, n: int, registry: BackendRegistry | None = None) -> None:
    """`n` consumer reads, one message each: each phase of a move."""
    from .test_tiering_shared_blob import _consumer

    c = _consumer(w, registry or w.registry)
    try:
        for _ in range(n):
            await c.run_once()
    finally:
        await c.close()


def steps(w: World, n: int, registry: BackendRegistry | None = None) -> None:
    run(_steps(w, n, registry))


def show_queue(db: psycopg.Connection) -> None:
    db.execute("UPDATE pgmq.q_tiering SET vt = now() WHERE vt > now()")


def queue_move(db: psycopg.Connection, s: Stored, tier: str) -> int:
    body = {"kind": "move", "file_id": str(s.file_id), "version_id": str(s.version_id),
            "location_id": str(s.location_id), "tier": tier, "phase": "inplace"}  # fmt: skip
    (mid,) = db.execute("SELECT pgmq.send('tiering'::text, %s::jsonb)", (Jsonb(body),)).fetchone()
    return mid


def tiering_queue(db: psycopg.Connection) -> list[dict]:
    return [r[0] for r in db.execute("SELECT message FROM pgmq.q_tiering ORDER BY msg_id")]


# ---------------------------------------------------------------------------
# The rule: when do two references want the same tier?
# ---------------------------------------------------------------------------


def facts(written: datetime, tier: T = T.HOT, **kw: Any) -> FileFacts:
    return FileFacts(written_at=written, tier=tier, **kw)


COOL_DAY = T0 + timedelta(days=30)


@pytest.mark.parametrize(
    ("younger_by", "same"),
    [
        (timedelta(0), True),  # the same age: it wants Cool now
        (timedelta(days=3), True),
        (timedelta(days=7), True),  # the edge of the window
        (timedelta(days=7, seconds=1), False),
        (timedelta(days=60), False),
    ],
)
def test_a_colder_step_is_shared_within_a_week(younger_by: timedelta, same: bool) -> None:
    b = facts(T0 + younger_by)
    assert same_tier_wanted(PARSED, b, None, COOL_DAY, T.WARM) is same


def test_reaching_a_warmer_tier_within_the_window_isnt_enough() -> None:
    """B reaches Cool within the week, not Cold, which is what A wants."""
    b = facts(T0 + timedelta(days=85), T.HOT)
    assert not same_tier_wanted(PARSED, b, None, T0 + timedelta(days=90), T.COLD)


def test_a_pin_says_what_it_wants_now_however_soon_it_ends() -> None:
    soon = Pin(T.HOT, until=COOL_DAY + DAY)
    assert not same_tier_wanted(PARSED, facts(T0), soon, COOL_DAY, T.WARM)
    # Gone, it is no pin at all.
    assert same_tier_wanted(PARSED, facts(T0), Pin(T.HOT, until=COOL_DAY), COOL_DAY, T.WARM)


def test_a_pin_to_the_same_tier_is_the_same_wish() -> None:
    assert same_tier_wanted(PARSED, facts(T0), Pin(T.COLD), T0 + DAY, T.COLD)
    assert same_tier_wanted(PARSED, facts(T0, T.COLD), Pin(T.HOT), T0 + DAY, T.HOT)


def test_a_warmer_move_has_no_window() -> None:
    """The schedule never warms, so only the same pin wants a warmer tier."""
    assert not same_tier_wanted(PARSED, facts(T0 + DAY, T.COLD), None, T0 + DAY, T.HOT)


def test_a_file_wanting_a_colder_tier_now_differs() -> None:
    """A wants Cold, and B is past its archive date: each moves on its own, from the
    warmer tier, so neither reads from Cold."""
    b = facts(T0, T.HOT)
    assert not same_tier_wanted(PARSED, b, None, T0 + timedelta(days=180), T.COLD)


@pytest.mark.parametrize(
    ("hours", "same"), [(0, True), (6, True), (11, True), (12, False), (24, False)]
)
def test_archive_is_shared_only_on_the_files_own_date(hours: int, same: bool) -> None:
    """A file is never archived on a date before its own: T0 is noon, so a file up to
    12 hours younger has the same date."""
    day = T0 + timedelta(days=180)
    b = facts(T0 + timedelta(hours=hours), T.COLD)
    assert same_tier_wanted(PARSED, b, None, day, T.ARCHIVE) is same


def test_a_date_passed_is_a_wish_for_archive() -> None:
    day = T0 + timedelta(days=200)
    assert same_tier_wanted(PARSED, facts(T0, T.COLD), Pin(T.HOT), day, T.ARCHIVE)
    held = Pin(T.HOT, override_archive=True)
    assert not same_tier_wanted(PARSED, facts(T0, T.COLD), held, day, T.ARCHIVE)


@pytest.mark.parametrize(
    ("age", "target", "same"),
    [
        (30, T.WARM, True),
        (25, T.WARM, True),  # 5 days off
        (22, T.WARM, False),  # 8 days off
        (31, T.COLD, False),
        (180, T.WARM, False),  # it wants Archive now
    ],
)
def test_an_old_version_follows_the_same_rule(age: int, target: T, same: bool) -> None:
    now = T0 + timedelta(days=age)
    assert old_version_wants_same_tier(PARSED, T0, T.HOT, now, target) is same


def test_an_old_version_is_never_archived_before_its_date() -> None:
    day = T0 + timedelta(days=180)
    assert old_version_wants_same_tier(PARSED, T0, T.COLD, day, T.ARCHIVE)
    young = T0 + DAY
    assert not old_version_wants_same_tier(PARSED, young, T.COLD, day, T.ARCHIVE)


def test_an_old_version_stays_as_cold_as_it_is() -> None:
    """Old versions only go colder: a Cold one wants nothing warmer."""
    assert old_version_wants_same_tier(PARSED, T0, T.COLD, T0 + DAY, T.COLD)
    assert not old_version_wants_same_tier(PARSED, T0, T.COLD, T0 + DAY, T.HOT)


# ---------------------------------------------------------------------------
# The split
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout", IN_PLACE.values(), ids=IN_PLACE.keys())
def test_a_warmer_pin_leaves_with_a_copy_and_the_blob_stays(
    db: psycopg.Connection, policy_db_url: str, layout: dict
) -> None:
    """B's pin wants Hot, and A's schedule keeps the blob in Cool. B gets its own blob.
    Only B's row changes. A's blob, tier and clock don't, so no early deletion."""
    w = world(db, policy_db_url, **layout)
    a = store(db, w, "a.segy", data=b"seismic" * 500)
    tick(w)
    advance_to(w, 31)
    tick(w)
    was = primary(db, a.file_id)
    assert was["logical_tier"] == "warm"
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, b.file_id, "hot")
    tick(w)

    assert copies(w) == 1
    assert w.sim.early_deletion_fees == []
    # A: nothing happened to it.
    after = primary(db, a.file_id)
    assert after == was
    assert audits(db, a.file_id) == audits(db, a.file_id)[:1]  # its own move to Cool only
    assert run(w.sim.head(a.ref)).native_class == "Cool"
    # B: its own blob, Hot, read back in full, the same bytes.
    mine = primary(db, b.file_id)
    assert mine["object_key"] != a.ref.key
    assert (mine["logical_tier"], mine["storage_class"]) == ("hot", "Hot")
    assert mine["verified"] and mine["verified_by"] == "azure_read_crc"
    assert run(w.sim.head(_ref(mine))).native_class == "Hot"
    assert read(w, mine) == b"seismic" * 500
    assert mine["crc64nvme"] == was["crc64nvme"]
    # Never asked again.
    advance_to(w, 32)
    assert tick(w).moves == []
    assert copies(w) == 1


def test_a_pin_gone_before_its_move_runs_makes_no_copy(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A copy costs money, and the message may be old. Look once more first, and let
    the scheduler decide afresh."""
    w, a, b = split_pair(db, policy_db_url)
    queue_move(db, a, "cold")  # queued for a pin released since: there's none now
    steps(w, 1)
    assert copies(w) == 0
    assert [x for x in locations(db, a.file_id) if x["role"] == "secondary"] == []
    assert tiering_queue(db) == []
    assert primary(db, a.file_id)["logical_tier"] == "hot"
    (at,) = db.execute("SELECT next_action_at FROM catalog.file WHERE file_id = %s",
                       (a.file_id,)).fetchone()  # fmt: skip
    assert at == w.clock.now()


def test_only_a_plain_blob_is_made_in_the_datasets_own_area(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The name is the file's and version's, as its upload would have been: never a
    path, and nobody else's file."""
    w, a, b = split_pair(db, policy_db_url)
    db.execute("UPDATE catalog.dataset SET key_prefix = 'projects/x/' WHERE dataset_id = %s",
               (w.dataset_id,))  # fmt: skip
    pin(db, w, a.file_id, "cold")
    tick(w)
    mine = primary(db, a.file_id)
    assert mine["object_key"].startswith(f"projects/x/{a.file_id}/{a.version_id}.own-")
    assert mine["container"] == a.ref.container


def test_a_colder_wish_leaves_from_hot_with_no_retrieval(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A's schedule says Cool, and B is pinned Hot. A leaves. The blob stays Hot for B
    all along, and the copy reads Hot."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, b.file_id, "hot")
    advance_to(w, 30)
    res = tick(w)
    assert (a.file_id, "warm") in res.moves
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert primary(db, b.file_id)["object_key"] == a.ref.key
    assert primary(db, b.file_id)["logical_tier"] == "hot"
    assert primary(db, a.file_id)["logical_tier"] == "warm"
    assert run(w.sim.head(_ref(primary(db, a.file_id)))).native_class == "Cool"
    assert drain_retention(w) == ["shared"]
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert w.sim.early_deletion_fees == []
    assert blob_moves(db, w) == 0


def test_the_file_with_nothing_left_to_share_moves_in_place(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A Cool blob, and nobody stays: A wants Cold and B wants Hot. Whoever moves first
    copies, and the other then has the blob to itself."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 31)
    tick(w)
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    advance_to(w, 95)  # the blob's minimum has passed
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "hot")
    tick(w)
    assert copies(w) == 1
    assert primary(db, a.file_id)["logical_tier"] == "cold"
    assert primary(db, b.file_id)["logical_tier"] == "hot"
    assert primary(db, a.file_id)["object_key"] != primary(db, b.file_id)["object_key"]
    assert w.sim.early_deletion_fees == []


def test_files_that_want_the_same_tier_move_together_with_no_copy(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "cold")
    tick(w)
    assert copies(w) == 0
    assert run(w.sim.head(a.ref)).native_class == "Cold"
    assert primary(db, a.file_id)["object_key"] == primary(db, b.file_id)["object_key"]
    assert primary(db, b.file_id)["logical_tier"] == "cold"


def test_files_that_want_the_same_warmer_tier_arent_held(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The hold is for a disagreement. Two pins that agree can't loop."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "cold")
    tick(w)
    assert run(w.sim.head(a.ref)).native_class == "Cold"
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.tier_pin SET logical_tier = 'hot' WHERE file_id = ANY(%s)",
                   ([a.file_id, b.file_id],))  # fmt: skip
    w.clock.set(w.clock.now() + MINUTE)
    tick(w)
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert copies(w) == 0


@pytest.mark.parametrize(
    ("second_at", "expect_copies"),
    [(25, 0), (22, 1)],
    ids=["younger_by_5_days", "younger_by_8_days"],
)
def test_the_same_schedule_a_little_apart_doesnt_copy(
    db: psycopg.Connection, policy_db_url: str, second_at: float, expect_copies: int
) -> None:
    """On the rule's own terms, end to end: the day A reaches Cool, B is 5 days short,
    so it goes along, 5 days early. At 8 days short, A gets its own."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 30 - second_at)
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    advance_to(w, 30)
    tick(w)
    assert copies(w) == expect_copies
    assert primary(db, a.file_id)["logical_tier"] == "warm"
    assert primary(db, b.file_id)["logical_tier"] == ("warm" if expect_copies == 0 else "hot")
    if not expect_copies:
        # B doesn't go back, and doesn't move again until Cold.
        advance_to(w, 35)
        assert tick(w).moves == []


@pytest.mark.parametrize(
    ("hours_younger", "expect_copies"), [(1, 0), (25, 1)], ids=["same_date", "next_date"]
)
def test_archive_on_an_in_place_layout_keeps_each_files_date(
    db: psycopg.Connection, policy_db_url: str, hours_younger: int, expect_copies: int
) -> None:
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    w.clock.set(w.clock.now() + timedelta(hours=hours_younger))
    b = dedupe(db, w, a, "b.segy")
    w.clock.set(w.t0 + timedelta(days=180))
    tick(w)
    if not expect_copies:
        assert copies(w) == 0
        assert run(w.sim.head(a.ref)).native_class == "Archive"
        assert primary(db, b.file_id)["logical_tier"] == "archive"
    else:
        # A is archived on its date on a blob of its own. B's stays online until its own.
        assert primary(db, a.file_id)["logical_tier"] == "archive"
        assert primary(db, b.file_id)["logical_tier"] != "archive"
        assert run(w.sim.head(a.ref)).native_class != "Archive"
        w.clock.set(w.clock.now() + timedelta(days=1, hours=2))
        tick(w)
        assert primary(db, b.file_id)["logical_tier"] == "archive"


def test_an_archive_date_with_a_pin_against_it_splits_and_the_pin_stays_true(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """At origin/main A's date archived the blob under B's hot pin."""
    w = world(db, policy_db_url)
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
    assert primary(db, a.file_id)["logical_tier"] == "archive"
    assert primary(db, b.file_id)["logical_tier"] == "hot"
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert res.recalls == []  # B's bytes are online: no recall was needed


def test_an_old_version_of_an_overwritten_file_leaves_too(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The one-pin loop: `sweep_versions` pushes the old version colder, and the pin
    wants Hot. The old version gets its own blob."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    tick(w)
    b = dedupe(db, w, a, "b.segy")
    pin(db, w, b.file_id, "hot")
    tick(w)
    overwrite(db, w, a, "a.segy")
    advance_to(w, 40)
    tick(w)
    assert copies(w) == 1
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    old = [x for x in locations(db, a.file_id) if x["version_id"] == a.version_id]
    live = [x for x in old if x["state"] == "committed" and x["role"] == "primary"]
    assert [x["logical_tier"] for x in live] == ["warm"]
    assert live[0]["object_key"] != a.ref.key
    assert primary(db, b.file_id)["object_key"] == a.ref.key


def test_a_deleted_files_last_version_has_a_plan_too(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """`sweep_versions` tiers it from its commit. It differs from B's pin, and leaves."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, b.file_id, "hot")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file SET deleted_at = %s WHERE file_id = %s",
                   (w.clock.now(), a.file_id))  # fmt: skip
    advance_to(w, 40)
    tick(w)
    assert copies(w) == 1
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert primary(db, b.file_id)["object_key"] == a.ref.key


def test_a_folder_pin_is_a_files_wish(db: psycopg.Connection, policy_db_url: str) -> None:
    """Pins on the folder above, as `FACTS_READ_SQL` follows them."""
    w = world(db, policy_db_url)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (folder,) = db.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, %s, 'hot',"
            " 'dir') RETURNING file_id",
            (w.dataset_id, w.root_id),
        ).fetchone()
    a = store(db, w, "a.segy")
    tick(w)
    fid, vid, lid = run(_dedupe(w, a, "b.segy", folder))
    pin(db, w, folder, "hot")
    tick(w)
    # A's schedule says Cool: B's pin, from its folder, says Hot.
    advance_to(w, 30)
    tick(w)
    assert copies(w) == 1
    assert primary(db, a.file_id)["logical_tier"] == "warm"
    assert primary(db, a.file_id)["object_key"] != a.ref.key
    assert primary(db, fid)["object_key"] == a.ref.key
    assert primary(db, fid)["logical_tier"] == "hot"


def test_an_old_version_on_the_blob_answers_for_itself(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A's pin is about A's current version, on its own blob by now. A's old version on
    the shared blob has only its schedule, which agrees with B's: no copy."""
    w, a, b = split_pair(db, policy_db_url)
    overwrite(db, w, a, "a.segy")
    pin(db, w, a.file_id, "hot")
    advance_to(w, 30)
    tick(w)
    assert copies(w) == 0
    assert run(w.sim.head(a.ref)).native_class == "Cool"
    assert primary(db, b.file_id)["object_key"] == a.ref.key


def test_a_deleted_files_pin_is_no_wish(db: psycopg.Connection, policy_db_url: str) -> None:
    """Its last version goes by its schedule, as `sweep_versions` moves it, whatever pin
    was left on the file: so it agrees with B, with no copy."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "hot")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file SET deleted_at = %s WHERE file_id = %s",
                   (w.clock.now(), a.file_id))  # fmt: skip
    advance_to(w, 30)
    tick(w)
    assert copies(w) == 0
    assert run(w.sim.head(a.ref)).native_class == "Cool"


def test_a_reference_with_no_plan_needs_no_copy(db: psycopg.Connection, policy_db_url: str) -> None:
    """A row in a dataset with no tiering policy wants nothing: the blob moves."""
    w = world(db, policy_db_url)
    other = make_world(db, policy_db_url, layout="azure_worm", policy=None)
    a = store(db, w, "a.segy")
    b = store(db, other, "elsewhere.segy", key="elsewhere.segy")
    tick(w)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.location SET backend_id = %s, container = %s, object_key = %s,"
            " backend_version_id = %s WHERE location_id = %s",
            (a.ref.backend_id, a.ref.container, a.ref.key, a.ref.version_id, b.location_id),
        )
    pin(db, w, a.file_id, "cold")
    tick(w)
    assert copies(w) == 0
    assert run(w.sim.head(a.ref)).native_class == "Cold"
    assert primary(db, b.file_id)["logical_tier"] == "cold"


def test_a_reference_whose_policy_is_broken_needs_no_copy(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = split_pair(db, policy_db_url)
    other = make_world(db, policy_db_url, layout="azure_worm", policy={"archive_day": -1})
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file SET dataset_id = %s, parent_id = %s WHERE file_id = %s",
                   (other.dataset_id, other.root_id, b.file_id))  # fmt: skip
    queue_move(db, a, "cold")
    run(_drain_work(w))
    assert copies(w) == 0
    assert run(w.sim.head(a.ref)).native_class == "Cold"


async def _drain_work(w: World) -> int:
    conn = await _worker(w)
    try:
        return await drain_work(conn, w, w.clock.now())
    finally:
        await conn.close()


def test_a_smart_reference_stays_smart(db: psycopg.Connection, policy_db_url: str) -> None:
    """On two accounts: B's pin to Hot would have ended Smart for good for both. Now B
    leaves, with an explicit tier, and A's blob is still Smart, never explicit."""
    w = make_world(db, policy_db_url, policy=SMART)
    a = store(db, w, "a.segy")
    tick(w)
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, b.file_id, "hot")
    tick(w)
    assert copies(w) == 1
    theirs = primary(db, a.file_id)
    assert (theirs["logical_tier"], theirs["explicit_tier_set"]) == ("smart", False)
    assert run(w.sim.head(a.ref)).native_class == "Smart"
    mine = primary(db, b.file_id)
    assert (mine["logical_tier"], mine["explicit_tier_set"]) == ("hot", True)
    assert mine["container"] == a.ref.container


def test_two_accounts_cold_step_splits_in_the_live_account(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w = make_world(db, policy_db_url, policy=POLICY)
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 80)
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, b.file_id, "hot")
    advance_to(w, 90)
    tick(w)
    assert primary(db, a.file_id)["logical_tier"] == "cold"
    assert primary(db, a.file_id)["container"] == a.ref.container
    assert primary(db, a.file_id)["object_key"] != a.ref.key
    assert primary(db, b.file_id)["logical_tier"] == "hot"


def test_s3s_class_change_is_its_own_copy_already(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A new version, in the new class. Nothing else to make."""
    w = world(db, policy_db_url, layout="s3")
    a = store(db, w, "a.segy")
    dedupe(db, w, a, "b.segy")

    class S3(_CopiesInPlace):
        kind = BackendKind.S3

    registry = BackendRegistry.from_mapping({w.backend_id: S3(w.sim)})  # type: ignore[dict-item]
    queue_move(db, a, "cold")
    steps(w, 1, registry)
    assert [x for x in locations(db, a.file_id) if x["role"] == "secondary"] == []
    assert not any("own_copy" in m for m in tiering_queue(db))
    assert primary(db, a.file_id)["logical_tier"] == "cold"


# ---------------------------------------------------------------------------
# Safety: the original stays, and nothing another reference needs goes
# ---------------------------------------------------------------------------


def test_the_original_is_the_record_until_the_copy_is_checked(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Step by step: a pending row, then a Hot copy waiting for its read-back. Only
    once checked does it get its tier, and only then the record."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    (mid,) = [queue_move(db, a, "cold")]
    assert mid
    steps(w, 1)  # the decision
    [t] = [x for x in locations(db, a.file_id) if x["role"] == "secondary"]
    assert t["state"] == "pending"
    assert primary(db, a.file_id)["object_key"] == a.ref.key
    assert copies(w) == 0
    steps(w, 1)  # the copy
    t = row(db, "SELECT * FROM catalog.location WHERE location_id = %s", t["location_id"])
    assert (t["state"], t["verified"], t["logical_tier"]) == ("committed", False, "hot")
    assert run(w.sim.head(_ref(t))).native_class == "Hot"
    show_queue(db)
    steps(w, 1)  # verifying: not read back, so it waits
    assert primary(db, a.file_id)["object_key"] == a.ref.key
    assert primary(db, a.file_id)["state"] == "committed"
    run(drain_verify(w))
    show_queue(db)
    steps(w, 2)  # read back: its tier
    t = row(db, "SELECT * FROM catalog.location WHERE location_id = %s", t["location_id"])
    assert (t["verified"], t["logical_tier"], t["role"]) == (True, "cold", "secondary")
    assert primary(db, a.file_id)["object_key"] == a.ref.key
    steps(w, 1)  # the record
    assert primary(db, a.file_id)["location_id"] == t["location_id"]
    assert primary(db, b.file_id)["object_key"] == a.ref.key
    (at,) = db.execute("SELECT next_action_at FROM catalog.file WHERE file_id = %s",
                       (a.file_id,)).fetchone()  # fmt: skip
    assert at == w.clock.now()  # looked at again, as after any move


@pytest.mark.parametrize(
    ("fault", "detail"),
    [(CopyFault.CORRUPT, "verify_failed"), (CopyFault.PARTIAL, "copy_length_mismatch")],
)
def test_a_bad_copy_is_dropped_and_the_original_stands(
    db: psycopg.Connection, policy_db_url: str, fault: CopyFault, detail: str
) -> None:
    w, a, b = split_pair(db, policy_db_url)
    w.sim.faults.corrupt_next_copy(fault)
    pin(db, w, a.file_id, "cold")
    tick(w)
    [why] = [x["details"] for x in audits(db, a.file_id, "tier.move.refused")
             if x["details"]["why"] == "own_copy_failed"]  # fmt: skip
    assert why["detail"] == detail
    assert why["to"] == "cold"
    # Nothing moved and nothing was lost: Cold is no date, so it tries again in a day.
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert len(held_reports(db, a.file_id)) == 1
    assert primary(db, a.file_id)["object_key"] == a.ref.key
    assert primary(db, b.file_id)["object_key"] == a.ref.key
    bad = [x for x in locations(db, a.file_id) if x["role"] == "secondary"]
    assert [x["state"] for x in bad] == ["deleting"]
    assert drain_retention(w) == ["deleted"]
    assert read(w, primary(db, b.file_id)) == b"x" * 4096


def test_a_blob_with_no_crc_to_check_against_isnt_copied(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = split_pair(db, policy_db_url)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.location SET verified = false, crc64nvme = NULL WHERE location_id = %s",
            (a.location_id,),
        )
    pin(db, w, a.file_id, "cold")
    tick(w)
    assert copies(w) == 0
    assert audits(db, a.file_id, "tier.move.refused")[0]["details"]["detail"] == "no_crc64"
    assert run(w.sim.head(a.ref)).native_class == "Hot"  # it waits
    assert len(held_reports(db, a.file_id)) == 1
    assert file_row(db, a.file_id)["next_action_at"] == w.clock.now() + DAY


def test_the_last_reference_gone_frees_the_original(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """`delete_blob`'s check: the first delete keeps the bytes for B. When B leaves
    too, the original has nobody, and goes."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    tick(w)
    assert drain_retention(w) == ["shared"]
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert read(w, primary(db, b.file_id)) == b"x" * 4096
    # B has it alone now: no more copies, and it moves in place.
    pin(db, w, b.file_id, "warm")
    tick(w)
    assert copies(w) == 1
    assert run(w.sim.head(a.ref)).native_class == "Cool"
    assert drain_retention(w) == []


def test_a_blob_in_archive_is_never_copied(db: psycopg.Connection, policy_db_url: str) -> None:
    """A copy from Archive is a rehydrate, which no policy step starts. A person's pin
    on a stub gets its bytes through a recall, as it always has."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    make_stub(db, b)
    tick(w)
    w.clock.set(w.t0 + timedelta(days=180))
    tick(w)
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    n = copies(w)
    restores = w.sim.faults.calls[Op.RESTORE]
    pin(db, w, b.file_id, "hot")
    res = tick(w)
    assert copies(w) == n
    assert res.moves == []
    assert len(res.recalls) == 1
    assert w.sim.faults.calls[Op.RESTORE] == restores  # the recall's queue, not this path
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert primary(db, b.file_id)["logical_tier"] == "archive"


def test_a_row_behind_a_blob_in_archive_starts_no_rehydrate(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The catalog says Hot and the blob is in Archive, because the feed is behind. The
    copy is refused, and nothing asks for a rehydrate: not this step, not the fallback."""
    w, a, b = split_pair(db, policy_db_url)
    run(w.sim.set_class(a.ref, T.ARCHIVE))
    pin(db, w, a.file_id, "cold")
    calls = w.sim.faults.calls[Op.SET_CLASS]
    tick(w)
    assert w.sim.faults.calls[Op.RESTORE] == 0
    assert w.sim.faults.calls[Op.SET_CLASS] == calls
    assert audits(db, a.file_id, "tier.move.refused")[0]["details"]["why"] == "source_archived"
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert primary(db, b.file_id)["object_key"] == a.ref.key


def test_an_annexed_file_stays_on_the_nas_side(db: psycopg.Connection, policy_db_url: str) -> None:
    """Decision 51: a split is between blobs. The file stays `remote`, with nothing
    resident, and no instruction goes to any edge."""
    w, a, b = split_pair(db, policy_db_url)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.file SET origin = 'annex', state = 'remote', resident_bytes = 0"
            " WHERE file_id = %s",
            (a.file_id,),
        )
    (before,) = db.execute("SELECT count(*) FROM pgmq.q_edge_instructions").fetchone()
    pin(db, w, a.file_id, "cold")
    tick(w)
    got = row(db, "SELECT origin, state, resident_bytes FROM catalog.file WHERE file_id = %s",
              a.file_id)  # fmt: skip
    assert got == {"origin": "annex", "state": "remote", "resident_bytes": 0}
    assert primary(db, a.file_id)["logical_tier"] == "cold"
    (after,) = db.execute(
        "SELECT count(*) FROM pgmq.q_edge_instructions WHERE message->>'file_id' = %s",
        (str(a.file_id),),
    ).fetchone()
    assert after == 0 and before >= 0


# ---------------------------------------------------------------------------
# WORM (decision 31)
# ---------------------------------------------------------------------------


def worm_pair(db: psycopg.Connection, url: str) -> tuple[World, Stored, Stored]:
    w, a, b = split_pair(db, url, **WORM_SHARE)
    assert retained(db, a) and retained(db, b)
    return w, a, b


def test_a_worm_copy_carries_retention_and_is_locked_before_it_is_the_record(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The version's retention is on the new blob, its row stamped, before the swap.
    The old row stays, as a retained secondary, since nothing of a retained version is
    deleted before retention ends. Its blob and bytes stay for the other file too."""
    w, a, b = worm_pair(db, policy_db_url)
    pin(db, w, b.file_id, "cold")
    tick(w)
    (until,) = db.execute(
        "SELECT retention_until FROM catalog.file_version WHERE version_id = %s", (b.version_id,)
    ).fetchone()
    mine = primary(db, b.file_id)
    assert mine["object_key"] != a.ref.key
    head = run(w.sim.head(_ref(mine)))
    assert head.retention_until == until
    assert head.native_class == "Cold"
    assert mine["worm_sync_needed"] is False
    assert mine["worm_stamped_until"] == until
    tags = run(w.sim.get_tags(_ref(mine)))
    assert tags["omnifile_worm"] in ("locked", "unlocked")
    # The old row is a secondary of the same version, still committed, and no delete
    # was queued for it.
    old = row(db, "SELECT * FROM catalog.location WHERE location_id = %s", b.location_id)
    assert (old["role"], old["state"]) == ("secondary", "committed")
    assert drain_retention(w) == []
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    # A file's tier and reads go by the primary.
    assert primary(db, a.file_id)["object_key"] == a.ref.key
    assert primary(db, a.file_id)["logical_tier"] == "hot"


def test_a_legal_hold_is_carried_over(db: psycopg.Connection, policy_db_url: str) -> None:
    w, a, b = worm_pair(db, policy_db_url)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.file_version SET legal_hold = true WHERE version_id = %s",
            (b.version_id,),
        )
    pin(db, w, b.file_id, "cold")
    tick(w)
    mine = primary(db, b.file_id)
    assert mine["object_key"] != a.ref.key
    assert run(w.sim.head(_ref(mine))).legal_hold is True
    assert mine["worm_stamped_hold"] is True


def test_a_hold_set_during_the_copy_is_stamped_before_the_swap(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = worm_pair(db, policy_db_url)
    queue_move(db, b, "cold")
    pin(db, w, b.file_id, "cold")
    steps(w, 2)  # the decision, the copy
    run(drain_verify(w))
    show_queue(db)
    steps(w, 2)  # verifying, its tier
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.file_version SET legal_hold = true WHERE version_id = %s",
            (b.version_id,),
        )
    run(_drain_work(w))
    mine = primary(db, b.file_id)
    assert mine["object_key"] != a.ref.key
    assert run(w.sim.head(_ref(mine))).legal_hold is True


def test_no_swap_until_the_copy_carries_retention(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Set Retention goes on failing: the file stays on its old copy, and the move waits.
    When Set Retention works again, the next try finishes with the same copy."""
    w, a, b = worm_pair(db, policy_db_url)
    w.sim.faults.outage(w.clock.now() - DAY, w.clock.now() + DAY, ops={Op.SET_RETENTION})
    pin(db, w, b.file_id, "cold")
    tick(w)
    mine = primary(db, b.file_id)
    assert mine["object_key"] == a.ref.key
    assert mine["location_id"] == b.location_id
    [t] = [x for x in locations(db, b.file_id) if x["role"] == "secondary"]
    assert t["worm_sync_needed"] is True and t["verified"]
    assert t["worm_stamped_until"] is None
    why = audits(db, b.file_id, "tier.move.refused")[0]
    assert (why["details"]["why"], why["details"]["detail"]) == ("own_copy_failed", "no_retention")
    assert run(w.sim.head(a.ref)).native_class == "Hot"  # it waits
    assert copies(w) == 1
    # Later, warmer: the same copy, carrying its retention at last.
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.tier_pin SET logical_tier = 'warm' WHERE file_id = %s",
                   (b.file_id,))  # fmt: skip
    w.clock.set(w.clock.now() + 2 * DAY)
    tick(w)
    mine = primary(db, b.file_id)
    assert mine["location_id"] == t["location_id"]
    assert mine["logical_tier"] == "warm"
    assert mine["worm_stamped_until"] is not None
    assert copies(w) == 1


def test_a_source_gone_during_a_split_is_a_person_s_to_look_at(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The bytes the other file needs are gone: nothing to fall back to."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    run(w.sim.delete(a.ref))
    tick(w)
    (dead,) = db.execute(
        "SELECT count(*) FROM pgmq.q_tiering_dlq WHERE message->'message'->>'file_id' = %s",
        (str(a.file_id),),
    ).fetchone()
    assert dead >= 1
    assert primary(db, a.file_id)["object_key"] == a.ref.key


def test_a_source_gone_with_a_copy_there_keeps_the_copy(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Never read back, so never the record. But it may be all that's left, so nothing
    drops it either: a dead letter, and a person looks."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 1)
    [t] = [x for x in locations(db, a.file_id) if x["role"] == "secondary"]
    run(w.sim.copy_object(a.ref, t["container"], t["object_key"], T.COLD))
    run(w.sim.delete(a.ref))
    steps(w, 1)  # it has its answer, and raises
    t = row(db, "SELECT * FROM catalog.location WHERE location_id = %s", t["location_id"])
    assert (t["state"], t["role"]) == ("pending", "secondary")
    assert primary(db, a.file_id)["object_key"] == a.ref.key
    (dead,) = db.execute(
        "SELECT count(*) FROM pgmq.q_tiering_dlq WHERE message->'message'->>'file_id' = %s",
        (str(a.file_id),),
    ).fetchone()
    assert dead == 1


def test_a_stamp_that_fails_once_is_tried_again(db: psycopg.Connection, policy_db_url: str) -> None:
    w, a, b = worm_pair(db, policy_db_url)
    w.sim.faults.fail_nth(Op.SET_RETENTION, 1)
    pin(db, w, b.file_id, "cold")
    tick(w)
    mine = primary(db, b.file_id)
    assert mine["object_key"] != a.ref.key
    assert mine["worm_stamped_until"] is not None


def test_a_bad_copy_of_a_retained_version_stays_unused(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Nothing of a retained version is deleted, so the bad copy stays a secondary. It
    is never the record and never used again: the next attempt makes a new one."""
    w, a, b = worm_pair(db, policy_db_url)
    w.sim.faults.corrupt_next_copy(CopyFault.CORRUPT)
    pin(db, w, b.file_id, "cold")
    tick(w)
    bad = [x for x in locations(db, b.file_id)
           if x["role"] == "secondary" and x["state"] == "committed"]  # fmt: skip
    assert len(bad) == 1 and not bad[0]["verified"]
    assert primary(db, b.file_id)["object_key"] == a.ref.key
    assert drain_retention(w) == []
    # It waits. A day later B leaves, on a new copy.
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    w.clock.set(w.clock.now() + DAY + MINUTE)
    tick(w)
    mine = primary(db, b.file_id)
    assert mine["object_key"] not in (a.ref.key, bad[0]["object_key"])
    assert mine["logical_tier"] == "cold"


def test_a_retained_secondary_is_no_reference(db: psycopg.Connection, policy_db_url: str) -> None:
    """After a split on a WORM share the old row stays on the blob, as a secondary. It
    has no plan, so it asks for no copy and holds nothing back, in the worker, the
    scheduler or the API. It does follow the blob, so it stays true."""
    w, a, b = worm_pair(db, policy_db_url)
    pin(db, w, b.file_id, "cold")
    tick(w)
    n = copies(w)
    pin(db, w, a.file_id, "warm")
    tick(w)
    assert run(w.sim.head(a.ref)).native_class == "Cool"
    assert copies(w) == n
    old = row(db, "SELECT * FROM catalog.location WHERE location_id = %s", b.location_id)
    assert (old["role"], old["logical_tier"]) == ("secondary", "warm")
    # Warmer, a minute later: no hold, as nothing else has a plan for the blob.
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.tier_pin SET logical_tier = 'hot' WHERE file_id = %s",
                   (a.file_id,))  # fmt: skip
    w.clock.set(w.clock.now() + MINUTE)
    run(_sweep_once(w))
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert copies(w) == n
    assert held_reports(db, a.file_id) == []
    assert run(_entry(w, a))["move_hold"] is None


def test_a_hold_reported_earlier_ends_when_only_a_secondary_is_left(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """B's move waited and was reported, and then A left. Nothing else has a plan for the
    blob now, so the report no longer stands in the scheduler, the worker or the API."""
    w, a, b = worm_pair(db, policy_db_url)
    w.sim.faults.outage(w.clock.now() - DAY, w.clock.now() + 2 * MINUTE, ops={Op.COPY_OBJECT})
    pin(db, w, b.file_id, "cold")
    tick(w)
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert len(held_reports(db, b.file_id)) == 1
    assert run(_entry(w, b))["move_hold"] is not None
    # Copies work again. A's pin leaves the blob as a retained secondary. It reads Hot.
    pin(db, w, a.file_id, "warm")
    w.clock.set(w.clock.now() + 3 * MINUTE)
    tick_only_a(db, w, a)
    assert primary(db, a.file_id)["object_key"] != a.ref.key
    assert row(db, "SELECT role FROM catalog.location WHERE location_id = %s",
               a.location_id) == {"role": "secondary"}  # fmt: skip
    assert run(_entry(w, b))["move_hold"] is None
    w.clock.set(w.clock.now() + MINUTE)
    run(_sweep_once(w))
    assert run(w.sim.head(a.ref)).native_class == "Cold"
    assert primary(db, b.file_id)["logical_tier"] == "cold"


def tick_only_a(db: psycopg.Connection, w: World, a: Stored) -> None:
    queue_move(db, a, "warm")
    run(_drain_work(w))


def test_worm_files_dont_join(db: psycopg.Connection, policy_db_url: str) -> None:
    """A new reference on a blob whose retention is earlier or later than its version's
    couldn't be stamped true, so each retained file makes its own copy."""
    w, a, b = worm_pair(db, policy_db_url)
    c = dedupe(db, w, a, "c.segy")
    same_bytes(db, b"\x09" * 32, a, b, c)
    pin(db, w, b.file_id, "cold")
    pin(db, w, c.file_id, "cold")
    tick(w)
    assert copies(w) == 2
    assert primary(db, b.file_id)["object_key"] != primary(db, c.file_id)["object_key"]


# ---------------------------------------------------------------------------
# Joining: one blob per wish
# ---------------------------------------------------------------------------


def three_of_a_kind(db: psycopg.Connection, url: str) -> tuple[World, Stored, Stored, Stored]:
    w, a, b = split_pair(db, url)
    c = dedupe(db, w, a, "c.segy")
    same_bytes(db, b"\x09" * 32, a, b, c)
    return w, a, b, c


def test_files_that_agree_share_their_new_blob(db: psycopg.Connection, policy_db_url: str) -> None:
    """A and B want Cold and C is pinned Hot: one copy, not two. The second joins it."""
    w, a, b, c = three_of_a_kind(db, policy_db_url)
    pin(db, w, c.file_id, "hot")
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "cold")
    tick(w)
    assert copies(w) == 1
    mine, yours = primary(db, a.file_id), primary(db, b.file_id)
    assert (mine["object_key"], mine["logical_tier"]) == (yours["object_key"], "cold")
    assert yours["verified"]
    assert run(w.sim.head(_ref(yours))).native_class == "Cold"
    assert primary(db, c.file_id)["object_key"] == a.ref.key
    assert sorted(drain_retention(w)) == ["shared", "shared"]
    assert read(w, primary(db, c.file_id)) == b"x" * 4096
    joined = [x["details"] for x in audits(db, b.file_id) if x["details"].get("joined")]
    assert len(joined) == 1
    assert joined[0]["to"] == "cold"


def test_a_joiner_takes_the_blobs_clock_and_is_looked_at_again(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A later join: the minimum-duration clock is the blob's, not the join's."""
    w, a, b, c = three_of_a_kind(db, policy_db_url)
    pin(db, w, c.file_id, "hot")
    pin(db, w, a.file_id, "cold")
    tick(w)
    was = primary(db, a.file_id)["tier_changed_at"]
    w.clock.set(w.clock.now() + timedelta(hours=1))
    pin(db, w, b.file_id, "cold")
    queue_move(db, b, "cold")
    steps(w, 1)
    yours = primary(db, b.file_id)
    assert yours["object_key"] == primary(db, a.file_id)["object_key"]
    assert yours["tier_changed_at"] == was
    (at,) = db.execute("SELECT next_action_at FROM catalog.file WHERE file_id = %s",
                       (b.file_id,)).fetchone()  # fmt: skip
    assert at == w.clock.now()


def test_a_held_file_doesnt_join_and_an_unheld_one_may_join_a_held_files_blob(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A retained version keeps every copy, so it can't retire its row to join. The
    other way round is safe: the blob stays longer, and that is all."""
    w, a, b, c = three_of_a_kind(db, policy_db_url)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file_version SET legal_hold = true WHERE version_id = %s",
                   (a.version_id,))  # fmt: skip
    pin(db, w, c.file_id, "hot")
    pin(db, w, b.file_id, "cold")
    tick(w)
    assert copies(w) == 1
    # A is held, and B's copy is there: A makes its own.
    pin(db, w, a.file_id, "cold")
    tick(w)
    assert copies(w) == 2
    assert primary(db, a.file_id)["object_key"] != primary(db, b.file_id)["object_key"]
    assert row(db, "SELECT role, state FROM catalog.location WHERE location_id = %s",
               a.location_id) == {"role": "secondary", "state": "committed"}  # fmt: skip
    # D isn't held, and A's blob, with a hold on it, is one it may join.
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file SET blake3 = NULL WHERE file_id = %s", (b.file_id,))
    d = dedupe_with(db, w, c, "d.segy", blake3=b"\x0a" * 32)
    same_bytes(db, b"\x09" * 32, c, d)
    pin(db, w, d.file_id, "cold")
    tick(w)
    assert copies(w) == 2
    assert primary(db, d.file_id)["object_key"] == primary(db, a.file_id)["object_key"]


def test_it_doesnt_join_other_bytes_or_another_tier(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b, c = three_of_a_kind(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    pin(db, w, c.file_id, "hot")
    tick(w)
    assert copies(w) == 1
    # Another tier: a new blob.
    pin(db, w, b.file_id, "warm")
    tick(w)
    assert copies(w) == 2
    assert primary(db, b.file_id)["object_key"] != primary(db, a.file_id)["object_key"]


def test_it_doesnt_join_a_blob_whose_bytes_arent_the_same(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b, c = three_of_a_kind(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    pin(db, w, c.file_id, "hot")
    tick(w)
    db.execute("UPDATE catalog.file SET blake3 = %s WHERE file_id = %s",
               (b"\x08" * 32, a.file_id))  # fmt: skip
    pin(db, w, b.file_id, "cold")
    tick(w)
    assert copies(w) == 2


def test_a_blob_being_moved_isnt_joined(db: psycopg.Connection, policy_db_url: str) -> None:
    """Its lock is held for a move: this file copies, which costs only that copy."""
    w, a, b, c = three_of_a_kind(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    pin(db, w, c.file_id, "hot")
    tick(w)
    key = primary(db, a.file_id)["object_key"]
    hold = psycopg.connect(policy_db_url, autocommit=False)
    try:
        hold.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"omnifile:blob-tier:{w.backend_id}/{a.ref.container}/{key}",),
        )
        pin(db, w, b.file_id, "cold")
        tick(w)
    finally:
        hold.rollback()
        hold.close()
    assert copies(w) == 2
    assert primary(db, b.file_id)["object_key"] != key


# ---------------------------------------------------------------------------
# Concurrency and crashes
# ---------------------------------------------------------------------------


def test_two_workers_deciding_at_once_make_one_copy(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A Cool blob, A wanting Cold and B Hot, each message on its own worker at the same
    moment. One wins the blob's lock and copies. The other defers, or sees the split
    running, and when it's done has the blob to itself."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 31)
    tick(w)
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    advance_to(w, 95)
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "hot")
    queue_move(db, a, "cold")
    queue_move(db, b, "hot")

    async def both() -> None:
        from .test_tiering_shared_blob import _consumer

        one, two = _consumer(w, w.registry), _consumer(w, w.registry)
        try:
            await asyncio.gather(one.run_once(), two.run_once())
        finally:
            await one.close()
            await two.close()

    run(both())
    running = [m for m in tiering_queue(db) if "own_copy" in m]
    assert len(running) == 1  # one split, and one message waiting behind it
    assert len(tiering_queue(db)) == 2
    tick(w)
    assert copies(w) == 1
    assert primary(db, a.file_id)["logical_tier"] == "cold"
    assert primary(db, b.file_id)["logical_tier"] == "hot"
    assert primary(db, a.file_id)["object_key"] != primary(db, b.file_id)["object_key"]
    drain_retention(w)
    assert read(w, primary(db, a.file_id)) == read(w, primary(db, b.file_id))


def test_a_move_of_the_blob_waits_for_a_split_under_way(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A long copy of the blob is running, with no lock held. Another reference's move
    doesn't change the blob under it."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "warm")
    queue_move(db, a, "cold")
    steps(w, 1)  # A's decision: the split is on
    calls = w.sim.faults.calls[Op.SET_CLASS]
    b_msg = queue_move(db, b, "warm")
    # A's copy comes first in the queue; hide it, as if it were running.
    db.execute("UPDATE pgmq.q_tiering SET vt = now() + interval '1 hour' WHERE msg_id <> %s",
               (b_msg,))  # fmt: skip
    steps(w, 1)
    assert w.sim.faults.calls[Op.SET_CLASS] == calls
    (read_ct,) = db.execute("SELECT read_ct FROM pgmq.q_tiering WHERE msg_id = %s",
                            (b_msg,)).fetchone()  # fmt: skip
    assert read_ct == 1  # still there: deferred, not lost
    assert primary(db, b.file_id)["logical_tier"] == "hot"


def test_a_move_waiting_on_a_split_is_handed_back_at_its_bound(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 1)
    b_msg = queue_move(db, b, "warm")
    db.execute("UPDATE pgmq.q_tiering SET vt = now() + interval '1 hour' WHERE msg_id <> %s",
               (b_msg,))  # fmt: skip
    db.execute("UPDATE pgmq.q_tiering SET read_ct = %s WHERE msg_id = %s",
               (ConsumerConfig.max_reads, b_msg))  # fmt: skip
    steps(w, 1)
    assert db.execute("SELECT 1 FROM pgmq.q_tiering WHERE msg_id = %s", (b_msg,)).fetchone() is None
    (at,) = db.execute("SELECT next_action_at FROM catalog.file WHERE file_id = %s",
                       (b.file_id,)).fetchone()  # fmt: skip
    assert at == w.clock.now() + tiering.BLOB_BUSY_HANDED_BACK


class _Crash(BaseException):
    """The worker dies: nothing after this runs, and the message stays hidden."""


class _CrashAround:
    """sim whose copy dies either before or after doing the copy."""

    def __init__(self, sim: Any, *, after: bool) -> None:
        self._sim = sim
        self._after = after
        self.armed = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)

    async def copy_object(self, *args: Any, **kw: Any) -> Any:
        if self.armed and not self._after:
            self.armed = False
            raise _Crash
        got = await self._sim.copy_object(*args, **kw)
        if self.armed:
            self.armed = False
            raise _Crash
        return got


@pytest.mark.parametrize(
    ("after", "expect"), [(True, 1), (False, 1)], ids=["copy_done", "copy_never_ran"]
)
def test_a_crash_during_the_copy_reuses_its_row_and_its_bytes(
    db: psycopg.Connection, policy_db_url: str, after: bool, expect: int
) -> None:
    """The message comes back after its timeout. A copy that got there is adopted, and
    read back as any other, with no second copy made."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 1)
    [t] = [x for x in locations(db, a.file_id) if x["role"] == "secondary"]
    crash = _CrashAround(w.sim, after=after)
    registry = BackendRegistry.from_mapping({w.backend_id: crash})  # type: ignore[dict-item]
    with pytest.raises(_Crash):
        steps(w, 1, registry)
    assert primary(db, a.file_id)["object_key"] == a.ref.key
    show_queue(db)
    run(_drain_work(w))
    assert copies(w) == expect
    mine = primary(db, a.file_id)
    assert mine["location_id"] == t["location_id"]
    assert mine["verified"] and read(w, mine) == b"x" * 4096
    assert [x for x in locations(db, a.file_id)
            if x["role"] == "secondary" and x["state"] != "deleting"] == []  # fmt: skip
    assert primary(db, b.file_id)["object_key"] == a.ref.key


@pytest.mark.parametrize("phase", ["pending", "copied", "checked"])
def test_a_lost_message_leaves_a_row_the_next_move_finishes(
    db: psycopg.Connection, policy_db_url: str, phase: str
) -> None:
    """The message is gone, say dead-lettered, at any point. The next sweep asks again,
    and that move picks up the row left behind: no second row, and no second copy."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, {"pending": 1, "copied": 2, "checked": 2}[phase])
    if phase == "checked":
        run(drain_verify(w))
    db.execute("DELETE FROM pgmq.q_tiering")
    n = copies(w)
    tick(w)
    rows = [x for x in locations(db, a.file_id) if x["state"] != "deleting"]
    assert [x["role"] for x in rows] == ["primary"]
    assert copies(w) == (n + 1 if phase == "pending" else n)
    assert primary(db, a.file_id)["logical_tier"] == "cold"
    assert primary(db, a.file_id)["object_key"] != a.ref.key


def test_a_blob_other_rows_use_isnt_taken_for_a_copy_left_behind(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A secondary of this version under its own-copy name, with another file on it. Its
    tier is that file's now, so this move makes a new copy."""
    w, a, b = split_pair(db, policy_db_url)
    key = f"{a.file_id}/{a.version_id}.own-earlier"
    ref = run(w.sim.copy_object(a.ref, a.ref.container, key, T.HOT))
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
            " object_key, backend_version_id, role, state, logical_tier, storage_class, size,"
            " crc64nvme, committed_at) SELECT %s, %s, backend_id, container, %s, %s, 'secondary',"
            " 'committed', 'hot', 'Hot', size, crc64nvme, %s FROM catalog.location"
            " WHERE location_id = %s",
            (a.file_id, a.version_id, key, ref.version_id, w.clock.now(), a.location_id),
        )
        c = store(db, w, "c.segy", data=b"c" * 4096)
        db.execute(
            "UPDATE catalog.location SET container = %s, object_key = %s, backend_version_id = %s"
            " WHERE location_id = %s",
            (ref.container, key, ref.version_id, c.location_id),
        )
    pin(db, w, a.file_id, "cold")
    tick(w)
    assert primary(db, a.file_id)["object_key"] not in (a.ref.key, key)
    assert run(w.sim.head(ref)).native_class == "Hot"
    assert primary(db, c.file_id)["object_key"] == key


def test_a_commit_for_a_copy_not_through_its_checks_changes_nothing(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A stale or replayed message: never the record, unread or in the wrong tier."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)  # the decision, the copy: not read back
    [message] = tiering_queue(db)
    db.execute("DELETE FROM pgmq.q_tiering")
    db.execute("SELECT pgmq.send('tiering'::text, %s::jsonb)",
               (Jsonb({**message, "phase": "commit"}),))  # fmt: skip
    steps(w, 1)
    assert primary(db, a.file_id)["object_key"] == a.ref.key
    assert tiering_queue(db) == []
    run(drain_verify(w))  # read back, but still in Hot, not the tier it wants
    db.execute("SELECT pgmq.send('tiering'::text, %s::jsonb)",
               (Jsonb({**message, "phase": "commit"}),))  # fmt: skip
    steps(w, 1)
    assert primary(db, a.file_id)["object_key"] == a.ref.key


def test_a_hold_set_just_before_the_swap_is_stamped_first(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the first look and the blob's lock. The swap looks again under the lock,
    comes back, stamps and only then swaps."""
    w, a, c = worm_pair(db, policy_db_url)
    pin(db, w, c.file_id, "warm")
    queue_move(db, c, "warm")
    steps(w, 2)
    run(drain_verify(w))
    show_queue(db)
    steps(w, 2)
    [t] = [x for x in locations(db, c.file_id) if x["role"] == "secondary"]
    # Its retention is on it before the commit's first look, so that look passes.
    run(_stamp(w, t["location_id"]))
    real = tiering.blob_lock_key

    def hold_lands(*args: Any) -> str:
        monkeypatch.setattr(tiering, "blob_lock_key", real)
        with psycopg.connect(policy_db_url, autocommit=True) as other:
            other.execute("SELECT set_config('omnifile.actor', 'system:test', false)")
            other.execute("UPDATE catalog.file_version SET legal_hold = true WHERE version_id = %s",
                          (c.version_id,))  # fmt: skip
        return real(*args)

    monkeypatch.setattr(tiering, "blob_lock_key", hold_lands)
    steps(w, 1)
    assert primary(db, c.file_id)["object_key"] == a.ref.key  # not swapped
    assert run(w.sim.head(_ref(t))).legal_hold is False
    show_queue(db)
    steps(w, 1)
    assert run(w.sim.head(_ref(t))).legal_hold is True
    assert primary(db, c.file_id)["location_id"] == t["location_id"]


async def _stamp(w: World, location_id: int) -> None:
    conn = await _worker(w)
    try:
        await stamp_worm(conn, w.registry, w.clock.now(), only=[location_id])
    finally:
        await conn.close()


def test_a_dedupe_during_a_split_attaches_to_the_original(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """C lands on the blob the copy is reading: that blob is the only one it can see.
    A's new blob isn't verified yet and isn't a primary. Afterwards C still reads, its
    row is true, and A's delete sees C and keeps the bytes."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "hot")
    queue_move(db, a, "cold")
    steps(w, 2)  # the decision, the copy
    fid, vid, lid = run(_dedupe(w, b, "c.segy", w.root_id))
    c = Stored(fid, vid, lid, a.ref)
    assert primary(db, fid)["object_key"] == a.ref.key
    show_queue(db)
    run(_drain_work(w))
    assert primary(db, a.file_id)["object_key"] != a.ref.key
    assert primary(db, c.file_id)["object_key"] == a.ref.key
    assert drain_retention(w) == ["shared"]
    assert read(w, primary(db, c.file_id)) == b"x" * 4096
    assert run(w.sim.head(a.ref)).native_class == primary(db, c.file_id)["storage_class"]


def test_a_dedupe_after_a_split_finds_the_right_blob(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """`find_duplicate` never sees A's old row, which is on its way out, and its
    new row has the same bytes."""
    w, a, b = split_pair(db, policy_db_url)
    same_bytes(db, b"\x07" * 32, a, b)
    pin(db, w, a.file_id, "cold")
    tick(w)
    db.execute("UPDATE catalog.file SET blake3 = NULL WHERE file_id = %s", (b.file_id,))

    async def find() -> dict | None:
        from psycopg.rows import dict_row

        url = as_app(w.url, "api")
        async with await psycopg.AsyncConnection.connect(url, row_factory=dict_row) as conn:
            return await find_duplicate(conn, w.dataset_id, b"\x07" * 32, 4096)

    got = run(find())
    assert got is not None
    assert got["location_id"] == primary(db, a.file_id)["location_id"]
    assert read(w, got) == b"x" * 4096


def test_the_swap_waits_for_a_dedupe_in_flight(db: psycopg.Connection, policy_db_url: str) -> None:
    """A dedupe holds the blob's lock shared. The swap comes back shortly, changed
    nothing, and then goes through."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    run(drain_verify(w))
    show_queue(db)
    steps(w, 2)  # verifying, its tier
    hold = psycopg.connect(policy_db_url, autocommit=False)
    try:
        hold.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
            (f"omnifile:blob-tier:{w.backend_id}/{a.ref.container}/{a.ref.key}",),
        )
        steps(w, 1)
        assert primary(db, a.file_id)["object_key"] == a.ref.key
        assert [m["phase"] for m in tiering_queue(db)] == ["commit"]
    finally:
        hold.rollback()
        hold.close()
    show_queue(db)
    steps(w, 1)
    assert primary(db, a.file_id)["object_key"] != a.ref.key


def test_a_replayed_swap_changes_nothing(db: psycopg.Connection, policy_db_url: str) -> None:
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    run(drain_verify(w))
    show_queue(db)
    steps(w, 2)
    [message] = tiering_queue(db)
    steps(w, 1)
    moved = primary(db, a.file_id)
    n = len(audits(db, a.file_id))
    db.execute("SELECT pgmq.send('tiering'::text, %s::jsonb)", (Jsonb(message),))
    steps(w, 1)
    assert primary(db, a.file_id) == moved
    assert len(audits(db, a.file_id)) == n


def test_the_feed_after_a_split_leaves_every_row_true(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The copy's Created and Tier Changed records, and the original's, each find
    their own rows."""
    w, a, b = split_pair(db, policy_db_url)
    run(_follow(w, a.ref.container, first=True))
    pin(db, w, a.file_id, "cold")
    tick(w)
    drain_retention(w)
    run(_follow(w, a.ref.container))
    assert primary(db, a.file_id)["logical_tier"] == "cold"
    assert primary(db, a.file_id)["storage_class"] == "Cold"
    assert primary(db, b.file_id)["logical_tier"] == "hot"
    assert primary(db, b.file_id)["storage_class"] == "Hot"


def test_a_split_leaves_nothing_for_reconcile_to_find(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Both blobs are known, in the tiers their rows say: no orphan and no mismatch."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    tick(w)
    drain_retention(w)

    async def go() -> Any:
        conn = await _worker(w)
        try:
            return await reconcile_container(
                conn, w.registry, w.backend_id, a.ref.container, w.clock.now()
            )
        finally:
            await conn.close()

    w.clock.set(w.clock.now() + timedelta(hours=2))
    report = run(go())
    assert report.drift_open == [], report
    (found,) = db.execute(
        "SELECT count(*) FROM catalog.reconcile_drift WHERE location_id = ANY(%s)",
        ([primary(db, a.file_id)["location_id"], primary(db, b.file_id)["location_id"]],),
    ).fetchone()
    assert found == 0


def test_a_file_overwritten_during_its_copy_still_moves_the_old_version(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """`_live` is still true: a superseded version is tiered too, on its own blob now."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    overwrite(db, w, a, "a.segy")
    show_queue(db)
    run(_drain_work(w))
    old = [x for x in locations(db, a.file_id)
           if x["version_id"] == a.version_id and x["role"] == "primary"
           and x["state"] == "committed"]  # fmt: skip
    assert len(old) == 1
    assert old[0]["object_key"] != a.ref.key
    assert old[0]["logical_tier"] == "cold"


def test_a_pruned_version_drops_its_copy(db: psycopg.Connection, policy_db_url: str) -> None:
    """The version went while its copy ran: nothing to move, and the copy goes."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    run(drain_verify(w))
    show_queue(db)
    steps(w, 2)
    [t] = [x for x in locations(db, a.file_id) if x["role"] == "secondary"]
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file_version SET state = 'pruned' WHERE version_id = %s",
                   (a.version_id,))  # fmt: skip
    steps(w, 1)
    t = row(db, "SELECT * FROM catalog.location WHERE location_id = %s", t["location_id"])
    assert t["state"] == "deleting"


def test_a_long_copy_asks_for_more_time_and_holds_no_id(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A split's copy has no more of a time promise than an archive copy's."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")

    from .test_tiering_shared_blob import _consumer, _SlowCopy

    slow = _SlowCopy(w.sim, BackendKind.AZURE_BLOB)
    registry = BackendRegistry.from_mapping({w.backend_id: slow})  # type: ignore[dict-item]

    async def go() -> tuple[timedelta, int]:
        c = _consumer(w, registry, vt=tiering.MOVE_VISIBILITY_SECONDS)
        try:
            await asyncio.wait_for(c.run_once(), 10)  # the decision
            worker = asyncio.create_task(c.run_once())
            await asyncio.wait_for(slow.reached.wait(), 10)
            hidden = await asyncio.to_thread(_hidden_for, w)
            xids = await asyncio.to_thread(_xids_held, w)
            slow.release.set()
            await asyncio.wait_for(worker, 20)
            return hidden, xids
        finally:
            slow.release.set()
            await c.close()

    hidden, xids = run(go())
    assert hidden > timedelta(hours=6)
    assert xids == 0


# ---------------------------------------------------------------------------
# When the copy can't be made: the fallback
# ---------------------------------------------------------------------------


def no_copies_for(w: World, how_long: timedelta) -> None:
    w.sim.faults.outage(w.clock.now() - DAY, w.clock.now() + how_long, ops={Op.COPY_OBJECT})


def test_a_copy_that_cant_be_made_is_tried_three_times_then_waits(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Only a move to Archive may move a blob over a file that differs. This one waits a
    day and tries again. The audit says why, under the file's own dataset, naming no other
    file, and nothing else has changed."""
    w, a, b = split_pair(db, policy_db_url)
    no_copies_for(w, timedelta(days=36500))
    pin(db, w, a.file_id, "cold")
    tick(w)
    assert copies(w) == 3
    why, waits = audits(db, a.file_id, "tier.move.refused")
    assert why["details"]["why"] == "own_copy_failed"
    assert why["details"]["detail"] == "Unavailable"
    assert why["dataset_id"] == w.dataset_id
    assert waits["details"]["why"] == "held_back_shared_blob"
    assert waits["details"]["to"] == "cold"
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert primary(db, a.file_id)["logical_tier"] == "hot"
    assert primary(db, b.file_id)["logical_tier"] == "hot"
    assert file_row(db, a.file_id)["next_action_at"] == w.clock.now() + DAY
    assert tiering_queue(db) == []
    assert drain_retention(w) == ["gone"]  # the intent's row goes, with no bytes to delete


def test_a_copy_that_fails_once_is_tried_again(db: psycopg.Connection, policy_db_url: str) -> None:
    w, a, b = split_pair(db, policy_db_url)
    w.sim.faults.fail_nth(Op.COPY_OBJECT, 1)
    pin(db, w, a.file_id, "cold")
    tick(w)
    assert copies(w) == 2
    assert audits(db, a.file_id, "tier.move.refused") == []
    assert primary(db, a.file_id)["object_key"] != a.ref.key


def test_the_fallback_is_no_longer_a_split(db: psycopg.Connection, policy_db_url: str) -> None:
    """Its message stops saying a split is under way, so other moves stop waiting."""
    w, a, b = archive_split(db, policy_db_url)
    no_copies_for(w, timedelta(days=36500))
    queue_move(db, b, "archive")
    steps(w, 2)  # the decision, the first try
    for _ in range(2):
        show_queue(db)
        steps(w, 1)
    [message] = tiering_queue(db)
    assert message["phase"] == "inplace" and message["no_own_copy"] is True
    assert "own_copy" not in message and "target_location_id" not in message
    assert "copy_tries" not in message


def test_a_warmer_move_that_cant_copy_waits_too(db: psycopg.Connection, policy_db_url: str) -> None:
    """The blob is Cold, by a pin that went with it. B's pin wants it Hot and A's pin stays."""
    w, a, b = split_pair(db, policy_db_url)
    agree_all = [a, b]
    for s_ in agree_all:
        pin(db, w, s_.file_id, "cold")
    no_copies_for(w, timedelta(days=36500))
    tick(w)
    assert run(w.sim.head(a.ref)).native_class == "Cold"
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.tier_pin SET logical_tier = 'hot' WHERE file_id = %s",
                   (b.file_id,))  # fmt: skip
    w.clock.set(w.clock.now() + MINUTE)
    run(_sweep_once(w))
    assert run(w.sim.head(a.ref)).native_class == "Cold"
    assert w.sim.early_deletion_fees == []
    assert len(held_reports(db, b.file_id)) == 1
    assert run(_entry(w, b))["move_hold"]["to"] == "hot"


def test_the_next_try_makes_the_copy(db: psycopg.Connection, policy_db_url: str) -> None:
    """A day later the copy works, and the file leaves, with no fee: the wait isn't
    remembered, and no message waited through it."""
    w, a, b = split_pair(db, policy_db_url)
    no_copies_for(w, timedelta(hours=6))
    pin(db, w, a.file_id, "cold")
    tick(w)
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    w.clock.set(w.clock.now() + DAY + MINUTE)
    run(_sweep_once(w))
    run(_drain_work(w))
    assert run(w.sim.head(a.ref)).native_class == "Hot"  # B's blob, and it stays
    assert primary(db, a.file_id)["logical_tier"] == "cold"
    assert primary(db, a.file_id)["object_key"] != a.ref.key
    assert w.sim.early_deletion_fees == []


def test_throttling_isnt_a_failed_copy(db: psycopg.Connection, policy_db_url: str) -> None:
    """A burst of throttling only delays it: it doesn't use up a try."""
    w, a, b = split_pair(db, policy_db_url)
    w.sim.faults.throttle(Op.COPY_OBJECT, times=tiering.OWN_COPY_TRIES + 1)
    pin(db, w, a.file_id, "cold")
    tick(w)
    assert audits(db, a.file_id, "tier.move.refused") == []
    assert primary(db, a.file_id)["object_key"] != a.ref.key


def test_set_tier_on_the_copy_failing_waits_too(db: psycopg.Connection, policy_db_url: str) -> None:
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    run(drain_verify(w))
    show_queue(db)
    steps(w, 1)  # verifying
    w.sim.faults.fail_nth(Op.SET_CLASS, 1)
    run(_drain_work(w))
    why = audits(db, a.file_id, "tier.move.refused")[0]
    assert why["details"]["why"] == "own_copy_failed"
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert primary(db, a.file_id)["object_key"] == a.ref.key


# ---------------------------------------------------------------------------
# What people and the bill see
# ---------------------------------------------------------------------------


def test_a_split_is_audited_in_its_own_dataset_and_counted(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[int, int, str]] = []
    monkeypatch.setattr(metrics, "tier_own_copy", lambda d, f, o: seen.append((d, f, o)))
    w, a, b, c = three_of_a_kind(db, policy_db_url)
    pin(db, w, c.file_id, "hot")
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "cold")
    tick(w)
    assert sorted(seen, key=lambda x: x[1]) == [
        (w.dataset_id, a.file_id, "copied"),
        (w.dataset_id, b.file_id, "joined"),
    ]
    [mine] = [x for x in audits(db, a.file_id) if x["details"]["how"] == "own_copy"]
    assert mine["dataset_id"] == w.dataset_id
    assert mine["version_id"] == a.version_id
    assert mine["details"]["from"] == "hot" and mine["details"]["to"] == "cold"
    # It names no other file, and no key: a key carries the file id of its first uploader.
    assert set(mine["details"]) == {"from", "to", "how", "verified", "target_location_id"}
    assert audits(db, c.file_id) == []


def test_a_failed_copy_is_counted(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(metrics, "tier_own_copy", lambda d, f, o: seen.append(o))
    w, a, b = split_pair(db, policy_db_url)
    no_copies_for(w, timedelta(days=36500))
    pin(db, w, a.file_id, "cold")
    tick(w)
    assert seen == ["failed"]


def test_a_file_getting_its_own_copy_isnt_shown_as_held(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Its pin differs from its tier, and no report says it is waiting: its copy is on
    the way."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    queue_move(db, a, "cold")
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "cold")
    run(_drain_work(w))
    assert run(w.sim.head(a.ref)).native_class == "Cold"
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.tier_pin SET logical_tier = 'hot' WHERE file_id = %s",
                   (b.file_id,))  # fmt: skip
    w.clock.set(w.clock.now() + MINUTE)
    assert run(_entry(w, b))["move_hold"] is None


def test_a_report_from_days_ago_isnt_a_hold_today(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The wait is a day from its report."""
    w, a, b = split_pair(db, policy_db_url)
    no_copies_for(w, timedelta(hours=1))
    pin(db, w, a.file_id, "cold")
    tick(w)
    assert run(_entry(w, a))["move_hold"] is not None
    w.clock.set(w.clock.now() + 3 * DAY)
    assert run(_entry(w, a))["move_hold"] is None


def test_the_portal_shows_the_tier_each_file_is_in(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    tick(w)
    got = run(_entry(w, a))
    assert got["tier"]["logical"] == "cold"
    assert run(_entry(w, b))["tier"]["logical"] == "hot"


# ---------------------------------------------------------------------------
# wp21's table of cases again, with the same sweeps
# ---------------------------------------------------------------------------


def _tally(db: psycopg.Connection, w: World) -> dict[str, int]:
    return {
        "moves": blob_moves(db, w),
        "fees": len(w.sim.early_deletion_fees),
        "copies": copies(w),
    }


@pytest.mark.parametrize(
    ("apart", "tally", "at"),
    [
        # Each on its own schedule, 10 days apart: one copy, the day A reaches Cool.
        (10, {"moves": 5, "fees": 0, "copies": 1},
         {30: ("warm", "hot"), 40: ("warm", "warm"), 90: ("cold", "warm"), 100: ("cold", "cold"),
          180: ("archive", "cold"), 190: ("archive", "archive")}),
        # 3 days apart: B goes along up to 3 days early, until its archive date, which
        # is its own.
        (3, {"moves": 3, "fees": 0, "copies": 1},
         {30: ("warm", "warm"), 90: ("cold", "cold"), 180: ("archive", "cold"),
          183: ("archive", "archive")}),
        # 50 days apart: B lands on a Cool blob, and the schedule never warms a file.
        (50, {"moves": 3, "fees": 0, "copies": 1},
         {51: ("warm", "warm"), 90: ("cold", "warm"), 140: ("cold", "cold"),
          180: ("archive", "cold"), 200: ("archive", "cold")}),
    ],
    ids=["10_days", "3_days", "50_days"],
)  # fmt: skip
def test_case_same_policy_a_older_than_b(
    db: psycopg.Connection, policy_db_url: str, apart: int, tally: dict, at: dict
) -> None:
    """wp21: A's schedule took the blob colder for B too, early. Daily sweeps to day 200."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    b = None
    for n in range(201):
        advance_to(w, n)
        if n == apart:
            tick(w)
            b = dedupe(db, w, a, "b.segy")
        tick(w)
        if n in at:
            assert b is not None
            got = (primary(db, a.file_id)["logical_tier"], primary(db, b.file_id)["logical_tier"])
            assert got == at[n], n
    assert _tally(db, w) == tally


def test_case_a_archive_date_b_pinned_hot(db: psycopg.Connection, policy_db_url: str) -> None:
    """wp21: A won and B was archived against its pin. Here both stay Hot until A's date.
    Sweeps at days 180 and 181."""
    w = world(db, policy_db_url, policy={"mode": "scheduled", "archive_day": 180})
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 170)
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, b.file_id, "hot")
    for n in (180, 181):
        advance_to(w, n)
        tick(w)
    assert primary(db, a.file_id)["logical_tier"] == "archive"
    assert primary(db, b.file_id)["logical_tier"] == "hot"
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert _tally(db, w) == {"moves": 0, "fees": 0, "copies": 1}


def test_case_a_archive_date_b_already_left(db: psycopg.Connection, policy_db_url: str) -> None:
    """The full schedule: A is in Cold by the time B comes, and B's pin takes it off the
    Cold blob ten days before A's date, reading Cold once. A's date then finds the blob
    alone."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    tick(w)
    for n in (30, 90):
        advance_to(w, n)
        tick(w)
    advance_to(w, 170)
    b = dedupe(db, w, a, "b.segy")
    pin(db, w, b.file_id, "hot")
    tick(w)
    for n in (180, 181):
        advance_to(w, n)
        tick(w)
    assert primary(db, a.file_id)["logical_tier"] == "archive"
    assert primary(db, b.file_id)["logical_tier"] == "hot"
    assert _tally(db, w) == {"moves": 3, "fees": 0, "copies": 1}


@pytest.mark.parametrize(
    ("policy", "tally"),
    [
        # Hot until day 90: A leaves from Hot, with nothing to pay.
        ({"mode": "scheduled", "cold_day": 90, "archive_day": 180},
         {"moves": 0, "fees": 0, "copies": 1}),
        # In Cool from day 30: the pin takes B off it at day 80, reading Cool once,
        # and A's blob goes on to Cold by itself.
        (POLICY, {"moves": 2, "fees": 0, "copies": 1}),
    ],
    ids=["from_hot", "from_cool"],
)  # fmt: skip
def test_case_a_schedule_cold_b_pinned_hot(
    db: psycopg.Connection, policy_db_url: str, policy: dict, tally: dict
) -> None:
    """wp21: B's pin brought the blob back a day later, for A too. Sweeps on days 90 to 92."""
    w = world(db, policy_db_url, policy=policy)
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 31)
    tick(w)
    advance_to(w, 80)
    b = dedupe(db, w, a, "b.segy")
    pin(db, w, b.file_id, "hot")
    tick(w)
    for n in (90, 91, 92):
        advance_to(w, n)
        tick(w)
        assert primary(db, a.file_id)["logical_tier"] == "cold"
        assert primary(db, b.file_id)["logical_tier"] == "hot"
    assert _tally(db, w) == tally


@pytest.mark.parametrize(
    ("n", "every"), [(180, MINUTE), (720, timedelta(minutes=10))], ids=["180_sweeps", "5_days"]
)
def test_case_a_pinned_cold_b_pinned_hot(
    db: psycopg.Connection, policy_db_url: str, n: int, every: timedelta
) -> None:
    """wp21: 180 moves and 90 fees with no guard, and 9 moves and 4 fees over 5 days with
    it, with the blob Hot on 4 of 720 sweeps. Now each pin is true on every sweep."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "hot")
    run(_follow(w, a.ref.container, first=True))
    hot = cold = 0
    for _ in range(n):
        w.clock.set(w.clock.now() + every)
        run(_sweep_once(w))
        hot += primary(db, b.file_id)["logical_tier"] == "hot"
        cold += primary(db, a.file_id)["logical_tier"] == "cold"
    assert (hot, cold) == (n, n)
    assert _tally(db, w) == {"moves": 0, "fees": 0, "copies": 1}


@pytest.mark.parametrize("layout", ["azure_worm", "azure_two_account"])
def test_case_one_pin_and_an_overwritten_original(
    db: psycopg.Connection, policy_db_url: str, layout: str
) -> None:
    """wp21: 61 moves and 30 fees in 60 sweeps with no guard, and the blob cold nearly all
    the time with it. The pin's blob stays Hot now."""
    w = make_world(db, policy_db_url, policy=POLICY, layout=layout)
    a = store(db, w, "a.segy")
    tick(w)
    b = dedupe(db, w, a, "b.segy")
    pin(db, w, b.file_id, "hot")
    tick(w)
    overwrite(db, w, a, "a.segy")
    advance_to(w, 40)
    fees = len(w.sim.early_deletion_fees)
    before = blob_moves(db, w)
    n = copies(w)
    for _ in range(60):
        w.clock.set(w.clock.now() + MINUTE)
        run(_sweep_once(w))
        assert primary(db, b.file_id)["logical_tier"] == "hot"
    # A's new version moves on its own blob, once.
    assert blob_moves(db, w) - before == 1
    assert len(w.sim.early_deletion_fees) == fees
    assert copies(w) - n == 1
    assert run(w.sim.head(a.ref)).native_class == "Hot"


# ---------------------------------------------------------------------------
# The same pins with copies failing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "every", "moves", "fees", "tries"),
    [(180, MINUTE, 0, 0, 3), (720, timedelta(minutes=10), 0, 0, 15)],
    ids=["180_sweeps", "5_days"],
)
def test_case_two_pins_with_copies_failing(
    db: psycopg.Connection, policy_db_url: str, n: int, every: timedelta, moves: int, fees: int,
    tries: int,
) -> None:  # fmt: skip
    """No write rights, say: every copy fails. Neither pin may move the blob over the
    other, so nothing moves. #71 measured 1 move in 180 sweeps and 9 moves and 4 fees in
    5 days here. Each day's retry tries a copy, three times, which bills nothing."""
    w, a, b = split_pair(db, policy_db_url)
    no_copies_for(w, timedelta(days=36500))
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "hot")
    run(_follow(w, a.ref.container, first=True))
    for _ in range(n):
        w.clock.set(w.clock.now() + every)
        run(_sweep_once(w))
    assert _tally(db, w) == {"moves": moves, "fees": fees, "copies": tries}


# ---------------------------------------------------------------------------
# A split under way never decides another file's archive date
# ---------------------------------------------------------------------------

ARCHIVE_ONLY = {"mode": "scheduled", "archive_day": 180}


class _ThrottledFor:
    """sim that throttles the copy to one file's own blob: that file's split is stuck,
    and any other file's copy works."""

    def __init__(self, sim: Any, file_id: int) -> None:
        self._sim = sim
        self._prefix = f"{file_id}/"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)

    async def copy_object(self, src: Any, container: str, key: str, tier: Any = None) -> Any:
        if key.startswith(self._prefix):
            raise Throttled("busy")
        return await self._sim.copy_object(src, container, key, tier)


def one_wants(sim: Any, w: World) -> BackendRegistry:
    return BackendRegistry.from_mapping({w.backend_id: sim})


async def _drain_with(w: World, registry: BackendRegistry) -> int:
    from .test_tiering_shared_blob import _consumer

    c = _consumer(w, registry)
    try:
        return await c.drain(limit=200)
    finally:
        await c.close()


def settle(
    db: psycopg.Connection, w: World, registry: BackendRegistry, *, reads: int = 25,
    verify: bool = True,
) -> None:  # fmt: skip
    """Run the queue as a worker would over some time: each look that defers or polls
    comes back, up to `reads` times. The clock doesn't move."""
    for _ in range(reads):
        n = run(_drain_with(w, registry))
        if verify:
            n += run(drain_verify(w))
        show_queue(db)
        if n == 0:
            break


def sweep_only(w: World) -> Any:
    async def go() -> Any:
        conn = await _worker(w)
        try:
            return await scheduler.sweep(conn, w.clock.now(), dataset_ids=[w.dataset_id])
        finally:
            await conn.close()

    return run(go())


def on_the_eve(db: psycopg.Connection, url: str, **kw: Any) -> tuple[World, Stored, Stored]:
    """Day 179.9. A and B share a Cold blob and have the same date, day 180. A's pin, which
    overrides the date, wants Hot, and B doesn't, so A is about to split."""
    w = world(db, url, **kw)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    for n in (30, 90):
        advance_to(w, n)
        tick(w)
    assert run(w.sim.head(a.ref)).native_class == "Cold"
    w.clock.set(w.t0 + timedelta(days=179.9))
    pin(db, w, a.file_id, "hot")
    sweep_only(w)
    return w, a, b


def split_on_the_eve(
    db: psycopg.Connection, url: str, **kw: Any
) -> tuple[World, Stored, Stored, BackendRegistry]:
    """And A's copy stays throttled."""
    w, a, b = on_the_eve(db, url, **kw)
    registry = one_wants(_ThrottledFor(w.sim, a.file_id), w)
    settle(db, w, registry, reads=3)
    assert any("own_copy" in m for m in tiering_queue(db))
    return w, a, b, registry


@pytest.mark.parametrize("layout", IN_PLACE.values(), ids=IN_PLACE.keys())
def test_an_archive_date_doesnt_wait_for_a_throttled_split(
    db: psycopg.Connection, policy_db_url: str, layout: dict
) -> None:
    """Once B has waited a short bound, it takes its own copy from the readable blob, into
    Archive, on its date. A's split goes on: its pin stays true, and the blob stays
    where A's copy can read it."""
    w, a, b, registry = split_on_the_eve(db, policy_db_url, **layout)
    w.clock.set(w.t0 + timedelta(days=180))
    assert (b.file_id, "archive") in sweep_only(w).moves
    settle(db, w, registry)
    mine = primary(db, b.file_id)
    assert mine["logical_tier"] == "archive"
    assert mine["object_key"] != a.ref.key
    assert run(w.sim.head(_ref(mine))).native_class == "Archive"
    # A's pin is true too. B has left, so its split had nothing left to do: the blob,
    # which stayed readable throughout, moved for A alone, 90 days into Cold, for no fee.
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert primary(db, a.file_id)["logical_tier"] == "hot"
    assert primary(db, a.file_id)["object_key"] == a.ref.key
    assert w.sim.early_deletion_fees == []
    assert audits(db, a.file_id, "tier.archive.late") == []
    assert audits(db, b.file_id, "tier.archive.late") == []


def test_an_archive_date_doesnt_wait_for_a_split_stuck_on_its_read_back(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = on_the_eve(db, policy_db_url)
    settle(db, w, w.registry, reads=2, verify=False)
    assert any("own_copy" in m for m in tiering_queue(db))
    # A's read-back sits in the queue, out of reach, as if a worker held it for hours.
    db.execute("UPDATE pgmq.q_verify SET vt = now() + interval '1 day'")
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    settle(db, w, w.registry)
    assert primary(db, b.file_id)["logical_tier"] == "archive"
    assert primary(db, b.file_id)["object_key"] != a.ref.key
    assert primary(db, a.file_id)["object_key"] == a.ref.key
    assert primary(db, a.file_id)["logical_tier"] == "cold"


def test_a_worm_files_archive_copy_during_a_split_carries_retention(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b, registry = split_on_the_eve(db, policy_db_url, **WORM_SHARE)
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    settle(db, w, registry)
    mine = primary(db, b.file_id)
    (until,) = db.execute(
        "SELECT retention_until FROM catalog.file_version WHERE version_id = %s", (b.version_id,)
    ).fetchone()
    assert mine["worm_stamped_until"] == until
    assert run(w.sim.head(_ref(mine))).retention_until == until
    old = row(db, "SELECT role, state FROM catalog.location WHERE location_id = %s", b.location_id)
    assert old == {"role": "secondary", "state": "committed"}


def test_a_move_other_than_archive_still_waits_for_the_split(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Only Archive has a promised date, so only Archive pays for a second copy. Past
    the bound this one is handed back, and the split's deadline limits how long."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 1)
    b_msg = queue_move(db, b, "warm")
    db.execute("UPDATE pgmq.q_tiering SET vt = now() + interval '1 hour' WHERE msg_id <> %s",
               (b_msg,))  # fmt: skip
    db.execute("UPDATE pgmq.q_tiering SET read_ct = %s WHERE msg_id = %s",
               (ConsumerConfig.max_reads, b_msg))  # fmt: skip
    steps(w, 1)
    assert [x for x in locations(db, b.file_id) if x["role"] == "secondary"] == []
    assert copies(w) == 0


def test_an_archive_move_waits_only_up_to_its_bound(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Under the bound it defers, and no copy is made: A may be done in moments."""
    w, a, b, registry = split_on_the_eve(db, policy_db_url)
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    b_msg = [m for m in tiering_queue(db) if m["file_id"] == str(b.file_id)]
    assert b_msg
    n = copies(w)
    db.execute("UPDATE pgmq.q_tiering SET vt = now() + interval '1 hour'"
               " WHERE message->>'file_id' <> %s", (str(b.file_id),))  # fmt: skip
    steps(w, 2, registry)  # its plan, then the look at the blob
    assert copies(w) == n
    assert [x for x in locations(db, b.file_id) if x["role"] == "secondary"] == []
    assert not any(m.get("own_copy") and m["file_id"] == str(b.file_id) for m in tiering_queue(db))


def test_if_its_own_copy_cant_be_made_the_date_still_holds(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The last resort, for both copies failing: B's fallback archives the blob for
    everyone rather than wait, and A's row says so. It is audited, and A's copy stops
    there: it asks for no rehydrate."""
    w, a, b, _ = split_on_the_eve(db, policy_db_url)
    w.sim.faults.outage(w.clock.now() - DAY, w.clock.now() + 365 * DAY, ops={Op.COPY_OBJECT})
    registry = one_wants(_ThrottledFor(w.sim, a.file_id), w)
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    settle(db, w, registry)
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert primary(db, b.file_id)["logical_tier"] == "archive"
    assert primary(db, a.file_id)["logical_tier"] == "archive"
    assert [x["details"]["how"] for x in audits(db, a.file_id)][-1] == "shared_blob"
    assert w.sim.faults.calls[Op.RESTORE] == 0


# The overall deadline


def test_a_split_stuck_throttling_goes_on_when_no_date_is_at_stake(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Past a day of reachable time it says so once, and goes on: B's tier isn't A's to
    change for a Cool step. When the throttling stops, it finishes."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, a.file_id, "warm")
    registry = one_wants(_ThrottledFor(w.sim, a.file_id), w)
    sweep_only(w)
    settle(db, w, registry, reads=4)
    w.clock.set(w.clock.now() + tiering.OWN_COPY_DEADLINE + MINUTE)
    reachable(db, w)
    settle(db, w, registry, reads=2)
    assert any("own_copy" in m for m in tiering_queue(db))
    assert audits(db, a.file_id, "tier.move.refused") == []
    [slow] = audits(db, a.file_id, "tier.move.waiting")
    assert slow["details"]["why"] == tiering.WAITING_SLOW_COPY
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    settle(db, w, w.registry)
    assert primary(db, a.file_id)["logical_tier"] == "warm"
    assert primary(db, b.file_id)["logical_tier"] == "hot"


def test_a_split_stuck_on_its_read_back_leaves_every_pin_true(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The reviewer's run. A pinned Cold, B pinned Hot, the copy made, its read-back
    pending, 25 hours pass, tiering first. It was: the copy dropped, and B in Cold against
    its pin, with no alert."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    pin(db, w, b.file_id, "hot")
    sweep_only(w)
    settle(db, w, w.registry, reads=4, verify=False)
    assert any("own_copy" in m for m in tiering_queue(db))
    w.clock.set(w.clock.now() + timedelta(hours=25))
    reachable(db, w)
    settle(db, w, w.registry, verify=False)
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert primary(db, b.file_id)["logical_tier"] == "hot"
    assert audits(db, a.file_id, "tier.move.refused") == []
    assert any("own_copy" in m for m in tiering_queue(db))
    # A read-back that comes at last finishes it.
    settle(db, w, w.registry)
    assert primary(db, a.file_id)["logical_tier"] == "cold"
    assert primary(db, a.file_id)["object_key"] != a.ref.key
    assert primary(db, b.file_id)["logical_tier"] == "hot"
    assert w.sim.early_deletion_fees == []


def archive_split(db: psycopg.Connection, url: str, **kw: Any) -> tuple[World, Stored, Stored]:
    """A's pin wants Hot, and B's date, day 180, has come: B is about to copy."""
    w = world(db, url, **kw)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, a.file_id, "hot")
    w.clock.set(w.t0 + timedelta(days=180))
    return w, a, b


def to_the_commit(db: psycopg.Connection, w: World, b: Stored) -> None:
    """Up to the swap: the copy made, checked and in Archive."""
    queue_move(db, b, "archive")
    steps(w, 2)
    run(drain_verify(w))
    show_queue(db)
    steps(w, 2)


def test_a_copy_already_in_its_tier_isnt_thrown_away_at_the_deadline(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Only the record is left, and dropping it from Archive would bill an early deletion."""
    w, a, b = archive_split(db, policy_db_url)
    to_the_commit(db, w, b)
    w.clock.set(w.clock.now() + tiering.OWN_COPY_DEADLINE + MINUTE)
    reachable(db, w)
    steps(w, 1)
    assert primary(db, b.file_id)["object_key"] != a.ref.key
    assert audits(db, b.file_id, "tier.move.refused") == []


def test_a_copy_still_needing_its_retention_is_no_exception_at_the_deadline(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Its stamp hasn't landed, so more than the record is left. The date wins."""
    w, a, b = archive_split(db, policy_db_url, **WORM_SHARE)
    to_the_commit(db, w, b)
    [t] = [x for x in locations(db, b.file_id) if x["role"] == "secondary"]
    assert t["worm_sync_needed"] is True and t["logical_tier"] == "archive"
    w.clock.set(w.clock.now() + tiering.OWN_COPY_DEADLINE + MINUTE)
    reachable(db, w)
    steps(w, 1)
    [why] = audits(db, b.file_id, "tier.move.refused")
    assert (why["details"]["why"], why["details"]["detail"]) == ("own_copy_failed", "deadline")
    assert primary(db, b.file_id)["object_key"] == a.ref.key


# A backlog on the shared verify queue, and archives after their date


@pytest.mark.parametrize(
    ("drains_at", "late"), [(180.2, False), (180.6, True)], ids=["same_date", "next_day"]
)
def test_a_backlog_on_the_verify_queue_delays_the_archive_and_says_so(
    db: psycopg.Connection, policy_db_url: str, drains_at: float, late: bool
) -> None:
    """The read-back waits its turn behind other files', short of the deadline. If that
    takes the archive past its date, the audit and the alert say so."""
    seen: list[tuple[int, int]] = []
    real = metrics.tier_archive_late
    metrics.tier_archive_late = lambda d, f: seen.append((d, f))  # type: ignore[assignment]
    try:
        w, a, b, registry = split_on_the_eve(db, policy_db_url)
        w.clock.set(w.t0 + timedelta(days=180))
        sweep_only(w)
        settle(db, w, registry, verify=False)
        assert primary(db, b.file_id)["logical_tier"] == "cold"
        w.clock.set(w.t0 + timedelta(days=drains_at))
        settle(db, w, registry)
    finally:
        metrics.tier_archive_late = real  # type: ignore[assignment]
    assert primary(db, b.file_id)["logical_tier"] == "archive"
    reports = audits(db, b.file_id, "tier.archive.late")
    assert bool(reports) is late
    assert bool(seen) is late
    if late:
        assert reports[0]["dataset_id"] == w.dataset_id
        assert (
            reports[0]["details"]["archive_at"] == (w.t0 + timedelta(days=180)).date().isoformat()
        )
        assert seen == [(w.dataset_id, b.file_id)]


def test_a_backlog_past_the_deadline_falls_back_and_is_late(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The read-back never comes. B's fallback archives the blob for everyone, late."""
    w, a, b, registry = split_on_the_eve(db, policy_db_url)
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    settle(db, w, registry, verify=False)
    assert any(m.get("own_copy") and m["file_id"] == str(b.file_id) for m in tiering_queue(db))
    w.clock.set(w.t0 + timedelta(days=181.1))
    reachable(db, w)
    settle(db, w, registry, verify=False)
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert primary(db, b.file_id)["object_key"] == a.ref.key
    [why] = [x["details"] for x in audits(db, b.file_id, "tier.move.refused")]
    assert why["detail"] == "deadline"
    assert len(audits(db, b.file_id, "tier.archive.late")) == 1


def test_a_move_to_another_tier_is_never_late(db: psycopg.Connection, policy_db_url: str) -> None:
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file SET archive_at = %s WHERE file_id = %s",
                   ((w.clock.now() - 5 * DAY).date(), a.file_id))  # fmt: skip
    queue_move(db, a, "cold")
    steps(w, 1)
    assert primary(db, a.file_id)["logical_tier"] == "cold"
    assert audits(db, a.file_id, "tier.archive.late") == []


def test_a_pin_that_didnt_override_the_date_doesnt_excuse_a_late_archive(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Such a pin doesn't hold a file past its date, so something else did."""
    w = world(db, policy_db_url, policy=ARCHIVE_ONLY)
    a = store(db, w, "a.segy")
    tick(w)
    pin(db, w, a.file_id, "hot")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.tier_pin SET override_archive = false WHERE file_id = %s",
                   (a.file_id,))  # fmt: skip
    w.clock.set(w.t0 + timedelta(days=183))
    tick(w)
    assert primary(db, a.file_id)["logical_tier"] == "archive"
    assert len(audits(db, a.file_id, "tier.archive.late")) == 1


def test_an_archive_on_its_date_or_just_after_midnight_isnt_late(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w = world(db, policy_db_url, policy=ARCHIVE_ONLY)
    a = store(db, w, "a.segy")
    tick(w)
    day = (w.t0 + timedelta(days=180)).replace(hour=0, minute=0, second=0, microsecond=0)
    w.clock.set(day + timedelta(days=1, minutes=30))
    tick(w)
    assert primary(db, a.file_id)["logical_tier"] == "archive"
    assert audits(db, a.file_id, "tier.archive.late") == []


def test_an_in_place_archive_past_its_date_is_late(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Any route: the scheduler was down for days, say."""
    w = world(db, policy_db_url, policy=ARCHIVE_ONLY)
    a = store(db, w, "a.segy")
    tick(w)
    w.clock.set(w.t0 + timedelta(days=183))
    tick(w)
    assert primary(db, a.file_id)["logical_tier"] == "archive"
    [late] = audits(db, a.file_id, "tier.archive.late")
    assert late["details"]["late_by_hours"] >= 24


def test_a_two_account_archive_past_its_date_is_late(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w = make_world(db, policy_db_url, policy=ARCHIVE_ONLY)
    a = store(db, w, "a.segy")
    tick(w)
    w.clock.set(w.t0 + timedelta(days=183))
    tick(w)
    assert primary(db, a.file_id)["logical_tier"] == "archive"
    assert len(audits(db, a.file_id, "tier.archive.late")) == 1


def test_a_join_past_its_date_is_late_too(db: psycopg.Connection, policy_db_url: str) -> None:
    w, a, b, c = three_of_a_kind(db, policy_db_url)
    pin(db, w, c.file_id, "hot")
    pin(db, w, a.file_id, "archive")
    tick(w)
    assert primary(db, a.file_id)["logical_tier"] == "archive"
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file SET archive_at = %s WHERE file_id = %s",
                   ((w.clock.now() - 5 * DAY).date(), b.file_id))  # fmt: skip
    pin(db, w, b.file_id, "archive")
    queue_move(db, b, "archive")
    steps(w, 1)
    assert primary(db, b.file_id)["object_key"] == primary(db, a.file_id)["object_key"]
    assert len(audits(db, b.file_id, "tier.archive.late")) == 1


@pytest.mark.parametrize("on_folder", [False, True], ids=["file_pin", "folder_pin"])
def test_a_pin_that_overrode_the_date_makes_nothing_late(
    db: psycopg.Connection, policy_db_url: str, on_folder: bool
) -> None:
    """An Owner chose to hold it past the date. It wasn't late, it was kept."""
    w = world(db, policy_db_url, policy=ARCHIVE_ONLY)
    parent = w.root_id
    if on_folder:
        with db.transaction():
            db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            (parent,) = db.execute(
                "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, %s,"
                " 'kept', 'dir') RETURNING file_id",
                (w.dataset_id, w.root_id),
            ).fetchone()
    a = store(db, w, "a.segy", parent=parent)
    tick(w)
    held = parent if on_folder else a.file_id
    pin(db, w, held, "hot")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.tier_pin SET override_archive = true WHERE file_id = %s",
                   (held,))  # fmt: skip
    w.clock.set(w.t0 + timedelta(days=183))
    tick(w)
    assert primary(db, a.file_id)["logical_tier"] != "archive"
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.tier_pin SET released_at = %s, released_by_identity_id = %s"
                   " WHERE file_id = %s", (w.clock.now(), w.identity_id, held))  # fmt: skip
    w.clock.set(w.clock.now() + MINUTE)
    tick(w)
    assert primary(db, a.file_id)["logical_tier"] == "archive"
    assert audits(db, a.file_id, "tier.archive.late") == []


def test_an_old_version_has_no_archive_date_of_the_files(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """`file.archive_at` is about the current version."""
    w = world(db, policy_db_url, policy=ARCHIVE_ONLY)
    a = store(db, w, "a.segy")
    tick(w)
    overwrite(db, w, a, "a.segy")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file SET archive_at = %s WHERE file_id = %s",
                   ((w.t0 + DAY).date(), a.file_id))  # fmt: skip
    queue_move(db, a, "archive")
    w.clock.set(w.t0 + 5 * DAY)
    run(_drain_work(w))
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert audits(db, a.file_id, "tier.archive.late") == []


# ---------------------------------------------------------------------------
# Retention is never shortened, by any route
# ---------------------------------------------------------------------------


def expired_world(db: psycopg.Connection, url: str) -> tuple[World, Stored, Stored, Stored]:
    """Day 31 on a 30-day share. A is expired. C, younger, left A's blob for Cold on day
    25 and is retained to day 55. E is pinned Hot on A's blob, so A differs."""
    w = world(db, url, worm_days=30)
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 25)
    c = dedupe(db, w, a, "c.segy")
    e = dedupe(db, w, a, "e.segy")
    same_bytes(db, b"\x09" * 32, a, c, e)
    pin(db, w, e.file_id, "hot")
    pin(db, w, c.file_id, "cold")
    tick(w)
    assert primary(db, c.file_id)["object_key"] != a.ref.key
    advance_to(w, 31)
    return w, a, c, e


def test_an_expired_version_joining_a_retained_blob_doesnt_shorten_it(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, c, e = expired_world(db, policy_db_url)
    theirs = primary(db, c.file_id)
    (until,) = db.execute(
        "SELECT retention_until FROM catalog.file_version WHERE version_id = %s", (c.version_id,)
    ).fetchone()
    assert run(w.sim.head(_ref(theirs))).retention_until == until
    pin(db, w, a.file_id, "cold")
    tick(w)
    mine = primary(db, a.file_id)
    assert mine["object_key"] == theirs["object_key"]  # it joined
    assert mine["worm_sync_needed"] is True
    result = run(_stamp_all(w))
    assert result.failed == 0
    assert run(w.sim.head(_ref(theirs))).retention_until == until
    assert primary(db, a.file_id)["worm_sync_needed"] is False


def test_an_expired_version_on_a_locked_blob_doesnt_wedge(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, c, e = expired_world(db, policy_db_url)
    theirs = primary(db, c.file_id)
    (until,) = db.execute(
        "SELECT retention_until FROM catalog.file_version WHERE version_id = %s", (c.version_id,)
    ).fetchone()
    run(w.sim.set_retention(_ref(theirs), until, locked=True))
    pin(db, w, a.file_id, "cold")
    tick(w)
    assert primary(db, a.file_id)["object_key"] == theirs["object_key"]
    result = run(_stamp_all(w))
    assert result.failed == 0
    assert primary(db, a.file_id)["worm_sync_needed"] is False


def test_a_split_of_an_expired_version_stamps_no_past_date(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """No join: A wants a tier nothing else is in."""
    w, a, c, e = expired_world(db, policy_db_url)
    pin(db, w, a.file_id, "warm")
    tick(w)
    mine = primary(db, a.file_id)
    assert mine["object_key"] not in (a.ref.key, primary(db, c.file_id)["object_key"])
    assert mine["worm_sync_needed"] is False
    assert run(w.sim.head(_ref(mine))).retention_until is None
    assert mine["worm_stamped_until"] is None
    assert "omnifile_retain_until" not in run(w.sim.get_tags(_ref(mine)))


def test_a_reference_with_an_earlier_date_doesnt_shorten_a_blob(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Still in force, but earlier than what the blob carries for another file."""
    w = world(db, policy_db_url, worm_days=3650)
    a = store(db, w, "a.segy")
    tick(w)
    advance_to(w, 10)
    b = dedupe(db, w, a, "b.segy")
    (later,) = db.execute(
        "SELECT retention_until FROM catalog.file_version WHERE version_id = %s", (b.version_id,)
    ).fetchone()
    (earlier,) = db.execute(
        "SELECT retention_until FROM catalog.file_version WHERE version_id = %s", (a.version_id,)
    ).fetchone()
    if later <= earlier:  # dedupe commits on the wall clock: keep the two apart
        later = earlier + 10 * DAY
        db.execute("UPDATE catalog.file_version SET retention_until = %s WHERE version_id = %s",
                   (later, b.version_id))  # fmt: skip
    run(_stamp_all(w))
    assert run(w.sim.head(a.ref)).retention_until == later
    # A's row is stamped again, as when its share locks: it mustn't take the date back.
    db.execute(
        "UPDATE catalog.location SET worm_sync_needed = true, worm_stamped_until = NULL"
        " WHERE location_id = %s",
        (a.location_id,),
    )
    run(_stamp_all(w))
    assert run(w.sim.head(a.ref)).retention_until == later
    assert run(w.sim.get_tags(a.ref))["omnifile_retain_until"] == later.astimezone(UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_clearing_one_files_hold_keeps_another_files_hold_on_the_blob(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = split_pair(db, policy_db_url, **WORM_SHARE)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file_version SET legal_hold = true WHERE version_id = ANY(%s)",
                   ([a.version_id, b.version_id],))  # fmt: skip
    run(_stamp_all(w))
    assert run(w.sim.head(a.ref)).legal_hold is True
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file_version SET legal_hold = false WHERE version_id = %s",
                   (a.version_id,))  # fmt: skip
    run(_stamp_all(w))
    assert run(w.sim.head(a.ref)).legal_hold is True
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file_version SET legal_hold = false WHERE version_id = %s",
                   (b.version_id,))  # fmt: skip
    run(_stamp_all(w))
    assert run(w.sim.head(a.ref)).legal_hold is False


def test_a_hold_on_a_row_on_its_way_out_doesnt_keep_the_blobs_hold(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = split_pair(db, policy_db_url)  # no share: no retention to refuse the delete
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file_version SET legal_hold = true WHERE version_id = %s",
                   (a.version_id,))  # fmt: skip
    run(_stamp_all(w))
    assert run(w.sim.head(a.ref)).legal_hold is True
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.location SET state = 'deleted', verified = false"
                   " WHERE location_id = %s", (b.location_id,))  # fmt: skip
        db.execute("UPDATE catalog.file_version SET legal_hold = true WHERE version_id = %s",
                   (b.version_id,))  # fmt: skip
        db.execute("UPDATE catalog.file_version SET legal_hold = false WHERE version_id = %s",
                   (a.version_id,))  # fmt: skip
    run(_stamp_all(w))
    assert run(w.sim.head(a.ref)).legal_hold is False


def test_a_row_with_no_retention_says_nothing_about_a_blobs(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Held, with no date of its own. A blob with no tag gets `none`, and one that another
    file's retention is on keeps what it says."""
    w, a, b = split_pair(db, policy_db_url)
    run(w.sim.set_tags(a.ref, {"omnifile_worm": "locked"}))
    lone = store(db, w, "lone.segy", data=b"l" * 4096)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file_version SET legal_hold = true WHERE version_id = ANY(%s)",
                   ([a.version_id, lone.version_id],))  # fmt: skip
    run(_stamp_all(w))
    assert run(w.sim.get_tags(a.ref))["omnifile_worm"] == "locked"
    assert run(w.sim.get_tags(lone.ref))["omnifile_worm"] == "none"


async def _stamp_all(w: World) -> Any:
    """Every row of this world: others share the database."""
    conn = await _worker(w)
    try:
        cur = await conn.execute(
            "SELECT l.location_id FROM catalog.location l JOIN catalog.file f USING (file_id)"
            " WHERE f.dataset_id = %s AND l.worm_sync_needed",
            (w.dataset_id,),
        )
        ids = [r[0] for r in await cur.fetchall()]
        return await stamp_worm(conn, w.registry, w.clock.now(), only=ids)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# A sweep reclaims what a split leaves behind
# ---------------------------------------------------------------------------


async def _retire(w: World) -> int:
    conn = await _worker(w)
    try:
        done, _ = await retire_replaced_secondaries(conn, w.clock.now(), dataset_ids=[w.dataset_id])
        return done
    finally:
        await conn.close()


def retire(w: World) -> int:
    return run(_retire(w))


def test_a_retained_secondary_goes_when_its_retention_ends(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Day 0, a 30-day share: the version is current, so prune never looks at it. The
    sweep retires its old row once retention ends, and `delete_blob` keeps the bytes
    while another file uses them."""
    w = world(db, policy_db_url, worm_days=30)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, a.file_id, "warm")
    tick(w)
    old = row(db, "SELECT role, state FROM catalog.location WHERE location_id = %s", a.location_id)
    assert old == {"role": "secondary", "state": "committed"}
    advance_to(w, 20)  # past the grace, and still retained
    assert retire(w) == 0
    advance_to(w, 31)
    assert retire(w) == 1
    assert drain_retention(w) == ["shared"]
    assert row(db, "SELECT state FROM catalog.location WHERE location_id = %s",
               a.location_id) == {"state": "deleted"}  # fmt: skip
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert primary(db, b.file_id)["object_key"] == a.ref.key
    assert retire(w) == 0


def test_the_last_retained_row_frees_the_blob(db: psycopg.Connection, policy_db_url: str) -> None:
    """Both left: nothing else needs the bytes, so they go."""
    w = world(db, policy_db_url, worm_days=30)
    a = store(db, w, "a.segy")
    b = store(db, w, "b.segy")
    tick(w)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.location SET container = %s, object_key = %s, backend_version_id = %s"
            " WHERE location_id = %s",
            (a.ref.container, a.ref.key, a.ref.version_id, b.location_id),
        )
    pin(db, w, a.file_id, "warm")
    tick(w)
    assert primary(db, a.file_id)["object_key"] != a.ref.key
    advance_to(w, 31)  # B's retention has ended too, so its row can go
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.location SET state = 'deleted', verified = false"
                   " WHERE location_id = %s", (b.location_id,))  # fmt: skip
    assert retire(w) == 1
    assert drain_retention(w) == ["deleted"]
    with pytest.raises(ObjectNotFound):
        run(w.sim.head(a.ref))


def test_a_legal_hold_keeps_a_secondary(db: psycopg.Connection, policy_db_url: str) -> None:
    w = world(db, policy_db_url, worm_days=30)
    a = store(db, w, "a.segy")
    dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, a.file_id, "warm")
    tick(w)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file_version SET legal_hold = true WHERE version_id = %s",
                   (a.version_id,))  # fmt: skip
    advance_to(w, 31)
    assert retire(w) == 0


def test_an_orphan_copy_goes_after_its_grace(db: psycopg.Connection, policy_db_url: str) -> None:
    """The split's message dead-lettered and the plan changed: nothing will use the copy.
    Not at once: the next move may still pick it up."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    [t] = [x for x in locations(db, a.file_id) if x["role"] == "secondary"]
    assert retire(w) == 0  # its message is there
    db.execute("DELETE FROM pgmq.q_tiering")
    db.execute("DELETE FROM pgmq.q_verify")
    assert retire(w) == 0  # within the grace
    w.clock.set(w.clock.now() + 2 * DAY)
    assert retire(w) == 1
    assert drain_retention(w) == ["deleted"]
    with pytest.raises(ObjectNotFound):
        run(w.sim.head(_ref(t)))
    assert primary(db, a.file_id)["object_key"] == a.ref.key


def test_a_pending_orphan_goes_too(db: psycopg.Connection, policy_db_url: str) -> None:
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 1)
    db.execute("DELETE FROM pgmq.q_tiering")
    w.clock.set(w.clock.now() + 2 * DAY)
    assert retire(w) == 1
    assert drain_retention(w) == ["gone"]


def test_the_sweep_leaves_a_copy_with_its_message_alone(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    run(drain_verify(w))
    w.clock.set(w.clock.now() + 2 * DAY)
    assert retire(w) == 0


def test_the_sweep_leaves_an_archive_copy_and_a_migrations_rows_alone(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A secondary in another container isn't a split's: a copy in the archive account,
    or a migration's copy of a source object."""
    w = make_world(db, policy_db_url, policy=POLICY)
    a = store(db, w, "a.segy")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
            " object_key, role, state, logical_tier, storage_class, size, crc64nvme,"
            " committed_at, tier_changed_at) SELECT file_id, version_id, backend_id,"
            " 'elsewhere/data', 'its-own-name', 'secondary', 'committed', 'hot', 'Hot', size,"
            " crc64nvme, %s, %s FROM catalog.location WHERE location_id = %s",
            (w.clock.now(), w.clock.now(), a.location_id),
        )
    w.clock.set(w.clock.now() + 2 * DAY)
    assert retire(w) == 0


def test_the_sweep_never_touches_a_migration_source(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Decision 40. A source's rows, even in one bucket: the catalog would refuse, and the
    whole sweep would fail with it."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    s3 = make_world(db, policy_db_url, layout="s3", policy=None)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "INSERT INTO catalog.source (dataset_id, backend_id, container, owners_sid)"
            " VALUES (%s, %s, 'acme', 'S-1-5-21-1-513')",
            (w.dataset_id, s3.backend_id),
        )
        db.execute(
            "UPDATE catalog.location SET backend_id = %s, container = 'acme', object_key = 'one',"
            " backend_version_id = NULL WHERE location_id = %s",
            (s3.backend_id, a.location_id),
        )
        db.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
            " object_key, role, state, logical_tier, storage_class, size, crc64nvme,"
            " committed_at, tier_changed_at) SELECT file_id, version_id, backend_id, container,"
            " 'two', 'secondary', 'committed', 'hot', 'Hot', size, crc64nvme, %s, %s"
            " FROM catalog.location WHERE location_id = %s",
            (w.clock.now(), w.clock.now(), a.location_id),
        )
    w.clock.set(w.clock.now() + 2 * DAY)
    assert retire(w) == 0


def test_the_sweep_needs_a_verified_primary(db: psycopg.Connection, policy_db_url: str) -> None:
    """Nothing replaced this copy yet."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    db.execute("DELETE FROM pgmq.q_tiering")
    db.execute("DELETE FROM pgmq.q_verify")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.location SET verified = false WHERE location_id = %s",
                   (a.location_id,))  # fmt: skip
    w.clock.set(w.clock.now() + 2 * DAY)
    assert retire(w) == 0


def test_the_sweep_is_a_job(db: psycopg.Connection, policy_db_url: str) -> None:
    """Registered with the worker, and run on its schedule."""
    from types import SimpleNamespace

    from omnifile_service.worker.retention_jobs import retention_jobs

    w = world(db, policy_db_url)
    fake = SimpleNamespace(registry=w.registry, clock=w.clock, consumer_config=ConsumerConfig())
    jobs = {getattr(j, "name", None): j for j in retention_jobs(fake)}  # type: ignore[arg-type]
    assert "replaced_secondaries" in jobs
    assert jobs["replaced_secondaries"].every == timedelta(hours=1)


# ---------------------------------------------------------------------------
# Nobody left to share with, and other small things
# ---------------------------------------------------------------------------


def test_a_split_with_nobody_left_makes_no_copy(db: psycopg.Connection, policy_db_url: str) -> None:
    """B left while the copy waited its turn: A has the blob to itself, and moves in place."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 1)  # the decision
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.location SET state = 'deleting', verified = false"
                   " WHERE location_id = %s", (b.location_id,))  # fmt: skip
    run(_drain_work(w))
    assert copies(w) == 0
    assert run(w.sim.head(a.ref)).native_class == "Cold"
    assert primary(db, a.file_id)["object_key"] == a.ref.key
    assert audits(db, a.file_id, "tier.move.refused") == []


def test_when_the_others_leave_during_the_copy_the_swap_still_runs(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The limit, stated in the docs. The copy is done, and the old blob has nobody: it
    goes, and leaving its tier early bills what moving it in place would have billed."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.location SET state = 'deleting', verified = false"
                   " WHERE location_id = %s", (b.location_id,))  # fmt: skip
    show_queue(db)
    run(_drain_work(w))
    assert primary(db, a.file_id)["object_key"] != a.ref.key
    assert drain_retention(w) == ["deleted"]


class _NeverStamps:
    def __init__(self, sim: Any) -> None:
        self._sim = sim

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)

    async def set_retention(self, *args: Any, **kw: Any) -> None:
        await asyncio.Event().wait()


def test_a_stamp_that_never_returns_gives_up(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung call would stall this replica's moves."""
    monkeypatch.setattr(tiering, "MOVE_CALL_TIMEOUT", 0.2)
    w, a, b = worm_pair(db, policy_db_url)
    pin(db, w, b.file_id, "cold")
    queue_move(db, b, "cold")
    steps(w, 2)
    run(drain_verify(w))
    show_queue(db)
    steps(w, 2)
    registry = one_wants(_NeverStamps(w.sim), w)
    run(asyncio.wait_for(_steps(w, 1, registry), 10))
    assert primary(db, b.file_id)["object_key"] == a.ref.key
    assert [m["stamp_tries"] for m in tiering_queue(db)] == [1]


# Checks a review found no test for


def test_a_join_needs_a_verified_blob(db: psycopg.Connection, policy_db_url: str) -> None:
    w, a, b, c = three_of_a_kind(db, policy_db_url)
    pin(db, w, c.file_id, "hot")
    pin(db, w, a.file_id, "cold")
    tick(w)
    mine = primary(db, a.file_id)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.location SET verified = false WHERE location_id = %s",
                   (mine["location_id"],))  # fmt: skip
    pin(db, w, b.file_id, "cold")
    tick(w)
    assert copies(w) == 2
    assert primary(db, b.file_id)["object_key"] != mine["object_key"]


def test_a_join_reads_the_row_again_under_its_lock(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blob moved on between the first look and its lock: this file copies."""
    w, a, b, c = three_of_a_kind(db, policy_db_url)
    pin(db, w, c.file_id, "hot")
    pin(db, w, a.file_id, "cold")
    tick(w)
    theirs = primary(db, a.file_id)
    real = tiering.blob_lock_key

    def blob_moves_on(backend: int, container: str, key: str) -> str:
        if key == theirs["object_key"]:
            with psycopg.connect(policy_db_url, autocommit=True) as other:
                other.execute("SELECT set_config('omnifile.actor', 'system:test', false)")
                other.execute(
                    "UPDATE catalog.location SET logical_tier = 'warm', storage_class = 'Cool'"
                    " WHERE location_id = %s",
                    (theirs["location_id"],),
                )
        return real(backend, container, key)

    monkeypatch.setattr(tiering, "blob_lock_key", blob_moves_on)
    pin(db, w, b.file_id, "cold")
    queue_move(db, b, "cold")
    steps(w, 1)
    assert primary(db, b.file_id)["location_id"] == b.location_id  # no join
    assert [x["state"] for x in locations(db, b.file_id) if x["role"] == "secondary"] == ["pending"]


def test_set_tier_needs_a_read_back(db: psycopg.Connection, policy_db_url: str) -> None:
    """A stale message for a copy nobody has read: it stays in Hot, where a bad one can
    still be dropped for nothing."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    [message] = tiering_queue(db)
    [t] = [x for x in locations(db, a.file_id) if x["role"] == "secondary"]
    db.execute("DELETE FROM pgmq.q_tiering")
    db.execute("SELECT pgmq.send('tiering'::text, %s::jsonb)",
               (Jsonb({**message, "phase": "archive"}),))  # fmt: skip
    steps(w, 1)
    assert run(w.sim.head(_ref(t))).native_class == "Hot"
    t = row(db, "SELECT * FROM catalog.location WHERE location_id = %s", t["location_id"])
    assert t["logical_tier"] == "hot"
    assert tiering_queue(db) == []


def test_the_swap_looks_again_under_its_lock(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version was pruned between the first look and the lock: no swap, and the copy
    goes on the next run, as the first look now sees it."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    run(drain_verify(w))
    show_queue(db)
    steps(w, 2)
    [t] = [x for x in locations(db, a.file_id) if x["role"] == "secondary"]
    real = tiering.blob_lock_key

    def pruned(*args: Any) -> str:
        monkeypatch.setattr(tiering, "blob_lock_key", real)
        with psycopg.connect(policy_db_url, autocommit=True) as other:
            other.execute("SELECT set_config('omnifile.actor', 'system:test', false)")
            other.execute("UPDATE catalog.file_version SET state = 'pruned' WHERE version_id = %s",
                          (a.version_id,))  # fmt: skip
        return real(*args)

    monkeypatch.setattr(tiering, "blob_lock_key", pruned)
    steps(w, 1)
    assert row(db, "SELECT role FROM catalog.location WHERE location_id = %s",
               t["location_id"]) == {"role": "secondary"}  # fmt: skip
    assert primary(db, a.file_id)["object_key"] == a.ref.key
    show_queue(db)
    steps(w, 1)
    assert row(db, "SELECT state FROM catalog.location WHERE location_id = %s",
               t["location_id"]) == {"state": "deleting"}  # fmt: skip


def test_the_copys_state_under_its_lock_counts_too(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy was dropped between the first look and the lock: no swap."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    run(drain_verify(w))
    show_queue(db)
    steps(w, 2)
    [t] = [x for x in locations(db, a.file_id) if x["role"] == "secondary"]
    real = tiering.blob_lock_key

    def dropped(*args: Any) -> str:
        monkeypatch.setattr(tiering, "blob_lock_key", real)
        with psycopg.connect(policy_db_url, autocommit=True) as other:
            other.execute("SELECT set_config('omnifile.actor', 'system:test', false)")
            other.execute("UPDATE catalog.location SET state = 'deleting', verified = false"
                          " WHERE location_id = %s", (t["location_id"],))  # fmt: skip
        return real(*args)

    monkeypatch.setattr(tiering, "blob_lock_key", dropped)
    steps(w, 1)
    assert primary(db, a.file_id)["location_id"] == a.location_id
    assert row(db, "SELECT role FROM catalog.location WHERE location_id = %s",
               t["location_id"]) == {"role": "secondary"}  # fmt: skip


# ---------------------------------------------------------------------------
# Decision 34: restoring a version while it splits
# ---------------------------------------------------------------------------


def _restore_request(
    db: psycopg.Connection, w: World, s: Stored, name: str | None, target: str = "restored"
) -> int:
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (rid,) = db.execute(
            "INSERT INTO catalog.version_restore (dataset_id, file_id, version_id, target,"
            " name, state, requested_by_identity_id, requested_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, 'requested', %s, %s, %s) RETURNING restore_id",
            (
                w.dataset_id,
                s.file_id,
                s.version_id,
                target,
                name,
                w.identity_id,
                w.clock.now(),
                w.clock.now(),
            ),
        ).fetchone()
    return rid


async def _run_restore(w: World, rid: int, registry: BackendRegistry | None = None) -> str:
    conn = await _worker(w)
    try:
        async with conn.transaction():
            return await portal_jobs.handle_restore(
                conn, registry or w.registry, rid, w.clock.now()
            )
    finally:
        await conn.close()


def old_version_splitting(db: psycopg.Connection, url: str) -> tuple[World, Stored, Stored]:
    """A's old version shares a blob with B's pin, and has its copy made but not verified."""
    w, a, b = split_pair(db, url, layout="azure_two_account")  # restores land in live/data
    pin(db, w, b.file_id, "hot")
    overwrite(db, w, a, "a.segy")
    db.execute("DELETE FROM pgmq.q_tiering")
    queue_move(db, a, "warm")
    advance_to(w, 40)
    steps(w, 2)
    assert any("own_copy" in m for m in tiering_queue(db))
    return w, a, b


def test_restoring_a_version_while_it_splits_reads_the_original(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The restore's copy is a file of its own, so it shares nothing with the split."""
    w, a, b = old_version_splitting(db, policy_db_url)
    rid = _restore_request(db, w, a, "a-restored.segy")
    assert run(_run_restore(w, rid)) == "done"
    (fid,) = db.execute(
        "SELECT result_file_id FROM catalog.version_restore WHERE restore_id = %s", (rid,)
    ).fetchone()
    restored = primary(db, fid)
    assert restored["object_key"] not in (a.ref.key,)
    assert read(w, restored) == b"x" * 4096
    # The split goes on, none the wiser, and the restored file isn't part of it.
    show_queue(db)
    run(_drain_work(w))
    old = [x for x in locations(db, a.file_id)
           if x["version_id"] == a.version_id and x["role"] == "primary"
           and x["state"] == "committed"]  # fmt: skip
    assert old[0]["object_key"] != a.ref.key
    assert primary(db, fid)["location_id"] == restored["location_id"]
    assert read(w, primary(db, b.file_id)) == b"x" * 4096


def test_restoring_a_version_after_its_swap_reads_the_new_blob(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = old_version_splitting(db, policy_db_url)
    show_queue(db)
    run(_drain_work(w))
    assert drain_retention(w) == ["shared"]
    rid = _restore_request(db, w, a, "a-restored.segy")
    assert run(_run_restore(w, rid)) == "done"
    (fid,) = db.execute(
        "SELECT result_file_id FROM catalog.version_restore WHERE restore_id = %s", (rid,)
    ).fetchone()
    assert read(w, primary(db, fid)) == b"x" * 4096


def test_a_restore_whose_source_went_under_it_finds_the_new_one(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Its copy read the old blob, which the swap's delete took. It fails, and its retry
    reads the primary afresh."""

    class Gone:
        def __init__(self, sim: Any) -> None:
            self._sim = sim

        def __getattr__(self, name: str) -> Any:
            return getattr(self._sim, name)

        async def copy_object(self, *args: Any, **kw: Any) -> Any:
            raise ObjectNotFound("the swap's delete got there first")

    w, a, b = old_version_splitting(db, policy_db_url)
    rid = _restore_request(db, w, a, "a-restored.segy")
    with pytest.raises(ObjectNotFound):
        run(_run_restore(w, rid, one_wants(Gone(w.sim), w)))
    show_queue(db)
    run(_drain_work(w))
    drain_retention(w)
    assert run(_run_restore(w, rid)) == "done"


def test_overwriting_from_an_old_version_during_its_split(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Decision 34's overwrite makes a new current version. The split of the old one
    carries on, and the file's current version is the restored one."""
    w, a, b = old_version_splitting(db, policy_db_url)
    rid = _restore_request(db, w, a, None, target="overwrite")
    assert run(_run_restore(w, rid)) == "done"
    show_queue(db)
    run(_drain_work(w))
    (content,) = db.execute(
        "SELECT result_version_id FROM catalog.version_restore WHERE restore_id = %s", (rid,)
    ).fetchone()
    assert primary(db, a.file_id)["version_id"] == content
    assert read(w, primary(db, a.file_id)) == b"x" * 4096
