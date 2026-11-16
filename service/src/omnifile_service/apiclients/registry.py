"""The API client registry in the catalog (`catalog.api_client`, `catalog.api_key`;
migration 0035), and the check every app credential passes (portal-api.md 2.2).

`admit_app` runs after an Entra app token is verified; `admit_key` checks an
OmniFile API key. Both answer the caller's `Principal`, with its roles cut to
the client's scopes (and a key's), and `api_client` set, so audit rows and
proposals record the client and its owner.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..api.db import Catalog
from ..api.validate import rfc3339
from ..web.context import Principal, etag
from ..web.problems import WebError
from . import keys

log = logging.getLogger("omnifile_service.apiclients")

SCOPES = ("Policy.Propose", "Recall.Request", "Archive.Read", "Archive.Adopt")
ACTOR = "system:web"
#: last_used_* is written at most this often per client or key, per replica.
TOUCH_EVERY = timedelta(seconds=60)

_CLIENT_COLS = (
    "c.api_client_id, c.app_id, c.display_name, c.scopes, c.owner_identity_id, c.state,"
    " c.created_by_identity_id, c.created_at, c.updated_at, c.disabled_at, c.revoked_at,"
    " c.last_used_at, c.entity_version"
)


class Touches:
    """When this replica last wrote last_used_* for a client or key."""

    def __init__(self) -> None:
        self._at: dict[str, tuple[datetime, str | None]] = {}

    def due(self, key: str, now: datetime, addr: str | None = None) -> bool:
        last = self._at.get(key)
        if last is not None and now - last[0] < TOUCH_EVERY and last[1] == addr:
            return False
        if len(self._at) > 10_000:
            self._at.clear()
        self._at[key] = (now, addr)
        return True


TOUCHES = Touches()


def _refuse_state(c: dict[str, Any]) -> None:
    if c["state"] != "enabled":
        raise WebError(
            "client_disabled",
            f"API client {c['display_name']!r} is {c['state']} in OmniFile's Settings, API clients",
        )


def _principal(c: dict[str, Any], roles: frozenset[str], key_id: str | None = None) -> Principal:
    return Principal(
        kind="app",
        roles=roles,
        app_id=c["app_id"],
        api_client={
            "api_client_id": c["api_client_id"],
            "display_name": c["display_name"],
            "app_id": c["app_id"],
            "owner_identity_id": c["owner_identity_id"],
        },
        key_id=key_id,
    )


async def admit_app(
    catalog: Catalog, app_id: str, token_roles: frozenset[str], now: datetime
) -> Principal:
    """An Entra app token's caller, if its application is registered and enabled."""
    async with catalog.read() as db:
        c = await (
            await db.execute(
                f"SELECT {_CLIENT_COLS} FROM catalog.api_client c WHERE app_id = %s", (app_id,)
            )
        ).fetchone()
    if c is None:
        raise WebError(
            "client_not_registered",
            "this application is not registered in OmniFile: an ArchiveAdmin registers it in"
            " Settings, API clients, with its scopes and owner",
        )
    _refuse_state(c)
    if TOUCHES.due(f"client:{c['api_client_id']}", now):
        async with catalog.tx(ACTOR) as db:
            await db.execute(
                "UPDATE catalog.api_client SET last_used_at = %s WHERE api_client_id = %s",
                (now, c["api_client_id"]),
            )
    return _principal(c, token_roles & frozenset(c["scopes"]))


