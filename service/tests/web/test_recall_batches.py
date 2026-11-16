"""Recall batches (portal-api.md section 5.19): plan paths or a dataset by a
due date, with an ETA and a cost; the worker starts the recalls. Only the
app that made a batch reads it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pooled
import psycopg
import pytest
from psycopg.rows import tuple_row

from omnifile_service.catalog.privileges import as_app
from omnifile_service.worker.pool import PooledConnect
from omnifile_service.worker.portal_jobs import handle_recall_batch

from .conftest import add_dir, add_file, seed


def _step(db_url: str, batch_id: str, pool: int | None = None) -> bool:
    """One worker step. With `pool` (or OMNIFILE_TEST_WORKER_POOL), on a connection from the
    worker's budget (worker/pool.py), as OMNIFILE_PG_POOL_MAX runs it: a `Lease`, given back
    after the step, and handed out again with tuple rows."""
    size = pool or pooled.budget()

    async def go() -> bool:
        url = as_app(db_url, "worker")
        connect = (
            (lambda: psycopg.AsyncConnection.connect(url, autocommit=True))
            if size is None
            else PooledConnect(url, size)
        )
        try:
            conn = await connect()
            try:
                async with conn.transaction():
                    more = await handle_recall_batch(conn, int(batch_id), datetime.now(UTC))
                assert conn.row_factory is tuple_row  # the step leaves the connection as it was
                return more
            finally:
                await conn.close()
        finally:
            if isinstance(connect, PooledConnect):
                await connect.close()

    return asyncio.run(go())


@pytest.mark.parametrize("pool", [None, 6], ids=["direct", "pooled"])
def test_a_batch_is_planned_started_and_read(web, sql, db_url, pool):
    w = seed(sql)
    raw = add_dir(sql, w, "raw")
    deep = add_dir(sql, w, "deep", parent=raw)
    a = add_file(sql, w, "a.segy", parent=raw, tier="archive", state="stub", size=2 * 2**30)
    b = add_file(sql, w, "b.segy", parent=deep, tier="archive", state="stub", size=2**30)
    online = add_file(sql, w, "c.segy", parent=raw, tier="hot")
    add_file(sql, w, "elsewhere.segy", tier="archive", state="stub")
    req = web.app_token(["Recall.Request"])
    due = (datetime.now(UTC) + timedelta(days=3)).isoformat().replace("+00:00", "Z")
    r = web.call("POST", "/v1/recall-batches", token=req,
                 json={"dataset_id": str(w.dataset_id), "paths": ["/raw", "/raw/deep"],
                       "due_by": due})  # fmt: skip
    assert r.status_code == 202, r.text
    batch = r.json()
    assert batch["state"] == "starting" and batch["paths"] == ["/raw", "/raw/deep"]
    assert batch["counts"] == {"files": 3, "planned": 2, "started": 0, "joined": 0, "online": 1,
                               "skipped": 0, "late": 0}  # fmt: skip
    assert batch["eta"] is not None and batch["eta_confidence"] == "provider_sla"
    assert batch["bytes"] == 3 * 2**30 + 4096
    # Retrieval fees from the price table (sim prices as Azure Blob).
    assert batch["cost"] is not None and batch["cost"]["currency"] == "USD"
    assert float(batch["cost"]["amount"]) > 0
    (action,) = sql.execute(
        "SELECT action FROM catalog.audit_log WHERE details->>'batch_id' = %s",
        (batch["batch_id"],),
    ).fetchone()
    assert action == "recall.batch"
    one = web.call("GET", "/v1/recall-batches/{batch_id}", token=req,
                   batch_id=batch["batch_id"], params={"limit": 2})  # fmt: skip
    assert one.status_code == 200, one.text
    page = one.json()
    assert len(page["items"]) == 2 and page["next_cursor"] is not None
    rest = web.call("GET", "/v1/recall-batches/{batch_id}", token=req, batch_id=batch["batch_id"],
                    params={"cursor": page["next_cursor"]}).json()  # fmt: skip
    items = {i["file_id"]: i for i in page["items"] + rest["items"]}
    assert items[str(online)]["status"] == "online"
    assert items[str(a)]["status"] == "planned" and items[str(a)]["priority"] == "standard"
    assert items[str(a)]["eta"] is not None and items[str(a)]["late"] is False
    assert items[str(a)]["fee"]["amount"] != "0.00"
    # The worker starts each planned file's recall, with the app as requester.
    while _step(db_url, batch["batch_id"], pool):
        pass
    done = web.call("GET", "/v1/recall-batches/{batch_id}", token=req,
                    batch_id=batch["batch_id"]).json()  # fmt: skip
    assert done["state"] == "done" and done["counts"]["started"] == 2
    assert done["done_at"] is not None
    recalls = {i["file_id"]: i["recall_id"] for i in done["items"] if i["recall_id"]}
    assert set(recalls) == {str(a), str(b)}
    (via, app) = sql.execute(
        "SELECT via, app_id FROM catalog.recall_requester WHERE recall_id = %s",
        (int(recalls[str(a)]),),
    ).fetchone()
    assert (via, app) == ("api", "prizmanyx-app")
    # A second batch over the same files joins the recalls.
    body = {"dataset_id": str(w.dataset_id), "paths": ["/raw/a.segy"]}
    again = web.call("POST", "/v1/recall-batches", token=req, json=body).json()
    while _step(db_url, again["batch_id"], pool):
        pass
    joined = web.call("GET", "/v1/recall-batches/{batch_id}", token=req,
                      batch_id=again["batch_id"]).json()  # fmt: skip
    assert joined["counts"]["joined"] == 1
    assert joined["items"][0]["recall_id"] == recalls[str(a)]


def test_no_promise_and_skipped_files(web, sql):
    w = seed(sql)
    add_file(sql, w, "big.segy", tier="archive", state="stub", size=2**40)
    sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, acl_id)"
        " VALUES (%s, %s, 'empty.segy', 'file', %s)",
        (w.dataset_id, w.root_id, w.acl_open),
    )
    req = web.app_token(["Recall.Request"])
    r = web.call("POST", "/v1/recall-batches", token=req,
                 json={"dataset_id": str(w.dataset_id), "priority": "high"})  # fmt: skip
    assert r.status_code == 202, r.text
    got = web.call("GET", "/v1/recall-batches/{batch_id}", token=req,
                   batch_id=r.json()["batch_id"]).json()  # fmt: skip
    items = {i["path"]: i for i in got["items"]}
    # The asked priority, and no promised time above the no-promise size
    # (anti-pattern 5): the batch has no ETA either.
    big = items["/big.segy"]
    assert (big["status"], big["priority"], big["eta"]) == ("planned", "high", None)
    assert got["eta"] is None and got["eta_confidence"] == "none"
    # A file whose bytes aren't uploaded yet is skipped, with the reason.
    empty = items["/empty.segy"]
    assert empty["status"] == "skipped" and "no stored bytes" in empty["reason"]
    assert got["counts"]["skipped"] == 1


def test_batches_are_for_their_app(web, sql):
    w = seed(sql)
    add_file(sql, w, "a.segy", tier="archive", state="stub")
    req = web.app_token(["Recall.Request"])
    body = {"dataset_id": str(w.dataset_id)}
    bid = web.call("POST", "/v1/recall-batches", token=req, json=body).json()["batch_id"]
    other = web.idp.app_token("another-partner", ["Recall.Request"])
    r = web.call("GET", "/v1/recall-batches/{batch_id}", token=other, batch_id=bid)
    assert r.status_code == 404
    r = web.call("POST", "/v1/recall-batches", token=web.app_token(["Archive.Read"]), json=body)
    assert r.status_code == 403 and r.json()["code"] == "role_required"
    # Not for sessions: a user recalls what they can read, file by file.
    admin = web.browser(w.admin)
    assert admin.post("/v1/recall-batches", body).status_code == 403
    r = web.call("POST", "/v1/recall-batches", token=req,
                 json={**body, "paths": ["/nowhere"]})  # fmt: skip
    assert r.status_code == 404
    r = web.call("POST", "/v1/recall-batches", token=req, check=False,
                 json={**body, "paths": [f"/p{i}" for i in range(101)]})  # fmt: skip
    assert r.status_code == 422 and r.json()["code"] == "batch_too_large"
    r = web.call("POST", "/v1/recall-batches", token=req, check=False,
                 json={**body, "priority": "expedited"})  # fmt: skip
    assert r.status_code == 400


def test_fees_in_two_currencies_are_never_added(web, sql, monkeypatch):
    """A migrating dataset can hold archived files on two providers whose price
    tables bill in different currencies: each total stays in its own."""
    from omnifile_service.web import recall_batches as rb

    real = rb.recall_options

    async def priced(db, loc, now, table):  # type: ignore[no-untyped-def]
        out = await real(db, loc, now, table)
        currency = "EUR" if str(loc["object_key"]).startswith("eu/") else "USD"
        for o in out["options"]:
            if o["fee_estimate"]:
                o["fee_estimate"] = {"amount": "10.00", "currency": currency}
        return out

    monkeypatch.setattr(rb, "recall_options", priced)
    w = seed(sql)
    eu = add_file(sql, w, "eu.segy", tier="archive", state="stub", key="eu/a.segy")
    us = add_file(sql, w, "us.segy", tier="archive", state="stub", key="us/a.segy")
    add_file(sql, w, "us2.segy", tier="archive", state="stub", key="us/b.segy")
    req = web.app_token(["Recall.Request"])
    r = web.call("POST", "/v1/recall-batches", token=req, json={"dataset_id": str(w.dataset_id)})
    assert r.status_code == 202, r.text
    b = r.json()
    assert b["costs"] == [{"amount": "10.00", "currency": "EUR"},
                          {"amount": "20.00", "currency": "USD"}]  # fmt: skip
    assert b["cost"] is None  # no single total across currencies
    got = web.call("GET", "/v1/recall-batches/{batch_id}", token=req,
                   batch_id=b["batch_id"]).json()  # fmt: skip
    fees = {i["file_id"]: i["fee"] for i in got["items"]}
    assert fees[str(eu)] == {"amount": "10.00", "currency": "EUR"}
    assert fees[str(us)] == {"amount": "10.00", "currency": "USD"}


@pytest.mark.parametrize("pool", [None, 6], ids=["direct", "pooled"])
def test_a_large_batch_is_planned_by_the_worker(web, sql, db_url, monkeypatch, pool):
    from omnifile_service.web import recall_batches as rb

    monkeypatch.setattr(rb, "INLINE_PLAN_MAX", 1)
    monkeypatch.setattr(rb, "CHUNK", 1)
    w = seed(sql)
    ids = [add_file(sql, w, f"f{i}.segy", tier="archive", state="stub") for i in range(3)]
    req = web.app_token(["Recall.Request"])
    r = web.call("POST", "/v1/recall-batches", token=req, json={"dataset_id": str(w.dataset_id)})
    assert r.status_code == 202, r.text
    b = r.json()
    assert (b["state"], b["counts"]["files"], b["eta"]) == ("planning", 3, None)
    steps = 0
    while _step(db_url, b["batch_id"], pool):
        steps += 1
    assert steps >= 6  # three planning chunks, the switch, three starts
    done = web.call("GET", "/v1/recall-batches/{batch_id}", token=req,
                    batch_id=b["batch_id"]).json()  # fmt: skip
    assert done["state"] == "done" and done["counts"]["started"] == 3
    assert sorted(int(i["file_id"]) for i in done["items"]) == sorted(ids)
    assert done["eta"] is not None
