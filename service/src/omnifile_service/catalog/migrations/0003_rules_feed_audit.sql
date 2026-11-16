-- Migration 0003: rules the database enforces, the edge event feed, the
-- audit log, time partitions and the as-of query.
--
-- Errors raised here use SQLSTATE class 'OF' so the API can map them:
--   OF001  directory not empty (tombstone a directory with live children)
--   OF002  WORM violation (retention, legal hold, locked policy)
--   OF003  policy not approved / proposal not approved
--   OF004  policy not possible on this storage layout (Smart + Archive, WORM)
--   OF005  actor not set for an audited change
--   OF006  one-way flag cleared (explicit tier)
--   OF007  stale content version (a rename/metadata version cites old bytes)
--
-- The service identifies the actor of a change with transaction-local
-- settings, which triggers read:
--   SET LOCAL omnifile.actor = 'user:<identity_id>' | 'app:<client id>'
--                            | 'edge:<edge_id>' | 'system:<job>';
--   SET LOCAL omnifile.origin_edge_id = '<edge_id>';   -- edge-caused change
--   SET LOCAL omnifile.origin_op_id = '<edge op id>';
-- (`SET LOCAL` and `set_config(..., true)` are both transaction-scoped.)

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------
CREATE FUNCTION catalog.current_actor() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('omnifile.actor', true), '')
$$;

-- entity_version + updated_at on every update.
CREATE FUNCTION catalog.bump_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.entity_version := OLD.entity_version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END $$;

CREATE TRIGGER file_bump BEFORE UPDATE ON catalog.file
    FOR EACH ROW EXECUTE FUNCTION catalog.bump_version();
CREATE TRIGGER dataset_bump BEFORE UPDATE ON catalog.dataset
    FOR EACH ROW EXECUTE FUNCTION catalog.bump_version();
CREATE TRIGGER share_bump BEFORE UPDATE ON catalog.share
    FOR EACH ROW EXECUTE FUNCTION catalog.bump_version();
CREATE TRIGGER recall_bump BEFORE UPDATE ON catalog.recall
    FOR EACH ROW EXECUTE FUNCTION catalog.bump_version();
CREATE TRIGGER restore_bump BEFORE UPDATE ON catalog.restore
    FOR EACH ROW EXECUTE FUNCTION catalog.bump_version();
CREATE TRIGGER migration_prefix_bump BEFORE UPDATE ON catalog.migration_prefix
    FOR EACH ROW EXECUTE FUNCTION catalog.bump_version();

-- ---------------------------------------------------------------------------
-- Time partitions (feed and audit). A registry, so retention can drop by
-- range without parsing partition bounds.
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.time_partition (
    parent     regclass NOT NULL,
    partition  regclass NOT NULL PRIMARY KEY,
    from_ts    timestamptz NOT NULL,
    to_ts      timestamptz NOT NULL,
    UNIQUE (parent, from_ts)
);

-- Create partitions from one `unit` back to `ahead` units forward (UTC).
-- Idempotent. The worker calls it daily; the migration seeds it.
CREATE FUNCTION catalog.ensure_time_partitions(p_parent regclass, p_unit text, p_ahead integer)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    step    interval := ('1 ' || p_unit)::interval;
    base    timestamptz := date_trunc(p_unit, now(), 'UTC');
    lo      timestamptz;
    hi      timestamptz;
    relname text;
    created integer := 0;
BEGIN
    IF p_unit NOT IN ('day', 'week', 'month') THEN
        RAISE EXCEPTION 'unit must be day, week or month, not %', p_unit;
    END IF;
    FOR i IN -1 .. p_ahead LOOP
        lo := base + step * i;
        hi := base + step * (i + 1);
        CONTINUE WHEN EXISTS (SELECT 1 FROM catalog.time_partition
                              WHERE parent = p_parent AND from_ts = lo);
        relname := format('%s_p%s', (SELECT c.relname FROM pg_class c WHERE c.oid = p_parent),
                          to_char(lo AT TIME ZONE 'UTC', 'YYYYMMDD'));
        EXECUTE format('CREATE TABLE catalog.%I PARTITION OF %s FOR VALUES FROM (%L) TO (%L)',
                       relname, p_parent, lo, hi);
        INSERT INTO catalog.time_partition (parent, partition, from_ts, to_ts)
            VALUES (p_parent, format('catalog.%I', relname)::regclass, lo, hi);
        created := created + 1;
    END LOOP;
    RETURN created;
