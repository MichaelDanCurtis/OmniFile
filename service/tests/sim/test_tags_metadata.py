"""Index tags and metadata changes."""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnifile_service.backends.base import (
    BackendError,
    Immutable,
    LogicalTier,
    RestoreSpeed,
    UnsupportedOperation,
)
from omnifile_service.backends.sim import AccountConfig, Flavour, SimBackend, VirtualClock

from .helpers import current, put, read, run


def test_tags_survive_the_archive_move_and_work_on_archived_blobs(azure: SimBackend) -> None:
    ref = put(azure, "data", "f", b"x")
    run(azure.set_tags(ref, {"omnifile-state": "uploaded"}))
    archived = run(azure.set_class(ref, LogicalTier.ARCHIVE))
    assert run(azure.get_tags(archived)) == {"omnifile-state": "uploaded"}
    run(azure.set_tags(archived, {"omnifile-state": "archived"}))
    assert run(azure.get_tags(archived)) == {"omnifile-state": "archived"}


def test_tag_limits(s3: SimBackend) -> None:
    ref = put(s3, "b", "k", b"x")
    run(s3.set_tags(ref, {f"k{i}": "v" for i in range(10)}))
    with pytest.raises(BackendError):
        run(s3.set_tags(ref, {f"k{i}": "v" for i in range(11)}))
    with pytest.raises(BackendError):
        run(s3.set_tags(ref, {"k" * 129: "v"}))


def test_tags_allowed_under_worm(worm: SimBackend, clock: VirtualClock) -> None:
    ref = put(worm, "data", "f", b"x")
    run(worm.set_legal_hold(ref, True))
    run(worm.set_tags(ref, {"hold": "legal"}))
    assert run(worm.get_tags(ref)) == {"hold": "legal"}


def test_set_metadata_makes_a_new_version_with_the_same_bytes(azure: SimBackend) -> None:
    ref = put(azure, "data", "f", b"bytes", metadata={"file-owner": "0"})
    run(azure.set_tags(ref, {"t": "1"}))
    new = run(azure.set_metadata(ref, {"file-owner": "1001"}))
    assert new.version_id != ref.version_id
    assert run(azure.head(current(ref))).metadata == {"file-owner": "1001"}
    assert run(azure.head(ref)).metadata == {"file-owner": "0"}
    assert read(azure, new) == b"bytes"
    assert run(azure.get_tags(new)) == {"t": "1"}
    assert run(azure.head(new)).native_class == "Smart"


def test_set_metadata_in_place_without_versioning(clock: VirtualClock) -> None:
    sim = SimBackend(clock, Flavour.AZURE, accounts=[AccountConfig("a", versioning=False)])
    ref = put(sim, "data", "f", b"x")
    assert run(sim.set_metadata(ref, {"file-group": "7"})) == ref
    assert run(sim.head(ref)).metadata == {"file-group": "7"}


def test_set_metadata_refused_when_archived_or_immutable(
    worm: SimBackend, s3: SimBackend, clock: VirtualClock
) -> None:
    ref = put(worm, "data", "f", b"x")
    run(worm.set_retention(ref, clock.now() + timedelta(days=1)))
    with pytest.raises(Immutable):
        run(worm.set_metadata(ref, {"a": "b"}))
    run(worm.set_class(ref, LogicalTier.ARCHIVE))
    with pytest.raises(UnsupportedOperation):
        run(worm.set_metadata(ref, {"a": "b"}))
    glacier = put(s3, "b", "k", b"x", tier=LogicalTier.ARCHIVE)
    with pytest.raises(UnsupportedOperation):
        run(s3.set_metadata(glacier, {"a": "b"}))
    run(s3.restore(glacier, RestoreSpeed.EXPEDITED, days=1))
    clock.advance(timedelta(minutes=5))
    new = run(s3.set_metadata(glacier, {"a": "b"}))  # CopyObject from the restored copy
    assert run(s3.head(new)).metadata == {"a": "b"}
