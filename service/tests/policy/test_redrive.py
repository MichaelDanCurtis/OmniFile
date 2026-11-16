"""What a dead letter would lose (worker/redrive.py): a recall ends where its user can
see it, a lost read-back never makes a file verified, and a lost delete comes back.
On real rows and sim, through the real consumer; only the provider's answers are made
to fail."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from watched import Interleaved, Watched

from omnifile_service.backends.base import (
    BackendError,
    LogicalTier,
    ObjectNotFound,
    Throttled,
)
from omnifile_service.backends.sim import SimBackend
from omnifile_service.catalog.privileges import as_app
from omnifile_service.notify.cards import RETRY_TEXT, failure_text
from omnifile_service.retention.worm_sync import delete_blob
from omnifile_service.web.entries import decorate
from omnifile_service.worker import redrive as rd
from omnifile_service.worker.backends import BackendRegistry
from omnifile_service.worker.queue import (
    THROTTLE_LIMIT,
    THROTTLED_SINCE_KEY,
    Consumer,
    ConsumerConfig,
    Message,
)
from omnifile_service.worker.redrive import RedriveConfig, redrive
from omnifile_service.worker.runner import Worker
from omnifile_service.worker.tiering import handle_move, handle_restore_budgeted
from omnifile_service.worker.verify import handle_verify

from .support import Stored, World, make_world, primary, queue, row, run, set_tier, store

NOW = RedriveConfig(first=timedelta(0), longest=timedelta(0))
DLQS = ("restores", "verify", "retention")


class Flaky:
    """A provider whose chosen calls fail."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self.errors: dict[str, Exception] = {}

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.backend, name)
        error = self.errors.get(name)
        if error is None:
            return attr

        def fail(*_a: Any, **_k: Any) -> Any:
            raise error

        if name == "get_range":
            return fail

        async def afail(*_a: Any, **_k: Any) -> Any:
            raise error

        return afail


@pytest.fixture(autouse=True)
def clean(db: psycopg.Connection) -> Iterator[None]:
    """The sweep reads the whole queue, and every active recall in the database."""
    for q in DLQS:
        db.execute(f"DELETE FROM pgmq.q_{q}_dlq")
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute(
            "UPDATE catalog.recall SET state = 'failed', failure = 'test'"
            " WHERE state IN ('requested', 'rehydrating', 'copying', 'downloading')"
        )
    yield


def world(db: psycopg.Connection, url: str, **kw: Any) -> tuple[World, Flaky, BackendRegistry]:
    w = make_world(db, url, layout=kw.pop("layout", "azure_worm"), **kw)
    flaky = Flaky(w.sim)
    return w, flaky, BackendRegistry.from_mapping({w.backend_id: flaky})  # type: ignore[dict-item]


def recall(db: psycopg.Connection, w: World, s: Stored, eta: datetime | None = None) -> int:
    """As `POST /recalls`: the recall and its message, in one transaction."""
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        (rid,) = db.execute(
            "INSERT INTO catalog.recall (file_id, version_id, edge_id, eta)"
            " VALUES (%s, %s, %s, %s) RETURNING recall_id",
            (s.file_id, s.version_id, w.edge_id, eta),
        ).fetchone()
        db.execute(
            "SELECT pgmq.send('restores'::text, %s::jsonb)", (Jsonb({"recall_id": str(rid)}),)
        )
    return rid


async def _connect(w: World) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(as_app(w.url, "worker"), autocommit=True)


