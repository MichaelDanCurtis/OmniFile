"""User-delegation SAS enforcement, archived reads, faults and the entry point."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest
import uvicorn
from starlette.testclient import TestClient

from omnifile_service.backends.base import LogicalTier, ObjectRef, RestoreSpeed
from omnifile_service.backends.sim import Op, VirtualClock
from omnifile_service.backends.sim_http import BlobFacade, azure_crc64, create_app
from omnifile_service.backends.sim_http import sas as sas_mod
from omnifile_service.backends.sim_http.__main__ import build, parse_args

from .client import Blob


def _upload(blob: Blob, data: bytes = b"payload") -> str:
    r = blob.upload(data, blob.sas())
    assert r.status_code == 201, r.text
    return r.headers["x-ms-version-id"]


# --- SAS ---------------------------------------------------------------------


def test_expired_sas_is_refused(blob: Blob, clock: VirtualClock) -> None:
    _upload(blob)
    sas = blob.sas("r", ttl=timedelta(minutes=10))
    assert blob.get(sas).status_code == 200
    clock.advance(timedelta(minutes=10))
    r = blob.get(sas)
    assert r.status_code == 403 and r.headers["x-ms-error-code"] == "AuthenticationFailed"
    assert b"Signature not valid in the specified time frame" in r.content


def test_not_yet_valid_sas_is_refused(blob: Blob, clock: VirtualClock) -> None:
    _upload(blob)
    sas = blob.sas("r", start=clock.now() + timedelta(minutes=5))
    assert blob.get(sas).status_code == 403
    clock.advance(timedelta(minutes=5))
    assert blob.get(sas).status_code == 200


def test_permissions_are_enforced(blob: Blob) -> None:
    _upload(blob)
    read_only = blob.sas("r")
    r = blob.put_block(b"zz", b"x", read_only)
    assert r.status_code == 403
    assert r.headers["x-ms-error-code"] == "AuthorizationPermissionMismatch"
    write_only = blob.sas("w")
    assert blob.get(write_only).headers["x-ms-error-code"] == "AuthorizationPermissionMismatch"
    assert blob.head(write_only).status_code == 403


def test_create_permission_creates_but_never_overwrites(blob: Blob, facade: BlobFacade) -> None:
    create_only = blob.sas("c")
    assert blob.upload(b"new", create_only).status_code == 201
    r = blob.put_block(b"again", b"x", create_only)
    assert r.headers["x-ms-error-code"] == "AuthorizationPermissionMismatch"
    # 'c' counts only from x-ms-version 2026-04-06 (Put Block, SAS table).
    other = Blob(blob.client, facade, "live", "data", "other")
    r = other.put_block(b"b", b"x", other.sas("c"), headers={"x-ms-version": "2025-11-05"})
    assert r.status_code == 403


def test_sas_is_scoped_to_its_blob(blob: Blob, facade: BlobFacade) -> None:
    _upload(blob)
    other = Blob(blob.client, facade, "live", "data", "dir/other.segy")
    _upload(other)
    r = other.get(blob.sas("r"))
    assert r.status_code == 403 and r.headers["x-ms-error-code"] == "AuthenticationFailed"
    assert b"Signature did not match" in r.content
    container_sas = facade.mint_sas("live", "data", None, "r")
    assert other.get(container_sas).status_code == 200
    wrong_container = facade.mint_sas("live", "elsewhere", None, "r")
    assert other.get(wrong_container).status_code == 403


def test_version_sas_reads_only_that_version(blob: Blob, facade: BlobFacade) -> None:
    v1 = _upload(blob, b"one")
    v2 = _upload(blob, b"two")
    vsas = blob.sas("r", version_id=v1)
    assert blob.get(vsas, version=v1).content == b"one"
    assert blob.get(vsas, version=v2).status_code == 403
    assert blob.get(vsas).status_code == 403  # not the base blob
    assert blob.put_block(b"x", b"x", blob.sas("rw", version_id=v1)).status_code == 403


def test_tampered_or_foreign_sas_is_refused(blob: Blob, facade: BlobFacade) -> None:
    _upload(blob)
    sas = blob.sas("r")
    tampered = sas.replace("sp=r", "sp=rw")
    assert blob.get(tampered).headers["x-ms-error-code"] == "AuthenticationFailed"
    stranger = sas_mod.UserDelegationKey.issue(facade.sim.clock.now())
    foreign = sas_mod.mint(
        stranger, "live", "data", blob.blob, "r", facade.sim.clock.now() + timedelta(hours=1)
    )
    assert blob.get(foreign).status_code == 403


def test_bad_permission_order_is_refused(blob: Blob, facade: BlobFacade) -> None:
    _upload(blob)
    assert blob.get(blob.sas("wr")).headers["x-ms-error-code"] == "AuthenticationFailed"


def test_https_only_sas_and_ip_ranges(blob: Blob) -> None:
    _upload(blob)
    r = blob.get(blob.sas("r", protocol="https"))
    assert r.status_code == 403 and r.headers["x-ms-error-code"] == "AuthorizationProtocolMismatch"
    assert blob.get(blob.sas("r", ip="127.0.0.1")).status_code == 200
    r = blob.get(blob.sas("r", ip="198.51.100.10-198.51.100.20"))
    assert r.headers["x-ms-error-code"] == "AuthorizationSourceIPMismatch"


def test_no_auth_bearer_and_shared_key_are_refused(blob: Blob) -> None:
    _upload(blob)
    r = blob.client.get(blob.path)
    assert r.status_code == 401 and r.headers["x-ms-error-code"] == "NoAuthenticationInformation"
    for auth in ("Bearer eyJ0eXAi.fake.token", "SharedKey live:c2lnbmF0dXJl"):
        r = blob.client.get(f"{blob.path}?{blob.sas('r')}", headers={"Authorization": auth})
        assert r.status_code == 403 and r.headers["x-ms-error-code"] == "AuthenticationFailed"


def test_older_signed_versions_use_their_string_to_sign(blob: Blob, facade: BlobFacade) -> None:
    _upload(blob)
    for sv in ("2020-12-06", "2025-07-05", "2026-04-06"):
        assert blob.get(blob.sas("r", signed_version=sv)).status_code == 200, sv
    assert blob.get(blob.sas("r", signed_version="2019-12-12")).status_code == 403


def test_expired_delegation_key(blob: Blob, facade: BlobFacade, clock: VirtualClock) -> None:
    _upload(blob)
    old_key = facade.key
    sas = sas_mod.mint(old_key, "live", "data", blob.blob, "r", clock.now() + timedelta(days=30))
    assert blob.get(sas).status_code == 200
    clock.advance(timedelta(days=7))
    assert blob.get(sas).status_code == 403  # the key lapsed before the SAS did
    assert blob.get(blob.sas("r")).status_code == 200  # a new key was issued


# --- archive -----------------------------------------------------------------


def test_archived_blob_read_is_refused_but_properties_work(
    blob: Blob, facade: BlobFacade, clock: VirtualClock
) -> None:
    sim = facade.sim
    sim.create_container("archive/data")
    arch = Blob(blob.client, facade, "archive", "data", "f.segy")
    _upload(arch)
    ref = ObjectRef(sim.backend_id, "archive/data", "f.segy")
    asyncio.run(sim.set_class(ref, LogicalTier.ARCHIVE))
    sas = arch.sas("rw")
    r = arch.get(sas)
    assert r.status_code == 409 and r.headers["x-ms-error-code"] == "BlobArchived"
    h = arch.head(sas)
    assert h.status_code == 200 and h.headers["x-ms-access-tier"] == "Archive"
    r = arch.upload(b"overwrite", sas)
    assert r.headers["x-ms-error-code"] == "BlobArchived"  # "Overwriting an archive blob fails."
    # A rehydrate copy is "being rehydrated" until done, then readable.
    handle = asyncio.run(sim.restore(ref, RestoreSpeed.STANDARD))
    assert handle.target is not None
    account, container = handle.target.container.split("/")
    target = Blob(blob.client, facade, account, container, handle.target.key)
    tsas = target.sas("r")
    assert target.get(tsas).headers["x-ms-error-code"] == "BlobBeingRehydrated"
    assert target.head(tsas).headers["x-ms-archive-status"] == "rehydrate-pending-to-hot"
    clock.advance(timedelta(hours=15))
    assert target.get(target.sas("r")).content == b"payload"


# --- faults --------------------------------------------------------------------


def test_backend_faults_become_azure_errors(blob: Blob, facade: BlobFacade) -> None:
    _upload(blob)
    sas = blob.sas("r")
    facade.sim.faults.throttle(Op.HEAD, retry_after=timedelta(seconds=2))
    r = blob.get(sas)
    assert r.status_code == 503 and r.headers["x-ms-error-code"] == "ServerBusy"
    assert r.headers["retry-after"] == "2"
    facade.sim.faults.fail_nth(Op.GET_RANGE, 1)
    r = blob.get(sas)
    assert r.status_code == 500 and r.headers["x-ms-error-code"] == "InternalError"
    assert blob.get(sas).status_code == 200


# --- admin endpoints and the entry point ------------------------------------------


def test_admin_sas_and_clock(facade: BlobFacade, clock: VirtualClock) -> None:
    with TestClient(create_app(facade, admin=True)) as c:
        r = c.post(
            "/_sim/sas",
            json={"account": "live", "container": "data", "blob": "x", "permissions": "rcw"},
        )
        assert r.status_code == 200 and r.json()["url"].startswith("http://testserver/live/data/x?")
        before = clock.now()
        assert c.post("/_sim/advance", json={"seconds": 60}).status_code == 200
        assert clock.now() - before == timedelta(seconds=60)
    with TestClient(create_app(facade)) as c:  # admin off: just another (bad) blob path
        assert c.post("/_sim/sas", json={}).status_code != 200


def test_entry_point_serves_over_real_http() -> None:
    args = parse_args(["--port", "0", "--clock", "virtual", "--admin"])
    facade, app = build(args)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(
            f"{base}/_sim/sas",
            data=json.dumps(
                {"account": "live", "container": "data", "blob": "f", "permissions": "rcw"}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        url = json.loads(urllib.request.urlopen(req, timeout=5).read())["url"]
        crc = azure_crc64(b"hi")
        put = urllib.request.Request(
            url + "&comp=block&blockid=QUFB",
            data=b"hi",
            headers={"x-ms-version": "2026-04-06", "x-ms-content-crc64": crc},
            method="PUT",
        )
        assert urllib.request.urlopen(put, timeout=5).status == 201
        body = b'<?xml version="1.0"?><BlockList><Latest>QUFB</Latest></BlockList>'
        commit = urllib.request.Request(
            url + "&comp=blocklist", data=body, headers={"x-ms-version": "2026-04-06"}, method="PUT"
        )
        assert urllib.request.urlopen(commit, timeout=5).status == 201
        got = urllib.request.urlopen(url, timeout=5)
        assert got.read() == b"hi"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert facade.sim.flavour == "azure"


@pytest.mark.parametrize("layout", ["two-account", "worm"])
def test_layouts(layout: str) -> None:
    facade, _ = build(parse_args(["--layout", layout]))
    assert facade.key.signed_service == "b"


def test_module_entry_point_prints_url_and_key(tmp_path: Path) -> None:
    key_file = tmp_path / "key.json"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnifile_service.backends.sim_http",
            "--port",
            "0",
            "--key-file",
            str(key_file),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        info = json.loads(proc.stdout.readline())
        assert info["layout"] == "two-account"
        assert json.loads(key_file.read_text())["Value"] == info["key"]["Value"]
        assert oct(key_file.stat().st_mode & 0o777) == "0o600"
        try:
            urllib.request.urlopen(f"{info['url']}/live/data/x", timeout=5)
        except urllib.error.HTTPError as e:
            assert e.code == 401
            assert e.headers["x-ms-error-code"] == "NoAuthenticationInformation"
        else:
            raise AssertionError("expected 401")
        # The /_sim/ helpers are opt-in (--admin): off here, so no SAS minting,
        # clock moves or backend calls over HTTP.
        for path in ("/_sim/sas", "/_sim/advance", "/_sim/backend", "/_sim/faults"):
            req = urllib.request.Request(f"{info['url']}{path}", data=b"{}", method="POST")
            try:
                urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as e:
                assert e.code != 200, path
            else:
                raise AssertionError(f"{path} answered without --admin")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
