"""Connect Azure (docs/design/cloud-connect.md section 1).

An ArchiveAdmin signs in to Azure Resource Manager with a delegated token,
picks a subscription and resource groups, and the service gives its own
managed identities a few custom, read-mostly roles there. Then `web` reads,
with its own identity, the storage accounts, VNets, private endpoints and
storage movers it may now see, and the admin confirms what OmniFile uses.

- `plan`: the grants a choice needs, with every action in each role. Ids are
  fixed by the scope (uuid5), so a rerun updates the same role and
  assignment, and removal finds them again.
- `list_with`, `grant_with`, `remove_with`: the work a sign-in's callback
  does with the admin's token (an `arm.Arm`). Only these calls are made:
  GETs while listing; in a grant, a permissions check and then PUTs of the
  planned role definitions and assignments; in a removal, their DELETEs.
- `discover`: `web`'s own reads (Settings' `Probes`).
- `settings_json`: the page.

No token is stored anywhere: the callback gets it, calls these, and drops it.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from ..api.validate import rfc3339
from .arm import Arm, ArmError
from .ports import ProbeError
from .storage import principals

AUTHZ_API = "2022-04-01"
RESOURCES_API = "2022-12-01"
GROUPS_API = "2021-04-01"
STORAGE_API = "2023-05-01"
NETWORK_API = "2024-05-01"
MOVER_API = "2026-05-01"
DNS_API = "2020-06-01"
BLOB_ZONE = "privatelink.blob."
#: Fixed: the uuid5 namespace of every role and assignment id the service makes.
NAMESPACE = uuid.UUID("5d0f6c1e-9a7b-4d0e-8f61-0b6f1e2c3a19")
#: At most this many subscriptions get their resource groups listed.
MAX_SUBSCRIPTIONS = 25
MAX_GROUPS = 500
#: A new custom role can take a moment to replicate (VERIFY RA-45).
ROLE_RETRIES = 3
ROLE_RETRY_S = 2.0
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_RG = re.compile(r"^[-\w._()]{1,90}$")

#: The roles, by purpose: who gets it, its name, and every action in it.
#: Control plane only: no dataActions, no keys, no deletes, no authorization writes.
ROLES: dict[str, dict[str, Any]] = {
    "discover": {
        "principal": "web",
        "name": "OmniFile Discovery Reader",
        "description": "Read-only: storage accounts, VNets, private endpoints and Storage Mover,"
        " for the OmniFile portal's Connect Azure. No data access, no keys, no writes.",
        "actions": [
            "Microsoft.Storage/storageAccounts/read",
            "Microsoft.Storage/storageAccounts/blobServices/read",
            "Microsoft.Network/virtualNetworks/read",
            "Microsoft.Network/virtualNetworks/subnets/read",
            "Microsoft.Network/privateEndpoints/read",
            "Microsoft.Network/privateEndpoints/privateDnsZoneGroups/read",
            "Microsoft.Network/privateDnsZones/read",
            "Microsoft.Network/privateDnsZones/virtualNetworkLinks/read",
            "Microsoft.Authorization/roleAssignments/read",
            "Microsoft.Authorization/roleDefinitions/read",
            "Microsoft.StorageMover/storageMovers/read",
            "Microsoft.StorageMover/storageMovers/agents/read",
            "Microsoft.StorageMover/storageMovers/projects/read",
            "Microsoft.StorageMover/storageMovers/endpoints/read",
        ],
    },
    "storage_mover": {
        "principal": "worker",
        "name": "OmniFile Mover Deployer",
        "description": "Create a Storage Mover and its project, and define and run its jobs, for"
        " OmniFile migrations. It can't delete movers or touch data.",
        "actions": [
            "Microsoft.StorageMover/storageMovers/read",
            "Microsoft.StorageMover/storageMovers/write",
            "Microsoft.StorageMover/storageMovers/projects/read",
            "Microsoft.StorageMover/storageMovers/projects/write",
            "Microsoft.StorageMover/storageMovers/agents/read",
            "Microsoft.StorageMover/storageMovers/endpoints/read",
            "Microsoft.StorageMover/storageMovers/projects/jobDefinitions/read",
            "Microsoft.StorageMover/storageMovers/projects/jobDefinitions/write",
            "Microsoft.StorageMover/storageMovers/projects/jobDefinitions/startJob/action",
            "Microsoft.StorageMover/storageMovers/projects/jobDefinitions/stopJob/action",
            "Microsoft.StorageMover/storageMovers/projects/jobDefinitions/jobRuns/read",
        ],
    },
}
#: What the admin must be allowed on each scope.
NEEDED = {
    "grant": (
        "Microsoft.Authorization/roleDefinitions/write",
        "Microsoft.Authorization/roleAssignments/write",
    ),
    "remove": (
        "Microsoft.Authorization/roleAssignments/delete",
        "Microsoft.Authorization/roleDefinitions/delete",
    ),
}


class ConnectError(Exception):
    """An operation that can't go on, in words for the page."""


