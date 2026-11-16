"""The catalog restore drill (`deploy/azure/README.md`, "Catalog backup and
restore"; M6 "catalog backup and restore").

Skipped unless ``OMNIFILE_RESTORE_DRILL=1``: it needs ``pg_dump`` and
``pg_restore`` of the server's own major version. `deploy/azure/restore-drill.sh`
starts a throwaway Postgres 17 container, sets the variables and runs it:

- ``OMNIFILE_RESTORE_DRILL_EXEC``: a command prefix that runs the two tools
  (``docker exec -i <container>``); empty means the local binaries.
- ``OMNIFILE_RESTORE_DRILL_SERVER``: the server as those tools reach it
  (inside the container, ``postgresql://postgres@localhost:5432/postgres``);
  default `OMNIFILE_TEST_DATABASE_URL`.

What it does, in the runbook's order: seed a catalog through the api (an
edge, a dataset, files and a folder), back it up with ``pg_dump``, make one
more change (the timeline the restore loses), restore into a new database
with ``pg_restore``, run the restore step (``catalog.feed_restored()``), and
serve the api on the restored database. Then the service's half of every
edge's re-snapshot: the edge's cursor from before gets ``reset_required``, a
snapshot carries the new incarnation and the restored rows (and not the lost
one), ids move on past anything the lost timeline used, and the feed reads on
from the snapshot. The edge's half (re-snapshot in place) is
`omnifile-replica`'s ``a_resnapshot_after_a_restore_applies_wound_back_rows_and_finds_lost_ones``
and `omnifile-edge`'s ``a_restored_catalog_is_resnapshotted_in_place``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import uuid

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .conftest import ENV, E, EdgeClient, World, create_file, serve_api
from .test_feed_and_changes import drain, page

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIFILE_RESTORE_DRILL") != "1",
    reason="the restore drill runs from deploy/azure/restore-drill.sh (OMNIFILE_RESTORE_DRILL=1)",
)


def _tool(name: str, dbname: str) -> list[str]:
    """argv for pg_dump / pg_restore on `dbname`, through the exec prefix."""
    server = os.environ.get("OMNIFILE_RESTORE_DRILL_SERVER") or os.environ[ENV]
    params = conninfo_to_dict(server)
    params["dbname"] = dbname
    prefix = shlex.split(os.environ.get("OMNIFILE_RESTORE_DRILL_EXEC", ""))
    return [*prefix, name, "--dbname", make_conninfo(**params)]


def _snapshot(edge: EdgeClient, dataset_id: int) -> tuple[str, dict[str, str]]:
    """A full snapshot of the dataset: its as_of, and file id -> name."""
    out: dict[str, str] = {}
    after = None
    as_of = ""
    while True:
        params = {"dataset_id": str(dataset_id), "limit": 1000}
        if after:
            params["after_file_id"] = after
        resp = edge.get(f"{E}/snapshot", params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        as_of = body.get("as_of", as_of)
        for f in body["files"]:
            out[f["file_id"]] = f["name"]
        if not body["more"]:
            return as_of, out
        after = body["files"][-1]["file_id"]


def test_a_catalog_restored_from_pg_dump_makes_every_edge_resnapshot(
    make_world, sql, db_url: str, ca, facade
) -> None:
    w: World = make_world()
    a = create_file(w, "a.segy")
    folder = create_file(w, "line_1042", kind="dir")
    b = create_file(w, "b.segy", parent=folder)
    _, before = drain(w)
    ack = w.edge.post(f"{E}/feed/ack", {"cursor": before})
    assert ack.status_code == 204, ack.text

    # The backup.
    src = conninfo_to_dict(db_url)["dbname"]
    dump = subprocess.run(
        [*_tool("pg_dump", src), "--format=custom", "--no-password"],
        check=True,
        capture_output=True,
    ).stdout
    assert dump[:5] == b"PGDMP", dump[:40]

    # The timeline the restore loses: a file the edge already knows by id.
    lost = create_file(w, "after-the-backup.segy")

    # Restore into a new database (Azure always restores to a new server).
    restored = f"{src}_restored_{uuid.uuid4().hex[:6]}"
    sql.execute(f'CREATE DATABASE "{restored}"')
    try:
        res = subprocess.run(
            [*_tool("pg_restore", restored), "--exit-on-error", "--no-password"],
            input=dump,
            capture_output=True,
        )
        assert res.returncode == 0, res.stderr.decode()
        params = conninfo_to_dict(db_url)
        params["dbname"] = restored
        restored_url = make_conninfo(**params)

        with (
            psycopg.connect(restored_url, autocommit=True) as rsql,
            serve_api(restored_url, ca, facade, w.backend_id) as served,
        ):
            # The same edge, its certificate known to the restored catalog.
            edge = EdgeClient(served.client, w.edge.edge_id, w.edge.key, w.edge.cert_pem)
            rw = World(edge, w.dataset_id, w.root_id, w.share_id, w.backend_id)

            # Without the restore step nothing tells the edge: its cursor reads on.
            # (On Azure the restored server also hands out transaction ids again,
            # which is how events would be skipped silently.)
            assert page(rw, before)["reset_required"] is False

            # Step 3 of the runbook, as the administrator, before anything else connects.
            (inc,) = rsql.execute("SELECT catalog.feed_restored()").fetchone()
            assert inc == 1

            # Every edge whose cursor is from before is told to re-snapshot ...
            assert page(rw, before) == {"events": [], "next": before, "reset_required": True}
            # ... and the snapshot it takes is the restored catalog's, in the new incarnation.
            as_of, files = _snapshot(edge, w.dataset_id)
            assert as_of.endswith(".1"), as_of
            assert files == {
                str(w.root_id): "",
                str(a): "a.segy",
                str(folder): "line_1042",
                str(b): "b.segy",
            }, files
            assert str(lost) not in files

            # A new file never reuses an id the lost timeline handed out.
            new = create_file(rw, "after-the-restore.segy")
            assert new > lost + (1 << 40)

            # The feed reads on from the snapshot's cursor, in the new incarnation.
            events, nxt = drain(rw, as_of)
            assert str(new) in [e["entity_id"] for e in events]
            assert nxt.endswith(".1") and page(rw, nxt)["reset_required"] is False
            ack = edge.post(f"{E}/feed/ack", {"cursor": nxt})
            assert ack.status_code == 204, ack.text

        # The source catalog is untouched by the drill.
        (src_inc,) = sql.execute("SELECT incarnation FROM catalog.feed_retention").fetchone()
        assert src_inc == 0
    finally:
        sql.execute(f'DROP DATABASE IF EXISTS "{restored}" WITH (FORCE)')
