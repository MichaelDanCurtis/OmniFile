-- Migration 0002: catalog schema v0, core tables.
--
-- Conventions
-- * Everything lives in the `catalog` schema. PGMQ lives in `pgmq` (0001).
-- * Keys are `bigint GENERATED ALWAYS AS IDENTITY`. The APIs send them as
--   decimal strings, so JavaScript clients never lose precision.
-- * States are `text` with CHECK constraints, not enum types. Adding a value
--   to a CHECK constraint is one ALTER in a transaction; enum changes are not.
-- * The namespace is a tree: every file, directory and symlink is a row in
--   `catalog.file` with `parent_id` + `name`. Each dataset has one root row
--   (kind 'dir', parent_id NULL, name ''). A directory rename is one update
--   of one row, and one feed event. Paths are derived, never stored; folder
--   scopes (holds, pins, share roots) point at a directory's file_id.
--   Names are `text COLLATE "C"` (bytewise) and must be valid UTF-8 (the
--   database encoding); the edge keeps other names local-only.
-- * Times from the filesystem are nanoseconds since the epoch (`*_ns`
--   bigint); NFS needs more than timestamptz's microseconds.
-- * SIDs use the `catalog.sid` domain. Hashes are `bytea` of fixed length
--   (BLAKE3 32 bytes, CRC64-NVME 8 bytes big-endian, MD5 16 bytes).
-- * Scale target: 10^8 files. Each index below says what it is for; anything
--   not listed is reached through one of them or is a small table.
--
-- Indexes on the big tables, and why (rough sizes at 10^8 files, 2 versions
-- and 1.3 locations each; VERIFY in the M7 scale test):
--   file          PK; (parent_id, name) live-only: lookup + listing (~4 GB);
--                 (dataset_id, file_id): snapshot export paging; hash(blake3):
--                 dedupe, 4-byte entries instead of 32 (~2 GB); partial
--                 indexes for due work, in-flight states, tombstones and stub
--                 requests stay small because they cover few rows.
--   file_version  (file_id, seq) unique; (parent_id, committed_at) INCLUDE
--                 (superseded_at, kind): as-of listings; partial indexes for
--                 pruning and WORM expiry.
--   location      (file_id), (version_id), one primary per version, and
--                 hash(object_key) for change-feed lookups (keys are long,
--                 equality only).
-- Deliberately not indexed: file.acl_id, file.current_version_id and other
-- FKs whose parent rows are never deleted (FK checks on delete would scan).
-- Not partitioned: `file` and `file_version` are reached by id and parent,
-- never by time, so partitions would not prune. Only the feed and audit log,
-- which are written by time and dropped by age, are partitioned (0003).

CREATE SCHEMA catalog;

CREATE DOMAIN catalog.sid AS text
    CHECK (VALUE ~ '^S-1-[0-9]+(-[0-9]+){1,15}$');

CREATE DOMAIN catalog.logical_tier AS text
    CHECK (VALUE IN ('hot', 'warm', 'cold', 'archive', 'smart'));

-- ---------------------------------------------------------------------------
-- Edges (enrolled appliances)
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.edge (
    edge_id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                  text NOT NULL UNIQUE,
    state                 text NOT NULL DEFAULT 'pending'
                          CHECK (state IN ('pending', 'active', 'revoked')),
    -- One-time enrolment token, stored only as its SHA-256.
    enrolment_token_sha256 bytea CHECK (octet_length(enrolment_token_sha256) = 32),
    enrolment_expires_at  timestamptz,
    enrolled_at           timestamptz,
    -- The current client certificate, by SHA-256 of its DER encoding.
    cert_sha256           bytea UNIQUE CHECK (octet_length(cert_sha256) = 32),
    cert_serial           text,
    cert_not_after        timestamptz,
    -- The previous certificate stays valid until it expires, so a renewal
    -- that the edge never received does not lock the edge out.
    prev_cert_sha256      bytea UNIQUE CHECK (octet_length(prev_cert_sha256) = 32),
    prev_cert_not_after   timestamptz,
    software_version      text,
    last_seen_at          timestamptz,
    feed_acked_cursor     text,
    feed_acked_at         timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    revoked_at            timestamptz,
    CONSTRAINT edge_pending_has_token
        CHECK (state <> 'pending' OR (enrolment_token_sha256 IS NOT NULL
                                      AND enrolment_expires_at IS NOT NULL)),
    CONSTRAINT edge_active_has_cert
        CHECK (state <> 'active' OR (cert_sha256 IS NOT NULL AND enrolled_at IS NOT NULL)),
    CONSTRAINT edge_revoked_at
        CHECK ((state = 'revoked') = (revoked_at IS NOT NULL))
);
-- Token lookup at enrolment, only while pending.
CREATE UNIQUE INDEX edge_enrolment_token_uq
    ON catalog.edge (enrolment_token_sha256) WHERE state = 'pending';

