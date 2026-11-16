"""tests/real/aws/harness-policy.json covers every AWS API the AWS run calls, beyond what the
first real run's IAM user already has (AmazonS3FullAccess, AmazonEC2FullAccess), and gives
that user no way to more than it needs: no role it makes can do more than the edge boundary
allows, no role of the owner's can be changed or passed anywhere but its one service, and no
tag the policy trusts can be put on someone else's resource. The routes the review of #56
found (a self-made admin role, a probe-named role passed to EC2, tags forged onto an
instance or a Roles Anywhere anchor, kms:Decrypt for any SSE-KMS object) are cases below,
decided by a small evaluator of the policy's own statements."""

from __future__ import annotations

import fnmatch
import json
import re

import pytest
from conftest import GOLDEN, REAL, harness

POLICY = REAL / "aws" / "harness-policy.json"
ROLES = REAL / "aws" / "harness-roles.yaml"
ACCT = "111122223333"
EDGE_BOUNDARY = f"arn:aws:iam::{ACCT}:policy/omnifile-real-base/omnifile-real-edge-boundary"
BASE_BOUNDARY = f"arn:aws:iam::{ACCT}:policy/omnifile-real-base/omnifile-real-base-boundary"
BASE = f"arn:aws:iam::{ACCT}:role/omnifile-real-base"
#: The user's managed policies, as far as the harness needs them: s3:* and ec2:* (the rest of
#: AmazonEC2FullAccess, ViewOnlyAccess and SecurityAudit are not counted on).
MANAGED = ("s3:*", "ec2:*")
#: Needs no permission at all.
FREE = {"sts:GetCallerIdentity"}
#: boto3 service names whose IAM prefix differs.
PREFIX = {"s3control": "s3", "resourcegroupstaggingapi": "tag"}
#: Actions a call needs besides its own: tags given at create, and the roles passed.
IMPLIED = {
    "iam:CreateRole": ["iam:TagRole"],
    "sqs:CreateQueue": ["sqs:TagQueue"],
    "events:PutRule": ["events:TagResource"],
    # Roles Anywhere checks rolesanywhere:TagResource too when a create carries tags (the
    # service reference's dependent actions): the review of #56 found the run would stop here.
    "rolesanywhere:CreateTrustAnchor": ["rolesanywhere:TagResource"],
    "rolesanywhere:CreateProfile": ["iam:PassRole", "rolesanywhere:TagResource"],
    "rolesanywhere:ImportCrl": ["rolesanywhere:TagResource"],
    "ec2:RunInstances": ["iam:PassRole", "ec2:CreateTags"],
    "s3:CreateJob": ["iam:PassRole", "s3:PutJobTagging"],
}
#: Every service the AWS run may call. A new one fails here first, so the policy keeps up.
KNOWN = {
    "ec2",
    "events",
    "iam",
    "rolesanywhere",
    "s3",
    "s3control",
    "sqs",
    "ssm",
    "sts",
    "resourcegroupstaggingapi",
}


def called(transcript: list[str]) -> set[str]:
    """IAM actions from the transcript's boto3 and aws CLI lines."""
    got = set()
    for ln in transcript:
        m = re.match(r"^boto3 ([a-z0-9-]+)\.([A-Za-z0-9]+)", ln)
        if m:
            svc, op = m.groups()
            assert svc in KNOWN, f"a new AWS service {svc}: cover it in {POLICY.name}"
            got.add(f"{PREFIX.get(svc, svc)}:{op}")
            continue
        m = re.match(r"^aws ([a-z0-9-]+) ([a-z0-9-]+)", ln)
        if m:
            svc, op = m.groups()
            svc = {"s3api": "s3"}.get(svc, svc)
            got.add(f"{svc}:{''.join(w.title() for w in op.split('-'))}")
    for a in list(got):
        got |= set(IMPLIED.get(a, []))
    return got


def listed(v) -> list:
    return v if isinstance(v, list) else [v]


def allowed(action: str, policy: dict) -> bool:
    """Action level: some Allow names it (conditions are the evaluator's business)."""
    if action in FREE or any(fnmatch.fnmatch(action, m) for m in MANAGED):
        return True
    return any(
        st["Effect"] == "Allow"
        and any(fnmatch.fnmatch(action.lower(), a.lower()) for a in listed(st["Action"]))
        for st in policy["Statement"]
    )


# ------------------------------------------------------------------ a small IAM evaluator


