"""Connect AWS (docs/design/cloud-connect.md section 2).

"Launch in AWS" opens CloudFormation's quick-create page with OmniFile's
template (`deploy/aws/cloudformation/omnifile-connect.yaml`, packaged here
byte for byte) and this deployment's values: the tenant, the ``api://``
audience and the service identities. The stack makes the role the service
assumes with its web identity (decision 43), with every write on the sources
denied (decision 40), the event queue and rule, the Batch role and the ops
bucket. Every name follows from the deployment id, so the role ARN, queue and
bucket are known from the account id alone.

Then the worker fetches (`cloud_jobs.py`): it assumes the role and reads the
buckets. Or the admin pastes the stack's outputs (`parse_outputs`). Both end
in the account's ``OMNIFILE_AWS_ACCOUNTS`` entry (`entry_from`), checked by
the loader the worker uses. The entry is recorded and shown for the
deployment's settings; the S3 backend never reads it from the catalog (B1:
where requests go and which identity is used are operator settings).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from importlib import resources
from typing import Any
from urllib.parse import quote, urlencode

from .aws import issuer, subjects

TEMPLATE = "cfn/omnifile-connect.yaml"
ENV_TEMPLATE_URL = "OMNIFILE_AWS_CONNECT_TEMPLATE_URL"
DEFAULT_REGION = "us-east-1"
DEFAULT_DEPLOYMENT = "omnifile"
DEPLOYMENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,19}$")
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
ACCOUNT = re.compile(r"^[0-9]{12}$")
_TEMPLATE_URL = re.compile(r"^https://[a-z0-9.-]+\.s3[a-z0-9.-]*\.amazonaws\.com/[^\s]+$")
#: Outputs the template gives (cloud-connect.md 2.2).
OUTPUTS = ("AccountId", "Region", "DeploymentId", "RoleArn", "BatchRoleArn", "OpsBucket",
           "EventQueueUrl", "EventQueueArn", "SourceBuckets", "S3EndpointDns", "StsEndpointDns",
           "SqsEndpointDns")  # fmt: skip


def template_text() -> str:
    return resources.files(__package__).joinpath(TEMPLATE).read_text(encoding="utf-8")


def names(account_id: str, deployment_id: str, region: str) -> dict[str, str]:
    """What the stack is called, from its parameters alone."""
    return {
        "stack_name": f"omnifile-{deployment_id}",
        "role_arn": f"arn:aws:iam::{account_id}:role/omnifile-service-{deployment_id}",
        "batch_role_arn": f"arn:aws:iam::{account_id}:role/omnifile-batch-{deployment_id}",
        "ops_bucket": f"omnifile-ops-{deployment_id}-{account_id}",
        "queue_url": f"https://sqs.{region}.amazonaws.com/{account_id}/omnifile-events-{deployment_id}",
    }


def private_network(env: Mapping[str, str]) -> bool:
    from ..backends.s3.config import ENV_PRIVATE

    return env.get(ENV_PRIVATE, "1").strip().lower() not in ("0", "false", "no")


def audience(env: Mapping[str, str]) -> str | None:
    from ..backends.s3.config import ENV_AUDIENCE

    return env.get(ENV_AUDIENCE) or None


_CIDR = re.compile(r"^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$")


def parameters(env: Mapping[str, str], tenant_id: str, deployment_id: str) -> dict[str, str]:
    # The interface endpoints' security group admits the service's own VNet range
    # (review of #64), never a wide default.
    cidr = (env.get("OMNIFILE_SERVICE_VNET_CIDR") or "").strip()
    return {
        "TenantId": tenant_id,
        "Audience": audience(env) or "",
        "ServiceSubjects": ",".join(subjects(env)),
        "DeploymentId": deployment_id,
        "CreateOidcProvider": "true",
        "SourceBuckets": "",
        "EndpointClientCidr": cidr if _CIDR.match(cidr) else "",
    }


def connect_json(
    env: Mapping[str, str],
    tenant_id: str,
    region: str | None,
    deployment_id: str | None,
    connections: list[dict[str, Any]],
) -> dict[str, Any]:
    region = region or DEFAULT_REGION
    dep = deployment_id or DEFAULT_DEPLOYMENT
    params = parameters(env, tenant_id, dep)
    problems = []
    if not params["Audience"]:
        problems.append(
            "The deployment has no web-identity audience (OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE),"
            " so AWS can't be told which tokens to trust."
        )
    if not params["ServiceSubjects"]:
        problems.append(
            "The deployment doesn't name the service identities that use AWS"
            " (OMNIFILE_AWS_WEB_IDENTITY_SUBJECTS): the role would trust no one."
        )
    console = f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}"
    stack = f"omnifile-{dep}"
    template_url = env.get(ENV_TEMPLATE_URL) or ""
    quick = None
    if template_url and _TEMPLATE_URL.match(template_url) and not problems:
        q = {"templateURL": template_url, "stackName": stack}
        q.update({f"param_{k}": v for k, v in params.items() if v})
        quick = f"{console}#/stacks/quickcreate?{urlencode(q, quote_via=quote)}"
    private = private_network(env)
    steps = [
        "Press Launch in AWS and sign in to the AWS account that holds the source buckets."
        " OmniFile never sees your AWS sign-in.",
        "Type the source bucket names into SourceBuckets (comma-separated). Leave the rest as"
        " filled in. Tick the IAM acknowledgement and create the stack.",
        "On each source bucket, turn on Properties, Amazon EventBridge. The stack can't, and"
        " OmniFile never changes a source bucket. AWS starts sending a bucket's events about"
        " five minutes after that; a change made before then sends none.",
        "When the stack shows CREATE_COMPLETE, come back and press Fetch with the account id"
        " and region. The service assumes the new role and reads the buckets.",
        "Confirm the account's settings. They go into the deployment's awsAccounts parameter"
        " (OMNIFILE_AWS_ACCOUNTS); then the account shows as configured here.",
    ]
    if private:
        steps.insert(
            2,
            "Private networking is on: give VpcId and SubnetIds so the stack makes the S3, STS"
            " and SQS interface endpoints, or use endpoints you already have. Fetch needs the"
            " S3 and STS endpoints from the stack's outputs.",
        )
    if quick is None:
        steps[0] = (
            "Download the template, open CloudFormation's Create stack page and choose Upload a"
            " template file. Type the parameter values shown here. (A one-click link needs the"
            f" template in S3: {ENV_TEMPLATE_URL}.)"
        )
    return {
        "ready": not problems,
        "problems": problems,
        "tenant_id": tenant_id,
        "audience": params["Audience"] or None,
        "subjects": subjects(env),
        "private_network": private,
        "region": region,
        "deployment_id": dep,
        "stack_name": stack,
        "role_arn_pattern": f"arn:aws:iam::<account>:role/omnifile-service-{dep}",
        "parameters": params,
        "quick_create_url": quick,
        "console_create_url": f"{console}#/stacks/create",
        "template_path": "/v1/settings/aws/connect/template",
        "steps": steps,
        "connections": connections,
    }


# --- outputs, pasted or fetched ------------------------------------------------------------


def parse_outputs(text: str) -> dict[str, str]:
    """The stack's outputs from the console's Outputs tab (key, value, description,
    tab-separated), ``describe-stacks`` JSON, a {"Key": "Value"} object, or
    ``Key=Value`` lines. Unknown keys are dropped."""
    out: dict[str, str] = {}
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, Mapping) and "Stacks" in data:
            stacks = data.get("Stacks") or []
            data = (stacks[0] if stacks else {}).get("Outputs") or []
        if isinstance(data, Mapping) and "Outputs" in data:
            data = data["Outputs"]
        if isinstance(data, list):
            for o in data:
                if isinstance(o, Mapping) and o.get("OutputKey") in OUTPUTS:
                    out[str(o["OutputKey"])] = str(o.get("OutputValue") or "")
        elif isinstance(data, Mapping):
            for k, v in data.items():
                if k in OUTPUTS and isinstance(v, str | int):
                    out[k] = str(v)
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        elif "=" in line and not line.startswith("arn:"):
            parts = [p.strip() for p in line.split("=", 1)]
        else:
            parts = line.split(None, 1)
        if len(parts) >= 2 and parts[0] in OUTPUTS and parts[1]:
            out[parts[0]] = parts[1].split()[0] if parts[0] != "SourceBuckets" else parts[1]
    return out


def _https(dns: str | None) -> str | None:
    if not dns:
        return None
    dns = dns.strip().removeprefix("https://")
    return f"https://{dns}" if dns else None


def endpoints_from(outputs: Mapping[str, str]) -> dict[str, str]:
    """Interface endpoints from the stack's DNS outputs. S3's first DNS name is a
    wildcard (``*.vpce-….s3.<region>.vpce.amazonaws.com``): its ``bucket.`` and
    ``control.`` names are the data and control endpoints (VERIFY RW-36)."""
    out: dict[str, str] = {}
    s3 = (outputs.get("S3EndpointDns") or "").strip()
    if s3:
        base = s3.removeprefix("*.")
        out["s3"] = f"https://bucket.{base}"
        out["s3_control"] = f"https://control.{base}"
    if (sts := _https(outputs.get("StsEndpointDns"))) is not None:
        out["sts"] = sts
    if (sqs := _https(outputs.get("SqsEndpointDns"))) is not None:
        out["sqs"] = sqs
    return out


def entry_from(
    account_id: str | None,
    region: str | None,
    deployment_id: str | None,
    outputs: Mapping[str, str],
    env: Mapping[str, str],
    endpoints: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """The account's OMNIFILE_AWS_ACCOUNTS entry, and what is wrong with it."""
    from ..backends.s3.config import AwsDeployment, ConfigError

    account_id = account_id or outputs.get("AccountId")
    region = region or outputs.get("Region")
    deployment_id = deployment_id or outputs.get("DeploymentId")
    problems: list[str] = []
    if not account_id or not ACCOUNT.match(account_id):
        problems.append("No 12-digit AccountId.")
    if not region or not REGION.match(region):
        problems.append("No Region.")
    if not deployment_id or not DEPLOYMENT_ID.match(deployment_id):
        problems.append("No DeploymentId.")
    if problems:
        return None, problems
    assert account_id and region and deployment_id
    n = names(account_id, deployment_id, region)
    for key, want in (("RoleArn", n["role_arn"]), ("BatchRoleArn", n["batch_role_arn"]),
                      ("OpsBucket", n["ops_bucket"])):  # fmt: skip
        got = outputs.get(key)
        if got and got != want:
            problems.append(f"{key} is {got}, not the template's {want}: was the template changed?")
    eps = {**endpoints_from(outputs), **dict(endpoints or {})}
    entry: dict[str, Any] = {
        "region": region,
        "role_arn": n["role_arn"],
        "event_queues": {"*": outputs.get("EventQueueUrl") or n["queue_url"]},
        "batch": {"role_arn": n["batch_role_arn"], "manifest_bucket": n["ops_bucket"]},
        "staging_bucket": n["ops_bucket"],
    }
    private = private_network(env)
    # The deployment's mode (review of #64): an account defaults to private, so a
    # deployment without private networking says so, or its entry is refused.
    entry["private_network"] = private
    if eps:
        entry["endpoints"] = eps
    aud = audience(env)
    if aud is None:
        problems.append("The deployment has no web-identity audience to check the entry with.")
        return entry, problems
    try:
        AwsDeployment.from_mapping(
            {account_id: entry}, web_identity_audience=aud, private_network=private
        )
    except ConfigError as e:
        problems.append(f"The worker would refuse it: {e}")
    return entry, problems


