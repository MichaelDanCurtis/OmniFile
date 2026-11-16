-- Migration 0005: read reports don't change a file's entity_version.
--
-- The edge reports reads for the `last_read` policy clock (control-api.md
-- section 4.7, POST /v0/edges/{id}/reads). They update file.last_read_at,
-- which the edge does not replicate. catalog.bump_version (0003) bumped
-- entity_version on every update, so each read report changed the
-- reconciliation digest (XOR over SHA-256("<file_id>:<entity_version>"),
-- section 6) with no feed event to explain it. Then every edge narrowed and
-- re-fetched rows that had not changed.
--
-- `file` now has its own BEFORE UPDATE function. An update that changes only
-- last_read_at keeps entity_version and updated_at as they are. Any other
-- change bumps entity_version as before. The feed trigger
-- (catalog.feed_file_update) already ignores last_read_at, so these updates
-- emit no events. The column stays on `file`, so the policy engine's reader is
-- unchanged. The table's fillfactor (85) leaves room for these as HOT updates.

CREATE FUNCTION catalog.file_bump() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.last_read_at IS DISTINCT FROM OLD.last_read_at
       AND to_jsonb(NEW) - 'last_read_at' - 'entity_version' - 'updated_at'
         = to_jsonb(OLD) - 'last_read_at' - 'entity_version' - 'updated_at' THEN
        NEW.entity_version := OLD.entity_version;
        NEW.updated_at := OLD.updated_at;
        RETURN NEW;
    END IF;
    NEW.entity_version := OLD.entity_version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- The same trigger name keeps its place in the (alphabetical) BEFORE order.
DROP TRIGGER file_bump ON catalog.file;
CREATE TRIGGER file_bump BEFORE UPDATE ON catalog.file
    FOR EACH ROW EXECUTE FUNCTION catalog.file_bump();