def _cond(op: str, want, got) -> bool:
    """One condition operator, with IAM's rule for a key the request lacks: a positive
    operator is false, a negated one true."""
    want = [str(w) for w in listed(want)]
    multi = op.startswith(("ForAnyValue:", "ForAllValues:"))
    base = op.split(":", 1)[1] if multi else op
    if base.endswith("IfExists"):
        if got is None:
            return True
        base = base[: -len("IfExists")]
    vals = [] if got is None else [str(g) for g in listed(got)]
    match = {
        "StringEquals": lambda v: v in want,
        "StringNotEquals": lambda v: v in want,
        "StringLike": lambda v: any(fnmatch.fnmatchcase(v, w) for w in want),
        "StringNotLike": lambda v: any(fnmatch.fnmatchcase(v, w) for w in want),
        "ArnLike": lambda v: any(fnmatch.fnmatchcase(v, w) for w in want),
        "ArnEquals": lambda v: v in want,
    }[base]
    negated = "Not" in base
    if op.startswith("ForAllValues:"):  # every value matches (true for none)
        return all((not match(v)) if negated else match(v) for v in vals)
    if multi:
        hit = any(match(v) for v in vals)
        return hit if not negated else not hit
    if not vals:
        return negated
    return (not match(vals[0])) if negated else match(vals[0])


def _applies(st: dict, action: str, resource: str, ctx: dict) -> bool:
    if not any(fnmatch.fnmatch(action.lower(), a.lower()) for a in listed(st["Action"])):
        return False
    if "NotResource" in st:
        if any(fnmatch.fnmatchcase(resource, r) for r in listed(st["NotResource"])):
            return False
    elif not any(fnmatch.fnmatchcase(resource, r) for r in listed(st["Resource"])):
        return False
    low = {k.lower(): v for k, v in ctx.items()}
    for op, conds in st.get("Condition", {}).items():
        for key, want in conds.items():
            if not _cond(op, want, low.get(key.lower())):
                return False
    return True


def decide(action: str, resource: str = "*", ctx: dict | None = None, managed=True) -> str:
    """allow, deny (an explicit Deny), or implicit (nothing allows it), for the user with the
    harness policy and, by default, its two managed policies (s3:*, ec2:* on *)."""
    stmts = json.loads(POLICY.read_text())["Statement"]
    if managed:
        stmts = stmts + [{"Effect": "Allow", "Action": list(MANAGED), "Resource": "*"}]
    hits = [st["Effect"] for st in stmts if _applies(st, action, resource, ctx or {})]
    return "deny" if "Deny" in hits else "allow" if "Allow" in hits else "implicit"


