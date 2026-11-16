"""Decision 63: who may read an annexed file takes the folders above it into account.

Properties, over seeded random worlds (the seed is in each assertion's message):

- the effective answer (`catalog.can_read_file`) is never wider than the captured ACL's
  (`catalog.acl_can_read` alone): the folders only take people away;
- it is exactly the captured ACL and every folder up to the export's root, as the Python
  model `read_set.may_read` says;
- `Folders.allow`, the set check placements and share copies use, never lets through a
  SID whose members a folder would keep out.

Then the behaviour through the API: the 0644 file under a 0700 folder of #70's review,
fail closed until the folders are read, an unreadable ancestor, SMB's strict traverse,
a gone folder, and the annex's `folders_read`.
"""

from __future__ import annotations

import random
import uuid

import pytest

from omnifile_service.catalog.read_set import (
    EVERYONE,
    FilerAccess,
    Folder,
    Folders,
    can_read,
    may_read,
    readable_in_folder_sql,
    readable_page_sql,
    readable_row_sql,
    readable_sql,
)

from .test_annex import (  # noqa: F401 - the fixture
    GEO,
    OWNERS,
    READ,
    TRAVERSE,
    ace,
    annex,
    arrived,
    events,
    nt_acl,
)

USERS = [f"S-1-5-21-1-2-3-{1100 + i}" for i in range(5)]
GROUPS = [f"S-1-5-21-1-2-3-{2100 + i}" for i in range(4)]
SEEDS = range(1, 401)


def _set(rng: random.Random, p: float, pool: list[str]) -> list[str]:
    return sorted(s for s in pool if rng.random() < p)


def _acl(rng: random.Random) -> dict:
    read = _set(rng, 0.3, USERS + GROUPS)
    if rng.random() < 0.3:
        read.append(EVERYONE)
    return {"read_sids": read, "deny_sids": _set(rng, 0.15, USERS + GROUPS),
            "deny_first": rng.random() < 0.5}  # fmt: skip


def _folder(rng: random.Random, path: str) -> Folder:
    r = rng.random()
    if r < 0.08:
        return Folder(path, None, None, error="unreadable")
    if r < 0.16:
        return Folder(path, None, None)  # not read yet
    a = _acl(rng) if rng.random() < 0.6 else {"read_sids": [EVERYONE], "deny_sids": [],
                                               "deny_first": False}  # fmt: skip
    return Folder(path, uuid.uuid4().bytes * 2, frozenset(a["read_sids"]),
                  frozenset(a["deny_sids"]), a["deny_first"])  # fmt: skip


def _filer(rng: random.Random) -> FilerAccess:
    """Decision 65's answer: unanswered, not limited, or some groups."""
    r = rng.random()
    if r < 0.1:
        return FilerAccess(None)
    if r < 0.5:
        return FilerAccess(False)
    return FilerAccess(True, frozenset(_set(rng, 0.5, GROUPS)) or frozenset({GROUPS[0]}))


def _users(rng: random.Random) -> list[tuple[str, list[str]]]:
    return [(u, _set(rng, 0.4, GROUPS)) for u in USERS] + [("S-1-5-21-9-9-9-500", [])]


def test_the_set_check_never_lets_through_anyone_a_folder_keeps_out() -> None:
    for seed in SEEDS:
        rng = random.Random(seed)
        folders = Folders(True, [_folder(rng, f"p{i}") for i in range(rng.randint(1, 4))],
                          filer=_filer(rng))  # fmt: skip
        sids = set(_set(rng, 0.3, USERS + GROUPS))
        if rng.random() < 0.2:
            sids.add(EVERYONE)
        if not folders.allow(sids):
            continue
        for user, groups in _users(rng):
            granted = EVERYONE in sids or user in sids or bool(sids & set(groups))
            if granted:
                assert folders.admits(user, groups), (seed, user, groups, sids)


def test_no_folders_apply_to_a_file_no_annex_holds() -> None:
    rng = random.Random(7)
    plain = Folders(annexed=False)
    for _ in range(50):
        a = _acl(rng)
        for user, groups in _users(rng):
            assert may_read(a, plain, user, groups) == can_read(
                a["read_sids"], a["deny_sids"], user, groups, a["deny_first"]
            )


def _answer(sql, filer: int, *, limited: bool, groups: list[str] | None = None) -> None:
    """Decision 65's question, answered for the filer."""
    sql.execute(
        "UPDATE catalog.filer SET access_limited = %s, access_groups = %s,"
        " access_confirmed_by = 'tests', access_confirmed_at = now() WHERE filer_id = %s",
        (limited, groups or [], filer),
    )


@pytest.fixture
def unread(annex, sql):  # noqa: F811
    """The annex, opted out of the test databases' open folders, with one annexed file
    three folders down: `long-term-storage/a/b/f.bin` under the export `geo`."""
    w, annex_id, ds, filer = annex
    sql.execute("INSERT INTO omnifile_test.unread_annex VALUES (%s)", (annex_id,))
    _answer(sql, filer, limited=False)
    [r] = events(w, annex_id, [arrived("a/b/f.bin", b"secret bytes")])
    return w, annex_id, ds, filer, int(r["file_id"])


PATHS = ["", "long-term-storage", "long-term-storage/a", "long-term-storage/a/b"]


def _acl_row(sql, ds: int, a: dict, search: dict | None = None) -> int:
    (acl_id,) = sql.execute(
        "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids, deny_sids,"
        " deny_first, search_sids, search_deny_sids)"
        " VALUES (%s, %s, 420, '[]', %s, %s, %s, %s, %s) RETURNING acl_id",
        (ds, uuid.uuid4().bytes * 2, a["read_sids"], a["deny_sids"], a["deny_first"],
         None if search is None else search["read_sids"],
         None if search is None else search["deny_sids"]),
    ).fetchone()  # fmt: skip
    return acl_id