class KeyRefusals:
    """Refused API keys, per caller address (review of #43), so junk keys cost
    neither the catalog nor the audit log without bound:

    - **a limiter on refusals:** each refusal takes a token from the address's
      bucket (`BURST`, refilled at `RATE` per second). From an address with none
      left, a refused key is answered `429 throttled` with no audit row, and a
      malformed one costs no catalog work. The key is parsed first and only
      refusals count, so a valid key from that address still gets in (re-review);
    - **aggregated audit rows:** at most one `api_key.refused` row per address,
      reason and key per `WINDOW` (unknown and malformed keys count as one
      key), carrying `count`, the refusals since the previous row.

    Per replica, as the other limits are."""

    RATE = 1.0
    BURST = 20.0
    WINDOW = 60.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._tokens: dict[str, tuple[float, float]] = {}
        self._rows: dict[tuple[str, str, str], tuple[float, int]] = {}

    def _level(self, addr: str, now: float) -> float:
        tokens, at = self._tokens.get(addr, (self.BURST, now))
        return min(self.BURST, tokens + (now - at) * self.RATE)

    def wait(self, addr: str | None) -> float | None:
        """Seconds until `addr` may try a key again, or None."""
        now = self.clock()
        level = self._level(addr or "-", now)
        return None if level >= 1 else (1 - level) / self.RATE

    def charge(self, addr: str | None) -> None:
        now = self.clock()
        a = addr or "-"
        self._tokens[a] = (self._level(a, now) - 1, now)
        if len(self._tokens) > 100_000:
            self._tokens = {a: self._tokens[a]}

    def audit_due(self, addr: str | None, reason: str, key_id: str | None) -> int | None:
        """The count to write now, or None while this window already has its row."""
        now = self.clock()
        grouped = reason in ("unknown", "malformed")
        k = (addr or "-", reason, "*" if grouped else (key_id or "-"))
        start, held = self._rows.get(k, (None, 0))  # type: ignore[assignment]
        if start is not None and now - start < self.WINDOW:
            self._rows[k] = (start, held + 1)
            return None
        if len(self._rows) > 100_000:
            self._rows.clear()
        self._rows[k] = (now, 0)
        return held + 1


async def _refused_key(
    catalog: Catalog,
    key_id: str | None,
    reason: str,
    addr: str | None,
    client_id: int | None = None,
    gate: KeyRefusals | None = None,
) -> WebError:
    """Audit a refused key (its public id only, aggregated per `KeyRefusals`) and
    answer 401."""
    count = 1
    if gate is not None:
        gate.charge(addr)
        due = gate.audit_due(addr, reason, key_id)
        if due is None:
            return WebError("unauthenticated", "API key rejected")
        count = due
    try:
        async with catalog.tx(ACTOR) as db:
            await db.execute(
                "INSERT INTO catalog.audit_log (actor, action, decision, client_addr, details)"
                " VALUES (%s, 'api_key.refused', 'deny', %s::inet, %s::jsonb)",
                (
                    f"key:{key_id}" if key_id else "key:unknown",
                    addr,
                    _json(
                        {
                            "key_id": key_id,
                            "reason": reason,
                            "count": count,
                            "api_client_id": str(client_id) if client_id else None,
                        }
                    ),
                ),
            )
    except Exception:  # noqa: BLE001 - a failed audit must not change the answer
        log.warning("could not audit a refused API key")
    return WebError("unauthenticated", "API key rejected")


def _json(v: Any) -> str:
    import json

    return json.dumps(v)


