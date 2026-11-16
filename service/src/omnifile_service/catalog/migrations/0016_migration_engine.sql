-- Migration 0016: the migration engine (M5; docs/design/migration-engine.md).
--
-- catalog.migration_prefix (0002) was never written to; it is reshaped
-- around sources: a key prefix inside a source, copied by Storage Mover (or
-- by Prizmanyx), verified file by file in Azure, then cut over.

ALTER TABLE catalog.migration_prefix DROP CONSTRAINT migration_prefix_pkey;
ALTER TABLE catalog.migration_prefix
    ADD COLUMN migration_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ADD COLUMN source_id      bigint NOT NULL REFERENCES catalog.source,
    ADD COLUMN to_backend_id  bigint NOT NULL REFERENCES catalog.backend,
    ADD COLUMN to_container   text COLLATE "C" NOT NULL,
    -- Copies land at target_root || <S3 key>: traceable to their object.
    ADD COLUMN target_root    text COLLATE "C" NOT NULL,
    ADD COLUMN copy_by        text NOT NULL DEFAULT 'omnifile'
                              CHECK (copy_by IN ('omnifile', 'external')),
    -- decision 35 reserves 'hand_off'; v1 serves.
    ADD COLUMN mode           text NOT NULL DEFAULT 'serve' CHECK (mode IN ('serve')),
    ADD COLUMN last_run_id    text,
    ADD COLUMN run_state      text CHECK (run_state IN ('running', 'succeeded', 'failed')),
    ADD COLUMN run_counters   jsonb,
    ADD COLUMN files_failed   bigint NOT NULL DEFAULT 0,
    ADD COLUMN last_error     text,
    ADD COLUMN requested_by   text NOT NULL DEFAULT 'system',
    ADD COLUMN created_at     timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN cut_over_at    timestamptz,
    ADD CONSTRAINT migration_prefix_shape CHECK (prefix = '' OR right(prefix, 1) = '/'),
    ADD CONSTRAINT migration_prefix_uq UNIQUE (source_id, prefix);
CREATE INDEX migration_prefix_dataset ON catalog.migration_prefix (dataset_id);

-- The engine finds a prefix's objects by key range.
CREATE INDEX location_key_range
    ON catalog.location (backend_id, container, object_key COLLATE "C")
    WHERE state IN ('pending', 'committed');

-- A copy whose CRC64-NVME differs from its source.
ALTER TABLE catalog.reconcile_drift DROP CONSTRAINT reconcile_drift_source_check;
ALTER TABLE catalog.reconcile_drift ADD CONSTRAINT reconcile_drift_source_check
    CHECK (source IN ('reconcile', 'changefeed', 'seed', 'migrate'));
ALTER TABLE catalog.reconcile_drift DROP CONSTRAINT reconcile_drift_kind_check;
ALTER TABLE catalog.reconcile_drift ADD CONSTRAINT reconcile_drift_kind_check
    CHECK (kind IN ('missing', 'unexpected', 'size_mismatch', 'tier_mismatch',
                    'retention_mismatch', 'hold_mismatch', 'deleted_externally',
                    'feed_record_failed', 'unmappable', 'path_conflict',
                    'checksum_mismatch'));

SELECT pgmq.create('migrate');
SELECT pgmq.create('migrate_dlq');
SELECT pgmq.create('migrate_verify');
SELECT pgmq.create('migrate_verify_dlq');
