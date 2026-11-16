"""Who may read a file: the one place the read predicate is written (decisions 28 and 63).

A file's ACL row (`catalog.acl`) holds who its own ACL lets read it, as the
edge or the NAS captured it. For an annexed file that isn't the answer: a
folder above it on the NAS can keep people out (decision 63), and the captured
ACL stays as it was read, for restores and placements. Everything that asks
"may this person read this file", or "who reads this file", goes through here:

- **SQL:** `readable_sql(file_id, acl)` is `catalog.can_read_file` (migrations
  0052, 0053 and 0055): the captured ACL, then every folder above an annexed file up
  to the export's root, then its filer's answer to whether the share or export
  limits access beyond the files' permissions (decision 65: only the AD groups it
  names). Parameters `%(sid)s` and `%(groups)s` (`web/access.acl_params`);
  `readable_sql_pos` takes them as two `%s`, sid then groups. The NAS folders the
  caller passes are walked once per query (`catalog.passable_dirs`), from each
  export's root through only the folder ACLs that let the caller through, and a
  row's test is a key lookup in that set. An annex's catalog folders are narrowed
  the same way. A query that asks more than once walks once with `PASS_CTE`.
- **Sets:** `ACL_SETS` selects a captured ACL's sets, and `folders_of` the
  folders above a file. A set compared with another (a staged ACL, a share
  copy's) must pass `Folders.allow` too.
- **In Python:** `may_read` is `catalog.can_read_file`, for tests.

The effective read set is never wider than the captured one: the folders only
take people away (`tests/catalog/test_read_set.py` checks it as a property).
`tests/catalog/test_read_set_lint.py` fails on any other caller of
`acl_can_read` or reader of `read_sids`.

On SMB the folders' check is strict. Windows grants "bypass traverse checking"
to everyone by default and Samba honours it, so a user a folder's DACL keeps
out may still open a file below it by its full path. OmniFile removes them
anyway: it may under-show, never over-show (the owner, 2026-11-07).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

EVERYONE = "S-1-1-0"

#: A listing's test, written inline: `catalog.can_read_file` (migration 0055) is
#: the same for one file, but a function call a row cost three times the test. The
#: captured ACL, then the file's (or catalog folder's) NAS folder among those the
#: caller passes, walked once per query: `{pass_}` is a sub-select that doesn't
#: refer to the row, so it is evaluated once (an InitPlan), and it appears once.
#: A file no annex holds has no folder (true); an annexed one whose folder isn't
#: set (NULL) is nobody's. A property test checks it against the function.
_READ = (
    "(coalesce(catalog.acl_can_read({a}.read_sids, {a}.deny_sids, {sid}, {groups},"
    " {a}.deny_first), false)"
    " AND coalesce((SELECT coalesce({pass_} ? d.dir_id::text, false)"
    " FROM (SELECT x.dir_id FROM catalog.annex_file x WHERE x.file_id = {f}"
    " UNION ALL SELECT y.dir_id FROM catalog.annex_folder y WHERE y.file_id = {f}"
    " LIMIT 1) d), true))"
)
_READ_ONE = (
    "catalog.can_read_file({a}.read_sids, {a}.deny_sids, {a}.deny_first, {f}, {sid}, {groups})"
)
_WALK = "(SELECT catalog.passable_dirs({sid}, {groups}))"

#: For a query that asks more than once (the share map's folders, then its files):
#: put `PASS_CTE` in its WITH and give `readable_sql(..., walked=True)`, so the
#: folders are walked once for the whole query.
PASS_CTE = (
    "pass AS MATERIALIZED (SELECT catalog.passable_dirs(%(sid)s::text, %(groups)s::text[]) AS p)"
)


def readable_sql(file_id: str, acl: str = "acl", walked: bool = False) -> str:
    """SQL: may the caller read the file `file_id` (an expression) whose ACL is
    the row `acl`? False when `acl` is NULL. For a whole share's count or listing:
    the folders the caller passes are walked once, whatever it takes. One item
    takes `readable_row_sql`, a page `readable_page_sql` or `readable_in_folder_sql`.
    `walked`: the query's WITH has `PASS_CTE`."""
    sid, groups = "%(sid)s::text", "%(groups)s::text[]"
    pass_ = "(SELECT p FROM pass)" if walked else _WALK.format(sid=sid, groups=groups)
    return _READ.format(a=acl, f=file_id, sid=sid, groups=groups, pass_=pass_)


