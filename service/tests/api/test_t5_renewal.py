"""api's server-certificate renewal never stops on its own, and api stops if it does.

Review of #34: `keep_renewed` and `first_certificate` caught only RuntimeError and ValueError.
A ClientAuthenticationError from the managed identity (IMDS), an InvalidSignature, a KeyError
on a malformed answer or an ssl.SSLError ended the renewal task silently; ten days later every
edge handshake would fail while the probes stayed green. Now any failure is retried with
backoff, and `serve` stops api (exit 1) when the task ends or the certificate is within
EXPIRY_MARGIN of its end, so the restart gets a fresh one.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
from datetime import UTC, datetime, timedelta

import pytest
from azure.core.exceptions import ClientAuthenticationError
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from starlette.applications import Starlette

from omnifile_service.api import __main__ as api_main
from omnifile_service.api.clientauth import (
    EnrolServerCertificates,
    ServerCertificate,
    server_tls_context,
)
from omnifile_service.enrol.ca import FileCASigner, cert_sha256, pem


class FailingToken:
    """The reviewer's probe: the managed identity's token endpoint is down."""

    calls = 0

    async def token(self) -> str:
        FailingToken.calls += 1
        raise ClientAuthenticationError("IMDS: temporarily unavailable")


def _material(sha: bytes, renew: datetime, until: datetime) -> ServerCertificate:
    return ServerCertificate("chain", b"key", until, renew, sha)


def test_a_token_failure_doesnt_end_renewal() -> None:
    """The probe (scratchpad/pr34/probe/renew_dies.py) as a test: before the fix the loop
    died on the first ClientAuthenticationError."""
    ca = FileCASigner.generate()
    certs = EnrolServerCertificates(
        "https://enrol.invalid/internal/v0/sign", FailingToken(), ca.ca_certificate
    )
    now = datetime.now(UTC)
    current = _material(b"\0" * 32, now - timedelta(seconds=1), now + timedelta(days=10))
    ctx = server_tls_context(pem(ca.ca_certificate).encode())
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) > 5:
            raise asyncio.CancelledError

    FailingToken.calls = 0
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(api_main.keep_renewed(ctx, certs, current, sleep=fake_sleep))
    assert FailingToken.calls == 5, "still retrying"
    # Backoff: 60 s doubling, up to 15 minutes.
    assert sleeps[1:] == pytest.approx([60.0, 120.0, 240.0, 480.0, 900.0], abs=1)


@pytest.mark.parametrize(
    "error",
    [
        KeyError("renew_after"),
        InvalidSignature(),
        ssl.SSLError("bad key"),
        OSError("network down"),
        TypeError("odd answer"),
    ],
)
def test_any_failure_is_retried(monkeypatch, error) -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    first = _material(b"1", now + timedelta(days=20), now + timedelta(days=30))
    second = _material(b"2", now + timedelta(days=40), now + timedelta(days=50))
    loaded: list[str] = []
    monkeypatch.setattr(api_main, "load_chain_in_memory", lambda ctx, c, k: loaded.append(c))
    answers = iter([error, second])

    class Certs:
        async def issue(self):
            item = next(answers)
            if isinstance(item, BaseException):
                raise item
            return item

    clock = [now]
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        clock[0] += timedelta(seconds=s)
        if len(slept) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            api_main.keep_renewed(None, Certs(), first, sleep=sleep, now=lambda: clock[0])  # type: ignore[arg-type]
        )
    assert loaded == ["chain"], "the retry got the new certificate in"


def test_a_failing_load_is_retried_too(monkeypatch) -> None:
    """ssl.SSLError from loading the new chain (a bad key) is a renewal failure, not the end."""
    now = datetime(2026, 11, 1, tzinfo=UTC)
    first = _material(b"1", now, now + timedelta(days=30))
    fresh = _material(b"2", now + timedelta(days=20), now + timedelta(days=50))
    calls = {"load": 0}

    def load(ctx, chain, key):
        calls["load"] += 1
        if calls["load"] == 1:
            raise ssl.SSLError("PEM lib")

    monkeypatch.setattr(api_main, "load_chain_in_memory", load)

    class Certs:
        async def issue(self):
            return fresh

    clock = [now]
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        clock[0] += timedelta(seconds=s)
        if len(slept) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            api_main.keep_renewed(None, Certs(), first, sleep=sleep, now=lambda: clock[0])  # type: ignore[arg-type]
        )
    assert calls["load"] == 2 and slept[1] == 60.0


def test_near_its_end_unrenewed_the_certificate_stops_api() -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    current = _material(b"1", now, now + api_main.EXPIRY_MARGIN - timedelta(minutes=1))

    class Never:
        async def issue(self):
            raise AssertionError("not asked: too late to renew in place")

    async def sleep(s: float) -> None:
        return None

    with pytest.raises(api_main.CertificateExpiring):
        asyncio.run(api_main.keep_renewed(None, Never(), current, sleep=sleep, now=lambda: now))  # type: ignore[arg-type]


def test_the_first_certificate_is_awaited_whatever_fails() -> None:
    attempts = iter(
        [
            ClientAuthenticationError("IMDS"),
            KeyError("renew_after"),
            _material(b"x", *[datetime.now(UTC)] * 2),
        ]
    )
    slept: list[float] = []

    class Certs:
        async def issue(self):
            item = next(attempts)
            if isinstance(item, BaseException):
                raise item
            return item

    async def sleep(s: float) -> None:
        slept.append(s)

    got = asyncio.run(api_main.first_certificate(Certs(), sleep=sleep))  # type: ignore[arg-type]
    assert got.sha256 == b"x" and slept == [1.0, 2.0]


# --- serve() stops api when renewal ends --------------------------------------------


def _server_certificate(ca: FileCASigner, until: datetime) -> ServerCertificate:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca.ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(until)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca.private_key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return ServerCertificate(
        pem(cert), key_pem, until, now - timedelta(seconds=1), cert_sha256(cert)
    )


def _free_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()[1]


def test_serve_exits_1_when_renewal_ends() -> None:
    """A certificate already inside the margin: the renewal task ends at its first look,
    and serve stops the server and exits non-zero instead of serving on."""
    ca = FileCASigner.generate()
    short = _server_certificate(ca, datetime.now(UTC) + timedelta(days=1))

    class Certs:
        closed = False

        async def issue(self):
            return short

        async def aclose(self):
            Certs.closed = True

    ctx = server_tls_context(pem(ca.ca_certificate).encode())

    async def run() -> None:
        await asyncio.wait_for(
            api_main.serve(
                Starlette(),
                host="127.0.0.1",
                port=_free_port(),
                ctx=ctx,
                certificates=Certs(),  # type: ignore[arg-type]
                health_port=_free_port(),
            ),
            timeout=30,
        )

    with pytest.raises(SystemExit) as stopped:
        asyncio.run(run())
    assert stopped.value.code == 1 and Certs.closed
