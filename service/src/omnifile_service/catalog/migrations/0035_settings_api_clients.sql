-- Migration 0035: the portal's Settings area and its API clients
-- (portal-api.md sections 2.2 and 5.20; the owner's requests of 2026-11-04).
--
-- 1. api_client: every application that may call the API with an Entra app
--    token. A token is accepted only from an application registered here and
--    enabled, in addition to its signature and app role. Each names its owner,
--    a person in the tenant, whose access a proposal is checked against.
--    api_key: OmniFile-issued keys for a client, kept as a hash (portal-api.md 2.2.1).
-- 2. identity.app_roles: the Entra app roles a person held at their last
--    portal sign-in, for that check.
-- 3. policy_proposal: the API client and owner a proposal came from.
-- 4. settings_test: the Settings page's test sends (a Teams card, an email,
--    an AWS role assumption), run by the worker, which holds those identities.
-- 5. backend: a row never turns private networking off or changes kind, and
--    what data uses never moves, whoever writes it. `web` adds and edits
--    backends only through catalog.backend_register.

CREATE TABLE catalog.api_client (
    api_client_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The Entra application (client) id: a token's azp (or appid).
    app_id             text NOT NULL UNIQUE
                       CHECK (app_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                              OR app_id ~ '^[a-z][a-z0-9-]{2,62}$'),
    display_name       text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    -- The app roles it may use; a token's other roles are dropped.
    scopes             text[] NOT NULL DEFAULT '{}'
                       CHECK (scopes <@ ARRAY['Policy.Propose', 'Recall.Request', 'Archive.Read',
                                              'Archive.Adopt']::text[]),
    owner_identity_id  bigint NOT NULL REFERENCES catalog.identity,
    state              text NOT NULL DEFAULT 'enabled'
                       CHECK (state IN ('enabled', 'disabled', 'revoked')),
    created_by_identity_id bigint NOT NULL REFERENCES catalog.identity,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    disabled_at        timestamptz,
    revoked_at         timestamptz,
    last_used_at       timestamptz,
    entity_version     bigint NOT NULL DEFAULT 1,
    CONSTRAINT api_client_revoked CHECK ((state = 'revoked') = (revoked_at IS NOT NULL))
);

-- A revoked client stays revoked.
CREATE FUNCTION catalog.api_client_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state = 'revoked' AND (NEW.state <> 'revoked' OR NEW.scopes <> OLD.scopes
                                  OR NEW.owner_identity_id <> OLD.owner_identity_id) THEN
        RAISE EXCEPTION 'OF035: API client % is revoked', OLD.api_client_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.app_id <> OLD.app_id THEN
        RAISE EXCEPTION 'OF035: an API client never changes application'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER api_client_guard BEFORE UPDATE ON catalog.api_client
    FOR EACH ROW EXECUTE FUNCTION catalog.api_client_guard();

-- OmniFile-issued API keys (owner decision, 2026-11-04), beside Entra app
-- identities, which stay the recommended path. A key belongs to one client and
-- carries scopes within the client's. `ofk1.<key_id>.<secret>`: the key_id is
-- public, for lookup; only the SHA-256 of the 32-byte secret is kept.
CREATE TABLE catalog.api_key (
    key_id          text PRIMARY KEY CHECK (key_id ~ '^[0-9a-f]{16}$'),
    api_client_id   bigint NOT NULL REFERENCES catalog.api_client,
    secret_sha256   bytea NOT NULL CHECK (length(secret_sha256) = 32),
    scopes          text[] NOT NULL
                    CHECK (scopes <@ ARRAY['Policy.Propose', 'Recall.Request', 'Archive.Read',
                                           'Archive.Adopt']::text[]),
    created_by_identity_id bigint NOT NULL REFERENCES catalog.identity,
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    revoked_at      timestamptz,
    rotated_from    text REFERENCES catalog.api_key (key_id),
    last_used_at    timestamptz,
    last_used_addr  inet,
    CHECK (expires_at > created_at)
);
CREATE INDEX api_key_client ON catalog.api_key (api_client_id, created_at DESC);

-- A key's secret and scopes never change, and a revoked key stays revoked.
CREATE FUNCTION catalog.api_key_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.secret_sha256 <> OLD.secret_sha256 OR NEW.scopes <> OLD.scopes
       OR NEW.api_client_id <> OLD.api_client_id
       OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
       OR NEW.expires_at > OLD.expires_at THEN
        RAISE EXCEPTION 'OF035: API key % can only be used, shortened or revoked', OLD.key_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER api_key_guard BEFORE UPDATE ON catalog.api_key
    FOR EACH ROW EXECUTE FUNCTION catalog.api_key_guard();

ALTER TABLE catalog.identity
    ADD COLUMN app_roles text[] NOT NULL DEFAULT '{}',
    ADD COLUMN app_roles_seen_at timestamptz;

