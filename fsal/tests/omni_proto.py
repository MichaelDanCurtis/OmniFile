# SPDX-License-Identifier: LGPL-3.0-or-later
"""The trusted.omnifile marker and the FSAL <-> daemon wire format, in Python.

An independent implementation of docs/contracts/fsal-socket.md (sections 3-5
and 8) for the FSAL tests: the fake daemon speaks it, and mkstub writes
markers with it. It is written from the contract, not from the C code, so a
disagreement between the two shows up as a test failure.
"""

import os
import struct

# ---- CRC-32C (Castagnoli) ---------------------------------------------------

_T = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0x82F63B78 if _c & 1 else _c >> 1
    _T.append(_c)


def crc32c(data: bytes) -> int:
    c = 0xFFFFFFFF
    for b in data:
        c = _T[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


assert crc32c(b"123456789") == 0xE3069283

# ---- marker (section 8) -----------------------------------------------------

MARKER_XATTR = "trusted.omnifile"
MAP_XATTR = "trusted.omnifile.map"
MAGIC = 0x31464D4F
RESIDENT, PARTIAL = 1, 2
F_ONLINE, F_ARCHIVED, F_READONLY = 0x1, 0x2, 0x4
F_DIR_UNPOPULATED, F_CHUNK_MAP, F_WORM = 0x8, 0x10, 0x20


def marker(file_id, version_id, size, resident, *, state=None, flags=F_ARCHIVED,
           seq=1, share_id=1, chunk_shift=0, layout=1, magic=MAGIC, bad_crc=False):
    if state is None:
        state = RESIDENT if resident == size else PARTIAL
    body = struct.pack("<IBBHQQQQQQB3x", magic, layout, state, flags, file_id,
                       version_id, size, resident, seq, share_id, chunk_shift)
    crc = crc32c(body)
    if bad_crc:
        crc ^= 0xDEADBEEF
    return body + struct.pack("<I", crc)


def parse_marker(b):
    magic, layout, state, flags, file_id, version_id, size, resident, seq, share_id, cs = \
        struct.unpack("<IBBHQQQQQQB3x", b[:60])
    return dict(magic=magic, layout=layout, state=state, flags=flags, file_id=file_id,
                version_id=version_id, size=size, resident=resident, seq=seq,
                share_id=share_id, chunk_shift=cs,
                crc_ok=crc32c(b[:60]) == struct.unpack("<I", b[60:64])[0])


def read_marker(path):
    try:
        return parse_marker(os.getxattr(path, MARKER_XATTR))
    except OSError:
        return None


def write_marker(path_or_fd, value):
    os.setxattr(path_or_fd, MARKER_XATTR, value)


# The golden vector shared with the Rust codec (edge/crates/omnifile-marker).
GOLDEN = bytes.fromhex(
    "4f4d46310102010008070605040302012a00000000000000000010000000000000"
    "0001000000000007000000000000000300000000000000000000000f6500c4")
assert marker(0x0102030405060708, 42, 1048576, 65536, state=PARTIAL, flags=F_ONLINE,
              seq=7, share_id=3) == GOLDEN

# The chunk-map golden vector (M5): 10 MiB at 4 MiB chunks, chunks 0 and 2.
MAP_GOLDEN = bytes.fromhex(
    "4f4d46310102110008070605040302012a000000000000000000a00000000000"
    "0000000000000000080000000000000003000000000000001600000" "0a2fb3ce0")
MAP_GOLDEN_MAP = bytes([0x05])
assert marker(0x0102030405060708, 42, 10 << 20, 0, state=PARTIAL, flags=F_ONLINE | F_CHUNK_MAP,
              seq=8, share_id=3, chunk_shift=22) == MAP_GOLDEN


def map_len(size, shift):
    chunks = (size + (1 << shift) - 1) >> shift
    return (chunks + 7) // 8


def map_bits(size, shift, chunks):
    """The map value with the given chunk indices set (LSB first)."""
    b = bytearray(map_len(size, shift))
    for c in chunks:
        b[c >> 3] |= 1 << (c & 7)
    return bytes(b)


def range_local(size, resident, shift, mapv, off, end):
    """Contract 6.1 coverage, independently of the C code."""
    end = min(end, size)
    if end <= off or end <= resident:
        return True
    if mapv is None:
        return False
    off = max(off, resident)
    return all(mapv[c >> 3] & (1 << (c & 7)) for c in range(off >> shift, ((end - 1) >> shift) + 1))


assert map_len(10 << 20, 22) == 1 and map_bits(10 << 20, 22, [0, 2]) == MAP_GOLDEN_MAP
assert range_local(10 << 20, 0, 22, MAP_GOLDEN_MAP, 0, 4 << 20)
assert range_local(10 << 20, 0, 22, MAP_GOLDEN_MAP, 8 << 20, 10 << 20)
assert not range_local(10 << 20, 0, 22, MAP_GOLDEN_MAP, 4 << 20, 8 << 20)


def write_map(path_or_fd, value):
    os.setxattr(path_or_fd, MAP_XATTR, value)


def read_map(path):
    try:
        return os.getxattr(path, MAP_XATTR)
    except OSError:
        return None


# ---- wire (sections 3-5) ----------------------------------------------------

HDR = struct.Struct("<HHIQ")
HELLO, HELLO_ACK = 0x0001, 0x8001
READ_INTENT, READ_DECISION = 0x0010, 0x8010
POPULATE_DIR, POPULATE_RESULT = 0x0011, 0x8011
WRITE_CLOSED, NS_HINT, BAD_MARKER = 0x0020, 0x0021, 0x0022
PING, PONG = 0x0030, 0x8030
RECALL_STATE, INVALIDATE, INVALIDATE_ACK = 0x0040, 0x0041, 0x8041
RESYNC_BEGIN, RESYNC_END = 0x0042, 0x0043
OPEN_STATE_QUERY, OPEN_STATE = 0x0044, 0x8044
ERROR = 0x7FFF

FIXED = {HELLO: 32, READ_INTENT: 136, POPULATE_DIR: 80, WRITE_CLOSED: 88, NS_HINT: 120,
         BAD_MARKER: 40, PING: 8, PONG: 8, INVALIDATE_ACK: 8, OPEN_STATE: 8, ERROR: 4}
NAMES = {v: k for k, v in globals().items()
         if isinstance(v, int) and k.isupper() and k in (
             "HELLO", "HELLO_ACK", "READ_INTENT", "READ_DECISION", "POPULATE_DIR",
             "POPULATE_RESULT", "WRITE_CLOSED",
             "NS_HINT", "BAD_MARKER", "PING", "PONG", "RECALL_STATE", "INVALIDATE",
             "INVALIDATE_ACK", "RESYNC_BEGIN", "RESYNC_END", "OPEN_STATE_QUERY",
             "OPEN_STATE", "ERROR")}

DEC_RECHECK, DEC_DELAY, DEC_EIO, DEC_PARK = 1, 2, 3, 4
RS_QUEUED, RS_RESTORING, RS_COPYING, RS_FETCHING = 1, 2, 3, 4
RS_DONE, RS_FAILED, RS_CANCELLED, RS_BLOCKED = 5, 6, 7, 8
FEAT_PARK, FEAT_CHUNK_MAP, FEAT_POPULATE_DIR, FEAT_NS_HINT, FEAT_WRITE_CLOSED = 1, 2, 4, 8, 16
FEAT_OPEN_STATE = 32
POL = {1: "wait", 2: "failfast", 3: "waitquick"}
OPS = {1: "read", 2: "read_plus", 3: "copy_src", 4: "write", 5: "setattr_size",
       6: "fallocate"}
ORIGIN = {0: "request", 1: "open_stateid", 2: "last_opener", 3: "unknown"}


def msg(mtype, req_id, fixed=b"", *strings, flags=0):
    body = fixed + b"".join(struct.pack("<H", len(s)) + s for s in strings)
    return HDR.pack(mtype, flags, HDR.size + len(body), req_id) + body


def obj_ref(fsid, ino, gen, export_id=0, file_id=0):
    return struct.pack("<QQIIQ", fsid, ino, gen, export_id, file_id)


def parse_obj(b):
    fsid, ino, gen, export_id, file_id = struct.unpack("<QQIIQ", b[:32])
    return dict(fsid=fsid, ino=ino, gen=gen, export_id=export_id, file_id=file_id)


def parse_ident(b):
    uid, gid, origin, vers, minor, sec, fam = struct.unpack("<IIBBBBB", b[:13])
    addr = b[16:32]
    port, = struct.unpack("<H", b[32:34])
    clientid, = struct.unpack("<Q", b[40:48])
    if fam == 4:
        a = ".".join(str(x) for x in addr[:4])
    elif fam == 6:
        a = ":".join("%x" % int.from_bytes(addr[i:i + 2], "big") for i in range(0, 16, 2))
    else:
        a = ""
    return dict(uid=uid, gid=gid, origin=ORIGIN.get(origin, origin), nfs="%d.%d" % (vers, minor),
                sec=sec, addr=a, port=port, clientid=clientid)


def strings(b, n):
    out = []
    for _ in range(n):
        if len(b) < 2:
            raise ValueError("short string")
        ln, = struct.unpack("<H", b[:2])
        if len(b) < 2 + ln:
            raise ValueError("string past end")
        out.append(b[2:2 + ln])
        b = b[2 + ln:]
    return out


def parse(rec):
    """Parse one FSAL->daemon record into a dict; raises ValueError."""
    if len(rec) < HDR.size:
        raise ValueError("short header")
    t, flags, length, req_id = HDR.unpack(rec[:HDR.size])
    if length != len(rec):
        raise ValueError("length %d != record %d" % (length, len(rec)))
    f = rec[HDR.size:]
    if t in FIXED and len(f) < FIXED[t]:
        raise ValueError("short fixed part for 0x%04x" % t)
    m = dict(type=NAMES.get(t, "0x%04x" % t), t=t, flags=flags, req_id=req_id)
    if t == HELLO:
        magic, major, minor, feats, pid, inst = struct.unpack("<IHHIIQ", f[:24])
        gv, fb = strings(f[32:], 2)
        m.update(magic=magic, major=major, minor=minor, features=feats, pid=pid,
                 instance=inst, ganesha=gv.decode(), build=fb.decode())
    elif t == READ_INTENT:
        version, off, ln, size, res = struct.unpack("<QQQQQ", f[32:72])
        op, pol, mflags, quick, parked = struct.unpack("<BBHIB", f[72:81])
        principal, path = strings(f[136:], 2)
        m.update(obj=parse_obj(f), version=version, off=off, len=ln, size=size,
                 resident=res, op=OPS.get(op, op), policy=POL.get(pol, pol),
                 marker_flags=mflags, quick_ms=quick, parked=parked,
                 ident=parse_ident(f[88:136]), principal=principal.decode(),
                 path=path.decode(errors="replace"))
    elif t == POPULATE_DIR:
        principal, path = strings(f[80:], 2)
        m.update(obj=parse_obj(f), ident=parse_ident(f[32:80]),
                 principal=principal.decode(), path=path.decode(errors="replace"))
    elif t == BAD_MARKER:
        reason, err = struct.unpack("<II", f[32:40])
        m.update(obj=parse_obj(f), reason=reason, errno=err)
    elif t == WRITE_CLOSED:
        m.update(obj=parse_obj(f), size_after=struct.unpack("<Q", f[32:40])[0],
                 ident=parse_ident(f[40:88]))
    elif t == NS_HINT:
        # Strings: principal, name, and new_name for RENAME only (contract 5.4).
        n = 3 if f[0] == 3 else 2
        s = strings(f[120:], n) + [b""]
        m.update(op=f[0], what=f[1], parent=parse_obj(f[8:40]), target=parse_obj(f[40:72]),
                 ident=parse_ident(f[72:120]), principal=s[0].decode(errors="replace"),
                 name=s[1].decode(errors="replace"), new_name=s[2].decode(errors="replace"))
    elif t in (PING, PONG):
        m.update(mono_ns=struct.unpack("<Q", f[:8])[0])
    elif t == INVALIDATE_ACK:
        m.update(inval_seq=struct.unpack("<Q", f[:8])[0])
    elif t == OPEN_STATE:
        m.update(write_opens=struct.unpack("<I", f[:4])[0])
    elif t == ERROR:
        code, = struct.unpack("<I", f[:4])
        m.update(code=code, message=strings(f[4:], 1)[0].decode(errors="replace"))
    return m


def hello_ack(req_id, *, major=0, minor=1, features=FEAT_PARK | FEAT_NS_HINT | FEAT_WRITE_CLOSED,
              instance=None, max_inflight=4096, park_ms_max=10000, answer_ttl_ms=1000):
    if instance is None:
        instance = int.from_bytes(os.urandom(8), "little")
    return msg(HELLO_ACK, req_id, struct.pack("<HHIQIIII", major, minor, features, instance,
                                              max_inflight, park_ms_max, answer_ttl_ms, 0))


def read_decision(req_id, decision, *, reason=0, eta_s=0xFFFFFFFF, ttl_ms=0, recall_id=0,
                  resident=0):
    return msg(READ_DECISION, req_id, struct.pack("<BBHIIIQQQ", decision, reason, 0, eta_s,
                                                  ttl_ms, 0, recall_id, resident, 0))


POP_DONE, POP_IN_PROGRESS, POP_EIO = 1, 2, 3


def populate_result(req_id, result, eta_ms=0):
    return msg(POPULATE_RESULT, req_id, struct.pack("<B3xI", result, eta_ms))


def recall_state(obj, version_id, recall_id, state, answer, *, eta_s=0xFFFFFFFF,
                 resident=0, valid_ms=10000):
    return msg(RECALL_STATE, 0, obj + struct.pack("<QQBBHIQII", version_id, recall_id, state,
                                                  answer, 0, eta_s, resident, valid_ms, 0))


def invalidate(req_id, obj, what, flags, seq):
    return msg(INVALIDATE, req_id, obj + struct.pack("<IIQ", what, flags, seq))


def open_state_query(req_id, obj):
    return msg(OPEN_STATE_QUERY, req_id, obj)


def error(code, text):
    return msg(ERROR, 0, struct.pack("<I", code), text.encode())


def ping(req_id, ns, t=PING):
    return msg(t, req_id, struct.pack("<Q", ns))


def fsid_of(path):
    """statfs f_fsid as the contract's u64 (val[0] | val[1] << 32)."""
    return os.statvfs(path).f_fsid & 0xFFFFFFFFFFFFFFFF


def ino_gen(path):
    """i_generation via FS_IOC_GETVERSION, as the daemon computes it."""
    import fcntl
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        buf = bytearray(8)
        fcntl.ioctl(fd, 0x80087601, buf)  # FS_IOC_GETVERSION (long)
        return struct.unpack("<Q", bytes(buf))[0] & 0xFFFFFFFF
    finally:
        os.close(fd)
