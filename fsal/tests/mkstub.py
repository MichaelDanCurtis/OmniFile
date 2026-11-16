#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Make a backend object and a placeholder (stub) for it, as the daemon would.

  mkstub.py <path> <file_id> <size> <resident> --backend DIR
            [--online] [--full] [--version N] [--bad-crc] [--bad-magic]
            [--marker-size N] [--synthetic] [--shift S]
            [--map CHUNKS] [--map-len N] [--map-no-flag] [--flag-no-map]
            [--map-extra-bit]

The backend object is DIR/<file_id>. Every 4 KiB block k holds 512 copies of
a non-zero 64-bit word derived from k, so zeros and misplaced blocks are both
caught. The placeholder is created like the daemon creates one (R1 section
5): O_TMPFILE in the target directory, bytes [0, resident) written, size set,
the trusted.omnifile marker set, and only then linked into place, so it is
never visible without its marker.

M5 chunk maps: --map 0,2,7 (or "all") writes those chunks' bytes and sets
their bits in trusted.omnifile.map, and the marker gets CHUNK_MAP with
chunk_shift --shift (default 22, 4 MiB). The other options craft broken
maps for the fail-closed tests. --synthetic computes the object's bytes
instead of storing them (a 100 GiB object needs no backend file).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import omni_proto as P  # noqa: E402

BLK = 4096


def word(k):
    return ((((k + 1) * 0x9E3779B97F4A7C15) + 0x0123456789ABCDEF) % (1 << 64)) | 1


def pattern(start, end):
    k = start // BLK
    out = bytearray()
    while k * BLK < end:
        out += word(k).to_bytes(8, "little") * (BLK // 8)
        k += 1
    return bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("file_id", type=int)
    ap.add_argument("size", type=int)
    ap.add_argument("resident", type=int)
    ap.add_argument("--backend", required=True)
    ap.add_argument("--online", action="store_true", help="ONLINE, not ARCHIVED")
    ap.add_argument("--full", action="store_true", help="RESIDENT: write every byte")
    ap.add_argument("--version", type=int, default=7)
    ap.add_argument("--bad-crc", action="store_true")
    ap.add_argument("--bad-magic", action="store_true")
    ap.add_argument("--marker-size", type=int, help="lie about the size (stale marker)")
    ap.add_argument("--no-marker", action="store_true", help="an ordinary sparse file")
    ap.add_argument("--synthetic", action="store_true", help="no backend file: computed bytes")
    ap.add_argument("--shift", type=int, default=22)
    ap.add_argument("--map", help="chunk indices to make local, comma-separated, or 'all'")
    ap.add_argument("--map-len", type=int, help="write a map of this many bytes (wrong length)")
    ap.add_argument("--map-no-flag", action="store_true", help="a map, but no CHUNK_MAP flag")
    ap.add_argument("--flag-no-map", action="store_true", help="CHUNK_MAP flag, no map xattr")
    ap.add_argument("--map-extra-bit", action="store_true", help="a bit set past the last chunk")
    a = ap.parse_args()
    assert a.size % BLK == 0 and a.resident % BLK == 0
    chunk = 1 << a.shift
    nchunks = (a.size + chunk - 1) // chunk
    if a.map == "all":
        chunks = list(range(nchunks))
    elif a.map:
        chunks = [int(x) for x in a.map.split(",") if x != ""]
    else:
        chunks = []

    os.makedirs(a.backend, exist_ok=True)
    src = os.path.join(a.backend, str(a.file_id))
    if a.synthetic:
        # record the size so the fake daemon knows the object
        with open(src + ".synthetic", "w") as f:
            f.write(str(a.size))
    elif not (os.path.exists(src) and os.path.getsize(src) == a.size):
        with open(src, "wb") as f:
            off = 0
            while off < a.size:
                n = min(8 << 20, a.size - off)
                f.write(pattern(off, off + n))
                off += n
    if os.path.lexists(a.path):
        os.unlink(a.path)
    res = a.size if a.full else a.resident
    fd = os.open(os.path.dirname(os.path.abspath(a.path)), os.O_TMPFILE | os.O_WRONLY, 0o644)
    try:
        left, off = res, 0
        while left:
            n = min(left, 8 << 20)
            os.pwrite(fd, pattern(off, off + n), off)
            off += n
            left -= n
        for c in chunks:
            lo, hi = c * chunk, min(a.size, (c + 1) * chunk)
            off = lo
            while off < hi:
                n = min(hi - off, 8 << 20)
                os.pwrite(fd, pattern(off, off + n), off)
                off += n
        os.ftruncate(fd, a.size)
        os.fsync(fd)
        if not a.no_marker:
            flags = P.F_ONLINE if a.online else P.F_ARCHIVED
            mapped = a.map is not None or a.map_len is not None or a.flag_no_map or \
                a.map_extra_bit
            if mapped and not a.full:
                v = P.map_bits(a.size, a.shift, chunks)
                if a.map_extra_bit:
                    v = bytearray(v)
                    v[-1] |= 0x80
                    v = bytes(v)
                if a.map_len is not None:
                    v = (v + bytes(a.map_len))[:a.map_len]
                if not a.flag_no_map:
                    P.write_map(fd, v)
                if not a.map_no_flag:
                    flags |= P.F_CHUNK_MAP
            m = P.marker(a.file_id, a.version, a.marker_size or a.size, res, flags=flags,
                         chunk_shift=a.shift if flags & P.F_CHUNK_MAP else 0,
                         bad_crc=a.bad_crc, magic=0x12345678 if a.bad_magic else P.MAGIC)
            P.write_marker(fd, m)
        os.fchmod(fd, 0o644)
        os.utime(fd, (1700000000, 1700000000))
        os.link("/proc/self/fd/%d" % fd, a.path, follow_symlinks=True)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