ALTER TABLE catalog.policy_proposal
    ADD COLUMN api_client_id bigint REFERENCES catalog.api_client,
    ADD COLUMN owner_identity_id bigint REFERENCES catalog.identity,
    -- The OmniFile API key it came with (null: an Entra token).
    ADD COLUMN key_id text REFERENCES catalog.api_key (key_id),
    -- Why a pending proposal can't be approved as it stands: its client was revoked.
    ADD COLUMN stale_reason text CHECK (stale_reason IN ('client_revoked')),
    ADD CONSTRAINT proposal_client_owner
        CHECK ((api_client_id IS NULL) = (owner_identity_id IS NULL)),
    ADD CONSTRAINT proposal_key_client CHECK (key_id IS NULL OR api_client_id IS NOT NULL);

-- Nobody approves their own proposal, through an API client either (review of #43): not
-- the client's owner, not who registered the client, not who made the key it came with.
-- As `proposal_not_self_approved` does for a person's own proposal, in the catalog.
CREATE FUNCTION catalog.proposal_client_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    answerable bigint[];
BEGIN
    IF NEW.state <> 'approved' OR NEW.api_client_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT ARRAY[NEW.owner_identity_id, c.created_by_identity_id, k.created_by_identity_id]
      INTO answerable
      FROM catalog.api_client c
      LEFT JOIN catalog.api_key k ON k.key_id = NEW.key_id
     WHERE c.api_client_id = NEW.api_client_id;
    IF NEW.decided_by_identity_id = ANY (answerable) THEN
        RAISE EXCEPTION 'proposal %: its API client''s owner, registrant or key maker can''t approve it',
            NEW.proposal_id USING ERRCODE = 'OF010';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER proposal_client_guard BEFORE UPDATE OF state, decided_by_identity_id
    ON catalog.policy_proposal FOR EACH ROW EXECUTE FUNCTION catalog.proposal_client_guard();

CREATE TABLE catalog.settings_test (
    test_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind           text NOT NULL CHECK (kind IN ('teams_card', 'email', 'aws_assume_role')),
    -- Who asked; cards and mail go to them, and only to them.
    identity_id    bigint NOT NULL REFERENCES catalog.identity,
    -- aws_assume_role: {role_arn}. Never a secret.
    params         jsonb NOT NULL DEFAULT '{}',
    state          text NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending', 'passed', 'failed')),
    detail         text,
    result         jsonb NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz
);
CREATE INDEX settings_test_identity ON catalog.settings_test (identity_id, created_at DESC);

SELECT pgmq.create('settings_test');
SELECT pgmq.create('settings_test_dlq');

-- Decision 22 and the CLI's rules in the catalog itself (review of #43), whoever writes:
-- - a row never turns private networking off, and never changes kind;
-- - an app's session (its group role, `-c role=omnifile_*`, catalog/connect.py) never
--   writes a public row; only the administrator can, for development;
-- - once a dataset, a source or a migration uses a row, its accounts, buckets, region and
--   nothing changes but what only adds or informs (admin/backends.py
--   UNDER_DATA_MAY_CHANGE: S3 buckets added, an inventory rule, a display name, an
--   Azure row's region).
CREATE FUNCTION catalog.backend_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    k text;
    used boolean;
BEGIN
    -- An app's session: its group role set (`-c role=`), or a login that may SET ROLE
    -- to one (re-review of #43: `SET ROLE NONE` leaves the GUC, not the membership).
    -- A superuser is a member of everything; it is the administrator.
    IF NOT NEW.private_network AND (
           current_setting('role') IN
               ('omnifile_api', 'omnifile_enrol', 'omnifile_web', 'omnifile_worker')
        OR (NOT (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = session_user)
            AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles g
                         WHERE g.rolname IN ('omnifile_api', 'omnifile_enrol', 'omnifile_web',
                                             'omnifile_worker')
                           AND pg_catalog.pg_has_role(session_user, g.oid, 'SET')))) THEN
        RAISE EXCEPTION 'backend %: an app never writes a public backend (decision 22)', NEW.name
            USING ERRCODE = 'OF011';
    END IF;
    IF TG_OP = 'INSERT' THEN
        RETURN NEW;
    END IF;
    IF OLD.private_network AND NOT NEW.private_network THEN
        RAISE EXCEPTION 'backend % uses private networking, and a row never turns it off',
            OLD.backend_id USING ERRCODE = 'OF011';
    END IF;
    IF NEW.kind <> OLD.kind THEN
        RAISE EXCEPTION 'backend % never changes kind', OLD.backend_id USING ERRCODE = 'OF011';
    END IF;
    IF NEW.config IS NOT DISTINCT FROM OLD.config THEN
        RETURN NEW;
    END IF;
    used := EXISTS (SELECT 1 FROM catalog.dataset d
                     WHERE d.backend_id = OLD.backend_id OR d.source_backend_id = OLD.backend_id)
         OR EXISTS (SELECT 1 FROM catalog.source s WHERE s.backend_id = OLD.backend_id)
         OR EXISTS (SELECT 1 FROM catalog.migration_prefix m WHERE m.to_backend_id = OLD.backend_id);
    IF NOT used THEN
        RETURN NEW;
    END IF;
    -- Nothing changes under data (re-review of #43), but what only adds or informs; the
    -- same list as admin/backends.py UNDER_DATA_MAY_CHANGE: S3 buckets may be added
    -- (never dropped), and an inventory rule, a display name and an Azure row's region
    -- may change. Any other key, added, changed or removed, is refused.
    FOR k IN SELECT jsonb_object_keys(OLD.config) UNION SELECT jsonb_object_keys(NEW.config) LOOP
        IF k = 'buckets' AND OLD.kind = 's3' THEN
            IF NOT (coalesce(NEW.config -> k, '[]'::jsonb) @> coalesce(OLD.config -> k, '[]'::jsonb)) THEN
                RAISE EXCEPTION 'backend % holds data: its buckets are never dropped',
                    OLD.backend_id USING ERRCODE = 'OF011';
            END IF;
        ELSIF k IN ('inventory', 'display_name') OR (k = 'region' AND OLD.kind = 'azure_blob') THEN
            CONTINUE;
        ELSIF (NEW.config -> k) IS DISTINCT FROM (OLD.config -> k) THEN
            RAISE EXCEPTION 'backend % holds data: % would change', OLD.backend_id, k
                USING ERRCODE = 'OF011';
        END IF;
    END LOOP;
    RETURN NEW;
END $$;
CREATE TRIGGER backend_guard BEFORE INSERT OR UPDATE ON catalog.backend
    FOR EACH ROW EXECUTE FUNCTION catalog.backend_guard();

-- The portal's Settings, Storage write path (review of #43): `web` has no INSERT or UPDATE
-- on catalog.backend, only this owner-run function (catalog/privileges.py
-- OWNER_FUNCTIONS). The row is built and checked by admin/backends.py, as the CLI does;
-- here it is always private, found by name, never changes kind, and backend_guard keeps
-- what data uses. The add or update is audited as the person.
CREATE FUNCTION catalog.backend_register(p_name text, p_kind text, p_config jsonb,
                                         p_actor text, p_actor_sid text, p_client_addr inet,
                                         p_details jsonb)
RETURNS TABLE (out_outcome text, out_backend_id bigint, out_before jsonb)
LANGUAGE plpgsql AS $$
DECLARE
    old catalog.backend;
    new_id bigint;
BEGIN
    IF p_actor IS NULL OR p_actor !~ '^user:[0-9]+$' THEN
        RAISE EXCEPTION 'a backend is registered by a signed-in person' USING ERRCODE = 'OF011';
    END IF;
    IF p_kind NOT IN ('azure_blob', 's3') OR jsonb_typeof(p_config) <> 'object' THEN
        RAISE EXCEPTION 'a backend is Azure Blob or S3, with an object config'
            USING ERRCODE = 'OF011';
    END IF;
    SELECT * INTO old FROM catalog.backend b WHERE b.name = p_name FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO catalog.backend (name, kind, config, private_network)
             VALUES (p_name, p_kind, p_config, true)
        ON CONFLICT (name) DO NOTHING RETURNING backend_id INTO new_id;
        IF new_id IS NOT NULL THEN
            INSERT INTO catalog.audit_log (actor, actor_sid, proof, action, decision,
                                           client_addr, details)
            VALUES (p_actor, p_actor_sid, CASE WHEN p_actor_sid IS NULL THEN NULL ELSE 'proven' END,
                    'backend.add', 'allow', p_client_addr,
                    coalesce(p_details, '{}'::jsonb) || jsonb_build_object(
                        'backend_id', new_id::text, 'name', p_name, 'kind', p_kind,
                        'config', p_config, 'private_network', true, 'db_user', session_user));
            RETURN QUERY SELECT 'added'::text, new_id, NULL::jsonb;
            RETURN;
        END IF;
        SELECT * INTO old FROM catalog.backend b WHERE b.name = p_name FOR UPDATE;
    END IF;
    IF old.kind <> p_kind THEN
        RAISE EXCEPTION 'backend % (%) is %, not %: a backend never changes kind', p_name,
            old.backend_id, old.kind, p_kind USING ERRCODE = 'OF011';
    END IF;
    IF old.config = p_config AND old.private_network THEN
        RETURN QUERY SELECT 'unchanged'::text, old.backend_id, old.config;
        RETURN;
    END IF;
    UPDATE catalog.backend b SET config = p_config, private_network = true
     WHERE b.backend_id = old.backend_id;
    INSERT INTO catalog.audit_log (actor, actor_sid, proof, action, decision, client_addr, details)
    VALUES (p_actor, p_actor_sid, CASE WHEN p_actor_sid IS NULL THEN NULL ELSE 'proven' END,
            'backend.update', 'allow', p_client_addr,
            coalesce(p_details, '{}'::jsonb) || jsonb_build_object(
                'backend_id', old.backend_id::text, 'name', p_name, 'kind', p_kind,
                'config', p_config, 'private_network', true, 'db_user', session_user,
                'before', jsonb_build_object('config', old.config,
                                             'private_network', old.private_network)));
    RETURN QUERY SELECT 'updated'::text, old.backend_id, old.config;
END $$;
