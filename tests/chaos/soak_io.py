#!/usr/bin/env python3
"""One file operation for the chaos soak (tests/chaos/soak.py), run as a test user.
Stdlib only (the system python3). Prints one JSON object.

  soak_io.py write PATH SIZE SEED     create or overwrite PATH with the seeded pattern,
                                      fsync, close: {"ok", "s"} or an error
  soak_io.py read PATH SIZE SEED      read PATH whole with O_DIRECT (the NFS client's page
                                      cache never answers) and compare with the pattern:
                                      {"ok", "bytes", "zero_blocks", "bad_blocks", "s"}
  soak_io.py check PATH SIZE SEED     the same compare of a local file (an SMB download)
  soak_io.py prefix PATH SIZE SEED    the compare over the local file's own length (a
                                      download that failed part way)
A read that fails part way still reports the zero and bad blocks it saw before the error.
  soak_io.py stat PATH | ls DIR | unlink PATH | mkdir DIR
  soak_io.py pattern PATH SIZE SEED   write the pattern to a local file (an SMB upload)

The pattern (random.Random(seed).randbytes in 1 MiB pieces) has no zero block, so a
4 KiB block of zeros where the pattern has data is a read of an unfilled region. An
error is {"ok": false, "errno": N, "name": "EIO"}: the soak counts errors (failing
closed is allowed) apart from zeros, wrong bytes and ENOENT (never allowed).
"""
import errno
import mmap
import os
import random
import sys
import time

MiB = 1 << 20
BLOCK = 4096


def out(**kw):
    import json

    print(json.dumps(kw))


def err(e, **kw):
    out(ok=False, errno=e.errno, name=errno.errorcode.get(e.errno, str(e.errno)), **kw)


def pieces(size, seed):
    rng = random.Random(seed)
    left = size
    while left:
        n = min(left, MiB)
        yield rng.randbytes(n)
        left -= n


def write(path, size, seed):
    t = time.time()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        for p in pieces(size, seed):
            view = memoryview(p)
            while view:
                view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    out(ok=True, s=round(time.time() - t, 3))


def compare(got, want, base, res):
    if got == want:
        return
    for i in range(0, max(len(got), len(want)), BLOCK):
        g, w = got[i:i + BLOCK], want[i:i + BLOCK]
        if g != w:
            if g and g == bytes(len(g)):
                res["zero_blocks"] += 1
                res.setdefault("first_zero", base + i)
            else:
                res["bad_blocks"] += 1
                res.setdefault("first_bad", base + i)


def scan(chunk, size, seed, res, limit=None):
    """Compare the file, read through chunk(offset, n) -> bytes, with the pattern, in
    order. Counts go into `res` as they are found, so an OSError part way (raised by
    chunk) leaves the counts of everything read before it. `limit` compares only the
    first `limit` bytes (a partial download): no short read is reported then."""
    n = 0
    res["bytes"] = 0
    for want in pieces(size, seed):
        if limit is not None:
            if n >= limit:
                break
            want = want[:limit - n]
        got = b""
        while len(got) < len(want):
            piece = chunk(n + len(got), len(want) - len(got))
            if not piece:
                break
            got += piece
        compare(got, want, n, res)
        n += len(got)
        res["bytes"] = n
        if len(got) < len(want):
            res["short_at"] = n
            return False
    return limit is None and bool(chunk(n, 1))


def read(path, size, seed, direct=True, limit=None):
    t = time.time()
    res = {"zero_blocks": 0, "bad_blocks": 0, "bytes": 0}
    flags = os.O_RDONLY | (os.O_DIRECT if direct else 0)
    extra = False
    try:
        fd = os.open(path, flags)
        try:
            buf = mmap.mmap(-1, MiB)  # page-aligned, as O_DIRECT needs

            def chunk(off, n):
                k = os.preadv(fd, [buf], off)
                return buf[:max(0, min(k, n))]

            extra = scan(chunk, size, seed, res, limit)
        finally:
            os.close(fd)
    except OSError as e:
        # The read failed part way: report what it had already seen. A block of zeros
        # before the error is still a read of an unfilled region.
        out(ok=False, errno=e.errno, name=errno.errorcode.get(e.errno, str(e.errno)),
            s=round(time.time() - t, 3), **res)
        return
    ok = res["zero_blocks"] == 0 and res["bad_blocks"] == 0 and "short_at" not in res and not extra
    out(ok=ok, longer=extra, s=round(time.time() - t, 3), **res)


def main(a):
    op = a[0]
    try:
        if op == "write":
            write(a[1], int(a[2]), int(a[3]))
        elif op == "read":
            read(a[1], int(a[2]), int(a[3]))
        elif op == "check":
            read(a[1], int(a[2]), int(a[3]), direct=False)
        elif op == "prefix":  # a partial download: compare what is there, over its length
            read(a[1], int(a[2]), int(a[3]), direct=False, limit=os.stat(a[1]).st_size)
        elif op == "pattern":
            with open(a[1], "wb") as f:
                for p in pieces(int(a[2]), int(a[3])):
                    f.write(p)
            out(ok=True)
        elif op == "stat":
            st = os.stat(a[1])
            out(ok=True, size=st.st_size, blocks=st.st_blocks)
        elif op == "ls":
            out(ok=True, names=sorted(os.listdir(a[1])))
        elif op == "unlink":
            os.unlink(a[1])
            out(ok=True)
        elif op == "mkdir":
            os.makedirs(a[1], exist_ok=True)
            out(ok=True)
        else:
            out(ok=False, errno=0, name="usage")
    except OSError as e:
        err(e)


if __name__ == "__main__":
    main(sys.argv[1:])
