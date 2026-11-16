"""Run the facade: python -m omnifile_service.backends.sim_http [--port 10000] ...

Prints one JSON line with the base URL and the user delegation key, so a test
harness can mint SAS tokens itself (or POST /_sim/sas). With ``--layout s3``
the line is ``{"url", "layout", "imds_url", "region", "access_key_id",
"secret_access_key"}`` instead (`s3.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

import uvicorn

from ..base import Clock, SystemClock
from ..sim import OffsetClock, SimBackend, VirtualClock
from .app import BlobFacade, create_app

LAYOUTS = {
    "two-account": SimBackend.azure_two_account,
    "worm": SimBackend.azure_worm,
    # M5: the S3-shaped facade (s3.py): SigV4-checked S3, STS and the fake
    # IMDSv2 that plays aws_signing_helper serve for the edge.
    "s3": SimBackend.s3,
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m omnifile_service.backends.sim_http")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=10000, help="0 picks a free port")
    p.add_argument("--layout", choices=sorted(LAYOUTS), default="two-account")
    p.add_argument(
        "--clock",
        choices=["system", "virtual", "offset"],
        default="system",
        help="virtual: time moves only via POST /_sim/advance; offset: the wall clock plus "
        "the offset in --clock-file, shared with other processes and moved by /_sim/advance",
    )
    p.add_argument(
        "--clock-file",
        help="the shared offset file for --clock offset (libfaketime's +<seconds> form)",
    )
    p.add_argument("--key-file", help="also write the user delegation key JSON here (mode 0600)")
    p.add_argument(
        "--admin",
        action="store_true",
        help="enable the /_sim/ helper endpoints (SAS minting, clock, faults, backend RPC):"
        " dev and tests only",
    )
    p.add_argument(
        "--s3",
        action="store_true",
        help="also host an S3-flavoured sim, read through presigned GETs under /_s3/",
    )
    p.add_argument("--log-level", default="warning")
    p.add_argument(
        "--bucket",
        action="append",
        default=[],
        help="--layout s3: create this bucket at start (repeatable)",
    )
    p.add_argument("--region", default="eu-west-1", help="--layout s3: the region it claims")
    return p.parse_args(argv)


def build(args: argparse.Namespace) -> tuple[object, object]:
    clock: Clock
    if args.clock == "virtual":
        clock = VirtualClock()
    elif args.clock == "offset":
        if not args.clock_file:
            raise SystemExit("--clock offset needs --clock-file")
        clock = OffsetClock(args.clock_file)
    else:
        clock = SystemClock()
    if args.layout == "s3":
        from .s3 import S3Facade, create_s3_app

        # SigV4 clock skew is checked only against the real clock.
        s3_facade = S3Facade(
            SimBackend.s3(clock), region=args.region, check_skew=args.clock == "system"
        )
        for bucket in args.bucket:
            s3_facade.create_bucket(bucket)
        return s3_facade, create_s3_app(s3_facade, admin=args.admin)
    s3 = SimBackend.s3(clock, backend_id=2) if args.s3 else None
    facade = BlobFacade(LAYOUTS[args.layout](clock), key_file=args.key_file, s3=s3)
    return facade, create_app(facade, admin=args.admin)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    facade, app = build(args)
    key: object = None
    if isinstance(facade, BlobFacade):
        key = facade.key.to_json()  # published to --key-file (mode 0600), and on rotation
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level=args.log_level)
    server = uvicorn.Server(config)

    async def serve() -> None:
        task = asyncio.create_task(server.serve())
        while not server.started:
            if task.done():
                await task
                sys.exit(1)
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        url = f"http://{args.host}:{port}"
        info: dict[str, object] = {"url": url, "layout": args.layout, "key": key}
        if args.layout == "s3":
            from .s3 import EDGE_ACCESS_KEY, EDGE_SECRET_KEY

            # The edge's static test credentials, never real ones; the fake
            # aws_signing_helper serve is on the same port, under /latest.
            del info["key"]
            info.update(
                imds_url=url,
                region=args.region,
                access_key_id=EDGE_ACCESS_KEY,
                secret_access_key=EDGE_SECRET_KEY,
            )
        print(json.dumps(info), flush=True)
        await task

    asyncio.run(serve())


if __name__ == "__main__":
    main()