#: A page's walk gives up past this many folder ACLs or folders the caller passes,
#: and each row then checks its own chain. A row's chain costs about 140 to 210 us,
#: and the walk about 4 us a folder (results/wp14-read-set-scale.md): at 10^4
#: folders, 20 to 50 ms, the cost of a page of about 100 rows checked on their own.
PAGE_WALK_MAX = 10_000

_ROW_DIR = (
    "(SELECT x.dir_id FROM catalog.annex_file x WHERE x.file_id = {f}"
    " UNION ALL SELECT y.dir_id FROM catalog.annex_folder y WHERE y.file_id = {f}"
    " LIMIT 1) d"
)
_CAPTURED = (
    "coalesce(catalog.acl_can_read({a}.read_sids, {a}.deny_sids, {sid}, {groups},"
    " {a}.deny_first), false)"
)
_PAGE = (
    "(" + _CAPTURED + " AND coalesce((SELECT CASE WHEN w.p IS NOT NULL"
    " THEN coalesce(w.p ? d.dir_id::text, false)"
    " ELSE catalog.dir_passes(d.dir_id, {sid}, {groups}) END"
    " FROM (SELECT (SELECT catalog.passable_dirs({sid}, {groups}, {max})) AS p) w,"
    " " + _ROW_DIR + "), true))"
)
_IN_FOLDER = (
    "(" + _CAPTURED + " AND coalesce((SELECT CASE WHEN d.dir_id = ({pd})"
    " THEN (SELECT catalog.dir_passes(({pd}), {sid}, {groups}))"
    " ELSE catalog.dir_passes(d.dir_id, {sid}, {groups}) END"
    " FROM " + _ROW_DIR + "), true))"
)
#: The annex folder a catalog folder's children are in: an annexed one's among its
#: first 64 children (a folder's are all in one), so the lookup costs the same in a
#: folder of a million. None among them: each row checks its own chain.
_FOLDER_DIR = (
    "SELECT CASE WHEN x.file_id IS NOT NULL THEN x.dir_id ELSE y.dir_id END"
    " FROM (SELECT c.file_id FROM catalog.file c WHERE c.parent_id = {folder}"
    " ORDER BY c.name LIMIT 64) c"
    " LEFT JOIN catalog.annex_file x ON x.file_id = c.file_id"
    " LEFT JOIN catalog.annex_folder y ON y.file_id = c.file_id"
    " WHERE x.dir_id IS NOT NULL OR y.dir_id IS NOT NULL LIMIT 1"
)


def readable_group_sql(annexed: str, dir_id: str, acl: str = "acl") -> str:
    """`readable_sql` for a group of the share map's roll-up (usage/share_map.py):
    files with the captured ACL `acl`, annexed or not (`annexed`), in the annex
    folder `dir_id`. The same test each of its files would get, so the group
    counts for the caller exactly when they do. The query's WITH has `PASS_CTE`."""
    sid, groups = "%(sid)s::text", "%(groups)s::text[]"
    captured = _CAPTURED.format(a=acl, sid=sid, groups=groups)
    return (
        f"({captured} AND (NOT {annexed}"
        f" OR coalesce((SELECT p FROM pass) ? {dir_id}::text, false)))"
    )


def readable_page_sql(file_id: str, acl: str = "acl", walk_max: int = PAGE_WALK_MAX) -> str:
    """`readable_sql` for a page of rows from many folders (an annex's files, its
    events, the audit log): the walk, once, if the caller passes no more than
    `PAGE_WALK_MAX` folders; else each row's own chain. Either way a page costs
    at most about a walk of that size, or its rows' chains."""
    sid, groups = "%(sid)s::text", "%(groups)s::text[]"
    return _PAGE.format(a=acl, f=file_id, sid=sid, groups=groups, max=int(walk_max))


