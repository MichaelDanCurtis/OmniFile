"""ACL snapshots and scans (control-api.md section 4.6, decision 28)."""

from __future__ import annotations

import hashlib
import json
import uuid

from omnifile_service.api.acls import canonical_json, read_sids

from .conftest import E, create_file

D = "S-1-5-21-3190051465-787089565-3716134449"  # FakeIdMap::DOMAIN_SID on the edge

# The edge's golden ACL (omnifile-acl snapshot.rs, `canonical_form_is_golden`),
# entries in its canonical (sorted) order.
GOLDEN_ENTRIES = [
    {"perms": "---", "scope": "access", "tag": "other"},
    {"perms": "---", "scope": "access", "tag": "owning_group"},
    {"perms": "---", "scope": "default", "tag": "other"},
    {"perms": "---", "scope": "default", "tag": "owning_group"},
    {"perms": "r-x", "scope": "access", "sid": f"{D}-1103", "tag": "group"},
    {"perms": "r-x", "scope": "default", "sid": f"{D}-1103", "tag": "group"},
    {"perms": "rwx", "scope": "access", "sid": f"{D}-1104", "tag": "group"},
    {"perms": "rwx", "scope": "access", "sid": f"{D}-1105", "tag": "group"},
    {"perms": "rwx", "scope": "access", "tag": "mask"},
    {"perms": "rwx", "scope": "access", "tag": "owner"},
    {"perms": "rwx", "scope": "default", "sid": f"{D}-1104", "tag": "group"},
    {"perms": "rwx", "scope": "default", "sid": f"{D}-1105", "tag": "group"},
    {"perms": "rwx", "scope": "default", "tag": "mask"},
    {"perms": "rwx", "scope": "default", "tag": "owner"},
]
GOLDEN_JSON = (
    '{"entries":['
    '{"perms":"---","scope":"access","tag":"other"},'
    '{"perms":"---","scope":"access","tag":"owning_group"},'
    '{"perms":"---","scope":"default","tag":"other"},'
    '{"perms":"---","scope":"default","tag":"owning_group"},'
    f'{{"perms":"r-x","scope":"access","sid":"{D}-1103","tag":"group"}},'
    f'{{"perms":"r-x","scope":"default","sid":"{D}-1103","tag":"group"}},'
    f'{{"perms":"rwx","scope":"access","sid":"{D}-1104","tag":"group"}},'
    f'{{"perms":"rwx","scope":"access","sid":"{D}-1105","tag":"group"}},'
    '{"perms":"rwx","scope":"access","tag":"mask"},'
    '{"perms":"rwx","scope":"access","tag":"owner"},'
    f'{{"perms":"rwx","scope":"default","sid":"{D}-1104","tag":"group"}},'
    f'{{"perms":"rwx","scope":"default","sid":"{D}-1105","tag":"group"}},'
    '{"perms":"rwx","scope":"default","tag":"mask"},'
    '{"perms":"rwx","scope":"default","tag":"owner"}'
    f'],"group_sid":"{D}-1105","mode":504,"owner_sid":"S-1-22-1-0"}}'
)


def body(owner: str | None, group: str | None, mode: int, entries: list[dict]) -> dict:
    ordered = sorted(entries, key=lambda e: json.dumps(e, sort_keys=True, separators=(",", ":")))
    sha = hashlib.sha256(canonical_json(owner, group, mode, ordered).encode()).hexdigest()
    out: dict = {"sha256": sha, "mode": mode, "entries": ordered}
    if owner:
        out["owner_sid"] = owner
    if group:
        out["group_sid"] = group
    return out


GOLDEN = body("S-1-22-1-0", f"{D}-1105", 0o770, GOLDEN_ENTRIES)
OPEN = body(
    f"{D}-2001",
    f"{D}-513",
    0o644,
    [
        {"perms": "rw-", "scope": "access", "tag": "owner"},
        {"perms": "r--", "scope": "access", "tag": "owning_group"},
        {"perms": "r--", "scope": "access", "tag": "other"},
    ],
)


def test_canonical_form_matches_the_edge_golden_vector() -> None:
    assert canonical_json("S-1-22-1-0", f"{D}-1105", 504, GOLDEN_ENTRIES) == GOLDEN_JSON
    assert GOLDEN["sha256"] == hashlib.sha256(GOLDEN_JSON.encode()).hexdigest()
    # read_sids as the edge derives them: the owner, named groups with r after
    # the mask; not the owning group (---) and not Everyone (other ---).
    assert read_sids("S-1-22-1-0", f"{D}-1105", GOLDEN_ENTRIES) == sorted(
        ["S-1-22-1-0", f"{D}-1103", f"{D}-1104", f"{D}-1105"]
    )
    masked = [*GOLDEN_ENTRIES[:8], {"perms": "-wx", "scope": "access", "tag": "mask"}]
    masked += [GOLDEN_ENTRIES[9]]
    assert read_sids("S-1-22-1-0", None, masked) == ["S-1-22-1-0"]  # the mask hides r
    assert read_sids(OPEN["owner_sid"], OPEN["group_sid"], OPEN["entries"]) == sorted(
        [f"{D}-2001", f"{D}-513", "S-1-1-0"]
    )


