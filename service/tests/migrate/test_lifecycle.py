"""The partner lifecycle in the engine (migration 0029; portal-api.md 5.17,
5.18): manual cutover, exclusions, cancel, the cutover report and the
typed lifecycle events."""

from __future__ import annotations

from datetime import UTC, datetime

from psycopg.rows import dict_row
from sources.support import BUCKET, put

from .test_engine import Rig, run


def _events(db, mid: int) -> list[dict]:  # type: ignore[no-untyped-def]
    cur = db.cursor(row_factory=dict_row)
    cur.execute(
        "SELECT type, data FROM catalog.partner_event WHERE entity = 'migration'"
        " AND entity_id = %s ORDER BY txid::text::numeric, event_id",
        (mid,),
    )
    return cur.fetchall()


def _edge_delete(db, r: Rig, name: str) -> int:  # type: ignore[no-untyped-def]
    """A user's delete through the edge: the file is tombstoned with the edge
    as its origin (api/changes.py), and nothing reaches S3."""
    fid, edge = db.execute(
        "SELECT f.file_id, d.edge_id FROM catalog.file f JOIN catalog.dataset d USING (dataset_id)"
        " WHERE f.dataset_id = %s AND f.name = %s AND f.deleted_at IS NULL",
        (r.w.dataset_id, name),
    ).fetchone()
    db.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (fid,))
    db.execute(
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
        " committed_at, size, edge_id, edge_op_id)"
        " SELECT file_id, dataset_id, parent_id, name, 'tombstone', 'committed', now(), size,"
        " %s, %s FROM catalog.file WHERE file_id = %s",
        (edge, f"op-{fid}", fid),
    )
    return fid


def test_a_manual_migration_waits_at_verified_for_its_cutover(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic" * 100))
    run(put(r.w, "m/b.segy", b"more"))
    r.seed()
    mid = r.request(cutover="manual")
    assert r.drive(mid, until="verified") == "verified"
    # Held: more steps flip nothing.
    assert r.step(mid) == "verified" and r.step(mid) == "verified"
    assert r.primary("a.segy")["backend_id"] == r.w.s3_id
    db.execute(
        "UPDATE catalog.migration_prefix SET cutover_requested_at = now() WHERE migration_id = %s",
        (mid,),
    )
    assert r.drive(mid) == "cut_over"
    assert r.primary("a.segy")["backend_id"] == r.w.home_id
    types = [e["type"] for e in _events(db, mid)]
    assert types[0] == "migration.started" and types[-1] == "migration.cut_over"
    assert "migration.verified" in types
    started = _events(db, mid)[0]["data"]
    assert started == {"migration_id": str(mid), "prefix": "m/",
                       "from_backend_id": str(r.w.s3_id), "to_backend_id": str(r.w.home_id),
                       "mode": "serve", "state": "pending"}  # fmt: skip
    assert _events(db, mid)[-1]["data"]["source_deletes_pending"] == 0


def test_a_deleted_file_is_reported_never_applied(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"stays"))
    run(put(r.w, "m/gone.segy", b"deleted by a user"))
    r.seed()
    mid = r.request()
    _edge_delete(db, r, "gone.segy")
    assert r.drive(mid) == "cut_over"
    cut = _events(db, mid)[-1]
    assert cut["type"] == "migration.cut_over" and cut["data"]["source_deletes_pending"] == 1
    cur = db.cursor(row_factory=dict_row)
    cur.execute("SELECT * FROM catalog.migration_source_deletes(%s)", (mid,))
    (row,) = cur.fetchall()
    assert row["object_key"] == "m/gone.segy" and row["edge_id"] is not None
    # The object is still in the bucket: OmniFile deleted nothing in S3.
    assert "m/gone.segy" in run(_keys(r))


async def _keys(r: Rig) -> list[str]:
    page = await r.w.s3.list(BUCKET, "m/", None)
    return [h.ref.key for h in page.items]


def test_an_excluded_object_stays_on_the_source(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"copied"))
    run(put(r.w, "m/odd.segy", b"never verifiable"))
    r.seed()
    mid = r.request()
    db.execute(
        "INSERT INTO catalog.migration_exclusion (migration_id, object_key, reason, excluded_by)"
        " VALUES (%s, 'm/odd.segy', 'SSE-C', 'user:1')",
        (mid,),
    )
    assert r.drive(mid) == "cut_over"
    m = r.mig(mid)
    assert (m["files_total"], m["files_verified"]) == (1, 1)
    assert r.primary("a.segy")["backend_id"] == r.w.home_id
    assert r.primary("odd.segy")["backend_id"] == r.w.s3_id
    # A live file still comes from S3, so the dataset isn't cut over.
    assert r.dataset_state() == "migrating"


def test_a_cancelled_migration_stops_and_its_prefix_can_start_again(db, connect) -> None:  # type: ignore[no-untyped-def]
    r = Rig(db, connect)
    run(put(r.w, "m/a.segy", b"seismic"))
    r.seed()
    mid = r.request()
    r.step(mid)
    db.execute(
        "UPDATE catalog.migration_prefix SET state = 'cancelled', cancelled_at = %s"
        " WHERE migration_id = %s",
        (datetime.now(UTC), mid),
    )
    assert r.step(mid) == "cancelled" and r.step(mid) == "cancelled"
    assert r.primary("a.segy")["backend_id"] == r.w.s3_id
    assert _events(db, mid)[-1]["type"] == "migration.cancelled"
    again = r.request()
    assert again != mid
    assert r.drive(again) == "cut_over"
    assert r.primary("a.segy")["backend_id"] == r.w.home_id
