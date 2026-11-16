"""Sources (portal-api.md section 5.10; catalog-seeding.md): register data that
already lives in a backend as a migration source of a dataset, or adopt data
Prizmanyx already moved into the dataset's own backend, and follow its
seeding.

`web` touches only the catalog: registering writes the source and queues
its seed (`sources.run.create_source`); the worker reads the inventory's
snapshot time and starts the change feed on the first seed step, so a bad
inventory shows up as a `failed` source with `last_error`.

Apps need `Archive.Adopt` to register or retry and `Archive.Read` (or
`Archive.Adopt`) to read; signed-in users need ArchiveAdmin. Apps act at
dataset level and every registration is audited (access-control.md
section 5).

A source names its backend by `backend_id` (and `container`), or inline as
`backend: {kind, config}` (the base contract's shape, section 11). Inline
config only *finds* a backend an ArchiveAdmin configured: it holds the
bucket and where it is, never anything that steers the service's
credentials or network. Endpoints, roles, web-identity audiences, CA
bundles, queues and `private_network: false` are refused with
`422 config_invalid` naming the field (line B's 88c525b): a partner who could
set them could send the service's token, or its traffic, to a host of their
choosing.
"""

from __future__ import annotations

import re
from typing import Any

from ..api.validate import parse_id, rfc3339, sid
from ..backends.s3.config import ConfigError, S3Config, ops_buckets_of
from ..sources.acl import Groups
from ..sources.model import SourceError
from ..sources.run import create_source, retry_source
from .context import Ctx, Principal, WebResult, audit, page, tuple_rows
from .problems import WebError, forbidden, invalid, not_found, role_required

FIELDS = {"dataset_id", "backend_id", "container", "backend", "prefix", "path", "inventory",
          "groups"}  # fmt: skip
#: What a partner may put in an inline S3 backend's config: the bucket and
#: where it is. `container` is A's name for the bucket; `private_network` may
#: only be true (it is the backend's, and a partner can't lower it).
S3_PARTNER_FIELDS = {"bucket", "container", "region", "account_id", "requester_pays",
                     "private_network"}  # fmt: skip
#: Named in the refusal, so the partner knows it is a deployment setting.
DEPLOYMENT_FIELDS = {
    "endpoint_url", "sqs_endpoint_url", "edge_endpoint", "control_endpoint_url", "endpoint",
    "role_arn", "roles_anywhere", "web_identity", "audience", "credentials", "ca_bundle",
    "event_queues", "batch", "staging_bucket", "buckets", "edge_credentials", "path_style",
    "extra_client_kwargs", "sts_endpoint_url", "s3tables_endpoint_url", "endpoints",
    "metadata_tables", "owned_buckets", "sim_credentials", "tls_max_version",
}  # fmt: skip
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
_ACCOUNT = re.compile(r"^[0-9]{12}$")
STATES = ("registered", "seeding", "seeded", "failed")


def _adopter(who: Principal) -> None:
    if who.is_app:
        if not who.has("Archive.Adopt"):
            raise role_required("Archive.Adopt")
    elif not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")


def _reader(who: Principal) -> None:
    if who.is_app:
        if not who.has("Archive.Read", "Archive.Adopt"):
            raise role_required("Archive.Read", "Archive.Adopt")
    elif not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")


def source_json(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": str(s["source_id"]),
        "kind": s["kind"],
        "dataset_id": str(s["dataset_id"]),
        "path": s["path"],
        "backend_id": str(s["backend_id"]),
        "container": s["container"],
        "prefix": s["prefix"],
        "inventory": s["inventory"],
        "groups": {
            "owners": s["owners_sid"],
            "writers": s["writers_sid"],
            "readers": s["readers_sid"],
        },  # fmt: skip
        "state": s["state"],
        "snapshot_at": rfc3339(s["snapshot_at"]),
        "counts": {
            "objects_seen": s["objects_seen"],
            "files_created": s["files_created"],
            "dirs_created": s["dirs_created"],
            "already_present": s["already_present"],
            "skipped": s["skipped"],
        },
        "last_error": s["last_error"],
        "active_migrations": int(s.get("active_migrations") or 0),
        "created_at": rfc3339(s["created_at"]),
        "updated_at": rfc3339(s["updated_at"]),
        "seeded_at": rfc3339(s["seeded_at"]),
    }


def _text(b: dict[str, Any], field: str, *, required: bool = False) -> str:
    v = b.get(field)
    if v is None:
        if required:
            raise invalid(field, "is required")
        return ""
    if not isinstance(v, str) or len(v) > 1024:
        raise invalid(field, "must be a string of at most 1024 characters")
    return v


