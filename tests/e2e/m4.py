"""The M4 exit test, service side (docs/milestones/M4.md "Exit"), against the M4 dev stack
(dev/m4/stack.py: sim_http on the virtual clock, api, web, worker, the fakes and the real
relay, each its own process).

    python tests/e2e/m4.py seed    the world: an edge, a dataset and share, files (sim objects)
    python tests/e2e/m4.py run     every check below; results to $E2E_RESULTS (markdown)

Run by tests/e2e/m4_exit_test.sh, which brings the stack up first. Each check is one line of
the results. The edge is played by this script: it holds an edge certificate from the dev
CA and calls `api` over mutual TLS as the daemon does (a stub read is `POST .../recalls`
with the reader's SID, `via = smb`; the fill is `POST .../recalls/{id}/filled`). Teams is
played by the fakes: a click is the fake Teams client signing a Bot Framework JWT and
POSTing the invoke to the real relay, which forwards it over mutual TLS to `api`.

What it proves:
  1. a stub read by an AD user sends them a card through the fake Bot Connector, and the
     card follows the recall requested -> rehydrating -> downloading -> available;
  2. Keep online on that card (through the relay) sets a 14-day keep_online hold;
  3. refusals: a stranger's click, a replayed click, an expired intent and a click by a
     user who lost read access are refused (relay or api), and api's refusals are audited;
  4. after an injected restore failure, Recall on the "failed" card queues a new recall
     and the same card follows it to available;
  5. when the Bot Connector answers 403, the user gets the email fallback (Graph sendMail);
  6. restore of an old version (decision 34): restored, restored_timestamped, overwrite,
     and 409 name_taken;
  7. the policy preview's cost changes with the policy; a saved version is active and the
     scheduler re-plans; a partner proposal changes nothing until another ArchiveAdmin
     approves it, and the proposer can't approve it;
  8. a reader's listings show only what their AD groups can read.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import ssl
import sys
import time
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import psycopg
import psycopg.rows
from psycopg.types.json import Jsonb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dev" / "m4"))
import world  # noqa: E402

WORK = Path(os.environ.get("M4_WORK") or Path(os.environ.get("TMPDIR", "/tmp")) / "omnifile-m4")
STACK: dict[str, Any] = {}
GiB = 1 << 30
# line_4001.segy is the portal test's (portal/e2e): a Reader recalls it ahead of time.
ARCHIVED = {
    "line_1042.segy": 2 * GiB,
    "line_2001.segy": 3 * GiB,
    "line_3001.segy": GiB,
    "line_4001.segy": GiB,
}
POLICY = {
    "mode": "scheduled",
    "clock": "written",
    "warm_day": 30,
    "cold_day": 120,
    "archive_day": 365,
    "stub_at": "archive",
    "notify": {
        "requester": True,
        "owners": False,
        "watchers_group_sid": None,
        "email_fallback": True,
    },
}


def stack() -> dict[str, Any]:
    if not STACK:
        STACK.update(json.loads((WORK / "stack.json").read_text()))
    return STACK


def db() -> psycopg.Connection:
    c = psycopg.connect(stack()["db_url"], autocommit=True, row_factory=psycopg.rows.dict_row)
    c.execute("SELECT set_config('omnifile.actor', 'e2e:m4', false)")
    return c


def seeded() -> dict[str, Any]:
    return json.loads((WORK / "seed.json").read_text())


def wait_for(what: str, fn: Callable[[], Any], timeout: float = 60, every: float = 0.5) -> Any:
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            last = fn()
        except Exception as e:  # noqa: BLE001 - keep polling, report the last error
            last = e
        if last and not isinstance(last, Exception):
            return last
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what} (last: {last!r})")


# --- the edge (this script, with an edge certificate) ------------------------------------


def _edge_cert(edge_id: int) -> tuple[str, str, bytes, datetime]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from omnifile_service.enrol.ca import FileCASigner, edge_certificate_template

    pki = stack()["pki"]
    ca = FileCASigner.from_files(pki["ca_key"], pki["ca_crt"])
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    cert = asyncio.run(
        ca.sign(edge_certificate_template(ca.ca_certificate, edge_id, key.public_key(), now))
    )
    key_path, crt_path = WORK / "pki" / "edge.key", WORK / "pki" / "edge.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    from cryptography.hazmat.primitives import hashes

    return str(crt_path), str(key_path), cert.fingerprint(hashes.SHA256()), cert.not_valid_after_utc


def mtls(cert: str, key: str) -> ssl.SSLContext:
    """A client context for mutual TLS to api (as the relay's sink builds it)."""
    ctx = ssl.create_default_context(cafile=stack()["pki"]["ca_crt"])
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    return ctx


class Edge:
    def __init__(self, edge_id: int) -> None:
        self.edge_id = edge_id
        s = stack()
        tls = mtls(str(WORK / "pki" / "edge.crt"), str(WORK / "pki" / "edge.key"))
        self.http = httpx.Client(base_url=s["urls"]["api"], verify=tls, trust_env=False, timeout=15)

    def post(self, path: str, body: dict) -> httpx.Response:
        return self.http.post(
            f"/v0/edges/{self.edge_id}{path}",
            json=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )

    def stub_read(self, file_id: int, version_id: int, who: dict, on_open: str = "wait") -> dict:
        r = self.post(
            "/recalls",
            {
                "file_id": str(file_id),
                "version_id": str(version_id),
                "offset": 0,
                "length": 1 << 20,
                "on_open": on_open,
                "requester": {
                    "sid": who["sid"],
                    "via": "smb",
                    "sec": "kerberos",
                    "client_addr": "10.20.30.40",
                },
            },
        )
        assert r.status_code == 200, r.text
        return r.json()

    def filled(self, recall_id: int | str, version_id: int, size: int) -> None:
        r = self.post(
            f"/recalls/{recall_id}/filled", {"version_id": str(version_id), "resident_bytes": size}
        )
        assert r.status_code == 204, r.text


# --- the fakes, sim and the relay --------------------------------------------------------


def fakes(method: str, path: str, body: Any = None) -> Any:
    r = httpx.request(method, stack()["urls"]["bot"] + path, json=body, trust_env=False, timeout=30)
    r.raise_for_status()
    return r.json()


def sim(path: str, body: dict) -> dict:
    r = httpx.post(stack()["urls"]["sim"] + path, json=body, trust_env=False, timeout=30)
    r.raise_for_status()
    return r.json()


def advance(seconds: float) -> None:
    sim("/_sim/advance", {"seconds": seconds})
    # The WORM facade shares the clock file; asking it rotates its key if it lapsed.
    httpx.post(stack()["urls"]["sim-worm"] + "/_sim/advance", json={"seconds": 0}, trust_env=False)


def sim_backend(backend_id: int | None = None):
    from omnifile_service.backends.sim import OffsetClock
    from omnifile_service.worker.backends import SimHttpBackend

    s = stack()
    bid = backend_id or s["backend_id"]
    with db() as c:
        (config,) = (
            c.execute("SELECT config FROM catalog.backend WHERE backend_id = %s", (bid,))
            .fetchone()
            .values()
        )
    return SimHttpBackend.from_config(bid, config, clock=OffsetClock(s["clock_file"]))


def relay_log() -> list[dict]:
    out = []
    for line in (Path(stack()["logs"]) / "relay.log").read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# --- the portal (web), signed in through the dev OIDC issuer --------------------------------


class Portal:
    """A browser's session, by hand: the session cookie and the CSRF token."""

    def __init__(self, key: str) -> None:
        s = stack()
        self.origin = s["urls"]["web"]
        self.http = httpx.Client(
            base_url=self.origin, verify=s["pki"]["ca_crt"], trust_env=False, timeout=15
        )
        self.cookies: dict[str, str] = {}
        r = self._get("/auth/login", params={"return_to": "/"})
        assert r.status_code == 302, r.text
        loc = r.headers["location"]
        r = self._get(loc + "&" + urlencode({"login_hint": key}))
        assert r.status_code == 302, r.text
        r = self._get(r.headers["location"])
        assert r.status_code == 302, r.text
        session = self.get("/auth/session")
        assert session.status_code == 200, session.text
        self.csrf = session.json()["csrf_token"]
        self.me = self.get("/v1/me").json()

    def _keep(self, r: httpx.Response) -> httpx.Response:
        for raw in r.headers.get_list("set-cookie"):
            name, _, rest = raw.partition("=")
            value = rest.split(";", 1)[0]
            if "max-age=0" in raw.lower() or value == "":
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        return r

    def _hdr(self, extra: dict | None = None) -> dict:
        h = dict(extra or {})
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return h

    def _get(self, url: str, **kw: Any) -> httpx.Response:
        if url.startswith(self.origin):
            url = url[len(self.origin) :]
        return self._keep(self.http.get(url, headers=self._hdr(), **kw))

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self._get(path, **kw)

    def send(
        self,
        method: str,
        path: str,
        body: Any = None,
        key: str | None = None,
        headers: dict | None = None,
    ):
        h = {"X-CSRF-Token": self.csrf, "Origin": self.origin, **(headers or {})}
        if method == "POST" and path.startswith("/v1") and not path.endswith("/preview"):
            h["Idempotency-Key"] = key or str(uuid.uuid4())
        return self._keep(self.http.request(method, path, json=body, headers=self._hdr(h)))

    def post(self, path: str, body: Any = None, **kw: Any) -> httpx.Response:
        return self.send("POST", path, body, **kw)


def partner_token() -> str:
    s = stack()
    secret = Path(s["partner"]["secret_file"]).read_text()
    r = httpx.post(
        s["urls"]["web"] + "/dev-idp/token",
        verify=s["pki"]["ca_crt"],
        trust_env=False,
        data={
            "grant_type": "client_credentials",
            "client_id": s["partner"]["client_id"],
            "client_secret": secret,
            "roles": "Policy.Propose",
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]


# --- seed ---------------------------------------------------------------------------------


def seed(_a: argparse.Namespace | None = None) -> dict[str, Any]:
    from omnifile_service.backends.base import LogicalTier

    s = stack()
    g = {k: world.group_sid(k) for k in world.GROUPS}
    tag = uuid.uuid4().hex[:6]
    with db() as c:
        (edge_id,) = (
            c.execute(
                "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
                " VALUES (%s, %s, now() + interval '1 day') RETURNING edge_id",
                (f"m4-edge-{tag}", hashlib.sha256(tag.encode()).digest()),
            )
            .fetchone()
            .values()
        )
    crt, key, sha, not_after = _edge_cert(edge_id)
    with db() as c:
        c.execute(
            "UPDATE catalog.edge SET state = 'active', enrolled_at = now(), cert_sha256 = %s,"
            " cert_not_after = %s, enrolment_token_sha256 = NULL WHERE edge_id = %s",
            (sha, not_after, edge_id),
        )
        (ds,) = (
            c.execute(
                "INSERT INTO catalog.dataset (name, backend_id, edge_id, storage_layout)"
                " VALUES (%s, %s, %s, 'two_account') RETURNING dataset_id",
                (f"seismic-{tag}", s["backend_id"], edge_id),
            )
            .fetchone()
            .values()
        )

        def acl(read: list[str]) -> int:
            return c.execute(
                "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids,"
                " deny_sids) VALUES (%s, %s, 488, '[]', %s::catalog.sid[], '{}') RETURNING acl_id",
                (ds, uuid.uuid4().bytes * 2, read),
            ).fetchone()["acl_id"]

        open_acl = acl([g["owners"], g["writers"], g["readers"]])
        secret_acl = acl([g["owners"]])

        def node(parent, name, kind="dir", acl_id=open_acl, **kw) -> int:
            cols = ["dataset_id", "parent_id", "name", "kind", "acl_id", "uid", "gid", "mode"]
            vals = [ds, parent, name, kind, acl_id, 20005, 10000, 0o770 if kind == "dir" else 0o640]
            for k, v in kw.items():
                cols.append(k)
                vals.append(v)
            sql = (
                f"INSERT INTO catalog.file ({', '.join(cols)}) VALUES"
                f" ({', '.join(['%s'] * len(vals))}) RETURNING file_id"
            )
            return c.execute(sql, vals).fetchone()["file_id"]

        root = node(None, "")
        share_name = f"seismic-{tag}"
        (share_id,) = (
            c.execute(
                "INSERT INTO catalog.share (dataset_id, edge_id, name, root_dir_id, protocols,"
                " nfs_sec, owners_group_sid, writers_group_sid, readers_group_sid, on_open,"
                " rewind_depth_days, rewind_depth_max_days)"
                " VALUES (%s, %s, %s, %s, ARRAY['smb','nfs4'], ARRAY['krb5'], %s, %s, %s, 'wait',"
                " 30, 90) RETURNING share_id",
                (ds, edge_id, share_name, root, g["owners"], g["writers"], g["readers"]),
            )
            .fetchone()
            .values()
        )
        admin = c.execute(
            "INSERT INTO catalog.identity (sid, kind, display_name) VALUES (%s, 'user', 'Seed')"
            " ON CONFLICT (sid) DO UPDATE SET sid = EXCLUDED.sid RETURNING identity_id",
            (world.sid(9999),),
        ).fetchone()["identity_id"]
        c.execute(
            "INSERT INTO catalog.policy (dataset_id, version, body, approved_by_identity_id)"
            " VALUES (%s, 1, %s, %s)",
            (ds, Jsonb(POLICY), admin),
        )
        survey = node(root, "survey")
        restricted = node(root, "restricted", acl_id=secret_acl)

    b = sim_backend()
    files: dict[str, dict] = {}

    async def put_all():
        refs = {}
        for i, (name, size) in enumerate(ARCHIVED.items()):
            refs[name] = [
                await b.put_synthetic(
                    "archive/data", f"m4-{tag}/{name}", size, LogicalTier.ARCHIVE, seed=i
                )
            ]
        for name, n in (("notes.txt", 3), ("survey.txt", 3), ("payroll.csv", 1)):
            refs[name] = [
                await b.put_synthetic(
                    "live/data", f"m4-{tag}/{name}/{v}", 4096 + v, LogicalTier.HOT, seed=100 + v
                )
                for v in range(n)
            ]
        await b.close()
        return refs

    refs = asyncio.run(put_all())
    now = datetime.now(UTC)
    with db() as c:
        for name, versions in refs.items():
            archived = name in ARCHIVED
            parent = restricted if name == "payroll.csv" else survey
            acl_id = secret_acl if name == "payroll.csv" else open_acl
            size = ARCHIVED.get(name, 4096 + len(versions) - 1)
            fid = c.execute(
                "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
                " resident_bytes, acl_id, uid, gid, mode, mtime_ns, written_at)"
                " VALUES (%s, %s, %s, 'file', %s, %s, %s, %s, 20005, 10000, 416,"
                " 1700000000000000000, %s) RETURNING file_id",
                (
                    ds,
                    parent,
                    name,
                    "stub" if archived else "resident",
                    size,
                    0 if archived else size,
                    acl_id,
                    now,
                ),
            ).fetchone()["file_id"]
            vids = []
            for i, ref in enumerate(versions):
                vsize = ARCHIVED.get(name, 4096 + i)
                at = now - timedelta(days=200 if archived else (len(versions) - i))
                vid = c.execute(
                    "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind,"
                    " state, size, acl_id, uid, gid, mode, mtime_ns) VALUES (%s, %s, %s, %s,"
                    " 'content', 'pending', %s, %s, 20005, 10000, 416, 1700000000000000000)"
                    " RETURNING version_id",
                    (fid, ds, parent, name, vsize, acl_id),
                ).fetchone()["version_id"]
                c.execute(
                    "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
                    " object_key, backend_version_id, role, state, logical_tier, storage_class,"
                    " explicit_tier_set, size, committed_at, tier_changed_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, 'primary', 'committed', %s, %s, %s, %s,"
                    " %s, %s)",
                    (
                        fid,
                        vid,
                        s["backend_id"],
                        ref.container,
                        ref.key,
                        ref.version_id,
                        "archive" if archived else "hot",
                        "Archive" if archived else "Hot",
                        archived,
                        vsize,
                        at,
                        at,
                    ),
                )
                c.execute(
                    "UPDATE catalog.file_version SET state = 'committed', committed_at = %s"
                    " WHERE version_id = %s",
                    (at, vid),
                )
                vids.append(vid)
            files[name] = {"file_id": fid, "versions": vids, "size": size}
    # Two WORM-layout datasets (one LRS account, version-level WORM; decision 31), one share
    # each: `vault` for the browser test, `vault2` for check 10. As the setup guide has the
    # customer do on Azure, the container exists with version-level immutability first.
    wb = sim_backend(s["worm_backend_id"])

    async def provision():
        try:
            await wb.create_container("worm/data", worm=True)
        finally:
            await wb.close()

    asyncio.run(provision())
    worm_shares = {}
    with db() as c:
        for key in ("vault", "vault2"):
            wds = c.execute(
                "INSERT INTO catalog.dataset (name, backend_id, edge_id, storage_layout)"
                " VALUES (%s, %s, %s, 'single_account') RETURNING dataset_id",
                (f"{key}-{tag}", s["worm_backend_id"], edge_id),
            ).fetchone()["dataset_id"]
            wacl = c.execute(
                "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids,"
                " deny_sids) VALUES (%s, %s, 488, '[]', %s::catalog.sid[], '{}') RETURNING acl_id",
                (wds, uuid.uuid4().bytes * 2, [g["owners"], g["readers"]]),
            ).fetchone()["acl_id"]
            wroot = c.execute(
                "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, acl_id)"
                " VALUES (%s, NULL, '', 'dir', %s) RETURNING file_id",
                (wds, wacl),
            ).fetchone()["file_id"]
            name = f"{key}-{tag}"
            wshare = c.execute(
                "INSERT INTO catalog.share (dataset_id, edge_id, name, root_dir_id, protocols,"
                " owners_group_sid, readers_group_sid, on_open)"
                " VALUES (%s, %s, %s, %s, ARRAY['smb'], %s, %s, 'wait') RETURNING share_id",
                (wds, edge_id, name, wroot, g["owners"], g["readers"]),
            ).fetchone()["share_id"]
            c.execute(
                "INSERT INTO catalog.policy (dataset_id, version, body, approved_by_identity_id)"
                " VALUES (%s, 1, %s, %s)",
                (wds, Jsonb(POLICY), admin),
            )
            worm_shares[key] = {"share_id": wshare, "name": name, "dataset_id": wds}
    out = {
        "worm": worm_shares,
        "edge_id": edge_id,
        "dataset_id": ds,
        "share_id": share_id,
        "share_name": share_name,
        "root_id": root,
        "survey_id": survey,
        "restricted_id": restricted,
        "files": files,
        "tag": tag,
    }
    (WORK / "seed.json").write_text(json.dumps(out, indent=1))
    # The Playwright tests read the same facts.
    print(json.dumps(out))
    return out


# --- checks -------------------------------------------------------------------------------


class Results:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, fn: Callable[[], str | None]) -> bool:
        t0 = time.monotonic()
        try:
            detail = fn() or ""
            ok = True
        except Exception as e:  # noqa: BLE001 - every check reports, the run goes on
            ok = False
            detail = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        dt = time.monotonic() - t0
        self.rows.append((name, ok, f"{detail} ({dt:.1f} s)"))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
        return ok


def notification(recall_id: int | str, identity_sid: str, channel: str = "teams") -> dict | None:
    with db() as c:
        return c.execute(
            "SELECT n.* FROM catalog.notification n JOIN catalog.identity i USING (identity_id)"
            " WHERE n.recall_id = %s AND i.sid = %s AND n.channel = %s",
            (int(recall_id), identity_sid, channel),
        ).fetchone()


def card_versions(conversation_id: str, activity_id: str) -> list[dict]:
    convs = fakes("GET", "/_fake/conversations")
    for act in convs.get(conversation_id, []):
        if act["activity_id"] == activity_id:
            return [v["card"] for v in act["versions"]]
    return []


def card_text(card: dict) -> str:
    return json.dumps(card)


STEP = re.compile(r"Step (\d) of 4")


def card_state(card: dict) -> str:
    text = card_text(card)
    if "Couldn't recall" in text:
        return "failed"
    m = STEP.search(text)
    return {"1": "requested", "2": "rehydrating", "3": "downloading", "4": "available"}.get(
        m[1] if m else "", "?"
    )


def buttons(card: dict) -> list[str]:
    return [a.get("title") for a in card.get("actions", []) if a.get("type") == "Action.Execute"]


def the_card(recall_id, who: dict) -> tuple[str, str]:
    n = wait_for(
        f"a Teams card for recall {recall_id}",
        lambda: (x := notification(recall_id, who["sid"])) and x["teams_activity_id"] and x,
    )
    return n["teams_conversation_id"], n["teams_activity_id"]


def wait_card(conv: str, act: str, state: str, timeout: float = 60) -> dict:
    def latest():
        versions = card_versions(conv, act)
        return versions and card_state(versions[-1]) == state and versions[-1]

    return wait_for(f"the card to show {state}", latest, timeout=timeout)


def recall_state(recall_id) -> str:
    with db() as c:
        return c.execute(
            "SELECT state FROM catalog.recall WHERE recall_id = %s", (int(recall_id),)
        ).fetchone()["state"]


def wait_recall(recall_id, state: str, timeout: float = 60) -> str:
    return wait_for(
        f"recall {recall_id} {state}",
        lambda: recall_state(recall_id) == state and state,
        timeout=timeout,
    )


def audits(audit_action: str, since: datetime, **match: Any) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT * FROM catalog.audit_log WHERE action = %s AND at >= %s ORDER BY at",
            (audit_action, since),
        ).fetchall()
    return [r for r in rows if all((r["details"] or {}).get(k) == v for k, v in match.items())]