@pytest.mark.parametrize("crowded", [False, True], ids=["one-file-folder", "crowded-folder"])
def test_the_effective_read_set_is_never_wider_than_the_captured_one(unread, sql, crowded) -> None:
    """And it is exactly the captured ACL and every folder, as the model says.

    `crowded`: the file's folder has more than 64 children, and its first 64 aren't
    annexed, so the one-folder form finds no NAS folder for them and checks each
    row's own chain: it still agrees."""
    from omnifile_service.catalog import read_set

    w, annex_id, ds, filer, fid = unread
    (parent,) = sql.execute(
        "SELECT parent_id FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()
    if crowded:
        sql.execute(
            "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, acl_id, origin)"
            " SELECT dataset_id, parent_id, 'a' || lpad(i::text, 3, '0'), 'dir', acl_id, origin"
            "   FROM catalog.file, generate_series(0, 63) i WHERE file_id = %s",
            (fid,),
        )
        found = sql.execute(read_set._FOLDER_DIR.format(folder="%s::bigint"), (parent,)).fetchone()
        assert found is None, "no annexed child among the first 64"
    for seed in range(1, 151):
        rng = random.Random(seed)
        captured = _acl(rng)
        sql.execute("DELETE FROM catalog.nas_folder_acl WHERE filer_id = %s", (filer,))
        sql.execute(
            "UPDATE catalog.file SET acl_id = %s WHERE file_id = %s",
            (_acl_row(sql, ds, captured), fid),
        )
        rows = []
        for path in PATHS:
            f = _folder(rng, path)
            if rng.random() < 0.05:
                rows.append(Folder(path, None, None))  # no row at all
                continue
            if f.error is not None:
                acl_id, err = None, f.error
            else:
                s = None if f.search is None else {"read_sids": sorted(f.search),
                                                   "deny_sids": sorted(f.deny)}  # fmt: skip
                acl_id = _acl_row(sql, ds, {"read_sids": [], "deny_sids": [],
                                            "deny_first": f.deny_first}, s)  # fmt: skip
                err = None
            sql.execute(
                "INSERT INTO catalog.nas_folder_acl (filer_id, path, acl_id, error, annex_id,"
                " observed_at) VALUES (%s, %s, %s, %s, %s, now())",
                (filer, path, acl_id, err, annex_id),
            )
            rows.append(f)
        filer_access = _filer(rng)
        if filer_access.limited is None:
            sql.execute(
                "UPDATE catalog.filer SET access_limited = NULL, access_groups = '{}',"
                " access_confirmed_by = NULL, access_confirmed_at = NULL WHERE filer_id = %s",
                (filer,),
            )
        else:
            _answer(sql, filer, limited=filer_access.limited,
                    groups=sorted(filer_access.groups))  # fmt: skip
        folders = Folders(True, rows, filer=filer_access)
        for user, groups in _users(rng):
            eff, cap = sql.execute(
                "SELECT catalog.can_read_file(a.read_sids, a.deny_sids, a.deny_first,"
                "                             f.file_id, %s, %s),"
                "       coalesce(catalog.acl_can_read(a.read_sids, a.deny_sids, %s, %s,"
                "                                     a.deny_first), false)"
                "  FROM catalog.file f JOIN catalog.acl a ON a.acl_id = f.acl_id"
                " WHERE f.file_id = %s",
                (user, groups, user, groups, fid),
            ).fetchone()
            assert not eff or cap, (seed, user, groups, "wider than the captured ACL")
            assert eff == may_read(captured, folders, user, groups), (seed, user, groups)
            # The same through every form a query may use (migration 0055): the walk,
            # a page's walk and its fall-back to each row's chain, a folder's
            # children, and one item.
            forms = {
                "walk": readable_sql("f.file_id", "a"),
                "page": readable_page_sql("f.file_id", "a"),
                "page, each row": readable_page_sql("f.file_id", "a", walk_max=0),
                "in its folder": readable_in_folder_sql("f.file_id", "%(d)s::bigint", "a"),
                "one item": readable_row_sql("f.file_id", "a"),
            }
            for name, pred in forms.items():
                (listed,) = sql.execute(
                    "SELECT " + pred + " FROM catalog.file f"
                    " JOIN catalog.acl a ON a.acl_id = f.acl_id WHERE f.file_id = %(f)s",
                    {"sid": user, "groups": groups, "f": fid, "d": parent},
                ).fetchone()
                assert listed == eff, (seed, user, groups, name)


def _posix(mode: int, owner: str, group: str) -> dict:
    """A POSIX ACL as the edge sends it: base entries from the mode."""

    def perms(bits: int) -> str:
        return ("r" if bits & 4 else "-") + ("w" if bits & 2 else "-") + ("x" if bits & 1 else "-")

    return {"kind": "posix", "owner_sid": owner, "group_sid": group, "mode": mode,
            "entries": [{"scope": "access", "tag": "owner", "perms": perms(mode >> 6)},
                        {"scope": "access", "tag": "owning_group", "perms": perms(mode >> 3 & 7)},
                        {"scope": "access", "tag": "other", "perms": perms(mode & 7)}]}  # fmt: skip


def _folders(w, annex_id: int, evs: list[dict]) -> None:
    res = events(w, annex_id, evs)
    assert [r["status"] for r in res] == ["applied"] * len(evs), res


def _reads(sql, fid: int, user: str, groups: list[str]) -> bool:
    (ok,) = sql.execute(
        "SELECT catalog.can_read_file(a.read_sids, a.deny_sids, a.deny_first, f.file_id, %s, %s)"
        "  FROM catalog.file f JOIN catalog.acl a ON a.acl_id = f.acl_id WHERE f.file_id = %s",
        (user, groups, fid),
    ).fetchone()
    return bool(ok)


ALICE, BOB = USERS[0], USERS[1]


def test_a_0644_file_under_a_0700_folder_is_its_owners_alone(make_world, sql) -> None:
    """#70's review: on every NFS server the file counted as readable by everyone."""
    from .test_annex import make_annex

    w = make_world()
    annex_id, ds, filer = make_annex(sql, w, protocol="nfs3")
    sql.execute("INSERT INTO omnifile_test.unread_annex VALUES (%s)", (annex_id,))
    _answer(sql, filer, limited=False)
    [r] = events(w, annex_id, [{**arrived("secret/f.txt", b"x"),
                                "acl": _posix(0o644, ALICE, GROUPS[0])}])  # fmt: skip
    fid = int(r["file_id"])
    open_dir = _posix(0o755, OWNERS, GROUPS[0])
    # Fail closed: nobody until every folder above it is read, its owner included.
    assert not _reads(sql, fid, ALICE, [])
    _folders(w, annex_id, [
        {"kind": "folder_acl", "path": "", "acl": open_dir},
        {"kind": "folder_acl", "path": "long-term-storage", "acl": open_dir},
        {"kind": "folder_acl", "path": "long-term-storage/secret",
         "acl": _posix(0o700, ALICE, GROUPS[0])},
    ])  # fmt: skip
    assert _reads(sql, fid, ALICE, [])
    assert not _reads(sql, fid, BOB, [GROUPS[0]]), "the folder keeps out its group"
    assert not _reads(sql, fid, BOB, []), "and everyone else"
    # Opened up: the file's own ACL decides again.
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/secret",
                          "acl": _posix(0o711, ALICE, GROUPS[0])}])  # fmt: skip
    assert _reads(sql, fid, BOB, [])


