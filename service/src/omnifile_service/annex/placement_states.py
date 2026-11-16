"""Whether a placement is still going: one definition, for everything that asks.

A placement is open from the moment it is asked for: `requested` and `rehydrating` while its
recall takes its many hours, then `ready`, `writing`, `verifying`, `securing`, `placing` and
`stuck`. Open is defined as not ended, so a state added later counts as open, and nothing
that holds back for an open placement (a retention's purge, above all) goes ahead on it.
"""

from __future__ import annotations

ENDED = ("placed", "failed", "cancelled")


def open_sql(alias: str) -> str:
    """The condition, for a query. The same words as the unique index in migration 0045."""
    ended = ", ".join(f"'{s}'" for s in ENDED)
    return f"{alias}.state NOT IN ({ended})"