RUN_TAGS = {
    "aws:RequestTag/omnifile-harness": "tests-real",
    "aws:RequestTag/omnifile-run": "rr-1",
    "aws:TagKeys": ["omnifile-harness", "omnifile-run"],
}
ROLE = f"arn:aws:iam::{ACCT}:role/omnifile-real/rr-1/self-made"
INSTANCE = f"arn:aws:ec2:us-west-2:{ACCT}:instance/i-0123"
ANCHOR = f"arn:aws:rolesanywhere:us-west-2:{ACCT}:trust-anchor/owners"
ESCALATIONS = {
    # a self-made role: without the edge boundary, or with any other, it can't be made
    "role, no boundary": ("iam:CreateRole", ROLE, RUN_TAGS),
    "role, the base boundary": (
        "iam:CreateRole",
        ROLE,
        {**RUN_TAGS, "iam:PermissionsBoundary": BASE_BOUNDARY},
    ),
    "role, outside the path": (
        "iam:CreateRole",
        f"arn:aws:iam::{ACCT}:role/admin",
        {**RUN_TAGS, "iam:PermissionsBoundary": EDGE_BOUNDARY},
    ),
    "inline policy on an unbounded role": ("iam:PutRolePolicy", ROLE, {}),
    "inline policy on the owner's probe role": (
        "iam:PutRolePolicy",
        f"{BASE}/omnifile-real-probe",
        {"iam:PermissionsBoundary": BASE_BOUNDARY},
    ),
    "attach a managed policy": (
        "iam:AttachRolePolicy",
        ROLE,
        {
            "iam:PermissionsBoundary": EDGE_BOUNDARY,
            "iam:PolicyARN": "arn:aws:iam::aws:policy/AdministratorAccess",
        },
    ),
    "drop a role's boundary": (
        "iam:DeleteRolePermissionsBoundary",
        ROLE,
        {"iam:PermissionsBoundary": EDGE_BOUNDARY},
    ),
    "swap a role's boundary": (
        "iam:PutRolePermissionsBoundary",
        ROLE,
        {"iam:PermissionsBoundary": EDGE_BOUNDARY},
    ),
    "rewrite the edge boundary": ("iam:CreatePolicyVersion", EDGE_BOUNDARY, {}),
    "trust a self-made role to oneself": (
        "iam:UpdateAssumeRolePolicy",
        ROLE,
        {"iam:PermissionsBoundary": EDGE_BOUNDARY},
    ),
    "a user, an access key": ("iam:CreateAccessKey", f"arn:aws:iam::{ACCT}:user/someone", {}),
    # passing roles: only the owner's two, each to its one service
    "a probe-named role to EC2": (
        "iam:PassRole",
        f"arn:aws:iam::{ACCT}:role/omnifile-real/rr-1/ofreal-rr-1-probe",
        {"iam:PassedToService": "ec2.amazonaws.com"},
    ),
    "the probe role to Lambda": (
        "iam:PassRole",
        f"{BASE}/omnifile-real-probe",
        {"iam:PassedToService": "lambda.amazonaws.com"},
    ),
    "an edge role to EC2": ("iam:PassRole", ROLE, {"iam:PassedToService": "ec2.amazonaws.com"}),
    # tags forged onto the owner's resources
    "tag an existing instance": (
        "ec2:CreateTags",
        INSTANCE,
        {"aws:TagKeys": ["omnifile-harness"], "aws:RequestTag/omnifile-harness": "tests-real"},
    ),
    "tag an existing instance with the run": (
        "ec2:CreateTags",
        INSTANCE,
        {"aws:TagKeys": ["omnifile-run"], "aws:RequestTag/omnifile-run": "rr-1"},
    ),
    "untag an instance": ("ec2:DeleteTags", INSTANCE, {"aws:TagKeys": ["omnifile-harness"]}),
    "tag with keys beyond the run's": (
        "rolesanywhere:TagResource",
        ANCHOR,
        {**RUN_TAGS, "aws:TagKeys": ["omnifile-harness", "omnifile-run", "owner-team"]},
    ),
    "tag without the run": (
        "rolesanywhere:TagResource",
        ANCHOR,
        {"aws:RequestTag/omnifile-harness": "tests-real", "aws:TagKeys": ["omnifile-harness"]},
    ),
    "tag through the tagging API": ("tag:TagResources", "*", RUN_TAGS),
    "tag another queue": (
        "sqs:TagQueue",
        f"arn:aws:sqs:us-west-2:{ACCT}:owners-queue",
        RUN_TAGS,
    ),
    "tag another rule": ("events:TagResource", f"arn:aws:events:us-west-2:{ACCT}:rule/x", RUN_TAGS),
    "tag another role": ("iam:TagRole", f"arn:aws:iam::{ACCT}:role/admin", RUN_TAGS),
    # what tags would have opened, without the run's own tag
    "a command on an instance tagged harness only": (
        "ssm:SendCommand",
        INSTANCE,
        {"ssm:resourceTag/omnifile-harness": "tests-real"},
    ),
    "the owner's anchor's CRL": ("rolesanywhere:UpdateCrl", ANCHOR, {}),
    "decrypt the owner's SSE-KMS object": (
        "kms:Decrypt",
        "arn:aws:kms:us-west-2:111122223333:key/k",
        {
            "kms:ViaService": "s3.us-west-2.amazonaws.com",
            "kms:EncryptionContext:aws:s3:arn": "arn:aws:s3:::owners-bucket/payroll.csv",
        },
    ),
    "decrypt outside S3": (
        "kms:Decrypt",
        "arn:aws:kms:us-west-2:111122223333:key/k",
        {"kms:EncryptionContext:aws:s3:arn": "arn:aws:s3:::ofreal-rr-1-kms/x"},
    ),
}
NEEDED = {
    "the edge role": (
        "iam:CreateRole",
        ROLE,
        {**RUN_TAGS, "iam:PermissionsBoundary": EDGE_BOUNDARY},
    ),
    "its policy": ("iam:PutRolePolicy", ROLE, {"iam:PermissionsBoundary": EDGE_BOUNDARY}),
    "its removal": ("iam:DeleteRole", ROLE, {"iam:PermissionsBoundary": EDGE_BOUNDARY}),
    "the probe to EC2": (
        "iam:PassRole",
        f"{BASE}/omnifile-real-probe",
        {"iam:PassedToService": "ec2.amazonaws.com"},
    ),
    "the batch role to Batch Operations": (
        "iam:PassRole",
        f"{BASE}/omnifile-real-batch-ops",
        {"iam:PassedToService": "batchoperations.s3.amazonaws.com"},
    ),
    # S3 Control's CreateJob was denied under a PassedToService condition in both forms
    # (StringEquals, then StringEqualsIfExists: two real runs), so the statement names the
    # one role and no service; the role's trust policy keeps it to S3 Batch Operations
    "the batch role when the service isn't named": (
        "iam:PassRole",
        f"{BASE}/omnifile-real-batch-ops",
        {},
    ),
    "a CRL with the run's tags and its anchor's": (
        "rolesanywhere:ImportCrl",
        "*",
        {
            "aws:RequestTag/omnifile-harness": "tests-real",
            "aws:RequestTag/omnifile-run": "rr-1",
            "aws:RequestTag/omnifile-expires": "2026-11-03T03:00:00Z",
            "aws:RequestTag/omnifile-trust-anchor": "0000-anchor",
            "aws:TagKeys": [
                "omnifile-trust-anchor",
                "omnifile-run",
                "omnifile-expires",
                "omnifile-harness",
            ],
        },
    ),
    "the probe's SSM registration": ("ssm:DescribeInstanceInformation", "*", {}),
    "tags on the probe at launch": (
        "ec2:CreateTags",
        INSTANCE,
        {**RUN_TAGS, "ec2:CreateAction": "RunInstances"},
    ),
    "a command on the run's probe": (
        "ssm:SendCommand",
        INSTANCE,
        {"ssm:resourceTag/omnifile-harness": "tests-real", "ssm:resourceTag/omnifile-run": "rr-1"},
    ),
    "the run's CRL": (
        "rolesanywhere:UpdateCrl",
        ANCHOR,
        {"aws:ResourceTag/omnifile-harness": "tests-real", "aws:ResourceTag/omnifile-run": "rr-1"},
    ),
    "a trust anchor, tagged at create": ("rolesanywhere:CreateTrustAnchor", "*", RUN_TAGS),
    "decrypt the run's SSE-KMS object": (
        "kms:Decrypt",
        "arn:aws:kms:us-west-2:111122223333:key/k",
        {
            "kms:ViaService": "s3.us-west-2.amazonaws.com",
            "kms:EncryptionContext:aws:s3:arn": "arn:aws:s3:::ofreal-rr-1-kms/obj",
        },
    ),
    "the run's queue, tagged at create": (
        "sqs:TagQueue",
        f"arn:aws:sqs:us-west-2:{ACCT}:ofreal-rr-1-events",
        RUN_TAGS,
    ),
}


