"""The card renderer: buttons per state and audience (teams.md 3.2), no buttons without
intents, escaping, elision, size, and the email body."""

from __future__ import annotations

import html
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from omnifile_service.notify.cards import (
    MAX_CARD_BYTES,
    Audience,
    CardState,
    card_bytes,
    elide_path,
    email,
    md_escape,
    render,
)

T0 = datetime(2026, 10, 6, 9, 14, tzinfo=UTC)
BASE = CardState(
    recall_id=123,
    state="requested",
    file_name="line_1042.segy",
    share="long-term-storage",
    path="dataset/line_1042.segy",
    size=8 * 10**9,
    priority="standard",
    requested_at=T0,
    eta=T0 + timedelta(hours=15),
    portal_url="https://portal.omnifile.internal",
)
INTENTS = {"recall": "tok-recall", "keep_online": "tok-keep", "track": "tok-track"}


def titles(card: dict) -> list[str]:
    return [a["title"] for a in card.get("actions", [])]


@pytest.mark.parametrize(
    ("state", "requester", "owner"),
    [
        ("requested", ["Keep online", "Open in portal"], ["Track", "Open in portal"]),
        ("rehydrating", ["Keep online", "Open in portal"], ["Track", "Open in portal"]),
        ("copying", ["Keep online", "Open in portal"], ["Track", "Open in portal"]),
        ("downloading", ["Keep online", "Open in portal"], ["Track"]),
        ("available", ["Keep online"], []),
        ("failed", ["Recall", "Open in portal"], ["Recall"]),
        ("cancelled", ["Recall"], []),
        ("superseded", ["Open in portal"], []),
    ],
)
def test_buttons_per_state_and_audience(state, requester, owner) -> None:
    s = replace(BASE, state=state)
    assert titles(render(s, Audience.REQUESTER, INTENTS)) == requester
    assert titles(render(s, Audience.OWNER, INTENTS)) == owner
    assert titles(render(s, "watcher", INTENTS)) == owner


def test_execute_buttons_carry_the_intent_and_verb() -> None:
    card = render(replace(BASE, state="failed"), Audience.REQUESTER, INTENTS)
    recall = card["actions"][0]
    assert recall == {
        "type": "Action.Execute",
        "title": "Recall",
        "verb": "recall",
        "data": {"omnifile_intent": "tok-recall"},
    }
    assert card["actions"][1] == {
        "type": "Action.OpenUrl",
        "title": "Open in portal",
        "url": "https://portal.omnifile.internal/recalls/123",
    }
    assert card["version"] == "1.4" and "refresh" not in card


def test_api_cards_have_no_execute_buttons() -> None:
    for state in ("requested", "downloading", "available", "failed", "cancelled"):
        card = render(replace(BASE, state=state), Audience.REQUESTER, None)
        assert all(a["type"] == "Action.OpenUrl" for a in card.get("actions", []))
        assert "omnifile_intent" not in json.dumps(card)


def test_hold_and_tracking_hide_their_buttons() -> None:
    held = replace(BASE, state="available", hold_until=T0 + timedelta(days=14))
    card = render(held, Audience.REQUESTER, INTENTS)
    assert titles(card) == []
    assert "Kept online until 2026-10-20" in json.dumps(card)
    tracking = replace(BASE, state="rehydrating", tracking=True)
    assert titles(render(tracking, Audience.OWNER, INTENTS)) == ["Open in portal"]


def test_no_time_promised_above_10_gb() -> None:
    big = replace(BASE, size=184 * 10**9)
    text = json.dumps(render(big, Audience.REQUESTER, None))
    assert "No time is promised for files over 10 GB" in text
    assert "Expected by" not in text
    small = json.dumps(render(BASE, Audience.REQUESTER, None))
    assert "Expected by 2026-10-07 00:14 UTC (provider estimate)" in small


def test_failure_text_is_generic_and_identity_note() -> None:
    card = json.dumps(render(replace(BASE, state="failed"), Audience.REQUESTER, None))
    assert "The storage service couldn't restore it." in card
    assert "Open the file again to try again." in card


@pytest.mark.parametrize(
    ("failure", "want", "not_want"),
    [
        (
            "edge fill failed: io: No space left on device (os error 28)",
            "The file server ran out of disk space while copying it.",
            "storage service",
        ),
        (
            "edge fill failed: io: Disk quota exceeded (os error 122)",
            "The file server ran out of disk space while copying it.",
            "quota",
        ),
        (
            "edge fill failed: whole-file CRC-64/NVME 00ff != expected 0abc",
            "didn't match the stored file",
            "00ff",
        ),
        (
            "edge fill failed: inode changed under the fill",
            "The file server couldn't finish copying it.",
            "inode",
        ),
        (
            "edge unreachable: edge 3 has not been heard from for over 60 minutes",
            "The file server stopped responding while copying it.",
            "edge 3",
        ),
        (
            "edge not filling: edge 3 has reported no fill running for over 60 minutes",
            "The file server isn't copying it.",
            "stopped responding",
        ),
        ("rehydrate failed: 409 BlobArchived", "The storage service couldn't restore it.", "409"),
    ],
)
def test_failure_text_follows_the_reason_without_its_text(failure, want, not_want) -> None:
    """#52 review: a full edge disk is not blamed on the storage service, and
    the card says that opening the file again retries; never the error text
    itself (teams.md 3.2)."""
    s = replace(BASE, state="failed", failure=failure)
    for text in (json.dumps(render(s, Audience.REQUESTER, None)), html.unescape(email(s)[1])):
        assert want in text
        assert not_want not in text
        assert "Open the file again to try again." in text
    claimed = replace(BASE, claimed=True, claimed_from="10.1.2.3")
    text = json.dumps(render(claimed, Audience.REQUESTER, None))
    assert "Opened over NFS from 10\\\\.1\\\\.2\\\\.3" in text
    assert "claimed by the client, not proven" in text


