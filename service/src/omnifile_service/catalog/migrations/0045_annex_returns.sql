-- Migration 0045: Annex phase A3, the return paths (docs/milestones/results/
-- wp14-a3.md; docs/design/annex.md sections 4.6, 7 and 8.4; decision 51).
--
-- A recalled file can be placed back where it came from, in a restore folder,
-- or in an OmniFile share. The service decides where, from the annex's
-- return rules (`recall_completed`), and only somewhere the requester could
-- add a file to themselves; the edge writes it there only on a ticket (A3.3).
-- This migration holds the whole of A3's schema:
--
-- - `catalog.acl.write_sids` / `write_deny_sids`: for a NAS folder's ACL,
--   who may add a file to it, deny-first like reads; `catalog.acl_can_write`.
-- - `catalog.nas_folder_acl`: each folder's ACL on a filer, as the edge last
--   read it (by path relative to the export; NULL `acl_id` with an `error` is
--   a folder whose ACL couldn't be read: nobody may place a file there).
-- - `catalog.nas_target_space`: free space the edge saw, for previews.
-- - `catalog.annex_placement`: one per recall that places a file somewhere.
-- - `catalog.annex_file.hold_until` (outbound rules skip a placed file until
--   then) and `pinned_by_breaker` (the loop breaker).
-- - `catalog.annex.placements_paused` and `annex_defaults.placements_paused`:
--   switches that stop every NAS write A3 adds.
-- - `catalog.file.origin = 'annex_restore'`: a recalled copy in a share.
-- - `catalog.placement_opens` and `catalog.recalled_copy_deletion` (A3.5).

-- ---------------------------------------------------------------------------
-- 1. Who may add a file to a NAS folder
-- ---------------------------------------------------------------------------
ALTER TABLE catalog.acl
    ADD COLUMN write_sids      catalog.sid[],
    ADD COLUMN write_deny_sids catalog.sid[] NOT NULL DEFAULT '{}';
COMMENT ON COLUMN catalog.acl.write_sids IS
    'For a NAS folder''s ACL: SIDs that may add a file to it (SMB FILE_ADD_FILE, NFSv4 '
    'ACE4_ADD_FILE, POSIX w and x), after denies. NULL: not a folder''s, or not known: '
    'nobody may place a file there.';

-- The same evaluation as reads, over the write sets; NULL (not known) is no.
CREATE FUNCTION catalog.acl_can_write(p_write catalog.sid[], p_deny catalog.sid[],
                                      p_user text, p_groups text[], p_deny_first boolean)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN p_write IS NULL THEN false
                ELSE coalesce(catalog.acl_can_read(p_write, p_deny, p_user, p_groups,
                                                   p_deny_first), false)
           END
$$;

CREATE TABLE catalog.nas_folder_acl (
    filer_id    bigint NOT NULL REFERENCES catalog.filer,
    -- Relative to the filer's export; '' is the export's root.
    path        text COLLATE "C" NOT NULL,
    acl_id      bigint REFERENCES catalog.acl,
    error       text CHECK (length(error) <= 500),
    annex_id    bigint NOT NULL REFERENCES catalog.annex,
    observed_at timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (filer_id, path),
    CONSTRAINT nas_folder_acl_known CHECK ((acl_id IS NULL) = (error IS NOT NULL))
);

CREATE TABLE catalog.nas_target_space (
    filer_id    bigint NOT NULL REFERENCES catalog.filer,
    path        text COLLATE "C" NOT NULL,
    free_bytes  bigint NOT NULL CHECK (free_bytes >= 0),
    total_bytes bigint NOT NULL CHECK (total_bytes >= 0),
    annex_id    bigint NOT NULL REFERENCES catalog.annex,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (filer_id, path)
);

-- ---------------------------------------------------------------------------
-- 2. Placements
-- ---------------------------------------------------------------------------
ALTER TABLE catalog.annex
    ADD COLUMN placements_paused boolean NOT NULL DEFAULT false;
ALTER TABLE catalog.annex_defaults
    -- Settings, "pause all placements": no NAS write of A3's while it is on.
    ADD COLUMN placements_paused boolean NOT NULL DEFAULT false;
ALTER TABLE catalog.annex_file
    ADD COLUMN hold_until        timestamptz,
    ADD COLUMN pinned_by_breaker boolean NOT NULL DEFAULT false;

ALTER TABLE catalog.file DROP CONSTRAINT file_origin_check;
ALTER TABLE catalog.file ADD CONSTRAINT file_origin_check
    CHECK (origin IN ('edge', 's3_inventory', 'adopt', 'service', 'annex', 'annex_restore'));

CREATE TABLE catalog.annex_placement (
    placement_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    annex_id         bigint NOT NULL REFERENCES catalog.annex,
    file_id          bigint NOT NULL REFERENCES catalog.file,
    version_id       bigint NOT NULL,
    -- The usual recall, when the bytes are in Archive and must come back first.
    recall_id        bigint REFERENCES catalog.recall,
    -- Who it is for: always a person (an API client must name one).
    requester_identity_id bigint NOT NULL REFERENCES catalog.identity,
    -- Open: portal, teams, api.
    request_via      text NOT NULL CHECK (request_via ~ '^[a-z][a-z_]{0,31}$'),
    api_client_id    bigint,
    note             text CHECK (length(note) <= 500),
    -- The return rule that decided, under this rule set version.
    rule             text NOT NULL,
    ruleset_version  integer NOT NULL,
    -- Where: the resolved target. `nas`: {kind, filer_id, folder, name}, the
    -- folder relative to the export; `share`: {kind, share_id, folder, name}.
    target           jsonb NOT NULL,
    -- The location the requester chose, when not the rule's default.
    chosen           jsonb,
    -- Open: same_content, restored_name, overwrite_keep_version.
    on_conflict      text NOT NULL CHECK (on_conflict ~ '^[a-z][a-z_]{0,31}$'),
    hold_days        integer NOT NULL DEFAULT 0 CHECK (hold_days BETWEEN 0 AND 365),
    notify           jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- Open (section 7): requested -> rehydrating -> ready -> writing ->
    -- verifying -> securing -> placing -> placed, or failed / cancelled.
    state            text NOT NULL DEFAULT 'requested'
                     CHECK (state ~ '^[a-z][a-z_]{0,31}$'),
    -- How it ended: placed, already_there, restored_name, overwrite_refused...
    outcome          text CHECK (length(outcome) <= 64),
    -- Open: no_space, quota, access_denied, crc_mismatch, name_taken,
    -- target_missing, acl_mismatch, readback_denied, cross_volume...
    fail_reason      text CHECK (length(fail_reason) <= 64),
    fail_detail      text CHECK (length(fail_detail) <= 500),
    staging_name     text,
    final_name       text,
    final_path       text COLLATE "C",
    bytes            bigint,
    crc64            text,
    -- A recalled copy in a share (A3.2): the server-side copy, the file it
    -- became (and the version), and its use since (A3.5).
    copy_container   text,
    copy_key         text,
    copy_backend_version_id text,
    placed_file_id   bigint REFERENCES catalog.file,
    placed_version_id bigint,
    card             jsonb,
    hold_until       timestamptz,
    opens_since      bigint NOT NULL DEFAULT 0,
    distinct_openers integer NOT NULL DEFAULT 0,
    last_opened_at   timestamptz,
    deleted_at       timestamptz,
    deleted_by       text,
    delete_job       bigint,
    requested_at     timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    done_at          timestamptz,
    CONSTRAINT annex_placement_done
        CHECK ((state IN ('placed', 'failed', 'cancelled')) = (done_at IS NOT NULL)),
    CONSTRAINT annex_placement_failed CHECK ((state = 'failed') = (fail_reason IS NOT NULL))
);
-- One open placement of a file to one target: a second request joins it.
CREATE UNIQUE INDEX annex_placement_open ON catalog.annex_placement
    (file_id, md5(target::text)) WHERE state NOT IN ('placed', 'failed', 'cancelled');
CREATE INDEX annex_placement_annex ON catalog.annex_placement (annex_id, requested_at);
CREATE INDEX annex_placement_requester ON catalog.annex_placement
    (requester_identity_id, requested_at);
CREATE INDEX annex_placement_recall ON catalog.annex_placement (recall_id)
    WHERE recall_id IS NOT NULL;
CREATE INDEX annex_placement_copy ON catalog.annex_placement (placed_file_id)
    WHERE placed_file_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. Recalled copies in shares (A3.5): open counts and bulk deletes
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.placement_opens (
    placement_id bigint NOT NULL REFERENCES catalog.annex_placement,
    hour         timestamptz NOT NULL,
    opens        bigint NOT NULL CHECK (opens >= 0),
    openers      integer NOT NULL CHECK (openers >= 0),
    PRIMARY KEY (placement_id, hour)
);

CREATE TABLE catalog.recalled_copy_deletion (
    deletion_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    requested_by   text NOT NULL,
    filter         jsonb,
    ids            bigint[],
    expected_count integer,
    dry_run        boolean NOT NULL DEFAULT false,
    -- Open: queued, running, done, refused.
    state          text NOT NULL DEFAULT 'queued' CHECK (state ~ '^[a-z][a-z_]{0,31}$'),
    results        jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    done_at        timestamptz
);
ALTER TABLE catalog.annex_placement
    ADD CONSTRAINT annex_placement_delete_job
        FOREIGN KEY (delete_job) REFERENCES catalog.recalled_copy_deletion (deletion_id);
