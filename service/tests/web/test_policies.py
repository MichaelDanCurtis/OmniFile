"""Policies, the live preview and proposals: nothing applies itself
(anti-pattern 7). Price tables and the audit log."""

from __future__ import annotations

from .conftest import add_file, seed

GOOD = {"mode": "scheduled", "clock": "last_read", "warm_day": 30, "cold_day": 120,
        "archive_day": 365, "stub_at": "archive",
        "notify": {"requester": True, "owners": False, "email_fallback": True}}  # fmt: skip
BAD = {**GOOD, "cold_day": 10}


def _active(sql, w):
    return sql.execute(
        "SELECT version, body, proposal_id FROM catalog.policy"
        " WHERE dataset_id = %s AND state = 'active'",
        (w.dataset_id,),
    ).fetchone()


def test_datasets_and_preview(web, sql):
    w = seed(sql)
    admin = web.browser(w.admin)
    # The whole session's datasets can pass one default page (100): ask for all of them.
    r = admin.get("/v1/datasets", params={"limit": 500})
    assert r.status_code == 200, r.text
    mine = next(d for d in r.json()["items"] if d["dataset_id"] == str(w.dataset_id))
    assert mine["storage_layout"] == "two_account" and mine["active_policy"] is None
    assert mine["shares"][0]["share_id"] == str(w.share_id)
    assert web.browser(w.reader).get("/v1/datasets").status_code == 403
    r = admin.post("/v1/policies/preview", {"dataset_id": str(w.dataset_id), "body": GOOD,
                                            "assumptions": {"tb": 2}})  # fmt: skip
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["valid"] is True and p["timeline"]["segments"]
    assert p["cost"]["per_tb_first_year"]["currency"] == "USD"
    r = admin.post("/v1/policies/preview", {"dataset_id": str(w.dataset_id), "body": BAD})
    assert r.status_code == 200 and r.json()["valid"] is False
    assert any(c["code"] == "days_not_increasing" for c in r.json()["checks"])
    r = admin.post(
        "/v1/policies/preview",
        {
            "dataset_id": str(w.dataset_id),
            "body": GOOD,
            "share_overrides": [
                {"share_id": str(w.share_id), "on_open": "auto", "wait_threshold_minutes": 5}
            ],
        },
    )
    assert r.status_code == 200, r.text  # fmt: skip
    assert web.browser(w.reader).post("/v1/policies/preview", {
        "dataset_id": str(w.dataset_id), "body": GOOD}).status_code == 403  # fmt: skip


def test_create_policy_versions(web, sql):
    w = seed(sql)
    fid = add_file(sql, w, "a.txt")
    admin = web.browser(w.admin)
    path = "/v1/datasets/{dataset_id}/policies"
    r = admin.post(path, {"body": BAD, "base_version": None}, dataset_id=w.dataset_id)
    assert r.status_code == 422 and r.json()["code"] == "policy_invalid"
    assert any(c["severity"] == "error" for c in r.json()["checks"])
    r = admin.post(path, {"body": GOOD, "base_version": None, "note": "first"},
                   dataset_id=w.dataset_id)  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 1 and r.json()["state"] == "active"
    assert r.json()["approved_by"]["kind"] == "identity"
    # The scheduler re-plans the dataset.
    (at,) = sql.execute("SELECT next_action_at FROM catalog.file WHERE file_id = %s",
                        (fid,)).fetchone()  # fmt: skip
    assert at is not None
    # A stale base.
    r = admin.post(path, {"body": GOOD, "base_version": None}, dataset_id=w.dataset_id)
    assert r.status_code == 409 and r.json()["code"] == "version_conflict"
    r = admin.post(path, {"body": {**GOOD, "archive_day": 400}, "base_version": 1},
                   dataset_id=w.dataset_id)  # fmt: skip
    assert r.status_code == 201 and r.json()["version"] == 2
    hist = admin.get(path, dataset_id=w.dataset_id).json()["items"]
    assert [(p["version"], p["state"]) for p in hist] == [(2, "active"), (1, "superseded")]
    assert admin.get("/v1/datasets/{dataset_id}/policy",
                     dataset_id=w.dataset_id).json()["version"] == 2  # fmt: skip
    # Only an ArchiveAdmin saves a policy; Owners may read them.
    r = web.browser(w.steward).post(path, {"body": GOOD, "base_version": 2},
                                    dataset_id=w.dataset_id)  # fmt: skip
    assert r.status_code == 403
    assert web.browser(w.owner).get(path, dataset_id=w.dataset_id).status_code == 200
    assert web.browser(w.reader).get(path, dataset_id=w.dataset_id).status_code == 403


