-- Migration 0038: Annex phase A1, Copy mode (docs/design/annex.md, decision 51).
--
-- An annex watches a directory on someone else's NAS and copies its files to
-- Azure as plain blobs. The edge next to the NAS reads it (read-only mount)
-- and uploads through the usual /uploads path. A copy whose every block was
-- put with its x-ms-content-crc64 is verified at commit (Azure checked each
-- block as it stored it: verified_by = 'upload_block_crc', the usual case);
-- any other is read back in Azure by the verify queue ('azure_read_crc',
-- decision 24). A2's removals need the in-Azure read of the whole blob
-- (annex.md section 3.3). Nothing is ever written to the NAS in A1.
--
-- - `catalog.filer`: a registered NAS, with one `backend` row of kind `nas`
--   (decision 35 reserved the kind). The service never reads NAS bytes.
-- - `catalog.source` gains kind `annex`: (nas backend, container = share or
--   export, prefix = path) -> a folder of the annex's dataset.
-- - `catalog.annex`, `catalog.annex_ruleset` (only the fixed Copy template in
--   A1), `catalog.annex_file` (one per copied file) and `catalog.annex_event`
--   (partitioned by day, kept by the worker's partition upkeep).
-- - ACLs captured from the NAS keep their raw descriptor; NT-style ACLs are
--   evaluated deny-first (`catalog.acl_can_read` with `deny_first`), so a
--   group deny is never outvoted by a group allow.
-- - Drift from reconciliation goes to `catalog.reconcile_drift`, source
--   `annex`.

-- ---------------------------------------------------------------------------
-- 1. Kinds
-- ---------------------------------------------------------------------------
ALTER TABLE catalog.backend DROP CONSTRAINT backend_kind_check;
ALTER TABLE catalog.backend ADD CONSTRAINT backend_kind_check
    CHECK (kind IN ('azure_blob', 's3', 'sim', 'nas'));

ALTER TABLE catalog.source DROP CONSTRAINT source_kind_check;
ALTER TABLE catalog.source ADD CONSTRAINT source_kind_check
    CHECK (kind IN ('source', 'adopt', 'annex'));

ALTER TABLE catalog.file DROP CONSTRAINT file_origin_check;
ALTER TABLE catalog.file ADD CONSTRAINT file_origin_check
    CHECK (origin IN ('edge', 's3_inventory', 'adopt', 'service', 'annex'));

ALTER TABLE catalog.reconcile_drift DROP CONSTRAINT reconcile_drift_source_check;
ALTER TABLE catalog.reconcile_drift ADD CONSTRAINT reconcile_drift_source_check
    CHECK (source IN ('reconcile', 'changefeed', 'seed', 'migrate', 'annex'));
ALTER TABLE catalog.reconcile_drift DROP CONSTRAINT reconcile_drift_kind_check;
ALTER TABLE catalog.reconcile_drift ADD CONSTRAINT reconcile_drift_kind_check
    CHECK (kind IN ('missing', 'unexpected', 'size_mismatch', 'tier_mismatch',
                    'retention_mismatch', 'hold_mismatch', 'deleted_externally',
                    'feed_record_failed', 'unmappable', 'path_conflict',
                    'checksum_mismatch',
                    -- annex reconciliation (annex.md section 2.3)
                    'missed_arrival', 'missed_departure', 'changed_no_event',
                    'copy_mismatch'));

-- ---------------------------------------------------------------------------
-- 2. ACLs captured from a NAS
-- ---------------------------------------------------------------------------
-- The raw descriptor, kept whole so a later restore (phase A3) puts back
-- exactly what was there: `ntfs_sd` (self-relative NT security descriptor,
-- over SMB), `nfs4` (the NFSv4 ACL as XDR), or `posix` (mode only).
ALTER TABLE catalog.acl
    ADD COLUMN raw_kind text CHECK (raw_kind IN ('ntfs_sd', 'nfs4', 'posix')),
    ADD COLUMN raw bytea,
    -- NT and NFSv4 ACLs: any matching deny wins over every allow.
    ADD COLUMN deny_first boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT acl_raw_kind CHECK (raw IS NULL OR raw_kind IS NOT NULL);

-- Deny-first evaluation (NT and NFSv4 ACLs, reduced to allow and deny sets
-- by the service): any deny that matches the user, one of their groups or
-- Everyone refuses; else any matching allow grants. A SID mapped wrongly can
-- only lose access.
CREATE FUNCTION catalog.acl_can_read(p_read catalog.sid[], p_deny catalog.sid[],
                                     p_user text, p_groups text[], p_deny_first boolean)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN NOT coalesce(p_deny_first, false)
            THEN catalog.acl_can_read(p_read, p_deny, p_user, p_groups)
        WHEN p_user = ANY (p_deny::text[]) THEN false
        WHEN coalesce(p_groups, '{}') && p_deny::text[] THEN false
        WHEN 'S-1-1-0' = ANY (p_deny::text[]) THEN false
        WHEN p_user = ANY (p_read::text[]) THEN true
        WHEN coalesce(p_groups, '{}') && p_read::text[] THEN true
        ELSE 'S-1-1-0' = ANY (p_read::text[])
    END
$$;

-- ---------------------------------------------------------------------------
-- 3. Filers
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.filer (
    filer_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name              text NOT NULL UNIQUE,
    -- Open (annex.md section 7): every NAS is a generic NFS/SMB server in v1.
    vendor            text NOT NULL DEFAULT 'generic'
                      CHECK (vendor IN ('generic', 'ontap', 'powerscale')),
    host              text NOT NULL CHECK (host ~ '^[A-Za-z0-9]([A-Za-z0-9.:-]{0,252})$'),
    protocol          text NOT NULL CHECK (protocol IN ('smb', 'nfs3', 'nfs4')),
    -- The SMB share name, or the NFS export path.
    export            text COLLATE "C" NOT NULL CHECK (export <> '' AND length(export) <= 1024),
    -- Open: later feeds (fpolicy, onefs_changelist) are additions.
    event_source      text NOT NULL DEFAULT 'scan'
                      CHECK (event_source IN ('scan', 'smb_notify')),
    -- The edge that mounts it, and with what (annex.md section 5).
    edge_id           bigint NOT NULL REFERENCES catalog.edge,
    credential_mode   text NOT NULL DEFAULT 'machine_account'
                      CHECK (credential_mode IN ('machine_account', 'service_keytab', 'sec_sys')),
    -- The `nas` backend row that sources of this filer bind to.
    backend_id        bigint NOT NULL UNIQUE REFERENCES catalog.backend,
    -- The edge's last mount check: {"ok", "read_only", "readable", "error",
    -- "kerberos", "checked_at"}.
    health            jsonb,
    health_checked_at timestamptz,
    created_by        text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    entity_version    bigint NOT NULL DEFAULT 1,
    CONSTRAINT filer_smb_notify CHECK (event_source <> 'smb_notify' OR protocol = 'smb'),
    CONSTRAINT filer_sec_sys_nfs CHECK (credential_mode <> 'sec_sys' OR protocol <> 'smb'),
    UNIQUE (host, protocol, export)
);
CREATE TRIGGER filer_bump BEFORE UPDATE ON catalog.filer
    FOR EACH ROW EXECUTE FUNCTION catalog.bump_version();