@pytest.mark.parametrize("name", list(ESCALATIONS))
def test_no_route_to_more_than_the_harness_needs(name):
    action, resource, ctx = ESCALATIONS[name]
    assert decide(action, resource, ctx) != "allow", name


@pytest.mark.parametrize("name", list(NEEDED))
def test_what_the_run_needs_is_still_allowed(name):
    action, resource, ctx = NEEDED[name]
    assert decide(action, resource, ctx) == "allow", name


def test_every_api_the_aws_run_calls_is_covered(tmp_path):
    policy = json.loads(POLICY.read_text())
    golden = (GOLDEN / "aws.transcript").read_text().splitlines()
    cheap = harness("all", "aws", tmp_path / "out", "--scope", "cheap")
    assert cheap.code == 0, cheap.stdout[-1500:]
    need = called(golden) | called(cheap.transcript)
    assert len(need) > 50  # the transcript was read
    missing = sorted(a for a in need if not allowed(a, policy))
    assert not missing, missing


#: Each create the run makes with tags, and the tag action it also needs, with the request
#: the harness sends (its three tags): the evaluator's verdict, Denies included. Action-level
#: coverage alone missed that rolesanywhere:TagResource was denied at create.
CREATE_TAGS = {
    "trust anchor": ("rolesanywhere:CreateTrustAnchor", "rolesanywhere:TagResource", "*"),
    "profile": ("rolesanywhere:CreateProfile", "rolesanywhere:TagResource", "*"),
    "CRL": ("rolesanywhere:ImportCrl", "rolesanywhere:TagResource", "*"),
    "edge role": ("iam:CreateRole", "iam:TagRole", ROLE),
    "queue": (
        "sqs:CreateQueue",
        "sqs:TagQueue",
        f"arn:aws:sqs:us-west-2:{ACCT}:ofreal-rr-1-events",
    ),
    "rule": (
        "events:PutRule",
        "events:TagResource",
        f"arn:aws:events:us-west-2:{ACCT}:rule/ofreal-rr-1-s3",
    ),
}
HARNESS_TAGS = {
    "aws:RequestTag/omnifile-harness": "tests-real",
    "aws:RequestTag/omnifile-run": "rr-1",
    "aws:RequestTag/omnifile-expires": "2026-11-03T03:00:00Z",
    "aws:TagKeys": ["omnifile-run", "omnifile-expires", "omnifile-harness"],
    "iam:PermissionsBoundary": EDGE_BOUNDARY,
}


