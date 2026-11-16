#!/usr/bin/env python3
"""Summarise fake-daemon log lines after a given time: dstats.py <since> [log]"""
import json, statistics, sys
since = float(sys.argv[1]); log = sys.argv[2] if len(sys.argv) > 2 else "/srv/omni-b/daemon.log"
ev = [json.loads(l) for l in open(log) if l.strip()]
ev = [e for e in ev if e["t"] >= since]
req = [e for e in ev if e["event"] == "request"]
ts = [e["t"] for e in req]
gaps = [b - a for a, b in zip(ts, ts[1:])]
out = {"requests": len(req),
       "decisions": {d: sum(1 for e in req if e["decision"] == d) for d in {e["decision"] for e in req}},
       "offsets": sorted({e["req"].get("offset") for e in req})[:8],
       "nfs": sorted({e["req"].get("nfs") for e in req}),
       "uids": sorted({e["req"].get("uid") for e in req}),
       "clients": sorted({e["req"].get("client") for e in req}),
       "paths": sorted({str(e["req"].get("path")) for e in req}),
       "recalls": [(e["event"], round(e["t"] - since, 2)) for e in ev if e["event"].startswith("recall")]}
if gaps:
    out["retry_gap_s"] = {"min": round(min(gaps), 3), "median": round(statistics.median(gaps), 3),
                          "max": round(max(gaps), 3)}
if ts:
    out["first_req_s"] = round(ts[0] - since, 2); out["last_req_s"] = round(ts[-1] - since, 2)
print(json.dumps(out))