def readable_in_folder_sql(file_id: str, folder: str, acl: str = "acl") -> str:
    """`readable_sql` for the children of one catalog folder, `folder` (an
    expression that doesn't refer to the row, such as `%(d)s`): they are all in
    one NAS folder, whose chain is checked once; a row in any other (which
    shouldn't be) checks its own. No walk, and a page of hidden rows costs little."""
    sid, groups = "%(sid)s::text", "%(groups)s::text[]"
    pd = _FOLDER_DIR.format(folder=folder)
    return _IN_FOLDER.format(a=acl, f=file_id, sid=sid, groups=groups, pd=pd)


def readable_row_sql(file_id: str, acl: str = "acl") -> str:
    """`readable_sql` for one file, or a few: the file's own chain of folders, a
    few index lookups a row, and no walk. For a page of up to `ROW_PAGE` rows, or
    a file's chain of ancestors. Parameters as `readable_sql`."""
    return _READ_ONE.format(a=acl, f=file_id, sid="%(sid)s::text", groups="%(groups)s::text[]")


def readable_sql_pos(file_id: str, acl: str = "acl") -> str:
    """`readable_sql` with positional parameters: the user's SID, then its groups.
    For a check of one file: its own chain of folders, no walk."""
    return _READ_ONE.format(a=acl, f=file_id, sid="%s::text", groups="%s::text[]")


def readable_by_sql(file_id: str, acl: str, sid: str, groups: str) -> str:
    """`readable_sql` for a reader given as SQL expressions (`i.sid::text`, and
    `i.group_sids::text[]`), such as a joined identity: the reader changes from
    row to row, so each row checks its own chain of folders."""
    return _READ_ONE.format(a=acl, f=file_id, sid=sid, groups=groups)


#: A captured ACL's sets, for comparisons that `Folders.allow` completes.
ACL_SETS = (
    "{a}.read_sids::text[] AS {p}read_sids, {a}.deny_sids::text[] AS {p}deny_sids,"
    " {a}.deny_first AS {p}deny_first"
)


def acl_sets(alias: str, prefix: str = "") -> str:
    return ACL_SETS.format(a=alias, p=prefix)


def captured(row: dict[str, Any], prefix: str = "") -> tuple[set[str], set[str], bool]:
    """(read, deny, deny_first) of a captured ACL a row selected with `acl_sets`.
    Who the ACL itself lets read: for an annexed file, `Folders.allow` takes away
    those its folders keep out."""
    return (
        set(row[f"{prefix}read_sids"] or ()),
        set(row[f"{prefix}deny_sids"] or ()),
        bool(row.get(f"{prefix}deny_first")),
    )


@dataclass(frozen=True)
class Folder:
    """A folder above an annexed file, as the edge last read it."""

    path: str
    #: The ACL's hash: two folders with the same one let the same people through.
    sha256: bytes | None
    #: Who may pass through (`catalog.acl.search_sids`); None: not known.
    search: frozenset[str] | None
    deny: frozenset[str] = frozenset()
    deny_first: bool = False
    error: str | None = None

    @property
    def known(self) -> bool:
        return self.error is None and self.search is not None

    @property
    def open(self) -> bool:
        """Everyone passes."""
        return self.known and EVERYONE in (self.search or ()) and not self.deny

    def admits(self, user: str, groups: Iterable[str]) -> bool:
        if not self.known:
            return False
        return can_read(list(self.search or ()), list(self.deny), user, list(groups),
                        self.deny_first)  # fmt: skip


@dataclass(frozen=True)
class FilerAccess:
    """Decision 65: whether the filer's share or export limits access beyond the files'
    permissions, as an ArchiveAdmin answered. `limited` None: not answered."""

    limited: bool | None
    groups: frozenset[str] = frozenset()

    def admits(self, user: str, groups: Iterable[str]) -> bool:
        if self.limited is None:
            return False
        if not self.limited:
            return True
        return user in self.groups or bool(set(groups) & self.groups)