def scope_of(subscription_id: str, rg: str) -> str:
    return f"/subscriptions/{subscription_id}/resourceGroups/{rg}"


def role_definition_id(purpose: str, scope: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{purpose}|{scope.lower()}"))


def assignment_name(purpose: str, scope: str, principal_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{purpose}|{scope.lower()}|{principal_id.lower()}"))


def role_name(purpose: str, subscription_id: str, rg: str) -> str:
    return f"{ROLES[purpose]['name']} ({rg}, {subscription_id[:8]})"


def check_choice(choice: Mapping[str, Any], listing: Mapping[str, Any] | None, tenant_id: str):
    """The choice against the latest listing. Raises ConnectError."""
    if not listing:
        raise ConnectError("sign in to Azure first, so the service knows what you can see")
    sub = str(choice.get("subscription_id") or "")
    subs = {s["subscription_id"]: s for s in listing.get("subscriptions") or []}
    if sub not in subs:
        raise ConnectError(f"subscription {sub} isn't in your latest listing")
    if (subs[sub].get("tenant_id") or tenant_id) != tenant_id:
        raise ConnectError(
            "that subscription is in another tenant: the service's identities live in"
            f" {tenant_id}, and Azure assigns roles only within a subscription's own tenant"
        )
    groups = {g["name"].lower() for g in listing.get("resource_groups") or []
              if g["subscription_id"] == sub}  # fmt: skip
    rgs = list(dict.fromkeys(choice.get("resource_groups") or []))
    mover = choice.get("mover_resource_group") or None
    for rg in [*rgs, *([mover] if mover else [])]:
        if not isinstance(rg, str) or not _RG.match(rg):
            raise ConnectError(f"{rg!r} isn't a resource group name")
        if rg.lower() not in groups:
            raise ConnectError(f"resource group {rg} isn't in subscription {sub} as listed")
    if not rgs:
        raise ConnectError("choose at least one resource group")
    return {"subscription_id": sub, "resource_groups": rgs, "mover_resource_group": mover}


def plan(choice: Mapping[str, Any] | None, env: Mapping[str, str]) -> list[dict[str, Any]]:
    """The grants `choice` needs, in the order they are made."""
    if not choice:
        return []
    ids = principals(env)
    sub = choice["subscription_id"]
    wanted: list[tuple[str, str]] = [("discover", rg) for rg in choice["resource_groups"]]
    mover = choice.get("mover_resource_group")
    if mover:
        if all(rg.lower() != mover.lower() for rg in choice["resource_groups"]):
            wanted.append(("discover", mover))
        wanted.append(("storage_mover", mover))
    out = []
    for purpose, rg in wanted:
        role = ROLES[purpose]
        scope = scope_of(sub, rg)
        pid = ids.get(role["principal"])
        out.append(
            {
                "key": f"{purpose}|{scope.lower()}|{role['principal']}",
                "purpose": purpose,
                "role_name": role_name(purpose, sub, rg),
                "role_definition_id": role_definition_id(purpose, scope),
                "scope": scope,
                "principal": role["principal"],
                "principal_id": pid,
                "actions": list(role["actions"]),
                "state": "planned" if pid else "missing_identity",
                "grant_id": None,
            }
        )
    return out


# --- with the admin's token -------------------------------------------------------------


async def list_with(arm: Arm, tenant_id: str) -> dict[str, Any]:
    """What the admin can see: tenants, subscriptions and their resource groups."""
    tenants = []
    for t in (await arm.get("/tenants", RESOURCES_API)).get("value") or []:
        tid = str(t.get("tenantId") or "")
        own = tid.lower() == tenant_id.lower()
        tenants.append(
            {
                "tenant_id": tid,
                "display_name": t.get("displayName"),
                "selectable": own,
                "reason": None
                if own
                else "The service's identities are in another tenant; Azure assigns roles only"
                " within a subscription's own tenant.",
            }
        )
    subs = []
    for s in (await arm.get("/subscriptions", RESOURCES_API)).get("value") or []:
        sid = str(s.get("subscriptionId") or "")
        if not _GUID.match(sid):
            continue
        subs.append(
            {
                "subscription_id": sid,
                "display_name": s.get("displayName"),
                "tenant_id": s.get("tenantId"),
                "state": s.get("state"),
            }
        )
    groups: list[dict[str, Any]] = []
    truncated = len(subs) > MAX_SUBSCRIPTIONS
    for s in subs[:MAX_SUBSCRIPTIONS]:
        if s["tenant_id"] not in (None, tenant_id) or s["state"] not in (None, "Enabled"):
            continue
        try:
            got = await arm.get(f"/subscriptions/{s['subscription_id']}/resourcegroups", GROUPS_API)
        except ArmError:
            continue
        for g in got.get("value") or []:
            if len(groups) >= MAX_GROUPS:
                truncated = True
                break
            groups.append(
                {
                    "subscription_id": s["subscription_id"],
                    "name": str(g.get("name") or ""),
                    "location": g.get("location"),
                }
            )
    return {"tenants": tenants, "subscriptions": subs, "resource_groups": groups,
            "truncated": truncated}  # fmt: skip


def _allowed(action: str, perms: Sequence[Mapping[str, Any]]) -> bool:
    a = action.lower()
    ok = False
    for p in perms:
        if any(fnmatch.fnmatchcase(a, x.lower()) for x in p.get("actions") or []) and not any(
            fnmatch.fnmatchcase(a, x.lower()) for x in p.get("notActions") or []
        ):
            ok = True
    return ok


async def check_rights(arm: Arm, scopes: Sequence[str], purpose: str) -> None:
    """Raise ConnectError naming the scope and action the admin lacks, before any write."""
    for scope in dict.fromkeys(scopes):
        try:
            got = await arm.get(f"{scope}/providers/Microsoft.Authorization/permissions", AUTHZ_API)
        except ProbeError as e:  # ArmError or no answer: nothing was written yet
            raise ConnectError(f"couldn't read your rights on {scope}: {e}") from None
        perms = got.get("value") or []
        for action in NEEDED[purpose]:
            if not _allowed(action, perms):
                raise ConnectError(
                    f"you may not {action} on {scope}: you need Owner or User Access"
                    " Administrator there. Nothing was changed."
                )


def role_body(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "properties": {
            "roleName": item["role_name"],
            "description": ROLES[item["purpose"]]["description"],
            "type": "CustomRole",
            "permissions": [
                {
                    "actions": list(item["actions"]),
                    "notActions": [],
                    "dataActions": [],
                    "notDataActions": [],
                }  # fmt: skip
            ],
            "assignableScopes": [item["scope"]],
        }
    }


def _subscription(scope: str) -> str:
    return scope.split("/")[2]


async def grant_with(
    arm: Arm, items: Sequence[Mapping[str, Any]], *, sleep=asyncio.sleep
) -> tuple[list[dict[str, Any]], str | None]:
    """Make the planned grants. Returns (grants made, the error that stopped it).

    Never raises for Azure's answers or a lost connection (review of #64): once an
    item's role definition is made, the item is in the result, so it is recorded,
    audited and removable even if its assignment then failed or its answer was lost
    (``uncertain``; removal treats a 404 as already gone)."""
    if any(not i.get("principal_id") for i in items):
        return (
            [],
            "the deployment doesn't name every service identity (OMNIFILE_SERVICE_PRINCIPAL_IDS)",
        )
    try:
        await check_rights(arm, [i["scope"] for i in items], "grant")
    except ConnectError as e:
        return [], str(e)
    made: list[dict[str, Any]] = []
    for item in items:
        scope, rid = item["scope"], item["role_definition_id"]
        name = assignment_name(item["purpose"], scope, item["principal_id"])
        path = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}"
        defined = False
        try:
            try:
                await arm.put(
                    f"{scope}/providers/Microsoft.Authorization/roleDefinitions/{rid}",
                    AUTHZ_API,
                    role_body(item),
                )
            except ArmError:
                raise
            except ProbeError:
                defined = True  # the answer was lost: the role may exist
                raise
            defined = True
            body = {
                "properties": {
                    "roleDefinitionId": f"/subscriptions/{_subscription(scope)}/providers"
                    f"/Microsoft.Authorization/roleDefinitions/{rid}",
                    "principalId": item["principal_id"],
                    "principalType": "ServicePrincipal",
                }
            }
            for attempt in range(ROLE_RETRIES + 1):
                try:
                    await arm.put(path, AUTHZ_API, body)
                    break
                except ArmError as e:
                    if e.code == "RoleAssignmentExists":
                        break
                    if e.code != "RoleDefinitionDoesNotExist" or attempt == ROLE_RETRIES:
                        raise
                    await sleep(ROLE_RETRY_S)
        except ProbeError as e:  # ArmError, or no answer at all
            what = "refused" if isinstance(e, ArmError) else "didn't answer"
            if defined:
                made.append({**item, "assignment_id": path, "uncertain": True})
            return made, f"Azure {what} {item['role_name']} on {scope}: {e}"
        made.append({**item, "assignment_id": path})
    return made, None


async def remove_with(
    arm: Arm, grants: Sequence[Mapping[str, Any]], still_used: set[str]
) -> tuple[list[int], str | None]:
    """Delete each grant's assignment, then its role unless `still_used` names it.
    Returns (grant ids removed, the error that stopped it)."""
    try:
        await check_rights(arm, [g["scope"] for g in grants], "remove")
    except ConnectError as e:
        return [], str(e)
    # Never raises (review of #64): what was removed before an error is returned.
    done: list[int] = []
    for g in grants:
        try:
            try:
                await arm.delete_resource(g["assignment_id"], AUTHZ_API)
            except ArmError as e:
                if e.status != 404:
                    raise
            if g["role_definition_id"] not in still_used:
                try:
                    await arm.delete_resource(
                        f"{g['scope']}/providers/Microsoft.Authorization/roleDefinitions/"
                        f"{g['role_definition_id']}",
                        AUTHZ_API,
                    )
                except ArmError as e:
                    if e.status != 404:
                        raise
        except ProbeError as e:  # ArmError, or no answer at all
            what = "refused" if isinstance(e, ArmError) else "didn't answer"
            return done, f"Azure {what} removing {g['role_name']}: {e}"
        done.append(int(g["grant_id"]))
    return done, None


# --- with web's own identity -------------------------------------------------------------


def _rg_of(resource_id: str) -> str:
    parts = resource_id.split("/")
    try:
        return parts[[p.lower() for p in parts].index("resourcegroups") + 1]
    except (ValueError, IndexError):
        return ""


async def discover(
    probes,
    scopes: Sequence[str],
    now: datetime,
    *,
    service_vnet_id: str | None = None,
) -> dict[str, Any]:
    """Storage accounts, VNets, private endpoints and storage movers in each scope.

    An account is ``private`` (decision 22; review of #64) only when all hold:
    an approved blob private endpoint (a connection without a status is not
    approved); public access closed: ``publicNetworkAccess`` Disabled, or the
    default action Deny with no IP or VNet rule; and, for that account's own
    approved blob endpoint, a ``privatelink.blob`` zone in the endpoint's DNS zone
    group that is linked to the service's VNet (`service_vnet_id`), so the service
    resolves that account privately. One linked zone elsewhere in the tenant doesn't
    vouch for another account's endpoint (review of #64's follow-up)."""
    accounts, vnets, pes, movers, errors = [], [], [], [], []
    for scope in scopes:
        try:
            got_a = await probes.arm_get(
                f"{scope}/providers/Microsoft.Storage/storageAccounts", STORAGE_API
            )
            got_v = await probes.arm_get(
                f"{scope}/providers/Microsoft.Network/virtualNetworks", NETWORK_API
            )
            got_p = await probes.arm_get(
                f"{scope}/providers/Microsoft.Network/privateEndpoints", NETWORK_API
            )
        except ProbeError as e:
            errors.append(f"{scope}: {e}")
            continue
        try:
            got_m = await probes.arm_get(
                f"{scope}/providers/Microsoft.StorageMover/storageMovers", MOVER_API
            )
        except ProbeError:
            got_m = {}
        for a in got_a.get("value") or []:
            p = a.get("properties") or {}
            sku = (a.get("sku") or {}).get("name")
            accounts.append(
                {
                    "id": str(a.get("id") or ""),
                    "name": str(a.get("name") or ""),
                    "resource_group": _rg_of(str(a.get("id") or "")),
                    "location": a.get("location"),
                    "sku": sku,
                    "kind": a.get("kind"),
                    "zrs": bool(sku and "ZRS" in sku.upper()),
                    "hns": bool(p.get("isHnsEnabled")),
                    "public_network_access": p.get("publicNetworkAccess"),
                    "_acls": p.get("networkAcls") or {},
                }
            )
        for v in got_v.get("value") or []:
            p = v.get("properties") or {}
            vnets.append(
                {
                    "id": str(v.get("id") or ""),
                    "name": str(v.get("name") or ""),
                    "resource_group": _rg_of(str(v.get("id") or "")),
                    "location": v.get("location"),
                    "address_prefixes": list(
                        (p.get("addressSpace") or {}).get("addressPrefixes") or []
                    ),  # fmt: skip
                    "subnets": [str(s.get("name")) for s in p.get("subnets") or []],
                }
            )
        for e in got_p.get("value") or []:
            p = e.get("properties") or {}
            conns = (p.get("privateLinkServiceConnections") or []) + (
                p.get("manualPrivateLinkServiceConnections") or []
            )
            c = (conns[0].get("properties") if conns else None) or {}
            pes.append(
                {
                    "id": str(e.get("id") or ""),
                    "name": str(e.get("name") or ""),
                    "resource_group": _rg_of(str(e.get("id") or "")),
                    "subnet_id": (p.get("subnet") or {}).get("id"),
                    "target_id": c.get("privateLinkServiceId"),
                    "group_ids": list(c.get("groupIds") or []),
                    "status": (c.get("privateLinkServiceConnectionState") or {}).get("status"),
                }
            )
        for m in got_m.get("value") or []:
            movers.append(
                {
                    "id": str(m.get("id") or ""),
                    "name": str(m.get("name") or ""),
                    "resource_group": _rg_of(str(m.get("id") or "")),
                    "location": m.get("location"),
                }
            )
    # Each approved blob endpoint's own DNS zone group, and each zone's VNet links (read once).
    zones: dict[str, dict[str, Any]] = {}

    async def zone_linked(zid: str) -> bool:
        if zid not in zones:
            linked = False
            try:
                links = await probes.arm_get(f"{zid}/virtualNetworkLinks", DNS_API)
                vnets_ = [
                    str(((x.get("properties") or {}).get("virtualNetwork") or {}).get("id") or "")
                    for x in links.get("value") or []
                ]
                linked = bool(service_vnet_id) and any(
                    v.lower() == str(service_vnet_id).lower() for v in vnets_
                )
            except ProbeError as e:
                errors.append(f"{zid}: its VNet links: {e}")
            zones[zid] = {"id": zid, "linked_to_service": linked}
        return bool(zones[zid]["linked_to_service"])

    endpoint_linked: dict[str, bool] = {}
    for e in pes:
        if "blob" not in e["group_ids"] or e["status"] != "Approved":
            continue
        try:
            groups = await probes.arm_get(f"{e['id']}/privateDnsZoneGroups", NETWORK_API)
        except ProbeError as err:
            errors.append(f"{e['id']}: its DNS zone group: {err}")
            endpoint_linked[e["id"]] = False
            continue
        zone_ids = [
            str((cfg.get("properties") or {}).get("privateDnsZoneId") or "")
            for g in groups.get("value") or []
            for cfg in (g.get("properties") or {}).get("privateDnsZoneConfigs") or []
        ]
        ok = False
        for zid in zone_ids:
            if zid.rsplit("/", 1)[-1].lower().startswith(BLOB_ZONE) and await zone_linked(zid):
                ok = True
        endpoint_linked[e["id"]] = ok
    if not service_vnet_id:
        errors.append(
            "The deployment doesn't name the service's VNet (OMNIFILE_SERVICE_VNET_ID), so no"
            " account can be shown as private."
        )
    for a in accounts:
        own = [
            e
            for e in pes
            if (e["target_id"] or "").lower() == a["id"].lower()
            and "blob" in e["group_ids"]
            and e["status"] == "Approved"
        ]
        a["blob_private_endpoint"] = bool(own)
        dns_linked = any(endpoint_linked.get(e["id"], False) for e in own)
        acls = a.pop("_acls")
        open_rules = bool(acls.get("ipRules")) or bool(acls.get("virtualNetworkRules"))
        closed = a["public_network_access"] == "Disabled" or (
            acls.get("defaultAction") == "Deny" and not open_rules
        )
        a["dns_linked"] = dns_linked
        a["private"] = a["blob_private_endpoint"] and closed and dns_linked
    return {
        "blob_dns_zones": list(zones.values()),
        "service_vnet_id": service_vnet_id,
        "discovered_at": rfc3339(now),
        "storage_accounts": accounts,
        "virtual_networks": vnets,
        "private_endpoints": pes,
        "storage_movers": movers,
        "errors": errors,
    }


# --- the catalog ---------------------------------------------------------------------------


async def connection(db) -> dict[str, Any] | None:
    return await (
        await db.execute("SELECT * FROM catalog.azure_connection WHERE connection_id = 1")
    ).fetchone()


async def upsert(db, tenant_id: str, **cols: Any) -> None:
    from psycopg import sql
    from psycopg.types.json import Jsonb

    vals = {k: Jsonb(v) if isinstance(v, dict | list) else v for k, v in cols.items()}
    names = list(vals)
    await db.execute(
        sql.SQL(
            "INSERT INTO catalog.azure_connection (tenant_id, {cols}) VALUES (%s, {ph})"
            " ON CONFLICT (connection_id) DO UPDATE SET {sets}, updated_at = now()"
        ).format(
            cols=sql.SQL(", ").join(sql.Identifier(n) for n in names),
            ph=sql.SQL(", ").join(sql.Placeholder() for _ in names),
            sets=sql.SQL(", ").join(
                sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(n)) for n in names
            ),
        ),
        (tenant_id, *vals.values()),
    )


