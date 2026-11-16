"""Running the guide's steps, each on the host its on= names, exactly as written."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import guide
from .azguard import GUARD_MARK, write_wrapper
from .core import Context, StepFailed


def run_steps(
    ctx: Context,
    steps: list[guide.Step],
    release: Path,
    handlers: dict[str, Callable[[guide.Step], str]],
    values: Callable[[], dict[str, str]],
    *,
    done_key: str = "guide_done",
    after: Callable[[guide.Step, str], None] | None = None,
    before: Callable[[guide.Step], str | None] | None = None,
) -> None:
    """Run STEPS in order. A workstation step runs here; any other on= goes to its handler
    (the test VM, the portal driver, the console). manual=yes, and hosts this run doesn't
    have (Hyper-V, vSphere, Windows), are recorded for the owner, not run. A step's output
    is kept, with the run's secrets masked, in OUT/guide/step-N.log. AFTER is called with
    each step that ran and its output (the gate check, recording what a step made)."""
    logs = ctx.out / "guide"
    logs.mkdir(parents=True, exist_ok=True)
    done = ctx.state.setdefault(done_key, [])
    skipped = ctx.state.setdefault("guide_manual", [])
    for s in steps:
        if s.n in done:
            continue
        with ctx.step(f"guide.{s.n}"):
            log = logs / f"step-{s.n}.log"
            if (
                s.manual
                or s.on in guide.OWNER_ONLY
                or (s.on not in handlers and s.on != "workstation")
            ):
                ctx.log(f"guide step {s.n} ({s.on}, {s.where}) is the owner's: recorded, not run")
                if s.manual and not ctx.dry_run:
                    # A person does this one (a copy and paste, a consent): wait for the owner.
                    print(f"\n--- guide step {s.n} ({s.where}), yours to do now:\n{s.body}")
                    input(f"Press Enter once step {s.n} is done (or not needed in this run): ")
                if not any(m["n"] == s.n for m in skipped):
                    skipped.append({"n": s.n, "on": s.on, "where": s.where})
                log.write_text(f"manual: {s.where}\n{s.body}")
                done.append(s.n)
                ctx.save_state()
                continue
            got = before(s) if before is not None else None
            if got == "skip":
                # BEFORE refuses a step by raising; "skip" leaves it out (a deploy whose gate
                # previewed nothing: its section doesn't apply); a dict is env for the step
                # (a deploy's one-shot grant for the az guard)
                log.write_text(f"not run: {s.where}\n")
                done.append(s.n)
                ctx.save_state()
                continue
            extra = got if isinstance(got, dict) else {}
            try:
                out = _with_wait(
                    ctx,
                    s,
                    lambda s=s, extra=extra: _workstation(ctx, s, release, values(), extra)
                    if s.on == "workstation"
                    else handlers[s.on](s),
                )
            finally:
                if after is not None:
                    # What a step made is recorded even when the step failed part way.
                    after(s, None)
            log.write_text(ctx.mask(out or ""))
            if after is not None:
                after(s, out or "")
            done.append(s.n)
            ctx.save_state()


def _with_wait(ctx: Context, s: guide.Step, fn: Callable[[], str]) -> str:
    """Run the step; with wait=N, rerun it every 60 s until it succeeds or N minutes pass."""
    if not s.wait_minutes:
        return fn()
    deadline = time.monotonic() + 60 * s.wait_minutes
    while True:
        try:
            return fn()
        except StepFailed:
            if ctx.dry_run or time.monotonic() > deadline:
                raise
            ctx.log(f"guide step {s.n} not ready yet; again in 60 s (wait={s.wait_minutes})")
            time.sleep(60)


def guard_dir(ctx: Context) -> Path:
    """Where the run's az guard wrapper is (harness/azguard.py, write_wrapper)."""
    return ctx.out / "guard"


def real_az(ctx: Context) -> str:
    """The real az, by absolute path: the one plan recorded, else the first on PATH that is
    not a guard."""
    got = ctx.state.get("real_az")
    if got:
        return got
    guard = guard_dir(ctx).resolve()
    for d in ctx.env.get("PATH", os.environ.get("PATH", "")).split(os.pathsep):
        cand = Path(d) / "az"
        if (
            d
            and cand.is_file()
            and os.access(cand, os.X_OK)
            and cand.resolve().parent != guard
            and GUARD_MARK not in cand.read_text(errors="replace")[:400]
        ):
            return str(cand)
    return ""


def guard_env(ctx: Context, extra: dict[str, str] | None = None) -> dict[str, str]:
    """What every guide step runs with: the run's az guard first on PATH, and no grant unless
    the harness gave this step one. The guard's own paths (the real az, the grants file, its
    log) are written into its wrapper, not handed to the step, and nothing in the step's
    environment moves them (a safety net against accidental deploys: azguard.py)."""
    d = guard_dir(ctx)
    real = real_az(ctx)
    if not real:
        raise StepFailed("no real az found to put behind the az guard")
    write_wrapper(
        d,
        python=sys.executable,
        real=real,
        grants=ctx.out / "deploy-grants.json",
        log=ctx.out / "az-guard.log",
    )
    return {
        "PATH": f"{d}{os.pathsep}{ctx.env.get('PATH', os.environ.get('PATH', ''))}",
        "OMNIFILE_DEPLOY_GRANT": "",
        "OMNIFILE_DEPLOY_APPROVED_DIGEST": "",
        **(extra or {}),
    }


def _workstation(
    ctx: Context,
    s: guide.Step,
    release: Path,
    values: dict[str, str],
    extra: dict[str, str] | None = None,
) -> str:
    if s.write_to:
        dest = release / s.write_to
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(guide.fill(s.body, values, ctx.secrets))
        return f"wrote {s.write_to}\n"
    script = ctx.out / "guide" / f"step-{s.n}.sh"
    script.write_text(s.body)  # exactly as written
    stdin = None
    if s.input_name:
        src = {**values, **ctx.secrets}
        clip = ctx.secrets.get("captured:__clipboard__")
        if s.input_name not in src and not clip:
            raise StepFailed(f"guide step {s.n} reads {s.input_name}, which the harness lacks")
        stdin = src.get(s.input_name, clip) + "\n"
    p = ctx.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", str(script)],
        cwd=release,
        check=False,
        input=stdin,
        env={
            **{k: v for k, v in values.items() if k.startswith(("OMNIFILE_", "OMNI_TEST_"))},
            **guard_env(ctx, extra),
        },
    )
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        (ctx.out / "guide" / f"step-{s.n}.log").write_text(ctx.mask(out))
        raise StepFailed(
            f"guide step {s.n} ({s.where}) exited {p.returncode}; see guide/step-{s.n}.log"
        )
    return out
