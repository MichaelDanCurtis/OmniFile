#!/usr/bin/env python3
"""Read a file over NFS and check every byte against the backend object.

  reader.py seq  <file> <backend> [--direct] [--start N] [--count N] [--bs N]
  reader.py one  <file> <backend> --start N [--bs N] [--repeat K] [--gap S]
  reader.py seek <file>

Prints one JSON object. Any zero block or mismatch is reported; a read error
is reported with its errno name and how long it took to arrive.
"""
import argparse
import errno
import json
import mmap
import os
import sys
import time

MiB = 1 << 20


def aligned_buf(n):
    return mmap.mmap(-1, n)


def check(data, backend_f, off):
    backend_f.seek(off)
    want = backend_f.read(len(data))
    if data == want:
        return None
    zero = data.count(0) > len(data) // 2
    return {"offset": off, "len": len(data), "mostly_zero": zero}


def do_seq(a):
    flags = os.O_RDONLY | (os.O_DIRECT if a.direct else 0)
    t0 = time.monotonic()
    fd = os.open(a.file, flags)
    size = os.fstat(fd).st_size
    end = size if a.count is None else min(size, a.start + a.count)
    bf = open(a.backend, "rb")
    buf = aligned_buf(a.bs)
    off, stalls, bad, err = a.start, [], [], None
    while off < end:
        t = time.monotonic()
        try:
            n = os.preadv(fd, [buf], off)
        except OSError as e:
            err = {"errno": errno.errorcode.get(e.errno, e.errno), "offset": off,
                   "after_s": round(time.monotonic() - t, 3)}
            break
        dt = time.monotonic() - t
        if dt > 0.5:
            stalls.append({"offset": off, "s": round(dt, 2)})
        if n == 0:
            break
        m = check(bytes(buf[:n]), bf, off)
        if m:
            bad.append(m)
        off += n
    os.close(fd)
    return {"mode": "seq", "direct": a.direct, "bytes": off - a.start,
            "total_s": round(time.monotonic() - t0, 2), "stalls": stalls,
            "bad_blocks": bad[:5], "n_bad": len(bad), "error": err,
            "correct": err is None and not bad and off >= end}


def do_one(a):
    fd = os.open(a.file, os.O_RDONLY)
    bf = open(a.backend, "rb")
    res = []
    for i in range(a.repeat):
        if i:
            time.sleep(a.gap)
        t = time.monotonic()
        try:
            data = os.pread(fd, a.bs, a.start)
            m = check(data, bf, a.start)
            res.append({"try": i, "ok": m is None, "bad": m,
                        "s": round(time.monotonic() - t, 3)})
        except OSError as e:
            res.append({"try": i, "errno": errno.errorcode.get(e.errno, e.errno),
                        "s": round(time.monotonic() - t, 3)})
    os.close(fd)
    # and once more with a fresh open
    fd = os.open(a.file, os.O_RDONLY)
    t = time.monotonic()
    try:
        data = os.pread(fd, a.bs, a.start)
        fresh = {"ok": check(data, bf, a.start) is None, "s": round(time.monotonic() - t, 3)}
    except OSError as e:
        fresh = {"errno": errno.errorcode.get(e.errno, e.errno), "s": round(time.monotonic() - t, 3)}
    os.close(fd)
    return {"mode": "one", "offset": a.start, "tries": res, "fresh_open": fresh}


def do_seek(a):
    fd = os.open(a.file, os.O_RDONLY)
    size = os.fstat(fd).st_size
    out = {"mode": "seek", "size": size}
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
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--gap", type=float, default=0.0)
    a = ap.parse_args()
    r = {"seq": do_seq, "one": do_one, "seek": do_seek}[a.mode](a)
    print(json.dumps(r))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
