"""Decision 28 in the portal: who may see and act on what.

- **Read** is `catalog.read_set.readable_sql`: the item's ACL and, for an annexed
  file, every folder above it on the NAS (decision 63), nothing else. Roles never
  grant read.
- **Unreadable is 404**, never 403: `FileScope.require_read()`.
- A user with no on-premises SID reads nothing; one whose groups are still
  being resolved (token overage) gets `503 groups_pending`.
- **Share roles** (Owner, Writer, Reader) are membership of the share's AD
  groups, by SID. A file belongs to the deepest share whose root contains it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..catalog.read_set import readable_row_sql
from .context import Principal
from .problems import WebError, not_found


def acl_params(who: Principal) -> dict[str, Any]:
    """Parameters for `catalog.read_set.readable_sql`. Raises `503 groups_pending` while an
    overage user's groups are unresolved. A user without a SID matches
    nothing: the SID is a value no ACL holds and the groups are empty."""
    if who.is_app:
        return {"sid": "S-1-0-0", "groups": []}
    if who.groups_source == "pending":
        raise WebError(
            "groups_pending", "your AD groups are being looked up; try again", retry_after_s=5
        )
    if who.sid is None:
        return {"sid": "S-1-0-0", "groups": []}
    return {"sid": who.sid, "groups": list(who.groups)}


@dataclass
class ShareRoles:
    owner: bool = False
    writer: bool = False
    reader: bool = False


def share_roles(who: Principal, share: dict[str, Any]) -> ShareRoles:
    if who.is_app or who.sid is None or who.groups_source == "pending":
        return ShareRoles()
    mine = set(who.all_sids)
    return ShareRoles(
        owner=share["owners_group_sid"] in mine,
        writer=share.get("writers_group_sid") in mine,
        reader=share.get("readers_group_sid") in mine,
    )


@dataclass
class FileScope:
    """A file or folder with its ancestors, ACLs and covering share."""

    who: Principal
    file: dict[str, Any]
    #: Self first, then up to the dataset root: file_id, parent_id, name, readable.
    chain: list[dict[str, Any]]
    share: dict[str, Any] | None
    #: Every share of the dataset, for filters by share; roles come from `share` alone.
    shares: list[dict[str, Any]] = field(default_factory=list)

    @property
    def file_id(self) -> int:
        return self.file["file_id"]

    @property
    def dataset_id(self) -> int:
        return self.file["dataset_id"]

    @property
    def readable(self) -> bool:
        return bool(self.chain and self.chain[0]["readable"]) and self.share is not None

    def require_read(self, what: str = "file") -> FileScope:
        if not self.readable:
            raise not_found(what)
        return self

    @property
    def roles(self) -> ShareRoles:
        """The caller's roles in the share that governs this item: the deepest covering
        share. An outer share's groups grant nothing inside a nested share."""
        return share_roles(self.who, self.share) if self.share is not None else ShareRoles()

    def path(self) -> list[dict[str, Any]]:
        """From the share root to this item. Unreadable ancestors are nameless."""
        root = self.share["root_dir_id"] if self.share else None
        segs: list[dict[str, Any]] = []
        for c in self.chain:
            if c["readable"]:
                segs.append({"file_id": str(c["file_id"]), "name": c["name"]})
            else:
                segs.append({"file_id": None, "name": None})
            if c["file_id"] == root or (root is None and c["parent_id"] is None):
                break
        return list(reversed(segs))

    @property
    def ancestor_ids(self) -> list[int]:
        return [c["file_id"] for c in self.chain]


#: An item and its ancestors, each readable or not: one item's own chain of
#: folders, with no walk (the review of #88). `%(f)s` and `acl_params`.
SCOPE_CHAIN_SQL = f"""
SELECT a.file_id, a.parent_id, a.name, a.acl_id,
       (a.acl_id IS NOT NULL AND {readable_row_sql("a.file_id")}) AS readable
  FROM catalog.file_ancestors(%(f)s) a
  LEFT JOIN catalog.acl acl ON acl.acl_id = a.acl_id
 ORDER BY a.depth
"""


async def load_scope(db, who: Principal, file_id: int, *, include_deleted: bool = False):
    """The item, or `404` if it doesn't exist (readability is the caller's check
    through `require_read`, so a missing and an unreadable item look alike)."""
    params = acl_params(who)
    f = await (
        await db.execute("SELECT * FROM catalog.file WHERE file_id = %s", (file_id,))
    ).fetchone()
    if f is None or (f["deleted_at"] is not None and not include_deleted):
        raise not_found()
    chain = await (await db.execute(SCOPE_CHAIN_SQL, {**params, "f": file_id})).fetchall()
    shares = await (
        await db.execute(
            "SELECT * FROM catalog.share WHERE dataset_id = %s ORDER BY share_id",
            (f["dataset_id"],),
        )
    ).fetchall()
    share = covering_share(shares, [c["file_id"] for c in chain])
    if who.sid is None or who.is_app:
        for c in chain:
            c["readable"] = False
    return FileScope(who, f, list(chain), share, list(shares))


def covering_share(shares: list[dict[str, Any]], chain_ids: list[int]) -> dict[str, Any] | None:
    """The deepest share whose root is the item or an ancestor of it."""
    depth = {fid: i for i, fid in enumerate(chain_ids)}
    best, best_depth = None, None
    for s in shares:
        root = s["root_dir_id"]
        d = len(chain_ids) if root is None else depth.get(root)
        if d is None:
            continue
        if best_depth is None or d < best_depth:
            best, best_depth = s, d
    return best


async def app_dataset_scope(db, file_id: int) -> dict[str, Any]:
    """Apps act at dataset level (section 2.2): the file, unfiltered."""
    f = await (
        await db.execute(
            "SELECT * FROM catalog.file WHERE file_id = %s AND deleted_at IS NULL", (file_id,)
        )
    ).fetchone()
    if f is None:
        raise not_found()
    return f
