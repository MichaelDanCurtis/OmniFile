"""The real-Azure checks that need the VNet, run on the test VM with its managed identity.

    azure_checks.py ID      (stdin: {"rg", "prefix", "args", "outputs", "facts"})

One JSON line back: {"ok": true, "evidence": {...}} | {"ok": true, "status": "partial",
"left": ...} | {"ok": false, "error": ...}. Scratch blobs go in a container `ofreal-checks`
of the live and archive accounts, which teardown removes with the resource group.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta


def say(o: dict) -> None:
    print(json.dumps(o, default=str))


def svc(account: str):
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import BlobServiceClient

    return BlobServiceClient(
        f"https://{account}.blob.core.windows.net", credential=ManagedIdentityCredential()
    )


def scratch(account: str):
    s = svc(account)
    c = s.get_container_client("ofreal-checks")
    if not c.exists():
        c.create_container()
    return s, c


def crc64(data: bytes) -> bytes:
    from azure.storage.extensions import checksums

    return checksums.crc64(data).to_bytes(8, "little")


def live(cfg):
    return cfg["outputs"].get("liveAccount") or f"st{cfg['prefix']}live"


def arch(cfg):
    return cfg["outputs"].get("archiveAccount") or f"st{cfg['prefix']}arch"


def ra01(cfg):
    from azure.core.exceptions import HttpResponseError

    _, c = scratch(live(cfg))
    b = c.get_blob_client("ra01")
    data = os.urandom(4096)
    bad = base64.b64encode(crc64(data[::-1])).decode()
    try:
        b.stage_block("YmxvY2sx", data, headers={"x-ms-content-crc64": bad})
    except HttpResponseError as e:
        return {"status": e.status_code, "error_code": e.error_code}
    raise RuntimeError("a block with a wrong x-ms-content-crc64 was accepted")


def ra02(cfg):
    """Write, overwrite, set tier and delete a blob, then read the change feed's records."""
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob.changefeed import ChangeFeedClient  # azure-storage-blob-changefeed

    _, c = scratch(live(cfg))
    b = c.get_blob_client(f"ra02-{int(time.time())}")
    b.upload_blob(b"v1")
    b.upload_blob(b"v2", overwrite=True)
    b.set_standard_blob_tier("Cool")
    b.delete_blob()
    t0 = datetime.now(UTC) - timedelta(minutes=5)
    cf = ChangeFeedClient(
        f"https://{live(cfg)}.blob.core.windows.net", credential=ManagedIdentityCredential()
    )
    deadline = time.time() + 1800
    seen = []
    while time.time() < deadline:
        seen = [e for e in cf.list_changes(start_time=t0) if b.blob_name in e.get("subject", "")]
        if len({e["eventType"] for e in seen}) >= 3:
            break
        time.sleep(60)
    return {
        "events": [
            {
                "eventType": e["eventType"],
                "schemaVersion": e.get("schemaVersion"),
                "fields": sorted(e.get("data", {})),
            }
            for e in seen
        ]
    }


def ra03(cfg):
    from azure.core.exceptions import HttpResponseError

    sa, ca = scratch(arch(cfg))
    _, cl = scratch(live(cfg))
    src = ca.get_blob_client("ra03")
    src.upload_blob(os.urandom(1 << 20), overwrite=True, standard_blob_tier="Archive")
    dst = cl.get_blob_client("ra03-rehydrated")
    try:
        r = dst.start_copy_from_url(src.url, rehydrate_priority="Standard", requires_sync=False)
        return {"accepted": True, "copy_status": r.get("copy_status")}
    except HttpResponseError as e:
        return {
            "accepted": False,
            "status": e.status_code,
            "error_code": e.error_code,
            "finding": "keep the recall copy in the archived blob's own account",
        }


def ra07(cfg):
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob.changefeed import ChangeFeedClient

    _, c = scratch(live(cfg))
    n = cfg["args"].get("blobs", 500)
    tag = f"ra07-{int(time.time())}"
    t0 = time.time()
    for i in range(n):
        c.upload_blob(f"{tag}/{i:05d}", b"x", overwrite=True)
    cf = ChangeFeedClient(
        f"https://{live(cfg)}.blob.core.windows.net", credential=ManagedIdentityCredential()
    )
    start = datetime.now(UTC) - timedelta(minutes=10)
    while time.time() - t0 < 3600:
        got = sum(1 for e in cf.list_changes(start_time=start) if tag in e.get("subject", ""))
        if got >= n:
            return {"blobs": n, "all_in_feed_after_s": round(time.time() - t0)}
        time.sleep(30)
    return {"blobs": n, "in_feed_after_1h": got}


def ra08(cfg):
    _, c = scratch(live(cfg))
    name = f"ra08-{int(time.time())}"
    for v in (b"a", b"bb", b"ccc"):
        c.upload_blob(name, v, overwrite=True)
    vs = [b for b in c.list_blobs(name_starts_with=name, include=["versions"])]
    rows = [{"version_id": b.version_id, "tier": b.blob_tier, "size": b.size} for b in vs]
    if len(rows) != 3 or any(r["version_id"] is None for r in rows):
        raise RuntimeError(f"want 3 versions with ids, got {rows}")
    return {"versions": rows}


