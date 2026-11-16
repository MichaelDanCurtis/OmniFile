"""Storage backends: the `catalog.backend` rows datasets and sources name.

A row is checked by the code the service builds it with, before anything is
written:

- **Azure Blob** (`AzureBlobConfig.from_mapping`, the worker's own parser). Two
  layouts (AGENTS.md, "Azure backend: two storage accounts per region"):
  - ``two_account`` (non-WORM): a live account whose default tier is Smart
    (ZRS) and an archive account (LRS or GRS), which must differ, because
    Archive is forbidden on ZRS. New files land in ``<live>/data``.
  - ``single_account`` (WORM shares, decision 31): one account, versioned, with
    version-level WORM when asked. Smart is refused: it can't hold Archive.
  Accounts are reached by their usual blob names; with private networking,
  private DNS resolves them to the private endpoints (decision 22).
- **Amazon S3** (`validate_backend_config` and `S3Config.for_backend`, against
  the service's own AWS settings, ``OMNIFILE_AWS_ACCOUNTS``): only the account,
  its region and the buckets. The account must be configured there, in that
  region. Endpoints, roles and queues are deployment settings, never a row.
  S3 is a migration source, never a dataset's home (decision 18), and a
  source is never one of OmniFile's own operations buckets (decision 40).

The portal's Settings, Storage page (`settings/storage.py`) builds its rows with
`azure_row` and `s3_row`, so the CLI and the page refuse the same things. It
writes them through the catalog's owner-run `backend_register` (migration
0035), which keeps the same rules as `register` here; the catalog's
`backend_guard` holds them for every writer.

**Private networking is never turned off by a row.** An S3 row can't ask for
public access when the deployment's AWS settings are private (the default). An
Azure row asks for it only in development (``OMNIFILE_DEV_MODE=1``): the
deployment turns storage public access off, and decision 22 forbids a public
fallback. A private row is never made public.

**Idempotent.** The row is found by name. The same answers again change
nothing. Different answers update a row no data uses yet. Once a dataset, a
source or a migration uses it, nothing changes but what only adds or informs
(`UNDER_DATA_MAY_CHANGE`, `UNDER_DATA_ADDITIVE`): S3 buckets may be added,
never dropped, and an inventory rule, a display name and an Azure row's region
may change. Accounts, buckets, layout, versioning, archive class and every
other key never move under data. The catalog's `backend_guard` (migration
0035) holds the same rule for every writer, the portal's Settings, Storage
included. Two runs adding one name at once both end well: the second
compares against the first's row. Every add and update is audited
(``system:admin``), with the database user and, since under Entra group sign-in
that user is the group, the token's ``oid``, ``upn``, ``appid`` and ``tid``.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..backends.azure.config import AzureBlobConfig
from ..backends.s3.config import (
    ENV_ACCOUNTS,
    ENV_ACCOUNTS_FILE,
    ENV_AUDIENCE,
    AwsDeployment,
    ConfigError,
    S3Config,
    validate_backend_config,
)
from ..backends.sim.capabilities import S3_DEEP_ARCHIVE, S3_GLACIER
from ..worker.backends import capabilities_for

#: The CLI's kinds and the catalog's.
KINDS = {"azure": "azure_blob", "s3": "s3"}
#: As dataset names (web/routes_provision.py).
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
#: Azure storage account names: 3 to 24 lowercase letters and digits.
AZURE_ACCOUNT = re.compile(r"[a-z0-9]{3,24}")
#: Azure container names: 3 to 63 lowercase letters, digits and single hyphens.
AZURE_CONTAINER = re.compile(r"(?=.{3,63}$)[a-z0-9]+(-[a-z0-9]+)*")
#: Azure region names, e.g. westeurope, eastus2.
AZURE_REGION = re.compile(r"[a-z][a-z0-9]{2,39}")
AWS_ACCOUNT = re.compile(r"[0-9]{12}")
AWS_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]")
#: S3 bucket names (general purpose), as the partner API checks them.
S3_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
BLOB_SUFFIX = "blob.core.windows.net"
#: What may change on a row data uses: keys that only inform (they move nothing), per
#: kind. Everything else, added, changed or removed, is refused. The catalog's
#: `backend_guard` (migration 0035) holds the same list for every writer, the portal's
#: Settings included; a test checks the two agree.
UNDER_DATA_MAY_CHANGE: dict[str, frozenset[str]] = {
    "azure_blob": frozenset({"region", "inventory", "display_name"}),
    "s3": frozenset({"inventory", "display_name"}),
}
#: Keys that may only grow while data uses the row: S3 buckets are added, never dropped.
UNDER_DATA_ADDITIVE: dict[str, frozenset[str]] = {"s3": frozenset({"buckets"})}
AUDIT_ACTOR = "system:admin"


class Refused(ValueError):
    """What the catalog must not hold, and why."""


@dataclass(frozen=True)
class Row:
    name: str
    #: The catalog's kind: ``azure_blob`` or ``s3``.
    kind: str
    config: dict[str, Any]
    private_network: bool


@dataclass(frozen=True)
class Result:
    #: ``added``, ``updated`` or ``unchanged``.
    outcome: str
    backend_id: int
    row: Row
    changes: list[str] = field(default_factory=list)


def _name(name: str | None, default: str) -> str:
    name = default if name is None else name
    if not NAME.fullmatch(name):
        raise Refused(
            f"--name {name!r}: 1 to 63 letters, digits, '.', '_' or '-', starting with a"
            " letter or digit"
        )
    return name


def _azure_account(flag: str, name: str) -> str:
    if not AZURE_ACCOUNT.fullmatch(name):
        raise Refused(
            f"{flag} {name!r} is not a storage account name: 3 to 24 lowercase letters and"
            " digits (the storageAccounts output of the deployment)"
        )
    return name


def azure_row(
    *,
    name: str | None = None,
    live: str | None = None,
    archive: str | None = None,
    account: str | None = None,
    worm: bool = False,
    smart: bool | None = None,
    container: str = "data",
    region: str | None = None,
    public: bool = False,
    env: Mapping[str, str],
) -> Row:
    """An Azure Blob backend: the live/archive pair, or one (WORM) account."""
    if account and (live or archive):
        raise Refused(
            "give --live and --archive (two accounts, non-WORM) or --account (one account),"
            " not both"
        )
    if not AZURE_CONTAINER.fullmatch(container):
        raise Refused(
            f"--container {container!r} is not a container name: 3 to 63 lowercase letters,"
            " digits and single hyphens"
        )
    config: dict[str, Any]
    if account:
        _azure_account("--account", account)
        if smart:
            raise Refused(
                "Smart needs the two-account layout: Smart needs zone-redundant storage, and"
                " Archive is forbidden on it, so one account can't hold both"
            )
        config = {
            "layout": "single_account",
            "accounts": {account: f"https://{account}.{BLOB_SUFFIX}"},
            "primary_account": account,
            "container": f"{account}/{container}",
        }
        if worm:
            config["worm"] = True
        default_name = f"azure-{account}"
    else:
        if not live or not archive:
            raise Refused(
                "the two-account layout needs both --live and --archive: the live account"
                " (ZRS, default tier Smart) and the archive account (LRS or GRS) of one"
                " region. One account (WORM shares): --account ACCOUNT"
            )
        if worm:
            raise Refused(
                "WORM shares use one account with version-level WORM (decision 31):"
                " --account ACCOUNT --worm"
            )
        _azure_account("--live", live)
        _azure_account("--archive", archive)
        if live == archive:
            raise Refused(
                "--live and --archive must be two accounts: Smart needs zone-redundant"
                " storage, and Archive is forbidden on it"
            )
        config = {
            "layout": "two_account",
            "accounts": {
                live: f"https://{live}.{BLOB_SUFFIX}",
                archive: f"https://{archive}.{BLOB_SUFFIX}",
            },
            "primary_account": live,
            "archive_account": archive,
            "smart": True if smart is None else smart,
            "container": f"{live}/{container}",
        }
        default_name = "azure"
    if region is not None:
        if not AZURE_REGION.fullmatch(region):
            raise Refused(f"--region {region!r} is not an Azure region name, e.g. westeurope")
        config["region"] = region
    if public and env.get("OMNIFILE_DEV_MODE") != "1":
        raise Refused(
            "private networking can't be turned off: the service reaches Azure storage only"
            " through its private endpoints, never a public path (decision 22). A public row"
            " is for development only (OMNIFILE_DEV_MODE=1)"
        )
    try:
        # What the worker and api build from the row.
        AzureBlobConfig.from_mapping(config)
        capabilities_for("azure_blob", config)
    except (ValueError, KeyError, TypeError) as e:
        raise Refused(f"the service would refuse this Azure backend: {e}") from None
    return Row(_name(name, default_name), "azure_blob", config, not public)


def _buckets(raw: Sequence[str]) -> list[str]:
    out: list[str] = []
    for part in raw:
        for b in str(part).split(","):
            b = b.strip()
            if not b:
                continue
            ip = True
            try:
                ipaddress.ip_address(b)
            except ValueError:
                ip = False
            if not S3_BUCKET.fullmatch(b) or ".." in b or ip:
                raise Refused(
                    f"bucket {b!r} is not an S3 bucket name: 3 to 63 lowercase letters,"
                    " digits, '.' and '-', starting and ending with a letter or digit"
                )
            if b not in out:
                out.append(b)
    if not out:
        raise Refused(
            "--buckets names the source buckets: a binding open to every bucket is refused"
        )
    return out


def s3_row(
    *,
    name: str | None = None,
    account: str,
    region: str,
    buckets: Sequence[str],
    versioning: bool = True,
    archive_class: str | None = None,
    public: bool = False,
    env: Mapping[str, str],
) -> Row:
    """An S3 backend (a migration source), checked against the service's AWS settings."""
    if not AWS_ACCOUNT.fullmatch(account):
        raise Refused(f"--account {account!r} is not a 12-digit AWS account id")
    if not AWS_REGION.fullmatch(region):
        raise Refused(f"--region {region!r} is not an AWS region name, e.g. eu-west-1")
    config: dict[str, Any] = {"account_id": account, "region": region, "buckets": _buckets(buckets)}
    if not versioning:
        config["versioning"] = False
    if archive_class is not None:
        if archive_class not in (S3_GLACIER, S3_DEEP_ARCHIVE):
            raise Refused(f"--archive-class is {S3_GLACIER} or {S3_DEEP_ARCHIVE}")
        config["archive_class"] = archive_class
    if not (env.get(ENV_ACCOUNTS) or env.get(ENV_ACCOUNTS_FILE)):
        raise Refused(
            f"the service's AWS settings are needed to check the row as the worker will:"
            f" set {ENV_ACCOUNTS_FILE} (or {ENV_ACCOUNTS}) and {ENV_AUDIENCE}, as the"
            " deployment passes them"
        )
    try:
        dep = AwsDeployment.from_env(env)
        validate_backend_config(config, deployment=dep)
        built = S3Config.for_backend(config, private_network=not public, deployment=dep)
    except (ConfigError, OSError) as e:
        raise Refused(f"the service would refuse this S3 backend: {e}") from None
    if public and built.private_network:
        raise Refused(
            "private networking is on in the service's AWS settings: S3 is reached only"
            " through its PrivateLink interface endpoints, never a public path (decision 22)"
        )
    ops = [b for b in config["buckets"] if b in built.ops_buckets]
    if ops:
        raise Refused(
            f"{', '.join(ops)}: OmniFile's own operations bucket (staging, Batch manifests or"
            " reports; decision 40), the only place it writes. A source is another bucket"
        )
    capabilities_for("s3", config)
    return Row(_name(name, f"s3-{account}"), "s3", config, not public)


