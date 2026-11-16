"""The scheduler and the move consumer against Postgres 17 and sim, on a
virtual clock: files move on schedule, pins win until they expire, Smart is
one-way, WORM archives in place and retained versions are never deleted."""

from __future__ import annotations

import psycopg
import pytest

from omnifile_service.backends.base import LogicalTier as T
from omnifile_service.backends.base import ObjectNotFound, RestoreState

from .support import (
    World,
    drain_retention,
    make_world,
    primary,
    queue,
    row,
    run,
    set_tier,
    store,
    tick,
)

SCHEDULED = {"mode": "scheduled", "warm_day": 30, "cold_day": 120, "archive_day": 365}


@pytest.fixture
def two(db: psycopg.Connection, policy_db_url: str) -> World:
    return make_world(db, policy_db_url, policy=SCHEDULED)


def advance_to(w: World, n: float) -> None:
    w.clock.set(w.day(n))


def stub_files(c: psycopg.Connection) -> list[int]:
    return [int(m["file_id"]) for m in queue(c, "edge_instructions") if m["kind"] == "stub"]


def test_hot_cool_cold_archive_on_schedule(db: psycopg.Connection, two: World) -> None:
    f = store(db, two, "line_1042.segy")
    tick(two)  # day 0: out of the Smart upload default into explicit Hot
    loc = primary(db, f.file_id)
    assert (loc["logical_tier"], loc["storage_class"], loc["explicit_tier_set"]) == (
        "hot",
        "Hot",
        True,
    )
    assert row(
        db, "SELECT next_action, next_action_at FROM catalog.file WHERE file_id = %s", f.file_id
    ) == {"next_action": "tier_warm", "next_action_at": two.day(30)}

    advance_to(two, 29)
    assert tick(two).moves == []
    advance_to(two, 30)
    assert tick(two).moves == [(f.file_id, "warm")]
    assert run(two.sim.head(f.ref)).native_class == "Cool"
    advance_to(two, 120)
    tick(two)
    assert primary(db, f.file_id)["logical_tier"] == "cold"
    assert stub_files(db) == []  # stub_at = archive (the default)

    advance_to(two, 365)
    res = tick(two)
    assert (f.file_id, "archive") in res.moves
    loc = primary(db, f.file_id)
    assert loc["logical_tier"] == "archive" and loc["container"] == "archive/data"
    assert loc["verified"] and loc["location_id"] != f.location_id
    # The copy matched (length + Content-MD5), so it is verified; the
    # original is deleted afterwards, by the retention queue.
    old = row(db, "SELECT state FROM catalog.location WHERE location_id = %s", f.location_id)
    assert old == {"state": "deleting"}
    assert run(two.sim.head(f.ref)).size == 4096
    assert drain_retention(two) == ["deleted"]
    old = row(db, "SELECT state FROM catalog.location WHERE location_id = %s", f.location_id)
    assert old == {"state": "deleted"}
    with pytest.raises(ObjectNotFound):
        run(two.sim.head(f.ref))
    # The live copy is gone; the archive account holds the bytes in Archive.
    head = run(
        two.sim.head(
            type(f.ref)(
                two.backend_id, loc["container"], loc["object_key"], loc["backend_version_id"]
            )
        )
    )
    assert head.native_class == "Archive" and head.restore_state is RestoreState.ARCHIVED
    assert row(db, "SELECT remote_tier FROM catalog.file WHERE file_id = %s", f.file_id) == {
        "remote_tier": "archive"
    }
    # ... and, archived, it becomes a stub (M1's edge_instructions message).
    assert stub_files(db) == [f.file_id]
    assert res.stubs == [f.file_id]
    # Settled until something changes.
    advance_to(two, 400)
    assert tick(two).moves == []


def test_smart_plus_archive_day(db: psycopg.Connection, policy_db_url: str) -> None:
    w = make_world(db, policy_db_url, policy={"mode": "smart", "archive_day": 90})
    f = store(db, w, "a.segy")
    tick(w)
    assert primary(db, f.file_id)["logical_tier"] == "smart"  # never given an explicit tier
    assert run(w.sim.head(f.ref)).native_class == "Smart"
    advance_to(w, 89)
    assert tick(w).moves == []
    advance_to(w, 90)
    assert tick(w).moves == [(f.file_id, "archive")]
    assert primary(db, f.file_id)["container"] == "archive/data"
    assert row(db, "SELECT archive_at FROM catalog.file WHERE file_id = %s", f.file_id) == {
        "archive_at": w.day(90).date()
    }


