"""Row builders shared by the catalog tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg


@dataclass
class World:
    """A minimal catalog: backend, dataset, root dir, edge, identities."""

    backend_id: int
    dataset_id: int
    root_id: int
    edge_id: int
    alice: int  # identity ids
    bob: int


def seed(c: psycopg.Connection, layout: str = "two_account") -> World:
    tag = uuid.uuid4().hex[:8]
    (backend_id,) = c.execute(
        "INSERT INTO catalog.backend (name, kind, private_network) VALUES (%s, 'azure_blob', true)"
        " RETURNING backend_id",
        (f"azure-{tag}",),
    ).fetchone()
    (edge_id,) = c.execute(
        "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
        " VALUES (%s, %s, now() + interval '1 day') RETURNING edge_id",
        (f"edge-{tag}", uuid.uuid4().bytes * 2),
    ).fetchone()
    (dataset_id,) = c.execute(
        "INSERT INTO catalog.dataset (name, backend_id, storage_layout, edge_id)"
        " VALUES (%s, %s, %s, %s) RETURNING dataset_id",
        (f"ds-{tag}", backend_id, layout, edge_id),
    ).fetchone()
    (root_id,) = c.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
        " VALUES (%s, NULL, '', 'dir') RETURNING file_id",
        (dataset_id,),
    ).fetchone()
    ids = []
    for rid in (1001, 1002):
        (iid,) = c.execute(
            "INSERT INTO catalog.identity (sid, posix_id) VALUES (%s, NULL) RETURNING identity_id",
            (f"S-1-5-21-{int(tag, 16) % 10**9}-{rid}",),
        ).fetchone()
        ids.append(iid)
    return World(backend_id, dataset_id, root_id, edge_id, ids[0], ids[1])


def add_file(c: psycopg.Connection, w: World, parent: int, name: str, kind: str = "file") -> int:
    (fid,) = c.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state)"
        " VALUES (%s, %s, %s, %s, 'resident') RETURNING file_id",
        (w.dataset_id, parent, name, kind),
    ).fetchone()
    return fid


def set_actor(c: psycopg.Connection, actor: str) -> None:
    c.execute("SELECT set_config('omnifile.actor', %s, true)", (actor,))
