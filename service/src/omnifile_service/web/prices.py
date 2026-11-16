"""Who sees prices (Settings, Prices and alerts; portal-api.md section 5.24).

`catalog.price_display.show_to` lists the portal roles that see costs:
`ArchiveAdmin`, `DataSteward` and `others` (every other signed-in user). A
user sees prices when any of their roles is listed; a user with neither admin
role counts as `others`. Partner applications always do: their API contract
carries the amounts.

For a caller who doesn't, the endpoint wrapper (`context.endpoint`) removes
every amount from the answer: each money object (`{"amount", "currency"}`,
optionally `"high"`) becomes null, and a list of them loses its entries. One
rule covers every route that prices something (the policy preview, recall
options, pin previews, checks, usage), so a new route can't forget it. `GET
/v1/price-tables/{kind}` is refused instead (`403 forbidden`), except to
ArchiveAdmins, whose price editor shows the rates it edits.

The setting is cached per replica for `TTL_S`; the replica that changes it
forgets its copy at once.
"""

from __future__ import annotations

import time
from typing import Any

ROLES = ("ArchiveAdmin", "DataSteward", "others")
TTL_S = 10.0


def is_money(v: Any) -> bool:
    return (
        isinstance(v, dict)
        and "amount" in v
        and "currency" in v
        and set(v) <= {"amount", "currency", "high"}
        and isinstance(v["amount"], str)
    )


def has_money(v: Any) -> bool:
    if is_money(v):
        return True
    if isinstance(v, dict):
        return any(has_money(x) for x in v.values())
    if isinstance(v, list):
        return any(has_money(x) for x in v)
    return False


def scrub(v: Any) -> Any:
    """`v` with every money object removed (null in an object, dropped from a list)."""
    if is_money(v):
        return None
    if isinstance(v, dict):
        return {k: scrub(x) for k, x in v.items()}
    if isinstance(v, list):
        return [scrub(x) for x in v if not is_money(x)]
    return v


def role_keys(roles: frozenset[str] | set[str]) -> list[str]:
    keys = [r for r in ("ArchiveAdmin", "DataSteward") if r in roles]
    return keys or ["others"]


def visible_for(who: Any, show_to: list[str] | tuple[str, ...]) -> bool:
    if who.is_app:
        return True
    return any(k in show_to for k in role_keys(who.roles))


class PriceDisplay:
    """The setting, cached for a few seconds per replica."""

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self._value: list[str] | None = None
        self._at = 0.0

    def forget(self) -> None:
        self._value = None

    async def show_to(self, catalog) -> list[str]:
        now = self.clock()
        if self._value is not None and now - self._at < TTL_S:
            return self._value
        async with catalog.read() as db:
            row = await (await db.execute("SELECT show_to FROM catalog.price_display")).fetchone()
        self._value = list(row["show_to"]) if row else list(ROLES)
        self._at = now
        return self._value

    async def visible(self, catalog, who: Any) -> bool:
        if who.is_app:
            return True
        return visible_for(who, await self.show_to(catalog))