async def active_grants(db) -> list[dict[str, Any]]:
    return await (
        await db.execute(
            "SELECT * FROM catalog.azure_grant WHERE state = 'active' ORDER BY grant_id"
        )
    ).fetchall()


async def _names(db, ids: set[int]) -> dict[int, str]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    rows = await (
        await db.execute(
            "SELECT identity_id, coalesce(display_name, upn, 'identity ' || identity_id) AS n"
            " FROM catalog.identity WHERE identity_id = ANY (%s)",
            (list(ids),),
        )
    ).fetchall()
    return {r["identity_id"]: r["n"] for r in rows}


def grant_json(g: Mapping[str, Any], names: Mapping[int, str]) -> dict[str, Any]:
    return {
        "grant_id": str(g["grant_id"]),
        "purpose": g["purpose"],
        "role_name": g["role_name"],
        "scope": g["scope"],
        "principal": g["principal"],
        "principal_id": str(g["principal_id"]),
        "assignment_id": g["assignment_id"],
        "state": g["state"],
        "made_at": rfc3339(g["made_at"]),
        "made_by": names.get(g["made_by"]),
        "removed_at": rfc3339(g["removed_at"]),
        "removed_by": names.get(g["removed_by"]) if g["removed_by"] else None,
    }


def granted_scopes(grants: Sequence[Mapping[str, Any]], purpose: str = "discover") -> list[str]:
    return list(dict.fromkeys(g["scope"] for g in grants if g["purpose"] == purpose
                              and g["state"] == "active"))  # fmt: skip


