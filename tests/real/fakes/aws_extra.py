"""What the dry run's moto server lacks, answered at botocore's before-call, plus the
transcript of every AWS call the harness makes from Python.

- IAM Roles Anywhere: trust anchors, profiles, CRLs and their tags (moto has none).
- S3 Control Batch Operations jobs: CreateJob, DescribeJob, ListJobs, job tags, status
  updates. A job completes at once; a Compute checksum job writes its completion report
  where the request asks, in the layout the service reads (migrate/checksums.py).
- RestoreObject on an object whose restore is still running answers 409
  RestoreAlreadyInProgress, as S3 documents (moto accepts it); a dry run's restores never
  finish, so the "already restored" half of RW-06 stays unproven, as it should.
- SSM command output: GetCommandInvocation runs tests/real/vm/aws_probe.py here, against
  the moto server, with the configuration the harness sent to the probe.

State is a JSON file in the run's output (REAL_FAKE_STATE with an -aws suffix), shared with
the aws CLI shim. Nothing here reaches AWS: every client of the session has the moto
server as its endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from botocore.awsrequest import AWSResponse

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def _norm(text: str, ctx) -> str:
    text = text.replace(str(REPO), "$REPO").replace(str(ctx.out), "$OUT")
    moto = ctx.env.get("REAL_FAKE_MOTO")
    return text.replace(moto, "$MOTO") if moto else text


# Values a run makes at random (moto's ids, receipts, version ids, test keys) or that carry
# them (policies naming endpoint ids): the transcript keeps the key, not the value.
VOLATILE_KEYS = {
    "UploadId",
    "ReceiptHandle",
    "ETag",
    "VersionId",
    "x509CertificateData",
    "crlData",
}
#: Policies are kept whole, as parsed JSON (keys sorted), so a golden transcript shows every
#: action, resource and condition: a `*` that creeps in fails the comparison. The ids moto
#: makes inside them are normalised by the tests, not hidden here.
POLICY_KEYS = {"PolicyDocument", "AssumeRolePolicyDocument"}


def _shape(v: Any, key: str = "") -> Any:
    if key in VOLATILE_KEYS:
        return "<volatile>"
    if key in POLICY_KEYS and isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    if key == "UserData" and isinstance(v, str):
        import base64

        try:  # boto3 has already base64'd it by now: record the script itself
            return base64.b64decode(v, validate=True).decode()
        except ValueError:
            return v
    if isinstance(v, bytes | bytearray):
        return f"<{len(v)} bytes>"
    if hasattr(v, "read"):
        return "<stream>"
    if isinstance(v, dict):
        return {k: _shape(x, k) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_shape(x, key) for x in v]
    if isinstance(v, str) and len(v) > 200:
        return f"<{len(v)} chars sha256:{hashlib.sha256(v.encode()).hexdigest()[:12]}>"
    return v


class Extra:
    def __init__(self, ctx, moto_url: str | None) -> None:
        self.ctx, self.moto_url = ctx, moto_url
        self.file = Path(ctx.env["REAL_FAKE_STATE"] + "-aws.json")
        self.pending: dict[int, dict[str, Any]] = {}

    # ---------------------------------------------------------------- state

    def load(self) -> dict[str, Any]:
        if self.file.exists():
            return json.loads(self.file.read_text())
        return {
            "anchors": {},
            "profiles": {},
            "crls": {},
            "tags": {},
            "jobs": {},
            "restoring": [],
            "commands": {},
            "seq": 0,
        }

    def save(self, s: dict[str, Any]) -> None:
        self.file.write_text(json.dumps(s, indent=2, sort_keys=True))

    def _id(self, s: dict[str, Any]) -> str:
        s["seq"] += 1
        return str(uuid.UUID(int=s["seq"]))

    # -------------------------------------------------------------- hooks

    def record(self, params, model, **kw) -> None:
        svc = model.service_model.service_name
        line = f"boto3 {svc}.{model.name} {json.dumps(_shape(params), sort_keys=True, default=str)}"
        with open(self.ctx.env["REAL_FAKE_TRANSCRIPT"], "a") as f:
            f.write(_norm(line, self.ctx) + "\n")
        self.pending[id(model)] = params

    def answer(self, model, params, context, **kw):
        svc, op = model.service_model.service_name, model.name
        api = context.get("ofreal_params") or {}
        pat = self.ctx.env.get("REAL_FAKE_FAIL_BOTO")
        if pat:
            import re

            line = f"{svc}.{op} {json.dumps(_shape(api), sort_keys=True, default=str)}"
            if re.search(pat, line):
                parsed = {
                    "Error": {"Code": "InjectedFailure", "Message": f"injected ({pat})"},
                    "ResponseMetadata": {"HTTPStatusCode": 500, "HTTPHeaders": {}},
                }
                return AWSResponse("https://fake.invalid/", 500, {}, None), parsed
        fn = getattr(self, f"_{svc.replace('-', '_')}_{op}", None)
        if fn is None:
            return None
        got = fn(api)
        if got is None:
            return None
        status, parsed = got
        parsed.setdefault("ResponseMetadata", {"HTTPStatusCode": status, "HTTPHeaders": {}})
        return AWSResponse("https://fake.invalid/", status, {}, None), parsed

    def after(self, http_response, parsed, model, context, **kw) -> None:
        """S3 behaviour moto doesn't model, on moto's own answers: under SSE-KMS a
        single-part ETag is not the object's MD5 (AWS documents it)."""
        svc, op = model.service_model.service_name, model.name
        api = context.get("ofreal_params") or {}
        if svc != "s3":
            return
        if op == "PutBucketEncryption":
            rules = api["ServerSideEncryptionConfiguration"]["Rules"]
            if any(
                r["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms" for r in rules
            ):
                s = self.load()
                s.setdefault("kms_buckets", []).append(api["Bucket"])
                self.save(s)
        if (
            op == "HeadObject"
            and f"{api.get('Bucket')}/{api.get('Key')}@{api.get('VersionId', '')}"
            in self.load()["restoring"]
        ):
            # A dry run's restores never finish (moto finishes them at once).
            parsed["Restore"] = 'ongoing-request="true"'
        if (
            op in ("PutObject", "HeadObject", "GetObject")
            and api.get("Bucket") in self.load().get("kms_buckets", [])
            and "ETag" in parsed
        ):
            parsed["ETag"] = '"' + hashlib.sha256(parsed["ETag"].encode()).hexdigest()[:32] + '"'
            parsed["ServerSideEncryption"] = "aws:kms"

    def stash(self, params, context, **kw) -> None:
        context["ofreal_params"] = dict(params)

    # ------------------------------------------------------- Roles Anywhere

    def _arn(self, kind: str, rid: str) -> str:
        return f"arn:aws:rolesanywhere:us-west-2:123456789012:{kind}/{rid}"

    def _rolesanywhere_CreateTrustAnchor(self, p):
        s = self.load()
        rid = self._id(s)
        ta = {
            "trustAnchorId": rid,
            "trustAnchorArn": self._arn("trust-anchor", rid),
            "name": p["name"],
            "enabled": p.get("enabled", False),
            "source": p["source"],
        }
        s["anchors"][rid] = ta
        s["tags"][ta["trustAnchorArn"]] = p.get("tags", [])
        self.save(s)
        return 201, {"trustAnchor": ta}

    def _rolesanywhere_CreateProfile(self, p):
        s = self.load()
        rid = self._id(s)
        pr = {
            "profileId": rid,
            "profileArn": self._arn("profile", rid),
            "name": p["name"],
            "roleArns": p["roleArns"],
            "durationSeconds": p.get("durationSeconds", 3600),
            "enabled": p.get("enabled", False),
        }
        s["profiles"][rid] = pr
        s["tags"][pr["profileArn"]] = p.get("tags", [])
        self.save(s)
        return 201, {"profile": pr}

    def _rolesanywhere_ImportCrl(self, p):
        # Real Roles Anywhere refuses a CRL that revokes nothing (the second real run).
        from cryptography import x509

        data = p["crlData"] if isinstance(p["crlData"], bytes) else p["crlData"].encode()
        if not list(x509.load_pem_x509_crl(data)):
            return 400, {
                "Error": {
                    "Code": "ValidationException",
                    "Message": "At least one of the included CRLs doesn't include a revoked "
                    "certificate.",
                }
            }
        s = self.load()
        rid = self._id(s)
        crl = {
            "crlId": rid,
            "crlArn": self._arn("crl", rid),
            "name": p["name"],
            "enabled": p.get("enabled", False),
            "trustAnchorArn": p["trustAnchorArn"],
            "crlData": base64.b64encode(p["crlData"]).decode()
            if isinstance(p["crlData"], bytes)
            else p["crlData"],
        }
        s["crls"][rid] = crl
        s["tags"][crl["crlArn"]] = p.get("tags", [])
        self.save(s)
        return 201, {"crl": {k: v for k, v in crl.items() if k != "crlData"}}

    def _rolesanywhere_UpdateCrl(self, p):
        s = self.load()
        crl = s["crls"].get(p["crlId"])
        if not crl:
            return 404, {"Error": {"Code": "ResourceNotFoundException", "Message": "no crl"}}
        self.save(s)
        return 200, {"crl": {k: v for k, v in crl.items() if k != "crlData"}}

    def _rolesanywhere_ListTrustAnchors(self, p):
        return 200, {"trustAnchors": list(self.load()["anchors"].values())}

    def _rolesanywhere_ListProfiles(self, p):
        return 200, {"profiles": list(self.load()["profiles"].values())}

    def _rolesanywhere_ListCrls(self, p):
        return 200, {
            "crls": [
                {k: v for k, v in c.items() if k != "crlData"} for c in self.load()["crls"].values()
            ]
        }

    def _rolesanywhere_TagResource(self, p):
        s = self.load()
        have = s["tags"].setdefault(p["resourceArn"], [])
        keys = {t["key"] for t in p["tags"]}
        s["tags"][p["resourceArn"]] = [t for t in have if t["key"] not in keys] + p["tags"]
        self.save(s)
        return 200, {}

    def _rolesanywhere_ListTagsForResource(self, p):
        return 200, {"tags": self.load()["tags"].get(p["resourceArn"], [])}

    def _delete(self, kind: str, key: str, rid: str, out: str):
        s = self.load()
        obj = s[kind].pop(rid, None)
        if obj is None:
            return 404, {"Error": {"Code": "ResourceNotFoundException", "Message": rid}}
        s["tags"].pop(obj[key], None)
        self.save(s)
        return 200, {out: {k: v for k, v in obj.items() if k != "crlData"}}

    def _rolesanywhere_DeleteTrustAnchor(self, p):
        return self._delete("anchors", "trustAnchorArn", p["trustAnchorId"], "trustAnchor")

    def _rolesanywhere_DeleteProfile(self, p):
        return self._delete("profiles", "profileArn", p["profileId"], "profile")

    def _rolesanywhere_DeleteCrl(self, p):
        return self._delete("crls", "crlArn", p["crlId"], "crl")

    # ------------------------------------------------------ S3 Control jobs

    def _s3control_CreateJob(self, p):
        s = self.load()
        jid = self._id(s)
        op = next(iter(p["Operation"]))
        objects = 0
        if "Spec" in p.get("Manifest", {}):
            objects = int(p.get("ofreal_count", 0))
        job = {
            "JobId": jid,
            "Status": "Complete",
            "Operation": op,
            "Priority": p.get("Priority", 10),
            "ProgressSummary": {
                "TotalNumberOfTasks": objects,
                "NumberOfTasksSucceeded": objects,
                "NumberOfTasksFailed": 0,
            },
            "Report": p.get("Report", {}),
            "Manifest": p.get("Manifest", {}),
            "OperationSpec": p["Operation"][op],
        }
        s["jobs"][jid] = job
        s["tags"][f"job/{jid}"] = p.get("Tags", [])
        self.save(s)
        if op == "S3ComputeObjectChecksum":
            self._checksum_report(job)
        return 200, {"JobId": jid}

    def _checksum_report(self, job: dict[str, Any]) -> None:
        """The completion report the real job writes: <prefix>/job-<id>/manifest.json naming
        results CSVs, one row per object (bucket, key, version, status, code, message JSON)."""
        import boto3

        rep = job["Report"]
        bucket = rep["Bucket"].split(":::")[-1]
        prefix = rep.get("Prefix", "reports")
        s3 = boto3.client(
            "s3",
            endpoint_url=self.moto_url,
            region_name="us-west-2",
            aws_access_key_id="fake",
            aws_secret_access_key="fake",
        )
        loc = job["Manifest"].get("Location", {})
        src_bucket, src_key = loc.get("ObjectArn", ":::/").split(":::")[-1].split("/", 1)
        rows = []
        manifest = s3.get_object(Bucket=src_bucket, Key=src_key)["Body"].read().decode()
        from urllib.parse import quote_plus, unquote_plus

        for line in manifest.splitlines():
            b, k, *v = line.split(",")
            head = s3.head_object(Bucket=b, Key=unquote_plus(k), ChecksumMode="ENABLED")
            crc = head.get("ChecksumCRC64NVME", "")
            msg = json.dumps(
                {
                    "checksum_base64": crc,
                    "checksumAlgorithm": "CRC64NVME",
                    "checksumType": "FULL_OBJECT",
                }
            )
            # Seven columns, the sixth empty on success, and the key form-encoded whatever the
            # manifest's spelling: a space as '+', a plus as '%2B', a percent sign as '%25',
            # a comma as '%2C', non-ASCII as its UTF-8 bytes, slashes kept. Real S3 wrote each
            # of these (run rr-202611150718-62e9); it is quote_plus with slashes safe.
            rk = quote_plus(unquote_plus(k), safe="/")
            rows.append(
                f'{b},{rk},{v[0] if v else ""},succeeded,200,,"{msg.replace(chr(34), chr(34) * 2)}"'
            )
        rkey = f"{prefix}/job-{job['JobId']}/results/{job['JobId']}-1.csv"
        s3.put_object(Bucket=bucket, Key=rkey, Body="\n".join(rows).encode())
        s3.put_object(
            Bucket=bucket,
            Key=f"{prefix}/job-{job['JobId']}/manifest.json",
            Body=json.dumps(
                {
                    "Format": "Report_CSV_20180820",
                    "ReportCreationDate": "2026-11-03T00:00:00Z",
                    "Results": [
                        {"TaskExecutionStatus": "succeeded", "Bucket": bucket, "Key": rkey}
                    ],
                }
            ).encode(),
        )

    def _s3control_DescribeJob(self, p):
        job = self.load()["jobs"].get(p["JobId"])
        if not job:
            return 404, {"Error": {"Code": "NoSuchJob", "Message": p["JobId"]}}
        return 200, {"Job": {k: job[k] for k in ("JobId", "Status", "Priority", "ProgressSummary")}}

    def _s3control_ListJobs(self, p):
        want = set(p.get("JobStatuses", []))
        jobs = [
            {"JobId": j["JobId"], "Status": j["Status"], "Operation": j["Operation"]}
            for j in self.load()["jobs"].values()
            if not want or j["Status"] in want
        ]
        return 200, {"Jobs": jobs}

    def _s3control_GetJobTagging(self, p):
        return 200, {"Tags": self.load()["tags"].get(f"job/{p['JobId']}", [])}

    def _s3control_UpdateJobStatus(self, p):
        s = self.load()
        s["jobs"][p["JobId"]]["Status"] = p["RequestedJobStatus"]
        self.save(s)
        return 200, {"JobId": p["JobId"], "Status": p["RequestedJobStatus"]}

    # ------------------------------------------------------------ S3 restore

    def _s3_RestoreObject(self, p):
        s = self.load()
        key = f"{p['Bucket']}/{p['Key']}@{p.get('VersionId', '')}"
        if key in s["restoring"]:
            return 409, {
                "Error": {
                    "Code": "RestoreAlreadyInProgress",
                    "Message": "Object restore is already in progress",
                }
            }
        s["restoring"].append(key)
        self.save(s)
        return None  # moto answers the first

    # ------------------------------------------------------------ SSM output

    def _ssm_DescribeInstanceInformation(self, p):
        ids = [v for f in p.get("Filters", []) if f["Key"] == "InstanceIds" for v in f["Values"]]
        return 200, {
            "InstanceInformationList": [{"InstanceId": i, "PingStatus": "Online"} for i in ids]
        }

    def _ssm_SendCommand(self, p):
        s = self.load()
        cid = self._id(s)
        s["commands"][cid] = p["Parameters"]["commands"]
        self.save(s)
        return 200, {
            "Command": {"CommandId": cid, "Status": "Pending", "InstanceIds": p["InstanceIds"]}
        }

    def _ssm_GetCommandInvocation(self, p):
        cmds = self.load()["commands"].get(p["CommandId"])
        if cmds is None:
            return 400, {"Error": {"Code": "InvocationDoesNotExist", "Message": p["CommandId"]}}
        cfg = cmds[-1].split("<<'OFREAL_CFG'\n", 1)[1].rsplit("\nOFREAL_CFG", 1)[0]
        env = dict(self.ctx.env, REAL_PROBE_ENDPOINT=self.moto_url or "", REAL_PROBE_DRY_RUN="1")
        r = subprocess.run(
            [sys.executable, str(REPO / "tests/real/vm/aws_probe.py")],
            input=cfg,
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
        )
        return 200, {
            "CommandId": p["CommandId"],
            "InstanceId": p["InstanceId"],
            "Status": "Success" if r.returncode == 0 else "Failed",
            "StandardOutputContent": r.stdout,
            "StandardErrorContent": r.stderr,
        }


def install(session, ctx, moto_url: str | None) -> Extra:
    if not ctx.dry_run or "REAL_FAKE_STATE" not in ctx.env:
        raise RuntimeError("aws_extra is for dry runs only")
    x = Extra(ctx, moto_url)
    session.events.register_first("before-parameter-build", x.record)
    session.events.register_first("before-parameter-build", x.stash)
    session.events.register_first("before-call", x.answer)
    session.events.register("after-call", x.after)
    return x
