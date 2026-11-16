-- Migration 0059: a recall whose file was replaced or deleted is `superseded`.
--
-- A recall of a file's current content had no end but `available` or `failed`.
-- When someone replaced the file (or deleted it) while it was being recalled,
-- the edge's fill found another file under the name and reported the recall
-- failed, and its requesters were told "recall failed" for a file that was
-- simply no longer the one they had asked for (the chaos soak of 2026-11-08,
-- recall 209; control-api.md section 4.4).
--
-- Now the commit that makes a new content version current, or a tombstone,
-- ends the file's active recalls of what was its current content, in the same
-- transaction: state `superseded`, with why (`replaced` or `deleted`) and the
-- version that did it (whose actor is who, where the reader may be told).
--
-- A recall someone asked for of a version itself is left alone:
--   `pinned`        a read in `.snapshots` or Previous Versions. The edge says
--                   so. It stays: nothing tells the catalog the reader left.
--   `held_by_jobs`  how many unfinished version restores (decision 34) and
--                   annex placements wait on this recall. Kept by triggers on
--                   those tables, so it is right for every path that makes,
--                   moves or ends one, and for the rows that exist when this
--                   migration runs (the backfill below). When the last one
--                   ends, the recall is an ordinary recall again.
-- Only recalls of the then-current version can be reached, so a recall of an
-- older version needs neither.
--
-- A recall that is kept while its file moves on is marked with the version
-- that would have ended it (`moved_on_version_id`). If its last job then
-- lets go without having had its bytes (it failed, was cancelled, or its row
-- was deleted), nothing wants the recall any more and no edge will fill it:
-- it ends there, `superseded`, as it would have at that commit. A job that
-- succeeds closes its recall itself (`available`).
--
-- `superseded_by_version_id` is no foreign key: the version may be pruned
-- long after, and a recall that then names no one is still right.

ALTER TABLE catalog.recall
    DROP CONSTRAINT recall_state_check,
    ADD CONSTRAINT recall_state_check
        CHECK (state IN ('requested', 'rehydrating', 'copying', 'downloading',
                         'available', 'failed', 'cancelled', 'superseded')),
    ADD COLUMN pinned boolean NOT NULL DEFAULT false,
    ADD COLUMN held_by_jobs integer NOT NULL DEFAULT 0 CHECK (held_by_jobs >= 0),
    ADD COLUMN superseded_reason text CHECK (superseded_reason IN ('replaced', 'deleted')),
    ADD COLUMN superseded_by_version_id bigint,
    ADD COLUMN moved_on_version_id bigint,
    ADD CONSTRAINT recall_superseded_why
        CHECK ((state = 'superseded') = (superseded_reason IS NOT NULL));

-- The jobs that wait on a recall: a version restore until it is done or has
-- failed, a placement until it is placed, failed or cancelled.
CREATE FUNCTION catalog.recall_jobs(p_recall_id bigint) RETURNS integer
LANGUAGE sql STABLE AS $$
    SELECT ((SELECT count(*) FROM catalog.version_restore v
              WHERE v.recall_id = p_recall_id AND v.state NOT IN ('done', 'failed'))
          + (SELECT count(*) FROM catalog.annex_placement p
              WHERE p.recall_id = p_recall_id
                AND p.state NOT IN ('placed', 'failed', 'cancelled')))::integer
$$;

