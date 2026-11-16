"""The `notify` consumer against the fakes and a real catalog (teams.md sections 3, 6-8)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg

from omnifile_service.worker.notify_jobs import build_jobs
from omnifile_service.worker.queue import Consumer, ConsumerConfig

from .conftest import run
from .support import (
    DOMAIN,
    Fakes,
    add_person,
    audits,
    graph_user,
    new_oid,
    seed_scenario,
    set_state,
    start_recall,
)


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now(UTC)

    def now(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def notify(c: psycopg.Connection, recall_id: int) -> None:
    c.execute("SELECT pgmq.send('notify', %s::jsonb)", (json.dumps({"recall_id": str(recall_id)}),))


def drain(connect, fakes: Fakes, clock: Clock, queue: str = "notify") -> Consumer:
    jobs = {j.queue: j for j in build_jobs(fakes.notifier, clock) if hasattr(j, "queue")}

    async def go() -> Consumer:
        consumer = Consumer(connect, queue, jobs[queue].handler, ConsumerConfig(max_failures=2))
        await consumer.drain()
        await consumer.close()
        return consumer

    return run(go())


def notification(c: psycopg.Connection, recall_id: int, identity_id: int, channel="teams"):
    cur = c.execute(
        "SELECT * FROM catalog.notification WHERE recall_id = %s AND identity_id = %s"
        " AND channel = %s",
        (recall_id, identity_id, channel),
    )
    row = cur.fetchone()
    return None if row is None else dict(zip([d.name for d in cur.description], row, strict=True))


def intents(c: psycopg.Connection, notification_id: int) -> list[tuple]:
    return c.execute(
        "SELECT action, card_version, used_at IS NOT NULL, revoked_at IS NOT NULL"
        " FROM catalog.teams_intent WHERE notification_id = %s ORDER BY card_version, action",
        (notification_id,),
    ).fetchall()


def titles(card: dict) -> list[str]:
    return [a["title"] for a in card.get("actions", [])]


def setup(db, fakes, **kw):
    s = seed_scenario(db, **kw)
    p = add_person(db, groups=[s.readers_sid], resolved=False)
    graph_user(fakes, p, groups=[s.readers_sid])
    return s, p


def test_the_requesters_card_follows_the_recall(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    s, p = setup(db, fakes)
    rid = start_recall(db, s, p)
    notify(db, rid)
    drain(connect, fakes, clock)

    n = notification(db, rid, p.identity_id)
    assert n["state"] == "sent" and n["card_version"] == 1 and n["audience"] == "requester"
    conv = n["teams_conversation_id"]
    card = fakes.bot.card(conv)
    assert titles(card) == ["Keep online", "Open in portal"]
    assert card["body"][0]["text"] == "Recalling line\\_1042\\.segy"
    assert "No time is promised yet." in json.dumps(card)  # 8 GB, and no ETA yet
    assert intents(db, n["notification_id"]) == [("keep_online", 1, False, False)]
    # The identity was resolved from Graph on the way (SID -> objectId, groups).
    ident = db.execute(
        "SELECT entra_object_id::text, groups_source, group_sids::text[] FROM catalog.identity"
        " WHERE identity_id = %s",
        (p.identity_id,),
    ).fetchone()
    assert ident[0] == p.oid and ident[1] == "graph"
    assert set(ident[2]) == {s.readers_sid, f"{DOMAIN}-513", "S-1-5-11"}
    assert [a["details"]["state"] for a in audits(db, "teams.card.sent", s.file_id)] == [
        "requested"
    ]

    # Same state again: coalesced, nothing sent.
    notify(db, rid)
    drain(connect, fakes, clock)
    assert len(fakes.bot.latest(conv).versions) == 1

    # A state change within 10 s waits; after 10 s it updates in place.
    set_state(db, rid, "rehydrating")
    notify(db, rid)
    drain(connect, fakes, clock)
    assert len(fakes.bot.latest(conv).versions) == 1
    assert db.execute("SELECT count(*) FROM pgmq.q_notify").fetchone()[0] == 1
    clock.advance(11)
    db.execute("UPDATE pgmq.q_notify SET vt = now()")
    drain(connect, fakes, clock)
    assert len(fakes.bot.activities(conv)) == 1
    assert len(fakes.bot.latest(conv).versions) == 2
    assert "Step 2 of 4" in json.dumps(fakes.bot.card(conv))
    got = intents(db, n["notification_id"])
    assert got == [("keep_online", 1, False, True), ("keep_online", 2, False, False)]

    for state in ("downloading", "available"):
        clock.advance(11)
        set_state(db, rid, state)
        notify(db, rid)
        drain(connect, fakes, clock)
    card = fakes.bot.card(conv)
    assert "is ready" in json.dumps(card) and titles(card) == ["Keep online"]
    assert [a["details"]["state"] for a in audits(db, "teams.card.updated", s.file_id)] == [
        "rehydrating",
        "downloading",
        "available",
    ]


def test_blocked_install_falls_back_to_email(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    s, p = setup(db, fakes)
    fakes.graph.blocked.add(p.oid)
    rid = start_recall(db, s, p)
    notify(db, rid)
    drain(connect, fakes, clock)
    assert notification(db, rid, p.identity_id)["state"] == "fell_back"
    assert notification(db, rid, p.identity_id, "email")["state"] == "sent"
    assert [m["subject"] for m in fakes.graph.mail] == ["Recalling line_1042.segy"]
    assert fakes.graph.mail[0]["to"] == [p.mail]
    # No mail for intermediate states; one at the end.
    for state in ("rehydrating", "downloading", "available"):
        clock.advance(11)
        set_state(db, rid, state)
        notify(db, rid)
        drain(connect, fakes, clock)
    assert [m["subject"] for m in fakes.graph.mail] == [
        "Recalling line_1042.segy",
        "line_1042.segy is ready",
    ]
    assert len(audits(db, "email.sent", s.file_id)) == 2
    assert fakes.bot.conversations == {}


def test_bot_403_reinstalls_once_then_emails(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    s, p = setup(db, fakes)
    rid = start_recall(db, s, p)
    notify(db, rid)
    drain(connect, fakes, clock)
    # 404 once: the conversation is re-created and a new card sent.
    fakes.bot.fail_next(404)
    clock.advance(11)
    set_state(db, rid, "rehydrating")
    notify(db, rid)
    drain(connect, fakes, clock)
    n = notification(db, rid, p.identity_id)
    assert n["state"] == "sent" and n["card_version"] == 2
    conv = n["teams_conversation_id"]
    assert len(fakes.bot.activities(conv)) == 2
    # 403 twice (update, then send after re-install): email.
    fakes.bot.fail_next(403, 2)
    clock.advance(11)
    set_state(db, rid, "failed")
    notify(db, rid)
    drain(connect, fakes, clock)
    assert notification(db, rid, p.identity_id)["state"] == "fell_back"
    assert [m["subject"] for m in fakes.graph.mail] == ["Couldn't recall line_1042.segy"]


def test_server_errors_back_off_and_are_audited(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    s, p = setup(db, fakes)
    rid = start_recall(db, s, p)
    fakes.bot.fail_next(502)
    notify(db, rid)
    drain(connect, fakes, clock)
    n = notification(db, rid, p.identity_id)
    assert n["state"] == "failed" and n["attempts"] == 1 and n["last_error"] == "bot_status_502"
    assert n["next_attempt_at"] == clock.t + timedelta(seconds=10)
    assert [a["details"]["error"] for a in audits(db, "teams.card.failed", s.file_id)] == [
        "bot_status_502"
    ]
    assert intents(db, n["notification_id"]) == []  # nothing minted for a card never shown
    clock.advance(11)
    db.execute("UPDATE pgmq.q_notify SET vt = now()")
    drain(connect, fakes, clock)
    n = notification(db, rid, p.identity_id)
    assert n["state"] == "sent" and n["failing_since"] is None


def test_a_recipient_who_loses_access_gets_one_last_card(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    s, p = setup(db, fakes)
    rid = start_recall(db, s, p)
    notify(db, rid)
    drain(connect, fakes, clock)
    conv = notification(db, rid, p.identity_id)["teams_conversation_id"]
    db.execute(
        "UPDATE catalog.identity SET group_sids = '{}', groups_resolved_at = %s"
        " WHERE identity_id = %s",
        (clock.t, p.identity_id),
    )
    clock.advance(11)
    set_state(db, rid, "rehydrating")
    notify(db, rid)
    drain(connect, fakes, clock)
    card = fakes.bot.card(conv)
    assert "You no longer have access" in json.dumps(card) and "actions" not in card
    n = notification(db, rid, p.identity_id)
    assert n["state"] == "skipped"
    assert intents(db, n["notification_id"])[-1][3] is True  # the old button is revoked
    clock.advance(11)
    set_state(db, rid, "available")
    notify(db, rid)
    drain(connect, fakes, clock)
    assert len(fakes.bot.latest(conv).versions) == 2


def test_people_without_entra_or_identity_are_skipped_and_audited(
    db, connect, fakes: Fakes
) -> None:
    clock = Clock()
    s = seed_scenario(db)
    ghost = add_person(db, groups=[s.readers_sid], resolved=False)  # not in the fake Graph
    rid = start_recall(db, s, ghost)
    db.execute(
        "INSERT INTO catalog.recall_requester (recall_id, uid, via, proof)"
        " VALUES (%s, 4242, 'nfs', 'claimed')",
        (rid,),
    )
    notify(db, rid)
    drain(connect, fakes, clock)
    notify(db, rid)
    drain(connect, fakes, clock)
    assert notification(db, rid, ghost.identity_id)["state"] == "skipped"
    reasons = sorted(a["details"]["reason"] for a in audits(db, "notify.skipped", s.file_id))
    assert reasons == ["no_identity", "not_in_entra"]  # once each
    assert fakes.bot.conversations == {} and fakes.graph.mail == []
    state = db.execute(
        "SELECT graph_state FROM catalog.identity WHERE identity_id = %s", (ghost.identity_id,)
    ).fetchone()[0]
    assert state == "not_found"


def test_owners_get_one_card_with_track(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    s, p = setup(db, fakes, policy={"notify": {"owners": True}})
    owners_gid = new_oid()
    fakes.graph.add_group(owners_gid, sid=s.owners_sid, displayName="Seismic owners")
    owner = add_person(db, groups=[s.readers_sid], resolved=False)
    graph_user(fakes, owner, groups=[s.readers_sid, s.owners_sid])
    # The group directory sync gives the owners group its objectId.
    run(_sync(connect, fakes, clock))
    rid = start_recall(db, s, p)
    notify(db, rid)
    drain(connect, fakes, clock)
    on = notification(db, rid, owner.identity_id)
    assert on["audience"] == "owner" and on["state"] == "sent"
    assert titles(fakes.bot.card(on["teams_conversation_id"])) == ["Track", "Open in portal"]
    clock.advance(11)
    set_state(db, rid, "rehydrating")
    notify(db, rid)
    drain(connect, fakes, clock)
    assert notification(db, rid, owner.identity_id)["card_version"] == 1  # not tracking
    assert notification(db, rid, p.identity_id)["card_version"] == 2


async def _sync(connect, fakes: Fakes, clock: Clock) -> int:
    conn = await connect()
    try:
        return await fakes.notifier.sync_group_directory(conn, clock.now())
    finally:
        await conn.close()


def test_identity_resolve_queue_and_group_directory(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    s = seed_scenario(db)
    p = add_person(db, groups=[], resolved=False)
    graph_user(fakes, p, groups=[s.readers_sid])
    db.execute(
        "SELECT pgmq.send('identity_resolve', %s::jsonb)",
        (json.dumps({"identity_id": str(p.identity_id)}),),
    )
    drain(connect, fakes, clock, "identity_resolve")
    row = db.execute(
        "SELECT entra_object_id::text, mail, graph_state, groups_source, group_sids::text[]"
        " FROM catalog.identity WHERE identity_id = %s",
        (p.identity_id,),
    ).fetchone()
    assert row[:4] == (p.oid, p.mail, "resolved", "graph")
    assert s.readers_sid in row[4]
    assert run(_sync(connect, fakes, clock)) >= 1
    kind = db.execute(
        "SELECT kind, entra_object_id IS NOT NULL FROM catalog.identity WHERE sid = %s",
        (s.readers_sid,),
    ).fetchone()
    assert kind == ("group", True)


# --- a recall whose file was replaced or deleted (migration 0059) ---------------------


def commit_version(
    c: psycopg.Connection, s, kind: str, *, actor: int | None = None, proof: str = "proven"
) -> int:
    """A new content version of the scenario's file, or its tombstone, committed as an
    edge's commit or unlink commits it: the catalog's own trigger ends the recall."""
    row = c.execute(
        "INSERT INTO catalog.file_version (file_id, dataset_id, parent_id, name, kind, state,"
        " committed_at, size, actor_via, actor_proof, actor_identity_id)"
        " VALUES (%s, %s, %s, 'line_1042.segy', %s, 'committed', now(), %s, %s, %s, %s)"
        " RETURNING version_id",
        (
            s.file_id,
            s.dataset_id,
            s.dir_id,
            kind,
            None if kind == "tombstone" else 4096,
            "smb" if actor else None,
            proof if actor else None,
            actor,
        ),
    ).fetchone()
    if kind == "tombstone":
        c.execute("UPDATE catalog.file SET deleted_at = now() WHERE file_id = %s", (s.file_id,))
    return row[0]


def test_a_replaced_files_requester_is_told_so_and_not_that_it_failed(
    db, connect, fakes: Fakes
) -> None:
    """The soak's recall 209. The commit that replaces the file ends the recall and
    queues the card itself; the card says what happened and offers no Recall."""
    clock = Clock()
    s, p = setup(db, fakes)
    rid = start_recall(db, s, p)
    notify(db, rid)
    drain(connect, fakes, clock)
    conv = notification(db, rid, p.identity_id)["teams_conversation_id"]
    set_state(db, rid, "downloading")
    assert db.execute("SELECT count(*) FROM pgmq.q_notify").fetchone()[0] == 0

    new = commit_version(db, s, "content")
    assert db.execute(
        "SELECT state, superseded_reason, superseded_by_version_id FROM catalog.recall"
        " WHERE recall_id = %s",
        (rid,),
    ).fetchone() == ("superseded", "replaced", new)
    # Queued by the commit, not by this test.
    assert db.execute("SELECT message FROM pgmq.q_notify").fetchall() == [
        ({"recall_id": str(rid)},)
    ]
    clock.advance(11)
    drain(connect, fakes, clock)
    card = fakes.bot.card(conv)
    text = json.dumps(card)
    assert card["body"][0]["text"] == "line\\_1042\\.segy was replaced"
    assert "This file was replaced while it was being recalled" in text
    assert "Couldn't recall" not in text and " by " not in text
    assert titles(card) == ["Open in portal"]
    n = notification(db, rid, p.identity_id)
    assert n["last_card_state"] == "superseded"
    assert intents(db, n["notification_id"])[0][3] is True  # Keep online is revoked
    assert [a["details"]["state"] for a in audits(db, "teams.card.updated", s.file_id)] == [
        "superseded"
    ]


def test_a_deleted_files_card_names_who_only_to_a_reader_and_only_on_proof(
    db, connect, fakes: Fakes
) -> None:
    clock = Clock()
    s, p = setup(db, fakes)
    deleter = add_person(db, groups=[s.readers_sid])
    db.execute(
        "UPDATE catalog.identity SET display_name = 'Ana Ruiz' WHERE identity_id = %s",
        (deleter.identity_id,),
    )
    rid = start_recall(db, s, p)
    notify(db, rid)
    drain(connect, fakes, clock)
    conv = notification(db, rid, p.identity_id)["teams_conversation_id"]
    commit_version(db, s, "tombstone", actor=deleter.identity_id)
    clock.advance(11)
    drain(connect, fakes, clock)
    card = fakes.bot.card(conv)
    assert card["body"][0]["text"] == "line\\_1042\\.segy was deleted"
    assert card["body"][1]["text"] == (
        "This file was deleted while it was being recalled, so the recall has stopped."
        " Deleted by Ana Ruiz."
    )

    # A uid an NFS client only claimed names no one.
    s2, p2 = setup(db, fakes)
    rid2 = start_recall(db, s2, p2)
    notify(db, rid2)
    drain(connect, fakes, clock)
    conv2 = notification(db, rid2, p2.identity_id)["teams_conversation_id"]
    commit_version(db, s2, "tombstone", actor=deleter.identity_id, proof="claimed")
    clock.advance(11)
    drain(connect, fakes, clock)
    text = json.dumps(fakes.bot.card(conv2))
    assert "was deleted while it was being recalled" in text and "Ana" not in text

    # Someone who can no longer read the file is told nothing about it, or who.
    s3, p3 = setup(db, fakes)
    rid3 = start_recall(db, s3, p3)
    notify(db, rid3)
    drain(connect, fakes, clock)
    conv3 = notification(db, rid3, p3.identity_id)["teams_conversation_id"]
    db.execute(
        "UPDATE catalog.identity SET group_sids = '{}', groups_resolved_at = %s"
        " WHERE identity_id = %s",
        (clock.t, p3.identity_id),
    )
    commit_version(db, s3, "tombstone", actor=deleter.identity_id)
    clock.advance(11)
    drain(connect, fakes, clock)
    text = json.dumps(fakes.bot.card(conv3))
    assert "You no longer have access" in text
    assert "Ana" not in text and "deleted" not in text


def test_a_superseded_recall_gets_its_one_last_email(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    s, p = setup(db, fakes)
    fakes.graph.blocked.add(p.oid)
    rid = start_recall(db, s, p)
    notify(db, rid)
    drain(connect, fakes, clock)
    commit_version(db, s, "content")
    clock.advance(11)
    drain(connect, fakes, clock)
    assert [m["subject"] for m in fakes.graph.mail] == [
        "Recalling line_1042.segy",
        "line_1042.segy was replaced",
    ]
    # And no second one, whatever comes after.
    notify(db, rid)
    clock.advance(11)
    drain(connect, fakes, clock)
    assert len(fakes.graph.mail) == 2
