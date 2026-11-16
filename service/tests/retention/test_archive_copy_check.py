"""The archive-copy integrity check (retention/worm_sync.py `delete_blob`,
real-azure-checks.md "Archive copy integrity"): before the original of a
two-account archive move is deleted, the copy's properties are read from
the provider and compared with the catalog. A mismatch blocks the delete.

The moves run through the real tiering and verify consumers on sim
(tests/policy/support.py). The provider's answer about the copy is then
changed underneath (a proxy over sim), as a copy damaged or replaced after
its read-back would answer."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from policy.support import World, make_world, primary, row, run, store, tick

from omnifile_service.backends.base import (
    BackendError,
    LogicalTier,
    ObjectHead,
    ObjectNotFound,
    ObjectRef,
)
from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.catalog.privileges import as_app
from omnifile_service.retention.worm_sync import delete_blob
from omnifile_service.worker.backends import BackendRegistry
from retention import delete_scan

QUEUES = ("tiering", "tiering_dlq", "retention", "verify", "restores", "notify")


@pytest.fixture
def two(db: psycopg.Connection, retention_db_url: str) -> Iterator[World]:
    for q in QUEUES:
        db.execute("SELECT pgmq.create(%s::text)", (q,))
        db.execute(f"DELETE FROM pgmq.q_{q}")
    yield make_world(db, retention_db_url, policy={"mode": "smart", "archive_day": 10})


class Provider:
    """sim, but answering about the archive copy what `patch` says."""

    def __init__(self, sim: Any, **patch: Any) -> None:
        self._sim = sim
        self.patch = patch

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)

    async def head(self, ref: ObjectRef) -> ObjectHead:
        h = await self._sim.head(ref)
        if ref.container.startswith("archive/") and self.patch:
            return dataclasses.replace(h, **self.patch)
        return h


def drain_retention(w: World, backend: Any) -> list[str]:
    """Run queued `delete_blob` work with `backend` as the provider."""
    registry = BackendRegistry.from_mapping({w.backend_id: backend})

    async def go() -> list[str]:
        conn = await psycopg.AsyncConnection.connect(as_app(w.url, "worker"), autocommit=True)
        out: list[str] = []
        try:
            while True:
                # Read first, as the consumer does: a read takes a transaction id.
                cur = await conn.execute(
                    "SELECT msg_id, message FROM pgmq.read('retention'::text, 30, 1)"
                )
                r = await cur.fetchone()
                if r is None:
                    return out
                async with conn.transaction():
                    if r[1].get("op") == "delete_blob":
                        lid = int(r[1]["location_id"])
                        out.append(await delete_blob(conn, registry, lid, w.clock.now()))
                    await conn.execute("SELECT pgmq.delete('retention'::text, %s::bigint)", (r[0],))
        finally:
            await conn.close()

    return run(go())


def archived(db: psycopg.Connection, w: World, name: str):  # type: ignore[no-untyped-def]
    """A file moved to the archive account by the real move, up to the
    original's delete, which waits on the retention queue."""
    f = store(db, w, name)
    w.clock.set(w.clock.now() + timedelta(days=10))
    tick(w)
    copy = primary(db, f.file_id)
    assert copy["container"] == "archive/data" and copy["verified"]
    assert copy["storage_class"] == "Archive"
    return f, copy


def refusal(db: psycopg.Connection, file_id: int) -> dict:
    r = row(db, "SELECT details FROM catalog.audit_log WHERE file_id = %s"
                " AND action = 'location.delete.refused' ORDER BY audit_id DESC LIMIT 1",
            file_id)  # fmt: skip
    assert r is not None
    return r["details"]


