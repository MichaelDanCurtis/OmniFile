"""Fixtures for the `web` (portal API) tests.

Needs a Postgres 16+ server (OMNIFILE_TEST_DATABASE_URL, as for tests/api;
skipped without it). One migrated database per session; each test seeds its
own backend, dataset, share and files, so tests don't interfere.

- `web`: the app on https://portal.test, with the dev OIDC issuer mounted at
  /dev-idp, its token endpoint called in process, and the real Policy preview
  functions. One TestClient (one event loop, one pool); `Browser`s take turns
  on it, each with its own session cookie.
- Every call through `Browser.call` / `App.call` is checked against
  portal-api.yaml (spec.py).
"""

from __future__ import annotations

import itertools
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from omnifile_service.api.db import Catalog
from omnifile_service.catalog.migrate import migrate
from omnifile_service.catalog.privileges import as_app
from omnifile_service.web.app import create_app
from omnifile_service.web.auth import Authenticator, StaticJwks, StaticSecret
from omnifile_service.web.config import OidcEndpoints, WebSettings
from omnifile_service.web.context import SESSION_COOKIE, WebServices
from omnifile_service.web.devidp import DevClient, DevIdp
from omnifile_service.web.preview import PolicyPreviewPort

from . import spec

ENV = "OMNIFILE_TEST_DATABASE_URL"
ORIGIN = "https://portal.test"
TENANT = "00000000-0000-4000-8000-00000000aaaa"
CLIENT_ID = "portal-client"
API_AUDIENCE = "api://omnifile-test"
APP_ID = "prizmanyx-app"
APP_SECRET = "app-secret-" + "x" * 16


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    server = os.environ.get(ENV)
    if not server:
        pytest.skip(f"{ENV} is not set; web tests need a Postgres 16+ server")
    name = f"omnifile_web_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(server, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(server)
    params["dbname"] = name
    url = make_conninfo(**params)
    migrate(url)
    yield url
    with psycopg.connect(server, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def sql(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("SELECT set_config('omnifile.actor', 'system:test', false)")
        yield conn


@dataclass
class App:
    client: TestClient
    idp: DevIdp
    services: WebServices
    sql: psycopg.Connection

    def browser(self, user: dict[str, Any] | None = None, **kw: Any) -> Browser:
        b = Browser(self)
        if user is not None:
            b.sign_in(user, **kw)
        return b

    def app_token(self, roles: list[str], **kw: Any) -> str:
        return self.idp.app_token(APP_ID, roles, **kw)

    def call(
        self, method: str, template: str, *, token: str | None = None, json: Any = None,
        params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
        key: str | None = None, check: bool = True, **path: Any,
    ) -> httpx.Response:  # fmt: skip
        """A partner call with a bearer token (no cookies)."""
        self.client.cookies.clear()
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        if method == "POST" and not template.endswith("/preview"):
            h.setdefault("Idempotency-Key", key or str(uuid.uuid4()))
        return _send(self.client, method, template, json, params, h, check, path)


def _send(client, method, template, json, params, headers, check, path) -> httpx.Response:
    url = template
    for name, value in path.items():
        url = url.replace("{" + name + "}", str(value))
    if check:
        spec.check_request(method, template, json)
    resp = client.request(method, url, json=json, params=params, headers=headers)
    if check:
        spec.check_response(
            method, template, resp.status_code, resp.headers.get("content-type", ""), resp.content
        )
    return resp


@dataclass
class Browser:
    """A signed-in browser: its session cookie and CSRF token."""

    app: App
    session: str | None = None
    csrf: str | None = None
    user: dict[str, Any] = field(default_factory=dict)

    @property
    def client(self) -> TestClient:
        return self.app.client

    def _cookies(self) -> None:
        self.client.cookies.clear()
        if self.session:
            self.client.cookies.set(SESSION_COOKIE, self.session, domain="portal.test", path="/")

    def sign_in(self, user: dict[str, Any], *, return_to: str = "/", reauth: bool = False):
        self.user = user
        self._cookies()
        params = {"return_to": return_to}
        if reauth:
            params["reauth"] = "1"
        r1 = self.client.get("/auth/login", params=params, follow_redirects=False)
        assert r1.status_code == 302, r1.text
        r2 = self.client.get(r1.headers["location"] + "&" + urlencode(dev_params(user)),
                             follow_redirects=False)  # fmt: skip
        assert r2.status_code == 302, r2.text
        r3 = self.client.get(r2.headers["location"], follow_redirects=False)
        assert r3.status_code == 302, r3.text
        self.session = self.client.cookies.get(SESSION_COOKIE)
        assert self.session
        s = self.call("GET", "/auth/session")
        assert s.status_code == 200, s.text
        self.csrf = s.json()["csrf_token"]
        return r3

    def call(
        self, method: str, template: str, *, json: Any = None,
        params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
        key: str | None = None, check: bool = True, csrf: bool = True, **path: Any,
    ) -> httpx.Response:  # fmt: skip
        self._cookies()
        h = dict(headers or {})
        if method in ("POST", "PUT", "PATCH", "DELETE") and csrf:
            h.setdefault("X-CSRF-Token", self.csrf or "")
            h.setdefault("Origin", ORIGIN)
        if method == "POST" and template.startswith("/v1") and not template.endswith("/preview"):
            h.setdefault("Idempotency-Key", key or str(uuid.uuid4()))
        return _send(self.client, method, template, json, params, h, check, path)

    def get(self, template: str, **kw: Any) -> httpx.Response:
        return self.call("GET", template, **kw)

    def post(self, template: str, json: Any = None, **kw: Any) -> httpx.Response:
        return self.call("POST", template, json=json, **kw)


def dev_params(user: dict[str, Any]) -> dict[str, str]:
    out = {
        "dev_oid": user["oid"],
        "dev_name": user.get("name", "Test user"),
        "dev_roles": ",".join(user.get("roles", [])),
        "dev_groups": ",".join(user.get("groups", [])),
    }
    if user.get("sid"):
        out["dev_sid"] = user["sid"]
    if user.get("overage"):
        out["dev_overage"] = "1"
    if user.get("no_auth_time"):
        out["dev_no_auth_time"] = "1"
    return out


def location_query(resp: httpx.Response) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}


@pytest.fixture
def web(db_url: str, sql) -> Iterator[App]:
    endpoints = OidcEndpoints.dev(ORIGIN)
    settings = WebSettings(
        origin=ORIGIN,
        tenant_id=TENANT,
        client_id=CLIENT_ID,
        oidc=endpoints,
        api_audiences=(API_AUDIENCE, CLIENT_ID),
        dev_mode=True,
        rate_per_s=1000,
        rate_burst=1000,
        preview_rate_per_s=1000,
        login_rate_per_s=1000,
        login_burst=1000,
    )
    idp = DevIdp(
        issuer=endpoints.issuer,
        tenant_id=TENANT,
        clients={
            CLIENT_ID: DevClient(CLIENT_ID, "dev-secret",
                               [settings.redirect_uri, settings.azure_redirect_uri]),
            APP_ID: DevClient(APP_ID, APP_SECRET, (), ("Policy.Propose", "Recall.Request",
                                                      "Archive.Read", "Archive.Adopt")),
        },
        users={},
        api_audience=API_AUDIENCE,
    )  # fmt: skip
    idp_app = idp.app()
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.mounted()), base_url=ORIGIN)
    catalog = Catalog(as_app(db_url, "web"), max_size=6)
    services = WebServices(
        catalog=catalog,
        settings=settings,
        auth=Authenticator(settings, catalog, StaticJwks(idp.public_keys), http,
                           StaticSecret("dev-secret")),
        preview=PolicyPreviewPort(),
        closers=[http.aclose],
    )  # fmt: skip
    app = create_app(services, dev_idp=idp_app)
    register_test_app(sql)
    with TestClient(app, base_url=ORIGIN, client=("10.1.2.3", 50000)) as client:
        yield App(client, idp, services, sql)


