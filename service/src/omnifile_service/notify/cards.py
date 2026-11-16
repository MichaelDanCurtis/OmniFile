"""The one card renderer (teams.md section 3), and the email body (section 8).

`render(state, audience, intents)` is pure. The worker passes `intents` (`{act: token}`) and
gets `Action.Execute` buttons; `api` passes ``None`` and gets none, because it holds only the
intent public keys. Both get the same text, so the invoke reply and the worker's later update
look alike.

Nothing secret is shown: intents live only in `data`, and the only id in visible text is the
recall id (in the portal link). File names and path segments are escaped, so a name can't
format the card or inject a link.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from ..api.recalls import note_safe_name
from .intents import DATA_KEY

SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
VERSION = "1.4"  # the Universal Actions minimum
MAX_CARD_BYTES = 20 * 1024
MAX_PATH_CHARS = 200
NO_PROMISE_BYTES = 10 * 10**9  # AGENTS.md: never promise a time above 10 GB

ACTIVE = ("requested", "rehydrating", "copying", "downloading")
STATES = (*ACTIVE, "available", "failed", "cancelled", "superseded")

BUTTON_TITLES = {"recall": "Recall", "keep_online": "Keep online", "track": "Track"}
OPEN_TITLE = "Open in portal"


class Audience(StrEnum):
    REQUESTER = "requester"
    OWNER = "owner"
    WATCHER = "watcher"


@dataclass(frozen=True)
class CardState:
    """What a card shows. Built from the catalog by `facts.card_state`."""

    recall_id: int
    state: str
    file_name: str
    share: str | None
    path: str  # within the share, "/"-separated, no leading slash
    size: int
    priority: str
    requested_at: datetime
    eta: datetime | None = None
    hold_until: datetime | None = None
    tracking: bool = False
    #: The requester's identity was only claimed (NFS sec=sys): from this address.
    claimed_from: str | None = None
    claimed: bool = False
    cancelled_by_recipient: bool = False
    #: The recipient can no longer read the file: the last card they get.
    no_access: bool = False
    portal_url: str | None = None
    # catalog.recall.failure: only its kind is shown, never its text
    failure: str | None = None
    #: `superseded`: why the recall stopped (`replaced` or `deleted`), and who did it,
    #: when the catalog knows and this recipient may be told (facts.card_state).
    superseded_reason: str | None = None
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unknown recall state {self.state!r}")

    @property
    def key(self) -> str:
        """Everything a card shows that can change after it was sent, for coalescing."""
        hold = self.hold_until.isoformat() if self.hold_until else "-"
        eta = self.eta.isoformat() if self.eta else "-"
        return "|".join(
            [self.state, hold, "t" if self.tracking else "-", "x" if self.no_access else "-", eta]
        )


# --- text helpers ------------------------------------------------------------

_MD = set("\\`*_{}[]()#+-.!|>~<")


def md_escape(text: str) -> str:
    """Control characters as `\\xNN` (as in the recall note), then Markdown escaped."""
    safe = note_safe_name(text)
    return "".join("\\" + ch if ch in _MD else ch for ch in safe)


def elide_path(path: str, limit: int = MAX_PATH_CHARS) -> str:
    """Each segment escaped; at most `limit` characters of the raw path, the middle elided."""
    if len(path) > limit:
        keep = limit - 1
        head = keep // 2
        path = path[:head] + "…" + path[len(path) - (keep - head) :]
    return "/".join(md_escape(seg) if seg != "…" else seg for seg in path.split("/"))


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{n} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{n} bytes"  # pragma: no cover


def utc(t: datetime | None) -> str:
    return "" if t is None else t.strftime("%Y-%m-%d %H:%M UTC")


def day(t: datetime) -> str:
    return "further notice" if t.year == 9999 else t.strftime("%Y-%m-%d")


def title(s: CardState) -> str:
    name = md_escape(s.file_name)
    if s.no_access:
        return "You no longer have access to this file"
    if s.state in ACTIVE:
        return f"Recalling {name}"
    if s.state == "available":
        return f"{name} is ready"
    if s.state == "failed":
        return f"Couldn't recall {name}"
    if s.state == "superseded":
        return f"{name} was {superseded_word(s)}"
    return "Recall cancelled"


def plain_title(s: CardState) -> str:
    """The activity summary and email subject: no Markdown escaping."""
    name = note_safe_name(s.file_name)
    if s.no_access:
        return "You no longer have access to a recalled file"
    if s.state in ACTIVE:
        return f"Recalling {name}"
    if s.state == "available":
        return f"{name} is ready"
    if s.state == "failed":
        return f"Couldn't recall {name}"
    if s.state == "superseded":
        return f"{name} was {superseded_word(s)}"
    return "Recall cancelled"


def superseded_word(s: CardState) -> str:
    return "deleted" if s.superseded_reason == "deleted" else "replaced"


def superseded_text(s: CardState, *, markdown: bool = True) -> str:
    """Why a recall stopped without the file: someone replaced or deleted it while it
    was coming back. Not a failure, and nothing to try again: a replaced file's new
    bytes are on the share. Who did it is named only when `facts.card_state` found
    that this recipient may know."""
    word = superseded_word(s)
    text = f"This file was {word} while it was being recalled, so the recall has stopped."
    if s.superseded_by:
        who = md_escape(s.superseded_by) if markdown else note_safe_name(s.superseded_by)
        text += f" {word.capitalize()} by {who}."
    return text


RETRY_TEXT = "Open the file again to try again."


def failure_text(s: CardState) -> str:
    """Why a recall failed, in general terms (teams.md section 3.2: never the
    provider's or the edge's error text), and that opening the file again
    retries: the next read starts a new recall. A fill the file server
    itself could not finish (control API 4.4, "failed") is not blamed on
    the storage service."""
    f = s.failure or ""
    if f.startswith("edge unreachable:"):
        why = "The file server stopped responding while copying it."
    elif f.startswith("edge not filling:"):
        why = "The file server isn't copying it."
    elif f.startswith("edge fill failed:"):
        if any(
            s in f for s in ("No space left", "os error 28", "Disk quota exceeded", "os error 122")
        ):
            why = "The file server ran out of disk space while copying it."
        elif "CRC-64" in f:
            why = "The copy on the file server didn't match the stored file, so it wasn't used."
        else:
            why = "The file server couldn't finish copying it."
    else:
        why = "The storage service couldn't restore it."
    return f"{why} {RETRY_TEXT}"


def step_text(s: CardState) -> str:
    share = md_escape(s.share) if s.share else "the file server"
    return {
        "requested": "Step 1 of 4: Requested.",
        "rehydrating": "Step 2 of 4: Coming back from Archive.",
        "copying": "Step 2 of 4: Coming back from Archive.",
        "downloading": "Step 3 of 4: Copying to the file server.",
        "available": f"Step 4 of 4: Ready on {share}.",
        "failed": failure_text(s),
        "cancelled": (
            "You cancelled this recall."
            if s.cancelled_by_recipient
            else "An administrator cancelled this recall."
        ),
        "superseded": superseded_text(s),
    }[s.state]


def eta_text(s: CardState) -> str | None:
    if s.state not in ("requested", "rehydrating", "copying"):
        return None
    if s.size > NO_PROMISE_BYTES:
        return "No time is promised for files over 10 GB."
    if s.eta is None:
        return "No time is promised yet."
    return f"Expected by {utc(s.eta)} (provider estimate)."


def identity_note(s: CardState) -> str | None:
    if not s.claimed:
        return None
    where = f" from {md_escape(s.claimed_from)}" if s.claimed_from else ""
    return f"Opened over NFS{where}. NFS sec=sys identities are claimed by the client, not proven."


def portal_link(s: CardState) -> str | None:
    if not s.portal_url:
        return None
    return f"{s.portal_url.rstrip('/')}/recalls/{s.recall_id}"


def buttons(s: CardState, audience: Audience) -> list[str]:
    """The buttons the table in teams.md section 3.2 gives: action names, plus "open"."""
    if s.no_access:
        return []
    requester = audience is Audience.REQUESTER
    keep = [] if s.hold_until is not None else ["keep_online"]
    track = [] if s.tracking else ["track"]
    st = s.state
    if st in ("requested", "rehydrating", "copying"):
        return [*keep, "open"] if requester else [*track, "open"]
    if st == "downloading":
        return [*keep, "open"] if requester else track
    if st == "available":
        return keep if requester else []
    if st == "failed":
        return ["recall", "open"] if requester else ["recall"]
    if st == "superseded":
        # Nothing to recall: a replaced file's bytes are new, a deleted one is gone.
        return ["open"] if requester else []
    return ["recall"] if requester else []  # cancelled


def actions_needing_intents(s: CardState, audience: Audience) -> list[str]:
    return [b for b in buttons(s, audience) if b != "open"]


# --- the card ------------------------------------------------------------------


def _facts(s: CardState, path_limit: int) -> list[dict[str, str]]:
    facts = []
    if s.share:
        facts.append({"title": "Share", "value": md_escape(s.share)})
    facts += [
        {"title": "Path", "value": elide_path(s.path, path_limit)},
        {"title": "Size", "value": human_size(s.size)},
        {"title": "Priority", "value": s.priority.capitalize()},
        {"title": "Requested", "value": utc(s.requested_at)},
    ]
    if s.hold_until is not None:
        facts.append({"title": "Kept online until", "value": day(s.hold_until)})
    return facts


def _build(
    s: CardState, audience: Audience, intents: Mapping[str, str] | None, path_limit: int
) -> dict[str, Any]:
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": title(s), "weight": "Bolder", "size": "Medium", "wrap": True}
    ]
    if s.no_access:
        body.append(
            {
                "type": "TextBlock",
                "text": "Your access changed, so OmniFile won't send more updates about it.",
                "wrap": True,
            }
        )
        return _card(body, [])
    body.append({"type": "TextBlock", "text": step_text(s), "wrap": True})
    eta = eta_text(s)
    if eta:
        body.append({"type": "TextBlock", "text": eta, "wrap": True, "isSubtle": True})
    if s.state == "available" and s.hold_until is not None:
        body.append(
            {"type": "TextBlock", "text": f"Kept online until {day(s.hold_until)}.", "wrap": True}
        )
    if s.tracking and audience is not Audience.REQUESTER:
        body.append({"type": "TextBlock", "text": "You're tracking this recall.", "isSubtle": True})
    body.append({"type": "FactSet", "facts": _facts(s, path_limit)})
    note = identity_note(s)
    if note:
        body.append({"type": "TextBlock", "text": note, "wrap": True, "isSubtle": True})
    actions: list[dict[str, Any]] = []
    for b in buttons(s, audience):
        if b == "open":
            url = portal_link(s)
            if url:
                actions.append({"type": "Action.OpenUrl", "title": OPEN_TITLE, "url": url})
            continue
        token = (intents or {}).get(b)
        if token:
            actions.append(
                {
                    "type": "Action.Execute",
                    "title": BUTTON_TITLES[b],
                    "verb": b,
                    "data": {DATA_KEY: token},
                }
            )
    return _card(body, actions)


def _card(body: list[dict[str, Any]], actions: list[dict[str, Any]]) -> dict[str, Any]:
    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": SCHEMA,
        "version": VERSION,
        "body": body,
    }
    if actions:
        card["actions"] = actions
    return card


def card_bytes(card: Mapping[str, Any]) -> int:
    return len(json.dumps(card, separators=(",", ":"), ensure_ascii=False).encode())


def render(
    state: CardState, audience: Audience | str, intents: Mapping[str, str] | None
) -> dict[str, Any]:
    """The Adaptive Card for `state` as `audience` sees it.

    `intents` maps an action (`recall`, `keep_online`, `track`) to its signed token; an
    action without one gets no button, and ``None`` means no `Action.Execute` at all.
    Long paths are elided until the card is within `MAX_CARD_BYTES`.
    """
    audience = Audience(audience)
    limit = MAX_PATH_CHARS
    while True:
        card = _build(state, audience, intents, limit)
        if card_bytes(card) <= MAX_CARD_BYTES or limit <= 20:
            return card
        limit //= 2


# --- a pin that wasn't kept (decision 66) -----------------------------------------


def pin_overridden_text(path: str, share: str | None, tier: str) -> tuple[str, str]:
    """Title and sentence, in plain words. It names no other file, not even that it exists
    by name: the other file may be one this person can't read."""
    return (
        "Your pin on this file wasn't kept",
        f"You pinned {note_safe_name(path)} on {share or 'the file server'} to {tier}. It has "
        "gone to Archive early: it shares its storage with another file whose archive date "
        "arrived, and neither could get a copy of its own. To read it, recall it.",
    )


