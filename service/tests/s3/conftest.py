from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from backend_contract.clock import ManualClock

from omnifile_service.backends.s3 import S3Backend, S3Config

from .s3_fakes import REGION, FakeAws

BUCKET = "contract"
ROLE = "arn:aws:iam::123456789012:role/omnifile-service"
AUDIENCE = "api://omnifile-aws"
MiB = 1024 * 1024


async def fake_token() -> str:
    """The managed identity's Entra token, as FakeAws's STS takes it."""
    return "entra-jwt"


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def fake(clock: ManualClock) -> FakeAws:
    aws = FakeAws(clock.now)
    aws.add_bucket(BUCKET, queue=aws.queue_url("omnifile-events"))
    return aws


def private_config(fake: FakeAws, **overrides: Any) -> S3Config:
    """A private-network binding, as a customer's would be (decision 22)."""
    kw: dict[str, Any] = {
        "region": REGION,
        "buckets": (BUCKET,),
        # The contract tests write all over their bucket: it is theirs. A
        # customer's binding owns none (S3 is a migration source).
        "owned_buckets": (BUCKET,),
        "endpoint_url": "https://bucket.vpce-0a1b2c3d-4e5f6a7b.s3.eu-west-1.vpce.amazonaws.com",
        "sqs_endpoint_url": "https://vpce-0a1b2c3d-9f8e7d6c.sqs.eu-west-1.vpce.amazonaws.com",
        "sts_endpoint_url": "https://vpce-0a1b2c3d-0e0e0e0e.sts.eu-west-1.vpce.amazonaws.com",
        "event_queues": {"*": fake.queue_url("omnifile-events")},
        "role_arn": ROLE,
        "web_identity_audience": AUDIENCE,
        "list_page_size": 2,
        "part_size": 5 * MiB,
    }
    kw.update(overrides)
    return S3Config(**kw)


@pytest.fixture
def make_backend(fake: FakeAws, clock: ManualClock) -> Callable[..., S3Backend]:
    def make(**overrides: Any) -> S3Backend:
        return S3Backend(
            1,
            private_config(fake, **overrides),
            clock=clock,
            on_client=fake.attach,
            token_provider=fake_token,
        )

    return make
