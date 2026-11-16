"""The AWS run: tagged buckets (Standard, Glacier, Deep Archive, Intelligent-Tiering
objects), an EventBridge rule to an SQS queue, S3 Batch Operations jobs, an IAM Roles
Anywhere trust anchor and profile, and an EC2 probe in a private subnet that reaches S3,
SQS, S3 Control, STS, Roles Anywhere and SSM only through interface endpoints. Then the
real-AWS checks, and teardown by tag.

Every call goes through boto3 sessions from session(); a dry run points them at a moto
server (plus tests/real/fakes/aws_extra.py for what moto lacks: Roles Anywhere, S3 Control
jobs, the S3-to-EventBridge delivery, SSM command output), and records each call in the
transcript.
"""

from __future__ import annotations

import itertools
import json
import re
from contextlib import contextmanager
from typing import Any

import boto3

from . import guide
from .allowlist import AllowList
from .core import (
    HARNESS_TAG_VALUE,
    REPO,
    TAG_HARNESS,
    TAG_RUN,
    AwaitingApproval,
    Context,
    Refused,
    StepFailed,
    UsageError,
    digest_of,
    norm_digest,
    prefix_for,
)
from .guiderun import run_steps
from .results import run_checks

PROBE_AMI_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
ENDPOINT_SERVICES = (
    "s3",
    "sqs",
    "s3-control",
    "sts",
    "rolesanywhere",
    "ssm",
    "ssmmessages",
    "ec2messages",
)
#: The cheap pass's endpoints: each bills by the hour, so only those its checks go through
#: (S3 and SQS, Roles Anywhere for the edge's sessions, and the three SSM needs, since the
#: probe has no internet). No STS or S3 Control endpoint: the checks that use them stay partial.
CHEAP_ENDPOINTS = ("s3", "sqs", "rolesanywhere", "ssm", "ssmmessages", "ec2messages")
#: The probe: the smallest x86 instance an account on AWS's free plan may start. Such an
#: account refuses every type that isn't Free Tier eligible, t3.nano among them
#: (`InvalidParameterCombination` from RunInstances, seen on the first real run).
PROBE_TYPE = {"full": "t3.micro", "cheap": "t3.micro"}
#: The cheap pass's restores: Flexible Retrieval at Bulk (free retrieval, 5-12 h).
RESTORE_TIER = {"full": "Standard", "cheap": "Bulk"}
#: The cheap pass's default expiry: one `all` takes well under an hour; the expiry only
#: bounds what a failed teardown would leave (--ttl-hours overrides it).
CHEAP_TTL_HOURS = 3
#: What tests/real/aws/harness-roles.yaml makes, once, from the owner's own session: the
#: harness reads and passes these, and never changes them.
BASE_PATH = "/omnifile-real-base/"
BASE_ROLES = {"batch": "omnifile-real-batch-ops", "probe": "omnifile-real-probe"}
BASE_PROFILE = "omnifile-real-probe"
#: Where IAM keeps Roles Anywhere's service-linked role (AWSServiceRoleForRolesAnywhere).
RA_SLR_PATH = "/aws-service-role/rolesanywhere.amazonaws.com/"
BASE_BOUNDARY = "omnifile-real-base-boundary"
#: The boundary every role the harness makes must carry (each run's edge role).
EDGE_BOUNDARY = "omnifile-real-edge-boundary"
VPC_CIDR = "10.61.0.0/16"
SUBNET_CIDR = "10.61.1.0/24"