-- ---------------------------------------------------------------------------
-- Storage backends
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.backend (
    backend_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            text NOT NULL UNIQUE,
    kind            text NOT NULL CHECK (kind IN ('azure_blob', 's3', 'sim')),  -- sim: tests
    -- Accounts, containers, buckets, regions, endpoint names. Never secrets:
    -- the service uses its managed identity, the edge Arc or Roles Anywhere.
    config          jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Decision 22: when true, every data-plane call uses the private endpoint
    -- and never falls back to a public path.
    private_network boolean NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Identity cache (AD SID <-> uid <-> Entra objectId)
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.identity (
    identity_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sid                catalog.sid NOT NULL UNIQUE,
    kind               text NOT NULL DEFAULT 'user' CHECK (kind IN ('user', 'group', 'computer')),
    sam_account_name   text,
    upn                text,
    display_name       text,
    -- From winbind (idmap_ad preferred). uid for users, gid for groups.
    posix_id           bigint CHECK (posix_id >= 0),
    entra_object_id    uuid UNIQUE,
    -- Transitive group SIDs, for portal/Teams/API access checks (decision 28).
    group_sids         catalog.sid[] NOT NULL DEFAULT '{}',
    groups_resolved_at timestamptz,
    winbind_resolved_at timestamptz,
    graph_resolved_at  timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
-- uid -> identity on every attributed read.
CREATE UNIQUE INDEX identity_posix_id_uq
    ON catalog.identity (kind, posix_id) WHERE posix_id IS NOT NULL;

-- How an edge learned that a uid is a user. `claimed` = NFS sec=sys (the
-- client says so); `proven` = Kerberos or SMB. One row per edge/uid/proof.
CREATE TABLE catalog.identity_observation (
    edge_id       bigint NOT NULL REFERENCES catalog.edge,
    uid           bigint NOT NULL CHECK (uid >= 0),
    proof         text NOT NULL CHECK (proof IN ('claimed', 'proven')),
    identity_id   bigint REFERENCES catalog.identity,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (edge_id, uid, proof)
);

-- ---------------------------------------------------------------------------
-- Datasets and shares
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.dataset (
    dataset_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name              text NOT NULL UNIQUE,
    -- Home backend: where new writes go (the destination while migrating,
    -- decision 14).
    backend_id        bigint NOT NULL REFERENCES catalog.backend,
    key_prefix        text COLLATE "C" NOT NULL DEFAULT '',
    -- Azure layout. `two_account`: ZRS live account (Smart allowed) plus an
    -- LRS/GRS archive account, archive = copy + delete. `single_account`:
    -- tiering by in-place Set Blob Tier; required for WORM (decision 31),
    -- because immutability forbids the delete. Smart + Archive is impossible
    -- on a single account (Archive is not allowed on ZRS).
    storage_layout    text NOT NULL DEFAULT 'two_account'
                      CHECK (storage_layout IN ('two_account', 'single_account')),
    migration_state   text NOT NULL DEFAULT 'steady'
                      CHECK (migration_state IN ('steady', 'migrating', 'cut_over')),
    source_backend_id bigint REFERENCES catalog.backend,
    -- v1: exactly one edge serves a dataset; its shares must be on that edge
    -- (share_dataset_edge_fk). file.state, resident_bytes and stub_* are
    -- that edge's state. Several edges per dataset need per-(edge, file)
    -- state and are a v2 item.
    edge_id           bigint REFERENCES catalog.edge,
    -- uid/gid/mode for S3 objects without x-amz-meta-file-* metadata.
    posix_defaults    jsonb NOT NULL DEFAULT '{}'::jsonb,
    entity_version    bigint NOT NULL DEFAULT 1,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT dataset_migrating_has_source
        CHECK (migration_state = 'steady' OR source_backend_id IS NOT NULL),
    CONSTRAINT dataset_source_differs
        CHECK (source_backend_id IS DISTINCT FROM backend_id),
    UNIQUE (dataset_id, edge_id)
);

-- Prefix-by-prefix cutover during a migration (decision 24).
CREATE TABLE catalog.migration_prefix (
    dataset_id          bigint NOT NULL REFERENCES catalog.dataset,
    prefix              text COLLATE "C" NOT NULL,
    state               text NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending', 'copying', 'verifying', 'verified',
                                         'cut_over', 'failed')),
    storage_mover_job   text,
    last_copy_started_at timestamptz,
    files_total         bigint,
    files_verified      bigint NOT NULL DEFAULT 0,
    -- S3 journal checked for changes up to this time (no change since copy).
    journal_checked_through timestamptz,
    verified_at         timestamptz,
    entity_version      bigint NOT NULL DEFAULT 1,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset_id, prefix),
    CONSTRAINT migration_prefix_verified
        CHECK (state NOT IN ('verified', 'cut_over')
               OR (verified_at IS NOT NULL AND files_total IS NOT NULL
                   AND files_verified = files_total))
);