def pin_overridden_card(
    path: str, share: str | None, tier: str, portal_url: str | None
) -> dict[str, Any]:
    """No buttons: nothing here needs an intent, and a recall starts as any other does."""
    heading, sentence = pin_overridden_text(path, share, tier)
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": md_escape(heading), "weight": "Bolder", "wrap": True},
        {"type": "TextBlock", "text": md_escape(sentence), "wrap": True},
    ]
    actions: list[dict[str, Any]] = []
    if portal_url:
        actions.append(
            {"type": "Action.OpenUrl", "title": OPEN_TITLE, "url": portal_url.rstrip("/")}
        )
    return _card(body, actions)


def pin_overridden_email(path: str, share: str | None, tier: str) -> tuple[str, str]:
    heading, sentence = pin_overridden_text(path, share, tier)
    return heading, f"<p><b>{html.escape(heading)}</b></p><p>{html.escape(sentence)}</p>"


# --- email ---------------------------------------------------------------------

EMAIL_STATES = ("requested", "available", "failed")


def email(state: CardState) -> tuple[str, str]:
    """Subject and HTML body: the card's facts and a portal link, no buttons, no intents."""
    e = html.escape
    s = state
    lines = [f"<p><b>{e(plain_title(s))}</b></p>"]
    step = {
        "requested": "Step 1 of 4: Requested.",
        "rehydrating": "Step 2 of 4: Coming back from Archive.",
        "copying": "Step 2 of 4: Coming back from Archive.",
        "downloading": "Step 3 of 4: Copying to the file server.",
        "available": f"Ready on {s.share or 'the file server'}.",
        "failed": failure_text(s),
        "cancelled": "The recall was cancelled.",
        "superseded": superseded_text(s, markdown=False),
    }[s.state]
    lines.append(f"<p>{e(step)}</p>")
    if s.state == "requested":
        if s.size > NO_PROMISE_BYTES:
            lines.append("<p>No time is promised for files over 10 GB.</p>")
        elif s.eta is not None:
            lines.append(f"<p>Expected by {e(utc(s.eta))} (provider estimate).</p>")
    rows = [("Share", s.share or ""), ("Path", note_safe_name(s.path)[:MAX_PATH_CHARS])]
    rows += [("Size", human_size(s.size)), ("Requested", utc(s.requested_at))]
    if s.hold_until is not None:
        rows.append(("Kept online until", day(s.hold_until)))
    lines.append(
        "<table>"
        + "".join(f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in rows)
        + "</table>"
    )
    if s.claimed:
        where = f" from {s.claimed_from}" if s.claimed_from else ""
        lines.append(
            f"<p>Opened over NFS{e(where)}. NFS sec=sys identities are claimed by the client,"
            " not proven.</p>"
        )
    link = portal_link(s)
    if link:
        lines.append(f'<p><a href="{e(link, quote=True)}">Open in the OmniFile portal</a></p>')
    return plain_title(s), "\n".join(lines)
