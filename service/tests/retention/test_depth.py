"""Versioning depth (decision 30): Owners set the depth within the
ArchiveAdmin maximum; versions beyond it are pruned, in the catalog and then
on the backend, unless retained, held or still needed."""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from omnifile_service.backends.base import ObjectNotFound
from omnifile_service.retention import (
    RetentionError,
    clear_legal_hold,
    lifecycle_policy,
    prune_versions,
    set_legal_hold,
    set_rewind_depth,
    set_rewind_max,
)

from .support import (
    add_file,
    audits,
    clock_days_ago,
    commit_content,
    commit_metadata,
    drain,
    expect_sqlstate,
    location,
    run,
    seed,
    share,
    version,
    with_conn,
    worker,
)

D = timedelta(days=1)


def test_depth_is_set_within_the_archiveadmin_maximum(db: psycopg.Connection, connect) -> None:
    w = seed(db, clock_days_ago(1))
    with_conn(connect, lambda c: set_rewind_max(c, w.share_id, 10, actor=f"user:{w.admin}"))
    with_conn(connect, lambda c: set_rewind_depth(c, w.share_id, 7, actor=f"user:{w.admin}"))
    assert share(db, w.share_id)["rewind_depth_days"] == 7
    with pytest.raises(RetentionError, match="above the ArchiveAdmin maximum"):
        with_conn(connect, lambda c: set_rewind_depth(c, w.share_id, 11, actor="user:1"))
    # Lowering the maximum pulls the depth down with it.
    with_conn(connect, lambda c: set_rewind_max(c, w.share_id, 3, actor=f"user:{w.admin}"))
    s = share(db, w.share_id)
    assert (s["rewind_depth_days"], s["rewind_depth_max_days"]) == (3, 3)
    assert audits(db, w.dataset_id, "share.rewind") == ["share.rewind_depth"] * 3
    # The catalog refuses a depth above the maximum even without the service.
    expect_sqlstate(db, "23514", "UPDATE catalog.share SET rewind_depth_days = 4"
                    " WHERE share_id = %s", (w.share_id,))  # fmt: skip


def _history(db, w, clock):
    """v1 at t0, v2 at t0+1d, v3 at t0+60d, v4 at t0+80d (current)."""
    t0 = clock.now()
    f = add_file(db, w, "line_1042.segy")
    v1 = commit_content(db, w, f, b"one", t0)
    v2 = commit_content(db, w, f, b"two", t0 + D)
    v3 = commit_content(db, w, f, b"three", t0 + 60 * D)
    v4 = commit_content(db, w, f, b"four", t0 + 80 * D)
    return f, (v1, v2, v3, v4)


def test_versions_beyond_the_depth_are_pruned_and_their_blobs_deleted(
    db: psycopg.Connection, connect
) -> None:
    clock = clock_days_ago(101)
    w = seed(db, clock, depth=30, depth_max=60)
    _, (v1, v2, v3, v4) = _history(db, w, clock)
    clock.advance(100 * D)  # t0+100d: v1, v2 superseded > 30 d ago; v3 only 20 d ago

    result = with_conn(connect, lambda c: prune_versions(c, clock.now(), dataset_id=w.dataset_id))
    assert result.versions == sorted([v1.version_id, v2.version_id])
    assert [version(db, v.version_id)["state"] for v in (v1, v2, v3, v4)] == [
        "pruned", "pruned", "committed", "committed"]  # fmt: skip
    assert location(db, v1.location_id)["state"] == "deleting"
    assert "version.prune" in audits(db, w.dataset_id, "version.")

    # The lifecycle-equivalent job deletes the bytes, then retires the rows.
    wk = worker(connect, w, clock)
    assert drain(wk, "retention") == 2
    for gone in (v1, v2):
        assert location(db, gone.location_id)["state"] == "deleted"
        with pytest.raises(ObjectNotFound):
            run(w.sim.head(gone.ref))
    for kept in (v3, v4):
        assert run(w.sim.head(kept.ref)).size > 0
    # A second pass finds nothing more.
    again = with_conn(connect, lambda c: prune_versions(c, clock.now(), dataset_id=w.dataset_id))
    assert again.versions == []


def test_legal_hold_blocks_pruning_until_released(db: psycopg.Connection, connect) -> None:
    clock = clock_days_ago(101)
    w = seed(db, clock, depth=30, depth_max=60)
    f, (v1, v2, _, _) = _history(db, w, clock)
    clock.advance(100 * D)
    hold = with_conn(
        connect,
        lambda c: set_legal_hold(c, f, reason="litigation 42", actor=f"user:{w.admin}",
                                 identity_id=w.admin),
    )  # fmt: skip
    assert version(db, v1.version_id)["legal_hold"]
    assert (
        with_conn(
            connect, lambda c: prune_versions(c, clock.now(), dataset_id=w.dataset_id)
        ).versions
        == []
    )
    # The catalog refuses it directly too.
    expect_sqlstate(db, "OF002", "UPDATE catalog.file_version SET state = 'pruned'"
                    " WHERE version_id = %s", (v1.version_id,))  # fmt: skip

    released = with_conn(
        connect,
        lambda c: clear_legal_hold(c, hold, actor=f"user:{w.admin}", identity_id=w.admin,
                                   now=clock.now()),
    )  # fmt: skip
    assert released == 4
    result = with_conn(connect, lambda c: prune_versions(c, clock.now(), dataset_id=w.dataset_id))
    assert result.versions == sorted([v1.version_id, v2.version_id])
    assert audits(db, w.dataset_id, "hold.") == ["hold.set", "hold.release"]


def test_bytes_still_in_use_are_not_pruned(db: psycopg.Connection, connect) -> None:
    """A metadata version carries old content; a deduped blob is shared."""
    clock = clock_days_ago(101)
    w = seed(db, clock, depth=0)
    t0 = clock.now()
    f = add_file(db, w, "a.segy")
    v1 = commit_content(db, w, f, b"bytes", t0)
    m = commit_metadata(db, w, f, t0 + D)  # current, content = v1
    # A second file whose first version dedupes onto another file's blob.
    g = add_file(db, w, "b.segy")
    h = add_file(db, w, "c.segy")
    hv = commit_content(db, w, h, b"same", t0)
    g1 = commit_content(db, w, g, b"same", t0, share_blob_of=hv)
    commit_content(db, w, g, b"newer", t0 + D)
    clock.advance(100 * D)

    result = with_conn(connect, lambda c: prune_versions(c, clock.now(), dataset_id=w.dataset_id))
    assert v1.version_id not in result.versions  # live content via the metadata version
    assert version(db, m)["content_version_id"] == v1.version_id
    assert g1.version_id in result.versions
    wk = worker(connect, w, clock)
    drain(wk, "retention")
    assert location(db, g1.location_id)["state"] == "deleted"
    assert run(w.sim.head(hv.ref)).size == 4  # h's live copy keeps the shared blob


def test_lifecycle_backstop_rule_targets_pruned_tags() -> None:
    rule = lifecycle_policy(data_container="live/data")["rules"][0]
    flt = rule["definition"]["filters"]
    assert flt["prefixMatch"] == ["data/"]
    assert flt["blobIndexMatch"] == [{"name": "omnifile_state", "op": "==", "value": "pruned"}]
