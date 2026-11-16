-- Migration 0008: versioning depth, WORM state on the backend, change-feed
-- sync, nightly reconcile, and the queues M3 needs (decisions 27, 30, 31).
--
-- 1. Queues. The worker's dead-letter queues (`<queue>_dlq`) were created at
--    run time by each consumer; they are created here instead, with the new
--    M3 queues:
--      retention   backend side of pruning (delete a pruned version's blob),
--                  WORM container policy create/lock/extend, per-version
--                  retention, legal hold and state tags
--      reconcile   an on-demand reconcile of one container
--      tiering     set_class moves from the policy engine (m3 policy track)
--    pgmq.create is idempotent, so databases where the worker already made
--    a `*_dlq` keep it.
-- 2. tier_pin.override_archive (decision 29): an Owner explicitly let a warmer
--    pin keep a file past its archive date.
-- 3. share: the typed WORM confirmation, when WORM was enabled, the test
--    window (at most 24 h, decision 31), and what the backend's container
--    policy is (none / unlocked / locked) so the worker can bring it in line.
-- 4. location: what WORM state the backend version carries (retention,
--    locked or not, legal hold), and a flag the triggers below raise when the
--    catalog wants something the backend doesn't have yet.
-- 5. catalog.rewind_depth_days(): the depth that applies to a folder (the
--    deepest share covering it), the counterpart of worm_retention_days().
-- 6. Change-feed cursors and lag samples, and reconcile runs and drift.
-- 7. Legal holds reach versions committed later; catalog.legal_hold_covers()
--    and catalog.worm_locked() for the WORM stamping job.
-- 8. catalog.file_bump (0005) also ignores the scheduler's clocks
--    (next_action, next_action_at, archive_at).

-- ---------------------------------------------------------------------------
-- 1. Queues
-- ---------------------------------------------------------------------------
SELECT pgmq.create(q)
  FROM unnest(ARRAY['retention', 'reconcile', 'tiering']) AS q;
SELECT pgmq.create(q || '_dlq')
  FROM unnest(ARRAY['restores', 'storage_mover', 'verify', 'notify', 'edge_instructions',
                    'retention', 'reconcile', 'tiering']) AS q;

-- ---------------------------------------------------------------------------
-- 2. Tier pins past the archive date (decision 29)
-- ---------------------------------------------------------------------------
ALTER TABLE catalog.tier_pin
    ADD COLUMN override_archive boolean NOT NULL DEFAULT false;

-- ---------------------------------------------------------------------------
-- 3. WORM share state (decision 31)
-- ---------------------------------------------------------------------------
ALTER TABLE catalog.share
    -- What the ArchiveAdmin typed to confirm (the share name). The service
    -- refuses to enable WORM unless it matches; kept as the record.
    ADD COLUMN worm_confirmation text,
    ADD COLUMN worm_enabled_at   timestamptz,
    -- The backend container policy, as the worker last set it.
    ADD COLUMN worm_policy_state text NOT NULL DEFAULT 'none'
                                 CHECK (worm_policy_state IN ('none', 'unlocked', 'locked')),
    ADD COLUMN worm_policy_days  integer CHECK (worm_policy_days > 0),
    ADD COLUMN worm_policy_synced_at timestamptz,
    -- Microsoft recommends a validation window of hours, at most 24 h.
    ADD CONSTRAINT share_worm_test_window
        CHECK (worm_lock_deadline IS NULL OR worm_enabled_at IS NULL
               OR worm_lock_deadline <= worm_enabled_at + interval '24 hours');

-- Auto-lock sweep: unlocked shares (few rows). A share with no deadline
-- recorded locks 24 h after it was enabled (or created).
CREATE INDEX share_worm_lock_due
    ON catalog.share (share_id) WHERE worm_mode = 'unlocked_test';
-- Shares whose backend policy lags the catalog (few rows).
CREATE INDEX share_worm_policy_stale
    ON catalog.share (share_id)
    WHERE worm_enabled AND (worm_policy_state <> CASE worm_mode WHEN 'locked' THEN 'locked'
                                                               ELSE 'unlocked' END
                            OR worm_policy_days IS DISTINCT FROM worm_retention_days);

-- ---------------------------------------------------------------------------
-- 4. Per-version WORM state on the backend
-- ---------------------------------------------------------------------------
ALTER TABLE catalog.location
    ADD COLUMN worm_sync_needed    boolean NOT NULL DEFAULT false,
    ADD COLUMN worm_stamped_until  timestamptz,
    ADD COLUMN worm_stamped_locked boolean NOT NULL DEFAULT false,
    ADD COLUMN worm_stamped_hold   boolean NOT NULL DEFAULT false,
    ADD COLUMN worm_synced_at      timestamptz,
    -- The newest change-feed record applied to this location; older records
    -- (a replayed page) are skipped, so the sync is idempotent per blob.
    ADD COLUMN feed_seen_at        timestamptz;

-- The stamping queue: small, because rows leave it once stamped.
CREATE INDEX location_worm_sync
    ON catalog.location (location_id) WHERE worm_sync_needed;

-- A committed copy of a retained or held version needs the backend to
-- carry that retention and hold. Raised on the location, in BEFORE so that
-- no second update (and no recursion) is needed.
CREATE FUNCTION catalog.location_worm_mark() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.role IN ('primary', 'secondary') AND NEW.state = 'committed'
       AND (TG_OP = 'INSERT' OR OLD.state IS DISTINCT FROM 'committed')
       AND EXISTS (SELECT 1 FROM catalog.file_version v
                    WHERE v.version_id = NEW.version_id
                      AND (v.retention_until IS NOT NULL OR v.legal_hold)) THEN
        NEW.worm_sync_needed := true;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER location_worm_mark BEFORE INSERT OR UPDATE ON catalog.location
    FOR EACH ROW EXECUTE FUNCTION catalog.location_worm_mark();

-- A version's retention or hold changed (set at commit by
-- file_version_before, extended, or a legal hold set or cleared): its
-- committed copies need stamping. A generic AFTER trigger with WHEN, so
-- changes made by the BEFORE trigger count too.
CREATE FUNCTION catalog.file_version_worm_mark() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE catalog.location
       SET worm_sync_needed = true
     WHERE version_id = NEW.version_id
       AND role IN ('primary', 'secondary') AND state = 'committed'
       AND NOT worm_sync_needed;
    RETURN NULL;
