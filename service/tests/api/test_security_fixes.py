"""Service-side fixes from the M1 security review (sections 2, 3, 4.2-4.4):
upload `complete` checks the committed blocks and pins the blob version;
stub leases carry a 128-bit MAC; requester proof is decided by the service;
new requester SIDs are validated and rate-limited."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from omnifile_service.api.__main__ import forwarded_source
from omnifile_service.api.context import (
    WindowLimiter,
    check_lease,
    lease_key_from_env,
    make_lease,
)
from omnifile_service.api.problems import ApiError
from omnifile_service.api.recalls import NOTE_NAME_MAX, note_safe_name, note_text

from .conftest import E, blocks_of, complete_body, create_file, upload_blocks
from .test_upload_flow import DATA, archive, intent, read_req, upload

COMPLETE = f"{E}/uploads/{{upload_id}}/complete"


# --- complete: committed blocks and the pinned version ------------------------


def test_complete_needs_blocks_that_combine_to_the_crc(make_world, facade_client):
    w = make_world()
    fid = create_file(w, "c.bin")
    up = w.edge.post(f"{E}/uploads", intent(w, fid, DATA)).json()
    upload_blocks(facade_client, up["target"]["url"], DATA, 4096)
    body = complete_body(DATA)

    no_blocks = {k: v for k, v in body.items() if k != "blocks"}
    resp = w.edge.post(COMPLETE, no_blocks, upload_id=up["upload_id"], check=False)
    assert resp.status_code == 400 and "blocks" in resp.json()["detail"]

    wrong_crc = {**body, "blocks": [dict(b) for b in body["blocks"]]}
    wrong_crc["blocks"][1]["crc64nvme"] = "0" * 16
    resp = w.edge.post(COMPLETE, wrong_crc, upload_id=up["upload_id"])
    assert resp.status_code == 400 and "combine" in resp.json()["detail"]

    short = {**body, "blocks": body["blocks"][:-1]}
    resp = w.edge.post(COMPLETE, short, upload_id=up["upload_id"])
    assert resp.status_code == 400 and "add up" in resp.json()["detail"]

    # Declared with another split than the one committed (same bytes, same CRC).
    other_split = {**body, "blocks": blocks_of(DATA, 2048)}
    resp = w.edge.post(COMPLETE, other_split, upload_id=up["upload_id"])
    assert resp.status_code == 400 and "block list differs" in resp.json()["detail"]

    assert w.edge.post(COMPLETE, body, upload_id=up["upload_id"]).status_code == 200


def test_complete_pins_the_version_the_edge_committed(make_world, facade_client, sql):
    """Someone holding the (still valid) write SAS overwrites the blob after the
    edge committed: the catalog keeps, and reads use, the edge's version."""
    w = make_world()
    fid = create_file(w, "pinned.segy")
    up = w.edge.post(f"{E}/uploads", intent(w, fid, DATA)).json()
    url = up["target"]["url"]
    mine = upload_blocks(facade_client, url, DATA, 4096)
    assert mine
    evil = bytes(reversed(DATA))
    theirs = upload_blocks(facade_client, url, evil, 2048)
    assert theirs and theirs != mine

    # Without the version, the current (overwritten) blob is checked and refused.
    resp = w.edge.post(COMPLETE, complete_body(DATA), upload_id=up["upload_id"])
    assert resp.status_code == 400 and "block list differs" in resp.json()["detail"]
    # With it, the edge's version is checked and recorded.
    done = w.edge.post(
        COMPLETE, complete_body(DATA, backend_version_id=mine), upload_id=up["upload_id"]
    )
    assert done.status_code == 200, done.text
    assert sql.execute(
        "SELECT backend_version_id FROM catalog.location WHERE version_id = %s",
        (int(up["version_id"]),),
    ).fetchone() == (mine,)
    # Reads name that version and get the edge's bytes.
    cred = w.edge.post(
        f"{E}/credentials", {"access": "read", "file_id": str(fid), "version_id": up["version_id"]}
    ).json()
    assert parse_qs(urlsplit(cred["url"]).query)["versionid"] == [mine]
    got = facade_client.get(cred["url"], headers={"x-ms-version": "2026-04-06"})
    assert got.status_code == 200 and got.content == DATA
    # A version id that isn't this blob's is refused.
    fid2 = create_file(w, "other.segy")
    data2 = DATA + b"!"
    up2 = w.edge.post(f"{E}/uploads", intent(w, fid2, data2)).json()
    upload_blocks(facade_client, up2["target"]["url"], data2, 4096)
    resp = w.edge.post(
        COMPLETE, complete_body(data2, backend_version_id="nope"), upload_id=up2["upload_id"]
    )
    assert resp.status_code == 400


