-- Migration 0026: a migration source is read-only to OmniFile
-- (catalog-seeding.md section 10; migration-engine.md section 7).
--
-- A registered source bucket is the client's. OmniFile reads it, restores
-- archived objects in it, and reports what users deleted; it never deletes,
-- overwrites, re-tiers (an S3 class change is an in-place CopyObject),
-- re-tags or rewrites the metadata of an object there: before, during and
-- after the migration. The rule is per bucket, so it holds for keys outside
-- every source prefix too, and from the moment the source is registered.
--
-- `catalog.on_source` is the one question every write path asks (the
-- service's SQL through `catalog/source_readonly.py`). It is about migration
-- sources: a source on another backend than its dataset's own. Data in the
-- dataset's own backend (an adopted source, kind 'adopt', or a source seeded
-- in place) is the dataset's, and not covered.

CREATE FUNCTION catalog.on_source(p_backend_id bigint, p_container text) RETURNS boolean
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT EXISTS (SELECT 1 FROM catalog.source s
                     JOIN catalog.dataset d ON d.dataset_id = s.dataset_id
                    WHERE s.backend_id = p_backend_id AND s.container = p_container
                      AND s.kind = 'source' AND s.backend_id <> d.backend_id)
$$;

-- The catalog's own backstop: a location on a source never becomes
-- `deleting`, which is what queues the provider delete.
CREATE FUNCTION catalog.location_source_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF catalog.on_source(NEW.backend_id, NEW.container) THEN
        RAISE EXCEPTION 'location %: s3://%/% is in a migration source, which OmniFile never deletes',
            NEW.location_id, NEW.container, NEW.object_key USING ERRCODE = 'OF008';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER location_source_guard
    BEFORE UPDATE OF state ON catalog.location
    FOR EACH ROW WHEN (NEW.state = 'deleting' AND OLD.state IS DISTINCT FROM 'deleting')
    EXECUTE FUNCTION catalog.location_source_guard();