END $$;
CREATE TRIGGER file_version_worm_mark_upd AFTER UPDATE ON catalog.file_version
    FOR EACH ROW
    WHEN (OLD.retention_until IS DISTINCT FROM NEW.retention_until
          OR OLD.legal_hold IS DISTINCT FROM NEW.legal_hold)
    EXECUTE FUNCTION catalog.file_version_worm_mark();
CREATE TRIGGER file_version_worm_mark_ins AFTER INSERT ON catalog.file_version
    FOR EACH ROW
    WHEN (NEW.retention_until IS NOT NULL OR NEW.legal_hold)
    EXECUTE FUNCTION catalog.file_version_worm_mark();

-- ---------------------------------------------------------------------------
-- 5. Rewind depth for a folder (decision 30)
-- ---------------------------------------------------------------------------
-- The deepest rewind_depth_days of the shares whose root contains the
-- folder; 0 when no share covers it.
CREATE FUNCTION catalog.rewind_depth_days(p_dataset_id bigint, p_parent_id bigint)
RETURNS integer
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE up(file_id, parent_id) AS (
        SELECT file_id, parent_id FROM catalog.file WHERE file_id = p_parent_id
        UNION
        SELECT f.file_id, f.parent_id FROM catalog.file f JOIN up ON f.file_id = up.parent_id
    )
    SELECT coalesce(max(s.rewind_depth_days), 0)
      FROM catalog.share s
     WHERE s.dataset_id = p_dataset_id
       AND (s.root_dir_id IS NULL OR s.root_dir_id IN (SELECT file_id FROM up))
$$;

-- Rewind depth changes are audited (Owners set the depth, ArchiveAdmin the
-- maximum; share_rewind_within_max keeps one under the other).
CREATE FUNCTION catalog.share_rewind_audit() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM catalog.audit('share.rewind_depth', NEW.dataset_id, NEW.share_id, NULL, NULL,
        jsonb_build_object('old', jsonb_build_object('days', OLD.rewind_depth_days,
                                                     'max_days', OLD.rewind_depth_max_days),
                           'new', jsonb_build_object('days', NEW.rewind_depth_days,
                                                     'max_days', NEW.rewind_depth_max_days)));
    RETURN NULL;
