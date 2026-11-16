-- Staging copies for urgent recalls (docs/design/migration-engine.md
-- section 4; decision 24). A user's (or the partner API's) restore of an
-- object under a migrating prefix is copied, inside S3, into the backend's
-- staging bucket, and a small Storage Mover run copies the staging prefix
-- onto the real target path, so the file needn't wait for the prefix's next
-- Additive run. The staging object is deleted once the copy is verified.

CREATE TABLE catalog.staging_run (
    staging_run_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    migration_id   bigint NOT NULL REFERENCES catalog.migration_prefix,
    job            text NOT NULL,
    run_id         text NOT NULL,
    state          text NOT NULL DEFAULT 'running' CHECK (state IN ('running', 'done', 'failed')),
    counters       jsonb,
    error          text,
    started_at     timestamptz NOT NULL,
    finished_at    timestamptz
);
CREATE UNIQUE INDEX staging_run_one_running
    ON catalog.staging_run (migration_id) WHERE state = 'running';

-- staged: copied into the staging bucket; moving: in a run; copied: the run
-- finished; failed: runs gave up on it (the prefix's next run copies the
-- file as any other); removed: the staging object is deleted (its file has
-- a verified copy, or it was given up), or was never made (`error`).
CREATE TABLE catalog.staging_object (
    migration_id      bigint NOT NULL REFERENCES catalog.migration_prefix,
    location_id       bigint NOT NULL REFERENCES catalog.location,
    restore_id        bigint REFERENCES catalog.restore,
    bucket            text NOT NULL,
    object_key        text NOT NULL,
    backend_version_id text,
    staging_run_id    bigint REFERENCES catalog.staging_run,
    state             text NOT NULL CHECK (state IN ('staged', 'moving', 'copied', 'removed',
                                                     'failed')),
    attempts          integer NOT NULL DEFAULT 0,
    error             text,
    staged_at         timestamptz NOT NULL,
    removed_at        timestamptz,
    PRIMARY KEY (migration_id, location_id)
);
CREATE INDEX staging_object_open
    ON catalog.staging_object (migration_id, state) WHERE state <> 'removed';
