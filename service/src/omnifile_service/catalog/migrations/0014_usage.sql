-- Migration 0014: usage snapshots for the partner API's GET /v1/usage
-- (M5; portal-api.md section 5.11).
--
-- A worker sweep counts the catalog once a day: per dataset, every
-- committed location (what the provider bills) by backend, role, logical
-- tier and native class; per share, the live files under it by tier. The
-- API reads the latest snapshot, never the whole catalog per request.

CREATE TABLE catalog.usage_snapshot (
    computed_at   timestamptz NOT NULL,
    dataset_id    bigint NOT NULL REFERENCES catalog.dataset,
    -- NULL: the dataset's stored copies. Set: the live files under that
    -- share (its deepest covering share, as access works).
    share_id      bigint REFERENCES catalog.share,
    backend_id    bigint REFERENCES catalog.backend,
    role          text,
    logical_tier  text,
    storage_class text,
    objects       bigint NOT NULL CHECK (objects >= 0),
    bytes         bigint NOT NULL CHECK (bytes >= 0),
    CONSTRAINT usage_snapshot_shape
        CHECK ((share_id IS NULL) = (backend_id IS NOT NULL AND role IS NOT NULL))
);
CREATE INDEX usage_snapshot_at ON catalog.usage_snapshot (computed_at DESC, dataset_id);
