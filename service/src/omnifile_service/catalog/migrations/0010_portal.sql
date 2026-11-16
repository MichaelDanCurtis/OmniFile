-- Migration 0010: the portal (`web` app, M4; docs/contracts/portal-api.md).
--
-- 1. web_session: server-side browser sessions (backend-for-frontend). The
--    cookie holds 256 random bits; the catalog stores only their SHA-256.
--    Idle timeout 60 minutes, absolute 10 hours (set by `web`).
-- 2. oidc_login: pending Entra sign-ins (state, nonce, PKCE verifier,
--    return_to), consumed once, 10-minute expiry.
-- 3. identity.groups_source: where identity.group_sids came from.
-- 4. recall_state_log: one row per recall state, written by a trigger, for
--    the portal's four-step view.
-- 5. version_restore and download_request: rewind actions the worker runs
--    (worker/portal_jobs.py, queue `portal`).
-- 6. price_table: an ArchiveAdmin's override of the built-in price table.
-- 7. policy.note: the approver's note on a policy version.
-- 8. catalog.file_ancestors(): a file and its ancestors (access checks, paths).
-- 9. PGMQ queues `portal` and `identity_resolve`, each with its `_dlq`.
--
-- Expired sessions, logins, download URLs and finished restores are swept
-- hourly by the worker's `portal_sweep`.

-- ---------------------------------------------------------------------------
-- 1. Sessions
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.web_session (
    session_sha256   bytea PRIMARY KEY CHECK (octet_length(session_sha256) = 32),
    -- NULL for a cloud-only user (no on-premises SID, so no AD identity):
    -- every ACL check is false for them, role-only routes still work.
    identity_id      bigint REFERENCES catalog.identity,
    entra_object_id  uuid NOT NULL,
    user_sid         catalog.sid,
    display_name     text,
    upn              text,
    roles            text[] NOT NULL DEFAULT '{}'
                     CHECK (roles <@ ARRAY['ArchiveAdmin', 'DataSteward', 'User']),
    -- Snapshot at sign-in (token groups plus Domain Users and Authenticated
    -- Users). With groups_source = 'pending' (overage) the identity's
    -- cached list is used once the worker has resolved it.
    group_sids       catalog.sid[] NOT NULL DEFAULT '{}',
    groups_source    text NOT NULL
                     CHECK (groups_source IN ('token', 'graph', 'winbind', 'pending', 'none')),
    csrf_token       text NOT NULL CHECK (length(csrf_token) >= 32),
    auth_time        timestamptz NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    idle_expires_at  timestamptz NOT NULL,
    expires_at       timestamptz NOT NULL,
    CONSTRAINT web_session_idle_within_absolute CHECK (idle_expires_at <= expires_at)
);
CREATE INDEX web_session_expiry ON catalog.web_session (expires_at);
CREATE INDEX web_session_identity ON catalog.web_session (identity_id)
    WHERE identity_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. Pending sign-ins
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.oidc_login (
    state          text PRIMARY KEY CHECK (length(state) >= 32),
    nonce          text NOT NULL CHECK (length(nonce) >= 32),
    code_verifier  text NOT NULL CHECK (length(code_verifier) BETWEEN 43 AND 128),
    -- A same-origin path ("/..."); `web` replaces anything else with "/".
    return_to      text NOT NULL DEFAULT '/'
                   CHECK (left(return_to, 1) = '/' AND left(return_to, 2) <> '//'),
    reauth         boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL
);
CREATE INDEX oidc_login_expiry ON catalog.oidc_login (expires_at);

-- ---------------------------------------------------------------------------
-- 3. Where an identity's groups came from
-- ---------------------------------------------------------------------------
ALTER TABLE catalog.identity
    ADD COLUMN groups_source text CHECK (groups_source IN ('token', 'graph', 'winbind'));