def kept(db: psycopg.Connection, w: World, f: Any, copy: dict, why: str) -> None:
    """The original is the primary again and still at the provider; the copy
    is on its way out; the refusal is audited; the file comes back later."""
    assert primary(db, f.file_id)["location_id"] == f.location_id
    loc = row(db, "SELECT state, verified FROM catalog.location WHERE location_id = %s",
              f.location_id)  # fmt: skip
    assert loc == {"state": "committed", "verified": True}
    assert run(w.sim.head(f.ref)).size == 4096
    assert row(db, "SELECT state FROM catalog.location WHERE location_id = %s",
               copy["location_id"])["state"] in ("deleting", "deleted")  # fmt: skip
    d = refusal(db, f.file_id)
    assert (d["why"], d["location_id"], d["replacement_location_id"]) == (
        why, f.location_id, copy["location_id"])  # fmt: skip
    nxt = row(db, "SELECT next_action, next_action_at FROM catalog.file WHERE file_id = %s",
              f.file_id)  # fmt: skip
    assert nxt["next_action_at"] == w.clock.now() + timedelta(days=1)


def test_a_good_copy_lets_the_original_go(db, two) -> None:  # type: ignore[no-untyped-def]
    f, copy = archived(db, two, "ok.segy")
    assert drain_retention(two, two.sim) == ["deleted"]
    with pytest.raises(ObjectNotFound):
        run(two.sim.head(f.ref))
    assert primary(db, f.file_id)["location_id"] == copy["location_id"]


def test_a_copy_whose_crc64_disagrees_keeps_the_original(db, two) -> None:  # type: ignore[no-untyped-def]
    """A provider that keeps a whole-object CRC64-NVME (S3 does; Azure's Get
    Blob Properties has none) answers with one that isn't the catalog's."""
    f, copy = archived(db, two, "crc.segy")
    provider = Provider(two.sim, crc64nvme=b"\xff" * 8)
    # The refusal, then the bad copy's delete (demoted to a secondary)
    # through the same queue.
    assert drain_retention(two, provider) == ["refused_replacement_crc64", "deleted"]
    kept(db, two, f, copy, "replacement_crc64")
    d = refusal(db, f.file_id)
    assert d["observed"]["crc64nvme"] == "ff" * 8
    assert d["expected"]["crc64nvme"] == bytes(copy["crc64nvme"]).hex()
    with pytest.raises(ObjectNotFound):
        run(two.sim.head(ObjectRef(two.backend_id, copy["container"], copy["object_key"],
                                   copy["backend_version_id"])))  # fmt: skip
    assert drain_retention(two, provider) == []


def test_a_copy_whose_crc64_agrees_lets_the_original_go(db, two) -> None:  # type: ignore[no-untyped-def]
    f, copy = archived(db, two, "same.segy")
    provider = Provider(two.sim, crc64nvme=bytes(copy["crc64nvme"]))
    assert drain_retention(two, provider) == ["deleted"]
    with pytest.raises(ObjectNotFound):
        run(two.sim.head(f.ref))


def test_a_copy_of_another_length_keeps_the_original(db, two) -> None:  # type: ignore[no-untyped-def]
    f, copy = archived(db, two, "short.segy")
    assert drain_retention(two, Provider(two.sim, size=4095)) == [
        "refused_replacement_size",
        "deleted",
    ]
    kept(db, two, f, copy, "replacement_size")


def test_a_copy_whose_md5_disagrees_keeps_the_original(db, two) -> None:  # type: ignore[no-untyped-def]
    """Azure's case: no CRC64 in the properties, but a Content-MD5."""
    f, copy = archived(db, two, "md5.segy")
    assert copy["md5"] is not None
    provider = Provider(two.sim, crc64nvme=None, md5=b"\x00" * 16)
    assert drain_retention(two, provider) == ["refused_replacement_md5", "deleted"]
    kept(db, two, f, copy, "replacement_md5")