-- A filer's backend row, for `web` (which has no INSERT on catalog.backend,
-- migration 0035): kind `nas` only, private (backend_guard refuses an app a
-- public row), found by name. NULL when the name is taken. Owner-run
-- (catalog/privileges.py OWNER_FUNCTIONS).
CREATE FUNCTION catalog.nas_backend_add(p_name text, p_config jsonb) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v bigint;
BEGIN
    IF p_name IS NULL OR p_name !~ '^nas-[A-Za-z0-9][A-Za-z0-9._-]{0,62}$'
       OR jsonb_typeof(p_config) <> 'object' THEN
        RAISE EXCEPTION 'a filer backend is named nas-<filer>, with an object config'
            USING ERRCODE = 'OF011';
    END IF;
    INSERT INTO catalog.backend (name, kind, config, private_network)
         VALUES (p_name, 'nas', p_config, true)
    ON CONFLICT (name) DO NOTHING RETURNING backend_id INTO v;
    RETURN v;
END $$;

-- ---------------------------------------------------------------------------
-- 4. Annexes
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.annex (
    annex_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                 text NOT NULL UNIQUE,
    source_id            bigint NOT NULL UNIQUE REFERENCES catalog.source,
    filer_id             bigint NOT NULL REFERENCES catalog.filer,
    dataset_id           bigint NOT NULL REFERENCES catalog.dataset,
    edge_id              bigint NOT NULL REFERENCES catalog.edge,
    -- The path under the filer's export ('' = the whole export; no leading
    -- or trailing '/').
    path                 text COLLATE "C" NOT NULL DEFAULT '',
    -- Open: archive_after, move_now and custom come with phase A2/A4.
    mode                 text NOT NULL DEFAULT 'copy' CHECK (mode IN ('copy')),
    state                text NOT NULL DEFAULT 'registered'
                         CHECK (state IN ('registered', 'baseline', 'active', 'paused',
                                          'retired')),
    -- The running rule set (catalog.annex_ruleset.version).
    ruleset_version      integer,
    quiet_minutes        integer NOT NULL DEFAULT 10 CHECK (quiet_minutes BETWEEN 1 AND 1440),
    quick_scan_minutes   integer NOT NULL DEFAULT 60
                         CHECK (quick_scan_minutes BETWEEN 1 AND 10080),
    full_scan_minutes    integer NOT NULL DEFAULT 1440
                         CHECK (full_scan_minutes BETWEEN 5 AND 43200),
    reconcile_days       integer NOT NULL DEFAULT 7 CHECK (reconcile_days BETWEEN 1 AND 90),
    -- "Purge copies of files deleted on the NAS after N days"; NULL = keep
    -- until an ArchiveAdmin purges them (decision 51, default).
    deleted_retention_days integer CHECK (deleted_retention_days BETWEEN 1 AND 36500),
    owners_sid           catalog.sid NOT NULL,
    -- The first scan (a baseline, never N "arrivals"; section 6.2).
    baseline_at          timestamptz,
    baseline_files       bigint,
    baseline_bytes       bigint,
    last_quick_scan_at   timestamptz,
    last_full_scan_at    timestamptz,
    last_reconcile_at    timestamptz,
    last_event_at        timestamptz,
    created_by           text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    entity_version       bigint NOT NULL DEFAULT 1,
    CONSTRAINT annex_path_shape
        CHECK (path = '' OR (left(path, 1) <> '/' AND right(path, 1) <> '/'))
);
CREATE INDEX annex_edge ON catalog.annex (edge_id);
CREATE INDEX annex_dataset ON catalog.annex (dataset_id);
CREATE TRIGGER annex_bump BEFORE UPDATE ON catalog.annex
    FOR EACH ROW EXECUTE FUNCTION catalog.bump_version();

