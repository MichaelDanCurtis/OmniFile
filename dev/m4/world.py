"""The M4 dev stack's people: one Entra tenant, one AD domain, its groups and users.

Shared by dev/m4/stack.py (dev OIDC issuer users, fake Graph seed) and the M4 exit tests
(tests/e2e/m4.py, portal/e2e/), so a portal sign-in, a Graph lookup and a catalog ACL all
name the same SIDs and objectIds. Nothing here is a secret.
"""

from __future__ import annotations

TENANT = "7f3c2a10-4b5d-4e6f-8a9b-0c1d2e3f4a5b"
BOT_APP_ID = "0b0f1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d"
TEAMS_APP_ID = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
GRAPH_CLIENT_ID = "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f6a"
PORTAL_CLIENT_ID = "omnifile-portal-dev"
API_AUDIENCE = "api://omnifile-dev"
PARTNER_APP = "prizmanyx-dev"
PARTNER_ROLES = ("Policy.Propose", "Recall.Request", "Archive.Read")
INTENT_KID = "intent-dev"
SENDER = "omnifile@contoso.test"

DOMAIN = "S-1-5-21-4000-5000-6000"

#: key -> (rid, objectId, display name, sAMAccountName)
GROUPS = {
    "owners": (2001, "a0000000-0000-4000-8000-000000002001", "Seismic Owners", "Seismic-Owners"),
    "writers": (2002, "a0000000-0000-4000-8000-000000002002", "Seismic Writers", "Seismic-Writers"),
    "readers": (2003, "a0000000-0000-4000-8000-000000002003", "Seismic Readers", "Seismic-Readers"),
    "other": (2004, "a0000000-0000-4000-8000-000000002004", "Finance", "Finance"),
    "admins": (2100, "a0000000-0000-4000-8000-000000002100", "OmniFile Admins", "OmniFile-Admins"),
}

#: key -> (rid, objectId, name, roles, groups)
USERS = {
    "admin": (
        1001,
        "b0000000-0000-4000-8000-000000001001",
        "Ada Admin",
        ("ArchiveAdmin",),
        ("admins",),
    ),
    "admin2": (
        1006,
        "b0000000-0000-4000-8000-000000001006",
        "Abe Admin",
        ("ArchiveAdmin",),
        ("admins",),
    ),
    "owner": (1002, "b0000000-0000-4000-8000-000000001002", "Olga Owner", ("User",), ("owners",)),
    "reader": (
        1003,
        "b0000000-0000-4000-8000-000000001003",
        "Rita Reader",
        ("User",),
        ("readers",),
    ),
    "stranger": (
        1004,
        "b0000000-0000-4000-8000-000000001004",
        "Sid Stranger",
        ("User",),
        ("other",),
    ),
    "steward": (
        1005,
        "b0000000-0000-4000-8000-000000001005",
        "Sam Steward",
        ("DataSteward",),
        ("readers",),
    ),
}


def sid(rid: int) -> str:
    return f"{DOMAIN}-{rid}"


def group_sid(key: str) -> str:
    return sid(GROUPS[key][0])


def user(key: str) -> dict:
    rid, oid, name, roles, groups = USERS[key]
    return {
        "key": key,
        "oid": oid,
        "name": name,
        "upn": f"{key}@contoso.test",
        "mail": f"{key}@contoso.test",
        "sid": sid(rid),
        "roles": list(roles),
        "groups": [group_sid(g) for g in groups],
        "group_oids": [GROUPS[g][1] for g in groups],
    }


def dev_idp_users() -> list[dict]:
    """OMNIFILE_DEV_IDP_USERS for `web` (DevUser.from_mapping)."""
    return [
        {
            "key": k,
            "oid": u["oid"],
            "name": u["name"],
            "upn": u["upn"],
            "onprem_sid": u["sid"],
            "groups": u["groups"],
            "roles": u["roles"],
        }
        for k in USERS
        for u in (user(k),)
    ]


def graph_seed() -> dict:
    """FakeGraph.from_seed's users and groups."""
    users = [
        {
            "id": u["oid"],
            "sid": u["sid"],
            "displayName": u["name"],
            "userPrincipalName": u["upn"],
            "mail": u["mail"],
            "groups": u["group_oids"],
        }
        for k in USERS
        for u in (user(k),)
    ]
    groups = [
        {"id": oid, "sid": sid(rid), "displayName": name, "samAccountName": sam}
        for rid, oid, name, sam in GROUPS.values()
    ]
    return {"users": users, "groups": groups, "blocked": []}
