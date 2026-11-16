"""The daily usage snapshot behind GET /v1/usage (usage/__init__.py), and the
share map's roll-up (usage/share_map.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import psycopg

from ..usage import share_map, snapshot
from .jobs import Job, SweepJob

if TYPE_CHECKING:
    from .runner import Worker

EVERY = timedelta(hours=24)
#: The share map's roll-up: the folders that changed, recounted each minute in
#: short, bounded transactions (usage/share_map.py).
SHARE_MAP_EVERY = timedelta(minutes=1)


def usage_jobs(worker: Worker) -> list[Job]:
    async def count(conn: psycopg.AsyncConnection, now: datetime) -> int:
        async with conn.transaction():
            return await snapshot(conn, now)

    return [
        SweepJob("usage_snapshot", EVERY, count),
        SweepJob("share_map", SHARE_MAP_EVERY, share_map.refresh),
    ]
