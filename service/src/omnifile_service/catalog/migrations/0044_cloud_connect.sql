-- Migration 0044: Connect Azure and AWS from the portal, Storage Mover
-- deployment and source scans (docs/design/cloud-connect.md; WP19).
--
-- - `catalog.azure_connect_op`: a delegated sign-in to Azure Resource
--   Manager in flight. It holds a PKCE verifier until the code is redeemed,
--   so it is `web`'s alone, as `oidc_login` is. No token is ever stored.
-- - `catalog.azure_connection`: one row: the latest listing of tenants,
--   subscriptions and resource groups, the admin's choice, what discovery
--   found and what the admin confirmed.
-- - `catalog.azure_grant`: every role assignment the service made for its
--   own identities, active or removed, and who made or removed it.
-- - `catalog.aws_connection`: an AWS account's settings entry as the admin
--   confirmed it. It is shown for the deployment's settings and never read
--   by the S3 backend (operator settings stay operator settings, B1).
-- - `catalog.cloud_task`: work the worker does for these pages (an AWS
--   fetch, a Storage Mover deployment, a source scan), with its progress,
--   on the `cloud_task` queue.

CREATE TABLE catalog.azure_connect_op (
    op_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    purpose          text NOT NULL CHECK (purpose IN ('list', 'grant', 'remove')),
    state            text NOT NULL DEFAULT 'started'
                     CHECK (state IN ('started', 'done', 'failed', 'expired')),
    oauth_state      text NOT NULL UNIQUE,
    nonce            text NOT NULL,
    -- Deleted (set NULL) when the code is redeemed.
    code_verifier    text,
    identity_id      bigint NOT NULL REFERENCES catalog.identity,
    entra_object_id  uuid NOT NULL,
    -- grant: the plan items (key, scope, role, principal) fixed at the start;
    -- remove: the grant ids. The callback does exactly this, nothing else.
    plan             jsonb NOT NULL DEFAULT '[]',
    detail           text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    expires_at       timestamptz NOT NULL,
    finished_at      timestamptz
);
CREATE INDEX azure_connect_op_recent ON catalog.azure_connect_op (created_at DESC);

CREATE TABLE catalog.azure_connection (
    connection_id    integer PRIMARY KEY DEFAULT 1 CHECK (connection_id = 1),
    tenant_id        text NOT NULL,
    listing          jsonb,
    listed_at        timestamptz,
    listed_by        bigint REFERENCES catalog.identity,
    choice           jsonb,
    chosen_at        timestamptz,
    chosen_by        bigint REFERENCES catalog.identity,
    discovered       jsonb,
    discovered_at    timestamptz,
    confirmed        jsonb,
    confirmed_at     timestamptz,
    confirmed_by     bigint REFERENCES catalog.identity,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE catalog.azure_grant (
    grant_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    purpose            text NOT NULL CHECK (purpose IN ('discover', 'storage_mover')),
    scope              text NOT NULL CHECK (scope ~ '^/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/]{1,90}$'),
    principal          text NOT NULL CHECK (principal IN ('web', 'worker')),
    principal_id       uuid NOT NULL,
    role_name          text NOT NULL,
    role_definition_id text NOT NULL,
    assignment_id      text NOT NULL,
    state              text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'removed')),
    made_by            bigint NOT NULL REFERENCES catalog.identity,
    made_at            timestamptz NOT NULL DEFAULT now(),
    op_id              bigint REFERENCES catalog.azure_connect_op ON DELETE SET NULL,
    removed_by         bigint REFERENCES catalog.identity,
    removed_at         timestamptz,
    CHECK ((state = 'removed') = (removed_at IS NOT NULL))
);
-- One active grant per role assignment.
CREATE UNIQUE INDEX azure_grant_active ON catalog.azure_grant (assignment_id)
    WHERE state = 'active';

CREATE TABLE catalog.aws_connection (
    account_id     text PRIMARY KEY CHECK (account_id ~ '^[0-9]{12}$'),
    region         text NOT NULL,
    deployment_id  text NOT NULL CHECK (deployment_id ~ '^[a-z0-9][a-z0-9-]{1,19}$'),
    -- The OMNIFILE_AWS_ACCOUNTS entry: names, ARNs and endpoints, never a secret.
    entry          jsonb NOT NULL,
    outputs        jsonb NOT NULL DEFAULT '{}',
    buckets        text[] NOT NULL DEFAULT '{}',
    confirmed_by   bigint NOT NULL REFERENCES catalog.identity,
    confirmed_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE catalog.cloud_task (
    task_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('aws_fetch', 'mover_deploy', 'source_scan')),
    identity_id  bigint NOT NULL REFERENCES catalog.identity,
    -- What to do: names and ids only, never a secret or a credential.
    params       jsonb NOT NULL DEFAULT '{}',
    state        text NOT NULL DEFAULT 'pending'
                 CHECK (state IN ('pending', 'running', 'done', 'capped', 'failed')),
    -- A scan's running totals and where its reader is (resume).
    progress     jsonb NOT NULL DEFAULT '{}',
    result       jsonb NOT NULL DEFAULT '{}',
    detail       text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);
CREATE INDEX cloud_task_kind ON catalog.cloud_task (kind, created_at DESC);

SELECT pgmq.create('cloud_task');
SELECT pgmq.create('cloud_task_dlq');
