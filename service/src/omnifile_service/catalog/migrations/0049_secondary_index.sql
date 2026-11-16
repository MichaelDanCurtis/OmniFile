-- migrate: no-transaction
-- Migration 0049: two partial indexes for decision 66 (wp26), each read a bounded
-- page at a time.
--
-- 1. `location_secondary_live`: find what a file's split left behind without reading
--    every row.
--
-- `retention.worm_sync.retire_replaced_secondaries` goes through secondaries in
-- location order, a batch at a time. On a table with few secondaries it reads only
-- those. After a migration they can be most of the table: the index then gives the
-- batches their order and a place to resume, and each batch is still bounded.
--
-- Runs outside a transaction (catalog/migrate.py, "No-transaction migrations"): the
-- index is built CONCURRENTLY, so edge commits go on during the build, and it may
-- run again after a failure part-way.

CREATE INDEX CONCURRENTLY IF NOT EXISTS location_secondary_live
    ON catalog.location (location_id)
    WHERE role = 'secondary' AND state IN ('pending', 'committed');

-- 2. `file_archive_due`: the files whose next step is Archive, by when they are due, for
--    the overdue count (`policy/overdue.py`). `file_next_action` holds every step, and
--    stale rows of other steps sort first in it, which could hide an overdue file from
--    a bounded scan.

CREATE INDEX CONCURRENTLY IF NOT EXISTS file_archive_due
    ON catalog.file (next_action_at)
    WHERE next_action = 'archive' AND deleted_at IS NULL;
