#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Stand-ins for the smb-guard's view of a file, for t_trunc_guard.sh.

  prewatch.py guard <mountpoint>   a fanotify pre-content group (FAN_PRE_ACCESS,
                                   FAN_MARK_FILESYSTEM) that allows every event
                                   on an unmarked file and NEVER answers one on
                                   a file with a trusted.omnifile marker, as
                                   the smb-guard waits for a fill that will not
                                   come (after a FAILED one). Prints "ready" or
                                   "unsupported", then one line per held event.
  prewatch.py order <file> <secs>  inotify on one file: prints the masks of the
                                   events (IN_ATTRIB for an xattr change,
                                   IN_MODIFY for the truncate) in order, as JSON.
"""
import ctypes
import errno
import json
import os
import select
import struct
import sys
import time

libc = ctypes.CDLL(None, use_errno=True)
MARKER = "trusted.omnifile"


def guard(mnt):
    FAN_CLOEXEC, FAN_CLASS_PRE_CONTENT, FAN_UNLIMITED_QUEUE = 0x1, 0x8, 0x10
    FAN_MARK_ADD, FAN_MARK_FILESYSTEM = 0x1, 0x100
    FAN_PRE_ACCESS, FAN_ALLOW = 0x00100000, 0x1
    libc.fanotify_mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64,
                                   ctypes.c_int, ctypes.c_char_p]
    fd = libc.fanotify_init(FAN_CLOEXEC | FAN_CLASS_PRE_CONTENT | FAN_UNLIMITED_QUEUE,
                            os.O_RDWR | os.O_LARGEFILE)
    if fd < 0 or libc.fanotify_mark(fd, FAN_MARK_ADD | FAN_MARK_FILESYSTEM,
                                    FAN_PRE_ACCESS, -100, mnt.encode()) != 0:
        print("unsupported", errno.errorcode.get(ctypes.get_errno(), "?"), flush=True)
        return
    print("ready", flush=True)
    held = []
    while True:
        buf = os.read(fd, 65536)
        off = 0
        while off + 24 <= len(buf):
            ev_len, _v, _r, _ml, mask, efd, pid = struct.unpack_from("<IBBHQii", buf, off)
            off += ev_len
            if efd < 0:
                continue
            try:
                os.getxattr(efd, MARKER)
                marked = True
            except OSError:
                marked = False
            if marked:
                held.append(efd)  # never answered while this group lives
                print("held pre-content event on a marked file: pid=%d" % pid, flush=True)
            else:
                os.write(fd, struct.pack("<iI", efd, FAN_ALLOW))
                os.close(efd)


def order(path, secs):
    IN_MODIFY, IN_ATTRIB = 0x2, 0x4
    fd = libc.inotify_init1(os.O_NONBLOCK)
    wd = libc.inotify_add_watch(fd, path.encode(), IN_MODIFY | IN_ATTRIB)
    if wd < 0:
        print(json.dumps({"error": ctypes.get_errno()}), flush=True)
        return
    print("ready", flush=True)
    masks, end = [], time.monotonic() + secs
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], max(0.0, end - time.monotonic()))
        if not r:
            continue
        buf = os.read(fd, 4096)
        off = 0
        while off + 16 <= len(buf):
            _wd, mask, _c, nlen = struct.unpack_from("<iIII", buf, off)
            masks.append(("M" if mask & IN_MODIFY else "") + ("A" if mask & IN_ATTRIB else ""))
            off += 16 + nlen
    print(json.dumps({"events": masks}), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "guard":
        guard(sys.argv[2])
    else:
        order(sys.argv[2], float(sys.argv[3]))
