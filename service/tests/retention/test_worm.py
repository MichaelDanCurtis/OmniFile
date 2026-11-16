"""WORM per share (decision 31) on sim's `azure_worm` layout: enable with a
typed confirmation, auto-lock after the validation window, extend-only once
locked, per-version retention and legal holds stamped on the backend with
state tags, nothing retained deletable by any path, and recall copies kept
out of the WORM container."""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from omnifile_service.backends.base import Immutable, LogicalTier, RestoreSpeed
from omnifile_service.retention import (
    RetentionError,
    cancel_worm_test,
    clear_legal_hold,
    delete_blob,
    enable_worm,
    expire_recall_copies,
    extend_worm_retention,
    prune_versions,
    set_legal_hold,
)
from omnifile_service.retention.common import TAG_HOLD, TAG_RETAIN, TAG_WORM
from omnifile_service.worker.changefeed_sync import register_container, sync_container
from omnifile_service.worker.queue import send

from .support import (
    add_dir,
    add_file,
    audits,
    clock_days_ago,
    commit_content,
    drain,
    expect_sqlstate,
    location,
    run,
    run_sweep,
    seed,
    share,
    version,
    with_conn,
    worker,
)

H = timedelta(hours=1)
D = timedelta(days=1)


def _enable(connect, w, clock, *, days=30, window=4 * H, typed=None):
    return with_conn(
        connect,
        lambda c: enable_worm(c, w.share_id, retention_days=days,
                              confirmation=typed if typed is not None else w.share_name,
                              actor=f"user:{w.admin}", now=clock.now(), test_window=window),
    )  # fmt: skip


def test_enable_needs_the_typed_share_name_and_a_worm_layout(
    db: psycopg.Connection, connect
) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock, layout="azure_worm")
    with pytest.raises(RetentionError, match="type the share name"):
        _enable(connect, w, clock, typed="yes")
    with pytest.raises(RetentionError, match="at most 24 hours"):
        _enable(connect, w, clock, window=25 * H)
    deadline = _enable(connect, w, clock)
    s = share(db, w.share_id)
    assert (s["worm_enabled"], s["worm_mode"], s["worm_retention_days"]) == (
        True, "unlocked_test", 30)  # fmt: skip
    assert s["worm_lock_deadline"] == deadline == clock.now() + 4 * H
    assert s["worm_confirmation"] == w.share_name
    assert audits(db, w.dataset_id, "share.worm") == ["share.worm.enable", "share.worm.confirm"]
    with pytest.raises(RetentionError, match="already on"):
        _enable(connect, w, clock)

    # A two-account (Smart) dataset can't be WORM: the catalog refuses it.
    w2 = seed(db, clock)
    with pytest.raises(psycopg.Error) as e:
        _enable(connect, w2, clock)
    assert e.value.sqlstate == "OF004"


