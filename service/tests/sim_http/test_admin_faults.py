"""POST /_sim/faults: fault injection for harnesses in another process."""

from __future__ import annotations

from starlette.testclient import TestClient

from omnifile_service.backends.sim import VirtualClock

from .client import Blob

DATA = bytes(range(256)) * 16


def test_fail_nth_get_then_ok(blob: Blob, client: TestClient) -> None:
    sas = blob.sas()
    blob.upload(DATA, sas, block_size=1024)
    assert client.post("/_sim/faults", json={"op": "get_range", "fail_nth": 1}).json() == {
        "ok": True
    }
    assert blob.get(sas).status_code >= 500
    r = blob.get(sas)
    assert r.status_code == 200 and r.content == DATA


def test_outage_window_on_the_backend_clock(
    blob: Blob, client: TestClient, clock: VirtualClock
) -> None:
    sas = blob.sas()
    blob.upload(DATA, sas, block_size=1024)
    client.post("/_sim/faults", json={"op": "get_range", "outage_s": 10})
    assert blob.get(sas).status_code == 503
    assert blob.head(sas).status_code == 200  # only get_range is out
    client.post("/_sim/advance", json={"seconds": 11})
    assert blob.get(sas).status_code == 200


def test_clear_and_bad_requests(blob: Blob, client: TestClient) -> None:
    sas = blob.sas()
    blob.upload(DATA, sas, block_size=1024)
    client.post("/_sim/faults", json={"op": "get_range", "fail_nth": 1})
    client.post("/_sim/faults", json={"clear": True})
    assert blob.get(sas).status_code == 200
    assert client.post("/_sim/faults", json={}).status_code == 400
    assert client.post("/_sim/faults", json={"op": "nope", "fail_nth": 1}).status_code == 400
