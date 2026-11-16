-- Migration 0013: sources and catalog seeding (M5; docs/design/catalog-seeding.md).
--
-- A source is a bucket prefix that already holds data, bound to a folder of a
-- dataset. Seeding writes one remote file per object (with its version and
-- primary location) in resumable batches on the `seed` queue.

CREATE TABLE catalog.source (
    source_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id       bigint NOT NULL REFERENCES catalog.dataset,
    -- Folder of the dataset the objects appear under, relative to its root
    -- ('' = the root), '/'-separated, no leading or trailing '/'.
    path             text COLLATE "C" NOT NULL DEFAULT '',
    backend_id       bigint NOT NULL REFERENCES catalog.backend,
    container        text COLLATE "C" NOT NULL,
    -- '' or ending in '/'.
    prefix           text COLLATE "C" NOT NULL DEFAULT '',
    -- {"kind": "s3_inventory", "bucket", "manifest_key"}, or NULL: list the prefix.
    inventory        jsonb,
    owners_sid       catalog.sid NOT NULL,
    writers_sid      catalog.sid,
    readers_sid      catalog.sid,
    state            text NOT NULL DEFAULT 'registered'
                     CHECK (state IN ('registered', 'seeding', 'seeded', 'failed')),
    -- The inventory's creation time, or when the listing started. Objects are
    -- as of this time; the change-feed cursor starts here.
    snapshot_at      timestamptz,
    -- Where the next batch starts (reader-specific).
    resume           jsonb,
    objects_seen     bigint NOT NULL DEFAULT 0 CHECK (objects_seen >= 0),
    files_created    bigint NOT NULL DEFAULT 0 CHECK (files_created >= 0),
    dirs_created     bigint NOT NULL DEFAULT 0 CHECK (dirs_created >= 0),
    already_present  bigint NOT NULL DEFAULT 0 CHECK (already_present >= 0),
    skipped          bigint NOT NULL DEFAULT 0 CHECK (skipped >= 0),
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    seeded_at        timestamptz,
    CONSTRAINT source_prefix_shape CHECK (prefix = '' OR right(prefix, 1) = '/'),
    CONSTRAINT source_path_shape
        CHECK (path = '' OR (left(path, 1) <> '/' AND right(path, 1) <> '/')),
    CONSTRAINT source_inventory_kind
        CHECK (inventory IS NULL OR inventory->>'kind' = 's3_inventory'),
    UNIQUE (backend_id, container, prefix)
);
CREATE INDEX source_dataset ON catalog.source (dataset_id);

SELECT pgmq.create('seed');
SELECT pgmq.create('seed_dlq');

-- Objects seeding can't place are drift, never dropped silently.
ALTER TABLE catalog.reconcile_drift DROP CONSTRAINT reconcile_drift_source_check;
ALTER TABLE catalog.reconcile_drift ADD CONSTRAINT reconcile_drift_source_check
    CHECK (source IN ('reconcile', 'changefeed', 'seed'));
ALTER TABLE catalog.reconcile_drift DROP CONSTRAINT reconcile_drift_kind_check;
ALTER TABLE catalog.reconcile_drift ADD CONSTRAINT reconcile_drift_kind_check
    CHECK (kind IN ('missing', 'unexpected', 'size_mismatch', 'tier_mismatch',
                    'retention_mismatch', 'hold_mismatch', 'deleted_externally',
                    'feed_record_failed', 'unmappable', 'path_conflict'));

-- Seeding inserts into tables whose statistics are still empty, and the old
-- join here then walked the whole location_one_primary index on every call
-- (quadratic seeding). Same result; the location is now looked up by the
-- version id directly, an index condition no plan can skip.
CREATE OR REPLACE FUNCTION catalog.file_resync_location(p_file_id bigint) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    v_cv    bigint;
    v_loc   bigint;
    v_tier  text;
    v_state text;
    v_exp   timestamptz;
BEGIN
    SELECT f.content_version_id INTO v_cv FROM catalog.file f WHERE f.file_id = p_file_id;
    IF v_cv IS NOT NULL THEN
        SELECT l.location_id, l.logical_tier INTO v_loc, v_tier
          FROM catalog.location l
         WHERE l.version_id = v_cv AND l.role = 'primary' AND l.state = 'committed';
    END IF;
    IF v_loc IS NOT NULL THEN
        SELECT CASE r.state WHEN 'requested' THEN 'in_progress' ELSE r.state END, r.expires_at
          INTO v_state, v_exp
          FROM catalog.restore r
         WHERE r.location_id = v_loc AND r.state <> 'cancelled'
         ORDER BY r.requested_at DESC, r.restore_id DESC
         LIMIT 1;
    END IF;
    UPDATE catalog.file f
       SET primary_location_id = v_loc, remote_tier = v_tier,
           restore_state = v_state, restore_expires_at = v_exp
     WHERE f.file_id = p_file_id
       AND (f.primary_location_id, f.remote_tier, f.restore_state, f.restore_expires_at)
           IS DISTINCT FROM (v_loc, v_tier, v_state, v_exp);
END $$;
