"""sim_http's S3 layout (M5): what the edge's Rust S3 reader relies on, called
the way it calls it: SigV4 header auth (and its refusals), ranged GETs pinned
to a version, HEAD headers, InvalidObjectState, requester pays, virtual-host
addressing, the fake IMDSv2 (``aws_signing_helper serve``) and the admin
routes. Ported from line B; the multipart and Batch tests are not, because
this tree's layout has no multipart uploads or S3 Control (`s3.py`)."""

from __future__ import annotations

import base64
from datetime import timedelta
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

from omnifile_service.backends.sim import SimBackend, VirtualClock
from omnifile_service.backends.sim.crc64 import crc64nvme, to_bytes
from omnifile_service.backends.sim_http.s3 import (
    EDGE_ACCESS_KEY,
    EDGE_SECRET_KEY,
    S3Facade,
    create_s3_app,
)
from omnifile_service.backends.sim_http.sigv4 import Credential, canonical_query, sign_headers

EDGE = Credential(EDGE_ACCESS_KEY, EDGE_SECRET_KEY)
HOST = "testserver"


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock()


@pytest.fixture
def facade(clock: VirtualClock) -> S3Facade:
    f = S3Facade(SimBackend.s3(clock))
    f.create_bucket("src")
    return f


@pytest.fixture
def client(facade: S3Facade) -> TestClient:
    return TestClient(create_s3_app(facade, admin=True))