#: The partner tests' application is a registered API client (portal-api.md 5.21),
#: owned by a DataSteward, so its calls and proposals are accepted.
APP_OWNER_SID = "S-1-5-21-900-900-900-1500"
#: Every application the web tests mint tokens for.
TEST_APPS = (APP_ID, "another-partner", "other-app")


def register_test_app(sql) -> None:
    sql.execute(
        "INSERT INTO catalog.identity (sid, kind, display_name, upn, app_roles, app_roles_seen_at)"
        " VALUES (%s, 'user', 'Owen Owner', 'owen@contoso.test', '{DataSteward}', now())"
        " ON CONFLICT (sid) DO UPDATE SET app_roles_seen_at = now()",
        (APP_OWNER_SID,),
    )
    for app in TEST_APPS:
        sql.execute(
            "INSERT INTO catalog.api_client (app_id, display_name, scopes, owner_identity_id,"
            " created_by_identity_id) SELECT %s, %s,"
            " '{Policy.Propose,Recall.Request,Archive.Read,Archive.Adopt}', i.identity_id,"
            " i.identity_id FROM catalog.identity i WHERE i.sid = %s"
            " ON CONFLICT (app_id) DO NOTHING",
            (app, f"Test {app}", APP_OWNER_SID),
        )


# --- the world: a dataset, a share, AD groups, ACLs and files ---------------------------------