def test_acls_are_stored_once_and_files_point_at_them(make_world, sql) -> None:
    w = make_world()
    req = {"dataset_id": str(w.dataset_id), "acls": [GOLDEN, OPEN]}
    resp = w.edge.post(f"{E}/acls", req)
    assert resp.status_code == 200, resp.text
    ids = resp.json()["acl_ids"]
    assert set(ids) == {GOLDEN["sha256"], OPEN["sha256"]}
    # Again (new key): harmless, same ids.
    assert w.edge.post(f"{E}/acls", req).json()["acl_ids"] == ids
    # Replay of the same key: the stored response.
    key = str(uuid.uuid4())
    first = w.edge.post(f"{E}/acls", {"dataset_id": str(w.dataset_id), "acls": [OPEN]}, key=key)
    replay = w.edge.post(f"{E}/acls", {"dataset_id": str(w.dataset_id), "acls": [OPEN]}, key=key)
    assert replay.headers.get("Idempotent-Replayed") == "true" and replay.json() == first.json()

    row = sql.execute(
        "SELECT owner_sid, group_sid, mode, entries, read_sids::text[] FROM catalog.acl"
        " WHERE acl_id = %s",
        (int(ids[GOLDEN["sha256"]]),),
    ).fetchone()
    assert row[:3] == ("S-1-22-1-0", f"{D}-1105", 0o770)
    assert row[3] == GOLDEN_ENTRIES
    assert set(row[4]) == {"S-1-22-1-0", f"{D}-1103", f"{D}-1104", f"{D}-1105"}

    # A folder snapshot and a file override: both are acl pointers.
    folder = create_file(w, "proj", kind="dir")
    secret = create_file(w, "secret.segy", parent=folder)
    ops = [
        {
            "op_id": f"acl-{uuid.uuid4().hex}",
            "op": "acl",
            "file": {"file_id": str(folder)},
            "acl_sha256": OPEN["sha256"],
        },
        {
            "op_id": f"acl-{uuid.uuid4().hex}",
            "op": "acl",
            "file": {"file_id": str(secret)},
            "acl_sha256": GOLDEN["sha256"],
        },
    ]
    res = w.edge.post(f"{E}/changes", {"ops": ops}).json()["results"]
    assert [r["status"] for r in res] == ["applied", "applied"], res
    got = dict(
        sql.execute(
            "SELECT file_id, acl_id FROM catalog.file WHERE file_id IN (%s, %s)", (folder, secret)
        ).fetchall()
    )
    assert got == {folder: int(ids[OPEN["sha256"]]), secret: int(ids[GOLDEN["sha256"]])}


def test_acl_refusals(make_world) -> None:
    w = make_world()
    other = make_world()

    def code(payload, edge=w.edge) -> tuple[int, str]:
        resp = edge.post(f"{E}/acls", payload, check=False)
        return resp.status_code, resp.json().get("code", "")

    ds = str(w.dataset_id)
    # Another edge's dataset.
    assert code({"dataset_id": str(other.dataset_id), "acls": [OPEN]}) == (404, "not_found")
    # A body filed under another body's hash.
    forged = {**OPEN, "sha256": GOLDEN["sha256"]}
    assert code({"dataset_id": ds, "acls": [forged]}) == (400, "invalid_request")
    # Named entries need a SID, the others must not carry one; perms shape.
    for bad_entry in (
        {"perms": "rwx", "scope": "access", "tag": "user"},
        {"perms": "rwx", "scope": "access", "tag": "other", "sid": f"{D}-1"},
        {"perms": "rw", "scope": "access", "tag": "owner"},
        {"perms": "rwx", "scope": "both", "tag": "owner"},
    ):
        b = {**OPEN, "entries": [bad_entry]}
        assert code({"dataset_id": ds, "acls": [b]})[0] == 400, bad_entry
    assert code({"dataset_id": ds, "acls": [{**OPEN, "mode": 0o10000}]})[0] == 400
    assert code({"dataset_id": ds, "acls": [OPEN] * 1001})[0] == 400


def test_acl_scans_bracket_a_scan(make_world, sql) -> None:
    w = make_world()
    other = make_world()
    resp = w.edge.post(f"{E}/acl-scans", {"dataset_id": str(w.dataset_id), "kind": "full"})
    assert resp.status_code == 201, resp.text
    scan = resp.json()["scan_id"]
    path = f"{E}/acl-scans/{{scan_id}}/complete"
    done = {"entries_seen": 120, "entries_changed": 3}
    # Not another edge's scan, and not another edge's dataset.
    assert other.edge.post(path, done, scan_id=scan, check=False).status_code == 404
    refused = other.edge.post(
        f"{E}/acl-scans", {"dataset_id": str(w.dataset_id), "kind": "full"}, check=False
    )
    assert refused.status_code == 404
    bad = w.edge.post(path, {"entries_seen": 1, "entries_changed": 2}, scan_id=scan, check=False)
    assert bad.status_code == 400
    assert w.edge.post(path, done, scan_id=scan).status_code == 204
    assert w.edge.post(path, done, scan_id=scan).status_code == 204  # idempotent by scan_id
    clash = w.edge.post(path, {**done, "entries_seen": 121}, scan_id=scan, check=False)
    assert clash.status_code == 409
    assert sql.execute(
        "SELECT kind, edge_id, entries_seen, entries_changed, completed_at IS NOT NULL"
        "  FROM catalog.acl_scan WHERE acl_scan_id = %s",
        (int(scan),),
    ).fetchone() == ("full", w.edge.edge_id, 120, 3, True)


