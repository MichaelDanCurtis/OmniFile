-- Migration 0052 (it was 0049, 0050 and 0051 until #77, #80 and #81 took them).
--
-- Decision 63: who can read an annexed file takes its parent folders into
-- account (annex.md section 5, "The folders above a file").
--
-- A file's captured ACL stays as the edge read it (restores and placements
-- use it). Anyone a folder above the file keeps out is removed from its read
-- set, wherever that set is used, by one predicate:
--
-- - catalog.acl.search_sids / search_deny_sids: who may pass through a folder
--   with this ACL, deny-first like reads where the ACL is (`deny_first`):
--   POSIX search (x); NT and NFSv4 traverse (FILE_TRAVERSE, ACE4_EXECUTE),
--   GENERIC_EXECUTE or GENERIC_ALL. On SMB this is strict: Windows' "bypass
--   traverse checking" would let a user open a file below a folder that
--   denies them by its full path, but OmniFile may under-show, never
--   over-show (the owner, 2026-11-07). NULL: not known (read before this
--   migration, or a deny names a principal the edge couldn't map), and
--   nobody passes.
-- - catalog.nas_folder_acl.gone_at: a folder's row is kept when the folder
--   goes, so a file deleted or removed from the NAS is still judged by the
--   folders it was in. The return path ignores gone folders.
-- - catalog.annex_folders(root, rel): the export-relative folders above an
--   annexed file, the export's root first.
-- - catalog.can_read_file(read, deny, deny_first, file, user, groups[, gates]):
--   the captured ACL and, for an annexed file, every folder above it up to the
--   export's root: each has been read and lets the user through. A folder not
--   read yet, or read with an error, keeps everyone out (fail closed). Below:
--   how the folders' answer is kept per folder. `catalog/read_set.py` is its only writer;
--   a test fails on any other caller of `acl_can_read` or reader of
--   `read_sids` (tests/catalog/test_read_set_lint.py).
-- - catalog.annex.folders_read_at, folders_read: the edge's last folder pass
--   that read every folder of the annex and every folder above its root.
--   Until the first one, the annex's files are hidden; the problem below
--   says so on the annex's page.

ALTER TABLE catalog.acl
    ADD COLUMN search_sids catalog.sid[],
    ADD COLUMN search_deny_sids catalog.sid[];

COMMENT ON COLUMN catalog.acl.search_sids IS
    'For a folder ACL: who may pass through the folder (POSIX x; NT and NFSv4 traverse). NULL: not known, nobody passes (decision 63).';

ALTER TABLE catalog.nas_folder_acl ADD COLUMN gone_at timestamptz;

ALTER TABLE catalog.annex
    ADD COLUMN folders_read_at timestamptz,
    ADD COLUMN folders_read jsonb;

CREATE FUNCTION catalog.annex_folders(p_root text, p_rel text)
RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    WITH parent AS (
        SELECT concat_ws('/', nullif(p_root, ''),
                         nullif(regexp_replace(p_rel, '/?[^/]*$', ''), '')) AS p
    ), parts AS (
        SELECT string_to_array(p, '/') AS a FROM parent WHERE p <> ''
    )
    SELECT ARRAY[''] || coalesce((
        SELECT array_agg(array_to_string(a[1:i], '/') ORDER BY i)
          FROM parts, generate_series(1, cardinality(a)) AS i
    ), '{}')
$$;

-- At scale (the review of #83): walking every file's folders on each read cost
-- about 19 us a row, half a CPU-hour per 10^8 rows of a listing. The folders'
-- answer is derived per NAS folder instead, kept current from folder events:
--
-- - catalog.read_gate: one row per distinct set of folder ACLs a chain of
--   folders has (a filer, and the acl_ids of every folder from the export's
--   root down). Few: most folders share their ACLs.
-- - catalog.annex_dir_gate: one row per folder that holds annexed files (its
--   path in the annex, and in the export), and its chain's gate. NULL: a folder
--   in the chain isn't read yet, or was read with an error, and nobody passes.
-- - catalog.annex_file.dir_id: the file's folder, set by a trigger on insert
--   and on a move. NULL keeps everyone out too.
-- - catalog.passable_gates(user, groups): the gates whose every folder lets the
--   user through. `catalog/read_set.py` evaluates it once per query (a
--   sub-select with no reference to the row), so each row costs two index
--   lookups and an array test.
--
-- A folder's change (a row of nas_folder_acl) recomputes the gates of the
-- folders below it, in one statement. An ACL whose search set isn't known is
-- in a gate nobody passes: passable_gates reads the sets at query time.
CREATE TABLE catalog.read_gate (
    gate_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filer_id bigint NOT NULL REFERENCES catalog.filer ON DELETE CASCADE,
    acl_ids  bigint[] NOT NULL,
    UNIQUE (filer_id, acl_ids)
);

CREATE TABLE catalog.annex_dir_gate (
    dir_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    annex_id bigint NOT NULL REFERENCES catalog.annex ON DELETE CASCADE,
    -- The folder under the annex's root ('' for the root), and in the export.
    dir      text COLLATE "C" NOT NULL,
    filer_id bigint NOT NULL REFERENCES catalog.filer ON DELETE CASCADE,
    epath    text COLLATE "C" NOT NULL,
    gate_id  bigint REFERENCES catalog.read_gate ON DELETE SET NULL,
    UNIQUE (annex_id, dir)
);
CREATE INDEX annex_dir_gate_epath ON catalog.annex_dir_gate (filer_id, epath);

ALTER TABLE catalog.annex_file ADD COLUMN dir_id bigint REFERENCES catalog.annex_dir_gate ON DELETE SET NULL;

-- One filer's gates are worked out one transaction at a time (the re-review of
-- #83): every annex on a filer shares the folders above its root, the export's
-- root at least, and a gate worked out while another transaction's folder change
-- hasn't committed would come from the old folder, and stay. A transaction takes
-- it before the gates' statement, which then sees what the last holder committed.
-- The edge's annex events take it first (api/annex.py), before any row lock, so
-- it can't deadlock with them; nothing else writes the rows the gates follow.
CREATE FUNCTION catalog.read_gate_lock(p_filer bigint)
RETURNS void
LANGUAGE sql AS $$
    SELECT pg_advisory_xact_lock(hashtextextended('omnifile read gates ' || p_filer, 0))
$$;

-- The gates of every folder of `p_filer` at `p_path` or below it ('' : all).
CREATE FUNCTION catalog.annex_dir_gates_recompute(p_filer bigint, p_path text)
RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM catalog.read_gate_lock(p_filer);
    -- A new statement, so a new snapshot: after the lock.
    WITH hit AS (
        SELECT g.dir_id, g.epath FROM catalog.annex_dir_gate g
         WHERE g.filer_id = p_filer
           AND (p_path = '' OR g.epath = p_path
                OR (g.epath >= p_path || '/' AND g.epath < p_path || '0'))
    ), chain AS (
        SELECT h.dir_id,
               bool_or(n.acl_id IS NULL OR n.error IS NOT NULL) AS closed,
               array_agg(DISTINCT n.acl_id ORDER BY n.acl_id) AS ids
          FROM hit h
         CROSS JOIN LATERAL unnest(catalog.annex_folders(h.epath, 'x')) AS d (path)
          LEFT JOIN catalog.nas_folder_acl n ON n.filer_id = p_filer AND n.path = d.path
         GROUP BY h.dir_id
    ), made AS (
        INSERT INTO catalog.read_gate (filer_id, acl_ids)
        SELECT DISTINCT p_filer, c.ids FROM chain c WHERE NOT c.closed
        ON CONFLICT (filer_id, acl_ids) DO NOTHING
        RETURNING gate_id, acl_ids
    )
    UPDATE catalog.annex_dir_gate g
       SET gate_id = CASE WHEN c.closed THEN NULL ELSE coalesce(m.gate_id, r.gate_id) END
      FROM chain c
      LEFT JOIN made m ON m.acl_ids = c.ids
      LEFT JOIN catalog.read_gate r ON r.filer_id = p_filer AND r.acl_ids = c.ids
     WHERE g.dir_id = c.dir_id
       AND g.gate_id IS DISTINCT FROM
           CASE WHEN c.closed THEN NULL ELSE coalesce(m.gate_id, r.gate_id) END;
END $$;

-- A file's folder, made with its gate the first time a file is there.
CREATE FUNCTION catalog.annex_file_dir() RETURNS trigger
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
        PERFORM catalog.annex_dir_gates_recompute(v_filer, v_epath);
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER annex_file_dir BEFORE INSERT OR UPDATE OF path, annex_id, dir_id
    ON catalog.annex_file FOR EACH ROW EXECUTE FUNCTION catalog.annex_file_dir();

-- The catalog folders an annex makes for the NAS's (the review of #83): they
-- carry the annex's Owners ACL, so their names are narrowed by the NAS folders
-- above them as a file's are: `path` is the folder under the annex's root, and
-- `dir_id` its parent's gate (set by the same trigger).
CREATE TABLE catalog.annex_folder (
    file_id  bigint PRIMARY KEY REFERENCES catalog.file ON DELETE CASCADE,
    annex_id bigint NOT NULL REFERENCES catalog.annex ON DELETE CASCADE,
    path     text COLLATE "C" NOT NULL,
    dir_id   bigint REFERENCES catalog.annex_dir_gate ON DELETE SET NULL
);

CREATE TRIGGER annex_folder_dir BEFORE INSERT OR UPDATE OF path, annex_id, dir_id
    ON catalog.annex_folder FOR EACH ROW EXECUTE FUNCTION catalog.annex_file_dir();

CREATE FUNCTION catalog.nas_folder_acl_gates() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM catalog.annex_dir_gates_recompute(OLD.filer_id, OLD.path);
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.acl_id IS NOT DISTINCT FROM OLD.acl_id
       AND NEW.error IS NOT DISTINCT FROM OLD.error THEN
        RETURN NEW;
    END IF;
    PERFORM catalog.annex_dir_gates_recompute(NEW.filer_id, NEW.path);
    RETURN NEW;
END $$;

CREATE TRIGGER nas_folder_acl_gates AFTER INSERT OR UPDATE OR DELETE
    ON catalog.nas_folder_acl FOR EACH ROW EXECUTE FUNCTION catalog.nas_folder_acl_gates();

CREATE FUNCTION catalog.passable_gates(p_user text, p_groups text[])
RETURNS bigint[]
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT coalesce(array_agg(g.gate_id), '{}') FROM catalog.read_gate g
     WHERE NOT EXISTS (
        SELECT 1 FROM unnest(g.acl_ids) AS i (acl_id)
          LEFT JOIN catalog.acl s ON s.acl_id = i.acl_id
         WHERE NOT coalesce(catalog.acl_can_read(s.search_sids, s.search_deny_sids,
                                                 p_user, p_groups, s.deny_first), false))
$$;

-- `p_gates`: catalog.passable_gates(p_user, p_groups), given once per query.
CREATE FUNCTION catalog.can_read_file(p_read catalog.sid[], p_deny catalog.sid[],
                                      p_deny_first boolean, p_file bigint,
                                      p_user text, p_groups text[], p_gates bigint[])
RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT coalesce(catalog.acl_can_read(p_read, p_deny, p_user, p_groups, p_deny_first), false)
       AND coalesce((SELECT coalesce(g.gate_id = ANY(p_gates), false)
                       FROM catalog.annex_file x
                       LEFT JOIN catalog.annex_dir_gate g ON g.dir_id = x.dir_id
                      WHERE x.file_id = p_file),
                    (SELECT coalesce(g.gate_id = ANY(p_gates), false)
                       FROM catalog.annex_folder y
                       LEFT JOIN catalog.annex_dir_gate g ON g.dir_id = y.dir_id
                      WHERE y.file_id = p_file),
                    true)
$$;

CREATE FUNCTION catalog.can_read_file(p_read catalog.sid[], p_deny catalog.sid[],
                                      p_deny_first boolean, p_file bigint,
                                      p_user text, p_groups text[])
RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT catalog.can_read_file(p_read, p_deny, p_deny_first, p_file, p_user, p_groups,
                                 catalog.passable_gates(p_user, p_groups))
$$;

-- The files already annexed.
UPDATE catalog.annex_file SET dir_id = NULL;

-- Every live annex's files are hidden until its edge has read the folders.
INSERT INTO catalog.annex_event (annex_id, kind, details)
SELECT annex_id, 'problem',
       jsonb_build_object(
           'reason', 'read_set_pending',
           'detail', 'Files are hidden from everyone until the edge has read the permissions of'
                     ' every folder above them, up to the export''s root (decision 63). That'
                     ' happens at its next scan after the update.')
  FROM catalog.annex
 WHERE state <> 'retired';