def click(conv: str, title: str, who: dict, card: dict | None = None) -> dict:
    body = {"conversation_id": conv, "title": title, "clicker": who["oid"]}
    if card is not None:
        body["card"] = card
    return fakes("POST", "/_fake/teams/click", body)


def forward_direct(intent: str, action: str, clicker: str) -> httpx.Response:
    """What the relay sends to api, sent with the relay's certificate, by hand: so api's
    own refusal (and its audit) is seen even where the relay refuses first."""
    s = stack()
    claims = json.loads(_b64(intent.split(".")[1]))
    stamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    body = {
        "intent": intent,
        "intent_id": claims["jti"],
        "action": action,
        "clicker_aad_object_id": clicker,
        "tenant_id": world.TENANT,
        "activity_time": stamp,
        "received_at": stamp,
    }
    tls = mtls(s["pki"]["relay_crt"], s["pki"]["relay_key"])
    with httpx.Client(base_url=s["urls"]["api"], verify=tls, trust_env=False) as h:
        return h.post(
            "/internal/v1/teams/intents", json=body, headers={"Idempotency-Key": claims["jti"]}
        )


def _b64(part: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def intent_of(card: dict, title: str) -> str:
    for a in card.get("actions", []):
        if a.get("type") == "Action.Execute" and a.get("title") == title:
            return a["data"]["omnifile_intent"]
    raise LookupError(f"no {title} on the card")


def run(_a: argparse.Namespace) -> int:
    s = seeded()
    files = s["files"]
    edge = Edge(s["edge_id"])
    reader, stranger = world.user("reader"), world.user("stranger")
    res = Results()
    t_start = datetime.now(UTC) - timedelta(seconds=5)
    ctx: dict[str, Any] = {}
    # A clean start: no faults left over from an earlier run.
    sim("/_sim/faults", {"clear": True})
    fakes("POST", "/_fake/bot/fail", {"clear": True})

    # 1. A stub read by an AD user -> card -> requested -> rehydrating -> downloading -> available
    def stub_read_card() -> str:
        f = files["line_1042.segy"]
        # The provider refuses the first restore call (the worker retries it a few seconds
        # later), so the first card shows "requested" before the recall moves on: cards
        # coalesce changes that come less than 10 s apart.
        sim("/_sim/faults", {"op": "restore", "fail_nth": 1})
        r = edge.stub_read(f["file_id"], f["versions"][-1], reader)
        assert r["status"] == "recall" and r["decision"] == "wait", r
        rid = r["recall_id"]
        ctx["r1"] = rid
        conv, act = the_card(rid, reader)
        ctx["conv"], ctx["act1"] = conv, act
        wait_card(conv, act, "requested", 30)
        wait_recall(rid, "rehydrating", 60)
        wait_card(conv, act, "rehydrating", 60)
        advance(16 * 3600)  # Standard rehydrate: "up to 15 h" on the virtual clock
        wait_recall(rid, "downloading", 60)
        wait_card(conv, act, "downloading", 60)
        edge.filled(rid, f["versions"][-1], f["size"])
        last = wait_card(conv, act, "available", 60)
        states = [card_state(c) for c in card_versions(conv, act)]
        seen = [x for i, x in enumerate(states) if i == 0 or x != states[i - 1]]
        assert seen == ["requested", "rehydrating", "downloading", "available"], states
        assert "Keep online" in buttons(last), buttons(last)
        with db() as c:
            log = [
                r["state"]
                for r in c.execute(
                    "SELECT state FROM catalog.recall_state_log WHERE recall_id = %s ORDER BY at",
                    (int(rid),),
                )
            ]
        return (
            f"recall {rid}: card states {seen} in {len(states)} versions of one activity;"
            f" recall_state_log {log}"
        )

    res.check(
        "1. stub read by an AD user -> Teams card follows the recall to available", stub_read_card
    )

    # 2. Keep online, through the real relay.
    def keep_online() -> str:
        conv = ctx["conv"]
        f = files["line_1042.segy"]
        card = card_versions(conv, ctx["act1"])[-1]
        clicked_at = datetime.fromisoformat(
            httpx.get(stack()["urls"]["sim"] + "/_sim/clock", trust_env=False).json()["now"]
        )  # the service clock (api's; the dev virtual clock here)
        r = click(conv, "Keep online", reader, card=card)
        assert r["status"] == 200, r
        reply = r["body"]
        assert reply.get("type") == "application/vnd.microsoft.card.adaptive", reply
        assert not buttons(reply["value"]), "api's reply card must have no Execute buttons"
        with db() as c:
            h = c.execute(
                "SELECT h.*, i.sid FROM catalog.hold h JOIN catalog.identity i"
                " ON i.identity_id = h.set_by_identity_id"
                " WHERE h.file_id = %s AND h.kind = 'keep_online' AND h.released_at IS NULL",
                (f["file_id"],),
            ).fetchone()
        assert h is not None, "no keep_online hold"
        days = (h["until"] - clicked_at).total_seconds() / 86400
        assert abs(days - 14) < 0.01, days
        assert h["sid"] == reader["sid"] and h["reason"] == "Teams", h
        assert audits("teams.intent.accepted", t_start, action="keep_online"), "no accept audit"
        # The worker then updates the card: "Kept online until", no Keep online button.
        n = notification(ctx["r1"], reader["sid"])

        def updated():
            v = card_versions(n["teams_conversation_id"], n["teams_activity_id"])[-1]
            return "Kept online until" in card_text(v) and "Keep online" not in buttons(v) and v

        wait_for("the card to show the hold", updated, 40)
        return (
            f"hold {h['hold_id']}: keep_online, {days:.0f} days, set by the clicker; card updated"
        )

    res.check("2. Keep online through the relay -> 14-day keep_online hold", keep_online)

    def replayed() -> str:
        # The accepted Keep online click of check 2, sent again byte for byte.
        r = fakes("POST", "/_fake/teams/replay")
        body = r["body"]
        text = json.dumps(body)
        assert r["status"] == 200 and "already" in text.lower(), r
        return f"relay: {body.get('type')} ({text[:80]}...)"

    res.check("2b. a replayed click is refused by the relay", replayed)

    # 3. Refusals, on a second recall's card (it still has its Keep online button).
    def second_recall() -> str:
        f = files["line_2001.segy"]
        r = edge.stub_read(f["file_id"], f["versions"][-1], reader)
        rid = r["recall_id"]
        ctx["r2"] = rid
        conv, act = the_card(rid, reader)
        card = wait_for(
            "a card with Keep online",
            lambda: (v := card_versions(conv, act)) and "Keep online" in buttons(v[-1]) and v[-1],
        )
        ctx["card2"] = card
        ctx["act2"] = act
        return f"recall {rid}, card {card_state(card)}"

    res.check("3a. a second stub read (line_2001.segy) -> card with Keep online", second_recall)

    def stranger_click() -> str:
        card = ctx["card2"]
        r = click(ctx["conv"], "Keep online", stranger, card=card)
        assert r["body"]["statusCode"] == 403, r
        code = r["body"]["value"].get("code") if isinstance(r["body"].get("value"), dict) else None
        # The relay refused before forwarding (clicker != intent's recipient). api would too:
        d = forward_direct(intent_of(card, "Keep online"), "keep_online", stranger["oid"])
        assert d.status_code == 403 and d.json()["code"] == "not_intended_recipient", d.text
        a = audits("teams.intent.refused", t_start, reason="not_intended_recipient")
        assert a, "api's refusal is not audited"
        return f"relay {r['body']['statusCode']} ({code}); api 403 not_intended_recipient, audited"

    res.check("3b. a stranger's click is refused (relay and api) and audited", stranger_click)

    def lost_access() -> str:
        card = ctx["card2"]
        with db() as c:
            before = c.execute(
                "SELECT group_sids::text[] FROM catalog.identity WHERE sid = %s", (reader["sid"],)
            ).fetchone()["group_sids"]
            c.execute(
                "UPDATE catalog.identity SET group_sids = %s::catalog.sid[] WHERE sid = %s",
                ([x for x in before if x != world.group_sid("readers")], reader["sid"]),
            )
        try:
            r = click(ctx["conv"], "Keep online", reader, card=card)
        finally:
            with db() as c:
                c.execute(
                    "UPDATE catalog.identity SET group_sids = %s::catalog.sid[] WHERE sid = %s",
                    (before, reader["sid"]),
                )
        assert r["body"]["statusCode"] == 403, r
        a = audits("teams.intent.refused", t_start, reason="forbidden")
        assert a, "no teams.intent.refused (forbidden) audit"
        with db() as c:
            held = c.execute(
                "SELECT count(*) AS n FROM catalog.hold WHERE file_id = %s",
                (files["line_2001.segy"]["file_id"],),
            ).fetchone()["n"]
        assert held == 0, "a hold was set for a user who lost access"
        return "relay forwarded, api 403 forbidden (no read), audited; no hold"

    res.check("3c. a click by a user who lost read access is refused and audited", lost_access)

    def expired() -> str:
        from cryptography.hazmat.primitives import serialization

        from omnifile_service.notify.intents import IntentSigner

        key = serialization.load_pem_private_key(Path(stack()["intent_key"]).read_bytes(), None)
        signer = IntentSigner(key, stack()["intent_kid"])  # type: ignore[arg-type]
        f = files["line_2001.segy"]
        n = notification(ctx["r2"], reader["sid"])
        m = signer.mint(
            action="keep_online",
            file_id=f["file_id"],
            recipient=reader["oid"],
            tenant=world.TENANT,
            now=datetime.now(UTC) - timedelta(days=8),
        )
        with db() as c:
            c.execute(
                "INSERT INTO catalog.teams_intent (intent_id, notification_id, action, file_id,"
                " version_id, recipient_identity_id, issued_at, expires_at, card_version)"
                " VALUES (%s, %s, 'keep_online', %s, %s, %s, %s, %s, %s)",
                (
                    m.intent_id,
                    n["notification_id"],
                    f["file_id"],
                    f["versions"][-1],
                    n["identity_id"],
                    m.issued_at,
                    m.expires_at,
                    n["card_version"],
                ),
            )
        card = {
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [],
            "actions": [
                {
                    "type": "Action.Execute",
                    "title": "Keep online",
                    "verb": "keep_online",
                    "data": {"omnifile_intent": m.token},
                }
            ],
        }
        r = click(ctx["conv"], "Keep online", reader, card=card)
        assert r["body"]["statusCode"] in (400, 403, 410), r
        d = forward_direct(m.token, "keep_online", reader["oid"])
        assert d.status_code == 410 and d.json()["code"] == "intent_expired", d.text
        assert audits("teams.intent.refused", t_start, reason="intent_expired"), "not audited"
        return f"relay {r['body']['statusCode']}; api 410 intent_expired, audited"

    res.check("3d. an expired intent is refused (relay and api) and audited", expired)

    # 4. Injected restore failure -> Recall on the failed card -> the card follows to available.
    def failure_then_recall() -> str:
        rid = ctx["r2"]
        f = files["line_2001.segy"]
        wait_recall(rid, "rehydrating", 60)
        # As the change feed's RestoreFailed record does (worker/changefeed_sync.py).
        with db() as c:
            c.execute(
                "UPDATE catalog.restore SET state = 'failed', failure = 'injected'"
                " WHERE restore_id = (SELECT restore_id FROM catalog.recall"
                " WHERE recall_id = %s)",
                (int(rid),),
            )
        wait_recall(rid, "failed", 60)
        conv, act = ctx["conv"], ctx["act2"]
        failed = wait_card(conv, act, "failed", 60)
        assert "Recall" in buttons(failed), buttons(failed)
        r = click(conv, "Recall", reader, card=failed)
        assert r["status"] == 200 and r["body"].get("type", "").endswith("adaptive"), r
        with db() as c:
            new = c.execute(
                "SELECT recall_id FROM catalog.recall WHERE file_id = %s AND recall_id <> %s"
                " ORDER BY recall_id DESC LIMIT 1",
                (f["file_id"], int(rid)),
            ).fetchone()
        assert new, "no new recall"
        new_id = new["recall_id"]
        wait_recall(new_id, "rehydrating", 60)
        # The same card (activity) now follows the new recall.
        n = wait_for(
            "the new recall's notification to take over the card",
            lambda: (
                (x := notification(new_id, reader["sid"])) and x["teams_activity_id"] == act and x
            ),
        )
        wait_card(conv, act, "rehydrating", 60)
        advance(16 * 3600)
        wait_recall(new_id, "downloading", 60)
        edge.filled(new_id, f["versions"][-1], f["size"])
        wait_card(conv, act, "available", 60)
        assert audits("recall.request", t_start), "no recall.request audit"
        return (
            f"recall {rid} failed; Recall queued {new_id} (notification"
            f" {n['notification_id']}); the same card followed it to available"
        )

    res.check(
        "4. injected restore failure -> Recall on the failed card -> available", failure_then_recall
    )

    # 5. Bot Connector 403 -> email through Graph sendMail.
    def email_fallback() -> str:
        f = files["line_3001.segy"]
        before = len(fakes("GET", "/_fake/graph")["mail"])
        fakes("POST", "/_fake/bot/fail", {"status": 403, "count": 2})
        r = edge.stub_read(f["file_id"], f["versions"][-1], reader)
        rid = r["recall_id"]

        def mailed():
            mail = fakes("GET", "/_fake/graph")["mail"][before:]
            return [m for m in mail if reader["mail"] in m["to"]]

        mail = wait_for("an email to the reader", mailed, 60)
        assert "line_3001.segy" in mail[0]["subject"], mail[0]["subject"]
        n = wait_for("the email notification", lambda: notification(rid, reader["sid"], "email"))
        t = notification(rid, reader["sid"], "teams")
        assert t is not None and t["state"] == "fell_back", t
        return (
            f"recall {rid}: Bot Connector 403 twice -> teams notification fell_back,"
            f" email '{mail[0]['subject']}' to {mail[0]['to'][0]} ({n['state']})"
        )

    res.check("5. Bot Connector 403 -> email fallback via Graph sendMail", email_fallback)

    # 6. Restore of an old version (decision 34), as the share's Owner in the portal API.
    def restores() -> str:
        owner = Portal("owner")
        f = files["survey.txt"]
        old = f["versions"][0]

        def restore(target: str) -> dict:
            r = owner.post(f"/v1/files/{f['file_id']}/versions/{old}/restore", {"target": target})
            assert r.status_code == 202, r.text
            vr = r.json()

            def done():
                x = owner.get(f"/v1/version-restores/{vr['restore_id']}")
                assert x.status_code == 200, x.text
                return x.json()["state"] in ("done", "failed") and x.json()

            return wait_for(f"restore {target} done", done, 60)

        a = restore("restored")
        assert a["state"] == "done" and a["name"] == "survey-restored.txt", a
        r = owner.post(f"/v1/files/{f['file_id']}/versions/{old}/restore", {"target": "restored"})
        assert r.status_code == 409 and r.json()["code"] == "name_taken", r.text
        b = restore("restored_timestamped")
        assert b["state"] == "done", b
        assert re.fullmatch(r"survey-restored-\d{4}-\d\d-\d\d-\d{6}Z\.txt", b["name"]), b
        c_ = restore("overwrite")
        assert c_["state"] == "done" and c_["result_file_id"] == str(f["file_id"]), c_
        names = [
            e["name"] for e in owner.get(f"/v1/dirs/{s['survey_id']}/children").json()["items"]
        ]
        assert "survey-restored.txt" in names and b["name"] in names, names
        versions = owner.get(f"/v1/files/{f['file_id']}/versions").json()["items"]
        assert len(versions) == len(f["versions"]) + 1, versions
        return (
            f"restored -> {a['name']}; again -> 409 name_taken; timestamped -> {b['name']};"
            f" overwrite -> new current version {c_['result_version_id']}"
            f" ({len(versions)} versions)"
        )

    res.check(
        "6. restore choices (decision 34): restored, name_taken, timestamped, overwrite", restores
    )

    # 7. Policies: preview cost follows the body; save re-plans; proposals need approval.
    def policies() -> str:
        admin = Portal("admin")
        ds = s["dataset_id"]

        def preview(body: dict) -> dict:
            r = admin.post("/v1/policies/preview", {"dataset_id": str(ds), "body": body})
            assert r.status_code == 200, r.text
            return r.json()

        a = preview(POLICY)
        faster = {**POLICY, "warm_day": 7, "cold_day": 30, "archive_day": 90}
        b = preview(faster)
        ca, cb = a["cost"]["per_tb_first_year"], b["cost"]["per_tb_first_year"]
        assert ca["amount"] != cb["amount"], (ca, cb)
        assert a["timeline"]["archive_day"] == 365 and b["timeline"]["archive_day"] == 90
        active = admin.get(f"/v1/datasets/{ds}/policy").json()
        with db() as c:
            hot = c.execute(
                "SELECT next_action_at FROM catalog.file WHERE file_id = %s",
                (files["notes.txt"]["file_id"],),
            ).fetchone()["next_action_at"]
        r = admin.post(
            f"/v1/datasets/{ds}/policies",
            {"body": faster, "base_version": active["version"], "note": "e2e"},
        )
        assert r.status_code == 201, r.text
        saved = r.json()
        now_active = admin.get(f"/v1/datasets/{ds}/policy").json()
        assert now_active["version"] == saved["version"] and now_active["state"] == "active"

        def replanned():
            with db() as c:
                x = c.execute(
                    "SELECT next_action_at FROM catalog.file WHERE file_id = %s",
                    (files["notes.txt"]["file_id"],),
                ).fetchone()["next_action_at"]
            return x is not None and x != hot and x

        when = wait_for("the scheduler to re-plan", replanned, 30)
        return (
            f"first year per TB {ca['amount']} -> {cb['amount']} {cb['currency']};"
            f" saved v{saved['version']} active; next action {hot} -> {when}"
        )

    res.check(
        "7a. policy preview cost changes with the policy; a saved version is active and re-planned",
        policies,
    )

    def proposals() -> str:
        ds = s["dataset_id"]
        admin, admin2 = Portal("admin"), Portal("admin2")
        # Decision 52: an app's token is refused until an ArchiveAdmin registers it, with a
        # named owner who can see the dataset. Register it through the Settings route.
        r = admin.post(
            "/v1/settings/api-clients",
            {
                "app_id": stack()["partner"]["client_id"],
                "display_name": "Partner (e2e)",
                "scopes": ["Policy.Propose"],
                "owner_identity_id": admin.me["identity_id"],
            },
        )
        assert r.status_code == 201, r.text
        token = partner_token()
        h = {"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())}
        st = stack()
        with httpx.Client(
            base_url=st["urls"]["web"], verify=st["pki"]["ca_crt"], trust_env=False
        ) as web:
            body = {**POLICY, "archive_day": 200}
            r = web.post(
                "/v1/policies/proposals",
                headers=h,
                json={"dataset_id": str(ds), "body": body, "rationale": "e2e: cheaper"},
            )
            assert r.status_code == 201, r.text
            prop = r.json()
            r = web.post(
                f"/v1/policies/proposals/{prop['proposal_id']}/approve",
                json={},
                headers={**h, "Idempotency-Key": str(uuid.uuid4())},
            )
            assert r.status_code == 401, f"an app approved its own proposal: {r.text}"
        before = admin.get(f"/v1/datasets/{ds}/policy").json()
        assert before["body"]["archive_day"] != 200, "the proposal applied itself"
        # An ArchiveAdmin's own proposal: they can't approve it; another admin can.
        r = admin.post(
            "/v1/policies/proposals", {"dataset_id": str(ds), "body": body, "rationale": "mine"}
        )
        mine = r.json()
        r = admin.post(f"/v1/policies/proposals/{mine['proposal_id']}/approve", {})
        assert r.status_code == 403 and r.json()["code"] == "self_approval", r.text
        r = admin2.post(f"/v1/policies/proposals/{prop['proposal_id']}/approve", {})
        assert r.status_code in (200, 201), r.text
        after = admin.get(f"/v1/datasets/{ds}/policy").json()
        assert (
            after["body"]["archive_day"] == 200 and after.get("proposal_id") == prop["proposal_id"]
        ), after
        return (
            f"partner proposal {prop['proposal_id']} stored, not applied; app can't approve"
            f" (401); self-approval 403; another ArchiveAdmin approved -> v{after['version']}"
        )

    res.check("7b. partner proposal changes nothing until another ArchiveAdmin approves", proposals)

    # 8. A reader sees only what their AD groups can read.
    def reader_scope() -> str:
        rd = Portal("reader")
        root = rd.get(f"/v1/dirs/{s['root_id']}/children").json()["items"]
        names = sorted(e["name"] for e in root)
        assert names == ["survey"], names
        r = rd.get(f"/v1/dirs/{s['restricted_id']}/children")
        assert r.status_code == 404, r.text
        payroll = files["payroll.csv"]["file_id"]
        assert rd.get(f"/v1/files/{payroll}").status_code == 404
        own = Portal("owner")
        on = sorted(e["name"] for e in own.get(f"/v1/dirs/{s['root_id']}/children").json()["items"])
        assert on == ["restricted", "survey"], on
        return f"reader sees {names}; restricted/ and payroll.csv are 404; owner sees {on}"

    res.check("8. a reader's listings follow their AD groups", reader_scope)

    # 9. Tier pins, rewind (as of a date), share rewind depth and the share access screen.
    def pins_rewind_access() -> str:
        owner, admin, rd = Portal("owner"), Portal("admin"), Portal("reader")
        notes = files["notes.txt"]
        until = (datetime.now(UTC) + timedelta(days=7)).isoformat()
        r = owner.post(
            "/v1/pins",
            {
                "items": [str(notes["file_id"])],
                "tier": "cold",
                "until": until,
                "reason": "e2e: project paused",
            },
        )
        assert r.status_code == 201 and r.json()["pins"], r.text
        pin = owner.get(f"/v1/files/{notes['file_id']}").json()["pin"]
        assert pin and pin["tier"] == "cold", pin
        r = rd.post("/v1/pins", {"items": [str(notes["file_id"])], "tier": "hot", "reason": "x"})
        assert r.status_code == 201 and r.json()["skipped"][0]["code"] == "forbidden", r.text
        # Rewind: the folder as it was 2.5 days ago shows notes.txt's first version.
        as_of = (datetime.now(UTC) - timedelta(days=2, hours=12)).isoformat()
        rows = owner.get(f"/v1/dirs/{s['survey_id']}/children", params={"as_of": as_of})
        assert rows.status_code == 200, rows.text
        then = {e["name"]: e for e in rows.json()["items"]}
        assert then["notes.txt"]["content_version_id"] == str(notes["versions"][0]), then
        assert "notes-restored.txt" not in then, sorted(then)
        # Rewind depth (Owners): 30 -> 14 days; 20 days back is then beyond it.
        share = owner.get(f"/v1/shares/{s['share_id']}")
        r = owner.send(
            "PATCH",
            f"/v1/shares/{s['share_id']}/settings",
            {"rewind_depth_days": 14},
            headers={"If-Match": share.headers["etag"]},
        )
        assert r.status_code == 200 and r.json()["rewind"]["depth_days"] == 14, r.text
        old = (datetime.now(UTC) - timedelta(days=20)).isoformat()
        r = owner.get(f"/v1/dirs/{s['survey_id']}/children", params={"as_of": old})
        assert r.status_code == 410 and r.json()["code"] == "beyond_rewind_depth", r.text
        # The access screen (ArchiveAdmin): Readers becomes Finance; the reader loses the share.
        share = admin.get(f"/v1/shares/{s['share_id']}")
        g = {k: world.group_sid(k) for k in world.GROUPS}
        body = {
            "owners_group_sid": g["owners"],
            "writers_group_sid": g["writers"],
            "readers_group_sid": g["other"],
        }
        r = admin.send(
            "PUT",
            f"/v1/shares/{s['share_id']}/access",
            body,
            headers={"If-Match": share.headers["etag"]},
        )
        assert r.status_code == 200 and r.json()["groups"]["readers"]["sid"] == g["other"], r.text
        assert str(s["share_id"]) in rd.me["reader_of"], rd.me  # before the change
        mine = rd.get("/v1/me").json()
        assert str(s["share_id"]) not in mine["reader_of"], mine
        a = audits("share.access", t_start)
        assert a, "share.access is not audited"
        # Put it back for anyone running more checks.
        share = admin.get(f"/v1/shares/{s['share_id']}")
        r = admin.send(
            "PUT",
            f"/v1/shares/{s['share_id']}/access",
            {**body, "readers_group_sid": g["readers"]},
            headers={"If-Match": share.headers["etag"]},
        )
        assert r.status_code == 200, r.text
        return (
            "pin cold (Owner), reader's pin skipped (forbidden); as-of listing shows the"
            " first version; depth 30 -> 14 days, 20 days back 410; access PUT audited"
        )

    res.check(
        "9. tier pins, rewind as of a date, rewind depth, share access screen", pins_rewind_access
    )

    # 10. WORM enable on a WORM-layout share (decision 31): typed name, re-auth, lock deadline.
    def worm_enable() -> str:
        v = s["worm"]["vault2"]
        admin = Portal("admin")
        path = f"/v1/shares/{v['share_id']}/worm/enable"

        def enable(confirm: str, who: Portal) -> httpx.Response:
            etag = who.get(f"/v1/shares/{v['share_id']}").headers["etag"]
            body = {"retention_days": 7, "confirm_name": confirm, "window_hours": 2}
            return who.post(path, body, headers={"If-Match": etag})

        r = enable(v["name"].upper(), admin)
        assert r.status_code == 422 and r.json()["code"] == "confirmation_mismatch", r.text
        age_auth("admin")
        r = enable(v["name"], admin)
        assert r.status_code == 401 and r.json()["code"] == "reauth_required", r.text
        fresh = Portal("admin")
        r = enable(v["name"], fresh)
        assert r.status_code == 200, r.text
        w = r.json()["worm"]
        assert w["enabled"] and w["mode"] == "unlocked_test" and w["lock_deadline"], w
        assert audits("share.worm.confirm", t_start), "not audited"
        r = enable(v["name"], fresh)
        assert r.status_code == 409 and r.json()["code"] == "worm_violation", r.text
        return (
            "wrong name 422; stale sign-in 401 reauth_required; after re-auth enabled"
            f" (test window, locks {w['lock_deadline']}); again 409"
        )

    res.check("10. WORM enable: typed share name, re-authentication, lock deadline", worm_enable)

    write_results(res)
    return 0 if all(ok for _, ok, _ in res.rows) else 1


def write_results(res: Results) -> None:
    path = Path(os.environ.get("E2E_RESULTS") or REPO / "tests/e2e/results/m4-service.md")
    passed = sum(ok for _, ok, _ in res.rows)
    lines = [f"### Service side (tests/e2e/m4.py): {passed}/{len(res.rows)} passed", ""]
    lines += [f"- {'PASS' if ok else 'FAIL'} {name}: {detail}" for name, ok, detail in res.rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    print(f"{passed}/{len(res.rows)} passed; results in {path}")


def main() -> int:
    p = argparse.ArgumentParser(prog="tests/e2e/m4.py")
    p.add_argument("command", choices=["seed", "run", "age-auth"])
    p.add_argument("who", nargs="?", help="age-auth: a dev/m4/world.py user key")
    a = p.parse_args()
    if a.command == "seed":
        seed(a)
        return 0
    if a.command == "age-auth":
        age_auth(a.who)
        return 0
    return run(a)


def age_auth(who: str) -> None:
    """Make `who`'s portal sign-ins an hour old (portal-api.md 2.4): WORM changes and legal
    holds then need a fresh sign-in."""
    with db() as c:
        n = c.execute(
            "UPDATE catalog.web_session SET auth_time = auth_time - interval '1 hour'"
            " WHERE entra_object_id = %s",
            (world.user(who)["oid"],),
        ).rowcount
    print(json.dumps({"aged_sessions": n}))


if __name__ == "__main__":
    sys.exit(main())