def test_smart_on_a_worm_dataset_is_a_layout_conflict_or_invalid(web, sql):
    w = seed(sql, layout="azure_worm")
    sql.execute("UPDATE catalog.share SET worm_enabled = true, worm_mode = 'unlocked_test',"
                " worm_retention_days = 30 WHERE share_id = %s", (w.share_id,))  # fmt: skip
    r = web.browser(w.admin).post("/v1/datasets/{dataset_id}/policies",
                                  {"body": {"mode": "smart", "archive_day": 200},
                                   "base_version": None}, dataset_id=w.dataset_id)  # fmt: skip
    assert r.status_code in (409, 422)
    assert r.json()["code"] in ("layout_conflict", "policy_invalid")


def test_a_proposal_changes_nothing_until_an_admin_approves(web, sql):
    w = seed(sql)
    admin = web.browser(w.admin)
    admin.post("/v1/datasets/{dataset_id}/policies", {"body": GOOD, "base_version": None},
               dataset_id=w.dataset_id)  # fmt: skip
    token = web.app_token(["Policy.Propose"])
    proposed = {**GOOD, "archive_day": 180}
    r = web.call(
        "POST",
        "/v1/policies/proposals",
        token=token,
        json={
            "dataset_id": str(w.dataset_id),
            "body": proposed,
            "rationale": "cold after 6 months",
        },
    )
    assert r.status_code == 201, r.text  # fmt: skip
    p = r.json()
    assert p["state"] == "pending" and p["base_version"] == 1 and p["stale"] is False
    assert p["proposed_by"] == {"kind": "application", "display_name": None,
                                "app_id": "prizmanyx-app", "identity_id": None}  # fmt: skip
    assert _active(sql, w)[0] == 1 and _active(sql, w)[1]["archive_day"] == 365
    # The app sees its own; it can't approve (bearer can't reach approve).
    mine = web.call("GET", "/v1/policies/proposals", token=token).json()["items"]
    assert [x["proposal_id"] for x in mine][0] == p["proposal_id"]
    r = web.call("POST", "/v1/policies/proposals/{proposal_id}/approve", token=token, json={},
                 proposal_id=p["proposal_id"])  # fmt: skip
    assert r.status_code == 401
    # Another app can't see it.
    other = web.idp.app_token("other-app", ["Policy.Propose"])
    r = web.call("GET", "/v1/policies/proposals/{proposal_id}", token=other,
                 proposal_id=p["proposal_id"])  # fmt: skip
    assert r.status_code == 404
    # The full view: diff and preview.
    full = admin.get("/v1/policies/proposals/{proposal_id}", proposal_id=p["proposal_id"]).json()
    assert full["diff"] == [{"field": "archive_day", "active": 365, "proposed": 180}]
    assert full["preview"]["valid"] is True
    # A DataSteward can't approve.
    r = web.browser(w.steward).post("/v1/policies/proposals/{proposal_id}/approve", {},
                                    proposal_id=p["proposal_id"])  # fmt: skip
    assert r.status_code == 403
    r = admin.post("/v1/policies/proposals/{proposal_id}/approve", {"note": "agreed"},
                   proposal_id=p["proposal_id"])  # fmt: skip
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["proposal"]["state"] == "approved" and out["policy"]["version"] == 2
    assert out["policy"]["proposal_id"] == p["proposal_id"]
    assert _active(sql, w)[1]["archive_day"] == 180
    r = admin.post("/v1/policies/proposals/{proposal_id}/approve", {},
                   proposal_id=p["proposal_id"])  # fmt: skip
    assert r.status_code == 409 and r.json()["code"] == "proposal_not_pending"