@pytest.mark.parametrize("name", list(CREATE_TAGS))
def test_a_create_and_the_tags_it_carries_are_both_allowed(name):
    create, tag, resource = CREATE_TAGS[name]
    assert tag in IMPLIED[create]
    assert decide(create, resource, HARNESS_TAGS) == "allow", create
    assert decide(tag, resource, HARNESS_TAGS) == "allow", tag


def test_tagging_an_existing_foreign_anchor_is_the_one_known_tradeoff():
    """IAM can't tell Roles Anywhere's create-time TagResource from one on a resource that
    already exists, so the run's tags can be put on an owner's anchor (README). What they
    open there is deletion and its CRLs, never its roles: no update of an anchor or profile,
    no profile with the owner's roles, no notification change."""
    assert decide("rolesanywhere:TagResource", ANCHOR, HARNESS_TAGS) == "allow"
    forged = {
        "aws:ResourceTag/omnifile-harness": "tests-real",
        "aws:ResourceTag/omnifile-run": "rr-1",
    }
    for a in (
        "rolesanywhere:UpdateTrustAnchor",
        "rolesanywhere:UpdateProfile",
        "rolesanywhere:PutNotificationSettings",
        "rolesanywhere:EnableTrustAnchor",
        "rolesanywhere:DisableTrustAnchor",
    ):
        assert decide(a, ANCHOR, forged) != "allow", a
    owners_admin = f"arn:aws:iam::{ACCT}:role/admin"
    ctx = {"iam:PassedToService": "rolesanywhere.amazonaws.com"}
    assert decide("iam:PassRole", owners_admin, ctx) != "allow"
    # what the same tags do open: say so plainly in the README
    assert decide("rolesanywhere:DeleteTrustAnchor", ANCHOR, forged) == "allow"
    readme = (REAL / "aws" / "README.md").read_text()
    assert "list-trust-anchors" in readme and "simulate-custom-policy" in readme


def test_the_policy_is_scoped():
    policy = json.loads(POLICY.read_text())
    mutating = re.compile(
        r":(Create|Delete|Put|Tag|Untag|Attach|Detach|Update|Import|Send|Pass|Set|Remove|Add)"
    )
    for st in policy["Statement"]:
        acts = listed(st["Action"])
        if st["Effect"] == "Deny":
            continue
        assert not any(a == "*" or a.endswith(":*") for a in acts), st["Sid"]
        if any(mutating.search(a) for a in acts):
            scoped = all(r != "*" for r in listed(st["Resource"])) or "Condition" in st
            assert scoped, f"{st['Sid']}: a mutating statement on * with no condition"


def _allows(policy, pattern: str) -> list[dict]:
    return [
        st
        for st in policy["Statement"]
        if st["Effect"] == "Allow"
        and any(fnmatch.fnmatch(a.lower(), pattern.lower()) for a in listed(st["Action"]))
    ]


def test_no_iam_write_without_the_edge_boundary():
    policy = json.loads(POLICY.read_text())
    for pat in ("iam:Create*", "iam:Put*", "iam:Attach*", "iam:Delete*", "iam:Update*"):
        for st in _allows(policy, pat):
            got = st.get("Condition", {}).get("StringEquals", {}).get("iam:PermissionsBoundary")
            assert got == EDGE_BOUNDARY, f"{st['Sid']} allows {pat} without the edge boundary"
    assert not _allows(policy, "iam:Attach*") and not _allows(policy, "iam:Update*")
    denied = {
        a
        for st in policy["Statement"]
        if st["Effect"] == "Deny" and "Condition" not in st
        for a in listed(st["Action"])
    }
    assert {"iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary"} <= denied
    for st in _allows(policy, "iam:PassRole"):
        # The batch role's statement names no service: S3 Control's CreateJob was denied
        # under a PassedToService condition in both forms on real AWS, and AWS's own
        # examples carry none. Its trust policy keeps it to S3 Batch Operations
        # (test_the_owners_roles_are_bounded_and_fixed).
        svc = st.get("Condition", {}).get("StringEquals", {}).get("iam:PassedToService")
        if svc != "rolesanywhere.amazonaws.com":  # edge roles, each under the edge boundary
            assert "*" not in st["Resource"] and st["Resource"].startswith(BASE), st["Sid"]
        if svc is None:
            assert st["Resource"] == f"{BASE}/omnifile-real-batch-ops", st["Sid"]