async def admit_key(
    catalog: Catalog,
    token: str,
    now: datetime,
    addr: str | None,
    gate: KeyRefusals | None = None,
) -> Principal:
    """An OmniFile API key's caller: the key exists, its secret matches, it is
    neither expired nor revoked, and its client is enabled.

    An address that keeps sending refused keys (`gate`) gets `429 throttled` for
    each further refusal instead of an audit row. The key is parsed first and only
    refusals count, so a valid key from that address still gets in (re-review of
    #43); a malformed one costs no catalog work, a well-formed one one lookup."""
    wait = gate.wait(addr) if gate is not None else None

    async def refuse(key_id: str | None, reason: str, client_id: int | None = None):
        if wait is not None:
            assert gate is not None
            gate.charge(addr)
            return WebError("throttled", "too many refused API keys",
                            retry_after_s=max(1, int(wait + 0.999)))  # fmt: skip
        return await _refused_key(catalog, key_id, reason, addr, client_id, gate)

    parsed = keys.parse(token)
    if parsed is None:
        raise await refuse(keys.key_id_of(token), "malformed")
    key_id, secret = parsed
    async with catalog.read() as db:
        row = await (
            await db.execute(
                f"SELECT k.key_id, k.secret_sha256, k.scopes AS key_scopes, k.expires_at,"
                f" k.revoked_at AS key_revoked_at, k.last_used_addr, {_CLIENT_COLS}"
                "  FROM catalog.api_key k JOIN catalog.api_client c USING (api_client_id)"
                " WHERE k.key_id = %s",
                (key_id,),
            )
        ).fetchone()
    if row is None:
        raise await refuse(key_id, "unknown")
    if not keys.matches(secret, row["secret_sha256"]):
        raise await refuse(key_id, "wrong_secret", row["api_client_id"])
    if row["key_revoked_at"] is not None:
        raise await refuse(key_id, "revoked", row["api_client_id"])
    if row["expires_at"] <= now:
        raise await refuse(key_id, "expired", row["api_client_id"])
    if row["state"] != "enabled":
        raise await refuse(key_id, f"client_{row['state']}", row["api_client_id"])
    if TOUCHES.due(f"key:{key_id}", now, addr):
        async with catalog.tx(ACTOR) as db:
            await db.execute(
                "UPDATE catalog.api_key SET last_used_at = %s, last_used_addr = %s::inet"
                " WHERE key_id = %s",
                (now, addr, key_id),
            )
            await db.execute(
                "UPDATE catalog.api_client SET last_used_at = %s WHERE api_client_id = %s",
                (now, row["api_client_id"]),
            )
    roles = frozenset(row["key_scopes"]) & frozenset(row["scopes"])
    return _principal(row, roles, key_id)


# --- reads, for Settings ------------------------------------------------------------


async def owner_json(db, identity_id: int) -> dict[str, Any]:
    o = await (
        await db.execute(
            "SELECT identity_id, display_name, upn, app_roles, app_roles_seen_at"
            "  FROM catalog.identity WHERE identity_id = %s",
            (identity_id,),
        )
    ).fetchone()
    if o is None:
        return {
            "identity_id": str(identity_id),
            "display_name": None,
            "upn": None,
            "roles": [],
            "roles_seen_at": None,
        }
    return {
        "identity_id": str(o["identity_id"]),
        "display_name": o["display_name"],
        "upn": o["upn"],
        "roles": list(o["app_roles"] or []),
        "roles_seen_at": rfc3339(o["app_roles_seen_at"]),
    }


def key_state(k: dict[str, Any], now: datetime) -> str:
    if k["revoked_at"] is not None:
        return "revoked"
    return "expired" if k["expires_at"] <= now else "active"


def key_json(k: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "key_id": k["key_id"],
        "scopes": list(k["scopes"]),
        "state": key_state(k, now),
        "created_at": rfc3339(k["created_at"]),
        "created_by": {
            "kind": "identity",
            "display_name": k.get("created_by_name"),
            "app_id": None,
            "identity_id": str(k["created_by_identity_id"]),
        },
        "expires_at": rfc3339(k["expires_at"]),
        "revoked_at": rfc3339(k["revoked_at"]),
        "rotated_from": k["rotated_from"],
        "last_used_at": rfc3339(k["last_used_at"]),
        "last_used_addr": None if k["last_used_addr"] is None else str(k["last_used_addr"]),
    }


