from __future__ import annotations

import hashlib
import json
import re

import psycopg

from omnifile_service.catalog.migrate import MIGRATIONS_DIR

from .support import add_file, seed

QUEUES = {"restores", "storage_mover", "verify", "notify", "edge_instructions"}


def test_vendored_pgmq_is_the_pinned_upstream_file() -> None:
    text = (MIGRATIONS_DIR / "0001_pgmq_v1_13_0.sql").read_text()
    header, _, upstream = text.partition("-- " + "=" * 75 + "\n")
    pinned = re.search(r"SHA-256 of the upstream file: ([0-9a-f]{64})", header)
    assert pinned and hashlib.sha256(upstream.encode()).hexdigest() == pinned[1]
    assert "The PostgreSQL License" in header and "Copyright (c) 2023, Tembo" in header


def test_queues_exist(conn: psycopg.Connection) -> None:
    names = {r[0] for r in conn.execute("SELECT queue_name FROM pgmq.list_queues()")}
    assert QUEUES <= names


def test_send_read_archive(conn: psycopg.Connection) -> None:
    (msg_id,) = conn.execute(
        "SELECT pgmq.send('verify', %s::jsonb)", ('{"file_id": 7}',)
    ).fetchone()
    row = conn.execute("SELECT msg_id, read_ct, message FROM pgmq.read('verify', 30, 1)").fetchone()
    assert row[0] == msg_id and row[1] == 1 and row[2] == {"file_id": 7}
    # Invisible while leased.
    assert conn.execute("SELECT * FROM pgmq.read('verify', 30, 1)").fetchall() == []
    assert conn.execute("SELECT pgmq.archive('verify', %s)", (msg_id,)).fetchone() == (True,)
    archived = conn.execute("SELECT message FROM pgmq.a_verify WHERE msg_id = %s", (msg_id,))
    assert archived.fetchone() == ({"file_id": 7},)


def _enqueue_with_recall(c: psycopg.Connection) -> tuple[int, int]:
    """The decision-27 pattern: catalog change and queue message, one transaction."""
    w = seed(c)
    f = add_file(c, w, w.root_id, "line_1042.segy")
    (vid,) = c.execute(
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind,"
        " committed_at) VALUES (%s, %s, %s, 'line_1042.segy', 'content', now())"
        " RETURNING version_id",
        (f, w.dataset_id, w.root_id),
    ).fetchone()
    (recall_id,) = c.execute(
        "INSERT INTO catalog.recall (file_id, version_id) VALUES (%s, %s) RETURNING recall_id",
        (f, vid),
    ).fetchone()
    (msg_id,) = c.execute(
        "SELECT pgmq.send('restores', %s::jsonb)", (json.dumps({"recall_id": recall_id}),)
    ).fetchone()
    return recall_id, msg_id


def _queued(c: psycopg.Connection, msg_id: int) -> bool:
    return (
        c.execute("SELECT 1 FROM pgmq.q_restores WHERE msg_id = %s", (msg_id,)).fetchone()
        is not None
    )


def test_enqueue_commits_with_the_catalog_change(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as c:
        recall_id, msg_id = _enqueue_with_recall(c)
        c.commit()
    with psycopg.connect(migrated_url) as c:
        assert c.execute(
            "SELECT 1 FROM catalog.recall WHERE recall_id = %s", (recall_id,)
        ).fetchone()
        assert _queued(c, msg_id)


def test_enqueue_rolls_back_with_the_catalog_change(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as c:
        recall_id, msg_id = _enqueue_with_recall(c)
        c.rollback()
    with psycopg.connect(migrated_url) as c:
        assert not c.execute(
            "SELECT 1 FROM catalog.recall WHERE recall_id = %s", (recall_id,)
        ).fetchone()
        assert not _queued(c, msg_id)