def test_no_self_approval_and_stale_proposals(web, sql):
    w = seed(sql)
    admin, admin2 = web.browser(w.admin), web.browser(w.admin2)
    admin.post("/v1/datasets/{dataset_id}/policies", {"body": GOOD, "base_version": None},
               dataset_id=w.dataset_id)  # fmt: skip
    r = admin.post("/v1/policies/proposals", {"dataset_id": str(w.dataset_id),
                                              "body": {**GOOD, "archive_day": 500},
                                              "rationale": "mine"})  # fmt: skip
    pid = r.json()["proposal_id"]
    r = admin.post("/v1/policies/proposals/{proposal_id}/approve", {}, proposal_id=pid)
    assert r.status_code == 403 and r.json()["code"] == "self_approval"
    # The catalog enforces it too.
    import psycopg

    identity = sql.execute("SELECT proposed_by_identity_id FROM catalog.policy_proposal"
                           " WHERE proposal_id = %s", (int(pid),)).fetchone()[0]  # fmt: skip
    try:
        sql.execute(
            "UPDATE catalog.policy_proposal SET state = 'approved', decided_at = now(),"
            " decided_by_identity_id = %s WHERE proposal_id = %s",
            (identity, int(pid)),
        )
        raise AssertionError("the catalog allowed self-approval")  # fmt: skip
    except psycopg.errors.CheckViolation:
        pass
    # The active policy moves on: the proposal is stale.
    admin2.post("/v1/datasets/{dataset_id}/policies", {"body": {**GOOD, "archive_day": 300},
                                                       "base_version": 1},
                dataset_id=w.dataset_id)  # fmt: skip
    assert admin2.get("/v1/policies/proposals/{proposal_id}", proposal_id=pid).json()["stale"]
    r = admin2.post("/v1/policies/proposals/{proposal_id}/approve", {}, proposal_id=pid)
    assert r.status_code == 409 and r.json()["code"] == "version_conflict"
    r = admin2.post("/v1/policies/proposals/{proposal_id}/approve", {"accept_stale": True},
                    proposal_id=pid)  # fmt: skip
    assert r.status_code == 200 and r.json()["policy"]["version"] == 3


def test_invalid_proposals_are_stored_but_never_approved(web, sql):
    w = seed(sql)
    steward = web.browser(w.steward)
    r = steward.post("/v1/policies/proposals", {"dataset_id": str(w.dataset_id), "body": BAD,
                                                "rationale": "try"})  # fmt: skip
    assert r.status_code == 201
    pid = r.json()["proposal_id"]
    r = web.browser(w.admin).post("/v1/policies/proposals/{proposal_id}/approve", {},
                                  proposal_id=pid)  # fmt: skip
    assert r.status_code == 422 and r.json()["code"] == "policy_invalid"
    assert _active(sql, w) is None


def test_reject_and_withdraw(web, sql):
    w = seed(sql)
    token = web.app_token(["Policy.Propose"])
    body = {"dataset_id": str(w.dataset_id), "body": GOOD, "rationale": "r"}
    a = web.call("POST", "/v1/policies/proposals", token=token, json=body).json()["proposal_id"]
    b = web.call("POST", "/v1/policies/proposals", token=token, json=body).json()["proposal_id"]
    admin = web.browser(w.admin)
    r = admin.post("/v1/policies/proposals/{proposal_id}/reject", {"note": "not now"},
                   proposal_id=a)  # fmt: skip
    assert r.status_code == 200 and r.json()["state"] == "rejected"
    assert r.json()["decision_note"] == "not now"
    r = admin.post("/v1/policies/proposals/{proposal_id}/withdraw", proposal_id=b)
    assert r.status_code == 403  # not the proposer
    r = web.call("POST", "/v1/policies/proposals/{proposal_id}/withdraw", token=token,
                 proposal_id=b)  # fmt: skip
    assert r.status_code == 200 and r.json()["state"] == "withdrawn"
    r = web.call("POST", "/v1/policies/proposals/{proposal_id}/withdraw", token=token,
                 proposal_id=a)  # fmt: skip
    assert r.status_code == 409


