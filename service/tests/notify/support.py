"""Shared by tests/notify and tests/api/test_teams_intents.py: catalog rows for a Teams
scenario, the fakes wired to a `Notifier`, and the relay (imported from ../relay/src).

The scenario (AGENTS.md "What it does"): a user in AD group "Seismic" opens
`line_1042.segy`, archived, in /long-term-storage/dataset. The file's ACL lets "Seismic" read.
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omnifile_service.notify.delivery import Notifier
from omnifile_service.notify.fakes import FakeBotConnector, FakeGraph
from omnifile_service.notify.intents import IntentSigner, public_key_b64
from omnifile_service.worker.notify_jobs import notifier_from_env

TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
BOT_APP_ID = "11111111-2222-3333-4444-555555555555"
TEAMS_APP_ID = "22222222-3333-4444-5555-666666666666"
GRAPH_CLIENT = "33333333-4444-5555-6666-777777777777"
KID = "intent-test"
DOMAIN = "S-1-5-21-1004336348-1177238915-682003330"
PORTAL = "https://portal.omnifile.internal"
SENDER = "omnifile@contoso.example"
GRAPH_BASE = "http://graph.test"
BOT_BASE = "http://bot.test"

RELAY_SRC = Path(__file__).resolve().parents[3] / "relay" / "src"


def relay_modules():
    """The relay's real code (unchanged), from relay/src; skip if it isn't next to us."""
    if not (RELAY_SRC / "omnifile_relay" / "app.py").exists():
        pytest.skip("relay/src is not next to service/")
    if str(RELAY_SRC) not in sys.path:
        sys.path.insert(0, str(RELAY_SRC))
    from omnifile_relay import app, config, sink

    return app, config, sink


def new_sid(rid: int | None = None) -> str:
    return f"{DOMAIN}-{rid or (uuid.uuid4().int % 10**8 + 10_000)}"


def new_oid() -> str:
    return str(uuid.uuid4())


@dataclass
class Person:
    identity_id: int
    sid: str
    oid: str
    mail: str | None


@dataclass
class Scenario:
    backend_id: int
    edge_id: int
    dataset_id: int
    root_id: int
    dir_id: int
    share_id: int
    file_id: int
    version_id: int
    acl_id: int
    readers_sid: str
    owners_sid: str


def _one(c: psycopg.Connection, sql: str, params: Any = None) -> Any:
    row = c.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def seed_scenario(
    c: psycopg.Connection,
    *,
    edge_id: int | None = None,
    backend_id: int | None = None,
    size: int = 8 * 10**9,
    policy: dict | None = None,
) -> Scenario:
    tag = uuid.uuid4().hex[:8]
    if backend_id is None:
        backend_id = _one(
            c,
            "INSERT INTO catalog.backend (name, kind, config, private_network)"
            " VALUES (%s, 'sim', '{\"layout\": \"azure_two_account\"}', true)"
            " RETURNING backend_id",
            (f"sim-{tag}",),
        )
    if edge_id is None:
        edge_id = _one(
            c,
            "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
            " VALUES (%s, %s, now() + interval '1 day') RETURNING edge_id",
            (f"edge-{tag}", uuid.uuid4().bytes * 2),
        )
    dataset_id = _one(
        c,
        "INSERT INTO catalog.dataset (name, backend_id, edge_id) VALUES (%s, %s, %s)"
        " RETURNING dataset_id",
        (f"ds-{tag}", backend_id, edge_id),
    )
    root_id = _one(
        c,
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
        " VALUES (%s, NULL, '', 'dir') RETURNING file_id",
        (dataset_id,),
    )
    top = _one(
        c,
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
        " VALUES (%s, %s, 'long-term-storage', 'dir') RETURNING file_id",
        (dataset_id, root_id),
    )
    dir_id = _one(
        c,
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind)"
        " VALUES (%s, %s, 'dataset', 'dir') RETURNING file_id",
        (dataset_id, top),
    )
    readers, owners = new_sid(), new_sid()
    share_id = _one(
        c,
        "INSERT INTO catalog.share (dataset_id, edge_id, name, root_dir_id, protocols, nfs_sec,"
        " owners_group_sid, readers_group_sid, on_open)"
        " VALUES (%s, %s, %s, %s, ARRAY['nfs4','smb'], ARRAY['krb5'], %s, %s, 'wait')"
        " RETURNING share_id",
        (dataset_id, edge_id, f"seismic-{tag}", top, owners, readers),
    )
    entries = [{"scope": "access", "tag": "group", "sid": readers, "perms": "r-x"}]
    acl_id = _one(
        c,
        "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids, deny_sids)"
        " VALUES (%s, %s, 416, %s::jsonb, %s::catalog.sid[], '{}') RETURNING acl_id",
        (dataset_id, hashlib.sha256(tag.encode()).digest(), json.dumps(entries), [readers]),
    )
    now = datetime.now(UTC) - timedelta(days=200)
    file_id = _one(
        c,
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
        " resident_bytes, acl_id) VALUES (%s, %s, 'line_1042.segy', 'file', 'stub', %s, 0, %s)"
        " RETURNING file_id",
        (dataset_id, dir_id, size, acl_id),
    )
    with c.transaction():
        version_id = _one(
            c,
            "INSERT INTO catalog.file_version (file_id, dataset_id, seq, parent_id, name, kind,"
            " state, committed_at, size) VALUES (%s, %s, 1, %s, 'line_1042.segy', 'content',"
            " 'pending', NULL, %s) RETURNING version_id",
            (file_id, dataset_id, dir_id, size),
        )
        c.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
            " object_key, role, state, logical_tier, storage_class, size, committed_at)"
            " VALUES (%s, %s, %s, 'archive/data', %s, 'primary', 'committed', 'archive',"
            " 'Archive', %s, %s)",
            (file_id, version_id, backend_id, f"{tag}/line_1042.segy", size, now),
        )
        c.execute(
            "UPDATE catalog.file_version SET state = 'committed', committed_at = %s"
            " WHERE version_id = %s",
            (now, version_id),
        )
    if policy is not None:
        approver = add_person(c, groups=[])
        c.execute(
            "INSERT INTO catalog.policy (dataset_id, version, body, approved_by_identity_id)"
            " VALUES (%s, 1, %s::jsonb, %s)",
            (dataset_id, json.dumps(policy), approver.identity_id),
        )
    return Scenario(
        backend_id,
        edge_id,
        dataset_id,
        root_id,
        dir_id,
        share_id,
        file_id,
        version_id,
        acl_id,
        readers,
        owners,
    )


def add_person(
    c: psycopg.Connection,
    *,
    groups: list[str],
    resolved: bool = True,
    mail: str | None = "geo@contoso.example",
    oid: str | None = None,
) -> Person:
    sid, oid = new_sid(), oid or new_oid()
    iid = _one(
        c,
        "INSERT INTO catalog.identity (sid, kind, entra_object_id, mail, group_sids,"
        " groups_resolved_at, graph_state, graph_resolved_at)"
        " VALUES (%s, 'user', %s, %s, %s::catalog.sid[], %s, %s, %s) RETURNING identity_id",
        (
            sid,
            oid if resolved else None,
            mail,
            groups,
            datetime.now(UTC) if resolved else None,
            "resolved" if resolved else None,
            datetime.now(UTC) if resolved else None,
        ),
    )
    return Person(iid, sid, oid, mail)


def start_recall(
    c: psycopg.Connection, s: Scenario, who: Person | None, *, via: str = "smb"
) -> int:
    rid = _one(
        c,
        "INSERT INTO catalog.recall (file_id, version_id, edge_id) VALUES (%s, %s, %s)"
        " RETURNING recall_id",
        (s.file_id, s.version_id, s.edge_id),
    )
    if who is not None:
        c.execute(
            "INSERT INTO catalog.recall_requester (recall_id, identity_id, via, proof)"
            " VALUES (%s, %s, %s, 'proven')",
            (rid, who.identity_id, via),
        )
    return rid


def set_state(c: psycopg.Connection, recall_id: int, state: str) -> None:
    c.execute(
        "UPDATE catalog.recall SET state = %s,"
        " completed_at = CASE WHEN %s IN ('available', 'failed', 'cancelled') THEN now() END"
        " WHERE recall_id = %s",
        (state, state, recall_id),
    )


def audits(c: psycopg.Connection, action: str, file_id: int) -> list[dict]:
    cur = c.execute(
        "SELECT actor, decision, details FROM catalog.audit_log"
        " WHERE action = %s AND file_id = %s ORDER BY at, audit_id",
        (action, file_id),
    )
    return [{"actor": a, "decision": d, "details": det} for a, d, det in cur.fetchall()]


# --- keys and the fakes ---------------------------------------------------------------


@dataclass
class Fakes:
    graph: FakeGraph
    bot: FakeBotConnector
    http: httpx.AsyncClient
    key: Ed25519PrivateKey
    notifier: Notifier

    @property
    def public_keys_json(self) -> str:
        return json.dumps({KID: public_key_b64(self.key)})

    def signer(self) -> IntentSigner:
        return IntentSigner(self.key, KID)


def make_fakes(tmp: Path, *, sender: str | None = SENDER) -> Fakes:
    graph = FakeGraph(tenant=TENANT, clients={GRAPH_CLIENT: "graph-secret"})
    bot = FakeBotConnector(clients={BOT_APP_ID: "bot-secret"})
    http = httpx.AsyncClient(
        mounts={
            f"{GRAPH_BASE}/": httpx.ASGITransport(app=graph.app()),
            f"{BOT_BASE}/": httpx.ASGITransport(app=bot.app()),
        }
    )
    key = Ed25519PrivateKey.generate()
    key_path = tmp / "intent.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (tmp / "bot.secret").write_text("bot-secret\n")
    (tmp / "graph.secret").write_text("graph-secret\n")
    env = {
        "OMNIFILE_DEV_MODE": "1",
        "OMNIFILE_TENANT_ID": TENANT,
        "OMNIFILE_TEAMS_BOT_APP_ID": BOT_APP_ID,
        "OMNIFILE_TEAMS_APP_ID": TEAMS_APP_ID,
        "OMNIFILE_TEAMS_BOT_CREDENTIAL": str(tmp / "bot.secret"),
        "OMNIFILE_GRAPH_CREDENTIAL": str(tmp / "graph.secret"),
        "OMNIFILE_GRAPH_CLIENT_ID": GRAPH_CLIENT,
        "OMNIFILE_TEAMS_SERVICE_URL": f"{BOT_BASE}/",
        "OMNIFILE_TEAMS_INTENT_SIGNING_KEY": str(key_path),
        "OMNIFILE_TEAMS_INTENT_KID": KID,
        "OMNIFILE_PORTAL_URL": PORTAL,
        "OMNIFILE_GRAPH_URL": f"{GRAPH_BASE}/v1.0",
        "OMNIFILE_ENTRA_AUTHORITY": GRAPH_BASE,
        "OMNIFILE_BOTCONNECTOR_TOKEN_URL": f"{BOT_BASE}/token",
    }
    if sender:
        env["OMNIFILE_NOTIFY_SENDER"] = sender
    notifier = notifier_from_env(env, http=http)
    assert notifier is not None
    return Fakes(graph, bot, http, key, notifier)


def graph_user(fakes: Fakes, p: Person, *, groups: list[str] = ()) -> None:
    """Put `p` into the fake directory, with group objects for each SID in `groups`."""
    gids = []
    for sid in groups:
        gid = next(
            (
                g["id"]
                for g in fakes.graph.groups.values()
                if g["onPremisesSecurityIdentifier"] == sid
            ),
            None,
        )
        if gid is None:
            gid = new_oid()
            fakes.graph.add_group(gid, sid=sid, displayName=f"group {sid[-5:]}")
        gids.append(gid)
    fakes.graph.add_user(
        p.oid,
        sid=p.sid,
        displayName="Geo Scientist",
        userPrincipalName="geo@contoso",
        mail=p.mail,
        groups=gids,
    )
