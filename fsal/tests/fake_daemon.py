#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-3.0-or-later
"""A fake omnifile-edge daemon: the daemon side of docs/contracts/fsal-socket.md.

It lets FSAL_OMNI be tested on its own. It serves the SEQPACKET socket, does
the HELLO handshake and resync, answers READ_INTENT with scripted policies,
"recalls" a file by copying bytes from a backend directory (fill, fsync, then
widen the marker), pushes RECALL_STATE, and can run the stub handshake
(narrow the marker, INVALIDATE with ACK_REQUIRED, punch only once acked).

  fake_daemon.py --root DIR --backend DIR [--log FILE] [--mode MODE]
                 [--recall-delay S] [--fetch-delay S] [--no-park] ...

Modes:
  normal            the protocol as written (default)
  hang-hello        accept connections, never answer anything
  hang              answer HELLO, then never answer anything again
  bad-version       answer HELLO with proto_major 1
  malformed         answer HELLO, then send a record whose length lies
  decision:<code>   answer every READ_INTENT with this decision code
                    (1 RECHECK, 2 DELAY, 3 EIO, 4 PARK, anything else garbage)

Every received message is logged as one JSON line. A control socket (--ctl)
takes JSON-line commands from the tests: {"cmd": "stub", "path": ...,
"resident": N}, {"cmd": "fetching" | "done", "path": ...}, {"cmd":
"invalidate", "path": ..., "fsid": N}, {"cmd": "flood", "n": N},
{"cmd": "push", "path": ..., "n": N, "every_ms": M}, {"cmd": "stats"}.

The socket reader of each connection never touches the disk: it answers
HELLO, PING and INVALIDATE_ACK at once (contract 5.6), and a per-connection
worker logs every message and handles READ_INTENT and POPULATE_DIR in order.

M5 (--chunk-map): the daemon offers CHUNK_MAP and answers HELLO with minor
2 (--proto-minor N overrides it; a minor below 2 never gives CHUNK_MAP). A
READ_INTENT for an ONLINE range is fetched chunk by chunk into the
placeholder (--fetchers threads, --chunk-delay seconds per chunk): fill
fence once per burst (FETCHING + acked INVALIDATE CONTENT), then per chunk
pwrite, fdatasync, map bit, marker (CHUNK_MAP), and a RECALL_STATE FETCHING
push (recall_id 0) that resumes the parked reads; DONE when nothing is in
flight for the file.

POPULATE_DIR (rewind trees, fsal-namespace.md section 7): a READONLY,
DIR_UNPOPULATED directory gets, after --populate-delay seconds, a file
v.txt ("as of <path>") with a READONLY marker and an unpopulated
subdirectory d; then its own flag is cleared, last, as the daemon does.
"""

import argparse
import json
import os
import queue
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import omni_proto as P  # noqa: E402

LOCK = threading.Lock()


class Conn:
    def __init__(self, sock, peer):
        self.sock = sock
        self.peer = peer
        self.features = 0
        self.lock = threading.Lock()
        self.acks = {}
        self.ack_cv = threading.Condition()
        self.alive = True
        # HELLO_ACK sent: pushes and INVALIDATEs go only to ready
        # connections (an FSAL closes on any message before HELLO_ACK)
        self.ready = False

    def send(self, rec):
        with self.lock:
            try:
                self.sock.send(rec)
                return True
            except OSError:
                self.alive = False
                return False


