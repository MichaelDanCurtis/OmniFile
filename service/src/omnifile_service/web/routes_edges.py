"""Edges: what each enrolled edge last reported (portal-api.md section 5.14;
decision 37, docs/design/observability.md section 2). M6.

`GET /v1/edges` lists every edge that isn't revoked, with its heartbeat:
software version, kernel, smb-guard state, outbox depth, fills and recalls
in flight, FSAL connections and data-volume space, plus a `status` the
service judges with the same thresholds as the alert rules
(deploy/azure/modules/app-metrics.bicep):

- `silent`: enrolled, and no heartbeat for 15 minutes (or ever);
- `attention`: the guard isn't armed, less than 10% of the data volume is
  free, or the break-glass account was used and not sealed again;
- `ok` otherwise; `pending` before enrolment.

ArchiveAdmin, or an app with `Archive.Read`.

`POST /v1/edges` and `POST /v1/edges/{edge_id}/bundle` add an edge and issue
its one-time enrolment bundle, which the edge's setup wizard takes
(docs/design/setup-wizard.md). ArchiveAdmin only.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from .. import __version__
from ..api.validate import rfc3339
from ..enrol import bundle
from .context import Ctx, Principal, WebResult, audit
from .problems import WebError, invalid, not_found, role_required
from .routes_sources import _reader

SILENT_AFTER = timedelta(minutes=15)
FREE_MIN = 0.10

_EDGES = """
SELECT edge_id, name, state, software_version, kernel, guard_state, health,
       last_seen_at, last_heartbeat_at, enrolled_at, cert_not_after
  FROM catalog.edge
 WHERE state <> 'revoked'
 ORDER BY name
"""


#: An edge that has failed to read its shares' quiet periods this long, files
#: wait the longest period, and a change to a share's period doesn't reach it
#: (decision 67, #80 re-check).
QUIET_STALE_S = 300


def status_of(e: dict[str, Any], now) -> str:
    if e["state"] == "pending":
        return "pending"
    hb = e["last_heartbeat_at"]
    if hb is None or now - hb > SILENT_AFTER:
        return "silent"
    h = e["health"] or {}
    free, cap = h.get("data_free_bytes"), h.get("data_capacity_bytes")
    if (
        e["guard_state"] not in (None, "armed")
        or (free is not None and cap and free / cap < FREE_MIN)
        or h.get("recovery_used") == 1
        or (h.get("quiet_stale_s") or 0) >= QUIET_STALE_S
    ):
        return "attention"
    return "ok"


def _ts(v) -> str | None:
    return None if v is None else rfc3339(v)


async def list_edges(ctx: Ctx) -> WebResult:
    _reader(ctx.who)
    async with ctx.svc.catalog.read() as db:
        rows = await (await db.execute(_EDGES)).fetchall()
    out = []
    for e in rows:
        h = e["health"] or {}
        out.append(
            {
                "edge_id": str(e["edge_id"]),
                "name": e["name"],
                "state": e["state"],
                "status": status_of(e, ctx.now),
                "software_version": e["software_version"],
                "kernel": e["kernel"],
                "guard_state": e["guard_state"],
                "last_heartbeat_at": _ts(e["last_heartbeat_at"]),
                "certificate_not_after": _ts(e["cert_not_after"]),
                "health": {k: h[k] for k in sorted(h) if isinstance(h[k], int)},
            }
        )
    return WebResult(200, {"edges": out})


# ------------------------------------------------------------------ the appliance

#: The install images appliance/convert.sh writes from one build, by platform. One
#: version names every part of a release (docs/release/README.md), so the files are
#: omnifile-edge_<service version>.<ext>. AWS has none: no EC2 image is built.
IMAGES = (
    ("hyperv", "vhdx"),
    ("vmware", "ova"),
    ("kvm", "qcow2"),
    ("azure", "vhd"),
)


_PEP440 = re.compile(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?")
_PRE = {"a": "alpha", "b": "beta", "rc": "rc"}


def release_version(v: str | None) -> str | None:
    """The release's VERSION from the service's PEP 440 version (release/stamp.sh
    writes 0.2.0-rc.1 as 0.2.0rc1). None for a build that isn't a release."""
    m = _PEP440.fullmatch(v or "")
    if m is None:
        return None
    return m[1] + (f"-{_PRE[m[2]]}.{m[3]}" if m[2] else "")


async def get_appliance(ctx: Ctx) -> WebResult:
    """`GET /v1/edges/appliance`: what the Add edge wizard needs to know. Which
    release's images to use, where they are published (only when the operator
    set OMNIFILE_APPLIANCE_IMAGES_URL: there is no default download location),
    whether enrolment is configured, and how long a code stays usable."""
    _admin(ctx.who)
    s = ctx.svc.settings
    release = release_version(__version__)
    base = s.appliance_images_url.rstrip("/") + "/" if s.appliance_images_url else None
    images = []
    for platform, ext in IMAGES:
        file = f"omnifile-edge_{release}.{ext}" if release else None
        images.append({
            "platform": platform,
            "file": file,
            "url": base + file if base and file else None,
        })  # fmt: skip
    return WebResult(200, {
        "release": release,
        "images": images,
        "checksums": base + "SHA256SUMS" if base else None,
        "enrolment_configured": bool(s.enrol_url and s.enrol_server_ca_sha256),
        "code_ttl_s": int(s.enrol_bundle_ttl.total_seconds()),
    })  # fmt: skip


# ------------------------------------------------------------------ adding edges

#: An edge's name: what the setup wizard calls the filer, and what the portal shows.
NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")


