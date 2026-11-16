-- Migration 0029: the partner API's migration lifecycle, typed lifecycle
-- events and recall batches (portal-api.md sections 5.17-5.19; ported from
-- line B, PR #10: 8344f8a, c876e30, 88c525b).
--
-- 1. migration_prefix: manual cutover, cancel; a cancelled prefix may be
--    started again. migration_run (an external copier's reported runs) and
--    migration_exclusion (objects an ArchiveAdmin took out of the
--    verification bar; they stay on the source).
-- 2. catalog.migration_source_deletes(): the cutover report. Files deleted
--    through an edge while their bytes still live in the migration's source.
--    OmniFile never applies them to the source (decision 40).
-- 3. catalog.partner_event: typed lifecycle events (migration.*,
--    recall.state, restore.state, proposal.decided), written by triggers in
--    the transaction of the change. They take their ids from the edge
--    feed's sequence and are served merged with it by (txid, event_id), so
--    one cursor and one retention floor cover both.
-- 4. recall_batch, recall_batch_item: recalls of paths or a dataset by a due
--    date, planned per file with an ETA and a fee.

-- ---------------------------------------------------------------------------
-- 1. Migrations
-- ---------------------------------------------------------------------------
ALTER TABLE catalog.migration_prefix
    -- auto: flip as soon as verified. manual: wait at `verified` for
    -- POST /v1/migrations/{id}/cutover (cutover_requested_at).
    ADD COLUMN cutover              text NOT NULL DEFAULT 'auto'
                                    CHECK (cutover IN ('auto', 'manual')),
    ADD COLUMN cutover_requested_at timestamptz,
    ADD COLUMN cutover_requested_by text,
    ADD COLUMN cancelled_at         timestamptz,
    ADD COLUMN cancelled_by         text,
    DROP CONSTRAINT migration_prefix_state_check,
    ADD CONSTRAINT migration_prefix_state_check
        CHECK (state IN ('pending', 'copying', 'verifying', 'verified', 'cut_over', 'failed',
                         'cancelled')),
    ADD CONSTRAINT migration_prefix_cancelled
        CHECK (state <> 'cancelled' OR cancelled_at IS NOT NULL),
    DROP CONSTRAINT migration_prefix_uq;
-- One migration per prefix of a source, unless it was cancelled.
CREATE UNIQUE INDEX migration_prefix_live_uq
    ON catalog.migration_prefix (source_id, prefix) WHERE state <> 'cancelled';

-- Runs an external copier (Prizmanyx) reports. Display only: OmniFile
-- verifies every file itself (decision 24). A run's start moves the journal
-- gate: nothing is flipped until the bucket's events are applied past it.
CREATE TABLE catalog.migration_run (
    migration_id bigint NOT NULL REFERENCES catalog.migration_prefix,
    run_ref      text COLLATE "C" NOT NULL CHECK (length(run_ref) BETWEEN 1 AND 256),
    state        text NOT NULL CHECK (state IN ('running', 'succeeded', 'failed')),
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz,
    counters     jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(counters) = 'object'),
    reported_by  text NOT NULL,
    reported_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (migration_id, run_ref)
);

-- Objects an ArchiveAdmin excluded (with re-authentication) from a
-- migration's verification bar: ones that can never be verified. They are
-- not flipped and stay served from the source, so the dataset itself
-- doesn't reach `cut_over` while they are live.
CREATE TABLE catalog.migration_exclusion (
    migration_id bigint NOT NULL REFERENCES catalog.migration_prefix,
    object_key   text COLLATE "C" NOT NULL,
    reason       text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    excluded_by  text NOT NULL,
    excluded_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (migration_id, object_key)
);

-- ---------------------------------------------------------------------------
-- 2. The cutover report (decision 40)
-- ---------------------------------------------------------------------------
-- One row per source object of the migration's prefix whose file was deleted
-- through an edge (its last tombstone names the edge) and which may still be
-- in the source: deletes the S3 change feed brought in (no edge) already
-- happened there, and a location marked `deleted` is gone. The edge is the
-- actor until namespace sync records who (the user) per operation.
CREATE FUNCTION catalog.migration_source_deletes(p_migration_id bigint)
RETURNS TABLE (location_id bigint, file_id bigint, container text, object_key text,
               backend_version_id text, etag text, deleted_at timestamptz, edge_id bigint)
LANGUAGE sql STABLE AS $$
    SELECT l.location_id, l.file_id, l.container, l.object_key, l.backend_version_id, l.etag,
           f.deleted_at, t.edge_id
      FROM catalog.migration_prefix m
      JOIN catalog.source s ON s.source_id = m.source_id
      JOIN catalog.location l
        ON l.backend_id = s.backend_id AND l.container = s.container
       AND l.object_key COLLATE "C" LIKE replace(replace(replace(m.prefix, '\', '\\'), '%', '\%'),
                                                 '_', '\_') || '%'
       AND l.role IN ('primary', 'secondary') AND l.state IN ('committed', 'deleting')
      JOIN catalog.file f ON f.file_id = l.file_id AND f.deleted_at IS NOT NULL
      JOIN LATERAL (SELECT v.edge_id FROM catalog.file_version v
                     WHERE v.file_id = f.file_id AND v.kind = 'tombstone'
                     ORDER BY v.seq DESC LIMIT 1) t ON t.edge_id IS NOT NULL
     WHERE m.migration_id = p_migration_id
$$;

-- ---------------------------------------------------------------------------
-- 3. Typed partner events
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.partner_event (
    -- The edge feed's sequence: (txid, event_id) orders both feeds together.
    event_id   bigint NOT NULL DEFAULT nextval('catalog.feed_event_id_seq') PRIMARY KEY,
    txid       xid8 NOT NULL DEFAULT pg_current_xact_id(),
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Open on the wire; a type is added here when something emits it.
    -- `migration.handed_off` is reserved (decision 35).
    type       text NOT NULL
               CHECK (type IN ('migration.started', 'migration.verified', 'migration.cut_over',
                               'migration.handed_off', 'migration.failed',
                               'migration.cancelled', 'recall.state', 'restore.state',
                               'proposal.decided')),
    dataset_id bigint NOT NULL,
    entity     text NOT NULL CHECK (entity IN ('migration', 'recall', 'restore', 'proposal')),
    entity_id  bigint NOT NULL,
    data       jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(data) = 'object')
);
CREATE INDEX partner_event_cursor ON catalog.partner_event (txid, event_id);

CREATE FUNCTION catalog.partner_ts(p timestamptz) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT to_char(p AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
$$;

CREATE FUNCTION catalog.partner_migration_event() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    t text;
    d jsonb;
BEGIN
    IF TG_OP = 'INSERT' THEN
        t := 'migration.started';
    ELSIF NEW.state IS DISTINCT FROM OLD.state THEN
        t := CASE NEW.state WHEN 'verified' THEN 'migration.verified'
                            WHEN 'cut_over' THEN 'migration.cut_over'
                            WHEN 'failed' THEN 'migration.failed'
                            WHEN 'cancelled' THEN 'migration.cancelled' END;
    END IF;
    IF t IS NULL THEN
        RETURN NULL;
    END IF;
    d := jsonb_build_object(
        'migration_id', NEW.migration_id::text, 'prefix', NEW.prefix,
        'from_backend_id', (SELECT s.backend_id::text FROM catalog.source s
                             WHERE s.source_id = NEW.source_id),
        'to_backend_id', NEW.to_backend_id::text, 'mode', NEW.mode, 'state', NEW.state);
    IF t = 'migration.cut_over' THEN
        d := d || jsonb_build_object(
            'source_deletes_pending',
            (SELECT count(*) FROM catalog.migration_source_deletes(NEW.migration_id)));
    END IF;
    INSERT INTO catalog.partner_event (type, dataset_id, entity, entity_id, data)
    VALUES (t, NEW.dataset_id, 'migration', NEW.migration_id, d);
    RETURN NULL;
END $$;
CREATE TRIGGER migration_partner_event AFTER INSERT OR UPDATE OF state
    ON catalog.migration_prefix
    FOR EACH ROW EXECUTE FUNCTION catalog.partner_migration_event();

CREATE FUNCTION catalog.partner_recall_event() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.state IS NOT DISTINCT FROM OLD.state THEN
        RETURN NULL;
    END IF;
    INSERT INTO catalog.partner_event (type, dataset_id, entity, entity_id, data)
    SELECT 'recall.state', f.dataset_id, 'recall', NEW.recall_id,
           jsonb_build_object('recall_id', NEW.recall_id::text, 'file_id', NEW.file_id::text,
                              'version_id', NEW.version_id::text, 'state', NEW.state,
                              'eta', catalog.partner_ts(NEW.eta))
      FROM catalog.file f WHERE f.file_id = NEW.file_id;
    RETURN NULL;
END $$;
CREATE TRIGGER recall_partner_event AFTER INSERT OR UPDATE OF state ON catalog.recall
    FOR EACH ROW EXECUTE FUNCTION catalog.partner_recall_event();

CREATE FUNCTION catalog.partner_restore_event() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.state IS NOT DISTINCT FROM OLD.state THEN
        RETURN NULL;
    END IF;
    INSERT INTO catalog.partner_event (type, dataset_id, entity, entity_id, data)
    SELECT 'restore.state', f.dataset_id, 'restore', NEW.restore_id,
           jsonb_build_object('restore_id', NEW.restore_id::text, 'file_id', l.file_id::text,
                              'state', NEW.state, 'triggered_by', NEW.triggered_by,
                              'migrate_on_complete', NEW.migrate_on_complete,
                              'expires_at', catalog.partner_ts(NEW.expires_at))
      FROM catalog.location l JOIN catalog.file f ON f.file_id = l.file_id
     WHERE l.location_id = NEW.location_id;
    RETURN NULL;
END $$;
CREATE TRIGGER restore_partner_event AFTER INSERT OR UPDATE OF state ON catalog.restore
    FOR EACH ROW EXECUTE FUNCTION catalog.partner_restore_event();

-- A proposal leaves `pending` (approved, rejected, withdrawn, superseded).
CREATE FUNCTION catalog.partner_proposal_event() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state = 'pending' AND NEW.state <> 'pending' THEN
        INSERT INTO catalog.partner_event (type, dataset_id, entity, entity_id, data)
        VALUES ('proposal.decided', NEW.dataset_id, 'proposal', NEW.proposal_id,
                jsonb_build_object('proposal_id', NEW.proposal_id::text, 'state', NEW.state,
                                   'proposed_by_app', NEW.proposed_by_app,
                                   'decided_at', catalog.partner_ts(NEW.decided_at)));
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER proposal_partner_event AFTER UPDATE OF state ON catalog.policy_proposal
    FOR EACH ROW EXECUTE FUNCTION catalog.partner_proposal_event();

-- Retention: partner events at or below the edge feed's dropped-through
-- cursor go with its partitions, so one floor answers 410 for both.
CREATE OR REPLACE FUNCTION catalog.feed_drop_before(p_before timestamptz) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    r record;
    top record;
    dropped integer := 0;
BEGIN
    FOR r IN SELECT partition FROM catalog.time_partition
              WHERE parent = 'catalog.feed_event'::regclass AND to_ts <= p_before
              ORDER BY from_ts LOOP
        EXECUTE format('SELECT txid, event_id FROM %s ORDER BY txid DESC, event_id DESC LIMIT 1',
                       r.partition) INTO top;
        IF top.txid IS NOT NULL THEN
            UPDATE catalog.feed_retention
               SET dropped_through_txid = greatest(dropped_through_txid, top.txid),
                   dropped_through_event = CASE WHEN top.txid >= dropped_through_txid
                                                THEN top.event_id
                                                ELSE dropped_through_event END,
                   dropped_at = now();
        END IF;
        DELETE FROM catalog.time_partition WHERE partition = r.partition;
        EXECUTE format('DROP TABLE %s', r.partition);
        dropped := dropped + 1;
    END LOOP;
    DELETE FROM catalog.partner_event e
     USING catalog.feed_retention fr
     WHERE (e.txid, e.event_id) <= (fr.dropped_through_txid, fr.dropped_through_event);
    RETURN dropped;
END $$;

-- ---------------------------------------------------------------------------
-- 4. Recall batches
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.recall_batch (
    batch_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id  bigint NOT NULL REFERENCES catalog.dataset,
    app_id      text NOT NULL,          -- the partner's azp; reads are limited to it
    paths       text[] COLLATE "C",     -- NULL: the whole dataset
    due_by      timestamptz,
    priority    text,                   -- as asked; NULL: the cheapest that meets due_by
    -- planning: files are being chosen; starting: recalls are being started;
    -- done: every planned file started or joined its recall.
    state       text NOT NULL DEFAULT 'planning'
                CHECK (state IN ('planning', 'starting', 'done')),
    files       bigint NOT NULL DEFAULT 0,
    bytes       bigint NOT NULL DEFAULT 0,
    planned     bigint NOT NULL DEFAULT 0,
    started     bigint NOT NULL DEFAULT 0,
    joined      bigint NOT NULL DEFAULT 0,
    online      bigint NOT NULL DEFAULT 0,
    skipped     bigint NOT NULL DEFAULT 0,
    late        bigint NOT NULL DEFAULT 0,
    eta         timestamptz,
    -- false when a file has no promised time (above the no-promise size).
    eta_known   boolean NOT NULL DEFAULT true,
    -- The planned files' fees, one total per currency ([{amount, currency}]):
    -- a migrating dataset's files can be priced by two providers' tables,
    -- and amounts in different currencies are never added.
    costs       jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(costs) = 'array'),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    done_at     timestamptz
);
CREATE INDEX recall_batch_app_idx ON catalog.recall_batch (app_id, batch_id);

CREATE TABLE catalog.recall_batch_item (
    batch_id   bigint NOT NULL REFERENCES catalog.recall_batch,
    file_id    bigint NOT NULL REFERENCES catalog.file,
    version_id bigint REFERENCES catalog.file_version,
    path       text COLLATE "C" NOT NULL,
    -- planned: chosen, its recall not started yet.
    status     text NOT NULL CHECK (status IN ('planned', 'started', 'joined', 'online',
                                               'skipped')),
    priority   text,
    reason     text,
    eta        timestamptz,
    late       boolean NOT NULL DEFAULT false,
    fee        numeric,  -- the retrieval fee estimate, in fee_currency
    fee_currency text CHECK (fee_currency ~ '^[A-Z]{3}$'),
    size       bigint NOT NULL DEFAULT 0,
    recall_id  bigint REFERENCES catalog.recall,
    PRIMARY KEY (batch_id, file_id)
);
CREATE INDEX recall_batch_item_planned
    ON catalog.recall_batch_item (batch_id, file_id) WHERE status = 'planned';
