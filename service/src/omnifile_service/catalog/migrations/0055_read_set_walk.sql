-- Migration 0055: decision 63's folders at scale, walked per query.
--
-- 0052 kept a gate per distinct chain of folder ACLs (catalog.read_gate), and every
-- query listed the gates the caller passes, then tested each row with
-- `gate_id = ANY(array)`. With a gate per folder, as per-user home folders have,
-- that cost grew with the number of gates: 29 us a row at 10^5 gates, and 94 s for
-- the share map of 10^6 files (results/wp14-read-set-scale.md). It also needed
-- recomputes, triggers and a lock per filer to stay true.
--
-- Now nothing is derived from folder ACLs. A query works out, once:
--
-- 1. the folder ACLs the caller passes: only those whose search set names the
--    caller, one of its groups or Everyone can (the GIN index), and of those the
--    ones catalog.acl_can_read lets through;
-- 2. the NAS folders that pass, walked down from each filer's export root through
--    those ACLs only, on filers whose answer (decision 65) admits the caller: a
--    folder not read, read with an error, or whose search set isn't known is never
--    reached, and nor is anything below it;
-- 3. the annex folders (catalog.annex_dir_gate) among them, as a jsonb object
--    keyed by dir_id, so a row's test is a key lookup (O(log n)).
--
-- Its cost grows with the folders the caller can pass, not with every folder,
-- so it is for a whole share's count. One item, or a page, checks chains instead
-- (the review of #88): catalog.dir_passes is one annex folder's chain, O(depth),
-- and catalog.can_read_file one file's. A page of rows from many folders walks
-- with a cap (`p_max`) and checks each row's chain past it; a folder's children
-- check that folder's chain once (catalog/read_set.py says which query takes which).
-- An annexed file whose folder isn't set (dir_id NULL) is nobody's.

DROP TRIGGER nas_folder_acl_gates ON catalog.nas_folder_acl;
DROP FUNCTION catalog.nas_folder_acl_gates();
DROP FUNCTION catalog.can_read_file(catalog.sid[], catalog.sid[], boolean, bigint, text, text[],
                                    bigint[]);
DROP FUNCTION catalog.passable_gates(text, text[]);
DROP FUNCTION catalog.annex_dir_gates_recompute(bigint, text);
DROP FUNCTION catalog.read_gate_lock(bigint);
ALTER TABLE catalog.annex_dir_gate DROP COLUMN gate_id;
DROP TABLE catalog.read_gate;

-- A file's folder, made the first time a file is there: its path in the export.
CREATE OR REPLACE FUNCTION catalog.annex_file_dir() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_dir   text := CASE WHEN strpos(NEW.path, '/') = 0 THEN ''
                         ELSE regexp_replace(NEW.path, '/[^/]*$', '') END;
    v_filer bigint;
    v_epath text;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.path = OLD.path AND NEW.annex_id = OLD.annex_id
       AND NEW.dir_id IS NOT DISTINCT FROM OLD.dir_id AND NEW.dir_id IS NOT NULL THEN
        RETURN NEW;
    END IF;
    SELECT g.dir_id INTO NEW.dir_id FROM catalog.annex_dir_gate g
     WHERE g.annex_id = NEW.annex_id AND g.dir = v_dir;
    IF NEW.dir_id IS NULL THEN
        SELECT a.filer_id, concat_ws('/', nullif(a.path, ''), nullif(v_dir, ''))
          INTO v_filer, v_epath FROM catalog.annex a WHERE a.annex_id = NEW.annex_id;
        INSERT INTO catalog.annex_dir_gate (annex_id, dir, filer_id, epath)
        VALUES (NEW.annex_id, v_dir, v_filer, v_epath)
        ON CONFLICT (annex_id, dir) DO UPDATE SET dir = EXCLUDED.dir
        RETURNING dir_id INTO NEW.dir_id;
    END IF;
    RETURN NEW;
END $$;

-- Each folder's parent, for the walk ('' is the export's root, which has none).
ALTER TABLE catalog.nas_folder_acl ADD COLUMN parent_path text COLLATE "C"
    GENERATED ALWAYS AS (CASE WHEN path = '' THEN NULL
                              WHEN strpos(path, '/') = 0 THEN ''
                              ELSE regexp_replace(path, '/[^/]*$', '') END) STORED;
CREATE INDEX nas_folder_acl_acl ON catalog.nas_folder_acl (acl_id) WHERE error IS NULL;
CREATE INDEX acl_search_sids_gin ON catalog.acl USING gin (search_sids);

