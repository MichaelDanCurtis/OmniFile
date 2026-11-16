"""Catalog rows for worker tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime

import psycopg

from omnifile_service.backends.base import ObjectRef
from omnifile_service.backends.sim import SimBackend


@dataclass
class World:
    backend_id: int
    dataset_id: int
    root_id: int
    edge_id: int


@dataclass
class StoredFile:
    file_id: int
    version_id: int
    location_id: int
    ref: ObjectRef


def seed(c: psycopg.Connection, *, policy: dict | None = None) -> World:
    tag = uuid.uuid4().hex[:8]
    (backend_id,) = c.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 'sim', '{\"layout\": \"azure_two_account\"}', true) RETURNING backend_id",
        (f"sim-{tag}",),
    ).fetchone()
    (edge_id,) = c.execute(
        "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
        " VALUES (%s, %s, now() + interval '1 day') RETURNING edge_id",
        (f"edge-{tag}", uuid.uuid4().bytes * 2),
    ).fetchone()
    (dataset_id,) = c.execute(
        "INSERT INTO catalog.dataset (name, backend_id, edge_id) VALUES (%s, %s, %s)"
        " RETURNING dataset_id",
        (f"ds-{tag}", backend_id, edge_id),
    ).fetchone()
    (root_id,) = c.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
        " VALUES (%s, NULL, '', 'dir') RETURNING file_id",
        (dataset_id,),
    ).fetchone()
    if policy is not None:
        (iid,) = c.execute(
            "INSERT INTO catalog.identity (sid) VALUES (%s) RETURNING identity_id",
            (f"S-1-5-21-{int(tag, 16) % 10**9}-500",),
        ).fetchone()
        c.execute(
            "INSERT INTO catalog.policy (dataset_id, version, body, approved_by_identity_id)"
            " VALUES (%s, 1, %s, %s)",
            (dataset_id, psycopg.types.json.Jsonb(policy), iid),
        )
    return World(backend_id, dataset_id, root_id, edge_id)


def add_dir(c: psycopg.Connection, w: World, parent: int, name: str) -> int:
    (fid,) = c.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
        " VALUES (%s, %s, %s, 'dir') RETURNING file_id",
        (w.dataset_id, parent, name),
    ).fetchone()
    return fid


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def store(
    c: psycopg.Connection,
    w: World,
    sim: SimBackend,
    name: str,
    data: bytes,
    committed_at: datetime,
    *,
    parent: int | None = None,
    catalog_size: int | None = None,
) -> StoredFile:
    """A file whose content version is uploaded to sim and verified."""
    ref = await sim.put("data", name, _one(data), None, {})
    size = len(data) if catalog_size is None else catalog_size
    with c.transaction():
        (fid,) = c.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
            " resident_bytes) VALUES (%s, %s, %s, 'file', 'resident', %s, %s) RETURNING file_id",
            (w.dataset_id, parent or w.root_id, name, size, size),
        ).fetchone()
        (vid,) = c.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, seq, parent_id, name, kind,"
            " state, committed_at, size) VALUES (%s, %s, 1, %s, %s, 'content', 'pending', NULL, %s)"
            " RETURNING version_id",
            (fid, w.dataset_id, parent or w.root_id, name, size),
        ).fetchone()
        (lid,) = c.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container, object_key,"
            " backend_version_id, role, state, logical_tier, storage_class, size, crc64nvme,"
            " verified, verified_at, verified_by, committed_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'primary', 'committed', 'smart', 'Smart', %s,"
            " %s, true, %s, 'upload_block_crc', %s) RETURNING location_id",
            (fid, vid, w.backend_id, ref.container, ref.key, ref.version_id, size,
             b"\0" * 8, committed_at, committed_at),
        ).fetchone()  # fmt: skip
        c.execute(
            "UPDATE catalog.file_version SET state = 'committed', committed_at = %s"
            " WHERE version_id = %s",
            (committed_at, vid),
        )
    return StoredFile(
        fid, vid, lid, ObjectRef(w.backend_id, ref.container, ref.key, ref.version_id)
    )


def file_row(c: psycopg.Connection, file_id: int) -> dict:
    cur = c.execute("SELECT * FROM catalog.file WHERE file_id = %s", (file_id,))
    names = [d.name for d in cur.description]
    return dict(zip(names, cur.fetchone(), strict=True))


def queue(c: psycopg.Connection, name: str) -> list[dict]:
    return [r[0] for r in c.execute(f"SELECT message FROM pgmq.q_{name} ORDER BY msg_id")]