-- Versions of an annex's rules. A1 holds only the fixed Copy template,
-- approved by the annex's Owners; the rule editor and proposals come with A4.
CREATE TABLE catalog.annex_ruleset (
    annex_id           bigint NOT NULL REFERENCES catalog.annex,
    version            integer NOT NULL CHECK (version > 0),
    body               jsonb NOT NULL,
    state              text NOT NULL
                       CHECK (state IN ('proposed', 'approved', 'active', 'superseded',
                                        'rejected', 'withdrawn')),
    -- Every proposal names a person (annex.md section 4.3).
    owner_identity_id  bigint NOT NULL REFERENCES catalog.identity,
    proposed_via       text NOT NULL CHECK (proposed_via IN ('portal', 'api')),
    proposed_by_app    text,
    -- An app's proposal: its API client and key (migration 0035). Nobody who
    -- answers for the client, nor the owner it named, approves it.
    api_client_id      bigint REFERENCES catalog.api_client,
    key_id             text REFERENCES catalog.api_key (key_id),
    decided_by_identity_id bigint REFERENCES catalog.identity,
    decided_at         timestamptz,
    decision_note      text,
    dry_run            jsonb,
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (annex_id, version),
    CONSTRAINT annex_ruleset_decided
        CHECK (state IN ('proposed', 'withdrawn')
               OR (decided_by_identity_id IS NOT NULL AND decided_at IS NOT NULL))
);
CREATE UNIQUE INDEX annex_ruleset_one_active
    ON catalog.annex_ruleset (annex_id) WHERE state = 'active';