END $$;
CREATE TRIGGER share_rewind_audit AFTER UPDATE ON catalog.share
    FOR EACH ROW
    WHEN ((OLD.rewind_depth_days, OLD.rewind_depth_max_days)
          IS DISTINCT FROM (NEW.rewind_depth_days, NEW.rewind_depth_max_days))
    EXECUTE FUNCTION catalog.share_rewind_audit();

-- ---------------------------------------------------------------------------
-- 6a. Change-feed sync
-- ---------------------------------------------------------------------------
-- One cursor per (backend, container) the sync reads. `cursor` is the
-- backend's opaque position, saved in the same transaction as the catalog
-- changes it covers, so a crash replays at most one page.
CREATE TABLE catalog.changefeed_cursor (
    backend_id      bigint NOT NULL REFERENCES catalog.backend,
    container       text COLLATE "C" NOT NULL,
    cursor          text,  -- NULL: not read yet (starts "from now")
    caught_up       boolean NOT NULL DEFAULT false,
    records_applied bigint NOT NULL DEFAULT 0,
    records_unmatched bigint NOT NULL DEFAULT 0,
    -- Lag: now minus the event time of the newest record applied.
    last_record_at  timestamptz,
    last_lag_seconds double precision,
    last_read_at    timestamptz,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (backend_id, container)
);

-- One sample per non-empty page, for the lag metric (M3 exit). Small:
-- pruned by age by the sync sweep.
CREATE TABLE catalog.changefeed_lag (
    backend_id   bigint NOT NULL,
    container    text COLLATE "C" NOT NULL,
    at           timestamptz NOT NULL,
    records      integer NOT NULL CHECK (records >= 0),
    max_lag_seconds double precision NOT NULL,
    mean_lag_seconds double precision NOT NULL,
    PRIMARY KEY (backend_id, container, at)
);

-- ---------------------------------------------------------------------------
-- 6b. Reconcile (nightly)
-- ---------------------------------------------------------------------------
-- Non-WORM accounts compare Blob Inventory with the catalog; WORM accounts
-- (where version-level immutability blocks Inventory) catch the change
-- feed up and compare a listing.
CREATE TABLE catalog.reconcile_run (
    run_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    backend_id    bigint NOT NULL REFERENCES catalog.backend,
    container     text COLLATE "C" NOT NULL,
    method        text NOT NULL CHECK (method IN ('inventory', 'listing_changefeed')),
    state         text NOT NULL DEFAULT 'running'
                  CHECK (state IN ('running', 'completed', 'failed')),
    snapshot_at   timestamptz,  -- the inventory's (or listing's) time
    started_at    timestamptz NOT NULL,
    completed_at  timestamptz,
    objects_seen  bigint NOT NULL DEFAULT 0,
    locations_checked bigint NOT NULL DEFAULT 0,
    drift_found   integer NOT NULL DEFAULT 0,
    drift_resolved integer NOT NULL DEFAULT 0,
    failure       text
);
CREATE INDEX reconcile_run_latest ON catalog.reconcile_run (backend_id, container, started_at DESC);

-- One row per open difference between the catalog and the backend. A later
-- run that no longer sees it sets resolved_at.
CREATE TABLE catalog.reconcile_drift (
    drift_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    first_run_id  bigint REFERENCES catalog.reconcile_run,
    last_run_id   bigint REFERENCES catalog.reconcile_run,
    source        text NOT NULL CHECK (source IN ('reconcile', 'changefeed')),
    backend_id    bigint NOT NULL REFERENCES catalog.backend,
    container     text COLLATE "C" NOT NULL,
    object_key    text COLLATE "C" NOT NULL,
    backend_version_id text,
    location_id   bigint REFERENCES catalog.location,
    kind          text NOT NULL
                  CHECK (kind IN ('missing', 'unexpected', 'size_mismatch', 'tier_mismatch',
                                  'retention_mismatch', 'hold_mismatch', 'deleted_externally')),
    expected      jsonb,
    observed      jsonb,
    detected_at   timestamptz NOT NULL,
    last_seen_at  timestamptz NOT NULL,
    resolved_at   timestamptz
);
CREATE UNIQUE INDEX reconcile_drift_open
    ON catalog.reconcile_drift (backend_id, container, object_key, kind,
                                coalesce(backend_version_id, ''))
    WHERE resolved_at IS NULL;