# --- POSIX.1e access-check order (M2 review N3) ---------------------------------

U = f"{D}-3001"  # a user
V = f"{D}-3002"  # another user
OWNER = f"{D}-3000"
G_STAFF = f"{D}-1200"
G_TEMPS = f"{D}-1201"
EVERYONE = "S-1-1-0"


def _e(tag: str, perms: str, sid: str | None = None, scope: str = "access") -> dict:
    out = {"perms": perms, "scope": scope, "tag": tag}
    if sid:
        out["sid"] = sid
    return out


# (name, owner, owning group, entries, [(user, groups, expected)])
CASES = [
    (
        "named user deny beats a granting group and other",
        OWNER,
        G_STAFF,
        [
            _e("owner", "rwx"),
            _e("user", "---", U),
            _e("owning_group", "r-x"),
            _e("mask", "rwx"),
            _e("other", "r--"),
        ],
        [(U, [G_STAFF], False), (V, [G_STAFF], True), (V, [], True), (OWNER, [], True)],
    ),
    (
        "mask limits named users and groups, not the owner or other",
        OWNER,
        G_STAFF,
        [
            _e("owner", "rw-"),
            _e("user", "rwx", U),
            _e("owning_group", "r-x"),
            _e("group", "rwx", G_TEMPS),
            _e("mask", "-wx"),
            _e("other", "r--"),
        ],
        [
            (U, [], False),  # named user: decided, and masked to -wx
            (V, [G_STAFF], False),  # matching groups, none grants after the mask
            (V, [G_TEMPS], False),
            (V, [], True),  # no matching entry: other
            (OWNER, [G_STAFF], True),  # owner entry, unmasked
        ],
    ),
    (
        "a matching group without r stops other; any granting group wins",
        OWNER,
        G_STAFF,
        [
            _e("owner", "rwx"),
            _e("owning_group", "---"),
            _e("group", "r--", G_TEMPS),
            _e("mask", "r--"),
            _e("other", "r--"),
        ],
        [
            (V, [G_STAFF], False),
            (V, [G_STAFF, G_TEMPS], True),
            (V, [G_TEMPS], True),
            (V, [f"{D}-9999"], True),
        ],
    ),
    (
        "the owner entry decides for the owner, whatever the groups",
        OWNER,
        G_STAFF,
        [_e("owner", "-w-"), _e("owning_group", "r--"), _e("other", "r--")],
        [(OWNER, [G_STAFF], False), (U, [G_STAFF], True), (U, [], True)],
    ),
    (
        "default entries never grant access",
        OWNER,
        G_STAFF,
        [
            _e("owner", "rwx"),
            _e("owning_group", "---"),
            _e("other", "---"),
            _e("other", "r--", scope="default"),
            _e("user", "rwx", U, scope="default"),
        ],
        [(U, [], False), (V, [G_STAFF], False), (OWNER, [], True)],
    ),
]


def test_posix_access_order(sql) -> None:
    from omnifile_service.api.acls import access_sets, can_read

    for name, owner, group, entries, checks in CASES:
        read, deny = access_sets(owner, group, entries)
        for user, groups, want in checks:
            assert can_read(read, deny, user, groups) is want, (name, user, groups)
            # The catalog function agrees (it is what portal/Teams/API checks use).
            (got,) = sql.execute(
                "SELECT catalog.acl_can_read(%s::catalog.sid[], %s::catalog.sid[], %s, %s)",
                (read, deny, user, groups),
            ).fetchone()
            assert got is want, (name, user, groups)
            # The GIN prefilter never loses a reader.
            if want:
                assert {user, *groups, EVERYONE} & set(read), (name, user)


def test_stored_acl_carries_deny_sids(make_world, sql) -> None:
    w = make_world()
    name, owner, group, entries, _ = CASES[0]
    b = body(owner, group, 0o750, entries)
    ids = w.edge.post(f"{E}/acls", {"dataset_id": str(w.dataset_id), "acls": [b]}).json()
    acl_id = int(ids["acl_ids"][b["sha256"]])
    read, deny, u_ok, v_ok = sql.execute(
        "SELECT read_sids::text[], deny_sids::text[],"
        "       catalog.acl_can_read(read_sids, deny_sids, %s, %s),"
        "       catalog.acl_can_read(read_sids, deny_sids, %s, %s)"
        "  FROM catalog.acl WHERE acl_id = %s",
        (U, [G_STAFF], V, [G_STAFF], acl_id),
    ).fetchone()
    assert U in deny and U not in read and EVERYONE in read
    assert (u_ok, v_ok) == (False, True)
