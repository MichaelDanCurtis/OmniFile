"""Adding datasets and shares (portal-api.md section 5.16; M6,
docs/design/share-provisioning.md).

- `GET /v1/backends`: the storage backends a dataset can live on.
- `POST /v1/datasets`: a dataset on an edge, with its root directory.
- `POST /v1/shares`: a share of a dataset's root. The edge renders it into
  its Samba and Ganesha config from the `share.upsert` it receives, reloads
  both servers, and gives the root the groups' ACL.

Signed-in ArchiveAdmin only, never an app: a share decides who reaches the
data. Both creations are audited (`dataset.add`, `share.add`). Every rule the
edge checks again before it serves a share (access-control.md section 3) is
checked here first, so a share the portal accepts is one the edge renders.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg

from .context import Ctx, Principal, WebResult, audit, if_match
from .problems import WebError, invalid, not_found, role_required
from .routes_partner import _names
from .routes_shares import _SID, NFS_SEC, restricted_groups, share_view

#: A dataset's name, as the portal and the partner API show it.
DATASET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
#: A share's name: its SMB share name, NFS pseudo path and the root folder's
#: name on the edge (ShareConfig::validate). Never hidden: `.omni` is the edge's.
SHARE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
RESERVED_SHARE_NAMES = {"global", "homes", "printers", "ipc$"}
PROTOCOLS = ("nfs3", "nfs4", "smb")
#: Characters Ganesha's CLIENT list can carry (ShareConfig::validate).
CLIENT = re.compile(r"[A-Za-z0-9._:/-]{1,253}")
KEY_PREFIX = re.compile(r"(?:[A-Za-z0-9!_.*'()-]+/)*")

DATASET_FIELDS = {"name", "backend_id", "edge_id", "storage_layout", "key_prefix"}
SHARE_FIELDS = {
    "dataset_id", "name", "protocols", "nfs_sec", "nfs_clients", "root_squash",
    "owners_group_sid", "writers_group_sid", "readers_group_sid", "on_open",
    "wait_threshold_minutes", "smb_encrypt", "rewind_depth_days", "rewind_depth_max_days",
    "host_trust_override", "path",
}  # fmt: skip


def _admin(who: Principal) -> None:
    if who.is_app or not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")


def _only(body: dict[str, Any], allowed: set[str], what: str) -> None:
    extra = set(body) - allowed
    if extra:
        raise invalid(sorted(extra)[0], f"is not a {what} field")


def _id(body: dict[str, Any], field: str) -> int:
    v = body.get(field)
    if isinstance(v, str) and v.isdigit():
        return int(v)
    if isinstance(v, int) and not isinstance(v, bool) and v > 0:
        return v
    raise invalid(field, "an id is required")


#: The widest network a host-trusted share may name (as the edge checks,
#: omnifile_acl::config): 0.0.0.0/1 and 128.0.0.0/1 are all of IPv4.
MIN_PREFIX_V4 = 8
MIN_PREFIX_V6 = 32


def _wild(c: str) -> bool:
    """A client entry that lets in more than named hosts or subnets."""
    low = c.lower()
    if "/" in c:
        addr, length = c.rsplit("/", 1)
        least = MIN_PREFIX_V6 if ":" in addr else MIN_PREFIX_V4
        if not length.isdigit() or int(length) < least:
            return True
    return any(ch in c for ch in "*?[") or low in ("all", "everyone", "0.0.0.0", "::")


def _servable_folder(name: str) -> bool:
    """A folder name the edge can serve as a share root: smbd expands `%` and
    trims a trailing space, and the edge refuses control characters, quotes,
    backslashes and semicolons (omnifile_acl::config)."""
    return not (
        name != name.rstrip()
        or any(ch in name for ch in '%";\\')
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name)
    )


async def list_backends(ctx: Ctx) -> WebResult:
    if not ctx.who.has("ArchiveAdmin", "DataSteward"):
        raise role_required("ArchiveAdmin", "DataSteward")
    async with ctx.svc.catalog.read() as db:
        rows = await (
            await db.execute(
                "SELECT backend_id, name, kind, private_network FROM catalog.backend ORDER BY name"
            )
        ).fetchall()
    return WebResult(
        200,
        {
            "backends": [
                {
                    "backend_id": str(r["backend_id"]),
                    "name": r["name"],
                    "kind": r["kind"],
                    "private_network": bool(r["private_network"]),
                    # Decision 18: S3 is a migration source, never a dataset's home;
                    # a `nas` backend is an annex's filer (the edge reads it).
                    "can_be_home": r["kind"] not in ("s3", "nas"),
                }
                for r in rows
            ]
        },
    )


async def add_dataset(ctx: Ctx) -> WebResult:
    """`POST /v1/datasets`: a dataset and its root directory."""
    _admin(ctx.who)
    b = ctx.body
    _only(b, DATASET_FIELDS, "dataset")
    name = b.get("name")
    if not isinstance(name, str) or not DATASET_NAME.fullmatch(name):
        raise invalid(
            "name", "1 to 63 letters, digits, '.', '_' or '-', starting with a letter or digit"
        )
    backend_id, edge_id = _id(b, "backend_id"), _id(b, "edge_id")
    layout = b.get("storage_layout", "two_account")
    if layout not in ("two_account", "single_account"):
        raise invalid("storage_layout", "two_account or single_account")
    prefix = b.get("key_prefix", "")
    if not isinstance(prefix, str) or len(prefix) > 512 or not KEY_PREFIX.fullmatch(prefix):
        raise invalid(
            "key_prefix", "empty, or path segments each ending in '/', e.g. projects/seismic/"
        )
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        backend = await (
            await db.execute(
                "SELECT kind FROM catalog.backend WHERE backend_id = %s", (backend_id,)
            )
        ).fetchone()
        if backend is None:
            raise invalid("backend_id", "no such backend")
        if backend["kind"] == "s3":
            raise invalid(
                "backend_id",
                "a dataset's home is Azure Blob; register S3 data as a migration source"
                " (POST /v1/sources)",
            )
        if backend["kind"] == "nas":
            raise invalid(
                "backend_id",
                "a dataset's home is Azure Blob; a NAS is annexed (POST /v1/annexes)",
            )
        edge = await (
            await db.execute("SELECT state FROM catalog.edge WHERE edge_id = %s", (edge_id,))
        ).fetchone()
        if edge is None or edge["state"] == "revoked":
            raise invalid("edge_id", "no such edge")
        row = await (
            await db.execute(
                "INSERT INTO catalog.dataset"
                " (name, backend_id, edge_id, storage_layout, key_prefix)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (name) DO NOTHING"
                " RETURNING dataset_id, migration_state",
                (name, backend_id, edge_id, layout, prefix),
            )
        ).fetchone()
        if row is None:
            raise WebError("name_taken", f"a dataset named {name} exists")
        ds = row["dataset_id"]
        # The root: owned by root until the edge gives it the share's groups.
        root = await (
            await db.execute(
                "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, uid, gid, mode)"
                " VALUES (%s, NULL, '', 'dir', 0, 0, %s) RETURNING file_id",
                (ds, 0o770),
            )
        ).fetchone()
        details = {"dataset_id": str(ds), "name": name, "backend_id": str(backend_id),
                   "edge_id": str(edge_id), "storage_layout": layout}  # fmt: skip
        await audit(db, ctx.who, "dataset.add", details=details)
    return WebResult(
        201,
        {
            "dataset_id": str(ds),
            "name": name,
            "backend_id": str(backend_id),
            "backend_kind": backend["kind"],
            "edge_id": str(edge_id),
            "storage_layout": layout,
            "key_prefix": prefix,
            "migration_state": row["migration_state"],
            "root_dir_id": str(root["file_id"]),
            "shares": [],
        },
    )


def _share_fields(b: dict[str, Any]) -> dict[str, Any]:
    """The new share's columns, each checked; access-control.md section 3."""
    name = b.get("name")
    if (
        not isinstance(name, str)
        or not SHARE_NAME.fullmatch(name)
        or name.lower() in RESERVED_SHARE_NAMES
    ):
        raise invalid(
            "name",
            "1 to 80 letters, digits, '.', '_' or '-', starting with a letter or digit;"
            " not global, homes or printers",
        )

    def strings(field: str, allowed: tuple[str, ...] | None = None) -> list[str]:
        v = b.get(field, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise invalid(field, "a list of strings")
        if allowed is not None and any(x not in allowed for x in v):
            raise invalid(field, f"each one of {', '.join(allowed)}")
        if len(set(v)) != len(v):
            raise invalid(field, "has a duplicate")
        return v

    protocols = strings("protocols", PROTOCOLS)
    if not protocols:
        raise invalid("protocols", "at least one of nfs3, nfs4, smb")
    nfs = any(p != "smb" for p in protocols)
    sec = strings("nfs_sec", NFS_SEC)
    clients = strings("nfs_clients")
    root_squash = b.get("root_squash", True)
    if not isinstance(root_squash, bool):
        raise invalid("root_squash", "true or false")
    if nfs:
        if not sec:
            raise invalid(
                "nfs_sec", "an NFS share needs a security flavour: krb5, krb5i, krb5p or sys"
            )
        for c in clients:
            if not CLIENT.fullmatch(c):
                raise invalid("nfs_clients", f"{c!r} isn't a host name, address or subnet")
        if "sys" in sec:
            if not clients:
                raise invalid("nfs_clients", "sec=sys trusts the client: list the hosts or subnets")
            for c in clients:
                if _wild(c):
                    raise invalid(
                        "nfs_clients", f"{c!r} lets in any host; name the hosts or subnets"
                    )
            if not root_squash:
                raise invalid("root_squash", "sec=sys shares squash root")
    elif sec or clients:
        raise invalid("nfs_sec" if sec else "nfs_clients", "only for NFS shares")

    sids: dict[str, str | None] = {}
    for f in ("owners_group_sid", "writers_group_sid", "readers_group_sid"):
        v = b.get(f)
        if v is None and f != "owners_group_sid":
            sids[f] = None
            continue
        if not isinstance(v, str) or not _SID.fullmatch(v):
            raise invalid(
                f, "an AD group's SID" + (" is required" if f == "owners_group_sid" else "")
            )
        sids[f] = v

    on_open = b.get("on_open", "fail")
    if on_open not in ("wait", "fail", "auto"):
        raise invalid("on_open", "wait, fail or auto")
    threshold = b.get("wait_threshold_minutes")
    if on_open == "auto":
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold <= 0:
            raise invalid("wait_threshold_minutes", "minutes, with on_open auto")
    elif threshold is not None:
        raise invalid("wait_threshold_minutes", "only with on_open auto")

    out: dict[str, Any] = {}
    for f in ("rewind_depth_days", "rewind_depth_max_days"):
        v = b.get(f, 0)
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 3650:
            raise invalid(f, "days, 0 to 3650")
        out[f] = v
    if out["rewind_depth_days"] > out["rewind_depth_max_days"]:
        raise invalid("rewind_depth_days", "at most rewind_depth_max_days")
    for f in ("smb_encrypt", "host_trust_override"):
        v = b.get(f, False)
        if not isinstance(v, bool):
            raise invalid(f, "true or false")
        out[f] = v
    return {
        "name": name,
        "protocols": protocols,
        "nfs_sec": sec,
        "nfs_clients": clients,
        "root_squash": root_squash,
        **sids,
        "on_open": on_open,
        "wait_threshold_minutes": threshold,
        **out,
    }


async def add_share(ctx: Ctx) -> WebResult:
    """`POST /v1/shares`: a share of a dataset's root, on the dataset's edge."""
    _admin(ctx.who)
    b = ctx.body
    _only(b, SHARE_FIELDS, "share")
    dataset_id = _id(b, "dataset_id")
    f = _share_fields(b)
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        ds = await (
            await db.execute(
                "SELECT d.edge_id, d.storage_layout,"
                " (SELECT file_id FROM catalog.file WHERE dataset_id = d.dataset_id"
                "   AND parent_id IS NULL) AS root_id"
                " FROM catalog.dataset d WHERE d.dataset_id = %s FOR UPDATE",
                (dataset_id,),
            )
        ).fetchone()
        if ds is None:
            raise invalid("dataset_id", "no such dataset")
        if ds["edge_id"] is None:
            raise invalid("dataset_id", "the dataset isn't on an edge")
        if ds["root_id"] is None:
            raise invalid("dataset_id", "the dataset has no root folder")
        annexed = await (
            await db.execute(
                "SELECT name FROM catalog.annex WHERE dataset_id = %s LIMIT 1", (dataset_id,)
            )
        ).fetchone()
        if annexed is not None:
            # Annex A1: an annex's copies are never served by the edge (no
            # placeholders, annex.md section 6.3); a read-only share of them
            # is phase A6.
            raise WebError(
                "dataset_annexed", f"the dataset holds the copies of annex {annexed['name']}"
            )
        root_share = await (
            await db.execute(
                "SELECT name FROM catalog.share WHERE dataset_id = %s"
                " AND coalesce(root_dir_id, %s) = %s",
                (dataset_id, ds["root_id"], ds["root_id"]),
            )
        ).fetchone()
        target = ds["root_id"]
        path = b.get("path", "/")
        if not isinstance(path, str):
            raise invalid("path", "a folder of the dataset, e.g. /projects/2026")
        names = _names(path or "/")
        if any(not _servable_folder(n) for n in names):
            raise invalid(
                "path",
                "a shared folder's path can't hold %, quotes, backslashes, semicolons,"
                " control characters, or a name ending in a space: rename the folder first",
            )
        if names:
            # On the edge, a dataset's root folder is named after its root
            # share (Replica::root_name): no root share, no folder to share.
            if root_share is None:
                raise invalid(
                    "path",
                    "share the dataset's root first: on the edge, its folder is named after"
                    " that share",
                )
            for name in names:
                row = await (
                    await db.execute(
                        "SELECT file_id, kind FROM catalog.file WHERE parent_id = %s"
                        " AND name = %s AND deleted_at IS NULL",
                        (target, name),
                    )
                ).fetchone()
                if row is None or row["kind"] != "dir":
                    raise invalid("path", "no such folder in the dataset")
                target = row["file_id"]
            taken = await (
                await db.execute("SELECT name FROM catalog.share WHERE root_dir_id = %s", (target,))
            ).fetchone()
            if taken is not None:
                raise WebError(
                    "dataset_shared", f"that folder is already shared as {taken['name']}"
                )
        elif root_share is not None:
            raise WebError(
                "dataset_shared", f"the dataset's root is already shared as {root_share['name']}"
            )
        clash = await (
            await db.execute(
                "SELECT name FROM catalog.share WHERE edge_id = %s AND lower(name) = lower(%s)",
                (ds["edge_id"], f["name"]),
            )
        ).fetchone()
        if clash is not None:
            raise WebError(
                "name_taken",
                f"the edge already has a share named {clash['name']} (names ignore case)",
            )
        if (
            "sys" in f["nfs_sec"]
            and not f["host_trust_override"]
            and await restricted_groups(db, dataset_id)
        ):
            raise invalid(
                "host_trust_override",
                "folders of this dataset carry a restricted group: a sec=sys share needs the"
                " recorded override",
            )
        try:
            row = await (
                await db.execute(
                    "INSERT INTO catalog.share (dataset_id, edge_id, name, root_dir_id, protocols,"
                    " nfs_sec, nfs_clients, root_squash, host_trust_override, owners_group_sid,"
                    " writers_group_sid, readers_group_sid, on_open, wait_threshold_minutes,"
                    " rewind_depth_days, rewind_depth_max_days, smb_encrypt)"
                    " VALUES (%(dataset_id)s, %(edge_id)s, %(name)s, %(root)s, %(protocols)s,"
                    " %(nfs_sec)s, %(nfs_clients)s, %(root_squash)s, %(host_trust_override)s,"
                    " %(owners_group_sid)s, %(writers_group_sid)s, %(readers_group_sid)s,"
                    " %(on_open)s, %(wait_threshold_minutes)s, %(rewind_depth_days)s,"
                    " %(rewind_depth_max_days)s, %(smb_encrypt)s)"
                    " ON CONFLICT (edge_id, name) DO NOTHING RETURNING *",
                    {
                        **f,
                        "dataset_id": dataset_id,
                        "edge_id": ds["edge_id"],
                        "root": target,
                    },
                )
            ).fetchone()
        except psycopg.errors.RaiseException as e:  # no free export id on the edge
            raise WebError("unavailable", str(e).splitlines()[0]) from e
        except psycopg.errors.UniqueViolation as e:  # a concurrent add took the name
            raise WebError("name_taken", f"the edge already has a share named {f['name']}") from e
        if row is None:
            raise WebError("name_taken", f"the edge already has a share named {f['name']}")
        details = {
            "share_id": str(row["share_id"]),
            "dataset_id": str(dataset_id),
            "name": f["name"],
            "protocols": f["protocols"],
            "nfs_sec": f["nfs_sec"],
            "trust_level": row["trust_level"],
            "host_trust_override": f["host_trust_override"],
            "nfs_export_id": row["nfs_export_id"],
            "path": "/" + "/".join(names),
        }
        await audit(db, ctx.who, "share.add", details=details)
        view = await share_view(ctx, db, row)
    return WebResult(201, view)


async def remove_share(ctx: Ctx) -> WebResult:
    """`DELETE /v1/shares/{share_id}`: the share stops being served. Only the
    share goes: the dataset, its files and their versions stay, and a new
    share can serve them again. Not a WORM share (decision 31), and not a
    root share while shares of its subfolders need its folder on the edge."""
    _admin(ctx.who)
    share_id = ctx.path_id("share_id")
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        s = await (
            await db.execute(
                "SELECT s.*, (SELECT file_id FROM catalog.file WHERE dataset_id = s.dataset_id"
                "   AND parent_id IS NULL) AS dataset_root"
                " FROM catalog.share s WHERE s.share_id = %s FOR UPDATE",
                (share_id,),
            )
        ).fetchone()
        if s is None:
            raise not_found("share")
        if_match(ctx.request, s["entity_version"])
        if s["worm_enabled"]:
            raise WebError(
                "worm_violation",
                "a WORM share is never removed: its retention protects what it holds",
            )
        if s["root_dir_id"] in (None, s["dataset_root"]):
            others = await (
                await db.execute(
                    "SELECT name FROM catalog.share WHERE dataset_id = %s AND share_id <> %s"
                    " ORDER BY name LIMIT 1",
                    (s["dataset_id"], share_id),
                )
            ).fetchone()
            if others is not None:
                raise WebError(
                    "share_in_use",
                    f"remove {others['name']} first: shares of the dataset's folders need the"
                    " root share's folder on the edge",
                )
        await db.execute("DELETE FROM catalog.share WHERE share_id = %s", (share_id,))
        details = {"share_id": str(share_id), "dataset_id": str(s["dataset_id"]),
                   "name": s["name"], "edge_id": str(s["edge_id"])}  # fmt: skip
        await audit(db, ctx.who, "share.remove", details=details)
    return WebResult(200, {"share_id": str(share_id), "removed": True})
