"""A plan wave's daily window and blackouts (migration 0037), shared by the
plan sweep (starting and cutting over a wave) and the engine (new Storage
Mover runs and restore waves for a wave's migrations)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

#: The audit actor of everything a plan does, and the owner of its migrations.
PLAN_ACTOR = "system:plans"


def parse(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def in_window(start_min: int | None, end_min: int | None, at: datetime) -> bool:
    """Is `at` (UTC) inside the daily window? No window: always."""
    if start_min is None or end_min is None:
        return True
    minute = at.hour * 60 + at.minute
    if start_min <= end_min:
        return start_min <= minute < end_min
    return minute >= start_min or minute < end_min  # wraps past midnight


def in_blackout(blackouts: Sequence[Mapping[str, Any]], at: datetime) -> bool:
    return any(parse(b["from"]) <= at < parse(b["to"]) for b in blackouts)


def open_now(w: Mapping[str, Any], at: datetime) -> bool:
    return in_window(w["window_start_min"], w["window_end_min"], at) and not in_blackout(
        w["blackouts"], at
    )


def next_open(
    start_min: int | None,
    end_min: int | None,
    blackouts: Sequence[Mapping[str, Any]],
    at: datetime,
    horizon: timedelta = timedelta(days=60),
) -> datetime | None:
    """The first minute from `at` inside the window and outside every
    blackout, within `horizon`; None if there is none (a plan check)."""
    t = at.replace(second=0, microsecond=0)
    if t < at:
        t += timedelta(minutes=1)
    end = at + horizon
    while t < end:
        if in_blackout(blackouts, t):
            t = max(parse(b["to"]) for b in blackouts if parse(b["from"]) <= t < parse(b["to"]))
            continue
        if in_window(start_min, end_min, t):
            return t
        t += timedelta(minutes=1)
    return None


async def wave_gate(conn: psycopg.AsyncConnection, migration_id: int, at: datetime) -> str | None:
    """Why the engine may not start a new run or restore wave for this
    migration now: it belongs to a plan wave that is outside its window or
    inside a blackout. None: not gated (or not a plan's)."""
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT w.window_start_min, w.window_end_min, w.blackouts"
        "  FROM catalog.migration_wave_prefix x JOIN catalog.migration_wave w USING (wave_id)"
        " WHERE x.migration_id = %s",
        (migration_id,),
    )
    w = await cur.fetchone()
    if w is None or open_now(w, at):
        return None
    return "outside the plan wave's window or inside a blackout: new runs and restores wait"


__all__ = ["PLAN_ACTOR", "in_blackout", "in_window", "next_open", "open_now", "parse", "wave_gate"]
