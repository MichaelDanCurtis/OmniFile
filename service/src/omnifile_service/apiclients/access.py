"""Whether an API client's owner may see a dataset (the owner's rule, 2026-11-04):
"whenever any type of API recommendation comes in, it needs to have its owner
stated, and they need to have access".

The owner can see a dataset when, as a person signed in to the portal, they
could see it to review a proposal for it:

- they held ArchiveAdmin or DataSteward at a portal sign-in within
  `ROLES_FRESH` (`identity.app_roles`, written at each sign-in; app roles
  live in Entra, and the catalog learns them only then), or
- they are an Owner of a share of the dataset: their SID or one of their
  AD groups (`identity.group_sids`, kept fresh by the worker from Graph) is
  the share's `owners_group_sid`.

Otherwise a proposal from the client is refused with `403 owner_lacks_access`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..web.problems import WebError

#: Roles seen at a sign-in older than this no longer count.
ROLES_FRESH = timedelta(days=30)
SEEING_ROLES = ("ArchiveAdmin", "DataSteward")


async def owner_can_see_dataset(db, owner_identity_id: int, dataset_id: int, now: datetime) -> bool:
    row = await (
        await db.execute(
            "SELECT (i.app_roles && %(roles)s::text[] AND i.app_roles_seen_at >= %(fresh)s)"
            "       AS by_role,"
            "       EXISTS (SELECT 1 FROM catalog.share s WHERE s.dataset_id = %(ds)s"
            "                  AND (s.owners_group_sid::text = i.sid::text"
            "                       OR s.owners_group_sid::text = ANY (i.group_sids::text[])))"
            "       AS by_share"
            "  FROM catalog.identity i WHERE i.identity_id = %(owner)s",
            {
                "roles": list(SEEING_ROLES),
                "fresh": now - ROLES_FRESH,
                "ds": dataset_id,
                "owner": owner_identity_id,
            },
        )
    ).fetchone()
    return bool(row and (row["by_role"] or row["by_share"]))


async def datasets_owner_can_see(db, owner_identity_id: int, now: datetime) -> list[int]:
    """Every dataset `owner_can_see_dataset` would allow, in one query (the live
    Activity feed filters a whole answer by it)."""
    rows = await (
        await db.execute(
            "SELECT d.dataset_id FROM catalog.dataset d"
            "  JOIN catalog.identity i ON i.identity_id = %(owner)s"
            " WHERE (i.app_roles && %(roles)s::text[] AND i.app_roles_seen_at >= %(fresh)s)"
            "    OR EXISTS (SELECT 1 FROM catalog.share s WHERE s.dataset_id = d.dataset_id"
            "               AND (s.owners_group_sid::text = i.sid::text"
            "                    OR s.owners_group_sid::text = ANY (i.group_sids::text[])))"
            " ORDER BY 1",
            {"roles": list(SEEING_ROLES), "fresh": now - ROLES_FRESH, "owner": owner_identity_id},
        )
    ).fetchall()
    return [r["dataset_id"] for r in rows]


async def require_owner_access(db, who, dataset_id: int, now: datetime) -> None:
    """For an app caller: its client's owner must see the dataset."""
    if not who.is_app:
        return
    client = who.api_client
    if client is None:
        # Every admitted app has a client (registry.admit_*); fail closed.
        raise WebError("client_not_registered", "this application is not registered")
    if not await owner_can_see_dataset(db, client["owner_identity_id"], dataset_id, now):
        raise WebError(
            "owner_lacks_access",
            f"the owner of API client {client['display_name']!r} can't see this dataset: they"
            " need ArchiveAdmin or DataSteward (seen at a portal sign-in in the last 30 days),"
            " or to be an Owner of one of its shares",
        )
