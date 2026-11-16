"""Connect Azure, Connect AWS, Storage Mover deployment and source scans
(docs/design/cloud-connect.md). Fakes only: the dev issuer for the delegated
sign-in, `FakeArm` for the admin's ARM calls and the worker's, `FakeProbes`
for web's own reads, fake AWS clients and the in-memory S3 for scans. Nothing
reaches Azure or AWS."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import psycopg
import pytest
import yaml
from psycopg.rows import dict_row
from watched import Watched, drain_queue, look
from web.conftest import _send, dev_params

from omnifile_service.catalog.privileges import as_app
from omnifile_service.settings import aws as aws_settings
from omnifile_service.settings import aws_connect
from omnifile_service.settings import azure_connect as az
from omnifile_service.settings.arm import ArmError
from omnifile_service.settings.cloud_jobs import CloudTasks, deploy_with, fetch_with
from omnifile_service.settings.fakes import (
    DEMO_SUBSCRIPTION,
    FakeArm,
    FakeProbes,
    demo_arm_answers,
)
from omnifile_service.web.azure_connect import AZURE_COOKIE
from omnifile_service.worker.queue import Message

from .conftest import TENANT, seed
from .test_storage import ACCT, AUDIENCE, AWS

WEB_OID = "22222222-2222-4222-8222-222222222222"
WORKER_OID = "11111111-1111-4111-8111-111111111111"
IDS = {"OMNIFILE_SERVICE_PRINCIPAL_IDS": f"web={WEB_OID},worker={WORKER_OID}"}
SUB = f"/subscriptions/{DEMO_SUBSCRIPTION}"
ROOT = Path(__file__).resolve().parents[3]
SERVICE_VNET = ("/subscriptions/00000000-0000-4000-8000-0000000000aa/resourceGroups/rg-omnifile"
                "/providers/Microsoft.Network/virtualNetworks/omnifile-vnet")  # fmt: skip


@pytest.fixture
def admin(web, sql):
    # The catalog is the session's: each test starts with no connection.
    sql.execute(
        "TRUNCATE catalog.azure_grant, catalog.azure_connection, catalog.azure_connect_op,"
        " catalog.aws_connection, catalog.cloud_task"
    )
    (mark,) = sql.execute("SELECT coalesce(max(audit_id), 0) FROM catalog.audit_log").fetchone()
    web.audit_mark = mark
    w = seed(sql)
    web.services.settings_env = dict(IDS)
    return w, web.browser(w.admin)


def audits(web, sql, action: str) -> list[dict]:
    rows = sql.execute(
        "SELECT details FROM catalog.audit_log WHERE action = %s AND audit_id > %s"
        " ORDER BY audit_id",
        (action, web.audit_mark),
    ).fetchall()
    return [r[0] for r in rows]


@pytest.fixture
def arm(web):
    fake = FakeArm(answers=demo_arm_answers(TENANT))
    tokens: list[str] = []

    def arm_for(token: str):
        tokens.append(token)
        return fake

    web.services.arm_for = arm_for
    fake.tokens = tokens  # type: ignore[attr-defined]
    return fake


def sign_in(b, purpose: str, *, user: dict | None = None, **body: Any):
    """Start a Connect Azure sign-in and follow it through the dev issuer back
    to the callback, as a browser does. Returns the callback's response."""
    r = b.post("/v1/settings/azure/sign-in", {"purpose": purpose, **body})
    assert r.status_code == 201, r.text
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{AZURE_COOKIE}=") and "SameSite=Lax" in cookie
    state = cookie.split("=", 1)[1].split(";", 1)[0]
    url = r.json()["authorize_url"]
    q = parse_qs(urlsplit(url).query)
    assert "https://management.azure.com/user_impersonation" in q["scope"][0]
    assert "offline_access" not in q["scope"][0]
    assert ("max_age" in q) == (purpose != "list")
    r2 = b.client.get(url + "&" + urlencode(dev_params(user or b.user)), follow_redirects=False)
    assert r2.status_code == 302, r2.text
    back = urlsplit(r2.headers["location"])
    assert back.path == "/auth/azure/callback"
    b._cookies()
    b.client.cookies.set(AZURE_COOKIE, state, domain="portal.test", path="/")
    params = {k: v[0] for k, v in parse_qs(back.query).items()}
    return callback(b, params)  # fmt: skip


def callback(b, params):
    """The callback, spec-checked, without following its redirect."""
    b.client.follow_redirects = False
    try:
        return _send(b.client, "GET", "/auth/azure/callback", None, params, {}, True, {})
    finally:
        b.client.follow_redirects = True


def last_op(b) -> dict[str, Any]:
    return b.get("/v1/settings/azure").json()["last_op"]


def _choose(b, rgs=("rg-storage",), mover: str | None = "rg-migration"):
    r = b.call("PUT", "/v1/settings/azure/choice",
               json={"subscription_id": DEMO_SUBSCRIPTION, "resource_groups": list(rgs),
                     "mover_resource_group": mover})  # fmt: skip
    assert r.status_code == 200, r.text
    return r.json()


def _writes(arm: FakeArm) -> list[str]:
    return [c for c in arm.calls if not c.startswith("GET ")]


# --- Connect Azure ----------------------------------------------------------------------------


