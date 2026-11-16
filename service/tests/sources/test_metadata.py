"""Seeding from S3 Metadata tables and following their journal
(docs/design/catalog-seeding.md sections 7 and 8; sources/metadata_s3.py,
worker/journal_sync.py), over a local Iceberg catalog and the S3 fake."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from s3.s3_fakes import ACCOUNT, WEB_IDENTITY

from omnifile_service.backends.base import BackendError, LogicalTier, ObjectRef
from omnifile_service.backends.s3 import S3Config
from omnifile_service.backends.s3.credentials import botocore_session_for
from omnifile_service.sources import (
    SourceError,
    create_source,
    metadata_s3,
    register_source,
    seed_step,
)
from omnifile_service.sources.__main__ import parse_inventory
from omnifile_service.sources.metadata_s3 import S3MetadataReader, change_record, check_config
from omnifile_service.worker import journal_sync
from omnifile_service.worker.journal_sync import follow_journal, journal_all

from .metadata_fake import TABLE_BUCKET_ARN, FakeMetadataTables
from .support import BUCKET, GROUPS, deployment, put, world

LISTS = {"ListObjectsV2", "ListObjectVersions", "ListObjects"}
#: Keys whose names need care: a space, '+', '%', non-ASCII. Metadata tables
#: carry keys as they are (not URL-encoded like a CSV inventory).
ODD = ["f/with space.segy", "f/plus+sign.txt", "f/per%25cent.dat", "f/ünï/čódé 東京.txt"]


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fresh_indexes():  # type: ignore[no-untyped-def]
    metadata_s3.clear_indexes()
    yield
    metadata_s3.clear_indexes()


@pytest.fixture
def tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Make a FakeMetadataTables for a world; the service reads it in place
    of the S3 Tables REST endpoint."""
    made: list[FakeMetadataTables] = []

    def make(w) -> FakeMetadataTables:  # type: ignore[no-untyped-def]
        t = FakeMetadataTables(tmp_path / f"t{len(made)}", w.fake, BUCKET)
        made.append(t)
        monkeypatch.setattr(metadata_s3, "load_catalog_for", lambda backend, cfg: t.catalog)
        return t

    return make


async def _with(connect, fn):  # type: ignore[no-untyped-def]
    conn = await connect()
    try:
        return await fn(conn)
    finally:
        await conn.close()


def _register(connect, w, t, prefix: str = "f/", **kw: Any) -> int:  # type: ignore[no-untyped-def]
    inventory = kw.pop("inventory", t.config if t is not None else None)
    return run(_with(connect, lambda c: register_source(
        c, w.s3, dataset_id=w.dataset_id, container=BUCKET, groups=GROUPS, prefix=prefix,
        inventory=inventory, **kw)))  # fmt: skip


def _seed(connect, w, sid: int, batch: int = 5000) -> int:  # type: ignore[no-untyped-def]
    async def go(conn) -> int:  # type: ignore[no-untyped-def]
        steps = 0
        while True:
            async with conn.transaction():
                state = await seed_step(conn, w.registry.get, sid, w.clock.now(), batch=batch)
            steps += 1
            if state in ("seeded", "failed"):
                return steps

    return run(_with(connect, go))


def _follow(connect, w, sid: int, **kw: Any) -> int:  # type: ignore[no-untyped-def]
    w.clock.advance(timedelta(seconds=5))
    return run(_with(connect, lambda c: follow_journal(c, w.registry, sid, w.clock.now(), **kw)))