def test_enable_autolocks_after_the_window_then_retention_only_grows(
    db: psycopg.Connection, connect
) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock, layout="azure_worm")
    early = commit_content(db, w, add_file(db, w, "before.segy"), b"old", clock.now())
    assert version(db, early.version_id)["retention_until"] is None  # before WORM
    _enable(connect, w, clock, days=30)
    wk = worker(connect, w, clock)
    drain(wk, "retention")  # the container policy, unlocked
    assert share(db, w.share_id)["worm_policy_state"] == "unlocked"
    w.sim.set_container_policy(w.container, 30, locked=False)  # unlocked: still changeable

    # A version committed in the window is retained from its commit,
    # stamped unlocked on the backend, with state tags.
    clock.advance(H)
    v = commit_content(db, w, add_file(db, w, "line.segy"), b"seismic", clock.now())
    until = version(db, v.version_id)["retention_until"]
    assert until == clock.now() + 30 * D
    assert location(db, v.location_id)["worm_sync_needed"]
    run_sweep(wk, "worm_stamp")
    head = run(w.sim.head(v.ref))
    assert head.retention_until == until
    tags = run(w.sim.get_tags(v.ref))
    assert (tags[TAG_WORM], tags[TAG_HOLD]) == ("unlocked", "0")
    assert tags[TAG_RETAIN] == until.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert not location(db, v.location_id)["worm_sync_needed"]

    # Not yet due: nothing locks.
    run_sweep(wk, "worm_autolock")
    assert share(db, w.share_id)["worm_mode"] == "unlocked_test"
    clock.advance(3 * H + timedelta(minutes=1))
    run_sweep(wk, "worm_autolock")
    s = share(db, w.share_id)
    assert (s["worm_mode"], s["worm_locked_at"]) == ("locked", clock.now())
    # Versions already in the share are now retained too, and the live ones
    # (including those committed in the window) count from the lock.
    assert version(db, early.version_id)["retention_until"] == clock.now() + 30 * D
    assert version(db, v.version_id)["retention_until"] == clock.now() + 30 * D
    until = clock.now() + 30 * D
    drain(wk, "retention")
    run_sweep(wk, "worm_stamp")
    assert share(db, w.share_id)["worm_policy_state"] == "locked"
    assert run(w.sim.get_tags(v.ref))[TAG_WORM] == "locked"
    assert run(w.sim.get_tags(early.ref))[TAG_WORM] == "locked"
    assert location(db, v.location_id)["worm_stamped_locked"]

    # Locked: shortening fails at every layer.
    with pytest.raises(Immutable):
        w.sim.set_container_policy(w.container, 29, locked=True)
    with pytest.raises(Immutable):
        run(w.sim.set_retention(v.ref, until - D))
    with pytest.raises(RetentionError, match="never shortened"):
        with_conn(connect, lambda c: extend_worm_retention(c, w.share_id, 29, actor="user:1"))
    expect_sqlstate(db, "OF002", "UPDATE catalog.share SET worm_retention_days = 29"
                    " WHERE share_id = %s", (w.share_id,))  # fmt: skip
    expect_sqlstate(db, "OF002", "UPDATE catalog.share SET worm_enabled = false,"
                    " worm_mode = NULL, worm_locked_at = NULL WHERE share_id = %s",
                    (w.share_id,))  # fmt: skip
    with pytest.raises(RetentionError, match="WORM is locked"):
        with_conn(connect, lambda c: cancel_worm_test(c, w.share_id, actor="user:1",
                                                       now=clock.now()))  # fmt: skip
    # Extending works, reaches the container policy, and extends the
    # versions already there (and their backend stamps).
    with_conn(connect, lambda c: extend_worm_retention(c, w.share_id, 45, actor="user:1"))
    drain(wk, "retention")
    s = share(db, w.share_id)
    assert (s["worm_retention_days"], s["worm_policy_days"]) == (45, 45)
    ext = version(db, v.version_id)
    assert ext["retention_until"] == ext["committed_at"] + 45 * D
    run_sweep(wk, "worm_stamp")
    assert run(w.sim.head(v.ref)).retention_until == ext["committed_at"] + 45 * D
    actions = audits(db, w.dataset_id, "share.worm")
    assert actions[-2:] == ["share.worm.lock", "share.worm.retention"]


def test_cancel_inside_the_window_keeps_given_retention(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock, layout="azure_worm")
    _enable(connect, w, clock, days=10)
    v = commit_content(db, w, add_file(db, w, "x"), b"x", clock.now())
    with_conn(connect, lambda c: cancel_worm_test(c, w.share_id, actor="user:1", now=clock.now()))
    s = share(db, w.share_id)
    assert (s["worm_enabled"], s["worm_mode"]) == (False, None)
    assert version(db, v.version_id)["retention_until"] is not None
    wk = worker(connect, w, clock)
    drain(wk, "retention")
    assert share(db, w.share_id)["worm_policy_state"] == "none"


