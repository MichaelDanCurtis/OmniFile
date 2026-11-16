"""M1's "stub after N minutes" policy and the edge_instructions handler,
against Postgres 17 and sim."""

from __future__ import annotations

from datetime import timedelta

import psycopg

from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.queue import ConsumerConfig
from omnifile_service.worker.runner import Worker
from omnifile_service.worker.stubs import DEFAULT_RESIDENT_BYTES, StubPolicy

from .conftest import run
from .support import add_dir, file_row, queue, seed, store

BIG = b"S" * (DEFAULT_RESIDENT_BYTES + 4096)
POLICY = StubPolicy(after=timedelta(minutes=30))


def _worker(connect, sim: SimBackend, clock: VirtualClock, backend_id: int) -> Worker:
    return Worker(
        connect,
        BackendRegistry.from_mapping({backend_id: sim}),
        clock=clock,
        stub_policy=POLICY,
        consumer_config=ConsumerConfig(backoff_base=0),
    )


def test_old_verified_files_get_desired_stub_state(db, connect, sim, clock) -> None:
    w = seed(db)
    old = run(store(db, w, sim, "old.segy", BIG, clock.now() - timedelta(minutes=31)))
    young = run(store(db, w, sim, "young.segy", BIG, clock.now() - timedelta(minutes=5)))
    small = run(store(db, w, sim, "small.txt", b"tiny", clock.now() - timedelta(hours=2)))
    worker = _worker(connect, sim, clock, w.backend_id)
    mine = {old.file_id, young.file_id, small.file_id}
    queued = [f for f in run(worker.sweep()) if f in mine]
    assert queued == [old.file_id]
    assert queue(db, "edge_instructions") == [
        {"kind": "stub", "file_id": str(old.file_id), "version_id": str(old.version_id)}
    ]
    assert old.file_id not in run(worker.sweep())  # already queued: no duplicate
    run(worker.instructions.drain())
    run(worker.instructions.close())
    row = file_row(db, old.file_id)
    assert row["stub_version_id"] == old.version_id
    assert row["stub_resident_bytes"] == DEFAULT_RESIDENT_BYTES
    assert row["state"] == "resident"  # the edge stubs; the API commits it
    assert file_row(db, young.file_id)["stub_version_id"] is None
    assert file_row(db, small.file_id)["stub_version_id"] is None
    # The desired state reaches the edge through the feed.
    (n,) = db.execute(
        "SELECT count(*) FROM catalog.feed_event WHERE entity = 'file' AND entity_id = %s",
        (old.file_id,),
    ).fetchone()
    assert n >= 2
    assert old.file_id not in run(worker.sweep())  # stub requested: not swept again


def test_keep_online_on_a_parent_folder_blocks_stubbing(db, connect, sim, clock) -> None:
    w = seed(db)
    folder = add_dir(db, w, w.root_id, "keep")
    f = run(store(db, w, sim, "k.segy", BIG, clock.now() - timedelta(hours=1), parent=folder))
    db.execute("SELECT set_config('omnifile.actor', 'tests', false)")  # holds are audited
    db.execute(
        "INSERT INTO catalog.hold (dataset_id, file_id, kind, reason, set_by_app)"
        " VALUES (%s, %s, 'keep_online', 'test', 'tests')",
        (w.dataset_id, folder),
    )
    assert f.file_id not in run(_worker(connect, sim, clock, w.backend_id).sweep())


def test_policy_overrides_and_never(db, connect, sim, clock) -> None:
    w = seed(db, policy={"stub_after_minutes": 5, "resident_mb": 0.5})
    f = run(store(db, w, sim, "p.segy", BIG, clock.now() - timedelta(minutes=6)))
    worker = _worker(connect, sim, clock, w.backend_id)
    assert f.file_id in run(worker.sweep())
    run(worker.instructions.drain())
    run(worker.instructions.close())
    assert file_row(db, f.file_id)["stub_resident_bytes"] == 512 * 1024
    w2 = seed(db, policy={"stub_at": "never"})
    g = run(store(db, w2, sim, "n.segy", BIG, clock.now() - timedelta(days=1)))
    assert g.file_id not in run(worker.sweep())


def test_m3_tiering_policies_are_left_to_the_scheduler(db, connect, sim, clock) -> None:
    """A dataset with an M3 policy (an archive day) stubs per its stub_at
    through the tiering scheduler, never on M1's "after N minutes"."""
    w = seed(db, policy={"mode": "scheduled", "archive_day": 3, "stub_at": "archive",
                         "stub_after_minutes": 5})  # fmt: skip
    f = run(store(db, w, sim, "m3.segy", BIG, clock.now() - timedelta(days=1)))
    assert f.file_id not in run(_worker(connect, sim, clock, w.backend_id).sweep())
    assert all(m["file_id"] != str(f.file_id) for m in queue(db, "edge_instructions"))


def test_a_cloud_copy_that_does_not_match_is_never_stubbed(db, connect, sim, clock) -> None:
    w = seed(db)
    f = run(
        store(
            db,
            w,
            sim,
            "bad.segy",
            BIG,
            clock.now() - timedelta(hours=1),
            catalog_size=len(BIG) + 1,
        )
    )
    worker = _worker(connect, sim, clock, w.backend_id)
    assert f.file_id in run(worker.sweep())
    run(worker.instructions.drain())
    run(worker.instructions.close())
    row = file_row(db, f.file_id)
    assert row["stub_version_id"] is None
    assert row["next_action"] == "stub"
    assert row["next_action_at"] == clock.now() + POLICY.mismatch_backoff
    assert f.file_id not in run(worker.sweep())  # backed off
    assert not [
        m
        for m in queue(db, "edge_instructions_dlq")
        if m["message"].get("file_id") == str(f.file_id)
    ]


def test_stale_instructions_are_acked_without_change(db, connect, sim, clock) -> None:
    w = seed(db)
    f = run(store(db, w, sim, "gone.segy", BIG, clock.now() - timedelta(hours=1)))
    worker = _worker(connect, sim, clock, w.backend_id)
    run(worker.sweep())
    db.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (f.file_id,))
    run(worker.instructions.drain())
    run(worker.instructions.close())
    assert file_row(db, f.file_id)["stub_version_id"] is None
    assert worker.instructions.stats.acked == 1


def test_unknown_instruction_is_dead_lettered(db, connect, sim, clock) -> None:
    w = seed(db)
    db.execute(
        "SELECT pgmq.send('edge_instructions'::text, %s::jsonb)",
        (psycopg.types.json.Jsonb({"kind": "teleport", "file_id": "1"}),),
    )
    db.execute("SELECT pgmq.create('edge_instructions_dlq')")
    db.execute("DELETE FROM pgmq.q_edge_instructions_dlq")
    worker = _worker(connect, sim, clock, w.backend_id)
    run(worker.instructions.drain())
    run(worker.instructions.close())
    (dead,) = queue(db, "edge_instructions_dlq")
    assert "teleport" in dead["error"]