@dataclass
class Folders:
    """The folders above a file, and its filer's answer: none apply to a file no annex
    holds."""

    annexed: bool
    #: The export's root first. Missing ones are rows with `search` None.
    rows: list[Folder] = field(default_factory=list)
    #: Decision 65. None for an annexed file: not loaded, so nobody passes.
    filer: FilerAccess | None = None

    @property
    def closed(self) -> str | None:
        """Why nobody passes, or None."""
        if not self.annexed:
            return None
        if self.filer is None or self.filer.limited is None:
            return "the filer's access question hasn't been answered"
        for r in self.rows:
            if r.error is not None:
                return f"the ACL of folder {r.path or '/'!r} can't be read"
            if r.search is None:
                return f"folder {r.path or '/'!r} hasn't been read yet"
        return None

    def admits(self, user: str, groups: Iterable[str]) -> bool:
        if not self.annexed:
            return True
        g = list(groups)
        if self.filer is None or not self.filer.admits(user, g):
            return False
        return all(r.admits(user, g) for r in self.rows)

    def allow(self, read: Iterable[str], covered: Iterable[bytes] = ()) -> bool:
        """Whether everyone a set of SIDs grants (a staged ACL's, a share
        copy's) gets through every folder. Fail closed: a folder passes a SID
        only if it denies nobody and grants that SID or Everyone, so a member
        can't be kept out by a group the set doesn't name. `covered`: hashes of
        folders the new file sits below too (a placement on the NAS), which
        keep out the same people wherever they are."""
        if not self.annexed:
            return True
        if self.closed is not None:
            return False
        sids = set(read)
        # Decision 65: a share or export that lets in only some groups lets through
        # a SID's members only if that SID is one of them.
        if self.filer is not None and self.filer.limited and not sids <= self.filer.groups:
            return False
        cover = set(covered)
        for r in self.rows:
            if r.open or (r.sha256 is not None and r.sha256 in cover):
                continue
            if r.deny or EVERYONE in sids:
                return False
            if not sids <= set(r.search or ()):
                return False
        return True


def can_read(read: list[str], deny: list[str], user: str, groups: list[str],
             deny_first: bool = False) -> bool:  # fmt: skip
    """`catalog.acl_can_read` in Python, both orders."""
    if not deny_first:
        if user in deny:
            return False
        if user in read:
            return True
        if set(groups) & (set(read) - {EVERYONE}):
            return True
        if set(groups) & set(deny):
            return False
        return EVERYONE in read
    if user in deny or set(groups) & set(deny) or EVERYONE in deny:
        return False
    if user in read or set(groups) & set(read):
        return True
    return EVERYONE in read


def may_read(acl: dict[str, Any] | None, folders: Folders, user: str, groups: list[str]) -> bool:
    """`catalog.can_read_file` in Python: `acl` has read_sids, deny_sids and deny_first."""
    if acl is None:
        return False
    ok = can_read(list(acl["read_sids"] or []), list(acl["deny_sids"] or []), user, groups,
                  bool(acl.get("deny_first")))  # fmt: skip
    return ok and folders.admits(user, groups)


def group_readable(read: Iterable[str], owner: str | None) -> bool:
    """Decision 64: whether an ACL lets anyone but the file's owner read it (a group,
    a named user, or Everyone): on a filer that shows no ACLs, the captured read set
    such a grant copies may be wider than the NAS's."""
    return any(s != owner for s in read)


#: The flag of the filer an annexed file was captured from: hidden and not acknowledged.
ACLS_HIDDEN_SQL = """
SELECT fl.acls_hidden_since IS NOT NULL AND fl.acls_hidden_ack_at IS NULL AS blocked
  FROM catalog.annex_file af
  JOIN catalog.annex a ON a.annex_id = af.annex_id
  JOIN catalog.filer fl ON fl.filer_id = a.filer_id
 WHERE af.file_id = %s
"""


async def acls_hidden_blocks(db: Any, file_id: int) -> bool:
    """Whether new grants from this annexed file are blocked (decision 64)."""
    row = await (await db.execute(ACLS_HIDDEN_SQL, (file_id,))).fetchone()
    if row is None:
        return False
    return bool(row["blocked"] if isinstance(row, dict) else row[0])