def test_pin_to_hot_blocks_archive_until_it_expires(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w = make_world(db, policy_db_url, policy={"mode": "smart", "archive_day": 100})
    held = store(db, w, "held.segy")
    respected = store(db, w, "respected.segy")
    tick(w)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "INSERT INTO catalog.tier_pin (dataset_id, file_id, logical_tier, until,"
            " set_by_identity_id, set_at, override_archive) VALUES (%s, %s, 'hot', %s, %s, %s,"
            " true), (%s, %s, 'hot', %s, %s, %s, false)",
            (
                w.dataset_id,
                held.file_id,
                w.day(150),
                w.identity_id,
                w.clock.now(),
                w.dataset_id,
                respected.file_id,
                w.day(150),
                w.identity_id,
                w.clock.now(),
            ),
        )
    advance_to(w, 10)
    res = tick(w)  # the pins take both files out of Smart into Hot
    assert sorted(res.moves) == sorted([(held.file_id, "hot"), (respected.file_id, "hot")])
    advance_to(w, 100)
    res = tick(w)
    # Without the override, the archive date is respected despite the pin.
    assert res.moves == [(respected.file_id, "archive")]
    assert primary(db, held.file_id)["logical_tier"] == "hot"
    advance_to(w, 149)
    assert tick(w).moves == []
    advance_to(w, 150)  # the pin expires: the policy takes over again
    assert tick(w).moves == [(held.file_id, "archive")]


def test_pin_on_folder_and_recall_for_warmer_pin(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w = make_world(db, policy_db_url, policy={"archive_day": 400})
    (folder,) = db.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, %s, 'pinned',"
        " 'dir') RETURNING file_id",
        (w.dataset_id, w.root_id),
    ).fetchone()
    a = store(db, w, "a.segy", parent=folder)
    b = store(db, w, "b.segy", parent=folder)
    tick(w)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "INSERT INTO catalog.tier_pin (dataset_id, file_id, logical_tier, set_by_identity_id,"
            " set_at) VALUES (%s, %s, 'archive', %s, %s)",
            (w.dataset_id, folder, w.identity_id, w.clock.now()),
        )
    advance_to(w, 1)
    res = tick(w)
    assert sorted(res.moves) == sorted([(a.file_id, "archive"), (b.file_id, "archive")])
    assert sorted(stub_files(db)) == sorted([a.file_id, b.file_id])

    # The edge stubbed `a`; an Owner now pins it back to hot: a recall starts.
    db.execute(
        "UPDATE catalog.file SET state = 'stub', resident_bytes = 0 WHERE file_id = %s",
        (a.file_id,),
    )
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "INSERT INTO catalog.tier_pin (dataset_id, file_id, logical_tier, until,"
            " set_by_identity_id, set_at, override_archive) VALUES (%s, %s, 'hot', %s, %s, %s,"
            " true)",
            (w.dataset_id, a.file_id, w.day(30), w.identity_id, w.clock.now()),
        )
    advance_to(w, 2)
    res = tick(w)
    assert res.moves == [] and len(res.recalls) == 1
    rec = row(db, "SELECT * FROM catalog.recall WHERE recall_id = %s", res.recalls[0])
    assert rec["file_id"] == a.file_id and rec["state"] == "requested"
    who = row(
        db,
        "SELECT via, proof, identity_id FROM catalog.recall_requester WHERE recall_id = %s",
        res.recalls[0],
    )
    assert who == {"via": "portal", "proof": "proven", "identity_id": w.identity_id}
    assert {"recall_id": str(res.recalls[0])} in queue(db, "restores")
    advance_to(w, 3)
    assert tick(w).recalls == []  # joined, not started twice


def test_smart_is_one_way(db: psycopg.Connection, policy_db_url: str) -> None:
    w = make_world(db, policy_db_url, policy={"mode": "smart", "archive_day": 365})
    f = store(db, w, "a.segy")
    set_tier(db, w, f, T.WARM)  # e.g. a pin that has since been released
    tick(w)
    loc = primary(db, f.file_id)
    assert loc["logical_tier"] == "warm" and loc["explicit_tier_set"]
    # A hand-made move back to Smart is refused before reaching the backend.
    db.execute(
        "SELECT pgmq.send('tiering'::text, %s::jsonb)",
        (
            psycopg.types.json.Jsonb(
                {
                    "kind": "move",
                    "file_id": str(f.file_id),
                    "version_id": str(f.version_id),
                    "location_id": str(f.location_id),
                    "tier": "smart",
                }
            ),
        ),
    )
    tick(w)
    assert primary(db, f.file_id)["logical_tier"] == "warm"
    assert run(w.sim.head(f.ref)).native_class == "Cool"
    refused = row(
        db,
        "SELECT details FROM catalog.audit_log WHERE file_id = %s AND action = 'tier.move.refused'",
        f.file_id,
    )
    assert refused["details"]["why"] == "smart_one_way"
    # The catalog's own guard: the flag never clears.
    with pytest.raises(psycopg.Error) as e:
        db.execute(
            "UPDATE catalog.location SET explicit_tier_set = false WHERE location_id = %s",
            (f.location_id,),
        )
    assert e.value.sqlstate == "OF006"