CREATE TABLE catalog.share (
    share_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id          bigint NOT NULL REFERENCES catalog.dataset,
    edge_id             bigint NOT NULL REFERENCES catalog.edge,
    name                text NOT NULL,
    -- Directory this share exports (NULL = the dataset root). FK added below.
    root_dir_id         bigint,
    protocols           text[] NOT NULL
                        CHECK (cardinality(protocols) > 0
                               AND protocols <@ ARRAY['nfs3', 'nfs4', 'smb']),
    nfs_sec             text[] NOT NULL DEFAULT '{}'
                        CHECK (nfs_sec <@ ARRAY['sys', 'krb5', 'krb5i', 'krb5p']),
    -- NFS CLIENT list: named hosts or subnets. Never '*'.
    nfs_clients         text[] NOT NULL DEFAULT '{}',
    root_squash         boolean NOT NULL DEFAULT true,
    -- Decision 28: proven (Kerberos, SMB) or host-trusted (any sec=sys).
    trust_level         text GENERATED ALWAYS AS (
                            CASE WHEN 'sys' = ANY (nfs_sec) THEN 'host_trusted'
                                 ELSE 'proven' END) STORED,
    -- Recorded override: a host-trusted share holding restricted folders.
    host_trust_override boolean NOT NULL DEFAULT false,
    owners_group_sid    catalog.sid NOT NULL,
    writers_group_sid   catalog.sid,
    readers_group_sid   catalog.sid,
    on_open             text NOT NULL DEFAULT 'fail' CHECK (on_open IN ('wait', 'fail', 'auto')),
    wait_threshold_minutes integer CHECK (wait_threshold_minutes > 0),
    -- Rewind (decision 30): Owners pick the depth within the ArchiveAdmin max.
    rewind_depth_days     integer NOT NULL DEFAULT 0 CHECK (rewind_depth_days >= 0),
    rewind_depth_max_days integer NOT NULL DEFAULT 0 CHECK (rewind_depth_max_days >= 0),
    -- Labels the file protocols list (.snapshots/<label>, SMB @GMT-), e.g.
    -- daily at 00:00 UTC plus hourly for the last 24 h (fsal-namespace.md).
    rewind_labels       jsonb NOT NULL
                        DEFAULT '{"daily_at_utc": "00:00", "hourly_for_hours": 24}'::jsonb,
    -- WORM (decision 31). One-way once locked: see catalog.share_worm_guard().
    worm_enabled        boolean NOT NULL DEFAULT false,
    worm_mode           text CHECK (worm_mode IN ('unlocked_test', 'locked')),
    worm_retention_days integer CHECK (worm_retention_days > 0),
    worm_lock_deadline  timestamptz,
    worm_locked_at      timestamptz,
    entity_version      bigint NOT NULL DEFAULT 1,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (edge_id, name),
    -- v1: the share's edge is the dataset's edge (one edge per dataset).
    CONSTRAINT share_dataset_edge_fk
        FOREIGN KEY (dataset_id, edge_id) REFERENCES catalog.dataset (dataset_id, edge_id),
    CONSTRAINT share_nfs_needs_sec
        CHECK (NOT (protocols && ARRAY['nfs3', 'nfs4']) OR cardinality(nfs_sec) > 0),
    CONSTRAINT share_host_trusted_needs_clients
        CHECK ('sys' <> ALL (nfs_sec)
               OR (cardinality(nfs_clients) > 0 AND '*' <> ALL (nfs_clients) AND root_squash)),
    CONSTRAINT share_auto_needs_threshold
        CHECK ((on_open = 'auto') = (wait_threshold_minutes IS NOT NULL)),
    CONSTRAINT share_rewind_within_max
        CHECK (rewind_depth_days <= rewind_depth_max_days),
    CONSTRAINT share_worm_fields
        CHECK (CASE WHEN worm_enabled
                    THEN worm_mode IS NOT NULL AND worm_retention_days IS NOT NULL
                    ELSE worm_mode IS NULL AND worm_locked_at IS NULL END),
    CONSTRAINT share_worm_locked_at
        CHECK ((worm_mode = 'locked') = (worm_locked_at IS NOT NULL))
);
CREATE INDEX share_dataset_idx ON catalog.share (dataset_id);

-- ---------------------------------------------------------------------------
-- ACLs, content-addressed (decision 28, access-control.md section 6)
-- ---------------------------------------------------------------------------
-- Many files share one ACL, so each distinct ACL is stored once per dataset
-- and files point at it. The edge reports the hash; the full ACL is sent
-- only when the service does not know the hash yet.
CREATE TABLE catalog.acl (
    acl_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id    bigint NOT NULL REFERENCES catalog.dataset,
    sha256        bytea NOT NULL CHECK (octet_length(sha256) = 32),
    owner_sid     catalog.sid,
    group_sid     catalog.sid,
    mode          integer NOT NULL CHECK (mode BETWEEN 0 AND 4095),
    -- [{"scope":"access"|"default","tag":"user"|"group"|"mask"|"other"|
    --   "owner"|"owning_group","sid":"S-1-...","perms":"rwx"}]
    entries       jsonb NOT NULL,
    -- SIDs that may read, derived by the service. Filters portal listings
    -- and authorises portal/Teams/API recalls.
    read_sids     catalog.sid[] NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, sha256)
);
-- "Which ACLs can this user read?" (user + group SIDs && read_sids). The
-- table holds distinct ACLs only, so this stays small at 10^8 files.
CREATE INDEX acl_read_sids_gin ON catalog.acl USING gin (read_sids);

