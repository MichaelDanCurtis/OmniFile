#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-or-later
"""A userspace NFS client for the guard tests, over libnfs (ctypes).

It needs no kernel NFS client, and it never retries: a DELAY (NFSv4) or
JUKEBOX (NFSv3) comes back as an error, so a test sees each answer the
server gave. One operation per run; the result is one JSON line:

  nfsc.py <3|4> <export> ls <dir>          {"ok": true, "names": [...]}
  nfsc.py <3|4> <export> cat <file>        {"ok": true, "data": "..."}
  nfsc.py <3|4> <export> sum <file>        {"ok": true, "bytes": N, "sha256": "...",
                                            "zero_blocks": N} (4 KiB blocks of zeros)
  nfsc.py <3|4> <export> mkdir|rmdir|unlink|creat <path>
  nfsc.py <3|4> <export> trunc <file>      open O_WRONLY|O_TRUNC
  nfsc.py <3|4> <export> write <file> <text>  open O_WRONLY, write
  nfsc.py <3|4> <export> chmod <path> <octal mode>
  nfsc.py <3|4> <export> rename <old> <new>
  nfsc.py <3|4> <export> ranges <file> <off:len,...> <wait s>
                                           one open, a read of each range, each read
                                           retried while the server says "not yet"
                                           (DELAY / JUKEBOX) for up to <wait s>:
                                           {"ok": true, "ranges": [{"sha256", "bytes",
                                           "not_yet", "s"}, ...]}
  nfsc.py <3|4> <export> held <file> <head bytes> <go file> <wait s>
                                           one open held throughout: read <head bytes>,
                                           create "<go file>.started", wait until <go
                                           file> exists, read the rest ("not yet"
                                           retried as above): "sum"'s result, with
                                           "not_yet" and "held_s"

A failure is {"ok": false, "errno": N, "error": "<libnfs message>"}. Paths
are relative to the export's root ("/" is the root). <export> is the NFSv3
path or the NFSv4 pseudo path.
"""

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import sys
import time

O_WRONLY, O_TRUNC = os.O_WRONLY, os.O_TRUNC


def _not_yet(err):
    """A "not yet" answer: NFSv4 DELAY is EAGAIN in libnfs; NFSv3 JUKEBOX
    comes back as EFAULT with the raw status (10008 = 0x2718, little-endian)
    in the message."""
    return err.get("name") == "EAGAIN" or "\x18'" in err.get("error", "")


def _zero_blocks(data):
    zero = bytes(4096)
    return sum(1 for i in range(0, len(data), 4096)
               if data[i:i + 4096] == zero[:len(data[i:i + 4096])])


class Dirent(ctypes.Structure):
    pass


Dirent._fields_ = [("next", ctypes.POINTER(Dirent)), ("name", ctypes.c_char_p)]


def lib():
    name = ctypes.util.find_library("nfs") or "libnfs.so.14"
    n = ctypes.CDLL(name)
    vp, cp, i = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
    pvp = ctypes.POINTER(ctypes.c_void_p)
    n.nfs_init_context.restype = vp
    n.nfs_destroy_context.argtypes = [vp]
    n.nfs_set_version.argtypes = [vp, i]
    n.nfs_set_timeout.argtypes = [vp, i]
    n.nfs_get_error.argtypes = [vp]
    n.nfs_get_error.restype = cp
    n.nfs_mount.argtypes = [vp, cp, cp]
    for f in ("nfs_mkdir", "nfs_rmdir", "nfs_unlink"):
        getattr(n, f).argtypes = [vp, cp]
    n.nfs_rename.argtypes = [vp, cp, cp]
    n.nfs_chmod.argtypes = [vp, cp, i]
    n.nfs_creat.argtypes = [vp, cp, i, pvp]
    n.nfs_open.argtypes = [vp, cp, i, pvp]
    n.nfs_close.argtypes = [vp, vp]
    n.nfs_read.argtypes = [vp, vp, ctypes.c_uint64, vp]
    n.nfs_lseek.argtypes = [vp, vp, ctypes.c_int64, i, ctypes.POINTER(ctypes.c_uint64)]
    n.nfs_write.argtypes = [vp, vp, ctypes.c_uint64, cp]
    n.nfs_opendir.argtypes = [vp, cp, pvp]
    n.nfs_readdir.argtypes = [vp, vp]
    n.nfs_readdir.restype = ctypes.POINTER(Dirent)
    n.nfs_closedir.argtypes = [vp, vp]
    return n


