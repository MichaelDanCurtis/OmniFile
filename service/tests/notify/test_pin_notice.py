"""The card, or email, to whoever set a pin that a forced Archive overrode (decision 66)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import psycopg

from omnifile_service.notify import cards
from omnifile_service.worker.tiering import PIN_OVERRIDDEN

from .support import Fakes, add_person, audits, graph_user, seed_scenario
from .test_delivery import Clock, drain, titles


def send(c: psycopg.Connection, file_id: int, identity_id: int, tier: str = "hot") -> None:
    c.execute(
        "SELECT pgmq.send('notify', %s::jsonb)",
        (json.dumps({"kind": PIN_OVERRIDDEN, "file_id": str(file_id),
                     "identity_id": str(identity_id), "tier": tier,
                     "at": datetime.now(UTC).isoformat()}),),
    )  # fmt: skip


def setup(db, fakes: Fakes):
    db.execute("DELETE FROM pgmq.q_notify")
    s = seed_scenario(db)
    p = add_person(db, groups=[s.readers_sid], resolved=False)
    graph_user(fakes, p, groups=[s.readers_sid])
    return s, p


def sent_cards(fakes: Fakes) -> list:
    return [a for conv in fakes.bot.conversations for a in fakes.bot.activities(conv)]


def card_text(activity: object) -> str:
    return str(activity)


def test_the_setter_gets_one_card_that_says_what_happened(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    s, p = setup(db, fakes)
    send(db, s.file_id, p.identity_id)
    drain(connect, fakes, clock)
    [activity] = sent_cards(fakes)
    text = card_text(activity)
    assert cards.md_escape("line_1042.segy") in activity.card["body"][1]["text"]
    assert "Archive" in text and "hot" in text
    assert "recall" in text
    assert fakes.graph.mail == []
    [row] = audits(db, "pin.notice.teams", s.file_id)
    assert row["details"]["tier"] == "hot"
    assert db.execute("SELECT count(*) FROM pgmq.q_notify").fetchone()[0] == 0


def test_the_card_has_no_buttons_and_names_no_other_file() -> None:
    card = cards.pin_overridden_card("/seismic/a_b.segy", "seismic", "warm", "https://portal/")
    assert titles(card) == [cards.OPEN_TITLE]
    assert all(a["type"] == "Action.OpenUrl" for a in card["actions"])
    assert cards.md_escape("a_b.segy") in card["body"][1]["text"]
    plain = cards.pin_overridden_card("/x.segy", None, "warm", None)
    assert titles(plain) == []
    assert "another file" in json.dumps(plain)


def test_a_blocked_install_falls_back_to_email(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    s, p = setup(db, fakes)
    fakes.graph.blocked.add(p.oid)
    send(db, s.file_id, p.identity_id)
    drain(connect, fakes, clock)
    assert sent_cards(fakes) == []
    [mail] = fakes.graph.mail
    assert mail["to"] == [p.mail]
    assert mail["subject"] == "Your pin on this file wasn't kept"
    assert len(audits(db, "pin.notice.email", s.file_id)) == 1


def test_email_stays_off_where_the_policy_turned_it_off(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    db.execute("DELETE FROM pgmq.q_notify")
    s = seed_scenario(db, policy={"mode": "scheduled", "archive_day": 180,
                                  "notify": {"email_fallback": False}})  # fmt: skip
    p = add_person(db, groups=[s.readers_sid], resolved=False)
    graph_user(fakes, p, groups=[s.readers_sid])
    fakes.graph.blocked.add(p.oid)
    send(db, s.file_id, p.identity_id)
    drain(connect, fakes, clock)
    assert fakes.graph.mail == []
    assert len(audits(db, "pin.notice.skipped", s.file_id)) == 1


def test_bot_errors_retry_and_then_email(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    s, p = setup(db, fakes)
    fakes.bot.fail_next(502)
    send(db, s.file_id, p.identity_id)
    drain(connect, fakes, clock)
    assert sent_cards(fakes) == [] and fakes.graph.mail == []
    [(message,)] = db.execute("SELECT message FROM pgmq.q_notify").fetchall()
    assert message["since"]
    # Past the hour email takes over, as for a recall's card.
    fakes.bot.fail_next(502)
    clock.advance(3700)
    db.execute("UPDATE pgmq.q_notify SET vt = now()")
    drain(connect, fakes, clock)
    assert [m["to"] for m in fakes.graph.mail] == [[p.mail]]
    assert db.execute("SELECT count(*) FROM pgmq.q_notify").fetchone()[0] == 0


def test_a_gone_conversation_is_made_again_once(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    s, p = setup(db, fakes)
    send(db, s.file_id, p.identity_id)
    drain(connect, fakes, clock)
    fakes.bot.fail_next(404)
    send(db, s.file_id, p.identity_id)
    clock.advance(11)
    drain(connect, fakes, clock)
    assert len(audits(db, "pin.notice.teams", s.file_id)) == 2


def test_someone_who_cant_read_the_file_hears_nothing(db, connect, fakes: Fakes) -> None:
    """They set the pin when they could. The card would name a file they can't read now."""
    clock = Clock()
    s, _ = setup(db, fakes)
    outsider = add_person(db, groups=[], resolved=False)
    graph_user(fakes, outsider)
    send(db, s.file_id, outsider.identity_id)
    drain(connect, fakes, clock)
    assert sent_cards(fakes) == [] and fakes.graph.mail == []
    assert len(audits(db, "pin.notice.skipped", s.file_id)) == 1


def test_no_entra_and_no_mail_is_skipped_and_audited(db, connect, fakes: Fakes) -> None:
    clock = Clock()
    s = seed_scenario(db)
    db.execute("DELETE FROM pgmq.q_notify")
    ghost = add_person(db, groups=[s.readers_sid], resolved=True, mail=None)
    graph_user(fakes, ghost)
    fakes.graph.blocked.add(ghost.oid)
    send(db, s.file_id, ghost.identity_id)
    drain(connect, fakes, clock)
    assert fakes.graph.mail == []
    assert len(audits(db, "pin.notice.skipped", s.file_id)) == 1


def test_no_transaction_is_open_across_the_send(db, connect, fakes: Fakes) -> None:
    """The edge feed's horizon is the oldest open id, for every edge."""
    clock = Clock()
    s, p = setup(db, fakes)
    seen: list[int] = []
    real = fakes.notifier.bot.send

    async def watching(*args, **kw):
        (n,) = db.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            " AND backend_xid IS NOT NULL AND pid <> pg_backend_pid()"
        ).fetchone()
        seen.append(n)
        return await real(*args, **kw)

    fakes.notifier.bot.send = watching
    send(db, s.file_id, p.identity_id)
    drain(connect, fakes, clock)
    assert seen == [0]


def test_a_recalls_message_still_goes_the_recall_way(db, connect, fakes: Fakes) -> None:
    """Two kinds on one queue: a recall's has no `kind`."""
    from .support import start_recall
    from .test_delivery import notify

    clock = Clock()
    s, p = setup(db, fakes)
    rid = start_recall(db, s, p)
    notify(db, rid)
    send(db, s.file_id, p.identity_id)
    drain(connect, fakes, clock)
    assert len(sent_cards(fakes)) == 2
    assert len(audits(db, "teams.card.sent", s.file_id)) == 1
    assert len(audits(db, "pin.notice.teams", s.file_id)) == 1
