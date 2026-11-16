"""DataPlane over any StorageBackend (the seam the real Azure backend plugs into)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from omnifile_service.api.dataplane import BackendDataPlane, Target
from omnifile_service.backends.base import EdgeAccess
from omnifile_service.backends.sim import SimBackend, VirtualClock


async def _one(chunks: list[bytes]):
    for c in chunks:
        yield c


def test_backend_data_plane_on_the_sim() -> None:
    async def run() -> None:
        sim = SimBackend.azure_two_account(VirtualClock(), backend_id=7)
        ref = await sim.put("live/data", "1/2", _one([b"abc"]), None, {})
        plane = BackendDataPlane(sim)
        target = Target(7, "live/data", "1/2", ref.version_id)
        cred = await plane.credential(target, EdgeAccess.READ, timedelta(minutes=60))
        assert cred["mode"] == "azure_user_delegation_sas" and "sp=r" in cred["url"]
        assert cred["cache_until"] < cred["expires_at"]
        head = await plane.head(target)
        assert head is not None and head.size == 3
        assert await plane.head(Target(7, "live/data", "missing")) is None

        s3 = SimBackend.s3(VirtualClock(), backend_id=8)
        cred = await BackendDataPlane(s3).credential(
            Target(8, "bucket", "k", "v1"), EdgeAccess.READ
        )
        assert cred["mode"] == "s3_roles_anywhere" and cred["bucket"] == "bucket"

    asyncio.run(run())


def test_a_presigned_s3_url_goes_to_the_edge_like_a_sas() -> None:
    from datetime import UTC, datetime

    from omnifile_service.backends.base import CredentialMode, EdgeCredential

    expires = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

    class Presigning:
        async def edge_credential(self, ref, access, ttl, *, if_match=None):  # type: ignore[no-untyped-def]
            return EdgeCredential(CredentialMode.S3_PRESIGNED_URL, access, expires,
                                  url="https://b.bucket.vpce-1.s3.eu-west-1.vpce.amazonaws.com/k"
                                      "?versionId=v1&X-Amz-Signature=s", details={})  # fmt: skip

    cred = asyncio.run(
        BackendDataPlane(Presigning()).credential(  # type: ignore[arg-type]
            Target(8, "b", "k", "v1"), EdgeAccess.READ
        )
    )
    assert cred == {
        "mode": "s3_presigned_url",
        "url": "https://b.bucket.vpce-1.s3.eu-west-1.vpce.amazonaws.com/k?versionId=v1"
        "&X-Amz-Signature=s",
        "expires_at": "2026-10-15T12:00:00.000Z",
        "cache_until": "2026-10-15T11:55:00.000Z",
    }
