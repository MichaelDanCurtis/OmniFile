"""Who sees prices, and the price editor's list prices (portal-api.md 5.8, 5.24; WP18)."""

from __future__ import annotations

from contextlib import contextmanager

from omnifile_service.web.prices import scrub

from .conftest import seed
from .test_policies import GOOD

ALL = ["ArchiveAdmin", "DataSteward", "others"]


@contextmanager
def _shown_to(admin, roles):  # type: ignore[no-untyped-def]
    """Set who sees prices, and put it back: the setting is one row for every test."""
    cur = admin.get("/v1/settings/prices")
    r = admin.call("PUT", "/v1/settings/prices", json={"show_to": roles},
                   headers={"If-Match": cur.headers["etag"]})  # fmt: skip
    assert r.status_code == 200, r.text
    try:
        yield r
    finally:
        cur = admin.get("/v1/settings/prices")
        admin.call("PUT", "/v1/settings/prices", json={"show_to": ALL},
                   headers={"If-Match": cur.headers["etag"]})  # fmt: skip


def test_only_signed_in_admins_read_and_change_it(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    for user in (w.steward, w.owner, w.reader):
        b = web.browser(user)
        assert b.get("/v1/settings/prices").status_code == 403
        r = b.call("PUT", "/v1/settings/prices", json={"show_to": []},
                   headers={"If-Match": '"1"'}, check=False)  # fmt: skip
        assert r.status_code == 403 and r.json()["code"] == "role_required"
    admin = web.browser(w.admin)
    r = admin.get("/v1/settings/prices")
    assert r.status_code == 200 and r.json()["show_to"] == ALL
    etag = r.headers["etag"]
    r = admin.call("PUT", "/v1/settings/prices", json={"show_to": ["ArchiveAdmin"]})
    assert r.status_code == 428
    for bad in (["Readers"], ["ArchiveAdmin", "ArchiveAdmin"], "ArchiveAdmin"):
        r = admin.call("PUT", "/v1/settings/prices", json={"show_to": bad},
                       headers={"If-Match": etag}, check=False)  # fmt: skip
        assert r.status_code == 400, bad
    r = admin.call("PUT", "/v1/settings/prices", json={"show_to": ALL, "x": 1},
                   headers={"If-Match": etag}, check=False)  # fmt: skip
    assert r.status_code == 400


def test_a_change_is_audited_and_stale_writes_refused(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    admin = web.browser(w.admin)
    before = admin.get("/v1/settings/prices").headers["etag"]
    with _shown_to(admin, ["DataSteward", "ArchiveAdmin"]) as r:
        assert r.json()["show_to"] == ["ArchiveAdmin", "DataSteward"]  # in role order
        assert r.json()["set_by"] == "Admin" and r.json()["set_at"]
        stale = admin.call("PUT", "/v1/settings/prices", json={"show_to": []},
                           headers={"If-Match": before})  # fmt: skip
        assert stale.status_code == 412
        row = sql.execute(
            "SELECT details FROM catalog.audit_log WHERE action = 'settings.prices.visibility'"
            " ORDER BY at DESC, audit_id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == {"from": ALL, "to": ["ArchiveAdmin", "DataSteward"]}


def test_hidden_prices_leave_no_amount_in_any_answer(web, sql) -> None:  # type: ignore[no-untyped-def]
    w = seed(sql)
    admin = web.browser(w.admin)
    steward = web.browser(w.steward)
    body = {"dataset_id": str(w.dataset_id), "body": GOOD}
    shown = steward.post("/v1/policies/preview", body).json()
    assert shown["cost"]["per_tb_first_year"]["currency"] == "USD"
    assert steward.get("/v1/me").json()["prices_visible"] is True
    with _shown_to(admin, ["ArchiveAdmin"]):
        r = steward.post("/v1/policies/preview", body)
        assert r.status_code == 200, r.text
        cost = r.json()["cost"]
        assert cost["per_tb_first_year"] is None and cost["keep_hot_per_tb_first_year"] is None
        assert all(i["amount"] is None for i in cost["breakdown"])
        assert '"currency"' not in r.text  # no money object anywhere
        assert r.json()["timeline"] == shown["timeline"]  # everything else is unchanged
        assert steward.get("/v1/me").json()["prices_visible"] is False
        r = steward.get("/v1/price-tables/{backend_kind}", backend_kind="azure_blob")
        assert r.status_code == 403 and r.json()["code"] == "forbidden"
        # The admins still see them.
        a = admin.post("/v1/policies/preview", body).json()
        assert a["cost"]["per_tb_first_year"]["currency"] == "USD"
        assert admin.get("/v1/me").json()["prices_visible"] is True
    with _shown_to(admin, []):
        # Nobody sees costs, but the ArchiveAdmins' editor still reads the rates.
        assert admin.get("/v1/me").json()["prices_visible"] is False
        assert admin.post("/v1/policies/preview", body).json()["cost"]["per_tb_first_year"] is None
        r = admin.get("/v1/price-tables/{backend_kind}", backend_kind="azure_blob")
        assert r.status_code == 200 and r.json()["classes"]
        # Partner applications keep their contract's amounts.
        r = web.call("POST", "/v1/policies/preview", json=body,
                     token=web.app_token(["Policy.Propose"]))  # fmt: skip
        assert r.status_code == 200, r.text
        assert r.json()["cost"]["per_tb_first_year"]["currency"] == "USD"
    assert steward.post("/v1/policies/preview", body).json()["cost"]["per_tb_first_year"]


def test_scrub_removes_every_money_object() -> None:
    m = {"amount": "1.00", "currency": "USD"}
    body = {"a": m, "b": [m, {"amount": "2", "currency": "EUR", "high": "3"}], "c": {"d": m},
            "keep": {"amount": 3, "currency": "USD"}, "n": [1, {"x": m}]}  # fmt: skip
    want = {"a": None, "b": [], "c": {"d": None}, "keep": {"amount": 3, "currency": "USD"},
            "n": [1, {"x": None}]}  # fmt: skip
    assert scrub(body) == want


def test_the_editor_gets_list_prices_and_a_put_ignores_them(web, sql) -> None:  # type: ignore[no-untyped-def]
    admin = web.browser(seed(sql).admin)
    r = admin.get("/v1/price-tables/{backend_kind}", backend_kind="sim")
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["list"]["classes"]["Hot"]["gb_month"] == t["classes"]["Hot"]["gb_month"]
    etag = r.headers["etag"]
    edit = {k: v for k, v in t.items() if k not in ("overridden", "etag", "id")}
    edit["classes"] = {**t["classes"], "Hot": {**t["classes"]["Hot"], "gb_month": "0.0100"}}
    r = admin.call("PUT", "/v1/price-tables/{backend_kind}", json=edit,
                   headers={"If-Match": etag}, backend_kind="sim")  # fmt: skip
    try:
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["classes"]["Hot"]["gb_month"] == "0.01"
        # The list price stays the built-in one, and isn't stored in the override.
        assert got["list"]["classes"]["Hot"]["gb_month"] == t["list"]["classes"]["Hot"]["gb_month"]
        (stored,) = sql.execute(
            "SELECT body FROM catalog.price_table WHERE backend_kind = 'sim' AND region = ''"
        ).fetchone()
        assert "list" not in stored
    finally:
        cur = admin.get("/v1/price-tables/{backend_kind}", backend_kind="sim")
        admin.call("DELETE", "/v1/price-tables/{backend_kind}",
                   headers={"If-Match": cur.headers["etag"]}, backend_kind="sim")  # fmt: skip


def test_a_huge_rate_is_refused_not_a_500(web, sql) -> None:  # type: ignore[no-untyped-def]
    """#63 review: a rate like 1e400 made the PUT fail with 500."""
    admin = web.browser(seed(sql).admin)
    r = admin.get("/v1/price-tables/{backend_kind}", backend_kind="sim")
    t, etag = r.json(), r.headers["etag"]
    base = {k: v for k, v in t.items() if k not in ("overridden", "etag", "id", "list")}
    for huge in ("1e400", "1" + "0" * 40, "123456789.5"):
        edit = {**base, "classes": {**t["classes"], "Hot": {**t["classes"]["Hot"],
                                                            "gb_month": huge}}}  # fmt: skip
        r = admin.call("PUT", "/v1/price-tables/{backend_kind}", json=edit,
                       headers={"If-Match": etag}, backend_kind="sim", check=False)  # fmt: skip
        assert r.status_code == 400, (huge, r.text)
        assert "gb_month" in r.json()["detail"]
    assert (
        admin.get("/v1/price-tables/{backend_kind}", backend_kind="sim").json()["overridden"]
        is False
    )


def test_a_refusal_carries_no_amount_for_a_hidden_role(web, sql, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """#63 review: policy_invalid's checks[].fee is money in a 4xx answer."""
    w = seed(sql)
    admin = web.browser(w.admin)
    fee = {"amount": "12.50", "currency": "USD"}

    def preview(body, facts):  # type: ignore[no-untyped-def]
        check = {"severity": "error", "code": "early_deletion", "message": "fee", "field": None}
        return {"valid": False, "checks": [{**check, "fee": fee}]}

    monkeypatch.setattr(web.services.preview, "policy_preview", preview)
    path = "/v1/datasets/{dataset_id}/policies"
    body = {"body": GOOD, "base_version": None}
    r = admin.post(path, body, dataset_id=w.dataset_id, check=False)
    assert r.status_code == 422 and r.json()["checks"][0]["fee"] == fee
    with _shown_to(admin, []):
        r = admin.post(path, body, dataset_id=w.dataset_id, check=False)
        assert r.status_code == 422 and r.json()["code"] == "policy_invalid"
        assert r.json()["checks"][0]["fee"] is None and "12.50" not in r.text