-- One row per NAS file a rule copies (excluded files never enter the
-- catalog). `file_id` is the catalog file in the annex's dataset.
CREATE TABLE catalog.annex_file (
    annex_id           bigint NOT NULL REFERENCES catalog.annex,
    file_id            bigint NOT NULL UNIQUE REFERENCES catalog.file,
    -- Path under the annex's root, '/'-separated.
    path               text COLLATE "C" NOT NULL,
    -- The file id on the NAS (inode, or SMB file id), as text.
    nas_file_id        text NOT NULL,
    -- As last seen on the NAS.
    size               bigint NOT NULL CHECK (size >= 0),
    mtime_ns           bigint,
    ctime_ns           bigint,
    btime_ns           bigint,
    change_id          text,
    -- As copied: the version and the NAS attributes it was read with.
    copied_version_id  bigint,
    copied_size        bigint,
    copied_mtime_ns    bigint,
    copied_ctime_ns    bigint,
    copied_at          timestamptz,
    verified_version_id bigint,
    verified_at        timestamptz,
    first_seen         timestamptz NOT NULL,
    last_seen          timestamptz NOT NULL,
    gone_since         timestamptz,
    -- In the first scan of the directory (not an arrival).
    baseline           boolean NOT NULL DEFAULT false,
    -- Open (section 7): removing, removed and returned come with A2/A3.
    nas_state          text NOT NULL DEFAULT 'present'
                       CHECK (nas_state IN ('present', 'gone', 'removing', 'removed',
                                            'returned')),
    rule               text,
    -- Purged by an ArchiveAdmin (or the deleted-file retention): the portal
    -- records the request and queues it; the worker prunes the copy's
    -- versions and deletes their blobs; the row stays as the record.
    purge_requested_at timestamptz,
    purge_requested_by text,
    purged_at          timestamptz,
    purged_by          text,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (annex_id, file_id),
    -- Only a file deleted on the NAS may be purged (decision 40, 51).
    CONSTRAINT annex_file_purge_gone
        CHECK (purge_requested_at IS NULL OR gone_since IS NOT NULL)
);
CREATE INDEX annex_file_path ON catalog.annex_file (annex_id, path) WHERE gone_since IS NULL;
CREATE INDEX annex_file_gone ON catalog.annex_file (annex_id, gone_since)
    WHERE gone_since IS NOT NULL AND purged_at IS NULL;