def _posix_entries(owner: str, group: str | None, *extra: dict, other: str = "r-x") -> dict:
    """A folder's POSIX ACL with a mask and named entries, as the edge sends it."""
    return {"kind": "posix", "owner_sid": owner, "group_sid": group, "mode": 0o755,
            "entries": [{"scope": "access", "tag": "owner", "perms": "rwx"},
                        {"scope": "access", "tag": "owning_group", "perms": "r-x"},
                        {"scope": "access", "tag": "mask", "perms": "r-x"},
                        {"scope": "access", "tag": "other", "perms": other}, *extra]}  # fmt: skip


def _unmapped(perms: str) -> dict:
    return {"scope": "access", "tag": "group", "sid": None, "perms": perms}


def _problems(sql, annex_id: int) -> list[dict]:
    return [r[0] for r in sql.execute(
        "SELECT details FROM catalog.annex_event WHERE annex_id = %s AND kind = 'problem'"
        " AND details->>'reason' = 'folder_unreadable'", (annex_id,)).fetchall()]  # fmt: skip


@pytest.mark.parametrize(
    "secret",
    [
        # Codex on #83: a named group the edge couldn't map, granting r but not x. Its
        # members can't pass on the NAS (a matching group entry that doesn't grant
        # denies), whatever `other` says, and who they are isn't known.
        pytest.param(_posix_entries(ALICE, GROUPS[0], _unmapped("r--")), id="unmapped-group"),
        # The same with x granted, then taken away by the mask.
        pytest.param(
            {
                **_posix_entries(ALICE, GROUPS[0], _unmapped("r-x")),
                "entries": [
                    *_posix_entries(ALICE, GROUPS[0], _unmapped("r-x"))["entries"][:2],
                    {"scope": "access", "tag": "mask", "perms": "r--"},
                    *_posix_entries(ALICE, GROUPS[0], _unmapped("r-x"))["entries"][3:],
                ],
            },
            id="masked-unmapped-group",
        ),  # fmt: skip
        # An owning group the edge couldn't map, whose entry doesn't grant x.
        pytest.param(
            {
                "kind": "posix",
                "owner_sid": ALICE,
                "mode": 0o745,
                "entries": [
                    {"scope": "access", "tag": "owner", "perms": "rwx"},
                    {"scope": "access", "tag": "owning_group", "perms": "r--"},
                    {"scope": "access", "tag": "other", "perms": "r-x"},
                ],
            },
            id="unmapped-owning-group",
        ),  # fmt: skip
    ],
)
def test_on_nfs3_an_unmapped_group_a_folder_may_keep_out_hides_what_is_below(
    make_world, sql, secret
) -> None:
    """Fail closed: the folder's search set can't be told, so nobody passes and it is
    reported, even where an identical ACL without that entry was already stored."""
    from .test_annex import make_annex

    w = make_world()
    annex_id, ds, filer = make_annex(sql, w, protocol="nfs3")
    sql.execute("INSERT INTO omnifile_test.unread_annex VALUES (%s)", (annex_id,))
    _answer(sql, filer, limited=False)
    [r] = events(w, annex_id, [{**arrived("secret/f.txt", b"x"),
                                "acl": _posix(0o644, ALICE, GROUPS[0])}])  # fmt: skip
    fid = int(r["file_id"])
    # The same entries without the unmapped one: stored first, under the same hash.
    open_dir = {"group_sid": GROUPS[0], **secret,
                "entries": [e for e in secret["entries"] if "sid" not in e]}  # fmt: skip
    _folders(w, annex_id, [
        {"kind": "folder_acl", "path": "", "acl": open_dir},
        {"kind": "folder_acl", "path": "long-term-storage", "acl": open_dir},
        {"kind": "folder_acl", "path": "long-term-storage/secret", "acl": open_dir},
    ])  # fmt: skip
    assert _reads(sql, fid, BOB, [])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/secret",
                          "acl": secret}])  # fmt: skip
    assert not _reads(sql, fid, BOB, []), "BOB may be in the group the folder keeps out"
    assert not _reads(sql, fid, ALICE, []), "unknown: nobody passes"
    [p] = _problems(sql, annex_id)
    assert p["folder"] == "long-term-storage/secret"


def test_on_nfs3_an_unmapped_group_that_grants_search_keeps_nobody_out(make_world, sql) -> None:
    """It can only add people who pass, so the folder's set is still known."""
    from .test_annex import make_annex

    w = make_world()
    annex_id, ds, filer = make_annex(sql, w, protocol="nfs3")
    sql.execute("INSERT INTO omnifile_test.unread_annex VALUES (%s)", (annex_id,))
    _answer(sql, filer, limited=False)
    [r] = events(w, annex_id, [{**arrived("secret/f.txt", b"x"),
                                "acl": _posix(0o644, ALICE, GROUPS[0])}])  # fmt: skip
    fid = int(r["file_id"])
    acl = _posix_entries(ALICE, GROUPS[0], _unmapped("r-x"))
    _folders(
        w,
        annex_id,
        [
            {"kind": "folder_acl", "path": p, "acl": acl}
            for p in ("", "long-term-storage", "long-term-storage/secret")
        ],
    )
    assert _reads(sql, fid, BOB, [])
    assert _problems(sql, annex_id) == []


def test_a_file_whose_read_set_is_unknown_never_takes_a_twins_row(make_world, sql) -> None:
    """The re-review of #83: acl_body drops an unmapped named group, so an ACL with one
    hashed the same as its twin without it, and the twin's row, filed first, lent the
    file its wider read set."""
    from .test_annex import make_annex

    w = make_world()
    annex_id, ds, filer = make_annex(sql, w, protocol="nfs3")
    twin = {"kind": "posix", "owner_sid": ALICE, "group_sid": GROUPS[0], "mode": 0o604,
            "entries": [{"scope": "access", "tag": "owner", "perms": "rw-"},
                        {"scope": "access", "tag": "owning_group", "perms": "r--"},
                        {"scope": "access", "tag": "mask", "perms": "---"},
                        {"scope": "access", "tag": "other", "perms": "r--"}]}  # fmt: skip
    # The same, with a group the edge couldn't map: the mask keeps its members out.
    unmapped = {**twin, "entries": [*twin["entries"], _unmapped("r--")]}
    rs = events(w, annex_id, [
        {**arrived("t1.bin", b"one"), "acl": twin},
        {**arrived("t2.bin", b"two"), "nas_file_id": "1002", "acl": unmapped},
    ])  # fmt: skip
    f1, f2 = (int(r["file_id"]) for r in rs)
    assert _reads(sql, f1, BOB, []), "the twin: everyone but its group"
    assert not _reads(sql, f2, BOB, []), "unknown: nobody"