class AWS:
    def __init__(self, ctx: Context, al: AllowList, moto_url: str | None = None) -> None:
        self.ctx, self.al, self.moto_url = ctx, al, moto_url
        self._session: boto3.Session | None = None

    # -------------------------------------------------------------- session

    def session(self) -> boto3.Session:
        if self._session is None:
            if self.ctx.dry_run:
                from fakes import aws_extra  # tests/real/fakes, only in a dry run

                s = boto3.Session(
                    aws_access_key_id="fake-access-key",
                    aws_secret_access_key="fake-secret-key",
                    region_name=self.region,
                )
                aws_extra.install(s, self.ctx, self.moto_url)
            else:
                s = boto3.Session(profile_name=self.al.one("aws-profile"), region_name=self.region)
            self._session = s
        return self._session

    def client(self, name: str):
        kw = {"endpoint_url": self.moto_url} if self.moto_url else {}
        return self.session().client(name, **kw)

    @property
    def region(self) -> str:
        return self.al.one("aws-region")

    # ------------------------------------------------------------- identity

    def pin_env(self) -> None:
        """Every subprocess, guide steps included, uses the allow-listed profile and region.
        Keys in the environment would override a profile, so a real run drops them."""
        env, profile = self.ctx.env, self.al.one("aws-profile")
        if not self.ctx.dry_run:
            for k in (
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_SECURITY_TOKEN",
                "AWS_DEFAULT_PROFILE",
            ):
                env.pop(k, None)
        else:
            cfg = env.get("AWS_CONFIG_FILE")
            if cfg:
                with open(cfg, "w") as f:
                    f.write(
                        f"[default]\nregion = {self.region}\n\n[profile {profile}]\n"
                        f"region = {self.region}\n"
                    )
        env.update(
            {"AWS_PROFILE": profile, "AWS_REGION": self.region, "AWS_DEFAULT_REGION": self.region}
        )

    def identity(self) -> dict[str, str]:
        self.pin_env()
        who = self.client("sts").get_caller_identity()
        acct = who["Account"]
        self.al.require("aws-account", acct, "AWS account")
        st = self.ctx.state.setdefault("identity", {"account": acct, "arn": who["Arn"]})
        if st["account"] != acct:
            raise Refused(
                f"run {self.ctx.run_id} was planned in account {st['account']}; "
                f"you are signed in to {acct}"
            )
        return st

    # ----------------------------------------------------------------- plan

    def names(self) -> dict[str, Any]:
        p = prefix_for(self.ctx.run_id)
        base = f"ofreal-{self.ctx.run_id}"
        return {
            "prefix": p,
            "source_bucket": f"{base}-src",
            "staging_bucket": f"{base}-staging",
            "reports_bucket": f"{base}-reports",
            "kms_bucket": f"{base}-kms",
            "queue": f"{base}-events",
            "rule": f"{base}-s3",
            "iam_path": f"/omnifile-real/{self.ctx.run_id}/",
            # The whole run id, never the 8-character prefix two runs can share.
            # The run's own role (the batch and probe roles are the owner's: BASE_ROLES).
            "roles": {
                "edge": f"{base}-edge-s3-read",
                "crl": f"{base}-service-crl",
            },
            "trust_anchor": f"{base}-edge-ca",
            "profile": f"{base}-edges",
            "vpc_cidr": VPC_CIDR,
            "subnet_cidr": SUBNET_CIDR,
            "endpoints": list(CHEAP_ENDPOINTS if self.scope == "cheap" else ENDPOINT_SERVICES),
            "probe": f"{base}-probe",
            "objects": {"STANDARD": 4, "GLACIER": 6, "DEEP_ARCHIVE": 2, "INTELLIGENT_TIERING": 2},
        }

    @property
    def scope(self) -> str:
        """full or cheap: fixed at plan (it is in the plan's digest), never changed after."""
        return self.ctx.state.get("scope") or self.ctx.scope or "full"

    def the_plan(self) -> dict[str, Any]:
        st = self.ctx.state
        return {
            "scope": self.scope,
            "cloud": "aws",
            "run": self.ctx.run_id,
            "account": st["identity"]["account"],
            "region": self.region,
            "expires": self.ctx.expires,
            "resources": self.names(),
        }

    def plan(self) -> None:
        ctx = self.ctx
        with ctx.step("identity"):
            self.identity()
        st = ctx.state
        st.update(
            {
                "run_id": ctx.run_id,
                "cloud": "aws",
                "dry_run": ctx.dry_run,
                "prefix": prefix_for(ctx.run_id),
            }
        )
        if ctx.scope and st.get("scope") and ctx.scope != st["scope"]:
            raise UsageError(f"run {ctx.run_id} was planned with --scope {st['scope']}")
        st["scope"] = ctx.scope or st.get("scope") or "full"
        if st["scope"] == "cheap" and not ctx.ttl_given:
            ctx.ttl_hours = CHEAP_TTL_HOURS
        ctx.fix_expiry(self.al.max_ttl)
        plan = self.the_plan()
        plan["estimated_cost_usd"] = estimate_cost(plan, ctx)
        (ctx.evidence / "plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        st["digest"] = digest_of(self.the_plan())
        st["planned"] = True
        ctx.save_state()
        est = plan["estimated_cost_usd"]
        ctx.log(
            f"estimated cost of this run: about ${est['total']:.2f} "
            f"({', '.join(f'{k} ${v:.2f}' for k, v in est['items'].items())}; "
            f"list prices, {self.region}, {est['hours']} h; evidence/plan.json)"
        )
        ctx.log(f"plan digest {st['digest']} (evidence/plan.json lists every resource)")

    def gate(self) -> None:
        ctx = self.ctx
        if not ctx.approve:
            raise AwaitingApproval(f"awaiting approval: rerun with --approve {ctx.state['digest']}")
        fresh = digest_of(self.the_plan())
        if ctx.approve == "auto":
            if not ctx.dry_run:
                raise Refused("--approve auto is for dry runs only")
            ctx.log(f"dry run: approving this run's own plan digest {fresh}")
            want = fresh
        else:
            want = norm_digest(ctx.approve)
        if fresh != want:
            raise AwaitingApproval(f"the plan changed: approved {want}, now {fresh}")
        ctx.state["approved"] = fresh
        ctx.save_state()

    # ------------------------------------------------------------------- up

    def tag_list(self) -> list[dict[str, str]]:
        return [{"Key": k, "Value": v} for k, v in self.ctx.tags().items()]

    def up(self) -> None:
        ctx = self.ctx
        with ctx.step("identity"):
            self.identity()
        if not ctx.state.get("planned"):
            raise UsageError(f"run {ctx.run_id} has no plan; run `plan` first")
        with ctx.step("gate"):
            self.gate()
        n = self.names()
        made = ctx.state.setdefault("made", {})
        s3 = self.client("s3")
        with ctx.step("up.iam"):
            self._iam(n)  # the owner's roles, first: nothing is made without them
        with ctx.step("up.buckets"):
            for key in ("source_bucket", "staging_bucket", "reports_bucket", "kms_bucket"):
                b = n[key]
                kw = (
                    {}
                    if self.region == "us-east-1"
                    else {"CreateBucketConfiguration": {"LocationConstraint": self.region}}
                )
                s3.create_bucket(Bucket=b, **kw)
                s3.put_bucket_tagging(Bucket=b, Tagging={"TagSet": self.tag_list()})
                s3.put_bucket_versioning(Bucket=b, VersioningConfiguration={"Status": "Enabled"})
                s3.put_public_access_block(
                    Bucket=b,
                    PublicAccessBlockConfiguration={
                        "BlockPublicAcls": True,
                        "IgnorePublicAcls": True,
                        "BlockPublicPolicy": True,
                        "RestrictPublicBuckets": True,
                    },
                )
            s3.put_bucket_encryption(
                Bucket=n["kms_bucket"],
                ServerSideEncryptionConfiguration={
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms"}}]
                },
            )
            made["buckets"] = [
                n[k] for k in ("source_bucket", "staging_bucket", "reports_bucket", "kms_bucket")
            ]
            ctx.save_state()
        with ctx.step("up.events"):
            sqs = self.client("sqs")
            q = sqs.create_queue(QueueName=n["queue"], tags=ctx.tags())["QueueUrl"]
            qarn = sqs.get_queue_attributes(QueueUrl=q, AttributeNames=["QueueArn"])["Attributes"][
                "QueueArn"
            ]
            ev = self.client("events")
            rule = ev.put_rule(
                Name=n["rule"],
                EventPattern=json.dumps(
                    {"source": ["aws.s3"], "detail": {"bucket": {"name": [n["source_bucket"]]}}}
                ),
                State="ENABLED",
                Tags=self.tag_list(),
            )["RuleArn"]
            sqs.set_queue_attributes(
                QueueUrl=q,
                Attributes={
                    "Policy": json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Principal": {"Service": "events.amazonaws.com"},
                                    "Action": "sqs:SendMessage",
                                    "Resource": qarn,
                                    "Condition": {"ArnEquals": {"aws:SourceArn": rule}},
                                }
                            ],
                        }
                    )
                },
            )
            ev.put_targets(Rule=n["rule"], Targets=[{"Id": "queue", "Arn": qarn}])
            s3.put_bucket_notification_configuration(
                Bucket=n["source_bucket"],
                NotificationConfiguration={"EventBridgeConfiguration": {}},
            )
            made.update({"queue_url": q, "queue_arn": qarn, "rule_arn": rule})
            ctx.save_state()
        with ctx.step("up.events-ready"):
            self._events_ready(n)
        with ctx.step("up.objects"):
            self._objects(n)
        with ctx.step("up.network"):
            self._network(n)
        with ctx.step("up.roles-anywhere"):
            self._roles_anywhere(n)
        with ctx.step("up.probe"):
            self._probe(n)
        steps = [s for s in self._guide_steps() if s.cloud == "aws"]
        if steps:
            from . import release

            rel = release.make(ctx.out, dry_run=ctx.dry_run, guide=self._guide_dir())
            g = self._guide_dir()
            guide.write_env(
                g / "omnifile.env.example", rel / "omnifile.env", self.guide_values(), partial=True
            )
            with ctx.step("up.identity-again"):
                self.identity()  # the account the steps will act in, checked just before them
            before = self._inventory()
            try:
                run_steps(ctx, steps, rel, {}, lambda: self.guide_values())
            finally:
                with ctx.step("up.adopt"):
                    self._adopt(before)
        ctx.state["up"] = True
        ctx.save_state()

    def guide_values(self) -> dict[str, str]:
        n, made = self.names(), self.ctx.state.get("made", {})
        from .pki import edge_ca

        ca = self.ctx.out / "pki" / "edge-ca-bundle.pem"
        ca.parent.mkdir(parents=True, exist_ok=True)
        ca.write_text(edge_ca(self.ctx))
        return {
            "AWS_REGION": self.region,
            "AWS_ACCOUNT_ID": self.ctx.state["identity"]["account"],
            "SOURCE_BUCKET": n["source_bucket"],
            "TRUST_ANCHOR_ARN": made.get("trust_anchor_arn", ""),
            "S3_INTERFACE_ENDPOINT_ID": made.get("endpoints", {}).get("s3", ""),
            "EDGE_CA_FILE": str(ca),
            "OMNIFILE_RUN_ID": self.ctx.run_id,
            "OMNIFILE_RUN_EXPIRES": self.ctx.expires,
            # WP7's section 17 adds -<suffix> to every name it makes (#33 at 3cd6a16): with the
            # run id there, adoption (by run id) finds and removes them.
            "OMNI_AWS_NAME_SUFFIX": self.ctx.run_id,
        }

    # ------------------------------------------------- guide-made resources

    def adoptable(self, name: str) -> bool:
        """Only what is visibly this run's: its whole run id in the name, as a word of its own
        (not the prefix, which two runs can share). Anything else that appeared during the
        steps is someone else's, and is never tagged or deleted."""
        leaf = name.rsplit("/", 1)[-1]
        rid = re.escape(self.ctx.run_id)
        return re.search(rf"(?<![0-9A-Za-z]){rid}(?![0-9A-Za-z])", leaf) is not None

    def _inventory(self) -> dict[str, set[str]]:
        """What exists now, of the kinds the guide's AWS steps make. A resource present after
        the steps and not before, and named for this run (adoptable), was made by them and gets
        the run's tags; nothing that was there before is ever adopted."""
        ra, iam = self.client("rolesanywhere"), self.client("iam")
        roles = {
            r["RoleName"] for p in iam.get_paginator("list_roles").paginate() for r in p["Roles"]
        }
        return {
            "anchors": {
                f"{t['trustAnchorArn']}|{t['name']}"
                for t in ra.list_trust_anchors()["trustAnchors"]
            },
            "profiles": {f"{p['profileArn']}|{p['name']}" for p in ra.list_profiles()["profiles"]},
            "roles": roles,
            "buckets": {b["Name"] for b in self.client("s3").list_buckets().get("Buckets", [])},
        }

    def _adopt(self, before: dict[str, set[str]]) -> None:
        after = self._inventory()
        ra, iam, s3 = self.client("rolesanywhere"), self.client("iam"), self.client("s3")
        tags = self.ctx.tags()
        adopted = self.ctx.state.setdefault("adopted", [])
        foreign = []
        for key in sorted(after["anchors"] - before["anchors"]) + sorted(
            after["profiles"] - before["profiles"]
        ):
            arn, name = key.split("|", 1)
            if not self.adoptable(name):
                foreign.append(name)
                continue
            ra.tag_resource(resourceArn=arn, tags=[{"key": k, "value": v} for k, v in tags.items()])
            adopted.append({"kind": "rolesanywhere", "id": arn})
        for name in sorted(after["roles"] - before["roles"]):
            if not self.adoptable(name):
                foreign.append(name)
                continue
            iam.tag_role(RoleName=name, Tags=self.tag_list())
            adopted.append({"kind": "role", "id": name})
        for name in sorted(after["buckets"] - before["buckets"]):
            if not self.adoptable(name):
                foreign.append(name)
                continue
            s3.put_bucket_tagging(Bucket=name, Tagging={"TagSet": self.tag_list()})
            adopted.append({"kind": "bucket", "id": name})
        if foreign:
            self.ctx.log(
                "not adopted (not named for this run, so not ours to remove): " + ", ".join(foreign)
            )
        if adopted:
            self.ctx.log(
                f"adopted {len(adopted)} resource(s) the guide's steps made: "
                + ", ".join(a["id"].rsplit("/", 1)[-1] for a in adopted)
            )
        self.ctx.save_state()

    def _guide_dir(self):
        """The guide under test: --guide, else docs/guide, else (a dry run only) the fixture."""
        if self.ctx.guide:
            return self.ctx.guide
        real = REPO / "docs" / "guide"
        if real.is_dir() or not self.ctx.dry_run:
            return real
        return REPO / "tests" / "real" / "fakes" / "guide"

    def _guide_steps(self) -> list[guide.Step]:
        g = self._guide_dir()
        if not g.is_dir():
            return []
        return guide.parse(g)

    def _events_ready(self, n: dict[str, Any]) -> None:
        """Wait until the source bucket's events reach the queue. S3 takes about five minutes
        to start sending a bucket's events to EventBridge after the setting is made (AWS's
        figure), and an object written before then has no event at all: the first full real
        run seeded its objects seconds after, and none of the 14 had one. A small object is
        written every half minute until one's event arrives."""
        s3, sqs, made = self.client("s3"), self.client("sqs"), self.ctx.state["made"]
        tries = itertools.count()

        def seen() -> bool:
            s3.put_object(
                Bucket=n["source_bucket"],
                Key=f"canary/ready-{next(tries):03d}",
                Body=b"ready",
                Tagging=f"{TAG_RUN}={self.ctx.run_id}",
            )
            m = sqs.receive_message(
                QueueUrl=made["queue_url"],
                MaxNumberOfMessages=10,
                WaitTimeSeconds=0 if self.ctx.dry_run else 20,
            )
            got = False
            for x in m.get("Messages", []):
                key = json.loads(x["Body"]).get("detail", {}).get("object", {}).get("key", "")
                got = got or key.startswith("canary/")
                sqs.delete_message(QueueUrl=made["queue_url"], ReceiptHandle=x["ReceiptHandle"])
            return got

        self.ctx.wait("the bucket's events to reach the queue", seen, timeout_s=900, every_s=10)

    def _objects(self, n: dict[str, Any]) -> None:
        """Small test objects in each storage class, with CRC64NVME, and keys that need care:
        spaces, '+', '%' and non-ASCII (the key-encoding checks)."""
        s3 = self.client("s3")
        keys = []
        tricky = ["plain", "with space", "plus+sign", "per%cent", "nörd-ünïcode"]
        for cls, count in n["objects"].items():
            for i in range(count):
                k = f"seed/{cls.lower()}/{tricky[i % len(tricky)]}-{i:02d}.segy"
                body = (f"OmniFile real-AWS test object {cls} {i}\n" * 64).encode()
                s3.put_object(
                    Bucket=n["source_bucket"],
                    Key=k,
                    Body=body,
                    StorageClass=cls,
                    ChecksumAlgorithm="CRC64NVME",
                    Tagging=f"{TAG_RUN}={self.ctx.run_id}",
                )
                keys.append({"key": k, "class": cls, "size": len(body)})
        self.ctx.state["made"]["objects"] = keys
        self.ctx.save_state()

    def _policy(self, rel: str, repl: dict[str, str]) -> str:
        text = (REPO / rel).read_text()
        for k, v in sorted(repl.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(k, v)
        return text

    def _iam(self, n: dict[str, Any]) -> None:
        """The batch and probe roles, and the probe's instance profile, are the owner's: made
        once by tests/real/aws/harness-roles.yaml, never by the harness, which may only read
        and pass them (tests/real/aws/harness-policy.json allows no IAM write on them). The run
        stops here if they are missing or have lost their boundary."""
        iam, made = self.client("iam"), self.ctx.state["made"]
        acct = self.ctx.state["identity"]["account"]
        boundary = f"arn:aws:iam::{acct}:policy{BASE_PATH}{BASE_BOUNDARY}"
        roles = made.setdefault("roles", {})
        for key, name in BASE_ROLES.items():
            try:
                r = iam.get_role(RoleName=name)["Role"]
            except iam.exceptions.NoSuchEntityException:
                raise StepFailed(
                    f"the role {name} is missing: the account owner deploys "
                    "tests/real/aws/harness-roles.yaml once first (tests/real/aws/README.md)"
                ) from None
            got = (r.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")
            if r.get("Path") != BASE_PATH or got != boundary:
                raise StepFailed(
                    f"the role {name} is not the one harness-roles.yaml makes (path "
                    f"{r.get('Path')}, boundary {got}, not {BASE_PATH} and {boundary}): "
                    "not using it"
                )
            roles[key] = r["Arn"]
        try:
            ip = iam.get_instance_profile(InstanceProfileName=BASE_PROFILE)["InstanceProfile"]
        except iam.exceptions.NoSuchEntityException:
            raise StepFailed(
                f"the instance profile {BASE_PROFILE} is missing: deploy "
                "tests/real/aws/harness-roles.yaml first (tests/real/aws/README.md)"
            ) from None
        if [x["Arn"] for x in ip.get("Roles", [])] != [roles["probe"]]:
            raise StepFailed(f"the instance profile {BASE_PROFILE} holds another role")
        made["instance_profile"] = ip["Arn"]
        # Roles Anywhere's service-linked role. The first trust anchor in an account makes it,
        # which needs iam:CreateServiceLinkedRole; the harness's user doesn't have that, and
        # the first real run stopped at CreateTrustAnchor with four buckets, a queue and six
        # endpoints already made. So the owner makes it once, and the run checks here first.
        if not iam.list_roles(PathPrefix=RA_SLR_PATH)["Roles"]:
            raise StepFailed(
                "Roles Anywhere's service-linked role is missing: the account owner runs "
                "`aws iam create-service-linked-role --aws-service-name "
                "rolesanywhere.amazonaws.com` once first (tests/real/aws/README.md)"
            )
        self.ctx.save_state()

    def _roles_anywhere(self, n: dict[str, Any]) -> None:
        """A trust anchor from a test CA shaped like OmniFile's edge CA (ECDSA P-384,
        self-signed), or the Azure run's real edge CA when --azure-run names one, the edge
        role from deploy/aws/iam/ with its placeholders filled, and the profile."""
        from .pki import edge_ca

        ctx, made = self.ctx, self.ctx.state["made"]
        ca_pem = edge_ca(ctx)
        ra = self.client("rolesanywhere")
        ta = ra.create_trust_anchor(
            name=n["trust_anchor"],
            enabled=True,
            tags=[{"key": k, "value": v} for k, v in ctx.tags().items()],
            source={
                "sourceType": "CERTIFICATE_BUNDLE",
                "sourceData": {"x509CertificateData": ca_pem},
            },
        )["trustAnchor"]
        made["trust_anchor_arn"] = ta["trustAnchorArn"]
        made["trust_anchor_id"] = ta["trustAnchorId"]
        iam = self.client("iam")
        acct = ctx.state["identity"]["account"]
        repl = {
            "S3_INTERFACE_ENDPOINT_ID": made["endpoints"]["s3"],
            "TRUST_ANCHOR_ID": ta["trustAnchorId"],
            "SOURCE_BUCKET": n["source_bucket"],
            "SOURCE_PREFIX": "seed/",
            "ACCOUNT_ID": acct,
            "REGION": self.region,
            "EDGE_ID": f"{n['prefix']}a",
        }
        r = iam.create_role(
            RoleName=n["roles"]["edge"],
            Path=n["iam_path"],
            AssumeRolePolicyDocument=self._policy("deploy/aws/iam/edge-role-trust.json", repl),
            # the only kind of role the harness's user may make: one that can read the runs'
            # objects and nothing else, whatever its own policy says (harness-roles.yaml)
            PermissionsBoundary=f"arn:aws:iam::{acct}:policy{BASE_PATH}{EDGE_BOUNDARY}",
            Tags=self.tag_list(),
            MaxSessionDuration=3600,
        )
        made["roles"]["edge"] = r["Role"]["Arn"]
        iam.put_role_policy(
            RoleName=n["roles"]["edge"],
            PolicyName="edge-s3-read",
            PolicyDocument=self._policy("deploy/aws/iam/edge-role-policy.json", repl),
        )
        prof = ra.create_profile(
            name=n["profile"],
            enabled=True,
            durationSeconds=3600,
            roleArns=[r["Role"]["Arn"]],
            tags=[{"key": k, "value": v} for k, v in ctx.tags().items()],
        )
        made["profile_arn"] = prof["profile"]["profileArn"]
        ctx.save_state()

    def _network(self, n: dict[str, Any]) -> None:
        ec2, made = self.client("ec2"), self.ctx.state["made"]

        def ts(kind: str) -> list[dict]:
            return [{"ResourceType": kind, "Tags": self.tag_list()}]

        vpc = ec2.create_vpc(CidrBlock=n["vpc_cidr"], TagSpecifications=ts("vpc"))["Vpc"]["VpcId"]
        made["vpc"] = vpc
        self.ctx.save_state()
        ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsSupport={"Value": True})
        ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsHostnames={"Value": True})
        az = ec2.describe_availability_zones()["AvailabilityZones"][0]["ZoneName"]
        sub = ec2.create_subnet(
            VpcId=vpc,
            CidrBlock=n["subnet_cidr"],
            AvailabilityZone=az,
            TagSpecifications=ts("subnet"),
        )["Subnet"]["SubnetId"]
        sg = ec2.create_security_group(
            GroupName=f"{n['probe']}-sg",
            VpcId=vpc,
            Description="OmniFile real-AWS probe: HTTPS inside the VPC",
            TagSpecifications=ts("security-group"),
        )["GroupId"]
        ec2.authorize_security_group_ingress(
            GroupId=sg,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [{"CidrIp": n["vpc_cidr"]}],
                }
            ],
        )
        made.update({"subnet": sub, "sg": sg})
        eps = made.setdefault("endpoints", {})
        for svc in n["endpoints"]:
            e = ec2.create_vpc_endpoint(
                VpcId=vpc,
                ServiceName=f"com.amazonaws.{self.region}.{svc}",
                VpcEndpointType="Interface",
                SubnetIds=[sub],
                SecurityGroupIds=[sg],
                PrivateDnsEnabled=svc not in ("s3",),
                TagSpecifications=ts("vpc-endpoint"),
            )
            eps[svc] = e["VpcEndpoint"]["VpcEndpointId"]
            dns = e["VpcEndpoint"].get("DnsEntries") or [{}]
            made.setdefault("endpoint_dns", {})[svc] = dns[0].get("DnsName", "")
        self.ctx.save_state()

    def _stage_helper(self, n: dict[str, Any]) -> None:
        """AWS's aws_signing_helper, at the version and SHA-256 the appliance pins
        (appliance/third-party/aws_signing_helper/PIN), put in the reports bucket for the probe
        to fetch through the S3 endpoint (it has no internet)."""
        import hashlib
        import urllib.request

        if self.ctx.dry_run:
            self.ctx.log(
                "dry run: the signing helper is not fetched; probe checks that need it "
                "answer partial"
            )
            return
        pin = dict(
            ln.split("=", 1)
            for ln in (REPO / "appliance/third-party/aws_signing_helper/PIN")
            .read_text()
            .splitlines()
            if "=" in ln and not ln.startswith("#")
        )
        url = pin["HELPER_URL"].replace("$HELPER_VERSION", pin["HELPER_VERSION"])
        data = urllib.request.urlopen(url, timeout=120).read()
        if hashlib.sha256(data).hexdigest() != pin["HELPER_SHA256"]:
            raise StepFailed("aws_signing_helper's SHA-256 is not the pinned one; not using it")
        self.client("s3").put_object(
            Bucket=n["reports_bucket"], Key="tools/aws_signing_helper", Body=data
        )

    def _probe(self, n: dict[str, Any]) -> None:
        ec2, made = self.client("ec2"), self.ctx.state["made"]
        self._stage_helper(n)
        ssm = self.client("ssm")
        ami = ssm.get_parameter(Name=PROBE_AMI_PARAM)["Parameter"]["Value"]
        s3dns = made.get("endpoint_dns", {}).get("s3", "").lstrip("*.")
        # The endpoint was made moments ago and may not answer yet when the instance boots
        # (the fourth real run's probe had no helper): try for ten minutes, and keep the
        # CLI's words where the probe's checks can report them.
        user_data = (
            "#!/bin/bash\n"
            "for i in $(seq 40); do\n"
            f"  aws s3 cp s3://{n['reports_bucket']}/tools/aws_signing_helper "
            f"/usr/local/bin/aws_signing_helper --region {self.region} "
            f"--endpoint-url https://bucket.{s3dns} >>/var/log/ofreal-helper.log 2>&1 "
            "&& chmod 755 /usr/local/bin/aws_signing_helper && break\n"
            "  sleep 15\n"
            "done\n"
        )
        r = ec2.run_instances(
            ImageId=ami,
            InstanceType=PROBE_TYPE[self.scope],
            MinCount=1,
            MaxCount=1,
            UserData=user_data,
            SubnetId=made["subnet"],
            SecurityGroupIds=[made["sg"]],
            IamInstanceProfile={"Arn": made["instance_profile"]},
            MetadataOptions={"HttpTokens": "required"},
            TagSpecifications=[
                {"ResourceType": "instance", "Tags": self.tag_list()},
                {"ResourceType": "volume", "Tags": self.tag_list()},
            ],
        )
        made["probe"] = r["Instances"][0]["InstanceId"]
        self.ctx.save_state()

    # ----------------------------------------------------------- exit, checks

    def exit(self) -> None:
        self.identity()
        self.ctx.log(
            "the AWS run has no exit test of its own (the M6 exit is Azure's); its "
            "lines are under checks"
        )

    def checks(self) -> dict[str, int]:
        with self.ctx.step("identity"):
            self.identity()
        from .checks.aws import AwsEnv

        try:
            return run_checks(self.ctx, AwsEnv(self.ctx, self))
        finally:
            if self.scope == "cheap":
                self._stop_probe()

    def _stop_probe(self) -> None:
        """The cheap pass terminates the probe as soon as its checks are done; a rerun of the
        probe's checks finds it gone and leaves them pending."""
        made = self.ctx.state.get("made", {})
        iid = made.get("probe")
        if not iid or made.get("probe_stopped"):
            return
        with self.ctx.step("checks.stop-probe"):
            self.client("ec2").terminate_instances(InstanceIds=[iid])
            made["probe_stopped"] = True
            self.ctx.save_state()
            self.ctx.log(f"cheap scope: probe {iid} terminated, its checks done")

    def _probe_ready(self) -> None:
        """Wait, once, for the probe's SSM agent to register: SendCommand refuses an instance
        SSM doesn't know yet (`InvalidInstanceId`), which a just-launched one is for a minute
        or two. The first full real run sent its first command seconds after RunInstances, and
        all six probe checks failed on that. A probe that never registers fails each of its
        checks at once after the first has waited."""
        made = self.ctx.state["made"]
        if made.get("probe_ready"):
            return
        if made.get("probe_unreachable"):
            raise StepFailed(made["probe_unreachable"])
        ssm = self.client("ssm")

        def online() -> bool:
            r = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [made["probe"]]}]
            )
            return any(i.get("PingStatus") == "Online" for i in r["InstanceInformationList"])

        try:
            self.ctx.wait("the probe to register with SSM", online, timeout_s=900, every_s=15)
        except StepFailed:
            made["probe_unreachable"] = (
                f"the probe {made['probe']} never registered with SSM in 15 minutes: its "
                "agent can't reach the ssm, ssmmessages and ec2messages endpoints, or its "
                "instance profile lacks the agent's rights"
            )
            self.ctx.save_state()
            raise StepFailed(made["probe_unreachable"]) from None
        made["probe_ready"] = True
        self.ctx.save_state()

    def probe(self, check: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run tests/real/vm/aws_probe.py CHECK on the EC2 probe (through SSM: the probe has
        no inbound access and no internet)."""
        made = self.ctx.state["made"]
        script = (REPO / "tests/real/vm/aws_probe.py").read_text()
        ssm = self.client("ssm")
        self._probe_ready()
        cfg = json.dumps(
            {
                "check": check,
                "args": args,
                "region": self.region,
                "endpoints": made.get("endpoints", {}),
                "names": self.names(),
                "made": {k: v for k, v in made.items() if k != "objects"},
            }
        )
        cmd = ssm.send_command(
            InstanceIds=[made["probe"]],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [
                    # the instance's user data fetches the signing helper: let it finish
                    "cloud-init status --wait >/dev/null 2>&1 || true",
                    "cat > /tmp/aws_probe.py <<'OFREAL_EOF'\n" + script + "\nOFREAL_EOF",
                    "python3 /tmp/aws_probe.py <<'OFREAL_CFG'\n" + cfg + "\nOFREAL_CFG",
                ]
            },
            TimeoutSeconds=1800,
        )["Command"]["CommandId"]

        def done():
            try:
                inv = ssm.get_command_invocation(CommandId=cmd, InstanceId=made["probe"])
            except ssm.exceptions.InvocationDoesNotExist:
                # SSM lists a command's invocation a moment after SendCommand answers: the
                # second real run asked at once and every probe check failed on this.
                return None
            return inv if inv["Status"] not in ("Pending", "InProgress", "Delayed") else None

        inv = self.ctx.wait(f"probe check {check}", done, timeout_s=1800, every_s=10)
        rows = [
            json.loads(x)
            for x in inv.get("StandardOutputContent", "").splitlines()
            if x.startswith("{")
        ]
        if inv["Status"] != "Success" or not rows:
            raise StepFailed(
                f"probe {check}: {inv['Status']} {inv.get('StandardErrorContent', '')[-300:]}"
            )
        return rows[-1]

    # ------------------------------------------------------------------ down

    def down(self) -> None:
        """Teardown by tag and by the run's names. Each part is isolated: one that fails is
        recorded, and every other part still runs; the proof then lists what is left."""
        ctx = self.ctx
        with ctx.step("identity"):
            self.identity()
        ctx.state.setdefault("prefix", prefix_for(ctx.run_id))
        n = self.names()
        done: list[str] = []
        errors: list[str] = []

        @contextmanager
        def part(name: str):
            try:
                with ctx.step(name):
                    yield
            except Exception as e:  # recorded; the rest of the teardown goes on
                errors.append(f"{name}: {type(e).__name__}: {e}")
                ctx.log(f"teardown part {name} failed, the rest goes on: {e}")

        with part("down.jobs"):
            s3c = self.client("s3control")
            acct = ctx.state["identity"]["account"]
            for j in s3c.list_jobs(
                AccountId=acct, JobStatuses=["Active", "New", "Preparing", "Ready", "Suspended"]
            ).get("Jobs", []):
                tags = s3c.get_job_tagging(AccountId=acct, JobId=j["JobId"]).get("Tags", [])
                if {"Key": TAG_RUN, "Value": ctx.run_id} in tags:
                    s3c.update_job_status(
                        AccountId=acct,
                        JobId=j["JobId"],
                        RequestedJobStatus="Cancelled",
                        StatusUpdateReason="omnifile real-cloud teardown",
                    )
                    done.append(f"cancelled job {j['JobId']}")
        ec2 = self.client("ec2")
        with part("down.probe"):
            res = ec2.describe_instances(
                Filters=[
                    {"Name": f"tag:{TAG_RUN}", "Values": [ctx.run_id]},
                    {
                        "Name": "instance-state-name",
                        "Values": ["pending", "running", "stopping", "stopped"],
                    },
                ]
            )
            ids = [i["InstanceId"] for r in res["Reservations"] for i in r["Instances"]]
            if ids:
                ec2.terminate_instances(InstanceIds=ids)
                ec2.get_waiter("instance_terminated").wait(InstanceIds=ids)
                done += [f"instance {i}" for i in ids]
        with part("down.endpoints"):
            done += self._endpoints_down()
        with part("down.network"):
            done += self._network_down()
        with part("down.events"):
            ev = self.client("events")
            try:
                tgt = ev.list_targets_by_rule(Rule=n["rule"]).get("Targets", [])
                if tgt:
                    ev.remove_targets(Rule=n["rule"], Ids=[t["Id"] for t in tgt])
                ev.delete_rule(Name=n["rule"])
                done.append(f"rule {n['rule']}")
            except ev.exceptions.ResourceNotFoundException:
                pass
        with part("down.queues"):
            sqs = self.client("sqs")
            for q in sqs.list_queues(QueueNamePrefix=n["queue"]).get("QueueUrls", []):
                if sqs.list_queue_tags(QueueUrl=q).get("Tags", {}).get(TAG_RUN) == ctx.run_id:
                    sqs.delete_queue(QueueUrl=q)
                    done.append(f"queue {q.rsplit('/', 1)[-1]}")
            # SQS lists a deleted queue for up to a minute. The first real run's proof
            # looked at once and reported the queue it had just deleted as left behind.
            if not ctx.dry_run:
                ctx.wait(
                    "the deleted queue to leave SQS's list",
                    lambda: not sqs.list_queues(QueueNamePrefix=n["queue"]).get("QueueUrls"),
                    timeout_s=180,
                    every_s=10,
                )
        with part("down.buckets"):
            done += self._buckets_down(errors)
        with part("down.roles-anywhere"):
            ra = self.client("rolesanywhere")
            for p in ra.list_profiles().get("profiles", []):
                if self._ra_tagged(ra, p["profileArn"]):
                    ra.delete_profile(profileId=p["profileId"])
                    done.append(f"profile {p['name']}")
            for c in ra.list_crls().get("crls", []):
                if self._ra_tagged(ra, c["crlArn"]):
                    ra.delete_crl(crlId=c["crlId"])
                    done.append(f"crl {c['name']}")
            for t in ra.list_trust_anchors().get("trustAnchors", []):
                if self._ra_tagged(ra, t["trustAnchorArn"]):
                    ra.delete_trust_anchor(trustAnchorId=t["trustAnchorId"])
                    done.append(f"trust anchor {t['name']}")
        with part("down.iam"):
            done += self._iam_down(n)
        left: list[str] = []
        with part("down.prove"):
            left = self._prove(n)
        report = {
            "runs": {ctx.run_id: {"done": done, "residue": left, "errors": errors, "warnings": []}}
        }
        (ctx.out / "teardown.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        if left or errors:
            raise StepFailed(f"teardown left resources or failed in part: {left + errors}")
        ctx.state["down"] = True
        ctx.save_state()
        ctx.log(f"teardown proved: nothing of {ctx.run_id} remains")

    def _endpoints_down(self) -> list[str]:
        """Interface endpoints first, and wait until they are gone: until then their network
        interfaces hold the subnet and the security group."""
        ec2, rid = self.client("ec2"), self.ctx.run_id
        flt = [{"Name": f"tag:{TAG_RUN}", "Values": [rid]}]

        def live() -> list[str]:
            return [
                e["VpcEndpointId"]
                for e in ec2.describe_vpc_endpoints(Filters=flt)["VpcEndpoints"]
                if e["State"].lower() not in ("deleted",)
            ]

        eps = live()
        if eps:
            ec2.delete_vpc_endpoints(VpcEndpointIds=eps)
            self.ctx.wait(
                "the interface endpoints to be deleted",
                lambda: not set(live()) & set(eps),
                timeout_s=900,
                every_s=15,
            )
        return [f"endpoint {e}" for e in eps]

    def _ra_tagged(self, ra, arn: str) -> bool:
        tags = ra.list_tags_for_resource(resourceArn=arn).get("tags", [])
        return {"key": TAG_RUN, "value": self.ctx.run_id} in tags

    def _network_down(self) -> list[str]:
        ec2, rid, done = self.client("ec2"), self.ctx.run_id, []
        flt = [{"Name": f"tag:{TAG_RUN}", "Values": [rid]}]
        for s in ec2.describe_subnets(Filters=flt)["Subnets"]:
            ec2.delete_subnet(SubnetId=s["SubnetId"])
            done.append(f"subnet {s['SubnetId']}")
        for g in ec2.describe_security_groups(Filters=flt)["SecurityGroups"]:
            ec2.delete_security_group(GroupId=g["GroupId"])
            done.append(f"security group {g['GroupId']}")
        for v in ec2.describe_vpcs(Filters=flt)["Vpcs"]:
            ec2.delete_vpc(VpcId=v["VpcId"])
            done.append(f"vpc {v['VpcId']}")
        return done

    def _buckets_down(self, errors: list[str]) -> list[str]:
        """Every version and delete marker, then the bucket: the run's by name
        (ofreal-<run id>-..., which only this run makes), or adopted and tagged. A bucket with
        no tags at all (NoSuchTagSet) is still the run's when its name says so. Object Lock
        isn't used here; a bucket with it is residue, never forced. Each bucket on its own."""
        s3, done = self.client("s3"), []
        base = f"ofreal-{self.ctx.run_id}-"
        adopted = {a["id"] for a in self.ctx.state.get("adopted", []) if a["kind"] == "bucket"}
        for b in s3.list_buckets().get("Buckets", []):
            name = b["Name"]
            if not name.startswith(base) and name not in adopted:
                continue
            try:
                done += self._bucket_down(s3, name, by_name=name.startswith(base))
            except Exception as e:
                errors.append(f"bucket {name}: {type(e).__name__}: {e}")
        return done

    def _bucket_down(self, s3, name: str, *, by_name: bool) -> list[str]:
        try:
            tags = {t["Key"]: t["Value"] for t in s3.get_bucket_tagging(Bucket=name)["TagSet"]}
        except s3.exceptions.ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchTagSet":
                raise
            tags = {}
        if tags.get(TAG_RUN) != self.ctx.run_id and not by_name:
            return []
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=name):
            objs = sorted(
                (
                    {"Key": o["Key"], "VersionId": o["VersionId"]}
                    for o in page.get("Versions", []) + page.get("DeleteMarkers", [])
                ),
                key=lambda o: o["Key"],
            )
            if objs:
                s3.delete_objects(Bucket=name, Delete={"Objects": objs, "Quiet": True})
        s3.delete_bucket(Bucket=name)
        return [f"bucket {name}"]

    @staticmethod
    def _run_roles(iam, n: dict[str, Any]) -> list[dict]:
        """The roles under the run's own path, checked here too (not only by PathPrefix)."""
        got = iam.list_roles(PathPrefix=n["iam_path"])["Roles"]
        return [r for r in got if r.get("Path", "").startswith(n["iam_path"])]

    def _iam_down(self, n: dict[str, Any]) -> list[str]:
        # Only the run's own roles, under its own path and tagged for it. The probe's instance
        # profile and the batch role are the owner's (harness-roles.yaml): never touched here.
        iam, done = self.client("iam"), []
        adopted = [a["id"] for a in self.ctx.state.get("adopted", []) if a["kind"] == "role"]
        names = sorted(r["RoleName"] for r in self._run_roles(iam, n))
        for name in names + [a for a in adopted if a not in names]:
            try:
                tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=name)["Tags"]}
            except iam.exceptions.NoSuchEntityException:
                continue
            if tags.get(TAG_RUN) != self.ctx.run_id or tags.get(TAG_HARNESS) != HARNESS_TAG_VALUE:
                continue
            for p in iam.list_role_policies(RoleName=name)["PolicyNames"]:
                iam.delete_role_policy(RoleName=name, PolicyName=p)
            for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
                iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
            iam.delete_role(RoleName=name)
            done.append(f"role {name}")
        return done

    def _prove(self, n: dict[str, Any]) -> list[str]:
        rid, left = self.ctx.run_id, []
        flt = [{"Name": f"tag:{TAG_RUN}", "Values": [rid]}]
        ec2 = self.client("ec2")
        for r in ec2.describe_instances(
            Filters=flt
            + [
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
                }
            ]
        )["Reservations"]:
            left += [f"instance {i['InstanceId']}" for i in r["Instances"]]
        left += [f"vpc {v['VpcId']}" for v in ec2.describe_vpcs(Filters=flt)["Vpcs"]]
        left += [
            f"endpoint {e['VpcEndpointId']}"
            for e in ec2.describe_vpc_endpoints(Filters=flt)["VpcEndpoints"]
            if e["State"].lower() not in ("deleted",)
        ]
        s3 = self.client("s3")
        left += [
            f"bucket {b['Name']}"
            for b in s3.list_buckets().get("Buckets", [])
            if b["Name"].startswith(f"ofreal-{rid}-")
        ]
        sqs = self.client("sqs")
        left += [
            f"queue {q}" for q in sqs.list_queues(QueueNamePrefix=n["queue"]).get("QueueUrls", [])
        ]
        iam = self.client("iam")
        left += [f"role {r['RoleName']}" for r in self._run_roles(iam, n)]
        all_roles = {
            r["RoleName"] for p in iam.get_paginator("list_roles").paginate() for r in p["Roles"]
        }
        buckets = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
        for a in self.ctx.state.get("adopted", []):
            if (a["kind"] == "role" and a["id"] in all_roles) or (
                a["kind"] == "bucket" and a["id"] in buckets
            ):
                left.append(f"adopted {a['kind']} {a['id']}")
        ra = self.client("rolesanywhere")
        left += [
            f"trust anchor {t['name']}"
            for t in ra.list_trust_anchors().get("trustAnchors", [])
            if self._ra_tagged(ra, t["trustAnchorArn"])
        ]
        tagging = self.client("resourcegroupstaggingapi")
        for r in tagging.get_resources(TagFilters=[{"Key": TAG_RUN, "Values": [rid]}])[
            "ResourceTagMappingList"
        ]:
            arn = r["ResourceARN"]
            # The tagging API keeps terminated instances and deleted endpoints for a while.
            if ":instance/" in arn or ":vpc-endpoint/" in arn or ":volume/" in arn:
                continue
            if ":job/" in arn and self._job_over(arn):
                continue
            left.append(f"tagged {arn}")
        return sorted(set(left))

    def _job_over(self, arn: str) -> bool:
        """A Batch Operations job that has ended or is being cancelled. AWS keeps an ended
        job's record, tags and all, and has no call that deletes one; it bills nothing more.
        The fourth real run's teardown reported its finished restore job as left behind."""
        j = self.client("s3control").describe_job(
            AccountId=self.ctx.state["identity"]["account"], JobId=arn.rsplit("/", 1)[1]
        )["Job"]
        return j["Status"] in ("Complete", "Failed", "Cancelled", "Cancelling")