def test_idempotent_post_replays(web, sql):
    w = seed(sql)
    admin = web.browser(w.admin)
    body = {"dataset_id": str(w.dataset_id), "body": GOOD, "rationale": "r"}
    r1 = admin.post("/v1/policies/proposals", body, key="k" * 20)
    r2 = admin.post("/v1/policies/proposals", body, key="k" * 20)
    assert r2.status_code == 201 and r2.headers["idempotent-replayed"] == "true"
    assert r1.json()["proposal_id"] == r2.json()["proposal_id"]
    r3 = admin.post("/v1/policies/proposals", {**body, "rationale": "other"}, key="k" * 20)
    assert r3.status_code == 422 and r3.json()["code"] == "idempotency_key_reused"
    r4 = admin.post("/v1/policies/proposals", body, headers={"Idempotency-Key": "short"})
    assert r4.status_code == 400


def test_price_tables(web, sql):
    seed(sql)
    admin = web.browser(seed(sql).admin)
    r = admin.get("/v1/price-tables/{backend_kind}", backend_kind="azure_blob")
    assert r.status_code == 200, r.text
    builtin = r.json()
    assert builtin["overridden"] is False and r.headers["etag"] == '"0"'
    override = {k: v for k, v in builtin.items() if k not in ("overridden", "etag", "id")}
    override["notes"] = ["negotiated rates"]
    r = admin.call("PUT", "/v1/price-tables/{backend_kind}", json=override,
                   backend_kind="azure_blob")  # fmt: skip
    assert r.status_code == 428
    r = admin.call("PUT", "/v1/price-tables/{backend_kind}", json=override,
                   headers={"If-Match": '"0"'}, backend_kind="azure_blob")  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["overridden"] is True and r.headers["etag"] == '"1"'
    r = admin.call("PUT", "/v1/price-tables/{backend_kind}", json=override,
                   headers={"If-Match": '"0"'}, backend_kind="azure_blob")  # fmt: skip
    assert r.status_code == 412
    r = admin.call("PUT", "/v1/price-tables/{backend_kind}", json={**override,
                   "currency": "dollars"}, headers={"If-Match": '"1"'}, backend_kind="azure_blob",
                   check=False)  # fmt: skip
    assert r.status_code == 400
    r = web.browser(seed(sql).steward).get("/v1/price-tables/{backend_kind}",
                                           backend_kind="azure_blob")  # fmt: skip
    assert r.json()["overridden"] is True
    r = admin.call("DELETE", "/v1/price-tables/{backend_kind}", headers={"If-Match": '"1"'},
                   backend_kind="azure_blob")  # fmt: skip
    assert r.status_code == 200 and r.json()["overridden"] is False
    r = web.browser(seed(sql).owner).get("/v1/price-tables/{backend_kind}",
                                         backend_kind="azure_blob")  # fmt: skip
    assert r.status_code == 403


def test_audit_log(web, sql):
    w = seed(sql)
    admin = web.browser(w.admin)
    admin.post("/v1/policies/proposals", {"dataset_id": str(w.dataset_id), "body": GOOD,
                                          "rationale": "r"})  # fmt: skip
    owner = web.browser(w.owner)
    owner.call("PATCH", "/v1/shares/{share_id}/settings", json={"rewind_depth_days": 5},
               headers={"If-Match": owner.get("/v1/shares/{share_id}",
                                              share_id=w.share_id).headers["etag"]},
               share_id=w.share_id)  # fmt: skip
    r = admin.get("/v1/audit", params={"action": "policy.propose", "limit": 5})
    assert r.status_code == 200, r.text
    row = r.json()["items"][0]
    assert row["actor"].startswith("user:") and row["actor_display_name"] == "Admin"
    assert row["client_addr"] == "10.1.2.3"
    # Owners: only with their share_id, and without client addresses.
    assert owner.get("/v1/audit").status_code == 403
    rows = owner.get("/v1/audit", params={"share_id": str(w.share_id)}).json()["items"]
    assert rows and all(x["share_id"] == str(w.share_id) for x in rows)
    assert all(x["client_addr"] is None for x in rows)
    assert web.browser(w.reader).get("/v1/audit", params={
        "share_id": str(w.share_id)}).status_code == 403  # fmt: skip
    # Paging, newest first.
    page1 = admin.get("/v1/audit", params={"limit": 1}).json()
    page2 = admin.get("/v1/audit", params={"limit": 1, "cursor": page1["next_cursor"]}).json()
    assert page1["items"][0]["at"] >= page2["items"][0]["at"]
    assert page1["items"][0]["audit_id"] != page2["items"][0]["audit_id"]
