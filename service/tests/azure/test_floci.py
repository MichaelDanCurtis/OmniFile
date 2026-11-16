"""Floci smoke lane: the Azure backend's real SDK plumbing against floci-az.

Layer 2 of the test strategy (docs/BUILD-PLAN.md): it proves that our code makes
valid Blob calls (container and blob CRUD, staged upload, ranged download,
listing, Get User Delegation Key and SAS use) where Floci is faithful, and
nothing else. Run it with:

    docker run -d --rm -p 127.0.0.1:4577:4577 -e FLOCI_AZ_TLS_ENABLED=true floci/floci-az:0.13.0
    OMNIFILE_TEST_FLOCI_AZ=http://127.0.0.1:4577 uv run pytest -m floci tests/azure

What Floci (0.13.0, checked 2026-10-05) does and doesn't do, found by running it:
- Bearer tokens are accepted without validation (dev auth mode). The SDK
  sends tokens only over HTTPS, so the lane runs with ``FLOCI_AZ_TLS_ENABLED``
  and trusts the CA served at ``/_floci/tls-cert``.
- ``x-ms-content-crc64`` is ignored: a wrong CRC is accepted. So CRC64 is
  never checked here (the unit tests and the real-Azure lane cover it).
- No tiers (``x-ms-access-tier`` absent), no Set Blob Tier or tags
  (``NotImplemented``), no versions, no change feed, no immutability.
- SAS: expiry and blob scope are enforced, but a tampered signature is
  **accepted**, so signature checks are not asserted here.
"""

from __future__ import annotations

import asyncio
import os
import urllib.request
import uuid
from collections.abc import AsyncIterator, Coroutine
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.storage.blob.aio import BlobClient
from conftest import FakeCredential

from omnifile_service.backends.azure import AzureAccount, AzureBlobBackend, AzureBlobConfig
from omnifile_service.backends.base import (
    EdgeAccess,
    LogicalTier,
    ObjectNotFound,
    ObjectRef,
    RestoreState,
    SystemClock,
)

ENV = "OMNIFILE_TEST_FLOCI_AZ"
ACCOUNT = "devstoreaccount1"  # Floci's default account

pytestmark = pytest.mark.floci


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def _stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _read(b: AzureBlobBackend, ref: ObjectRef, offset: int, length: int) -> bytes:
    return b"".join([c async for c in b.get_range(ref, offset, length)])


@pytest.fixture(scope="module")
def floci(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    base = os.environ.get(ENV)
    if not base:
        pytest.skip(f"{ENV} is not set (e.g. http://127.0.0.1:4577); Floci lane skipped")
    host = base.split("://", 1)[-1].rstrip("/")
    ca: Path = tmp_path_factory.mktemp("floci") / "ca.pem"
    with urllib.request.urlopen(f"http://{host}/_floci/tls-cert", timeout=10) as resp:
        ca.write_bytes(resp.read())
    return f"https://{host}/{ACCOUNT}", str(ca)


def _backend(floci: tuple[str, str]) -> AzureBlobBackend:
    """Built inside the test's event loop: aiohttp sessions are loop-bound."""
    url, ca = floci
    config = AzureBlobConfig(
        accounts={ACCOUNT: AzureAccount(ACCOUNT, url)},
        layout="single_account",
        primary_account=ACCOUNT,
        versioning=False,
        auto_create_containers=True,
        connection_verify=ca,
        block_size=1024,
        extra_client_kwargs={"retry_total": 1},
    )
    return AzureBlobBackend(1, config, credential=FakeCredential(), clock=SystemClock())


def test_staged_upload_head_range_list_delete(floci: tuple[str, str]) -> None:
    async def body() -> None:
        async with _backend(floci) as b:
            data = bytes(range(256)) * 10  # 2,560 bytes: three staged blocks
            meta = {"file-owner": "1001", "file-group": "2001", "file-permissions": "0640"}
            ref = await b.put("smoke", "dir/line_1042.segy", _stream(data), None, meta)
            assert ref.container == f"{ACCOUNT}/smoke"
            head = await b.head(ref)
            assert head.size == len(data)
            assert head.metadata == meta  # file_owner etc. on the wire, mapped back
            assert head.logical_tier is LogicalTier.HOT
            assert head.restore_state is RestoreState.NOT_NEEDED
            assert await _read(b, ref, 0, len(data)) == data
            assert await _read(b, ref, 1000, 100) == data[1000:1100]
            assert await _read(b, ref, len(data) + 5, 10) == b""
            page = await b.list("smoke", "dir/", None)
            assert "dir/line_1042.segy" in [h.ref.key for h in page.items]
            await b.delete(ref)
            with pytest.raises(ObjectNotFound):
                await b.head(ref)

    run(body())


def test_user_delegation_sas_for_the_edge(floci: tuple[str, str]) -> None:
    _, ca = floci

    async def body() -> None:
        async with _backend(floci) as b:
            run_id = uuid.uuid4().hex[:8]  # Floci keeps state between runs
            ref = await b.put("smoke", f"sas/{run_id}/a.bin", _stream(b"payload"), None, {})
            # Write SAS are capped at 15 minutes (M1 security review); reads at 60.
            read = await b.edge_credential(ref, EdgeAccess.READ, timedelta(minutes=60))
            new = ObjectRef(1, "smoke", f"sas/{run_id}/new.bin")
            write = await b.edge_credential(new, EdgeAccess.WRITE, timedelta(minutes=15))
            assert read.url and write.url
            assert dict(parse_qsl(urlsplit(read.url).query))["sp"] == "r"
            assert dict(parse_qsl(urlsplit(write.url).query))["sp"] == "cw"
            async with BlobClient.from_blob_url(read.url, connection_verify=ca) as c:
                assert await (await c.download_blob()).readall() == b"payload"
            async with BlobClient.from_blob_url(write.url, connection_verify=ca) as c:
                await c.upload_blob(b"from the edge")
            # A read SAS is scoped to its blob: another blob is refused.
            other = read.url.replace(f"sas/{run_id}/a.bin", f"sas/{run_id}/new.bin")
            async with BlobClient.from_blob_url(other, connection_verify=ca) as c:
                with pytest.raises((ClientAuthenticationError, HttpResponseError)):
                    await c.download_blob()
            assert await _read(b, new, 0, 100) == b"from the edge"

    run(body())


def test_expired_sas_is_refused(floci: tuple[str, str]) -> None:
    _, ca = floci

    async def body() -> None:
        async with _backend(floci) as b:
            ref = await b.put("smoke", "sas/exp.bin", _stream(b"x"), None, {})
            cred = await b.edge_credential(ref, EdgeAccess.READ, timedelta(seconds=-60))
            async with BlobClient.from_blob_url(cred.url, connection_verify=ca) as c:
                with pytest.raises((ClientAuthenticationError, HttpResponseError)):
                    await c.download_blob()

    run(body())
