-- The bulk seed path (docs/design/catalog-seeding.md section 6, M7 scale).
--
-- A seed batch is COPYed into an unlogged staging table, then merged into
-- the catalog by one owner-run function, `catalog.seed_merge`, in a few
-- set-based statements. The rows it writes are already in their final,
-- committed form: each file names its content version and primary location,
-- each version is committed with its WORM retention and legal hold, each
-- location carries its WORM flag, and the retention audit rows are written in
-- one INSERT. The statement-level feed trigger on catalog.file still emits one
-- feed event per file, in one statement per batch.
--
-- The per-row triggers on catalog.file_version and catalog.location would
-- only redo that work row by row (about nine tenths of seeding's time, M7.md),
-- so their INSERT triggers are skipped while seed_merge runs:
-- `catalog.seed_bypassed()` is true only inside it (the function turns
-- omnifile.seed_bypass on at its start and off before it returns or raises,
-- and current_user is the catalog owner only inside an owner-run function).
-- It sets the variable with set_config, not a SET clause: on PostgreSQL 15
-- and later only a superuser may put a custom parameter in a function's SET
-- clause, and the migrations run as Azure's administrator, which isn't one.
-- An app that sets the variable
-- itself still runs as its own role, so its inserts fire every trigger. No
-- other catalog function inserts into these tables (a test checks), and the
-- UPDATE triggers never skip.
--
-- The skip is an optimisation, not a dependency: if it is not active (for
-- example after the catalog's owner is renamed), the triggers fire on rows
-- that are already final and change nothing but the file's entity_version.

-- One seed batch's objects, between COPY and merge. Unlogged: the rows live
-- for one transaction, and a crash loses only a batch that never committed.
CREATE SEQUENCE catalog.seed_stage_id_seq;

CREATE UNLOGGED TABLE catalog.seed_stage (
    stage_id           bigint NOT NULL,
    ord                integer NOT NULL,
    parent_id          bigint NOT NULL,
    name               text COLLATE "C" NOT NULL,
    object_key         text COLLATE "C" NOT NULL,
    backend_version_id text,
    size               bigint NOT NULL CHECK (size >= 0),
    mtime_ns           bigint NOT NULL,
    last_modified      timestamptz NOT NULL,
    logical_tier       catalog.logical_tier NOT NULL,
    storage_class      text NOT NULL,
    etag               text,
    md5                bytea
);
CREATE INDEX seed_stage_batch ON catalog.seed_stage (stage_id);

-- True only inside seed_merge. The owner's name is fixed when this migration
-- runs (as the owner): an owner-run function's statements run as it. Every
-- name is schema-qualified, since it runs under the caller's search_path (a
-- SET clause would stop it being inlined into the triggers' WHEN).
DO $do$
BEGIN
    EXECUTE format(
        $f$CREATE FUNCTION catalog.seed_bypassed() RETURNS boolean
               LANGUAGE sql STABLE
           AS $b$ SELECT coalesce(pg_catalog.current_setting('omnifile.seed_bypass', true),
                                  ''::pg_catalog.text) OPERATOR(pg_catalog.=) 'on'::pg_catalog.text
                         AND current_user OPERATOR(pg_catalog.=) %L::pg_catalog.name $b$ $f$,
        current_user);
END $do$;

-- INSERT triggers that seed_merge makes up for skip while it runs. Triggers
-- that fired on INSERT and UPDATE are split, so an UPDATE never skips; the
-- names keep each table's firing order (alphabetical within a timing).
DROP TRIGGER file_version_before ON catalog.file_version;
CREATE TRIGGER file_version_before_ins BEFORE INSERT ON catalog.file_version
    FOR EACH ROW WHEN (NOT catalog.seed_bypassed())
    EXECUTE FUNCTION catalog.file_version_before();
CREATE TRIGGER file_version_before_upd BEFORE UPDATE ON catalog.file_version
    FOR EACH ROW EXECUTE FUNCTION catalog.file_version_before();

DROP TRIGGER file_version_commit_floor_ins ON catalog.file_version;
CREATE TRIGGER file_version_commit_floor_ins BEFORE INSERT ON catalog.file_version
    FOR EACH ROW
    WHEN (NEW.state = 'committed' AND NEW.committed_at IS NOT NULL
          AND NOT catalog.seed_bypassed())
    EXECUTE FUNCTION catalog.file_version_commit_floor();

DROP TRIGGER file_version_hold_on_commit ON catalog.file_version;
CREATE TRIGGER file_version_hold_on_commit_ins BEFORE INSERT ON catalog.file_version
    FOR EACH ROW WHEN (NOT catalog.seed_bypassed())
    EXECUTE FUNCTION catalog.file_version_hold_on_commit();
CREATE TRIGGER file_version_hold_on_commit_upd BEFORE UPDATE ON catalog.file_version
    FOR EACH ROW EXECUTE FUNCTION catalog.file_version_hold_on_commit();

DROP TRIGGER file_version_commit_ins ON catalog.file_version;
CREATE TRIGGER file_version_commit_ins AFTER INSERT ON catalog.file_version
    FOR EACH ROW WHEN (NEW.state = 'committed' AND NOT catalog.seed_bypassed())
    EXECUTE FUNCTION catalog.file_version_after_commit();

DROP TRIGGER file_version_worm_mark_ins ON catalog.file_version;
CREATE TRIGGER file_version_worm_mark_ins AFTER INSERT ON catalog.file_version
    FOR EACH ROW
    WHEN ((NEW.retention_until IS NOT NULL OR NEW.legal_hold) AND NOT catalog.seed_bypassed())
    EXECUTE FUNCTION catalog.file_version_worm_mark();

DROP TRIGGER location_worm_mark ON catalog.location;
CREATE TRIGGER location_worm_mark_ins BEFORE INSERT ON catalog.location
    FOR EACH ROW WHEN (NOT catalog.seed_bypassed())
    EXECUTE FUNCTION catalog.location_worm_mark();
CREATE TRIGGER location_worm_mark_upd BEFORE UPDATE ON catalog.location
    FOR EACH ROW EXECUTE FUNCTION catalog.location_worm_mark();

DROP TRIGGER location_sync_file ON catalog.location;
CREATE TRIGGER location_sync_file_ins AFTER INSERT ON catalog.location
    FOR EACH ROW WHEN (NOT catalog.seed_bypassed())
    EXECUTE FUNCTION catalog.location_sync_file();
CREATE TRIGGER location_sync_file_upd AFTER UPDATE ON catalog.location
    FOR EACH ROW EXECUTE FUNCTION catalog.location_sync_file();

-- Merge one staged batch into a dataset: a remote file per row, its committed
-- content version (sequence 1, committed at the object's LastModified) and its
-- committed primary location, exactly as the row-by-row seeder and the
-- triggers leave them. A row whose name is taken by a live entry, or whose
-- parent is not a live folder of the dataset, is not written. Returns the
-- staged rows written, with their file ids; the staged rows are removed.
--
-- Its cost is linear in the batch whatever the planner thinks of the staging
-- table (whose statistics are of other batches): the ids are drawn up front,
-- so the three inserts read the same staged rows and are never joined; each
-- folder's retention and hold is a jsonb lookup; and the check for a taken
-- name is one index probe per row. A writer that takes a name between that
-- check and the insert fails the batch (a unique violation), which the queue
-- retries; the retry finds the name taken.
--
-- What the skipped triggers would have done, and where it is done here:
--   file_version_before        sequence 1 (a new file); WORM retention from
--                              the folder's shares, per folder
--   file_version_commit_floor  nothing: a new file has no earlier version
--   file_version_hold_on_commit legal hold from the folder's holds, per folder
--   file_version_after_commit  the file names its version from the start
--   file_version_worm_mark,
--   location_worm_mark         worm_sync_needed on the location
--   location_sync_file         the file names its primary location and tier
-- and the `version.retention` audit row the row path writes when a version
-- commits on a WORM share.
CREATE FUNCTION catalog.seed_merge(
    p_stage bigint, p_dataset_id bigint, p_backend_id bigint, p_container text,
    p_acl_id bigint, p_uid bigint, p_gid bigint, p_mode integer, p_origin text,
    p_seen_at timestamptz)
RETURNS TABLE (ord integer, file_id bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
#variable_conflict use_column
DECLARE
    v_actor  text := catalog.current_actor();
    v_holds  boolean;
    -- Folder id -> {"d": its WORM days or null, "h": under a legal hold}, for
    -- the batch's live folders of the dataset only.
    v_folder jsonb;
    v_fseq   regclass := pg_get_serial_sequence('catalog.file', 'file_id')::regclass;
    v_vseq   regclass := pg_get_serial_sequence('catalog.file_version', 'version_id')::regclass;
    v_lseq   regclass := pg_get_serial_sequence('catalog.location', 'location_id')::regclass;
BEGIN
    SELECT EXISTS (SELECT 1 FROM catalog.hold h
                    WHERE h.dataset_id = p_dataset_id AND h.kind = 'legal'
                      AND h.released_at IS NULL)
      INTO v_holds;
    -- Once per folder, not per file: an index probe each (a scalar subquery,
    -- never a join the planner could turn into a scan of the catalog).
    SELECT coalesce(jsonb_object_agg(d.parent_id::text, jsonb_build_object(
               'd', catalog.worm_retention_days(p_dataset_id, d.parent_id),
               'h', v_holds AND catalog.legal_hold_covers(d.parent_id))), '{}'::jsonb)
      INTO v_folder
      FROM (SELECT DISTINCT st.parent_id FROM catalog.seed_stage st
             WHERE st.stage_id = p_stage) d
     WHERE (SELECT p.kind = 'dir' AND p.deleted_at IS NULL AND p.dataset_id = p_dataset_id
              FROM catalog.file p WHERE p.file_id = d.parent_id);
    IF v_actor IS NULL AND jsonb_path_exists(v_folder, '$.*.d ? (@ != null)') THEN
        RAISE EXCEPTION 'omnifile.actor must be set for version.retention'
            USING ERRCODE = 'OF005';
    END IF;
    -- The bypass, for the one statement below. set_config's is_local lasts
    -- to the end of the transaction, not of this function, so it is turned
    -- off again before the function returns, and before an error leaves it.
    PERFORM pg_catalog.set_config('omnifile.seed_bypass', 'on', true);
    RETURN QUERY
    WITH s AS MATERIALIZED (
        SELECT st.*, nextval(v_fseq) AS fid, nextval(v_vseq) AS vid, nextval(v_lseq) AS lid,
               st.last_modified
                   + make_interval(days => (v_folder -> st.parent_id::text ->> 'd')::integer)
                   AS retain,
               (v_folder -> st.parent_id::text ->> 'h')::boolean AS held
          FROM catalog.seed_stage st
         WHERE st.stage_id = p_stage
           AND v_folder ? st.parent_id::text
           AND (SELECT x.file_id FROM catalog.file x
                 WHERE x.parent_id = st.parent_id AND x.name = st.name
                   AND x.deleted_at IS NULL) IS NULL
         ORDER BY st.ord
    ),
    f AS (
        INSERT INTO catalog.file
            (file_id, dataset_id, parent_id, name, kind, state, size, resident_bytes, acl_id,
             mtime_ns, uid, gid, mode, origin, current_version_id, content_version_id,
             primary_location_id, remote_tier)
        OVERRIDING SYSTEM VALUE
        SELECT s.fid, p_dataset_id, s.parent_id, s.name, 'file', 'remote', s.size, 0, p_acl_id,
               s.mtime_ns, p_uid, p_gid, p_mode, p_origin, s.vid, s.vid, s.lid, s.logical_tier
          FROM s
    ),
    v AS (
        INSERT INTO catalog.file_version
            (version_id, file_id, dataset_id, seq, parent_id, name, kind, state, committed_at,
             size, acl_id, mtime_ns, uid, gid, mode, retention_until, legal_hold)
        OVERRIDING SYSTEM VALUE
        SELECT s.vid, s.fid, p_dataset_id, 1, s.parent_id, s.name, 'content', 'committed',
               s.last_modified, s.size, p_acl_id, s.mtime_ns, p_uid, p_gid, p_mode, s.retain,
               s.held
          FROM s
    ),
    l AS (
        INSERT INTO catalog.location
            (location_id, file_id, version_id, backend_id, container, object_key,
             backend_version_id, role, state, logical_tier, storage_class, size, etag, md5,
             committed_at, tier_changed_at, feed_seen_at, worm_sync_needed)
        OVERRIDING SYSTEM VALUE
        SELECT s.lid, s.fid, s.vid, p_backend_id, p_container, s.object_key,
               s.backend_version_id, 'primary', 'committed', s.logical_tier, s.storage_class,
               s.size, s.etag, s.md5, s.last_modified, s.last_modified, p_seen_at,
               s.retain IS NOT NULL OR s.held
          FROM s
    ),
    a AS (
        INSERT INTO catalog.audit_log
            (actor, action, decision, dataset_id, file_id, version_id, details)
        SELECT v_actor, 'version.retention', 'allow', p_dataset_id, s.fid, s.vid,
               jsonb_build_object('old', NULL::timestamptz, 'new', s.retain)
          FROM s WHERE s.retain IS NOT NULL
    )
    -- Data-modifying CTEs always run to completion, read or not.
    SELECT s.ord, s.fid FROM s;
    PERFORM pg_catalog.set_config('omnifile.seed_bypass', 'off', true);
    DELETE FROM catalog.seed_stage st WHERE st.stage_id = p_stage;
    RETURN;
EXCEPTION WHEN OTHERS THEN
    -- (The block's rollback has undone the 'on' already; this says so.)
    PERFORM pg_catalog.set_config('omnifile.seed_bypass', 'off', true);
    RAISE;
END $$;