def test_a_retained_version_cannot_be_deleted_by_any_path(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(3)
    w = seed(db, clock, layout="azure_worm")
    _enable(connect, w, clock, days=30, window=H)
    clock.advance(2 * H)
    wk = worker(connect, w, clock)
    run_sweep(wk, "worm_autolock")
    f = add_file(db, w, "evidence.segy")
    v1 = commit_content(db, w, f, b"first", clock.now())
    commit_content(db, w, f, b"second", clock.now() + H)  # v1 superseded
    clock.advance(2 * D)
    run_sweep(wk, "worm_stamp")

    # Pruning (depth 0, no grace) skips it.
    pruned = with_conn(connect, lambda c: prune_versions(c, clock.now(), dataset_id=w.dataset_id,
                                                         grace=timedelta(0)))  # fmt: skip
    assert v1.version_id not in pruned.versions
    # The catalog refuses every direct route.
    vid, lid = v1.version_id, v1.location_id
    expect_sqlstate(db, "OF002", "UPDATE catalog.file_version SET state = 'pruned'"
                    " WHERE version_id = %s", (vid,))  # fmt: skip
    expect_sqlstate(db, "OF002", "DELETE FROM catalog.file_version WHERE version_id = %s", (vid,))
    shorten = "UPDATE catalog.file_version SET retention_until = retention_until - interval '1 day'"
    expect_sqlstate(db, "OF002", shorten + " WHERE version_id = %s", (vid,))
    expect_sqlstate(db, "OF002", "UPDATE catalog.location SET state = 'deleting',"
                    " verified = false WHERE location_id = %s", (lid,))  # fmt: skip
    expect_sqlstate(db, "OF002", "UPDATE catalog.location SET role = 'staging'"
                    " WHERE location_id = %s", (lid,))  # fmt: skip
    expect_sqlstate(db, "OF002", "DELETE FROM catalog.location WHERE location_id = %s", (lid,))
    # A forged delete message does nothing (the location isn't `deleting`).
    with_conn(connect, lambda c: send(c, "retention", {"op": "delete_blob",
                                                       "location_id": str(lid)}))  # fmt: skip
    drain(wk, "retention")
    assert location(db, lid)["state"] == "committed"
    # The backend refuses too.
    with pytest.raises(Immutable):
        run(w.sim.delete(v1.ref))
    assert run(w.sim.head(v1.ref)).size == 5


def test_legal_hold_on_a_folder_is_stamped_audited_and_released(
    db: psycopg.Connection, connect
) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock, layout="azure_worm")
    folder = add_dir(db, w, w.root_id, "case-17")
    a = commit_content(db, w, add_file(db, w, "a", folder), b"a", clock.now())
    other = commit_content(db, w, add_file(db, w, "b"), b"b", clock.now())
    hold = with_conn(
        connect,
        lambda c: set_legal_hold(c, folder, reason="case 17", actor="app:legal",
                                 app="legal-tool"),
    )  # fmt: skip
    # A second, file-level hold on `a`.
    hold2 = with_conn(
        connect,
        lambda c: set_legal_hold(c, a.file_id, reason="case 18", actor=f"user:{w.admin}",
                                 identity_id=w.admin),
    )  # fmt: skip
    # A version committed later under the folder is held from the start.
    later = commit_content(db, w, add_file(db, w, "c", folder), b"c", clock.now() + H)
    assert version(db, later.version_id)["legal_hold"]
    assert version(db, a.version_id)["legal_hold"]
    assert not version(db, other.version_id)["legal_hold"]

    wk = worker(connect, w, clock)
    run_sweep(wk, "worm_stamp")
    for s in (a, later):
        assert run(w.sim.head(s.ref)).legal_hold
        assert run(w.sim.get_tags(s.ref))[TAG_HOLD] == "1"
    with pytest.raises(Immutable):
        run(w.sim.delete(a.ref))

    # Releasing the folder hold keeps `a` held (its own hold) and frees `c`.
    n = with_conn(
        connect,
        lambda c: clear_legal_hold(
            c, hold, actor=f"user:{w.admin}", identity_id=w.admin, now=clock.now()
        ),
    )
    assert n == 1
    assert version(db, a.version_id)["legal_hold"]
    assert not version(db, later.version_id)["legal_hold"]
    run_sweep(wk, "worm_stamp")
    assert not run(w.sim.head(later.ref)).legal_hold
    assert run(w.sim.get_tags(later.ref))[TAG_HOLD] == "0"
    assert run(w.sim.head(a.ref)).legal_hold
    with_conn(
        connect,
        lambda c: clear_legal_hold(
            c, hold2, actor=f"user:{w.admin}", identity_id=w.admin, now=clock.now()
        ),
    )
    run_sweep(wk, "worm_stamp")
    assert not run(w.sim.head(a.ref)).legal_hold
    assert audits(db, w.dataset_id, "hold.") == ["hold.set", "hold.set", "hold.release",
                                                 "hold.release"]  # fmt: skip
    assert audits(db, w.dataset_id, "version.legal_hold").count("version.legal_hold") >= 3