def consume(w: World, registry: BackendRegistry, queue_name: str = "restores") -> Consumer:
    async def restores(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_restore_budgeted(msg, conn, registry=registry, now=w.clock.now())

    async def verify(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_verify(msg, conn, registry=registry)

    async def retention(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await delete_blob(conn, registry, int(msg.body["location_id"]), w.clock.now())

    handlers = {"restores": restores, "verify": verify, "retention": retention}
    return Consumer(
        lambda: _connect(w), queue_name, handlers[queue_name], ConsumerConfig(backoff_base=0)
    )


def drain(w: World, registry: BackendRegistry, queue_name: str = "restores") -> None:
    async def go() -> None:
        c = consume(w, registry, queue_name)
        try:
            await c.drain(limit=50)
        finally:
            await c.close()

    run(go())


def sweep(w: World, registry: BackendRegistry, cfg: RedriveConfig = NOW) -> int:
    async def go() -> int:
        conn = await _connect(w)
        try:
            return await redrive(conn, registry, w.clock.now(), cfg)
        finally:
            await conn.close()

    return run(go())


def state(db: psycopg.Connection, rid: int) -> dict:
    found = row(db, "SELECT * FROM catalog.recall WHERE recall_id = %s", rid)
    assert found is not None
    return found


def dead(db: psycopg.Connection, q: str) -> list[dict]:
    return [r[0] for r in db.execute(f"SELECT message FROM pgmq.q_{q}_dlq ORDER BY msg_id")]


def live(db: psycopg.Connection, q: str, key: str, value: int) -> int:
    (n,) = db.execute(
        f"SELECT count(*) FROM pgmq.q_{q} WHERE message->>%s = %s", (key, str(value))
    ).fetchone()
    return n


def archived(db: psycopg.Connection, w: World, name: str = "a.segy") -> Stored:
    s = store(db, w, name)
    set_tier(db, w, s, LogicalTier.ARCHIVE)
    return s


# --- a recall ends visibly ------------------------------------------------------------------


def test_a_dead_lettered_recall_is_stuck_until_the_sweep_sends_it_again(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The bug: five Azure errors, and the recall stays `requested` with no message."""
    w, flaky, reg = world(db, policy_db_url)
    s = archived(db, w)
    eta = datetime.now(UTC) + timedelta(hours=15)
    rid = recall(db, w, s, eta)
    flaky.errors["restore"] = BackendError("503 from Azure")
    drain(w, reg)
    assert state(db, rid)["state"] == "requested"
    assert live(db, "restores", "recall_id", rid) == 0
    letters = dead(db, "restores")
    assert len(letters) == 1 and "503" in letters[0]["error"]

    assert sweep(w, reg) == 1
    assert live(db, "restores", "recall_id", rid) == 1
    assert dead(db, "restores") == []  # the depth says what is waiting now
    # The promised time no longer means anything, and no new one is made up.
    assert state(db, rid)["eta"] is None
    assert {"recall_id": str(rid)} in queue(db, "notify")
    flaky.errors.clear()
    drain(w, reg)
    assert state(db, rid)["state"] == "rehydrating"


def test_a_recall_that_fails_every_time_ends_where_its_user_sees_it(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    s = archived(db, w)
    rid = recall(db, w, s)
    flaky.errors["restore"] = BackendError("the secret-account-name said no")
    for _ in range(NOW.tries + 2):
        drain(w, reg)
        assert state(db, rid)["state"] == "requested"
        sweep(w, reg)
    got = state(db, rid)
    assert got["state"] == "failed"
    assert got["completed_at"] is not None and got["eta"] is None
    # The portal shows this text. Never the provider's, and no time.
    assert "secret-account-name" not in got["failure"]
    assert re.findall(r"\d+", got["failure"]) == []  # no time, and no count to get wrong
    assert "again" in got["failure"]
    # The Teams card says so in general terms, and how to try again.
    text = failure_text(SimpleNamespace(failure=got["failure"]))  # type: ignore[arg-type]
    assert text == f"The storage service couldn't restore it. {RETRY_TEXT}"
    assert {"recall_id": str(rid)} in queue(db, "notify")  # the card follows
    (audit,) = db.execute(
        "SELECT details FROM catalog.audit_log WHERE action = 'recall.gave_up'"
        " AND details->>'recall_id' = %s",
        (str(rid),),
    ).fetchone()
    assert "secret-account-name" in audit["error"]  # for the operator
    # The dead letter stays, so the alert does, and nothing looks at it again.
    letters = dead(db, "restores")
    assert len(letters) == 1 and letters[0]["gave_up"] is True
    assert sweep(w, reg) == 0
    # The user can try again: a new recall, not a join.
    (again,) = db.execute(
        "INSERT INTO catalog.recall (file_id, version_id, edge_id) VALUES (%s, %s, %s)"
        " ON CONFLICT (file_id, version_id)"
        "   WHERE state IN ('requested', 'rehydrating', 'copying', 'downloading') DO NOTHING"
        " RETURNING recall_id",
        (s.file_id, s.version_id, w.edge_id),
    ).fetchone()
    assert again != rid


def _annex(db: psycopg.Connection, w: World) -> int:
    tag = uuid.uuid4().hex[:8]
    (nas,) = db.execute(
        "INSERT INTO catalog.backend (name, kind, config, private_network)"
        " VALUES (%s, 'nas', '{}', false) RETURNING backend_id",
        (f"nas-{tag}",),
    ).fetchone()
    (filer,) = db.execute(
        "INSERT INTO catalog.filer (name, host, protocol, export, edge_id, backend_id, created_by)"
        " VALUES (%s, %s, 'smb', 'geo', %s, %s, 't') RETURNING filer_id",
        (f"f-{tag}", f"h-{tag}", w.edge_id, nas),
    ).fetchone()
    (src,) = db.execute(
        "INSERT INTO catalog.source (dataset_id, path, backend_id, container, prefix, owners_sid,"
        " kind) VALUES (%s, %s, %s, 'geo', 'lts/', 'S-1-5-21-1-2-3-512', 'annex')"
        " RETURNING source_id",
        (w.dataset_id, f"from-{tag}", nas),
    ).fetchone()
    (annex,) = db.execute(
        "INSERT INTO catalog.annex (name, source_id, filer_id, dataset_id, edge_id, path,"
        " owners_sid, created_by) VALUES (%s, %s, %s, %s, %s, 'lts', 'S-1-5-21-1-2-3-512', 't')"
        " RETURNING annex_id",
        (f"a-{tag}", src, filer, w.dataset_id, w.edge_id),
    ).fetchone()
    return annex


def test_a_failed_recall_fails_the_placement_waiting_on_it(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Giving up goes through the same path as any other failure of a recall."""
    w, flaky, reg = world(db, policy_db_url)
    s = archived(db, w)
    rid = recall(db, w, s)
    (ident,) = db.execute(
        "INSERT INTO catalog.identity (sid, kind) VALUES (%s, 'user') RETURNING identity_id",
        (f"S-1-5-21-9-9-{int(uuid.uuid4().hex[:8], 16) % 10**8}-1105",),
    ).fetchone()
    (pid,) = db.execute(
        "INSERT INTO catalog.annex_placement (annex_id, file_id, version_id, recall_id,"
        " requester_identity_id, request_via, rule, ruleset_version, target, on_conflict, state)"
        " VALUES (%s, %s, %s, %s, %s, 'portal', 'back', 1, %s, 'restored_name', 'rehydrating')"
        " RETURNING placement_id",
        (_annex(db, w), s.file_id, s.version_id, rid, ident,
         Jsonb({"kind": "nas", "folder": "lts", "name": "a.segy"})),
    ).fetchone()  # fmt: skip
    flaky.errors["restore"] = BackendError("no")
    for _ in range(NOW.tries + 2):
        drain(w, reg)
        sweep(w, reg)
    assert state(db, rid)["state"] == "failed"
    assert db.execute(
        "SELECT state, fail_reason FROM catalog.annex_placement WHERE placement_id = %s", (pid,)
    ).fetchone() == ("failed", "recall_failed")


def test_each_send_waits_longer(db: psycopg.Connection, policy_db_url: str) -> None:
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["restore"] = BackendError("no")
    cfg = RedriveConfig(first=timedelta(minutes=5), longest=timedelta(minutes=15))
    for waited in (5, 10, 15, 15):
        drain(w, reg)
        assert live(db, "restores", "recall_id", rid) == 0
        assert sweep(w, reg, cfg) == 0, "sent again before its time"
        db.execute(
            "UPDATE pgmq.q_restores_dlq SET enqueued_at = now() - %s::interval",
            (timedelta(minutes=waited - 1),),
        )
        assert sweep(w, reg, cfg) == 0, f"sent again before {waited} minutes"
        db.execute(
            "UPDATE pgmq.q_restores_dlq SET enqueued_at = now() - %s::interval",
            (timedelta(minutes=waited, seconds=1),),
        )
        assert sweep(w, reg, cfg) == 1
        assert live(db, "restores", "recall_id", rid) == 1


def test_an_outage_doesnt_fail_the_fleet(db: psycopg.Connection, policy_db_url: str) -> None:
    """Nothing answers, not even a HEAD of the recall's own object: every recall waits,
    however many tries it has used, until the outage limit."""
    w, flaky, reg = world(db, policy_db_url)
    rids = [recall(db, w, archived(db, w, f"f{i}.segy")) for i in range(3)]
    flaky.errors["head"] = BackendError("connection refused")
    flaky.errors["restore"] = BackendError("connection refused")
    for _ in range(NOW.tries + 4):
        drain(w, reg)
        sweep(w, reg)
    for rid in rids:
        assert state(db, rid)["state"] == "requested"
        assert live(db, "restores", "recall_id", rid) == 1
    # Over: the same messages go through, with nothing lost.
    flaky.errors.clear()
    drain(w, reg)
    assert {state(db, rid)["state"] for rid in rids} == {"rehydrating"}


def test_a_hanging_provider_is_asked_once_a_sweep(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    for i in range(3):
        recall(db, w, archived(db, w, f"f{i}.segy"))
    flaky.errors["head"] = BackendError("connection refused")
    drain(w, reg)
    cfg = RedriveConfig(first=timedelta(0), longest=timedelta(0), tries=0)
    asked: list[Any] = []
    real = reg.get

    class Counting:
        def __getattr__(self, name: str) -> Any:
            return getattr(flaky, name)

        async def head(self, ref: Any) -> Any:
            asked.append(ref)
            return await flaky.head(ref)

    reg.get = lambda _id: Counting()  # type: ignore[method-assign]
    try:
        sweep(w, reg, cfg)
    finally:
        reg.get = real  # type: ignore[method-assign]
    assert len(asked) == 1
    assert len(queue(db, "restores")) == 3


def test_an_outage_that_never_ends_still_ends_the_recall(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["head"] = BackendError("connection refused")
    for _ in range(NOW.tries + 1):
        drain(w, reg)
        sweep(w, reg)
    drain(w, reg)
    assert state(db, rid)["state"] == "requested"
    long_ago = (datetime.now(UTC) - NOW.outage_limit - timedelta(minutes=1)).isoformat()
    db.execute(
        "UPDATE pgmq.q_restores_dlq SET message = jsonb_set(message, '{message,_first_dead_at}',"
        " to_jsonb(%s::text))",
        (long_ago,),
    )
    sweep(w, reg)  # one last try
    assert state(db, rid)["state"] == "requested"
    drain(w, reg)
    sweep(w, reg)
    assert state(db, rid)["state"] == "failed"


def test_a_mass_failure_reaches_users_a_batch_at_a_time(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    rids = [recall(db, w, archived(db, w, f"f{i}.segy")) for i in range(3)]
    flaky.errors["restore"] = BackendError("no")
    cfg = RedriveConfig(first=timedelta(0), longest=timedelta(0), tries=0, batch=2)
    drain(w, reg)
    db.execute(
        "UPDATE pgmq.q_restores_dlq SET message = jsonb_set(message, '{message,_last_try}', 'true')"
    )
    assert sweep(w, reg, cfg) == 2
    assert sorted(state(db, r)["state"] for r in rids) == ["failed", "failed", "requested"]
    assert sweep(w, reg, cfg) == 1


def test_a_recall_with_no_message_and_no_dead_letter_is_sent_again(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A message lost another way: purged by hand, say."""
    w, _flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    db.execute("DELETE FROM pgmq.q_restores")
    assert sweep(w, reg) == 0, "a new recall may have a message on its way"
    db.execute(
        "UPDATE catalog.recall SET requested_at = now() - interval '3 minutes'"
        " WHERE recall_id = %s",
        (rid,),
    )
    assert sweep(w, reg) == 1
    assert live(db, "restores", "recall_id", rid) == 1
    assert sweep(w, reg) == 0, "only once"


def test_a_dead_letter_nobody_is_waiting_on_is_only_taken_away(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["restore"] = BackendError("no")
    drain(w, reg)
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.recall SET state = 'cancelled' WHERE recall_id = %s", (rid,))
    assert sweep(w, reg) == 0
    assert dead(db, "restores") == []
    assert live(db, "restores", "recall_id", rid) == 0


def test_the_worker_runs_the_sweep(db: psycopg.Connection, policy_db_url: str) -> None:
    w, _flaky, reg = world(db, policy_db_url)

    async def connect() -> psycopg.AsyncConnection:
        return await _connect(w)

    assert "dead_letter_redrive" in Worker(connect, reg, clock=w.clock).sweeps


# --- errors that aren't the recall's fault --------------------------------------------------


def _throttled_for(db: psycopg.Connection, q: str, how_long: timedelta) -> None:
    since = (datetime.now(UTC) - how_long).isoformat()
    db.execute(
        f"UPDATE pgmq.q_{q} SET vt = now(), message = message || %s",
        (Jsonb({THROTTLED_SINCE_KEY: since}),),
    )


def test_a_burst_of_throttling_uses_up_no_attempt(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["restore"] = Throttled("slow down", timedelta(seconds=0))
    for _ in range(ConsumerConfig.max_failures + 2):
        drain(w, reg)
    assert dead(db, "restores") == []
    (body,) = db.execute("SELECT message FROM pgmq.q_restores").fetchone()
    assert "_failures" not in body and THROTTLED_SINCE_KEY in body
    assert state(db, rid)["state"] == "requested"
    # And it goes through when the provider has room.
    flaky.errors.clear()
    drain(w, reg)
    assert state(db, rid)["state"] == "rehydrating"
    (body,) = db.execute("SELECT message FROM pgmq.q_restores").fetchone()
    assert THROTTLED_SINCE_KEY not in body


def test_a_throttled_message_waits_as_the_provider_asks(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    recall(db, w, archived(db, w))
    flaky.errors["restore"] = Throttled("slow down", timedelta(minutes=7))
    drain(w, reg)
    (left,) = db.execute("SELECT extract(epoch FROM vt - now()) FROM pgmq.q_restores").fetchone()
    assert 6 * 60 < left <= 7 * 60


def test_days_of_503s_reach_the_alert_and_then_the_outage_rule(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Azure answers a gateway's 503 as `Throttled`. On a fresh message each time it would
    loop for ever with no alert, and the outage limit would never apply."""
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["head"] = Throttled("503 from a gateway")
    flaky.errors["restore"] = Throttled("503 from a gateway")
    drain(w, reg)
    assert dead(db, "restores") == [] and live(db, "restores", "recall_id", rid) == 1
    _throttled_for(db, "restores", THROTTLE_LIMIT - timedelta(minutes=1))
    drain(w, reg)
    assert dead(db, "restores") == [], "just under the limit"
    _throttled_for(db, "restores", THROTTLE_LIMIT + timedelta(minutes=1))
    drain(w, reg)
    letters = dead(db, "restores")
    assert len(letters) == 1 and "503" in letters[0]["error"]
    # A HEAD that gets the same 503 is no answer: it waits, up to the limit, like an outage.
    for _ in range(NOW.tries + 2):
        sweep(w, reg)
        drain(w, reg)
    assert state(db, rid)["state"] == "requested"
    assert len(dead(db, "restores")) == 1, "each re-send comes straight back, not an hour later"
    long_ago = (datetime.now(UTC) - NOW.outage_limit - timedelta(minutes=1)).isoformat()
    db.execute(
        "UPDATE pgmq.q_restores_dlq SET message = jsonb_set(message, '{message,_first_dead_at}',"
        " to_jsonb(%s::text))",
        (long_ago,),
    )
    sweep(w, reg)  # one last try
    drain(w, reg)
    sweep(w, reg)
    assert state(db, rid)["state"] == "failed"


def test_a_throttled_read_back_has_the_same_limit(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    _unverified(db, s)
    flaky.errors["get_range"] = Throttled("503 from a gateway")
    for _ in range(ConsumerConfig.max_failures + 2):
        drain(w, reg, "verify")
        db.execute("UPDATE pgmq.q_verify SET vt = now()")
    assert dead(db, "verify") == [], "a burst uses up no attempt"
    _throttled_for(db, "verify", THROTTLE_LIMIT + timedelta(minutes=1))
    drain(w, reg, "verify")
    assert len(dead(db, "verify")) == 1


def test_an_object_that_is_gone_fails_the_recall_at_once(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["head"] = ObjectNotFound("gone")
    drain(w, reg)
    assert state(db, rid)["state"] == "failed"
    assert dead(db, "restores") == []


def test_an_answer_from_the_provider_clears_the_count(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A restore can take a day. Errors from the morning don't count against it."""
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["restore"] = BackendError("no")
    drain(w, reg)
    sweep(w, reg)
    (body,) = db.execute("SELECT message FROM pgmq.q_restores").fetchone()
    assert body[rd.REDRIVES_KEY] == 1
    flaky.errors.clear()
    drain(w, reg)
    assert state(db, rid)["state"] == "rehydrating"
    (body,) = db.execute("SELECT message FROM pgmq.q_restores").fetchone()
    assert rd.REDRIVES_KEY not in body


# --- a lost read-back -----------------------------------------------------------------------


def _unverified(db: psycopg.Connection, s: Stored) -> None:
    db.execute(
        "UPDATE catalog.location SET verified = false, verified_at = NULL, verified_by = NULL"
        " WHERE location_id = %s",
        (s.location_id,),
    )
    db.execute(
        "SELECT pgmq.send('verify'::text, %s::jsonb)",
        (Jsonb({"location_id": str(s.location_id)}),),
    )


def test_a_lost_read_back_never_verifies_and_ends_on_the_alert(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    _unverified(db, s)
    flaky.errors["get_range"] = RuntimeError("a bug in the read path")
    for _ in range(NOW.tries + 2):
        drain(w, reg, "verify")
        assert primary(db, s.file_id)["verified"] is False
        sweep(w, reg)
    assert primary(db, s.file_id)["verified"] is False
    (decision, details) = db.execute(
        "SELECT decision, details FROM catalog.audit_log WHERE action = 'location.verify'"
        " AND file_id = %s",
        (s.file_id,),
    ).fetchone()
    assert decision == "deny" and details["location_id"] == str(s.location_id)
    # This loop's rounds: the first delivery, the re-sends, the last try.
    assert f"{NOW.tries + 2} rounds" in details["reason"]
    # The dead letter stays, so the alert does, and nothing more is tried.
    letters = dead(db, "verify")
    assert len(letters) == 1 and letters[0]["gave_up"] is True
    assert sweep(w, reg) == 0
    assert live(db, "verify", "location_id", s.location_id) == 0


def test_a_read_back_given_up_on_tells_the_annex(
    db: psycopg.Connection, policy_db_url: str, monkeypatch
) -> None:
    """Given up on, its copy stayed `copied` with no word on the annex (annex/readback.py)."""
    told: list[tuple] = []

    async def spy(conn, location_id, file_id, version_id, why, cause):
        told.append((location_id, file_id, version_id, cause))
        return 0

    monkeypatch.setattr(rd, "copy_refused", spy)
    w, flaky, reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    _unverified(db, s)
    flaky.errors["get_range"] = RuntimeError("a bug in the read path")
    for _ in range(NOW.tries + 2):
        drain(w, reg, "verify")
        sweep(w, reg)
    (vid,) = db.execute(
        "SELECT version_id FROM catalog.location WHERE location_id = %s", (s.location_id,)
    ).fetchone()
    # Review of #81: said as what it was, not as a copy that didn't match.
    assert told == [(s.location_id, s.file_id, vid, "gave_up")]
    (cause,) = db.execute(
        "SELECT details->>'cause' FROM catalog.audit_log WHERE action = 'location.verify'"
        " AND decision = 'deny' AND version_id = %s",
        (vid,),
    ).fetchone()
    assert cause == "gave_up"


def test_each_cause_of_a_refusal_has_its_own_words() -> None:
    from omnifile_service.annex.readback import CAUSES

    assert "didn't match" in CAUSES["mismatch"]
    assert "didn't match" not in CAUSES["gave_up"] and "kept failing" in CAUSES["gave_up"]
    assert "didn't match" not in CAUSES["missing"]


def test_a_read_back_sent_again_can_still_verify(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    _unverified(db, s)
    flaky.errors["get_range"] = RuntimeError("a bad afternoon")
    drain(w, reg, "verify")
    assert len(dead(db, "verify")) == 1
    sweep(w, reg)
    # One of its re-sends, not yet the last try.
    (body,) = db.execute("SELECT message FROM pgmq.q_verify").fetchone()
    assert body[rd.REDRIVES_KEY] == 1 and rd.LAST_TRY_KEY not in body
    flaky.errors.clear()
    drain(w, reg, "verify")
    assert primary(db, s.file_id)["verified"] is True


def test_a_dead_read_back_of_a_location_already_verified_is_taken_away(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    _unverified(db, s)
    flaky.errors["get_range"] = RuntimeError("no")
    drain(w, reg, "verify")
    db.execute(
        "UPDATE catalog.location SET verified = true, verified_at = now(),"
        " verified_by = 'azure_read_crc' WHERE location_id = %s",
        (s.location_id,),
    )
    assert sweep(w, reg) == 0
    assert dead(db, "verify") == []
    assert live(db, "verify", "location_id", s.location_id) == 0


def test_a_read_that_keeps_erroring_ends_in_a_dead_letter_not_a_loop(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A provider error on a read-back retries, further apart. Before, it did so every 30
    s for ever, with a fresh message each time, so even the queue-age alert stayed quiet."""
    from omnifile_service.worker import verify as v

    w, flaky, reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    _unverified(db, s)
    flaky.errors["get_range"] = BackendError("503")
    drain(w, reg, "verify")
    (vt_seconds, body) = db.execute(
        "SELECT extract(epoch FROM vt - now()), message FROM pgmq.q_verify"
    ).fetchone()
    assert 20 < vt_seconds <= 30 and body[v.READ_ERRORS_KEY] == 1
    db.execute(
        "UPDATE pgmq.q_verify SET vt = now(), message = message || %s",
        (Jsonb({v.READ_ERRORS_KEY: v.MAX_READ_ERRORS - 1}),),
    )
    for _ in range(ConsumerConfig.max_failures + 1):
        drain(w, reg, "verify")
    assert len(dead(db, "verify")) == 1
    assert primary(db, s.file_id)["verified"] is False


# --- a lost delete --------------------------------------------------------------------------


def _deleting(db: psycopg.Connection, s: Stored) -> None:
    """A pruned version's bytes on their way out, as `prune_versions` leaves them."""
    with db.transaction():
        db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
        db.execute("UPDATE catalog.file_version SET state = 'pruned' WHERE version_id = %s",
                   (s.version_id,))  # fmt: skip
        db.execute(
            "UPDATE catalog.location SET state = 'deleting', verified = false"
            " WHERE location_id = %s",
            (s.location_id,),
        )
        db.execute(
            "SELECT pgmq.send('retention'::text, %s::jsonb)",
            (Jsonb({"op": "delete_blob", "location_id": str(s.location_id)}),),
        )


def _location_state(db: psycopg.Connection, s: Stored) -> str:
    (st,) = db.execute(
        "SELECT state FROM catalog.location WHERE location_id = %s", (s.location_id,)
    ).fetchone()
    return st


def test_a_lost_delete_comes_back(db: psycopg.Connection, policy_db_url: str) -> None:
    w, flaky, reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    _deleting(db, s)
    flaky.errors["get_tags"] = RuntimeError("no")
    flaky.errors["delete"] = RuntimeError("no")
    drain(w, reg, "retention")
    assert _location_state(db, s) == "deleting"
    assert len(dead(db, "retention")) == 1
    assert sweep(w, reg) == 1
    flaky.errors.clear()
    drain(w, reg, "retention")
    assert _location_state(db, s) == "deleted"
    assert dead(db, "retention") == []


def test_a_delete_that_always_fails_stops_and_says_so(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    _deleting(db, s)
    flaky.errors["get_tags"] = RuntimeError("no")
    flaky.errors["delete"] = RuntimeError("no")
    for _ in range(NOW.tries + 1):
        drain(w, reg, "retention")
        sweep(w, reg)
    assert _location_state(db, s) == "deleting"  # the bytes are still there, and billed
    letters = dead(db, "retention")
    assert len(letters) == 1 and letters[0]["gave_up"] is True
    assert (
        db.execute(
            "SELECT count(*) FROM catalog.audit_log WHERE action = 'retention.gave_up'"
            " AND details->'message'->>'location_id' = %s",
            (str(s.location_id),),
        ).fetchone()[0]
        == 1
    )
    assert sweep(w, reg) == 0


def _dead_retention(db: psycopg.Connection, body: dict) -> None:
    db.execute(
        "SELECT pgmq.send('retention_dlq'::text, %s::jsonb)",
        (Jsonb({"queue": "retention", "msg_id": 1, "failures": 5, "error": "x", "message": body}),),
    )


def test_a_worm_policy_dead_letter_is_taken_away_since_its_sweep_redoes_it(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, _flaky, reg = world(db, policy_db_url)
    _dead_retention(db, {"op": "worm_policy", "share_id": "1"})
    sweep(w, reg)
    assert dead(db, "retention") == []
    assert queue(db, "retention") == []


def test_a_message_the_sweep_doesnt_understand_is_left_for_a_person(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, _flaky, reg = world(db, policy_db_url)
    _dead_retention(db, {"op": "something_new"})
    sweep(w, reg)
    letters = dead(db, "retention")
    assert len(letters) == 1 and letters[0]["gave_up"] is True
    assert queue(db, "retention") == []


def test_an_annex_purge_comes_back_only_while_it_is_wanted(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """`purge_due` skips a purge already requested, so a lost one would never run."""
    w, _flaky, reg = world(db, policy_db_url)
    s = store(db, w, "gone-from-the-nas.segy")
    annex = _annex(db, w)
    db.execute(
        "INSERT INTO catalog.annex_file (annex_id, file_id, path, nas_file_id, size, mtime_ns,"
        " first_seen, last_seen, nas_state, copied_size, gone_since, purge_requested_at,"
        " purge_requested_by)"
        " VALUES (%s, %s, 'x', 'x', 1, 1, now(), now(), 'present', 1, now(), now(), 'someone')",
        (annex, s.file_id),
    )
    body = {"op": "annex_purge", "annex_id": str(annex), "file_id": str(s.file_id)}
    _dead_retention(db, body)
    assert sweep(w, reg) == 1
    assert [m["op"] for m in queue(db, "retention")] == ["annex_purge"]
    db.execute("DELETE FROM pgmq.q_retention")
    db.execute(
        "UPDATE catalog.annex_file SET purged_at = now(), purged_by = 'x' WHERE file_id = %s",
        (s.file_id,),
    )
    _dead_retention(db, body)
    assert sweep(w, reg) == 0  # purged since: only taken away
    assert dead(db, "retention") == [] and queue(db, "retention") == []


def test_sim_is_what_these_tests_run_on(db: psycopg.Connection, policy_db_url: str) -> None:
    w, flaky, _reg = world(db, policy_db_url)
    assert isinstance(flaky.backend, SimBackend) and w.layout == "azure_worm"


def test_a_dead_letter_whose_recall_has_a_message_again_is_only_taken_away(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Someone sent it by hand, or the change feed nudged it: only one message runs."""
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["restore"] = BackendError("no")
    drain(w, reg)
    db.execute("SELECT pgmq.send('restores'::text, %s::jsonb)", (Jsonb({"recall_id": str(rid)}),))
    assert sweep(w, reg) == 0
    assert dead(db, "restores") == []
    assert live(db, "restores", "recall_id", rid) == 1


def test_an_archive_moves_lost_read_back_ends_with_the_original_kept(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Its `verifying` phase used to send the read-back again every 30 s for ever. A dead
    letter isn't lost now: it waits for this sweep, and acts on the deny at the end."""
    w, flaky, reg = world(db, policy_db_url, layout="azure_two_account")
    s = store(db, w, "a.segy")
    db.execute(
        "SELECT pgmq.send('tiering'::text, %s::jsonb)",
        (Jsonb({"kind": "move", "file_id": str(s.file_id), "version_id": str(s.version_id),
                "location_id": str(s.location_id), "tier": "archive"}),),
    )  # fmt: skip

    async def moves(msg: Message, conn: psycopg.AsyncConnection) -> None:
        await handle_move(msg, conn, registry=reg, now=w.clock.now())

    def move_on() -> None:
        async def go() -> None:
            c = Consumer(lambda: _connect(w), "tiering", moves, ConsumerConfig(backoff_base=0))
            try:
                await c.drain(limit=10)
            finally:
                await c.close()

        run(go())

    move_on()
    (copy,) = db.execute(
        "SELECT location_id FROM catalog.location WHERE version_id = %s AND role = 'secondary'",
        (s.version_id,),
    ).fetchone()
    flaky.errors["get_range"] = RuntimeError("a bug in the read path")
    for _ in range(NOW.tries + 2):
        drain(w, reg, "verify")
        db.execute("UPDATE pgmq.q_tiering SET vt = now()")
        move_on()
        # While it lies dead, the waiting move doesn't send another.
        if dead(db, "verify") and not dead(db, "verify")[-1].get("gave_up"):
            assert live(db, "verify", "location_id", copy) == 0
        sweep(w, reg)
    db.execute("UPDATE pgmq.q_tiering SET vt = now()")
    move_on()
    (st,) = db.execute(
        "SELECT state FROM catalog.location WHERE location_id = %s", (copy,)
    ).fetchone()
    assert st == "deleting", "the copy that failed its read-back is dropped"
    assert primary(db, s.file_id)["location_id"] == s.location_id, "the original is kept"
    assert primary(db, s.file_id)["logical_tier"] != "archive"


# --- an outage and read-backs ---------------------------------------------------------------


def _to_the_bound(
    db: psycopg.Connection, w: World, reg: BackendRegistry, q: str = "verify"
) -> None:
    for _ in range(NOW.tries):
        drain(w, reg, q)
        sweep(w, reg)
    drain(w, reg, q)


def test_an_outage_doesnt_deny_read_backs_in_bulk(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Denying them would drop every archive copy and leave every upload for `resend --all`."""
    w, flaky, reg = world(db, policy_db_url)
    files = [store(db, w, f"f{i}.segy") for i in range(3)]
    for s in files:
        _unverified(db, s)
    flaky.errors["get_range"] = RuntimeError("connection refused")
    flaky.errors["head"] = RuntimeError("connection refused")
    _to_the_bound(db, w, reg)
    for _ in range(3):
        sweep(w, reg)
        drain(w, reg, "verify")
    assert (
        db.execute(
            "SELECT count(*) FROM catalog.audit_log WHERE action = 'location.verify'"
            " AND file_id = ANY(%s)",
            ([s.file_id for s in files],),
        ).fetchone()[0]
        == 0
    )
    # Over: the same messages verify, with nothing lost.
    flaky.errors.clear()
    sweep(w, reg)
    drain(w, reg, "verify")
    assert all(primary(db, s.file_id)["verified"] for s in files)


def test_a_read_back_outage_that_never_ends_still_ends(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    _unverified(db, s)
    flaky.errors["get_range"] = RuntimeError("connection refused")
    flaky.errors["head"] = RuntimeError("connection refused")
    _to_the_bound(db, w, reg)
    long_ago = (datetime.now(UTC) - NOW.outage_limit - timedelta(minutes=1)).isoformat()
    db.execute(
        "UPDATE pgmq.q_verify_dlq SET message = jsonb_set(message, '{message,_first_dead_at}',"
        " to_jsonb(%s::text))",
        (long_ago,),
    )
    sweep(w, reg)  # one last try
    drain(w, reg, "verify")
    sweep(w, reg)
    assert (
        db.execute(
            "SELECT decision FROM catalog.audit_log WHERE action = 'location.verify'"
            " AND file_id = %s",
            (s.file_id,),
        ).fetchone()[0]
        == "deny"
    )


def test_a_re_send_after_a_day_of_read_errors_comes_back_at_once(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The sweep's own re-send keeps the count, so it doesn't loop unseen for another day.
    A person's resend clears it (`tests/admin/test_dead_letters.py`)."""
    from omnifile_service.worker import verify as v

    w, flaky, reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    _unverified(db, s)
    db.execute(
        "UPDATE pgmq.q_verify SET message = message || %s",
        (Jsonb({v.READ_ERRORS_KEY: v.MAX_READ_ERRORS}),),
    )
    flaky.errors["get_range"] = BackendError("503")
    drain(w, reg, "verify")
    assert len(dead(db, "verify")) == 1
    sweep(w, reg)
    (body,) = db.execute("SELECT message FROM pgmq.q_verify").fetchone()
    assert body[v.READ_ERRORS_KEY] == v.MAX_READ_ERRORS
    drain(w, reg, "verify")
    assert len(dead(db, "verify")) == 1


# --- the reviewer's survivors ---------------------------------------------------------------


def test_a_blob_that_is_really_gone_doesnt_wait_out_the_outage_limit(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """ "Not found" is an answer. Taken for an outage, the recall would wait 3 days."""
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    drain(w, reg)
    assert state(db, rid)["state"] == "rehydrating"
    # Only the status fails, so the handler itself never sees the object is gone.
    flaky.errors["restore_status"] = BackendError("no")
    flaky.errors["head"] = ObjectNotFound("gone")
    for _ in range(NOW.tries + 2):
        db.execute("UPDATE pgmq.q_restores SET vt = now()")
        drain(w, reg)
        sweep(w, reg)
    assert state(db, rid)["state"] == "failed"


def test_an_outage_that_just_ended_gets_one_more_try(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Every try failed during it, and now the provider answers. That isn't this recall's
    fault, so it isn't failed until a try has been made with the provider up."""
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["head"] = BackendError("connection refused")
    flaky.errors["restore"] = BackendError("connection refused")
    for _ in range(NOW.tries):
        drain(w, reg)
        sweep(w, reg)
    drain(w, reg)
    flaky.errors.clear()
    sweep(w, reg)
    assert state(db, rid)["state"] == "requested"
    drain(w, reg)
    assert state(db, rid)["state"] == "rehydrating"


def test_a_recall_that_finished_during_the_probe_isnt_failed(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The sweep reads the recall, asks the provider, and only then locks it."""
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["restore"] = BackendError("no")
    for _ in range(NOW.tries + 1):
        drain(w, reg)
        sweep(w, reg)
    drain(w, reg)
    db.execute("DELETE FROM pgmq.q_notify")

    def finished(_call: str) -> None:
        with db.transaction():
            db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            db.execute(
                "UPDATE catalog.recall SET state = 'downloading' WHERE recall_id = %s", (rid,)
            )

    provider = Watched(flaky, w.url, finished)
    probing = BackendRegistry.from_mapping({w.backend_id: provider})  # type: ignore[dict-item]
    sweep(w, probing)
    assert provider.calls() == ["head"]
    assert state(db, rid)["state"] == "downloading"
    assert queue(db, "notify") == []


def test_only_the_first_re_send_touches_the_card(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Not a Teams message every time it is sent again."""
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w), datetime.now(UTC) + timedelta(hours=15))
    flaky.errors["restore"] = BackendError("no")
    db.execute("DELETE FROM pgmq.q_notify")
    for _ in range(3):
        drain(w, reg)
        sweep(w, reg)
    assert queue(db, "notify") == [{"recall_id": str(rid)}]


@pytest.mark.parametrize("what", ["a_message_arrived", "it_was_cancelled"])
def test_a_lost_recall_is_looked_at_again_under_its_lock(
    db: psycopg.Connection, policy_db_url: str, what: str
) -> None:
    w, _flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    db.execute("DELETE FROM pgmq.q_restores")
    db.execute(
        "UPDATE catalog.recall SET requested_at = now() - interval '3 minutes'"
        " WHERE recall_id = %s",
        (rid,),
    )

    def between() -> None:
        with db.transaction():
            db.execute("SELECT set_config('omnifile.actor', 'system:test', true)")
            if what == "a_message_arrived":
                db.execute(
                    "SELECT pgmq.send('restores'::text, %s::jsonb)",
                    (Jsonb({"recall_id": str(rid)}),),
                )
            else:
                db.execute(
                    "UPDATE catalog.recall SET state = 'cancelled' WHERE recall_id = %s", (rid,)
                )

    async def go() -> int:
        conn = await _connect(w)
        try:
            racing = Interleaved(conn, "FOR UPDATE SKIP LOCKED", between)
            return await rd._send_lost(racing, NOW)  # type: ignore[arg-type]
        finally:
            await conn.close()

    assert run(go()) == 0
    want = 1 if what == "a_message_arrived" else 0
    assert live(db, "restores", "recall_id", rid) == want


def test_a_file_whose_read_back_was_given_up_on_says_so_in_the_portals_answer(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    good = store(db, w, "b.segy")
    _unverified(db, s)
    flaky.errors["get_range"] = RuntimeError("a bug in the read path")

    async def entries() -> dict[int, Any]:
        conn = await psycopg.AsyncConnection.connect(
            as_app(w.url, "web"), autocommit=True, row_factory=dict_row
        )
        try:
            cur = await conn.execute(
                "SELECT * FROM catalog.file WHERE file_id = ANY(%s)", ([s.file_id, good.file_id],)
            )
            files = await cur.fetchall()
            out = await decorate(conn, files, [], w.clock.now())
        finally:
            await conn.close()
        return {int(e["file_id"]): e["read_back_failed_at"] for e in out}

    assert run(entries()) == {s.file_id: None, good.file_id: None}, "not until it gives up"
    for _ in range(NOW.tries + 2):
        drain(w, reg, "verify")
        sweep(w, reg)
    got = run(entries())
    assert got[s.file_id] is not None and got[good.file_id] is None
    # A person resends it, and the read-back passes: the file says nothing any more.
    db.execute("DELETE FROM pgmq.q_verify_dlq")
    db.execute(
        "SELECT pgmq.send('verify'::text, %s::jsonb)",
        (Jsonb({"location_id": str(s.location_id)}),),
    )
    flaky.errors.clear()
    drain(w, reg, "verify")
    assert run(entries()) == {s.file_id: None, good.file_id: None}


def test_a_recall_waiting_out_its_dead_letters_time_isnt_taken_for_a_lost_one(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """Old enough to be looked at, with no message, but its letter says when to send it. Taking
    it for lost would send it now, every sweep, and the letter would never end it."""
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["restore"] = BackendError("no")
    drain(w, reg)
    db.execute(
        "UPDATE catalog.recall SET requested_at = now() - interval '3 minutes'"
        " WHERE recall_id = %s",
        (rid,),
    )
    assert sweep(w, reg, RedriveConfig(first=timedelta(hours=1), longest=timedelta(hours=1))) == 0
    assert live(db, "restores", "recall_id", rid) == 0
    assert len(dead(db, "restores")) == 1


def test_an_outage_after_the_last_try_earns_another(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """The last try is one made with the provider up. One lost to an outage doesn't count."""
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["restore"] = BackendError("no")
    for _ in range(NOW.tries + 1):
        drain(w, reg)
        sweep(w, reg)
    (body,) = db.execute("SELECT message FROM pgmq.q_restores").fetchone()
    assert body[rd.LAST_TRY_KEY] is True
    # That try fails in an outage, and the sweep holds it.
    flaky.errors["head"] = BackendError("connection refused")
    drain(w, reg)
    sweep(w, reg)
    (body,) = db.execute("SELECT message FROM pgmq.q_restores").fetchone()
    assert rd.LAST_TRY_KEY not in body
    # Over: it isn't failed unseen, but tried once more.
    flaky.errors.pop("head")
    drain(w, reg)
    sweep(w, reg)
    assert state(db, rid)["state"] == "requested"
    (body,) = db.execute("SELECT message FROM pgmq.q_restores").fetchone()
    assert body[rd.LAST_TRY_KEY] is True


def test_a_backend_with_no_binding_isnt_taken_for_an_outage(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """No wait brings a binding back: the recall ends, not 3 days later."""
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["restore"] = BackendError("no")
    for _ in range(NOW.tries + 1):
        drain(w, reg)
        sweep(w, reg)
    drain(w, reg)
    assert state(db, rid)["state"] == "requested"
    sweep(w, BackendRegistry.from_mapping({}))
    assert state(db, rid)["state"] == "failed"


def test_a_throttled_read_back_keeps_its_floor_and_its_default(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A provider asking for a second doesn't get a read of a whole file every second."""
    w, flaky, reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    _unverified(db, s)
    flaky.errors["get_range"] = Throttled("slow down", timedelta(seconds=1))
    drain(w, reg, "verify")
    (left,) = db.execute("SELECT extract(epoch FROM vt - now()) FROM pgmq.q_verify").fetchone()
    assert 3 < left <= 5
    db.execute("UPDATE pgmq.q_verify SET vt = now()")
    flaky.errors["get_range"] = Throttled("slow down")
    drain(w, reg, "verify")
    (left,) = db.execute("SELECT extract(epoch FROM vt - now()) FROM pgmq.q_verify").fetchone()
    assert 25 < left <= 30


def test_a_deny_for_another_copy_of_the_file_says_nothing_about_this_one(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """An archive copy that failed its read-back is dropped and the original kept. A primary
    copy still waiting for its own read-back isn't "not checked"."""
    w, _flaky, _reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    _unverified(db, s)
    db.execute(
        "INSERT INTO catalog.audit_log (actor, action, decision, file_id, version_id, details)"
        " VALUES ('system:test', 'location.verify', 'deny', %s, %s, %s)",
        (s.file_id, s.version_id, Jsonb({"location_id": str(s.location_id + 1000), "reason": "x"})),
    )

    async def entry() -> Any:
        conn = await psycopg.AsyncConnection.connect(
            as_app(w.url, "web"), autocommit=True, row_factory=dict_row
        )
        try:
            cur = await conn.execute("SELECT * FROM catalog.file WHERE file_id = %s", (s.file_id,))
            (out,) = await decorate(conn, await cur.fetchall(), [], w.clock.now())
        finally:
            await conn.close()
        return out["read_back_failed_at"]

    assert run(entry()) is None


def test_a_removals_read_back_is_sent_again_though_the_copy_is_verified(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """A removal from a NAS reads a verified copy again (annex.md section 3.3), so its letter
    isn't one nobody is waiting on, and it keeps its purpose."""
    w, _flaky, reg = world(db, policy_db_url)
    s = store(db, w, "a.segy")
    assert primary(db, s.file_id)["verified"] is True
    db.execute(
        "SELECT pgmq.send('verify_dlq'::text, %s::jsonb)",
        (Jsonb({"queue": "verify", "msg_id": 1, "failures": 5, "error": "x",
                "message": {"location_id": str(s.location_id), "purpose": "removal"}}),),
    )  # fmt: skip
    assert sweep(w, reg) == 1
    (body,) = db.execute(
        "SELECT message FROM pgmq.q_verify WHERE message->>'location_id' = %s",
        (str(s.location_id),),
    ).fetchone()
    assert body["purpose"] == "removal"
    assert dead(db, "verify") == []


def test_a_recall_ends_on_the_waits_the_docs_give(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    """deploy/azure/README.md and wp23.md give about 5 h 25 min: these waits, 315 minutes,
    plus 8 rounds of 75 s. If this changes, change them."""
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["restore"] = BackendError("no")
    waits = [5, 10, 20, 40, 60, 60, 60, 60]
    assert sum(waits) == 315

    def died(ago: timedelta) -> None:
        db.execute("UPDATE pgmq.q_restores_dlq SET enqueued_at = now() - %s", (ago,))

    for minutes in waits:
        assert state(db, rid)["state"] == "requested"
        drain(w, reg)
        died(timedelta(minutes=minutes, seconds=-30))
        assert sweep(w, reg, RedriveConfig()) == 0, minutes
        died(timedelta(minutes=minutes, seconds=30))
        assert sweep(w, reg, RedriveConfig()) == 1, minutes
    assert state(db, rid)["state"] == "failed"


def test_a_letter_the_queue_didnt_write_doesnt_stop_the_sweep(
    db: psycopg.Connection, policy_db_url: str
) -> None:
    w, flaky, reg = world(db, policy_db_url)
    rid = recall(db, w, archived(db, w))
    flaky.errors["restore"] = BackendError("no")
    drain(w, reg)
    for bad in (["not", "an", "object"], {"failures": "many", "message": "not an object"}):
        db.execute("SELECT pgmq.send('restores_dlq'::text, %s::jsonb)", (Jsonb(bad),))
        db.execute("SELECT pgmq.send('verify_dlq'::text, %s::jsonb)", (Jsonb(bad),))
        db.execute("SELECT pgmq.send('retention_dlq'::text, %s::jsonb)", (Jsonb(bad),))
    assert sweep(w, reg) >= 1
    assert live(db, "restores", "recall_id", rid) == 1
