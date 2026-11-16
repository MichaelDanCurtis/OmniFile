"""Who made a namespace change on the edge (control-api.md 4.1 `actor`,
migration 0030; ported from line B, PR #10: d981ee9). The versions an op
writes record the actor, with proof derived by the service (decision 28)
and the catalog identity of its SID or uid."""

from __future__ import annotations

import uuid

from .conftest import E, World, create_file

SID = "S-1-5-21-1-2-3-1107"


def op(op_name: str, **fields) -> dict:
    return {"op_id": f"op-{uuid.uuid4().hex}", "op": op_name, **fields}


def changes(w: World, *ops: dict) -> list[dict]:
    resp = w.edge.post(f"{E}/changes", {"ops": list(ops)})
    assert resp.status_code == 200, resp.text
    return resp.json()["results"]


def actor_of(sql, version_id: str) -> tuple:
    return sql.execute(
        "SELECT kind, actor_via, actor_uid, actor_sid, actor_sec, actor_proof,"
        " host(actor_client_addr), actor_identity_id"
        " FROM catalog.file_version WHERE version_id = %s",
        (int(version_id),),
    ).fetchone()


def test_each_op_records_its_actor_with_the_services_proof(make_world, sql) -> None:
    # The world's one share is NFSv4 Kerberos-only: trust level proven.
    w = make_world()
    f = create_file(w, "a.segy")
    d = create_file(w, "dir", kind="dir")
    krb = {"via": "nfs", "uid": 41023, "sid": SID, "sec": "krb5p", "client_addr": "10.1.2.3"}
    sys_ = {"via": "nfs", "uid": 41023, "sid": SID, "sec": "sys", "proof": "proven"}
    res = changes(
        w,
        op(
            "rename",
            file={"file_id": str(f)},
            new_parent={"file_id": str(d)},
            new_name="b.segy",
            actor=krb,
        ),  # fmt: skip
        op("setattr", file={"file_id": str(f)}, attrs={"uid": 5, "gid": 6}, actor=sys_),
        op(
            "create",
            parent={"file_id": str(d)},
            name="sub",
            kind="dir",
            actor={"via": "smb", "uid": 41023, "sid": SID, "proof": "proven"},
        ),  # fmt: skip
        op("unlink", file={"file_id": str(f)}, actor={"via": "local", "uid": 0}),
    )
    assert [r["status"] for r in res] == ["applied"] * 4, res
    ident = sql.execute("SELECT identity_id FROM catalog.identity WHERE sid = %s", (SID,))
    (identity_id,) = ident.fetchone()
    # Kerberos NFS on a Kerberos-only share: proven. The SID became an identity.
    assert actor_of(sql, res[0]["version_id"]) == (
        "rename", "nfs", 41023, SID, "krb5p", "proven", "10.1.2.3", identity_id,
    )  # fmt: skip
    # sec=sys is only claimed, whatever the edge says.
    assert actor_of(sql, res[1]["version_id"])[1:6] == ("nfs", 41023, SID, "sys", "claimed")
    # SMB on a dataset with no proven SMB share: claimed.
    assert actor_of(sql, res[2]["version_id"])[1:6] == ("smb", 41023, SID, None, "claimed")
    # A local process: claimed; root has no identity.
    assert actor_of(sql, res[3]["version_id"]) == (
        "tombstone", "local", 0, None, None, "claimed", None, None,
    )  # fmt: skip


def test_unknown_carries_no_identity_and_no_actor_records_nothing(make_world, sql) -> None:
    w = make_world()
    f = create_file(w, "a.segy")
    g = create_file(w, "b.segy")
    # The reconcile pass found it: anything else sent with it is ignored.
    found = op("unlink", file={"file_id": str(f)},
               actor={"via": "unknown", "uid": 7, "sid": SID, "sec": "sys"})  # fmt: skip
    res = changes(w, found, op("unlink", file={"file_id": str(g)}))
    assert [r["status"] for r in res] == ["applied"] * 2
    assert actor_of(sql, res[0]["version_id"]) == (
        "tombstone", "unknown", None, None, None, "unknown", None, None,
    )  # fmt: skip
    assert actor_of(sql, res[1]["version_id"])[1:] == (None,) * 7
    # A replay is a duplicate with the first answer: nothing recorded twice.
    (again,) = changes(w, found)
    assert again["status"] == "duplicate" and again["version_id"] == res[0]["version_id"]
    n = sql.execute("SELECT count(*) FROM catalog.file_version WHERE file_id = %s", (f,))
    assert n.fetchone()[0] == 2  # its create and its tombstone


def test_a_uid_with_a_known_identity_and_a_refused_sid(make_world, sql) -> None:
    w = make_world()
    (known,) = sql.execute(
        "INSERT INTO catalog.identity (sid, posix_id) VALUES ('S-1-5-21-1-2-3-1200', 20002)"
        " RETURNING identity_id"
    ).fetchone()
    f = create_file(w, "a.segy")
    g = create_file(w, "b.segy")
    res = changes(
        w,
        # No SID (winbind had none): the identity of the uid.
        op("unlink", file={"file_id": str(f)}, actor={"via": "nfs", "uid": 20002, "sec": "sys"}),
        # A SID of another domain is not added as an identity, and the uid
        # isn't used for it either.
        op("unlink", file={"file_id": str(g)},
           actor={"via": "nfs", "uid": 20002, "sid": "S-1-5-21-9-9-9-1107", "sec": "sys"}),
    )  # fmt: skip
    assert [r["status"] for r in res] == ["applied"] * 2
    assert actor_of(sql, res[0]["version_id"])[7] == known
    assert actor_of(sql, res[1]["version_id"])[7] is None
    assert actor_of(sql, res[1]["version_id"])[3] == "S-1-5-21-9-9-9-1107"


def test_a_bad_actor_is_a_400(make_world) -> None:
    w = make_world()
    f = create_file(w, "a.segy")
    for actor in (
        "nfs",
        {"via": "ftp"},
        {"via": "nfs", "uid": -1},
        {"via": "nfs", "uid": 0xFFFFFFFF},
        {"via": "nfs", "sid": "not-a-sid"},
        {"via": "nfs", "sec": "md5"},
        {"via": "smb", "proof": "certain"},
        {"via": "smb", "client_addr": "x" * 65},
    ):
        resp = w.edge.post(
            f"{E}/changes",
            {"ops": [op("unlink", file={"file_id": str(f)}, actor=actor)]},
            check=False,
        )
        assert resp.status_code == 400, (actor, resp.text)