END $$;

-- ---------------------------------------------------------------------------
-- Audit log (append-only; partitioned by month, retention by DROP)
-- ---------------------------------------------------------------------------
CREATE SEQUENCE catalog.audit_log_id_seq AS bigint;
CREATE TABLE catalog.audit_log (
    audit_id     bigint NOT NULL DEFAULT nextval('catalog.audit_log_id_seq'),
    at           timestamptz NOT NULL DEFAULT now(),
    -- 'user:<identity_id>', 'app:<client id>', 'edge:<edge_id>', 'system:<job>'
    actor        text NOT NULL,
    actor_sid    catalog.sid,
    proof        text CHECK (proof IN ('claimed', 'proven')),
    trust_level  text CHECK (trust_level IN ('proven', 'host_trusted')),
    -- e.g. share.open, recall.request, acl.change, policy.approve,
    -- share.worm.enable, share.worm.lock, share.worm.retention, hold.set,
    -- hold.release, version.retention, version.legal_hold, tier_pin.set ...
    action       text NOT NULL,
    decision     text NOT NULL CHECK (decision IN ('allow', 'deny', 'info')),
    dataset_id   bigint,
    share_id     bigint,
    file_id      bigint,
    version_id   bigint,
    client_addr  inet,
    details      jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (at, audit_id)
) PARTITION BY RANGE (at);
CREATE TABLE catalog.audit_log_default PARTITION OF catalog.audit_log DEFAULT;
-- "Everything that happened to this file", "everything this person did".
CREATE INDEX audit_log_file ON catalog.audit_log (file_id, at) WHERE file_id IS NOT NULL;
CREATE INDEX audit_log_actor ON catalog.audit_log (actor, at);
SELECT catalog.ensure_time_partitions('catalog.audit_log', 'month', 6);

CREATE FUNCTION catalog.audit_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = 'OF002';
END $$;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON catalog.audit_log
    FOR EACH ROW EXECUTE FUNCTION catalog.audit_append_only();

-- Write one audit row; the actor must be set.
CREATE FUNCTION catalog.audit(p_action text, p_dataset_id bigint, p_share_id bigint,
                              p_file_id bigint, p_version_id bigint, p_details jsonb)
RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    a text := catalog.current_actor();
BEGIN
    IF a IS NULL THEN
        RAISE EXCEPTION 'omnifile.actor must be set for %', p_action USING ERRCODE = 'OF005';
    END IF;
    INSERT INTO catalog.audit_log (actor, action, decision, dataset_id, share_id, file_id,
                                   version_id, details)
    VALUES (a, p_action, 'allow', p_dataset_id, p_share_id, p_file_id, p_version_id,
            coalesce(p_details, '{}'::jsonb));
END $$;

