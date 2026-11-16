"""The M5 exit test, service side (docs/BUILD-PLAN.md M5; docs/milestones/M5.md).

The gate, against the S3 fake and sim:

- a bucket with Standard, Glacier and Deep Archive objects is served
  through one share: the edge's feed carries all three, at their paths,
  with their S3 locations;
- a user read of the Glacier object starts a restore, a Teams card (the
  notify queue) and, once the restore completes, a Storage Mover copy of
  it (staged: decision 24), while the edge gets an S3 pointer to fill
  from in the meantime;
- the verifier passes, and the prefix flips with no client-visible change:
  same files, paths, sizes and content versions; only where the bytes come
  from changes.

The NFS half of the gate (a real Ganesha share and a real fill) needs the
M1 stack: `tests/e2e/`. This runs the real `api` app, the real S3 backend
over the fake, and the worker's handlers.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta
from typing import Any

import psycopg
from s3.s3_fakes import WEB_IDENTITY, FakeAws
from sources.support import BUCKET, GROUPS, REGION, put

from omnifile_service.api.dataplane import BackendDataPlane
from omnifile_service.backends.base import LogicalTier, ObjectRef
from omnifile_service.backends.s3 import S3Backend, S3Config
from omnifile_service.backends.sim import crc64nvme
from omnifile_service.catalog.privileges import as_app
from omnifile_service.migrate import SimMover, migrate_step, request_location, verify_copy
from omnifile_service.sources import register_source, seed_step
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.changefeed_sync import sync_container
from omnifile_service.worker.queue import Message, Retry
from omnifile_service.worker.recalls import RecallConfig, handle_restore

from .conftest import E
from .test_upload_flow import feed_all, read_req

NOW = RecallConfig(poll_min=timedelta(0), poll_max=timedelta(0), poll_no_eta=timedelta(0))
OBJECTS = {"std.segy": b"standard" * 64, "gl.segy": b"glacier!" * 96, "da.segy": b"deeparch" * 80}


class Clock:
    """The S3 fake's clock: the system clock plus an offset, so the api and
    the sim (on the system clock) and S3's restore times agree."""

    def __init__(self) -> None:
        from datetime import UTC, datetime

        self._dt = datetime
        self._utc = UTC
        self.offset = timedelta(0)

    def now(self):  # type: ignore[no-untyped-def]
        return self._dt.now(self._utc) + self.offset

    def advance(self, d: timedelta) -> None:
        self.offset += d


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _tx(db_url: str, fn):  # type: ignore[no-untyped-def]
    async with await psycopg.AsyncConnection.connect(as_app(db_url, "worker")) as conn:
        async with conn.transaction():
            return await fn(conn)


