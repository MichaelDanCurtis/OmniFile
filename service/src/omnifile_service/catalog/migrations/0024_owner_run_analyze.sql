-- Planner statistics for the seeder (sources/run.py): seeding fills these
-- tables faster than autovacuum re-analyses them. ANALYZE needs the tables'
-- owner (or PostgreSQL 17's MAINTAIN), and no app owns them
-- (catalog/privileges.py), so the worker calls this, which runs as the owner.
-- Execution is granted to the worker alone, by privileges.py.
CREATE FUNCTION catalog.analyze_namespace() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    ANALYZE catalog.file, catalog.file_version, catalog.location;
END $$;
