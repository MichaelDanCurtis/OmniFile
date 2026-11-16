-- Migration 0037: staged migration plans, the console's actions, its rolled-up
-- progress and the Storage Mover agents' last poll (WP12 console;
-- portal-api.md 5.22).
--
-- A plan splits a dataset's migration into waves. Each wave is a list of
-- S3 prefixes started together (one migration_prefix each) once its start
-- time has come, inside its daily window, outside its blackouts, after the
-- wave it depends on is verified, and once its sources are catalogued. The
-- worker's plan sweep (migrate/plans.py) does that. A wave's migrations are
-- the plan's (`requested_by` system:plans): their cutover, cancel and retry
-- go through the plan's routes only, never the app that proposed it.
--
-- What a wave's window and blackouts gate, exactly:
--   * starting the wave;
--   * its cutover: its migrations always hold at `verified` (cutover =
--     manual), the sweep asks for each cutover inside the window, and a
--     migration that leaves `verified` drops the sweep's request;
--   * the engine's new Storage Mover runs (scheduled reruns and Copy again)
--     and its new S3 restore waves (Restore again included) for the wave.
-- Not gated: a Storage Mover run or restore already under way, and the
-- per-file verification, which reads only inside Azure. Bandwidth and egress
-- figures are advisory: recorded and shown, never applied.
-- A plan from an API client is `proposed` until an ArchiveAdmin approves it
-- (anti-pattern 7).

CREATE TABLE catalog.migration_plan (
    plan_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id         bigint NOT NULL REFERENCES catalog.dataset,
    name               text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    state              text NOT NULL DEFAULT 'proposed'
                       CHECK (state IN ('proposed', 'approved', 'rejected', 'done', 'cancelled')),
    -- The person answerable for it: an AD identity in the source's groups.
    owner_identity_id  bigint NOT NULL REFERENCES catalog.identity,
    proposed_by        text NOT NULL,
    decided_by         text,
    decided_at         timestamptz,
    decision_note      text,
    entity_version     bigint NOT NULL DEFAULT 1,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX migration_plan_dataset ON catalog.migration_plan (dataset_id);

CREATE TABLE catalog.migration_wave (
    wave_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plan_id            bigint NOT NULL REFERENCES catalog.migration_plan,
    seq                integer NOT NULL CHECK (seq BETWEEN 1 AND 500),
    name               text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    prefixes           text[] COLLATE "C" NOT NULL
                       CHECK (cardinality(prefixes) BETWEEN 1 AND 100),
    start_at           timestamptz NOT NULL,
    -- A daily window in UTC minutes; both null: any time of day. An end
    -- before the start wraps past midnight.
    window_start_min   integer CHECK (window_start_min BETWEEN 0 AND 1439),
    window_end_min     integer CHECK (window_end_min BETWEEN 0 AND 1439),
    -- [{"from": <rfc3339>, "to": <rfc3339>}]: see the gates above.
    blackouts          jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(blackouts) = 'array'),
    -- This wave waits until wave `after_seq` of the plan is verified.
    after_seq          integer,
    cutover            text NOT NULL DEFAULT 'auto' CHECK (cutover IN ('auto', 'manual')),
    copier             text NOT NULL DEFAULT 'auto' CHECK (copier IN ('auto', 'external')),
    -- Advisory (shown, not enforced; portal-api.md 5.22): the Storage Mover
    -- agent's upload limit, and an egress budget.
    bandwidth_mbps     integer CHECK (bandwidth_mbps > 0),
    egress_gb_per_day  integer CHECK (egress_gb_per_day > 0),
    -- blocked: the wave it waits for was cancelled, so it never starts.
    -- cancelled: by the plan, or every one of its migrations was cancelled.
    state              text NOT NULL DEFAULT 'planned'
                       CHECK (state IN ('planned', 'waiting', 'blocked', 'started', 'verified',
                                        'cut_over', 'failed', 'cancelled')),
    waiting_reason     text,
    started_at         timestamptz,
    verified_at        timestamptz,
    cut_over_at        timestamptz,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, seq),
    CHECK ((window_start_min IS NULL) = (window_end_min IS NULL)),
    CHECK (after_seq IS NULL OR after_seq < seq)
);
CREATE INDEX migration_wave_due ON catalog.migration_wave (state, start_at)
    WHERE state IN ('planned', 'waiting', 'blocked', 'started', 'verified', 'failed');

-- The migrations a started wave made, one per prefix.
CREATE TABLE catalog.migration_wave_prefix (
    wave_id       bigint NOT NULL REFERENCES catalog.migration_wave,
    prefix        text COLLATE "C" NOT NULL,
    migration_id  bigint NOT NULL REFERENCES catalog.migration_prefix,
    PRIMARY KEY (wave_id, prefix)
);
CREATE UNIQUE INDEX migration_wave_prefix_migration ON catalog.migration_wave_prefix (migration_id);

-- The console's in-place fixes, which the engine acts on at its next step:
-- copy again now (a new Additive run, which also copies again the copies
-- that failed their check), and restore again the restored S3 copies that
-- are about to expire unverified (S3 extends a restored copy's life).
ALTER TABLE catalog.migration_prefix
    ADD COLUMN rerun_requested_at   timestamptz,
    ADD COLUMN restore_requested_at timestamptz;

-- The console's progress, rolled up by the worker (migrate/progress.py), so
-- reading the console never scans a migration's objects. A table of its own:
-- a count never touches the migration's row (nor its entity_version), and
-- `counted_state` says whether a finished migration needs one last count.
CREATE TABLE catalog.migration_count (
    migration_id      bigint PRIMARY KEY REFERENCES catalog.migration_prefix,
    counted_state     text NOT NULL,
    bytes_total       bigint NOT NULL,
    bytes_verified    bigint NOT NULL,
    -- Copies that failed their check whose file still has no verified copy.
    failing_now       bigint NOT NULL,
    archived_waiting  bigint NOT NULL,
    restores_expiring bigint NOT NULL,
    counted_at        timestamptz NOT NULL
);

-- Samples of a migration's verified bytes, for the last hour's throughput.
CREATE TABLE catalog.migration_progress (
    migration_id    bigint NOT NULL REFERENCES catalog.migration_prefix,
    at              timestamptz NOT NULL,
    bytes_verified  bigint NOT NULL,
    files_verified  bigint NOT NULL,
    PRIMARY KEY (migration_id, at)
);

-- Storage Mover agents as the worker last read them through ARM, for the
-- console (web reads only the catalog).
CREATE TABLE catalog.mover_agent (
    name             text PRIMARY KEY,
    status           text,
    version          text,
    host             text,
    arc_resource_id  text,
    last_status_at   timestamptz,
    error            text,
    polled_at        timestamptz NOT NULL
);