-- One row per ACL scan (on-change batch or nightly full scan).
CREATE TABLE catalog.acl_scan (
    acl_scan_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    edge_id       bigint NOT NULL REFERENCES catalog.edge,
    dataset_id    bigint NOT NULL REFERENCES catalog.dataset,
    kind          text NOT NULL CHECK (kind IN ('on_change', 'full')),
    started_at    timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz,
    entries_seen  bigint NOT NULL DEFAULT 0,
    entries_changed bigint NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Policies and proposals (anti-pattern 7: nothing applies itself)
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.policy_proposal (
    proposal_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id              bigint NOT NULL REFERENCES catalog.dataset,
    base_policy_id          bigint,
    body                    jsonb NOT NULL,
    rationale               text,
    proposed_by_kind        text NOT NULL CHECK (proposed_by_kind IN ('identity', 'application')),
    proposed_by_identity_id bigint REFERENCES catalog.identity,
    proposed_by_app         text,  -- Entra application (client) id, e.g. Prizmanyx
    state                   text NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending', 'approved', 'rejected', 'withdrawn',
                                             'superseded')),
    decided_by_identity_id  bigint REFERENCES catalog.identity,
    decided_at              timestamptz,
    decision_note           text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT proposal_by_identity
        CHECK ((proposed_by_kind = 'identity') = (proposed_by_identity_id IS NOT NULL)),
    CONSTRAINT proposal_by_app
        CHECK ((proposed_by_kind = 'application') = (proposed_by_app IS NOT NULL)),
    CONSTRAINT proposal_decided
        CHECK (state IN ('pending', 'withdrawn', 'superseded')
               OR (decided_by_identity_id IS NOT NULL AND decided_at IS NOT NULL)),
    -- Nobody approves their own proposal.
    CONSTRAINT proposal_not_self_approved
        CHECK (decided_by_identity_id IS NULL
               OR decided_by_identity_id IS DISTINCT FROM proposed_by_identity_id)
);
CREATE INDEX policy_proposal_pending ON catalog.policy_proposal (dataset_id) WHERE state = 'pending';

CREATE TABLE catalog.policy (
    policy_id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id              bigint NOT NULL REFERENCES catalog.dataset,
    version                 integer NOT NULL CHECK (version > 0),
    -- ARCHITECTURE.md section 5: mode scheduled|smart, clock, warm/cold/
    -- archive days, stub_at, resident_mb, on_open, notify, ...
    body                    jsonb NOT NULL,
    state                   text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'superseded')),
    proposal_id             bigint REFERENCES catalog.policy_proposal,
    -- Every policy is approved by a person (an ArchiveAdmin; the role is
    -- checked by the service against Entra app roles).
    approved_by_identity_id bigint NOT NULL REFERENCES catalog.identity,
    approved_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, version)
);
CREATE UNIQUE INDEX policy_one_active ON catalog.policy (dataset_id) WHERE state = 'active';
ALTER TABLE catalog.policy_proposal
    ADD CONSTRAINT policy_proposal_base_fk FOREIGN KEY (base_policy_id) REFERENCES catalog.policy;

-- ---------------------------------------------------------------------------
-- Files (the live namespace) and their versions
-- ---------------------------------------------------------------------------
-- `file` holds one row per live entry (plus tombstones), with the current
-- version's attributes copied in, so the edge replica and listings never
-- need a join. History lives in `file_version` (decision 30).
CREATE TABLE catalog.file (
    file_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id         bigint NOT NULL REFERENCES catalog.dataset,
    parent_id          bigint REFERENCES catalog.file,  -- NULL only for the dataset root
    name               text COLLATE "C" NOT NULL,
    kind               text NOT NULL DEFAULT 'file' CHECK (kind IN ('file', 'dir', 'symlink')),
    symlink_target     text,
    size               bigint NOT NULL DEFAULT 0 CHECK (size >= 0),
    mtime_ns           bigint,
    uid                bigint CHECK (uid >= 0),
    gid                bigint CHECK (gid >= 0),
    mode               integer CHECK (mode BETWEEN 0 AND 4095),
    owner_sid          catalog.sid,
    acl_id             bigint REFERENCES catalog.acl,
    -- Local state on the edge. `remote` = not local on the edge at all (a
    -- file presented from S3 or Azure by the migration namespace).
    state              text NOT NULL DEFAULT 'resident'
                       CHECK (state IN ('landing', 'uploading', 'resident', 'stub', 'recalling',
                                        'remote')),
    resident_bytes     bigint NOT NULL DEFAULT 0 CHECK (resident_bytes >= 0),
    blake3             bytea CHECK (octet_length(blake3) = 32),
    crc64nvme          bytea CHECK (octet_length(crc64nvme) = 8),
    -- current_version_id: the newest committed version of any kind.
    -- content_version_id: the version holding the bytes now; metadata,
    -- rename and tombstone versions carry it forward. Locations, stubs,
    -- credentials, recalls and the edge marker all use content_version_id.
    current_version_id bigint,
    content_version_id bigint,
    -- Denormalised from the content version's primary location and its
    -- restore (kept by triggers in 0003), so the edge knows without a round
    -- trip whether a read needs a restore, and the flag never goes stale.
    primary_location_id bigint,
    remote_tier        catalog.logical_tier,
    restore_state      text CHECK (restore_state IN ('in_progress', 'completed', 'expired',
                                                     'failed')),
    restore_expires_at timestamptz,
    -- Desired state for the edge: stub this version once it is verified.
    stub_version_id    bigint,
    stub_resident_bytes bigint CHECK (stub_resident_bytes >= 0),
    -- Policy clocks.
    written_at         timestamptz,
    last_read_at       timestamptz,
    archive_at         date,
    next_action        text CHECK (next_action IN ('tier_warm', 'tier_cold', 'archive', 'stub',
                                                   'prune_versions')),
    next_action_at     timestamptz,
    origin             text NOT NULL DEFAULT 'edge'
                       CHECK (origin IN ('edge', 's3_inventory', 'adopt', 'service')),
    entity_version     bigint NOT NULL DEFAULT 1,
    deleted_at         timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT file_root_shape
        CHECK (CASE WHEN parent_id IS NULL THEN kind = 'dir' AND name = ''
                    ELSE name <> '' AND name <> '.' AND name <> '..'
                         AND strpos(name, '/') = 0 END),
    CONSTRAINT file_not_own_parent CHECK (parent_id IS DISTINCT FROM file_id),
    CONSTRAINT file_resident_bytes_le_size CHECK (resident_bytes <= size),
    CONSTRAINT file_resident_whole
        CHECK (kind <> 'file' OR state <> 'resident' OR resident_bytes = size),
    CONSTRAINT file_stub_partial CHECK (state <> 'stub' OR resident_bytes < size),
    CONSTRAINT file_symlink_target CHECK ((kind = 'symlink') = (symlink_target IS NOT NULL)),
    CONSTRAINT file_next_action_pair CHECK ((next_action IS NULL) = (next_action_at IS NULL))
) WITH (fillfactor = 85);  -- room for HOT updates of state and clocks