_RIDS: dict[tuple[str, str], int] = {}
_RID_COUNTER = itertools.count(6000)


def _rid_for(domain: str, key: str) -> int:
    """One RID per person in a world, never shared (not hash(): it varies per process)."""
    return _RIDS.setdefault((domain, key), next(_RID_COUNTER))


@dataclass
class World:
    backend_id: int
    edge_id: int
    dataset_id: int
    root_id: int
    share_id: int
    share_name: str
    domain: str
    g_owners: str
    g_writers: str
    g_readers: str
    g_other: str
    acl_open: int
    acl_secret: int
    files: dict[str, int] = field(default_factory=dict)
    versions: dict[str, list[int]] = field(default_factory=dict)

    def user(self, key: str, *, roles: list[str] | None = None, groups: list[str] | None = None,
             sid_rid: int | None = None, **extra: Any) -> dict[str, Any]:  # fmt: skip
        rid = sid_rid or _rid_for(self.domain, key)
        return {
            "oid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.domain}/{key}")),
            "name": key.title(),
            "sid": f"{self.domain}-{rid}",
            "roles": roles if roles is not None else ["User"],
            "groups": groups or [],
            **extra,
        }

    # The cast.
    @property
    def reader(self) -> dict[str, Any]:
        return self.user("reader", groups=[self.g_readers], sid_rid=3001)

    @property
    def owner(self) -> dict[str, Any]:
        return self.user("owner", groups=[self.g_owners], sid_rid=3002)

    @property
    def writer(self) -> dict[str, Any]:
        return self.user("writer", groups=[self.g_writers], sid_rid=3008)

    @property
    def admin(self) -> dict[str, Any]:
        return self.user("admin", roles=["ArchiveAdmin"], sid_rid=3003)

    @property
    def admin2(self) -> dict[str, Any]:
        return self.user("admin2", roles=["ArchiveAdmin"], sid_rid=3004)

    @property
    def steward(self) -> dict[str, Any]:
        return self.user("steward", roles=["DataSteward"], groups=[self.g_readers], sid_rid=3005)

    @property
    def outsider(self) -> dict[str, Any]:
        return self.user("outsider", groups=[self.g_other], sid_rid=3007)


def _acl(sql, ds: int, read: list[str], deny: list[str] | None = None) -> int:
    (acl_id,) = sql.execute(
        "INSERT INTO catalog.acl (dataset_id, sha256, mode, entries, read_sids, deny_sids)"
        " VALUES (%s, %s, 488, '[]', %s, %s) RETURNING acl_id",
        (ds, uuid.uuid4().bytes * 2, read, deny or []),
    ).fetchone()
    return acl_id