@pytest.mark.parametrize("with_entries", [True, False], ids=["entries", "mode-only"])
def test_a_file_whose_unmapped_owning_group_reads_less_than_other_is_nobodys(
    with_entries,
) -> None:
    """The review of #83: a 0604 file whose gid the edge couldn't map. POSIX keeps that
    group's members out and lets everyone else read; who they are isn't known, so
    nobody is counted a reader. With the group mapped, it is Everyone but that group."""
    from omnifile_service.annex.acl import acl_body

    acl = _posix(0o604, ALICE, GROUPS[0])
    del acl["group_sid"]
    if not with_entries:
        del acl["entries"]
    body = acl_body(acl, "nfs3")
    assert (body["read"], body["deny"]) == ([], [])
    mapped = acl_body(_posix(0o604, ALICE, GROUPS[0]), "nfs3")
    assert EVERYONE in mapped["read"] and GROUPS[0] in mapped["deny"]
    # 0644: the group reads as much as other, so nobody is kept out by it.
    acl = _posix(0o644, ALICE, GROUPS[0])
    del acl["group_sid"]
    assert EVERYONE in acl_body(acl, "nfs3")["read"]


def test_who_may_add_a_file_is_unknown_where_an_unmapped_principal_may_be_kept_out() -> None:
    """The same for write sets (placements): POSIX needs w and x, and an NFSv4 deny of
    any write bit for a principal the edge couldn't map could be the requester's."""
    from omnifile_service.annex.acl import acl_body, folder_write_sets

    posix = _posix_entries(ALICE, GROUPS[0], _unmapped("r-x"), other="rwx")
    assert folder_write_sets(posix, acl_body(posix, "nfs3"))[0] is None
    clean = _posix_entries(ALICE, GROUPS[0], other="rwx")
    assert folder_write_sets(clean, acl_body(clean, "nfs3"))[0] is not None
    v4 = {"kind": "nfs4", "owner_sid": ALICE, "group_sid": GROUPS[0], "mode": 0o777,
          "aces": [{"type": "deny", "sid": None, "mask": 0x4000_0000, "flags": 0},
                   {"type": "allow", "sid": EVERYONE, "mask": 0x1F01FF, "flags": 0}]}  # fmt: skip
    assert folder_write_sets(v4, acl_body(v4, "nfs4"))[0] is None


def test_an_unmapped_group_matters_only_where_other_would_let_its_members_in() -> None:
    """Where `other` doesn't grant search either, the sets without the group are right:
    its members get through only by another group that does, as POSIX says."""
    from omnifile_service.annex.acl import acl_body, folder_search_sets

    acl = _posix_entries(ALICE, GROUPS[0], _unmapped("r--"), other="r--")
    search, _ = folder_search_sets(acl, acl_body(acl, "nfs3"))
    assert search is not None and EVERYONE not in search


def test_a_folder_nobody_is_known_to_place_into_still_judges_reads(make_world, sql) -> None:
    """An unmapped group that may search but not write: reads go on, and the folder's
    write set is unknown (nobody places a file there), not a stored twin's."""
    from .test_annex import make_annex

    w = make_world()
    annex_id, ds, filer = make_annex(sql, w, protocol="nfs3")
    sql.execute("INSERT INTO omnifile_test.unread_annex VALUES (%s)", (annex_id,))
    _answer(sql, filer, limited=False)
    [r] = events(w, annex_id, [{**arrived("secret/f.txt", b"x"),
                                "acl": _posix(0o644, ALICE, GROUPS[0])}])  # fmt: skip
    fid = int(r["file_id"])
    twin = _posix_entries(ALICE, GROUPS[0], other="rwx")
    acl = _posix_entries(ALICE, GROUPS[0], _unmapped("r-x"), other="rwx")
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "", "acl": twin},
                           {"kind": "folder_acl", "path": "long-term-storage", "acl": twin},
                           {"kind": "folder_acl", "path": "long-term-storage/secret",
                            "acl": acl}])  # fmt: skip
    assert _reads(sql, fid, BOB, [])
    rows = dict(sql.execute(
        "SELECT n.path, a.write_sids IS NOT NULL FROM catalog.nas_folder_acl n"
        "  JOIN catalog.acl a ON a.acl_id = n.acl_id WHERE n.filer_id = %s", (filer,),
    ).fetchall())  # fmt: skip
    assert rows == {"": True, "long-term-storage": True, "long-term-storage/secret": False}


def test_an_unreadable_folder_above_the_annex_hides_its_files_and_says_so(unread, sql) -> None:
    w, annex_id, ds, filer, fid = unread
    open_dir = nt_acl([ace(0, READ | TRAVERSE, GEO)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": p, "acl": open_dir} for p in PATHS])
    assert _reads(sql, fid, BOB, [GEO])
    # The export's root, above the annex: the edge couldn't read it.
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "", "error": "EACCES"}])
    assert not _reads(sql, fid, BOB, [GEO])
    (detail,) = sql.execute(
        "SELECT details FROM catalog.annex_event WHERE annex_id = %s AND kind = 'problem'"
        " AND details->>'reason' = 'folder_unreadable'",
        (annex_id,),
    ).fetchone()
    assert detail["folder"] == "" and "hidden from everyone" in detail["detail"]


def test_on_smb_a_folder_that_doesnt_grant_traverse_keeps_its_readers_out(unread, sql) -> None:
    """Strict (the owner, 2026-11-07): Windows' bypass traverse checking would let them
    open the file by its full path; OmniFile doesn't count on it."""
    w, annex_id, ds, filer, fid = unread
    with_traverse = nt_acl([ace(0, READ | TRAVERSE, GEO)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": p, "acl": with_traverse} for p in PATHS])
    assert _reads(sql, fid, BOB, [GEO])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/a",
                          "acl": nt_acl([ace(0, READ, GEO)])}])  # fmt: skip
    assert not _reads(sql, fid, BOB, [GEO])
    # A deny for them on a folder wins over the allow.
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/a",
                          "acl": nt_acl([ace(1, TRAVERSE, BOB),
                                         ace(0, READ | TRAVERSE, GEO)])}])  # fmt: skip
    assert not _reads(sql, fid, BOB, [GEO])
    assert _reads(sql, fid, USERS[2], [GEO])


def _listed(sql, fid: int, user: str, groups: list[str]) -> bool:
    """Through `readable_sql`: the folders walked once per query (migration 0055)."""
    (ok,) = sql.execute(
        "SELECT " + readable_sql("f.file_id") + " FROM catalog.file f"
        " JOIN catalog.acl acl ON acl.acl_id = f.acl_id WHERE f.file_id = %(f)s",
        {"sid": user, "groups": groups, "f": fid},
    ).fetchone()
    return bool(ok)