# ---------------------------------------------------------------- catalog


def _usage(cur: psycopg.Cursor, backend_id: int) -> dict[str, int]:
    got = cur.execute(
        "SELECT"
        " (SELECT count(*) FROM catalog.dataset"
        "   WHERE backend_id = %(id)s OR source_backend_id = %(id)s) AS datasets,"
        " (SELECT count(*) FROM catalog.source WHERE backend_id = %(id)s) AS sources,"
        " (SELECT count(*) FROM catalog.migration_prefix WHERE to_backend_id = %(id)s)"
        "   AS migrations",
        {"id": backend_id},
    ).fetchone()
    return {k: int(v) for k, v in dict(got or {}).items()}


def _change_under_data(kind: str, old: Mapping[str, Any], new: Mapping[str, Any]) -> str | None:
    """Why `new` can't replace `old` while data uses the row, or None: any key
    added, changed or removed, but those of `UNDER_DATA_MAY_CHANGE`, and buckets
    only added (`UNDER_DATA_ADDITIVE`)."""
    why = []
    changed = []
    missing = object()
    for k in sorted(set(old) | set(new)):
        if k in UNDER_DATA_ADDITIVE.get(kind, frozenset()):
            gone = [b for b in (old.get(k) or ()) if b not in (new.get(k) or ())]
            if gone:
                why.append(f"{k} {', '.join(gone)} would be dropped")
        elif k in UNDER_DATA_MAY_CHANGE.get(kind, frozenset()):
            continue
        elif old.get(k, missing) != new.get(k, missing):
            changed.append(k)
    if changed:
        why.append(f"{', '.join(sorted(changed))} would change")
    return "; ".join(why) or None