#: S3 Metadata tables: the table bucket and names only. Endpoints and
#: credentials are the backend's (sources/metadata_s3.py), never the caller's.
METADATA_FIELDS = {"kind", "table_bucket_arn", "namespace", "table", "journal_table"}


def _inventory(v: Any) -> dict[str, str] | None:
    if v is None:
        return None
    if isinstance(v, dict) and v.get("kind") == "listing":
        if set(v) != {"kind"}:
            raise invalid("inventory", "a listing takes no settings")
        return {"kind": "listing"}
    if isinstance(v, dict) and v.get("kind") == "s3_metadata":
        if set(v) - METADATA_FIELDS:
            raise invalid(
                "inventory", f"{sorted(set(v) - METADATA_FIELDS)[0]} is not an s3_metadata field"
            )
        for k in METADATA_FIELDS - {"kind"}:
            if k in v and (not isinstance(v[k], str) or not v[k] or len(v[k]) > 1024):
                raise invalid("inventory", f"{k} must be a string")
        if "table_bucket_arn" not in v:
            raise invalid("inventory", "table_bucket_arn is required")
        return dict(v)
    if not isinstance(v, dict) or set(v) - {"kind", "bucket", "manifest_key"}:
        raise invalid("inventory", "must be {kind, bucket, manifest_key} or null")
    if v.get("kind") != "s3_inventory":
        raise invalid("inventory", "kind must be s3_inventory, s3_metadata or listing")
    for k in ("bucket", "manifest_key"):
        if not isinstance(v.get(k), str) or not v[k]:
            raise invalid("inventory", f"{k} is required")
    return {"kind": "s3_inventory", "bucket": v["bucket"], "manifest_key": v["manifest_key"]}


def _groups(v: Any) -> Groups:
    if not isinstance(v, dict) or set(v) - {"owners", "writers", "readers"}:
        raise invalid("groups", "must be {owners, writers?, readers?}")
    if v.get("owners") is None:
        raise invalid("groups", "owners is required")
    out = {}
    for k in ("owners", "writers", "readers"):
        out[k] = None if v.get(k) is None else sid(v[k], f"groups.{k}")
    return Groups(out["owners"], out["writers"], out["readers"])  # type: ignore[arg-type]


def _config_invalid(field: str, why: str) -> WebError:
    return WebError("config_invalid", f"{field}: {why}")


def inline_s3(v: Any) -> dict[str, Any]:
    """The partner allow-list for `backend: {kind, config}`. Returns the
    checked config: bucket, region and account_id (or None)."""
    if not isinstance(v, dict):
        raise _config_invalid("backend", "must be {kind, config}")
    for k in v:
        if k not in ("kind", "config"):
            raise _config_invalid(f"backend.{k}", "is not a field of an inline backend")
    if v.get("kind") != "s3":
        raise _config_invalid("backend.kind", "v1 takes s3 sources only (decision 23)")
    config = v.get("config")
    if not isinstance(config, dict):
        raise _config_invalid("backend.config", "must be an object")
    for k in config:
        if k in DEPLOYMENT_FIELDS:
            raise _config_invalid(f"backend.config.{k}", "is a deployment setting an"
                                  " ArchiveAdmin makes, never a partner's")  # fmt: skip
        if k not in S3_PARTNER_FIELDS:
            allowed = ", ".join(sorted(S3_PARTNER_FIELDS))
            why = f"is not a field a partner may set (allowed: {allowed})"
            raise _config_invalid(f"backend.config.{k}", why)
    if "private_network" in config and config["private_network"] is not True:
        raise _config_invalid("backend.config.private_network", "a partner can't turn private"
                              " networking off (decision 22)")  # fmt: skip
    if "bucket" in config and "container" in config and config["bucket"] != config["container"]:
        raise _config_invalid("backend.config.container", "names another bucket than bucket")
    bucket = config.get("bucket", config.get("container"))
    if not isinstance(bucket, str) or not _BUCKET.match(bucket):
        raise _config_invalid("backend.config.bucket", "is required: an S3 bucket name")
    region = config.get("region")
    if not isinstance(region, str) or not _REGION.match(region):
        raise _config_invalid("backend.config.region", "is required: an AWS region name")
    account = config.get("account_id")
    if account is not None and (not isinstance(account, str) or not _ACCOUNT.match(account)):
        raise _config_invalid("backend.config.account_id", "must be the 12-digit account id")
    payer = config.get("requester_pays", False)
    if not isinstance(payer, bool):
        raise _config_invalid("backend.config.requester_pays", "must be true or false")
    if payer:
        raise _config_invalid("backend.config.requester_pays", "requester-pays buckets are not"
                              " supported yet")  # fmt: skip
    return {"bucket": bucket, "region": region, "account_id": account}