def test_a_listing_walks_the_folders_once_however_many_rows_and_times_it_asks(unread, sql) -> None:
    """Migration 0055: one walk per query, never one per row or per sub-select, the
    share map's folders, groups and recalls included (they ask three times)."""
    from omnifile_service.web.routes_visual import _map_readable

    w, annex_id, ds, filer, fid = unread
    params = {"sid": BOB, "groups": [GEO], "f": fid, "ds": ds, "root": fid,
              "active": ["requested"]}  # fmt: skip

    def walks(q: str) -> tuple[int, int]:
        plan = sql.execute("EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) " + q, params).fetchone()[0]
        calls: list[int] = []

        def scan(node: dict) -> None:
            if "passable_dirs" in str(node.get("Output", "")) + str(node.get("Filter", "")):
                calls.append(node.get("Actual Loops", 0))
            for child in node.get("Plans", []):
                scan(child)

        scan(plan[0]["Plan"])
        return len(calls), max(calls, default=0)

    flat = (
        "SELECT count(*) FROM catalog.file f JOIN catalog.acl acl ON acl.acl_id = f.acl_id"
        " WHERE f.dataset_id = %(ds)s AND " + readable_sql("f.file_id")
    )
    assert walks(flat) == (1, 1)
    assert walks(_map_readable()) == (1, 1)


def test_one_item_and_a_folders_page_never_walk(unread, sql) -> None:
    """The review of #88: a file's page (load_scope, used by the file page, a recall,
    rewind and tiers) and a browse page check chains, never the walk, which at
    10^6 passable folders took seconds."""
    from omnifile_service.web import access, routes_browse  # noqa: F401 - the forms in use

    w, annex_id, ds, filer, fid = unread
    (parent,) = sql.execute(
        "SELECT parent_id FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()
    params = {"sid": BOB, "groups": [GEO], "f": fid, "d": parent}
    browse = (
        "SELECT f.file_id FROM catalog.file f JOIN catalog.acl acl ON acl.acl_id = f.acl_id"
        " WHERE f.parent_id = %(d)s AND "
        + readable_in_folder_sql("f.file_id", "%(d)s::bigint")
        + " ORDER BY f.name LIMIT 101"
    )
    for q in (access.SCOPE_CHAIN_SQL, browse):
        plan = "\n".join(r[0] for r in sql.execute("EXPLAIN (VERBOSE) " + q, params).fetchall())
        assert "passable_dirs" not in plan, q
    # The browse route writes its listing with the folder form, and the routes of one
    # item (the share's root, a returned file) and the tier preview (at most 1000
    # files below a folder) never use the uncapped walk.
    import inspect

    from omnifile_service.web import routes_annex_returns, routes_shares, routes_tiers

    src = inspect.getsource(routes_browse)
    assert src.count('readable_in_folder_sql("f.file_id", "%(d)s::bigint")') == 1
    for mod in (access, routes_browse, routes_shares, routes_annex_returns, routes_tiers):
        assert "readable_sql(" not in inspect.getsource(mod), mod.__name__
    # The tier preview's walk gives up past the page cap: every call carries it.
    import re

    from omnifile_service.catalog.read_set import PAGE_WALK_MAX

    plan = "\n".join(r[0] for r in sql.execute(
        "EXPLAIN (VERBOSE) " + routes_tiers.DESCENDANT_READABLE,
        {**params, "root": parent, "cap": 1000}).fetchall())  # fmt: skip
    calls = re.findall(r"passable_dirs\(([^()]*)\)", plan)
    assert calls and all(c.rstrip().endswith(str(PAGE_WALK_MAX)) for c in calls), calls


def test_a_child_in_another_folder_than_its_siblings_checks_its_own(unread, sql) -> None:
    """A folder's page checks its children's NAS folder once; a child whose folder
    differs (it shouldn't) is judged by its own."""
    w, annex_id, ds, filer, fid = unread
    open_dir = nt_acl([ace(0, READ | TRAVERSE, GEO)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": p, "acl": open_dir}
                           for p in [*PATHS, "long-term-storage/c"]])  # fmt: skip
    [r] = events(w, annex_id, [{**arrived("a/b/g.bin", b"other bytes"), "nas_file_id": "1002"}])
    sib = int(r["file_id"])
    # The sibling's NAS folder changes, its catalog folder doesn't.
    sql.execute("UPDATE catalog.annex_file SET path = 'c/g.bin' WHERE file_id = %s", (sib,))
    (parent,) = sql.execute(
        "SELECT parent_id FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()

    def shown(f: int) -> bool:
        (ok,) = sql.execute(
            "SELECT " + readable_in_folder_sql("f.file_id", "%(d)s::bigint") + " FROM"
            " catalog.file f JOIN catalog.acl acl ON acl.acl_id = f.acl_id WHERE f.file_id = %(f)s",
            {"sid": BOB, "groups": [GEO], "f": f, "d": parent},
        ).fetchone()
        return bool(ok)

    assert shown(fid) and shown(sib)
    closed = nt_acl([ace(0, READ | TRAVERSE, OWNERS)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/c", "acl": closed}])
    assert shown(fid) and not shown(sib), "its own folder keeps them out"
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/c", "acl": open_dir},
                           {"kind": "folder_acl", "path": "long-term-storage/a/b",
                            "acl": closed}])  # fmt: skip
    assert not shown(fid) and shown(sib), "and its siblings' folder doesn't"


def test_each_folders_gate_follows_its_folders(unread, sql) -> None:
    """The folders' answer is kept per NAS folder (catalog.annex_dir_gate), from folder
    events: a change applies to the folders below it, and to no other, at once."""
    w, annex_id, ds, filer, fid = unread
    open_dir = nt_acl([ace(0, READ | TRAVERSE, GEO)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": p, "acl": open_dir} for p in PATHS])
    # A sibling whose name starts the same: `long-term-storage/ab`.
    [r] = events(w, annex_id, [{**arrived("ab/g.bin", b"other bytes"), "nas_file_id": "1002"}])
    sib = int(r["file_id"])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/ab",
                          "acl": open_dir}])  # fmt: skip
    assert _listed(sql, fid, BOB, [GEO]) and _listed(sql, sib, BOB, [GEO])
    closed = nt_acl([ace(0, READ | TRAVERSE, OWNERS)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/a", "acl": closed}])
    assert not _listed(sql, fid, BOB, [GEO]), "a folder above it closed"
    assert _listed(sql, sib, BOB, [GEO]), "not a sibling's"
    # The export's root closes everything; opened again, everything opens.
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "", "acl": closed}])
    assert not _listed(sql, sib, BOB, [GEO])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "", "acl": open_dir},
                           {"kind": "folder_acl", "path": "long-term-storage/a",
                            "acl": open_dir}])  # fmt: skip
    assert _listed(sql, fid, BOB, [GEO]) and _listed(sql, sib, BOB, [GEO])
    # A folder's row deleted: nobody passes.
    sql.execute(
        "DELETE FROM catalog.nas_folder_acl WHERE filer_id = %s AND path = %s",
        (filer, "long-term-storage/a/b"),
    )
    assert not _listed(sql, fid, BOB, [GEO])