-- ---------------------------------------------------------------------------
-- WORM on shares (decision 31): one-way once locked, audited always
-- ---------------------------------------------------------------------------
CREATE FUNCTION catalog.share_worm_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    layout text;
BEGIN
    IF NEW.worm_enabled THEN
        SELECT storage_layout INTO layout FROM catalog.dataset WHERE dataset_id = NEW.dataset_id;
        IF layout <> 'single_account' THEN
            RAISE EXCEPTION 'WORM needs a single_account dataset (in-place tiering)'
                USING ERRCODE = 'OF004';
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.worm_mode = 'locked' THEN
        IF NOT NEW.worm_enabled OR NEW.worm_mode IS DISTINCT FROM 'locked' THEN
            RAISE EXCEPTION 'share %: WORM is locked and cannot be disabled', OLD.share_id
                USING ERRCODE = 'OF002';
        END IF;
        IF NEW.worm_retention_days < OLD.worm_retention_days THEN
            RAISE EXCEPTION 'share %: locked retention can be extended, never shortened',
                OLD.share_id USING ERRCODE = 'OF002';
        END IF;
        IF NEW.worm_locked_at IS DISTINCT FROM OLD.worm_locked_at THEN
            RAISE EXCEPTION 'share %: lock time is fixed', OLD.share_id USING ERRCODE = 'OF002';
        END IF;
        -- The share's scope decides which versions are retained, so it is
        -- fixed once locked.
        IF NEW.dataset_id <> OLD.dataset_id OR NEW.root_dir_id IS DISTINCT FROM OLD.root_dir_id THEN
            RAISE EXCEPTION 'share %: a locked WORM share cannot move', OLD.share_id
                USING ERRCODE = 'OF002';
        END IF;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.worm_enabled THEN
        PERFORM catalog.audit('share.worm.enable', NEW.dataset_id, NULL, NULL, NULL,
            jsonb_build_object('mode', NEW.worm_mode, 'retention_days', NEW.worm_retention_days,
                               'share', NEW.name));
    ELSIF TG_OP = 'UPDATE' AND (OLD.worm_enabled, OLD.worm_mode, OLD.worm_retention_days,
                                OLD.worm_lock_deadline)
          IS DISTINCT FROM (NEW.worm_enabled, NEW.worm_mode, NEW.worm_retention_days,
                            NEW.worm_lock_deadline) THEN
        PERFORM catalog.audit(
            CASE WHEN NOT OLD.worm_enabled AND NEW.worm_enabled THEN 'share.worm.enable'
                 WHEN OLD.worm_enabled AND NOT NEW.worm_enabled THEN 'share.worm.disable'
                 WHEN OLD.worm_mode IS DISTINCT FROM NEW.worm_mode THEN 'share.worm.lock'
                 ELSE 'share.worm.retention' END,
            NEW.dataset_id, NEW.share_id, NULL, NULL,
            jsonb_build_object('old', jsonb_build_object('enabled', OLD.worm_enabled,
                                   'mode', OLD.worm_mode, 'retention_days', OLD.worm_retention_days),
                               'new', jsonb_build_object('enabled', NEW.worm_enabled,
                                   'mode', NEW.worm_mode, 'retention_days', NEW.worm_retention_days)));
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER share_worm_guard BEFORE INSERT OR UPDATE ON catalog.share
    FOR EACH ROW EXECUTE FUNCTION catalog.share_worm_guard();

CREATE FUNCTION catalog.share_worm_no_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.worm_mode = 'locked' THEN
        RAISE EXCEPTION 'share %: a locked WORM share cannot be deleted', OLD.share_id
            USING ERRCODE = 'OF002';
    END IF;
    IF OLD.worm_enabled THEN
        PERFORM catalog.audit('share.worm.delete', OLD.dataset_id, OLD.share_id, NULL, NULL,
            jsonb_build_object('share', OLD.name, 'mode', OLD.worm_mode));
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER share_worm_no_delete BEFORE DELETE ON catalog.share
    FOR EACH ROW EXECUTE FUNCTION catalog.share_worm_no_delete();

-- Retention for a version committed in folder p_parent_id: the longest
-- retention of the WORM shares whose root contains that folder (decision
-- 31 is per share). NULL when no WORM share covers it.
CREATE FUNCTION catalog.worm_retention_days(p_dataset_id bigint, p_parent_id bigint)
RETURNS integer
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE up(file_id, parent_id) AS (
        SELECT file_id, parent_id FROM catalog.file WHERE file_id = p_parent_id
        UNION
        SELECT f.file_id, f.parent_id FROM catalog.file f JOIN up ON f.file_id = up.parent_id
    )
    SELECT max(s.worm_retention_days)
      FROM catalog.share s
     WHERE s.dataset_id = p_dataset_id
       AND s.worm_enabled
       AND (s.root_dir_id IS NULL OR s.root_dir_id IN (SELECT file_id FROM up))
$$;

-- A dataset that has WORM shares stays single_account.
CREATE FUNCTION catalog.dataset_layout_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.storage_layout <> 'single_account' AND EXISTS (
        SELECT 1 FROM catalog.share WHERE dataset_id = NEW.dataset_id AND worm_enabled) THEN
        RAISE EXCEPTION 'dataset % has WORM shares and must stay single_account', NEW.dataset_id
            USING ERRCODE = 'OF004';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER dataset_layout_guard BEFORE UPDATE OF storage_layout ON catalog.dataset
    FOR EACH ROW EXECUTE FUNCTION catalog.dataset_layout_guard();