-- Count them again for one recall. The recall's row is locked first: a commit
-- that is about to end this recall waits for the job's transaction, and then
-- sees the count on the row it looks at again; and two jobs that start and end
-- at the same instant count one after the other, each seeing the other's rows
-- (a new statement reads what has been committed by then).
--
-- `p_let_go`: the job stopped waiting without having had its bytes. If it was
-- the last, and the recall is of content its file has moved on from, the
-- recall ends here: `superseded`, for the reason and by the version of the
-- commit that passed it by. A deleted file's recall of the content it still
-- names ends as `deleted` too, when no commit marked it (the recall was asked
-- for after the delete, or before this migration): no edge fills a deleted
-- file, and the idle rule would fail the recall an hour later.
CREATE FUNCTION catalog.recall_count_jobs(p_recall_id bigint, p_let_go boolean) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    v_ended bigint;
BEGIN
    IF p_recall_id IS NULL THEN
        RETURN;
    END IF;
    PERFORM 1 FROM catalog.recall WHERE recall_id = p_recall_id FOR UPDATE;
    UPDATE catalog.recall
       SET held_by_jobs = catalog.recall_jobs(p_recall_id)
     WHERE recall_id = p_recall_id
       AND held_by_jobs IS DISTINCT FROM catalog.recall_jobs(p_recall_id);
    IF NOT p_let_go THEN
        RETURN;
    END IF;
    UPDATE catalog.recall r
       SET state = 'superseded',
           superseded_reason = CASE
               WHEN r.moved_on_version_id IS NULL THEN 'deleted'
               WHEN (SELECT v.kind FROM catalog.file_version v
                      WHERE v.version_id = r.moved_on_version_id) = 'tombstone' THEN 'deleted'
               ELSE 'replaced' END,
           superseded_by_version_id = coalesce(r.moved_on_version_id, f.current_version_id),
           completed_at = now()
      FROM catalog.file f
     WHERE r.recall_id = p_recall_id AND f.file_id = r.file_id
       AND NOT r.pinned AND r.held_by_jobs = 0
       AND r.state IN ('requested', 'rehydrating', 'copying', 'downloading')
       AND (r.moved_on_version_id IS NOT NULL
            OR (f.deleted_at IS NOT NULL AND f.content_version_id = r.version_id))
    RETURNING r.recall_id INTO v_ended;
    IF v_ended IS NOT NULL THEN
        PERFORM pgmq.send('notify', jsonb_build_object('recall_id', v_ended::text));
    END IF;