def mover_scope(grants: Sequence[Mapping[str, Any]]) -> str | None:
    s = granted_scopes(grants, "storage_mover")
    return s[0] if s else None


async def settings_json(
    db, env: Mapping[str, str], tenant_id: str, now: datetime
) -> dict[str, Any]:
    c = await connection(db)
    grants = await (
        await db.execute(
            "SELECT * FROM catalog.azure_grant WHERE state = 'active' OR removed_at > now() -"
            " interval '30 days' ORDER BY grant_id"
        )
    ).fetchall()
    op = await (
        await db.execute(
            "SELECT op_id, purpose, state, detail, created_at, finished_at, expires_at"
            " FROM catalog.azure_connect_op ORDER BY op_id DESC LIMIT 1"
        )
    ).fetchone()
    ids = {g["made_by"] for g in grants} | {g["removed_by"] for g in grants}
    if c:
        ids |= {c["listed_by"], c["chosen_by"], c["confirmed_by"]}
    names = await _names(db, ids)
    choice = (c or {}).get("choice")
    items = plan(choice, env)
    active = {(g["purpose"], g["scope"].lower(), g["principal"]): g for g in grants
              if g["state"] == "active"}  # fmt: skip
    for i in items:
        g = active.get((i["purpose"], i["scope"].lower(), i["principal"]))
        if g is not None:
            i["state"], i["grant_id"] = "active", str(g["grant_id"])
    listing = None
    if c and c["listing"]:
        listing = {**c["listing"], "listed_at": rfc3339(c["listed_at"]),
                   "listed_by": names.get(c["listed_by"])}  # fmt: skip
    last_op = None
    if op:
        state = op["state"]
        if state == "started" and op["expires_at"] <= now:
            state = "expired"
        last_op = {
            "op_id": str(op["op_id"]),
            "purpose": op["purpose"],
            "state": state,
            "detail": op["detail"],
            "created_at": rfc3339(op["created_at"]),
            "finished_at": rfc3339(op["finished_at"]),
        }
    ids_env = principals(env)
    out = {
        "tenant_id": tenant_id,
        "identities": [{"name": n, "object_id": ids_env.get(n)} for n in ("web", "worker")],
        "listing": listing,
        "choice": None
        if not choice
        else {
            **choice,
            "chosen_at": rfc3339(c["chosen_at"]),
            "chosen_by": names.get(c["chosen_by"]),
        },
        "plan": items,
        "grants": [grant_json(g, names) for g in grants],
        "discovered": (c or {}).get("discovered"),
        "confirmed": None
        if not (c and c["confirmed"])
        else {
            **c["confirmed"],
            "confirmed_at": rfc3339(c["confirmed_at"]),
            "confirmed_by": names.get(c["confirmed_by"]),
        },  # fmt: skip
        "last_op": last_op,
    }
    out["steps"] = steps(out)
    return out


