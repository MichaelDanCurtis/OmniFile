"""Every resource a run makes carries the run id and an expiry."""

from __future__ import annotations

import json

from conftest import harness

RUN, EXPIRES = "rr-dry-0001", "2026-11-03T12:00:00Z"
TAGS = f"omnifile-run={RUN} omnifile-expires={EXPIRES} omnifile-harness=tests-real"

AZ_CREATES = ("az group create", "az acr create", "az network nsg create", "az vm create")


def test_every_azure_create_is_tagged_and_the_deployment_tags_everything(out):
    r = harness("plan", "azure", out)
    assert r.code == 0
    r = harness("up", "azure", out, "--run-id", RUN, "--approve", "auto")
    assert r.code == 0, r.stdout[-1500:]
    # The harness's own creates. The guide's `az group create --name` (step 2 of the fixture,
    # step 10 of the guide) is the guide's: teardown finds that group by name too
    # (test_teardown.py).
    creates = [
        c
        for c in r.transcript
        if c.startswith(AZ_CREATES) and not c.startswith("az group create --name")
    ]
    assert {c.split(" -")[0] for c in creates} == set(AZ_CREATES)
    for c in creates:
        assert TAGS in c, c
    # What the guide's deploy made (main.bicep's `tags`, from test.bicepparam's env).
    s = r.fake_state()
    assert s["resources"]
    for res in s["resources"]:
        assert res["tags"].get("omnifile-run") == RUN, res["id"]
        # the guide's deployment takes the expiry in the guide's form (minutes, no seconds)
        assert res["tags"].get("omnifile-expires") in (EXPIRES, "2026-11-03T12:00Z"), res["id"]
    # Entra objects take no Azure tags: the apps the guide made are tagged in Graph, and the
    # PG admin group carries the run in its description and its name's prefix.
    assert all(f"omnifile-run:{RUN}" in a["tags"] for a in s["apps"])
    harness("down", "azure", out, "--run-id", RUN)


def _tags_of(call: dict) -> dict[str, str]:
    for k in ("Tags", "tags"):
        if k in call:
            v = call[k]
            if isinstance(v, dict):
                return v
            return {(t.get("Key") or t.get("key")): (t.get("Value") or t.get("value")) for t in v}
    for spec in call.get("TagSpecifications", []):
        return {t["Key"]: t["Value"] for t in spec["Tags"]}
    if "Tagging" in call:
        if isinstance(call["Tagging"], str):
            return dict(kv.split("=") for kv in call["Tagging"].split("&"))
        return {t["Key"]: t["Value"] for t in call["Tagging"]["TagSet"]}
    return {}


def test_every_aws_create_is_tagged(out):
    r = harness("all", "aws", out)
    assert r.code == 0
    calls = [
        (ln.split()[1], json.loads(ln.split(" ", 2)[2]))
        for ln in r.transcript
        if ln.startswith("boto3 ")
    ]
    created = {}
    for op, call in calls:
        name = op.split(".", 1)[1]
        if name.startswith(("Create", "Put", "Run", "Import")) and name not in (
            "PutBucketVersioning",
            "PutPublicAccessBlock",
            "PutBucketEncryption",
            "PutBucketNotificationConfiguration",
            "PutRolePolicy",
            "PutTargets",
            "CreateMultipartUpload",
        ):
            created[op] = created.get(op, 0) + 1
            if op == "s3.CreateBucket":
                continue  # tagged by the PutBucketTagging that follows
            if op == "s3.PutObject":
                # Objects go with their bucket, which is tagged; the seed objects carry the
                # run id as an object tag too.
                assert call["Bucket"].startswith(f"ofreal-{RUN}-"), call
                if call["Key"].startswith("seed/"):
                    assert _tags_of(call).get("omnifile-run") == RUN, call
                continue
            t = _tags_of(call)
            assert t.get("omnifile-run") == RUN, (op, call)
            assert t.get("omnifile-expires") == EXPIRES, (op, call)
    buckets = [c["Bucket"] for op, c in calls if op == "s3.CreateBucket"]
    tagged = [c["Bucket"] for op, c in calls if op == "s3.PutBucketTagging"]
    assert set(buckets) <= set(tagged)
    for need in (
        "sqs.CreateQueue",
        "events.PutRule",
        "iam.CreateRole",
        "ec2.CreateVpc",
        "ec2.CreateVpcEndpoint",
        "ec2.RunInstances",
        "rolesanywhere.CreateTrustAnchor",
        "rolesanywhere.CreateProfile",
        "s3control.CreateJob",
    ):
        assert created.get(need), f"the run never called {need}"


def test_the_expiry_is_fixed_at_plan_and_capped(out, tmp_path):
    r = harness("plan", "aws", out, "--ttl-hours", "500")
    assert r.code == 0
    assert r.state["expires"] == "2026-11-04T00:00:00Z"  # max-ttl-hours 24 of the allow-list
    again = harness("plan", "aws", out, "--run-id", RUN, "--ttl-hours", "1")
    assert again.state["expires"] == "2026-11-04T00:00:00Z"  # a run keeps its expiry
