"""Settings, Storage (portal-api.md 5.20): the CLI's code path, private
networking always, and the live checks over fake DNS and ARM."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnifile_service.settings.fakes import FakeProbes
from omnifile_service.settings.ports import ProbeError

from .conftest import seed

ACCT = "123456789012"
AUDIENCE = "api://omnifile-test-service"
AWS_ACCOUNT: dict[str, Any] = {
    "region": "eu-west-1",
    "endpoints": {
        "s3": "https://bucket.vpce-0abc-1234.s3.eu-west-1.vpce.amazonaws.com",
        "sts": "https://vpce-0123.sts.eu-west-1.vpce.amazonaws.com",
        "sqs": "https://vpce-0def.sqs.eu-west-1.vpce.amazonaws.com",
    },
    "role_arn": f"arn:aws:iam::{ACCT}:role/omnifile-service",
    "event_queues": {"*": f"https://sqs.eu-west-1.amazonaws.com/{ACCT}/omnifile-events"},
    "staging_bucket": "omni-ops",
}
AWS = {
    "OMNIFILE_AWS_ACCOUNTS": json.dumps({ACCT: AWS_ACCOUNT}),
    "OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE": AUDIENCE,
}
SUB = "00000000-0000-4000-8000-0000000000aa"
WORKER = "11111111-1111-4111-8111-111111111111"


def _uniq(sql) -> str:
    n = sql.execute("SELECT count(*) FROM catalog.backend").fetchone()[0]
    return f"{n:04d}"


@pytest.fixture
def admin(web, sql):
    w = seed(sql)
    return w, web.browser(w.admin)


def _azure(live: str, archive: str, **kw: Any) -> dict[str, Any]:
    return {"kind": "azure", "live": live, "archive": archive, **kw}


def test_storage_lists_backends_and_is_for_admins(web, sql, admin) -> None:
    w, b = admin
    r = b.get("/v1/settings/storage")
    assert r.status_code == 200, r.text
    body = r.json()
    assert any(x["backend_id"] == str(w.backend_id) for x in body["backends"])
    assert body["dev_mode"] is True and body["aws_configured"] is False
    for who in (w.steward, w.reader):
        r = web.browser(who).get("/v1/settings/storage")
        assert r.status_code == 403 and r.json()["code"] == "role_required"
    # Never an app.
    r = web.call("GET", "/v1/settings/storage", token=web.app_token(["Archive.Read"]))
    assert r.status_code == 401


def test_add_and_edit_an_azure_backend_through_the_clis_code(web, sql, admin) -> None:
    w, b = admin
    u = _uniq(sql)
    name = f"azure-{u}"
    draft = _azure(f"live{u}", f"arch{u}", name=name, region="westeurope")
    r = b.post("/v1/settings/storage", draft)
    assert r.status_code == 201, r.text
    got = r.json()
    assert got["outcome"] == "added" and got["backend"]["private_network"] is True
    assert got["backend"]["config"]["archive_account"] == f"arch{u}"
    bid = got["backend"]["backend_id"]
    # The same answers again: unchanged (idempotent by name, as the CLI).
    again = b.post("/v1/settings/storage", draft)
    assert again.status_code == 200 and again.json()["outcome"] == "unchanged"
    # Edit: the region changes; the row keeps its name and kind.
    r = b.call(
        "PUT",
        "/v1/settings/storage/{backend_id}",
        json={**draft, "region": "northeurope"},
        backend_id=bid,
    )
    assert r.status_code == 200 and r.json()["outcome"] == "updated", r.text
    assert any("region" in c for c in r.json()["changes"])
    r = b.call(
        "PUT",
        "/v1/settings/storage/{backend_id}",
        json={"kind": "s3", "account": ACCT, "region": "eu-west-1", "buckets": ["seismic"]},
        backend_id=bid,
    )
    assert r.status_code == 400 and "never changes kind" in r.json()["detail"]
    # Audited with the person, through admin.backends.register.
    row = sql.execute(
        "SELECT actor, actor_sid, details->>'name' FROM catalog.audit_log"
        " WHERE action = 'backend.add' ORDER BY audit_id DESC LIMIT 1"
    ).fetchone()
    assert row[0].startswith("user:") and row[1] == w.admin["sid"] and row[2] == name


def test_the_clis_refusals_are_the_pages(web, sql, admin) -> None:
    _w, b = admin
    r = b.post("/v1/settings/storage", _azure("same01", "same01"))
    assert r.status_code == 400
    assert r.json()["errors"][0]["field"] == "live" and "two accounts" in r.json()["detail"]
    r = b.post("/v1/settings/storage", {"kind": "azure", "account": "worm01", "smart": True})
    assert r.status_code == 400 and "Smart needs the two-account layout" in r.json()["detail"]
    # There is no public option at all.
    r = b.post("/v1/settings/storage", {**_azure("pub01", "pub02"), "public": True}, check=False)
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "public"
    # S3 without the service's AWS settings: the CLI's message.
    r = b.post(
        "/v1/settings/storage",
        {"kind": "s3", "account": ACCT, "region": "eu-west-1", "buckets": ["seismic-raw"]},
    )
    assert r.status_code == 400 and "AWS settings" in r.json()["detail"]


def test_adding_storage_needs_a_recent_sign_in(web, sql) -> None:
    w = seed(sql)
    stale = web.browser(w.user("admin", roles=["ArchiveAdmin"], sid_rid=3003, no_auth_time=True))
    r = stale.post("/v1/settings/storage", _azure("rea01", "rea02"))
    assert r.status_code == 401 and r.json()["code"] == "reauth_required"


def test_an_s3_backend_is_checked_against_the_aws_settings(web, sql, admin) -> None:
    _w, b = admin
    web.services.settings_env = dict(AWS)
    u = _uniq(sql)
    r = b.post(
        "/v1/settings/storage",
        {
            "kind": "s3",
            "name": f"s3-{u}",
            "account": ACCT,
            "region": "eu-west-1",
            "buckets": ["seismic-raw"],
        },
    )
    assert r.status_code == 201, r.text
    bid = r.json()["backend"]["backend_id"]
    assert r.json()["backend"]["private_network"] is True
    # OmniFile's own operations bucket is never a source (decision 40).
    r = b.post(
        "/v1/settings/storage",
        {
            "kind": "s3",
            "name": f"s3o-{u}",
            "account": ACCT,
            "region": "eu-west-1",
            "buckets": ["omni-ops"],
        },
    )
    assert r.status_code == 400 and "operations bucket" in r.json()["detail"]
    # Checks: the settings, the interface endpoints' DNS, and the role's last test.
    web.services.probes = FakeProbes(
        dns={
            "bucket.vpce-0abc-1234.s3.eu-west-1.vpce.amazonaws.com": ["10.40.0.5"],
            "vpce-0123.sts.eu-west-1.vpce.amazonaws.com": ["52.94.1.1"],
        }
    )
    r = b.get("/v1/settings/storage/{backend_id}/checks", backend_id=bid)
    assert r.status_code == 200, r.text
    by = {(c["code"], c["title"]): c["status"] for c in r.json()["checks"]}
    assert by[("aws_settings", "The service's AWS settings")] == "ok"
    assert by[("private_dns", "Private DNS for S3")] == "ok"
    assert by[("private_dns", "Private DNS for STS")] == "failed"  # a public address
    assert by[("assume_role", "The service's AWS role")] == "unknown"


def _arm(
    probes: FakeProbes,
    live: str,
    arch: str,
    *,
    roles=("OmniFile Blob Worker (rg)",),
    feed: bool = True,
    live_sku: str = "Standard_ZRS",
    public: str = "Disabled",
) -> None:
    rg = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts"
    probes.arm[
        f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts"
    ] = {
        "value": [
            {
                "name": live,
                "id": f"{rg}/{live}",
                "sku": {"name": live_sku},
                "properties": {"publicNetworkAccess": public},
            },
            {
                "name": arch,
                "id": f"{rg}/{arch}",
                "sku": {"name": "Standard_GRS"},
                "properties": {"publicNetworkAccess": "Disabled"},
            },
        ]
    }
    for acct in (live, arch):
        rd = f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/r-{acct}"
        probes.arm[
            f"{rg}/{acct}/providers/Microsoft.Authorization/roleAssignments"
            f"?$filter=assignedTo('{WORKER}')"
        ] = {"value": [{"properties": {"roleDefinitionId": rd}}] if roles else []}
        # Read at the account's scope, where web's reader role reaches a custom role.
        probes.arm[f"{rg}/{acct}/providers/Microsoft.Authorization/roleDefinitions/r-{acct}"] = {
            "properties": {"roleName": roles[0] if roles else "Reader"}
        }
        probes.arm[f"{rg}/{acct}/blobServices/default"] = {
            "properties": {"changeFeed": {"enabled": feed}, "isVersioningEnabled": True}
        }


def test_azure_checks_private_dns_rbac_pair_and_change_feed(web, sql, admin) -> None:
    _w, b = admin
    u = _uniq(sql)
    live, arch = f"chk{u}l", f"chk{u}a"
    r = b.post("/v1/settings/storage", _azure(live, arch, name=f"chk-{u}"))
    bid = r.json()["backend"]["backend_id"]
    # Without probes: nothing goes out, and the page says so.
    r = b.get("/v1/settings/storage/{backend_id}/checks", backend_id=bid)
    assert {c["code"]: c["status"] for c in r.json()["checks"]}["probes"] == "not_configured"

    probes = FakeProbes(
        dns={
            f"{live}.blob.core.windows.net": ["10.1.0.4"],
            f"{arch}.blob.core.windows.net": ["10.1.0.5"],
        }
    )
    _arm(probes, live, arch)
    web.services.probes = probes
    web.services.settings_env = {
        "OMNIFILE_AZURE_SUBSCRIPTION_ID": SUB,
        "OMNIFILE_AZURE_RESOURCE_GROUP": "rg",
        "OMNIFILE_SERVICE_PRINCIPAL_IDS": f"worker={WORKER}",
    }
    checks = b.get("/v1/settings/storage/{backend_id}/checks", backend_id=bid).json()["checks"]
    bad = [c for c in checks if c["status"] != "ok"]
    assert not bad, bad
    assert {c["code"] for c in checks} >= {
        "private_dns",
        "rbac_role",
        "public_access",
        "change_feed",
        "archive_pair",
    }

    # Decision 22: a public answer for a private backend fails, with what to fix.
    probes.dns[f"{live}.blob.core.windows.net"] = ["20.60.1.1"]
    _arm(probes, live, arch, roles=(), feed=False, live_sku="Standard_LRS", public="Enabled")
    checks = b.get("/v1/settings/storage/{backend_id}/checks", backend_id=bid).json()["checks"]
    st = {}
    for c in checks:
        st.setdefault(c["code"], set()).add(c["status"])
    assert "failed" in st["private_dns"] and "failed" in st["rbac_role"]
    assert "failed" in st["change_feed"] and st["archive_pair"] == {"failed"}
    assert "warning" in st["public_access"]
    assert all(c["fix"] for c in checks if c["status"] == "failed")

    # ARM refusing (no Reader) is an unknown, not a pass.
    probes.arm[
        f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts"
    ] = ProbeError("answered 403 AuthorizationFailed", 403)
    checks = b.get("/v1/settings/storage/{backend_id}/checks", backend_id=bid).json()["checks"]
    assert {c["code"]: c["status"] for c in checks}["rbac_role"] == "unknown"


def test_checks_of_a_sim_backend_and_an_unknown_one(web, sql, admin) -> None:
    w, b = admin
    r = b.get("/v1/settings/storage/{backend_id}/checks", backend_id=w.backend_id)
    assert r.status_code == 200 and r.json()["checks"][-1]["code"] == "kind"
    r = b.get("/v1/settings/storage/{backend_id}/checks", backend_id="999999999")
    assert r.status_code == 404


def test_the_catalog_never_turns_private_networking_off(sql, admin) -> None:
    w, _b = admin
    import psycopg

    with pytest.raises(psycopg.Error, match="never turns it off"):
        sql.execute(
            "UPDATE catalog.backend SET private_network = false WHERE backend_id = %s",
            (w.backend_id,),
        )
    with pytest.raises(psycopg.Error, match="never changes kind"):
        sql.execute("UPDATE catalog.backend SET kind = 's3' WHERE backend_id = %s", (w.backend_id,))
