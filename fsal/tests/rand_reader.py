#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Random-offset reads of a synthetic object over NFS, every byte checked.

  rand_reader.py <file> --n 1000 [--threads 16] [--bs 65536] [--seed 1]
                 [--direct] [--deadline 300]

The object's bytes are mkstub.py's pattern (every 4 KiB block k holds 512
copies of a non-zero word derived from k), so a read is checked without a
backend file and zeros can never pass. Prints one JSON object: how many
reads were correct, wrong, zero-filled or failed (by errno), and the
latency distribution. A read slower than 5 s on NFSv3 is the 5 s JUKEBOX
retry the parked-read path exists to avoid.
"""
import argparse
import errno
import json
import mmap
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mkstub import pattern  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--bs", type=int, default=65536)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--direct", action="store_true")
    ap.add_argument("--deadline", type=float, default=300)
    a = ap.parse_args()
    size = os.stat(a.file).st_size
    rnd = random.Random(a.seed)
    offs = [rnd.randrange(0, (size - a.bs) // 4096) * 4096 for _ in range(a.n)]
    lock = threading.Lock()
    res = {"correct": 0, "wrong": 0, "zeros": 0, "errors": {}, "lat": []}
    t_end = time.monotonic() + a.deadline

    def worker():
        fd = os.open(a.file, os.O_RDONLY | (os.O_DIRECT if a.direct else 0))
        buf = mmap.mmap(-1, a.bs)
        while True:
            with lock:
                if not offs or time.monotonic() > t_end:
                    break
                off = offs.pop()
            t = time.monotonic()
            try:
                n = os.preadv(fd, [buf], off)
                data = bytes(buf[:n])
                good = data == pattern(off, off + a.bs)[:n] and n == a.bs
                zero = data.count(0) > len(data) // 2
                with lock:
                    res["lat"].append(time.monotonic() - t)
                    if good:
                        res["correct"] += 1
                    else:
                        res["wrong"] += 1
                        res["zeros"] += 1 if zero else 0
            except OSError as e:
                with lock:
                    k = errno.errorcode.get(e.errno, str(e.errno))
                    res["errors"][k] = res["errors"].get(k, 0) + 1
        os.close(fd)

    t0 = time.monotonic()
    ths = [threading.Thread(target=worker) for _ in range(a.threads)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    lat = sorted(res.pop("lat"))

    def q(p):
        return round(lat[min(len(lat) - 1, int(p * len(lat)))], 3) if lat else None
    res.update(n=a.n, total_s=round(time.monotonic() - t0, 2), p50_s=q(0.5), p99_s=q(0.99),
               max_s=round(lat[-1], 3) if lat else None, over_5s=sum(1 for x in lat if x > 5.0),
               not_done=len(offs))
    res["ok"] = (res["correct"] == a.n and not res["wrong"] and not res["errors"])
    print(json.dumps(res), flush=True)


if __name__ == "__main__":
    main()