def test_recall_copies_land_outside_the_worm_container(db: psycopg.Connection, connect) -> None:
    """Archive in place, recall by copy into the non-WORM recall container;
    the copy expires after the window while the original stays immutable."""
    clock = clock_days_ago(10)
    w = seed(db, clock, layout="azure_worm")
    _enable(connect, w, clock, days=30, window=H)
    clock.advance(2 * H)
    wk = worker(connect, w, clock)
    run_sweep(wk, "worm_autolock")
    v = commit_content(db, w, add_file(db, w, "deep.segy"), b"seismic" * 100, clock.now())
    run_sweep(wk, "worm_stamp")
    run(w.sim.set_class(v.ref, LogicalTier.ARCHIVE))  # in place, allowed under WORM
    for c in (w.container, "worm/recall"):
        with_conn(connect, lambda conn, c=c: register_container(conn, w.backend_id, c))
    with_conn(connect, lambda c: sync_container(c, w.registry, w.backend_id, w.container,
                                                clock.now()))  # fmt: skip

    handle = run(w.sim.restore(v.ref, RestoreSpeed.STANDARD))
    assert handle.target is not None and handle.target.container == "worm/recall"
    (tlid,) = db.execute(
        "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
        " backend_version_id, role, state, logical_tier, storage_class, size)"
        " VALUES (%s, %s, %s, %s, %s, %s, 'recall_copy', 'pending', 'hot', 'Hot', 700)"
        " RETURNING location_id",
        (v.file_id, v.version_id, w.backend_id, handle.target.container, handle.target.key,
         handle.target.version_id),
    ).fetchone()  # fmt: skip
    (rid,) = db.execute(
        "INSERT INTO catalog.restore (location_id, backend_id, mechanism, speed, provider_ref,"
        " target_location_id, state, triggered_by) VALUES (%s, %s, 'azure_copy_rehydrate',"
        " 'standard', %s, %s, 'in_progress', 'user_recall') RETURNING restore_id",
        (v.location_id, w.backend_id, handle.provider_ref, tlid),
    ).fetchone()

    # The change feed completes it (15 h Standard rehydrate).
    clock.advance(15 * H + timedelta(minutes=1))
    for c in (w.container, "worm/recall"):
        with_conn(connect, lambda conn, c=c: sync_container(conn, w.registry, w.backend_id, c,
                                                            clock.now()))  # fmt: skip
    state, completed = db.execute(
        "SELECT state, completed_at FROM catalog.restore WHERE restore_id = %s", (rid,)
    ).fetchone()
    assert state == "completed" and completed is not None
    assert location(db, tlid)["state"] == "committed"
    assert run(w.sim.head(handle.target)).size == 700

    # After the user's window the copy goes; the archived original stays.
    clock.advance(8 * D)
    n = with_conn(connect, lambda c: expire_recall_copies(c, clock.now()))
    assert n == 1
    # Queued, in one transaction with marking it: the `retention` consumer deletes it.
    assert location(db, tlid)["state"] == "deleting"
    assert db.execute(
        "SELECT count(*) FROM pgmq.q_retention WHERE message->>'op' = 'delete_blob'"
        " AND message->>'location_id' = %s",
        (str(tlid),),
    ).fetchone() == (1,)
    assert with_conn(connect, lambda c: delete_blob(c, w.registry, tlid, clock.now())) == "deleted"
    assert location(db, tlid)["state"] == "deleted"
    assert location(db, v.location_id)["state"] == "committed"
    with pytest.raises(Immutable):
        run(w.sim.delete(v.ref))


def test_enable_cancel_cycling_raises_an_alert(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(1)
    w = seed(db, clock, layout="azure_worm")
    for _ in range(2):
        _enable(connect, w, clock, days=10)
        with_conn(connect, lambda c: cancel_worm_test(c, w.share_id, actor="user:1",
                                                       now=clock.now()))  # fmt: skip
    _enable(connect, w, clock, days=10)
    assert audits(db, w.dataset_id, "share.worm.cycling") == ["share.worm.cycling"] * 2
