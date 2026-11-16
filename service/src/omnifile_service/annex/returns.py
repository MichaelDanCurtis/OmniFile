"""Where a recalled annex file may be placed (annex.md sections 3.6, 4.5 and
4.6; phase A3.1).

The return rules (`recall_completed`, `annex/rules.py`) are the service's to
evaluate, here, both for the builder's preview and for real: their inputs
(the removed file as the catalog knows it, and who asks) are the service's.
The edge never evaluates them; it places a file only on a ticket (A3.3).

- `decide`: the first matching return rule for a file and a requester.
- `resolve`: a location, for this file, as a concrete target: a folder of a
  filer (relative to its export) or of a share, and a name.
- `write_check`: whether the requester may add a file there (section 4.6:
  the edge writes with its own account, so it must never write where the
  requester couldn't). A NAS folder is checked against its captured ACL
  (`catalog.nas_folder_acl`, `acl_can_write`, deny-first); a share folder
  against the share's Owners and Writers groups and the folder's POSIX ACL.
  Anything not known is a no.
- `targets_for`: what a requester may pick for a file, default first.
- `preview`: per return rule and target, over the annex's removed files.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass, field
from typing import Any

from ..api.acls import access_sets, can_read
from ..catalog.read_set import acl_sets, captured
from . import rules as annex_rules

#: Files a preview looks at, at most (it says when it stopped).
PREVIEW_MAX_FILES = 200_000
#: Removed files can be placed back where they came from; a file the NAS
#: lost in Copy mode (`gone`) too. A file still on the NAS can go only
#: elsewhere.
ORIGIN_STATES = ("removed", "gone")


@dataclass
class Facts:
    """A removed file, as the catalog knows it."""

    file_id: int
    annex_id: int
    #: Relative to the annex's root.
    path: str
    size: int
    nas_state: str
    owner_sid: str | None = None
    group_sid: str | None = None

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def folder(self) -> str:
        return posixpath.dirname(self.path)


@dataclass
class Requester:
    sid: str | None
    groups: list[str] = field(default_factory=list)

    @property
    def sids(self) -> set[str]:
        return {x for x in [self.sid, *self.groups] if x}


def _cond_match(c: dict[str, Any], f: Facts, who: Requester | None) -> bool | None:
    """True, False, or None (depends on who asks, and nobody is named)."""
    if c.get("path") and not any(annex_rules.glob_match(g, f.path) for g in c["path"]):
        return False
    if c.get("extension") and not annex_rules.ext_match(c["extension"], f.path):
        return False
    if "size_gte" in c and f.size < c["size_gte"]:
        return False
    if "size_lt" in c and f.size >= c["size_lt"]:
        return False
    if c.get("owner_sids") and f.owner_sid not in c["owner_sids"]:
        return False
    if c.get("group_sids") and f.group_sid not in c["group_sids"]:
        return False
    if c.get("requester_in"):
        if who is None:
            return None
        if not who.sids & set(c["requester_in"]):
            return False
    return True


def decide(body: dict[str, Any], f: Facts, who: Requester) -> dict[str, Any] | None:
    """The first return rule that matches, with what it says; None if the
    rule set has no return rule for it (placed nowhere)."""
    for r in annex_rules.returns(body.get("rules", [])):
        if _cond_match(r["conditions"], f, who):
            return decision_of(r)
    return None


def decision_of(r: dict[str, Any]) -> dict[str, Any]:
    get = annex_rules.rule_get
    return {
        "rule": r["id"],
        "exclude": bool(get(r, "exclude")),
        "default": get(r, "place_at"),
        "choices": list(get(r, "let_requester_choose", [])),
        "on_conflict": get(r, "on_conflict", "restored_name"),
        "hold_days": get(r, "hold_days", 0),
        "notify": get(r, "notify", []),
    }


def _join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


@dataclass
class Target:
    """A concrete place: `nas` (a filer folder, relative to its export) or
    `share` (a folder of a share), and a file name."""

    kind: str
    folder: str
    name: str
    filer_id: int | None = None
    share_id: int | None = None
    #: For the loop checks: the annex whose folder it is, and the folder
    #: below that annex's root (None for the file's own folder).
    annex_id: int | None = None
    origin: bool = False

    def json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "folder": self.folder, "name": self.name}
        if self.filer_id is not None:
            out["filer_id"] = str(self.filer_id)
        if self.share_id is not None:
            out["share_id"] = str(self.share_id)
        if self.origin:
            out["origin"] = True
        return out

    @property
    def path(self) -> str:
        return _join(self.folder, self.name)


async def resolve(db, loc: dict[str, Any], f: Facts, annex: dict[str, Any]) -> Target | None:
    """`loc` for this file; None when it names nothing that exists."""
    rel = f.folder if loc.get("keep_relative_path", True) else ""
    if loc["kind"] == "share_folder":
        return Target("share", _join(loc["path"], rel), f.name, share_id=int(loc["share_id"]))
    if loc.get("origin"):
        return Target("nas", _join(annex["path"], f.folder), f.name,
                      filer_id=annex["filer_id"], annex_id=annex["annex_id"],
                      origin=True)  # fmt: skip
    if "annex_id" in loc:
        other = await (
            await db.execute(
                "SELECT annex_id, filer_id, path FROM catalog.annex"
                " WHERE annex_id = %s AND state <> 'retired'",
                (int(loc["annex_id"]),),
            )
        ).fetchone()
        if other is None:
            return None
        return Target("nas", _join(other["path"], loc["path"], rel), f.name,
                      filer_id=other["filer_id"], annex_id=other["annex_id"])  # fmt: skip
    if "filer_id" in loc:
        return Target("nas", _join(loc["path"], rel), f.name, filer_id=int(loc["filer_id"]))
    return Target("nas", _join(annex["path"], loc["path"], rel), f.name,
                  filer_id=annex["filer_id"], annex_id=annex["annex_id"])  # fmt: skip


def location_root(loc: dict[str, Any], annex: dict[str, Any]) -> str | None:
    """A NAS location's own folder, relative to the export (what the edge
    reports free space for); None for the origin and shares."""
    if loc["kind"] != "nas_folder" or loc.get("origin"):
        return None
    if "filer_id" in loc:
        return loc["path"]
    if "annex_id" in loc:
        return None
    return _join(annex["path"], loc["path"])


def within(loc: dict[str, Any], chosen: dict[str, Any]) -> bool:
    """`chosen` is `loc`, or a folder below it ("a folder of my choice")."""
    if chosen.get("kind") != loc["kind"] or bool(chosen.get("origin")) != bool(loc.get("origin")):
        return False
    if loc.get("origin"):
        return True
    for k in ("annex_id", "filer_id", "share_id"):
        if chosen.get(k) != loc.get(k):
            return False
    if chosen.get("keep_relative_path", True) != loc.get("keep_relative_path", True):
        return False
    p, c = loc["path"], chosen["path"]
    return p == c or p == "" or c.startswith(p + "/")


# --- may the requester add a file there? ----------------------------------------------


async def write_check(db, t: Target, who: Requester) -> tuple[bool, str | None]:
    """(allowed, why not). Fails toward "can't place it here"."""
    if not who.sid:
        return False, "the requester has no AD identity"
    if t.kind == "nas":
        return await _nas_write(db, t, who)
    return await _share_write(db, t, who)


async def _nas_write(db, t: Target, who: Requester) -> tuple[bool, str | None]:
    # The deepest folder on the target's path that the edge has read: the
    # file goes in the target folder itself, or in folders the placement
    # makes below that one, which inherit its ACL (P0).
    parts = t.folder.split("/") if t.folder else []
    candidates = ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]
    rows = await (
        await db.execute(
            "SELECT n.path, n.error, a.write_sids, a.write_deny_sids, a.deny_first"
            "  FROM catalog.nas_folder_acl n LEFT JOIN catalog.acl a ON a.acl_id = n.acl_id"
            " WHERE n.filer_id = %s AND n.path = ANY(%s) AND n.gone_at IS NULL",
            (t.filer_id, candidates),
        )
    ).fetchall()
    by = {r["path"]: r for r in rows}
    deepest = next((by[p] for p in candidates if p in by), None)
    if t.origin and t.folder not in by:
        return False, "its folder is no longer on the NAS"
    if deepest is None:
        return False, "the edge hasn't read that folder's ACL yet"
    if deepest["error"] is not None or deepest["write_sids"] is None:
        return False, "that folder's ACL can't be read"
    ok = await (
        await db.execute(
            "SELECT catalog.acl_can_write(%s::catalog.sid[], %s::catalog.sid[], %s, %s, %s) AS ok",
            (
                deepest["write_sids"],
                deepest["write_deny_sids"],
                who.sid,
                list(who.groups),
                deepest["deny_first"],
            ),
        )  # fmt: skip
    ).fetchone()
    if not ok["ok"]:
        return False, "the requester can't add files to that folder on the NAS"
    return True, None


async def _share_write(db, t: Target, who: Requester) -> tuple[bool, str | None]:
    s = await (
        await db.execute("SELECT * FROM catalog.share WHERE share_id = %s", (t.share_id,))
    ).fetchone()
    if s is None:
        return False, "no such share"
    if not {s["owners_group_sid"], s["writers_group_sid"]} & who.sids:
        return False, "the requester isn't an Owner or Writer of that share"
    # The deepest existing folder of the share on the target's path, and its
    # POSIX ACL (decision 28): w and x to add a file.
    cur = s["root_dir_id"]
    if cur is None:
        root = await (
            await db.execute(
                "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL",
                (s["dataset_id"],),
            )
        ).fetchone()
        cur = root["file_id"] if root else None
    if cur is None:
        return False, "the share has no root folder"
    for name in [p for p in t.folder.split("/") if p]:
        row = await (
            await db.execute(
                "SELECT file_id, kind FROM catalog.file WHERE parent_id = %s AND name = %s"
                " AND deleted_at IS NULL",
                (cur, name),
            )
        ).fetchone()
        if row is None:
            break
        if row["kind"] != "dir":
            return False, f"{name} is a file in that share"
        cur = row["file_id"]
    acl = await (
        await db.execute(
            "SELECT a.owner_sid::text AS owner_sid, a.group_sid::text AS group_sid, a.entries"
            "  FROM catalog.file f JOIN catalog.acl a ON a.acl_id = f.acl_id"
            " WHERE f.file_id = %s",
            (cur,),
        )
    ).fetchone()
    if acl is None:
        return False, "that share folder's ACL isn't known"
    write, deny = access_sets(acl["owner_sid"], acl["group_sid"], acl["entries"], need=3)
    if not can_read(write, deny, who.sid or "", list(who.groups)):
        return False, "the requester can't add files to that share folder"
    return True, None


async def share_acl_problem(db, file_id: int) -> str | None:
    """Why the file's own ACL can't be carried into a share, or None.

    A recalled copy in a share keeps the file's own read set (section 4.6),
    in the share's model: POSIX entries naming SIDs (decision 28). A POSIX
    ACL from an NFSv3 NAS carries over as it is. An NT or NFSv4 ACL carries
    over as its read set, which is exact only without denies: POSIX can't
    make a group's deny win over another group's allow, so a copy of such a
    file could be read by someone the NAS refused. It isn't offered."""
    row = await (
        await db.execute(
            f"SELECT a.raw_kind, {acl_sets('a')}"
            "  FROM catalog.file f JOIN catalog.acl a"
            " ON a.acl_id = f.acl_id WHERE f.file_id = %s",
            (file_id,),
        )
    ).fetchone()
    if row is None:
        return "the file's ACL isn't known"
    if row["raw_kind"] == "posix":
        return None
    if captured(row)[1]:
        return "its NAS ACL denies some readers, which a share's ACL can't express"
    return None