def test_connect_azure_lists_plans_grants_discovers_and_confirms(web, sql, admin, arm) -> None:
    w, b = admin
    s = b.get("/v1/settings/azure").json()
    assert s["listing"] is None and s["plan"] == [] and s["steps"][0]["status"] == "done"
    assert [x["status"] for x in s["steps"][1:]] == ["todo"] * 5

    # 1. List: only GETs, with the admin's token, which is then dropped.
    r = sign_in(b, "list")
    assert r.status_code == 302 and r.headers["location"].startswith("/settings/azure?op=")
    assert last_op(b)["state"] == "done", last_op(b)
    assert _writes(arm) == [] and len(arm.tokens) == 1
    s = b.get("/v1/settings/azure").json()
    tenants = {t["tenant_id"]: t for t in s["listing"]["tenants"]}
    assert tenants[TENANT]["selectable"] and not tenants[
        "99999999-9999-4999-8999-999999999999"]["selectable"]  # fmt: skip
    assert {g["name"] for g in s["listing"]["resource_groups"]} == {
        "rg-storage", "rg-network", "rg-migration"}  # fmt: skip

    # 2. Choose: the plan, every action shown, nothing made yet.
    s = _choose(b, ("rg-storage", "rg-network"))
    plan = s["plan"]
    assert [(p["purpose"], p["scope"].rsplit("/", 1)[1], p["principal"]) for p in plan] == [
        ("discover", "rg-storage", "web"), ("discover", "rg-network", "web"),
        ("discover", "rg-migration", "web"), ("storage_mover", "rg-migration", "worker"),
    ]  # fmt: skip
    assert all(p["state"] == "planned" for p in plan)
    assert plan[0]["principal_id"] == WEB_OID and plan[3]["principal_id"] == WORKER_OID
    every = {a for p in plan for a in p["actions"]}
    assert not any("listKeys" in a or a.endswith("/delete") for a in every)
    assert not any(a.startswith("Microsoft.Authorization/") and not a.endswith("/read")
                   for a in every)  # fmt: skip
    assert _writes(arm) == []

    # 3. Grant: rights checked on every scope first, then exactly the plan's PUTs.
    arm.calls.clear()
    r = sign_in(b, "grant")
    assert last_op(b)["state"] == "done", last_op(b)
    scopes = list(dict.fromkeys(p["scope"] for p in plan))
    perms = [c for c in arm.calls if c.endswith("/Microsoft.Authorization/permissions")]
    assert perms == [f"GET {s}/providers/Microsoft.Authorization/permissions" for s in scopes]
    assert arm.calls.index(perms[-1]) < arm.calls.index(_writes(arm)[0])
    puts = _writes(arm)
    assert len(puts) == 2 * len(plan) and all(c.startswith("PUT ") for c in puts)
    for p in plan:
        authz = f"{p['scope']}/providers/Microsoft.Authorization"
        rd = f"{authz}/roleDefinitions/{p['role_definition_id']}"
        body = arm.bodies[rd]["properties"]
        assert body["assignableScopes"] == [p["scope"]] and body["type"] == "CustomRole"
        assert body["permissions"][0]["actions"] == p["actions"]
        assert body["permissions"][0]["dataActions"] == []
        name = az.assignment_name(p["purpose"], p["scope"], p["principal_id"])
        ra = arm.bodies[f"{p['scope']}/providers/Microsoft.Authorization/roleAssignments/{name}"]
        assert ra["properties"]["principalId"] == p["principal_id"]
        assert ra["properties"]["principalType"] == "ServicePrincipal"
    s = b.get("/v1/settings/azure").json()
    assert all(p["state"] == "active" for p in s["plan"]) and len(s["grants"]) == 4
    made = audits(web, sql, "settings.azure_grant")
    assert len(made) == 4 and made[0]["actions"] == plan[0]["actions"]

    # No token anywhere in the catalog, and the verifier is gone.
    ops = sql.execute("SELECT code_verifier, plan::text, detail FROM catalog.azure_connect_op")
    for verifier, plan_text, detail in ops.fetchall():
        assert verifier is None
        for t in arm.tokens:
            assert t not in plan_text and t not in (detail or "")
    dump = sql.execute(
        "SELECT string_agg(t::text, '') FROM (SELECT * FROM catalog.azure_connection) t"
    ).fetchone()[0]
    assert not any(t in dump for t in arm.tokens)

    # 5. Discover with web's own identity (fake probes), over the granted groups.
    st = f"{SUB}/resourceGroups/rg-storage"
    acct = f"{st}/providers/Microsoft.Storage/storageAccounts"
    probes = FakeProbes(arm={
        acct: {"value": [
            {"id": f"{acct}/omnilive01", "name": "omnilive01", "location": "westeurope",
             "sku": {"name": "Standard_ZRS"}, "kind": "StorageV2",
             "properties": {"publicNetworkAccess": "Disabled"}},
            {"id": f"{acct}/openacct", "name": "openacct", "location": "westeurope",
             "sku": {"name": "Standard_LRS"}, "properties": {"publicNetworkAccess": "Enabled"}},
        ]},
        f"{st}/providers/Microsoft.Network/virtualNetworks": {"value": []},
        f"{st}/providers/Microsoft.Network/privateEndpoints": {"value": [
            {"id": f"{st}/providers/Microsoft.Network/privateEndpoints/pe-blob", "name": "pe-blob",
             "properties": {"subnet": {"id": "subnet-1"}, "privateLinkServiceConnections": [
                 {"properties": {"privateLinkServiceId": f"{acct}/omnilive01",
                                 "groupIds": ["blob"],
                                 "privateLinkServiceConnectionState": {"status": "Approved"}}}]}},
        ]},
    })  # fmt: skip
    for rg in ("rg-network", "rg-migration"):
        for kind in ("Storage/storageAccounts", "Network/virtualNetworks",
                     "Network/privateEndpoints"):  # fmt: skip
            probes.arm[f"{SUB}/resourceGroups/{rg}/providers/Microsoft.{kind}"] = {"value": []}
    zone = (f"{SUB}/resourceGroups/rg-network/providers/Microsoft.Network/privateDnsZones/"
            "privatelink.blob.core.windows.net")  # fmt: skip
    pe = f"{st}/providers/Microsoft.Network/privateEndpoints/pe-blob"
    probes.arm[f"{pe}/privateDnsZoneGroups"] = _zone_group(zone)
    probes.arm[f"{zone}/virtualNetworkLinks"] = _links(SERVICE_VNET)
    web.services.settings_env = {
        **web.services.settings_env,
        "OMNIFILE_SERVICE_VNET_ID": SERVICE_VNET,
    }
    web.services.probes = probes
    r = b.post("/v1/settings/azure/discover", {})
    assert r.status_code == 200, r.text
    found = {a["name"]: a for a in r.json()["discovered"]["storage_accounts"]}
    assert found["omnilive01"]["private"] and found["omnilive01"]["zrs"]
    assert not found["openacct"]["private"]

    # 6. Confirm: a public account can't be used (decision 22).
    r = b.post("/v1/settings/azure/confirm",
               {"storage_accounts": [f"{acct}/openacct"]}, check=False)  # fmt: skip
    assert r.status_code == 400 and "privately" in r.text
    r = b.post("/v1/settings/azure/confirm", {"storage_accounts": [f"{acct}/omnilive01"]})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["confirmed"]["storage_accounts"] == [f"{acct}/omnilive01"]
    assert all(x["status"] == "done" for x in s["steps"]), s["steps"]


def _account(name: str, **props: Any) -> dict[str, Any]:
    acct = f"{SUB}/resourceGroups/rg-storage/providers/Microsoft.Storage/storageAccounts"
    return {"id": f"{acct}/{name}", "name": name, "sku": {"name": "Standard_ZRS"},
            "properties": props}  # fmt: skip


def _zone_group(*zone_ids: str) -> dict[str, Any]:
    """A private endpoint's DNS zone group, as ARM lists it."""
    cfgs = [{"name": "c", "properties": {"privateDnsZoneId": z}} for z in zone_ids]
    return {"value": [{"name": "default", "properties": {"privateDnsZoneConfigs": cfgs}}]}


def _links(*vnet_ids: str) -> dict[str, Any]:
    return {"value": [{"properties": {"virtualNetwork": {"id": v}}} for v in vnet_ids]}


def _pe(target: str, status: str | None) -> dict[str, Any]:
    state = {} if status is None else {"privateLinkServiceConnectionState": {"status": status}}
    conn = {"properties": {"privateLinkServiceId": target, "groupIds": ["blob"], **state}}
    return {"id": f"{target}-pe", "name": "pe",
            "properties": {"privateLinkServiceConnections": [conn]}}  # fmt: skip


def test_private_means_closed_approved_and_resolved_privately() -> None:
    """Review of #64 (decision 22): Deny with an IP or VNet rule isn't closed, a private
    endpoint with no status isn't approved, and the blob zone must be linked to the
    service's VNet."""
    st = f"{SUB}/resourceGroups/rg-storage"
    accounts = [
        _account("disabled", publicNetworkAccess="Disabled"),
        _account("denynorules", networkAcls={"defaultAction": "Deny"}),
        _account("denyiprule", networkAcls={"defaultAction": "Deny",
                                            "ipRules": [{"value": "203.0.113.0/24"}]}),
        _account("denyvnetrule", networkAcls={"defaultAction": "Deny",
                                              "virtualNetworkRules": [{"id": "subnet"}]}),
        _account("allow", networkAcls={"defaultAction": "Allow"}),
        _account("nostatus", publicNetworkAccess="Disabled"),
    ]  # fmt: skip
    pes = [_pe(a["id"], "Approved") for a in accounts[:-1]] + [_pe(accounts[-1]["id"], None)]
    zone = f"{st}/providers/Microsoft.Network/privateDnsZones/privatelink.blob.core.windows.net"

    def probes(linked: str | None) -> FakeProbes:
        arm = {
            f"{st}/providers/Microsoft.Storage/storageAccounts": {"value": accounts},
            f"{st}/providers/Microsoft.Network/virtualNetworks": {"value": []},
            f"{st}/providers/Microsoft.Network/privateEndpoints": {"value": pes},
            f"{zone}/virtualNetworkLinks": _links() if linked is None else _links(linked),
        }
        for e in pes:
            arm[f"{e['id']}/privateDnsZoneGroups"] = _zone_group(zone)
        return FakeProbes(arm=arm)

    now = datetime.now(UTC)
    got = asyncio.run(az.discover(probes(SERVICE_VNET), [st], now, service_vnet_id=SERVICE_VNET))
    private = {a["name"]: a["private"] for a in got["storage_accounts"]}
    assert private == {"disabled": True, "denynorules": True, "denyiprule": False,
                       "denyvnetrule": False, "allow": False, "nostatus": False}  # fmt: skip
    assert got["blob_dns_zones"] == [{"id": zone, "linked_to_service": True}]
    # A zone linked to another VNet, or no service VNet named: nothing is private.
    other = asyncio.run(az.discover(probes("/subscriptions/x/vnet-other"), [st], now,
                                    service_vnet_id=SERVICE_VNET))  # fmt: skip
    assert not any(a["private"] for a in other["storage_accounts"])
    unnamed = asyncio.run(az.discover(probes(SERVICE_VNET), [st], now))
    assert not any(a["private"] for a in unnamed["storage_accounts"])
    assert any("OMNIFILE_SERVICE_VNET_ID" in e for e in unnamed["errors"])