def settings_value(env: Mapping[str, str], account_id: str, entry: Mapping[str, Any]) -> str:
    """The whole OMNIFILE_AWS_ACCOUNTS value: the deployment's accounts with this one."""
    from ..backends.s3.config import ENV_ACCOUNTS

    try:
        current = json.loads(env.get(ENV_ACCOUNTS) or "{}")
    except ValueError:
        current = {}
    if not isinstance(current, dict):
        current = {}
    current[account_id] = dict(entry)
    return json.dumps(current, indent=2, sort_keys=True)


def configured(env: Mapping[str, str], account_id: str) -> bool:
    from ..backends.s3.config import ENV_ACCOUNTS

    try:
        return account_id in (json.loads(env.get(ENV_ACCOUNTS) or "{}") or {})
    except (ValueError, TypeError):
        return False


def connection_json(row: Mapping[str, Any], env: Mapping[str, str], by: str | None) -> dict:
    from ..api.validate import rfc3339

    return {
        "account_id": row["account_id"],
        "region": row["region"],
        "deployment_id": row["deployment_id"],
        "entry": dict(row["entry"] or {}),
        "buckets": list(row["buckets"] or []),
        "env_name": "OMNIFILE_AWS_ACCOUNTS",
        "parameter_name": "awsAccounts",
        "settings_json": settings_value(env, row["account_id"], row["entry"] or {}),
        "configured": configured(env, row["account_id"]),
        "confirmed_at": rfc3339(row["confirmed_at"]),
        "confirmed_by": by,
        "steps": [
            "Set the deployment's awsAccounts parameter (OMNIFILE_AWS_ACCOUNTS on the worker,"
            " api and web) to the value shown, and re-deploy (deploy/azure/README.md).",
            "Back here, press Test on the role: the worker assumes it with its web identity.",
            "Add each source bucket on the Storage page (an S3 backend), then start a migration.",
        ],
    }


def issuer_of(tenant_id: str) -> str:
    return issuer(tenant_id)
