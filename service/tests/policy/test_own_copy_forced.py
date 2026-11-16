"""Decision 66, second review: what may move a blob over a file that differs, who hears of it,
and the sweep and stamps around a split. Real scheduler, move consumer, retention consumer and
stamps, against Postgres 17 and sim. See `test_own_copy.py` for the split itself.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from omnifile_service import metrics
from omnifile_service.backends.base import LogicalTier as T
from omnifile_service.backends.base import ObjectNotFound
from omnifile_service.backends.sim.faults import Op
from omnifile_service.catalog.privileges import as_app
from omnifile_service.policy.overdue import ARCHIVE_OVERDUE_SQL, OWN_COPY_STUCK_SQL, SCAN
from omnifile_service.retention import worm_sync
from omnifile_service.retention.worm_sync import stamp_worm
from omnifile_service.worker import tiering
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.queue import Consumer, ConsumerConfig, Defer
from omnifile_service.worker.retention_jobs import (
    LEFT_BEHIND_OP,
    LEFT_BEHIND_SWEPT,
    retention_jobs,
)

from .support import Stored, World, drain_retention, drain_verify, primary, row, run, store, tick
from .test_own_copy import (
    DAY,
    MINUTE,
    WORM_SHARE,
    _ref,
    _restore_request,
    _run_restore,
    _ThrottledFor,
    archive_split,
    copies,
    locations,
    no_copies_for,
    one_wants,
    queue_move,
    reachable,
    retire,
    settle,
    show_queue,
    split_pair,
    steps,
    sweep_only,
    tiering_queue,
    world,
)
from .test_tiering_shared_blob import (
    LOCK_LOCATION,
    _Paused,
    _probe_file,
    _probe_location,
    _run_move,
    _sweep_once,
    _worker,
    agree,
    audits,
    dedupe,
    file_row,
    held_reports,
    move_message,
    overwrite,
    pin,
)


def json_text(d: dict) -> str:
    import json

    return json.dumps(d)


# ---------------------------------------------------------------------------
# The deadline counts reachable time, and only for a date
# ---------------------------------------------------------------------------


def test_an_outage_doesnt_count_towards_an_archive_deadline(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """25 hours with no heartbeat is an outage, not a stuck copy (migration 0040, as recalls
    count it). The window starts again when the edges come back, and only a whole
    reachable day ends the copy."""
    w, a, b = archive_split(db, policy_db_url)
    queue_move(db, b, "archive")
    steps(w, 2)
    assert any("own_copy" in m for m in tiering_queue(db))
    reachable(db, w)
    w.clock.set(w.clock.now() + timedelta(hours=25))
    show_queue(db)
    settle(db, w, w.registry, reads=2, verify=False)
    assert audits(db, b.file_id, "tier.move.refused") == []
    back = w.clock.now()
    reachable(db, w, since=back)
    w.clock.set(back + timedelta(hours=23))
    reachable(db, w, since=back)
    show_queue(db)
    settle(db, w, w.registry, reads=2, verify=False)
    assert audits(db, b.file_id, "tier.move.refused") == []
    assert any("own_copy" in m for m in tiering_queue(db))
    w.clock.set(back + timedelta(hours=25))
    reachable(db, w, since=back)
    show_queue(db)
    settle(db, w, w.registry, verify=False)
    [why] = audits(db, b.file_id, "tier.move.refused")
    assert why["details"]["detail"] == "deadline"
    assert run(w.sim.head(a.ref)).native_class == "Archive"


def test_with_no_heartbeats_at_all_nothing_is_counted(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = archive_split(db, policy_db_url)
    queue_move(db, b, "archive")
    steps(w, 2)
    db.execute("DELETE FROM catalog.heartbeat_window")
    w.clock.set(w.clock.now() + 5 * DAY)
    show_queue(db)
    settle(db, w, w.registry, reads=2, verify=False)
    assert any("own_copy" in m for m in tiering_queue(db))
    assert audits(db, b.file_id, "tier.move.refused") == []


def test_an_archive_split_stuck_throttling_ends_in_the_fallback(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A date is at stake: past a reachable day, the blob moves for everyone."""
    w, a, b = archive_split(db, policy_db_url)
    registry = one_wants(_ThrottledFor(w.sim, b.file_id), w)
    sweep_only(w)
    settle(db, w, registry, reads=4)
    assert any("own_copy" in m for m in tiering_queue(db))
    w.clock.set(w.clock.now() + tiering.OWN_COPY_DEADLINE - MINUTE)
    reachable(db, w)
    settle(db, w, registry, reads=2)
    assert any("own_copy" in m for m in tiering_queue(db))
    w.clock.set(w.clock.now() + 2 * MINUTE)
    reachable(db, w)
    settle(db, w, registry)
    assert not any("own_copy" in m for m in tiering_queue(db))
    [why] = audits(db, b.file_id, "tier.move.refused")
    assert (why["details"]["why"], why["details"]["detail"]) == ("own_copy_failed", "deadline")
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert primary(db, a.file_id)["logical_tier"] == "archive"


# ---------------------------------------------------------------------------
# Only a move to Archive may move a blob over a file that differs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["warm", "cold", "archive"])
def test_no_other_move_forces_a_blob(db: psycopg.Connection, policy_db_url: str, tier: str) -> None:
    w, a, b = split_pair(db, policy_db_url)
    no_copies_for(w, timedelta(days=36500))
    pin(db, w, a.file_id, tier)
    for _ in range(3):
        tick(w)
        w.clock.set(w.clock.now() + DAY + MINUTE)
    assert run(w.sim.head(a.ref)).native_class == "Hot"
    assert primary(db, a.file_id)["logical_tier"] == "hot"
    assert w.sim.faults.calls[Op.SET_CLASS] == 0
    assert w.sim.early_deletion_fees == []
    assert not [x for x in audits(db, a.file_id) if x["details"].get("forced")]