# --- display ---------------------------------------------------------------------------


async def display(db, t: Target) -> str:
    """Where it lands, as a person would type it: `\\\\host\\share\\a\\b` over
    SMB, `host:/export/a/b` over NFS, or the share's name and path."""
    if t.kind == "share":
        s = await (
            await db.execute("SELECT name FROM catalog.share WHERE share_id = %s", (t.share_id,))
        ).fetchone()
        return f"{s['name'] if s else t.share_id}:/{t.path}"
    f = await (
        await db.execute(
            "SELECT host, protocol, export FROM catalog.filer WHERE filer_id = %s", (t.filer_id,)
        )
    ).fetchone()
    if f is None:
        return t.path
    if f["protocol"] == "smb":
        return "\\\\" + "\\".join([f["host"], f["export"], *[p for p in t.path.split("/") if p]])
    return f"{f['host']}:/{_join(f['export'], t.path)}"


# --- what a requester may pick -------------------------------------------------------------


async def share_lookup(db, share: dict[str, Any], path: str) -> dict[str, Any] | None:
    """The live catalog item at `path` below the share's root, if any."""
    cur = share["root_dir_id"]
    if cur is None:
        root = await (
            await db.execute(
                "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL",
                (share["dataset_id"],),
            )
        ).fetchone()
        cur = root["file_id"] if root else None
    row: dict[str, Any] | None = None
    for name in [p for p in path.split("/") if p]:
        if cur is None:
            return None
        row = await (
            await db.execute(
                "SELECT file_id, kind, acl_id FROM catalog.file WHERE parent_id = %s"
                " AND name = %s AND deleted_at IS NULL",
                (cur, name),
            )
        ).fetchone()
        if row is None:
            return None
        cur = row["file_id"]
    return row


