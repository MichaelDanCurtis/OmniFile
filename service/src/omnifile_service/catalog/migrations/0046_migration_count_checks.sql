-- Migration 0046: copies whose check won't end by itself (docs/design/migration-engine.md
-- section 5). The worker's progress sweep counts them, as it does `failing_now`, so the
-- console and the migrations API read a number, and `web` never reads a queue: a dead letter
-- holds provider text and object keys.
--
-- copies_given_up:    their check went to the migrate_verify dead letters.
-- copies_stranded:    the S3 object's checksum is known, and nothing is queued to check them.
-- copies_no_checksum: the S3 object has no checksum on record, and nothing is computing one.
ALTER TABLE catalog.migration_count
    ADD COLUMN copies_given_up bigint NOT NULL DEFAULT 0,
    ADD COLUMN copies_stranded bigint NOT NULL DEFAULT 0,
    ADD COLUMN copies_no_checksum bigint NOT NULL DEFAULT 0;
