"""The IAM policies the AWS setup guide gives the client (deploy/aws/iam;
decision 50, threat model T4) say what the guide and the code rely on."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnifile_service.enrol.ca import ROLES_ANYWHERE_OU
from omnifile_service.worker.crl_jobs import TAG_KEY

IAM = Path(__file__).resolve().parents[2] / "deploy" / "aws" / "iam"


def policy(name: str) -> dict:
    if not IAM.exists():
        pytest.skip("deploy/aws is not in this checkout")
    doc = json.loads((IAM / name).read_text())
    assert doc["Version"] == "2012-10-17"
    return doc


def actions(stmt: dict) -> list[str]:
    a = stmt["Action"]
    return [a] if isinstance(a, str) else list(a)


def test_the_service_may_only_import_and_update_its_own_crl() -> None:
    doc = policy("service-crl-policy.json")
    allowed = {a for s in doc["Statement"] if s["Effect"] == "Allow" for a in actions(s)}
    assert allowed == {
        "rolesanywhere:ImportCrl",
        "rolesanywhere:TagResource",
        "rolesanywhere:UpdateCrl",
        "rolesanywhere:ListCrls",
    }
    for s in doc["Statement"]:
        acts = actions(s)
        if "rolesanywhere:ImportCrl" in acts:
            # IAM can't scope ImportCrl by resource: the tag the worker sets is the scope.
            assert s["Condition"]["StringEquals"] == {
                f"aws:RequestTag/{TAG_KEY}": "TRUST_ANCHOR_ID"
            }
            assert s["Condition"]["ForAllValues:StringEquals"] == {"aws:TagKeys": [TAG_KEY]}
        if "rolesanywhere:UpdateCrl" in acts:
            assert s["Resource"].endswith(":crl/*")
            assert s["Condition"]["StringEquals"] == {
                f"aws:ResourceTag/{TAG_KEY}": "TRUST_ANCHOR_ID"
            }


def test_the_edge_role_trusts_only_the_edge_cas_roles_anywhere_certificates() -> None:
    [stmt] = policy("edge-role-trust.json")["Statement"]
    assert stmt["Principal"] == {"Service": "rolesanywhere.amazonaws.com"}
    cond = stmt["Condition"]
    assert ":trust-anchor/" in cond["ArnEquals"]["aws:SourceArn"]
    assert cond["StringEquals"]["aws:PrincipalTag/x509Subject/OU"] == ROLES_ANYWHERE_OU
    assert all(
        cn.startswith("omnifile-edge-")
        for cn in cond["StringEquals"]["aws:PrincipalTag/x509Subject/CN"]
    )


def test_the_edge_role_reads_only_through_the_interface_endpoint() -> None:
    doc = policy("edge-role-policy.json")
    allows = [s for s in doc["Statement"] if s["Effect"] == "Allow"]
    assert {a for s in allows for a in actions(s)} == {"s3:GetObject", "s3:GetObjectVersion"}
    assert all("aws:SourceVpce" in s["Condition"]["StringEquals"] for s in allows)
    denied = {a for s in doc["Statement"] if s["Effect"] == "Deny" for a in actions(s)}
    for a in ("s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:RestoreObject",
              "s3:PutObjectTagging", "s3:PutObjectAcl"):  # fmt: skip
        assert a in denied, a
