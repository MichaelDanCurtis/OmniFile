"""Small helpers shared by the retention and reconcile modules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import psycopg

from ..backends.base import Capabilities, LogicalTier

#: Index tag keys for WORM state (AGENTS.md: holds and state live in index
#: tags, because tags can be set on Archive-tier blobs and metadata cannot).
TAG_WORM = "omnifile_worm"  # none | unlocked | locked
TAG_HOLD = "omnifile_hold"  # 0 | 1
TAG_RETAIN = "omnifile_retain_until"  # ISO 8601 UTC, or absent
TAG_STATE = "omnifile_state"  # pruned: the catalog no longer needs this blob


class RetentionError(Exception):
    """A retention, WORM or depth request the catalog refuses."""


async def set_actor(conn: psycopg.AsyncConnection, actor: str) -> None:
    """Who is changing the catalog, for the audit triggers (0003). Must run
    inside the transaction that makes the change."""
    await conn.execute("SELECT set_config('omnifile.actor', %s, true)", (actor,))


def data_container(config: Mapping[str, Any]) -> str:
    """The container a dataset's blobs go to. Same rule as
    `api.data.upload_container`: `config.container`, else the live
    account's `data` container."""
    if config.get("container"):
        return str(config["container"])
    return f"{config.get('live_account', 'live')}/{config.get('data_container', 'data')}"


def native_to_logical(caps: Capabilities) -> dict[str, LogicalTier]:
    """Native class name (lower-cased) -> logical tier, from `capabilities()`."""
    out: dict[str, LogicalTier] = {}
    for logical, infos in caps.tiers.items():
        for info in infos:
            out.setdefault(info.native_class.lower(), logical)
    return out