def _files(db: psycopg.Connection, w) -> dict[str, dict]:  # type: ignore[no-untyped-def]
    """path -> the live file and its primary location."""
    rows = db.execute(
        """
        WITH RECURSIVE t(file_id, path) AS (
            SELECT file_id, ''::text COLLATE "C" FROM catalog.file
             WHERE dataset_id = %s AND parent_id IS NULL
            UNION ALL
            SELECT f.file_id, t.path || '/' || f.name FROM catalog.file f
              JOIN t ON f.parent_id = t.file_id WHERE f.deleted_at IS NULL)
        SELECT t.path, f.file_id, f.kind, f.state, f.size, l.object_key, l.backend_version_id,
               l.storage_class, l.logical_tier, l.md5, l.etag
          FROM t JOIN catalog.file f USING (file_id)
          LEFT JOIN catalog.location l ON l.location_id = f.primary_location_id
         WHERE t.path <> ''
        """,
        (w.dataset_id,),
    )
    names = [d.name for d in rows.description]
    return {r[0]: dict(zip(names, r, strict=True)) for r in rows.fetchall()}


def _source(db: psycopg.Connection, sid: int) -> dict:
    cur = db.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM catalog.source WHERE source_id = %s", (sid,))
    return cur.fetchone()  # type: ignore[return-value]


def _versions(db: psycopg.Connection, file_id: int) -> list[tuple[str, str | None]]:
    return [
        (k, s)
        for k, s in db.execute(
            "SELECT v.kind, l.state FROM catalog.file_version v"
            "  LEFT JOIN catalog.location l ON l.version_id = v.version_id AND l.role = 'primary'"
            " WHERE v.file_id = %s ORDER BY v.seq",
            (file_id,),
        ).fetchall()
    ]


def _lists(w) -> list[str]:  # type: ignore[no-untyped-def]
    return [op for op in w.fake.ops() if op in LISTS]


def _bucket_writes(w) -> list[str]:  # type: ignore[no-untyped-def]
    """What the service sent the source bucket that would change it."""
    writes = {"PutObject", "CopyObject", "DeleteObject", "DeleteObjects",
              "CreateMultipartUpload", "UploadPart", "CompleteMultipartUpload",
              "PutObjectTagging", "RestoreObject"}  # fmt: skip
    return [op for op, p in w.fake.calls if op in writes and p.get("Bucket") == BUCKET]