#: List prices (USD, us-west-2, checked against the AWS pricing pages' published rates as of
#: 2026-11; an estimate, not a bill). Each line names what it prices.
PRICES = {
    "interface endpoint, per endpoint-AZ hour": 0.01,
    "t3.micro, per hour": 0.0104,
    "gp3 GB-month": 0.08,
    "S3 Batch Operations, per job": 0.25,
    "Glacier Flexible Retrieval restore requests, per 1,000 (Standard)": 0.05,
    "Glacier Flexible Retrieval restore requests, per 1,000 (Bulk)": 0.025,
    "S3 Standard GB-month": 0.023,
}
#: How long the run's resources live in a normal `all`: about one hour.
RUN_HOURS = 1.0
#: S3 Batch Operations jobs the checks create: RW-18's restore waves (two, or one in the
#: cheap pass) and RW-19's checksum job.
BATCH_JOBS = {"full": 3, "cheap": 2}


def estimate_cost(plan: dict[str, Any], ctx) -> dict[str, Any]:
    """What one `all` of this plan costs at list prices, item by item. Requests to SQS,
    EventBridge, KMS and S3 are pennies' fractions and left out; so is a failed teardown,
    which the expiry bounds."""
    r = plan["resources"]
    scope = plan.get("scope", "full")
    h = RUN_HOURS
    eps = len(r["endpoints"]) * PRICES["interface endpoint, per endpoint-AZ hour"] * h
    probe_type = PROBE_TYPE[scope]
    ec2 = PRICES[f"{probe_type}, per hour"] * h + 8 * PRICES["gp3 GB-month"] * h / 730
    tier = RESTORE_TIER[scope]
    restores = sum(r["objects"].values())
    restore = (
        restores / 1000 * PRICES[f"Glacier Flexible Retrieval restore requests, per 1,000 ({tier})"]
    )
    jobs = BATCH_JOBS[scope] * PRICES["S3 Batch Operations, per job"]
    big_gib = 0 if scope == "cheap" else 5.2
    storage = big_gib * PRICES["S3 Standard GB-month"] * h / 730
    # Deep Archive's and Glacier's minimum durations bill small objects for 180 and 90 days:
    # a few KB each, under a hundredth of a cent in all.
    items = {
        "interface endpoints": round(eps, 4),
        f"probe ({probe_type})": round(ec2, 4),
        "S3 Batch Operations jobs": round(jobs, 4),
        f"restores ({tier})": round(restore, 4),
        "large-object storage": round(storage, 4),
    }
    return {
        "total": round(sum(items.values()), 2),
        "items": items,
        "hours": h,
        "prices": PRICES,
        "currency": "USD",
    }