-- `p_max`: give up, NULL, when the caller passes more folder ACLs or folders than
-- that, so a page can check its own rows instead (catalog/read_set.py). NULL: walk
-- whatever it takes (a whole share's count).
CREATE FUNCTION catalog.passable_dirs(p_user text, p_groups text[], p_max integer DEFAULT NULL)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH RECURSIVE me AS (
        SELECT (ARRAY[p_user, 'S-1-1-0'] || coalesce(p_groups, '{}'))::catalog.sid[] AS sids
    ), acls AS MATERIALIZED (
        SELECT s.acl_id FROM catalog.acl s, me
         WHERE s.search_sids && me.sids
           AND coalesce(catalog.acl_can_read(s.search_sids, s.search_deny_sids,
                                             p_user, p_groups, s.deny_first), false)
         LIMIT p_max + 1
    ), filers AS MATERIALIZED (
        -- Each filer's answer once (decision 65), not once a folder.
        SELECT fl.filer_id FROM catalog.filer fl
         WHERE catalog.filer_admits(fl.filer_id, p_user, p_groups)
    ), folders AS MATERIALIZED (
        SELECT n.filer_id, n.path, n.parent_path FROM catalog.nas_folder_acl n
         WHERE n.error IS NULL AND n.acl_id IN (SELECT acl_id FROM acls)
           AND n.filer_id IN (SELECT filer_id FROM filers)
         LIMIT p_max + 1
    ), walk (filer_id, path, depth) AS (
        SELECT f.filer_id, f.path, 0 FROM folders f WHERE f.path = ''
        UNION ALL
        -- A parent is its child's path less a name, so the walk can't loop; the
        -- depth bound is for a path that should never be (4096 bytes at most).
        SELECT f.filer_id, f.path, w.depth + 1 FROM folders f
          JOIN walk w ON w.filer_id = f.filer_id AND f.parent_path = w.path
         WHERE w.depth < 2048
    )
    SELECT CASE
               WHEN p_max IS NOT NULL AND ((SELECT count(*) FROM acls) > p_max
                                           OR (SELECT count(*) FROM folders) > p_max)
                   THEN NULL
               ELSE (SELECT coalesce(jsonb_object_agg(g.dir_id::text, true), '{}')
                       FROM walk w JOIN catalog.annex_dir_gate g
                         ON g.filer_id = w.filer_id AND g.epath = w.path)
           END
$$;

-- Whether every NAS folder from the export's root down to annex folder `p_dir` lets
-- the user through, on a filer whose answer admits them: that folder's own chain,
-- O(depth), with no walk. NULL (no folder recorded): nobody.
CREATE FUNCTION catalog.dir_passes(p_dir bigint, p_user text, p_groups text[])
RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT coalesce((
        SELECT bool_and(n.error IS NULL AND n.acl_id IS NOT NULL
                        AND coalesce(catalog.acl_can_read(s.search_sids, s.search_deny_sids,
                                                          p_user, p_groups, s.deny_first),
                                     false))
               AND catalog.filer_admits(min(g.filer_id), p_user, p_groups)
          FROM catalog.annex_dir_gate g
         CROSS JOIN LATERAL unnest(catalog.annex_folders(g.epath, 'x')) AS c (path)
          LEFT JOIN catalog.nas_folder_acl n ON n.filer_id = g.filer_id AND n.path = c.path
          LEFT JOIN catalog.acl s ON s.acl_id = n.acl_id
         WHERE g.dir_id = p_dir), false)
$$;

-- One file (or catalog folder): its own folder's chain, with no walk. A whole
-- share's count doesn't call this: `catalog/read_set.py` writes the same test
-- inline over `passable_dirs`, walked once per query, and a property test checks
-- the forms agree.
CREATE OR REPLACE FUNCTION catalog.can_read_file(p_read catalog.sid[], p_deny catalog.sid[],
                                                 p_deny_first boolean, p_file bigint,
                                                 p_user text, p_groups text[])
RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT coalesce(catalog.acl_can_read(p_read, p_deny, p_user, p_groups, p_deny_first), false)
       AND coalesce((SELECT catalog.dir_passes(d.dir_id, p_user, p_groups)
                       FROM (SELECT x.dir_id FROM catalog.annex_file x WHERE x.file_id = p_file
                             UNION ALL
                             SELECT y.dir_id FROM catalog.annex_folder y
                              WHERE y.file_id = p_file
                             LIMIT 1) d), true)
$$;