def test_a_pin_to_archive_never_takes_another_file_offline(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A pins Archive on day 10, and its copy can't be made. Until A's own date nothing moves,
    B stays online, and nobody is told anything was forced. On A's date, the date wins."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    tick(w)
    w.clock.set(w.t0 + timedelta(days=5))
    b = dedupe(db, w, a, "b.segy")
    clear_notices(db)
    no_copies_for(w, timedelta(days=36500))
    w.clock.set(w.t0 + timedelta(days=10))
    pin(db, w, a.file_id, "archive")
    for day in (10, 11, 30, 90, 179):
        w.clock.set(w.t0 + timedelta(days=day, minutes=1))
        sweep_only(w)
        settle(db, w, w.registry)
        assert run(w.sim.head(a.ref)).native_class != "Archive", day
        assert primary(db, b.file_id)["logical_tier"] != "archive"
    assert held_reports(db, a.file_id)
    assert not [x for x in audits(db, b.file_id) if x["details"].get("forced")]
    assert notices(db) == []
    w.clock.set(w.t0 + timedelta(days=180, minutes=1))
    sweep_only(w)
    settle(db, w, w.registry)
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert audits(db, b.file_id)[-1]["details"]["forced"] == "another_files_archive_date"


def test_an_old_versions_date_forces_too(db: psycopg.Connection, policy_db_url: str) -> None:
    """A was overwritten, so its old version's date is its commit's, not the file's. It has
    no pin, and is archived only on that date, which is always respected."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    pin(db, w, b.file_id, "hot")
    tick(w)
    w.clock.set(w.t0 + timedelta(days=100))
    overwrite(db, w, a, "a.segy")
    with db.transaction():  # the file's own date is now its new version's
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file SET written_at = %s, archive_at = %s WHERE file_id = %s",
                   (w.clock.now(), (w.t0 + timedelta(days=280)).date(), a.file_id))  # fmt: skip
    no_copies_for(w, timedelta(days=36500))
    w.clock.set(w.t0 + timedelta(days=180, minutes=1))
    assert (a.version_id, "archive") in sweep_only(w).version_moves
    settle(db, w, w.registry)
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert audits(db, b.file_id)[-1]["details"]["forced"] == "another_files_archive_date"


def _pins_queued_move_overtaken(
    db: psycopg.Connection, url: str, how: str, layout: str = "azure_worm"
) -> tuple[World, Stored, Stored]:
    """A pinned Archive on day 10 and its move was queued; then, before it ran, A was
    overwritten, restored over (decision 34) or deleted. B, younger, shares the blob, and
    copies fail."""
    w = world(db, url, layout=layout)
    a = store(db, w, "a.segy")
    tick(w)
    w.clock.set(w.t0 + timedelta(days=5))
    b = dedupe(db, w, a, "b.segy")
    w.clock.set(w.t0 + timedelta(days=10))
    pin(db, w, a.file_id, "archive")
    if how == "overwrite":
        overwrite(db, w, a, "a.segy")
    elif how == "restore":
        rid = _restore_request(db, w, a, None, target="overwrite")
        assert run(_run_restore(w, rid)) == "done"
    else:
        with db.transaction():
            db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            db.execute("UPDATE catalog.file SET deleted_at = %s WHERE file_id = %s",
                       (w.clock.now(), a.file_id))  # fmt: skip
    no_copies_for(w, timedelta(days=36500))
    return w, a, b


@pytest.mark.parametrize("how", ["overwrite", "delete"])
def test_a_pins_move_overtaken_by_a_new_version_or_a_delete_forces_nothing(
    db: psycopg.Connection, policy_db_url: str, how: str
) -> None:
    """The version the message names is no longer current, so its date is its own, from its
    commit: day 180, not today. The move waits, and nobody is forced."""
    w, a, b = _pins_queued_move_overtaken(db, policy_db_url, how)
    clear_notices(db)
    run(_run_move(w, move_message(a, "archive")))
    assert run(w.sim.head(a.ref)).native_class != "Archive"
    assert primary(db, b.file_id)["logical_tier"] != "archive"
    assert not [x for x in audits(db, b.file_id) if x["details"].get("forced")]
    assert notices(db) == []


@pytest.mark.parametrize("how", ["overwrite", "restore", "delete"])
def test_an_overtaken_versions_date_is_its_own(
    db: psycopg.Connection, policy_db_url: str, how: str
) -> None:
    """What the forced path asks, for each way a version stops being current. A restore
    needs the live account, so these run on two accounts, where Archive is a copy and
    forces nothing anyway: the answer is the same rule on every layout."""
    w, a, _ = _pins_queued_move_overtaken(db, policy_db_url, how, layout="azure_two_account")

    async def arrived(at: datetime) -> bool:
        conn = await _worker(w)
        try:
            row_ = await tiering._load(conn, a.location_id)
            assert row_ is not None
            assert row_["content_version_id"] != a.version_id or row_["deleted_at"] is not None
            return await tiering._archive_date_arrived(conn, row_, at)
        finally:
            await conn.close()

    assert run(arrived(w.t0 + timedelta(days=10))) is False
    assert run(arrived(w.t0 + timedelta(days=179))) is False
    assert run(arrived(w.t0 + timedelta(days=180, minutes=1))) is True


def test_an_old_versions_own_date_still_forces(db: psycopg.Connection, policy_db_url: str) -> None:
    """The same move on that version's own date is the date's."""
    w, a, b = _pins_queued_move_overtaken(db, policy_db_url, "overwrite")
    w.clock.set(w.t0 + timedelta(days=180, minutes=1))
    run(_run_move(w, move_message(a, "archive")))
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert audits(db, b.file_id)[-1]["details"]["forced"] == "another_files_archive_date"


def test_a_move_for_a_lone_blob_never_waits(db: psycopg.Connection, policy_db_url: str) -> None:
    """With copies failing, and any tier: no copy is needed and none is tried."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    no_copies_for(w, timedelta(days=36500))
    for tier, native in (("warm", "Cool"), ("hot", "Hot"), ("cold", "Cold")):
        pin(db, w, a.file_id, tier)
        tick(w)
        assert run(w.sim.head(a.ref)).native_class == native
        with db.transaction():
            db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            db.execute("DELETE FROM catalog.tier_pin WHERE file_id = %s", (a.file_id,))
    assert copies(w) == 0
    assert audits(db, a.file_id, "tier.move.refused") == []


def test_an_old_versions_wait_is_no_sweep_after_sweep(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Old versions aren't woken by `next_action_at`: the version sweep stays away for the
    day too."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    pin(db, w, b.file_id, "hot")
    tick(w)
    overwrite(db, w, a, "a.segy")
    w.clock.set(w.t0 + timedelta(days=40))
    no_copies_for(w, timedelta(days=36500))
    before = copies(w)
    for _ in range(5):
        w.clock.set(w.clock.now() + MINUTE)
        run(_sweep_once(w))
    assert copies(w) - before == tiering.OWN_COPY_TRIES
    assert len(held_reports(db, a.file_id)) == 1
    w.clock.set(w.clock.now() + DAY + MINUTE)
    run(_sweep_once(w))
    assert copies(w) - before == 2 * tiering.OWN_COPY_TRIES


def test_an_old_versions_date_doesnt_wait_for_a_reported_wait(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The version sweep stays away for a day, except for Archive."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    pin(db, w, b.file_id, "hot")
    tick(w)
    overwrite(db, w, a, "a.segy")
    no_copies_for(w, timedelta(days=36500))
    w.clock.set(w.t0 + timedelta(days=179.9))
    run(_sweep_once(w))
    settle(db, w, w.registry)
    assert len(held_reports(db, a.file_id)) == 1
    w.clock.set(w.t0 + timedelta(days=180))
    assert sweep_only(w).version_moves == [(a.version_id, "archive")]


# ---------------------------------------------------------------------------
# A forced Archive is said out loud
# ---------------------------------------------------------------------------


def notices(db: psycopg.Connection) -> list[dict]:
    return [r[0] for r in db.execute("SELECT message FROM pgmq.q_notify ORDER BY msg_id")]


def clear_notices(db: psycopg.Connection) -> None:
    db.execute("DELETE FROM pgmq.q_notify")


def forced_world(db: psycopg.Connection, url: str, **kw: Any) -> tuple[World, Stored, Stored]:
    """A's date is day 180. B is younger, and copies fail."""
    clear_notices(db)
    w = world(db, url, **kw)
    a = store(db, w, "a.segy")
    tick(w)
    w.clock.set(w.t0 + timedelta(days=170))
    b = dedupe(db, w, a, "b.segy")
    no_copies_for(w, timedelta(days=36500))
    return w, a, b


def test_a_forced_archive_says_why_counts_and_tells_who_set_the_pin(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The date wins, and B goes offline against its pin. B's audit says why, the counter
    drives an alert, and the pin's setter gets a card. None of it names A."""
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(metrics, "tier_forced_archive", lambda d, f: seen.append((d, f)))
    w, a, b = forced_world(db, policy_db_url)
    pin(db, w, b.file_id, "hot")
    w.clock.set(w.t0 + timedelta(days=180))
    assert (a.file_id, "archive") in sweep_only(w).moves
    settle(db, w, w.registry)
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert primary(db, b.file_id)["logical_tier"] == "archive"
    why = audits(db, b.file_id)[-1]["details"]
    assert why["how"] == "shared_blob"
    assert why["forced"] == "another_files_archive_date"
    assert why["why"] == "neither_file_could_get_its_own_copy"
    assert "a.segy" not in str(why) and str(a.file_id) not in str(why)
    # A's own move is on its own date, so it isn't forced.
    assert "forced" not in audits(db, a.file_id)[-1]["details"]
    assert seen == [(w.dataset_id, b.file_id)]
    assert notices(db) == [
        {
            "kind": tiering.PIN_OVERRIDDEN,
            "file_id": str(b.file_id),
            "identity_id": str(w.identity_id),
            "tier": "hot",
            "at": w.clock.now().isoformat(),
        }
    ]


def test_every_file_it_goes_over_is_said(db: psycopg.Connection, policy_db_url: str) -> None:
    """B has a pin and C has its own date. Each row says so, not only the first found."""
    w, a, b = forced_world(db, policy_db_url)
    c = dedupe(db, w, a, "c.segy")
    pin(db, w, b.file_id, "hot")
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    settle(db, w, w.registry)
    for s in (b, c):
        assert audits(db, s.file_id)[-1]["details"]["forced"] == "another_files_archive_date"
    assert [m["file_id"] for m in notices(db)] == [str(b.file_id)]


def test_a_file_that_wanted_it_isnt_said_to_be_forced(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """B has A's date, so it wants Archive too. Only C, younger and pinned, is forced."""
    clear_notices(db)
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    tick(w)
    w.clock.set(w.t0 + timedelta(days=170))
    c = dedupe(db, w, a, "c.segy")
    no_copies_for(w, timedelta(days=36500))
    pin(db, w, c.file_id, "hot")
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    settle(db, w, w.registry)
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    for s_ in (a, b):
        assert not [x for x in audits(db, s_.file_id) if x["details"].get("forced")]
    assert audits(db, c.file_id)[-1]["details"]["forced"] == "another_files_archive_date"


def test_a_folders_pin_tells_who_set_it(db: psycopg.Connection, policy_db_url: str) -> None:
    w, a, _ = forced_world(db, policy_db_url)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (folder,) = db.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, %s, 'd',"
            " 'dir') RETURNING file_id",
            (w.dataset_id, w.root_id),
        ).fetchone()
    c = store(db, w, "in-folder.segy", parent=folder, data=b"c" * 4096)
    pin(db, w, folder, "warm")
    w.clock.set(w.t0 + timedelta(days=175))
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.location SET object_key = %s, backend_version_id = %s,"
            " container = %s, crc64nvme = (SELECT crc64nvme FROM catalog.location"
            " WHERE location_id = %s) WHERE location_id = %s",
            (a.ref.key, a.ref.version_id, a.ref.container, a.location_id, c.location_id),
        )
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    settle(db, w, w.registry)
    assert primary(db, c.file_id)["logical_tier"] == "archive"
    assert [(m["file_id"], m["tier"]) for m in notices(db)] == [(str(c.file_id), "warm")]


def test_a_forced_archive_with_no_pin_tells_nobody_but_is_counted(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(metrics, "tier_forced_archive", lambda d, f: seen.append((d, f)))
    w, a, b = forced_world(db, policy_db_url)
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    settle(db, w, w.registry)
    assert primary(db, b.file_id)["logical_tier"] == "archive"
    assert audits(db, b.file_id)[-1]["details"]["forced"] == "another_files_archive_date"
    assert seen == [(w.dataset_id, b.file_id)]
    assert notices(db) == []


def test_a_pin_to_archive_wasnt_overridden(db: psycopg.Connection, policy_db_url: str) -> None:
    """B wanted it too: nothing forced, nobody told, not counted."""
    w, a, b = forced_world(db, policy_db_url)
    pin(db, w, b.file_id, "archive")
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    settle(db, w, w.registry)
    assert primary(db, b.file_id)["logical_tier"] == "archive"
    assert "forced" not in audits(db, b.file_id)[-1]["details"]
    assert notices(db) == []


def test_a_pin_on_a_newer_version_wasnt_overridden(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """B's pin is about B's bytes now. The old version on this blob has no pin to break."""
    w, a, b = forced_world(db, policy_db_url)
    overwrite(db, w, b, "b.segy")
    pin(db, w, b.file_id, "hot")
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    settle(db, w, w.registry)
    assert run(w.sim.head(a.ref)).native_class == "Archive"
    assert notices(db) == []


def test_the_api_shows_a_wait_only_for_a_move_that_waits(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """With a wait reported on the blob: a pin to the tier it's in is no move, and a pin to
    Archive waits like any other until its file's date has come. A pin to another tier is
    shown."""
    from .test_tiering_shared_blob import _entry

    w, a, b = split_pair(db, policy_db_url)
    no_copies_for(w, timedelta(days=36500))
    pin(db, w, b.file_id, "cold")
    tick(w)
    assert run(_entry(w, b))["move_hold"]["to"] == "cold"

    def pin_to(tier: str) -> None:
        with db.transaction():
            db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            db.execute("UPDATE catalog.tier_pin SET logical_tier = %s WHERE file_id = %s",
                       (tier, b.file_id))  # fmt: skip

    pin_to("hot")
    assert run(_entry(w, b))["move_hold"] is None
    pin_to("archive")
    assert run(_entry(w, b))["move_hold"]["to"] == "archive"
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file SET archive_at = %s WHERE file_id = %s",
                   (w.clock.now().date(), b.file_id))  # fmt: skip
    assert run(_entry(w, b))["move_hold"] is None  # its date has come: it may move the blob


def test_the_api_shows_no_wait_once_the_policy_is_gone(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """With no active policy the scheduler looks at nothing, so nothing tries again."""
    from .test_tiering_shared_blob import _entry

    w, a, b = split_pair(db, policy_db_url)
    no_copies_for(w, timedelta(days=36500))
    pin(db, w, b.file_id, "cold")
    tick(w)
    assert run(_entry(w, b))["move_hold"] is not None
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.policy SET state = 'superseded' WHERE dataset_id = %s",
                   (w.dataset_id,))  # fmt: skip
    assert run(_entry(w, b))["move_hold"] is None


# ---------------------------------------------------------------------------
# Overdue, stuck, and the alerts that keep firing on them
# ---------------------------------------------------------------------------


def overdue(db: psycopg.Connection, now: datetime, w: World | None = None) -> int:
    """This world's, since others share the database. Production counts every dataset."""
    datasets = [w.dataset_id] if w is not None else None
    (n,) = db.execute(ARCHIVE_OVERDUE_SQL, {"now": now, "datasets": datasets}).fetchone()
    return n


def test_an_overdue_file_is_counted_until_it_is_in_archive(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """In the two-account layout a stuck read-back waits with no limit. It says so
    while it is stuck, not only when the archive finally happens."""
    w = world(db, policy_db_url, layout="azure_two_account")
    a = store(db, w, "a.segy")
    tick(w)
    base = overdue(db, w.t0 + timedelta(days=181), w)
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    show_queue(db)
    assert overdue(db, w.clock.now(), w) == base  # on its date: not late yet
    w.clock.set(w.t0 + timedelta(days=182))
    assert overdue(db, w.clock.now(), w) == base + 1
    settle(db, w, w.registry)
    assert primary(db, a.file_id)["logical_tier"] == "archive"
    assert file_row(db, a.file_id)["next_action"] == "archive"  # until a sweep looks again
    assert overdue(db, w.clock.now() + MINUTE, w) == base


def test_overdue_starts_an_hour_past_the_end_of_the_date(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """As `tier.archive.late` does: just past midnight isn't late."""
    w = world(db, policy_db_url, layout="azure_two_account")
    store(db, w, "a.segy")
    tick(w)
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    midnight = datetime.combine(
        (w.clock.now() + DAY).date(), datetime.min.time(), tzinfo=w.clock.now().tzinfo
    )
    assert overdue(db, midnight + timedelta(minutes=30), w) == 0
    assert overdue(db, midnight + timedelta(minutes=61), w) == 1


def test_a_file_still_on_its_way_to_an_overriding_pin_isnt_overdue(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Past its date, under a pin that overrode it, with a move to the pin's tier pending."""
    w = world(db, policy_db_url, layout="azure_two_account")
    a = store(db, w, "a.segy")
    tick(w)
    base = overdue(db, w.t0 + timedelta(days=182), w)
    pin(db, w, a.file_id, "cold")
    w.clock.set(w.t0 + timedelta(days=182))
    sweep_only(w)
    assert file_row(db, a.file_id)["next_action"] == "tier_cold"
    assert overdue(db, w.clock.now(), w) == base


def test_a_future_look_isnt_overdue(db: psycopg.Connection, policy_db_url: str) -> None:
    """Under a pin that ends later, the next look is then: nothing is due yet."""
    w = world(db, policy_db_url, layout="azure_two_account")
    a = store(db, w, "a.segy")
    tick(w)
    base = overdue(db, w.t0 + timedelta(days=182), w)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.file SET next_action = 'archive', next_action_at = %s,"
            " archive_at = %s WHERE file_id = %s",
            (w.t0 + timedelta(days=200), (w.t0 + timedelta(days=100)).date(), a.file_id),
        )
    assert overdue(db, w.t0 + timedelta(days=182), w) == base


def test_the_collector_reads_both_counts(db: psycopg.Connection, policy_db_url: str) -> None:
    """The gauges are what the alerts read, at their own bound."""

    async def connect() -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(as_app(policy_db_url, "worker"))

    w, a, b = split_pair(db, policy_db_url, layout="azure_two_account")
    no_copies_for(w, timedelta(days=36500))
    pin(db, w, a.file_id, "cold")
    w.clock.set(w.t0 + timedelta(days=182))
    tick(w)
    later = w.clock.now() + MINUTE
    wall = datetime.now(tz=later.tzinfo)
    first = run(metrics.Collector(connect, now=lambda: wall).collect_once())
    assert first.own_copy_stuck >= 1
    assert (
        run(
            metrics.Collector(connect, now=lambda: wall + timedelta(hours=24.5)).collect_once()
        ).own_copy_stuck
        >= 1
    )
    assert (
        run(
            metrics.Collector(connect, now=lambda: wall + timedelta(days=30)).collect_once()
        ).own_copy_stuck
        == 0
    )
    # The clock the count reads is the collector's.
    (n,) = db.execute(ARCHIVE_OVERDUE_SQL, {"now": later, "datasets": None}).fetchone()
    assert overdue(db, later, w) >= 1 and n >= 1
    assert run(metrics.Collector(connect, now=lambda: later).collect_once()).archive_overdue == n


def test_a_pin_that_overrode_the_date_isnt_overdue(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    tick(w)
    w.clock.set(w.t0 + timedelta(days=170))
    pin(db, w, a.file_id, "hot")
    tick(w)
    base = overdue(db, w.t0 + timedelta(days=181), w)
    w.clock.set(w.t0 + timedelta(days=182))
    tick(w)
    assert primary(db, a.file_id)["logical_tier"] == "hot"
    assert overdue(db, w.clock.now(), w) == base


def test_stale_rows_of_other_steps_dont_hide_an_overdue_file(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The reviewer's run: one overdue file read 1, and 10,000 stale `stub` rows due 30 days
    ago in another dataset made it read 0. `next_action_at` isn't cleared when the scheduler
    stops taking a file, so such rows can pile up."""
    w = world(db, policy_db_url, layout="azure_two_account")
    store(db, w, "a.segy")
    tick(w)
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    later = w.t0 + timedelta(days=182)
    assert overdue(db, later, w) == 1
    (before,) = db.execute(ARCHIVE_OVERDUE_SQL, {"now": later, "datasets": None}).fetchone()
    other = world(db, policy_db_url)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
            " resident_bytes, next_action, next_action_at, archive_at)"
            " SELECT %s, %s, 'stale-' || i, 'file', 'stub', 1, 0, 'stub', %s, %s"
            " FROM generate_series(1, %s) i",
            (other.dataset_id, other.root_id, later - timedelta(days=30),
             (later - timedelta(days=30)).date(), SCAN),
        )  # fmt: skip
    (after,) = db.execute(ARCHIVE_OVERDUE_SQL, {"now": later, "datasets": None}).fetchone()
    assert after == before >= 1
    assert overdue(db, later, w) == 1


def test_archive_rows_with_no_policy_dont_fill_the_scan(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """10,000 Archive-due rows, due longest, in a dataset with no policy. They're left out
    inside the scan, so the overdue file still counts."""
    w = world(db, policy_db_url, layout="azure_two_account")
    store(db, w, "a.segy")
    tick(w)
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    later = w.t0 + timedelta(days=182)
    (before,) = db.execute(ARCHIVE_OVERDUE_SQL, {"now": later, "datasets": None}).fetchone()
    other = world(db, policy_db_url, policy=None)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
            " resident_bytes, next_action, next_action_at, archive_at)"
            " SELECT %s, %s, 'old-' || i, 'file', 'stub', 1, 0, 'archive', %s, %s"
            " FROM generate_series(1, %s) i",
            (other.dataset_id, other.root_id, later - timedelta(days=300),
             (later - timedelta(days=300)).date(), SCAN),
        )  # fmt: skip
    (after,) = db.execute(ARCHIVE_OVERDUE_SQL, {"now": later, "datasets": None}).fetchone()
    assert after == before >= 1


def test_a_policy_with_no_tiering_clears_the_steps_left_behind(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    from omnifile_service.policy.scheduler import reschedule_dataset

    w = world(db, policy_db_url, layout="azure_two_account")
    a = store(db, w, "a.segy")
    s = store(db, w, "s.segy", data=b"s" * 4096)
    tick(w)
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    assert file_row(db, a.file_id)["next_action"] == "archive"
    stub_at = w.clock.now() + DAY
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        # A stub step isn't the tiering policy's, so it stays.
        db.execute("UPDATE catalog.file SET next_action = 'stub', next_action_at = %s"
                   " WHERE file_id = %s", (stub_at, s.file_id))  # fmt: skip
        db.execute("UPDATE catalog.policy SET state = 'superseded' WHERE dataset_id = %s",
                   (w.dataset_id,))  # fmt: skip
        db.execute(
            "INSERT INTO catalog.policy (dataset_id, version, body, approved_by_identity_id)"
            ' VALUES (%s, 99, \'{"mode": "m1"}\', %s)',
            (w.dataset_id, w.identity_id),
        )

    async def go() -> None:
        conn = await _worker(w)
        try:
            async with conn.transaction():
                await reschedule_dataset(conn, w.dataset_id, w.clock.now())
        finally:
            await conn.close()

    run(go())
    row_ = file_row(db, a.file_id)
    assert (row_["next_action"], row_["next_action_at"]) == (None, None)
    row_ = file_row(db, s.file_id)
    assert (row_["next_action"], row_["next_action_at"]) == ("stub", stub_at)


def test_a_dataset_with_no_policy_now_isnt_overdue(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Its rows stay due, and nothing will archive them."""
    w = world(db, policy_db_url, layout="azure_two_account")
    store(db, w, "a.segy")
    tick(w)
    w.clock.set(w.t0 + timedelta(days=180))
    sweep_only(w)
    later = w.t0 + timedelta(days=182)
    assert overdue(db, later, w) == 1
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.policy SET state = 'superseded' WHERE dataset_id = %s",
                   (w.dataset_id,))  # fmt: skip
    assert overdue(db, later, w) == 0


def test_the_overdue_count_is_bounded_by_its_index(db: psycopg.Connection) -> None:
    """The cost is the point: an index range from the oldest entry, a limit, no sort or scan
    of the table, whatever its size."""
    assert f"LIMIT {SCAN}" in ARCHIVE_OVERDUE_SQL
    with db.transaction():
        for knob in ("seqscan", "bitmapscan", "sort"):
            db.execute(f"SET LOCAL enable_{knob} = off")
        plan = "\n".join(
            r[0]
            for r in db.execute(
                "EXPLAIN " + ARCHIVE_OVERDUE_SQL,
                {"now": datetime(2026, 11, 7), "datasets": None},
            ).fetchall()
        )
    assert "Index Scan using file_archive_due" in plan, plan
    assert "next_action = 'archive'" not in plan, plan  # the index holds only those
    assert "Sort" not in plan, plan
    assert "Seq Scan" not in plan, plan


def test_the_stuck_count_stays_up_while_it_waits(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Each day's retry reports again, so the count doesn't lapse between them. The audit's
    own time is the wall clock, which the collector reads too."""
    w, a, b = split_pair(db, policy_db_url)
    no_copies_for(w, timedelta(days=36500))
    pin(db, w, a.file_id, "cold")

    def stuck() -> int:
        (n,) = db.execute(
            OWN_COPY_STUCK_SQL + " AND a.file_id = %(f)s",
            {"since": datetime.now(tz=w.t0.tzinfo) - timedelta(hours=25), "f": a.file_id},
        ).fetchone()
        return n

    assert stuck() == 0
    tick(w)
    assert stuck() == 1
    # Tomorrow's retry adds one, and the alert still fires.
    w.clock.set(w.clock.now() + DAY + MINUTE)
    tick(w)
    assert stuck() == 2
    assert len(held_reports(db, a.file_id)) == 2


# ---------------------------------------------------------------------------
# One audit line for a move waiting behind a split
# ---------------------------------------------------------------------------


def test_a_move_waiting_behind_a_split_says_so_once(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A's split is stuck. B and C agree on Cold, and wait for it: one line, not one a look,
    and none about A."""
    w = world(db, policy_db_url)
    a = store(db, w, "a-secret.segy")
    b = dedupe(db, w, a, "b.segy")
    pin(db, w, a.file_id, "warm")
    registry = one_wants(_ThrottledFor(w.sim, a.file_id), w)
    sweep_only(w)
    settle(db, w, registry, reads=2)
    assert any("own_copy" in m for m in tiering_queue(db))
    pin(db, w, b.file_id, "cold")
    queue_move(db, b, "cold")
    settle(db, w, registry, reads=4)
    [line] = audits(db, b.file_id, "tier.move.waiting")
    assert line["details"]["to"] == "cold"
    assert line["details"]["why"] == tiering.WAITING_BEHIND_SPLIT
    assert "secret" not in str(line)
    assert run(w.sim.head(a.ref)).native_class == "Hot"


def test_each_reason_gets_its_own_line(db: psycopg.Connection, policy_db_url: str) -> None:
    """Once a day for each reason, and a second day says so again."""
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")

    async def say(why: str) -> None:
        conn = await _worker(w)
        try:
            async with conn.transaction():
                row_ = await tiering._load(conn, a.location_id)
                await tiering._report_waiting(conn, row_, T.COLD, why, w.clock.now())
        finally:
            await conn.close()

    run(say(tiering.WAITING_BEHIND_SPLIT))
    run(say(tiering.WAITING_BEHIND_SPLIT))
    run(say(tiering.WAITING_SLOW_COPY))
    assert len(audits(db, a.file_id, "tier.move.waiting")) == 2
    w.clock.set(w.clock.now() + DAY + MINUTE)
    run(say(tiering.WAITING_SLOW_COPY))
    assert len(audits(db, a.file_id, "tier.move.waiting")) == 3


def test_a_move_to_archive_behind_a_split_doesnt_call_itself_waiting(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    b = dedupe(db, w, a, "b.segy")
    pin(db, w, a.file_id, "warm")
    registry = one_wants(_ThrottledFor(w.sim, a.file_id), w)
    sweep_only(w)
    settle(db, w, registry, reads=2)
    pin(db, w, b.file_id, "archive")
    queue_move(db, b, "archive")
    settle(db, w, registry, reads=2)
    assert audits(db, b.file_id, "tier.move.waiting") == []


# ---------------------------------------------------------------------------
# The sweep can't delete the last bytes
# ---------------------------------------------------------------------------


def orphan(db: psycopg.Connection, url: str, **kw: Any) -> tuple[World, Stored, Stored, dict]:
    """A's copy is checked and its message is gone, and the plan changed: the sweep's to
    retire. If it is the last copy of these bytes, nothing else stops the delete."""
    w, a, b = split_pair(db, url, **kw)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    run(drain_verify(w))
    db.execute("DELETE FROM pgmq.q_tiering")
    db.execute("DELETE FROM pgmq.q_verify")
    [t] = [x for x in locations(db, a.file_id) if x["role"] == "secondary"]
    w.clock.set(w.clock.now() + 2 * DAY)
    return w, a, b, t


def refused_rows(db: psycopg.Connection, s: Stored) -> list[dict]:
    return audits(db, s.file_id, "location.delete.refused")


@pytest.mark.parametrize(
    ("how", "outcome"),
    [
        ("missing", "refused_primary_missing"),
        ("size", "refused_primary_size"),
        ("md5", "refused_primary_md5"),
    ],
)
def test_the_sweep_wont_delete_when_the_primary_isnt_there_or_differs(
    db: psycopg.Connection, policy_db_url: str, how: str, outcome: str
) -> None:
    w, a, b, t = orphan(db, policy_db_url)
    assert retire(w) == 1
    if how == "missing":
        run(w.sim.delete(a.ref))
    else:
        # Azure's Get Blob Properties gives no whole-blob CRC: size, MD5, and that the row was
        # read back, are what a delete can check. (S3's CRC: `copy_check`'s own tests.)
        column = {
            "size": "size = size + 1",
            "md5": "md5 = decode('00112233445566778899aabbccddeeff', 'hex')",
        }[how]
        with db.transaction():
            db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            db.execute(f"UPDATE catalog.location SET {column} WHERE location_id = %s",
                       (a.location_id,))  # fmt: skip
    assert drain_retention(w) == [outcome]
    # The bytes stay, the row is back as it was, and a person is told.
    assert run(w.sim.head(_ref(t))).size == 4096
    assert row(db, "SELECT state, verified FROM catalog.location WHERE location_id = %s",
               t["location_id"]) == {"state": "committed", "verified": True}  # fmt: skip
    [refused] = refused_rows(db, a)
    assert refused["details"] == {"location_id": t["location_id"], "why": outcome[8:]}
    assert refused["dataset_id"] == w.dataset_id


def test_good_bytes_over_a_primary_never_read_back_stay(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Azure gives no whole-blob CRC, and this original was never read back. The copy was,
    so it's the one known good: it stays, and a person looks."""
    w, a, b, t = orphan(db, policy_db_url)
    assert retire(w) == 1
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.location SET verified = false, verified_at = NULL, verified_by = NULL,"
            " md5 = NULL WHERE location_id = %s",
            (a.location_id,),
        )
    assert drain_retention(w) == ["refused_primary_unverified"]
    assert run(w.sim.head(_ref(t))).size == 4096


def test_a_retained_secondary_is_checked_the_same_way(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The check comes before "another row uses these bytes", which can be out of date."""
    w = world(db, policy_db_url, worm_days=30)
    a = store(db, w, "a.segy")
    dedupe(db, w, a, "b.segy")
    tick(w)
    pin(db, w, a.file_id, "warm")
    tick(w)
    w.clock.set(w.t0 + timedelta(days=31))
    assert retire(w) == 1
    run(w.sim.delete(_ref(primary(db, a.file_id))))
    assert drain_retention(w) == ["refused_primary_missing"]
    assert run(w.sim.head(a.ref)).size == 4096


class _SwapsOnHead:
    """sim that, the first time the primary is looked at, has it swapped for one never
    read back, as a two-account copy adopted unverified after its source vanished."""

    def __init__(self, sim: Any, url: str, primary_key: str, location_id: int) -> None:
        self._sim = sim
        self._url = url
        self._key = primary_key
        self._lid = location_id
        self.swapped = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)

    async def head(self, ref: Any) -> Any:
        got = await self._sim.head(ref)
        if ref.key == self._key and not self.swapped:
            self.swapped = True
            await asyncio.to_thread(self._swap)
        return got

    def _swap(self) -> None:
        with psycopg.connect(self._url) as c:
            c.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            c.execute(
                "UPDATE catalog.location SET verified = false, verified_at = NULL,"
                " verified_by = NULL, md5 = NULL WHERE location_id = %s",
                (self._lid,),
            )


async def _delete_once(w: World, registry: BackendRegistry, location_id: int) -> str:
    conn = await _worker(w)
    try:
        async with conn.transaction():
            return await worm_sync.delete_blob(conn, registry, location_id, w.clock.now())
    finally:
        await conn.close()


def test_a_primary_swapped_during_the_look_is_looked_at_again(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Selected while the primary was read back, swapped for one that wasn't before the
    delete. Under the primary's lock it reads again, comes back later, and then refuses:
    this copy is the good bytes, and the primary's were never checked."""
    w, a, b, t = orphan(db, policy_db_url)
    assert retire(w) == 1
    swaps = _SwapsOnHead(w.sim, policy_db_url, a.ref.key, a.location_id)
    registry = one_wants(swaps, w)
    with pytest.raises(Defer):
        run(_delete_once(w, registry, t["location_id"]))
    assert swaps.swapped
    assert run(_delete_once(w, registry, t["location_id"])) == "refused_primary_unverified"
    assert run(w.sim.head(_ref(t))).size == 4096


def test_a_swap_under_way_makes_the_delete_wait(db: psycopg.Connection, policy_db_url: str) -> None:
    """The primary's swaps hold its blob's lock. The delete doesn't decide while one does."""
    w, a, b, t = orphan(db, policy_db_url)
    assert retire(w) == 1
    other = psycopg.connect(policy_db_url)
    try:
        other.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"omnifile:blob-tier:{w.backend_id}/{a.ref.container}/{a.ref.key}",),
        )
        with pytest.raises(Defer):
            run(_delete_once(w, w.registry, t["location_id"]))
    finally:
        other.rollback()
        other.close()
    assert run(_delete_once(w, w.registry, t["location_id"])) == "deleted"


def test_a_failed_copy_still_goes_over_an_unread_primary(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A copy whose read-back was denied is never the good bytes."""
    w, a, b, t = orphan(db, policy_db_url)
    assert retire(w) == 1
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.location SET verified = false, verified_at = NULL,"
                   " verified_by = NULL, md5 = NULL WHERE location_id = %s",
                   (a.location_id,))  # fmt: skip
        db.execute(
            "INSERT INTO catalog.audit_log (actor, action, decision, dataset_id, file_id,"
            " version_id, details) VALUES ('system:verify', 'location.verify', 'deny',"
            " %s, %s, %s, %s::jsonb)",
            (
                w.dataset_id,
                a.file_id,
                a.version_id,
                json_text({"location_id": str(t["location_id"])}),
            ),
        )
    assert run(_delete_once(w, w.registry, t["location_id"])) == "deleted"


def test_the_sweep_deletes_when_the_primary_is_there_and_matches(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b, t = orphan(db, policy_db_url)
    assert retire(w) == 1
    assert drain_retention(w) == ["deleted"]
    with pytest.raises(ObjectNotFound):
        run(w.sim.head(_ref(t)))
    assert refused_rows(db, a) == []


def test_a_row_promoted_just_before_the_sweeps_update_is_left_alone(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep reads, then writes. If the row became the record between the two, it must
    not be marked for deletion."""
    w, a, b, t = orphan(db, policy_db_url)
    real = worm_sync.set_actor

    async def promote_first(conn: Any, actor: str) -> None:
        await real(conn, actor)
        with psycopg.connect(policy_db_url) as other:
            other.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            other.execute("UPDATE catalog.location SET role = 'secondary' WHERE location_id = %s",
                          (a.location_id,))  # fmt: skip
            other.execute("UPDATE catalog.location SET role = 'primary' WHERE location_id = %s",
                          (t["location_id"],))  # fmt: skip

    monkeypatch.setattr(worm_sync, "set_actor", promote_first)
    assert retire(w) == 0
    assert row(db, "SELECT role, state FROM catalog.location WHERE location_id = %s",
               t["location_id"]) == {"role": "primary", "state": "committed"}  # fmt: skip
    assert drain_retention(w) == []


def test_the_same_container_on_another_backend_isnt_a_splits(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A migration's copy can have the same container name in another provider."""
    w = world(db, policy_db_url)
    w2 = world(db, policy_db_url)
    a = store(db, w, "a.segy")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
            " object_key, role, state, logical_tier, storage_class, size, crc64nvme,"
            " committed_at, tier_changed_at) SELECT file_id, version_id, %s, container,"
            " 'its-own-name', 'secondary', 'committed', 'hot', 'Hot', size,"
            " crc64nvme, %s, %s FROM catalog.location WHERE location_id = %s",
            (w2.backend_id, w.clock.now(), w.clock.now(), a.location_id),
        )
    w.clock.set(w.clock.now() + 2 * DAY)
    assert retire(w) == 0


@pytest.mark.parametrize(
    ("age", "goes"),
    [(MINUTE, False), (timedelta(hours=23), False), (timedelta(hours=25), True)],
    ids=["a_minute", "23_hours", "25_hours"],
)
def test_the_grace_is_a_day(
    db: psycopg.Connection, policy_db_url: str, age: timedelta, goes: bool
) -> None:
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    db.execute("DELETE FROM pgmq.q_tiering")
    db.execute("DELETE FROM pgmq.q_verify")
    w.clock.set(w.clock.now() + age)
    assert retire(w) == (1 if goes else 0)


# ---------------------------------------------------------------------------
# Daily, once across replicas, and it catches up
# ---------------------------------------------------------------------------


def _sweep_jobs(w: World) -> tuple[Any, Any]:
    fake = SimpleNamespace(registry=w.registry, clock=w.clock, consumer_config=ConsumerConfig())
    jobs = retention_jobs(fake)  # type: ignore[arg-type]
    sweep = next(j for j in jobs if getattr(j, "name", None) == "replaced_secondaries")
    queue = next(j for j in jobs if getattr(j, "queue", None) == "retention")
    return sweep, queue


async def _bootstrap(w: World, job: Any) -> int:
    conn = await _worker(w)
    try:
        return await job.fn(conn, w.clock.now())
    finally:
        await conn.close()


def left_behind_messages(db: psycopg.Connection) -> list[dict]:
    return [
        r[0]
        for r in db.execute(
            "SELECT message FROM pgmq.q_retention WHERE message->>'op' = %s", (LEFT_BEHIND_OP,)
        )
    ]


#: The audit log is append-only, so a pass one test records is there for the next. Each runs
#: later than the last, in the order of this file.
SWEEP_TEST_DAYS = {"once_a_day": 3000, "aside": 3010, "backlog": 3020}


def _pass_done_at(db: psycopg.Connection, when: datetime, *, retired: int = 0) -> None:
    db.execute("SELECT set_config('omnifile.actor', 'system:test', false)")
    db.execute(
        "SELECT catalog.audit(%s, NULL, NULL, NULL, NULL, %s)",
        (LEFT_BEHIND_SWEPT, Jsonb({"at": when.isoformat(), "retired": retired})),
    )


def test_every_replica_starting_queues_one_sweep_a_day(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A replica starting runs every sweep job at once. The first to find no message and no
    pass in the last day sends it, so a restart doesn't sweep again."""
    db.execute("DELETE FROM pgmq.q_retention")
    w = world(db, policy_db_url)
    w.clock.set(w.t0 + timedelta(days=SWEEP_TEST_DAYS["once_a_day"]))
    job, _ = _sweep_jobs(w)
    assert run(_bootstrap(w, job)) == 1
    assert run(_bootstrap(w, job)) == 0
    assert len(left_behind_messages(db)) == 1
    # A dead-lettered one comes back within the hour.
    db.execute("DELETE FROM pgmq.q_retention")
    assert run(_bootstrap(w, job)) == 1
    db.execute("DELETE FROM pgmq.q_retention")
    # A pass finished: nothing for a day, and then the next.
    _pass_done_at(db, w.clock.now())
    w.clock.set(w.clock.now() + timedelta(hours=23))
    assert run(_bootstrap(w, job)) == 0
    assert left_behind_messages(db) == []
    w.clock.set(w.clock.now() + timedelta(hours=2))
    assert run(_bootstrap(w, job)) == 1


def test_a_replica_deciding_at_the_same_moment_stands_aside(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    db.execute("DELETE FROM pgmq.q_retention")
    w = world(db, policy_db_url)
    w.clock.set(w.t0 + timedelta(days=SWEEP_TEST_DAYS["aside"]))
    job, _ = _sweep_jobs(w)
    other = psycopg.connect(policy_db_url)
    try:
        other.execute("SELECT pg_advisory_xact_lock(hashtext('omnifile:retire-left-behind'))")
        assert run(_bootstrap(w, job)) == 0
        assert left_behind_messages(db) == []
    finally:
        other.rollback()
        other.close()


def test_a_backlog_catches_up_in_batches_and_then_rests(
    db: psycopg.Connection, policy_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pages of secondaries follow each other at once, so no backlog stays billed for
    months, and the last one records the pass."""
    monkeypatch.setattr(worm_sync.retire_replaced_secondaries, "__kwdefaults__",
                        {"limit": 2, "after": 0, "dataset_ids": None})  # fmt: skip
    db.execute("DELETE FROM pgmq.q_retention")
    w = world(db, policy_db_url)
    orphans = []
    for i in range(5):
        a = store(db, w, f"{i}.segy", data=bytes([i]) * 4096)
        with db.transaction():
            db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            (lid,) = db.execute(
                "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
                " object_key, role, state, logical_tier, storage_class, size, crc64nvme,"
                " committed_at, tier_changed_at) SELECT file_id, version_id, backend_id,"
                " container, 'orphan-' || %s, 'secondary', 'pending', 'hot', 'Hot', size,"
                " crc64nvme, %s, %s FROM catalog.location WHERE location_id = %s"
                " RETURNING location_id",
                (str(i), w.clock.now(), w.clock.now(), a.location_id),
            ).fetchone()
        orphans.append(lid)
    w.clock.set(w.t0 + timedelta(days=SWEEP_TEST_DAYS["backlog"]))
    job, queue = _sweep_jobs(w)
    # The pass the hourly job would start, but from this test's rows: others share the table.
    db.execute(
        "SELECT pgmq.send('retention', %s::jsonb)",
        (Jsonb({"op": LEFT_BEHIND_OP, "after": str(min(orphans) - 1)}),),
    )

    async def connect() -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(as_app(w.url, "worker"), autocommit=True)

    async def go() -> None:
        c = Consumer(connect, "retention", queue.handler, ConsumerConfig())
        try:
            await c.drain(limit=50)
        finally:
            await c.close()

    run(go())
    states = db.execute(
        "SELECT state FROM catalog.location WHERE location_id = ANY(%s)", (orphans,)
    ).fetchall()
    assert {s for (s,) in states} <= {"deleted"}, states
    # Nothing waits in the queue, where a day's wait would read as a stuck queue, and the
    # next day's check reads what the last page wrote.
    assert left_behind_messages(db) == []
    [(details,)] = db.execute(
        "SELECT details FROM catalog.audit_log WHERE action = %s ORDER BY audit_id DESC LIMIT 1",
        (LEFT_BEHIND_SWEPT,),
    ).fetchall()
    assert details["retired"] >= 5
    assert details["at"] == w.clock.now().isoformat()
    assert run(_bootstrap(w, job)) == 0


def test_one_call_reads_one_page(db: psycopg.Connection, policy_db_url: str) -> None:
    """It is the page that bounds a transaction, not what it finds."""
    w, a, b, t = orphan(db, policy_db_url)
    c = store(db, w, "c.segy", data=b"c" * 4096)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
            " object_key, role, state, logical_tier, storage_class, size, crc64nvme,"
            " committed_at, tier_changed_at) SELECT file_id, version_id, backend_id, container,"
            " 'orphan-c', 'secondary', 'pending', 'hot', 'Hot', size, crc64nvme, %s, %s"
            " FROM catalog.location WHERE location_id = %s",
            (w.t0, w.t0, c.location_id),
        )

    async def one_page() -> tuple[int, int | None]:
        conn = await _worker(w)
        try:
            return await worm_sync.retire_replaced_secondaries(
                conn, w.clock.now(), limit=1, after=t["location_id"] - 1,
                dataset_ids=[w.dataset_id],
            )  # fmt: skip
        finally:
            await conn.close()

    done, more = run(one_page())
    assert (done, more) == (1, t["location_id"])


def test_the_cursor_carries_on_from_where_it_stopped(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    db.execute("DELETE FROM pgmq.q_tiering")
    db.execute("DELETE FROM pgmq.q_verify")
    w.clock.set(w.clock.now() + 2 * DAY)
    [t] = [x for x in locations(db, a.file_id) if x["role"] == "secondary"]

    async def after(start: int) -> tuple[int, int | None]:
        conn = await _worker(w)
        try:
            return await worm_sync.retire_replaced_secondaries(
                conn, w.clock.now(), after=start, dataset_ids=[w.dataset_id]
            )
        finally:
            await conn.close()

    assert run(after(t["location_id"]))[0] == 0  # past it
    assert run(after(t["location_id"] - 1))[0] == 1


def test_the_index_is_what_a_page_reads(db: psycopg.Connection) -> None:
    with db.transaction():
        for knob in ("seqscan", "bitmapscan", "sort"):
            db.execute(f"SET LOCAL enable_{knob} = off")
        plan = "\n".join(
            r[0]
            for r in db.execute(
                "EXPLAIN " + worm_sync._SECONDARY_PAGE, {"after": 0, "limit": 500}
            ).fetchall()
        )
    assert "location_secondary_live" in plan, plan
    assert "Sort" not in plan, plan


# ---------------------------------------------------------------------------
# Stamps of one blob take turns
# ---------------------------------------------------------------------------


async def _stamp_only(w: World, location_id: int, registry: BackendRegistry | None = None) -> Any:
    conn = await _worker(w)
    try:
        return await stamp_worm(conn, registry or w.registry, w.clock.now(), only=[location_id])
    finally:
        await conn.close()


def _hold_on(db: psycopg.Connection, s: Stored, held: bool) -> None:
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file_version SET legal_hold = %s WHERE version_id = %s",
                   (held, s.version_id))  # fmt: skip


class _PausedHold:
    """sim whose hold change has been decided and not yet reached the provider."""

    def __init__(self, sim: Any) -> None:
        self._sim = sim
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)

    async def set_legal_hold(self, ref: Any, on: bool) -> Any:
        self.reached.set()
        await self.release.wait()
        return await self._sim.set_legal_hold(ref, on)


def test_a_hold_set_during_anothers_clear_isnt_cleared_at_the_provider(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A's clear has checked that nothing else holds the blob and is on its way to the
    provider. B's hold is set then. B's stamp must not run until A's is done, or A's clear
    lands last and takes B's hold off. It stands aside, its flag stays up, and its next
    sweep sets the hold."""
    w, a, b = split_pair(db, policy_db_url, **WORM_SHARE)
    _hold_on(db, a, True)
    run(_stamp_only(w, a.location_id))
    assert run(w.sim.head(a.ref)).legal_hold is True
    _hold_on(db, a, False)
    paused = _PausedHold(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]

    async def race() -> None:
        clearing = asyncio.create_task(_stamp_only(w, a.location_id, registry))
        await asyncio.wait_for(paused.reached.wait(), 10)
        await asyncio.to_thread(_hold_on_elsewhere, policy_db_url, b)
        done = await asyncio.wait_for(_stamp_only(w, b.location_id), 10)
        assert done.stamped == 0
        paused.release.set()
        await asyncio.wait_for(clearing, 10)

    run(race())
    assert row(db, "SELECT worm_sync_needed FROM catalog.location WHERE location_id = %s",
               b.location_id) == {"worm_sync_needed": True}  # fmt: skip
    assert run(_stamp_only(w, b.location_id)).stamped == 1
    assert run(w.sim.head(a.ref)).legal_hold is True
    assert run(w.sim.get_tags(a.ref))["omnifile_hold"] == "1"


def _hold_on_elsewhere(url: str, s: Stored) -> None:
    with psycopg.connect(url) as c:
        _hold_on(c, s, True)


def test_a_stamp_holds_no_transaction_across_its_calls(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The edge feed's horizon is the oldest open id. The blob's lock is a session lock,
    which takes none, and is let go after."""
    w, a, b = split_pair(db, policy_db_url, **WORM_SHARE)
    _hold_on(db, a, True)
    paused = _PausedHold(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]
    from .test_tiering_shared_blob import _xids_held

    async def race() -> None:
        stamping = asyncio.create_task(_stamp_only(w, a.location_id, registry))
        await asyncio.wait_for(paused.reached.wait(), 10)
        assert await asyncio.to_thread(_xids_held, w) == 0
        paused.release.set()
        await asyncio.wait_for(stamping, 10)

    run(race())


def test_the_blobs_lock_is_let_go_after_each_stamp(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A session lock outlives its statement, and a worker's connection is long-lived."""
    w, a, b = split_pair(db, policy_db_url, **WORM_SHARE)
    _hold_on(db, a, True)

    async def go() -> int:
        conn = await _worker(w)
        try:
            await stamp_worm(conn, w.registry, w.clock.now(), only=[a.location_id])
            cur = await conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                " AND pid = pg_backend_pid()"
            )
            return (await cur.fetchone())[0]  # type: ignore[index]
        finally:
            await conn.close()

    assert run(go()) == 0


def test_a_row_that_never_had_a_hold_doesnt_write_no_hold_on_a_held_blob(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """B's stamp is for its retention. It says nothing about a hold that is A's."""
    w, a, b = split_pair(db, policy_db_url, **WORM_SHARE)
    _hold_on(db, a, True)
    run(_stamp_only(w, a.location_id))
    assert row(db, "SELECT worm_sync_needed FROM catalog.location WHERE location_id = %s",
               b.location_id) == {"worm_sync_needed": True}  # fmt: skip
    run(_stamp_only(w, b.location_id))
    assert run(w.sim.get_tags(a.ref))["omnifile_hold"] == "1"
    assert run(w.sim.head(a.ref)).legal_hold is True


def test_a_cleared_row_doesnt_write_no_hold_on_a_blob_another_file_holds(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = split_pair(db, policy_db_url, **WORM_SHARE)
    _hold_on(db, a, True)
    _hold_on(db, b, True)
    run(_stamp_only(w, a.location_id))
    run(_stamp_only(w, b.location_id))
    _hold_on(db, a, False)
    run(_stamp_only(w, a.location_id))
    assert run(w.sim.get_tags(a.ref))["omnifile_hold"] == "1"
    assert run(w.sim.head(a.ref)).legal_hold is True
    _hold_on(db, b, False)
    run(_stamp_only(w, b.location_id))
    assert run(w.sim.get_tags(a.ref))["omnifile_hold"] == "0"
    assert run(w.sim.head(a.ref)).legal_hold is False


# ---------------------------------------------------------------------------
# The swap takes its rows before its file
# ---------------------------------------------------------------------------


def test_the_swap_takes_its_rows_lowest_first_before_any_file(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The feed takes rows then files. Blocked on the copy's row, the swap holds the
    original's row and no file yet."""
    w, a, b = split_pair(db, policy_db_url)
    pin(db, w, a.file_id, "cold")
    queue_move(db, a, "cold")
    steps(w, 2)
    run(drain_verify(w))
    show_queue(db)
    steps(w, 2)  # verifying, its tier
    [t] = [x for x in locations(db, a.file_id) if x["role"] == "secondary"]
    show_queue(db)

    async def race() -> None:
        from .test_own_copy import _steps

        holder = await _worker(w)
        try:
            async with holder.transaction():
                await holder.execute(LOCK_LOCATION + " FOR UPDATE", (t["location_id"],))
                worker = asyncio.create_task(_steps(w, 1))
                await asyncio.sleep(0.7)
                assert await asyncio.to_thread(_probe_location, w, a.location_id)
                assert not await asyncio.to_thread(_probe_file, w, a.file_id)
        finally:
            await holder.close()
        await asyncio.wait_for(worker, 10)

    run(race())
    assert primary(db, a.file_id)["location_id"] == t["location_id"]


# Files that agree go on sharing, whatever the copies do. (`test_tiering_shared_blob.py`,
# `test_files_that_agree_move_with_copies_failing`, and
# `test_a_single_reference_is_never_held` hold the same for the worker.)


def test_a_mover_reached_by_two_workers_with_files_agreeing_doesnt_wait(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, a, b = split_pair(db, policy_db_url)
    agree(db, w, "cold", a, b)
    no_copies_for(w, timedelta(days=36500))
    paused = _Paused(w.sim)
    registry = BackendRegistry.from_mapping({w.backend_id: paused})  # type: ignore[dict-item]
    paused.release.set()
    assert run(_run_move(w, move_message(a, "cold"), registry)) is False
    assert paused.calls == ["cold"]
    assert primary(db, b.file_id)["logical_tier"] == "cold"
    assert held_reports(db, a.file_id) == []