def test_tags_are_denied_except_at_create():
    policy = json.loads(POLICY.read_text())
    denies = [st for st in policy["Statement"] if st["Effect"] == "Deny"]

    def denied(action: str) -> list[dict]:
        return [st for st in denies if action in listed(st["Action"])]

    ec2 = denied("ec2:CreateTags")
    assert ec2 and "RunInstances" in json.dumps(ec2[0]["Condition"]["StringNotEquals"])
    for a in ("rolesanywhere:UntagResource", "ec2:DeleteTags", "tag:TagResources"):
        assert denied(a), a
        assert all("omnifile-*" in json.dumps(st["Condition"]) for st in denied(a))
    # Roles Anywhere tags: only the run's three keys and the CRL's anchor tag (what the
    # service's own CRL policy keys on), with the run's values (at create)
    for st in _allows(policy, "rolesanywhere:TagResource"):
        c = st["Condition"]
        assert c["StringEquals"]["aws:RequestTag/omnifile-harness"] == "tests-real"
        assert c["StringLike"]["aws:RequestTag/omnifile-run"] == "rr-*"
        assert set(c["ForAllValues:StringEquals"]["aws:TagKeys"]) == {
            "omnifile-harness",
            "omnifile-run",
            "omnifile-expires",
            "omnifile-trust-anchor",
        }
    for st in _allows(policy, "ssm:SendCommand"):
        if any(":instance/" in r for r in listed(st["Resource"])):
            c = json.dumps(st["Condition"])
            assert "ssm:resourceTag/omnifile-run" in c and "ssm:resourceTag/omnifile-harness" in c
    for st in _allows(policy, "kms:Decrypt"):
        like = st["Condition"]["StringLike"]["kms:EncryptionContext:aws:s3:arn"]
        assert like == "arn:aws:s3:::ofreal-rr-*"


# --------------------------------------------- the owner's template: harness-roles.yaml


def _roles_template() -> dict:
    from fakes import owner_stack

    return owner_stack.load(ROLES)["Resources"]


def _actions(doc: dict) -> set[str]:
    return {a for st in doc["Statement"] if st["Effect"] == "Allow" for a in listed(st["Action"])}


def test_the_owners_roles_are_bounded_and_fixed():
    from harness import aws as harness_aws

    res = _roles_template()
    roles = {
        r["Properties"]["RoleName"]: r["Properties"]
        for r in res.values()
        if r["Type"] == "AWS::IAM::Role"
    }
    assert set(roles) == set(harness_aws.BASE_ROLES.values())
    boundaries = {
        lid: r["Properties"] for lid, r in res.items() if r["Type"] == "AWS::IAM::ManagedPolicy"
    }
    names = {p["ManagedPolicyName"] for p in boundaries.values()}
    assert names == {harness_aws.BASE_BOUNDARY, harness_aws.EDGE_BOUNDARY}
    base = next(
        p for p in boundaries.values() if p["ManagedPolicyName"] == harness_aws.BASE_BOUNDARY
    )
    for name, p in roles.items():
        assert p["Path"] == harness_aws.BASE_PATH
        assert p["PermissionsBoundary"] == {"Ref": "BaseBoundary"}, name
        trust = p["AssumeRolePolicyDocument"]["Statement"]
        assert all(set(st["Principal"]) == {"Service"} for st in trust), name  # no person
        assert not p.get("ManagedPolicyArns"), name
        acts = set().union(*(_actions(pol["PolicyDocument"]) for pol in p["Policies"]))
        # what a role's own policies say stays inside its boundary
        assert acts <= _actions(base["PolicyDocument"]), (
            name,
            acts - _actions(base["PolicyDocument"]),
        )
        assert {a for a in acts if a.startswith("iam:")} <= {"iam:PassRole"}, name
    edge = next(
        p for p in boundaries.values() if p["ManagedPolicyName"] == harness_aws.EDGE_BOUNDARY
    )
    (st,) = edge["PolicyDocument"]["Statement"]
    assert set(st["Action"]) == {"s3:GetObject", "s3:GetObjectVersion"}
    assert st["Resource"] == "arn:aws:s3:::ofreal-rr-*/*"
    # The batch role is S3 Batch Operations' alone, in this account: the user's PassRole of
    # it names no service (see harness-policy.json), so this trust policy is what holds.
    trust = roles["omnifile-real-batch-ops"]["AssumeRolePolicyDocument"]["Statement"]
    assert [s["Principal"] for s in trust] == [{"Service": "batchoperations.s3.amazonaws.com"}]
    assert all("aws:SourceAccount" in json.dumps(s.get("Condition", {})) for s in trust)
    # the probe passes the batch role and no other, with no service condition (S3 refuses
    # CreateJob under one); the trust policy above keeps the role to Batch Operations
    probe = roles["omnifile-real-probe"]
    passes = [
        s
        for pol in probe["Policies"]
        for s in pol["PolicyDocument"]["Statement"]
        if "iam:PassRole" in listed(s["Action"])
    ]
    assert passes and all(
        "Condition" not in s and s["Resource"] == {"Fn::GetAtt": ["BatchRole", "Arn"]}
        for s in passes
    )


