"""A listing as a seed source, for a prefix without an inventory: the
backend's own listing, current versions only. On a versioned backend it
lists versions, so each location names the version it was seeded from."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..backends.base import ObjectHead, StorageBackend, VersionListing
from .model import Batch, SeedObject


class ListingReader:
    def __init__(self, backend: StorageBackend, container: str, prefix: str) -> None:
        self.backend = backend
        self.container = container
        self.prefix = prefix

    async def snapshot_at(self) -> datetime:
        return self.backend.clock.now()

    def _object(self, head: ObjectHead) -> SeedObject:
        return SeedObject(
            key=head.ref.key,
            version_id=head.ref.version_id,
            size=head.size,
            last_modified=head.last_modified or self.backend.clock.now(),
            native_class=head.native_class,
            logical_tier=head.logical_tier,
            etag=head.etag,
            # The seeder stores the ETag as the MD5 only when it is one: a
            # backend can know an object's MD5 and still give another ETag
            # (a multipart upload, SSE-KMS, the sim's Azure-style ETags).
            etag_is_md5=head.md5 is not None and head.etag == head.md5.hex(),
        )

    async def read(self, resume: Mapping[str, Any] | None, limit: int) -> Batch:
        cursor = (resume or {}).get("cursor")
        versioned = (
            isinstance(self.backend, VersionListing) and self.backend.capabilities().versioning
        )
        out: list[SeedObject] = []
        rows = 0
        while True:
            if versioned:
                vpage = await self.backend.list_all_versions(  # type: ignore[attr-defined]
                    self.container, self.prefix, cursor
                )
                rows += len(vpage.items)
                out += [self._object(v.head) for v in vpage.items if v.is_current]
                cursor = vpage.cursor
            else:
                page = await self.backend.list(self.container, self.prefix, cursor)
                rows += len(page.items)
                out += [self._object(h) for h in page.items]
                cursor = page.cursor
            if cursor is None:
                return Batch(out, None, rows)
            if len(out) >= limit:
                return Batch(out, {"cursor": cursor}, rows)