class Daemon:
    def __init__(self, a):
        self.a = a
        self.conns = []
        self.logf = open(a.log, "a", buffering=1) if a.log else None
        self.recalls = {}  # (file_id, version) -> dict
        self.populating = {}  # rewind directories being filled
        self.next_req = (1 << 63) | 1
        self.seq = 1000
        self.stats = {"read_intent": 0, "hello": 0, "conns": 0, "chunks_fetched": 0,
                      "chunk_intents": 0, "max_parked_flag": 0}
        self.chunk_files = {}  # file_id -> dict(inflight=set, lock, ...)
        self.chunk_q = []
        self.chunk_cv = threading.Condition()
        self.ino_cache = {}
        self.instance = int.from_bytes(os.urandom(8), "little")

    def log(self, _t=None, **kw):
        kw["t"] = _t if _t is not None else round(time.time(), 3)
        if self.a.log_stall and "type" in kw:
            time.sleep(self.a.log_stall)  # a congested disk (--log-stall)
        if self.logf:
            self.logf.write(json.dumps(kw, default=str) + "\n")

    def req_id(self):
        with LOCK:
            self.next_req += 1
            return self.next_req

    def broadcast(self, rec):
        for c in list(self.conns):
            if c.alive and c.ready:
                c.send(rec)

    # ---- files ----------------------------------------------------------
    def find(self, fsid, ino):
        p = self.ino_cache.get(ino)
        if p is not None:
            try:
                if os.lstat(p).st_ino == ino:
                    return p
            except OSError:
                pass
        for dp, _dn, fn in os.walk(self.a.root):
            for f in fn:
                p = os.path.join(dp, f)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if st.st_ino == ino:
                    self.ino_cache[ino] = p
                    return p
        return None

    def find_dir(self, ino):
        for dp, dn, _fn in os.walk(self.a.root):
            for d in dn:
                p = os.path.join(dp, d)
                try:
                    if os.lstat(p).st_ino == ino:
                        return p
                except OSError:
                    continue
        return None

    def populate(self, c, m):
        """Fill a rewind directory, then answer (never before)."""
        path = self.find_dir(m["obj"]["ino"])
        mk = P.read_marker(path) if path else None
        if mk is None or not mk["flags"] & P.F_READONLY:
            c.send(P.populate_result(m["req_id"], P.POP_EIO))
            self.log(event="populate", path=path, result="eio")
            return
        with LOCK:
            busy = self.populating.get(path)
            if busy is None:
                self.populating[path] = True
        if busy:
            c.send(P.populate_result(m["req_id"], P.POP_IN_PROGRESS))
            return

        def run():
            time.sleep(self.a.populate_delay)
            if mk["flags"] & P.F_DIR_UNPOPULATED:
                rel = os.path.relpath(path, self.a.root)
                f = os.path.join(path, "v.txt")
                body = ("as of %s\n" % rel).encode()
                with open(f, "wb") as fp:
                    fp.write(body)
                P.write_marker(f, P.marker(7, 1, len(body), len(body), flags=P.F_READONLY,
                                           seq=self.next_seq()))
                os.chmod(f, 0o444)
                d = os.path.join(path, "d")
                os.mkdir(d, 0o555)
                P.write_marker(d, P.marker(8, 0, 0, 0, flags=P.F_READONLY | P.F_DIR_UNPOPULATED,
                                           seq=self.next_seq()))
                P.write_marker(path, P.marker(mk["file_id"], 0, 0, 0, flags=P.F_READONLY,
                                              seq=self.next_seq()))
            with LOCK:
                self.populating.pop(path, None)
                self.stats["populated"] = self.stats.get("populated", 0) + 1
            c.send(P.populate_result(m["req_id"], P.POP_DONE))
            self.log(event="populate", path=path, result="done")
        threading.Thread(target=run, daemon=True).start()

    def obj_of(self, path, file_id=0):
        st = os.stat(path)
        return P.obj_ref(P.fsid_of(path), st.st_ino, P.ino_gen(path), 0, file_id)

    # ---- recall ---------------------------------------------------------
    def recall(self, m, path):
        mk = P.read_marker(path)
        key = (m["obj"]["file_id"], m["version"])
        with LOCK:
            r = self.recalls.get(key)
            if r is None:
                rid = len(self.recalls) + 1
                delay = self.a.fetch_delay if (mk and mk["flags"] & P.F_ONLINE and
                                               not mk["flags"] & P.F_ARCHIVED) \
                    else self.a.recall_delay
                r = dict(id=rid, path=path, start=time.time(), delay=delay, done=False,
                         obj=P.obj_ref(m["obj"]["fsid"], m["obj"]["ino"], m["obj"]["gen"],
                                       0, m["obj"]["file_id"]),
                         version=m["version"], answer=None)
                self.recalls[key] = r
                if not self.a.no_fill:
                    threading.Thread(target=self.fill, args=(r,), daemon=True).start()
        return r

    def fill(self, r):
        end = r["start"] + r["delay"]
        while time.time() < end:
            # progress pushes: the FSAL answers retries from memory
            self.broadcast(P.recall_state(r["obj"], r["version"], r["id"], P.RS_RESTORING,
                                          r["answer"] or P.DEC_DELAY,
                                          eta_s=max(1, int(end - time.time())), valid_ms=5000))
            time.sleep(min(1.0, max(0.05, end - time.time())))
        path = r["path"]
        mk = P.read_marker(path)
        if mk is None or self.fence(r, mk) is None:
            self.log(event="fill_fence_failed", path=path)
            r["done"] = True
            return
        if self.a.fill_hold:
            time.sleep(self.a.fill_hold)  # a slow fetch, for the fence test
        src = os.path.join(self.a.backend, str(mk["file_id"]))
        fd = os.open(path, os.O_WRONLY)
        try:
            with open(src, "rb") as f:
                f.seek(mk["resident"])
                off = mk["resident"]
                while True:
                    b = f.read(8 << 20)
                    if not b:
                        break
                    if not self.same_marker(path, mk):
                        self.log(event="fill_stopped", path=path, reason="marker changed")
                        r["done"] = True
                        return
                    os.pwrite(fd, b, off)
                    off += len(b)
            os.fsync(fd)
            if not self.same_marker(path, mk):
                self.log(event="fill_stopped", path=path, reason="marker changed")
                r["done"] = True
                return
            # widen only after the bytes are durable (R1 section 5), and
            # never recreate a marker the FSAL removed (XATTR_REPLACE)
            st = os.fstat(fd)
            os.utime(fd, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.setxattr(fd, P.MARKER_XATTR,
                        P.marker(mk["file_id"], mk["version_id"], mk["size"], mk["size"],
                                 flags=mk["flags"], seq=self.next_seq()), os.XATTR_REPLACE)
        finally:
            os.close(fd)
        r["done"] = True
        self.log(event="filled", path=path, file_id=mk["file_id"])
        self.broadcast(P.recall_state(r["obj"], r["version"], r["id"], P.RS_DONE, P.DEC_DELAY,
                                      resident=mk["size"], valid_ms=1000))

    def next_seq(self):
        with LOCK:
            self.seq += 1
            return self.seq

    # ---- M5: chunk fetches ------------------------------------------------
    def src_read(self, file_id, off, n):
        src = os.path.join(self.a.backend, str(file_id))
        if os.path.exists(src + ".synthetic"):
            from mkstub import pattern
            return pattern(off, off + n)[:n]
        with open(src, "rb") as f:
            f.seek(off)
            return f.read(n)

    def chunk_request(self, m, path, mk):
        """Queue the chunks [off, off+len) needs; True if any is pending."""
        shift = mk["chunk_shift"] or self.a.chunk_shift
        size = mk["size"]
        end = min(size, m["off"] + max(1, m["len"]))
        first = max(m["off"], mk["resident"]) >> shift
        last = (end - 1) >> shift
        mapv = P.read_map(path) if mk["flags"] & P.F_CHUNK_MAP else None
        key = mk["file_id"]
        with self.chunk_cv:
            f = self.chunk_files.get(key)
            if f is None:
                f = dict(inflight=set(), lock=threading.Lock(), fenced=False, path=path,
                         obj=P.obj_ref(m["obj"]["fsid"], m["obj"]["ino"], m["obj"]["gen"], 0,
                                       mk["file_id"]),
                         version=mk["version_id"], shift=shift)
                self.chunk_files[key] = f
            pending = False
            for c in range(first, last + 1):
                if mapv is not None and (c >> 3) < len(mapv) and mapv[c >> 3] & (1 << (c & 7)):
                    continue
                pending = True
                if c not in f["inflight"]:
                    f["inflight"].add(c)
                    self.chunk_q.append((key, c))
            self.chunk_cv.notify_all()
        return pending

    def chunk_worker(self):
        while True:
            with self.chunk_cv:
                while not self.chunk_q:
                    self.chunk_cv.wait()
                key, c = self.chunk_q.pop(0)
                f = self.chunk_files[key]
            try:
                self.fetch_chunk(f, key, c)
            except Exception as e:  # noqa: BLE001
                self.log(event="chunk_error", file_id=key, chunk=c, error=repr(e))
            with self.chunk_cv:
                f["inflight"].discard(c)
                idle = not f["inflight"]
                if idle:
                    f["fenced"] = False
            if idle:
                self.broadcast(P.recall_state(f["obj"], f["version"], 0, P.RS_DONE, P.DEC_DELAY,
                                              valid_ms=1000))

    def fetch_chunk(self, f, key, c):
        path = f["path"]
        with f["lock"]:
            mk = P.read_marker(path)
            if mk is None or mk["file_id"] != key:
                return
            if not f["fenced"]:
                # the fill fence (contract 6.1), once per burst of fetches
                self.broadcast(P.recall_state(f["obj"], f["version"], 0, P.RS_FETCHING,
                                              P.DEC_DELAY, valid_ms=5000))
                ok, _ = self.inval_all(f["obj"], 0x4)
                if not ok:
                    self.log(event="chunk_fence_failed", path=path)
                    return
                f["fenced"] = True
        if self.a.chunk_delay:
            time.sleep(self.a.chunk_delay)
        shift = f["shift"]
        lo, hi = c << shift, min(mk["size"], (c + 1) << shift)
        data = self.src_read(key, lo, hi - lo)
        with f["lock"]:
            if not self.same_marker(path, mk) and not self.same_content(path, mk):
                self.log(event="chunk_stopped", path=path, reason="marker changed")
                return
            fd = os.open(path, os.O_WRONLY)
            try:
                os.pwrite(fd, data, lo)
                os.fdatasync(fd)
                now = P.read_marker(path)
                if now is None or now["version_id"] != mk["version_id"] or \
                        now["size"] != mk["size"]:
                    return
                v = bytearray(P.read_map(path) or b"") if now["flags"] & P.F_CHUNK_MAP else \
                    bytearray()
                want = P.map_len(mk["size"], shift)
                if len(v) != want:
                    v = bytearray(want)
                v[c >> 3] |= 1 << (c & 7)
                # widen: the map first, then the marker (contract 8)
                os.setxattr(fd, P.MAP_XATTR, bytes(v))
                st = os.fstat(fd)
                os.utime(fd, ns=(st.st_atime_ns, st.st_mtime_ns))
                os.setxattr(fd, P.MARKER_XATTR,
                            P.marker(now["file_id"], now["version_id"], now["size"],
                                     now["resident"], state=P.PARTIAL,
                                     flags=now["flags"] | P.F_CHUNK_MAP, seq=self.next_seq(),
                                     share_id=now["share_id"], chunk_shift=shift),
                            os.XATTR_REPLACE)
            finally:
                os.close(fd)
            self.stats["chunks_fetched"] += 1
        # a widening: parked reads re-check the marker and the map
        self.broadcast(P.recall_state(f["obj"], f["version"], 0, P.RS_FETCHING, P.DEC_DELAY,
                                      valid_ms=5000))

    @staticmethod
    def same_content(path, mk):
        now = P.read_marker(path)
        try:
            size = os.stat(path).st_size
        except OSError:
            return False
        return (now is not None and now["file_id"] == mk["file_id"] and
                now["version_id"] == mk["version_id"] and now["size"] == mk["size"] and
                size == mk["size"])

    # ---- acknowledged invalidation -----------------------------------------
    def inval_all(self, obj, what):
        """INVALIDATE with ACK_REQUIRED on every live connection; True only
        if every live connection acked within 1 s (a dead one needs none).
        A live connection still in its handshake cannot ack: not acked."""
        pending = []
        ok = True
        for c in list(self.conns):
            if not c.alive:
                continue
            if not c.ready:
                ok = False
                continue
            rid, seq = self.req_id(), self.next_seq()
            c.send(P.invalidate(rid, obj, what, 0x1, seq))
            pending.append((c, rid, seq))
        for c, rid, seq in pending:
            with c.ack_cv:
                deadline = time.time() + 1.0
                while rid not in c.acks and time.time() < deadline and c.alive:
                    c.ack_cv.wait(max(0.0, deadline - time.time()))
                if c.acks.get(rid) != seq and c.alive:
                    ok = False
        return ok, len(pending)

    def fence(self, r, mk):
        """The fill fence (contract 6.1): push FETCHING, then an acked
        INVALIDATE what=CONTENT. Returns the marker to compare against
        before every write, or None if the fence could not be raised."""
        self.broadcast(P.recall_state(r["obj"], r["version"], r["id"], P.RS_FETCHING,
                                      r["answer"] or P.DEC_DELAY, valid_ms=5000))
        ok, _ = self.inval_all(r["obj"], 0x4)
        return mk if ok else None

    @staticmethod
    def same_marker(path, mk):
        now = P.read_marker(path)
        try:
            size = os.stat(path).st_size
        except OSError:
            return False
        return (now is not None and now["seq"] == mk["seq"] and
                now["version_id"] == mk["version_id"] and now["size"] == mk["size"] and
                size == mk["size"])

    # ---- the stub handshake (R1 5.1, steps 3-6) ---------------------------
    def stub(self, path, resident):
        mk = P.read_marker(path)
        if mk is None:
            return {"error": "no marker"}
        size = mk["size"]
        flags = mk["flags"]
        # 3. narrow the marker
        P.write_marker(path, P.marker(mk["file_id"], mk["version_id"], size, resident,
                                      flags=flags, seq=self.next_seq()))
        # 4. acknowledged invalidation on every live connection
        obj = self.obj_of(path, mk["file_id"])
        t0 = time.time()
        ok, nconn = self.inval_all(obj, 0x1 | 0x2)
        t1 = time.time()
        ack_ms = round((t1 - t0) * 1000, 1)
        # 5. no ack: do not punch
        if not ok:
            return {"acked": False, "punched": False, "ack_ms": ack_ms}
        # 6. punch
        fd = os.open(path, os.O_WRONLY)
        try:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            FALLOC_FL_KEEP_SIZE, FALLOC_FL_PUNCH_HOLE = 1, 2
            rc = libc.fallocate(fd, FALLOC_FL_KEEP_SIZE | FALLOC_FL_PUNCH_HOLE,
                                ctypes.c_long(resident), ctypes.c_long(size - resident))
            if rc != 0:
                return {"acked": True, "punched": False, "errno": ctypes.get_errno()}
        finally:
            os.close(fd)
        with LOCK:
            for k in [k for k in self.recalls if k[0] == mk["file_id"]]:
                del self.recalls[k]
        # t_sent, t_ack: epoch seconds of the INVALIDATE and its ack, which
        # t_marker_cache compares with the slowxattr shim's hold log
        return {"acked": True, "punched": True, "ack_ms": ack_ms, "conns": nconn,
                "t_sent": t0, "t_ack": t1}

    # ---- protocol -------------------------------------------------------
    def on_read_intent(self, c, m):
        self.stats["read_intent"] += 1
        mode = self.a.mode
        if mode.startswith("decision:"):
            c.send(P.read_decision(m["req_id"], int(mode.split(":")[1])))
            return
        path = self.find(m["obj"]["fsid"], m["obj"]["ino"])
        if path is None:
            c.send(P.read_decision(m["req_id"], P.DEC_EIO, reason=6))
            return
        mk = P.read_marker(path)
        if mk and mk["resident"] >= min(mk["size"], m["off"] + m["len"]) and m["op"] in (
                "read", "read_plus", "copy_src"):
            c.send(P.read_decision(m["req_id"], P.DEC_RECHECK))
            return
        online = mk and mk["flags"] & P.F_ONLINE and not mk["flags"] & P.F_ARCHIVED
        if online and (c.features & P.FEAT_CHUNK_MAP) and m["op"] in ("read", "read_plus"):
            self.stats["chunk_intents"] += 1
            if m["parked"]:
                self.stats["max_parked_flag"] += 1
            if not self.chunk_request(m, path, mk):
                c.send(P.read_decision(m["req_id"], P.DEC_RECHECK))
                return
            dec = P.DEC_PARK if c.features & P.FEAT_PARK else P.DEC_DELAY
            c.send(P.read_decision(m["req_id"], dec, reason=3, ttl_ms=self.a.ttl_ms))
            return
        r = self.recall(m, path)
        left = max(0.0, r["start"] + r["delay"] - time.time())
        if online and (c.features & P.FEAT_PARK) and m["op"] in ("read", "read_plus"):
            dec = P.DEC_PARK
        elif m["policy"] == "failfast":
            dec = P.DEC_EIO
        elif m["policy"] == "waitquick":
            dec = P.DEC_DELAY if left * 1000 < m["quick_ms"] else P.DEC_EIO
        else:
            dec = P.DEC_DELAY
        r["answer"] = dec if dec in (P.DEC_DELAY, P.DEC_EIO) else P.DEC_DELAY
        c.send(P.read_decision(m["req_id"], dec, reason=1, eta_s=int(left) + 1,
                               ttl_ms=self.a.ttl_ms, recall_id=r["id"]))

    def serve(self, c):
        """The connection's socket reader. It never touches the disk:
        contract 5.6 wants a PING answered at once, and a log write or a
        marker read or a tree walk (find) can stall for seconds on a loaded
        host, after which the FSAL declares the daemon dead (3 s without
        traffic) and fails closed.
        HELLO, PING and INVALIDATE_ACK are answered here; every message is
        then logged, and READ_INTENT and POPULATE_DIR handled, in arrival
        order by the connection's worker (conn_worker)."""
        mode = self.a.mode
        c.sock.settimeout(None)
        work = queue.SimpleQueue()
        threading.Thread(target=self.conn_worker, args=(c, work), daemon=True).start()
        while c.alive:
            try:
                rec = c.sock.recv(8192)
            except OSError:
                break
            if not rec:
                break
            t = round(time.time(), 3)
            try:
                m = P.parse(rec)
            except ValueError as e:
                work.put((t, dict(event="malformed_from_fsal", error=str(e), raw=rec.hex()),
                          None))
                c.send(P.error(3, "malformed"))
                break
            logged = dict({k: v for k, v in m.items() if k != "t"}, conn=c.peer)
            if mode == "hang-hello":
                work.put((t, logged, None))
                continue
            if m["t"] == P.HELLO:
                work.put((t, logged, None))
                note = self.on_hello(c, m)
                if note:
                    work.put((t, note, None))
                continue
            if mode == "hang":
                work.put((t, logged, None))
                continue
            if m["t"] == P.PING:
                c.send(P.ping(m["req_id"], m["mono_ns"], P.PONG))
            elif m["t"] == P.INVALIDATE_ACK:
                with c.ack_cv:
                    c.acks[m["req_id"]] = m["inval_seq"]
                    c.ack_cv.notify_all()
            elif m["t"] == P.OPEN_STATE:
                with c.ack_cv:
                    c.acks[m["req_id"]] = ("open", m["write_opens"])
                    c.ack_cv.notify_all()
            elif m["t"] in (P.READ_INTENT, P.POPULATE_DIR):
                work.put((t, logged, m))
                continue
            work.put((t, logged, None))
        c.alive = False
        work.put(None)

    def conn_worker(self, c, work):
        """Log each message of one connection, in order, and handle the
        requests that read the disk (READ_INTENT, POPULATE_DIR)."""
        while True:
            item = work.get()
            if item is None:
                self.log(event="closed", conn=c.peer)
                return
            t, logged, m = item
            self.log(_t=t, **logged)
            if m is None:
                continue
            try:
                if m["t"] == P.READ_INTENT:
                    self.on_read_intent(c, m)
                else:
                    self.populate(c, m)
            except Exception as e:  # noqa: BLE001
                self.log(event="handler_error", error=repr(e))

    def on_hello(self, c, m):
        """Answer HELLO (no disk I/O). Returns the event to log, if any."""
        mode = self.a.mode
        self.stats["hello"] += 1
        if mode == "bad-version":
            c.send(P.hello_ack(m["req_id"], major=1))
            return None
        feats = P.FEAT_NS_HINT | P.FEAT_WRITE_CLOSED | P.FEAT_OPEN_STATE
        if not self.a.no_park:
            feats |= P.FEAT_PARK
        if not self.a.no_populate:
            feats |= P.FEAT_POPULATE_DIR
        minor = 1
        if self.a.chunk_map:
            feats |= P.FEAT_CHUNK_MAP
            minor = 2
        if self.a.proto_minor is not None:
            minor = self.a.proto_minor
        minor = min(minor, m["minor"])
        c.features = feats & m["features"]
        if minor < 2:
            # v0.1: no chunk maps; POPULATE_DIR as A's minor 1
            c.features &= ~P.FEAT_CHUNK_MAP
        c.send(P.hello_ack(m["req_id"], minor=minor, features=feats,
                           instance=self.instance,
                           park_ms_max=self.a.park_ms_max,
                           answer_ttl_ms=self.a.answer_ttl_ms))
        c.ready = True
        c.send(P.msg(P.RESYNC_BEGIN, 0))
        with LOCK:
            active = [r for r in self.recalls.values() if not r["done"]]
        for r in active:
            c.send(P.recall_state(r["obj"], r["version"], r["id"], P.RS_RESTORING,
                                  r["answer"] or P.DEC_DELAY, valid_ms=5000))
        if self.a.resync_delay:
            def end_later(conn=c):
                time.sleep(self.a.resync_delay)
                conn.send(P.msg(P.RESYNC_END, 0))
                self.log(event="resync_end_sent")
            threading.Thread(target=end_later, daemon=True).start()
        else:
            c.send(P.msg(P.RESYNC_END, 0))
        if mode == "malformed":
            c.send(P.HDR.pack(P.READ_DECISION, 0, 999, 5) + b"\0" * 40)
        return dict(event="negotiated", fsal_minor=m["minor"], minor=minor,
                    features=c.features)

    def keepalive(self):
        """The daemon pings too, so an idle FSAL connection stays up."""
        while True:
            time.sleep(0.5)
            if self.a.mode in ("hang", "hang-hello") or not self.a.keepalive:
                continue
            for c in list(self.conns):
                if c.alive and c.ready:
                    c.send(P.ping(self.req_id(), time.monotonic_ns()))

    def control(self, path):
        if os.path.exists(path):
            os.unlink(path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        s.listen(4)
        while True:
            c, _ = s.accept()
            f = c.makefile("rw")
            for line in f:
                cmd = json.loads(line)
                if cmd["cmd"] == "stub":
                    out = self.stub(cmd["path"], int(cmd["resident"]))
                elif cmd["cmd"] == "push":
                    # n FETCHING pushes, every_ms apart, from a thread. Each
                    # says resident_bytes (default: the whole file) while
                    # the marker is left alone, so every push resumes every
                    # parked read of the file, and each resumed read
                    # re-checks the marker, is still not covered and parks
                    # again, as after a RECHECK. valid_ms (default 1) lets
                    # each re-park send its READ_INTENT.
                    path = cmd["path"]
                    mk = P.read_marker(path)
                    obj = self.obj_of(path, mk["file_id"])
                    rec = P.recall_state(obj, mk["version_id"], 0, P.RS_FETCHING, P.DEC_DELAY,
                                         resident=int(cmd.get("resident", mk["size"])),
                                         valid_ms=int(cmd.get("valid_ms", 1)))
                    n, every = int(cmd.get("n", 1)), int(cmd.get("every_ms", 20)) / 1000
                    # gap_ms: each push is followed by a second one gap_ms
                    # later (pairs, every_ms apart)
                    gap = int(cmd.get("gap_ms", 0)) / 1000

                    def pushes(rec=rec, n=n, every=every, gap=gap):
                        for _ in range(n):
                            self.broadcast(rec)
                            if gap:
                                time.sleep(gap)
                                self.broadcast(rec)
                            time.sleep(max(0.0, every - gap))
                    threading.Thread(target=pushes, daemon=True).start()
                    out = {"pushing": n}
                elif cmd["cmd"] in ("fetching", "done", "failed"):
                    # push RECALL_STATE for a file; "fetching" raises the
                    # fill fence (FETCHING + acked INVALIDATE CONTENT)
                    path = cmd["path"]
                    mk = P.read_marker(path)
                    obj = self.obj_of(path, mk["file_id"] if mk else 0)
                    ver = mk["version_id"] if mk else 0
                    if cmd["cmd"] == "fetching":
                        self.broadcast(P.recall_state(obj, ver, 99, P.RS_FETCHING, P.DEC_DELAY,
                                                      valid_ms=int(cmd.get("valid_ms", 2000))))
                        ok, n = self.inval_all(obj, 0x4)
                        out = {"acked": ok, "conns": n}
                    else:
                        st = P.RS_DONE if cmd["cmd"] == "done" else P.RS_FAILED
                        self.broadcast(P.recall_state(obj, ver, 99, st, P.DEC_EIO,
                                                      valid_ms=int(cmd.get("valid_ms", 1000))))
                        out = {"sent": True}
                elif cmd["cmd"] == "invalidate":
                    # an acked INVALIDATE for a file, optionally under a
                    # wrong fsid (one the FSAL does not know)
                    path = cmd["path"]
                    st = os.stat(path)
                    fsid = int(cmd.get("fsid", P.fsid_of(path)))
                    obj = P.obj_ref(fsid, st.st_ino, P.ino_gen(path), 0, 0)
                    t0 = time.time()
                    ok, n = self.inval_all(obj, int(cmd.get("what", 0x1)))
                    t1 = time.time()
                    out = {"acked": ok, "conns": n,
                           "ack_ms": round((t1 - t0) * 1000, 1),
                           "t_sent": t0, "t_ack": t1}
                elif cmd["cmd"] == "open_state":
                    # OPEN_STATE_QUERY for a file on every ready
                    # connection: the NFSv4 share states open for write
                    # (contract 5.5), one count per connection
                    path = cmd["path"]
                    st = os.stat(path)
                    obj = P.obj_ref(P.fsid_of(path), st.st_ino, P.ino_gen(path), 0, 0)
                    counts = []
                    for fc in list(self.conns):
                        if not (fc.alive and fc.ready):
                            continue
                        rid = self.req_id()
                        fc.send(P.open_state_query(rid, obj))
                        with fc.ack_cv:
                            deadline = time.time() + 1.0
                            while rid not in fc.acks and time.time() < deadline and fc.alive:
                                fc.ack_cv.wait(max(0.0, deadline - time.time()))
                            a = fc.acks.pop(rid, None)
                        counts.append(a[1] if isinstance(a, tuple) else None)
                    out = {"counts": counts, "write_opens": sum(x or 0 for x in counts),
                           "answered": all(x is not None for x in counts)}
                elif cmd["cmd"] == "flood":
                    # n RECALL_STATE pushes for objects that do not exist,
                    # to fill the FSAL's pending table
                    n, vms = int(cmd["n"]), int(cmd.get("valid_ms", 600000))
                    t0 = time.time()
                    for fc in list(self.conns):
                        if not (fc.alive and fc.ready):
                            continue
                        for i in range(n):
                            fc.send(P.recall_state(P.obj_ref(0xF100D, i + 1, 0, 0, i + 1), 1,
                                                  i + 1, P.RS_QUEUED, P.DEC_DELAY,
                                                  valid_ms=vms))
                    out = {"sent": n, "s": round(time.time() - t0, 1)}
                elif cmd["cmd"] == "stats":
                    out = dict(self.stats, conns_alive=sum(1 for x in self.conns if x.alive))
                else:
                    out = {"error": "unknown"}
                self.log(event="ctl", cmd=cmd, out=out)
                f.write(json.dumps(out) + "\n")
                f.flush()
            c.close()

    def run(self):
        path = self.a.socket
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        s.bind(path)
        os.chmod(path, 0o600)
        s.listen(16)
        if self.a.ctl:
            threading.Thread(target=self.control, args=(self.a.ctl,), daemon=True).start()
        threading.Thread(target=self.keepalive, daemon=True).start()
        for _ in range(self.a.fetchers):
            threading.Thread(target=self.chunk_worker, daemon=True).start()
        self.log(event="listening", socket=path, mode=self.a.mode, pid=os.getpid())
        while True:
            cs, _ = s.accept()
            pid, uid, gid = struct.unpack("3i", cs.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            exe = ""
            try:
                exe = os.readlink("/proc/%d/exe" % pid)
            except OSError:
                pass
            peer = "pid=%d uid=%d exe=%s" % (pid, uid, os.path.basename(exe))
            if uid != 0 or not exe.endswith("ganesha.nfsd"):
                self.log(event="peer_refused", peer=peer)
                cs.send(P.error(4, "peer not allowed"))
                cs.close()
                continue
            c = Conn(cs, peer)
            self.conns.append(c)
            self.stats["conns"] += 1
            # serve first: the handshake must not wait for a log write
            threading.Thread(target=self.serve, args=(c,), daemon=True).start()
            self.log(event="accepted", peer=peer)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="/run/omnifile/fsal.sock")
    ap.add_argument("--ctl", default="/run/omnifile/fake-ctl.sock")
    ap.add_argument("--root", required=True, help="exported tree, to find files by inode")
    ap.add_argument("--backend", required=True, help="backend objects, named by file_id")
    ap.add_argument("--log")
    ap.add_argument("--mode", default="normal")
    ap.add_argument("--recall-delay", type=float, default=3.0)
    ap.add_argument("--fetch-delay", type=float, default=1.0)
    ap.add_argument("--no-fill", action="store_true")
    ap.add_argument("--no-park", action="store_true")
    ap.add_argument("--no-populate", action="store_true",
                    help="do not offer POPULATE_DIR in HELLO_ACK")
    ap.add_argument("--populate-delay", type=float, default=0.0)
    ap.add_argument("--resync-delay", type=float, default=0.0,
                    help="send RESYNC_END this many seconds after HELLO_ACK")
    ap.add_argument("--fill-hold", type=float, default=0.0,
                    help="after raising the fill fence, wait this long before writing")
    ap.add_argument("--keepalive", action="store_true", help="ping the FSAL every 0.5 s")
    ap.add_argument("--log-stall", type=float, default=0.0,
                    help="each received message's log write takes this many more seconds")
    ap.add_argument("--ttl-ms", type=int, default=0)
    ap.add_argument("--answer-ttl-ms", type=int, default=1000)
    ap.add_argument("--park-ms-max", type=int, default=10000)
    ap.add_argument("--chunk-map", action="store_true", help="offer CHUNK_MAP (v0.2)")
    ap.add_argument("--proto-minor", type=int, help="answer HELLO with this minor")
    ap.add_argument("--chunk-shift", type=int, default=22)
    ap.add_argument("--fetchers", type=int, default=8)
    ap.add_argument("--chunk-delay", type=float, default=0.0,
                    help="seconds per chunk fetch (a remote object's latency)")
    a = ap.parse_args()
    Daemon(a).run()


if __name__ == "__main__":
    main()