def test_a_gate_is_never_worked_out_from_a_folder_change_not_yet_committed(
    unread, sql, db_url
) -> None:
    """The re-review of #83: every annex on a filer shares the export's root. One
    transaction closes it and hasn't committed; another files a file in a new folder.
    With derived gates and no lock per filer, the new folder's gate came from the old
    root, open; walked per query (migration 0055), the closed root keeps them out."""
    import threading
    import time

    import psycopg

    w, annex_id, ds, filer, fid = unread
    open_dir = nt_acl([ace(0, READ | TRAVERSE, GEO)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": p, "acl": open_dir} for p in PATHS])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/new",
                          "acl": open_dir}])  # fmt: skip
    closed = _acl_row(sql, ds, {"read_sids": [], "deny_sids": [], "deny_first": True},
                      {"read_sids": [OWNERS], "deny_sids": []})  # fmt: skip
    (new_fid,) = sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, size, acl_id, origin,"
        " state, resident_bytes)"
        " SELECT dataset_id, parent_id, 'g.bin', 'file', size, acl_id, origin, state,"
        "        resident_bytes"
        "   FROM catalog.file WHERE file_id = %s RETURNING file_id",
        (fid,),
    ).fetchone()
    s1 = psycopg.connect(db_url)
    s1.execute(
        "UPDATE catalog.nas_folder_acl SET acl_id = %s WHERE filer_id = %s AND path = ''",
        (closed, filer),
    )
    done = threading.Event()

    def file_it() -> None:
        with psycopg.connect(db_url, autocommit=True) as s2:
            s2.execute(
                "INSERT INTO catalog.annex_file (annex_id, file_id, path, nas_file_id, size,"
                " first_seen, last_seen) VALUES (%s, %s, 'new/g.bin', 'n2', 1, now(), now())",
                (annex_id, new_fid),
            )
        done.set()

    t = threading.Thread(target=file_it)
    t.start()
    time.sleep(1.0)
    s1.commit()
    s1.close()
    t.join(10)
    assert not _listed(sql, new_fid, BOB, [GEO]), "the root, closed, keeps them out"


def test_a_file_moved_takes_its_new_folders_gate(unread, sql) -> None:
    w, annex_id, ds, filer, fid = unread
    open_dir = nt_acl([ace(0, READ | TRAVERSE, GEO)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": p, "acl": open_dir} for p in PATHS])
    assert _listed(sql, fid, BOB, [GEO])
    # To a folder not read yet: hidden.
    sql.execute("UPDATE catalog.annex_file SET path = 'c/f.bin' WHERE file_id = %s", (fid,))
    assert not _listed(sql, fid, BOB, [GEO])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/c",
                          "acl": open_dir}])  # fmt: skip
    assert _listed(sql, fid, BOB, [GEO])
    # Back where it was.
    sql.execute("UPDATE catalog.annex_file SET path = 'a/b/f.bin' WHERE file_id = %s", (fid,))
    assert _listed(sql, fid, BOB, [GEO])
    # No folder recorded for it: nobody (fail closed).
    sql.execute("SET session_replication_role = replica")
    sql.execute("UPDATE catalog.annex_file SET dir_id = NULL WHERE file_id = %s", (fid,))
    sql.execute("SET session_replication_role = origin")
    assert not _listed(sql, fid, BOB, [GEO])
    assert not _reads(sql, fid, BOB, [GEO]), "for one file too"


def test_a_catalog_folders_name_is_narrowed_by_the_nas_folders_above_it(unread, sql) -> None:
    """The review of #83: the folders an annex makes carry its Owners ACL, so an Owner
    saw a subfolder's name under a NAS folder that keeps them out."""
    w, annex_id, ds, filer, fid = unread
    owner = USERS[3]
    open_dir = nt_acl([ace(0, READ | TRAVERSE, GEO), ace(0, READ | TRAVERSE, OWNERS)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": p, "acl": open_dir} for p in PATHS])
    rows = dict(sql.execute(
        "SELECT y.path, y.file_id FROM catalog.annex_folder y WHERE y.annex_id = %s",
        (annex_id,),
    ).fetchall())  # fmt: skip
    assert set(rows) == {"a", "a/b"}
    assert _listed(sql, rows["a"], owner, [OWNERS]) and _listed(sql, rows["a/b"], owner, [OWNERS])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/a",
                          "acl": nt_acl([ace(0, READ | TRAVERSE, GEO)])}])  # fmt: skip
    assert _listed(sql, rows["a"], owner, [OWNERS]), "its own parent lets them through"
    assert not _listed(sql, rows["a/b"], owner, [OWNERS]), "a folder above it doesn't"


