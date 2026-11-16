"""Settings, Storage: the backends, adding and editing them, and their live checks.

**One code path with the CLI.** Rows are built by `admin.backends.azure_row`
and `s3_row` and written by `admin.backends.register`, exactly as
``python -m omnifile_service.admin backend add`` does, so the page and the CLI
refuse the same things. The page never asks for public access: every row it
makes uses private networking (decision 22), and the catalog's
`backend_guard` refuses turning it off from any writer.

**Checks** (`check_backend`), each `ok`, `warning`, `failed`, `unknown` or
`not_configured`, with what to fix:

- Azure Blob, per account: its blob name resolves to private addresses only
  (``private_dns``); the service's identities hold Storage Blob Data
  Contributor or Owner on it (``rbac_role``, ARM role assignments); public
  network access is off (``public_access``); versioning and the change feed
  are on (``change_feed``), which rewind and catalog sync read; and the
  live/archive pair is a ZRS live account with Smart and a non-ZRS archive
  account (``archive_pair``).
- S3: the account is in the service's AWS settings and the row passes their
  checks (``aws_settings``); the S3 and STS interface endpoints resolve to
  private addresses (``private_dns``); the role was assumed by the latest
  test (``assume_role``; the worker runs it, `tests.py`).

The deployment's names come from `web`'s environment, never a secret:
``OMNIFILE_AZURE_SUBSCRIPTION_ID`` (where the storage accounts are),
``OMNIFILE_SERVICE_PRINCIPAL_IDS`` (``worker=<object id>,api=<object id>``,
the identities that need the data role) and the AWS settings the worker has
(``OMNIFILE_AWS_ACCOUNTS[_FILE]``, ``OMNIFILE_AWS_WEB_IDENTITY_AUDIENCE``).
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from ..admin import backends as admin
from ..api.validate import rfc3339
from ..catalog.connect import connect
from .ports import ProbeError

STORAGE_API = "2023-05-01"
AUTHZ_API = "2022-04-01"
DATA_ROLES = ("Storage Blob Data Contributor", "Storage Blob Data Owner")
#: The data role each service identity needs on an account. The deployment
#: gives the custom roles of deploy/azure/modules/roles.bicep (named
#: "<role> (<resource group>)"); the built-in ones are accepted too.
NEEDED_ROLES: dict[str, tuple[str, ...]] = {
    "worker": ("OmniFile Blob Worker", *DATA_ROLES),
    "api": ("OmniFile SAS Minter", *DATA_ROLES),
    "verifier": ("Storage Blob Data Reader", *DATA_ROLES),
}
#: A role-assumption test newer than this answers the S3 role check.
ASSUME_FRESH = timedelta(days=7)
_FLAG = re.compile(r"--([a-z-]+)")


def check(code: str, status: str, title: str, message: str, fix: str | None = None) -> dict:
    return {"code": code, "status": status, "title": title, "message": message, "fix": fix}


def principals(env: Mapping[str, str]) -> dict[str, str]:
    """``OMNIFILE_SERVICE_PRINCIPAL_IDS``: app name -> managed identity object id."""
    out: dict[str, str] = {}
    for part in (env.get("OMNIFILE_SERVICE_PRINCIPAL_IDS") or "").split(","):
        name, _, oid = part.strip().partition("=")
        if name and re.fullmatch(r"[0-9a-fA-F-]{36}", oid.strip()):
            out[name.strip()] = oid.strip().lower()
    return out


def private_deployment(env: Mapping[str, str], dev_mode: bool) -> bool:
    """Outside dev mode, storage is reached only through private endpoints."""
    return not dev_mode


def aws_configured(env: Mapping[str, str]) -> bool:
    from ..backends.s3.config import ENV_ACCOUNTS, ENV_ACCOUNTS_FILE

    return bool(env.get(ENV_ACCOUNTS) or env.get(ENV_ACCOUNTS_FILE))


# --- rows -------------------------------------------------------------------------------


def backend_json(r: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(r["config"] or {})
    return {
        "backend_id": str(r["backend_id"]),
        "name": r["name"],
        "kind": r["kind"],
        "private_network": bool(r["private_network"]),
        "can_be_home": r["kind"] != "s3",
        "where": admin.where(r["kind"], config),
        "config": config,
        "uses": dict(r.get("uses") or {"datasets": 0, "sources": 0, "migrations": 0}),
        "created_at": rfc3339(r["created_at"]),
    }


def field_of(message: str) -> str:
    """The draft field a refusal names (`--live` -> live), for the form."""
    m = _FLAG.search(message)
    return m[1].replace("-", "_") if m else "kind"


def build_row(draft: Mapping[str, Any], env: Mapping[str, str], dev_mode: bool) -> admin.Row:
    """The row the CLI would build from these answers. Raises admin.Refused."""
    e = dict(env)
    e["OMNIFILE_DEV_MODE"] = "1" if dev_mode else ""
    kind = draft.get("kind")
    if kind == "azure":
        allowed = {
            "kind",
            "name",
            "live",
            "archive",
            "account",
            "worm",
            "smart",
            "container",
            "region",
        }
        extra = sorted(set(draft) - allowed)
        if extra:
            raise admin.Refused(f"--{extra[0]} is not an Azure backend field")
        return admin.azure_row(
            name=draft.get("name"),
            live=draft.get("live"),
            archive=draft.get("archive"),
            account=draft.get("account"),
            worm=bool(draft.get("worm", False)),
            smart=draft.get("smart"),
            container=draft.get("container") or "data",
            region=draft.get("region"),
            public=False,
            env=e,
        )
    if kind == "s3":
        allowed = {"kind", "name", "account", "region", "buckets", "versioning", "archive_class"}
        extra = sorted(set(draft) - allowed)
        if extra:
            raise admin.Refused(f"--{extra[0]} is not an S3 backend field")
        if not draft.get("account") or not draft.get("region"):
            raise admin.Refused("--account and --region are required for S3")
        return admin.s3_row(
            name=draft.get("name"),
            account=str(draft["account"]),
            region=str(draft["region"]),
            buckets=list(draft.get("buckets") or []),
            versioning=bool(draft.get("versioning", True)),
            archive_class=draft.get("archive_class"),
            public=False,
            env=e,
        )
    raise admin.Refused("--kind is azure or s3")


@dataclass(frozen=True)
class Actor:
    """The signed-in person a backend change is audited as."""

    actor: str
    sid: str | None = None
    client_addr: str | None = None


def show_sync(db: Any, backend_id: int) -> dict[str, Any] | None:
    with connect(db, autocommit=True) as conn:
        return admin.show_backend(conn, str(backend_id))


def list_sync(db: Any) -> list[dict[str, Any]]:
    with connect(db, autocommit=True) as conn:
        return admin.list_backends(conn)


async def register(db: Any, row: admin.Row, who: Actor) -> admin.Result:
    """Write `row` through the catalog's owner-run `backend_register` (migration
    0035): `web` has no INSERT or UPDATE on catalog.backend. The function always
    makes the row private, finds it by name, never changes its kind, and audits
    the change as `who`; the catalog's `backend_guard` refuses moving what data
    uses (the CLI's rules). Its refusals are `400 invalid_request` (OF011)."""
    r = await (
        await db.execute(
            "SELECT out_outcome, out_backend_id, out_before FROM catalog.backend_register("
            "%s, %s, %s::jsonb, %s, %s, %s::inet, '{}'::jsonb)",
            (row.name, row.kind, json.dumps(row.config), who.actor, who.sid, who.client_addr),
        )
    ).fetchone()
    before = dict(r["out_before"] or {})
    changes = _changes(before, row.config) if r["out_outcome"] == "updated" else []
    return admin.Result(r["out_outcome"], int(r["out_backend_id"]), row, changes)


def _changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    return [
        f"{k}: {old.get(k)!r} -> {new.get(k)!r}"
        for k in sorted(set(old) | set(new))
        if old.get(k) != new.get(k)
    ]


# --- checks -----------------------------------------------------------------------------


def _private(addrs: list[str]) -> bool:
    return bool(addrs) and all(ipaddress.ip_address(a).is_private for a in addrs)


async def _dns(probes, code: str, host: str, what: str, required: bool) -> dict:
    try:
        addrs = await probes.resolve(host)
    except ProbeError as e:
        return check(
            code,
            "failed",
            f"Private DNS for {what}",
            str(e),
            "Link the privatelink DNS zone for it to the service's virtual network.",
        )
    if _private(addrs):
        return check(
            code,
            "ok",
            f"Private DNS for {what}",
            f"{host} resolves to {', '.join(addrs)}, a private address.",
        )
    msg = f"{host} resolves to {', '.join(addrs)}, a public address."
    if not required:
        return check(code, "warning", f"Private DNS for {what}", msg)
    return check(
        code,
        "failed",
        f"Private DNS for {what}",
        msg + " Private networking means never public (decision 22), so the service won't"
        " use this path.",
        "Create the private endpoint, and link its privatelink DNS zone to the service's"
        " virtual network (setup guide).",
    )


async def _accounts(probes, sub: str, rg: str = "") -> dict[str, dict[str, Any]]:
    """The storage accounts `web` may read: the resource group's when
    ``OMNIFILE_AZURE_RESOURCE_GROUP`` is set (where its reader role is)."""
    scope = f"/subscriptions/{sub}" + (f"/resourceGroups/{rg}" if rg else "")
    got = await probes.arm_get(f"{scope}/providers/Microsoft.Storage/storageAccounts", STORAGE_API)
    return {a.get("name"): a for a in got.get("value") or [] if isinstance(a, dict)}


async def _roles_of(probes, scope: str, oid: str) -> set[str]:
    got = await probes.arm_get(
        f"{scope}/providers/Microsoft.Authorization/roleAssignments?$filter=assignedTo('{oid}')",
        AUTHZ_API,
    )
    names: set[str] = set()
    for a in got.get("value") or []:
        rd = ((a or {}).get("properties") or {}).get("roleDefinitionId")
        if not isinstance(rd, str) or not rd.startswith("/"):
            continue
        # Read at the account's scope, where web's reader role reaches it: a
        # custom role is defined for this resource group only.
        d = await probes.arm_get(
            f"{scope}/providers/Microsoft.Authorization/roleDefinitions/{rd.rsplit('/', 1)[-1]}",
            AUTHZ_API,
        )
        name = (d.get("properties") or {}).get("roleName")
        if isinstance(name, str):
            names.add(name.split(" (", 1)[0])
    return names


async def _azure_checks(
    probes, config: Mapping[str, Any], private: bool, env: Mapping[str, str]
) -> list[dict]:
    out: list[dict] = []
    accounts: dict[str, str] = dict(config.get("accounts") or {})
    for name, url in accounts.items():
        host = urlsplit(url).hostname or f"{name}.blob.core.windows.net"
        out.append(await _dns(probes, "private_dns", host, f"account {name}", private))
    sub = (env.get("OMNIFILE_AZURE_SUBSCRIPTION_ID") or "").strip()
    if not sub:
        msg = "OMNIFILE_AZURE_SUBSCRIPTION_ID is not set on the web app, so it can't look."
        for code, title in (
            ("rbac_role", "The service's data role"),
            ("public_access", "Public network access"),
            ("change_feed", "Versioning and the change feed"),
        ):
            out.append(
                check(
                    code,
                    "not_configured",
                    title,
                    msg,
                    "Set it to the subscription of the storage accounts.",
                )
            )
        out.append(_pair_from_config(config))
        return out
    try:
        found = await _accounts(
            probes, sub, (env.get("OMNIFILE_AZURE_RESOURCE_GROUP") or "").strip()
        )
    except ProbeError as e:
        out.append(
            check(
                "rbac_role",
                "unknown",
                "The service's data role",
                f"Azure Resource Manager {e}",
                "Give the web app's identity Reader on the storage accounts.",
            )
        )
        out.append(_pair_from_config(config))
        return out
    who = principals(env)
    for name in accounts:
        acct = found.get(name)
        if acct is None:
            out.append(
                check(
                    "rbac_role",
                    "failed",
                    f"Account {name}",
                    f"No storage account {name} in subscription {sub}.",
                    "Check the account name, or the subscription setting.",
                )
            )
            continue
        aid = acct.get("id") or ""
        if not who:
            out.append(
                check(
                    "rbac_role",
                    "not_configured",
                    f"Data role on {name}",
                    "OMNIFILE_SERVICE_PRINCIPAL_IDS is not set, so the page doesn't"
                    " know which identities to look for.",
                    "Set it to worker=<object id>,api=<object id>.",
                )
            )
        for app, oid in who.items():
            if app not in NEEDED_ROLES:
                continue  # web is named for Connect Azure (cloud-connect.md) and has no data role
            try:
                roles = await _roles_of(probes, aid, oid)
            except ProbeError as e:
                out.append(
                    check(
                        "rbac_role",
                        "unknown",
                        f"Data role of {app} on {name}",
                        f"Azure Resource Manager {e}",
                    )
                )
                continue
            needed = NEEDED_ROLES.get(app, DATA_ROLES)
            held = [r for r in needed if r in roles]
            if held:
                out.append(
                    check(
                        "rbac_role",
                        "ok",
                        f"Data role of {app} on {name}",
                        f"{app} holds {held[0]}.",
                    )
                )
            else:
                out.append(
                    check(
                        "rbac_role",
                        "failed",
                        f"Data role of {app} on {name}",
                        f"{app} ({oid}) holds none of {', '.join(needed)} on {name}.",
                        f"Assign {needed[0]} on the account to {app}'s managed identity"
                        " (deploy/azure/main.bicep does).",
                    )
                )
        props = acct.get("properties") or {}
        pna = props.get("publicNetworkAccess")
        deny = ((props.get("networkAcls") or {}).get("defaultAction")) == "Deny"
        if pna == "Disabled" or deny:
            out.append(
                check(
                    "public_access",
                    "ok",
                    f"Public network access on {name}",
                    "Off: the account is reached through its private endpoint.",
                )
            )
        else:
            out.append(
                check(
                    "public_access",
                    "warning" if private else "ok",
                    f"Public network access on {name}",
                    f"publicNetworkAccess is {pna or 'not set'}.",
                    "Turn public network access off on the account." if private else None,
                )
            )
        try:
            svc = await probes.arm_get(f"{aid}/blobServices/default", STORAGE_API)
        except ProbeError as e:
            out.append(
                check(
                    "change_feed",
                    "unknown",
                    f"Change feed on {name}",
                    f"Azure Resource Manager {e}",
                )
            )
        else:
            sp = svc.get("properties") or {}
            feed = bool((sp.get("changeFeed") or {}).get("enabled"))
            versions = bool(sp.get("isVersioningEnabled"))
            if feed and versions:
                out.append(
                    check(
                        "change_feed",
                        "ok",
                        f"Change feed on {name}",
                        "Versioning and the change feed are on.",
                    )
                )
            else:
                missing = [
                    w for w, on in (("versioning", versions), ("the change feed", feed)) if not on
                ]
                out.append(
                    check(
                        "change_feed",
                        "failed",
                        f"Change feed on {name}",
                        f"{' and '.join(missing).capitalize()} off: catalog sync and rewind read"
                        " them.",
                        "Turn on blob versioning and the change feed on the account.",
                    )
                )
    out.append(_pair_from_arm(config, found))
    return out


def _pair_from_config(config: Mapping[str, Any]) -> dict:
    if config.get("layout", "two_account") == "two_account":
        live, arch = config.get("primary_account"), config.get("archive_account")
        return check(
            "archive_pair",
            "ok",
            "Live and archive accounts",
            f"Live {live}, archive {arch}: two accounts, as Smart and Archive need."
            " Their redundancy couldn't be read.",
        )
    return check(
        "archive_pair",
        "ok",
        "One account",
        f"Account {config.get('primary_account')}, tiered in place"
        + (" with version-level WORM." if config.get("worm") else "."),
    )


def _pair_from_arm(config: Mapping[str, Any], found: Mapping[str, Any]) -> dict:
    if config.get("layout", "two_account") != "two_account":
        return _pair_from_config(config)
    live, arch = config.get("primary_account"), config.get("archive_account")
    sku = {n: ((found.get(n) or {}).get("sku") or {}).get("name") for n in (live, arch)}
    if sku[live] is None or sku[arch] is None:
        return _pair_from_config(config)
    problems = []
    if config.get("smart") and not str(sku[live]).endswith("ZRS"):
        problems.append(f"the live account {live} is {sku[live]}; Smart needs ZRS")
    if "ZRS" in str(sku[arch]):
        problems.append(f"the archive account {arch} is {sku[arch]}; Archive is forbidden on ZRS")
    if problems:
        return check(
            "archive_pair",
            "failed",
            "Live and archive accounts",
            "; ".join(problems).capitalize() + ".",
            "Register storage accounts of the right redundancy under a new name.",
        )
    return check(
        "archive_pair",
        "ok",
        "Live and archive accounts",
        f"Live {live} ({sku[live]}), archive {arch} ({sku[arch]}).",
    )


async def _s3_checks(
    db, probes, config: Mapping[str, Any], private: bool, env: Mapping[str, str], now: datetime
) -> list[dict]:
    from ..backends.s3.config import AwsDeployment, ConfigError, S3Config

    out: list[dict] = []
    if not aws_configured(env):
        return [
            check(
                "aws_settings",
                "not_configured",
                "The service's AWS settings",
                "The web app has no AWS settings (OMNIFILE_AWS_ACCOUNTS_FILE).",
                "Give web the same AWS settings the worker has.",
            )
        ]
    try:
        dep = AwsDeployment.from_env(env)
        built = S3Config.for_backend(config, private_network=private, deployment=dep)
    except (ConfigError, OSError) as e:
        return [
            check(
                "aws_settings",
                "failed",
                "The service's AWS settings",
                str(e),
                "Add the account to OMNIFILE_AWS_ACCOUNTS, in this region.",
            )
        ]
    out.append(
        check(
            "aws_settings",
            "ok",
            "The service's AWS settings",
            f"Account {built.account_id} in {built.region} is configured; the buckets"
            " are sources, none of them OmniFile's own.",
        )
    )
    for label, url in (("S3", built.endpoint_url), ("STS", built.sts_endpoint_url)):
        if not url:
            out.append(
                check(
                    "private_dns",
                    "failed" if built.private_network else "warning",
                    f"{label} interface endpoint",
                    f"No {label} endpoint is configured, so the public one would be used.",
                    f"Set endpoints.{label.lower()} to the interface endpoint.",
                )
            )
            continue
        host = urlsplit(url).hostname or url
        if probes is None:
            out.append(
                check(
                    "private_dns",
                    "not_configured",
                    f"Private DNS for {label}",
                    "Live checks are off (OMNIFILE_SETTINGS_PROBES).",
                )
            )
        else:
            out.append(await _dns(probes, "private_dns", host, label, built.private_network))
    role = dep.accounts[built.account_id].role_arn
    t = await (
        await db.execute(
            "SELECT state, detail, finished_at FROM catalog.settings_test"
            " WHERE kind = 'aws_assume_role' AND params->>'role_arn' = %s AND created_at >= %s"
            " ORDER BY created_at DESC LIMIT 1",
            (role, now - ASSUME_FRESH),
        )
    ).fetchone()
    if not role:
        out.append(
            check(
                "assume_role",
                "failed",
                "The service's AWS role",
                "The account has no role_arn in the AWS settings.",
                "Create the role (Settings, AWS) and set role_arn.",
            )
        )
    elif t is None or t["state"] == "pending":
        out.append(
            check(
                "assume_role",
                "unknown",
                "The service's AWS role",
                f"{role} wasn't tested in the last 7 days.",
                "Press Test on Settings, AWS.",
            )
        )
    elif t["state"] == "passed":
        out.append(
            check(
                "assume_role",
                "ok",
                "The service's AWS role",
                f"The worker assumed {role} at {rfc3339(t['finished_at'])}.",
            )
        )
    else:
        out.append(
            check(
                "assume_role",
                "failed",
                "The service's AWS role",
                t["detail"] or f"The worker could not assume {role}.",
                "Give the role the trust policy on Settings, AWS.",
            )
        )
    return out


async def check_backend(
    db, probes, b: Mapping[str, Any], env: Mapping[str, str], dev_mode: bool, now: datetime
) -> list[dict]:
    kind, config = b["kind"], dict(b["config"] or {})
    private = bool(b["private_network"]) or private_deployment(env, dev_mode)
    if not b["private_network"] and private:
        head = [
            check(
                "private_network",
                "failed",
                "Private networking",
                "This row asks for public access in a private deployment.",
                "Register the storage again; a row never turns private networking off.",
            )
        ]
    else:
        head = [
            check(
                "private_network",
                "ok" if b["private_network"] else "warning",
                "Private networking",
                "On: reached only through private endpoints."
                if b["private_network"]
                else "Off (development only).",
            )
        ]
    if kind == "s3":
        return head + await _s3_checks(db, probes, config, bool(b["private_network"]), env, now)
    if kind == "azure_blob":
        if probes is None:
            return head + [
                check(
                    "probes",
                    "not_configured",
                    "Live checks",
                    "Live checks are off: the web app makes no outbound call without"
                    " OMNIFILE_SETTINGS_PROBES=1.",
                    "Set OMNIFILE_SETTINGS_PROBES=1 and give web Reader on the storage accounts.",
                ),
                _pair_from_config(config),
            ]
        return head + await _azure_checks(probes, config, private, env)
    return head + [
        check(
            "kind", "ok", "Simulated storage", f"A {kind} backend (development): nothing to check."
        )
    ]
