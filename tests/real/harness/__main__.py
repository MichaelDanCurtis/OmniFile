"""tests/real/run.sh PHASE --cloud azure|aws|tenant [options] (see run.sh for the options)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import allowlist, interfaces
from .core import (
    DRY_NOW,
    DRY_RUN_ID,
    REAL,
    REPO,
    AwaitingApproval,
    Context,
    HarnessError,
    Refused,
    UsageError,
    new_run_id,
    parse_iso,
    require_run_id,
)

PHASES = ("plan", "up", "exit", "checks", "collect", "down", "all")


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="tests/real/run.sh", description=__doc__)
    ap.add_argument("phase", choices=PHASES)
    ap.add_argument("--cloud", required=True, choices=("azure", "aws", "tenant"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--run-id")
    ap.add_argument("--approve", action="append", default=[])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--allow-list", type=Path)
    ap.add_argument("--guide", type=Path)
    ap.add_argument("--appliance", type=Path)
    ap.add_argument("--r3-dir", type=Path)
    ap.add_argument("--azure-run")
    ap.add_argument("--ttl-hours", type=int)
    ap.add_argument(
        "--scope",
        choices=("full", "cheap"),
        help="AWS: cheap leaves out the large-object checks (fixed at plan; default full)",
    )
    ap.add_argument("--inject-failure")
    ap.add_argument("--expired", action="store_true")
    return ap.parse_args(argv)


def check_out(out: Path) -> None:
    """OUT holds the run's state, its secrets file and, while a phase runs, a copy of the az
    sign-in: never somewhere git could commit it. Inside the repository only the gitignored
    tests/real/out is allowed."""
    repo, allowed = REPO.resolve(), (REAL / "out").resolve()
    if out.is_relative_to(repo) and not out.is_relative_to(allowed):
        raise Refused(
            f"--out {out} is inside the repository; use a directory outside it, or under "
            f"{allowed} (gitignored)"
        )


def _on_signal(signum, frame):  # noqa: ARG001
    """SIGTERM and SIGHUP end a phase the way Ctrl-C does, so its finally (which deletes the
    az sign-in copy and still tears down) runs."""
    raise KeyboardInterrupt(f"signal {signum}")


def make_context(a: argparse.Namespace) -> Context:
    if a.allow_list and not a.dry_run:
        raise Refused("--allow-list is for dry runs; a real run reads tests/real/allow-list.local")
    if "auto" in a.approve and not a.dry_run:
        raise Refused("--approve auto is for dry runs only")
    if a.dry_run and a.phase == "all" and not a.approve:
        # A dry run end to end approves the digests it computes itself (and logs them). `up`
        # on its own, dry or not, still stops at the gate without --approve.
        a.approve = ["auto"]
    if a.dry_run:
        now = parse_iso(os.environ.get("REAL_NOW", DRY_NOW))
    else:
        now = datetime.now(UTC).replace(microsecond=0)
    if a.azure_run:
        require_run_id(a.azure_run)
    if a.run_id is not None:
        run_id = require_run_id(a.run_id)
    elif a.dry_run:
        run_id = DRY_RUN_ID
    elif a.phase in ("plan", "all"):
        run_id = new_run_id(now)
    else:
        raise UsageError(f"{a.phase} needs --run-id (plan prints it)")
    out = (a.out or REAL / "out" / run_id).resolve()
    check_out(out)
    ctx = Context(
        phase=a.phase,
        cloud=a.cloud,
        dry_run=a.dry_run,
        out=out,
        run_id=run_id,
        now=now,
        approve=a.approve[0] if a.approve else None,
        approvals=list(a.approve),
        guide=a.guide.resolve() if a.guide else None,
        appliance=a.appliance,
        azure_run=a.azure_run,
        ttl_hours=a.ttl_hours if a.ttl_hours is not None else 12,
        ttl_given=a.ttl_hours is not None,
        scope=a.scope,
        inject=a.inject_failure,
        expired=a.expired,
    )
    if a.allow_list:
        ctx.allow_list_path = a.allow_list.resolve()
    ctx.out.mkdir(parents=True, exist_ok=True)
    ctx.load_state()
    ctx.r3_dir = a.r3_dir  # type: ignore[attr-defined]
    return ctx


def cloud_for(ctx: Context, al, moto_url):
    if ctx.cloud == "azure":
        from .azure import Azure

        return Azure(ctx, al)
    if ctx.cloud == "aws":
        from .aws import AWS

        return AWS(ctx, al, moto_url)
    from .tenant import Tenant

    return Tenant(ctx, al)


def start_moto(ctx: Context):
    import logging
    import socket

    # AWS managed policies (AmazonSSMManagedInstanceCore for the probe) exist in moto only
    # when asked for; the server's request log stays out of the harness's output.
    os.environ["MOTO_IAM_LOAD_MANAGED_POLICIES"] = "true"
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    from moto.server import ThreadedMotoServer

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    url = f"http://127.0.0.1:{port}"
    # The owner's one-time step (tests/real/aws/harness-roles.yaml), outside the transcript.
    from fakes import owner_stack

    region = allowlist.load(ctx.allow_list_path, dry_run=True).one("aws-region")
    alt = os.environ.get("REAL_FAKE_OWNER_STACK")  # a test's variant of the template
    owner_stack.deploy(url, region, *([Path(alt)] if alt else []))
    return server, url


def main(argv: list[str]) -> int:
    try:
        a = parse(argv)
    except SystemExit as e:
        return int(e.code or 0) if e.code in (0, None) else 2
    try:
        ctx = make_context(a)
    except HarnessError as e:
        print(f"run.sh: {e}", file=sys.stderr)
        return e.code
    moto = None
    moto_url = None
    code = 0
    import signal

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _on_signal)
    try:
        if ctx.dry_run and ctx.cloud == "aws":
            moto, moto_url = start_moto(ctx)
        ctx.build_env(moto_url)
        if ctx.cloud == "azure" and interfaces.missing():
            raise Refused(
                "WP1's scripts are missing from this branch: " + ", ".join(interfaces.missing())
            )
        al = allowlist.load(ctx.allow_list_path, dry_run=ctx.dry_run)
        cloud = cloud_for(ctx, al, moto_url)
        code = run(ctx, cloud, a.phase)
    except HarnessError as e:
        ctx.log(f"stopped: {e}")
        code = e.code
    except Exception as e:  # a single phase that broke: say so, keep the traceback
        import traceback

        (ctx.out / "error.txt").write_text(traceback.format_exc())
        ctx.log(f"stopped: {type(e).__name__}: {e} (traceback in error.txt)")
        code = 1
    except KeyboardInterrupt as e:
        ctx.log(f"stopped: interrupted ({e or 'Ctrl-C'})")
        code = 130
    finally:
        from . import azpin

        azpin.unpin(ctx)  # the copy of the sign-in never outlives the phase
        if moto is not None:
            moto.stop()
    summary = {
        "run": ctx.run_id,
        "cloud": ctx.cloud,
        "phase": a.phase,
        "dry_run": ctx.dry_run,
        "exit_code": code,
        "steps": ctx.steps_seen,
    }
    (ctx.out / f"summary-{a.phase}.json").write_text(json.dumps(summary, indent=2) + "\n")
    return code


def run(ctx: Context, cloud, phase: str) -> int:
    from .collect import collect

    if phase != "all":
        ctx.phase = phase
        if phase == "collect":
            return collect(ctx)
        if phase == "up" and ctx.state.get("down"):
            # Its resources are gone: a resume (or any up) would act on nothing that exists.
            raise UsageError(
                f"run {ctx.run_id} was torn down; plan it again (tests/real/run.sh plan "
                f"--cloud {ctx.cloud} --run-id {ctx.run_id}) before up"
            )
        if phase == "up" and ctx.cloud == "azure" and getattr(ctx, "r3_dir", None):
            ctx.state["r3_dir"] = str(ctx.r3_dir)
        got = getattr(cloud, phase)()
        if phase == "plan":
            ctx.state.pop("down", None)
            ctx.save_state()
        if phase == "checks" and got and got.get("fail"):
            return 1
        return 0
    code = 0
    made = False
    paused = False
    ctx.driver = "all"  # type: ignore[attr-defined]  # the phase a resume command names
    try:
        ctx.phase = "plan"
        reuse = ctx.state.get("digest") and ctx.approve and ctx.approve != "auto"
        if reuse and not ctx.state.get("down"):
            # `plan` already ran for this run id and the owner approved its digest: planning
            # again could change it (and, for Azure, means nothing new: the same group).
            ctx.log(f"using run {ctx.run_id}'s plan (digest {ctx.state['digest']})")
        else:
            cloud.plan()
        made = True
        ctx.state.pop("down", None)
        ctx.save_state()
        ctx.phase = "up"
        try:
            cloud.up()
        except AwaitingApproval:
            if ctx.state.get("paused_at"):
                # A later deploy's own what-if, not approved yet: the earlier deploys are
                # done and stay up, so the owner can read it and resume. A teardown here
                # would make the resume it prints act on nothing.
                paused = True
            raise
        for ph in ("exit", "checks"):
            ctx.phase = ph
            try:
                got = getattr(cloud, ph)()
                if ph == "checks" and got and got.get("fail"):
                    code = max(code, 1)
            except AwaitingApproval:
                raise
            except Refused:
                raise
            except HarnessError as e:
                ctx.log(f"{ph} failed: {e}")
                code = max(code, e.code)
        ctx.phase = "collect"
        code = max(code, collect(ctx))
    except Refused:
        raise  # nothing was touched: no teardown either
    except HarnessError as e:
        ctx.log(f"stopped: {e}")
        code = e.code
        if paused:
            ctx.log(
                f"run {ctx.run_id} stays up, paused at guide step {ctx.state['paused_at']} "
                f"(expires {ctx.expires}); resume with the command above, or tear it down "
                f"with tests/real/run.sh down --cloud {ctx.cloud} --run-id {ctx.run_id}"
            )
    except Exception as e:  # a cloud API error or a bug: still tear down, then report it
        import traceback

        (ctx.out / "error.txt").write_text(traceback.format_exc())
        ctx.log(f"stopped: {type(e).__name__}: {e} (traceback in error.txt)")
        code = 1
    finally:
        if not paused and (made or ctx.state.get("created") or ctx.state.get("made")):
            ctx.phase = "down"
            ctx.inject = None if ctx.inject and not ctx.inject.startswith("down") else ctx.inject
            try:
                cloud.down()
            except Exception as e:
                ctx.log(f"teardown failed: {type(e).__name__}: {e}")
                code = max(code, 1)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