END $$;
REVOKE ALL ON FUNCTION catalog.recall_jobs(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION catalog.recall_count_jobs(bigint, boolean) FROM PUBLIC;

-- A job that starts to wait on a recall, stops, or moves to another recall is
-- counted again. The trigger's arguments are the states in which the job no
-- longer waits; the first is the one in which it had its bytes (it closes its
-- recall itself), and in the others it let go. A step between two waiting
-- states changes nothing and takes no lock: the worker moves a placement on
-- while it holds the recall's row.
--
-- A deleted job is counted too. Nothing deletes one today; if something ever
-- does, a job that was waiting must not leave its recall held for good.
CREATE FUNCTION catalog.recall_job_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_waited boolean;
    v_waits  boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.recall_id IS NOT NULL AND NOT (OLD.state = ANY (TG_ARGV)) THEN
            PERFORM catalog.recall_count_jobs(OLD.recall_id, true);
        END IF;
        RETURN NULL;
    END IF;
    v_waited := TG_OP = 'UPDATE' AND OLD.recall_id IS NOT NULL
                AND NOT (OLD.state = ANY (TG_ARGV));
    v_waits := NEW.recall_id IS NOT NULL AND NOT (NEW.state = ANY (TG_ARGV));
    IF TG_OP = 'UPDATE' AND OLD.recall_id IS NOT DISTINCT FROM NEW.recall_id
       AND v_waited = v_waits THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.recall_id IS DISTINCT FROM NEW.recall_id THEN
        -- It moved to another recall: it let go of the one it was waiting on.
        PERFORM catalog.recall_count_jobs(OLD.recall_id, v_waited);
        PERFORM catalog.recall_count_jobs(NEW.recall_id, false);
        RETURN NULL;
    END IF;
    PERFORM catalog.recall_count_jobs(NEW.recall_id,
                                      v_waited AND NOT v_waits AND NEW.state <> TG_ARGV[0]);
    RETURN NULL;
END $$;
CREATE TRIGGER version_restore_holds_recall
    AFTER INSERT OR UPDATE OF state, recall_id OR DELETE ON catalog.version_restore
    FOR EACH ROW EXECUTE FUNCTION catalog.recall_job_changed('done', 'failed');
CREATE TRIGGER annex_placement_holds_recall
    AFTER INSERT OR UPDATE OF state, recall_id OR DELETE ON catalog.annex_placement
    FOR EACH ROW EXECUTE FUNCTION catalog.recall_job_changed('placed', 'failed', 'cancelled');

-- The jobs that are waiting now, when this migration runs: without this, a
-- restore or a placement already under way would lose its recall to the next
-- write of its file.
UPDATE catalog.recall r
   SET held_by_jobs = catalog.recall_jobs(r.recall_id)
 WHERE r.state IN ('requested', 'rehydrating', 'copying', 'downloading')
   AND catalog.recall_jobs(r.recall_id) > 0;

-- As in 0003, and: a content version or a tombstone that becomes current ends
-- the recalls of the content it replaces. A metadata or rename version moves
-- no bytes and ends nothing.
CREATE OR REPLACE FUNCTION catalog.file_version_after_commit() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_was bigint;
    v_recall bigint;
BEGIN
    UPDATE catalog.file_version
       SET superseded_at = NEW.committed_at
     WHERE file_id = NEW.file_id AND state = 'committed' AND superseded_at IS NULL
       AND version_id <> NEW.version_id;
    IF NEW.kind IN ('content', 'tombstone') THEN
        SELECT f.content_version_id INTO v_was FROM catalog.file f WHERE f.file_id = NEW.file_id;
    END IF;
    UPDATE catalog.file f
       SET current_version_id = NEW.version_id,
           content_version_id = CASE NEW.kind
                                    WHEN 'content' THEN NEW.version_id
                                    WHEN 'tombstone' THEN f.content_version_id
                                    ELSE NEW.content_version_id END
     WHERE f.file_id = NEW.file_id;
    -- The active recalls of the content the file held until now end here,
    -- and their requesters are told (the worker's `notify` queue). One that
    -- someone asked for of that version itself (pinned, or held by a job) is
    -- kept, and marked with this version.
    --
    -- One statement for both, with no test of `pinned` or `held_by_jobs` in
    -- its WHERE: a row that a job's transaction has locked is then waited for
    -- and read again, whichever way the job is changing the count. With the
    -- test in the WHERE, a job letting go at this instant was missed: the row
    -- did not match as this statement first saw it, so it was not waited for.
    IF v_was IS NOT NULL AND v_was <> NEW.version_id THEN
        FOR v_recall IN
            WITH passed AS (
                UPDATE catalog.recall
                   SET state = CASE WHEN pinned OR held_by_jobs > 0 THEN state
                                    ELSE 'superseded' END,
                       superseded_reason = CASE
                           WHEN pinned OR held_by_jobs > 0 THEN NULL
                           WHEN NEW.kind = 'tombstone' THEN 'deleted'
                           ELSE 'replaced' END,
                       superseded_by_version_id = CASE WHEN pinned OR held_by_jobs > 0 THEN NULL
                                                       ELSE NEW.version_id END,
                       moved_on_version_id = coalesce(moved_on_version_id, NEW.version_id),
                       completed_at = CASE WHEN pinned OR held_by_jobs > 0 THEN completed_at
                                           ELSE now() END
                 WHERE file_id = NEW.file_id AND version_id = v_was
                   AND state IN ('requested', 'rehydrating', 'copying', 'downloading')
                RETURNING recall_id, state
            )
            SELECT recall_id FROM passed WHERE state = 'superseded'
        LOOP
            PERFORM pgmq.send('notify', jsonb_build_object('recall_id', v_recall::text));
        END LOOP;
    END IF;
    PERFORM catalog.file_resync_location(NEW.file_id);
    RETURN NULL;
END $$;

-- A partner reading `recall.state` events is told why a recall was superseded.
CREATE OR REPLACE FUNCTION catalog.partner_recall_event() RETURNS trigger
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
           || CASE WHEN NEW.state = 'superseded'
                   THEN jsonb_build_object('superseded_reason', NEW.superseded_reason)
                   ELSE '{}'::jsonb END
      FROM catalog.file f WHERE f.file_id = NEW.file_id;
    RETURN NULL;
END $$;