def test_write_sas_lasts_15_minutes_and_read_60(make_world, facade_client):
    w = make_world()
    fid = create_file(w, "ttl.bin")
    up, done = upload(w, facade_client, fid)
    now = datetime.now(UTC)

    def left(cred: dict) -> float:
        return (datetime.fromisoformat(cred["expires_at"].replace("Z", "+00:00")) - now).seconds

    assert 800 < left(up["target"]) <= 900
    read = w.edge.post(
        f"{E}/credentials",
        {"access": "read", "file_id": str(fid), "version_id": done["version_id"]},
    ).json()
    assert 3500 < left(read) <= 3600


# --- stub leases ----------------------------------------------------------------


def test_stub_lease_has_a_128_bit_mac_and_the_key_32_bytes() -> None:
    key = b"k" * 32
    exp = datetime.now(UTC) + timedelta(minutes=10)
    lease, _ = make_lease(key, 1, 2, 3, 1024, exp)
    expiry, mac = lease.split(".")
    assert len(mac) == 32  # 128 bits, hex
    now = datetime.now(UTC)
    check_lease(key, lease, 1, 2, 3, 1024, now)
    for args in ((1, 2, 3, 1025), (1, 2, 4, 1024), (9, 2, 3, 1024)):
        with pytest.raises(ApiError):
            check_lease(key, lease, *args, now)
    flipped = f"{expiry}.{'0' if mac[0] != '0' else '1'}{mac[1:]}"
    for bad in (flipped, f"{int(expiry) + 60}.{mac}", "123", "7.zz", str(2**62)):
        with pytest.raises(ApiError):
            check_lease(key, bad, 1, 2, 3, 1024, now)
    with pytest.raises(ApiError, match="expired"):
        check_lease(key, lease, 1, 2, 3, 1024, now + timedelta(minutes=12))

    assert lease_key_from_env(None) is None
    assert lease_key_from_env(base64.b64encode(b"x" * 32).decode()) == b"x" * 32
    for value in (base64.b64encode(b"x" * 31).decode(), "not base64!"):
        with pytest.raises(ValueError):
            lease_key_from_env(value)


# --- recalls: proof and SIDs -----------------------------------------------------


def _recall(w, facade_client, sql, name: str) -> tuple[int, str]:
    fid = create_file(w, name)
    _, done = upload(w, facade_client, fid)
    archive(sql, fid)
    return fid, done["version_id"]