async def name_taken(db, t: Target) -> bool | None:
    """Whether the target's name is taken now, as far as the catalog knows
    (a NAS name OmniFile has seen, or a share file); None if it can't tell."""
    if t.kind == "share":
        s = await (
            await db.execute("SELECT * FROM catalog.share WHERE share_id = %s", (t.share_id,))
        ).fetchone()
        if s is None:
            return None
        return await share_lookup(db, s, t.path) is not None
    if t.annex_id is None:
        return None
    a = await (
        await db.execute("SELECT path FROM catalog.annex WHERE annex_id = %s", (t.annex_id,))
    ).fetchone()
    if a is None:
        return None
    rel = t.path[len(a["path"]) + 1 :] if a["path"] else t.path
    row = await (
        await db.execute(
            "SELECT 1 FROM catalog.annex_file WHERE annex_id = %s AND path = %s"
            " AND gone_since IS NULL AND nas_state = 'present'",
            (t.annex_id, rel),
        )
    ).fetchone()
    return row is not None


async def space(db, loc: dict[str, Any], t: Target, annex: dict[str, Any]) -> dict | None:
    if t.kind != "nas":
        return None
    root = annex["path"] if loc.get("origin") or "annex_id" in loc else location_root(loc, annex)
    row = await (
        await db.execute(
            "SELECT free_bytes, total_bytes, observed_at FROM catalog.nas_target_space"
            " WHERE filer_id = %s AND path = %s",
            (t.filer_id, root if root is not None else annex["path"]),
        )
    ).fetchone()
    return dict(row) if row else None