def _a_bucket(w) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Versions with several rows per key, a delete marker, a folder
    marker, odd names, and one key outside the prefix."""
    refs: dict[str, Any] = {}
    refs["one"] = run(put(w, "f/one.segy", b"1"))
    run(put(w, "f/two.segy", b"old"))
    w.clock.advance(timedelta(seconds=1))
    refs["two"] = run(put(w, "f/two.segy", b"newer"))
    run(put(w, "f/gone.segy", b"x"))
    w.clock.advance(timedelta(seconds=1))
    run(w.client.delete(ObjectRef(w.s3_id, BUCKET, "f/gone.segy")))  # a delete marker
    run(put(w, "f/empty/", b""))
    run(put(w, "f/deep/a/b.txt", b"deep"))
    run(put(w, "f/cold.segy", b"c", LogicalTier.ARCHIVE))
    for key in ODD:
        refs[key] = run(put(w, key, key.encode()))
    run(put(w, "other/outside.txt"))
    return refs


# --- seeding --------------------------------------------------------------------


def test_metadata_tables_seed_current_versions_with_no_list(db, connect, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    refs = _a_bucket(w)
    t.snapshot()
    w.fake.calls.clear()
    sid = _register(connect, w, t)
    _seed(connect, w, sid, batch=2)  # many batches: a key's rows straddle them
    files = _files(db, w)
    want = {"/one.segy", "/two.segy", "/empty", "/deep", "/deep/a", "/deep/a/b.txt",
            "/cold.segy", "/with space.segy", "/plus+sign.txt", "/per%25cent.dat", "/ünï",
            "/ünï/čódé 東京.txt"}  # fmt: skip
    assert set(files) == want
    two = files["/two.segy"]
    assert (two["size"], two["backend_version_id"]) == (5, refs["two"].version_id)
    assert files["/cold.segy"]["logical_tier"] == "archive"
    for key in ODD:
        f = files["/" + key[2:]]
        assert (f["object_key"], f["backend_version_id"]) == (key, refs[key].version_id)
        assert f["size"] == len(key.encode())
    one = files["/one.segy"]
    assert one["state"] == "remote" and one["etag"] is not None
    assert one["md5"] is not None  # single part, SSE-S3: the ETag is the MD5
    src = _source(db, sid)
    assert src["state"] == "seeded" and src["inventory"]["kind"] == "s3_metadata"
    # Every row read (old versions and the marker too), current ones seeded.
    assert src["files_created"] == 8 and src["skipped"] == 0
    assert _lists(w) == [] and _bucket_writes(w) == []


def test_seeding_twice_changes_nothing(db, connect, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    _a_bucket(w)
    t.snapshot()
    sid = _register(connect, w, t)
    _seed(connect, w, sid)
    before = _files(db, w)
    db.execute(
        "UPDATE catalog.source SET state = 'seeding', resume = NULL WHERE source_id = %s", (sid,)
    )
    _seed(connect, w, sid)
    assert _files(db, w) == before
    assert _source(db, sid)["already_present"] == 8


def test_a_crash_mid_seed_resumes_from_the_last_batch(db, connect, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    _a_bucket(w)
    t.snapshot()
    sid = _register(connect, w, t)

    async def two_batches_then_crash(conn) -> None:  # type: ignore[no-untyped-def]
        for _ in range(2):
            async with conn.transaction():
                await seed_step(conn, w.registry.get, sid, w.clock.now(), batch=2)

        class Crash(Exception):
            pass

        with pytest.raises(Crash):
            async with conn.transaction():
                await seed_step(conn, w.registry.get, sid, w.clock.now(), batch=2)
                raise Crash  # the worker died before the batch committed

    run(_with(connect, two_batches_then_crash))
    src = _source(db, sid)
    assert src["state"] == "seeding" and src["resume"]["snapshot_id"] is not None
    assert src["files_created"] + src["dirs_created"] > 0
    metadata_s3.clear_indexes()  # a new worker process
    _seed(connect, w, sid, batch=2)
    src = _source(db, sid)
    assert src["state"] == "seeded" and src["files_created"] == 8
    assert src["already_present"] == 0  # nothing read twice
    assert len(_files(db, w)) == 12


def test_a_seed_whose_snapshot_expired_starts_again_on_the_current_one(db, connect, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    _a_bucket(w)
    t.snapshot()
    sid = _register(connect, w, t)
    db.execute(
        "UPDATE catalog.source SET state = 'seeding', resume = %s WHERE source_id = %s",
        (Jsonb({"snapshot_id": 12345, "task": 3, "offset": 7}), sid),
    )
    _seed(connect, w, sid)
    assert _source(db, sid)["state"] == "seeded" and len(_files(db, w)) == 12


def test_the_snapshot_time_allows_for_the_tables_lag(db, connect, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    run(put(w, "f/a"))
    t.snapshot()
    sid = _register(connect, w, t)
    snap = t.inventory.refresh().current_snapshot()
    src = _source(db, sid)
    assert snap is not None
    assert src["snapshot_at"].timestamp() == pytest.approx(
        snap.timestamp_ms / 1000 - metadata_s3.INVENTORY_LAG.total_seconds(), abs=0.01
    )
    # The bucket's EventBridge feed starts there too (restores still come that way).
    (cursor,) = db.execute(
        "SELECT count(*) FROM catalog.changefeed_cursor WHERE backend_id = %s AND container = %s",
        (w.s3_id, BUCKET),
    ).fetchone()
    assert cursor == 1


def test_an_empty_table_or_another_buckets_table_fails_the_source(db, connect, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    with pytest.raises(SourceError, match="no snapshot"):
        _register(connect, w, t)
    run(put(w, "f/a"))
    t.bucket_column = "someone-else"
    t.snapshot()
    sid = _register(connect, w, t)
    _seed(connect, w, sid)
    src = _source(db, sid)
    assert src["state"] == "failed" and "someone-else" in src["last_error"]


# --- choosing the reader ------------------------------------------------------------


def _metadata_backend(monkeypatch, db: psycopg.Connection, w, namespace: str) -> None:  # type: ignore[no-untyped-def]
    # The tables are the operator's account settings, never a row's.
    deployment(monkeypatch, db, w, metadata_tables={
        BUCKET: {"table_bucket_arn": TABLE_BUCKET_ARN, "namespace": namespace}})  # fmt: skip


def test_a_bucket_with_metadata_tables_prefers_them(db, connect, tables, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    _metadata_backend(monkeypatch, db, w, t.namespace)
    run(put(w, "f/a"))
    run(put(w, "g/b"))
    t.snapshot()
    w.fake.calls.clear()
    sid = _register(connect, w, None)  # no inventory named
    assert _source(db, sid)["inventory"] == t.config
    _seed(connect, w, sid)
    assert _lists(w) == []
    # A listing only when asked for.
    listed = _register(connect, w, None, prefix="g/", inventory={"kind": "listing"})
    assert _source(db, listed)["inventory"] == {"kind": "listing"}
    _seed(connect, w, listed)
    assert _lists(w) != []
    assert set(_files(db, w)) == {"/a", "/b"}


def test_metadata_config_is_checked() -> None:
    good = {"kind": "s3_metadata", "table_bucket_arn": TABLE_BUCKET_ARN}
    assert check_config(good, region="eu-west-1") == good
    with pytest.raises(SourceError, match="table_bucket_arn"):
        check_config({"kind": "s3_metadata", "table_bucket_arn": "arn:aws:s3:::bucket"})
    with pytest.raises(SourceError, match="eu-west-1"):
        check_config(good, region="us-east-1")
    with pytest.raises(SourceError, match="doesn't take"):
        check_config({**good, "uri": "https://attacker.example/iceberg"})
    with pytest.raises(SourceError, match="namespace"):
        check_config({**good, "namespace": "Bad-Name"})


def test_create_source_refuses_bad_metadata_config(db, connect, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)

    async def go(conn, inventory) -> int:  # type: ignore[no-untyped-def]
        async with conn.transaction():
            return await create_source(
                conn, dataset_id=w.dataset_id, backend_id=w.s3_id, container=BUCKET,
                groups=GROUPS, prefix="f/", inventory=inventory,
            )  # fmt: skip

    arn = "arn:aws:s3tables:us-east-1:123456789012:bucket/aws-s3"
    wrong_region = {"kind": "s3_metadata", "table_bucket_arn": arn}
    with pytest.raises(SourceError, match="us-east-1"):
        run(_with(connect, lambda c: go(c, wrong_region)))
    with pytest.raises(SourceError, match="no settings"):
        run(_with(connect, lambda c: go(c, {"kind": "listing", "x": 1})))


def test_private_networking_needs_the_s3_tables_endpoint() -> None:
    from omnifile_service.backends.s3.config import (
        AwsDeployment,
        ConfigError,
        check_row_keys,
    )

    tables = {"b": {"table_bucket_arn": TABLE_BUCKET_ARN}}
    vpce = "vpce-0abc.{}.eu-west-1.vpce.amazonaws.com"
    private = {"endpoint_url": f"https://bucket.{vpce.format('s3')}",
               "sts_endpoint_url": f"https://{vpce.format('sts')}"}  # fmt: skip
    with pytest.raises(ValueError, match="s3tables_endpoint_url"):
        S3Config(region="eu-west-1", buckets=("b",), **private, **WEB_IDENTITY,
                 metadata_tables=tables)  # fmt: skip
    # The tables and their endpoint are the operator's account settings.
    dep = AwsDeployment.from_mapping(
        {ACCOUNT: {"region": "eu-west-1", "role_arn": WEB_IDENTITY["role_arn"],
                   "endpoints": {"s3": private["endpoint_url"], "sts": private["sts_endpoint_url"],
                                 "s3tables": f"https://{vpce.format('s3tables')}"},
                   "metadata_tables": tables}},
        web_identity_audience=WEB_IDENTITY["web_identity_audience"],
    )  # fmt: skip
    ok = S3Config.for_backend(
        {"account_id": ACCOUNT, "region": "eu-west-1", "buckets": ["b"]},
        private_network=True,
        deployment=dep,
    )
    assert ok.metadata_tables["b"]["table_bucket_arn"] == TABLE_BUCKET_ARN
    assert ok.s3tables_endpoint_url == f"https://{vpce.format('s3tables')}"
    # A row (which a partner's source may point at) can name neither.
    for key in ("metadata_tables", "s3tables_endpoint_url"):
        with pytest.raises(ConfigError, match=key):
            check_row_keys({"account_id": ACCOUNT, "region": "eu-west-1", key: tables})


def test_the_rest_catalog_is_the_backends_never_the_sources(db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    seen: dict[str, Any] = {}

    def fake_load(name: str, **props: Any) -> str:
        seen.update(props, name=name)
        return "catalog"

    import pyiceberg.catalog

    monkeypatch.setattr(pyiceberg.catalog, "load_catalog", fake_load)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    # An emulator binding (`default` chain, loopback only): the keys above.
    emu = S3Config(region="eu-west-1", endpoint_url="http://127.0.0.1:9", private_network=False,
                   credentials="default")  # fmt: skip
    backend = type("B", (), {"config": emu, "backend_id": w.s3_id})()
    cfg = check_config({"kind": "s3_metadata", "table_bucket_arn": TABLE_BUCKET_ARN})
    assert metadata_s3.load_catalog_for(backend, cfg) == "catalog"
    assert seen["uri"] == "https://s3tables.eu-west-1.amazonaws.com/iceberg"
    assert seen["s3.endpoint"] == "http://127.0.0.1:9"
    assert (seen["rest.signing-name"], seen["rest.sigv4-enabled"]) == ("s3tables", "true")
    assert seen["warehouse"] == TABLE_BUCKET_ARN
    assert seen["botocore_session"] is not None and seen["s3.access-key-id"] == "AKIDEXAMPLE"


def test_web_identity_signs_s3_tables_too() -> None:
    """The synchronous session PyIceberg signs with holds the web identity
    provider alone (no keys, no stray chain), and calls STS only when a
    request is first signed."""
    cfg = S3Config(region="eu-west-1", private_network=False, **WEB_IDENTITY)
    calls: list[str] = []
    session = botocore_session_for(cfg, token_provider=lambda: calls.append("t") or "jwt")
    (provider,) = session.get_component("credential_provider").providers
    assert provider.METHOD == "assume-role-with-web-identity"
    assert calls == [], "deferred: no token asked for, no STS call yet"


def test_parse_inventory_metadata_and_listing() -> None:
    assert parse_inventory(None, metadata_table=TABLE_BUCKET_ARN) == {
        "kind": "s3_metadata", "table_bucket_arn": TABLE_BUCKET_ARN}  # fmt: skip
    assert parse_inventory(None, listing=True) == {"kind": "listing"}
    with pytest.raises(SystemExit):
        parse_inventory("s3://b/k/manifest.json", listing=True)


# --- the journal --------------------------------------------------------------------


def _seeded(connect, w, t) -> int:  # type: ignore[no-untyped-def]
    """Seed a source over a bucket holding f/seed.txt."""
    run(put(w, "f/seed.txt"))
    t.snapshot()
    sid = _register(connect, w, t)
    _seed(connect, w, sid)
    return sid


def test_the_journal_keeps_a_seeded_source_current(db, connect, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    old = run(put(w, "f/old.txt", b"one"))
    run(put(w, "f/gone.txt"))
    run(put(w, "f/v.txt", b"v1"))
    run(put(w, "f/cls.segy", b"c"))
    sid = _seeded(connect, w, t)
    first = _follow(connect, w, sid)  # nothing new yet
    assert first == 0
    w.fake.calls.clear()
    w.clock.advance(timedelta(minutes=1))
    # A new object (with an odd name), an overwrite, a delete marker.
    new = run(put(w, "f/nü dir/new+1.segy", b"12345"))
    t.created("f/nü dir/new+1.segy", w.clock.now())
    second = run(put(w, "f/old.txt", b"second"))
    t.created("f/old.txt", w.clock.now())
    run(w.client.delete(ObjectRef(w.s3_id, BUCKET, "f/gone.txt")))
    t.delete_marker("f/gone.txt", w.clock.now())
    # An overwrite then its version deleted for good: v1 is current again.
    v1 = w.fake.buckets[BUCKET].keys["f/v.txt"][-1]
    v2 = run(put(w, "f/v.txt", b"v2!"))
    t.created("f/v.txt", w.clock.now())
    v2_version = w.fake.buckets[BUCKET].keys["f/v.txt"][-1]
    w.clock.advance(timedelta(seconds=1))
    run(w.client.delete(v2))
    t.version_deleted("f/v.txt", v2_version, w.clock.now())
    # A lifecycle transition (S3's, not ours).
    w.fake.buckets[BUCKET].keys["f/cls.segy"][-1].storage_class = "GLACIER"
    t.class_changed("f/cls.segy", w.clock.now())
    # Outside the prefix: not this source's.
    run(put(w, "other/x"))
    t.created("other/x", w.clock.now())
    w.fake.calls.clear()

    assert _follow(connect, w, sid) == 6
    files = _files(db, w)
    f = files["/nü dir/new+1.segy"]
    assert (f["object_key"], f["backend_version_id"], f["size"]) == (
        "f/nü dir/new+1.segy", new.version_id, 5)  # fmt: skip
    o = files["/old.txt"]
    assert (o["backend_version_id"], o["size"]) == (second.version_id, 6)
    assert _versions(db, o["file_id"]) == [("content", "committed"), ("content", "committed")]
    assert o["backend_version_id"] != old.version_id
    assert "/gone.txt" not in files
    v = files["/v.txt"]
    assert (v["backend_version_id"], v["size"]) == (v1.vid, 2)
    c = files["/cls.segy"]
    assert (c["storage_class"], c["logical_tier"]) == ("GLACIER", "archive")
    # The journal prompts HEADs; nothing is listed and nothing is written.
    assert _lists(w) == [] and _bucket_writes(w) == []
    assert set(w.fake.ops()) <= {"HeadObject"}
    cursor = _source(db, sid)["journal"]
    assert cursor["snapshot_id"] == t.journal_table.refresh().current_snapshot().snapshot_id
    assert "after" not in cursor
    # Nothing new: nothing applied. Read again from the start: nothing changes.
    assert _follow(connect, w, sid) == 0
    before = _files(db, w)
    db.execute("UPDATE catalog.source SET journal = NULL WHERE source_id = %s", (sid,))
    _follow(connect, w, sid)
    assert _files(db, w) == before
    assert len(_versions(db, before["/old.txt"]["file_id"])) == 2


def test_changes_made_while_seeding_come_from_the_journal(db, connect, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    run(put(w, "f/a.txt", b"a"))
    t.snapshot()
    sid = _register(connect, w, t)
    w.clock.advance(timedelta(minutes=5))
    run(put(w, "f/during.txt", b"made while seeding"))
    t.created("f/during.txt", w.clock.now())
    _seed(connect, w, sid)
    assert "/during.txt" not in _files(db, w)  # the snapshot predates it
    assert _follow(connect, w, sid) == 1
    assert "/during.txt" in _files(db, w)


def test_the_journal_waits_until_the_source_is_seeded(db, connect, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    run(put(w, "f/a.txt"))
    t.snapshot()
    sid = _register(connect, w, t)
    run(put(w, "f/b.txt"))
    t.created("f/b.txt", w.clock.now())
    assert _follow(connect, w, sid) == 0 and _source(db, sid)["journal"] is None


def test_a_crash_mid_journal_resumes_after_the_last_batch(db, connect, tables, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    sid = _seeded(connect, w, t)
    for i in range(5):
        run(put(w, f"f/n{i}.txt"))
        t.created(f"f/n{i}.txt", w.clock.now() + timedelta(seconds=i))
    real = journal_sync.apply_record
    calls = {"n": 0}

    async def flaky(conn, backend, rec, now, tiers=None, heads=None):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 3:
            raise BackendError("HEAD timed out")
        return await real(conn, backend, rec, now, tiers, heads)

    monkeypatch.setattr(journal_sync, "apply_record", flaky)
    with pytest.raises(BackendError):
        _follow(connect, w, sid, batch=2)
    cursor = _source(db, sid)["journal"]
    assert cursor["after"] is not None and cursor["to"] is not None  # one batch in
    assert len(_files(db, w)) == 3
    monkeypatch.setattr(journal_sync, "apply_record", real)
    assert _follow(connect, w, sid, batch=2) == 3  # the rest, not the first two again
    assert len(_files(db, w)) == 6
    assert "after" not in _source(db, sid)["journal"]


def test_a_read_limit_continues_on_the_next_pass(db, connect, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    sid = _seeded(connect, w, t)
    for i in range(5):
        run(put(w, f"f/n{i}.txt"))
        t.created(f"f/n{i}.txt", w.clock.now() + timedelta(seconds=i))
    assert _follow(connect, w, sid, limit=3) == 3
    assert _follow(connect, w, sid, limit=3) == 2
    assert _follow(connect, w, sid, limit=3) == 0
    assert len(_files(db, w)) == 6


def test_an_expired_journal_snapshot_is_read_by_time(db, connect, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    sid = _seeded(connect, w, t)
    run(put(w, "f/new.txt"))
    t.created("f/new.txt", w.clock.now())
    hour_ago = (w.clock.now() - timedelta(hours=1)).isoformat()
    db.execute(
        "UPDATE catalog.source SET journal = %s WHERE source_id = %s",
        (Jsonb({"snapshot_id": 987654321, "through": hour_ago}), sid),
    )
    assert _follow(connect, w, sid) == 1
    assert "/new.txt" in _files(db, w)


def test_the_sweep_follows_every_metadata_source(db, connect, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    sid = _seeded(connect, w, t)
    run(put(w, "f/new.txt"))
    t.created("f/new.txt", w.clock.now())
    n = run(_with(connect, lambda c: journal_all(c, w.registry, w.clock.now())))
    assert n >= 1 and "/new.txt" in _files(db, w)
    assert _source(db, sid)["last_error"] is None


def test_journal_rows_map_to_change_records() -> None:
    base = {"key": "k", "sequence_number": "1", "record_timestamp": "2026-10-01T00:00:00",
            "storage_class": "STANDARD", "size": 3}  # fmt: skip
    put_rec = change_record(1, "b", {**base, "record_type": "CREATE", "version_id": "v"})
    assert put_rec is not None and (put_rec.kind, put_rec.ref.version_id) == ("created", "v")
    marker = change_record(1, "b", {**base, "record_type": "DELETE", "is_delete_marker": True})
    assert marker is not None and marker.detail.get("tombstone") == "true"
    gone = change_record(1, "b", {**base, "record_type": "DELETE", "version_id": "v"})
    assert gone is not None and gone.kind == "deleted" and "tombstone" not in gone.detail
    tier = change_record(1, "b", {**base, "record_type": "UPDATE_METADATA",
                                  "storage_class": "GLACIER"})  # fmt: skip
    assert tier is not None and (tier.kind, tier.native_class) == ("tier_changed", "GLACIER")
    assert change_record(1, "b", {**base, "record_type": "ANNOTATION"}) is None


def test_the_reader_is_a_seed_reader(db, tables) -> None:  # type: ignore[no-untyped-def]
    w = world(db)
    t = tables(w)
    run(put(w, "f/a.txt", b"abc"))
    t.snapshot()
    r = S3MetadataReader(w.s3, BUCKET, "f/", t.config)
    got = run(r.read(None, 10))
    assert got.resume is None and [o.key for o in got.objects] == ["f/a.txt"]
    assert got.objects[0].size == 3 and got.objects[0].etag_is_md5