async def resolve_inline(db, cfg: dict[str, Any]) -> int:
    """The backend an ArchiveAdmin configured for this bucket: an S3 backend
    in the region whose `buckets` names it (an inline backend never matches a
    binding open to every bucket). Private networking first."""
    found = await (
        await db.execute(
            "SELECT backend_id, config FROM catalog.backend"
            " WHERE kind = 's3' AND config->>'region' = %s"
            "   AND jsonb_typeof(config->'buckets') = 'array' AND config->'buckets' ? %s"
            " ORDER BY private_network DESC, backend_id",
            (cfg["region"], cfg["bucket"]),
        )
    ).fetchall()
    if not found:
        why = (
            f"no S3 backend an ArchiveAdmin configured serves {cfg['bucket']!r} in {cfg['region']}"
        )
        raise _config_invalid("backend.config.bucket", why)
    for b in found:
        conf = b["config"] or {}
        known = conf.get("account_id") or (conf.get("batch") or {}).get("account_id")
        if cfg["account_id"] is None or known is None or known == cfg["account_id"]:
            return int(b["backend_id"])
    raise _config_invalid("backend.config.account_id", "is not the account of the backend"
                          " configured for this bucket")  # fmt: skip


def _refused(e: SourceError) -> WebError:
    if e.code == "invalid_request":
        return WebError("invalid_request", str(e), errors=[{"field": "body", "message": str(e)}])
    return WebError(e.code, str(e))


#: A source with the count of its migrations that aren't cancelled, so the
#: portal can offer to start one where there is none (whatever the page size).
SOURCE_SELECT = (
    "SELECT s.*, (SELECT count(*) FROM catalog.migration_prefix m"
    " WHERE m.source_id = s.source_id AND m.state <> 'cancelled') AS active_migrations"
    " FROM catalog.source s"
)


async def _load(db, source_id: int) -> dict[str, Any]:
    s = await (await db.execute(SOURCE_SELECT + " WHERE s.source_id = %s", (source_id,))).fetchone()
    if s is None:
        raise not_found("source")
    return s


async def register(ctx: Ctx) -> WebResult:
    """`POST /v1/sources`: data to migrate from another backend."""
    return await _register(ctx, "source")


async def adopt(ctx: Ctx) -> WebResult:
    """`POST /v1/adopt`: data Prizmanyx already moved into the dataset's own
    backend, cataloged at its original paths with no migration."""
    return await _register(ctx, "adopt")


async def _register(ctx: Ctx, kind: str) -> WebResult:
    who = ctx.who
    _adopter(who)
    b = ctx.body
    extra = set(b) - FIELDS
    if extra:
        raise invalid(sorted(extra)[0], "is not a source field")
    inline = None
    if "backend" in b:
        if "backend_id" in b or "container" in b:
            raise invalid("backend", "give backend_id and container, or backend, not both")
        if kind == "adopt":
            raise _config_invalid("backend", "adopt takes the dataset's own backend_id")
        inline = inline_s3(b["backend"])
    for field in ("dataset_id", "groups") + (() if inline else ("backend_id",)):
        if field not in b:
            raise invalid(field, "is required")
    dataset_id = parse_id(b["dataset_id"], "dataset_id")
    backend_id = None if inline else parse_id(b["backend_id"], "backend_id")
    container = inline["bucket"] if inline else _text(b, "container", required=True)
    prefix, path = _text(b, "prefix"), _text(b, "path")
    inventory = _inventory(b.get("inventory"))
    groups = _groups(b["groups"])
    async with ctx.svc.catalog.tx(who.actor) as db:
        if inline is not None:
            backend_id = await resolve_inline(db, inline)
        assert backend_id is not None
        try:
            with tuple_rows(db):
                source_id = await create_source(
                    db, dataset_id=dataset_id, backend_id=backend_id, container=container,
                    groups=groups, prefix=prefix, path=path, inventory=inventory, kind=kind,
                )  # fmt: skip
        except SourceError as e:
            raise _refused(e) from None
        action = "source.adopt" if kind == "adopt" else "source.register"
        await audit(db, who, action, dataset_id=dataset_id,
                    details={"source_id": str(source_id), "backend_id": str(backend_id),
                             "container": container, "prefix": prefix, "path": path,
                             "inventory": inventory, "inline_backend": inline})  # fmt: skip
        out = source_json(await _load(db, source_id))
    return WebResult(201, out)


