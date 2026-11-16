-- The database roles of the service's managed identities (docs/guide/README.md, section 7.1).
--
--   psql "host=<server> dbname=postgres user=<Entra admin group> sslmode=verify-full sslrootcert=system" \
--     -v prefix=<namePrefix> -f docs/guide/sql/create-principals.sql
--
-- Run as the Entra administrator, in the `postgres` database. Each app signs in to the catalog as its
-- own managed identity, `id-<prefix>-<app>` (deploy/azure/README.md, "The catalog connection"), so each
-- needs a role of exactly that name. A role that already exists is left alone, so a rerun is safe.
-- The relay has none: it never touches the catalog.
\set ON_ERROR_STOP on
-- psql variables aren't expanded inside the DO block's body, so the prefix travels as a setting.
SELECT set_config('omnifile.prefix', :'prefix', false);
DO $do$
DECLARE
    app text;
    principal text;
BEGIN
    IF current_setting('omnifile.prefix') !~ '^[a-z0-9]{2,8}$' THEN
        RAISE EXCEPTION 'prefix must be the deployment''s namePrefix (2 to 8 lowercase letters or digits)';
    END IF;
    FOREACH app IN ARRAY ARRAY['api', 'enrol', 'web', 'worker', 'verifier'] LOOP
        principal := format('id-%s-%s', current_setting('omnifile.prefix'), app);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = principal) THEN
            RAISE NOTICE 'role % already exists', principal;
        ELSE
            PERFORM pgaadauth_create_principal(principal, false, false);
            RAISE NOTICE 'created role %', principal;
        END IF;
    END LOOP;
END
$do$;