def seed(sql, *, layout: str = "azure_two_account", depth: int = 30, depth_max: int = 90,
         nfs_sec: tuple[str, ...] = ("krb5",)) -> World:  # fmt: skip
    tag = uuid.uuid4().hex[:8]
    n = int(tag, 16)
    domain = f"S-1-5-21-{n % 10**9}-{(n >> 8) % 10**9}-{(n >> 16) % 10**9}"
    storage = "single_account" if layout == "azure_worm" else "two_account"
    container = "worm/data" if layout == "azure_worm" else "live/data"
    (backend_id,) = sql.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 'sim', %s, true) RETURNING backend_id",
        (f"sim-{tag}", Jsonb({"layout": layout, "container": container})),
    ).fetchone()
    (edge_id,) = sql.execute(
        "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
        " VALUES (%s, %s, now() + interval '1 day') RETURNING edge_id",
        (f"edge-{tag}", uuid.uuid4().bytes * 2),
    ).fetchone()
    (ds,) = sql.execute(
        "INSERT INTO catalog.dataset (name, backend_id, edge_id, storage_layout)"
        " VALUES (%s, %s, %s, %s) RETURNING dataset_id",
        (f"ds-{tag}", backend_id, edge_id, storage),
    ).fetchone()
    g = {k: f"{domain}-{rid}" for k, rid in
         (("owners", 2001), ("writers", 2002), ("readers", 2003), ("other", 2004))}  # fmt: skip
    acl_open = _acl(sql, ds, [g["owners"], g["writers"], g["readers"]])
    acl_secret = _acl(sql, ds, [g["owners"]])
    (root,) = sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, acl_id)"
        " VALUES (%s, NULL, '', 'dir', %s) RETURNING file_id",
        (ds, acl_open),
    ).fetchone()
    name = f"share-{tag}"
    (share_id,) = sql.execute(
        "INSERT INTO catalog.share (dataset_id, edge_id, name, protocols, nfs_sec,"
        " owners_group_sid, writers_group_sid, readers_group_sid, on_open,"
        " rewind_depth_days, rewind_depth_max_days)"
        " VALUES (%s, %s, %s, ARRAY['nfs4'], %s, %s, %s, %s, 'wait', %s, %s) RETURNING share_id",
        (ds, edge_id, name, list(nfs_sec), g["owners"], g["writers"], g["readers"], depth,
         depth_max),
    ).fetchone()  # fmt: skip
    for kind, sid in g.items():
        sql.execute(
            "INSERT INTO catalog.identity (sid, kind, display_name, sam_account_name)"
            " VALUES (%s, 'group', %s, %s)",
            (sid, f"{kind.title()} {tag}", f"grp-{kind}-{tag}"),
        )
    w = World(backend_id, edge_id, ds, root, share_id, name, domain, g["owners"], g["writers"],
              g["readers"], g["other"], acl_open, acl_secret)  # fmt: skip
    w.files[""] = root
    return w


def add_dir(sql, w: World, name: str, *, parent: int | None = None, acl: int | None = None) -> int:
    (fid,) = sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, acl_id)"
        " VALUES (%s, %s, %s, 'dir', %s) RETURNING file_id",
        (w.dataset_id, parent or w.root_id, name, acl or w.acl_open),
    ).fetchone()
    w.files[name] = fid
    return fid


def add_file(
    sql, w: World, name: str, *, parent: int | None = None, acl: int | None = None,
    tier: str = "hot", size: int = 4096, versions: int = 1, ago_days: float = 0,
    state: str = "resident", key: str | None = None, backend_version: str | None = None,
    crc: bytes | None = None,
) -> int:  # fmt: skip
    """A file with `versions` committed content versions (the last current),
    each with a committed primary location in `tier`."""
    acl = acl or w.acl_open
    resident = size if state == "resident" else 0
    (fid,) = sql.execute(
        "INSERT INTO catalog.file (dataset_id, parent_id, name, kind, state, size,"
        " resident_bytes, acl_id, mtime_ns, uid, gid, mode, written_at)"
        " VALUES (%s, %s, %s, 'file', %s, %s, %s, %s, 1700000000000000000, 1000, 1000, 420,"
        " now()) RETURNING file_id",
        (w.dataset_id, parent or w.root_id, name, state, size, resident, acl),
    ).fetchone()
    vids = []
    native = {"hot": "Hot", "warm": "Cool", "cold": "Cold", "archive": "Archive",
              "smart": "Smart"}[tier]  # fmt: skip
    container = "archive/data" if tier == "archive" else "live/data"
    for i in range(versions):
        at = f"now() - interval '{ago_days + (versions - 1 - i)} days'"
        (vid,) = sql.execute(
            "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind,"
            " state, size, acl_id, mtime_ns, uid, gid, mode, crc64nvme)"
            " VALUES (%s, %s, %s, %s, 'content', 'pending', %s, %s, 1700000000000000000,"
            " 1000, 1000, 420, %s) RETURNING version_id",
            (fid, w.dataset_id, parent or w.root_id, name, size, acl, crc),
        ).fetchone()
        sql.execute(
            "INSERT INTO catalog.location (file_id, version_id, backend_id, container,"
            " object_key, backend_version_id, role, state, logical_tier, storage_class, size,"
            f" committed_at, tier_changed_at, crc64nvme) VALUES (%s, %s, %s, %s, %s, %s,"
            f" 'primary', 'committed', %s, %s, %s, {at}, {at}, %s)",
            (fid, vid, w.backend_id, container, key or f"{fid}/{vid}", backend_version, tier,
             native, size, crc),
        )  # fmt: skip
        sql.execute(
            f"UPDATE catalog.file_version SET state = 'committed', committed_at = {at}"
            " WHERE version_id = %s",
            (vid,),
        )
        vids.append(vid)
    w.files[name] = fid
    w.versions[name] = vids
    return fid