def _signed(
    facade: S3Facade,
    method: str,
    path: str,
    query: str = "",
    *,
    cred: Credential = EDGE,
    host: str = HOST,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    return sign_headers(
        method=method,
        url_path=path,
        query=query,
        headers={"host": host, "x-amz-content-sha256": "UNSIGNED-PAYLOAD", **(extra or {})},
        body=b"",
        credential=cred,
        region="eu-west-1",
        service="s3",
        now=facade.now(),
    )


def _get(client: TestClient, facade: S3Facade, path: str, query: str = "", **kw: object):
    headers = _signed(facade, "GET", path, query, **kw)  # type: ignore[arg-type]
    return client.get(path + (f"?{query}" if query else ""), headers=headers)


def _seed(client: TestClient, key: str, data: bytes, **kw: object) -> dict:
    body = {"bucket": "src", "key": key, "data_b64": base64.b64encode(data).decode(), **kw}
    r = client.post("/_sim/s3/objects", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_ranged_get_pinned_to_a_version(client: TestClient, facade: S3Facade) -> None:
    old = _seed(client, "dir/a b.segy", b"0123456789")
    _seed(client, "dir/a b.segy", b"abcdefghij")
    path = "/src/" + quote("dir/a b.segy")
    q = f"versionId={old['version_id']}"
    r = _get(client, facade, path, q, extra={"range": "bytes=2-5"})
    assert r.status_code == 206 and r.content == b"2345"
    assert r.headers["content-range"] == "bytes 2-5/10"
    assert r.headers["x-amz-version-id"] == old["version_id"]
    assert "x-amz-checksum-crc64nvme" not in r.headers  # never on ranges
    r = _get(client, facade, path, extra={"range": "bytes=20-30"})
    assert r.status_code == 416 and b"<Code>InvalidRange</Code>" in r.content
    # A whole GET with checksum mode carries the full-object CRC64NVME.
    r = _get(client, facade, path, extra={"x-amz-checksum-mode": "ENABLED"})
    assert r.content == b"abcdefghij"
    want = base64.b64encode(to_bytes(crc64nvme(b"abcdefghij"))).decode()
    assert r.headers["x-amz-checksum-crc64nvme"] == want
    assert facade.stats["src"]["GET"] == 2 and facade.stats["src"]["LIST"] == 0


def test_sigv4_refusals(client: TestClient, facade: S3Facade) -> None:
    _seed(client, "k", b"x")
    assert client.get("/src/k").status_code == 403  # anonymous
    bad = Credential(EDGE_ACCESS_KEY, "wrong")
    r = _get(client, facade, "/src/k", cred=bad)
    assert r.status_code == 403 and b"SignatureDoesNotMatch" in r.content
    r = _get(client, facade, "/src/k", cred=Credential("AKIAUNKNOWN000000000", "x"))
    assert b"InvalidAccessKeyId" in r.content
    headers = _signed(facade, "GET", "/src/k")
    headers["range"] = "bytes=0-0"  # added after signing, but not signed: still valid
    assert client.get("/src/k", headers=headers).status_code == 206
    headers = _signed(facade, "GET", "/src/k", extra={"range": "bytes=0-0"})
    headers["range"] = "bytes=0-1"  # a signed header changed
    assert client.get("/src/k", headers=headers).status_code == 403


def test_imds_session_credentials_expire(
    client: TestClient, facade: S3Facade, clock: VirtualClock
) -> None:
    assert client.get("/latest/meta-data/iam/security-credentials/").status_code == 401
    token = client.put(
        "/latest/api/token", headers={"x-aws-ec2-metadata-token-ttl-seconds": "300"}
    ).text
    h = {"x-aws-ec2-metadata-token": token}
    role = client.get("/latest/meta-data/iam/security-credentials/", headers=h).text
    client.post("/_sim/imds", json={"ttl_seconds": 60})
    creds = client.get(f"/latest/meta-data/iam/security-credentials/{role}", headers=h).json()
    assert creds["Code"] == "Success" and creds["Type"] == "AWS-HMAC"
    session = Credential(creds["AccessKeyId"], creds["SecretAccessKey"], creds["Token"])
    _seed(client, "k", b"x")
    assert _get(client, facade, "/src/k", cred=session).status_code == 200
    no_token = Credential(session.access_key_id, session.secret_access_key)
    assert b"InvalidToken" in _get(client, facade, "/src/k", cred=no_token).content
    clock.advance(timedelta(seconds=61))
    r = _get(client, facade, "/src/k", cred=session)
    assert r.status_code == 400 and b"ExpiredToken" in r.content
    stats = client.get("/_sim/imds").json()
    assert stats["credential_fetches"] == 1 and stats["token_puts"] == 1
    clock.advance(timedelta(seconds=300))  # the IMDS token lapsed too
    assert client.get("/latest/meta-data/iam/security-credentials/", headers=h).status_code == 401


def test_archived_objects_and_restore_headers(
    client: TestClient, facade: S3Facade, clock: VirtualClock
) -> None:
    _seed(client, "g", b"glacier", storage_class="GLACIER")
    r = _get(client, facade, "/src/g")
    assert r.status_code == 403 and b"<Code>InvalidObjectState</Code>" in r.content
    head = client.head("/src/g", headers=_signed(facade, "HEAD", "/src/g"))
    assert head.headers["x-amz-storage-class"] == "GLACIER" and "x-amz-restore" not in head.headers
    client.post("/_sim/s3/restore", json={"bucket": "src", "key": "g", "days": 1})
    head = client.head("/src/g", headers=_signed(facade, "HEAD", "/src/g"))
    assert head.headers["x-amz-restore"] == 'ongoing-request="true"'
    client.post("/_sim/advance", json={"seconds": 5 * 3600})
    head = client.head("/src/g", headers=_signed(facade, "HEAD", "/src/g"))
    assert head.headers["x-amz-restore"].startswith('ongoing-request="false", expiry-date=')
    assert _get(client, facade, "/src/g").content == b"glacier"
    _seed(
        client,
        "it",
        b"i" * 200_000,
        storage_class="INTELLIGENT_TIERING",
        it_archive="ARCHIVE_ACCESS",
    )
    head = client.head("/src/it", headers=_signed(facade, "HEAD", "/src/it"))
    assert head.headers["x-amz-archive-status"] == "ARCHIVE_ACCESS"


def test_requester_pays_and_virtual_hosts(client: TestClient, facade: S3Facade) -> None:
    client.post("/_sim/s3/buckets", json={"bucket": "payer", "requester_pays": True})
    client.post(
        "/_sim/s3/objects",
        json={"bucket": "payer", "key": "k", "data_b64": base64.b64encode(b"pay").decode()},
    )
    r = _get(client, facade, "/payer/k")
    assert r.status_code == 403 and b"AccessDenied" in r.content
    r = _get(client, facade, "/payer/k", extra={"x-amz-request-payer": "requester"})
    assert r.content == b"pay" and r.headers["x-amz-request-charged"] == "requester"
    host = "payer.bucket.vpce-1.s3.eu-west-1.vpce.amazonaws.com"
    headers = _signed(facade, "GET", "/k", host=host, extra={"x-amz-request-payer": "requester"})
    assert client.get("/k", headers=headers).content == b"pay"


def test_synthetic_objects_never_held_in_memory(client: TestClient, facade: S3Facade) -> None:
    size = 100 * 1024**3
    got = client.post(
        "/_sim/s3/objects", json={"bucket": "src", "key": "big", "synthetic_size": size, "seed": 7}
    ).json()
    assert got["crc64nvme_b64"] is None
    off = size - 1000
    r = _get(client, facade, "/src/big", extra={"range": f"bytes={off}-{off + 9}"})
    assert r.content == bytes((7 + i) % 251 for i in range(off, off + 10))
    assert client.get("/_sim/s3/stats").json()["buckets"]["src"]["bytes_out"] == 10


def test_canonical_query_sorts_and_encodes() -> None:
    assert canonical_query("versionId=a%2Fb&restore") == "restore=&versionId=a%2Fb"
    assert canonical_query("prefix=a b&list-type=2") == "list-type=2&prefix=a%20b"


def test_admin_routes_are_off_without_admin(facade: S3Facade) -> None:
    plain = TestClient(create_s3_app(facade, admin=False))
    assert plain.post("/_sim/s3/buckets", json={"bucket": "x"}).status_code == 404


def test_if_match_pins_unversioned_reads(client: TestClient, facade: S3Facade) -> None:
    client.post("/_sim/s3/buckets", json={"bucket": "flat", "versioning": False})
    seed = {"bucket": "flat", "key": "k", "data_b64": base64.b64encode(b"one").decode()}
    first = client.post("/_sim/s3/objects", json=seed).json()
    assert first["version_id"] is None
    r = _get(client, facade, "/flat/k", extra={"if-match": first["etag"]})
    assert r.status_code == 200 and "x-amz-version-id" not in r.headers
    assert _get(client, facade, "/flat/k", extra={"if-match": "*"}).status_code == 200
    client.post("/_sim/s3/objects", json={**seed, "data_b64": base64.b64encode(b"two").decode()})
    unquoted = first["etag"].strip('"')
    r = _get(client, facade, "/flat/k", extra={"if-match": unquoted, "range": "bytes=0-0"})
    assert r.status_code == 412 and b"<Code>PreconditionFailed</Code>" in r.content
    assert b"<Condition>If-Match</Condition>" in r.content
    head = client.head(
        "/flat/k", headers=_signed(facade, "HEAD", "/flat/k", extra={"if-match": unquoted})
    )
    assert head.status_code == 412
    # Preconditions come before the archived-state check.
    client.post("/_sim/s3/objects", json={**seed, "key": "g", "storage_class": "GLACIER"})
    r = _get(client, facade, "/flat/g", extra={"if-match": '"nope"'})
    assert r.status_code == 412


def test_deletes_copies_and_restores_are_counted(client: TestClient, facade: S3Facade) -> None:
    """The M5 exit test reads these counters: no DeleteObject ever reaches a
    migration source (decision 36), and copies and restores are visible."""
    from omnifile_service.backends.sim_http.s3 import SERVICE_ACCESS_KEY, SERVICE_SECRET_KEY

    svc = Credential(SERVICE_ACCESS_KEY, SERVICE_SECRET_KEY)
    _seed(client, "a", b"abc")
    _seed(client, "g", b"cold", storage_class="GLACIER")
    client.post("/_sim/s3/buckets", json={"bucket": "ops"})
    extra = {"x-amz-copy-source": "/src/a"}
    r = client.put("/ops/copy", headers=_signed(facade, "PUT", "/ops/copy", cred=svc, extra=extra))
    assert r.status_code == 200, r.text
    body = b"<RestoreRequest><Days>1</Days></RestoreRequest>"
    r = client.post(
        "/src/g?restore",
        content=body,
        headers=_signed(facade, "POST", "/src/g", "restore", cred=svc),
    )
    assert r.status_code == 202, r.text
    for path in ("/ops/copy", "/ops/missing"):
        r = client.delete(path, headers=_signed(facade, "DELETE", path, cred=svc))
        assert r.status_code == 204, r.text
    stats = client.get("/_sim/s3/stats").json()["buckets"]
    assert stats["src"]["COPY_FROM"] == 1 and stats["src"]["RESTORE"] == 1
    assert stats["ops"]["DELETE"] == 2 and "DELETE" not in stats["src"]


def test_tag_writes_are_counted(client: TestClient, facade: S3Facade) -> None:
    """Tag writes count as TAG: no write of any kind may reach a migration
    source unseen."""
    from omnifile_service.backends.sim_http.s3 import SERVICE_ACCESS_KEY, SERVICE_SECRET_KEY

    svc = Credential(SERVICE_ACCESS_KEY, SERVICE_SECRET_KEY)
    _seed(client, "a", b"abc")
    tags = b"<Tagging><TagSet><Tag><Key>k</Key><Value>v</Value></Tag></TagSet></Tagging>"
    r = client.put(
        "/src/a?tagging",
        content=tags,
        headers=_signed(facade, "PUT", "/src/a", "tagging", cred=svc),
    )
    assert r.status_code == 200, r.text
    r = _get(client, facade, "/src/a", "tagging", cred=svc)
    assert b"<Key>k</Key><Value>v</Value>" in r.content
    r = client.delete(
        "/src/a?tagging", headers=_signed(facade, "DELETE", "/src/a", "tagging", cred=svc)
    )
    assert r.status_code == 204, r.text
    assert client.get("/_sim/s3/stats").json()["buckets"]["src"]["TAG"] == 2


def test_put_object_checks_its_checksum(client: TestClient, facade: S3Facade) -> None:
    from omnifile_service.backends.sim_http.s3 import SERVICE_ACCESS_KEY, SERVICE_SECRET_KEY

    svc = Credential(SERVICE_ACCESS_KEY, SERVICE_SECRET_KEY)
    body = b"hello"
    good = base64.b64encode(to_bytes(crc64nvme(body))).decode()

    def put(crc: str) -> object:
        headers = sign_headers(
            method="PUT",
            url_path="/src/p",
            query="",
            headers={"host": HOST, "x-amz-checksum-crc64nvme": crc},
            body=body,
            credential=svc,
            region="eu-west-1",
            service="s3",
            now=facade.now(),
        )
        return client.put("/src/p", content=body, headers=headers)

    r = put(base64.b64encode(to_bytes(crc64nvme(b"other"))).decode())
    assert r.status_code == 400 and b"BadDigest" in r.content
    r = put(good)
    assert r.status_code == 200 and r.headers["x-amz-checksum-crc64nvme"] == good
    assert _get(client, facade, "/src/p").content == body


def test_unsupported_operations_are_501(client: TestClient, facade: S3Facade) -> None:
    r = _get(client, facade, "/src", "list-type=2")
    assert r.status_code == 501 and b"NotImplemented" in r.content
    r = client.post("/src/k?uploads", headers=_signed(facade, "POST", "/src/k", "uploads"))
    assert r.status_code == 501


def test_sts_web_identity_issues_a_session(
    client: TestClient, facade: S3Facade, clock: VirtualClock
) -> None:
    """AssumeRoleWithWebIdentity is unsigned; its session signs S3 reads
    until it expires; a refused token gets InvalidIdentityToken."""
    import re

    role = "arn:aws:iam::123456789012:role/omnifile-service"
    form = {
        "Action": "AssumeRoleWithWebIdentity",
        "Version": "2011-06-15",
        "RoleArn": role,
        "RoleSessionName": "omnifile-service",
        "WebIdentityToken": "jwt-from-entra",
        "DurationSeconds": "900",
    }
    r = client.post("/", data=form)
    assert r.status_code == 200, r.text

    def tag(name: str) -> str:
        m = re.search(rf"<{name}>(.*?)</{name}>".encode(), r.content)
        assert m is not None, name
        return m.group(1).decode()

    session = Credential(tag("AccessKeyId"), tag("SecretAccessKey"), tag("SessionToken"))
    assert client.get("/_sim/sts").json()["calls"] == [
        {"role": role, "session": "omnifile-service"}
    ]
    _seed(client, "k", b"x")
    assert _get(client, facade, "/src/k", cred=session).status_code == 200
    clock.advance(timedelta(seconds=901))
    assert b"ExpiredToken" in _get(client, facade, "/src/k", cred=session).content
    r = client.post("/", data={**form, "WebIdentityToken": "invalid"})
    assert r.status_code == 400 and b"InvalidIdentityToken" in r.content


def test_banner_names_the_s3_layout() -> None:
    """What the edge's tests/support/sim_s3.rs parses from stdout."""
    import json
    import subprocess
    import sys

    proc = subprocess.Popen(
        [sys.executable, "-m", "omnifile_service.backends.sim_http", "--port", "0",
         "--layout", "s3", "--admin", "--clock", "virtual", "--bucket", "seis"],
        stdout=subprocess.PIPE,
        text=True,
    )  # fmt: skip
    try:
        assert proc.stdout is not None
        info = json.loads(proc.stdout.readline())
    finally:
        proc.kill()
        proc.wait()
    assert info["layout"] == "s3" and "key" not in info
    assert info["imds_url"] == info["url"] and info["url"].startswith("http://127.0.0.1:")
    assert info["region"] == "eu-west-1"
    assert info["access_key_id"] == EDGE_ACCESS_KEY
    assert info["secret_access_key"] == EDGE_SECRET_KEY
