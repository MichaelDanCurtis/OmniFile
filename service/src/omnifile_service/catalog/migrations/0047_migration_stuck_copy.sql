-- Migration 0047: which objects the stuck-copy counts of migration 0046 are, so an ArchiveAdmin
-- can pick them (docs/contracts/portal-api.md 5.22).
--
-- The worker's progress sweep writes this each time it counts them, because only it can tell:
-- the count needs both verify queues, which `web` must never read, and whether the source runs
-- Batch, which only the worker's binding knows. `web` reads a page of it by key, and never
-- counts it. At most `migrate/progress.py` KEEP_KEYS keys per kind, the first by key.
--
-- stuck_counted_at: when the sweep last looked, at least hourly (`STUCK_EVERY`). A count of
-- the bytes doesn't change it, and NULL means it hasn't looked since the prefix last waited on
-- a check.
ALTER TABLE catalog.migration_count ADD COLUMN stuck_counted_at timestamptz;

CREATE TABLE catalog.migration_stuck_copy (
    migration_id bigint NOT NULL REFERENCES catalog.migration_prefix,
    kind         text NOT NULL CHECK (kind IN ('stranded', 'no_checksum')),
    object_key   text COLLATE "C" NOT NULL,
    PRIMARY KEY (migration_id, kind, object_key)
);
