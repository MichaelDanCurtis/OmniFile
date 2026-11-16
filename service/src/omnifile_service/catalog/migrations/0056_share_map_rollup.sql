-- Migration 0056: the share map from a roll-up kept by the worker (portal-api.md
-- section 5.22).
--
-- Counting a share's files live, as the map did for everyone but an ArchiveAdmin
-- or DataSteward, took 211 to 342 s at 10^7 files (results/wp14-share-map.md),
-- and the hourly whole-share count (0036) recounted each share in one transaction.
-- Now the worker keeps a roll-up per catalog folder, recounted only for folders
-- that changed, and the map sums it.
--
-- - catalog.share_map_group: one row per group of a folder's live files with one
--   captured ACL, one annex folder, one state and one primary copy's backend,
--   tier and class: how many, their bytes and the bytes on the edge. Nothing
--   about who may read is stored: the map puts every group through the same
--   predicate a file goes through (catalog/read_set.py), at query time.
-- - catalog.share_map_folder: when each catalog folder was last counted.
--   - A folder with nothing queued is true as of now; one with something queued,
--     as of its count. The map's "as of" is the oldest of those it sums.
--   - A folder made after the roll-up began (`since` in share_map_state) and not
--     counted yet is queued, and was empty when it was made: as of then.
--   - A share with a folder from before that and no count yet, or with no folder
--     counted at all, shows "counting", never a total of zero.
-- - catalog.share_map_dirty: folders whose files changed since their count, an
--   append-only queue, so an edge's commit only inserts (no row it could wait
--   on). The worker recounts the folders in it, a bounded batch at a time, each
--   in its own short transaction, and takes off only the entries its count saw.
-- - catalog.share_map_state: the worker's cursor through the folders made before
--   this migration (`backfill`), so the first count resumes where it stopped.
--
-- What marks a folder for recount (the triggers below):
-- - a file inserted, deleted, or changed in what the map counts (its folder, kind,
--   state, size, bytes on the edge, ACL, primary copy, deletion): its folder,
--   before and after a move;
-- - a folder moved, renamed or deleted: the folder itself (its own groups don't
--   change, and the map walks the tree at query time, so it follows at once);
-- - a location's state, backend, tier or class changing (catalog.location, not
--   catalog.file): the folder of the file it is the primary copy of;
-- - an annexed file's annex folder changing, or its annex row going: its catalog
--   folder.
--
-- The hourly count (catalog.share_map_count and share_map_run, 0036) goes: the map
-- was its only reader.

CREATE TABLE catalog.share_map_group (
    folder_id     bigint NOT NULL REFERENCES catalog.file ON DELETE CASCADE,
    acl_id        bigint,
    -- Whether its files are annexed, and in which annex folder (NULL: none
    -- recorded, which for an annexed file means nobody may read it).
    annexed       boolean NOT NULL,
    dir_id        bigint,
    edge_state    text NOT NULL,
    backend_id    bigint,
    logical_tier  text,
    storage_class text,
    files         bigint NOT NULL CHECK (files >= 0),
    bytes         bigint NOT NULL CHECK (bytes >= 0),
    local_bytes   bigint NOT NULL CHECK (local_bytes >= 0)
);
CREATE INDEX share_map_group_folder ON catalog.share_map_group (folder_id);

CREATE TABLE catalog.share_map_folder (
    folder_id  bigint PRIMARY KEY REFERENCES catalog.file ON DELETE CASCADE,
    counted_at timestamptz NOT NULL
);

