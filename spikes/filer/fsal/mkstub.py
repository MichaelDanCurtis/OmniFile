#!/usr/bin/env python3
"""Make a pattern backend object and a stub for it (shared stub convention).

  mkstub.py <stub-path> <key> <size> <resident-bytes> [--backend DIR] [--resident]

The backend object is /srv/omni-b/backend/<key>. Every 4 KiB block k holds
512 copies of a non-zero 64-bit word derived from k, so zeros and misplaced
blocks are both detectable. The stub is a full-size sparse file with only its
first <resident-bytes> written, and user.omnifile.* xattrs set.
With --resident the whole file is written and state=resident.
"""
import argparse
import os

BLK = 4096


def word(k):
    return ((((k + 1) * 0x9E3779B97F4A7C15) + 0x0123456789ABCDEF) % (1 << 64)) | 1


def block(k):
    return word(k).to_bytes(8, "little") * (BLK // 8)


def write_pattern(f, start, end):
    k = start // BLK
    f.seek(k * BLK)
    buf = bytearray()
    while k * BLK < end:
        buf += block(k)
        k += 1
        if len(buf) >= (8 << 20):
            f.write(buf)
            buf = bytearray()
    f.write(buf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("key")
    ap.add_argument("size", type=int)
    ap.add_argument("resident", type=int)
    ap.add_argument("--backend", default="/srv/omni-b/backend")
    ap.add_argument("--resident-file", action="store_true", dest="full")
    a = ap.parse_args()
    assert a.size % BLK == 0 and a.resident % BLK == 0

    src = os.path.join(a.backend, a.key)
    if not (os.path.exists(src) and os.path.getsize(src) == a.size):
        with open(src, "wb") as f:
            write_pattern(f, 0, a.size)
    if os.path.exists(a.path):
        os.unlink(a.path)
    res = a.size if a.full else a.resident
    with open(src, "rb") as fi, open(a.path, "wb") as fo:
        left = res
        while left:
            b = fi.read(min(left, 8 << 20))
            fo.write(b)
            left -= len(b)
        fo.truncate(a.size)
    os.setxattr(a.path, "user.omnifile.key", a.key.encode())
    os.setxattr(a.path, "user.omnifile.resident_bytes", str(res).encode())
    os.setxattr(a.path, "user.omnifile.state", b"resident" if a.full else b"stub")
    os.utime(a.path, (1700000000, 1700000000))
    os.chmod(a.path, 0o644)


if __name__ == "__main__":
    main()