def test_each_account_is_checked_through_its_own_endpoints_dns_zone() -> None:
    """Follow-up of #64: one linked zone doesn't vouch for every account. Each account's
    own blob endpoint must register in a privatelink.blob zone linked to the service's
    VNet; an endpoint with no zone group, or whose zone isn't linked, isn't."""
    st = f"{SUB}/resourceGroups/rg-storage"
    zones = f"{st}/providers/Microsoft.Network/privateDnsZones"
    linked_zone = f"{zones}/privatelink.blob.core.windows.net"
    hub_zone = (f"{SUB}/resourceGroups/rg-hub/providers/Microsoft.Network/privateDnsZones/"
                "privatelink.blob.core.windows.net")  # fmt: skip
    accounts = [_account(n, publicNetworkAccess="Disabled")
                for n in ("linked", "otherzone", "nogroup", "wrongzone")]  # fmt: skip
    pes = [_pe(a["id"], "Approved") for a in accounts]
    arm = {
        f"{st}/providers/Microsoft.Storage/storageAccounts": {"value": accounts},
        f"{st}/providers/Microsoft.Network/virtualNetworks": {"value": []},
        f"{st}/providers/Microsoft.Network/privateEndpoints": {"value": pes},
        f"{pes[0]['id']}/privateDnsZoneGroups": _zone_group(linked_zone),
        f"{pes[1]['id']}/privateDnsZoneGroups": _zone_group(hub_zone),
        f"{pes[2]['id']}/privateDnsZoneGroups": {"value": []},
        # A zone linked to the service VNet, but a file zone, not blob's.
        f"{pes[3]['id']}/privateDnsZoneGroups": _zone_group(
            f"{zones}/privatelink.file.core.windows.net"),
        f"{linked_zone}/virtualNetworkLinks": _links(SERVICE_VNET),
        f"{hub_zone}/virtualNetworkLinks": _links("/subscriptions/x/vnet-hub"),
        f"{zones}/privatelink.file.core.windows.net/virtualNetworkLinks": _links(SERVICE_VNET),
    }  # fmt: skip
    got = asyncio.run(az.discover(FakeProbes(arm=arm), [st], datetime.now(UTC),
                                  service_vnet_id=SERVICE_VNET))  # fmt: skip
    by = {a["name"]: a for a in got["storage_accounts"]}
    assert {n: a["dns_linked"] for n, a in by.items()} == {
        "linked": True, "otherzone": False, "nogroup": False, "wrongzone": False}  # fmt: skip
    assert {n: a["private"] for n, a in by.items()} == {
        "linked": True, "otherzone": False, "nogroup": False, "wrongzone": False}  # fmt: skip
    assert {z["id"]: z["linked_to_service"] for z in got["blob_dns_zones"]} == {
        linked_zone: True, hub_zone: False}  # fmt: skip


def test_grants_are_removable_and_removal_needs_its_own_sign_in(web, sql, admin, arm) -> None:
    _w, b = admin
    sign_in(b, "list")
    _choose(b, ("rg-storage",), None)
    sign_in(b, "grant")
    s = b.get("/v1/settings/azure").json()
    (g,) = s["grants"]
    arm.calls.clear()
    r = b.post("/v1/settings/azure/sign-in", {"purpose": "remove", "grant_ids": ["999"]},
               check=False)  # fmt: skip
    assert r.status_code == 404
    sign_in(b, "remove", grant_ids=[g["grant_id"]])
    assert last_op(b)["state"] == "done", last_op(b)
    assert _writes(arm) == [
        f"DELETE {g['assignment_id']}",
        f"DELETE {g['scope']}/providers/Microsoft.Authorization/roleDefinitions/"
        f"{s['plan'][0]['role_definition_id']}",
    ]
    s = b.get("/v1/settings/azure").json()
    assert s["grants"][0]["state"] == "removed" and s["plan"][0]["state"] == "planned"
    assert (
        sql.execute(
            "SELECT count(*) FROM catalog.audit_log WHERE action = 'settings.azure_grant_remove'"
        ).fetchone()[0]
        == 1
    )


def test_without_the_rights_nothing_is_granted(web, sql, admin, arm) -> None:
    _w, b = admin
    sign_in(b, "list")
    _choose(b, ("rg-storage", "rg-network"), None)
    arm.answers[
        f"{SUB}/resourceGroups/rg-network/providers/Microsoft.Authorization/permissions"
    ] = {"value": [{"actions": ["*"], "notActions": ["Microsoft.Authorization/*/write"]}]}
    arm.calls.clear()
    sign_in(b, "grant")
    op = last_op(b)
    assert op["state"] == "failed" and "roleDefinitions/write" in op["detail"]
    assert "rg-network" in op["detail"] and _writes(arm) == []
    assert b.get("/v1/settings/azure").json()["grants"] == []


def test_a_grant_that_fails_half_way_keeps_what_was_made(web, sql, admin, arm) -> None:
    _w, b = admin
    sign_in(b, "list")
    plan = _choose(b, ("rg-storage", "rg-network"), None)["plan"]
    second = plan[1]
    rd = (f"{second['scope']}/providers/Microsoft.Authorization/roleDefinitions/"
          f"{second['role_definition_id']}")  # fmt: skip
    arm.fail_once[rd] = ArmError(403, "AuthorizationFailed", "no")
    sign_in(b, "grant")
    op = last_op(b)
    assert op["state"] == "failed" and "1 of 2" in op["detail"]
    s = b.get("/v1/settings/azure").json()
    assert [p["state"] for p in s["plan"]] == ["active", "planned"]
    assert len(s["grants"]) == 1


def test_a_lost_answer_mid_grant_still_records_and_audits_what_was_made(
    web, sql, admin, arm
) -> None:
    """Review of #64: a transport error (no answer) after a role and an assignment were
    made in Azure must not leave them unrecorded, unaudited and unremovable."""
    from omnifile_service.settings.ports import ProbeError

    _w, b = admin
    sign_in(b, "list")
    plan = _choose(b, ("rg-storage", "rg-network"), None)["plan"]
    second = plan[1]
    name = az.assignment_name(second["purpose"], second["scope"], second["principal_id"])
    path = f"{second['scope']}/providers/Microsoft.Authorization/roleAssignments/{name}"
    arm.fail_once[path] = ProbeError("ReadTimeout: no answer")
    sign_in(b, "grant")
    op = last_op(b)
    assert op["state"] == "failed" and "didn't answer" in op["detail"], op
    s = b.get("/v1/settings/azure").json()
    assert [p["state"] for p in s["plan"]] == ["active", "active"]
    made = [m for m in audits(web, sql, "settings.azure_grant") if "assignment_id" in m]
    assert [m["uncertain"] for m in made] == [False, True]

    # Removing it works: the lost assignment answers 404, which counts as gone. A lost
    # answer half-way through a removal keeps what was removed before it.
    ids = [g["grant_id"] for g in s["grants"]]
    first = s["grants"][0]
    arm.fail_once[s["grants"][1]["assignment_id"]] = ProbeError("ConnectError: reset")
    sign_in(b, "remove", grant_ids=ids)
    op = last_op(b)
    assert op["state"] == "failed" and "1 of 2" in op["detail"], op
    s = b.get("/v1/settings/azure").json()
    states = {g["grant_id"]: g["state"] for g in s["grants"]}
    assert states[first["grant_id"]] == "removed" and states[ids[1]] == "active"
    assert len(audits(web, sql, "settings.azure_grant_remove")) == 1
    sign_in(b, "remove", grant_ids=[ids[1]])
    assert last_op(b)["state"] == "done"


