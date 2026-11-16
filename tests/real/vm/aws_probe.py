#!/usr/bin/env python3
"""The real-AWS checks that must run behind the interface endpoints (on the EC2 probe).

The harness sends this file and a JSON configuration through SSM (the probe has no inbound
access and no internet) and reads one JSON line back:
    {"ok": true, "evidence": {...}}  |  {"ok": true, "status": "partial", "left": "..."}
    |  {"ok": false, "error": "..."}

On the probe it uses the AWS CLI that Amazon Linux 2023 ships, against each interface
endpoint's own DNS name, and the Python standard library for TLS. In a dry run the harness
runs it on the workstation against moto (REAL_PROBE_ENDPOINT, REAL_PROBE_DRY_RUN=1); what
needs the real network (TLS, the signing helper) then answers partial.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time

DRY = os.environ.get("REAL_PROBE_DRY_RUN") == "1"
MOTO = os.environ.get("REAL_PROBE_ENDPOINT")
HELPER = "/usr/local/bin/aws_signing_helper"
#: What the instance's user data wrote while fetching it (tests/real/harness/aws.py).
HELPER_LOG = "/var/log/ofreal-helper.log"


def say(obj: dict) -> None:
    print(json.dumps(obj, default=str))


def partial(left: str, **ev) -> None:
    say({"ok": True, "status": "partial", "left": left, "evidence": ev})


def endpoint(cfg: dict, svc: str) -> str | None:
    if DRY:
        return MOTO
    dns = cfg["made"].get("endpoint_dns", {}).get(svc)
    if not dns:
        return None
    # S3's interface endpoint answers under bucket.<its DNS name> (its certificate names
    # bucket.vpce-… and *.bucket.vpce-…, never the bare name: the fourth real run).
    return f"https://{'bucket.' if svc == 's3' else ''}{dns.lstrip('*.')}"


def aws(cfg: dict, svc: str, *args: str, env: dict | None = None) -> dict:
    """The AWS CLI on the probe; boto3 against moto in a dry run."""
    if DRY:
        import boto3
        from botocore import xform_name

        c = boto3.client(
            {"s3api": "s3"}.get(svc, svc),
            endpoint_url=MOTO,
            region_name=cfg["region"],
            aws_access_key_id="fake",
            aws_secret_access_key="fake",
        )
        op = xform_name(args[0].replace("-", "_"))
        params = json.loads(args[1]) if len(args) > 1 else {}
        r = getattr(c, op)(**params)
        r.pop("ResponseMetadata", None)
        return r
    cmd = ["aws", svc, args[0], "--region", cfg["region"], "--output", "json"]
    ep = endpoint(cfg, "s3" if svc == "s3api" else svc)
    if ep:
        cmd += ["--endpoint-url", ep]
    if len(args) > 1:
        cmd += ["--cli-input-json", args[1]]
    p = subprocess.run(cmd, capture_output=True, text=True, env=env or os.environ)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[-400:])
    return json.loads(p.stdout or "{}")


# ------------------------------------------------------------------- checks


def list_restore_status(cfg: dict) -> None:
    b = cfg["names"]["source_bucket"]
    r = aws(
        cfg,
        "s3api",
        "list-objects-v2",
        json.dumps(
            {"Bucket": b, "Prefix": "seed/glacier/", "OptionalObjectAttributes": ["RestoreStatus"]}
        ),
    )
    items = r.get("Contents", [])
    key = cfg["args"]["key"]
    mine = [o for o in items if o["Key"] == key]
    if not mine:
        say({"ok": False, "error": f"{key} not listed"})
        return
    if "RestoreStatus" not in mine[0]:
        if DRY:
            partial("moto doesn't report RestoreStatus; on the probe through the real endpoint")
            return
        say({"ok": False, "error": "no RestoreStatus on an object with a restore"})
        return
    ev = {
        "restore_status": mine[0]["RestoreStatus"],
        "listed": len(items),
        "endpoint": endpoint(cfg, "s3"),
    }
    if DRY:
        partial("through the real S3 interface endpoint (dry run: moto)", **ev)
        return
    say({"ok": True, "evidence": ev})


def queue_lag(cfg: dict) -> None:
    want, t0 = set(cfg["args"]["keys"]), cfg["args"]["sent_at"]
    q = cfg["made"]["queue_url"]
    seen, order, lags = set(), [], []
    deadline = time.time() + (5 if DRY else 300)
    while want - seen and time.time() < deadline:
        r = aws(
            cfg,
            "sqs",
            "receive-message",
            json.dumps(
                {"QueueUrl": q, "MaxNumberOfMessages": 10, "WaitTimeSeconds": 0 if DRY else 10}
            ),
        )
        for m in r.get("Messages", []):
            e = json.loads(m["Body"])
            k = e.get("detail", {}).get("object", {}).get("key")
            if k in want and k not in seen:
                seen.add(k)
                order.append(k)
                lags.append(time.time() - t0)
            aws(
                cfg,
                "sqs",
                "delete-message",
                json.dumps({"QueueUrl": q, "ReceiptHandle": m["ReceiptHandle"]}),
            )
    if want - seen:
        say({"ok": False, "error": f"{len(want - seen)} events never arrived"})
        return
    ev = {
        "events": len(seen),
        "max_lag_s": round(max(lags), 1),
        "in_order": order == sorted(order),
        "endpoint": endpoint(cfg, "sqs"),
    }
    if DRY:
        partial("through the real SQS interface endpoint (dry run: moto)", **ev)
        return
    say({"ok": True, "evidence": ev})


def _helper_env(cfg: dict, cert: dict, d: str) -> list[str]:
    cf, kf = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    open(cf, "w").write(cert["cert"])
    open(kf, "w").write(cert["key"])
    os.chmod(kf, 0o600)
    m = cfg["made"]
    args = [
        HELPER,
        "credential-process",
        "--certificate",
        cf,
        "--private-key",
        kf,
        "--trust-anchor-arn",
        m["trust_anchor_arn"],
        "--profile-arn",
        m["profile_arn"],
        "--role-arn",
        m["roles"]["edge"],
    ]
    ep = endpoint(cfg, "rolesanywhere")
    if ep:
        args += ["--endpoint", ep]
    return args


def _session(cfg: dict, cert: dict) -> dict | None:
    with tempfile.TemporaryDirectory() as d:
        p = subprocess.run(_helper_env(cfg, cert, d), capture_output=True, text=True)
        if p.returncode != 0:
            return {"refused": p.stderr.strip()[-300:]}
        return json.loads(p.stdout)


def _needs_helper() -> bool:
    if DRY or not os.path.exists(HELPER):
        why = ""
        if not DRY and os.path.exists(HELPER_LOG):
            with open(HELPER_LOG) as f:
                why = ": " + f.read()[-300:].strip()
        partial(
            "dry run, or the signing helper is not on the probe: run on the probe"
            if DRY
            else f"{HELPER} missing{why}"
        )
        return True
    return False


def roles_anywhere_read(cfg: dict) -> None:
    if _needs_helper():
        return
    s = _session(cfg, cfg["args"]["cert"])
    if "refused" in s:
        say({"ok": False, "error": f"no session: {s['refused']}"})
        return
    env = dict(
        os.environ,
        AWS_ACCESS_KEY_ID=s["AccessKeyId"],
        AWS_SECRET_ACCESS_KEY=s["SecretAccessKey"],
        AWS_SESSION_TOKEN=s["SessionToken"],
    )
    b = cfg["names"]["source_bucket"]
    k = next(o for o in [{"Key": "seed/standard/plain-00.segy"}])["Key"]
    got = aws(cfg, "s3api", "head-object", json.dumps({"Bucket": b, "Key": k}), env=env)
    try:
        aws(cfg, "s3api", "put-object", json.dumps({"Bucket": b, "Key": "seed/forbidden"}), env=env)
        say({"ok": False, "error": "the edge role could write to the source"})
        return
    except RuntimeError as e:
        denied = "AccessDenied" in str(e)
    exp = s.get("Expiration")
    say(
        {
            "ok": denied,
            "evidence": {
                "read": got.get("ContentLength"),
                "write_denied": denied,
                "expiration": exp,
            },
            "error": "write not denied",
        }
    )


def roles_anywhere_session(cfg: dict) -> None:
    if _needs_helper():
        return
    partial(
        "an enrol-issued certificate reaches the probe only from an enrolled edge; "
        "record from the appliance"
    )


def trust_conditions(cfg: dict) -> None:
    if _needs_helper():
        return
    got = {}
    for name, cert in cfg["args"]["certs"].items():
        s = _session(cfg, cert)
        got[name] = "refused" not in s
    want = cfg["args"]["expect"]
    say(
        {
            "ok": got == want,
            "evidence": {"got": got, "want": want},
            "error": f"sessions {got}, want {want}",
        }
    )


def revoked_refused(cfg: dict) -> None:
    if _needs_helper():
        return
    deadline = time.time() + cfg["args"]["within_s"] + 120
    t0 = time.time()
    while time.time() < deadline:
        s = _session(cfg, cfg["args"]["cert"])
        if "refused" in s:
            say(
                {
                    "ok": True,
                    "evidence": {
                        "refused_after_s": round(time.time() - t0, 1),
                        "answer": s["refused"],
                    },
                }
            )
            return
        time.sleep(10)
    say({"ok": False, "error": "the revoked certificate still gets sessions"})


def credential_process(cfg: dict) -> None:
    if _needs_helper():
        return
    s = _session(cfg, cfg["args"]["cert"])
    ok = "AccessKeyId" in s
    say(
        {
            "ok": ok,
            "evidence": {
                "version": s.get("Version"),
                "expiration": s.get("Expiration"),
                "region_from_arn": cfg["made"]["trust_anchor_arn"].split(":")[3],
            },
            "error": str(s),
        }
    )


def tls_endpoints(cfg: dict) -> None:
    if DRY:
        partial("TLS to the real interface endpoints (dry run: moto speaks plain HTTP)")
        return
    out = {}
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    for svc in ("s3", "sqs"):
        host = endpoint(cfg, svc)[len("https://") :]
        if svc == "s3":
            host = f"{cfg['names']['source_bucket']}.{host}"
        with socket.create_connection((host, 443), timeout=10) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as t:
                out[svc] = {"host": host, "version": t.version()}
    ok = all(v["version"] in ("TLSv1.2", "TLSv1.3") for v in out.values())
    say({"ok": ok, "evidence": out, "error": f"TLS versions {out}"})


CHECKS = {
    "list-restore-status": list_restore_status,
    "queue-lag": queue_lag,
    "roles-anywhere-read": roles_anywhere_read,
    "roles-anywhere-session": roles_anywhere_session,
    "trust-conditions": trust_conditions,
    "revoked-refused": revoked_refused,
    "credential-process": credential_process,
    "tls-endpoints": tls_endpoints,
}


def main() -> int:
    cfg = json.loads(sys.stdin.read())
    try:
        CHECKS[cfg["check"]](cfg)
    except Exception as e:  # one line back, always
        say({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