def _changes(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    out = []
    for k in sorted(set(old) | set(new)):
        if old.get(k) != new.get(k):
            out.append(f"{k}: {old.get(k)!r} -> {new.get(k)!r}")
    return out


def _audit(cur: psycopg.Cursor, action: str, details: dict[str, Any]) -> None:
    cur.execute(
        "INSERT INTO catalog.audit_log (actor, action, decision, details)"
        " VALUES (%s, %s, 'allow', %s::jsonb || jsonb_build_object('db_user', session_user))",
        (AUDIT_ACTOR, action, Jsonb(details)),
    )


def register(
    conn: psycopg.Connection, row: Row, *, signed_in: Mapping[str, str] | None = None
) -> Result:
    """Add `row`, or bring the row of that name to it. `conn` is autocommit; the
    work is one transaction. `signed_in`: the Entra claims of the sign-in
    (`db.signed_in_as`), for the audit record."""
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        lock = (
            "SELECT backend_id, kind, config, private_network FROM catalog.backend"
            " WHERE name = %s FOR UPDATE"
        )
        old = cur.execute(lock, (row.name,)).fetchone()
        details: dict[str, Any] = {
            "name": row.name,
            "kind": row.kind,
            "config": row.config,
            "private_network": row.private_network,
        }
        if signed_in:
            details["entra"] = dict(signed_in)
        if old is None:
            got = cur.execute(
                "INSERT INTO catalog.backend (name, kind, config, private_network)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (name) DO NOTHING RETURNING backend_id",
                (row.name, row.kind, Jsonb(row.config), row.private_network),
            ).fetchone()
            if got is not None:
                backend_id = int(got["backend_id"])
                _audit(cur, "backend.add", {"backend_id": str(backend_id), **details})
                return Result("added", backend_id, row)
            # Another run added it since: its row (committed, since the INSERT
            # waited for it) is compared like any other.
            old = cur.execute(lock, (row.name,)).fetchone()
            if old is None:
                raise Refused(f"{row.name} was added and removed at the same moment: run again")
        backend_id = int(old["backend_id"])
        before = dict(old["config"] or {})
        if old["kind"] != row.kind:
            raise Refused(
                f"backend {row.name} ({backend_id}) is {old['kind']}, not {row.kind}: a backend"
                " never changes kind. Give this one another --name"
            )
        if before == row.config and old["private_network"] == row.private_network:
            return Result("unchanged", backend_id, row)
        if old["private_network"] and not row.private_network:
            raise Refused(
                f"backend {row.name} ({backend_id}) uses private networking, and a row never"
                " turns it off (decision 22)"
            )
        uses = _usage(cur, backend_id)
        if any(uses.values()):
            why = _change_under_data(row.kind, before, row.config)
            if why:
                held = ", ".join(f"{n} {k}" for k, n in uses.items() if n)
                raise Refused(
                    f"backend {row.name} ({backend_id}) holds data ({held}): {why}. Accounts,"
                    " buckets, region and layout never change under data; register new"
                    " storage under another --name"
                )
        cur.execute(
            "UPDATE catalog.backend SET config = %s, private_network = %s WHERE backend_id = %s",
            (Jsonb(row.config), row.private_network, backend_id),
        )
        changes = _changes(before, row.config)
        if old["private_network"] != row.private_network:
            changes.append(f"private_network: {old['private_network']} -> {row.private_network}")
        _audit(
            cur,
            "backend.update",
            {
                "backend_id": str(backend_id),
                **details,
                "before": {"config": before, "private_network": old["private_network"]},
            },
        )
        return Result("updated", backend_id, row, changes)


def list_backends(conn: psycopg.Connection) -> list[dict[str, Any]]:
    cur = conn.cursor(row_factory=dict_row)
    rows = cur.execute(
        "SELECT backend_id, name, kind, config, private_network, created_at"
        " FROM catalog.backend ORDER BY backend_id"
    ).fetchall()
    return [{**r, "uses": _usage(cur, int(r["backend_id"]))} for r in rows]


def show_backend(conn: psycopg.Connection, which: str) -> dict[str, Any] | None:
    cur = conn.cursor(row_factory=dict_row)
    by_id = which.isdigit()
    r = cur.execute(
        "SELECT backend_id, name, kind, config, private_network, created_at"
        " FROM catalog.backend WHERE " + ("backend_id = %s" if by_id else "name = %s"),
        (int(which) if by_id else which,),
    ).fetchone()
    if r is None:
        return None
    return {**r, "uses": _usage(cur, int(r["backend_id"]))}


def where(kind: str, config: Mapping[str, Any]) -> str:
    """Where a row's bytes are, in a few words."""
    if kind == "azure_blob":
        if config.get("layout", "two_account") == "two_account":
            s = f"live {config.get('primary_account')}, archive {config.get('archive_account')}"
            s += ", Smart" if config.get("smart") else ""
        else:
            s = f"account {config.get('primary_account')}"
            s += ", WORM" if config.get("worm") else ""
        return s + (f", {config['region']}" if config.get("region") else "")
    if kind == "s3":
        buckets = config.get("buckets") or ()
        return (
            f"account {config.get('account_id')}, {config.get('region')}, buckets"
            f" {', '.join(buckets) if buckets else '(any)'}"
        )
    return str(config.get("layout") or config.get("data_plane") or "")