def test_a_new_role_that_hasnt_replicated_is_retried(monkeypatch) -> None:
    item = az.plan({"subscription_id": DEMO_SUBSCRIPTION, "resource_groups": ["rg-storage"]},
                   IDS)[0]  # fmt: skip
    arm = FakeArm(answers=demo_arm_answers(TENANT))
    name = az.assignment_name(item["purpose"], item["scope"], item["principal_id"])
    path = f"{item['scope']}/providers/Microsoft.Authorization/roleAssignments/{name}"
    arm.fail_once[path] = ArmError(400, "RoleDefinitionDoesNotExist", "not yet")
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    made, err = asyncio.run(az.grant_with(arm, [item], sleep=sleep))
    assert err is None and len(made) == 1 and slept == [az.ROLE_RETRY_S]


def test_the_callback_refuses_another_browser_another_person_and_a_replay(
    web, sql, admin, arm
) -> None:
    w, b = admin
    # Another browser: no cookie.
    r = b.post("/v1/settings/azure/sign-in", {"purpose": "list"})
    url = r.json()["authorize_url"]
    r2 = b.client.get(url + "&" + urlencode(dev_params(b.user)), follow_redirects=False)
    q = {k: v[0] for k, v in parse_qs(urlsplit(r2.headers["location"]).query).items()}
    b._cookies()
    r3 = callback(b, q)
    assert r3.status_code == 401
    # The right browser, then the same state again.
    b.client.cookies.set(AZURE_COOKIE, q["state"], domain="portal.test", path="/")
    r4 = callback(b, q)
    assert r4.status_code == 302
    r5 = callback(b, q)
    assert r5.status_code == 401
    # Signed in to Azure as someone else.
    arm.calls.clear()
    sign_in(b, "list", user=w.admin2)
    op = last_op(b)
    assert op["state"] == "failed" and "someone else" in op["detail"] and arm.calls == []


def test_another_tenants_subscription_cant_be_chosen(web, sql, admin, arm) -> None:
    _w, b = admin
    arm.answers["/subscriptions"]["value"].append(
        {"subscriptionId": "00000000-0000-4000-8000-0000000000b2", "displayName": "Fabrikam",
         "tenantId": "99999999-9999-4999-8999-999999999999", "state": "Enabled"}
    )  # fmt: skip
    sign_in(b, "list")
    r = b.call("PUT", "/v1/settings/azure/choice", check=False,
               json={"subscription_id": "00000000-0000-4000-8000-0000000000b2",
                     "resource_groups": ["rg-x"]})  # fmt: skip
    assert r.status_code == 400 and "another tenant" in r.text
    r = b.call(
        "PUT",
        "/v1/settings/azure/choice",
        check=False,
        json={"subscription_id": DEMO_SUBSCRIPTION, "resource_groups": ["rg-unknown"]},
    )
    assert r.status_code == 400


def test_a_grant_needs_the_service_identities(web, sql, admin, arm) -> None:
    _w, b = admin
    web.services.settings_env = {}
    sign_in(b, "list")
    s = _choose(b, ("rg-storage",), None)
    assert s["plan"][0]["state"] == "missing_identity"
    r = b.post("/v1/settings/azure/sign-in", {"purpose": "grant"}, check=False)
    assert r.status_code == 422 and r.json()["code"] == "dependency_failed"
    r = b.post("/v1/settings/azure/discover", {}, check=False)
    assert r.status_code == 422


ADMIN_ONLY = [
    ("GET", "/v1/settings/azure", None),
    ("POST", "/v1/settings/azure/sign-in", {"purpose": "list"}),
    ("PUT", "/v1/settings/azure/choice",
     {"subscription_id": DEMO_SUBSCRIPTION, "resource_groups": ["rg-storage"]}),
    ("POST", "/v1/settings/azure/discover", {}),
    ("POST", "/v1/settings/azure/confirm", {"storage_accounts": []}),
    ("GET", "/v1/settings/aws/connect", None),
    ("GET", "/v1/settings/aws/connect/template", None),
    ("POST", "/v1/settings/aws/connect/fetch",
     {"account_id": ACCT, "region": "eu-west-1", "deployment_id": "omnifile"}),
    ("POST", "/v1/settings/aws/connect/outputs", {"text": "RoleArn x"}),
    ("POST", "/v1/settings/aws/connect/confirm",
     {"account_id": ACCT, "region": "eu-west-1", "deployment_id": "omnifile", "outputs": {},
      "buckets": []}),
    ("POST", "/v1/settings/storage-mover/deploy",
     {"name": "omnifile-mover", "project": "migrations", "location": "westeurope"}),
    ("GET", "/v1/settings/tasks/{task_id}", None),
    ("POST", "/v1/source-scans", {"backend_id": "1", "bucket": "acme-wells", "prefix": ""}),
    ("GET", "/v1/source-scans/{scan_id}", None),
]  # fmt: skip


def test_every_cloud_connect_route_is_for_archive_admins(web, sql, admin) -> None:
    w, _b = admin
    for who in (w.steward, w.reader):
        b = web.browser(who)
        for method, path, body in ADMIN_ONLY:
            r = b.call(method, path, json=body, check=False, task_id="1", scan_id="1")
            assert r.status_code == 403 and r.json()["code"] == "role_required", (who, path)
    r = web.call("GET", "/v1/settings/azure", token=web.app_token(["Archive.Read"]),
                 check=False)  # fmt: skip
    assert r.status_code == 401


# --- Connect AWS ----------------------------------------------------------------------------


AWS_ENV = {**AWS, "OMNIFILE_AWS_WEB_IDENTITY_SUBJECTS": WORKER_OID}


