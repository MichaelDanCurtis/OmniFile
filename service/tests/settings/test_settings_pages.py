"""Settings' other pages (portal-api.md 5.20): AWS, identity, Teams, email,
Storage Mover, alerts and people, and the test sends the worker runs. Fakes
only: no Azure, AWS, Graph or Teams."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from omnifile_service.catalog.privileges import as_app
from omnifile_service.settings.fakes import FakeProbes
from omnifile_service.settings.ports import ProbeError
from omnifile_service.settings.worker_jobs import SettingsTests
from omnifile_service.worker.queue import Message

from .conftest import TENANT, seed
from .test_storage import ACCT, AUDIENCE, AWS

ROLE = f"arn:aws:iam::{ACCT}:role/omnifile-service"
WORKER_OID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def admin(web, sql):
    w = seed(sql)
    return w, web.browser(w.admin)


# --- the worker side, with fakes ----------------------------------------------------


@dataclass
class FakeGraph:
    sent: list[tuple] = field(default_factory=list)

    async def send_mail(self, sender, to, subject, body) -> None:
        self.sent.append((sender, to, subject))


@dataclass
class FakeBot:
    sent: list[tuple] = field(default_factory=list)

    async def send(self, service_url, conversation_id, card, summary) -> str:
        self.sent.append((conversation_id, summary))
        return "activity-1"


@dataclass
class FakeConfig:
    sender: str | None = "omnifile@contoso.test"


@dataclass
class FakeNotifier:
    config: FakeConfig = field(default_factory=FakeConfig)
    graph: FakeGraph = field(default_factory=FakeGraph)
    bot: FakeBot = field(default_factory=FakeBot)
    blocked: bool = False

    async def _conversation(self, conn, identity_id, oid, now):
        if self.blocked:
            return None
        return {"service_url": "https://smba.test/", "conversation_id": f"chat-{oid}"}


def run_worker(db_url: str, test_id: str, tests: SettingsTests) -> None:
    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(as_app(db_url, "worker")) as conn:
            async with conn.transaction():
                now = datetime.now(UTC)
                msg = Message(1, 1, now, now, {"test_id": test_id})
                await tests.handle(msg, conn, now)

    asyncio.run(go())


def _poll(b, test_id: str) -> dict[str, Any]:
    r = b.get("/v1/settings/tests/{test_id}", test_id=test_id)
    assert r.status_code == 200, r.text
    return r.json()


# --- AWS ------------------------------------------------------------------------------


def test_aws_trust_policy_federates_the_service_identity(web, sql, admin) -> None:
    _w, b = admin
    web.services.settings_env = {**AWS, "OMNIFILE_AWS_WEB_IDENTITY_SUBJECTS": WORKER_OID}
    s = b.get("/v1/settings/aws").json()
    assert s["configured"] and s["audience"] == AUDIENCE and s["subjects"] == [WORKER_OID]
    assert s["issuer"] == f"https://sts.windows.net/{TENANT}/"
    assert s["accounts"][0]["role_arn"] == ROLE
    r = b.get("/v1/settings/aws/trust-policy", params={"role_arn": ROLE})
    assert r.status_code == 200, r.text
    t = r.json()
    st = t["trust_policy"]["Statement"][0]
    host = f"sts.windows.net/{TENANT}/"
    assert st["Principal"] == {"Federated": f"arn:aws:iam::{ACCT}:oidc-provider/{host}"}
    assert st["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert st["Condition"]["StringEquals"] == {f"{host}:aud": AUDIENCE, f"{host}:sub": WORKER_OID}
    assert t["configured_account"] is True and "AccessKey" not in json.dumps(t)
    r = b.get(
        "/v1/settings/aws/trust-policy", params={"role_arn": "arn:aws:iam::1:user/x"}, check=False
    )
    assert r.status_code == 400


def test_the_aws_test_runs_in_the_worker_and_records_no_credentials(web, sql, db_url, admin):
    _w, b = admin
    r = b.post("/v1/settings/aws/test", {"role_arn": ROLE})
    assert r.status_code == 202 and r.json()["state"] == "pending", r.text
    tid = r.json()["test_id"]
    assumed: list[tuple] = []

    async def assume(arn: str, account: str) -> dict[str, Any]:
        assumed.append((arn, account))
        return {"expires_at": "2026-11-04T10:00:00Z"}

    run_worker(db_url, tid, SettingsTests(None, assume))
    t = _poll(b, tid)
    assert t["state"] == "passed" and t["result"]["assumed_role_arn"] == ROLE
    assert assumed == [(ROLE, ACCT)] and "secret" not in json.dumps(t).lower()

    # A refusal is the test's answer.
    tid = b.post("/v1/settings/aws/test", {"role_arn": ROLE}).json()["test_id"]

    async def refuse(arn: str, account: str) -> dict[str, Any]:
        raise RuntimeError("AccessDenied: Not authorized to perform sts:AssumeRoleWithWebIdentity")

    run_worker(db_url, tid, SettingsTests(None, refuse))
    t = _poll(b, tid)
    assert t["state"] == "failed" and "AccessDenied" in t["detail"]
    # The S3 check reads the latest test of the account's role.
    web.services.settings_env = dict(AWS)
    bid = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network) VALUES"
        " (%s, 's3', %s, true) RETURNING backend_id",
        (
            f"s3t-{tid}",
            json.dumps({"account_id": ACCT, "region": "eu-west-1", "buckets": ["seismic-raw"]}),
        ),
    ).fetchone()[0]
    checks = b.get("/v1/settings/storage/{backend_id}/checks", backend_id=bid).json()["checks"]
    assert {c["code"]: c["status"] for c in checks}["assume_role"] == "failed"


def test_someone_elses_test_is_not_found(web, sql, admin) -> None:
    w, b = admin
    tid = b.post("/v1/settings/aws/test", {"role_arn": ROLE}).json()["test_id"]
    other = web.browser(w.admin2)
    r = other.get("/v1/settings/tests/{test_id}", test_id=tid)
    assert r.status_code == 404


# --- identity ---------------------------------------------------------------------------


def test_identity_from_sign_ins_and_from_graph(web, sql, admin) -> None:
    w, b = admin
    r = b.get("/v1/settings/identity")
    assert r.status_code == 200, r.text
    s = r.json()
    roles = {x["role"]: x for x in s["roles"]}
    assert (
        roles["ArchiveAdmin"]["people_seen"] >= 1 and roles["ArchiveAdmin"]["source"] == "sign_ins"
    )
    assert s["consent"]["status"] == "granted"
    assert s["consent"]["url"].startswith(
        f"https://login.microsoftonline.com/{TENANT}/adminconsent"
    )
    assert s["graph"]["status"] == "not_configured"

    client = web.services.settings.client_id
    graph = {
        f"/servicePrincipals(appId='{client}')?$select=id,appRoles": {
            "id": "sp-1",
            "appRoles": [{"id": "r-admin", "value": "ArchiveAdmin"}],
        },
        "/servicePrincipals/sp-1/appRoleAssignedTo?$top=999": {
            "value": [
                {
                    "appRoleId": "r-admin",
                    "principalType": "Group",
                    "principalId": "g-1",
                    "principalDisplayName": f"Owners {w.share_name[6:]}",
                },
                {
                    "appRoleId": "r-admin",
                    "principalType": "User",
                    "principalId": "u-1",
                    "principalDisplayName": "Ann Admin",
                },
                {
                    "appRoleId": "r-admin",
                    "principalType": "ServicePrincipal",
                    "principalId": "sp-x",
                    "principalDisplayName": "Some app",
                },
            ]
        },
        "/oauth2PermissionGrants?$filter=clientId eq 'sp-1' and consentType eq 'AllPrincipals'": {
            "value": [{"scope": "openid profile"}]
        },
    }
    web.services.probes = FakeProbes(graph=graph)
    s = b.get("/v1/settings/identity").json()
    admin_groups = {x["role"]: x for x in s["roles"]}["ArchiveAdmin"]["groups"]
    assert admin_groups == [
        {"object_id": "g-1", "display_name": f"Owners {w.share_name[6:]}", "sid": w.g_owners}
    ]
    # A person named for the role is listed apart from the groups (decision 89); an
    # application holding a person's role is neither.
    admin_role = {x["role"]: x for x in s["roles"]}["ArchiveAdmin"]
    assert admin_role["people"] == [{"object_id": "u-1", "display_name": "Ann Admin"}]
    assert {x["role"]: x for x in s["roles"]}["User"]["people"] == []
    assert s["role_holders"] == "groups"
    assert s["consent"]["status"] == "granted" and s["graph"]["status"] == "ok"
    web.services.settings.role_holders = "people"
    assert b.get("/v1/settings/identity").json()["role_holders"] == "people"
    web.services.settings.role_holders = "groups"

    # Without Application.Read.All: a warning, and the sign-in view.
    web.services.probes = FakeProbes(
        graph={
            f"/servicePrincipals(appId='{client}')?$select=id,appRoles": ProbeError(
                "answered 403", 403
            )
        }
    )
    s = b.get("/v1/settings/identity").json()
    assert s["graph"]["status"] == "warning" and "Application.Read.All" in s["graph"]["fix"]


def test_whoami(web, sql, admin) -> None:
    w, b = admin
    r = b.get("/v1/settings/identity/whoami")
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["sid"] == w.admin["sid"] and me["roles"] == ["ArchiveAdmin"]
    assert me["session"]["recently_authenticated"] is True
    assert {c["code"] for c in me["checks"]} >= {"sid", "groups", "role", "reauth"}


# --- Teams and email ------------------------------------------------------------------


def test_teams_health_package_and_test_card(web, sql, db_url, admin, tmp_path) -> None:
    _w, b = admin
    t = b.get("/v1/settings/teams").json()
    assert t["configured"] is False and t["health"]["status"] == "not_configured"
    assert t["package"]["available"] is False and t["package"]["instructions"]
    r = b.get("/v1/settings/teams/package")
    assert r.status_code == 404
    zip_path = tmp_path / "omnifile-teams-app.zip"
    zip_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    web.services.settings_env = {
        "OMNIFILE_TENANT_ID": TENANT,
        "OMNIFILE_TEAMS_APP_ID": "t-app",
        "OMNIFILE_TEAMS_BOT_APP_ID": "bot",
        "OMNIFILE_TEAMS_APP_PACKAGE": str(zip_path),
    }
    t = b.get("/v1/settings/teams").json()
    assert t["configured"] and t["package"] == {
        "available": True,
        "file_name": zip_path.name,
        "instructions": [],
    }
    r = b.get("/v1/settings/teams/package", check=False)
    assert r.status_code == 200 and r.content.startswith(b"PK")
    assert r.headers["content-type"] == "application/zip"

    r = b.post("/v1/settings/teams/test-card", {})
    assert r.status_code == 202, r.text
    n = FakeNotifier()
    run_worker(db_url, r.json()["test_id"], SettingsTests(n, None))  # type: ignore[arg-type]
    done = _poll(b, r.json()["test_id"])
    assert done["state"] == "passed", done
    assert len(n.bot.sent) == 1 and n.bot.sent[0][0].startswith("chat-")
    # A tenant that blocked the app: failed, with why.
    r = b.post("/v1/settings/teams/test-card", {})
    run_worker(db_url, r.json()["test_id"], SettingsTests(FakeNotifier(blocked=True), None))  # type: ignore[arg-type]
    assert "blocked" in _poll(b, r.json()["test_id"])["detail"]
    # No notifier on the worker.
    r = b.post("/v1/settings/teams/test-card", {})
    run_worker(db_url, r.json()["test_id"], SettingsTests(None, None))  # type: ignore[arg-type]
    assert "aren't configured on the worker" in _poll(b, r.json()["test_id"])["detail"]


def test_email_settings_and_test_email(web, sql, db_url, admin) -> None:
    w, b = admin
    e = b.get("/v1/settings/email").json()
    assert e["configured"] is False and e["instructions"]
    web.services.settings_env = {"OMNIFILE_NOTIFY_SENDER": "omnifile@contoso.test"}
    sql.execute(
        "UPDATE catalog.identity SET mail = 'ada@contoso.test' WHERE sid = %s", (w.admin["sid"],)
    )
    e = b.get("/v1/settings/email").json()
    assert e["sender"] == "omnifile@contoso.test" and e["my_mail"] == "ada@contoso.test"
    r = b.post("/v1/settings/email/test", {})
    assert r.status_code == 202
    n = FakeNotifier()
    run_worker(db_url, r.json()["test_id"], SettingsTests(n, None))  # type: ignore[arg-type]
    assert _poll(b, r.json()["test_id"])["state"] == "passed"
    assert n.graph.sent == [("omnifile@contoso.test", "ada@contoso.test", "OmniFile test email")]
    with psycopg.connect(db_url, row_factory=dict_row) as c:
        a = c.execute(
            "SELECT actor, decision FROM catalog.audit_log WHERE action = 'settings.test'"
            " ORDER BY audit_id DESC LIMIT 1"
        ).fetchone()
    assert a == {"actor": "system:settings", "decision": "allow"}


# --- Storage Mover, alerts, people ------------------------------------------------------


def test_storage_mover_read_only(web, sql, admin) -> None:
    _w, b = admin
    m = b.get("/v1/settings/storage-mover").json()
    assert m["configured"] is False and "OMNIFILE_STORAGE_MOVER" in m["error"]
    cfg = {
        "subscription_id": "sub",
        "resource_group": "rg",
        "storage_mover": "sm",
        "project": "omnifile",
        "source_endpoints": {"seismic-raw": "s3-src"},
        "target_endpoints": {"live/data": "blob-tgt"},
    }
    base = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.StorageMover/storageMovers/sm"
    web.services.settings_env = {"OMNIFILE_STORAGE_MOVER": json.dumps(cfg)}
    web.services.probes = FakeProbes(
        arm={
            base: {"properties": {"provisioningState": "Succeeded"}},
            f"{base}/agents": {
                "value": [
                    {
                        "name": "agent-1",
                        "properties": {
                            "agentStatus": "Online",
                            "lastStatusUpdate": "2026-11-03T10:00:00Z",
                        },
                    }
                ]
            },
            f"{base}/projects": {
                "value": [
                    {"name": "omnifile", "properties": {"provisioningState": "Succeeded"}},
                    {"name": "other", "properties": {}},
                ]
            },
        }
    )
    m = b.get("/v1/settings/storage-mover").json()
    assert m["error"] is None and m["storage_mover"]["provisioning_state"] == "Succeeded"
    assert m["agents"][0]["status"] == "Online"
    assert [(p["name"], p["in_use"]) for p in m["projects"]] == [
        ("omnifile", True),
        ("other", False),
    ]
    assert m["endpoints"]["source"] == {"seismic-raw": "s3-src"}
    # Only GETs: the page never changes Storage Mover.
    assert all(c.startswith("arm ") for c in web.services.probes.calls)


def test_alerts_are_read_only_status(web, sql, admin) -> None:
    w, b = admin
    import os

    sql.execute(
        "UPDATE catalog.edge SET state = 'active', enrolled_at = now(), cert_sha256 = %s,"
        " cert_not_after = now() + interval '30 days',"
        " last_heartbeat_at = now() - interval '1 hour' WHERE edge_id = %s",
        (os.urandom(32), w.edge_id),
    )
    a = b.get("/v1/settings/alerts").json()
    by = {x["name"]: x["status"] for x in a["alerts"]}
    assert by["edge-silent"] == "firing" and by["queue-backlog"] == "unknown"
    assert "app-metrics.bicep" in a["where"]
    assert by["tier-held-back"] == "ok"
    sql.execute("SELECT set_config('omnifile.actor', 'system:test', false)")
    sql.execute(
        "SELECT catalog.audit('tier.move.refused', NULL, NULL, NULL, NULL, %s::jsonb)",
        ('{"to": "hot", "why": "held_back_shared_blob"}',),
    )
    a = b.get("/v1/settings/alerts").json()
    assert {x["name"]: x["status"] for x in a["alerts"]}["tier-held-back"] == "firing"
    assert {x["name"]: x["status"] for x in a["alerts"]}["tier-archive-late"] == "ok"
    sql.execute(
        "SELECT catalog.audit('tier.archive.late', NULL, NULL, NULL, NULL, %s::jsonb)",
        ('{"to": "archive", "archive_at": "2026-11-01"}',),
    )
    a = b.get("/v1/settings/alerts").json()
    assert {x["name"]: x["status"] for x in a["alerts"]}["tier-archive-late"] == "firing"
    assert {x["name"]: x["status"] for x in a["alerts"]}["tier-forced-archive"] == "ok"
    sql.execute(
        "SELECT catalog.audit('tier.move', NULL, NULL, NULL, NULL, %s::jsonb)",
        ('{"to": "archive", "how": "shared_blob", "forced": "another_files_archive_date"}',),
    )
    a = b.get("/v1/settings/alerts").json()
    assert {x["name"]: x["status"] for x in a["alerts"]}["tier-forced-archive"] == "firing"
    held = next(x for x in a["alerts"] if x["name"] == "tier-held-back")
    assert "write rights" in held["detail"] and "daily" in held["detail"]
    assert {x["name"]: x["status"] for x in a["alerts"]}["tier-archive-overdue"] == "ok"
    (root,) = sql.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL",
        (w.dataset_id,),
    ).fetchone()
    sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size, resident_bytes,"
        " next_action, next_action_at, archive_at) VALUES (%s, %s, 'overdue.segy', 'file',"
        " 'stub', 1, 0, 'archive', now() - interval '3 days', current_date - 3)",
        (w.dataset_id, root),
    )
    a = b.get("/v1/settings/alerts").json()
    # Not under a tiering policy, nothing will archive it, so it isn't overdue.
    assert {x["name"]: x["status"] for x in a["alerts"]}["tier-archive-overdue"] == "ok"
    sql.execute(
        "INSERT INTO catalog.policy (dataset_id, version, body, approved_by_identity_id)"
        ' SELECT %s, 1, \'{"mode": "scheduled", "archive_day": 180}\', identity_id'
        " FROM catalog.identity ORDER BY identity_id LIMIT 1 ON CONFLICT DO NOTHING",
        (w.dataset_id,),
    )
    a = b.get("/v1/settings/alerts").json()
    assert {x["name"]: x["status"] for x in a["alerts"]}["tier-archive-overdue"] == "firing"


def test_people_search(web, sql, admin) -> None:
    w, b = admin
    r = b.get("/v1/settings/people", params={"q": "adm"})
    assert r.status_code == 200
    hit = [p for p in r.json()["items"] if p["upn"] is None or True]
    assert any(p["display_name"] == "Admin" and "ArchiveAdmin" in p["roles"] for p in hit)
    r = b.get("/v1/settings/people", params={"q": "a"}, check=False)
    assert r.status_code == 400
    # A wildcard is taken literally.
    assert b.get("/v1/settings/people", params={"q": "%%"}).json()["items"] == []
