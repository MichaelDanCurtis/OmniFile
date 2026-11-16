"""Fault injection, and server-side copy (including the decision-24 verifier case)."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from omnifile_service.backends.base import (
    LogicalTier,
    NotRestored,
    Throttled,
)
from omnifile_service.backends.sim import (
    CopyFault,
    InjectedError,
    Op,
    SimBackend,
    Unavailable,
    VirtualClock,
)

from .helpers import put, read, run


def test_fail_nth_call(s3: SimBackend) -> None:
    ref = put(s3, "b", "k", b"x")
    s3.faults.fail_nth(Op.HEAD, 2)
    run(s3.head(ref))
    with pytest.raises(InjectedError):
        run(s3.head(ref))
    run(s3.head(ref))
    s3.faults.fail_nth(Op.GET_RANGE, 1, error=TimeoutError("socket"))
    with pytest.raises(TimeoutError):
        read(s3, ref)
    assert read(s3, ref) == b"x"


def test_injected_throttling(s3: SimBackend) -> None:
    s3.faults.throttle(Op.PUT, times=2, retry_after=timedelta(seconds=3))
    for _ in range(2):
        with pytest.raises(Throttled) as info:
            put(s3, "b", "k", b"x")
        assert info.value.retry_after == timedelta(seconds=3)
    put(s3, "b", "k", b"x")


def test_outage_window(s3: SimBackend, clock: VirtualClock) -> None:
    ref = put(s3, "b", "k", b"x")
    start = clock.now() + timedelta(minutes=1)
    s3.faults.outage(start, start + timedelta(minutes=10), ops={Op.GET_RANGE, Op.HEAD})
    run(s3.head(ref))
    clock.advance(timedelta(minutes=5))
    with pytest.raises(Unavailable):
        run(s3.head(ref))
    put(s3, "b", "k2", b"y")  # not in the outage's op set
    clock.advance(timedelta(minutes=6))
    run(s3.head(ref))


def test_virtual_and_real_latency(s3: SimBackend, clock: VirtualClock) -> None:
    s3.faults.latency(Op.PUT, timedelta(seconds=30), virtual=True, count=1)
    before = clock.now()
    put(s3, "b", "k", b"x")
    assert clock.now() - before == timedelta(seconds=30)
    s3.faults.latency(Op.HEAD, timedelta(milliseconds=20), count=1)
    t = time.monotonic()
    run(s3.head(put(s3, "b", "k2", b"x")))
    assert time.monotonic() - t >= 0.015


def test_copy_across_accounts(azure: SimBackend) -> None:
    src = put(azure, "data", "f", b"payload", metadata={"file-owner": "1001"})
    dst = run(azure.copy_object(src, "archive/staging", "f2", LogicalTier.WARM))
    assert dst.container == "archive/staging"
    assert read(azure, dst) == b"payload"
    head = run(azure.head(dst))
    assert head.native_class == "Cool" and head.metadata == {"file-owner": "1001"}
    # No tier: the destination account's default (Smart on the live account).
    back = run(azure.copy_object(dst, "data", "f3"))
    assert run(azure.head(back)).native_class == "Smart"


def test_copy_of_an_archived_object_needs_a_restore(azure: SimBackend) -> None:
    archived = run(azure.set_class(put(azure, "data", "f", b"x"), LogicalTier.ARCHIVE))
    with pytest.raises(NotRestored):
        run(azure.copy_object(archived, "data", "copy"))


@pytest.mark.parametrize("mode", [CopyFault.CORRUPT, CopyFault.PARTIAL])
def test_damaged_copy_is_caught_only_by_a_read_back(s3: SimBackend, mode: CopyFault) -> None:
    """Decision 24: verify every copied file by reading it, not by its properties."""
    data = bytes(range(256)) * 8
    src = put(s3, "src", "f", data)
    s3.faults.corrupt_next_copy(mode)
    dst = run(s3.copy_object(src, "dst", "f"))
    src_head, dst_head = run(s3.head(src)), run(s3.head(dst))
    assert dst_head.crc64nvme == src_head.crc64nvme  # the recorded value is stale
    truth_src = run(s3.compute_checksum(src))
    truth_dst = run(s3.compute_checksum(dst))
    assert truth_src.crc64nvme == src_head.crc64nvme
    assert truth_dst.crc64nvme != truth_src.crc64nvme
    assert read(s3, dst) != data
    clean = run(s3.copy_object(src, "dst", "g"))  # the fault was used up
    assert run(s3.compute_checksum(clean)) == truth_src


def test_s3_compute_checksum_works_on_archived_objects(s3: SimBackend) -> None:
    ref = put(s3, "b", "k", b"glacier bytes", tier=LogicalTier.ARCHIVE)
    with pytest.raises(NotRestored):
        read(s3, ref)
    assert run(s3.compute_checksum(ref)).crc64nvme == run(s3.head(ref)).crc64nvme
