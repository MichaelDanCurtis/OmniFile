-- Migration 0009: a change-feed record the catalog can't apply no longer
-- stops its container's feed (M3 review).
--
-- The sync retries a failing page; after `consecutive_failures` reaches the
-- worker's limit it skips the records that still fail, recording each as
-- drift of kind `feed_record_failed` (with the record), and moves on.

ALTER TABLE catalog.changefeed_cursor
    ADD COLUMN consecutive_failures integer NOT NULL DEFAULT 0
                                    CHECK (consecutive_failures >= 0),
    ADD COLUMN skipped_records bigint NOT NULL DEFAULT 0 CHECK (skipped_records >= 0);

ALTER TABLE catalog.reconcile_drift DROP CONSTRAINT reconcile_drift_kind_check;
ALTER TABLE catalog.reconcile_drift ADD CONSTRAINT reconcile_drift_kind_check
    CHECK (kind IN ('missing', 'unexpected', 'size_mismatch', 'tier_mismatch',
                    'retention_mismatch', 'hold_mismatch', 'deleted_externally',
                    'feed_record_failed'));