def test_worm_archives_in_place(db: psycopg.Connection, policy_db_url: str) -> None:
    w = make_world(
        db,
        policy_db_url,
        layout="azure_worm",
        worm_days=3650,
        policy={"mode": "scheduled", "cold_day": 90, "archive_day": 180},
    )
    f = store(db, w, "record.segy")
    v = row(
        db, "SELECT retention_until FROM catalog.file_version WHERE version_id = %s", f.version_id
    )
    assert v["retention_until"] == w.day(3650)
    tick(w)
    assert primary(db, f.file_id)["logical_tier"] == "hot"
    advance_to(w, 90)
    tick(w)
    advance_to(w, 180)
    res = tick(w)
    assert (f.file_id, "archive") in res.moves
    loc = primary(db, f.file_id)
    # Same location row, same blob and version: Set Blob Tier in place.
    assert loc["location_id"] == f.location_id
    assert (loc["container"], loc["object_key"], loc["backend_version_id"]) == (
        f.ref.container,
        f.ref.key,
        f.ref.version_id,
    )
    assert (loc["logical_tier"], loc["storage_class"]) == ("archive", "Archive")
    assert run(w.sim.head(f.ref)).native_class == "Archive"
    how = row(
        db,
        "SELECT details FROM catalog.audit_log WHERE file_id = %s"
        " AND action = 'tier.move' ORDER BY at DESC LIMIT 1",
        f.file_id,
    )
    assert how["details"]["how"] == "in_place"
    assert stub_files(db) == [f.file_id]


def test_retained_version_never_copy_deleted(db: psycopg.Connection, two: World) -> None:
    f = store(db, two, "evidence.segy")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.file_version SET legal_hold = true WHERE version_id = %s",
            (f.version_id,),
        )
    advance_to(two, 365)
    tick(two)
    loc = primary(db, f.file_id)
    assert loc["location_id"] == f.location_id and loc["logical_tier"] != "archive"
    assert loc["container"] == "live/data"
    assert run(two.sim.head(f.ref)).size == 4096  # the original still exists
    refused = row(
        db,
        "SELECT details FROM catalog.audit_log WHERE file_id = %s AND action = 'tier.move.refused'",
        f.file_id,
    )
    assert refused["details"]["why"] == "retained"
    nxt = row(db, "SELECT next_action_at FROM catalog.file WHERE file_id = %s", f.file_id)
    assert nxt["next_action_at"] > two.clock.now()
    # And the catalog itself refuses the delete of a held version's copy.
    with pytest.raises(psycopg.Error) as e:
        db.execute(
            "UPDATE catalog.location SET state = 'deleted' WHERE location_id = %s", (f.location_id,)
        )
    assert e.value.sqlstate == "OF002"


@pytest.mark.parametrize(
    ("stub_at", "day", "stubbed"),
    [
        ("leaves_hot", 29, False),
        ("leaves_hot", 30, True),
        ("never", 400, False),
        ("archive", 120, False),
        ("archive", 365, True),
    ],
)
def test_stub_at(
    db: psycopg.Connection, policy_db_url: str, stub_at: str, day: int, stubbed: bool
) -> None:
    w = make_world(db, policy_db_url, policy={**SCHEDULED, "stub_at": stub_at})
    f = store(db, w, "s.segy")
    tick(w)
    for d in (30, 120, 365, 400):
        if d > day:
            break
        advance_to(w, d)
        tick(w)
    advance_to(w, day)
    tick(w)
    assert (f.file_id in stub_files(db)) is stubbed


def test_keep_online_hold_blocks_stub(db: psycopg.Connection, two: World) -> None:
    f = store(db, two, "kept.segy")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_app, until)"
            " VALUES (%s, %s, 'keep_online', 'test', 'test', %s)",
            (two.dataset_id, f.file_id, two.day(400)),
        )
    advance_to(two, 365)
    tick(two)
    assert primary(db, f.file_id)["logical_tier"] == "archive"
    assert stub_files(db) == []
    advance_to(two, 400)
    tick(two)
    assert stub_files(db) == [f.file_id]


def test_last_read_clock(db: psycopg.Connection, policy_db_url: str) -> None:
    w = make_world(
        db, policy_db_url, policy={"clock": "last_read", "warm_day": 30, "archive_day": 365}
    )
    f = store(db, w, "used.segy")
    tick(w)
    advance_to(w, 20)
    db.execute(
        "UPDATE catalog.file SET last_read_at = %s WHERE file_id = %s", (w.clock.now(), f.file_id)
    )
    advance_to(w, 30)  # was due on day 30, but the read moved it to day 50
    res = tick(w)
    assert res.moves == []
    nxt = row(
        db, "SELECT next_action, next_action_at FROM catalog.file WHERE file_id = %s", f.file_id
    )
    assert nxt == {"next_action": "tier_warm", "next_action_at": w.day(50)}
    advance_to(w, 50)
    assert tick(w).moves == [(f.file_id, "warm")]


def test_m1_policies_are_left_alone(db: psycopg.Connection, policy_db_url: str) -> None:
    w = make_world(db, policy_db_url, policy={"stub_after_minutes": 5})
    store(db, w, "m1.segy")
    advance_to(w, 1000)
    res = tick(w)
    assert res.evaluated == 0 and res.moves == []