async def targets_for(
    db, body: dict[str, Any], f: Facts, who: Requester, annex: dict[str, Any]
) -> dict[str, Any]:
    """The decision for this file and requester, and each location it allows,
    default first: resolved, with the write check, name and free space."""
    d = decide(body, f, who)
    if d is None or d["exclude"]:
        return {"rule": d["rule"] if d else None, "targets": []}
    out = []
    for i, loc in enumerate([d["default"], *d["choices"]]):
        if loc is None:
            continue
        item: dict[str, Any] = {
            "location": loc,
            "default": i == 0,
            "on_conflict": d["on_conflict"],
            "hold_days": d["hold_days"],
        }
        if loc.get("origin") and f.nas_state not in ORIGIN_STATES:
            # Decision 83 (A3.4): with overwrite, an older version may replace it there.
            older = d["on_conflict"] == "overwrite_keep_version"
            item.update(
                allowed=False,
                why_not="the file is still on the NAS"
                + ("; an older version of it can replace it there" if older else ""),
                replace_older=older,
            )
            out.append(item)
            continue
        t = await resolve(db, loc, f, annex)
        if t is None:
            item.update(allowed=False, why_not="that location no longer exists")
            out.append(item)
            continue
        ok, why = await write_check(db, t, who)
        if ok and t.kind == "share":
            why = await share_acl_problem(db, f.file_id)
            ok = why is None
        item.update(
            target=t.json(),
            display=await display(db, t),
            allowed=ok,
            why_not=why,
            name_taken=await name_taken(db, t),
            space=await space(db, loc, t, annex),
        )
        out.append(item)
    return {"rule": d["rule"], "targets": out}


