-- Migration 0017: restore waves (M5; docs/design/migration-engine.md section 8).
--
-- A wave is one S3 Batch Operations restore job over a migration prefix's
-- archived objects. Each object also gets a catalog.restore row (the same
-- provider reference a single restore uses), so the change feed's Restore
-- Completed events update it like any other.

CREATE TABLE catalog.restore_wave (
    wave_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    migration_id  bigint NOT NULL REFERENCES catalog.migration_prefix,
    backend_id    bigint NOT NULL REFERENCES catalog.backend,
    job_id        text,
    state         text NOT NULL DEFAULT 'running' CHECK (state IN ('running', 'done', 'failed')),
    objects       integer NOT NULL CHECK (objects > 0),
    speed         text NOT NULL CHECK (speed IN ('standard', 'bulk')),
    days          integer NOT NULL CHECK (days > 0),
    requested_at  timestamptz NOT NULL,
    finished_at   timestamptz,
    last_status   jsonb,
    failure       text
);
CREATE UNIQUE INDEX restore_wave_one_running
    ON catalog.restore_wave (migration_id) WHERE state = 'running';

ALTER TABLE catalog.restore ADD COLUMN wave_id bigint REFERENCES catalog.restore_wave;
CREATE INDEX restore_wave_idx ON catalog.restore (wave_id) WHERE wave_id IS NOT NULL;

-- Checksum jobs (design note section 5): S3 Batch Operations Compute
-- checksum over the S3 objects whose copies wait to be verified but that
-- have no stored CRC64NVME. The values come back in the completion report.
CREATE TABLE catalog.checksum_job (
    checksum_job_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    migration_id    bigint NOT NULL REFERENCES catalog.migration_prefix,
    backend_id      bigint NOT NULL REFERENCES catalog.backend,
    name            text NOT NULL UNIQUE,
    job_id          text,
    state           text NOT NULL DEFAULT 'running' CHECK (state IN ('running', 'done', 'failed')),
    objects         integer NOT NULL CHECK (objects > 0),
    found           integer,
    requested_at    timestamptz NOT NULL,
    finished_at     timestamptz,
    failure         text
);
CREATE UNIQUE INDEX checksum_job_one_running
    ON catalog.checksum_job (migration_id) WHERE state = 'running';
-- The S3 locations a job was asked about, so one it couldn't checksum
-- (SSE-C, gone) isn't asked about again.
CREATE TABLE catalog.checksum_job_object (
    checksum_job_id bigint NOT NULL REFERENCES catalog.checksum_job,
    location_id     bigint NOT NULL REFERENCES catalog.location,
    PRIMARY KEY (checksum_job_id, location_id)
);
CREATE INDEX checksum_job_object_location ON catalog.checksum_job_object (location_id);