-- 1. Name lookup and directory listing: (parent, name) among live rows.
--    Partial, so a tombstoned name can be created again.
CREATE UNIQUE INDEX file_name_uq
    ON catalog.file (parent_id, name) WHERE deleted_at IS NULL;
-- 2. One root per dataset. Also serves "all files of a dataset" scans via
--    the FK below (dataset deletes, snapshot exports by dataset).
CREATE UNIQUE INDEX file_dataset_root_uq ON catalog.file (dataset_id) WHERE parent_id IS NULL;
CREATE INDEX file_dataset_idx ON catalog.file (dataset_id, file_id);
-- 3. Whole-file dedupe (equality only). A hash index keeps a 4-byte hash
--    per row instead of the 32-byte key: a fraction of a btree at 10^8 rows.
CREATE INDEX file_blake3_hash
    ON catalog.file USING hash (blake3) WHERE blake3 IS NOT NULL AND deleted_at IS NULL;
-- 4. Tiering scheduler: "what is due now?".
CREATE INDEX file_next_action
    ON catalog.file (next_action_at) WHERE next_action_at IS NOT NULL AND deleted_at IS NULL;
-- 5. Work in flight (small): stuck-upload sweeps and the portal's live view.
CREATE INDEX file_in_flight
    ON catalog.file (state, updated_at) WHERE state IN ('landing', 'uploading', 'recalling');
-- 6. Tombstones to purge, or to apply at the source at cutover.
CREATE INDEX file_tombstones ON catalog.file (dataset_id, deleted_at) WHERE deleted_at IS NOT NULL;

ALTER TABLE catalog.share
    ADD CONSTRAINT share_root_dir_fk FOREIGN KEY (root_dir_id) REFERENCES catalog.file;

