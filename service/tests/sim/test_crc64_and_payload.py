from __future__ import annotations

import hashlib
import os

import pytest

from omnifile_service.backends.sim import (
    ChecksumMismatch,
    SimBackend,
    VirtualClock,
    crc64nvme,
    crc64nvme_combine,
)
from omnifile_service.backends.sim.crc64 import (
    CHECK_VALUE,
    POLY_REFLECTED,
    combine_blocks,
    to_bytes,
)
from omnifile_service.backends.sim.payload import SyntheticPayload

from .helpers import put, read, run, stream


def test_crc64_nvme_check_value() -> None:
    # Catches both a wrong polynomial form and wrong init/xorout.
    assert crc64nvme(b"123456789") == 0xAE8B14860A799888 == CHECK_VALUE
    assert POLY_REFLECTED == 0x9A6C9329AC4BC9B5
    assert crc64nvme(b"") == 0


def test_crc64_is_incremental_and_combinable() -> None:
    a, b = os.urandom(1000), os.urandom(333)
    whole = crc64nvme(a + b)
    assert crc64nvme(b, crc64nvme(a)) == whole
    assert crc64nvme_combine(crc64nvme(a), crc64nvme(b), len(b)) == whole
    assert combine_blocks([(len(a), crc64nvme(a)), (len(b), crc64nvme(b))]) == whole


def test_put_records_block_and_whole_checksums(s3: SimBackend, azure: SimBackend) -> None:
    blocks = [b"a" * 100, b"b" * 50, b"c" * 7]
    data = b"".join(blocks)
    ref = put(s3, "b", "k", *blocks)
    head = run(s3.head(ref))
    assert head.crc64nvme == to_bytes(crc64nvme(data))  # S3 stores a full-object CRC64NVME
    assert head.md5 == hashlib.md5(data).digest()
    assert s3.block_checksums(ref) == [(len(x), to_bytes(crc64nvme(x))) for x in blocks]
    aref = put(azure, "data", "k", *blocks)
    assert run(azure.head(aref)).crc64nvme is None  # Azure keeps no whole-blob CRC [V]
    assert azure.block_checksums(aref) == s3.block_checksums(ref)


def test_put_rejects_a_wrong_checksum(s3: SimBackend) -> None:
    with pytest.raises(ChecksumMismatch):
        run(s3.put("b", "k", stream(b"abc"), None, {}, crc64nvme=b"\0" * 8))
    ok = run(s3.put("b", "k", stream(b"abc"), None, {}, crc64nvme=to_bytes(crc64nvme(b"abc"))))
    assert read(s3, ok) == b"abc"


def test_synthetic_payload_is_deterministic_and_sized() -> None:
    p = SyntheticPayload(10 * 1024**3, seed=3)
    assert p.size == 10 * 1024**3
    assert p.read(p.size - 4, 100) == p.read(p.size - 4, 4)
    assert len(p.read(1000, 600)) == 600
    assert p.read(0, 3) == bytes([3, 4, 5])


def test_large_payloads_spill_to_disk(clock: VirtualClock) -> None:
    sim = SimBackend.s3(clock, spill_threshold=16)
    ref = put(sim, "b", "big", b"x" * 1000)
    assert sim._spill_dir is not None and os.listdir(sim._spill_dir)
    assert read(sim, ref, 990, 100) == b"x" * 10
    sim.close()
    assert sim._spill_dir is None
