-- Migration 0036: the share map's counts (portal-api.md section 5.22; WP13).
--
-- GET /v1/shares/{share_id}/map shows an ArchiveAdmin every file under a
-- share, by what the edge holds and where the primary copy is. Counting
-- that per request is a pass over the share's files (minutes at 10^8), so
-- the worker counts each share once an hour (usage/share_map.py) and the
-- route reads the latest count. One row per share, edge state, backend,
-- tier and class; a share's rows are replaced together.

CREATE TABLE catalog.share_map_count (
    share_id      bigint NOT NULL REFERENCES catalog.share ON DELETE CASCADE,
    computed_at   timestamptz NOT NULL,
    -- file.state.
    edge_state    text NOT NULL,
    -- The primary committed copy's backend, tier and class; NULL with no copy yet.
    backend_id    bigint REFERENCES catalog.backend,
    logical_tier  text,
    storage_class text,
    files         bigint NOT NULL CHECK (files >= 0),
    bytes         bigint NOT NULL CHECK (bytes >= 0),
    local_bytes   bigint NOT NULL CHECK (local_bytes >= 0)
);
CREATE INDEX share_map_count_share ON catalog.share_map_count (share_id);

-- When each share was last counted (a share with no files has no count rows).
CREATE TABLE catalog.share_map_run (
    share_id    bigint PRIMARY KEY REFERENCES catalog.share ON DELETE CASCADE,
    computed_at timestamptz NOT NULL
);
