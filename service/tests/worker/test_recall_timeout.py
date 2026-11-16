"""A `downloading` recall whose edge stopped working on it fails
(worker/recall_timeout.py): on heartbeat and fill evidence, never on how
long the fill has taken, and never while the service itself could not hear
the edge."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg

from omnifile_service.backends.sim import SimBackend
from omnifile_service.worker.recall_timeout import (
    NO_FILL_PREFIX,
    SILENT_PREFIX,
    EdgeTimeoutConfig,
    candidates,
    fail_one,
    fail_unreachable,
)

from .conftest import run
from .support import World, queue, seed, store

T0 = datetime(2026, 11, 5, tzinfo=UTC)
CFG = EdgeTimeoutConfig(silent=timedelta(minutes=60), no_fill=timedelta(minutes=60))


def _downloading(
    db: psycopg.Connection, w: World, sim: SimBackend, name: str, ago: str, size: int = 1024
) -> int:
    """A file of the world's dataset (`size` bytes in the catalog), and a
    recall of its content that reached `downloading` `ago` (an interval)
    ago. Other tests' recalls share the database, so each test checks its
    own."""
    f = run(store(db, w, sim, name, b"x" * 1024, T0, catalog_size=size))
    (rid,) = db.execute(
        "INSERT INTO catalog.recall (file_id, version_id, edge_id, state, requested_at)"
        " VALUES (%s, %s, %s, 'downloading', now() - %s::interval) RETURNING recall_id",
        (f.file_id, f.version_id, w.edge_id, ago),
    ).fetchone()
    return rid


def _edge(
    db: psycopg.Connection,
    w: World,
    *,
    heard: str | None,
    fills: int | None,
    fill_ago: str | None,
    feed_lag: int | None = None,
) -> None:
    """The edge's last heartbeat `heard` ago (None: never), the gauge it sent
    (None: it does not report fills), its last fill `fill_ago` ago and the
    feed lag its last heartbeat reported."""
    health: dict = {} if fills is None else {"fills_running": fills}
    if feed_lag is not None:
        health["feed_lag_events"] = feed_lag
    db.execute(
        "UPDATE catalog.edge SET"
        " last_heartbeat_at = now() - %s::interval, last_seen_at = now() - %s::interval,"
        " health = %s, last_fill_at = now() - %s::interval WHERE edge_id = %s",
        (heard, heard, psycopg.types.json.Jsonb(health), fill_ago, w.edge_id),
    )


def _window(db: psycopg.Connection, started: str = "3 hours", last: str = "20 seconds") -> None:
    """The service's reachable window (migration 0040): heartbeats arriving
    since `started` ago, the latest `last` ago."""
    db.execute(
        "INSERT INTO catalog.heartbeat_window (id, started_at, last_at)"
        " VALUES (true, now() - %s::interval, now() - %s::interval)"
        " ON CONFLICT (id) DO UPDATE SET started_at = EXCLUDED.started_at,"
        " last_at = EXCLUDED.last_at",
        (started, last),
    )


def _state(db: psycopg.Connection, rid: int) -> tuple[str, str | None]:
    return db.execute(
        "SELECT state, failure FROM catalog.recall WHERE recall_id = %s", (rid,)
    ).fetchone()


def _why(db: psycopg.Connection, rid: int) -> list[str]:
    return [
        r[0]
        for r in db.execute(
            "SELECT details->>'why' FROM catalog.audit_log WHERE action = 'recall.edge_timeout'"
            " AND details->>'recall_id' = %s",
            (str(rid),),
        )
    ]


def _sweep(connect, config: EdgeTimeoutConfig = CFG) -> int:
    async def go() -> int:
        conn = await connect()
        try:
            return await fail_unreachable(conn, T0, config)
        finally:
            await conn.close()

    return run(go())


def test_a_recall_whose_edge_went_silent_fails_and_users_are_told(db, connect, sim) -> None:
    w = seed(db)
    _window(db)
    rid = _downloading(db, w, sim, "gone.segy", "3 hours")
    _edge(db, w, heard="2 hours", fills=1, fill_ago="2 hours")
    _sweep(connect)
    state, failure = _state(db, rid)
    assert state == "failed"
    assert failure.startswith(SILENT_PREFIX) and "not been heard from" in failure
    assert {"recall_id": str(rid)} in queue(db, "notify")
    assert _why(db, rid) == ["silent"]
    # Done once: a second sweep leaves it alone.
    _sweep(connect)
    assert _why(db, rid) == ["silent"]


def test_silence_counts_only_from_downloading(db, connect, sim) -> None:
    """#55 review: the silent rule's grace. A recall 10 minutes into
    `downloading`, on an edge silent for 2 hours, stays `downloading`."""
    w = seed(db)
    _window(db)
    rid = _downloading(db, w, sim, "late.segy", "10 minutes")
    _edge(db, w, heard="2 hours", fills=1, fill_ago="2 hours")
    _sweep(connect)
    assert _state(db, rid) == ("downloading", None)


def test_no_heartbeats_arriving_means_nothing_fails_for_silence(db, connect, sim) -> None:
    """#55 review: a live api the edges cannot reach (its ingress, allow-list
    or certificates), or one that lost the catalog, receives no heartbeats:
    the reachable window is closed and nothing fails for silence or
    idleness, however long it lasts."""
    w = seed(db)
    rid = _downloading(db, w, sim, "unreachable.segy", "3 hours")
    _edge(db, w, heard="2 hours", fills=0, fill_ago="3 hours")
    _window(db, started="1 day", last="2 hours")
    _sweep(connect)
    assert _state(db, rid) == ("downloading", None)


def test_after_an_outage_every_edge_gets_the_whole_limit_again(db, connect, sim) -> None:
    """#55 review: an outage of over an hour ends and the first edge
    heartbeats, opening a new window. Another edge, silent since before the
    outage, is not failed before its own next heartbeat could come: silence
    counts from the window's start. Once the window has lasted the whole
    limit, the edge that never came back is failed."""
    w = seed(db)
    rid = _downloading(db, w, sim, "after.segy", "3 hours")
    _edge(db, w, heard="2 hours", fills=0, fill_ago="3 hours")
    _window(db, started="30 seconds", last="10 seconds")
    _sweep(connect)
    assert _state(db, rid) == ("downloading", None)
    _window(db, started="59 minutes", last="10 seconds")
    _sweep(connect)
    assert _state(db, rid) == ("downloading", None)
    _window(db, started="61 minutes", last="10 seconds")
    _sweep(connect)
    assert _state(db, rid)[0] == "failed"
    assert _why(db, rid) == ["silent"]


def test_an_edge_that_heartbeats_but_runs_no_fill_fails_its_recalls(db, connect, sim) -> None:
    w = seed(db)
    _window(db)
    rid = _downloading(db, w, sim, "idle.segy", "3 hours")
    _edge(db, w, heard="30 seconds", fills=0, fill_ago="2 hours")
    _sweep(connect)
    state, failure = _state(db, rid)
    assert state == "failed"
    assert failure.startswith(NO_FILL_PREFIX) and "no fill running" in failure
    assert _why(db, rid) == ["no_fill"]


def test_a_fill_in_resume_backoff_after_an_outage_is_not_idle(db, connect, sim) -> None:
    """#55 re-review: idleness counts from the window's start too. After an
    outage the edge's first heartbeat back reports no fill running (its fill
    is in resume backoff) while its last fill is stamped from before the
    outage: its recall stays `downloading`."""
    w = seed(db)
    rid = _downloading(db, w, sim, "backoff.segy", "3 hours")
    _window(db, started="30 seconds", last="10 seconds")
    _edge(db, w, heard="10 seconds", fills=0, fill_ago="2 hours")
    _sweep(connect)
    assert _state(db, rid) == ("downloading", None)


def test_an_edge_catching_up_on_its_feed_is_not_idle(db, connect, sim) -> None:
    """#55 review: an edge still reading its feed may not have seen the
    recall reach `downloading`; its idleness is not held against it."""
    w = seed(db)
    _window(db)
    rid = _downloading(db, w, sim, "lag.segy", "3 hours")
    _edge(db, w, heard="30 seconds", fills=0, fill_ago="2 hours", feed_lag=5000)
    _sweep(connect)
    assert _state(db, rid) == ("downloading", None)


def test_a_slow_fill_that_is_alive_is_never_timed_out(db, connect, sim) -> None:
    """A 2 TB file at 10 GiB/h is in `downloading` for days. While its edge
    heartbeats with a fill running, it stays there, however long."""
    w = seed(db)
    _window(db)
    rid = _downloading(db, w, sim, "huge.segy", "9 days", size=2 * 10**12)
    _edge(db, w, heard="40 seconds", fills=1, fill_ago="40 seconds")
    for _ in range(3):
        _sweep(connect)
        assert _state(db, rid) == ("downloading", None)
    # An edge that does not report its fills is judged on heartbeats alone.
    _edge(db, w, heard="40 seconds", fills=None, fill_ago=None)
    _sweep(connect)
    assert _state(db, rid) == ("downloading", None)


def test_only_a_recall_of_the_current_content_is_judged(db, connect, sim) -> None:
    """#55 review: a recall of a version the file has moved past (or one a
    version restore made) is not one an edge fills: never failed here."""
    w = seed(db)
    _window(db)
    rid = _downloading(db, w, sim, "moved.segy", "3 hours")
    newer = run(store(db, w, sim, "moved-newer.segy", b"y" * 10, T0))
    db.execute(
        "UPDATE catalog.recall SET version_id = %s WHERE recall_id = %s",
        (newer.version_id, rid),
    )
    _edge(db, w, heard="2 hours", fills=0, fill_ago="2 hours")
    _sweep(connect)
    assert _state(db, rid) == ("downloading", None)


def test_a_filled_report_racing_the_sweep_wins(db, connect, sim) -> None:
    """#55 review: the edge's `filled` lands between the sweep's query and
    its update. The recall stays `available`: no failure, no card, no
    audit row."""
    w = seed(db)
    _window(db)
    rid = _downloading(db, w, sim, "race.segy", "3 hours")
    _edge(db, w, heard="2 hours", fills=0, fill_ago="2 hours")
    before = len(queue(db, "notify"))

    async def go() -> bool:
        conn = await connect()
        try:
            rows = [r for r in await candidates(conn, CFG) if r["recall_id"] == rid]
            assert rows, "a candidate"
            # What `filled` does, in between.
            db.execute(
                "UPDATE catalog.recall SET state = 'available', completed_at = now()"
                " WHERE recall_id = %s",
                (rid,),
            )
            return await fail_one(conn, rows[0], CFG)
        finally:
            await conn.close()

    assert run(go()) is False
    assert _state(db, rid) == ("available", None)
    assert len(queue(db, "notify")) == before
    assert _why(db, rid) == []


def test_other_states_are_never_touched_and_the_limits_can_be_off(db, connect, sim) -> None:
    w = seed(db)
    _window(db)
    rid = _downloading(db, w, sim, "off.segy", "3 hours")
    _edge(db, w, heard="2 hours", fills=0, fill_ago="2 hours")
    assert _sweep(connect, EdgeTimeoutConfig(silent=None, no_fill=None)) == 0
    assert _state(db, rid) == ("downloading", None)
    db.execute("UPDATE catalog.recall SET state = 'rehydrating' WHERE recall_id = %s", (rid,))
    _sweep(connect)
    assert _state(db, rid)[0] == "rehydrating"


def test_config_from_the_environment() -> None:
    assert EdgeTimeoutConfig.from_env({}) == EdgeTimeoutConfig()
    c = EdgeTimeoutConfig.from_env(
        {"OMNIFILE_RECALL_EDGE_SILENT_MINUTES": "0", "OMNIFILE_RECALL_NO_FILL_MINUTES": "90"}
    )
    assert c == EdgeTimeoutConfig(silent=None, no_fill=timedelta(minutes=90))