-- ---------------------------------------------------------------------------
-- Policies: approved by a person, and possible on the storage layout
-- ---------------------------------------------------------------------------
-- This is the validation hook the policy editor also calls before saving.
CREATE FUNCTION catalog.policy_layout_problem(p_dataset_id bigint, p_body jsonb)
RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN p_body->>'mode' = 'smart' AND EXISTS (
                 SELECT 1 FROM catalog.share WHERE dataset_id = p_dataset_id AND worm_enabled)
            THEN 'Smart tier is not offered on WORM shares'
        WHEN p_body->>'mode' = 'smart' AND p_body ? 'archive_day' AND (
                 SELECT storage_layout FROM catalog.dataset WHERE dataset_id = p_dataset_id)
                 = 'single_account'
            THEN 'Smart plus Archive needs two accounts; this dataset tiers in place'
    END
$$;

CREATE FUNCTION catalog.policy_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    p catalog.policy_proposal;
    problem text;
BEGIN
    IF NEW.proposal_id IS NOT NULL THEN
        SELECT * INTO p FROM catalog.policy_proposal WHERE proposal_id = NEW.proposal_id;
        IF p.state <> 'approved' OR p.dataset_id <> NEW.dataset_id THEN
            RAISE EXCEPTION 'proposal % is not approved for dataset %', NEW.proposal_id,
                NEW.dataset_id USING ERRCODE = 'OF003';
        END IF;
    END IF;
    problem := catalog.policy_layout_problem(NEW.dataset_id, NEW.body);
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION '%', problem USING ERRCODE = 'OF004';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER policy_guard BEFORE INSERT OR UPDATE OF body, proposal_id ON catalog.policy
    FOR EACH ROW EXECUTE FUNCTION catalog.policy_guard();

-- ---------------------------------------------------------------------------
-- Versions: commit bookkeeping, WORM retention, prune protection
-- ---------------------------------------------------------------------------
CREATE FUNCTION catalog.file_version_before() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    worm_days integer;
    current_content bigint;
BEGIN
    IF TG_OP = 'INSERT' AND NEW.seq IS NULL THEN
        SELECT coalesce(max(seq), 0) + 1 INTO NEW.seq
            FROM catalog.file_version WHERE file_id = NEW.file_id;
    END IF;
    -- A metadata or rename version carries the file's CURRENT content
    -- version: it never moves the bytes. The caller may omit it; if it cites
    -- one, it must be the current one, or the commit would roll the file
    -- back to older bytes. The row lock orders this against a concurrent
    -- content commit, which also updates the file row.
    IF TG_OP = 'INSERT' AND NEW.kind IN ('metadata', 'rename') THEN
        SELECT f.content_version_id INTO current_content
          FROM catalog.file f WHERE f.file_id = NEW.file_id FOR UPDATE;
        IF NEW.content_version_id IS NOT NULL THEN
            SELECT CASE WHEN v.kind = 'content' THEN v.version_id ELSE v.content_version_id END
              INTO NEW.content_version_id
              FROM catalog.file_version v WHERE v.version_id = NEW.content_version_id;
            IF NEW.content_version_id IS DISTINCT FROM current_content THEN
                RAISE EXCEPTION 'file %: content version % is not current (%)', NEW.file_id,
                    NEW.content_version_id, current_content USING ERRCODE = 'OF007';
            END IF;
        END IF;
        NEW.content_version_id := current_content;
    END IF;
    -- On a WORM share, a committed version is retained for retention_days.
    IF NEW.state = 'committed' AND NEW.retention_until IS NULL THEN
        worm_days := catalog.worm_retention_days(NEW.dataset_id, NEW.parent_id);
        IF worm_days IS NOT NULL THEN
            NEW.retention_until := NEW.committed_at + make_interval(days => worm_days);
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF OLD.retention_until IS NOT NULL
           AND (NEW.retention_until IS NULL OR NEW.retention_until < OLD.retention_until) THEN
            RAISE EXCEPTION 'version %: retention can be extended, never shortened',
                OLD.version_id USING ERRCODE = 'OF002';
        END IF;
        IF NEW.state = 'pruned' AND OLD.state <> 'pruned'
           AND (OLD.legal_hold OR OLD.retention_until > now()) THEN
            RAISE EXCEPTION 'version % is retained or on legal hold', OLD.version_id
                USING ERRCODE = 'OF002';
        END IF;
        IF OLD.retention_until IS DISTINCT FROM NEW.retention_until THEN
            PERFORM catalog.audit('version.retention', NEW.dataset_id, NULL, NEW.file_id,
                NEW.version_id, jsonb_build_object('old', OLD.retention_until,
                                                   'new', NEW.retention_until));
        END IF;
        IF OLD.legal_hold IS DISTINCT FROM NEW.legal_hold THEN
            PERFORM catalog.audit('version.legal_hold', NEW.dataset_id, NULL, NEW.file_id,
                NEW.version_id, jsonb_build_object('on', NEW.legal_hold));
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER file_version_before BEFORE INSERT OR UPDATE ON catalog.file_version
    FOR EACH ROW EXECUTE FUNCTION catalog.file_version_before();