def steps(s: Mapping[str, Any]) -> list[dict[str, Any]]:
    def step(code, title, done, detail, attention=False):
        status = "done" if done else ("attention" if attention else "todo")
        return {"code": code, "title": title, "status": status, "detail": detail}

    ids = {i["name"]: i["object_id"] for i in s["identities"]}
    plan_items = s["plan"]
    granted = bool(plan_items) and all(i["state"] == "active" for i in plan_items)
    disc = s["discovered"]
    return [
        step(
            "identities",
            "The service's identities are known",
            all(ids.values()),
            "The deployment names the web and worker managed identities."
            if all(ids.values())
            else "The deployment doesn't set OMNIFILE_SERVICE_PRINCIPAL_IDS (web=…,worker=…):"
            " re-deploy with the identities' object ids.",
            attention=not all(ids.values()),
        ),
        step(
            "sign_in",
            "Sign in to Azure",
            s["listing"] is not None,
            "Lists the tenants, subscriptions and resource groups you can see.",
        ),
        step(
            "choose",
            "Choose the subscription and resource groups",
            s["choice"] is not None,
            "Where the client's storage, network and Storage Mover are.",
        ),
        step(
            "grant",
            "Grant the service's identities their roles",
            granted,
            f"{sum(i['state'] == 'active' for i in plan_items)} of {len(plan_items)} grants made."
            if plan_items
            else "Shown before anything is made; a second sign-in makes them.",
        ),
        step(
            "discover",
            "Discover storage and network",
            disc is not None and not disc.get("errors"),
            "Found "
            f"{len(disc['storage_accounts'])} storage accounts, {len(disc['virtual_networks'])}"
            f" VNets and {len(disc['private_endpoints'])} private endpoints."
            if disc
            else "Reads what the service may now see, with its own identity.",
            attention=bool(disc and disc.get("errors")),
        ),
        step(
            "confirm",
            "Confirm what OmniFile uses",
            s["confirmed"] is not None,
            "Ticked storage accounts are offered on the Storage page.",
        ),
    ]
