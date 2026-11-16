"""What the readers hand the seeder (docs/design/catalog-seeding.md)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..backends.base import LogicalTier


class SourceError(Exception):
    """A source can't be registered or read as configured (bad manifest,
    unsupported format, an overlapping prefix). Seeding stops with the
    message. `code` is the API's problem code for it (portal-api.md
    section 7): `invalid_request` for a bad request, `source_conflict` when
    the catalog's state refuses it."""

    def __init__(self, message: str, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SeedObject:
    """One current object version, as of the source's snapshot."""

    key: str
    version_id: str | None
    size: int
    last_modified: datetime
    native_class: str
    logical_tier: LogicalTier
    etag: str | None = None
    #: The ETag is the object's MD5 (single part, not SSE-KMS or SSE-C).
    etag_is_md5: bool = False


@dataclass(frozen=True)
class Batch:
    objects: Sequence[SeedObject]
    #: Where the next batch starts; None when the source is exhausted.
    resume: Mapping[str, Any] | None
    #: Rows read, including those that aren't current objects (old
    #: versions, delete markers), for the source's counters.
    rows_read: int = 0


class Reader(Protocol):
    async def snapshot_at(self) -> datetime: ...

    async def read(self, resume: Mapping[str, Any] | None, limit: int) -> Batch: ...