def test_the_run_uses_the_owners_roles_and_never_changes_them(tmp_path):
    r = harness("all", "aws", tmp_path / "out", "--scope", "cheap")
    assert r.code == 0, r.stdout[-1500:]
    writes = re.compile(
        r"^boto3 iam\.(Create|Put|Attach|Detach|Delete|Update|Tag|Untag|Add|Remove)"
    )
    owners = {"omnifile-real-probe", "omnifile-real-batch-ops"}

    def owned(ln: str) -> bool:
        """A write to the owner's role, instance profile or boundary (not merely naming one)."""
        a = json.loads(ln.split(" ", 2)[2]) if ln.count(" ") >= 2 else {}
        names = {a.get("RoleName"), a.get("InstanceProfileName")}
        return bool(names & owners) or "policy/omnifile-real-base/" in a.get("PolicyArn", "")

    bad = [ln for ln in r.transcript if writes.match(ln) and owned(ln)]
    assert not bad, bad
    creates = [ln for ln in r.transcript if ln.startswith("boto3 iam.CreateRole")]
    assert creates and all(
        '"PermissionsBoundary": ' in ln and "omnifile-real-edge-boundary" in ln for ln in creates
    )
    run = next(ln for ln in r.transcript if ln.startswith("boto3 ec2.RunInstances"))
    assert "instance-profile/omnifile-real-base/omnifile-real-probe" in run
    job = next(ln for ln in r.transcript if ln.startswith("boto3 s3control.CreateJob"))
    assert "role/omnifile-real-base/omnifile-real-batch-ops" in job


def test_a_missing_owners_role_stops_the_run_before_anything_is_made(tmp_path):
    broken = tmp_path / "roles.yaml"
    text = ROLES.read_text().replace(
        "RoleName: omnifile-real-probe", "RoleName: omnifile-real-probe-x"
    )
    broken.write_text(text)
    r = harness(
        "all",
        "aws",
        tmp_path / "out",
        "--scope",
        "cheap",
        env={"REAL_FAKE_OWNER_STACK": str(broken)},
    )
    assert r.code != 0
    assert "harness-roles.yaml" in r.stdout
    assert not [
        ln
        for ln in r.transcript
        if ln.startswith(("boto3 s3.CreateBucket", "boto3 ec2.Create", "boto3 ec2.RunInstances"))
    ]


def test_no_roles_anywhere_service_role_stops_the_run_before_anything_is_made(tmp_path):
    """The first real run: CreateTrustAnchor was refused for iam:CreateServiceLinkedRole, with
    four buckets, a queue and six endpoints already made. The owner makes that role once, and
    the run looks for it before it makes anything."""
    r = harness(
        "all", "aws", tmp_path / "out", "--scope", "cheap", env={"REAL_FAKE_NO_RA_SLR": "1"}
    )
    assert r.code != 0
    assert "create-service-linked-role --aws-service-name rolesanywhere.amazonaws.com" in r.stdout
    assert not [
        ln
        for ln in r.transcript
        if ln.startswith(("boto3 s3.CreateBucket", "boto3 ec2.Create", "boto3 sqs.CreateQueue"))
    ]
    assert "create-service-linked-role" in (REAL / "aws" / "README.md").read_text()


def test_the_cheap_scope_leaves_out_the_large_object_check(tmp_path):
    r = harness("all", "aws", tmp_path / "out", "--scope", "cheap")
    assert r.code == 0, r.stdout[-1500:]
    rw02 = r.results["RW-02"]
    assert rw02["status"] == "pending" and "cheap scope" in rw02["detail"]
    assert not [ln for ln in r.transcript if "rw02-big" in ln]
    assert r.state["scope"] == "cheap"
    # the scope is part of what the owner approves: another scope is another plan
    p = harness("plan", "aws", tmp_path / "out2", "--scope", "full")
    q = harness("plan", "aws", tmp_path / "out3", "--scope", "cheap")
    assert p.state["digest"] != q.state["digest"]
    r = harness("plan", "aws", tmp_path / "out3", "--run-id", "rr-dry-0001", "--scope", "full")
    assert r.code == 2 and "was planned with --scope cheap" in r.stdout


