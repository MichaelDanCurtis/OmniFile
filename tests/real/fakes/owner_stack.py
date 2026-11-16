"""The owner's one-time step in a dry run: tests/real/aws/harness-roles.yaml made in the moto
server, as `aws cloudformation deploy` would make it in a real account, before the harness
runs. It goes through a client of its own, not the harness's session, so none of it is in the
run's transcript: the harness's IAM user never makes these.

It reads the template itself, so a dry run uses exactly the roles, policies and boundaries the
owner reviews. Only what the template uses is understood: AWS::IAM::ManagedPolicy, Role and
InstanceProfile, with Ref, Fn::Sub and Fn::GetAtt (Arn).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import boto3
import yaml

TEMPLATE = Path(__file__).resolve().parent.parent / "aws" / "harness-roles.yaml"
ACCOUNT = "123456789012"  # moto's
RA_SERVICE = "rolesanywhere.amazonaws.com"


def load(path: Path = TEMPLATE) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def resolve(v: Any, refs: dict[str, str], attrs: dict[str, str], region: str) -> Any:
    pseudo = {"AWS::AccountId": ACCOUNT, "AWS::Region": region, "AWS::Partition": "aws"}
    if isinstance(v, dict):
        if set(v) == {"Ref"}:
            name = v["Ref"]
            return pseudo.get(name) or refs[name]
        if set(v) == {"Fn::Sub"}:
            return re.sub(
                r"\$\{([^}]+)\}", lambda m: pseudo.get(m.group(1)) or refs[m.group(1)], v["Fn::Sub"]
            )
        if set(v) == {"Fn::GetAtt"}:
            res, att = v["Fn::GetAtt"]
            assert att == "Arn", att
            return attrs[res]
        return {k: resolve(x, refs, attrs, region) for k, x in v.items()}
    if isinstance(v, list):
        return [resolve(x, refs, attrs, region) for x in v]
    return v


def deploy(moto_url: str, region: str, path: Path = TEMPLATE) -> dict[str, str]:
    """Make the template's resources in moto; returns logical id -> ARN."""
    iam = boto3.client(
        "iam",
        endpoint_url=moto_url,
        region_name=region,
        aws_access_key_id="fake-owner-key",
        aws_secret_access_key="fake-owner-secret",
    )
    res = load(path)["Resources"]
    refs: dict[str, str] = {}
    attrs: dict[str, str] = {}
    order = ["AWS::IAM::ManagedPolicy", "AWS::IAM::Role", "AWS::IAM::InstanceProfile"]
    unknown = {r["Type"] for r in res.values()} - set(order)
    assert not unknown, f"harness-roles.yaml uses {unknown}: teach owner_stack.py"
    for kind in order:
        for lid, r in res.items():
            if r["Type"] != kind:
                continue
            p = resolve(r["Properties"], refs, attrs, region)
            if kind == "AWS::IAM::ManagedPolicy":
                arn = iam.create_policy(
                    PolicyName=p["ManagedPolicyName"],
                    Path=p["Path"],
                    PolicyDocument=json.dumps(p["PolicyDocument"]),
                    Description=p.get("Description", ""),
                )["Policy"]["Arn"]
                refs[lid] = attrs[lid] = arn
            elif kind == "AWS::IAM::Role":
                arn = iam.create_role(
                    RoleName=p["RoleName"],
                    Path=p["Path"],
                    AssumeRolePolicyDocument=json.dumps(p["AssumeRolePolicyDocument"]),
                    PermissionsBoundary=p["PermissionsBoundary"],
                    MaxSessionDuration=p.get("MaxSessionDuration", 3600),
                    Tags=p.get("Tags", []),
                )["Role"]["Arn"]
                for pol in p.get("Policies", []):
                    iam.put_role_policy(
                        RoleName=p["RoleName"],
                        PolicyName=pol["PolicyName"],
                        PolicyDocument=json.dumps(pol["PolicyDocument"]),
                    )
                for m in p.get("ManagedPolicyArns", []):
                    iam.attach_role_policy(RoleName=p["RoleName"], PolicyArn=m)
                refs[lid], attrs[lid] = p["RoleName"], arn
            else:
                ip = iam.create_instance_profile(
                    InstanceProfileName=p["InstanceProfileName"], Path=p["Path"]
                )["InstanceProfile"]
                for role in p.get("Roles", []):
                    iam.add_role_to_instance_profile(
                        InstanceProfileName=p["InstanceProfileName"], RoleName=role
                    )
                refs[lid], attrs[lid] = p["InstanceProfileName"], ip["Arn"]
    # The owner's other one-time step (tests/real/aws/README.md, step 1b): Roles Anywhere's
    # service-linked role. A test leaves it out to see the run stop before it makes anything.
    if os.environ.get("REAL_FAKE_NO_RA_SLR") != "1":
        iam.create_service_linked_role(AWSServiceName=RA_SERVICE)
    return attrs
