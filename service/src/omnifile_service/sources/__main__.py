"""Register a source and watch it seed: python -m omnifile_service.sources

    register --dataset N --backend N --bucket B [--prefix P/] [--path a/b]
             [--inventory s3://bucket/key/manifest.json
              | --metadata-table arn:aws:s3tables:REGION:ACCOUNT:bucket/aws-s3
                [--metadata-namespace b_BUCKET]
              | --list]
             --owners SID [--writers SID] [--readers SID]
    status [--source N]
    retry --source N        (a failed source, from where it stopped)

With none of --inventory, --metadata-table and --list, a bucket whose backend
names its S3 Metadata tables seeds from them, and any other is listed.

The catalog is the worker's (`catalog.connect`: OMNIFILE_PG_* with Entra sign-in on
Azure, or OMNIFILE_DATABASE_URL). The worker's `seed` queue does the
seeding; `register` checks the inventory and starts the feed now, then
queues it. `POST /v1/sources` on the partner API registers one too
(portal-api.md section 5.10), leaving the start to the worker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import urlsplit

from psycopg.rows import dict_row

from ..catalog.connect import Db, aconnect, from_env_or_exit
from ..worker.backends import BackendRegistry
from .acl import Groups
from .model import SourceError
from .run import register_source, retry_source


def parse_inventory(
    url: str | None,
    *,
    metadata_table: str | None = None,
    metadata_namespace: str | None = None,
    listing: bool = False,
) -> dict[str, str] | None:
    if sum((url is not None, metadata_table is not None, listing)) > 1:
        raise SystemExit("give one of --inventory, --metadata-table and --list")
    if listing:
        return {"kind": "listing"}
    if metadata_table is not None:
        out = {"kind": "s3_metadata", "table_bucket_arn": metadata_table}
        if metadata_namespace:
            out["namespace"] = metadata_namespace
        return out
    if url is None:
        return None
    u = urlsplit(url)
    if u.scheme != "s3" or not u.netloc or not u.path.strip("/"):
        raise SystemExit("--inventory is s3://<bucket>/<key of manifest.json>")
    return {"kind": "s3_inventory", "bucket": u.netloc, "manifest_key": u.path.lstrip("/")}


async def register(a: argparse.Namespace, url: Db) -> None:
    async with await aconnect(url, autocommit=True) as conn:
        registry = await BackendRegistry.load(conn)
        try:
            backend = registry.get(a.backend)
            sid = await register_source(
                conn, backend, dataset_id=a.dataset, container=a.bucket, prefix=a.prefix,
                path=a.path,
                inventory=parse_inventory(
                    a.inventory, metadata_table=a.metadata_table,
                    metadata_namespace=a.metadata_namespace, listing=a.list,
                ),
                groups=Groups(a.owners, a.writers, a.readers),
            )  # fmt: skip
        except SourceError as e:
            raise SystemExit(str(e)) from None
        finally:
            await registry.close()
    print(json.dumps({"source_id": sid, "state": "registered"}))


async def retry(a: argparse.Namespace, url: Db) -> None:
    async with await aconnect(url, autocommit=True) as conn:
        try:
            await retry_source(conn, a.source)
        except SourceError as e:
            raise SystemExit(str(e)) from None
    print(json.dumps({"source_id": a.source, "state": "seeding"}))


async def status(a: argparse.Namespace, url: Db) -> None:
    async with await aconnect(url, autocommit=True) as conn:
        cur = conn.cursor(row_factory=dict_row)
        await cur.execute(
            "SELECT source_id, dataset_id, path, backend_id, container, prefix, state,"
            "       snapshot_at, objects_seen, files_created, dirs_created, already_present,"
            "       skipped, last_error, seeded_at"
            "  FROM catalog.source WHERE %s::bigint IS NULL OR source_id = %s::bigint"
            " ORDER BY source_id",
            (a.source, a.source),
        )
        for row in await cur.fetchall():
            print(json.dumps(row, default=str))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m omnifile_service.sources")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register")
    r.add_argument("--dataset", type=int, required=True)
    r.add_argument("--backend", type=int, required=True)
    r.add_argument("--bucket", required=True)
    r.add_argument("--prefix", default="")
    r.add_argument("--path", default="")
    r.add_argument("--inventory")
    r.add_argument("--metadata-table", help="the S3 Metadata table bucket's ARN")
    r.add_argument("--metadata-namespace")
    r.add_argument("--list", action="store_true", help="list the prefix")
    r.add_argument("--owners", required=True)
    r.add_argument("--writers")
    r.add_argument("--readers")
    s = sub.add_parser("status")
    s.add_argument("--source", type=int)
    t = sub.add_parser("retry")
    t.add_argument("--source", type=int, required=True)
    a = p.parse_args(argv)
    url = from_env_or_exit("worker")
    commands = {"register": register, "status": status, "retry": retry}
    asyncio.run(commands[a.cmd](a, url))


if __name__ == "__main__":
    main()
