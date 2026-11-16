"""Object bytes: in memory, spilled to a temp file, or synthetic.

Payloads are immutable once written, so versions and copies share them.
Synthetic payloads have a size but no stored bytes (a deterministic pattern
is generated on read), so tests can model 10 GiB recalls without 10 GiB of
RAM.
"""

from __future__ import annotations

import os
from typing import Protocol

_PATTERN_LEN = 251  # prime, so the pattern does not line up with block sizes


class Payload(Protocol):
    @property
    def size(self) -> int: ...

    def read(self, offset: int, length: int) -> bytes: ...


class MemoryPayload:
    def __init__(self, data: bytes) -> None:
        self._data = data

    @property
    def size(self) -> int:
        return len(self._data)

    def read(self, offset: int, length: int) -> bytes:
        return self._data[offset : offset + length]


class FilePayload:
    def __init__(self, path: str, data: bytes) -> None:
        self._path = path
        self._size = len(data)
        with open(path, "wb") as f:
            f.write(data)

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        with open(self._path, "rb") as f:
            f.seek(offset)
            return f.read(max(0, min(length, self._size - offset)))

    def remove(self) -> None:
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass


class SyntheticPayload:
    """Byte i is (seed + i) % 251. Never materialised."""

    def __init__(self, size: int, seed: int = 0) -> None:
        if size < 0:
            raise ValueError("size must be >= 0")
        self._size = size
        self._seed = seed % _PATTERN_LEN
        self._cycle = bytes((self._seed + i) % _PATTERN_LEN for i in range(2 * _PATTERN_LEN))

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        end = min(offset + length, self._size)
        if offset >= end:
            return b""
        out = bytearray()
        pos = offset
        while pos < end:
            start = pos % _PATTERN_LEN
            n = min(end - pos, _PATTERN_LEN)
            out += self._cycle[start : start + n]
            pos += n
        return bytes(out)


class CorruptPayload:
    """`base` with one byte flipped (an injected silent corruption)."""

    def __init__(self, base: Payload, offset: int) -> None:
        self._base = base
        self._offset = offset

    @property
    def size(self) -> int:
        return self._base.size

    def read(self, offset: int, length: int) -> bytes:
        data = self._base.read(offset, length)
        i = self._offset - offset
        if 0 <= i < len(data):
            data = data[:i] + bytes([data[i] ^ 0xFF]) + data[i + 1 :]
        return data


class TruncatedPayload:
    """The first `size` bytes of `base` (an injected partial copy)."""

    def __init__(self, base: Payload, size: int) -> None:
        self._base = base
        self._size = min(size, base.size)

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        return self._base.read(offset, max(0, min(length, self._size - offset)))
