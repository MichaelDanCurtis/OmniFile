"""Whether something teardown found by name is the run's to delete.

Two runs can share a namePrefix (it is 8 characters: WP1's limit), and Entra objects found by
a name filter can be anyone's. So a name match alone deletes nothing. An object is the run's
when it carries the run's tag, or the run recorded its id, or, untagged, it was made inside
the run's own time window (from its plan to now). Anything carrying another run's tag is
never the run's, whatever its name. An expired-run sweep (--expired) has no window for the
runs it sweeps, so for those only the tag counts.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .core import TAG_RUN, Context, current_time, parse_iso

#: Clock skew between this machine and Entra's createdDateTime.
SLACK = timedelta(minutes=10)


def runs_in_tags(tags: list[str] | None) -> set[str]:
    """Run ids an Entra application's tags name (`omnifile-run:<id>`)."""
    return {t.split(":", 1)[1] for t in tags or [] if t.startswith(f"{TAG_RUN}:")}


def runs_in_text(text: str | None) -> set[str]:
    """Run ids a group's description names (`omnifile-run=<id>`): groups take no tags."""
    return set(re.findall(rf"{re.escape(TAG_RUN)}=(\S+)", text or ""))


def window_end(ctx: Context) -> datetime:
    return current_time(ctx.dry_run, ctx.now)


def why_not_ours(
    ctx: Context,
    rid: str,
    *,
    runs: set[str],
    created: str | None,
    recorded: bool = False,
) -> str | None:
    """None when the object is run RID's; otherwise why teardown leaves it alone."""
    others = runs - {rid}
    if others:
        return f"it carries another run's tag ({', '.join(sorted(others))})"
    if rid in runs or recorded:
        return None
    if rid != ctx.run_id:
        return "it is untagged, and an expired run's objects are found by tag only"
    started = ctx.state.get("started")
    if not started or not created:
        return "it is untagged, and when it was made can't be checked against the run"
    try:
        made = parse_iso(created)
        lo = parse_iso(started) - SLACK
    except ValueError:
        return f"it is untagged, and its creation time {created!r} can't be read"
    if lo <= made <= window_end(ctx) + SLACK:
        return None
    return f"it is untagged and was made at {created}, outside this run (from {started})"