-- ---------------------------------------------------------------------------
-- 7. Legal holds reach versions committed after the hold (decision 31)
-- ---------------------------------------------------------------------------
-- True when an active legal hold covers the file: on the file itself or on
-- any folder above it.
CREATE FUNCTION catalog.legal_hold_covers(p_file_id bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE up(file_id, parent_id) AS (
        SELECT file_id, parent_id FROM catalog.file WHERE file_id = p_file_id
        UNION
        SELECT f.file_id, f.parent_id FROM catalog.file f JOIN up ON f.file_id = up.parent_id
    )
    SELECT EXISTS (SELECT 1 FROM catalog.hold h
                    WHERE h.kind = 'legal' AND h.released_at IS NULL
                      AND h.file_id IN (SELECT file_id FROM up))
$$;

-- A version committed under an active legal hold is held from the start.
-- Runs after file_version_before (trigger names sort), so the hold is set on
-- the final row; the hold.set audit row covers it.
CREATE FUNCTION catalog.file_version_hold_on_commit() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'committed' AND NOT NEW.legal_hold
       AND (TG_OP = 'INSERT' OR OLD.state <> 'committed')
       AND EXISTS (SELECT 1 FROM catalog.hold h
                    WHERE h.dataset_id = NEW.dataset_id AND h.kind = 'legal'
                      AND h.released_at IS NULL)
       AND catalog.legal_hold_covers(NEW.file_id) THEN
        NEW.legal_hold := true;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER file_version_hold_on_commit BEFORE INSERT OR UPDATE ON catalog.file_version
    FOR EACH ROW EXECUTE FUNCTION catalog.file_version_hold_on_commit();

-- True when a locked WORM share covers the folder: per-version retention is
-- then stamped locked (compliance) rather than unlocked (validation window).
CREATE FUNCTION catalog.worm_locked(p_dataset_id bigint, p_parent_id bigint)
RETURNS boolean
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE up(file_id, parent_id) AS (
        SELECT file_id, parent_id FROM catalog.file WHERE file_id = p_parent_id
        UNION
        SELECT f.file_id, f.parent_id FROM catalog.file f JOIN up ON f.file_id = up.parent_id
    )
    SELECT EXISTS (SELECT 1 FROM catalog.share s
                    WHERE s.dataset_id = p_dataset_id AND s.worm_mode = 'locked'
                      AND (s.root_dir_id IS NULL OR s.root_dir_id IN (SELECT file_id FROM up)))
$$;

-- ---------------------------------------------------------------------------
-- 8. Scheduler clocks don't change a file's entity_version (extends 0005)
-- ---------------------------------------------------------------------------
-- The tiering scheduler writes next_action, next_action_at and archive_at;
-- like last_read_at (0005) they are service-only, the feed ignores them, and
-- bumping entity_version for them would change the edges' reconciliation
-- digest with no feed event to explain it. An update that changes only
-- these keeps entity_version and updated_at.
CREATE OR REPLACE FUNCTION catalog.file_bump() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.last_read_at, NEW.next_action, NEW.next_action_at, NEW.archive_at)
           IS DISTINCT FROM (OLD.last_read_at, OLD.next_action, OLD.next_action_at, OLD.archive_at)
       AND to_jsonb(NEW) - 'last_read_at' - 'next_action' - 'next_action_at' - 'archive_at'
                         - 'entity_version' - 'updated_at'
         = to_jsonb(OLD) - 'last_read_at' - 'next_action' - 'next_action_at' - 'archive_at'
                         - 'entity_version' - 'updated_at' THEN
        NEW.entity_version := OLD.entity_version;
        NEW.updated_at := OLD.updated_at;
        RETURN NEW;
    END IF;
    NEW.entity_version := OLD.entity_version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END $$;
