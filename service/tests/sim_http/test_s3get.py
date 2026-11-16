"""The facade's S3 sim: presigned GETs for S3-resident files (`s3get.py`)."""

from __future__ import annotations

import asyncio
import base64
from datetime import timedelta

import httpx
import pytest
from starlette.testclient import TestClient

from omnifile_service.api.dataplane import SimHttpDataPlane, Target
from omnifile_service.backends.base import EdgeAccess, LogicalTier, RestoreSpeed
from omnifile_service.backends.sim import SimBackend, VirtualClock, crc64nvme
from omnifile_service.backends.sim_http import BlobFacade, create_app
from omnifile_service.backends.sim_http.s3get import presign
from omnifile_service.worker.backends import SimHttpBackend

BASE = "http://sim.test"
DATA = bytes(range(256)) * 40


async def _one(data: bytes):
    yield data


@pytest.fixture
def s3facade() -> BlobFacade:
    clock = VirtualClock()
    return BlobFacade(SimBackend.azure_two_account(clock), s3=SimBackend.s3(clock, backend_id=2))


@pytest.fixture
def c(s3facade: BlobFacade):  # type: ignore[no-untyped-def]
    with TestClient(create_app(s3facade, admin=True), base_url=BASE) as client:
        yield client


def _put(f: BlobFacade, key: str, data: bytes = DATA, tier=None):  # type: ignore[no-untyped-def]
    return asyncio.run(f.s3.put("bkt", key, _one(data), tier, {}))  # type: ignore[union-attr]


def _url(f: BlobFacade, ref, ttl: timedelta = timedelta(minutes=5)) -> str:  # type: ignore[no-untyped-def]
    return presign(f.key, BASE, ref.container, ref.key, ref.version_id, f.s3.clock.now() + ttl)  # type: ignore[union-attr]


