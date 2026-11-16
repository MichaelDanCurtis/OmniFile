"""CRC-64/NVME in pure Python.

The same algorithm as Azure's x-ms-content-crc64 and S3's CRC64NVME [V]:
normal polynomial 0xad93d23594c93659, reflected input and output (so the
right-shifting table uses the bit-reversed constant 0x9a6c9329ac4bc9b5), init
and xorout all ones. Check value for b"123456789": 0xAE8B14860A799888.

Table driven, one byte at a time: fine for tests and small objects, far too
slow for real seismic files (the edge computes those in Rust). When the
Azure SDK's native CRC64 extension is installed (it is a service
dependency), `crc64nvme` uses it instead, after checking it agrees.
"""

from __future__ import annotations

POLY_NORMAL = 0xAD93D23594C93659
MASK = 0xFFFFFFFFFFFFFFFF


def _reverse64(x: int) -> int:
    return int(f"{x:064b}"[::-1], 2)


POLY_REFLECTED = _reverse64(POLY_NORMAL)  # 0x9a6c9329ac4bc9b5
CHECK_VALUE = 0xAE8B14860A799888


def _make_table() -> tuple[int, ...]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ POLY_REFLECTED if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_TABLE = _make_table()


def _crc64nvme_py(data: bytes, crc: int = 0) -> int:
    c = crc ^ MASK
    table = _TABLE
    for b in data:
        c = table[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ MASK


def _native():
    """The C implementation shipped with azure-storage-blob[ext-checksums]
    (`azure.storage.extensions.checksums.crc64`), if it is installed and
    agrees with this module on the check value and on continuation. It is
    about 100x faster, which the M1 end-to-end test needs (hundreds of MB
    through the sim facade). Otherwise None, and the table loop is used."""
    try:
        from azure.storage.extensions.checksums import crc64  # type: ignore[import-not-found]
    except ImportError:
        return None
    probe = b"123456789" * 37
    try:
        ok = crc64.compute(b"123456789", 0) == CHECK_VALUE and crc64.compute(
            probe[100:], crc64.compute(probe[:100], 0)
        ) == _crc64nvme_py(probe)
    except Exception:  # noqa: BLE001 - any failure means: do not use it
        return None
    return crc64.compute if ok else None


_NATIVE = _native()


def crc64nvme(data: bytes, crc: int = 0) -> int:
    """CRC-64/NVME of `data`. Pass a previous result as `crc` to continue it."""
    if _NATIVE is not None:
        return _NATIVE(bytes(data), crc)
    return _crc64nvme_py(data, crc)


def to_bytes(crc: int) -> bytes:
    """Big-endian 8 bytes, as in S3's x-amz-checksum-crc64nvme.

    Azure's x-ms-content-crc64 is little-endian (the Azure SDK's
    calculate_crc64_bytes uses to_bytes(8, "little")); see sim_http."""
    return crc.to_bytes(8, "big")


def _gf2_times(mat: list[int], vec: int) -> int:
    out = 0
    i = 0
    while vec:
        if vec & 1:
            out ^= mat[i]
        vec >>= 1
        i += 1
    return out


def _gf2_square(mat: list[int]) -> list[int]:
    return [_gf2_times(mat, mat[n]) for n in range(64)]


def crc64nvme_combine(crc1: int, crc2: int, len2: int) -> int:
    """CRC of A+B from crc(A), crc(B) and len(B) (zlib's crc32_combine, widened).

    This is how per-block CRCs become a whole-object value without reading the
    data again (the catalog's Azure whole-file CRC, [V]).
    """
    if len2 <= 0:
        return crc1
    # Operator for one zero bit, then squared to 2 and 4 zero bits.
    odd = [POLY_REFLECTED] + [1 << (n - 1) for n in range(1, 64)]
    even = _gf2_square(odd)
    odd = _gf2_square(even)
    while True:
        even = _gf2_square(odd)
        if len2 & 1:
            crc1 = _gf2_times(even, crc1)
        len2 >>= 1
        if not len2:
            break
        odd = _gf2_square(even)
        if len2 & 1:
            crc1 = _gf2_times(odd, crc1)
        len2 >>= 1
        if not len2:
            break
    return crc1 ^ crc2


def combine_blocks(blocks: list[tuple[int, int]]) -> int:
    """Whole-object CRC from [(block_length, block_crc), ...] in order."""
    total = crc64nvme(b"")
    for length, crc in blocks:
        total = crc64nvme_combine(total, crc, length)
    return total
