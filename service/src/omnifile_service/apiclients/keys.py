"""OmniFile-issued API keys (owner decision, 2026-11-04).

Entra app identities stay the recommended way in: nothing to store, rotate or
leak on our side. A key is the alternative for a client that can't hold an
Entra credential.

- **Form:** ``ofk1.<key_id>.<secret>``. ``key_id`` is 16 hex characters
  (8 random bytes), public, for lookup and for the audit. ``secret`` is
  32 random bytes, base64url without padding (43 characters).
- **Kept as a hash.** The catalog keeps the SHA-256 of the secret (a 256-bit
  random value needs no slow hash), compared in constant time. The key is in
  the creating response only.
- **Sent only as** ``Authorization: Bearer ofk1.…``. A key anywhere in the
  query string is refused before anything reads it, and `RedactKeys` takes
  any key out of every log line (uvicorn's access log included).
- **Expiry:** 90 days by default, at most 365. Rotation makes a new key and
  shortens the old one's life to an overlap (7 days by default).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

PREFIX = "ofk1"
DEFAULT_TTL = timedelta(days=90)
MAX_TTL = timedelta(days=365)
DEFAULT_OVERLAP = timedelta(days=7)
#: A whole key, anywhere in a string (for redaction and query checks).
_ANY_KEY = re.compile(r"ofk1[^\s\"'&;,]*")
_KEY = re.compile(r"^ofk1\.([0-9a-f]{16})\.([A-Za-z0-9_-]{43})$")
KEY_ID = re.compile(r"^[0-9a-f]{16}$")
#: A key's head in a query string (checked raw and percent-decoded).
_IN_QUERY = re.compile(r"ofk1\.[0-9a-f]{16}")
REDACTED = "ofk1.[redacted]"


@dataclass(frozen=True)
class NewKey:
    key_id: str
    #: The whole key, for the creating response only.
    key: str
    secret_sha256: bytes


def generate() -> NewKey:
    key_id = secrets.token_bytes(8).hex()
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    return NewKey(key_id, f"{PREFIX}.{key_id}.{secret}", digest(secret))


def digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def looks_like_key(token: str) -> bool:
    return token.startswith(PREFIX + ".")


def parse(token: str) -> tuple[str, str] | None:
    """(key_id, secret), or None for anything not shaped like a key."""
    m = _KEY.match(token)
    return (m[1], m[2]) if m else None


def key_id_of(token: str) -> str | None:
    """The public id of a malformed or unknown key, for the audit: only when
    it is shaped like one (16 hex characters)."""
    parts = token.split(".")
    return parts[1] if len(parts) >= 2 and KEY_ID.match(parts[1]) else None


def matches(secret: str, stored: bytes) -> bool:
    return hmac.compare_digest(digest(secret), bytes(stored))


def in_query(query_string: str) -> bool:
    """A key in a query string (raw or percent-encoded): refused, never read."""
    from urllib.parse import unquote

    q = query_string
    for _ in range(3):
        if _IN_QUERY.search(q):
            return True
        q = unquote(q)
    return False


def redact(text: str) -> str:
    return _ANY_KEY.sub(REDACTED, text) if "ofk1" in text else text


def _decoded(text: str) -> str:
    from urllib.parse import unquote

    for _ in range(3):
        nxt = unquote(text)
        if nxt == text:
            break
        text = nxt
    return text


class RedactKeys(logging.Filter):
    """Takes any API key out of a log record: its message and its arguments.

    - uvicorn's access lines lose their query string altogether (review of #43): a
      key there may be percent-encoded in any way;
    - any other line holding a key, raw or percent-encoded, is written decoded,
      with the key redacted."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) >= 3:
            path = args[2]
            if isinstance(path, str) and "?" in path:
                record.args = (*args[:2], path.split("?", 1)[0], *args[3:])
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string is not ours to fix
            return True
        if "ofk1" in msg:
            record.msg = redact(msg)
            record.args = ()
        elif "%" in msg and "ofk1" in (plain := _decoded(msg)):
            record.msg = redact(plain)
            record.args = ()
        return True


def install_redaction(*loggers: str) -> RedactKeys:
    """On the root logger's handlers and on the named loggers and their
    handlers (``uvicorn.access``, ``uvicorn.error``), so no key reaches a log
    line whichever logger writes it."""
    f = RedactKeys()
    targets = [logging.getLogger(), *(logging.getLogger(n) for n in loggers)]
    for lg in targets:
        if f not in lg.filters:
            lg.addFilter(f)
        for h in lg.handlers:
            if f not in h.filters:
                h.addFilter(f)
    return f