CREATE FUNCTION catalog.file_version_no_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.legal_hold OR OLD.retention_until > now() THEN
        RAISE EXCEPTION 'version % is retained or on legal hold', OLD.version_id
            USING ERRCODE = 'OF002';
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER file_version_no_delete BEFORE DELETE ON catalog.file_version
    FOR EACH ROW EXECUTE FUNCTION catalog.file_version_no_delete();

-- Recompute the file's location summary (primary location, tier, restore
-- state) from its content version. Called whenever a version commits, a
-- location changes or a restore changes, so the edge's view of "archived,
-- restore running, restored until" never goes stale. Updates only on change.
CREATE FUNCTION catalog.file_resync_location(p_file_id bigint) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    v_loc   bigint;
    v_tier  text;
    v_state text;
    v_exp   timestamptz;
BEGIN
    SELECT l.location_id, l.logical_tier INTO v_loc, v_tier
      FROM catalog.file f
      JOIN catalog.location l ON l.version_id = f.content_version_id
     WHERE f.file_id = p_file_id AND l.role = 'primary' AND l.state = 'committed';
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

-- When a version commits it becomes current: the previous one is
-- superseded at the same instant, and the file points at it. Content
-- versions also become the file's content version; the others carry it.
CREATE FUNCTION catalog.file_version_after_commit() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE catalog.file_version
       SET superseded_at = NEW.committed_at
     WHERE file_id = NEW.file_id AND state = 'committed' AND superseded_at IS NULL
       AND version_id <> NEW.version_id;
    UPDATE catalog.file f
       SET current_version_id = NEW.version_id,
           content_version_id = CASE NEW.kind
                                    WHEN 'content' THEN NEW.version_id
                                    WHEN 'tombstone' THEN f.content_version_id
                                    ELSE NEW.content_version_id END
     WHERE f.file_id = NEW.file_id;
    PERFORM catalog.file_resync_location(NEW.file_id);
    RETURN NULL;
END $$;
CREATE TRIGGER file_version_commit_ins AFTER INSERT ON catalog.file_version
    FOR EACH ROW WHEN (NEW.state = 'committed')
    EXECUTE FUNCTION catalog.file_version_after_commit();
CREATE TRIGGER file_version_commit_upd AFTER UPDATE OF state ON catalog.file_version
    FOR EACH ROW WHEN (OLD.state = 'pending' AND NEW.state = 'committed')
    EXECUTE FUNCTION catalog.file_version_after_commit();

-- "Folder X as of time T": entries valid at T, excluding deletes. Pruned
-- versions are gone; the API refuses T older than the share's rewind depth.
CREATE FUNCTION catalog.folder_as_of(p_dir_id bigint, p_at timestamptz)
RETURNS SETOF catalog.file_version
LANGUAGE sql STABLE AS $$
    SELECT v.*
      FROM catalog.file_version v
     WHERE v.parent_id = p_dir_id
       AND v.state = 'committed'
       AND v.committed_at <= p_at
       AND (v.superseded_at IS NULL OR v.superseded_at > p_at)
       AND v.kind <> 'tombstone'
     ORDER BY v.name
$$;

-- ---------------------------------------------------------------------------
-- Files: directory tombstones; locations -> file summary; tier flags
-- ---------------------------------------------------------------------------
-- A directory is tombstoned only after its children, so the edge always
-- removes leaves first (no recursive delete events).
CREATE FUNCTION catalog.file_dir_tombstone_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kind = 'dir' AND OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL
       AND EXISTS (SELECT 1 FROM catalog.file
                    WHERE parent_id = NEW.file_id AND deleted_at IS NULL) THEN
        RAISE EXCEPTION 'directory % is not empty', NEW.file_id USING ERRCODE = 'OF001';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER file_dir_tombstone_guard BEFORE UPDATE OF deleted_at ON catalog.file
    FOR EACH ROW EXECUTE FUNCTION catalog.file_dir_tombstone_guard();

