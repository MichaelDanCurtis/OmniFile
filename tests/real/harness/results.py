"""Running the registered checks of one cloud and keeping their results (OUT/results.json)."""

from __future__ import annotations

import json
import traceback
from typing import Any

from .checks import CheckFailed, Partial, Pending, load_all
from .core import REAL, Context, iso


def _save(ctx: Context, results: dict[str, Any]) -> None:
    (ctx.out / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")


def load(ctx: Context) -> dict[str, Any]:
    f = ctx.out / "results.json"
    return json.loads(f.read_text()) if f.exists() else {}


def record(ctx: Context, cid: str, status: str, detail: str, evidence: dict | None = None) -> None:
    results = load(ctx)
    ev = ctx.evidence / f"{cid}.json"
    ev.write_text(
        json.dumps(
            {
                "id": cid,
                "run": ctx.run_id,
                "status": status,
                "detail": detail,
                "evidence": evidence or {},
                "dry_run": ctx.dry_run,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n"
    )
    results[cid] = {
        "status": status,
        "detail": detail,
        "evidence": f"evidence/{cid}.json",
        "at": iso(ctx.now),
        "run": ctx.run_id,
        "dry_run": ctx.dry_run,
    }
    _save(ctx, results)


def run_checks(ctx: Context, env: Any, only: set[str] | None = None) -> dict[str, int]:
    """Every check of ctx.cloud: automated ones run, exit ones take their exit step's result,
    manual ones are listed (OUT/manual-steps.txt) for the owner."""
    reg = load_all()
    counts = {"pass": 0, "fail": 0, "partial": 0, "manual": 0, "pending": 0}
    manual_lines: list[str] = []
    exits = ctx.state.get("exit", {})
    for cid in sorted(reg):
        c = reg[cid]
        if c.cloud != ctx.cloud or (only and cid not in only):
            continue
        if c.kind == "manual":
            record(ctx, cid, "manual", f"manual step ({c.needs})", {"steps": c.steps})
            manual_lines.append(f"{cid} {c.title} (needs: {c.needs})")
            manual_lines += [f"    {i}. {s}" for i, s in enumerate(c.steps, 1)]
            counts["manual"] += 1
            continue
        if c.kind == "exit":
            got = _exit_of(ctx, c.exit_step, exits)
            if got is None:
                record(ctx, cid, "pending", f"exit step {c.exit_step} did not run")
                counts["pending"] += 1
            else:
                record(
                    ctx,
                    cid,
                    got["status"],
                    f"exit step {c.exit_step}: {got['detail']}",
                    {"exit_step": c.exit_step, "evidence": got.get("evidence")},
                )
                counts[got["status"]] += 1
            continue
        skip = env.not_in_scope() if hasattr(env, "not_in_scope") else {}
        if cid in skip:
            record(ctx, cid, "pending", f"not in the cheap scope: {skip[cid]}")
            counts["pending"] += 1
            continue
        with ctx.step(f"check.{cid}"):
            try:
                ev = c.fn(env) or {}
                record(ctx, cid, "pass", c.title, ev)
                counts["pass"] += 1
            except Partial as e:
                record(ctx, cid, "partial", f"left: {e}", e.evidence)
                counts["partial"] += 1
            except Pending as e:
                record(ctx, cid, "pending", str(e))
                counts["pending"] += 1
            except CheckFailed as e:
                record(ctx, cid, "fail", str(e))
                counts["fail"] += 1
            except Exception as e:  # a broken check is a failed check, with its traceback
                record(
                    ctx,
                    cid,
                    "fail",
                    f"{type(e).__name__}: {e}",
                    {"traceback": traceback.format_exc()},
                )
                counts["fail"] += 1
    if manual_lines:
        (ctx.out / f"manual-steps-{ctx.cloud}.txt").write_text(
            f"Manual checks for run {ctx.run_id} ({ctx.cloud}). Record each with\n"
            f"  tests/real/collect.py --out {ctx.out} --record ID=pass|fail --note '...' "
            f"[--evidence FILE]\n\n" + "\n".join(manual_lines) + "\n"
        )
    ctx.log("checks: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return counts


def _exit_of(ctx: Context, step: str, exits: dict[str, Any]) -> dict[str, Any] | None:
    """A step of this run's exit, or `azure:<step>` of the Azure run this run names."""
    if not step.startswith("azure:"):
        return exits.get(step)
    if not ctx.azure_run:
        return None
    f = REAL / "out" / ctx.azure_run / "state.json"
    if not f.exists():
        return None
    st = json.loads(f.read_text())
    # A dry Azure run's exit proves nothing for a real tenant run, or the reverse; nor does
    # one that never came up.
    if st.get("dry_run") != ctx.dry_run or not st.get("up"):
        return None
    return st.get("exit", {}).get(step.split(":", 1)[1])


def exit_result(ctx: Context, step: str, status: str, detail: str, evidence: Any = None) -> None:
    ctx.state.setdefault("exit", {})[step] = {
        "status": status,
        "detail": detail,
        "evidence": evidence,
    }
    ctx.save_state()
