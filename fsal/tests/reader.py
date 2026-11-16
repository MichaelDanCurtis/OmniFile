#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Read a file over NFS and check every byte against the backend object.

  reader.py seq  <file> <backend> [--direct] [--start N] [--count N] [--bs N]
  reader.py one  <file> <backend> --start N [--bs N] [--direct]
  reader.py seek <file>

Prints one JSON object. "correct" is true only if every byte read matched and
no error happened; a zero-filled block is flagged "mostly_zero" (the failure
the guard exists to prevent). An error is reported with its errno name and
how long it took to arrive.
"""
import argparse
import errno
import json
import mmap
import os
import time

MiB = 1 << 20


def check(data, bf, off):
    bf.seek(off)
    want = bf.read(len(data))
    if data == want:
        return None
    return {"offset": off, "len": len(data), "mostly_zero": data.count(0) > len(data) // 2}


def do_seq(a):
    fd = os.open(a.file, os.O_RDONLY | (os.O_DIRECT if a.direct else 0))
    size = os.fstat(fd).st_size
    end = size if a.count is None else min(size, a.start + a.count)
    bf = open(a.backend, "rb")
    buf = mmap.mmap(-1, a.bs)
    off, bad, err, stalls = a.start, [], None, []
    t0 = time.monotonic()
    while off < end:
        t = time.monotonic()
        want = min(a.bs, end - off)
        try:
            n = os.preadv(fd, [memoryview(buf)[:want]], off)
        except OSError as e:
            err = {"errno": errno.errorcode.get(e.errno, e.errno), "offset": off,
                   "after_s": round(time.monotonic() - t, 3)}
            break
        if time.monotonic() - t > 0.5:
            stalls.append({"offset": off, "s": round(time.monotonic() - t, 2)})
        if n == 0:
            break
        m = check(bytes(buf[:n]), bf, off)
        if m:
            bad.append(m)
        off += n
    os.close(fd)
    return {"mode": "seq", "bytes": off - a.start, "total_s": round(time.monotonic() - t0, 2),
            "stalls": stalls, "n_bad": len(bad), "bad": bad[:3], "error": err,
            "zeros": any(b["mostly_zero"] for b in bad),
            "correct": err is None and not bad and off >= end}


def do_one(a):
    fd = os.open(a.file, os.O_RDONLY | (os.O_DIRECT if a.direct else 0))
    bf = open(a.backend, "rb")
    t = time.monotonic()
    try:
        if a.direct:  # an aligned buffer; every byte comes from the server
            buf = mmap.mmap(-1, a.bs)
            n = os.preadv(fd, [buf], a.start)
            data = bytes(buf[:n])
        else:
            data = os.pread(fd, a.bs, a.start)
        m = check(data, bf, a.start)
        r = {"ok": m is None, "bad": m, "zeros": bool(m and m["mostly_zero"]),
             "s": round(time.monotonic() - t, 3), "n": len(data)}
    except OSError as e:
        r = {"errno": errno.errorcode.get(e.errno, e.errno), "s": round(time.monotonic() - t, 3)}
    os.close(fd)
    return r


def do_seek(a):
    fd = os.open(a.file, os.O_RDONLY)
    out = {"size": os.fstat(fd).st_size}
    for name, whence, off in (("hole_from_0", os.SEEK_HOLE, 0),
                              ("data_from_8M", os.SEEK_DATA, 8 * MiB),
                              ("hole_from_8M", os.SEEK_HOLE, 8 * MiB)):
        try:
            out[name] = os.lseek(fd, off, whence)
        except OSError as e:
            out[name] = errno.errorcode.get(e.errno, e.errno)
    os.close(fd)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["seq", "one", "seek"])
    ap.add_argument("file")
    ap.add_argument("backend", nargs="?")
    ap.add_argument("--direct", action="store_true")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--count", type=int)
    ap.add_argument("--bs", type=int, default=MiB)
    a = ap.parse_args()
    print(json.dumps({"seq": do_seq, "one": do_one, "seek": do_seek}[a.mode](a)), flush=True)


if __name__ == "__main__":
    main()