-- Decision 29: explicit_tier_set never goes back to false.
CREATE FUNCTION catalog.location_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.explicit_tier_set AND NOT NEW.explicit_tier_set THEN
        RAISE EXCEPTION 'location %: a blob with an explicit tier never returns to Smart',
            OLD.location_id USING ERRCODE = 'OF006';
    END IF;
    -- Decision 31: the copies of a retained or held version are never
    -- deleted. Recall copies and staging copies are not the record and may go.
    -- (A migration source of a retained version is deleted only after its
    -- retention ends.)
    -- The role of such a location is frozen too, so it can't be relabelled
    -- (say, to staging) and then deleted.
    IF ((OLD.role IN ('primary', 'secondary') AND OLD.state NOT IN ('deleting', 'deleted')
         AND (TG_OP = 'DELETE' OR NEW.state IN ('deleting', 'deleted')))
        OR (TG_OP = 'UPDATE' AND NEW.role IS DISTINCT FROM OLD.role))
       AND EXISTS (SELECT 1 FROM catalog.file_version v
                    WHERE v.version_id = OLD.version_id
                      AND (v.legal_hold OR v.retention_until > now())) THEN
        RAISE EXCEPTION 'location %: its version is retained or on legal hold', OLD.location_id
            USING ERRCODE = 'OF002';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;
CREATE TRIGGER location_guard BEFORE UPDATE OR DELETE ON catalog.location
    FOR EACH ROW EXECUTE FUNCTION catalog.location_guard();

-- Location and restore changes refresh the file's location summary.
CREATE FUNCTION catalog.location_sync_file() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM catalog.file_resync_location(NEW.file_id);
    RETURN NULL;
END $$;
CREATE TRIGGER location_sync_file AFTER INSERT OR UPDATE ON catalog.location
    FOR EACH ROW EXECUTE FUNCTION catalog.location_sync_file();

CREATE FUNCTION catalog.restore_sync_file() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM catalog.file_resync_location(l.file_id)
       FROM catalog.location l WHERE l.location_id = NEW.location_id;
    RETURN NULL;
END $$;
CREATE TRIGGER restore_sync_file AFTER INSERT OR UPDATE ON catalog.restore
    FOR EACH ROW EXECUTE FUNCTION catalog.restore_sync_file();

-- Holds and tier pins are always audited.
CREATE FUNCTION catalog.audit_hold_pin() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    what text := CASE TG_TABLE_NAME WHEN 'hold' THEN 'hold' ELSE 'tier_pin' END;
BEGIN
    PERFORM catalog.audit(
        what || CASE WHEN TG_OP = 'INSERT' THEN '.set'
                     WHEN NEW.released_at IS NOT NULL AND OLD.released_at IS NULL THEN '.release'
                     ELSE '.change' END,
        NEW.dataset_id, NULL, NEW.file_id, NULL, to_jsonb(NEW));
    RETURN NULL;
END $$;
CREATE TRIGGER hold_audit AFTER INSERT OR UPDATE ON catalog.hold
    FOR EACH ROW EXECUTE FUNCTION catalog.audit_hold_pin();
CREATE TRIGGER tier_pin_audit AFTER INSERT OR UPDATE ON catalog.tier_pin
    FOR EACH ROW EXECUTE FUNCTION catalog.audit_hold_pin();