def test_the_cheap_pass_is_as_cheap_as_the_checks_allow(tmp_path):
    from harness.aws import CHEAP_ENDPOINTS

    r = harness("all", "aws", tmp_path / "out", "--scope", "cheap")
    assert r.code == 0, r.stdout[-1500:]
    t = r.transcript
    # the fewest interface endpoints its checks go through
    eps = [ln for ln in t if ln.startswith("boto3 ec2.CreateVpcEndpoint")]
    assert len(eps) == len(CHEAP_ENDPOINTS) == 6
    assert not [ln for ln in eps if ".sts" in ln or ".s3-control" in ln]
    # the smallest probe a free-plan account may start (it refuses t3.nano), terminated once
    # its checks ran (before the teardown)
    run = next(ln for ln in t if ln.startswith("boto3 ec2.RunInstances"))
    assert '"InstanceType": "t3.micro"' in run
    stop = next(i for i, ln in enumerate(t) if ln.startswith("boto3 ec2.TerminateInstances"))
    collect = next(i for i, ln in enumerate(t) if "s3.DeleteBucket" in ln)
    assert stop < collect and r.state["made"]["probe_stopped"] is True
    # Flexible Retrieval at Bulk only
    restores = [ln for ln in t if "RestoreObject" in ln or "S3InitiateRestoreObject" in ln]
    assert restores and all("Standard" not in ln and "STANDARD" not in ln for ln in restores)
    assert any('"Tier": "Bulk"' in ln for ln in restores)
    assert any('"GlacierJobTier": "BULK"' in ln for ln in t)
    # two Batch Operations jobs ($0.25 each), not three: one restore wave and the checksum job
    assert len([ln for ln in t if ln.startswith("boto3 s3control.CreateJob")]) == 2
    # the shortest expiry that fits the run, unless --ttl-hours says otherwise
    assert r.state["expires"] == "2026-11-03T03:00:00Z"
    p = harness("plan", "aws", tmp_path / "o2", "--scope", "cheap", "--ttl-hours", "12")
    assert p.state["expires"] == "2026-11-03T12:00:00Z"


def test_the_plan_prints_an_estimated_cost(tmp_path):
    cheap = harness("plan", "aws", tmp_path / "c", "--scope", "cheap")
    assert harness("plan", "aws", tmp_path / "f", "--scope", "full").code == 0
    assert "estimated cost of this run: about $" in cheap.stdout
    c = json.loads((tmp_path / "c" / "evidence" / "plan.json").read_text())["estimated_cost_usd"]
    f = json.loads((tmp_path / "f" / "evidence" / "plan.json").read_text())["estimated_cost_usd"]
    assert c["currency"] == "USD" and set(c["items"]) and c["total"] < f["total"] < 2.0
    assert c["items"]["interface endpoints"] == 0.06 and c["items"]["probe (t3.micro)"] < 0.02


def test_parts_are_small_in_the_cheap_pass():
    from datetime import datetime

    from harness.checks.aws import AwsEnv
    from harness.core import Context

    ctx = Context(
        phase="checks",
        cloud="aws",
        dry_run=False,
        out=REAL,
        run_id="rr-dry-0001",
        now=datetime(2026, 11, 3),
    )

    class A:
        scope = "cheap"

    assert AwsEnv(ctx, A()).mib == 5 and AwsEnv(ctx, A()).tier == "Bulk"
    A.scope = "full"
    assert AwsEnv(ctx, A()).mib == 100 and AwsEnv(ctx, A()).tier == "Standard"


def test_the_policy_is_attached_in_parts_that_fit_a_managed_policy():
    """The first real run: `put-user-policy` refused the policy (2,048 characters of inline
    policy on a user; this one is about 8,500). It is attached as managed policies of at most
    6,144 characters each, which hold its statements in its order and nothing else."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("split_policy", REAL / "aws" / "split_policy.py")
    sp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sp)
    whole = json.loads(POLICY.read_text())
    files = sorted((REAL / "aws").glob("harness-policy-part*.json"))
    docs = [json.loads(f.read_text()) for f in files]
    assert len(docs) >= 2, "one part would be the inline policy that IAM refused"
    assert [st for d in docs for st in d["Statement"]] == whole["Statement"]
    for f, d in zip(files, docs, strict=True):
        assert d["Version"] == whole["Version"]
        assert sp.size(d) <= sp.LIMIT, f"{f.name} is over a managed policy's size"
    assert sp.main(["--check"]) == 0, "the parts are stale: run tests/real/aws/split_policy.py"
    readme = (REAL / "aws" / "README.md").read_text()
    assert "put-user-policy --user-name" not in readme, "the README still attaches it inline"
    for n in range(1, len(docs) + 1):
        assert f"harness-policy-part{n}.json" in readme or "harness-policy-part$n.json" in readme
