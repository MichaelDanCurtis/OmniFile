-- Migration 0027: a migration's cutover may swap primary and secondary on a
-- retained or held version (decision 31 with migration-engine.md section 7).
-- Ported from #10 (B 0019).
--
-- 0003's location_guard froze the role of every primary or secondary copy
-- of a retained or held version, so a label could not be moved (say, to
-- staging) and the copy then deleted. That also refused the cutover's flip
-- (S3 object -> secondary, verified copy -> primary) for any file on legal
-- hold or under WORM retention, and the prefix's whole cutover failed with
-- it. A role swap destroys nothing: retention and holds protect a version's
-- bytes and its copies from deletion and overwrite, not which committed copy
-- is the primary. The S3 object stays a retained secondary that OmniFile
-- never writes or deletes (decision 40).
--
-- Allowed now, on a retained or held version, and only this:
-- - a copy goes between `primary` and `secondary` (no other role);
-- - it stays `committed` (not pending, deleting or deleted), and nothing
--   else that names its bytes changes in the same update (file, version,
--   backend, container, key, provider version id);
-- - a promotion only of a verified copy (`verified`, `verified_by` set);
-- - a demotion only while another committed primary or secondary copy of
--   the same version is verified (the one taking over);
-- - and at commit the version still has a committed primary (a deferred
--   check), so a held version is never left without one.
-- Everything else 0003 refused stays refused: deleting a held copy, marking
-- it deleting or deleted, and relabelling it to any other role. Shortening
-- retention stays refused by file_version_before (0003).

CREATE FUNCTION catalog.location_role_swap_ok(o catalog.location, n catalog.location)
RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT o.role IN ('primary', 'secondary') AND n.role IN ('primary', 'secondary')
       AND o.role <> n.role
       AND o.state = 'committed' AND n.state = 'committed'
       AND n.file_id = o.file_id AND n.version_id = o.version_id
       AND n.backend_id = o.backend_id AND n.container = o.container
       AND n.object_key = o.object_key
       AND n.backend_version_id IS NOT DISTINCT FROM o.backend_version_id
       AND CASE n.role
             WHEN 'primary' THEN n.verified AND n.verified_by IS NOT NULL
             ELSE EXISTS (SELECT 1 FROM catalog.location x
                           WHERE x.version_id = o.version_id
                             AND x.location_id <> o.location_id
                             AND x.state = 'committed'
                             AND x.role IN ('primary', 'secondary')
                             AND x.verified AND x.verified_by IS NOT NULL)
           END
$$;

CREATE OR REPLACE FUNCTION catalog.location_guard() RETURNS trigger
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
    -- (say, to staging) and then deleted; the one exception is a verified
    -- swap of primary and secondary (0027, a migration's cutover).
    IF ((OLD.role IN ('primary', 'secondary') AND OLD.state NOT IN ('deleting', 'deleted')
         AND (TG_OP = 'DELETE' OR NEW.state IN ('deleting', 'deleted')))
        OR (TG_OP = 'UPDATE' AND NEW.role IS DISTINCT FROM OLD.role
            AND NOT catalog.location_role_swap_ok(OLD, NEW)))
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

-- At commit: a retained or held version whose copies changed role still has
-- a committed primary.
CREATE FUNCTION catalog.location_held_primary_check() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM catalog.file_version v
                WHERE v.version_id = NEW.version_id
                  AND (v.legal_hold OR v.retention_until > now()))
       AND NOT EXISTS (SELECT 1 FROM catalog.location l
                        WHERE l.version_id = NEW.version_id
                          AND l.role = 'primary' AND l.state = 'committed') THEN
        RAISE EXCEPTION 'version %: a retained or held version keeps a committed primary',
            NEW.version_id USING ERRCODE = 'OF002';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER location_held_primary AFTER UPDATE OF role ON catalog.location
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW WHEN (OLD.role IS DISTINCT FROM NEW.role)
    EXECUTE FUNCTION catalog.location_held_primary_check();