-- Every committed content change is a version, and so is every rename,
-- attribute change and delete (a tombstone). A version that changes no
-- bytes points at the version that holds them (`content_version_id`).
-- Each version is valid from `committed_at` until `superseded_at` (NULL =
-- current), with the parent and name it had then, so "folder X at time T"
-- is one index range (catalog.folder_as_of) and a directory rename needs
-- no child versions.
CREATE TABLE catalog.file_version (
    version_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_id            bigint NOT NULL REFERENCES catalog.file,
    dataset_id         bigint NOT NULL REFERENCES catalog.dataset,
    seq                integer NOT NULL CHECK (seq > 0),  -- 1, 2, ... per file
    parent_id          bigint REFERENCES catalog.file,
    name               text COLLATE "C" NOT NULL,
    kind               text NOT NULL CHECK (kind IN ('content', 'metadata', 'rename', 'tombstone')),
    content_version_id bigint REFERENCES catalog.file_version,
    state              text NOT NULL DEFAULT 'committed'
                       CHECK (state IN ('pending', 'committed', 'pruned')),
    committed_at       timestamptz,
    superseded_at      timestamptz,
    size               bigint CHECK (size >= 0),
    mtime_ns           bigint,
    blake3             bytea CHECK (octet_length(blake3) = 32),
    crc64nvme          bytea CHECK (octet_length(crc64nvme) = 8),
    uid                bigint,
    gid                bigint,
    mode               integer,
    -- The ACL at that time: rewind shows a version only to users it allowed.
    acl_id             bigint REFERENCES catalog.acl,
    edge_id            bigint REFERENCES catalog.edge,
    edge_op_id         text,  -- the edge change that produced it
    -- WORM (decision 31): set at commit on WORM shares; may only grow.
    retention_until    timestamptz,
    legal_hold         boolean NOT NULL DEFAULT false,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (file_id, seq),
    CONSTRAINT file_version_committed CHECK (state = 'pending' OR committed_at IS NOT NULL),
    CONSTRAINT file_version_interval
        CHECK (superseded_at IS NULL OR superseded_at >= committed_at),
    -- Content and tombstone versions point at nothing. Metadata and rename
    -- versions get the file's current content version from a trigger
    -- (NULL for directories, and for files whose bytes aren't uploaded yet).
    CONSTRAINT file_version_content_ref
        CHECK (kind NOT IN ('content', 'tombstone') OR content_version_id IS NULL)
);
-- As-of listing: (folder, committed_at). The folder id implies the dataset.
-- Ordered by committed_at, so the scan stops at T; superseded_at is in the
-- index so the "still valid at T" filter needs no heap visit.
CREATE INDEX file_version_as_of
    ON catalog.file_version (parent_id, committed_at)
    INCLUDE (superseded_at, kind)
    WHERE state = 'committed';
-- Pruning beyond the rewind depth (held versions are skipped).
CREATE INDEX file_version_prunable
    ON catalog.file_version (dataset_id, superseded_at)
    WHERE superseded_at IS NOT NULL AND state = 'committed' AND NOT legal_hold;
-- WORM expiry sweeps.
CREATE INDEX file_version_retained
    ON catalog.file_version (retention_until) WHERE retention_until IS NOT NULL;
-- An edge op that produced a version, for idempotent replays.
CREATE UNIQUE INDEX file_version_edge_op
    ON catalog.file_version (edge_id, edge_op_id) WHERE edge_op_id IS NOT NULL;

ALTER TABLE catalog.file
    ADD CONSTRAINT file_current_version_fk FOREIGN KEY (current_version_id)
        REFERENCES catalog.file_version DEFERRABLE INITIALLY DEFERRED,
    ADD CONSTRAINT file_content_version_fk FOREIGN KEY (content_version_id)
        REFERENCES catalog.file_version DEFERRABLE INITIALLY DEFERRED,
    ADD CONSTRAINT file_stub_version_fk FOREIGN KEY (stub_version_id)
        REFERENCES catalog.file_version DEFERRABLE INITIALLY DEFERRED;
-- Edge stub queue (small): stub requested, not done yet.
CREATE INDEX file_stub_requested
    ON catalog.file (dataset_id) WHERE stub_version_id IS NOT NULL AND state = 'resident';

-- ---------------------------------------------------------------------------
-- Locations: where a version's bytes are
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.location (
    location_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_id          bigint NOT NULL REFERENCES catalog.file,
    version_id       bigint NOT NULL REFERENCES catalog.file_version,
    backend_id       bigint NOT NULL REFERENCES catalog.backend,
    -- Azure "account/container"; S3 bucket.
    container        text COLLATE "C" NOT NULL,
    object_key       text COLLATE "C" NOT NULL,
    -- Azure blob version id / S3 VersionId, when versioning is on.
    backend_version_id text,
    -- `primary` is the copy reads use. Others: the migration source, the
    -- original before an archive copy deletes it, an S3 staging copy, and a
    -- WORM recall copy in the non-WORM recall container (decision 31).
    role             text NOT NULL CHECK (role IN ('primary', 'secondary', 'staging', 'recall_copy')),
    state            text NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending', 'committed', 'deleting', 'deleted')),
    logical_tier     catalog.logical_tier,
    storage_class    text,  -- native: Hot/Cool/Cold/Archive/Smart, STANDARD/GLACIER/...
    tier_changed_at  timestamptz,  -- minimum-duration clock
    -- Decision 29: once a blob is given an explicit tier it can never return
    -- to Azure Smart. One-way: see catalog.location_guard().
    explicit_tier_set boolean NOT NULL DEFAULT false,
    size             bigint CHECK (size >= 0),
    crc64nvme        bytea CHECK (octet_length(crc64nvme) = 8),
    md5              bytea CHECK (octet_length(md5) = 16),
    etag             text,
    verified         boolean NOT NULL DEFAULT false,
    verified_at      timestamptz,
    -- upload_block_crc: the edge sent per-block x-ms-content-crc64 and the
    -- service combined them; azure_read_crc: read in Azure and computed;
    -- s3_batch_checksum: S3 Batch Compute checksum (decision 24).
    verified_by      text CHECK (verified_by IN ('upload_block_crc', 'azure_read_crc',
                                                 's3_batch_checksum')),
    committed_at     timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT location_verified
        CHECK (NOT verified OR (verified_at IS NOT NULL AND verified_by IS NOT NULL
                                AND crc64nvme IS NOT NULL AND state = 'committed')),
    CONSTRAINT location_committed
        CHECK (state = 'pending' OR committed_at IS NOT NULL)
);
-- All locations of a file (and of its versions).
CREATE INDEX location_file_idx ON catalog.location (file_id);
CREATE INDEX location_version_idx ON catalog.location (version_id);
-- At most one primary copy per version, among live rows.
CREATE UNIQUE INDEX location_one_primary
    ON catalog.location (version_id) WHERE role = 'primary' AND state IN ('pending', 'committed');
-- Change feed / inventory reconciliation: object name -> location. Hash
-- index again, because keys are long and only equality is needed.
CREATE INDEX location_key_hash ON catalog.location USING hash (object_key);

ALTER TABLE catalog.file
    ADD CONSTRAINT file_primary_location_fk
        FOREIGN KEY (primary_location_id) REFERENCES catalog.location DEFERRABLE INITIALLY DEFERRED;

-- ---------------------------------------------------------------------------
-- Tier pins (decision 29): a manual tier from the portal
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.tier_pin (
    pin_id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id           bigint NOT NULL REFERENCES catalog.dataset,
    -- A file, or a folder (a kind='dir' row; applies to everything below it).
    file_id              bigint NOT NULL REFERENCES catalog.file,
    logical_tier         catalog.logical_tier NOT NULL,
    until                timestamptz,       -- NULL = until released
    reason               text,
    set_by_identity_id   bigint NOT NULL REFERENCES catalog.identity,
    set_at               timestamptz NOT NULL DEFAULT now(),
    released_at          timestamptz,
    released_by_identity_id bigint REFERENCES catalog.identity,
    CONSTRAINT tier_pin_released
        CHECK ((released_at IS NULL) = (released_by_identity_id IS NULL))
);
-- One active pin per file or folder. To find a file's effective pin the
-- service walks its ancestors (tree depth, not table size).
CREATE UNIQUE INDEX tier_pin_active
    ON catalog.tier_pin (file_id) WHERE released_at IS NULL;

-- ---------------------------------------------------------------------------
-- Holds (legal hold, keep online)
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.hold (
    hold_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id           bigint NOT NULL REFERENCES catalog.dataset,
    file_id              bigint NOT NULL REFERENCES catalog.file,  -- a file or a folder
    kind                 text NOT NULL CHECK (kind IN ('legal', 'keep_online')),
    reason               text NOT NULL,
    set_by_identity_id   bigint REFERENCES catalog.identity,
    set_by_app           text,
    set_at               timestamptz NOT NULL DEFAULT now(),
    until                timestamptz,
    released_at          timestamptz,
    released_by_identity_id bigint REFERENCES catalog.identity,
    CONSTRAINT hold_setter CHECK (num_nonnulls(set_by_identity_id, set_by_app) = 1)
);
CREATE INDEX hold_active ON catalog.hold (file_id) WHERE released_at IS NULL;

-- ---------------------------------------------------------------------------
-- Restores (provider side) and recalls (user side)
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.restore (
    restore_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    location_id        bigint NOT NULL REFERENCES catalog.location,
    backend_id         bigint NOT NULL REFERENCES catalog.backend,
    mechanism          text NOT NULL
                       CHECK (mechanism IN ('azure_copy_rehydrate', 's3_restore_object',
                                            's3_batch_restore')),
    -- Azure: standard|high. S3: expedited|standard|bulk.
    speed              text NOT NULL CHECK (speed IN ('expedited', 'standard', 'bulk', 'high')),
    provider_ref       text,  -- Azure copy id; S3 Batch job id
    target_location_id bigint REFERENCES catalog.location,  -- Azure: the rehydrated copy
    days               integer CHECK (days > 0),  -- S3 temporary copy lifetime
    state              text NOT NULL DEFAULT 'requested'
                       CHECK (state IN ('requested', 'in_progress', 'completed', 'expired',
                                        'failed', 'cancelled')),
    triggered_by       text NOT NULL CHECK (triggered_by IN ('user_recall', 'migration_wave', 'api')),
    migrate_on_complete boolean NOT NULL DEFAULT false,  -- decision 15
    bytes              bigint,
    requested_at       timestamptz NOT NULL DEFAULT now(),
    eta                timestamptz,
    completed_at       timestamptz,
    expires_at         timestamptz,  -- S3 restored copy; none for Intelligent-Tiering
    failure            text,
    entity_version     bigint NOT NULL DEFAULT 1,
    updated_at         timestamptz NOT NULL DEFAULT now()
);
-- Start-or-join: one open restore per location.
CREATE UNIQUE INDEX restore_one_open
    ON catalog.restore (location_id) WHERE state IN ('requested', 'in_progress');
-- Restored-copy expiry tracking.
CREATE INDEX restore_expiring
    ON catalog.restore (expires_at) WHERE state = 'completed' AND expires_at IS NOT NULL;
-- Per-backend budgets (10 GiB/h Azure priority; 1,000 S3 restores/s).
CREATE INDEX restore_open_by_backend
    ON catalog.restore (backend_id, requested_at) WHERE state IN ('requested', 'in_progress');

CREATE TABLE catalog.recall (
    recall_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_id        bigint NOT NULL REFERENCES catalog.file,
    version_id     bigint NOT NULL REFERENCES catalog.file_version,
    edge_id        bigint REFERENCES catalog.edge,  -- the edge that fills it
    -- The steps the portal and Teams card show. `copying`: an S3-resident
    -- file's restored copy is being moved into Azure by Storage Mover
    -- (decision 20) or into the WORM recall container; cards show it as
    -- part of "rehydrating". Mapping to base.RestoreState and the FSAL's
    -- states: docs/design/fsal-socket.md section 5.5 ("State mapping").
    state          text NOT NULL DEFAULT 'requested'
                   CHECK (state IN ('requested', 'rehydrating', 'copying', 'downloading',
                                    'available', 'failed', 'cancelled')),
    restore_id     bigint REFERENCES catalog.restore,
    priority       text NOT NULL DEFAULT 'standard' CHECK (priority IN ('standard', 'high')),
    due_by         timestamptz,
    eta            timestamptz,
    requested_at   timestamptz NOT NULL DEFAULT now(),
    completed_at   timestamptz,
    failure        text,
    entity_version bigint NOT NULL DEFAULT 1,
    updated_at     timestamptz NOT NULL DEFAULT now()
);
-- Start-or-join: one active recall per content version (a rewind read of
-- an old version and a read of the current one are different recalls).
CREATE UNIQUE INDEX recall_one_active
    ON catalog.recall (file_id, version_id)
    WHERE state IN ('requested', 'rehydrating', 'copying', 'downloading');
CREATE INDEX recall_restore_idx ON catalog.recall (restore_id) WHERE restore_id IS NOT NULL;

-- Everyone who asked for a recall gets a card.
CREATE TABLE catalog.recall_requester (
    requester_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recall_id     bigint NOT NULL REFERENCES catalog.recall,
    identity_id   bigint REFERENCES catalog.identity,  -- NULL until the uid resolves
    uid           bigint,
    via           text NOT NULL CHECK (via IN ('nfs', 'smb', 'portal', 'teams', 'api')),
    proof         text NOT NULL CHECK (proof IN ('claimed', 'proven')),
    sec_flavour   text CHECK (sec_flavour IN ('sys', 'krb5', 'krb5i', 'krb5p', 'ntlm', 'kerberos')),
    client_addr   inet,
    app_id        text,  -- partner API caller
    requested_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT recall_requester_who CHECK (num_nonnulls(identity_id, uid, app_id) >= 1),
    CONSTRAINT recall_requester_entra_proven
        CHECK (via NOT IN ('portal', 'teams') OR proof = 'proven')
);
CREATE UNIQUE INDEX recall_requester_uq
    ON catalog.recall_requester (recall_id, identity_id, uid, via) NULLS NOT DISTINCT;

CREATE TABLE catalog.notification (
    notification_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recall_id           bigint NOT NULL REFERENCES catalog.recall,
    identity_id         bigint NOT NULL REFERENCES catalog.identity,
    channel             text NOT NULL CHECK (channel IN ('teams', 'email')),
    state               text NOT NULL DEFAULT 'queued'
                        CHECK (state IN ('queued', 'sent', 'failed', 'fell_back')),
    -- To update the Adaptive Card in place.
    teams_conversation_id text,
    teams_activity_id   text,
    last_card_state     text,
    attempts            integer NOT NULL DEFAULT 0,
    last_error          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (recall_id, identity_id, channel)
);

-- Teams card button intents. One row per button on a sent card; the relay
-- forwards a click as a signed intent, and the service consumes the row
-- once (UPDATE ... WHERE used_at IS NULL), so a replayed click is refused
-- even when its signature is still valid.
CREATE TABLE catalog.teams_intent (
    intent_id             uuid PRIMARY KEY,
    notification_id       bigint NOT NULL REFERENCES catalog.notification,
    action                text NOT NULL CHECK (action IN ('recall', 'keep_online', 'track')),
    file_id               bigint NOT NULL REFERENCES catalog.file,
    version_id            bigint NOT NULL REFERENCES catalog.file_version,
    recipient_identity_id bigint NOT NULL REFERENCES catalog.identity,
    issued_at             timestamptz NOT NULL DEFAULT now(),
    expires_at            timestamptz NOT NULL,
    used_at               timestamptz,
    used_by_object_id     uuid,  -- Entra objectId of the clicker
    result                text,
    CONSTRAINT teams_intent_expiry CHECK (expires_at > issued_at),
    CONSTRAINT teams_intent_used CHECK ((used_at IS NULL) = (used_by_object_id IS NULL))
);
CREATE INDEX teams_intent_expiry_idx ON catalog.teams_intent (expires_at) WHERE used_at IS NULL;

-- ---------------------------------------------------------------------------
-- Idempotency keys for every mutating API call
-- ---------------------------------------------------------------------------
CREATE TABLE catalog.idempotency_key (
    principal      text NOT NULL,  -- 'edge:<id>', 'app:<client id>', 'user:<objectId>'
    key            text NOT NULL CHECK (length(key) BETWEEN 16 AND 128),
    method         text NOT NULL,
    path           text NOT NULL,
    request_sha256 bytea NOT NULL CHECK (octet_length(request_sha256) = 32),
    status         integer,  -- NULL while the first request is still running
    response       jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    PRIMARY KEY (principal, key)
);
CREATE INDEX idempotency_key_expiry ON catalog.idempotency_key (expires_at);