async def list_sources(ctx: Ctx) -> WebResult:
    _reader(ctx.who)
    ds = ctx.query_id("dataset_id")
    state = ctx.query("state")
    if state is not None and state not in STATES:
        raise invalid("state", "is not a source state")
    limit = ctx.limit()
    after = ctx.cursor("sources")
    async with ctx.svc.catalog.read() as db:
        found = await (
            await db.execute(
                SOURCE_SELECT + " WHERE s.kind <> 'annex'"  # listed under /v1/annexes
                "   AND (%(ds)s::bigint IS NULL OR s.dataset_id = %(ds)s::bigint)"
                "   AND (%(state)s::text IS NULL OR s.state = %(state)s::text)"
                "   AND (%(after)s::bigint IS NULL OR s.source_id < %(after)s::bigint)"
                " ORDER BY s.source_id DESC LIMIT %(limit)s",
                {
                    "ds": ds,
                    "state": state,
                    "after": int(after[0]) if after else None,
                    "limit": limit + 1,
                },  # fmt: skip
            )
        ).fetchall()
    more = len(found) > limit
    found = list(found[:limit])
    nxt = ctx.next_cursor("sources", [found[-1]["source_id"]]) if more else None
    return WebResult(200, page([source_json(s) for s in found], nxt))


async def get_source(ctx: Ctx) -> WebResult:
    _reader(ctx.who)
    async with ctx.svc.catalog.read() as db:
        return WebResult(200, source_json(await _load(db, ctx.path_id("source_id"))))


async def retry(ctx: Ctx) -> WebResult:
    who = ctx.who
    _adopter(who)
    if ctx.body:
        raise invalid(sorted(ctx.body)[0], "retry takes no fields")
    source_id = ctx.path_id("source_id")
    async with ctx.svc.catalog.tx(who.actor) as db:
        s = await _load(db, source_id)
        try:
            with tuple_rows(db):
                await retry_source(db, source_id)
        except SourceError as e:
            raise _refused(e) from None
        details = {"source_id": str(source_id), "last_error": s["last_error"]}
        await audit(db, who, "source.retry", dataset_id=s["dataset_id"], details=details)
        out = source_json(await _load(db, source_id))
    return WebResult(200, out)


# --- the backends a source can come from -----------------------------------------------

#: Backend kinds a source can be registered from (v1: S3, decision 23).
SOURCE_KINDS = ("s3",)


def source_backend_json(b: dict[str, Any]) -> dict[str, Any]:
    """What the portal's New migration wizard needs to pick a bucket: the
    backend's region and buckets, never its endpoints, roles or
    credentials. `buckets` null: the backend takes any bucket (it lists
    none). OmniFile's own operations buckets are never offered."""
    config = b["config"] or {}
    try:
        s3 = S3Config.for_backend(config, private_network=b["private_network"])
    except ConfigError:
        # Not loadable here (create_source treats it the same way): the
        # row's own fields still name its buckets.
        s3 = None
    ops = ops_buckets_of(config) | (s3.ops_buckets if s3 is not None else frozenset())
    listed = config.get("buckets")
    buckets = (
        sorted(str(x) for x in listed if str(x) not in ops) if isinstance(listed, list) else None
    )
    tables = sorted(s3.metadata_tables) if s3 is not None else []
    return {
        "backend_id": str(b["backend_id"]),
        "name": b["name"],
        "kind": b["kind"],
        "region": config.get("region"),
        "private_network": bool(b["private_network"]),
        "buckets": buckets,
        "metadata_tables": [t for t in tables if buckets is None or t in buckets],
    }


async def list_source_backends(ctx: Ctx) -> WebResult:
    """`GET /v1/source-backends`: the backends an ArchiveAdmin configured
    that data can migrate from, with their buckets. For a signed-in
    ArchiveAdmin only: the portal picks one and never creates one (a partner
    names its bucket inline instead, `backend: {kind, config}`)."""
    who = ctx.who
    if who.is_app:
        raise forbidden("the source picker is for a signed-in ArchiveAdmin")
    if not who.has("ArchiveAdmin"):
        raise role_required("ArchiveAdmin")
    async with ctx.svc.catalog.read() as db:
        rows = await (
            await db.execute(
                "SELECT backend_id, name, kind, config, private_network FROM catalog.backend"
                " WHERE kind = ANY(%s) ORDER BY name, backend_id",
                (list(SOURCE_KINDS),),
            )
        ).fetchall()
    return WebResult(200, {"backends": [source_backend_json(b) for b in rows]})
