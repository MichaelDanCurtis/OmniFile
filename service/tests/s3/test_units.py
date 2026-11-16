"""The S3 backend's pure parts: config, error mapping, event parsing, the
x-amz-restore header and TLS (config and credentials: test_config_credentials.py)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from backend_contract.clock import ManualClock
from botocore.exceptions import ClientError, EndpointConnectionError
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from omnifile_service.backends.base import (
    BackendError,
    ChangeKind,
    NotRestored,
    ObjectNotFound,
    ObjectRef,
    Throttled,
    UnsupportedOperation,
)
from omnifile_service.backends.s3 import S3Backend, S3Config
from omnifile_service.backends.s3.backend import RestoreBudget, crc64_b64, parse_restore
from omnifile_service.backends.s3.errors import translate
from omnifile_service.backends.s3.events import to_record

S3_VPCE = "https://bucket.vpce-0a1b2c3d.s3.eu-west-1.vpce.amazonaws.com"


# --- errors ------------------------------------------------------------------------


def _client_error(code: str, status: int, **headers: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "m"},
            "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": headers},
        },
        "Op",
    )


@pytest.mark.parametrize(
    ("code", "status", "reading", "expected"),
    [
        ("NoSuchKey", 404, False, ObjectNotFound),
        ("404", 404, False, ObjectNotFound),
        ("NoSuchBucket", 404, False, ObjectNotFound),
        ("SlowDown", 503, False, Throttled),
        ("ThrottlingException", 400, False, Throttled),
        ("InvalidObjectState", 403, True, NotRestored),
        ("InvalidObjectState", 403, False, UnsupportedOperation),
        ("InvalidStorageClass", 400, False, UnsupportedOperation),
        ("AccessDenied", 403, False, BackendError),
    ],
)
def test_error_mapping(code: str, status: int, reading: bool, expected: type) -> None:
    err = translate(_client_error(code, status), "s3://b/k", reading=reading)
    assert type(err) is expected


def test_throttling_keeps_retry_after_and_transport_errors_are_backend_errors() -> None:
    err = translate(_client_error("SlowDown", 503, **{"retry-after": "3"}), "x")
    assert isinstance(err, Throttled) and err.retry_after == timedelta(seconds=3)
    err = translate(EndpointConnectionError(endpoint_url=S3_VPCE), "x")
    assert type(err) is BackendError


# --- events -------------------------------------------------------------------------


def _event(detail_type: str, **detail: Any) -> dict[str, Any]:
    return {
        "version": "0",
        "source": "aws.s3",
        "detail-type": detail_type,
        "time": "2026-10-14T09:00:00Z",
        "detail": {
            "bucket": {"name": "b"},
            "object": {"key": "dir/line 1042.segy", "size": 5, "version-id": "v1"},
            **detail,
        },
    }


def test_restore_completed_event() -> None:
    rec = to_record(
        3,
        json.dumps(
            _event(
                "Object Restore Completed",
                **{
                    "restore-expiry-time": "2026-10-16T00:00:00Z",
                    "source-storage-class": "GLACIER",
                },
            )
        ),
    )
    assert rec is not None
    assert rec.kind is ChangeKind.RESTORE_COMPLETED
    assert (rec.ref.backend_id, rec.ref.container, rec.ref.key) == (3, "b", "dir/line 1042.segy")
    assert rec.ref.version_id == "v1"
    assert rec.at == datetime(2026, 10, 14, 9, tzinfo=UTC)
    assert rec.native_class == "GLACIER" and rec.size == 5
    # The keys worker/changefeed_sync.py reads.
    assert rec.detail == {
        "expires_at": "2026-10-16T00:00:00Z",
        "source_class": "GLACIER",
        "provider_ref": "s3-restore:b/dir/line 1042.segy@v1",
    }


def test_delete_events_say_whether_they_are_tombstones() -> None:
    marker = to_record(1, _event("Object Deleted", **{"deletion-type": "Delete Marker Created"}))
    assert marker is not None and marker.detail["tombstone"] == "true"
    gone = to_record(1, _event("Object Deleted", **{"deletion-type": "Permanently Deleted"}))
    assert gone is not None and "tombstone" not in gone.detail


def test_class_change_and_ignored_and_malformed_events() -> None:
    rec = to_record(
        1, _event("Object Storage Class Changed", **{"destination-storage-class": "STANDARD_IA"})
    )
    assert rec is not None and rec.kind is ChangeKind.TIER_CHANGED
    assert rec.native_class == "STANDARD_IA"
    tier = to_record(
        1, _event("Object Access Tier Changed", **{"destination-access-tier": "ARCHIVE_ACCESS"})
    )
    assert tier is not None and tier.native_class == "ARCHIVE_ACCESS"
    assert to_record(1, _event("Object Tags Added")) is None
    assert to_record(1, {"source": "aws.ec2", "detail-type": "Object Created"}) is None
    with pytest.raises(ValueError):
        to_record(1, {"source": "aws.s3", "detail-type": "Object Created", "time": "x"})


# --- restore header, CRC, budget ------------------------------------------------------


def test_parse_restore_header() -> None:
    assert parse_restore(None) == (None, None)
    assert parse_restore('ongoing-request="true"') == (True, None)
    ongoing, expiry = parse_restore(
        'ongoing-request="false", expiry-date="Fri, 16 Oct 2026 00:00:00 GMT"'
    )
    assert ongoing is False and expiry == datetime(2026, 10, 16, tzinfo=UTC)


def test_crc_header_is_base64_of_the_big_endian_crc() -> None:
    assert crc64_b64(0) == "AAAAAAAAAAA="
    assert crc64_b64(0x0102030405060708) == "AQIDBAUGBwg="


def test_restore_budget_refills_with_time() -> None:
    clock = ManualClock()
    budget = RestoreBudget(1000, clock)
    for _ in range(1000):
        budget.take()
    with pytest.raises(Throttled):
        budget.take()
    clock.advance(timedelta(milliseconds=10))
    for _ in range(10):
        budget.take()
    with pytest.raises(Throttled):
        budget.take()


# --- TLS and credentials --------------------------------------------------------------


def _self_signed(tmp_path: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "vpce-test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "vpce.pem", tmp_path / "vpce.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def tls12_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A local HTTPS server that speaks TLS 1.2 only, like an S3 interface
    endpoint, and answers every request 404."""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    cert_path, key_path = _self_signed(tmp_path)
    versions: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:  # noqa: N802
            versions.append(self.connection.version())  # type: ignore[attr-defined]
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}", cert_path, versions
    finally:
        server.shutdown()
        server.server_close()


def test_tls_1_2_endpoints_work_and_certificates_are_verified(tls12_endpoint) -> None:  # type: ignore[no-untyped-def]
    """The real SDK transport (no fake): a TLS 1.2-only endpoint is reached
    and its certificate is checked against `ca_bundle`."""
    url, cert_path, versions = tls12_endpoint
    common: dict[str, Any] = {
        "region": "eu-west-1",
        "endpoint_url": url,
        "credentials": "default",
        "private_network": False,
        "path_style": True,
        "max_attempts": 1,
    }

    async def head(config: S3Config) -> None:
        backend = S3Backend(1, config)
        try:
            await backend.head(ObjectRef(1, "bucket", "key"))
        finally:
            await backend.close()

    with pytest.raises(ObjectNotFound):
        asyncio.run(head(S3Config(**common, ca_bundle=str(cert_path))))
    assert versions == ["TLSv1.2"]
    with pytest.raises(BackendError, match="(?i)certificate|ssl"):
        asyncio.run(head(S3Config(**common)))
    assert versions == ["TLSv1.2"]