CREATE TABLE catalog.share_map_dirty (
    seq       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    folder_id bigint NOT NULL,
    at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX share_map_dirty_folder ON catalog.share_map_dirty (folder_id);

CREATE TABLE catalog.share_map_state (
    key   text PRIMARY KEY,
    value text NOT NULL
);
-- Folders made from now on are queued as they are made.
INSERT INTO catalog.share_map_state (key, value) VALUES ('since', now()::text);

CREATE FUNCTION catalog.share_map_file_ins() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO catalog.share_map_dirty (folder_id)
    SELECT DISTINCT n.parent_id FROM new_rows n
     WHERE n.kind = 'file' AND n.parent_id IS NOT NULL;
    -- A folder made now: queued for its first count.
    INSERT INTO catalog.share_map_dirty (folder_id)
    SELECT n.file_id FROM new_rows n WHERE n.kind = 'dir';
    RETURN NULL;
END $$;

CREATE TRIGGER share_map_file_ins AFTER INSERT ON catalog.file
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION catalog.share_map_file_ins();

CREATE FUNCTION catalog.share_map_file_upd() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO catalog.share_map_dirty (folder_id)
    SELECT DISTINCT d.folder_id
      FROM new_rows n JOIN old_rows o USING (file_id)
     CROSS JOIN LATERAL (VALUES
            -- A file: its folder, before and after.
            (CASE WHEN 'file' IN (n.kind, o.kind) THEN n.parent_id END),
            (CASE WHEN 'file' IN (n.kind, o.kind) THEN o.parent_id END),
            -- A folder moved, renamed or deleted: itself.
            (CASE WHEN 'dir' IN (n.kind, o.kind) THEN n.file_id END)) AS d (folder_id)
     WHERE d.folder_id IS NOT NULL
       AND (n.parent_id, n.name, n.kind, n.state, n.size, n.resident_bytes, n.acl_id,
            n.primary_location_id, n.deleted_at)
           IS DISTINCT FROM
           (o.parent_id, o.name, o.kind, o.state, o.size, o.resident_bytes, o.acl_id,
            o.primary_location_id, o.deleted_at);
    RETURN NULL;
END $$;

CREATE TRIGGER share_map_file_upd AFTER UPDATE ON catalog.file
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION catalog.share_map_file_upd();

CREATE FUNCTION catalog.share_map_file_del() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO catalog.share_map_dirty (folder_id)
    SELECT DISTINCT o.parent_id FROM old_rows o
     WHERE o.kind = 'file' AND o.parent_id IS NOT NULL;
    RETURN NULL;
END $$;

CREATE TRIGGER share_map_file_del AFTER DELETE ON catalog.file
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION catalog.share_map_file_del();

-- A copy's tier, class, backend or state lives on catalog.location.
CREATE FUNCTION catalog.share_map_location() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        INSERT INTO catalog.share_map_dirty (folder_id)
        SELECT DISTINCT f.parent_id
          FROM new_rows n JOIN old_rows o USING (location_id)
          JOIN catalog.file f ON f.file_id = n.file_id AND f.primary_location_id = n.location_id
         WHERE f.parent_id IS NOT NULL
           AND (n.state, n.backend_id, n.logical_tier, n.storage_class)
               IS DISTINCT FROM (o.state, o.backend_id, o.logical_tier, o.storage_class);
    ELSIF TG_OP = 'INSERT' THEN
        INSERT INTO catalog.share_map_dirty (folder_id)
        SELECT DISTINCT f.parent_id
          FROM new_rows n
          JOIN catalog.file f ON f.file_id = n.file_id AND f.primary_location_id = n.location_id
         WHERE f.parent_id IS NOT NULL;
    ELSE
        INSERT INTO catalog.share_map_dirty (folder_id)
        SELECT DISTINCT f.parent_id
          FROM old_rows o
          JOIN catalog.file f ON f.file_id = o.file_id AND f.primary_location_id = o.location_id
         WHERE f.parent_id IS NOT NULL;
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER share_map_location_ins AFTER INSERT ON catalog.location
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION catalog.share_map_location();
CREATE TRIGGER share_map_location_upd AFTER UPDATE ON catalog.location
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION catalog.share_map_location();
CREATE TRIGGER share_map_location_del AFTER DELETE ON catalog.location
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION catalog.share_map_location();

-- An annexed file's annex folder (decision 63) is part of its group.
CREATE FUNCTION catalog.share_map_annex_file() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        INSERT INTO catalog.share_map_dirty (folder_id)
        SELECT DISTINCT f.parent_id
          FROM new_rows n JOIN old_rows o USING (file_id)
          JOIN catalog.file f ON f.file_id = n.file_id
         WHERE f.parent_id IS NOT NULL AND n.dir_id IS DISTINCT FROM o.dir_id;
    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO catalog.share_map_dirty (folder_id)
        SELECT DISTINCT f.parent_id
          FROM old_rows o JOIN catalog.file f ON f.file_id = o.file_id
         WHERE f.parent_id IS NOT NULL;
    ELSE
        INSERT INTO catalog.share_map_dirty (folder_id)
        SELECT DISTINCT f.parent_id
          FROM new_rows n JOIN catalog.file f ON f.file_id = n.file_id
         WHERE f.parent_id IS NOT NULL;
    END IF;
    RETURN NULL;
END $$;

CREATE TRIGGER share_map_annex_file_ins AFTER INSERT ON catalog.annex_file
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION catalog.share_map_annex_file();
CREATE TRIGGER share_map_annex_file_upd AFTER UPDATE ON catalog.annex_file
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION catalog.share_map_annex_file();
CREATE TRIGGER share_map_annex_file_del AFTER DELETE ON catalog.annex_file
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION catalog.share_map_annex_file();

DROP TABLE catalog.share_map_count;
DROP TABLE catalog.share_map_run;
