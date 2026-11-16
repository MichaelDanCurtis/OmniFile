"""Staged migration plans (portal-api.md section 5.22; migration 0037).

A plan splits a dataset's migration into waves. `plan_sweep` runs every
minute in the worker and, for each wave:

- **starts** it, for an approved plan only (one `request_location` per
  prefix, all in one transaction), once its start time has come, the time is
  inside its daily window and outside its blackouts, the wave it depends on
  is verified, and the sources its prefixes lie in are catalogued. Until then
  the wave is `waiting`, with the reason. If the wave it depends on was
  cancelled, it is `blocked` and never starts. The migrations belong to the
  plan (`requested_by` system:plans): their cutover, cancel and retry go
  through the plan's routes, never the app that proposed the plan;
- **follows** it, for approved and cancelled plans alike (cancelling a plan
  stops only the waves not started): `verified` once every migration not
  cancelled is, and at least one is; `cut_over` likewise; `failed` while one
  is failed, and back to `started` once none is; `cancelled` if all were;
- **cuts it over**: a wave's migrations always hold at `verified`
  (`cutover = manual`), and for an `auto` wave the sweep asks for each
  cutover itself, only for a migration that is verified (decision 24: the
  engine checks again and flips nothing unverified), inside the window and
  outside blackouts. A migration that leaves `verified` drops the request
  (the engine clears it). A `manual` wave waits for an ArchiveAdmin.

What the window and blackouts gate is listed in migration 0037: starting,
cutting over, and the engine's new runs and restore waves for the wave
(migrate/windows.py `wave_gate`). A run or restore already under way isn't
paused. Bandwidth and egress figures are advisory.

Everything a sweep does to a wave happens in the wave's own transaction,
under a row lock, so a crash leaves each wave as it was or as it became:
the next sweep picks up where the last one stopped.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from ..worker.queue import send
from .engine import QUEUE, MigrationError, request_location
from .windows import PLAN_ACTOR, in_blackout, in_window, next_open, open_now

log = logging.getLogger("omnifile_service.migrate")

ACTOR = PLAN_ACTOR
#: The copier choice of a wave -> the engine's `copy_by`.
COPY_BY = {"auto": "omnifile", "external": "external"}
#: Waves the sweep looks at.
LIVE = ("planned", "waiting", "blocked", "started", "verified", "failed")


async def _blocked(
    conn: psycopg.AsyncConnection, w: Mapping[str, Any], now: datetime
) -> tuple[str, str] | None:
    """Why a planned wave can't start now, as (state, reason), or None."""
    if w["after_seq"] is not None:
        cur = await conn.execute(
            "SELECT state FROM catalog.migration_wave WHERE plan_id = %s AND seq = %s",
            (w["plan_id"], w["after_seq"]),
        )
        dep = await cur.fetchone()
        if dep is None or dep[0] == "cancelled":
            return ("blocked", f"wave {w['after_seq']} was cancelled: this wave doesn't start")
        if dep[0] == "blocked":
            return ("blocked", f"wave {w['after_seq']} is blocked: this wave doesn't start")
        if dep[0] not in ("verified", "cut_over"):
            return ("waiting", f"waiting for wave {w['after_seq']} to be verified")
    if now < w["start_at"]:
        return ("waiting", "its start time hasn't come")
    if in_blackout(w["blackouts"], now):
        return ("waiting", "inside a blackout")
    if not in_window(w["window_start_min"], w["window_end_min"], now):
        return ("waiting", "outside its daily window")
    for prefix in w["prefixes"]:
        cur = await conn.execute(
            "SELECT state FROM catalog.source s JOIN catalog.dataset d USING (dataset_id)"
            " WHERE s.dataset_id = %s AND s.kind = 'source' AND s.backend_id = d.source_backend_id"
            "   AND starts_with(%s, s.prefix) ORDER BY length(s.prefix) DESC LIMIT 1",
            (w["dataset_id"], prefix),
        )
        src = await cur.fetchone()
        if src is None:
            return ("waiting", f"{prefix!r} is not inside a source of the dataset")
        if src[0] != "seeded":
            return ("waiting", f"the source of {prefix!r} is still being catalogued ({src[0]})")
    return None


async def _start(conn: psycopg.AsyncConnection, w: Mapping[str, Any], now: datetime) -> None:
    to_backend = await (
        await conn.execute(
            "SELECT backend_id FROM catalog.dataset WHERE dataset_id = %s", (w["dataset_id"],)
        )  # fmt: skip
    ).fetchone()
    assert to_backend is not None
    for prefix in w["prefixes"]:
        mid = await request_location(
            conn, dataset_id=w["dataset_id"], prefix=prefix, to_backend_id=int(to_backend[0]),
            copy_by=COPY_BY[w["copier"]],
            # The plan's, not its proposer's: an API client that proposed the
            # plan can't cut over, cancel or retry the wave's migrations.
            requested_by=ACTOR,
            # Held at verified: the sweep asks for the cutover (decision 24).
            cutover="manual",
        )  # fmt: skip
        await conn.execute(
            "INSERT INTO catalog.migration_wave_prefix (wave_id, prefix, migration_id)"
            " VALUES (%s, %s, %s)",
            (w["wave_id"], prefix, mid),
        )
    await conn.execute(
        "UPDATE catalog.migration_wave SET state = 'started', started_at = %s,"
        " waiting_reason = NULL, updated_at = %s WHERE wave_id = %s",
        (now, now, w["wave_id"]),
    )


