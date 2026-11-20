"""Settings, Identity: the tenant, the app roles and the AD groups that hold them,
and admin consent.

From Graph, when `web`'s identity may read them: the portal app's service
principal (`servicePrincipals(appId=…)`), its role assignments
(`appRoleAssignedTo`) and its tenant-wide delegated grant
(`oauth2PermissionGrants`, consentType ``AllPrincipals``). Those reads need
Application.Read.All (VERIFY on a real tenant), which register.sh doesn't grant
today. Without it the page falls back to what the catalog saw at sign-ins:
who held each role in the last 30 days, and consent shown as granted when
people signed in (they couldn't have without it).

Admin consent can't be granted by the app itself: the page links to Entra's
admin-consent URL, for a tenant administrator.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from ..api.validate import rfc3339
from .ports import ProbeError

USER_ROLES = ("ArchiveAdmin", "DataSteward", "User")
APP_ROLES = ("Policy.Propose", "Recall.Request", "Archive.Read", "Archive.Adopt")
SEEN = timedelta(days=30)


def consent_url(tenant_id: str, client_id: str) -> str:
    return (
        f"https://login.microsoftonline.com/{quote(tenant_id)}/adminconsent"
        f"?client_id={quote(client_id)}"
    )


async def _seen(db, now: datetime) -> dict[str, int]:
    rows = await (
        await db.execute(
            "SELECT r AS role, count(*) AS n FROM catalog.identity i, unnest(i.app_roles) r"
            " WHERE i.app_roles_seen_at >= %s GROUP BY r",
            (now - SEEN,),
        )
    ).fetchall()
    return {r["role"]: int(r["n"]) for r in rows}


async def identity_settings(
    db,
    probes,
    tenant_id: str,
    client_id: str,
    audiences: list[str],
    now: datetime,
    role_holders: str = "groups",
) -> dict[str, Any]:
    seen = await _seen(db, now)
    roles: list[dict[str, Any]] = [
        {
            "role": r,
            "member_types": ["user"],
            "groups": [],
            "people": [],
            "people_seen": seen.get(r, 0),
            "source": "sign_ins",
        }
        for r in USER_ROLES
    ] + [
        {
            "role": r,
            "member_types": ["application"],
            "groups": [],
            "people": [],
            "people_seen": 0,
            "source": "sign_ins",
        }
        for r in APP_ROLES
    ]
    signed_in = sum(seen.values()) > 0
    consent = {
        "status": "granted" if signed_in else "unknown",
        "detail": "People signed in to the portal in the last 30 days, so the delegated sign-in"
        " permissions are consented."
        if signed_in
        else "Nobody signed in recently, and Graph couldn't be asked.",
        "url": consent_url(tenant_id, client_id),
    }
    graph = {
        "code": "graph",
        "status": "not_configured",
        "title": "Reading Entra from Graph",
        "message": "Live checks are off (OMNIFILE_SETTINGS_PROBES): roles are shown as"
        " seen at sign-ins.",
        "fix": None,
    }
    if probes is not None:
        try:
            sp = await probes.graph_get(
                f"/servicePrincipals(appId='{quote(client_id)}')?$select=id,appRoles"
            )
            defs = {a.get("id"): a.get("value") for a in sp.get("appRoles") or []}
            assigned = await probes.graph_get(
                f"/servicePrincipals/{quote(str(sp.get('id')))}/appRoleAssignedTo?$top=999"
            )
            by_role: dict[str, list[dict[str, Any]]] = {}
            people: dict[str, list[dict[str, Any]]] = {}
            for a in assigned.get("value") or []:
                role = defs.get(a.get("appRoleId"))
                if role is not None and a.get("principalType") == "User":
                    # Named people (decision 89), by Entra's name only: no SID is looked up.
                    people.setdefault(role, []).append(
                        {
                            "object_id": a.get("principalId"),
                            "display_name": a.get("principalDisplayName"),
                        }
                    )
                    continue
                if role is None or a.get("principalType") != "Group":
                    continue
                by_role.setdefault(role, []).append(
                    {
                        "object_id": a.get("principalId"),
                        "display_name": a.get("principalDisplayName"),
                        "sid": None,
                    }
                )
            sids = await _group_sids(db, [g["display_name"] for gs in by_role.values() for g in gs])
            for r in roles:
                r["groups"] = [
                    {**g, "sid": sids.get(g["display_name"])} for g in by_role.get(r["role"], [])
                ]
                r["people"] = people.get(r["role"], [])
                r["source"] = "graph"
            grants = await probes.graph_get(
                f"/oauth2PermissionGrants?$filter=clientId eq '{quote(str(sp.get('id')))}'"
                " and consentType eq 'AllPrincipals'"
            )
            scopes = " ".join(g.get("scope") or "" for g in grants.get("value") or []).split()
            ok = {"openid", "profile"} <= set(scopes)
            consent = {
                "status": "granted" if ok else "missing",
                "detail": f"Tenant-wide consent for {', '.join(sorted(set(scopes)))}."
                if ok
                else "No tenant-wide consent for openid and profile: people are asked to"
                " consent, or can't sign in.",
                "url": consent_url(tenant_id, client_id),
            }
            graph = {
                "code": "graph",
                "status": "ok",
                "title": "Reading Entra from Graph",
                "message": "Roles, groups and consent read from Graph now.",
                "fix": None,
            }
        except ProbeError as e:
            graph = {
                "code": "graph",
                "status": "warning",
                "title": "Reading Entra from Graph",
                "message": f"Graph {e}. Roles are shown as seen at sign-ins.",
                "fix": "Reading the app's role assignments needs Application.Read.All for the"
                " web app's identity; or see Enterprise applications, Users and groups, in"
                " the Entra admin centre.",
            }
    return {
        "tenant_id": tenant_id,
        "client_id": client_id,
        "api_audiences": audiences,
        "consent": consent,
        "graph": graph,
        "roles": roles,
        "role_holders": role_holders,
    }


async def _group_sids(db, names: list[str | None]) -> dict[str, str]:
    names = [n for n in names if n]
    if not names:
        return {}
    rows = await (
        await db.execute(
            "SELECT display_name, sid::text AS sid FROM catalog.identity"
            " WHERE kind = 'group' AND display_name = ANY (%s)",
            (names,),
        )
    ).fetchall()
    return {r["display_name"]: r["sid"] for r in rows}


async def whoami(db, who, settings, now: datetime) -> dict[str, Any]:
    owner = []
    if who.sid and who.groups_source != "pending":
        rows = await (
            await db.execute(
                "SELECT share_id FROM catalog.share WHERE owners_group_sid::text = ANY (%s)"
                " ORDER BY share_id",
                (who.all_sids,),
            )
        ).fetchall()
        owner = [str(r["share_id"]) for r in rows]
    session = who.session or {}
    at = session.get("auth_time")
    recent = at is not None and settings.clock() - at <= settings.reauth_max_age
    checks = []

    def c(code, status, title, message, fix=None):
        checks.append(
            {"code": code, "status": status, "title": title, "message": message, "fix": fix}
        )

    if who.sid:
        c("sid", "ok", "On-premises identity", f"Your token carries your AD SID {who.sid}.")
    else:
        c(
            "sid",
            "failed",
            "On-premises identity",
            "Your token has no onprem_sid: you are cloud-only to OmniFile, so file access checks"
            " are all false.",
            "Sign in with an account synced from AD, or add the onprem_sid"
            " optional claim to the app registration.",
        )
    if who.groups_source == "pending":
        c("groups", "warning", "AD groups", "Your groups are being looked up (token overage).")
    elif who.groups:
        c("groups", "ok", "AD groups", f"{len(who.groups)} group SIDs, from {who.groups_source}.")
    else:
        c(
            "groups",
            "warning",
            "AD groups",
            "No group SIDs.",
            "The groups claim must be on-premises group SIDs (token configuration).",
        )
    if "ArchiveAdmin" in who.roles:
        c("role", "ok", "App role", "You hold ArchiveAdmin, which Settings needs.")
    c(
        "reauth",
        "ok" if recent else "warning",
        "Recent sign-in",
        "You signed in within 10 minutes, as changes here need."
        if recent
        else "Changes here will ask you to sign in again first.",
    )
    return {
        "object_id": who.object_id,
        "identity_id": None if who.identity_id is None else str(who.identity_id),
        "display_name": who.display_name,
        "upn": who.upn,
        "sid": who.sid,
        "roles": sorted(who.roles),
        "groups_source": who.groups_source,
        "group_count": len(who.groups),
        "owner_of": owner,
        "session": {
            "auth_time": rfc3339(at),
            "expires_at": rfc3339(session.get("expires_at") or now),
            "recently_authenticated": recent,
        },
        "checks": checks,
    }
