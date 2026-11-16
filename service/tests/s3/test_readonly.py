"""`S3Backend` writes only OmniFile's own keys (catalog-seeding.md section 10).

A customer's binding owns no bucket: a source is the client's. Deletes,
puts, in-place copies (class and metadata changes), tags and write
credentials are refused for every other key, before any request is sent.
The staging prefix and the Batch Operations manifests are OmniFile's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from omnifile_service.backends.base import EdgeAccess, LogicalTier, ObjectRef, WriteRefused
from omnifile_service.backends.s3 import S3Backend
from omnifile_service.backends.s3.config import STAGING_PREFIX, BatchOps

from .conftest import BUCKET
from .s3_fakes import ACCOUNT, FakeAws

STAGING = "omnifile-ops"


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


@pytest.fixture
def pair(make_backend, fake: FakeAws) -> tuple[S3Backend, S3Backend, ObjectRef]:  # type: ignore[no-untyped-def]
    """(the client's own tools, the service's read-only binding, an object)."""
    fake.add_bucket(STAGING)
    buckets = (BUCKET, STAGING)
    client = make_backend(buckets=buckets, owned_buckets=buckets)
    service = make_backend(
        buckets=buckets,
        owned_buckets=(),
        staging_bucket=STAGING,
        private_network=False,
        endpoint_url=None,
        sqs_endpoint_url=None,
        batch=BatchOps(ACCOUNT, f"arn:aws:iam::{ACCOUNT}:role/batch", STAGING),
    )
    ref = run(client.put(BUCKET, "survey/line.segy", _one(b"seismic"), None, {}))
    return client, service, ref


def _writes(fake: FakeAws, since: int) -> list[str]:
    reads = {"HeadObject", "GetObject", "ListObjectVersions", "GetObjectTagging"}
    return [op for op in fake.ops()[since:] if op not in reads]


def test_a_delete_outside_staging_is_refused_without_a_request(pair, fake) -> None:  # type: ignore[no-untyped-def]
    _, service, ref = pair
    since = len(fake.calls)
    with pytest.raises(WriteRefused, match="never writes to a client's bucket"):
        run(service.delete(ref))
    with pytest.raises(WriteRefused):
        run(service.delete(ObjectRef(ref.backend_id, ref.container, ref.key)))
    assert fake.ops()[since:] == []
    assert run(service.head(ref)).size == 7  # still there


def test_every_write_to_a_source_object_is_refused(pair, fake) -> None:  # type: ignore[no-untyped-def]
    _, service, ref = pair
    since = len(fake.calls)
    with pytest.raises(WriteRefused):
        run(service.set_class(ref, LogicalTier.COLD))
    with pytest.raises(WriteRefused):
        run(service.set_metadata(ref, {"file_owner": "0"}))
    with pytest.raises(WriteRefused):
        run(service.set_tags(ref, {"omnifile_hold": "1"}))
    with pytest.raises(WriteRefused):
        run(service.put(BUCKET, "survey/new.segy", _one(b"x"), None, {}))
    with pytest.raises(WriteRefused):
        run(service.copy_object(ref, BUCKET, "survey/line.segy-restored"))
    with pytest.raises(WriteRefused):  # outside the staging prefix
        run(service.copy_object(ref, STAGING, "elsewhere/line.segy"))
    with pytest.raises(WriteRefused):
        run(service.edge_credential(ref, EdgeAccess.WRITE, timedelta(minutes=5)))
    assert _writes(fake, since) == []


def test_the_staging_prefix_and_manifests_are_omnifiles(pair, fake) -> None:  # type: ignore[no-untyped-def]
    _, service, ref = pair
    staged = run(service.copy_object(ref, STAGING, f"{STAGING_PREFIX}7/line.segy"))
    assert run(service.head(staged)).size == 7
    run(service.delete(staged))
    assert service.config.owns(STAGING, "omnifile/batch/omnifile-wave-1-1.csv")
    assert not service.config.owns(BUCKET, f"{STAGING_PREFIX}7/line.segy")
    assert not service.config.owns(BUCKET, "omnifile/batch/x.csv")
    # Reads and restores of the source go ahead.
    assert run(service.head(ref)).size == 7