def test_a_copy_with_no_checksum_must_have_been_read_back(db, two) -> None:  # type: ignore[no-untyped-def]
    """No CRC64 and no MD5 in the properties: the length is enough only for a
    copy that passed the in-Azure read-back before it went to Archive."""
    provider = Provider(two.sim, crc64nvme=None, md5=None)
    f, copy = archived(db, two, "read-back.segy")
    assert copy["verified"]
    assert drain_retention(two, provider) == ["deleted"]
    with pytest.raises(ObjectNotFound):
        run(two.sim.head(f.ref))

    g, gcopy = archived(db, two, "never-read.segy")
    db.execute(
        "UPDATE catalog.location SET verified = false, verified_at = NULL, verified_by = NULL"
        " WHERE location_id = %s",
        (gcopy["location_id"],),
    )
    assert drain_retention(two, provider) == ["refused_replacement_unverified", "deleted"]
    kept(db, two, g, gcopy, "replacement_unverified")


def test_a_missing_copy_keeps_the_original(db, two) -> None:  # type: ignore[no-untyped-def]
    f, copy = archived(db, two, "gone.segy")
    run(two.sim.delete(ObjectRef(two.backend_id, copy["container"], copy["object_key"],
                                 copy["backend_version_id"])))  # fmt: skip
    assert drain_retention(two, two.sim) == ["refused_replacement_missing", "gone"]
    kept(db, two, f, copy, "replacement_missing")


def test_a_live_version_is_never_left_without_a_copy(db, two) -> None:  # type: ignore[no-untyped-def]
    """Deleting the only copy of a live version (no replacement at all) is
    refused too; pruned versions and dropped copies are deleted as before."""
    f = store(db, two, "only.segy")
    db.execute(
        "UPDATE catalog.location SET state = 'deleting', verified = false WHERE location_id = %s",
        (f.location_id,),
    )
    registry = BackendRegistry.from_mapping({two.backend_id: two.sim})

    async def go() -> str:
        conn = await psycopg.AsyncConnection.connect(as_app(two.url, "worker"), autocommit=True)
        try:
            async with conn.transaction():
                return await delete_blob(conn, registry, f.location_id, two.clock.now())
        finally:
            await conn.close()

    assert run(go()) == "refused_no_replacement"
    assert primary(db, f.file_id)["location_id"] == f.location_id
    assert run(two.sim.head(f.ref)).size == 4096
    assert refusal(db, f.file_id)["why"] == "no_replacement"


def test_a_refused_delete_ends_and_never_makes_the_bad_copy_primary(db, two) -> None:  # type: ignore[no-untyped-def]
    """Azure's worst case (review on #35): no CRC64 and no MD5 in any
    properties, an original that was never read back, and a copy one byte
    short. One refusal; the bad copy is demoted and deleted without a check
    of its own (its delete must not refuse in turn and swap them back)."""
    f, copy = archived(db, two, "pingpong.segy")
    db.execute(
        "UPDATE catalog.location SET verified = false, verified_at = NULL, verified_by = NULL"
        " WHERE location_id = %s",
        (f.location_id,),
    )

    class AzureLike(Provider):
        async def head(self, ref: ObjectRef) -> ObjectHead:
            h = dataclasses.replace(await self._sim.head(ref), crc64nvme=None, md5=None)
            if ref.container.startswith("archive/"):
                h = dataclasses.replace(h, size=h.size - 1)
            return h

    registry = BackendRegistry.from_mapping({two.backend_id: AzureLike(two.sim)})
    outcomes: list[str] = []
    primaries: list[int] = []

    async def go() -> None:
        conn = await psycopg.AsyncConnection.connect(as_app(two.url, "worker"), autocommit=True)
        try:
            for _ in range(6):  # more than enough for a loop to show
                cur = await conn.execute(
                    "SELECT msg_id, message FROM pgmq.read('retention'::text, 0, 1)"
                )
                r = await cur.fetchone()
                if r is None:
                    return
                async with conn.transaction():
                    lid = int(r[1]["location_id"])
                    outcomes.append(await delete_blob(conn, registry, lid, two.clock.now()))
                    await conn.execute("SELECT pgmq.delete('retention'::text, %s::bigint)", (r[0],))
                primaries.append(primary(db, f.file_id)["location_id"])
        finally:
            await conn.close()

    run(go())
    assert outcomes == ["refused_replacement_size", "deleted"]
    assert set(primaries) == {f.location_id}  # the short copy was never the record again
    refusals = row(
        db,
        "SELECT count(*) AS n FROM catalog.audit_log WHERE file_id = %s"
        " AND action = 'location.delete.refused'",
        f.file_id,
    )
    assert refusals == {"n": 1}
    gone = row(db, "SELECT role, state FROM catalog.location WHERE location_id = %s",
               copy["location_id"])  # fmt: skip
    assert gone == {"role": "secondary", "state": "deleted"}
    assert run(two.sim.head(f.ref)).size == 4096


