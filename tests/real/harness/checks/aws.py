"""docs/milestones/real-aws-checks.md, line by line (RW-nn).

S3 behaviour that doesn't depend on the network path is checked from the workstation with
boto3. What must go through the interface endpoints (TLS 1.2, virtual-hosted addressing,
Roles Anywhere sessions, SQS reads) runs on the EC2 probe (tests/real/vm/aws_probe.py,
through SSM). In a dry run both go to moto; what moto can't show comes back partial.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import time
from typing import Any
from urllib.parse import quote, unquote_plus

from awscrt import checksums

from . import CheckFailed, Partial, Pending, auto, manual


def data(name: str, n: int) -> bytes:
    """Test bytes, the same on every run (so a dry run's transcript is stable); nothing
    depends on them being secret or unique."""
    import random

    return random.Random(name).randbytes(n)


def crc64(data: bytes) -> str:
    return base64.b64encode(checksums.crc64nvme(data).to_bytes(8, "big")).decode()


#: What `--scope cheap` leaves out (the owner's cheap first pass: small objects only). Each
#: stays pending, with why, until a run with --scope full.
NOT_CHEAP = {
    "RW-02": "it builds a 5.2 GiB object (UploadPartCopy); run with --scope full",
}


class AwsEnv:
    def __init__(self, ctx, aws) -> None:
        self.ctx, self.aws = ctx, aws
        self._events: list[dict] | None = None

    @property
    def tier(self) -> str:
        """Restores at Standard, or Bulk in the cheap pass."""
        from ..aws import RESTORE_TIER

        return RESTORE_TIER[self.aws.scope]

    def not_in_scope(self) -> dict[str, str]:
        return NOT_CHEAP if self.aws.scope == "cheap" else {}

    @property
    def n(self) -> dict[str, Any]:
        return self.aws.names()

    @property
    def made(self) -> dict[str, Any]:
        return self.ctx.state.get("made", {})

    def c(self, name: str):
        return self.aws.client(name)

    def objects(self, cls: str) -> list[dict]:
        return [o for o in self.made.get("objects", []) if o["class"] == cls]

    @property
    def mib(self) -> int:
        """Part size: 100 MiB on a real account, 5 MiB (S3's minimum) in a dry run and in the
        cheap pass."""
        return 5 if self.ctx.dry_run or self.aws.scope == "cheap" else 100

    def probe(self, check: str, **args: Any) -> dict[str, Any]:
        if self.made.get("probe_stopped"):
            raise Pending("the cheap pass terminated the probe once its checks ran")
        if not self.made.get("probe"):
            raise Pending("the EC2 probe is not up")
        r = self.aws.probe(check, args)
        if r.get("status") == "partial":
            raise Partial(r.get("left", "see evidence"), r.get("evidence"))
        if not r.get("ok"):
            raise CheckFailed(r.get("error", f"probe {check} failed"))
        return r.get("evidence", {})

    def events(self) -> list[dict]:
        """Every EventBridge event on the run's queue so far: what is waiting is read (and
        deleted from the queue) and added to what earlier checks read."""
        if self._events is None:
            self._events = []
        if True:
            sqs, got = self.c("sqs"), self._events
            for _ in range(20):
                m = sqs.receive_message(
                    QueueUrl=self.made["queue_url"],
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=0 if self.ctx.dry_run else 5,
                )
                msgs = m.get("Messages", [])
                if not msgs:
                    break
                for x in msgs:
                    got.append(json.loads(x["Body"]))
                    sqs.delete_message(
                        QueueUrl=self.made["queue_url"], ReceiptHandle=x["ReceiptHandle"]
                    )
            self._events = got
            (self.ctx.evidence / "events.json").write_text(json.dumps(got, indent=2, default=str))
        return self._events


# ------------------------------------------------------------- storage behaviour


@auto("RW-01", "multipart with a full-object CRC64NVME: HEAD returns the CRC the catalog keeps")
def rw01(env: AwsEnv):
    s3, b = env.c("s3"), env.n["source_bucket"]
    parts = [data("rw01-1", env.mib << 20), data("rw01-2", 1024)]
    whole = b"".join(parts)
    key = "checks/rw01-multipart.bin"
    up = s3.create_multipart_upload(
        Bucket=b, Key=key, ChecksumAlgorithm="CRC64NVME", ChecksumType="FULL_OBJECT"
    )
    done = []
    for i, p in enumerate(parts, 1):
        r = s3.upload_part(
            Bucket=b,
            Key=key,
            UploadId=up["UploadId"],
            PartNumber=i,
            Body=p,
            ChecksumAlgorithm="CRC64NVME",
        )
        done.append(
            {"PartNumber": i, "ETag": r["ETag"], "ChecksumCRC64NVME": r["ChecksumCRC64NVME"]}
        )
    s3.complete_multipart_upload(
        Bucket=b,
        Key=key,
        UploadId=up["UploadId"],
        MultipartUpload={"Parts": done},
        ChecksumCRC64NVME=crc64(whole),
        ChecksumType="FULL_OBJECT",
    )
    h = s3.head_object(Bucket=b, Key=key, ChecksumMode="ENABLED")
    if h.get("ChecksumCRC64NVME") != crc64(whole):
        raise CheckFailed(f"HEAD CRC {h.get('ChecksumCRC64NVME')} != {crc64(whole)}")
    return {"crc64nvme": crc64(whole), "checksum_type": h.get("ChecksumType")}


@auto("RW-02", "UploadPartCopy above 5 GiB: does S3 compute the full-object CRC itself?")
def rw02(env: AwsEnv):
    """A source above 5 GiB is built server-side (UploadPartCopy of one uploaded part), so
    nothing large is uploaded; in a dry run, two small parts stand in."""
    s3, b = env.c("s3"), env.n["source_bucket"]
    part = data("rw02", env.mib << 20)
    s3.put_object(Bucket=b, Key="checks/rw02-part.bin", Body=part, ChecksumAlgorithm="CRC64NVME")
    count = 2 if env.ctx.dry_run else 53  # 53 x 100 MiB = 5.2 GiB
    big = "checks/rw02-big.bin"
    up = s3.create_multipart_upload(
        Bucket=b, Key=big, ChecksumAlgorithm="CRC64NVME", ChecksumType="FULL_OBJECT"
    )
    ps = []
    for i in range(1, count + 1):
        r = s3.upload_part_copy(
            Bucket=b,
            Key=big,
            UploadId=up["UploadId"],
            PartNumber=i,
            CopySource={"Bucket": b, "Key": "checks/rw02-part.bin"},
        )
        ps.append({"PartNumber": i, "ETag": r["CopyPartResult"]["ETag"]})
    s3.complete_multipart_upload(
        Bucket=b, Key=big, UploadId=up["UploadId"], MultipartUpload={"Parts": ps}
    )
    h = s3.head_object(Bucket=b, Key=big, ChecksumMode="ENABLED")
    want = crc64(part * count)
    got = h.get("ChecksumCRC64NVME")
    finding = (
        "S3 computed the full-object CRC with no part checksums"
        if got == want
        else "S3 gave no full-object CRC: CompleteMultipartUpload must be given one"
        if not got
        else f"S3 gave a CRC that is not the object's ({got} != {want})"
    )
    if got and got != want:
        raise CheckFailed(finding)
    return {"finding": finding, "size": len(part) * count, "head_crc": got}


@auto("RW-03", "CopyObject with ChecksumAlgorithm=CRC64NVME keeps the source's CRC")
def rw03(env: AwsEnv):
    s3, b = env.c("s3"), env.n["source_bucket"]
    src = env.objects("STANDARD")[0]["key"]
    s3.copy_object(
        Bucket=env.n["staging_bucket"],
        Key="checks/rw03-copy",
        CopySource={"Bucket": b, "Key": src},
        ChecksumAlgorithm="CRC64NVME",
    )
    a = s3.head_object(Bucket=b, Key=src, ChecksumMode="ENABLED").get("ChecksumCRC64NVME")
    c = s3.head_object(
        Bucket=env.n["staging_bucket"], Key="checks/rw03-copy", ChecksumMode="ENABLED"
    ).get("ChecksumCRC64NVME")
    if not a or a != c:
        raise CheckFailed(f"source CRC {a}, copy CRC {c}")
    return {"crc64nvme": a}


manual(
    "RW-04",
    "the x-amz-restore header after expiry, and GetObject in the gap",
    "two days: a 1-day restore has to finish and expire",
    [
        "Restore a Glacier object with Days=1 (run.sh up makes several); keep the run 60 h "
        "(--ttl-hours 60).",
        "HEAD it every 10 minutes from the expiry date in x-amz-restore until the header goes; "
        "GetObject in that gap; record the gap and whether bytes came back.",
    ],
)


@auto(
    "RW-05",
    "ListObjectsV2 with OptionalObjectAttributes=RestoreStatus through the interface endpoint",
)
def rw05(env: AwsEnv):
    g = env.objects("GLACIER")[0]["key"]
    try:
        env.c("s3").restore_object(
            Bucket=env.n["source_bucket"],
            Key=g,
            RestoreRequest={"Days": 1, "GlacierJobParameters": {"Tier": env.tier}},
        )
    except env.c("s3").exceptions.ClientError as e:
        if "RestoreAlreadyInProgress" not in str(e):
            raise
    return env.probe("list-restore-status", key=g)


@auto("RW-06", "RestoreObject: 409 while a restore runs, 200 (expiry extended) once restored")
def rw06(env: AwsEnv):
    s3, b = env.c("s3"), env.n["source_bucket"]
    key = env.objects("GLACIER")[1]["key"]
    req = {"Days": 1, "GlacierJobParameters": {"Tier": env.tier}}
    first = s3.restore_object(Bucket=b, Key=key, RestoreRequest=req)["ResponseMetadata"][
        "HTTPStatusCode"
    ]
    try:
        s3.restore_object(Bucket=b, Key=key, RestoreRequest=req)
        raise CheckFailed("a second RestoreObject during a restore was accepted")
    except s3.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        status = e.response["ResponseMetadata"]["HTTPStatusCode"]
        if code != "RestoreAlreadyInProgress" or status != 409:
            raise CheckFailed(f"want 409 RestoreAlreadyInProgress, got {status} {code}") from None
    ev = {"first": first, "second": "409 RestoreAlreadyInProgress"}
    head = s3.head_object(Bucket=b, Key=env.objects("GLACIER")[0]["key"])
    if 'ongoing-request="false"' not in head.get("Restore", ""):
        raise Partial(
            "the 200 on an already-restored copy: rerun checks once RW-05's restore "
            "has finished (3-5 h)",
            ev,
        )
    again = s3.restore_object(Bucket=b, Key=env.objects("GLACIER")[0]["key"], RestoreRequest=req)
    ev["restored"] = again["ResponseMetadata"]["HTTPStatusCode"]
    if ev["restored"] != 200:
        raise CheckFailed(f"RestoreObject on a restored copy gave {ev['restored']}")
    return ev


manual(
    "RW-07",
    "Intelligent-Tiering archive tiers",
    "90 days or more in Archive Access",
    [
        "Turn on the Archive Access tier for a prefix of a long-lived bucket; wait 90 days.",
        "HEAD shows ArchiveStatus; RestoreObject without Days; afterwards the object is in the "
        "frequent tier with no x-amz-restore expiry.",
    ],
)


@auto("RW-08", "a single-part ETag is the MD5 under SSE-S3 and not under SSE-KMS")
def rw08(env: AwsEnv):
    s3 = env.c("s3")
    body = b"OmniFile ETag check\n" * 100
    md5 = hashlib.md5(body).hexdigest()
    a = s3.put_object(Bucket=env.n["source_bucket"], Key="checks/rw08-s3", Body=body)["ETag"].strip(
        '"'
    )
    k = s3.put_object(Bucket=env.n["kms_bucket"], Key="checks/rw08-kms", Body=body)["ETag"].strip(
        '"'
    )
    if a != md5:
        raise CheckFailed(f"SSE-S3 ETag {a} is not the MD5 {md5}")
    if k == md5:
        raise CheckFailed("the SSE-KMS ETag equals the MD5; the verifier's KMS assumption is wrong")
    return {"md5": md5, "sse_s3_etag": a, "sse_kms_etag": k}


manual(
    "RW-09",
    "real restore times for Flexible Retrieval and Deep Archive",
    "pilot data",
    [
        "Restore 8 GB, 184 GB and 1.2 TB objects at Standard and Bulk from each class; record the "
        "times; they set the ETA table.",
    ],
)

# ------------------------------------------------------------------------ events


@auto("RW-10", "EventBridge events carry keys as written: spaces, '+', '%' and non-ASCII")
def rw10(env: AwsEnv):
    created = {
        e["detail"]["object"]["key"]
        for e in env.events()
        if e.get("detail-type") == "Object Created"
    }
    want = {o["key"] for o in env.made["objects"]}
    missing = want - created
    if missing:
        raise CheckFailed(
            f"{len(missing)} keys had no event or came encoded: {sorted(missing)[:5]}"
        )
    return {"keys": sorted(want)}


@auto("RW-11", "the detail fields of each detail-type")
def rw11(env: AwsEnv):
    s3 = env.c("s3")
    s3.delete_object(Bucket=env.n["source_bucket"], Key=env.objects("STANDARD")[-1]["key"])
    seen: dict[str, set] = {}
    for e in env.events():
        seen.setdefault(e.get("detail-type", "?"), set()).update(
            e.get("detail", {}).get("object", {})
        )
        seen[e.get("detail-type", "?")].update(k for k in e.get("detail", {}) if k != "object")
    need = {
        "Object Created": {"version-id", "size"},
        "Object Deleted": {"deletion-type"},
        "Object Restore Initiated": {"source-storage-class"},
        "Object Restore Completed": {"restore-expiry-time", "source-storage-class"},
        "Object Storage Class Changed": {"destination-storage-class"},
    }
    ok = {t: sorted(f & seen.get(t, set())) for t, f in need.items()}
    missing = {t: sorted(f - seen.get(t, set())) for t, f in need.items() if f - seen.get(t, set())}
    if missing:
        raise Partial(
            f"not seen yet: {missing} (restores take hours, a class change a day)", {"seen": ok}
        )
    return {"seen": ok}


@auto("RW-12", "delivery lag and order from the bucket to the queue, read through the SQS endpoint")
def rw12(env: AwsEnv):
    s3 = env.c("s3")
    keys = [f"checks/rw12-{i:03d}" for i in range(20)]
    t0 = time.time()
    for k in keys:
        s3.put_object(Bucket=env.n["source_bucket"], Key=k, Body=b"lag")
    return env.probe("queue-lag", keys=keys, sent_at=t0)


manual(
    "RW-13",
    "key encoding in CSV inventories",
    "48 h for the first inventory report",
    [
        "Add a daily CSV inventory to the source bucket (destination: the reports bucket) and keep "
        "the run up 48 h (--ttl-hours 50).",
        "Read the report's keys for the tricky names the run uploaded; record how each is encoded.",
    ],
)
manual(
    "RW-14",
    "IntelligentTieringAccessTier and EncryptionStatus values in inventories",
    "the same inventory report",
    [
        "From RW-13's report: the IntelligentTieringAccessTier and EncryptionStatus columns "
        "of the IT and KMS objects; record the values.",
    ],
)
manual(
    "RW-15",
    "manifest.checksum and creationTimestamp",
    "the same inventory report",
    [
        "From RW-13's report: check manifest.checksum is the hex MD5 of manifest.json and "
        "creationTimestamp is milliseconds since the epoch.",
    ],
)
manual(
    "RW-16",
    "following a seeded source",
    "an Azure run and an AWS run both up",
    [
        "Register the AWS run's source bucket in the portal (Migrations, Add source) and seed it.",
        "PutObject, an overwrite, a delete marker and a version delete under seed/; each reaches the "
        "catalog (portal file view) with one HEAD per event (CloudTrail data events).",
        "A burst of 5,000 uploads in a minute keeps up.",
    ],
)


@auto("RW-17", "HEAD with checksum mode on an archived object returns its CRC without a restore")
def rw17(env: AwsEnv):
    s3 = env.c("s3")
    o = env.objects("DEEP_ARCHIVE")[0]
    h = s3.head_object(Bucket=env.n["source_bucket"], Key=o["key"], ChecksumMode="ENABLED")
    if not h.get("ChecksumCRC64NVME"):
        raise CheckFailed("no CRC64NVME on HEAD of an archived object")
    if h.get("Restore"):
        raise CheckFailed("the object had a restore; the check needs one without")
    return {"key": o["key"], "crc64nvme": h["ChecksumCRC64NVME"], "class": h.get("StorageClass")}


def _manifest(env: AwsEnv, name: str, objs: list[dict], versions: bool) -> tuple[str, str]:
    s3, b = env.c("s3"), env.n["source_bucket"]
    rows = []
    for o in objs:
        # encoded as the backend's manifests are (`S3Backend._manifest`)
        line = [b, quote(o["key"], safe="/")]
        if versions:
            line.append(s3.head_object(Bucket=b, Key=o["key"]).get("VersionId", ""))
        rows.append(",".join(line))
    key = f"manifests/{name}.csv"
    put = s3.put_object(Bucket=env.n["reports_bucket"], Key=key, Body="\n".join(rows).encode())
    return key, put["ETag"]


def _job(env: AwsEnv, op: dict, manifest: tuple[str, str], versions: bool, count: int) -> str:
    s3c = env.c("s3control")
    acct = env.ctx.state["identity"]["account"]
    fields = ["Bucket", "Key"] + (["VersionId"] if versions else [])
    job = s3c.create_job(
        AccountId=acct,
        ConfirmationRequired=False,
        Operation=op,
        Priority=10,
        RoleArn=env.made["roles"]["batch"],
        ClientRequestToken=f"{env.ctx.run_id}-{len(fields)}-{next(iter(op))}",
        Manifest={
            "Spec": {"Format": "S3BatchOperations_CSV_20180820", "Fields": fields},
            "Location": {
                "ObjectArn": f"arn:aws:s3:::{env.n['reports_bucket']}/{manifest[0]}",
                "ETag": manifest[1].strip('"'),
            },
        },
        Report={
            "Bucket": f"arn:aws:s3:::{env.n['reports_bucket']}",
            "Format": "Report_CSV_20180820",
            "Enabled": True,
            "Prefix": "reports",
            "ReportScope": "AllTasks",
            # Required for S3ComputeObjectChecksum (InvalidRequest without it, the fourth
            # real run); a restore job takes it or leaves it. The backend sends it too.
            "ExpectedBucketOwner": acct,
        },
        Tags=env.aws.tag_list(),
    )
    jid = job["JobId"]

    def done():
        j = s3c.describe_job(AccountId=acct, JobId=jid)["Job"]
        return j if j["Status"] in ("Complete", "Failed", "Cancelled") else None

    j = env.ctx.wait(f"batch job {jid}", done, timeout_s=3600, every_s=30)
    if j["Status"] != "Complete":
        raise CheckFailed(f"job {jid} ended {j['Status']}")
    del count
    return jid


@auto("RW-18", "Batch Operations restore waves from a CSV manifest, with and without version ids")
def rw18(env: AwsEnv):
    objs = env.objects("GLACIER")[2:]
    ev = {}
    # A job costs $0.25: the cheap pass runs the one wave with version ids, the form the
    # migration engine uses, and leaves the other for a full run.
    waves = (True,) if env.aws.scope == "cheap" else (False, True)
    for versions in waves:
        m = _manifest(env, f"restore-{'v' if versions else 'nov'}", objs, versions)
        jid = _job(
            env,
            {
                "S3InitiateRestoreObject": {
                    "ExpirationInDays": 1,
                    "GlacierJobTier": env.tier.upper(),
                }
            },
            m,
            versions,
            len(objs),
        )
        ev["versions" if versions else "no_versions"] = jid
    raise Partial(
        "the Restore Completed events (hours), and creating the job through the S3 "
        "Control interface endpoint from the probe"
        + ("; the wave without version ids (cheap scope)" if len(waves) == 1 else ""),
        ev,
    )


@auto("RW-19", "Batch Operations Compute checksum: the completion report's layout and fields")
def rw19(env: AwsEnv):
    # Names whose spelling in the report matters to the backend's decoding: a space, a plus,
    # a percent sign, a name that already looks encoded, a comma (the report is CSV) and
    # non-ASCII. Written here, in Standard, so the job answers in minutes.
    s3 = env.c("s3")
    tricky = ["with space", "plus+sign", "per%cent", "looks%2Bencoded", "com,ma", "nörd-ünï"]
    objs = env.objects("STANDARD")[:1]
    for i, name in enumerate(tricky):
        key = f"checks/rw19/{name}-{i:02d}.bin"
        s3.put_object(
            Bucket=env.n["source_bucket"],
            Key=key,
            Body=data(f"rw19-{i}", 2048),
            ChecksumAlgorithm="CRC64NVME",
        )
        objs.append({"key": key})
    m = _manifest(env, "checksum", objs, False)
    jid = _job(
        env,
        {
            "S3ComputeObjectChecksum": {
                "ChecksumAlgorithm": "CRC64NVME",
                "ChecksumType": "FULL_OBJECT",
            }
        },
        m,
        False,
        len(objs),
    )
    rb = env.n["reports_bucket"]
    man = json.loads(
        s3.get_object(Bucket=rb, Key=f"reports/job-{jid}/manifest.json")["Body"].read()
    )
    rows = []
    for r in man["Results"]:
        body = s3.get_object(Bucket=rb, Key=r["Key"])["Body"].read().decode()
        rows += list(csv.reader(io.StringIO(body)))
    # The report as S3 wrote it, kept whatever the comparison below says of it.
    (env.ctx.evidence / "RW-19-report.json").write_text(
        json.dumps({"manifest": man, "rows": rows}, indent=2, ensure_ascii=False)
    )
    # Read as the backend reads it (`S3Backend.batch_checksums`): a row is Bucket, Key,
    # VersionId, TaskStatus, then two columns and the result message as the seventh; the key
    # is form-decoded (a real report writes a space as '+' and a plus as '%2B'); the
    # algorithm is named; the value is in hex or base64.
    got, msg = {}, {}
    for row in rows:
        try:
            msg = json.loads(row[6])
        except (IndexError, ValueError):
            raise CheckFailed(f"a report row isn't the layout the backend reads: {row}") from None
        if row[3] != "succeeded" or msg.get("checksumAlgorithm") != "CRC64NVME":
            continue
        if "checksum_hex" in msg:
            got[unquote_plus(row[1])] = bytes.fromhex(str(msg["checksum_hex"]))
        elif "checksum_base64" in msg:
            got[unquote_plus(row[1])] = base64.b64decode(str(msg["checksum_base64"]))
    wrong = []
    for o in objs:
        head = s3.head_object(Bucket=env.n["source_bucket"], Key=o["key"], ChecksumMode="ENABLED")
        if got.get(o["key"]) != base64.b64decode(head.get("ChecksumCRC64NVME", "")):
            wrong.append(o["key"])
    if wrong:
        raise CheckFailed(
            f"the backend's reading of the report finds no matching checksum for {wrong}; "
            f"the report's keys: {[r[1] for r in rows]} (evidence/RW-19-report.json)"
        )
    return {
        "job": jid,
        "layout": f"reports/job-{jid}/manifest.json",
        "fields": sorted(msg),
        "report_keys": {
            o["key"]: next((r[1] for r in rows if unquote_plus(r[1]) == o["key"]), None)
            for o in objs
        },
        "manifest": man,
    }


manual(
    "RW-20",
    "presigned GETs from the service's web-identity session",
    "an Azure run and an AWS run with the service's web-identity role",
    [
        "Give the Azure run's service the AWS run's web-identity role (OMNIFILE_AWS_ACCOUNTS).",
        "From the edge, read an S3-resident file with edge_credentials = presigned; record the "
        "URL's lifetime cap, a ranged GET of a version, and 403 InvalidObjectState on Glacier.",
    ],
)


@auto(
    "RW-21", "an edge's Roles Anywhere session reads the source prefix only, through the endpoint"
)
def rw21(env: AwsEnv):
    from ..pki import leaf

    cert = leaf(env.ctx, "edge", f"omnifile-edge-{env.n['prefix']}a", "omnifile-roles-anywhere")
    return env.probe("roles-anywhere-read", cert=cert, prefix="seed/")


@auto("RW-22", "a trust anchor from OmniFile's edge CA, and a session from a certificate it issued")
def rw22(env: AwsEnv):
    ra = env.c("rolesanywhere")
    ta = [
        t
        for t in ra.list_trust_anchors()["trustAnchors"]
        if t["trustAnchorArn"] == env.made["trust_anchor_arn"]
    ]
    if not ta:
        raise CheckFailed("the run's trust anchor is gone")
    ev = {"trust_anchor": env.made["trust_anchor_arn"], "real_edge_ca": bool(env.ctx.azure_run)}
    if not env.ctx.azure_run:
        raise Partial(
            "made from a test CA of the same shape; rerun with --azure-run for the "
            "real edge CA and an enrol-issued certificate",
            ev,
        )
    ev.update(env.probe("roles-anywhere-session", cert_from="enrol"))
    return ev


@auto("RW-23", "the trust policy admits that edge's certificate and refuses the others")
def rw23(env: AwsEnv):
    from ..pki import leaf

    p = env.n["prefix"]
    certs = {
        "edge": leaf(env.ctx, "edge", f"omnifile-edge-{p}a", "omnifile-roles-anywhere"),
        "mtls": leaf(env.ctx, "mtls", f"omnifile-edge-{p}a", None),
        "other-edge": leaf(env.ctx, "other-edge", f"omnifile-edge-{p}b", "omnifile-roles-anywhere"),
        "other-ca": leaf(
            env.ctx, "other-ca", f"omnifile-edge-{p}a", "omnifile-roles-anywhere", ca="other-ca"
        ),
    }
    return env.probe(
        "trust-conditions",
        certs=certs,
        expect={"edge": True, "mtls": False, "other-edge": False, "other-ca": False},
    )


@auto("RW-24", "the CRL import and update, and a revoked edge's next session refused")
def rw24(env: AwsEnv):
    from ..pki import crl, leaf

    ra = env.c("rolesanywhere")
    revoked = leaf(
        env.ctx, "revoked", f"omnifile-edge-{env.n['prefix']}a", "omnifile-roles-anywhere"
    )
    tags = [{"key": "omnifile-trust-anchor", "value": env.made["trust_anchor_id"]}] + [
        {"key": k, "value": v} for k, v in env.ctx.tags().items()
    ]
    # Roles Anywhere refuses a CRL that revokes nothing ("At least one of the included CRLs
    # doesn't include a revoked certificate", the second real run), so the first import
    # already carries the revoked certificate, as the worker's first publish does.
    listed = crl(env.ctx, [revoked["serial"]]).encode()
    got = ra.import_crl(
        name=f"{env.n['trust_anchor']}-crl",
        enabled=True,
        tags=tags,
        trustAnchorArn=env.made["trust_anchor_arn"],
        crlData=listed,
    )["crl"]
    ra.update_crl(crlId=got["crlId"], crlData=listed)
    if not any(c["crlId"] == got["crlId"] for c in ra.list_crls()["crls"]):
        raise CheckFailed("ListCrls doesn't find the imported CRL")
    ev = {"crl": got["crlArn"]}
    left = []
    try:
        ev.update(env.probe("revoked-refused", cert=revoked, within_s=60))
    except Partial as e:  # the probe proved part: keep what it and this check recorded
        left.append(str(e))
        ev.update(e.evidence)
    finally:
        # What the weekly refresh would meet once every revoked certificate has expired and
        # the CA's CRL is empty again: recorded, never a failure of this check.
        try:
            ra.update_crl(crlId=got["crlId"], crlData=crl(env.ctx, []).encode())
            ev["empty_update"] = "accepted"
        except Exception as e:  # noqa: BLE001 - the answer is the evidence
            ev["empty_update"] = f"refused: {type(e).__name__}: {e}"[:300]
        # What the product sends instead: a CRL whose one entry is a serial no live
        # certificate has (Roles Anywhere can't tell it from a long-expired certificate's).
        try:
            ra.update_crl(crlId=got["crlId"], crlData=crl(env.ctx, ["0badc0de0badc0de"]).encode())
            ev["stale_serial_only_update"] = "accepted"
        except Exception as e:  # noqa: BLE001 - the answer is the evidence
            ev["stale_serial_only_update"] = f"refused: {type(e).__name__}: {e}"[:300]
    if left:
        raise Partial(left[0], ev)
    raise Partial(
        "the service role's web-identity session and its tag conditions "
        "(service-crl-policy.json), and what happens when nextUpdate passes",
        ev,
    )


manual(
    "RW-25",
    "the restore budget under a wave",
    "10,000 or more archived objects",
    [
        "Put 10,000 small objects in GLACIER (about $0.30), start a batch restore of them, and "
        "meanwhile send 50 single RestoreObject calls a second for a minute.",
        "Record the share of SlowDown answers and that retries clear them.",
    ],
)
manual(
    "RW-26",
    "staging copies keep the full-object CRC",
    "a restored Glacier object (hours)",
    [
        "Once RW-05's restore has finished, CopyObject it into the staging bucket as STANDARD with "
        "ChecksumAlgorithm=CRC64NVME; compare the CRCs.",
    ],
)
manual(
    "RW-27",
    "the snapshot gap is replayed from the queue",
    "an Azure run and an AWS run, and an inventory (48 h)",
    [
        "Seed the AWS run's bucket from an inventory taken after the EventBridge rule; write "
        "objects between the inventory's snapshot and the registration; each reaches the catalog.",
    ],
)

# ---------------------------------------------------------- network, credentials


@auto("RW-28", "aws_signing_helper credential-process: flags, and the region from the anchor ARN")
def rw28(env: AwsEnv):
    from ..pki import leaf

    cert = leaf(env.ctx, "edge", f"omnifile-edge-{env.n['prefix']}a", "omnifile-roles-anywhere")
    ev = env.probe("credential-process", cert=cert)
    raise Partial(
        "the key through PKCS#11 in the TPM: on the appliance (WP2's smoke test "
        "covers the sealed key with the stub)",
        ev,
    )


@auto("RW-29", "TLS 1.2 to the S3 and SQS interface endpoints, virtual-hosted addressing")
def rw29(env: AwsEnv):
    return env.probe("tls-endpoints")


manual(
    "RW-30",
    "the edge's own Roles Anywhere session by edge_credential's pointer",
    "an Azure run whose appliance reads the AWS run's bucket",
    [
        "Register the AWS run's bucket as a source; on the appliance, read an S3-resident file; "
        "record the helper's session in the edge's audit.",
    ],
)
manual(
    "RW-31",
    "ThrottlingException above 1,000 restore requests a second",
    "a load test",
    [
        "Send 1,200 RestoreObject requests a second for 30 s against 40,000 GLACIER objects; record "
        "the throttling answers and the token bucket's pace.",
    ],
)
manual(
    "RW-32",
    "S3 Metadata tables",
    "a 10^5-object bucket with a metadata configuration",
    [
        "Follow docs/design/s3-metadata.md section 5 on a bucket of about 10^5 objects.",
        "Record each sub-item of the line (endpoint and signing, data files, names, schemas, "
        "versions, backfill and lag, snapshots, journal rows, zero LIST).",
    ],
)


# ------------------------------------------------------------------ Connect AWS (WP19)

manual(
    "RW-33",
    "the connect template published in S3",
    "the first release",
    [
        'Publish omnifile-connect.yaml; set awsConnectTemplateUrl; record the page"s Launch in AWS link.',
    ],
)

manual(
    "RW-34",
    "the quick-create link opens filled in",
    "an AWS console sign-in",
    [
        "Press Launch in AWS; record the parameters the console shows.",
    ],
)

manual(
    "RW-35",
    "the connect stack creates cleanly",
    "a fresh AWS account",
    [
        "Create the stack with and without CreateOidcProvider and a VPC; record events and outputs.",
    ],
)

manual(
    "RW-36",
    'the S3 interface endpoint"s DNS names',
    "the stack of RW-35 with a VPC",
    [
        "Record S3EndpointDns, and that bucket. and control. names answer S3 and S3 Control.",
    ],
)

manual(
    "RW-37",
    "the fetch through real STS reads only",
    "the stack of RW-35",
    [
        'Press Fetch; record the task"s result and CloudTrail"s events for the session (no writes).',
    ],
)

manual(
    "RW-38",
    "the deny statement holds against an added full-access policy",
    "the stack of RW-35",
    [
        "Attach AmazonS3FullAccess to the role; try a PutObject on a source; record AccessDenied. Run a Batch restore.",
    ],
)

manual(
    "RW-39",
    "the confirmed entry loads in the worker",
    "a deployment and the stack of RW-35",
    [
        "Set awsAccounts to the confirmed value; re-deploy; record Test and a restore wave.",
    ],
)

manual(
    "RW-40",
    "a scan of a real prefix",
    "a bucket of about 10^6 objects",
    [
        "Scan it from New migration; record time, LIST count, classes and CloudTrail (lists only).",
    ],
)
