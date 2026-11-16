"""Settings, AWS: the trust policy a role needs for the service's identity (decision 43).

The service holds no AWS key. It calls STS ``AssumeRoleWithWebIdentity`` with
its managed identity's Entra token for the deployment's fixed ``api://``
audience. Such a token is a v1 token: issuer ``https://sts.windows.net/<tenant>/``,
``aud`` the audience, ``sub`` the identity's object id. So the role's trust
policy names:

- the account's IAM OIDC provider for that issuer (created once per account,
  with the audience as its client id);
- ``sts:AssumeRoleWithWebIdentity`` only;
- ``<issuer host+path>:aud`` equal to the audience and ``…:sub`` one of the
  service identities that use AWS (``OMNIFILE_AWS_WEB_IDENTITY_SUBJECTS``, the
  worker's, and the api's and verifier's where they read S3).

Nothing is stored: the page builds the policy from the pasted ARN and the
deployment's settings. Test runs in the worker (`tests.py`), whose identity
the policy trusts. VERIFY on real AWS (real-aws-checks.md): that the v1
issuer is the one STS sees, and IAM's thumbprint handling for it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

ROLE_ARN = re.compile(r"^arn:aws:iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]{1,512})$")
_GUID = re.compile(r"^[0-9a-fA-F-]{36}$")


def subjects(env: Mapping[str, str]) -> list[str]:
    raw = env.get("OMNIFILE_AWS_WEB_IDENTITY_SUBJECTS") or ""
    out = [s.strip().lower() for s in raw.split(",") if _GUID.match(s.strip())]
    return out


def issuer(tenant_id: str) -> str:
    return f"https://sts.windows.net/{tenant_id}/"


def deployment(env: Mapping[str, str]):
    """The service's AWS settings, or None when there are none (or they're broken)."""
    from ..backends.s3.config import AwsDeployment, ConfigError
    from .storage import aws_configured

    if not aws_configured(env):
        return None
    try:
        return AwsDeployment.from_env(env)
    except (ConfigError, OSError):
        return None


def settings_json(env: Mapping[str, str], tenant_id: str) -> dict[str, Any]:
    from ..backends.s3.config import ENV_AUDIENCE

    dep = deployment(env)
    accounts = []
    if dep is not None:
        for a in dep.accounts.values():
            accounts.append(
                {
                    "account_id": a.account_id,
                    "region": a.region,
                    "role_arn": a.role_arn,
                    "sts_endpoint": a.endpoints.get("sts"),
                }
            )
    return {
        "configured": dep is not None,
        "private_network": dep.private_network if dep is not None else True,
        "audience": (dep.web_identity_audience if dep is not None else None)
        or env.get(ENV_AUDIENCE)
        or None,
        "tenant_id": tenant_id,
        "issuer": issuer(tenant_id),
        "subjects": subjects(env),
        "accounts": accounts,
    }


def trust_policy(role_arn: str, env: Mapping[str, str], tenant_id: str) -> dict[str, Any]:
    """The policy and the steps for `role_arn`. Raises ValueError for a bad ARN."""
    m = ROLE_ARN.match(role_arn)
    if not m:
        raise ValueError("not an IAM role ARN: arn:aws:iam::<12 digits>:role/<name>")
    account = m[1]
    s = settings_json(env, tenant_id)
    host = f"sts.windows.net/{tenant_id}/"
    provider_arn = f"arn:aws:iam::{account}:oidc-provider/{host}"
    audience = s["audience"]
    subs = s["subjects"] or ["<the worker's managed identity object id>"]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "OmniFileServiceWebIdentity",
                "Effect": "Allow",
                "Principal": {"Federated": provider_arn},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        f"{host}:aud": audience or "<the deployment's api:// audience>",
                        f"{host}:sub": subs if len(subs) > 1 else subs[0],
                    }
                },
            }
        ],
    }
    configured = any(a["account_id"] == account for a in s["accounts"])
    steps = [
        f"In account {account}, create the IAM OIDC identity provider once: URL"
        f" {issuer(tenant_id)}, audience {audience or '<the api:// audience>'}"
        f" (aws iam create-open-id-connect-provider --url {issuer(tenant_id)}"
        f" --client-id-list {audience or '<audience>'}).",
        f"Give the role {m[2]} this trust policy (aws iam update-assume-role-policy"
        f" --role-name {m[2].rsplit('/', 1)[-1]} --policy-document file://trust.json).",
        "Give it the permissions the setup guide lists for the service's role (reads of the"
        " source buckets, restores, Batch, the event queue, and the CRL upload of"
        " deploy/aws/iam/service-crl-policy.json). No access key is ever made.",
        "Put the ARN in the account's role_arn in the service's AWS settings"
        " (OMNIFILE_AWS_ACCOUNTS), then press Test.",
    ]
    if not s["subjects"]:
        steps.insert(
            0,
            "The web app doesn't know the service identities' object ids"
            " (OMNIFILE_AWS_WEB_IDENTITY_SUBJECTS): fill in the worker's, from the"
            " deployment's identityPrincipalIds output.",
        )
    return {
        "role_arn": role_arn,
        "account_id": account,
        "configured_account": configured,
        "oidc_provider": {"url": issuer(tenant_id), "arn": provider_arn, "audience": audience},
        "trust_policy": policy,
        "steps": steps,
    }