async def client_json(db, c: dict[str, Any], now: datetime) -> dict[str, Any]:
    creator = await (
        await db.execute(
            "SELECT display_name FROM catalog.identity WHERE identity_id = %s",
            (c["created_by_identity_id"],),
        )
    ).fetchone()
    ks = await (
        await db.execute(
            "SELECT k.*, i.display_name AS created_by_name FROM catalog.api_key k"
            "  LEFT JOIN catalog.identity i ON i.identity_id = k.created_by_identity_id"
            " WHERE k.api_client_id = %s ORDER BY k.created_at DESC, k.key_id",
            (c["api_client_id"],),
        )
    ).fetchall()
    return {
        "api_client_id": str(c["api_client_id"]),
        "app_id": c["app_id"],
        "display_name": c["display_name"],
        "scopes": [s for s in SCOPES if s in (c["scopes"] or [])],
        "owner": await owner_json(db, c["owner_identity_id"]),
        "state": c["state"],
        "created_by": {
            "kind": "identity",
            "display_name": (creator or {}).get("display_name"),
            "app_id": None,
            "identity_id": str(c["created_by_identity_id"]),
        },
        "created_at": rfc3339(c["created_at"]),
        "updated_at": rfc3339(c["updated_at"]),
        "disabled_at": rfc3339(c["disabled_at"]),
        "revoked_at": rfc3339(c["revoked_at"]),
        "last_used_at": rfc3339(c["last_used_at"]),
        "etag": etag(c["entity_version"]),
        "keys": [key_json(k, now) for k in ks],
    }


async def load(db, api_client_id: int, *, lock: bool = False) -> dict[str, Any] | None:
    return await (
        await db.execute(
            f"SELECT {_CLIENT_COLS} FROM catalog.api_client c WHERE api_client_id = %s"
            + (" FOR UPDATE" if lock else ""),
            (api_client_id,),
        )
    ).fetchone()


async def ref_json(db, api_client_id: int | None, owner_identity_id: int | None):
    """`ApiClientRef` for a proposal (null when it came from no client)."""
    if api_client_id is None:
        return None
    c = await (
        await db.execute(
            "SELECT c.api_client_id, c.display_name, c.app_id, i.display_name AS owner_name"
            "  FROM catalog.api_client c"
            "  LEFT JOIN catalog.identity i ON i.identity_id = %s"
            " WHERE c.api_client_id = %s",
            (owner_identity_id, api_client_id),
        )
    ).fetchone()
    if c is None:
        return None
    return {
        "api_client_id": str(c["api_client_id"]),
        "display_name": c["display_name"],
        "app_id": c["app_id"],
        "owner": {
            "kind": "identity",
            "display_name": c["owner_name"],
            "app_id": None,
            "identity_id": str(owner_identity_id),
        },
    }


# --- approving an API client's proposal (review of #43) ------------------------------


async def answerable(db, p: dict[str, Any]) -> set[int]:
    """Who answers for an API client's proposal: the client's owner, who
    registered the client, and who made the key it came with. None of them may
    approve it (the catalog's `proposal_client_guard` refuses it too)."""
    if p.get("api_client_id") is None:
        return set()
    row = await (
        await db.execute(
            "SELECT c.created_by_identity_id AS registrant, k.created_by_identity_id AS key_maker"
            "  FROM catalog.api_client c LEFT JOIN catalog.api_key k ON k.key_id = %s"
            " WHERE c.api_client_id = %s",
            (p.get("key_id"), p["api_client_id"]),
        )
    ).fetchone()
    out = {int(p["owner_identity_id"])}
    for k in ("registrant", "key_maker"):
        if row and row[k] is not None:
            out.add(int(row[k]))
    return out


async def check_approvable(db, p: dict[str, Any], approver: int, now: datetime) -> None:
    """Refuse approving an API client's proposal when the approver answers for the
    client (`self_approval`), the client is no longer enabled (`client_disabled`),
    or its owner can no longer see the dataset (`owner_lacks_access`)."""
    if p.get("api_client_id") is None:
        return
    from .access import owner_can_see_dataset

    if approver in await answerable(db, p):
        raise WebError(
            "self_approval",
            "you answer for the API client this proposal came from (its owner, who registered"
            " it, or who made its key), so another ArchiveAdmin approves it",
        )
    c = await load(db, int(p["api_client_id"]))
    if c is None or c["state"] != "enabled":
        state = "gone" if c is None else c["state"]
        raise WebError("client_disabled", f"the API client this proposal came from is {state}")
    if not await owner_can_see_dataset(db, int(p["owner_identity_id"]), p["dataset_id"], now):
        raise WebError(
            "owner_lacks_access",
            "the API client's owner can no longer see this dataset, so its proposal can't be"
            " approved",
        )