FOLDERS_SQL = """
SELECT d.path, n.error, s.sha256, s.search_sids::text[] AS search,
       s.search_deny_sids::text[] AS search_deny, coalesce(s.deny_first, false) AS deny_first
  FROM catalog.annex_file af
  JOIN catalog.annex a ON a.annex_id = af.annex_id
 CROSS JOIN LATERAL unnest(catalog.annex_folders(a.path, af.path)) WITH ORDINALITY AS d (path, i)
  LEFT JOIN catalog.nas_folder_acl n ON n.filer_id = a.filer_id AND n.path = d.path
  LEFT JOIN catalog.acl s ON s.acl_id = n.acl_id
 WHERE af.file_id = %s
 ORDER BY d.i
"""


def _folder(r: dict[str, Any]) -> Folder:
    return Folder(
        r["path"],
        bytes(r["sha256"]) if r["sha256"] is not None else None,
        frozenset(r["search"]) if r["search"] is not None else None,
        frozenset(r["search_deny"] or ()),
        bool(r["deny_first"]),
        r["error"],
    )


FILER_SQL = """
SELECT fl.access_limited, fl.access_groups::text[] AS access_groups
  FROM catalog.annex_file af
  JOIN catalog.annex a ON a.annex_id = af.annex_id
  JOIN catalog.filer fl ON fl.filer_id = a.filer_id
 WHERE af.file_id = %s
"""


async def folders_of(db: Any, file_id: int) -> Folders:
    """The folders above `file_id` on its NAS, and its filer's answer, if an annex
    holds it. `db` returns dict rows."""
    rows = await (await db.execute(FOLDERS_SQL, (file_id,))).fetchall()
    if not rows:
        return Folders(annexed=False)
    f = await (await db.execute(FILER_SQL, (file_id,))).fetchone()
    filer = None if f is None else FilerAccess(f["access_limited"],
                                               frozenset(f["access_groups"] or ()))  # fmt: skip
    return Folders(annexed=True, rows=[_folder(r) for r in rows], filer=filer)


#: Per annexed file: the folders above it that don't let everyone through, or that
#: haven't been read, the export's root first. Who may read the file is narrowed by
#: these (the portal says so beside the file).
NARROWING_SQL = """
SELECT af.file_id,
       array_agg(d.path ORDER BY d.i) FILTER (
           WHERE NOT (n.error IS NULL AND s.search_sids IS NOT NULL
                      AND 'S-1-1-0' = ANY (s.search_sids::text[])
                      AND cardinality(coalesce(s.search_deny_sids, '{}')) = 0)) AS narrow
  FROM catalog.annex_file af
  JOIN catalog.annex a ON a.annex_id = af.annex_id
 CROSS JOIN LATERAL unnest(catalog.annex_folders(a.path, af.path)) WITH ORDINALITY AS d (path, i)
  LEFT JOIN catalog.nas_folder_acl n ON n.filer_id = a.filer_id AND n.path = d.path
  LEFT JOIN catalog.acl s ON s.acl_id = n.acl_id
 WHERE af.file_id = ANY(%s)
 GROUP BY af.file_id
"""


async def narrowing(db: Any, file_ids: list[int]) -> dict[int, list[str]]:
    """`NARROWING_SQL` for a page of files: file id -> folder paths ("" is the export's root)."""
    if not file_ids:
        return {}
    rows = await (await db.execute(NARROWING_SQL, (file_ids,))).fetchall()
    return {r["file_id"]: list(r["narrow"] or []) for r in rows}


async def nas_folders(db: Any, filer_id: int, folder: str) -> list[Folder]:
    """The folders a file placed in `folder` (export-relative) on a filer sits
    below, as the edge last read them, the export's root first. Only those
    read without an error."""
    parts = folder.split("/") if folder else []
    paths = ["/".join(parts[:i]) for i in range(len(parts) + 1)]
    rows = await (
        await db.execute(
            "SELECT n.path, n.error, s.sha256, s.search_sids::text[] AS search,"
            " s.search_deny_sids::text[] AS search_deny,"
            " coalesce(s.deny_first, false) AS deny_first"
            "  FROM catalog.nas_folder_acl n LEFT JOIN catalog.acl s ON s.acl_id = n.acl_id"
            " WHERE n.filer_id = %s AND n.path = ANY(%s) AND n.gone_at IS NULL",
            (filer_id, paths),
        )
    ).fetchall()
    return [f for f in (_folder(r) for r in rows) if f.known]