-- ---------------------------------------------------------------------------
-- The edge event feed
-- ---------------------------------------------------------------------------
-- Ordered by (txid, event_id), not by event_id alone: ids are handed out
-- at insert, but transactions commit in a different order, so a reader
-- paging by id could pass an id whose transaction commits later and never
-- see it. A reader only returns events with txid below the oldest running
-- transaction (pg_snapshot_xmin), and no transaction below that line can
-- still add events, so the (txid, event_id) order is final. The cost: one
-- long-running transaction holds the feed back. Keep transactions short;
-- the worker alerts when the horizon lags.
--
-- Events are thin (entity, id, version); the API joins the current row
-- when it serves them. An edge that sees the same entity_version twice, or
-- an older one, does nothing.
CREATE SEQUENCE catalog.feed_event_id_seq AS bigint;
CREATE TABLE catalog.feed_event (
    event_id       bigint NOT NULL DEFAULT nextval('catalog.feed_event_id_seq'),
    txid           xid8 NOT NULL DEFAULT pg_current_xact_id(),
    created_at     timestamptz NOT NULL DEFAULT now(),
    dataset_id     bigint,  -- NULL: for every edge
    edge_id        bigint,  -- NULL: every edge serving the dataset
    entity         text NOT NULL
                   CHECK (entity IN ('file', 'dataset', 'share', 'acl', 'recall', 'edge')),
    entity_id      bigint NOT NULL,
    entity_version bigint,
    op             text NOT NULL CHECK (op IN ('upsert', 'rename', 'delete', 'conflict')),
    -- Set when an edge's own change caused the event, so it can skip echoes.
    origin_edge_id bigint,
    origin_op_id   text,
    detail         jsonb,  -- only for 'conflict': why the edge change lost
    PRIMARY KEY (created_at, event_id)
) PARTITION BY RANGE (created_at);
CREATE TABLE catalog.feed_event_default PARTITION OF catalog.feed_event DEFAULT;
CREATE INDEX feed_event_cursor ON catalog.feed_event (txid, event_id);
SELECT catalog.ensure_time_partitions('catalog.feed_event', 'week', 8);

-- Partition health. The worker runs ensure_time_partitions daily, so the
-- DEFAULT partitions should stay empty: a row there means a partition was
-- missing when it was written, and creating the partition for that range
-- will then fail until the rows are moved. The worker alerts on any row
-- this returns (sampled up to 1,000 rows, so the check stays cheap).
CREATE FUNCTION catalog.default_partition_rows()
RETURNS TABLE (parent regclass, sampled_rows bigint)
LANGUAGE sql STABLE AS $$
    SELECT * FROM (
        SELECT 'catalog.feed_event'::regclass,
               (SELECT count(*) FROM (SELECT 1 FROM catalog.feed_event_default LIMIT 1000) s)
        UNION ALL
        SELECT 'catalog.audit_log'::regclass,
               (SELECT count(*) FROM (SELECT 1 FROM catalog.audit_log_default LIMIT 1000) s)
    ) t (parent, sampled_rows)
    WHERE sampled_rows > 0
$$;

-- Everything older than this was dropped; a cursor at or below it must
-- re-snapshot. One row.
CREATE TABLE catalog.feed_retention (
    singleton             boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    dropped_through_txid  xid8 NOT NULL DEFAULT '0'::xid8,
    dropped_through_event bigint NOT NULL DEFAULT 0,
    dropped_at            timestamptz
);
INSERT INTO catalog.feed_retention DEFAULT VALUES;

CREATE FUNCTION catalog.feed_horizon() RETURNS xid8
LANGUAGE sql VOLATILE AS $$
    SELECT pg_snapshot_xmin(pg_current_snapshot())
$$;

-- Read up to p_limit events after the cursor, below p_horizon (taken from
-- catalog.feed_horizon() in an earlier statement; an older horizon is only
-- more conservative). If fewer than p_limit rows come back, the caller may
-- move its cursor to (p_horizon, 0): every event below the horizon has been
-- seen.
CREATE FUNCTION catalog.feed_read(p_after_txid xid8, p_after_event bigint, p_horizon xid8,
                                  p_edge_id bigint, p_dataset_ids bigint[], p_limit integer)
RETURNS SETOF catalog.feed_event
LANGUAGE sql STABLE AS $$
    SELECT e.*
      FROM catalog.feed_event e
     WHERE (e.txid, e.event_id) > (p_after_txid, p_after_event)
       AND e.txid < p_horizon
       AND (e.dataset_id IS NULL OR e.dataset_id = ANY (p_dataset_ids))
       AND (e.edge_id IS NULL OR e.edge_id = p_edge_id)
     ORDER BY e.txid, e.event_id
     LIMIT p_limit
$$;

-- Drop feed partitions that ended before p_before, recording the highest
-- cursor dropped.
CREATE FUNCTION catalog.feed_drop_before(p_before timestamptz) RETURNS integer
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
    RETURN dropped;
END $$;

CREATE FUNCTION catalog.feed_origin_edge() RETURNS bigint
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('omnifile.origin_edge_id', true), '')::bigint
$$;
CREATE FUNCTION catalog.feed_origin_op() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('omnifile.origin_op_id', true), '')
$$;