def _admin(who: Principal) -> None:
    """Adding an edge hands out a credential: ArchiveAdmin only, never an app."""
    if who.is_app or not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")


def _enrol_settings(ctx: Ctx) -> tuple[str, str]:
    s = ctx.svc.settings
    if not s.enrol_url or not s.enrol_server_ca_sha256:
        raise WebError(
            "unavailable",
            "enrolment isn't configured on this service: set OMNIFILE_ENROL_PUBLIC_URL and "
            "OMNIFILE_ENROL_SERVER_CA (deploy/azure/README.md)",
        )
    return s.enrol_url, s.enrol_server_ca_sha256


def _issued(edge_id: int, name: str, token: str, expires, url: str, ca: str) -> dict[str, Any]:
    return {
        "edge_id": str(edge_id),
        "name": name,
        "bundle": bundle.make(url, ca, token),
        "expires_at": rfc3339(expires),
    }


async def add_edge(ctx: Ctx) -> WebResult:
    """`POST /v1/edges {name}`: a pending edge and its one-time bundle (section 5.15)."""
    _admin(ctx.who)
    url, ca = _enrol_settings(ctx)
    extra = set(ctx.body) - {"name"}
    if extra:
        raise invalid(sorted(extra)[0], "is not an edge field")
    name = ctx.body.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise invalid(
            "name", "1 to 63 lowercase letters, digits or hyphens, starting with a letter"
        )
    token, token_sha = bundle.new_token()
    expires = ctx.now + ctx.svc.settings.enrol_bundle_ttl
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        row = await (
            await db.execute(
                "INSERT INTO catalog.edge (name, enrolment_token_sha256, enrolment_expires_at)"
                " VALUES (%s, %s, %s) ON CONFLICT (name) DO NOTHING RETURNING edge_id",
                (name, token_sha, expires),
            )
        ).fetchone()
        if row is None:
            raise WebError("name_taken", f"an edge named {name} exists")
        await audit(db, ctx.who, "edge.add", details={"edge_id": str(row["edge_id"]), "name": name})
    return WebResult(201, _issued(row["edge_id"], name, token, expires, url, ca))


async def new_bundle(ctx: Ctx) -> WebResult:
    """`POST /v1/edges/{edge_id}/bundle`: a new bundle for an edge that hasn't
    enrolled; the previous one stops working."""
    _admin(ctx.who)
    url, ca = _enrol_settings(ctx)
    edge_id = ctx.path_id("edge_id")
    token, token_sha = bundle.new_token()
    expires = ctx.now + ctx.svc.settings.enrol_bundle_ttl
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        row = await (
            await db.execute(
                "UPDATE catalog.edge SET enrolment_token_sha256 = %s, enrolment_expires_at = %s"
                " WHERE edge_id = %s AND state = 'pending' RETURNING name",
                (token_sha, expires, edge_id),
            )
        ).fetchone()
        if row is None:
            state = await (
                await db.execute("SELECT state FROM catalog.edge WHERE edge_id = %s", (edge_id,))
            ).fetchone()
            if state is None:
                raise not_found("edge")
            raise WebError(
                "edge_enrolled",
                f"the edge is {state['state']}: a bundle is only for an edge that hasn't enrolled",
            )
        details = {"edge_id": str(edge_id), "name": row["name"]}
        await audit(db, ctx.who, "edge.bundle", details=details)
    return WebResult(201, _issued(edge_id, row["name"], token, expires, url, ca))


async def revoke_edge(ctx: Ctx) -> WebResult:
    """`POST /v1/edges/{edge_id}/revoke {}`: a lost or stolen filer stops
    working at once (section 5.15). Its certificate, current and previous,
    is refused on every control call, it can never renew, and an unused
    bundle stops working. Its Roles Anywhere certificates go into the edge
    CA's CRL for AWS (decision 50). Its datasets, shares and files stay. Signed-in
    ArchiveAdmin with a recent sign-in, audited. One-way: a filer that is
    found again enrols as a new edge."""
    _admin(ctx.who)
    ctx.svc.auth.require_recent_auth(ctx.who)
    if ctx.body:
        raise invalid(sorted(ctx.body)[0], "is not a revoke field")
    edge_id = ctx.path_id("edge_id")
    async with ctx.svc.catalog.tx(ctx.who.actor) as db:
        row = await (
            await db.execute(
                "UPDATE catalog.edge SET state = 'revoked', revoked_at = %s,"
                " enrolment_token_sha256 = NULL, enrolment_expires_at = NULL"
                " WHERE edge_id = %s AND state <> 'revoked' RETURNING name",
                (ctx.now, edge_id),
            )
        ).fetchone()
        if row is None:
            had = await (
                await db.execute("SELECT name FROM catalog.edge WHERE edge_id = %s", (edge_id,))
            ).fetchone()
            if had is None:
                raise not_found("edge")
            return WebResult(
                200, {"edge_id": str(edge_id), "name": had["name"], "state": "revoked"}
            )
        await audit(
            db, ctx.who, "edge.revoke", details={"edge_id": str(edge_id), "name": row["name"]}
        )
        # Its Roles Anywhere certificates go into the edge CA's CRL, which
        # the worker uploads to each trust anchor (decision 50).
        await db.execute(
            "SELECT pgmq.send('roles_anywhere_crl', %s::jsonb)",
            (json.dumps({"reason": "revoke", "edge_id": str(edge_id)}),),
        )
    return WebResult(200, {"edge_id": str(edge_id), "name": row["name"], "state": "revoked"})
