"""Upload `complete`: the committed blob must be the blocks the edge declared.

Azure keeps no whole-blob CRC64, and reading the blob back would double the
I/O of every upload (ARCHITECTURE: "no extra read pass"). What Azure does
keep is the committed block list (Get Block List: block names and sizes, in
order, for a blob version), and it checked every block's body against the
`x-ms-content-crc64` the edge sent with it (Put Block). So at `complete`:

1. the edge declares its blocks: `[{id, length, crc64nvme}]`, in order;
2. their CRCs, combined with the lengths, must give the file's CRC64-NVME
   (`crc64nvme`, also declared in the intent);
3. the committed block list of the blob version being recorded must be
   exactly those ids and lengths.

A block list someone else committed with the write SAS (other ids, other
order, missing or extra blocks, other sizes) then fails `complete`. The
version recorded is the one checked, and reads use it, so a later overwrite
through a still-valid write SAS never changes what the catalog points at.
"""

from __future__ import annotations

import re
from typing import Any

from .validate import bad, req, req_int, req_str

POLY_REFLECTED = 0x9A6C9329AC4BC9B5  # CRC-64/NVME, reflected
MAX_BLOCKS = 50_000  # Azure: committed blocks per blob
MAX_BLOCK_BYTES = 4000 * 1024 * 1024  # Azure: Put Block, version 2019-12-12+
_HEX16 = re.compile(r"^[0-9a-f]{16}$")

Matrix = list[int]  # 64 rows: column vectors of a GF(2) 64x64 matrix


def _times(mat: Matrix, vec: int) -> int:
    out, i = 0, 0
    while vec:
        if vec & 1:
            out ^= mat[i]
        vec >>= 1
        i += 1
    return out


def _compose(a: Matrix, b: Matrix) -> Matrix:
    """The operator `a` after `b`."""
    return [_times(a, col) for col in b]


def _one_zero_bit() -> Matrix:
    m = [POLY_REFLECTED]
    row = 1
    for _ in range(63):
        m.append(row)
        row <<= 1
    return m


_IDENTITY: Matrix = [1 << i for i in range(64)]


def _one_zero_byte() -> Matrix:
    bit, m = _one_zero_bit(), _IDENTITY
    for _ in range(8):
        m = _compose(bit, m)
    return m


_ONE_BYTE: Matrix = _one_zero_byte()


def zeros_operator(nbytes: int) -> Matrix:
    """The operator that advances a CRC-64/NVME register over `nbytes` zero bytes."""
    result, base, n = _IDENTITY, _ONE_BYTE, nbytes
    while n:
        if n & 1:
            result = _compose(base, result)
        n >>= 1
        if n:
            base = _compose(base, base)
    return result


def crc64_combine(crc1: int, crc2: int, len2: int, op: Matrix | None = None) -> int:
    """CRC-64/NVME of A || B from crc(A), crc(B) and len(B) (zlib's method:
    with init and xorout all ones, the two cancel). `op` may be a cached
    `zeros_operator(len2)`."""
    if len2 == 0:
        return crc1
    return _times(op or zeros_operator(len2), crc1) ^ crc2


def parse_blocks(value: Any, size: int) -> list[tuple[str, int, int]]:
    """The declared `blocks`: [(id, length, crc)], with lengths adding up to `size`."""
    if not isinstance(value, list) or len(value) > MAX_BLOCKS:
        raise bad(f"blocks must be a list of at most {MAX_BLOCKS}")
    out: list[tuple[str, int, int]] = []
    total = 0
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise bad(f"blocks[{i}] must be an object")
        bid = req_str(item, "id", max_len=88)  # base64 of at most 64 bytes
        length = req_int(item, "length", minimum=0)
        if length > MAX_BLOCK_BYTES:
            raise bad(f"blocks[{i}].length is over {MAX_BLOCK_BYTES}")
        crc = req(item, "crc64nvme")
        if not isinstance(crc, str) or not _HEX16.match(crc):
            raise bad(f"blocks[{i}].crc64nvme must be 16 lowercase hex digits")
        out.append((bid, length, int(crc, 16)))
        total += length
    if total != size:
        raise bad(f"blocks add up to {total} bytes, not {size}")
    return out


def combined_crc(blocks: list[tuple[str, int, int]]) -> int:
    """The whole-file CRC-64/NVME the declared block CRCs add up to."""
    crc = 0  # CRC-64/NVME of no bytes
    ops: dict[int, Matrix] = {}  # every block but the last has the same length
    for _, length, block_crc in blocks:
        op = ops.get(length)
        if op is None:
            op = ops[length] = zeros_operator(length)
        crc = crc64_combine(crc, block_crc, length, op)
    return crc
