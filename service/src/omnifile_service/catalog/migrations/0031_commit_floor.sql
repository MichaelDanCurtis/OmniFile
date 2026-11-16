-- Migration 0031: a file's versions commit in order, whoever's clock wrote
-- them.
--
-- A committed version supersedes the file's current one at its own
-- `committed_at` (file_version_after_commit, migration 0003), and a version
-- is never superseded before it was committed (`file_version_interval`).
-- But versions are committed by different clocks: the database's (`now()`:
-- the edge's renames, unlinks and owner changes, POST .../changes), the
-- worker's, and S3's (a seeded object's version commits at its LastModified,
-- `sources/seed.py`). When the current version's time is ahead of the
-- writer's (clock skew between AWS and the catalog, an object dated in the
-- future, the dev stack's virtual clock), the new version would supersede it
-- before it began: the check refused the insert, the edge got a 500, and a
-- user's delete of a freshly seeded S3 file ended as a conflict in the edge's
-- outbox, never in the catalog (found by the M5 exit test, step 15).
--
-- The source follower already floored its versions at the current one's
-- time (`sources/follow.py` `_commit_floor`). This does the same for every
-- writer: a version committed before the file's current version is
-- committed at that version's time instead. A later commit time only ever
-- lengthens what retention counts from it.

CREATE FUNCTION catalog.file_version_commit_floor() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_floor timestamptz;
BEGIN
    SELECT max(committed_at) INTO v_floor
      FROM catalog.file_version
     WHERE file_id = NEW.file_id AND state = 'committed' AND superseded_at IS NULL
       AND version_id IS DISTINCT FROM NEW.version_id;
    IF v_floor IS NOT NULL AND NEW.committed_at < v_floor THEN
        NEW.committed_at := v_floor;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER file_version_commit_floor_ins BEFORE INSERT ON catalog.file_version
    FOR EACH ROW WHEN (NEW.state = 'committed' AND NEW.committed_at IS NOT NULL)
    EXECUTE FUNCTION catalog.file_version_commit_floor();
CREATE TRIGGER file_version_commit_floor_upd BEFORE UPDATE OF state, committed_at
    ON catalog.file_version
    FOR EACH ROW WHEN (OLD.state = 'pending' AND NEW.state = 'committed'
                       AND NEW.committed_at IS NOT NULL)
    EXECUTE FUNCTION catalog.file_version_commit_floor();
