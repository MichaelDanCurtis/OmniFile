"""A migration source is read-only to OmniFile (catalog-seeding.md
section 10).

The source bucket is the client's. OmniFile reads it, restores archived
objects in it (decision 15) and reports what users deleted; it never
deletes, overwrites, re-tiers (an S3 class change is an in-place
CopyObject, a new object version), re-tags or rewrites the metadata of an
object there. That holds before the migration starts (as soon as the bucket
is a registered source), while it runs and after the cutover.

So every write path asks one question, `on_source`, before any provider
call:

- retention: a pruned version's bytes are kept at the source (its location
  stays ``committed``; the version is still ``pruned``), and `delete_blob`
  refuses one;
- tiering and pins: the scheduler doesn't move a file whose primary copy is
  on a source; it waits for the cutover, when the verified Azure copy
  becomes the primary and the policy acts on it;
- WORM and legal holds: the catalog alone records them for a source object;
  no retention, hold or tag is stamped on it;
- and the catalog refuses to mark a source location ``deleting``
  (migration 0026, `catalog.location_source_guard`).

`S3Backend` refuses the same writes itself, outside OmniFile's own areas
(the staging prefix and the Batch Operations manifests), as a second line.
"""

from __future__ import annotations

import psycopg


def on_source(alias: str) -> str:
    """SQL: location `alias` is in the bucket of a registered migration
    source (a source on another backend than its dataset's own)."""
    return f"catalog.on_source({alias}.backend_id, {alias}.container)"


async def location_on_source(conn: psycopg.AsyncConnection, location_id: int) -> bool:
    cur = await conn.execute(
        "SELECT catalog.on_source(backend_id, container) FROM catalog.location"
        " WHERE location_id = %s",
        (location_id,),
    )
    row = await cur.fetchone()
    return bool(row and row[0])


__all__ = ["location_on_source", "on_source"]