@pytest.mark.parametrize(
    ("reason", "title", "sentence"),
    [
        (
            "replaced",
            "line\\_1042\\.segy was replaced",
            "This file was replaced while it was being recalled, so the recall has stopped.",
        ),
        (
            "deleted",
            "line\\_1042\\.segy was deleted",
            "This file was deleted while it was being recalled, so the recall has stopped.",
        ),
    ],
)
def test_a_superseded_recall_says_what_happened_not_that_it_failed(reason, title, sentence) -> None:
    """The soak's recall 209: its file was overwritten while it was recalled, and the
    requester was told "Couldn't recall". Nothing failed and nothing is to be tried
    again: the card says the file was replaced (or deleted), with no Recall button."""
    s = replace(BASE, state="superseded", superseded_reason=reason)
    card = render(s, Audience.REQUESTER, INTENTS)
    assert card["body"][0]["text"] == title
    assert card["body"][1]["text"] == sentence
    text = json.dumps(card)
    for wrong in ("Couldn't recall", "try again", "storage service", "file server", "cancelled"):
        assert wrong not in text
    assert titles(card) == ["Open in portal"]
    assert "Expected by" not in text and "No time is promised" not in text
    subject, body = email(s)
    assert subject == f"line_1042.segy was {reason}"
    assert sentence in html.unescape(body) and "try again" not in body
    # A Keep online hold is the file's and outlives the recall: the card still shows it.
    held = replace(s, hold_until=T0 + timedelta(days=14))
    facts = next(b for b in render(held, Audience.REQUESTER, INTENTS)["body"] if "facts" in b)
    assert {"title": "Kept online until", "value": "2026-10-20"} in facts["facts"]
    assert "Kept online until" in email(held)[1]


def test_a_superseded_card_names_who_only_when_it_is_given_one() -> None:
    """`facts.card_state` decides whether this recipient may be told; the renderer
    shows what it is given, escaped."""
    s = replace(BASE, state="superseded", superseded_reason="deleted")
    assert " by " not in json.dumps(render(s, Audience.REQUESTER, None))
    named = replace(s, superseded_by="Ana [Field] Ruiz")
    card = render(named, Audience.REQUESTER, None)
    assert card["body"][1]["text"].endswith("Deleted by Ana \\[Field\\] Ruiz.")
    assert "Deleted by Ana [Field] Ruiz." in html.unescape(email(named)[1])
    replaced = replace(named, superseded_reason="replaced")
    assert "Replaced by Ana" in render(replaced, Audience.REQUESTER, None)["body"][1]["text"]
    # Someone who can no longer read the file is told nothing about it.
    gone = json.dumps(render(replace(named, no_access=True), Audience.REQUESTER, None))
    assert "Ana" not in gone and "deleted" not in gone


def test_no_access_card_says_nothing_about_the_file() -> None:
    card = render(replace(BASE, no_access=True), Audience.REQUESTER, INTENTS)
    text = json.dumps(card)
    assert "You no longer have access to this file" in text
    assert "line_1042" not in text and "actions" not in card


def test_names_are_escaped() -> None:
    evil = replace(BASE, file_name="[click](https://evil.example)*\x1b.segy")
    text = json.dumps(render(evil, Audience.REQUESTER, None))
    assert "](https" not in text and "\\x1b" in text
    assert md_escape("a_b*c") == "a\\_b\\*c"


def test_long_paths_are_elided_and_cards_stay_small() -> None:
    long = "/".join(["d" * 200] * 40) + "/line_1042.segy"
    out = elide_path(long)
    assert "…" in out and out.endswith("line\\_1042\\.segy")
    card = render(replace(BASE, path=long, file_name="n" * 255), Audience.REQUESTER, INTENTS)
    assert card_bytes(card) <= MAX_CARD_BYTES


def test_email_body() -> None:
    subject, body = email(replace(BASE, file_name="<b>x</b>.segy"))
    assert subject == "Recalling <b>x</b>.segy"
    assert "&lt;b&gt;" in body and "<b>x</b>.segy" not in body.split("</p>", 1)[1]
    assert "https://portal.omnifile.internal/recalls/123" in body
    assert "omnifile_intent" not in body
    _, ready = email(replace(BASE, state="available"))
    assert "Ready on long-term-storage." in ready