async def _follow(conn: psycopg.AsyncConnection, w: Mapping[str, Any], now: datetime) -> None:
    cur = conn.cursor(row_factory=dict_row)
    await cur.execute(
        "SELECT m.migration_id, m.state, m.cutover_requested_at"
        "  FROM catalog.migration_wave_prefix p"
        "  JOIN catalog.migration_prefix m USING (migration_id)"
        " WHERE p.wave_id = %s FOR UPDATE OF m",
        (w["wave_id"],),
    )
    ms = await cur.fetchall()
    if not ms:
        return
    states = {m["state"] for m in ms}
    live = states - {"cancelled"}
    if not live:
        # Nothing of the wave goes on: its dependents are blocked.
        await _wave(conn, w, now, state="cancelled",
                    waiting_reason="every migration of the wave was cancelled")  # fmt: skip
        return
    if "failed" in live:
        if w["state"] != "failed":
            await _wave(conn, w, now, state="failed",
                        waiting_reason="a migration of the wave failed")  # fmt: skip
        return
    if w["state"] == "failed":
        # Retried: none is failed any more.
        await _wave(conn, w, now, state="started", waiting_reason=None)
    if live == {"cut_over"}:
        await _wave(conn, w, now, state="cut_over", cut_over_at=now, waiting_reason=None)
        return
    if live <= {"verified", "cut_over"}:
        if w["state"] in ("started", "failed"):
            await _wave(conn, w, now, state="verified", verified_at=now, waiting_reason=None)
        if w["cutover"] != "auto":
            return
        if not open_now(w, now):
            await _wave(conn, w, now, waiting_reason="cutover waits for its window")
            return
        for m in ms:
            # Only a verified migration is asked; the engine flips nothing
            # that isn't (it counts again first).
            if m["state"] == "verified" and m["cutover_requested_at"] is None:
                await conn.execute(
                    "UPDATE catalog.migration_prefix SET cutover_requested_at = %s,"
                    " cutover_requested_by = %s, updated_at = %s WHERE migration_id = %s",
                    (now, ACTOR, now, m["migration_id"]),
                )
                await send(conn, QUEUE, {"migration_id": m["migration_id"]})
        return
    if w["state"] == "verified":
        # A file changed in S3 sent a migration back to verifying.
        await _wave(conn, w, now, state="started", verified_at=None)


async def _wave(conn: psycopg.AsyncConnection, w: Mapping[str, Any], now: datetime,
                **cols: Any) -> None:  # fmt: skip
    sets = ", ".join(f"{k} = %({k})s" for k in cols)
    await conn.execute(
        f"UPDATE catalog.migration_wave SET {sets}, updated_at = %(now)s WHERE wave_id = %(w)s",
        {**cols, "now": now, "w": w["wave_id"]},
    )


async def plan_sweep(conn: psycopg.AsyncConnection, now: datetime) -> int:
    """One pass over the live waves of approved and cancelled plans; returns
    the waves it started. `conn` is in autocommit: each wave is its own
    transaction."""
    cur = await conn.execute(
        "SELECT w.wave_id FROM catalog.migration_wave w"
        "  JOIN catalog.migration_plan p USING (plan_id)"
        " WHERE p.state IN ('approved', 'cancelled') AND w.state = ANY(%s)"
        " ORDER BY w.plan_id, w.seq",
        (list(LIVE),),
    )
    ids = [r[0] for r in await cur.fetchall()]
    moved = 0
    for wave_id in ids:
        try:
            async with conn.transaction():
                await conn.execute("SELECT set_config('omnifile.actor', %s, true)", (ACTOR,))
                c = conn.cursor(row_factory=dict_row)
                await c.execute(
                    "SELECT w.*, p.dataset_id, p.proposed_by, p.state AS plan_state"
                    "  FROM catalog.migration_wave w JOIN catalog.migration_plan p USING (plan_id)"
                    " WHERE w.wave_id = %s FOR UPDATE OF w SKIP LOCKED",
                    (wave_id,),
                )
                w = await c.fetchone()
                if w is None:
                    continue
                if w["state"] in ("planned", "waiting", "blocked"):
                    if w["plan_state"] != "approved":
                        continue  # a cancelled plan starts nothing
                    why = await _blocked(conn, w, now)
                    if why is None:
                        await _start(conn, w, now)
                        moved += 1
                    elif (w["state"], w["waiting_reason"]) != why:
                        await _wave(conn, w, now, state=why[0], waiting_reason=why[1])
                else:
                    await _follow(conn, w, now)
        except MigrationError as e:
            # A prefix the engine refuses (overlap, a source gone): the wave
            # stops with the reason, and its other prefixes weren't started.
            async with conn.transaction():
                await conn.execute(
                    "UPDATE catalog.migration_wave SET state = 'failed', waiting_reason = %s,"
                    " updated_at = %s WHERE wave_id = %s",
                    (str(e)[:500], now, wave_id),
                )
            log.warning("wave %s refused: %s", wave_id, e)
    await _close_plans(conn, now)
    return moved


async def _close_plans(conn: psycopg.AsyncConnection, now: datetime) -> None:
    await conn.execute(
        "UPDATE catalog.migration_plan p SET state = 'done', updated_at = %s"
        " WHERE p.state = 'approved' AND NOT EXISTS ("
        "   SELECT 1 FROM catalog.migration_wave w"
        "    WHERE w.plan_id = p.plan_id AND w.state NOT IN ('cut_over', 'cancelled'))",
        (now,),
    )


__all__ = ["ACTOR", "in_blackout", "in_window", "next_open", "plan_sweep"]
