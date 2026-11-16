"""Bulk snapshot exports (control-api.md section 6; `exports.py`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime

import psycopg
import pytest
import zstandard
from watched import Watched, drain_queue

from omnifile_service import exports
from omnifile_service.backends.base import ObjectNotFound, ObjectRef
from omnifile_service.catalog.privileges import as_app

from .conftest import E, World, create_file

GET_EXPORT = f"{E}/snapshot-exports/{{export_id}}"


def run_exports(db_url: str, facade, export_id: str) -> int:
    """What the worker's `snapshot_export` consumer does, for this export's
    messages until there are none (other tests share the queue). Returns the
    messages handled."""

    async def go() -> int:
        n = 0
        async with await psycopg.AsyncConnection.connect(
            as_app(db_url, "worker"), autocommit=True
        ) as conn:
            while True:
                row = await (
                    await conn.execute(
                        "SELECT msg_id, message FROM pgmq.q_snapshot_export"
                        " WHERE message->>'export_id' = %s ORDER BY msg_id LIMIT 1",
                        (str(export_id),),
                    )
                ).fetchone()
                if row is None:
                    return n
                async with conn.transaction():
                    await exports.step(conn, lambda _id: facade.sim, int(row[1]["export_id"]))
                    await conn.execute(
                        "SELECT pgmq.delete('snapshot_export', %s::bigint)", (row[0],)
                    )
                n += 1

    return asyncio.run(go())


def start(w: World, dataset_id: int | None = None):
    return w.edge.post(f"{E}/snapshot-exports", {"dataset_id": str(dataset_id or w.dataset_id)})


def lines_of(facade_client, part: dict) -> list[dict]:
    resp = facade_client.get(part["url"])
    assert resp.status_code == 200, resp.text
    blob = resp.content
    assert len(blob) == part["bytes"]
    assert hashlib.sha256(blob).hexdigest() == part["sha256"]
    raw = zstandard.ZstdDecompressor().decompressobj().decompress(blob)
    out = [json.loads(x) for x in raw.decode().splitlines()]
    assert len(out) == part["rows"]
    return out


def paged(w: World) -> dict:
    """The whole paged snapshot, for comparison."""
    out = w.edge.get(f"{E}/snapshot", {"dataset_id": str(w.dataset_id), "limit": 10000}).json()
    assert out["more"] is False
    return out


def test_an_export_holds_the_paged_snapshot(
    make_world, db_url, facade, facade_client, monkeypatch
) -> None:
    monkeypatch.setattr(exports, "PART_ROWS", 2)
    monkeypatch.setattr(exports, "FLUSH_BYTES", 64)  # several compressor chunks per part
    w = make_world()
    ids = [create_file(w, f"f{i}") for i in range(6)]
    # A deleted file (in the middle) is not in the snapshot, nor in the export.
    op = {"op_id": "op-unlink-f2", "op": "unlink", "file": {"file_id": str(ids[2])}}
    assert w.edge.post(f"{E}/changes", {"ops": [op]}).json()["results"][0]["status"] == "applied"
    resp = start(w)
    assert resp.status_code == 202, resp.text
    ex = resp.json()
    assert ex["state"] == "running" and ex["as_of"]
    running = w.edge.get(GET_EXPORT, export_id=ex["export_id"]).json()
    assert running["state"] == "running" and "parts" not in running

    # root + 5 live files, 2 per part: 3 parts, each a queue message.
    assert run_exports(db_url, facade, ex["export_id"]) == 3
    ready = w.edge.get(GET_EXPORT, export_id=ex["export_id"])
    assert ready.status_code == 200, ready.text
    ready = ready.json()
    assert ready["state"] == "ready" and ready["as_of"] == ex["as_of"]
    assert len(ready["parts"]) == 3
    lines = [line for p in ready["parts"] for line in lines_of(facade_client, p)]
    snap = paged(w)
    # Dataset, shares and ACLs first (the first part only), then the files.
    assert lines[0] == {"dataset": snap["dataset"]}
    meta = 1 + len(snap["shares"]) + len(snap["acls"])
    assert [x["share"] for x in lines[1 : 1 + len(snap["shares"])]] == snap["shares"]
    assert [x["acl"] for x in lines[1 + len(snap["shares"]) : meta]] == snap["acls"]
    assert [x["file"] for x in lines[meta:]] == snap["files"]
    assert len(lines[meta:]) == 6
    # The parts are where the export says, outside the user-data container.
    assert all("/omnifile-exports/" in p["url"] for p in ready["parts"])


def test_an_empty_dataset_is_one_part(make_world, db_url, facade, facade_client) -> None:
    w = make_world()
    ex = start(w).json()
    assert run_exports(db_url, facade, ex["export_id"]) == 1
    ready = w.edge.get(GET_EXPORT, export_id=ex["export_id"]).json()
    assert ready["state"] == "ready" and len(ready["parts"]) == 1
    lines = lines_of(facade_client, ready["parts"][0])
    assert [next(iter(x)) for x in lines] == ["dataset", "share", "file"]


def test_exports_are_the_edges_own(make_world) -> None:
    w, other = make_world(), make_world()
    assert start(w, other.dataset_id).status_code == 404
    ex = start(other).json()
    nope = w.edge.get(GET_EXPORT, export_id=ex["export_id"], check=False)
    assert nope.status_code == 404


def test_an_s3_dataset_has_no_export_area(make_world, sql) -> None:
    w = make_world()
    (b,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES ('s3-exp-' || gen_random_uuid(), 'sim', '{\"flavour\": \"s3\"}', false)"
        " RETURNING backend_id"
    ).fetchone()
    sql.execute(
        "UPDATE catalog.dataset SET backend_id = %s WHERE dataset_id = %s", (b, w.dataset_id)
    )
    resp = start(w)
    assert resp.status_code == 409 and resp.json()["code"] == "layout_conflict", resp.text


def test_old_exports_expire_and_their_parts_go(make_world, db_url, facade, sql) -> None:
    w = make_world()
    create_file(w, "a")
    ex = start(w).json()
    run_exports(db_url, facade, ex["export_id"])
    row = sql.execute(
        "SELECT container, parts, created_at FROM catalog.snapshot_export WHERE export_id = %s",
        (int(ex["export_id"]),),
    ).fetchone()
    container, parts, created = row
    ref = ObjectRef(w.backend_id, container, parts[0]["key"])

    async def sweep(now: datetime) -> int:
        async with await psycopg.AsyncConnection.connect(
            as_app(db_url, "worker"), autocommit=True
        ) as conn:
            return await exports.expire(conn, now, lambda _id: facade.sim)

    # Not yet old: kept.
    assert asyncio.run(sweep(created + exports.KEEP / 2)) == 0
    asyncio.run(facade.sim.head(ref))
    assert asyncio.run(sweep(created + exports.KEEP + exports.KEEP / 48)) >= 1
    with pytest.raises(ObjectNotFound):
        asyncio.run(facade.sim.head(ref))
    gone = w.edge.get(GET_EXPORT, export_id=ex["export_id"]).json()
    assert gone["state"] == "expired" and "parts" not in gone
    # A late message for an expired export writes nothing.
    sql.execute(
        "SELECT pgmq.send('snapshot_export', %s::jsonb)",
        (json.dumps({"export_id": int(ex["export_id"])}),),
    )
    assert run_exports(db_url, facade, ex["export_id"]) == 1
    with pytest.raises(ObjectNotFound):
        asyncio.run(facade.sim.head(ref))
    (after,) = sql.execute(
        "SELECT parts FROM catalog.snapshot_export WHERE export_id = %s",
        (int(ex["export_id"]),),
    ).fetchone()
    assert after == parts


def test_an_export_holds_no_id_across_its_calls(make_world, db_url, facade, sql) -> None:
    """Run: its row lock took one across the whole upload of a million rows. Its reads
    run during the upload, so it does keep read locks: a part of 1M rows is about a minute,
    and a waiting migration gives up rather than queue readers."""
    w = make_world()
    create_file(w, "a")
    ex = start(w).json()
    sql.execute(
        "DELETE FROM pgmq.q_snapshot_export WHERE message->>'export_id' <> %s", (ex["export_id"],)
    )
    provider = Watched(facade.sim, db_url)

    async def handler(msg, conn) -> None:  # type: ignore[no-untyped-def]
        await exports.step(conn, lambda _id: provider, int(msg.body["export_id"]))

    stats = drain_queue(db_url, "snapshot_export", handler)
    assert stats.acked == 1 and stats.dead == 0, stats.errors
    assert provider.calls() == ["put"]
    assert provider.with_ids() == []
    # And its expiry, on the sweep's autocommit connection.
    created = sql.execute(
        "SELECT created_at FROM catalog.snapshot_export WHERE export_id = %s",
        (int(ex["export_id"]),),
    ).fetchone()[0]

    async def sweep() -> int:
        async with await psycopg.AsyncConnection.connect(
            as_app(db_url, "worker"), autocommit=True
        ) as conn:
            return await exports.expire(conn, created + 2 * exports.KEEP, lambda _id: provider)

    assert asyncio.run(sweep()) >= 1
    assert "delete" in provider.calls()
    assert (
        provider.with_ids() == []
        and [s for s in provider.seen if s.call == "delete" and s.tables] == []
    )


def test_a_second_holder_of_an_export_step_waits(make_world, db_url, facade, sql) -> None:
    """The row lock made it wait before. The advisory lock defers it, keeping its message."""
    w = make_world()
    create_file(w, "a")
    ex = start(w).json()
    export_id = int(ex["export_id"])
    sql.execute(
        "DELETE FROM pgmq.q_snapshot_export WHERE message->>'export_id' <> %s", (ex["export_id"],)
    )

    async def handler(msg, conn) -> None:  # type: ignore[no-untyped-def]
        await exports.step(conn, lambda _id: facade.sim, int(msg.body["export_id"]))

    with psycopg.connect(db_url) as holder:
        holder.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"omnifile:export:{export_id}",)
        )
        stats = drain_queue(db_url, "snapshot_export", handler)
        assert stats.retried == 1 and stats.acked == 0
        holder.rollback()
    assert sql.execute(
        "SELECT count(*) FROM pgmq.q_snapshot_export WHERE message->>'export_id' = %s",
        (ex["export_id"],),
    ).fetchone() == (1,)


def test_an_export_that_expired_during_its_upload_records_nothing(
    make_world, db_url, facade, sql
) -> None:
    w = make_world()
    create_file(w, "a")
    ex = start(w).json()
    export_id = int(ex["export_id"])
    sql.execute(
        "DELETE FROM pgmq.q_snapshot_export WHERE message->>'export_id' <> %s", (ex["export_id"],)
    )

    def expired(call: str) -> None:
        if call == "put":
            with psycopg.connect(db_url, autocommit=True) as c:
                c.execute(
                    "UPDATE catalog.snapshot_export SET state = 'expired', finished_at = now()"
                    " WHERE export_id = %s",
                    (export_id,),
                )

    provider = Watched(facade.sim, db_url, expired)

    async def handler(msg, conn) -> None:  # type: ignore[no-untyped-def]
        await exports.step(conn, lambda _id: provider, int(msg.body["export_id"]))

    stats = drain_queue(db_url, "snapshot_export", handler)
    assert stats.dead == 0, stats.errors
    assert provider.calls() == ["put"]
    (state, parts) = sql.execute(
        "SELECT state, parts FROM catalog.snapshot_export WHERE export_id = %s", (export_id,)
    ).fetchone()
    assert (state, parts) == ("expired", [])
    assert sql.execute(
        "SELECT count(*) FROM pgmq.q_snapshot_export WHERE message->>'export_id' = %s",
        (ex["export_id"],),
    ).fetchone() == (0,)
