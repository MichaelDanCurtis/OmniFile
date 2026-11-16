#!/usr/bin/env python3
"""Fake OmniFile daemon for the FSAL_OMNI spike (part B). Throwaway.

Listens on a Unix socket. Each connection carries one JSON line from
FSAL_OMNI and gets one JSON line back: {"decision": "allow"|"delay"|"eio"}.

Policies (sent by the FSAL, per share):
  wait            start a recall if none is running; answer "delay" until the
                  file is resident, then "allow".
  failfast        queue a recall; answer "eio" now.
  waitquick:<ms>  if the estimated recall time <= ms, behave as wait,
                  otherwise as failfast.

A "recall" copies /srv/omni-b/backend/<key> into the stub after
--recall-delay seconds, restores atime/mtime, then sets
user.omnifile.state=resident and resident_bytes=size.

Every request is logged as one JSON line to --log.
"""
import argparse
import json
import os
import socketserver
import threading
import time

XA_STATE = "user.omnifile.state"
XA_RES = "user.omnifile.resident_bytes"
XA_KEY = "user.omnifile.key"

ARGS = None
LOCK = threading.Lock()
RECALLS = {}  # key -> {"started": t, "done": bool, "path": str}
LOGF = None


def log(rec):
    rec["t"] = round(time.time(), 3)
    with LOCK:
        LOGF.write(json.dumps(rec) + "\n")
        LOGF.flush()


def find_by_key(key):
    for root in ARGS.roots:
        for d, _, files in os.walk(root):
            for f in files:
                p = os.path.join(d, f)
                try:
                    if os.getxattr(p, XA_KEY).decode() == key:
                        return p
                except OSError:
                    pass
    return None


def resolve(req):
    p = req.get("path")
    key = req.get("key") or ""
    if p:
        try:
            st = os.stat(p)
            if req.get("ino") in (None, st.st_ino):
                return p
        except OSError:
            pass
    return find_by_key(key) if key else None


def state_of(path):
    try:
        return os.getxattr(path, XA_STATE).decode()
    except OSError:
        return None


def do_recall(key, path):
    time.sleep(ARGS.recall_delay)
    src = os.path.join(ARGS.backend, key)
    st = os.stat(path)
    res = int(os.getxattr(path, XA_RES).decode() or 0)
    t0 = time.time()
    with open(src, "rb") as fi, open(path, "r+b") as fo:
        fi.seek(res)
        fo.seek(res)
        while True:
            b = fi.read(8 << 20)
            if not b:
                break
            fo.write(b)
        fo.flush()
        os.fsync(fo.fileno())
    # A recall is not a user modification: keep atime/mtime.
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.setxattr(path, XA_RES, str(st.st_size).encode())
    os.setxattr(path, XA_STATE, b"resident")
    with LOCK:
        RECALLS[key]["done"] = True
    log({"event": "recall_done", "key": key, "path": path,
         "bytes": st.st_size - res, "copy_s": round(time.time() - t0, 3)})


def start_recall(key, path):
    with LOCK:
        r = RECALLS.get(key)
        if r and not r["done"]:
            return False
        RECALLS[key] = {"started": time.time(), "done": False, "path": path}
    log({"event": "recall_start", "key": key, "path": path,
         "eta_s": ARGS.recall_delay})
    threading.Thread(target=do_recall, args=(key, path), daemon=True).start()
    return True


def decide(req):
    path = resolve(req)
    if path is None:
        return "eio", "unknown file"
    key = req.get("key") or os.getxattr(path, XA_KEY).decode()
    if state_of(path) == "resident":
        return "allow", "resident"
    policy = req.get("policy", "wait")
    if policy.startswith("waitquick:"):
        quick_ms = int(policy.split(":", 1)[1])
        policy = "wait" if ARGS.recall_delay * 1000 <= quick_ms else "failfast"
    started = start_recall(key, path)
    note = "recall started" if started else "recall in progress"
    if policy == "wait":
        return "delay", note
    return "eio", note


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(65536)
        try:
            req = json.loads(line)
            if ARGS.hang:
                time.sleep(3600)
            decision, why = decide(req)
        except Exception as e:  # noqa: BLE001 - spike
            req, decision, why = {"raw": line.decode(errors="replace")}, "eio", repr(e)
        log({"event": "request", "req": req, "decision": decision, "why": why})
        self.wfile.write((json.dumps({"decision": decision}) + "\n").encode())


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    global ARGS, LOGF
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="/run/omnifile/fsal.sock")
    ap.add_argument("--backend", default="/srv/omni-b/backend")
    ap.add_argument("--roots", nargs="+", default=["/srv/omni-b/data"])
    ap.add_argument("--recall-delay", type=float, default=20.0)
    ap.add_argument("--log", default="/srv/omni-b/daemon.log")
    ap.add_argument("--hang", action="store_true",
                    help="accept requests but never answer (timeout test)")
    ARGS = ap.parse_args()
    LOGF = open(ARGS.log, "a")
    os.makedirs(os.path.dirname(ARGS.socket), exist_ok=True)
    try:
        os.unlink(ARGS.socket)
    except FileNotFoundError:
        pass
    srv = Server(ARGS.socket, Handler)
    os.chmod(ARGS.socket, 0o600)
    log({"event": "start", "recall_delay": ARGS.recall_delay, "hang": ARGS.hang})
    srv.serve_forever()


if __name__ == "__main__":
    main()
