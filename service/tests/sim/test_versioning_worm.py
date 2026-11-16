"""Versioning (decision 30) and version-level WORM (decision 31)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from omnifile_service.backends.base import (
    Immutable,
    LogicalTier,
    ObjectNotFound,
    ObjectRef,
    UnsupportedOperation,
)
from omnifile_service.backends.sim import (
    AccountConfig,
    Flavour,
    Redundancy,
    SimBackend,
    VirtualClock,
)

from .helpers import current, put, read, run, stream


def test_overwrite_makes_versions_and_old_ones_stay_readable(azure: SimBackend) -> None:
    v1 = put(azure, "data", "f", b"one")
    v2 = put(azure, "data", "f", b"two")
    versions = run(azure.list_versions(current(v1)))
    assert [v.ref.version_id for v in versions] == [v2.version_id, v1.version_id]
    assert [v.is_current for v in versions] == [True, False]
    assert read(azure, v1) == b"one" and read(azure, current(v1)) == b"two"


def test_azure_delete_turns_current_into_previous(azure: SimBackend) -> None:
    v1 = put(azure, "data", "f", b"one")
    run(azure.delete(current(v1)))
    versions = run(azure.list_versions(current(v1)))
    assert len(versions) == 1 and not versions[0].is_current
    assert not versions[0].is_delete_marker  # Azure has no delete markers
    assert read(azure, v1) == b"one"
    with pytest.raises(ObjectNotFound):
        run(azure.head(current(v1)))
    v2 = put(azure, "data", "f", b"two")  # writing again makes a new current version
    assert read(azure, current(v2)) == b"two"


def test_s3_delete_adds_a_delete_marker(s3: SimBackend) -> None:
    v1 = put(s3, "b", "f", b"one")
    run(s3.delete(current(v1)))
    versions = run(s3.list_versions(current(v1)))
    assert versions[0].is_delete_marker and versions[0].is_current
    with pytest.raises(ObjectNotFound):
        run(s3.head(current(v1)))
    # Removing the marker brings the object back (S3 undelete).
    run(s3.delete(versions[0].ref))
    assert read(s3, current(v1)) == b"one"


def test_unversioned_overwrite_replaces(clock: VirtualClock) -> None:
    sim = SimBackend(clock, Flavour.AZURE, accounts=[AccountConfig("a", versioning=False)])
    r1 = put(sim, "data", "f", b"one")
    assert r1.version_id is None
    put(sim, "data", "f", b"two")
    assert [v.size for v in run(sim.list_versions(current(r1)))] == [3]
    assert read(sim, current(r1)) == b"two"


def test_unversioned_container_with_a_policy_refuses_overwrite(clock: VirtualClock) -> None:
    sim = SimBackend(clock, Flavour.AZURE, accounts=[AccountConfig("a", versioning=False)])
    sim.create_container("a/data", retention_days=7)
    put(sim, "a/data", "f", b"one")
    put(sim, "a/data", "g", b"new blobs can still be created")
    with pytest.raises(Immutable, match="versioning is off"):
        put(sim, "a/data", "f", b"two")
    with pytest.raises(Immutable):
        run(sim.delete(current(put(sim, "a/data", "h", b"x"))))
    clock.advance(timedelta(days=8))
    put(sim, "a/data", "f", b"two")


def test_worm_versions_are_immutable_until_retention_ends(
    worm: SimBackend, clock: VirtualClock
) -> None:
    v1 = put(worm, "data", "f", b"one")
    run(worm.set_retention(v1, clock.now() + timedelta(days=30)))
    v2 = put(worm, "data", "f", b"two")  # versioning on: the write is a new version [W]
    with pytest.raises(Immutable):
        run(worm.delete(v1))
    run(worm.delete(v2))  # v2 has no retention
    with pytest.raises(ObjectNotFound):  # Azure: deleting the current version leaves none
        run(worm.delete(current(v1)))
    assert read(worm, v1) == b"one"
    clock.advance(timedelta(days=30))
    run(worm.delete(v1))
    assert run(worm.list_versions(current(v1))) == []


def test_container_default_policy_protects_every_version(clock: VirtualClock) -> None:
    sim = SimBackend.azure_worm(clock)
    sim.create_container("data", retention_days=10, policy_locked=True)
    v1 = put(sim, "data", "f", b"one")
    assert run(sim.head(v1)).retention_until == clock.now() + timedelta(days=10)
    with pytest.raises(Immutable):
        run(sim.delete(v1))
    with pytest.raises(Immutable, match="shortened"):
        sim.set_container_policy("data", 5, locked=True)
    with pytest.raises(Immutable):
        sim.set_container_policy("data", 20, locked=False)
    with pytest.raises(Immutable):
        sim.delete_container_policy("data")
    sim.set_container_policy("data", 20, locked=True)  # extending is fine


def test_unlocked_retention_can_shorten_locked_cannot(
    worm: SimBackend, clock: VirtualClock
) -> None:
    ref = put(worm, "data", "f", b"x")
    until = clock.now() + timedelta(days=30)
    run(worm.set_retention(ref, until, locked=False))
    run(worm.set_retention(ref, until - timedelta(days=20), locked=False))  # validation window
    run(worm.set_retention(ref, until))  # lock it
    with pytest.raises(Immutable):
        run(worm.set_retention(ref, until - timedelta(days=1)))
    with pytest.raises(Immutable):
        run(worm.set_retention(ref, until + timedelta(days=1), locked=False))
    run(worm.set_retention(ref, until + timedelta(days=1)))


def test_legal_hold_outlives_retention(worm: SimBackend, clock: VirtualClock) -> None:
    ref = put(worm, "data", "f", b"x")
    run(worm.set_legal_hold(ref, True))
    clock.advance(timedelta(days=3650))
    with pytest.raises(Immutable):
        run(worm.delete(ref))
    assert run(worm.head(ref)).legal_hold
    run(worm.set_legal_hold(ref, False))
    run(worm.delete(ref))


def test_set_tier_on_immutable_version_is_allowed(worm: SimBackend, clock: VirtualClock) -> None:
    ref = put(worm, "data", "f", b"x")
    run(worm.set_retention(ref, clock.now() + timedelta(days=365)))
    run(worm.set_legal_hold(ref, True))
    for tier in (LogicalTier.COLD, LogicalTier.ARCHIVE):
        assert run(worm.set_class(ref, tier)) == ref
    assert run(worm.head(ref)).native_class == "Archive"


def test_worm_is_unsupported_without_it(azure: SimBackend, s3: SimBackend) -> None:
    for sim, container in ((azure, "data"), (s3, "b")):
        ref = put(sim, container, "f", b"x")
        with pytest.raises(UnsupportedOperation):
            run(sim.set_retention(ref, sim.clock.now()))
        with pytest.raises(UnsupportedOperation):
            run(sim.set_legal_hold(ref, True))


def test_worm_needs_versioning(clock: VirtualClock) -> None:
    with pytest.raises(ValueError):
        AccountConfig("a", Redundancy.LRS, versioning=False, version_level_worm=True)
    sim = SimBackend.azure_worm(clock)
    with pytest.raises(UnsupportedOperation):
        sim.create_container("data2", versioning=False, worm=True)


def test_concurrent_overwrites_each_make_a_version(azure: SimBackend) -> None:
    async def many() -> None:
        await asyncio.gather(
            *(azure.put("data", "hot", stream(bytes([i])), None, {}) for i in range(50))
        )

    run(many())
    versions = run(azure.list_versions(ObjectRef(azure.backend_id, "data", "hot")))
    assert len(versions) == 50 and len({v.ref.version_id for v in versions}) == 50
    assert sum(v.is_current for v in versions) == 1