-- What happened, per annex and day: the timeline and the audit of the copier.
CREATE SEQUENCE catalog.annex_event_id_seq AS bigint;
CREATE TABLE catalog.annex_event (
    event_id     bigint NOT NULL DEFAULT nextval('catalog.annex_event_id_seq'),
    at           timestamptz NOT NULL DEFAULT now(),
    annex_id     bigint NOT NULL,
    -- Open (section 7). A1 writes baseline, arrived, changed, left, renamed,
    -- copied, verified, copy_failed, skipped, drift, scan, mount and purged.
    kind         text NOT NULL CHECK (kind ~ '^[a-z][a-z_]{0,39}$'),
    path         text COLLATE "C",
    file_id      bigint,
    version_id   bigint,
    size         bigint,
    -- When the NAS saw it, if the edge knows better than `at` (arrival by
    -- creation time, departure window).
    observed_at  timestamptz,
    details      jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (at, event_id)
) PARTITION BY RANGE (at);
CREATE TABLE catalog.annex_event_default PARTITION OF catalog.annex_event DEFAULT;
CREATE INDEX annex_event_annex ON catalog.annex_event (annex_id, at);
CREATE INDEX annex_event_file ON catalog.annex_event (file_id, at) WHERE file_id IS NOT NULL;
SELECT catalog.ensure_time_partitions('catalog.annex_event', 'day', 14);

-- Annex defaults for new annexes (Settings, annex.md section 7). One row.
CREATE TABLE catalog.annex_defaults (
    singleton           boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    quiet_minutes       integer NOT NULL DEFAULT 10 CHECK (quiet_minutes BETWEEN 1 AND 1440),
    quick_scan_minutes  integer NOT NULL DEFAULT 60
                        CHECK (quick_scan_minutes BETWEEN 1 AND 10080),
    full_scan_minutes   integer NOT NULL DEFAULT 1440
                        CHECK (full_scan_minutes BETWEEN 5 AND 43200),
    reconcile_days      integer NOT NULL DEFAULT 7 CHECK (reconcile_days BETWEEN 1 AND 90),
    updated_by          text,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    entity_version      bigint NOT NULL DEFAULT 1
);
INSERT INTO catalog.annex_defaults DEFAULT VALUES;
CREATE TRIGGER annex_defaults_bump BEFORE UPDATE ON catalog.annex_defaults
    FOR EACH ROW EXECUTE FUNCTION catalog.bump_version();

-- ---------------------------------------------------------------------------
-- 5. A copy verified (at commit, or by worker/verify.py) is recorded on its
--    annex file
-- ---------------------------------------------------------------------------
CREATE FUNCTION catalog.annex_location_verified() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_annex bigint;
    v_path  text;
BEGIN
    UPDATE catalog.annex_file af
       SET verified_version_id = NEW.version_id, verified_at = NEW.verified_at,
           updated_at = now()
     WHERE af.file_id = NEW.file_id
       AND af.copied_version_id = NEW.version_id
     RETURNING af.annex_id, af.path INTO v_annex, v_path;
    IF v_annex IS NOT NULL THEN
        INSERT INTO catalog.annex_event (annex_id, kind, path, file_id, version_id, size, details)
        VALUES (v_annex, 'verified', v_path, NEW.file_id, NEW.version_id, NEW.size,
                jsonb_build_object('verified_by', NEW.verified_by,
                                   'crc64nvme', encode(NEW.crc64nvme, 'hex')));
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER location_annex_verified
    AFTER UPDATE OF verified ON catalog.location
    FOR EACH ROW WHEN (NEW.verified AND NOT OLD.verified AND NEW.role = 'primary')
    EXECUTE FUNCTION catalog.annex_location_verified();

-- ---------------------------------------------------------------------------
-- 6. Partition health covers the annex events too
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION catalog.default_partition_rows()
RETURNS TABLE (parent regclass, sampled_rows bigint)
LANGUAGE sql STABLE AS $$
    SELECT * FROM (
        SELECT 'catalog.feed_event'::regclass,
               (SELECT count(*) FROM (SELECT 1 FROM catalog.feed_event_default LIMIT 1000) s)
        UNION ALL
        SELECT 'catalog.audit_log'::regclass,
               (SELECT count(*) FROM (SELECT 1 FROM catalog.audit_log_default LIMIT 1000) s)
        UNION ALL
        SELECT 'catalog.annex_event'::regclass,
               (SELECT count(*) FROM (SELECT 1 FROM catalog.annex_event_default LIMIT 1000) s)
    ) t (parent, sampled_rows)
    WHERE sampled_rows > 0
$$;