# --- the preview over removed files ---------------------------------------------------------


async def preview(db, body: dict[str, Any], annex: dict[str, Any]) -> dict[str, Any]:
    """Per return rule, the removed files it would take; per target, how
    many names are taken today and the bytes needed against free space. A
    rule naming requesters counts what it *may* take (who asks decides)."""
    rules = annex_rules.returns(body.get("rules", []))
    per: dict[str, dict[str, Any]] = {
        r["id"]: {"id": r["id"], "files": 0, "bytes": 0, "may_take": 0, "targets": {}}
        for r in rules
    }
    none = {"files": 0, "bytes": 0}
    seen = 0
    truncated = False
    cur = await db.execute(
        "SELECT af.file_id, af.path, coalesce(af.copied_size, af.size) AS size, af.nas_state,"
        "       acl.owner_sid::text AS owner_sid, acl.group_sid::text AS group_sid"
        "  FROM catalog.annex_file af JOIN catalog.file f ON f.file_id = af.file_id"
        "  LEFT JOIN catalog.acl acl ON acl.acl_id = f.acl_id"
        " WHERE af.annex_id = %s AND af.nas_state = ANY(%s) AND af.purged_at IS NULL"
        "   AND af.purge_requested_at IS NULL"
        " ORDER BY af.path LIMIT %s",
        (annex["annex_id"], list(ORIGIN_STATES), PREVIEW_MAX_FILES + 1),
    )
    rows = await cur.fetchall()
    if len(rows) > PREVIEW_MAX_FILES:
        truncated, rows = True, rows[:PREVIEW_MAX_FILES]
    for row in rows:
        seen += 1
        f = Facts(row["file_id"], annex["annex_id"], row["path"], int(row["size"] or 0),
                  row["nas_state"], row["owner_sid"], row["group_sid"])  # fmt: skip
        taken = False
        for r in rules:
            m = _cond_match(r["conditions"], f, None)
            if m is False:
                continue
            p = per[r["id"]]
            if m is None:
                p["may_take"] += 1
                continue
            p["files"] += 1
            p["bytes"] += f.size
            d = decision_of(r)
            if d["default"] is not None:
                t = await resolve(db, d["default"], f, annex)
                key = json.dumps(d["default"], sort_keys=True)
                tg = p["targets"].setdefault(
                    key, {"location": d["default"], "files": 0, "bytes": 0, "names_taken": 0}
                )
                tg["files"] += 1
                tg["bytes"] += f.size
                if t is not None and await name_taken(db, t):
                    tg["names_taken"] += 1
            taken = True
            break
        if not taken:
            none["files"] += 1
            none["bytes"] += f.size
    out_rules = []
    for r in rules:
        p = per[r["id"]]
        tlist = []
        for tg in p["targets"].values():
            loc = tg["location"]
            root = None if loc["kind"] != "nas_folder" else (
                annex["path"] if loc.get("origin") or "annex_id" in loc
                else location_root(loc, annex)
            )  # fmt: skip
            fid = int(loc["filer_id"]) if "filer_id" in loc else annex["filer_id"]
            sp = None
            if root is not None:
                sp = await (
                    await db.execute(
                        "SELECT free_bytes, total_bytes FROM catalog.nas_target_space"
                        " WHERE filer_id = %s AND path = %s",
                        (fid, root),
                    )
                ).fetchone()
            tg["free_bytes"] = sp["free_bytes"] if sp else None
            tlist.append(tg)
        p["targets"] = tlist
        out_rules.append(p)
    return {"files": seen, "truncated": truncated, "rules": out_rules, "placed_nowhere": none}