def main(argv):
    vers, export, op, args = int(argv[1]), argv[2], argv[3], argv[4:]
    n = lib()
    ctx = n.nfs_init_context()
    n.nfs_set_version(ctx, vers)
    n.nfs_set_timeout(ctx, 20000)

    def fail(rc):
        msg = n.nfs_get_error(ctx)
        return {"ok": False, "errno": -rc, "name": errno.errorcode.get(-rc, "?"),
                "error": msg.decode(errors="replace") if msg else ""}

    rc = n.nfs_mount(ctx, b"127.0.0.1", export.encode())
    if rc != 0:
        out = fail(rc)
        out["step"] = "mount"
        return out
    path = args[0].encode() if args else b"/"
    fh = ctypes.c_void_p()
    if op == "ls":
        d = ctypes.c_void_p()
        rc = n.nfs_opendir(ctx, path, ctypes.byref(d))
        if rc != 0:
            return fail(rc)
        names = []
        while True:
            e = n.nfs_readdir(ctx, d)
            if not e:
                break
            names.append(e.contents.name.decode(errors="replace"))
        n.nfs_closedir(ctx, d)
        return {"ok": True, "names": sorted(x for x in names if x not in (".", ".."))}
    if op in ("cat", "sum"):
        rc = n.nfs_open(ctx, path, os.O_RDONLY, ctypes.byref(fh))
        if rc != 0:
            return fail(rc)
        buf = ctypes.create_string_buffer(1 << 16)
        data = bytearray()
        while True:
            got = n.nfs_read(ctx, fh, len(buf), buf)
            if got < 0:
                n.nfs_close(ctx, fh)
                return fail(got)
            if got == 0:
                break
            data += buf.raw[:got]
        n.nfs_close(ctx, fh)
        if op == "cat":
            return {"ok": True, "data": data.decode(errors="replace")}
        return {"ok": True, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "zero_blocks": _zero_blocks(data)}
    if op in ("ranges", "held"):
        rc = n.nfs_open(ctx, path, os.O_RDONLY, ctypes.byref(fh))
        if rc != 0:
            return fail(rc)
        wait = float(args[-1])
        buf = ctypes.create_string_buffer(1 << 20)
        pos = ctypes.c_uint64()

        def read(off, length):
            """`length` bytes at `off` (fewer at EOF), each request retried
            while the answer is "not yet"; (bytes, not-yet answers) or an error."""
            got, not_yet = bytearray(), 0
            while len(got) < length:
                end = time.monotonic() + wait
                while True:
                    rc = n.nfs_lseek(ctx, fh, off + len(got), os.SEEK_SET, ctypes.byref(pos))
                    if rc < 0:
                        return fail(rc), not_yet
                    r = n.nfs_read(ctx, fh, min(len(buf), length - len(got)), buf)
                    if r >= 0:
                        break
                    err = fail(r)
                    if not _not_yet(err) or time.monotonic() > end:
                        return err, not_yet
                    not_yet += 1
                    time.sleep(0.5)
                if r == 0:
                    break
                got += buf.raw[:r]
            return bytes(got), not_yet

        if op == "ranges":
            res = []
            for spec in args[1].split(","):
                off, length = (int(x) for x in spec.split(":"))
                t0 = time.monotonic()
                data, not_yet = read(off, length)
                if isinstance(data, dict):
                    n.nfs_close(ctx, fh)
                    data.update(range=spec, not_yet=not_yet, done=res)
                    return data
                res.append({"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                            "not_yet": not_yet, "s": round(time.monotonic() - t0, 3)})
            n.nfs_close(ctx, fh)
            return {"ok": True, "ranges": res}
        head, go = int(args[1]), args[2]
        data, not_yet = read(0, head)
        if isinstance(data, dict):
            n.nfs_close(ctx, fh)
            data["at"] = "head"
            return data
        with open(go + ".started", "w") as f:
            f.write(str(len(data)))
        t0 = time.monotonic()
        while not os.path.exists(go):
            if time.monotonic() - t0 > wait:
                n.nfs_close(ctx, fh)
                return {"ok": False, "error": f"{go} never appeared"}
            time.sleep(0.5)
        held = time.monotonic() - t0
        rest, more = read(len(data), 1 << 62)
        n.nfs_close(ctx, fh)
        if isinstance(rest, dict):
            rest.update(at=len(data), not_yet=not_yet + more)
            return rest
        data += rest
        return {"ok": True, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "zero_blocks": _zero_blocks(data), "not_yet": not_yet + more,
                "held_s": round(held, 1)}
    if op in ("mkdir", "rmdir", "unlink"):
        rc = getattr(n, "nfs_" + op)(ctx, path)
    elif op == "rename":
        rc = n.nfs_rename(ctx, path, args[1].encode())
    elif op == "chmod":
        rc = n.nfs_chmod(ctx, path, int(args[1], 8))
    elif op == "creat":
        rc = n.nfs_creat(ctx, path, 0o644, ctypes.byref(fh))
    elif op in ("trunc", "write"):
        rc = n.nfs_open(ctx, path, O_WRONLY | (O_TRUNC if op == "trunc" else 0),
                        ctypes.byref(fh))
        if rc == 0 and op == "write":
            text = args[1].encode()
            rc = n.nfs_write(ctx, fh, len(text), text)
            rc = 0 if rc == len(text) else (rc if rc < 0 else -errno.EIO)
    else:
        return {"ok": False, "error": "unknown op " + op}
    if rc != 0:
        return fail(rc)
    if fh:
        n.nfs_close(ctx, fh)
    return {"ok": True}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv)))