def test_a_folders_search_set_coming_to_be_known_opens_its_gate(unread, sql) -> None:
    """An ACL row stored first without one (a file's, or read before decision 63)."""
    w, annex_id, ds, filer, fid = unread
    open_dir = nt_acl([ace(0, READ | TRAVERSE, GEO)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": p, "acl": open_dir} for p in PATHS])
    (acl_id,) = sql.execute(
        "SELECT acl_id FROM catalog.nas_folder_acl WHERE filer_id = %s AND path = %s",
        (filer, "long-term-storage/a"),
    ).fetchone()
    sql.execute("UPDATE catalog.acl SET search_sids = NULL WHERE acl_id = %s", (acl_id,))
    assert not _listed(sql, fid, BOB, [GEO])
    sql.execute("UPDATE catalog.acl SET search_sids = %s WHERE acl_id = %s", ([GEO], acl_id))
    assert _listed(sql, fid, BOB, [GEO])


def test_a_gone_folder_still_judges_the_files_that_were_in_it(unread, sql) -> None:
    w, annex_id, ds, filer, fid = unread
    closed = nt_acl([ace(0, READ | TRAVERSE, OWNERS)])
    open_dir = nt_acl([ace(0, READ | TRAVERSE, GEO)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": p, "acl": open_dir} for p in PATHS[:3]]
           + [{"kind": "folder_acl", "path": PATHS[3], "acl": closed}])  # fmt: skip
    assert not _reads(sql, fid, BOB, [GEO])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": PATHS[3], "gone": True}])
    assert not _reads(sql, fid, BOB, [GEO]), "its last ACL still keeps them out"
    assert _reads(sql, fid, BOB, [OWNERS, GEO])


def test_folders_read_says_when_the_annex_has_every_folder(unread, sql) -> None:
    w, annex_id, ds, filer, fid = unread
    events(w, annex_id, [{"kind": "folders_read",
                          "details": {"folders": 4, "unreadable": 0, "ancestors": 1,
                                      "complete": True}}])  # fmt: skip
    at, counts = sql.execute(
        "SELECT folders_read_at, folders_read FROM catalog.annex WHERE annex_id = %s",
        (annex_id,),
    ).fetchone()
    assert at is not None and counts == {"folders": 4, "unreadable": 0, "ancestors": 1}


def _closed_folder(sid: str) -> Folders:
    """An annexed file below one folder that lets only `sid` through."""
    root = Folder("", b"o" * 32, frozenset({EVERYONE}))
    return Folders(True, [root, Folder("geo", b"c" * 32, frozenset({sid}), frozenset(), True)],
                   filer=FilerAccess(False))  # fmt: skip


def test_a_staged_acl_must_get_through_the_folders_unless_they_cover_it_there() -> None:
    """Placements (decision 51 and 63): a reader the staged ACL names must get through
    every folder above the file, unless the target sits below the same ACL."""
    from omnifile_service.api import annex_placements as P

    staged = {"kind": "nfs4", "raw": "eA==", "owner_sid": GEO, "group_sid": GEO,
              "aces": [{"type": "allow", "sid": GEO, "mask": READ, "flags": 0},
                       {"type": "allow", "sid": OWNERS, "mask": READ, "flags": 0}]}  # fmt: skip
    src = {"read_sids": [GEO, OWNERS], "deny_sids": [], "deny_first": True, "owner_sid": GEO,
           "group_sid": GEO, "raw_kind": "nfs4",
           "entries": [{"ace": "allow", "sid": GEO, "mask": READ, "flags": 0},
                       {"ace": "allow", "sid": OWNERS, "mask": READ, "flags": 0}]}  # fmt: skip
    assert P.within_catalog(staged, "nfs4", {**src, "folders": Folders(False)}, None)
    narrow = {**src, "folders": _closed_folder(GEO)}
    assert not P.within_catalog(staged, "nfs4", narrow, None), "OWNERS can't get through"
    assert P.within_catalog(staged, "nfs4", narrow, None, covered=[b"c" * 32]), "put back"
    assert not P.within_catalog(staged, "nfs4", {k: v for k, v in src.items()}, None), (
        "no folders said: fail closed"
    )


def test_a_share_copy_must_get_through_the_folders_too() -> None:
    from omnifile_service.worker.share_placements import within

    src = {"read_sids": [GEO, OWNERS], "deny_sids": [], "deny_first": False,
           "raw_kind": "posix"}  # fmt: skip
    assert within([GEO], {**src, "folders": Folders(False)})
    assert within([GEO], {**src, "folders": _closed_folder(GEO)})
    assert not within([GEO, OWNERS], {**src, "folders": _closed_folder(GEO)})
    assert not within([GEO], src), "no folders said: fail closed"


def test_who_a_card_goes_to_is_judged_by_the_folders_too(unread, sql, db_url) -> None:
    """Teams and the portal ask through catalog/read_set.py, never the raw set."""
    import asyncio

    import psycopg
    from psycopg.rows import dict_row

    from omnifile_service.catalog.privileges import as_app
    from omnifile_service.notify import facts

    w, annex_id, ds, filer, fid = unread
    (ident,) = sql.execute(
        "INSERT INTO catalog.identity (sid, kind, group_sids) VALUES (%s, 'user', %s)"
        " ON CONFLICT (sid) DO UPDATE SET group_sids = EXCLUDED.group_sids RETURNING identity_id",
        (BOB, [GEO]),
    ).fetchone()

    async def can() -> bool:
        async with await psycopg.AsyncConnection.connect(
            as_app(db_url, "worker"), row_factory=dict_row
        ) as c:
            return await facts.can_read(c, fid, ident)

    assert not asyncio.run(can()), "its folders aren't read yet"
    with_traverse = nt_acl([ace(0, READ | TRAVERSE, GEO)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": p, "acl": with_traverse} for p in PATHS])
    assert asyncio.run(can())
    _folders(w, annex_id, [{"kind": "folder_acl", "path": PATHS[2],
                            "acl": nt_acl([ace(0, READ | TRAVERSE, OWNERS)])}])  # fmt: skip
    assert not asyncio.run(can())


def test_whoever_a_staged_acl_gives_control_must_get_through_the_folders_too() -> None:
    """WRITE_DAC is read one step later (decision 51), so a principal with control is a
    reader for the folders as well."""
    from omnifile_service.api import annex_placements as P

    wdac = 0x0004_0000
    staged = {"kind": "nfs4", "raw": "eA==", "owner_sid": GEO, "group_sid": GEO,
              "aces": [{"type": "allow", "sid": GEO, "mask": READ, "flags": 0},
                       {"type": "allow", "sid": OWNERS, "mask": wdac, "flags": 0}]}  # fmt: skip
    src = {"read_sids": [GEO, OWNERS], "deny_sids": [], "deny_first": True, "owner_sid": GEO,
           "group_sid": GEO, "raw_kind": "nfs4",
           "entries": [{"ace": "allow", "sid": GEO, "mask": READ, "flags": 0},
                       {"ace": "allow", "sid": OWNERS, "mask": READ, "flags": 0}]}  # fmt: skip
    assert P.within_catalog(staged, "nfs4", {**src, "folders": Folders(False)}, None)
    assert not P.within_catalog(staged, "nfs4", {**src, "folders": _closed_folder(GEO)}, None)


def test_a_placement_judges_the_file_by_its_folders(unread, sql, facade_client, db_url) -> None:
    """The placement's source carries the folders above the file, read from the catalog."""
    import asyncio

    import psycopg
    from psycopg.rows import dict_row

    from omnifile_service.api import annex_placements as P

    from .test_annex import copy

    w, annex_id, ds, filer, fid = unread
    vid = copy(w, facade_client, str(fid), b"secret bytes")
    _folders(
        w,
        annex_id,
        [
            {"kind": "folder_acl", "path": p, "acl": nt_acl([ace(0, READ | TRAVERSE, GEO)])}
            for p in PATHS
        ],
    )

    async def source():
        async with await psycopg.AsyncConnection.connect(db_url, row_factory=dict_row) as c:
            return await P._source(c, {"file_id": fid, "version_id": int(vid)})

    src = asyncio.run(source())
    f = src["folders"]
    assert f.annexed and [r.path for r in f.rows] == PATHS and f.closed is None
    # And the filer's answer (decision 65).
    _answer(sql, filer, limited=True, groups=[OWNERS])
    assert asyncio.run(source())["folders"].filer == FilerAccess(True, frozenset({OWNERS}))


def test_a_share_that_lets_in_only_some_groups_keeps_everyone_else_out(unread, sql) -> None:
    """Decision 65: SMB share permissions and NFS export rules can't be read through a
    mount, so an ArchiveAdmin says which AD groups the share lets in."""
    w, annex_id, ds, filer, fid = unread
    open_dir = nt_acl([ace(0, READ | TRAVERSE, GEO)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": p, "acl": open_dir} for p in PATHS])
    assert _reads(sql, fid, BOB, [GEO])
    _answer(sql, filer, limited=True, groups=[OWNERS])
    assert not _reads(sql, fid, BOB, [GEO]), "the share doesn't let GEO in"
    assert _reads(sql, fid, BOB, [GEO, OWNERS])
    # Unanswered: nobody (fail closed).
    sql.execute(
        "UPDATE catalog.filer SET access_limited = NULL, access_groups = '{}',"
        " access_confirmed_by = NULL, access_confirmed_at = NULL WHERE filer_id = %s",
        (filer,),
    )
    assert not _reads(sql, fid, BOB, [GEO, OWNERS])


def test_a_staged_or_copied_reader_must_be_one_the_share_lets_in() -> None:
    from omnifile_service.worker.share_placements import within

    src = {"read_sids": [GEO, OWNERS], "deny_sids": [], "deny_first": False,
           "raw_kind": "posix"}  # fmt: skip
    open_root = [Folder("", b"o" * 32, frozenset({EVERYONE}))]
    limited = Folders(True, open_root, filer=FilerAccess(True, frozenset({OWNERS})))
    assert within([OWNERS], {**src, "folders": limited})
    assert not within([GEO, OWNERS], {**src, "folders": limited})
    assert not within([OWNERS], {**src, "folders": Folders(True, open_root)}), "unanswered"


def test_an_unanswered_filer_admits_nobody_and_a_limited_one_only_its_groups() -> None:
    assert not FilerAccess(None).admits(ALICE, GROUPS)
    assert FilerAccess(False).admits(ALICE, [])
    limited = FilerAccess(True, frozenset({GROUPS[1]}))
    assert limited.admits(ALICE, [GROUPS[1]]) and not limited.admits(ALICE, [GROUPS[0]])
    open_root = [Folder("", b"o" * 32, frozenset({EVERYONE}))]
    assert not Folders(True, open_root, filer=FilerAccess(None)).admits(ALICE, GROUPS)
    assert Folders(True, open_root, filer=FilerAccess(False)).admits(ALICE, [])


def test_the_share_maps_roll_up_judges_annexed_files_as_files_are(unread, sql, db_url) -> None:
    """Migration 0056: a group keeps whether its files are annexed and their annex
    folder, so the map counts them for exactly who may read them, and a change of
    an annexed file's annex folder marks its catalog folder for recount."""
    from web.test_share_map_rollup import _LIVE, _map, _queued, _refresh

    from omnifile_service.web import routes_visual

    w, annex_id, ds, filer, fid = unread
    open_dir = nt_acl([ace(0, READ | TRAVERSE, GEO)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": p, "acl": open_dir} for p in PATHS])
    (root,) = sql.execute(
        "SELECT file_id FROM catalog.file WHERE dataset_id = %s AND parent_id IS NULL", (ds,)
    ).fetchone()
    (parent,) = sql.execute(
        "SELECT parent_id FROM catalog.file WHERE file_id = %s", (fid,)
    ).fetchone()
    # Catalog folders BOB may read, so only the NAS's folders decide.
    sql.execute(
        "UPDATE catalog.file SET acl_id = (SELECT acl_id FROM catalog.file WHERE file_id = %s)"
        " WHERE dataset_id = %s AND kind = 'dir'",
        (fid, ds),
    )
    _refresh(db_url)
    group = sql.execute(
        "SELECT annexed, dir_id FROM catalog.share_map_group WHERE folder_id = %s", (parent,)
    ).fetchone()
    (dir_id,) = sql.execute(
        "SELECT dir_id FROM catalog.annex_file WHERE file_id = %s", (fid,)
    ).fetchone()
    assert group == (True, dir_id)
    rolled = routes_visual._map_readable()
    live = routes_visual.readable_dirs() + _LIVE.format(
        readable=readable_sql("f.file_id", walked=True)
    )

    def both() -> tuple[dict, dict]:
        return _map(sql, rolled, root, BOB, [GEO]), _map(sql, live, root, BOB, [GEO])

    got, want = both()
    assert got == want and got, "BOB reads the file"
    # A folder closed: at once, with no recount.
    closed = nt_acl([ace(0, READ | TRAVERSE, OWNERS)])
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/a", "acl": closed}])
    got, want = both()
    assert got == want and not any(k[0] == "edge" for k in got)
    # Its annex folder changes to one BOB may not read: its catalog folder is
    # queued, and BOB's map leaves it at once, with no recount.
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/a", "acl": open_dir}])
    _refresh(db_url)
    got, want = both()
    assert got == want and any(k[0] == "edge" for k in got)
    sql.execute("DELETE FROM catalog.share_map_dirty")
    sql.execute("UPDATE catalog.annex_file SET path = 'c/f.bin' WHERE file_id = %s", (fid,))
    assert parent in _queued(sql)
    got, want = both()
    assert got == want and not any(k[0] == "edge" for k in got)
    # Its annex row going: queued as well.
    sql.execute("DELETE FROM catalog.share_map_dirty")
    sql.execute("BEGIN")
    sql.execute("DELETE FROM catalog.annex_file WHERE file_id = %s", (fid,))
    assert parent in _queued(sql)
    sql.execute("ROLLBACK")
    # An annexed file with no annex folder recorded counts for nobody.
    _folders(w, annex_id, [{"kind": "folder_acl", "path": "long-term-storage/a", "acl": open_dir}])
    sql.execute("SET session_replication_role = replica")
    sql.execute("UPDATE catalog.annex_file SET dir_id = NULL WHERE file_id = %s", (fid,))
    sql.execute("SET session_replication_role = origin")
    sql.execute("INSERT INTO catalog.share_map_dirty (folder_id) VALUES (%s)", (parent,))
    _refresh(db_url)
    assert sql.execute(
        "SELECT annexed, dir_id FROM catalog.share_map_group WHERE folder_id = %s", (parent,)
    ).fetchone() == (True, None)
    got, want = both()
    assert got == want and not any(k[0] == "edge" for k in got)