def _cfn_load(text: str) -> dict:
    class L(yaml.SafeLoader):
        pass

    def tag(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            v: Any = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            v = loader.construct_sequence(node, deep=True)
        else:
            v = loader.construct_mapping(node, deep=True)
        name = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
        return {name: v}

    L.add_multi_constructor("!", tag)
    return yaml.load(text, Loader=L)  # noqa: S506 - SafeLoader subclass


def test_the_template_ships_in_the_service_and_trusts_what_the_page_shows() -> None:
    deploy = (ROOT / "deploy/aws/cloudformation/omnifile-connect.yaml").read_text()
    assert aws_connect.template_text() == deploy
    t = _cfn_load(deploy)
    role = t["Resources"]["ServiceRole"]["Properties"]
    fmt, subs = role["AssumeRolePolicyDocument"]["Fn::Sub"]
    # Resolve the Sub as CloudFormation would, for this deployment.
    text = (fmt.replace("${AWS::Partition}", "aws").replace("${AWS::AccountId}", ACCT)
            .replace("${TenantId}", TENANT).replace("${Audience}", AUDIENCE)
            .replace("${Subjects}", WORKER_OID))  # fmt: skip
    assert subs == {"Subjects": {"Fn::Join": ['","', {"Ref": "ServiceSubjects"}]}}
    trust = json.loads(text)
    page = aws_settings.trust_policy(f"arn:aws:iam::{ACCT}:role/omnifile-service-omnifile",
                                     AWS_ENV, TENANT)["trust_policy"]  # fmt: skip
    st = page["Statement"][0]
    st["Condition"]["StringEquals"] = {
        k: [v] if k.endswith(":sub") else v for k, v in st["Condition"]["StringEquals"].items()
    }
    assert trust == page
    # Decision 40 (review of #64): every S3 write family is denied on every resource but
    # the ops bucket, so no path (bucket ARN, access point, Multi-Region Access Point) slips by.
    res = t["Resources"]
    ops = [{"Fn::GetAtt": "OpsBucket.Arn"}, {"Fn::Sub": "${OpsBucket.Arn}/*"}]
    for role_name in ("ServiceRole", "BatchRole"):
        pols = res[role_name]["Properties"]["Policies"]
        st_ = {s["Sid"]: s for p in pols for s in p["PolicyDocument"]["Statement"]}
        deny = st_["NeverWriteOutsideTheOpsBucket"]
        assert deny["Effect"] == "Deny" and "Resource" not in deny
        assert deny["NotResource"] == ops
        assert set(deny["Action"]) == {"s3:Put*", "s3:Delete*", "s3:Replicate*", "s3:Bypass*",
                                       "s3:AbortMultipartUpload",
                                       "s3:ObjectOwnerOverrideToBucketOwner"}  # fmt: skip
    stmts = {s["Sid"]: s for p in role["Policies"] for s in p["PolicyDocument"]["Statement"]}
    # Restores are allowed explicitly ("OmniFile may always request restores"), and no
    # allowed action outside the ops bucket falls under a denied pattern.
    assert "s3:RestoreObject" in stmts["ReadAndRestoreTheSourceObjects"]["Action"]
    import fnmatch

    for sid in ("ReadTheSourceBuckets", "ReadAndRestoreTheSourceObjects", "ListBucketsForConnect",
                "BatchOperations"):  # fmt: skip
        acts = stmts[sid]["Action"]
        for a in [acts] if isinstance(acts, str) else acts:
            assert not any(fnmatch.fnmatchcase(a, d) for d in
                           stmts["NeverWriteOutsideTheOpsBucket"]["Action"]), a  # fmt: skip
    allowed = {a for s in stmts.values() if s["Effect"] == "Allow" for a in
               ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}  # fmt: skip
    assert "sts:AssumeRole" not in allowed and not any(a.startswith("iam:Create") for a in allowed)

    # Every ARN names its partition: a literal one only inside the partition choice.
    def walk(node, path):
        if isinstance(node, str):
            if "arn:aws" in node and "${AWS::Partition}" not in node:
                assert "Fn::If" in path, (path, node)
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, [*path, k])
        elif isinstance(node, list):
            for v in node:
                walk(v, path)

    walk(res, [])
    # ServiceSubjects and SourceBuckets are checked per value: nothing can break the JSON.
    import re as _re

    pat = t["Parameters"]["ServiceSubjects"]["AllowedPattern"]
    assert _re.fullmatch(pat, WORKER_OID) and not _re.fullmatch(pat, 'x","*')
    assert t["Parameters"]["SourceBuckets"]["AllowedPattern"]
    # Batch's trust is this account's jobs only; the ops bucket is TLS-only.
    trust_b = res["BatchRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust_b["Condition"]["StringEquals"] == {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
    tls = res["OpsBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"][0]
    assert tls["Effect"] == "Deny" and tls["Condition"] == {
        "Bool": {"aws:SecureTransport": "false"}
    }
    # The interface endpoints: a policy each, never s3:* or *; the security group has no
    # wide default (the portal gives the service's VNet range, a rule requires it).
    for name in ("S3Endpoint", "StsEndpoint", "SqsEndpoint"):
        doc = res[name]["Properties"]["PolicyDocument"]
        for s in doc["Statement"]:
            acts = [s["Action"]] if isinstance(s["Action"], str) else s["Action"]
            assert not any(a in ("*", "s3:*", "sqs:*", "sts:*") for a in acts), name
    assert res["StsEndpoint"]["Properties"]["PolicyDocument"]["Statement"][0]["Resource"] == {
        "Fn::GetAtt": "ServiceRole.Arn"}  # fmt: skip
    assert t["Parameters"]["EndpointClientCidr"]["Default"] == ""
    # Follow-up of #64: an interrupted multipart upload to the ops bucket can be aborted
    # by the service through the endpoint, and S3 aborts what is left after a day.
    s3_ep = res["S3Endpoint"]["Properties"]["PolicyDocument"]["Statement"]
    ops_write = next(s for s in s3_ep if s["Sid"] == "WriteOnlyTheOpsBucket")
    assert "s3:AbortMultipartUpload" in ops_write["Action"]
    assert ops_write["Resource"] == {"Fn::Sub": "${OpsBucket.Arn}/*"}
    assert "s3:AbortMultipartUpload" in stmts["TheOpsBucket"]["Action"]
    rules = res["OpsBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
    assert [r["AbortIncompleteMultipartUpload"] for r in rules if r["Status"] == "Enabled"] == [
        {"DaysAfterInitiation": 1}]  # fmt: skip
    assert "EndpointsNeedTheirClients" in t["Rules"]
    outputs = set(t["Outputs"])
    assert outputs == set(aws_connect.OUTPUTS)


def test_the_service_may_pass_only_the_batch_role_which_only_batch_operations_assumes() -> None:
    """Real AWS, 2026-11-14: `CreateJob` was denied `iam:PassRole` while the statement
    carried an `iam:PassedToService` condition (StringEquals, and StringEqualsIfExists),
    and worked without it. So the statement names the one role and has no condition, and
    the role's trust policy is what keeps it to S3 Batch Operations."""
    for path in (
        "deploy/aws/cloudformation/omnifile-connect.yaml",
        "service/src/omnifile_service/settings/cfn/omnifile-connect.yaml",
    ):
        res = _cfn_load((ROOT / path).read_text())["Resources"]
        statements = [
            s
            for role in res.values()
            if role["Type"] == "AWS::IAM::Role"
            for p in role["Properties"].get("Policies", [])
            for s in p["PolicyDocument"]["Statement"]
        ]
        passes = [s for s in statements if "iam:PassRole" in json.dumps(s["Action"])]
        assert passes == [
            {
                "Sid": "PassOnlyTheBatchRole",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": {"Fn::GetAtt": "BatchRole.Arn"},
            }
        ], path
        assert res["BatchRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"] == [
            {
                "Effect": "Allow",
                "Principal": {"Service": "batchoperations.s3.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}},
            }
        ], path


def test_launch_in_aws_fills_the_template_and_says_what_is_missing(web, sql, admin) -> None:
    _w, b = admin
    web.services.settings_env = {
        **AWS_ENV,
        aws_connect.ENV_TEMPLATE_URL: "https://omnifile-releases.s3.amazonaws.com/v1/omnifile-connect.yaml",
    }
    r = b.get("/v1/settings/aws/connect", params={"region": "eu-west-1"})
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["ready"] and c["private_network"] and c["stack_name"] == "omnifile-omnifile"
    q = parse_qs(urlsplit(c["quick_create_url"].replace("#/stacks/quickcreate?", "?")).query)
    assert q["param_TenantId"] == [TENANT] and q["param_Audience"] == [AUDIENCE]
    assert q["param_ServiceSubjects"] == [WORKER_OID] and q["stackName"] == ["omnifile-omnifile"]
    assert c["quick_create_url"].startswith("https://eu-west-1.console.aws.amazon.com/")
    # Real AWS, 2026-11-14: a bucket's events start about five minutes after EventBridge is
    # turned on, and the step that turns it on says so.
    [eventbridge] = [s for s in c["steps"] if "EventBridge" in s]
    assert "about five minutes after" in eventbridge
    # Without a template in S3: the download path; without an audience: not ready.
    web.services.settings_env = {}
    c = b.get("/v1/settings/aws/connect").json()
    assert c["quick_create_url"] is None and not c["ready"] and len(c["problems"]) == 2
    r = b.get("/v1/settings/aws/connect/template")
    assert r.status_code == 200 and r.text == aws_connect.template_text()
    assert r.headers["content-type"].startswith("application/yaml")


OUTPUTS = {
    "AccountId": ACCT, "Region": "eu-west-1", "DeploymentId": "omnifile",
    "RoleArn": f"arn:aws:iam::{ACCT}:role/omnifile-service-omnifile",
    "BatchRoleArn": f"arn:aws:iam::{ACCT}:role/omnifile-batch-omnifile",
    "OpsBucket": f"omnifile-ops-omnifile-{ACCT}",
    "EventQueueUrl": f"https://sqs.eu-west-1.amazonaws.com/{ACCT}/omnifile-events-omnifile",
    "SourceBuckets": "acme-wells,acme-logs",
    "S3EndpointDns": "*.vpce-0abc-1234.s3.eu-west-1.vpce.amazonaws.com",
    "StsEndpointDns": "vpce-0123-5678.sts.eu-west-1.vpce.amazonaws.com",
    "SqsEndpointDns": "vpce-0def-9abc.sqs.eu-west-1.vpce.amazonaws.com",
}  # fmt: skip


@pytest.mark.parametrize("shape", ["console", "describe", "object", "lines"])
def test_pasted_outputs_become_an_entry_the_worker_would_load(shape) -> None:
    if shape == "console":
        text = "\n".join(f"{k}\t{v}\tdescription\t-" for k, v in OUTPUTS.items())
    elif shape == "describe":
        text = json.dumps({"Stacks": [{"Outputs": [
            {"OutputKey": k, "OutputValue": v} for k, v in OUTPUTS.items()]}]})  # fmt: skip
    elif shape == "object":
        text = json.dumps(OUTPUTS)
    else:
        text = "\n".join(f"{k}={v}" for k, v in OUTPUTS.items())
    got = aws_connect.parse_outputs(text)
    assert got == OUTPUTS
    entry, problems = aws_connect.entry_from(None, None, None, got, AWS_ENV)
    assert problems == [], problems
    assert (
        entry["endpoints"]["s3"] == "https://bucket.vpce-0abc-1234.s3.eu-west-1.vpce.amazonaws.com"
    )
    assert entry["endpoints"]["s3_control"].startswith("https://control.vpce-0abc-1234.")
    assert entry["batch"] == {"role_arn": OUTPUTS["BatchRoleArn"], "manifest_bucket":
                              OUTPUTS["OpsBucket"]}  # fmt: skip
    assert "secret" not in json.dumps(entry).lower()


def test_connect_aws_finishes_without_private_networking(web, sql, db_url, admin) -> None:
    """Review of #64: in a deployment without private networking the entry says so, the
    worker would load it, and a fetch needs no interface endpoints."""
    from omnifile_service.backends.s3.config import AwsDeployment

    _w, b = admin
    env = {**AWS_ENV, "OMNIFILE_AWS_PRIVATE_NETWORK": "0"}
    public = {k: v for k, v in OUTPUTS.items() if not k.endswith("Dns")}
    entry, problems = aws_connect.entry_from(None, None, None, public, env)
    assert problems == [], problems
    assert entry["private_network"] is False and "endpoints" not in entry
    dep = AwsDeployment.from_mapping({ACCT: entry}, web_identity_audience=AUDIENCE,
                                     private_network=False)  # fmt: skip
    assert dep.accounts[ACCT].private_network is False
    web.services.settings_env = env
    r = b.post(
        "/v1/settings/aws/connect/fetch",
        {"account_id": ACCT, "region": "eu-west-1", "deployment_id": "omnifile"},
    )
    assert r.status_code == 202, r.text
    # And private stays private: the entry carries it.
    entry, problems = aws_connect.entry_from(None, None, None, OUTPUTS, AWS_ENV)
    assert problems == [] and entry["private_network"] is True


def test_the_endpoints_security_group_gets_the_service_vnet_range(web, sql, admin) -> None:
    _w, b = admin
    web.services.settings_env = {**AWS_ENV, "OMNIFILE_SERVICE_VNET_CIDR": "10.60.0.0/16"}
    c = b.get("/v1/settings/aws/connect").json()
    assert c["parameters"]["EndpointClientCidr"] == "10.60.0.0/16"
    web.services.settings_env = {**AWS_ENV, "OMNIFILE_SERVICE_VNET_CIDR": "not a cidr"}
    assert b.get("/v1/settings/aws/connect").json()["parameters"]["EndpointClientCidr"] == ""


def test_outputs_that_break_private_networking_are_refused() -> None:
    public = {k: v for k, v in OUTPUTS.items() if not k.endswith("Dns")}
    _entry, problems = aws_connect.entry_from(None, None, None, public, AWS_ENV)
    assert problems and "refuse" in problems[0]
    changed = {**OUTPUTS, "RoleArn": f"arn:aws:iam::{ACCT}:role/someone-else"}
    _entry, problems = aws_connect.entry_from(None, None, None, changed, AWS_ENV)
    assert any("template" in p for p in problems)


def test_paste_and_confirm_record_the_entry_for_the_deployment(web, sql, admin) -> None:
    _w, b = admin
    web.services.settings_env = dict(AWS_ENV)
    text = "\n".join(f"{k}\t{v}" for k, v in OUTPUTS.items())
    r = b.post("/v1/settings/aws/connect/outputs", {"text": text})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["problems"] == [] and got["buckets"] == ["acme-wells", "acme-logs"]
    r = b.post("/v1/settings/aws/connect/confirm", {
        "account_id": ACCT, "region": "eu-west-1", "deployment_id": "omnifile",
        "outputs": got["outputs"], "buckets": got["buckets"]})  # fmt: skip
    assert r.status_code == 200, r.text
    c = r.json()
    value = json.loads(c["settings_json"])
    assert value[ACCT]["role_arn"] == OUTPUTS["RoleArn"] and c["configured"] is True
    assert c["parameter_name"] == "awsAccounts" and c["env_name"] == "OMNIFILE_AWS_ACCOUNTS"
    assert (
        sql.execute(
            "SELECT count(*) FROM catalog.audit_log WHERE action = 'settings.aws_confirm'"
        ).fetchone()[0]
        == 1
    )
    assert b.get("/v1/settings/aws/connect").json()["connections"][0]["account_id"] == ACCT
    # B1: the S3 backend's settings are still the deployment's, not this row.
    from omnifile_service.backends.s3.config import AwsDeployment

    dep = AwsDeployment.from_env(AWS)
    assert dep.accounts[ACCT].role_arn == f"arn:aws:iam::{ACCT}:role/omnifile-service"


def test_fetch_checks_endpoints_first_then_the_worker_only_reads(web, sql, db_url, admin) -> None:
    _w, b = admin
    web.services.settings_env = dict(AWS_ENV)
    body = {"account_id": ACCT, "region": "eu-west-1", "deployment_id": "omnifile"}
    r = b.post("/v1/settings/aws/connect/fetch", {**body, "endpoints": {
        "sts": "https://sts.eu-west-1.amazonaws.com"}}, check=False)  # fmt: skip
    assert r.status_code == 400 and "interface endpoint" in r.text
    eps = {"s3": "https://bucket.vpce-0abc-1234.s3.eu-west-1.vpce.amazonaws.com",
           "sts": "https://vpce-0123-5678.sts.eu-west-1.vpce.amazonaws.com",
           "sqs": "https://vpce-0def-9abc.sqs.eu-west-1.vpce.amazonaws.com"}  # fmt: skip
    r = b.post("/v1/settings/aws/connect/fetch", {**body, "endpoints": eps})
    assert r.status_code == 202, r.text
    tid = r.json()["task_id"]
    calls: list[str] = []

    class Client:
        def __init__(self, answers):
            self.answers = answers

        def __getattr__(self, name):
            async def call(**kw):
                calls.append(name)
                got = self.answers[name]
                return got(**kw) if callable(got) else got

            return call

    s3 = Client({
        "list_buckets": {"Buckets": [{"Name": "acme-wells"}, {"Name": "acme-logs"},
                                     {"Name": f"omnifile-ops-omnifile-{ACCT}"}]},
        "get_bucket_location": {"LocationConstraint": "eu-west-1"},
        "get_bucket_versioning": {"Status": "Enabled"},
        "get_bucket_notification_configuration":
            lambda Bucket: {"EventBridgeConfiguration": {}} if Bucket == "acme-wells" else {},
    })  # fmt: skip
    sqs = Client({"get_queue_attributes": {"Attributes": {"QueueArn": "arn"}}})

    async def fetch(params, env):
        return await fetch_with((s3, sqs, None), params, env)

    run_task(db_url, tid, CloudTasks(fetch, None, lambda _b: None, AWS_ENV))
    t = b.get("/v1/settings/tasks/{task_id}", task_id=tid).json()
    assert t["state"] == "done", t
    res = t["result"]
    assert [x["name"] for x in res["buckets"]] == ["acme-wells", "acme-logs"]
    assert res["buckets"][0]["eventbridge"] is True and res["buckets"][1]["eventbridge"] is False
    assert res["entry"]["endpoints"]["sts"] == eps["sts"] and res["queue"]["ok"]
    reads = ("list_", "get_")
    assert calls and all(c.startswith(reads) for c in calls), calls
    assert "secret" not in json.dumps(t).lower()


# --- Storage Mover deployment ------------------------------------------------------------------


def run_task(db_url: str, task_id: str, tasks: CloudTasks) -> None:
    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(as_app(db_url, "worker")) as conn:
            async with conn.transaction():
                now = datetime.now(UTC)
                await tasks.handle(Message(1, 1, now, now, {"task_id": task_id}), conn, now)

    asyncio.run(go())


def test_storage_mover_deploys_once_azure_grants_the_worker(web, sql, db_url, admin, arm) -> None:
    _w, b = admin
    body = {"name": "omnifile-mover", "project": "migrations", "location": "westeurope"}
    r = b.post("/v1/settings/storage-mover/deploy", body, check=False)
    assert r.status_code == 422 and "Connect Azure" in r.text
    s = b.get("/v1/settings/storage-mover").json()
    assert s["can_deploy"]["ok"] is False and s["deployment"] is None and s["agent_steps"]

    sign_in(b, "list")
    _choose(b, ("rg-storage",), "rg-migration")
    sign_in(b, "grant")
    r = b.post("/v1/settings/storage-mover/deploy", body)
    assert r.status_code == 202, r.text
    tid = r.json()["task_id"]

    worker_arm = FakeArm()
    run_task(db_url, tid, CloudTasks(None, worker_arm, lambda _b: None, {}))  # type: ignore[arg-type]
    base = (f"{SUB}/resourceGroups/rg-migration/providers/Microsoft.StorageMover/"
            "storageMovers/omnifile-mover")  # fmt: skip
    assert worker_arm.calls == [f"PUT {base}", f"PUT {base}/projects/migrations", f"GET {base}"]
    assert worker_arm.bodies[base]["location"] == "westeurope"
    t = b.get("/v1/settings/tasks/{task_id}", task_id=tid).json()
    assert t["state"] == "done", t

    # The page reads the new mover's agents until one registers.
    web.services.probes = FakeProbes(arm={
        base: {"properties": {"provisioningState": "Succeeded"}},
        f"{base}/agents": {"value": [{"name": "agent-dc1", "properties": {
            "agentStatus": "Online", "agentVersion": "2.1"}}]},
        f"{base}/projects": {"value": [{"name": "migrations", "properties": {}}]},
    })  # fmt: skip
    s = b.get("/v1/settings/storage-mover").json()
    d = s["deployment"]
    assert d["state"] == "done" and d["registration"]["storage_mover"] == "omnifile-mover"
    assert json.loads(d["config_value"])["project"] == "migrations"
    assert s["agents"][0]["status"] == "Online" and s["configured"] is False


def test_a_refused_deployment_says_what_the_worker_needs() -> None:
    arm = FakeArm(fail_once={})
    base = (f"{SUB}/resourceGroups/rg-migration/providers/Microsoft.StorageMover/"
            "storageMovers/m1")  # fmt: skip
    arm.fail_once[base] = ArmError(403, "AuthorizationFailed", "no")
    p = {"subscription_id": DEMO_SUBSCRIPTION, "resource_group": "rg-migration", "name": "m1",
         "project": "p1", "location": "westeurope"}  # fmt: skip
    from omnifile_service.settings.cloud_jobs import Failed

    with pytest.raises(Failed, match="Mover Deployer"):
        asyncio.run(deploy_with(arm, p))


# --- source scans ----------------------------------------------------------------------------


def _s3_source(sql, bucket: str = "acme-wells") -> int:
    (bid,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 's3', %s, true) RETURNING backend_id",
        (f"s3-{bucket}", json.dumps({"account_id": ACCT, "region": "eu-west-1",
                                     "buckets": [bucket]})),
    ).fetchone()  # fmt: skip
    return bid


def test_a_scan_reports_progress_stops_at_its_cap_and_never_writes(web, sql, db_url, admin):
    from backend_contract.clock import ManualClock
    from s3.conftest import fake_token, private_config
    from s3.s3_fakes import FakeAws, Version

    from omnifile_service.backends.s3 import S3Backend

    _w, b = admin
    clock = ManualClock()
    fake = FakeAws(clock.now)
    fake.add_bucket("acme-wells", versioning=False)
    keys = fake.buckets["acme-wells"].keys
    for i in range(6):
        keys[f"wells/2019/line_{i}.segy"] = [Version("null", b"x" * 10, "DEEP_ARCHIVE")]
    for i in range(4):
        keys[f"wells/logs/log_{i}.txt"] = [Version("null", b"x" * 3, "STANDARD")]
    backend = S3Backend(
        1, private_config(fake, buckets=("acme-wells",), owned_buckets=(), list_page_size=3),
        clock=clock, on_client=fake.attach, token_provider=fake_token,
    )  # fmt: skip
    bid = _s3_source(sql)
    with sql.cursor() as cur:
        sql_s3 = cur.execute("SELECT count(*) FROM catalog.backend").fetchone()
    assert sql_s3
    r = b.post("/v1/source-scans", {"backend_id": str(bid), "bucket": "nope-bucket",
                                    "prefix": "wells/"}, check=False)  # fmt: skip
    assert r.status_code == 400
    r = b.post("/v1/source-scans", {"backend_id": str(bid), "bucket": "acme-wells",
                                    "prefix": "wells/"})  # fmt: skip
    assert r.status_code == 202 and r.json()["state"] == "pending", r.text
    sid = r.json()["scan_id"]

    from omnifile_service.migrate import scan as scanning

    old = scanning.BATCH
    scanning.BATCH = 4  # three batches over ten objects
    try:
        tasks = CloudTasks(None, None, lambda _b: backend, {})  # type: ignore[arg-type]
        run_task(db_url, sid, tasks)
        s = b.get("/v1/source-scans/{scan_id}", scan_id=sid).json()
        assert s["state"] == "running" and s["progress"]["objects"] >= 4 and s["result"] is None
        for _ in range(5):
            run_task(db_url, sid, tasks)
    finally:
        scanning.BATCH = old
    s = b.get("/v1/source-scans/{scan_id}", scan_id=sid).json()
    assert s["state"] == "done", s
    res = s["result"]
    assert res["objects"] == 10 and res["restore"]["objects"] == 6
    pre = {p["prefix"]: p for p in res["prefixes"]}
    assert pre["wells/2019/"]["restore_objects"] == 6 and pre["wells/logs/"]["restore_objects"] == 0
    assert res["restore"]["hours"] == 48.0 and res["restore"]["prices_set"] is False
    assert res["waves"][0]["copy_only"] and res["waves"][0]["prefixes"] == ["wells/logs/"]
    # Decision 40: only lists.
    assert set(fake.ops()) <= {"ListObjectsV2", "ListObjectVersions"}, fake.ops()

    # A cap stops it, and says so.
    r = b.post("/v1/source-scans", {"backend_id": str(bid), "bucket": "acme-wells",
                                    "prefix": "wells/", "max_objects": 1000})  # fmt: skip
    sid = r.json()["scan_id"]
    sql.execute(
        "UPDATE catalog.cloud_task SET params = params || '{\"max_objects\": 2}'"
        " WHERE task_id = %s",
        (int(sid),),
    )
    scanning.BATCH = 4
    try:
        run_task(db_url, sid, CloudTasks(None, None, lambda _b: backend, {}))  # type: ignore[arg-type]
    finally:
        scanning.BATCH = old
    s = b.get("/v1/source-scans/{scan_id}", scan_id=sid).json()
    assert s["state"] == "capped" and "cap" in s["detail"] and s["result"]["objects"] < 10


def test_restore_estimates_use_the_s3_price_table_and_promise_nothing_over_10_gib() -> None:
    from dataclasses import dataclass as dc

    from omnifile_service.backends.base import LogicalTier
    from omnifile_service.migrate import scan as scanning
    from omnifile_service.policy.cost import load_price_table

    @dc
    class Obj:
        key: str
        size: int
        native_class: str
        logical_tier: Any = LogicalTier.HOT

    t = scanning.Tally()
    gib = scanning.GiB
    t.add([Obj("raw/a", 11 * gib, "GLACIER"), Obj("raw/b", gib, "GLACIER"),
           Obj("raw/c", gib, "DEEP_ARCHIVE"), Obj("doc/d", 10, "STANDARD")], "")  # fmt: skip
    price = {"retrieval_per_gb": "0.0025", "read_per_10k": "0.25"}
    prices = load_price_table("s3", {
        "backend_kind": "s3", "as_of": "2026-11-01", "verify": True, "currency": "USD",
        "classes": {"GLACIER": price, "DEEP_ARCHIVE": price}})  # fmt: skip
    res = scanning.result(t, prices)
    r = res["restore"]
    assert r["objects"] == 3 and r["waves"] == 1 and r["prices_set"]
    # 13 GiB * 0.0025 + 3/10000 * 0.25
    assert r["cost"] == "0.03" and r["currency"] == "USD"
    assert res["large_objects"] == {"objects": 1, "bytes": 11 * gib}
    assert any("10 GiB" in n for n in r["notes"])
    assert scanning.result(t)["restore"]["cost"] is None


def test_waves_pack_prefixes_under_the_object_and_byte_limits() -> None:
    from omnifile_service.migrate import scan as scanning

    big = scanning.WAVE_MAX // 2 + 1
    rows = [
        {"prefix": "a/", "objects": big, "bytes": 1, "restore_objects": big, "restore_bytes": 1},
        {"prefix": "b/", "objects": big, "bytes": 1, "restore_objects": big, "restore_bytes": 1},
        {"prefix": "c/", "objects": 5, "bytes": 1, "restore_objects": 0, "restore_bytes": 0},
        {"prefix": "d/", "objects": 1, "bytes": scanning.WAVE_BYTES, "restore_objects": 1,
         "restore_bytes": scanning.WAVE_BYTES},
    ]  # fmt: skip
    waves = scanning.suggest_waves(rows)
    assert [w["prefixes"] for w in waves] == [["c/"], ["a/"], ["b/"], ["d/"]]
    assert waves[0]["copy_only"] and not waves[1]["copy_only"]


def test_cloud_tasks_belong_to_the_worker_queue(sql) -> None:
    with sql.cursor(row_factory=dict_row) as cur:
        q = cur.execute(
            "SELECT queue_name FROM pgmq.meta WHERE queue_name LIKE 'cloud_task%%'"
        ).fetchall()
    assert {r["queue_name"] for r in q} == {"cloud_task", "cloud_task_dlq"}


def test_a_cloud_task_holds_no_id_while_it_runs(web, sql, db_url, admin, arm) -> None:
    """An ARM deployment takes minutes, and its row lock held every edge's feed back."""
    from omnifile_service.settings.cloud_jobs import CALL_HIDE_SECONDS

    _w, b = admin
    sign_in(b, "list")
    _choose(b, ("rg-storage",), "rg-migration")
    sign_in(b, "grant")
    body = {"name": "omnifile-mover", "project": "migrations", "location": "westeurope"}
    sql.execute("DELETE FROM pgmq.q_cloud_task")
    r = b.post("/v1/settings/storage-mover/deploy", body)
    assert r.status_code == 202, r.text
    seen = []
    hidden: list[float] = []

    class Watching:
        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            attr = getattr(worker_arm, name)
            if not callable(attr):
                return attr

            def called(*a, **k):  # type: ignore[no-untyped-def]
                seen.append(look(db_url, name))
                (left,) = sql.execute(
                    "SELECT extract(epoch FROM vt - now()) FROM pgmq.q_cloud_task"
                ).fetchone()
                hidden.append(float(left))
                return attr(*a, **k)

            return called

    worker_arm = FakeArm()
    tasks = CloudTasks(None, Watching(), lambda _b: None, {})  # type: ignore[arg-type]

    async def handler(msg, conn) -> None:  # type: ignore[no-untyped-def]
        await tasks.handle(msg, conn, datetime.now(UTC))

    stats = drain_queue(db_url, "cloud_task", handler)
    assert stats.dead == 0, stats.errors
    assert seen and [s for s in seen if s.ids] == []
    assert hidden and 3000 < hidden[0] <= CALL_HIDE_SECONDS


def test_a_source_scan_holds_no_id_across_its_read(web, sql, db_url, admin) -> None:
    """Its row lock did, across a listing of the whole batch."""
    from backend_contract.clock import ManualClock
    from s3.conftest import fake_token, private_config
    from s3.s3_fakes import FakeAws, Version

    from omnifile_service.backends.s3 import S3Backend

    _w, b = admin
    clock = ManualClock()
    fake = FakeAws(clock.now)
    fake.add_bucket("acme-scan", versioning=False)
    fake.buckets["acme-scan"].keys["wells/a.segy"] = [Version("null", b"x" * 10, "STANDARD")]
    backend = S3Backend(
        1, private_config(fake, buckets=("acme-scan",), owned_buckets=(), list_page_size=3),
        clock=clock, on_client=fake.attach, token_provider=fake_token,
    )  # fmt: skip
    bid = _s3_source(sql, "acme-scan")
    sql.execute("DELETE FROM pgmq.q_cloud_task")
    r = b.post("/v1/source-scans", {"backend_id": str(bid), "bucket": "acme-scan",
                                    "prefix": "wells/"})  # fmt: skip
    assert r.status_code == 202, r.text
    hidden: list[float] = []

    def how_long(_call: str) -> None:
        (left,) = sql.execute(
            "SELECT extract(epoch FROM vt - now()) FROM pgmq.q_cloud_task"
        ).fetchone()
        hidden.append(float(left))

    provider = Watched(backend, db_url, how_long)
    tasks = CloudTasks(None, None, lambda _b: provider, {})  # type: ignore[arg-type]

    async def handler(msg, conn) -> None:  # type: ignore[no-untyped-def]
        await tasks.handle(msg, conn, datetime.now(UTC))

    stats = drain_queue(db_url, "cloud_task", handler)
    assert stats.dead == 0, stats.errors
    assert provider.seen and provider.with_ids() == [] and provider.with_locks() == []
    assert hidden and hidden[0] > 3000, "a read can outlast a minute's visibility"
