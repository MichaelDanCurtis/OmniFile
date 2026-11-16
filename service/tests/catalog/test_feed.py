"""The edge event feed: what emits events, and the ordering guarantee."""

from __future__ import annotations

from collections.abc import Callable

import psycopg

from omnifile_service.catalog.migrate import migrate

from .support import World, add_file, seed


def events(c: psycopg.Connection, w: World, after: tuple[int, int] = (0, 0), limit: int = 100):
    (horizon,) = c.execute("SELECT catalog.feed_horizon()").fetchone()
    rows = c.execute(
        "SELECT txid::text::bigint, event_id, entity, entity_id, op, origin_edge_id, origin_op_id"
        " FROM catalog.feed_read(%s::text::xid8, %s, %s, %s, %s, %s)",
        (str(after[0]), after[1], horizon, w.edge_id, [w.dataset_id], limit),
    ).fetchall()
    return rows


def test_file_changes_emit_only_what_the_edge_holds(migrated_url: str) -> None:
    conn = psycopg.connect(migrated_url)
    w = seed(conn)
    f = add_file(conn, w, w.root_id, "a")
    conn.execute("UPDATE catalog.file SET last_read_at = now() WHERE file_id = %s", (f,))
    conn.execute("UPDATE catalog.file SET name = 'b' WHERE file_id = %s", (f,))
    conn.execute(
        "SELECT set_config('omnifile.origin_edge_id', %s, true),"
        " set_config('omnifile.origin_op_id', 'op-42', true)",
        (str(w.edge_id),),
    )
    conn.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (f,))
    conn.commit()  # the reader below only sees committed transactions
    conn.close()
    with psycopg.connect(migrated_url) as reader:
        ops = [(e[2], e[4], e[5], e[6]) for e in events(reader, w) if e[3] == f]
    assert ops == [
        ("file", "upsert", None, None),
        ("file", "rename", None, None),
        ("file", "delete", w.edge_id, "op-42"),
    ]


def test_feed_never_skips_a_late_committing_transaction(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as c:
        w = seed(c)
        c.commit()
    slow = psycopg.connect(migrated_url)
    fast = psycopg.connect(migrated_url)
    reader = psycopg.connect(migrated_url, autocommit=True)
    try:
        # `slow` takes a transaction id first, then writes its event last.
        slow.execute("SELECT pg_current_xact_id()")
        f_fast = add_file(fast, w, w.root_id, "fast")
        fast.commit()
        f_slow = add_file(slow, w, w.root_id, "slow")

        # `fast` committed, but its event sorts after `slow`'s transaction,
        # which is still open: nothing may be served yet.
        assert [e[3] for e in events(reader, w) if e[3] in (f_fast, f_slow)] == []

        slow.commit()
        got = [e for e in events(reader, w) if e[3] in (f_fast, f_slow)]
        assert [e[3] for e in got] == [f_slow, f_fast]  # transaction order, not id order
        assert got[0][1] > got[1][1]  # ...although slow's event id is the larger one

        # Paging from the first event returns the second, and nothing twice.
        after = (got[0][0], got[0][1])
        assert [e[3] for e in events(reader, w, after) if e[3] in (f_fast, f_slow)] == [f_fast]
    finally:
        for c in (slow, fast, reader):
            c.close()


def test_dropping_partitions_records_the_retention_horizon(
    make_database: Callable[[], str],
) -> None:
    url = make_database()
    migrate(url)
    with psycopg.connect(url) as c:
        w = seed(c)
        add_file(c, w, w.root_id, "x")
        c.commit()
        (count,) = c.execute(
            "SELECT count(*) FROM catalog.feed_event WHERE dataset_id = %s AND entity = 'file'",
            (w.dataset_id,),
        ).fetchone()
        assert count == 2  # root + x
        dropped = c.execute("SELECT catalog.feed_drop_before(now() + interval '60 days')")
        assert dropped.fetchone()[0] >= 1
        (txid, ev) = c.execute(
            "SELECT dropped_through_txid::text::bigint, dropped_through_event"
            " FROM catalog.feed_retention"
        ).fetchone()
        assert txid > 0 and ev > 0
        # Partitions come back for current time on the next ensure call.
        c.execute("SELECT catalog.ensure_time_partitions('catalog.feed_event', 'week', 2)")
        c.commit()
