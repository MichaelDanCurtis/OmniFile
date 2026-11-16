"""`python -m omnifile_service.admin`: the administrator's commands.

Exit codes: 0 done (or already as asked), 1 refused or failed, 2 bad usage or
no catalog configured.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

import psycopg

from ..catalog import connect
from . import backends, dead_letters
from .db import admin_db, signed_in_as

RESTART = "The api and worker read the backends when they start: restart both apps so they use it."


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m omnifile_service.admin",
        description="OmniFile administrator commands. Run them as the catalog's administrator.",
    )
    top = p.add_subparsers(dest="area", required=True)
    b = top.add_parser("backend", help="storage backends (catalog.backend)")
    cmd = b.add_subparsers(dest="command", required=True)

    add = cmd.add_parser("add", help="register a storage backend (idempotent)")
    kind = add.add_subparsers(dest="kind", required=True)

    az = kind.add_parser(
        "azure",
        help="Azure Blob: the live/archive pair of a region, or one WORM account",
        description="Two accounts (non-WORM): --live and --archive. One account (WORM"
        " shares): --account, with --worm.",
    )
    az.add_argument("--name", help="the backend's name (default: azure, or azure-<account>)")
    az.add_argument("--live", help="the live account (ZRS, default tier Smart)")
    az.add_argument("--archive", help="the archive account (LRS or GRS)")
    az.add_argument("--account", help="one account (WORM shares), instead of the pair")
    az.add_argument("--worm", action="store_true", help="version-level WORM (with --account)")
    az.add_argument(
        "--smart",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="the live account's default tier is Smart (default: yes, with two accounts)",
    )
    az.add_argument("--container", default="data", help="where new files land (default: data)")
    az.add_argument("--region", help="the accounts' Azure region, e.g. westeurope (recorded)")
    az.add_argument("--public", action="store_true", help="no private networking (dev only)")

    s3 = kind.add_parser("s3", help="Amazon S3: a migration source's buckets")
    s3.add_argument("--name", help="the backend's name (default: s3-<account>)")
    s3.add_argument("--account", required=True, help="the 12-digit AWS account id")
    s3.add_argument("--region", required=True, help="the buckets' region, e.g. eu-west-1")
    s3.add_argument(
        "--buckets", required=True, action="append", help="source buckets, comma-separated"
    )
    s3.add_argument(
        "--versioning",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="the buckets have versioning on (default: yes)",
    )
    s3.add_argument("--archive-class", help="GLACIER (default) or DEEP_ARCHIVE")
    s3.add_argument(
        "--public", action="store_true", help="no PrivateLink (only if the AWS settings allow)"
    )

    ls = cmd.add_parser("list", help="every backend, and what uses it")
    ls.add_argument("--json", action="store_true", help="one JSON object per line")
    show = cmd.add_parser("show", help="one backend, as JSON")
    show.add_argument("backend", help="its name or id")

    d = top.add_parser(
        "dead-letters",
        help="messages the worker gave up on (the dead-letters alert)",
        description=dead_letters.RESEND_WARNING,
    )
    dcmd = d.add_subparsers(dest="command", required=True)
    dls = dcmd.add_parser("list", help="what is in the dead-letter queues")
    dls.add_argument("--queue", help="one queue, e.g. restores (default: all)")
    dls.add_argument("--json", action="store_true", help="one JSON object per line")
    for name, about in (
        ("resend", "put messages back on their queue, with all their tries"),
        ("drop", "take messages out without running them (kept in the queue's archive)"),
    ):
        one = dcmd.add_parser(name, help=about)
        one.add_argument("--queue", required=True, help="the queue, e.g. restores")
        one.add_argument("--msg-id", type=int, action="append", default=[], help="one message")
        one.add_argument("--all", action="store_true", help="every message in the queue")
        one.add_argument("--yes", action="store_true", help="confirm --all")
        if name == "drop":
            one.add_argument(
                "--force",
                action="store_true",
                help="drop a message even though nothing would send it again",
            )
    return p


def _row(a: argparse.Namespace, env: Mapping[str, str]) -> backends.Row:
    if a.kind == "azure":
        return backends.azure_row(
            name=a.name,
            live=a.live,
            archive=a.archive,
            account=a.account,
            worm=a.worm,
            smart=a.smart,
            container=a.container,
            region=a.region,
            public=a.public,
            env=env,
        )
    return backends.s3_row(
        name=a.name,
        account=a.account,
        region=a.region,
        buckets=a.buckets,
        versioning=a.versioning,
        archive_class=a.archive_class,
        public=a.public,
        env=env,
    )


def _json(r: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "backend_id": str(r["backend_id"]),
        "name": r["name"],
        "kind": r["kind"],
        "private_network": bool(r["private_network"]),
        "config": r["config"] or {},
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        "uses": r["uses"],
    }


def _dead_letters(
    a: argparse.Namespace,
    conn: psycopg.Connection,
    signed_in: Mapping[str, str],
    out: TextIO,
) -> None:
    if a.command == "list":
        found = dead_letters.letters(conn, a.queue)
        if a.json:
            for x in found:
                print(json.dumps(x.to_json(), sort_keys=True), file=out)
            return
        if not found:
            print("no dead letters", file=out)
            return
        print(f"{'QUEUE':<20} {'MSG':>8}  {'DEAD SINCE':<20} {'GAVE UP':<7}  ERROR", file=out)
        for x in found:
            print(
                f"{x.queue:<20} {x.msg_id:>8}  {x.dead_at:%Y-%m-%d %H:%M:%S}  "
                f"{'yes' if x.gave_up else 'no':<7}  {x.error[:100]}",
                file=out,
            )
            print(f"{'':<20} {'':>8}  {json.dumps(x.message, sort_keys=True)[:200]}", file=out)
        return
    if a.command == "resend":
        done = dead_letters.resend(
            conn, a.queue, a.msg_id, everything=a.all, yes=a.yes, signed_in=signed_in
        )
        print(f"resent {len(done)} message(s): {', '.join(str(x.msg_id) for x in done)}", file=out)
        print(f"warning: {dead_letters.RESEND_WARNING}", file=out)
        return
    gone = dead_letters.drop(
        conn, a.queue, a.msg_id, everything=a.all, yes=a.yes, force=a.force, signed_in=signed_in
    )
    print(f"dropped {len(gone)} message(s): {', '.join(str(x.msg_id) for x, _ in gone)}", file=out)
    for x, why in gone:
        if why:
            print(f"stranded: message {x.msg_id}: {why}", file=out)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    credential: connect.TokenCredential | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    env = os.environ if env is None else env
    out = out or sys.stdout
    err = err or sys.stderr
    a = _parser().parse_args(argv)
    try:
        # Checked before anything connects: a bad answer never reaches the catalog.
        row = _row(a, env) if a.area == "backend" and a.command == "add" else None
    except backends.Refused as e:
        print(f"refused: {e}", file=err)
        return 1
    try:
        db = admin_db(env, credential=credential)
    except ValueError as e:
        print(f"catalog: {e}", file=err)
        return 2
    try:
        with connect.connect(db, autocommit=True) as conn:
            if a.area == "dead-letters":
                _dead_letters(a, conn, signed_in_as(db, env), out)
            elif row is not None:
                r = backends.register(conn, row, signed_in=signed_in_as(db, env))
                what = f"{row.name} (backend {r.backend_id}, {row.kind}"
                what += ", private network)" if row.private_network else ", PUBLIC network)"
                print(f"{r.outcome}: {what}: {backends.where(row.kind, row.config)}", file=out)
                for c in r.changes:
                    print(f"  {c}", file=out)
                if r.outcome != "unchanged":
                    print(RESTART, file=out)
            elif a.command == "list":
                rows = backends.list_backends(conn)
                if a.json:
                    for r in rows:
                        print(json.dumps(_json(r), sort_keys=True), file=out)
                else:
                    print(
                        f"{'ID':>4}  {'NAME':<20} {'KIND':<10} {'PRIVATE':<7}  USED BY  WHERE",
                        file=out,
                    )
                    for r in rows:
                        u = r["uses"]
                        used = f"{u['datasets']}d {u['sources']}s {u['migrations']}m"
                        print(
                            f"{r['backend_id']:>4}  {r['name']:<20} {r['kind']:<10}"
                            f" {'yes' if r['private_network'] else 'NO':<7}  {used:<7}"
                            f"  {backends.where(r['kind'], r['config'] or {})}",
                            file=out,
                        )
            else:
                found = backends.show_backend(conn, a.backend)
                if found is None:
                    print(f"no backend {a.backend}", file=err)
                    return 1
                print(json.dumps(_json(found), indent=2, sort_keys=True), file=out)
    except backends.Refused as e:
        print(f"refused: {e}", file=err)
        return 1
    except psycopg.errors.InsufficientPrivilege as e:
        print(
            f"refused: {str(e).strip()}. Only the catalog's administrator does this"
            " (no app may): connect as the Entra administrator, as for the migrations",
            file=err,
        )
        return 1
    except psycopg.errors.UndefinedTable as e:
        print(
            f"the catalog has no tables yet ({str(e).strip()}): run the migrations first", file=err
        )
        return 1
    except psycopg.OperationalError as e:
        print(f"can't reach the catalog: {str(e).strip()}", file=err)
        return 1
    return 0
