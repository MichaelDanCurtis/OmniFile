-- migrate: no-transaction
-- Migration 0043: the live Activity feed and who sees prices (WP18;
-- portal-api.md sections 5.13 and 5.24).
--
-- Runs outside a transaction (catalog/migrate.py, "No-transaction
-- migrations"): the indexes are built CONCURRENTLY, so edge commits to
-- file_version go on during the build, and every statement may run again
-- after a failure part-way.
--
-- 1. The live feed (`GET /v1/activity/live`) reads the last few minutes of
--    each source by time, every few seconds per open Activity page. Each read
--    is an index range scan:
--    - uploads: content versions an edge committed, by committed_at (new);
--    - recalls: each recall's state changes, by time (new);
--    - migrations: their typed partner events, by time (new);
--    - tier moves: audit_log's primary key (at, ...);
--    - annex events: annex_event's primary key (at, ...);
--    - heartbeats: catalog.edge, one row per edge.
--
-- 2. Prices (Settings, Prices and alerts): which portal roles see costs.
--    One row. `show_to` lists the roles that see them: ArchiveAdmin,
--    DataSteward, and `others` (every other signed-in user). An empty list
--    hides prices from everyone. The `web` app removes every amount from the
--    answers of a caller whose roles are all hidden (web/prices.py); the
--    ArchiveAdmins' price editor still shows the rates it edits. Partner
--    applications are not affected.

CREATE INDEX CONCURRENTLY IF NOT EXISTS file_version_edge_committed
    ON catalog.file_version (committed_at)
    WHERE kind = 'content' AND edge_id IS NOT NULL AND committed_at IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS recall_state_log_at ON catalog.recall_state_log (at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS partner_event_migration_at
    ON catalog.partner_event (created_at) WHERE entity = 'migration';

CREATE TABLE IF NOT EXISTS catalog.price_display (
    singleton          boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    show_to            text[] NOT NULL DEFAULT '{ArchiveAdmin,DataSteward,others}'
                       CHECK (show_to <@ '{ArchiveAdmin,DataSteward,others}'::text[]),
    set_by_identity_id bigint REFERENCES catalog.identity,
    set_at             timestamptz,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    entity_version     bigint NOT NULL DEFAULT 1
);

INSERT INTO catalog.price_display DEFAULT VALUES ON CONFLICT (singleton) DO NOTHING;

CREATE OR REPLACE TRIGGER price_display_bump BEFORE UPDATE ON catalog.price_display
    FOR EACH ROW EXECUTE FUNCTION catalog.bump_version();