def test_a_presigned_get_reads_that_version_and_ranges(c, s3facade) -> None:  # type: ignore[no-untyped-def]
    v1 = _put(s3facade, "dir/a b.segy", DATA)
    _put(s3facade, "dir/a b.segy", b"newer")
    url = _url(s3facade, v1)
    whole = c.get(url)
    assert whole.status_code == 200 and whole.content == DATA
    assert whole.headers["x-amz-version-id"] == v1.version_id
    part = c.get(url, headers={"range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == DATA[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(DATA)}"
    assert c.get(url, headers={"range": "bytes=10240-"}).status_code == 416
    head = c.head(url)
    assert head.status_code == 200 and head.headers["content-length"] == str(len(DATA))


def test_the_signature_and_expiry_are_checked(c, s3facade) -> None:  # type: ignore[no-untyped-def]
    v1 = _put(s3facade, "k", DATA)
    v2 = _put(s3facade, "k", b"two")
    url = _url(s3facade, v1)
    # Another version, key or a changed signature: refused.
    assert c.get(url.replace(v1.version_id, v2.version_id)).status_code == 403
    assert c.get(url.replace("/k?", "/other?")).status_code == 403
    assert c.get(url[:-1] + ("0" if url[-1] != "0" else "1")).status_code == 403
    assert "SignatureDoesNotMatch" in c.get(url[:-1] + "x").text
    # Expired.
    s3facade.s3.clock.advance(timedelta(minutes=6))
    r = c.get(url)
    assert r.status_code == 403 and "expired" in r.text


def test_an_archived_object_is_invalid_object_state_until_restored(c, s3facade) -> None:  # type: ignore[no-untyped-def]
    ref = _put(s3facade, "cold", DATA, LogicalTier.ARCHIVE)
    r = c.get(_url(s3facade, ref))
    assert r.status_code == 403 and "<Code>InvalidObjectState</Code>" in r.text
    asyncio.run(s3facade.s3.restore(ref, RestoreSpeed.EXPEDITED, 1))  # type: ignore[union-attr]
    s3facade.s3.clock.advance(timedelta(hours=1))  # type: ignore[union-attr]
    r = c.get(_url(s3facade, ref))
    assert r.status_code == 200 and r.content == DATA


def test_checksum_mode_returns_the_full_object_crc_on_whole_reads_only(c, s3facade) -> None:  # type: ignore[no-untyped-def]
    ref = _put(s3facade, "k", DATA)
    url = _url(s3facade, ref)
    want = base64.b64encode(crc64nvme(DATA).to_bytes(8, "big")).decode()
    on = {"x-amz-checksum-mode": "ENABLED"}
    for r in (c.head(url, headers=on), c.get(url, headers=on)):
        assert r.headers["x-amz-checksum-crc64nvme"] == want
        assert r.headers["x-amz-checksum-type"] == "FULL_OBJECT"
    assert "x-amz-checksum-crc64nvme" not in c.head(url).headers  # not asked for
    part = c.get(url, headers={**on, "range": "bytes=0-9"})
    assert part.status_code == 206 and "x-amz-checksum-crc64nvme" not in part.headers


def test_faults_can_target_the_s3_sim_and_bytes_served_are_counted(c, s3facade) -> None:  # type: ignore[no-untyped-def]
    ref = _put(s3facade, "f.segy")
    url = _url(s3facade, ref)
    fault = {"backend": "s3", "op": "get_range", "fail_nth": 1}
    assert c.post("/_sim/faults", json=fault).json() == {"ok": True}
    assert c.get(url).status_code == 500
    assert c.get(url, headers={"range": "bytes=0-99"}).content == DATA[:100]
    assert c.get(url, headers={"range": "bytes=100-"}).content == DATA[100:]
    assert c.get("/_sim/s3-served").json() == {"bkt/f.segy": len(DATA)}
    c.post("/_sim/faults", json={"backend": "s3", "op": "get_range", "outage_s": 10})
    assert c.get(url).status_code == 503
    c.post("/_sim/advance", json={"seconds": 11})
    assert c.get(url).status_code == 200


def test_requests_to_the_s3_sim_are_counted_per_bucket_and_caller(c, s3facade) -> None:  # type: ignore[no-untyped-def]
    """GET /_sim/s3-requests: what a test needs to show that only its own
    harness ever wrote a source bucket, and OmniFile only its staging bucket
    (decision 40)."""
    from omnifile_service.backends.sim_http import rpc

    def call(op, *args, harness=False):  # type: ignore[no-untyped-def]
        body = {"backend": "s3", "op": op, "args": [rpc.encode(a) for a in args], "kwargs": {}}
        if harness:
            body["harness"] = True
        r = c.post("/_sim/backend", json=body)
        assert r.status_code == 200 and "error" not in r.json(), r.text
        return rpc.decode(r.json()["result"])

    call("create_container", "src", harness=True)
    call("create_container", "ops", harness=True)
    ref = call("put_bytes", "src", "a.segy", DATA, None, harness=True)
    # OmniFile: a HEAD, a restore and its status, a copy into its staging
    # bucket, the staging object deleted.
    call("head", ref)
    cold = call("put_bytes", "src", "b.segy", DATA, LogicalTier.ARCHIVE, harness=True)
    handle = call("restore", cold, RestoreSpeed.STANDARD, 1)
    call("restore_status", handle)
    staged = call("copy_object", ref, "ops", "staging/1/a.segy", None)
    call("delete", staged)
    c.get(_url(s3facade, ref), headers={"range": "bytes=0-9"})
    got = c.get("/_sim/s3-requests").json()
    assert got["harness"] == {"src": {"CREATE_BUCKET": 1, "PUT": 2}, "ops": {"CREATE_BUCKET": 1}}
    assert got["omnifile"] == {
        "src": {"HEAD": 2, "RESTORE": 1, "COPY_FROM": 1, "GET": 1},
        "ops": {"COPY": 1, "DELETE": 1},
    }
    assert {"PUT", "COPY", "DELETE", "TAG"} <= set(got["writes"])
    assert not set(got["writes"]) & set(got["omnifile"]["src"])


def test_a_facade_without_s3_says_so(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/_s3/b/k?X-Sim-Expires=1&X-Amz-Signature=x").status_code == 501
    assert client.post("/_sim/faults", json={"backend": "s3", "clear": True}).status_code == 400


def test_the_data_plane_and_the_worker_reach_the_s3_sim(s3facade) -> None:  # type: ignore[no-untyped-def]
    ref = _put(s3facade, "m/f.segy", DATA)
    app = create_app(s3facade, admin=True)

    async def go() -> None:
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
        plane = SimHttpDataPlane(BASE, s3facade.key, http=http, clock=s3facade.sim.clock.now,
                                 flavour="s3")  # fmt: skip
        t = Target(2, "bkt", "m/f.segy", ref.version_id)
        cred = await plane.credential(t, EdgeAccess.READ)
        assert cred["mode"] == "s3_presigned_url" and cred["cache_until"] < cred["expires_at"]
        got = await http.get(cred["url"], headers={"range": "bytes=0-9"})
        assert got.status_code == 206 and got.content == DATA[:10]
        h = await plane.head(t)
        assert h is not None and h.version_id == ref.version_id
        # HEAD with checksum mode: the object's stored full CRC64NVME.
        assert h.crc64nvme == crc64nvme(DATA).to_bytes(8, "big")
        chunks = [c async for c in plane.get_range(t, 5, 100)]
        assert b"".join(chunks) == DATA[5:105]
        from omnifile_service.api.problems import ApiError

        with pytest.raises(ApiError):
            await plane.credential(t, EdgeAccess.WRITE)
        # The worker drives the S3 sim through the RPC, not the Azure one.
        w = SimHttpBackend(2, plane, layout="s3")
        head = await w.head(ref)
        assert head.size == len(DATA) and head.ref.container == "bkt"
        await http.aclose()

    asyncio.run(go())


def _head_etag(f: BlobFacade, ref) -> str:  # type: ignore[no-untyped-def]
    return asyncio.run(f.s3.head(ref)).etag  # type: ignore[union-attr]


def test_if_match_is_a_signed_header_and_a_replaced_object_answers_412(c, s3facade) -> None:  # type: ignore[no-untyped-def]
    """Codex review on #23: a presigned GET of an unversioned key (no
    version id in the URL) is pinned by the catalog's ETag, signed as
    If-Match. A replaced object answers 412 and serves none of its bytes."""
    v1 = _put(s3facade, "u.segy", DATA)
    etag = _head_etag(s3facade, v1)
    exp = s3facade.s3.clock.now() + timedelta(minutes=5)  # type: ignore[union-attr]
    url = presign(s3facade.key, BASE, "bkt", "u.segy", None, exp, if_match=etag)
    assert "X-Sim-If-Match=1" in url
    ok = c.get(url, headers={"if-match": etag, "range": "bytes=0-99"})
    assert ok.status_code == 206 and ok.content == DATA[:100]
    # The signed header is required, and exactly that value.
    r = c.get(url, headers={"range": "bytes=0-99"})
    assert r.status_code == 403 and "SignatureDoesNotMatch" in r.text
    r = c.get(url, headers={"if-match": '"other"', "range": "bytes=0-99"})
    assert r.status_code == 403 and "SignatureDoesNotMatch" in r.text
    # The key is overwritten between two chunk reads: the next read of the
    # old URL is refused, never the new object's bytes.
    _put(s3facade, "u.segy", b"replacement " * 100)
    r = c.get(url, headers={"if-match": etag, "range": "bytes=100-199"})
    assert r.status_code == 412 and "<Code>PreconditionFailed</Code>" in r.text
    assert b"replacement" not in r.content
    # An unsigned URL keeps working as before (and pins nothing).
    plain = presign(s3facade.key, BASE, "bkt", "u.segy", None, exp)
    assert "X-Sim-If-Match" not in plain
    assert c.get(plain, headers={"range": "bytes=0-10"}).content.startswith(b"replacement")


def test_the_data_plane_signs_the_catalog_etag_into_presigned_reads(s3facade) -> None:  # type: ignore[no-untyped-def]
    ref = _put(s3facade, "e.segy", DATA)
    etag = _head_etag(s3facade, ref)
    app = create_app(s3facade, admin=True)

    async def go() -> None:
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
        plane = SimHttpDataPlane(BASE, s3facade.key, http=http, clock=s3facade.sim.clock.now,
                                 flavour="s3")  # fmt: skip
        # A bare ETag (as an inventory row stores it) is quoted.
        bare = etag.strip('"')
        cred = await plane.credential(Target(2, "bkt", "e.segy", etag=bare), EdgeAccess.READ)
        assert cred["if_match"] == f'"{bare}"'
        got = await http.get(
            cred["url"], headers={"if-match": cred["if_match"], "range": "bytes=0-9"}
        )
        assert got.status_code == 206 and got.content == DATA[:10]
        # No ETag known: no signed header.
        cred = await plane.credential(Target(2, "bkt", "e.segy"), EdgeAccess.READ)
        assert "if_match" not in cred and "X-Sim-If-Match" not in cred["url"]
        await http.aclose()

    asyncio.run(go())


def test_read_credential_signs_the_locations_etag(s3facade) -> None:  # type: ignore[no-untyped-def]
    """`POST .../credentials` for a presigned read: the location's ETag (an
    inventory row stores it bare) becomes the signed If-Match."""
    from omnifile_service.api.data import read_credential

    ref = _put(s3facade, "r.segy", DATA)
    bare = _head_etag(s3facade, ref).strip('"')
    app = create_app(s3facade, admin=True)

    async def go() -> None:
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
        plane = SimHttpDataPlane(BASE, s3facade.key, http=http, clock=s3facade.sim.clock.now,
                                 flavour="s3")  # fmt: skip
        loc = {"etag": bare, "crc64nvme": crc64nvme(DATA).to_bytes(8, "big")}
        cred = await read_credential(None, loc, plane, Target(2, "bkt", "r.segy"))  # type: ignore[arg-type]
        assert cred["if_match"] == f'"{bare}"' and "X-Sim-If-Match=1" in cred["url"]
        await http.aclose()

    asyncio.run(go())