def ra09(cfg):
    from azure.core.exceptions import HttpResponseError

    _, c = scratch(live(cfg))
    b = c.get_blob_client(f"ra09-{int(time.time())}")
    cur = b.upload_blob(b"current")["version_id"]
    try:
        c.get_blob_client(b.blob_name, version_id=cur).delete_blob()
        return {"deleted_current_by_version_id": True}
    except HttpResponseError as e:
        return {
            "deleted_current_by_version_id": False,
            "status": e.status_code,
            "error_code": e.error_code,
            "finding": "the archive move must delete the base blob, not by version id",
        }


def ra10(cfg):
    size = cfg["args"].get("size_mib", 1024) << 20
    _, cl = scratch(live(cfg))
    _, ca = scratch(arch(cfg))
    src = cl.get_blob_client(f"ra10-{int(time.time())}")
    data = os.urandom(size)
    src.upload_blob(data, overwrite=True)
    dst = ca.get_blob_client(src.blob_name)
    dst.start_copy_from_url(src.url, requires_sync=False)
    while dst.get_blob_properties().copy.status == "pending":
        time.sleep(5)
    props = dst.get_blob_properties()
    t0 = time.time()
    got = dst.download_blob(validate_content=False).readall()
    secs = time.time() - t0
    same = hashlib.sha256(got).digest() == hashlib.sha256(data).digest()
    dst.set_standard_blob_tier("Archive")
    tier = dst.get_blob_properties().blob_tier
    return {
        "landed_tier": props.blob_tier,
        "read_back_same": same,
        "tier_now": tier,
        "read_s_per_tb": round(secs * (1 << 40) / size),
    }


def ra11(cfg):
    _, c = scratch(live(cfg))
    b = c.get_blob_client(f"ra11-{int(time.time())}")
    b.stage_block("YQ==", b"a" * 1024)
    b.stage_block("Yg==", b"b" * 1024)
    v1 = b.commit_block_list(["YQ==", "Yg=="])["version_id"]
    b.upload_blob(b"other", overwrite=True)
    blocks = c.get_blob_client(b.blob_name, version_id=v1).get_block_list("committed")[0]
    if [x.id for x in blocks] != ["YQ==", "Yg=="]:
        raise RuntimeError(f"version {v1}'s block list is {[x.id for x in blocks]}")
    return {"version": v1, "blocks": [x.id for x in blocks]}