# --- no delete of an original skips the check -------------------------------------


def test_sims_own_archive_move_keeps_an_original_it_cannot_check() -> None:
    """sim plays Azure: its archive move (set_class on the live account)
    deletes the original only when the copy's properties prove it."""
    sim = SimBackend.azure_two_account(VirtualClock())
    ref = sim.put_synthetic("data", "big.segy", 5 << 30)  # no Content-MD5
    with pytest.raises(BackendError, match="unverified"):
        run(sim.set_class(ref, LogicalTier.ARCHIVE))
    assert run(sim.head(ref)).size == 5 << 30
    assert run(sim.list("archive/data", "", None)).items == []


SRC = Path(__file__).resolve().parents[2] / "src" / "omnifile_service"
FIXTURES = Path(__file__).resolve().parent / "scan_fixtures"


def test_every_copy_then_delete_acts_on_the_check() -> None:
    """A function that copies an object and deletes one (itself, or through
    the module's helpers it calls) is an archive move: it must act on
    `copy_mismatch`'s answer before the delete. Taking the check out of
    Azure's or sim's move fails this test, and so does a new unchecked one."""
    found = delete_scan.moves(SRC)
    assert {k for k, ok in found.items() if not ok} == set(), found
    assert found[("backends/azure/backend.py", "_archive_move")]
    assert found[("backends/sim/backend.py", "_azure_archive_move")]


def test_the_move_scan_catches_each_way_round_the_check() -> None:
    """Review on #35: a move split across helpers, one by Put Block From URL
    or S3 UploadPartCopy, and one that ignores the check's answer (or keeps
    it and never reads it) are all caught; a move that acts on it, itself or
    in a helper, is not."""
    found = delete_scan.moves(FIXTURES / "moves")
    assert {name: ok for (_, name), ok in found.items()} == {
        "split_across_helpers": False,
        "by_staged_blocks": False,
        "by_s3_part_copy": False,
        "check_ignored": False,
        "check_assigned_never_read": False,
        "checked_properly": True,
        "checked_in_a_helper": True,
    }


def test_only_known_paths_delete_through_a_backend() -> None:
    """Outside the backends, a backend's `delete` (on any receiver: a name,
    an attribute, a call's result) is called only by `delete_blob`, which
    checks a live version's replacement first, and by paths that never
    delete an original of a live version. A new caller fails this test
    until it is looked at."""
    callers = delete_scan.deleters(SRC)
    assert ("retention/worm_sync.py", "delete_blob") in callers
    assert callers == set(ALLOWED_DELETERS), sorted(callers)


def test_the_delete_scan_sees_every_kind_of_receiver() -> None:
    """Review on #35: `registry.get(bid).delete(ref)` and
    `self._backend.delete(ref)` were missed when only `name.delete` counted."""
    found = {name for _, name in delete_scan.deleters(FIXTURES / "deleters", skip="")}
    assert found == {"by_name", "by_registry_call", "by_attribute", "by_subscript"}


#: (module, function) that may call a backend's delete outside the backends,
#: and why it never deletes the original of a live version.
ALLOWED_DELETERS = {
    ("retention/worm_sync.py", "delete_blob"): "checks a live version's replacement first",
    ("exports.py", "expire"): "an expired snapshot export's parts",
    ("migrate/staging.py", "_delete_all"): "staging copies under omnifile-staging/",
    ("migrate/engine.py", "verify_copy"): "a migration copy that failed its check",
}