def _ask(w, fid: int, vid: str, uid: int, **requester) -> dict:
    body = read_req(fid, vid, uid=uid)
    body["requester"] = {"uid": uid, **requester}
    resp = w.edge.post(f"{E}/recalls", body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _proof(sql, recall_id: str, uid: int) -> str:
    return sql.execute(
        "SELECT proof FROM catalog.recall_requester WHERE recall_id = %s AND uid = %s",
        (int(recall_id), uid),
    ).fetchone()[0]


def test_proof_is_decided_by_the_service(make_world, facade_client, sql):
    w = make_world()  # one NFSv4 share, sec=krb5 only: trust_level proven
    fid, vid = _recall(w, facade_client, sql, "proof.segy")
    r = _ask(w, fid, vid, 1, via="nfs", sec="krb5p", proof="proven")["recall_id"]
    assert _proof(sql, r, 1) == "proven"
    # The edge can't raise it...
    _ask(w, fid, vid, 2, via="nfs", sec="sys", proof="proven")
    assert _proof(sql, r, 2) == "claimed"
    _ask(w, fid, vid, 3, via="nfs", proof="proven")  # no flavour
    assert _proof(sql, r, 3) == "claimed"
    _ask(w, fid, vid, 4, via="smb", sec="kerberos", proof="proven")  # no SMB share
    assert _proof(sql, r, 4) == "claimed"
    _ask(w, fid, vid, 5, via="smb", sec="krb5", proof="proven")  # NFS flavour over SMB
    assert _proof(sql, r, 5) == "claimed"
    # ...but can lower it, and needn't send it at all.
    _ask(w, fid, vid, 6, via="nfs", sec="krb5", proof="claimed")
    assert _proof(sql, r, 6) == "claimed"
    _ask(w, fid, vid, 7, via="nfs", sec="krb5")
    assert _proof(sql, r, 7) == "proven"
    edge_said = sql.execute(
        "SELECT details->>'edge_proof' FROM catalog.audit_log"
        " WHERE action = 'recall.request' AND details->>'recall_id' = %s"
        "   AND (details->>'uid')::int = 2",
        (r,),
    ).fetchone()
    assert edge_said == ("proven",)

    # An SMB share that is proven makes SMB proven.
    sql.execute(
        "INSERT INTO catalog.share (dataset_id, edge_id, name, protocols, owners_group_sid)"
        " VALUES (%s, %s, %s, ARRAY['smb'], 'S-1-5-21-1-2-3-512')",
        (w.dataset_id, w.edge.edge_id, f"smb-{w.share_id}"),
    )
    _ask(w, fid, vid, 8, via="smb", sec="kerberos", proof="proven")
    assert _proof(sql, r, 8) == "proven"
    # A host-trusted (sec=sys) share of the same dataset: nothing over NFS or
    # SMB is proven any more, Kerberos or not.
    sql.execute(
        "INSERT INTO catalog.share (dataset_id, edge_id, name, protocols, nfs_sec, nfs_clients,"
        " owners_group_sid) VALUES (%s, %s, %s, ARRAY['nfs3', 'smb'], ARRAY['sys'],"
        " ARRAY['10.1.0.0/16'], 'S-1-5-21-1-2-3-512')",
        (w.dataset_id, w.edge.edge_id, f"sys-{w.share_id}"),
    )
    _ask(w, fid, vid, 9, via="nfs", sec="krb5", proof="proven")
    _ask(w, fid, vid, 10, via="smb", sec="kerberos", proof="proven")
    assert _proof(sql, r, 9) == _proof(sql, r, 10) == "claimed"


def _notifies(sql, recall_id: str) -> int:
    return sql.execute(
        "SELECT count(*) FROM pgmq.q_notify WHERE message->>'recall_id' = %s", (recall_id,)
    ).fetchone()[0]


def _identity(sql, sid: str):
    return sql.execute("SELECT identity_id FROM catalog.identity WHERE sid = %s", (sid,)).fetchone()


def _rejected(sql, recall_id: str, uid: int):
    row = sql.execute(
        "SELECT details->>'sid_rejected' FROM catalog.audit_log"
        " WHERE action = 'recall.request' AND details->>'recall_id' = %s"
        "   AND (details->>'uid')::int = %s",
        (recall_id, uid),
    ).fetchone()
    return row[0] if row else None


def test_new_sids_are_validated_and_rate_limited(api, make_world, facade_client, sql):
    w = make_world()  # its share's groups are in domain S-1-5-21-1-2-3
    fid, vid = _recall(w, facade_client, sql, "sids.segy")
    req = {"via": "nfs", "sec": "krb5"}
    r = _ask(w, fid, vid, 100, **req)["recall_id"]
    base = _notifies(sql, r)
    assert base == 1  # the first requester (uid only)

    bad = {
        101: ("S-1-5-21-9-9-9-1001", "unknown_domain"),
        102: ("S-1-5-18", "not_a_domain_account"),  # LocalSystem
        103: ("S-1-5-21-1-2-3-512", "well_known_rid"),  # Domain Admins (a group)
    }
    for uid, (sid, why) in bad.items():
        _ask(w, fid, vid, uid, sid=sid, **req)
        assert _identity(sql, sid) is None, sid
        assert _rejected(sql, r, uid) == why
    assert _notifies(sql, r) == base  # no cards for SIDs that failed

    good = "S-1-5-21-1-2-3-5001"
    _ask(w, fid, vid, 104, sid=good, **req)
    assert _identity(sql, good) is not None
    assert _rejected(sql, r, 104) is None
    assert _notifies(sql, r) == base + 1
    # A SID the catalog knows passes without counting as new.
    _ask(w, fid, vid, 105, sid=good, **req)
    assert _rejected(sql, r, 105) is None

    # At most N new SIDs per edge per window.
    api.services.new_sids = WindowLimiter(2, 600)
    _ask(w, fid, vid, 106, sid="S-1-5-21-1-2-3-5002", **req)
    _ask(w, fid, vid, 107, sid="S-1-5-21-1-2-3-5003", **req)
    _ask(w, fid, vid, 108, sid="S-1-5-21-1-2-3-5004", **req)
    assert _identity(sql, "S-1-5-21-1-2-3-5003") is not None
    assert _identity(sql, "S-1-5-21-1-2-3-5004") is None
    assert _rejected(sql, r, 108) == "rate_limited"
    # A SID-only requester that fails is audited but not recorded or notified.
    before = _notifies(sql, r)
    body = read_req(fid, vid)
    body["requester"] = {"sid": "S-1-5-21-7-7-7-1001", **req}
    assert w.edge.post(f"{E}/recalls", body).status_code == 200
    assert _notifies(sql, r) == before


# --- the recall note -------------------------------------------------------------


def test_note_escapes_control_characters_and_caps_the_name() -> None:
    evil = "a\nFAKE: recall cancelled\r\x1b[2J\x00\x7f\x85 ‮b.segy"
    note = note_text(evil, None, "fail")
    first = note.splitlines()[0]
    assert first.startswith("a\\x0aFAKE: recall cancelled\\x0d\\x1b[2J\\x00\\x7f\\x85")
    assert "\\u2028\\u202eb.segy is archived." in first
    assert len(note.splitlines()) == 4  # only the note's own lines
    assert not any(
        (ord(c) < 0x20 and c != "\n") or 0x7F <= ord(c) <= 0x9F or c in " ‮" for c in note
    )
    long = note_safe_name("é" * 1000)
    assert len(long) == NOTE_NAME_MAX and long.endswith("…")
    assert note_safe_name("line_1042.segy") == "line_1042.segy"


def test_recall_note_over_the_api_has_no_raw_control_characters(make_world, facade_client, sql):
    w = make_world()
    fid, vid = _recall(w, facade_client, sql, "x\ny.segy")
    note = _ask(w, fid, vid, 1, via="nfs", sec="sys")["note"]
    assert note.startswith("x\\x0ay.segy is archived.")


# --- re-review: verification by read-back, replay, any-address ranges ----------


def test_upload_is_unverified_until_read_back_and_cannot_stub(make_world, facade_client, sql):
    """The facade (like Azure) lists no block CRCs, so `complete` must not mark
    the copy verified from the edge's CRCs: it queues a read-back."""
    w = make_world()
    fid = create_file(w, "unverified.segy")
    up = w.edge.post(f"{E}/uploads", intent(w, fid, DATA)).json()
    upload_blocks(facade_client, up["target"]["url"], DATA, 4096)
    assert w.edge.post(COMPLETE, complete_body(DATA), upload_id=up["upload_id"]).status_code == 200
    loc = sql.execute(
        "SELECT location_id, verified, verified_by FROM catalog.location WHERE version_id = %s",
        (int(up["version_id"]),),
    ).fetchone()
    assert loc[1:] == (False, None)
    queued = sql.execute(
        "SELECT count(*) FROM pgmq.q_verify WHERE message->>'location_id' = %s", (str(loc[0]),)
    ).fetchone()[0]
    assert queued == 1
    refused = w.edge.post(
        f"{E}/files/{{file_id}}/stub/prepare", {"version_id": up["version_id"]}, file_id=fid
    )
    assert refused.status_code == 409 and "not verified" in refused.json()["detail"]

    assert loc[0] in w.verify()
    assert sql.execute(
        "SELECT verified, verified_by FROM catalog.location WHERE location_id = %s", (loc[0],)
    ).fetchone() == (True, "azure_read_crc")
    ok = w.edge.post(
        f"{E}/files/{{file_id}}/stub/prepare", {"version_id": up["version_id"]}, file_id=fid
    )
    assert ok.status_code == 200, ok.text


def test_read_back_mismatch_stays_unverified_and_is_audited(make_world, facade_client, sql):
    """The edge declares CRCs for bytes it never uploaded (consistent with each
    other, so `complete` accepts them): the read-back catches it."""
    w = make_world()
    fid = create_file(w, "liar.segy")
    claimed = b"Z" * len(DATA)
    up = w.edge.post(f"{E}/uploads", intent(w, fid, claimed)).json()
    upload_blocks(facade_client, up["target"]["url"], DATA, 4096)  # other bytes, same size
    body = complete_body(claimed)
    body["blocks"] = [
        {**b, "id": o["id"]}
        for b, o in zip(body["blocks"], complete_body(DATA)["blocks"], strict=True)
    ]
    assert w.edge.post(COMPLETE, body, upload_id=up["upload_id"]).status_code == 200
    (loc,) = sql.execute(
        "SELECT location_id FROM catalog.location WHERE version_id = %s", (int(up["version_id"]),)
    ).fetchone()
    assert loc in w.verify()
    assert sql.execute(
        "SELECT verified FROM catalog.location WHERE location_id = %s", (loc,)
    ).fetchone() == (False,)
    assert sql.execute(
        "SELECT count(*) FROM catalog.audit_log WHERE action = 'location.verify'"
        " AND decision = 'deny' AND details->>'location_id' = %s",
        (str(loc),),
    ).fetchone() == (1,)
    refused = w.edge.post(
        f"{E}/files/{{file_id}}/stub/prepare", {"version_id": up["version_id"]}, file_id=fid
    )
    assert refused.status_code == 409


def test_a_backend_with_block_crcs_verifies_at_complete(api, make_world, facade, sql):
    """A backend that lists block CRCs (the sim) verifies at `complete`, and
    refuses a block whose CRC differs from the declared one."""
    from omnifile_service.api.dataplane import BackendDataPlane

    api.services.planes.add(api.backend_id, BackendDataPlane(facade.sim))
    w = make_world()
    fid = create_file(w, "sim.segy")
    up = w.edge.post(f"{E}/uploads", intent(w, fid, DATA)).json()
    run_put = __import__("asyncio").run

    async def chunks():
        for i in range(0, len(DATA), 4096):
            yield DATA[i : i + 4096]

    ref = run_put(facade.sim.put("live/data", _key(sql, up), chunks(), None, {}))
    sim_blocks = run_put(facade.sim.committed_blocks(ref))
    body = complete_body(DATA, backend_version_id=ref.version_id)
    body["blocks"] = [
        {**b, "id": s.block_id} for b, s in zip(body["blocks"], sim_blocks, strict=True)
    ]
    bad = {**body, "blocks": [dict(b) for b in body["blocks"]]}
    bad["blocks"][0]["crc64nvme"], bad["blocks"][1]["crc64nvme"] = (
        body["blocks"][1]["crc64nvme"],
        body["blocks"][0]["crc64nvme"],
    )
    resp = w.edge.post(COMPLETE, bad, upload_id=up["upload_id"])
    assert resp.status_code == 400
    assert w.edge.post(COMPLETE, body, upload_id=up["upload_id"]).status_code == 200
    assert sql.execute(
        "SELECT verified, verified_by FROM catalog.location WHERE version_id = %s",
        (int(up["version_id"]),),
    ).fetchone() == (True, "upload_block_crc")


def _key(sql, up) -> str:
    return sql.execute(
        "SELECT object_key FROM catalog.location WHERE version_id = %s", (int(up["version_id"]),)
    ).fetchone()[0]


def test_a_late_renewal_retry_replays_the_issued_certificate(api, enrol_edge) -> None:
    """The stored response is returned before the proof's time window is
    checked, so a retry after a long outage still gets its certificate."""
    from .conftest import csr_pem, new_key, renewal_body

    edge = enrol_edge()
    body = renewal_body(edge, csr_pem(new_key()))
    key = "renew-late-" + "k" * 16
    first = edge.post(f"{E}/certificate", body, key=key)
    assert first.status_code == 201
    real = api.services.clock
    api.services.clock = lambda: real() + timedelta(hours=2)
    try:
        late = edge.post(f"{E}/certificate", body, key=key)
    finally:
        api.services.clock = real
    assert late.status_code == 201 and late.json()["cert_pem"] == first.json()["cert_pem"]
    assert late.headers.get("Idempotent-Replayed") == "true"
    # A NEW request carrying that proof two hours on is refused.
    api.services.clock = lambda: real() + timedelta(hours=2)
    try:
        stale = edge.post(f"{E}/certificate", body, key="renew-new-" + "k" * 16, check=False)
    finally:
        api.services.clock = real
    assert stale.status_code == 401 and stale.json()["code"] == "certificate_invalid"


def test_any_address_ranges_are_refused() -> None:
    from omnifile_service.api.clientauth import parse_networks

    for cidr in ("0.0.0.0/0", "::/0", "10.0.0.0/16, 0.0.0.0/0"):
        with pytest.raises(ValueError):
            parse_networks(cidr.split(","))
    with pytest.raises(SystemExit):
        forwarded_source("true", "0.0.0.0/0")
    assert parse_networks(["10.0.0.0/16"])


def test_near_any_address_ranges_are_refused() -> None:
    """Review of #34: refusing only /0 let 0.0.0.0/1,128.0.0.0/1 trust everyone. A trust list
    names nothing wider than /16 (IPv4) or /48 (IPv6)."""
    from omnifile_service.api.clientauth import parse_networks

    for cidr in ("0.0.0.0/1,128.0.0.0/1", "10.0.0.0/8", "10.0.0.0/15", "::/1", "2001:db8::/47"):
        with pytest.raises(ValueError, match="wider"):
            parse_networks(cidr.split(","))
    assert parse_networks(["10.0.0.0/16", "10.60.0.0/26", "2001:db8::/48", "10.1.2.3"])


def test_worker_reads_sim_http_back_in_ranges(facade, facade_client) -> None:
    """The e2e worker reaches the facade over HTTP (`SimHttpHeadBackend`): its
    `get_range` must return every byte, in 8 MiB ranged GETs."""
    import asyncio

    import httpx

    from omnifile_service.api.dataplane import SimHttpDataPlane
    from omnifile_service.backends.base import ObjectRef
    from omnifile_service.backends.sim_http import create_app as create_facade
    from omnifile_service.worker.backends import SimHttpHeadBackend
    from omnifile_service.worker.verify import read_crc

    from .conftest import SIM_BASE

    data = bytes(range(256)) * (9 * 4096 + 7)  # just over 9 MiB: two ranges
    url = SIM_BASE + "/live/data/rb.bin?" + facade.mint_sas("live", "data", "rb.bin", "cw")
    version = upload_blocks(facade_client, url, data, 4 * 1024 * 1024)

    async def go():
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_facade(facade)), base_url=SIM_BASE
        )
        b = SimHttpHeadBackend(1, SimHttpDataPlane(SIM_BASE, facade.key, http=http))
        try:
            ref = ObjectRef(1, "live/data", "rb.bin", version)
            got = b"".join([c async for c in b.get_range(ref, 0, len(data) + 1)])
            n, crc = await read_crc(b, ref, len(data))
            return got, n, crc
        finally:
            await b.close()

    got, n, crc = asyncio.run(go())
    from omnifile_service.backends.sim.crc64 import crc64nvme

    assert got == data and n == len(data) and crc == crc64nvme(data)
