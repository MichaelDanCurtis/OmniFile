"""Universal Actions invoke response bodies.

Shape per Microsoft's Universal Action Model: the HTTP status is 200 and the body is
``{"statusCode": n, "type": "...", "value": ...}``. The relay cannot mint new intents, so the
cards it returns have no buttons; later updates (rehydrating, downloading, available) are sent
by the service, which holds the bot credentials.
"""

from __future__ import annotations

CARD = "application/vnd.microsoft.card.adaptive"
MESSAGE = "application/vnd.microsoft.activity.message"
ERROR = "application/vnd.microsoft.error"

_DONE_TEXT = {
    "recall": "Recall requested. This card will update as the file comes back online.",
    "keep_online": "Done. This file will be kept online.",
    "track": "You're tracking this recall. This card will update as it progresses.",
}

# User-facing text for rejected clicks. Kept generic: no file names, no identities.
ERROR_TEXT = {
    "wrong_recipient": "This card was sent to someone else, so its buttons only work for them.",
    "intent_expired": "This card has expired. Use the OmniFile portal to recall the file.",
    "already_received": "We already have this request. This card will update as it progresses.",
    "rejected": "OmniFile couldn't do this. You may no longer have access to the file.",
    "unavailable": "OmniFile couldn't be reached. Please try again in a minute.",
    "invalid": "This card's request is not valid.",
}


def card_response(card: dict) -> dict:
    return {"statusCode": 200, "type": CARD, "value": card}


def message_response(text: str) -> dict:
    return {"statusCode": 200, "type": MESSAGE, "value": text}


def error_response(status: int, code: str) -> dict:
    return {
        "statusCode": status,
        "type": ERROR,
        "value": {"code": code, "message": ERROR_TEXT.get(code, ERROR_TEXT["invalid"])},
    }


def done_card(action: str) -> dict:
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": "OmniFile", "weight": "Bolder"},
            {"type": "TextBlock", "text": _DONE_TEXT[action], "wrap": True},
        ],
    }