def test_m5_exit_service_side(make_world, api, facade, sql, db_url) -> None:  # type: ignore[no-untyped-def]
    clock = Clock()
    fake = FakeAws(clock.now)
    queue = fake.queue_url("m5-events")
    fake.add_bucket(BUCKET, queue=queue)
    fake.add_bucket("staging")
    (s3_id,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES ('m5-s3-' || gen_random_uuid(), 's3', %s, false) RETURNING backend_id",
        ('{"region": "eu-west-1"}',),
    ).fetchone()
    s3 = S3Backend(
        s3_id,
        S3Config(
            region=REGION, buckets=(BUCKET, "staging"), private_network=False,
            **WEB_IDENTITY, event_queues={BUCKET: queue}, staging_bucket="staging",
        ),
        clock=clock,  # type: ignore[arg-type]
        on_client=fake.attach,
    )  # fmt: skip
    api.services.planes.add(s3_id, BackendDataPlane(s3))
    home = facade.sim
    registry = BackendRegistry.from_mapping({s3_id: s3, api.backend_id: home})
    mover = SimMover(registry)

    # --- A bucket with Standard, Glacier and Deep Archive objects ----------------
    w = make_world(on_open="wait")
    sql.execute(
        "UPDATE catalog.dataset SET storage_layout = 'two_account',"
        ' posix_defaults = \'{"uid": 5000, "gid": 5001}\' WHERE dataset_id = %s',
        (w.dataset_id,),
    )

    class W:  # what sources.support.put needs
        pass

    # The client's own tools fill its bucket; the service's binding (`s3`)
    # is read-only there.
    client = S3Backend(
        s3_id, dataclasses.replace(s3.config, owned_buckets=(BUCKET,)),
        clock=clock,  # type: ignore[arg-type]
        on_client=fake.attach,
    )  # fmt: skip
    sw = W()
    sw.client = client  # type: ignore[attr-defined]
    run(put(sw, "m/std.segy", OBJECTS["std.segy"]))  # type: ignore[arg-type]
    gl = run(put(sw, "m/gl.segy", OBJECTS["gl.segy"], tier=LogicalTier.ARCHIVE))  # type: ignore[arg-type]
    da = run(put(sw, "m/da.segy", OBJECTS["da.segy"]))  # type: ignore[arg-type]
    run(client.set_class(da, LogicalTier.ARCHIVE, native_class="DEEP_ARCHIVE"))

    async def seed(conn) -> int:  # type: ignore[no-untyped-def]
        sid = await register_source(
            conn, s3, dataset_id=w.dataset_id, container=BUCKET, groups=GROUPS, prefix="m/"
        )
        return sid

    sid = run(_tx(db_url, seed))
    while run(_tx(db_url, lambda c: seed_step(c, registry.get, sid, clock.now()))) != "seeded":
        pass

    def sync() -> None:
        clock.advance(timedelta(seconds=5))
        run(_tx(db_url, lambda c: sync_container(c, registry, s3_id, BUCKET, clock.now())))

    sync()

    # --- Served through one share: the edge's feed has all three ------------------
    events, cursor = feed_all(w)
    files = _files(events)
    assert set(files) == set(OBJECTS), files
    for name, data in OBJECTS.items():
        f = files[name]
        assert f["size"] == len(data) and f["location"]["backend_kind"] == "s3"
        assert f["location"]["container"] == BUCKET and f["location"]["object_key"] == f"m/{name}"
    assert files["std.segy"]["location"]["archived"] is False
    assert files["gl.segy"]["location"]["archived"] is True
    assert files["da.segy"]["location"]["storage_class"] == "DEEP_ARCHIVE"
    before = {n: _visible(f) for n, f in files.items()}

    # An online S3 object reads at once: the credential is only a pointer
    # (no secret, no URL); the edge signs with its own Roles Anywhere session.
    std = files["std.segy"]
    online = w.edge.post(f"{E}/recalls", read_req(int(std["file_id"]), std["version_id"]))
    assert online.status_code == 200, online.text
    assert online.json()["status"] == "online"
    pointer = online.json()["credential"]
    assert pointer["mode"] == "s3_roles_anywhere" and "url" not in pointer
    assert (pointer["bucket"], pointer["key"]) == (BUCKET, "m/std.segy")
    assert pointer["version_id"] == std["location"]["backend_version_id"]
    assert pointer["tls_max_version"] == "1.2" and pointer["requester_pays"] is False
    # The location's ETag, quoted for If-Match (unversioned objects are
    # pinned by it).
    assert pointer["etag"].startswith('"') and pointer["etag"].endswith('"')
    # Seeded from a listing, the catalog had no CRC: the credential carries
    # the one S3 stores for the object, which the edge verifies the fill by.
    want_crc = f"{crc64nvme(OBJECTS['std.segy']):016x}"
    assert files["std.segy"].get("crc64nvme") is None
    assert online.json()["credential"]["crc64nvme"] == want_crc
    # Recorded, so the feed carries it to the edge (and the next read needs
    # no HEAD).
    learned, cursor = feed_all(w, cursor)
    assert _files(learned)["std.segy"]["crc64nvme"] == want_crc

    # --- A user read of the Glacier object: restore, card, copy ------------------------
    migration = run(_tx(db_url, lambda c: request_location(
        c, dataset_id=w.dataset_id, prefix="m/", to_backend_id=api.backend_id)))  # fmt: skip

    def step(n: int = 1) -> str:
        state = ""
        for _ in range(n):
            state = run(_tx(db_url, lambda c: migrate_step(
                c, registry.get, mover, migration, clock.now()))).state  # fmt: skip
            pending = sql.execute(
                "SELECT location_id FROM catalog.location WHERE backend_id = %s"
                "   AND state = 'pending' ORDER BY location_id",
                (api.backend_id,),
            ).fetchall()
            for (lid,) in pending:
                run(_tx(db_url, lambda c, lid=lid: verify_copy(c, registry.get, lid, clock.now())))
            sync()
        return state

    assert step(3) == "verifying"  # Standard copied; the archived two wait
    g = files["gl.segy"]
    r = w.edge.post(f"{E}/recalls", read_req(int(g["file_id"]), g["version_id"]))
    assert r.status_code == 200 and r.json()["status"] == "recall", r.text
    recall_id = int(r.json()["recall_id"])

    def worker_restore() -> None:
        async def go(conn) -> None:  # type: ignore[no-untyped-def]
            msg = Message(1, 1, clock.now(), clock.now(), {"recall_id": str(recall_id)})
            try:
                await handle_restore(msg, conn, registry=registry, now=clock.now(), config=NOW)
            except Retry:
                pass

        run(_tx(db_url, go))

    worker_restore()
    restore = sql.execute(
        "SELECT r.mechanism, r.state, r.migrate_on_complete FROM catalog.restore r"
        "  JOIN catalog.recall c USING (restore_id) WHERE c.recall_id = %s",
        (recall_id,),
    ).fetchone()
    assert restore == ("s3_restore_object", "in_progress", True)
    assert [c for c, _ in fake.calls].count("RestoreObject") == 1
    # The Teams card: the recall's state changes went to the notify queue.
    notified = [m for (m,) in sql.execute("SELECT message FROM pgmq.q_notify").fetchall()]
    assert {"recall_id": str(recall_id)} in notified

    # Standard retrieval from Glacier: about 4 h in the fake.
    clock.advance(timedelta(hours=5))
    sync()
    worker_restore()
    (state,) = sql.execute(
        "SELECT state FROM catalog.recall WHERE recall_id = %s", (recall_id,)
    ).fetchone()
    assert state == "downloading"
    events2, cursor = feed_all(w, cursor)
    rec = [e for e in events2 if e["type"] == "recall.state"][-1]["recall"]
    assert rec["state"] == "downloading"
    assert rec["read_credential"]["mode"] == "s3_roles_anywhere"
    assert rec["read_credential"]["version_id"] == gl.version_id

    # The restore serves the move too: staged, copied by a Storage Mover run.
    last_run = sql.execute(
        "SELECT last_run_id FROM catalog.migration_prefix WHERE migration_id = %s", (migration,)
    ).fetchone()[0]
    step(2)
    staged = sql.execute(
        "SELECT state FROM catalog.staging_object WHERE migration_id = %s", (migration,)
    ).fetchall()
    assert staged and staged[0][0] in ("copied", "removed"), staged
    gl_copy = sql.execute(
        "SELECT verified, verified_by FROM catalog.location WHERE backend_id = %s"
        "   AND object_key LIKE %s AND state = 'committed'",
        (api.backend_id, "%/m/gl.segy"),
    ).fetchone()
    assert gl_copy == (True, "azure_read_crc")
    assert (
        sql.execute(
            "SELECT last_run_id FROM catalog.migration_prefix WHERE migration_id = %s", (migration,)
        ).fetchone()[0]
        == last_run
    ), "no rerun of the whole prefix"

    # The Deep Archive object comes back too (a wave or a recall); here, a
    # recall, then the prefix is complete.
    d = files["da.segy"]
    r2 = w.edge.post(f"{E}/recalls", read_req(int(d["file_id"]), d["version_id"]))
    recall_id = int(r2.json()["recall_id"])
    worker_restore()
    clock.advance(timedelta(hours=13))
    sync()
    worker_restore()

    # --- The verifier passes and the prefix flips ---------------------------------------
    state = ""
    for _ in range(10):
        state = step()
        if state == "cut_over":
            break
    assert state == "cut_over"
    m = sql.execute(
        "SELECT files_total, files_verified FROM catalog.migration_prefix WHERE migration_id = %s",
        (migration,),
    ).fetchone()
    assert m == (3, 3)
    (staging_left,) = sql.execute(
        "SELECT count(*) FROM catalog.staging_object"
        " WHERE migration_id = %s AND state <> 'removed'",
        (migration,),
    ).fetchone()
    assert staging_left == 0

    # --- No client-visible change ----------------------------------------------------------
    events3, _ = feed_all(w)
    after = _files(events3)
    assert {n: _visible(f) for n, f in after.items()} == before
    # The CRCs the reads learned from S3 reached the edge's feed, and are the
    # objects' own.
    for name in ("std.segy", "gl.segy"):
        assert after[name]["crc64nvme"] == f"{crc64nvme(OBJECTS[name]):016x}", name
    for f in after.values():
        assert f["location"]["backend_kind"] != "s3", f["location"]
        assert f["location"]["object_key"].endswith(f"m/{f['name']}")
    # The edge reads the current bytes from Azure now.
    std2 = after["std.segy"]
    online = w.edge.post(f"{E}/recalls", read_req(int(std2["file_id"]), std2["version_id"]))
    assert online.json()["credential"]["mode"] == "azure_user_delegation_sas"
    # Nothing was deleted in S3.
    for key in ("m/std.segy", "m/gl.segy", "m/da.segy"):
        run(s3.head(ObjectRef(s3_id, BUCKET, key)))


def _files(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for e in events:
        f = e.get("file")
        if f and f.get("kind") == "file" and not f.get("deleted"):
            out[f["name"]] = f
    return out


def _visible(f: dict[str, Any]) -> tuple[Any, ...]:
    """What a client sees through the share."""
    keys = ("file_id", "parent_id", "name", "size", "version_id", "uid", "gid", "mode",
            "mtime_ns")  # fmt: skip
    return tuple(f.get(k) for k in keys)