-- Files: statement-level, so seeding 10^8 rows inserts events in bulk.
CREATE FUNCTION catalog.feed_file_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO catalog.feed_event (dataset_id, entity, entity_id, entity_version, op,
                                    origin_edge_id, origin_op_id)
    SELECT n.dataset_id, 'file', n.file_id, n.entity_version,
           CASE WHEN n.deleted_at IS NULL THEN 'upsert' ELSE 'delete' END,
           catalog.feed_origin_edge(), catalog.feed_origin_op()
      FROM new_rows n
     ORDER BY n.file_id;
    RETURN NULL;
END $$;
CREATE TRIGGER file_feed_insert AFTER INSERT ON catalog.file
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION catalog.feed_file_insert();

-- Updates emit only when something the edge replica holds has changed;
-- policy clocks and scheduler fields don't reach the edge.
CREATE FUNCTION catalog.feed_file_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO catalog.feed_event (dataset_id, entity, entity_id, entity_version, op,
                                    origin_edge_id, origin_op_id)
    SELECT n.dataset_id, 'file', n.file_id, n.entity_version,
           CASE WHEN n.deleted_at IS NOT NULL AND o.deleted_at IS NULL THEN 'delete'
                WHEN (n.parent_id, n.name) IS DISTINCT FROM (o.parent_id, o.name) THEN 'rename'
                ELSE 'upsert' END,
           catalog.feed_origin_edge(), catalog.feed_origin_op()
      FROM new_rows n
      JOIN old_rows o USING (file_id)
     WHERE (n.parent_id, n.name, n.kind, n.symlink_target, n.size, n.mtime_ns, n.uid, n.gid,
            n.mode, n.owner_sid, n.acl_id, n.state, n.resident_bytes, n.crc64nvme,
            n.content_version_id, n.primary_location_id, n.remote_tier, n.restore_state,
            n.restore_expires_at, n.stub_version_id, n.stub_resident_bytes, n.deleted_at)
           IS DISTINCT FROM
           (o.parent_id, o.name, o.kind, o.symlink_target, o.size, o.mtime_ns, o.uid, o.gid,
            o.mode, o.owner_sid, o.acl_id, o.state, o.resident_bytes, o.crc64nvme,
            o.content_version_id, o.primary_location_id, o.remote_tier, o.restore_state,
            o.restore_expires_at, o.stub_version_id, o.stub_resident_bytes, o.deleted_at)
     ORDER BY n.file_id;
    RETURN NULL;
END $$;
CREATE TRIGGER file_feed_update AFTER UPDATE ON catalog.file
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION catalog.feed_file_update();

-- Low-volume entities: row-level.
CREATE FUNCTION catalog.feed_row() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    j jsonb := to_jsonb(NEW);
    ds bigint;
    target_edge bigint;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'recall' THEN
            SELECT dataset_id INTO ds FROM catalog.file WHERE file_id = NEW.file_id;
            target_edge := (j->>'edge_id')::bigint;
        WHEN 'share' THEN
            ds := (j->>'dataset_id')::bigint;
            target_edge := (j->>'edge_id')::bigint;
        ELSE
            ds := (j->>'dataset_id')::bigint;
    END CASE;
    INSERT INTO catalog.feed_event (dataset_id, edge_id, entity, entity_id, entity_version, op,
                                    origin_edge_id, origin_op_id)
    VALUES (ds, target_edge, TG_ARGV[0], (j->>TG_ARGV[1])::bigint,
            (j->>'entity_version')::bigint, 'upsert',
            catalog.feed_origin_edge(), catalog.feed_origin_op());
    RETURN NULL;
END $$;
CREATE TRIGGER share_feed AFTER INSERT OR UPDATE ON catalog.share
    FOR EACH ROW EXECUTE FUNCTION catalog.feed_row('share', 'share_id');
CREATE TRIGGER dataset_feed AFTER INSERT OR UPDATE ON catalog.dataset
    FOR EACH ROW EXECUTE FUNCTION catalog.feed_row('dataset', 'dataset_id');
CREATE TRIGGER recall_feed AFTER INSERT OR UPDATE ON catalog.recall
    FOR EACH ROW EXECUTE FUNCTION catalog.feed_row('recall', 'recall_id');
CREATE TRIGGER acl_feed AFTER INSERT ON catalog.acl
    FOR EACH ROW EXECUTE FUNCTION catalog.feed_row('acl', 'acl_id');