-- ---------------------------------------------------------------------------
-- 4. Recall state history (the four-step view)
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.recall_state_log (
    log_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recall_id  bigint NOT NULL REFERENCES catalog.recall,
    state      text NOT NULL,
    at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX recall_state_log_recall ON catalog.recall_state_log (recall_id, at);

CREATE FUNCTION catalog.recall_state_logged() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.state IS DISTINCT FROM OLD.state THEN
        INSERT INTO catalog.recall_state_log (recall_id, state, at)
        VALUES (NEW.recall_id, NEW.state,
                CASE WHEN TG_OP = 'INSERT' THEN NEW.requested_at ELSE now() END);
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER recall_state_log AFTER INSERT OR UPDATE OF state ON catalog.recall
    FOR EACH ROW EXECUTE FUNCTION catalog.recall_state_logged();

-- Recalls that already exist get their current state, so the view has a row.
INSERT INTO catalog.recall_state_log (recall_id, state, at)
SELECT recall_id, 'requested', requested_at FROM catalog.recall;
INSERT INTO catalog.recall_state_log (recall_id, state, at)
SELECT recall_id, state, updated_at FROM catalog.recall WHERE state <> 'requested';

-- ---------------------------------------------------------------------------
-- 5. Version restores and downloads (decision 30)
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.version_restore (
    restore_id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id               bigint NOT NULL REFERENCES catalog.dataset,
    file_id                  bigint NOT NULL REFERENCES catalog.file,
    version_id               bigint NOT NULL REFERENCES catalog.file_version,
    target                   text NOT NULL CHECK (target IN ('current', 'copy')),
    -- copy: the new file's name (default "<stem> (restored YYYY-MM-DD)<ext>").
    name                     text COLLATE "C",
    state                    text NOT NULL DEFAULT 'requested'
                             CHECK (state IN ('requested', 'waiting_recall', 'copying',
                                              'done', 'failed')),
    recall_id                bigint REFERENCES catalog.recall,
    result_file_id           bigint REFERENCES catalog.file,
    result_version_id        bigint REFERENCES catalog.file_version,
    failure                  text,
    requested_by_identity_id bigint NOT NULL REFERENCES catalog.identity,
    requested_at             timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT version_restore_copy_name CHECK ((target = 'copy') = (name IS NOT NULL)),
    CONSTRAINT version_restore_done
        CHECK (state <> 'done' OR result_version_id IS NOT NULL),
    CONSTRAINT version_restore_waiting
        CHECK (state <> 'waiting_recall' OR recall_id IS NOT NULL)
);
CREATE INDEX version_restore_open ON catalog.version_restore (state, updated_at)
    WHERE state IN ('requested', 'waiting_recall', 'copying');
CREATE INDEX version_restore_recall ON catalog.version_restore (recall_id)
    WHERE recall_id IS NOT NULL;

CREATE TABLE catalog.download_request (
    download_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id               bigint NOT NULL REFERENCES catalog.dataset,
    file_id                  bigint NOT NULL REFERENCES catalog.file,
    version_id               bigint NOT NULL REFERENCES catalog.file_version,
    disposition              text NOT NULL DEFAULT 'attachment'
                             CHECK (disposition IN ('attachment', 'inline')),
    state                    text NOT NULL DEFAULT 'preparing'
                             CHECK (state IN ('preparing', 'ready', 'needs_recall', 'failed',
                                              'expired')),
    -- The read SAS. Never logged; cleared when it expires.
    url                      text,
    expires_at               timestamptz,
    failure                  text,
    requested_by_identity_id bigint NOT NULL REFERENCES catalog.identity,
    requested_at             timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT download_url_only_when_ready
        CHECK ((state = 'ready') = (url IS NOT NULL)),
    CONSTRAINT download_ready_expires CHECK (state <> 'ready' OR expires_at IS NOT NULL)
);
CREATE INDEX download_request_live ON catalog.download_request (expires_at)
    WHERE url IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 6. Price table overrides
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.price_table (
    backend_kind       text NOT NULL CHECK (backend_kind IN ('azure_blob', 's3', 'sim')),
    -- '' = every region the built-in table covers.
    region             text NOT NULL DEFAULT '',
    body               jsonb NOT NULL,
    as_of              date NOT NULL,
    set_by_identity_id bigint NOT NULL REFERENCES catalog.identity,
    set_at             timestamptz NOT NULL DEFAULT now(),
    entity_version     bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (backend_kind, region)
);

-- ---------------------------------------------------------------------------
-- 7. The approver's note on a policy version
-- ---------------------------------------------------------------------------
ALTER TABLE catalog.policy ADD COLUMN note text;

-- ---------------------------------------------------------------------------
-- 8. A file and its ancestors, for the portal's access checks and paths
-- ---------------------------------------------------------------------------
-- Self first (depth 0), then each parent up to the dataset root. Bounded, so
-- a corrupt parent chain can't loop.
CREATE FUNCTION catalog.file_ancestors(p_file_id bigint)
RETURNS TABLE (file_id bigint, parent_id bigint, name text, acl_id bigint, depth integer)
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE up(file_id, parent_id, name, acl_id, depth) AS (
        SELECT f.file_id, f.parent_id, f.name::text, f.acl_id, 0
          FROM catalog.file f WHERE f.file_id = p_file_id
        UNION ALL
        SELECT f.file_id, f.parent_id, f.name::text, f.acl_id, up.depth + 1
          FROM catalog.file f JOIN up ON f.file_id = up.parent_id
         WHERE up.depth < 4096
    )
    SELECT * FROM up
$$;

-- ---------------------------------------------------------------------------
-- 9. Queues
-- ---------------------------------------------------------------------------
--   portal           version restores and download SAS minting
--   identity_resolve a user's groups from Graph (token group overage)
SELECT pgmq.create(q) FROM unnest(ARRAY['portal', 'identity_resolve']) AS q;
SELECT pgmq.create(q || '_dlq') FROM unnest(ARRAY['portal', 'identity_resolve']) AS q;