def _forged(url: str) -> int:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"X-Forwarded-Client-Cert": "Hash=00;Cert=forged"})
    try:
        return urllib.request.urlopen(req, context=ctx, timeout=20).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _no_cert(url: str) -> str:
    """A TLS handshake with api's TCP ingress without a client certificate."""
    import socket
    from urllib.parse import urlsplit

    u = urlsplit(url)
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((u.hostname, u.port or 443), timeout=15) as raw:
            with c.wrap_socket(raw, server_hostname=u.hostname) as t:
                t.sendall(b"GET /v0/edges/self HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                return t.recv(64).decode(errors="replace").split("\r\n", 1)[0] or "closed"
    except (ssl.SSLError, OSError) as e:
        return f"refused: {type(e).__name__}"


def ra12(cfg):
    api = (
        cfg["outputs"].get("apiUrl")
        or f"https://{cfg['prefix']}-api.{cfg['outputs'].get('internalEnvironmentDefaultDomain', '')}"
    )
    code = _forged(f"{api}/v0/edges/self/heartbeat")
    if code in (200, 201, 204):
        raise RuntimeError(f"a forged XFCC got {code}")
    plain = _no_cert(api)
    if not (plain.startswith("refused") or " 4" in plain):
        raise RuntimeError(f"a peer without an edge certificate got {plain!r}")
    return {"forged_xfcc": code, "no_client_cert": plain}


def ra13(cfg):
    from azure.core.exceptions import HttpResponseError
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas

    s, c = scratch(live(cfg))
    b = c.get_blob_client("ra13")
    b.upload_blob(b"sas", overwrite=True)
    key = s.get_user_delegation_key(datetime.now(UTC), datetime.now(UTC) + timedelta(hours=1))
    sas = generate_blob_sas(
        s.account_name,
        "ofreal-checks",
        "ra13",
        user_delegation_key=key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )
    tampered = sas.replace("sp=r", "sp=rw")
    try:
        urllib.request.urlopen(f"{b.url}?{tampered}", timeout=20)
        raise RuntimeError("a tampered SAS was accepted")
    except urllib.error.HTTPError as e:
        sas_code = e.code
    try:
        ra01(cfg)
        crc = "refused"
    except HttpResponseError as e:
        crc = e.error_code
    return {"tampered_sas": sas_code, "bad_crc64": crc}


def _pg(cfg):
    import psycopg

    host = cfg["outputs"].get("postgresFqdn") or f"pg-{cfg['prefix']}.postgres.database.azure.com"
    user = cfg["outputs"].get("postgresAdminName") or f"ofreal-{cfg['prefix']}-pg-admins"
    return psycopg.connect(
        host=host,
        dbname="omnifile",
        user=user,
        password=cfg["args"]["token"],
        sslmode="verify-full",
        sslrootcert="system",
    )


def ra28(cfg):
    with _pg(cfg) as db:
        roles = {
            r[0] for r in db.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE 'omnifile_%'")
        }
        members = db.execute(
            "SELECT m.rolname, r.rolname FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid"
            " JOIN pg_roles m ON m.oid = a.member WHERE r.rolname LIKE 'omnifile_%'"
        ).fetchall()
    want = {"omnifile_api", "omnifile_enrol", "omnifile_web", "omnifile_worker"}
    if want - roles:
        raise RuntimeError(f"missing group roles {sorted(want - roles)}")
    return {"roles": sorted(roles), "members": [list(m) for m in members]}


def ra29(cfg):
    with _pg(cfg) as db:
        rows = db.execute(
            "SELECT usename, application_name, backend_start FROM pg_stat_activity"
            " WHERE usename LIKE 'id-%'"
        ).fetchall()
    users = {r[0] for r in rows}
    want = {f"id-{cfg['prefix']}-{a}" for a in ("api", "enrol", "web", "worker")}
    if want - users:
        raise RuntimeError(f"no session as {sorted(want - users)}")
    oldest = min(r[2] for r in rows)
    newest = max(r[2] for r in rows)
    return {"sessions": sorted(users), "after_an_hour": (newest - oldest).total_seconds() > 3600}


def ra32(cfg):
    _, cl = scratch(live(cfg))
    _, ca = scratch(arch(cfg))
    data = os.urandom(1 << 20)
    src = cl.get_blob_client(f"ra32-{int(time.time())}")
    src.upload_blob(data, overwrite=True)
    dst = ca.get_blob_client(src.blob_name)
    dst.start_copy_from_url(src.url, requires_sync=True)
    p = dst.get_blob_properties()
    md5 = p.content_settings.content_md5
    if md5 and bytes(md5) != hashlib.md5(data).digest():
        raise RuntimeError("the archive copy's Content-MD5 is not the source's")
    dst.set_standard_blob_tier("Archive")
    return {"content_md5_on_copy": bool(md5), "crc64_property": "none (Azure keeps no CRC64)"}


def rt25(cfg):
    """A partner token by client credentials (certificate assertion), then a partner API call."""
    import httpx
    import jwt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    a = cfg["args"]
    cert = x509.load_pem_x509_certificate(a["cert"].encode())
    key = serialization.load_pem_private_key(a["key"].encode(), None)
    now = int(time.time())
    tok = f"https://login.microsoftonline.com/{a['tenant']}/oauth2/v2.0/token"
    assertion = jwt.encode(
        {
            "aud": tok,
            "iss": a["client_id"],
            "sub": a["client_id"],
            "jti": os.urandom(8).hex(),
            "nbf": now,
            "exp": now + 600,
        },
        key,
        algorithm="ES256",
        headers={
            "x5t#S256": base64.urlsafe_b64encode(cert.fingerprint(hashes.SHA256()))
            .decode()
            .rstrip("=")
        },
    )
    r = httpx.post(
        tok,
        data={
            "client_id": a["client_id"],
            "grant_type": "client_credentials",
            "scope": f"{a['web']}/.default",
            "client_assertion": assertion,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        },
    )
    r.raise_for_status()
    claims = jwt.decode(r.json()["access_token"], options={"verify_signature": False})
    got = httpx.get(
        f"{a['web']}/v1/usage",
        headers={"Authorization": f"Bearer {r.json()['access_token']}"},
        verify=False,
    )
    return {
        "idtyp": claims.get("idtyp"),
        "iss": claims.get("iss"),
        "aud": claims.get("aud"),
        "roles": claims.get("roles"),
        "partner_api": got.status_code,
    }


CHECKS = {
    "RA-01": ra01,
    "RA-02": ra02,
    "RA-03": ra03,
    "RA-07": ra07,
    "RA-08": ra08,
    "RA-09": ra09,
    "RA-10": ra10,
    "RA-11": ra11,
    "RA-12": ra12,
    "RA-13": ra13,
    "RA-28": ra28,
    "RA-29": ra29,
    "RA-32": ra32,
    "RT-25": rt25,
}


def main() -> int:
    cid = sys.argv[1]
    cfg = json.loads(sys.stdin.read() or "{}")
    try:
        say({"ok": True, "id": cid, "evidence": CHECKS[cid](cfg)})
    except Exception as e:
        say({"ok": False, "id": cid, "error": f"{type(e).__name__}: {e}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
