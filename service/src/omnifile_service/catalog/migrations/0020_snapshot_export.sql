-- Bulk snapshot exports (control-api.md section 6): a large dataset's
-- snapshot as zstd-compressed NDJSON parts in blob storage, for an edge
-- that must re-read 10^8 rows. `as_of` is the feed cursor taken when the
-- export starts, before any row is read, so replaying the feed from it is
-- harmless. The worker's `snapshot_export` queue writes one part per
-- message: `after_file_id` is where the next part starts. Parts live in
-- `container` under `prefix` on `backend_id` (snapshots/export.py); the
-- expiry sweep deletes them and marks the export expired.

CREATE TABLE catalog.snapshot_export (
    export_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edge_id       bigint NOT NULL REFERENCES catalog.edge (edge_id),
    dataset_id    bigint NOT NULL REFERENCES catalog.dataset (dataset_id),
    state         text NOT NULL DEFAULT 'running'
                  CHECK (state IN ('running', 'ready', 'failed', 'expired')),
    as_of         text NOT NULL,
    backend_id    bigint NOT NULL REFERENCES catalog.backend (backend_id),
    container     text NOT NULL,
    prefix        text NOT NULL,
    -- [{key, rows, bytes, sha256}], in order
    parts         jsonb NOT NULL DEFAULT '[]',
    after_file_id bigint NOT NULL DEFAULT 0,
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);

CREATE INDEX snapshot_export_edge ON catalog.snapshot_export (edge_id, dataset_id);
CREATE INDEX snapshot_export_age ON catalog.snapshot_export (created_at)
    WHERE state <> 'expired';

SELECT pgmq.create('snapshot_export');
SELECT pgmq.create('snapshot_export_dlq');
