"""Rules the catalog enforces itself: constraints, triggers, feed, as-of."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from .support import World, add_file, seed, set_actor

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def expect(c: psycopg.Connection, sqlstate: str, sql: str, params: Sequence[Any] = ()) -> None:
    """Run `sql` in a savepoint and assert it fails with `sqlstate`."""
    with pytest.raises(psycopg.Error) as info:
        with c.transaction():
            c.execute(sql, params)
    assert info.value.sqlstate == sqlstate, info.value


def commit_version(
    c: psycopg.Connection,
    w: World,
    file_id: int,
    parent: int,
    name: str,
    at: datetime,
    kind: str = "content",
    content_of: int | None = None,
) -> int:
    (vid,) = c.execute(
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind,"
        " content_version_id, committed_at, size) VALUES (%s, %s, %s, %s, %s, %s, %s, 1)"
        " RETURNING version_id",
        (file_id, w.dataset_id, parent, name, kind, content_of, at),
    ).fetchone()
    return vid


def share(c: psycopg.Connection, w: World, **cols: Any) -> int:
    base: dict[str, Any] = {
        "dataset_id": w.dataset_id,
        "edge_id": w.edge_id,
        "name": f"s{cols.pop('n', '')}",
        "protocols": ["nfs4"],
        "nfs_sec": ["krb5"],
        "owners_group_sid": "S-1-5-21-1-2-3-512",
    }
    base.update(cols)
    names = ", ".join(base)
    marks = ", ".join(["%s"] * len(base))
    (sid,) = c.execute(
        f"INSERT INTO catalog.share ({names}) VALUES ({marks}) RETURNING share_id",
        list(base.values()),
    ).fetchone()
    return sid


# --- shares and trust (decision 28) ----------------------------------------


def test_share_trust_level_follows_nfs_sec(conn: psycopg.Connection) -> None:
    w = seed(conn)
    proven = share(conn, w, n=1)
    trusted = share(conn, w, n=2, nfs_sec=["sys", "krb5"], nfs_clients=["10.1.0.0/16"])
    levels = dict(
        conn.execute(
            "SELECT share_id, trust_level FROM catalog.share WHERE share_id IN (%s, %s)",
            (proven, trusted),
        ).fetchall()
    )
    assert levels == {proven: "proven", trusted: "host_trusted"}


@pytest.mark.parametrize(
    "cols",
    [
        {"nfs_sec": ["sys"]},  # no client list
        {"nfs_sec": ["sys"], "nfs_clients": ["*"]},  # wildcard
        {"nfs_sec": ["sys"], "nfs_clients": ["host1"], "root_squash": False},
        {"nfs_sec": []},  # NFS without a flavour
        {"on_open": "auto"},  # auto without a threshold
        {"rewind_depth_days": 30, "rewind_depth_max_days": 7},
        {"owners_group_sid": "not-a-sid"},
    ],
)
def test_share_rules(conn: psycopg.Connection, cols: dict[str, Any]) -> None:
    w = seed(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            share(conn, w, **cols)


# --- namespace -------------------------------------------------------------


def test_names_are_unique_among_live_entries_only(conn: psycopg.Connection) -> None:
    w = seed(conn)
    f = add_file(conn, w, w.root_id, "a.segy")
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            add_file(conn, w, w.root_id, "a.segy")
    conn.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (f,))
    add_file(conn, w, w.root_id, "a.segy")


def test_one_root_per_dataset_and_root_shape(conn: psycopg.Connection) -> None:
    w = seed(conn)
    expect(
        conn,
        "23505",
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind) VALUES (%s, NULL, '', 'dir')",
        (w.dataset_id,),
    )
    for bad in ("", ".", "..", "a/b"):
        expect(
            conn,
            "23514",
            "INSERT INTO catalog.file (dataset_id, parent_id, name) VALUES (%s, %s, %s)",
            (w.dataset_id, w.root_id, bad),
        )


def test_directory_tombstone_needs_empty_directory(conn: psycopg.Connection) -> None:
    w = seed(conn)
    d = add_file(conn, w, w.root_id, "survey", kind="dir")
    f = add_file(conn, w, d, "line.segy")
    expect(conn, "OF001", "UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (d,))
    conn.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (f,))
    conn.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (d,))


# --- versions and rewind (decision 30) -------------------------------------


def test_commit_supersedes_and_points_the_file_at_it(conn: psycopg.Connection) -> None:
    w = seed(conn)
    f = add_file(conn, w, w.root_id, "a")
    v1 = commit_version(conn, w, f, w.root_id, "a", T0)
    v2 = commit_version(conn, w, f, w.root_id, "a", T0 + timedelta(hours=1))
    rows = conn.execute(
        "SELECT version_id, seq, superseded_at FROM catalog.file_version"
        " WHERE file_id = %s ORDER BY seq",
        (f,),
    ).fetchall()
    assert rows == [(v1, 1, T0 + timedelta(hours=1)), (v2, 2, None)]
    current = conn.execute("SELECT current_version_id FROM catalog.file WHERE file_id = %s", (f,))
    assert current.fetchone() == (v2,)


def test_folder_as_of(conn: psycopg.Connection) -> None:
    w = seed(conn)
    d1 = add_file(conn, w, w.root_id, "d1", kind="dir")
    d2 = add_file(conn, w, w.root_id, "d2", kind="dir")
    a = add_file(conn, w, d1, "a")
    b = add_file(conn, w, d1, "b")
    va = commit_version(conn, w, a, d1, "a", T0)
    vb = commit_version(conn, w, b, d1, "b", T0)
    # T0+1h: b is overwritten. T0+2h: a moves to d2. T0+3h: b is deleted.
    vb2 = commit_version(conn, w, b, d1, "b", T0 + timedelta(hours=1))
    va2 = commit_version(conn, w, a, d2, "a", T0 + timedelta(hours=2), "rename", va)
    commit_version(conn, w, b, d1, "b", T0 + timedelta(hours=3), "tombstone")

    def at(d: int, hours: float) -> set[int]:
        rows = conn.execute(
            "SELECT version_id FROM catalog.folder_as_of(%s, %s)",
            (d, T0 + timedelta(hours=hours)),
        )
        return {r[0] for r in rows}

    assert at(d1, -1) == set()
    assert at(d1, 0.5) == {va, vb}
    assert at(d1, 1.5) == {va, vb2}
    assert at(d1, 2.5) == {vb2}
    assert at(d2, 2.5) == {va2}
    assert at(d1, 3.5) == set()


def test_as_of_query_uses_the_index(conn: psycopg.Connection) -> None:
    conn.execute("SET LOCAL enable_seqscan = off")
    plan = "\n".join(
        r[0]
        for r in conn.execute(
            "EXPLAIN SELECT * FROM catalog.file_version WHERE parent_id = 1 AND state = 'committed'"
            " AND committed_at <= now() AND (superseded_at IS NULL OR superseded_at > now())"
        )
    )
    assert "file_version_as_of" in plan


# --- WORM (decision 31) ----------------------------------------------------


def test_worm_needs_single_account_layout(conn: psycopg.Connection) -> None:
    w = seed(conn, layout="two_account")
    set_actor(conn, "user:1")
    with pytest.raises(psycopg.Error) as info:
        with conn.transaction():
            share(conn, w, worm_enabled=True, worm_mode="unlocked_test", worm_retention_days=7)
    assert info.value.sqlstate == "OF004"


def test_worm_changes_need_an_actor_and_are_audited(conn: psycopg.Connection) -> None:
    w = seed(conn, layout="single_account")
    with pytest.raises(psycopg.Error) as info:
        with conn.transaction():
            share(conn, w, worm_enabled=True, worm_mode="unlocked_test", worm_retention_days=7)
    assert info.value.sqlstate == "OF005"
    set_actor(conn, f"user:{w.alice}")
    s = share(conn, w, worm_enabled=True, worm_mode="unlocked_test", worm_retention_days=7)
    conn.execute(
        "UPDATE catalog.share SET worm_mode = 'locked', worm_locked_at = now() WHERE share_id = %s",
        (s,),
    )
    actions = [
        r[0]
        for r in conn.execute(
            "SELECT action FROM catalog.audit_log WHERE dataset_id = %s ORDER BY audit_id",
            (w.dataset_id,),
        )
    ]
    assert actions == ["share.worm.enable", "share.worm.lock"]


def test_locked_worm_is_one_way(conn: psycopg.Connection) -> None:
    w = seed(conn, layout="single_account")
    set_actor(conn, f"user:{w.alice}")
    s = share(
        conn,
        w,
        worm_enabled=True,
        worm_mode="locked",
        worm_retention_days=30,
        worm_locked_at=T0,
    )
    for sql in (
        "UPDATE catalog.share SET worm_enabled = false, worm_mode = NULL, worm_locked_at = NULL,"
        " worm_retention_days = NULL WHERE share_id = %s",
        "UPDATE catalog.share SET worm_retention_days = 29 WHERE share_id = %s",
        "UPDATE catalog.share SET worm_locked_at = now() WHERE share_id = %s",
    ):
        expect(conn, "OF002", sql, (s,))
    conn.execute("UPDATE catalog.share SET worm_retention_days = 60 WHERE share_id = %s", (s,))
    expect(
        conn,
        "OF004",
        "UPDATE catalog.dataset SET storage_layout = 'two_account' WHERE dataset_id = %s",
        (w.dataset_id,),
    )


def test_worm_versions_are_retained(conn: psycopg.Connection) -> None:
    w = seed(conn, layout="single_account")
    set_actor(conn, f"user:{w.alice}")
    share(conn, w, worm_enabled=True, worm_mode="locked", worm_retention_days=10, worm_locked_at=T0)
    f = add_file(conn, w, w.root_id, "record.pdf")
    now = datetime.now(UTC)
    v = commit_version(conn, w, f, w.root_id, "record.pdf", now)
    (until,) = conn.execute(
        "SELECT retention_until FROM catalog.file_version WHERE version_id = %s", (v,)
    ).fetchone()
    assert until == now + timedelta(days=10)
    expect(
        conn,
        "OF002",
        "UPDATE catalog.file_version SET state = 'pruned' WHERE version_id = %s",
        (v,),
    )
    expect(conn, "OF002", "DELETE FROM catalog.file_version WHERE version_id = %s", (v,))
    expect(
        conn,
        "OF002",
        "UPDATE catalog.file_version SET retention_until = retention_until - interval '1 day'"
        " WHERE version_id = %s",
        (v,),
    )
    conn.execute("UPDATE catalog.file_version SET legal_hold = true WHERE version_id = %s", (v,))
    audited = conn.execute(
        "SELECT action FROM catalog.audit_log WHERE version_id = %s", (v,)
    ).fetchall()
    assert audited == [("version.legal_hold",)]


# --- policies (anti-pattern 7, decision 31) --------------------------------


def test_policy_rules(conn: psycopg.Connection) -> None:
    w = seed(conn, layout="single_account")
    (prop,) = conn.execute(
        "INSERT INTO catalog.policy_proposal (dataset_id, body, proposed_by_kind, proposed_by_app)"
        " VALUES (%s, '{}', 'application', 'prizmanyx') RETURNING proposal_id",
        (w.dataset_id,),
    ).fetchone()
    insert = (
        "INSERT INTO catalog.policy (dataset_id, version, body, proposal_id,"
        " approved_by_identity_id) VALUES (%s, %s, %s::jsonb, %s, %s)"
    )
    # A pending proposal cannot become a policy.
    expect(conn, "OF003", insert, (w.dataset_id, 1, "{}", prop, w.alice))
    # Smart + Archive is impossible on a single-account dataset.
    smart = '{"mode": "smart", "archive_day": 365}'
    expect(conn, "OF004", insert, (w.dataset_id, 1, smart, None, w.alice))
    conn.execute(
        "UPDATE catalog.policy_proposal SET state = 'approved', decided_by_identity_id = %s,"
        " decided_at = now() WHERE proposal_id = %s",
        (w.alice, prop),
    )
    conn.execute(insert, (w.dataset_id, 1, '{"mode": "scheduled"}', prop, w.alice))
    # Nobody approves their own proposal.
    expect(
        conn,
        "23514",
        "INSERT INTO catalog.policy_proposal (dataset_id, body, proposed_by_kind,"
        " proposed_by_identity_id, state, decided_by_identity_id, decided_at)"
        " VALUES (%s, '{}', 'identity', %s, 'approved', %s, now())",
        (w.dataset_id, w.bob, w.bob),
    )


# --- locations and tiers (decision 29) --------------------------------------


def _location(c: psycopg.Connection, w: World, f: int, v: int, tier: str = "smart") -> int:
    (lid,) = c.execute(
        "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
        " role, state, committed_at, logical_tier) VALUES (%s, %s, %s, 'acct/live', %s,"
        " 'primary', 'committed', now(), %s) RETURNING location_id",
        (f, v, w.backend_id, f"k/{f}", tier),
    ).fetchone()
    return lid


def test_location_feeds_file_summary_and_explicit_tier_is_one_way(conn: psycopg.Connection) -> None:
    w = seed(conn)
    f = add_file(conn, w, w.root_id, "a")
    v = commit_version(conn, w, f, w.root_id, "a", T0)
    lid = _location(conn, w, f, v)
    assert conn.execute(
        "SELECT primary_location_id, remote_tier FROM catalog.file WHERE file_id = %s", (f,)
    ).fetchone() == (lid, "smart")
    conn.execute(
        "UPDATE catalog.location SET logical_tier = 'cold', explicit_tier_set = true"
        " WHERE location_id = %s",
        (lid,),
    )
    assert conn.execute(
        "SELECT remote_tier FROM catalog.file WHERE file_id = %s", (f,)
    ).fetchone() == ("cold",)
    expect(
        conn,
        "OF006",
        "UPDATE catalog.location SET explicit_tier_set = false WHERE location_id = %s",
        (lid,),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            _location(conn, w, f, v)  # a second primary for the same version


def test_tier_pin_is_audited_and_one_active_per_target(conn: psycopg.Connection) -> None:
    w = seed(conn)
    d = add_file(conn, w, w.root_id, "keep", kind="dir")
    pin = (
        "INSERT INTO catalog.tier_pin (dataset_id, file_id, logical_tier, set_by_identity_id)"
        " VALUES (%s, %s, 'hot', %s)"
    )
    expect(conn, "OF005", pin, (w.dataset_id, d, w.alice))
    set_actor(conn, f"user:{w.alice}")
    conn.execute(pin, (w.dataset_id, d, w.alice))
    expect(conn, "23505", pin, (w.dataset_id, d, w.alice))
    assert conn.execute(
        "SELECT action FROM catalog.audit_log WHERE file_id = %s", (d,)
    ).fetchall() == [("tier_pin.set",)]


# --- recalls ---------------------------------------------------------------


def test_one_active_recall_per_version(conn: psycopg.Connection) -> None:
    w = seed(conn)
    f = add_file(conn, w, w.root_id, "a")
    v = commit_version(conn, w, f, w.root_id, "a", T0)
    sql = "INSERT INTO catalog.recall (file_id, version_id) VALUES (%s, %s) RETURNING recall_id"
    (r,) = conn.execute(sql, (f, v)).fetchone()
    expect(conn, "23505", sql, (f, v))
    # An older version of the same file is a different recall (rewind). It is pinned, as
    # a read of that version itself is: a recall of the file's current content would
    # end when the next version is committed (migration 0059).
    conn.execute("UPDATE catalog.recall SET pinned = true WHERE recall_id = %s", (r,))
    v2 = commit_version(conn, w, f, w.root_id, "a", T0 + timedelta(hours=1))
    conn.execute(sql, (f, v2))
    conn.execute("UPDATE catalog.recall SET state = 'available' WHERE recall_id = %s", (r,))
    conn.execute(sql, (f, v))
    expect(
        conn,
        "23514",
        "INSERT INTO catalog.recall_requester (recall_id, identity_id, via, proof)"
        " VALUES (%s, %s, 'teams', 'claimed')",
        (r, w.alice),
    )


# --- audit -----------------------------------------------------------------


def test_audit_log_is_append_only(conn: psycopg.Connection) -> None:
    set_actor(conn, "system:test")
    conn.execute("SELECT catalog.audit('test.event', NULL, NULL, NULL, NULL, '{}')")
    expect(conn, "OF002", "UPDATE catalog.audit_log SET action = 'x' WHERE action = 'test.event'")
    expect(conn, "OF002", "DELETE FROM catalog.audit_log WHERE action = 'test.event'")


# --- review fixes ------------------------------------------------------------


def test_content_version_survives_rename_and_tier_moves_reach_the_file(
    conn: psycopg.Connection,
) -> None:
    w = seed(conn)
    d = add_file(conn, w, w.root_id, "d", kind="dir")
    f = add_file(conn, w, w.root_id, "a")
    v1 = commit_version(conn, w, f, w.root_id, "a", T0)
    lid = _location(conn, w, f, v1, tier="hot")
    # Rename, then chmod: both new versions, neither moves the bytes.
    conn.execute("UPDATE catalog.file SET parent_id = %s WHERE file_id = %s", (d, f))
    r = commit_version(conn, w, f, d, "a", T0 + timedelta(hours=1), "rename", v1)
    m = commit_version(conn, w, f, d, "a", T0 + timedelta(hours=2), "metadata", r)
    assert conn.execute(
        "SELECT content_version_id FROM catalog.file_version WHERE version_id = %s", (m,)
    ).fetchone() == (v1,)  # normalised to the content version
    row = conn.execute(
        "SELECT current_version_id, content_version_id, primary_location_id"
        " FROM catalog.file WHERE file_id = %s",
        (f,),
    ).fetchone()
    assert row == (m, v1, lid)
    # The archive move after the rename still reaches the file.
    conn.execute(
        "UPDATE catalog.location SET logical_tier = 'archive' WHERE location_id = %s", (lid,)
    )
    assert conn.execute(
        "SELECT remote_tier FROM catalog.file WHERE file_id = %s", (f,)
    ).fetchone() == ("archive",)
    # A restore of that location shows on the file too.
    (rid,) = conn.execute(
        "INSERT INTO catalog.restore (location_id, backend_id, mechanism, speed, triggered_by)"
        " VALUES (%s, %s, 'azure_copy_rehydrate', 'standard', 'user_recall') RETURNING restore_id",
        (lid, w.backend_id),
    ).fetchone()
    assert conn.execute(
        "SELECT restore_state FROM catalog.file WHERE file_id = %s", (f,)
    ).fetchone() == ("in_progress",)
    conn.execute("UPDATE catalog.restore SET state = 'failed' WHERE restore_id = %s", (rid,))
    assert conn.execute(
        "SELECT restore_state FROM catalog.file WHERE file_id = %s", (f,)
    ).fetchone() == ("failed",)


def test_one_edge_per_dataset(conn: psycopg.Connection) -> None:
    w = seed(conn)
    other = seed(conn)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.transaction():
            share(conn, w, n=9, edge_id=other.edge_id)
    share(conn, w, n=1)
    expect(
        conn,
        "23503",
        "UPDATE catalog.dataset SET edge_id = %s WHERE dataset_id = %s",
        (other.edge_id, w.dataset_id),
    )


def test_worm_share_cannot_be_deleted_or_moved_once_locked(conn: psycopg.Connection) -> None:
    w = seed(conn, layout="single_account")
    set_actor(conn, f"user:{w.alice}")
    d = add_file(conn, w, w.root_id, "records", kind="dir")
    s = share(
        conn,
        w,
        worm_enabled=True,
        worm_mode="locked",
        worm_retention_days=30,
        worm_locked_at=T0,
        root_dir_id=d,
    )
    expect(conn, "OF002", "DELETE FROM catalog.share WHERE share_id = %s", (s,))
    expect(conn, "OF002", "UPDATE catalog.share SET root_dir_id = NULL WHERE share_id = %s", (s,))
    # An unlocked (test-mode) WORM share may still be deleted, and that is audited.
    t = share(conn, w, n=2, worm_enabled=True, worm_mode="unlocked_test", worm_retention_days=1)
    conn.execute("DELETE FROM catalog.share WHERE share_id = %s", (t,))
    assert conn.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'share.worm.delete'"
        " AND share_id = %s",
        (t,),
    ).fetchone() == (1,)


def test_worm_retention_is_scoped_to_the_share_root(conn: psycopg.Connection) -> None:
    w = seed(conn, layout="single_account")
    set_actor(conn, f"user:{w.alice}")
    records = add_file(conn, w, w.root_id, "records", kind="dir")
    sub = add_file(conn, w, records, "2026", kind="dir")
    scratch = add_file(conn, w, w.root_id, "scratch", kind="dir")
    share(
        conn,
        w,
        worm_enabled=True,
        worm_mode="locked",
        worm_retention_days=30,
        worm_locked_at=T0,
        root_dir_id=records,
    )
    inside = add_file(conn, w, sub, "a.pdf")
    outside = add_file(conn, w, scratch, "b.tmp")
    now = datetime.now(UTC)
    vi = commit_version(conn, w, inside, sub, "a.pdf", now)
    vo = commit_version(conn, w, outside, scratch, "b.tmp", now)
    got = dict(
        conn.execute(
            "SELECT version_id, retention_until FROM catalog.file_version"
            " WHERE version_id IN (%s, %s)",
            (vi, vo),
        ).fetchall()
    )
    assert got == {vi: now + timedelta(days=30), vo: None}


def test_copies_of_retained_versions_are_never_deleted(conn: psycopg.Connection) -> None:
    w = seed(conn, layout="single_account")
    set_actor(conn, f"user:{w.alice}")
    share(conn, w, worm_enabled=True, worm_mode="locked", worm_retention_days=30, worm_locked_at=T0)
    f = add_file(conn, w, w.root_id, "a")
    v = commit_version(conn, w, f, w.root_id, "a", datetime.now(UTC))
    lid = _location(conn, w, f, v, tier="hot")
    expect(
        conn,
        "OF002",
        "UPDATE catalog.location SET state = 'deleting' WHERE location_id = %s",
        (lid,),
    )
    expect(conn, "OF002", "DELETE FROM catalog.location WHERE location_id = %s", (lid,))
    # A recall copy is not the record and may go.
    (rc,) = conn.execute(
        "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
        " role, state, committed_at) VALUES (%s, %s, %s, 'acct/recall', 'r', 'recall_copy',"
        " 'committed', now()) RETURNING location_id",
        (f, v, w.backend_id),
    ).fetchone()
    conn.execute("UPDATE catalog.location SET state = 'deleted' WHERE location_id = %s", (rc,))


def test_sim_backends_are_allowed(conn: psycopg.Connection) -> None:
    conn.execute(
        "INSERT INTO catalog.backend (name, kind, private_network) VALUES ('sim-x', 'sim', false)"
    )


def test_default_partitions_start_empty_and_rows_there_are_reported(
    conn: psycopg.Connection,
) -> None:
    assert conn.execute("SELECT * FROM catalog.default_partition_rows()").fetchall() == []
    set_actor(conn, "system:test")
    conn.execute(
        "INSERT INTO catalog.audit_log (at, actor, action, decision)"
        " VALUES ('2001-01-01', 'system:test', 'test.old', 'info')"
    )
    assert conn.execute(
        "SELECT parent::text, sampled_rows FROM catalog.default_partition_rows()"
    ).fetchall() == [("catalog.audit_log", 1)]


def test_rename_cannot_roll_the_bytes_back(conn: psycopg.Connection) -> None:
    w = seed(conn)
    f = add_file(conn, w, w.root_id, "a")
    v1 = commit_version(conn, w, f, w.root_id, "a", T0)
    v2 = commit_version(conn, w, f, w.root_id, "a", T0 + timedelta(hours=1))
    # A rename citing the old content version is refused...
    with pytest.raises(psycopg.Error) as info:
        with conn.transaction():
            commit_version(conn, w, f, w.root_id, "b", T0 + timedelta(hours=2), "rename", v1)
    assert info.value.sqlstate == "OF007"
    # ...and one that cites nothing gets the current bytes.
    r = commit_version(conn, w, f, w.root_id, "b", T0 + timedelta(hours=2), "rename")
    assert conn.execute(
        "SELECT content_version_id FROM catalog.file_version WHERE version_id = %s", (r,)
    ).fetchone() == (v2,)
    assert conn.execute(
        "SELECT current_version_id, content_version_id FROM catalog.file WHERE file_id = %s", (f,)
    ).fetchone() == (r, v2)
    # A directory rename has no content version at all.
    d = add_file(conn, w, w.root_id, "d", kind="dir")
    commit_version(conn, w, d, w.root_id, "e", T0, "rename")


def test_retained_copies_cannot_be_relabelled_then_deleted(conn: psycopg.Connection) -> None:
    w = seed(conn, layout="single_account")
    set_actor(conn, f"user:{w.alice}")
    share(conn, w, worm_enabled=True, worm_mode="locked", worm_retention_days=30, worm_locked_at=T0)
    f = add_file(conn, w, w.root_id, "a")
    v = commit_version(conn, w, f, w.root_id, "a", datetime.now(UTC))
    lid = _location(conn, w, f, v, tier="hot")
    expect(
        conn, "OF002", "UPDATE catalog.location SET role = 'staging' WHERE location_id = %s", (lid,)
    )
    expect(
        conn,
        "OF002",
        "UPDATE catalog.location SET role = 'recall_copy' WHERE location_id = %s",
        (lid,),
    )
    expect(conn, "OF002", "DELETE FROM catalog.location WHERE location_id = %s", (lid,))
