//! Replica schema v1 (`docs/design/edge-metadata.md` §4).
//!
//! Each `node` row holds two states side by side:
//! - the **desired** state from the catalog (`parent_lid`, `name`, attributes,
//!   `content_version_id`, `deleted`), written by feed apply;
//! - the **actual** filesystem state (`ino`, `ino_gen`, `fs_parent_lid`,
//!   `fs_name`, `fs_version_id`), written only after a filesystem operation
//!   succeeded.
//!
//! `fs_pending = 1` means the two may differ. Feed apply sets it in the same
//! transaction as the row change and the cursor; the materialiser clears it
//! once the filesystem matches. A crash in between is redone on restart, and
//! every filesystem operation is idempotent, so a redo is harmless.

pub const SCHEMA_VERSION: i64 = 1;

pub const DDL: &str = r#"
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node (
    lid                 INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id             INTEGER,
    dataset_id          INTEGER NOT NULL DEFAULT 0,
    parent_file_id      INTEGER,
    parent_lid          INTEGER,
    waiting_parent      INTEGER NOT NULL DEFAULT 0,
    parked_at           INTEGER,
    name                TEXT NOT NULL DEFAULT '',
    kind                TEXT NOT NULL,
    symlink_target      TEXT,
    size                INTEGER NOT NULL DEFAULT 0,
    mtime_ns            INTEGER NOT NULL DEFAULT 0,
    uid                 INTEGER NOT NULL DEFAULT 0,
    gid                 INTEGER NOT NULL DEFAULT 0,
    mode                INTEGER NOT NULL DEFAULT 0,
    owner_sid           TEXT,
    acl_id              INTEGER,
    catalog_state       TEXT,
    content_version_id  INTEGER,
    crc64nvme           INTEGER,
    location            TEXT,
    archived            INTEGER NOT NULL DEFAULT 0,
    online              INTEGER NOT NULL DEFAULT 0,
    stub_version_id     INTEGER,
    stub_resident_bytes INTEGER,
    stub_status         TEXT NOT NULL DEFAULT 'none',
    entity_version      INTEGER NOT NULL DEFAULT 0,
    deleted             INTEGER NOT NULL DEFAULT 0,
    local_dirty         INTEGER NOT NULL DEFAULT 0,
    ino                 INTEGER,
    ino_gen             INTEGER,
    fs_parent_lid       INTEGER,
    fs_name             TEXT,
    fs_version_id       INTEGER,
    fs_ctime_ns         INTEGER,
    fs_pending          INTEGER NOT NULL DEFAULT 0,
    fs_conflict         TEXT,
    fs_error            TEXT,
    fs_attempts         INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS node_file_id ON node(file_id) WHERE file_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS node_parent_name ON node(parent_lid, name);
CREATE INDEX IF NOT EXISTS node_ino ON node(ino) WHERE ino IS NOT NULL;
CREATE INDEX IF NOT EXISTS node_pending ON node(fs_pending) WHERE fs_pending = 1;
CREATE INDEX IF NOT EXISTS node_waiting ON node(parent_file_id) WHERE waiting_parent = 1;
CREATE INDEX IF NOT EXISTS node_stub ON node(stub_status) WHERE stub_status = 'wanted';

CREATE TABLE IF NOT EXISTS version (
    version_id      INTEGER PRIMARY KEY,
    file_id         INTEGER NOT NULL,
    size            INTEGER NOT NULL,
    mtime_ns        INTEGER NOT NULL DEFAULT 0,
    crc64nvme       INTEGER,
    blake3          TEXT,
    committed_at    TEXT,
    retention_until TEXT
);

CREATE TABLE IF NOT EXISTS acl (
    acl_id         INTEGER PRIMARY KEY,
    dataset_id     INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    entity_version INTEGER NOT NULL,
    body           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS share (
    share_id               INTEGER PRIMARY KEY,
    dataset_id             INTEGER NOT NULL,
    name                   TEXT NOT NULL,
    root_dir_id            INTEGER,
    on_open                TEXT NOT NULL,
    wait_threshold_minutes INTEGER,
    worm                   INTEGER NOT NULL DEFAULT 0,
    entity_version         INTEGER NOT NULL,
    body                   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS share_root ON share(root_dir_id);

CREATE TABLE IF NOT EXISTS dataset (
    dataset_id      INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    backend_kind    TEXT NOT NULL,
    migration_state TEXT NOT NULL,
    entity_version  INTEGER NOT NULL,
    body            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recall (
    recall_id       INTEGER PRIMARY KEY,
    file_id         INTEGER NOT NULL,
    version_id      INTEGER NOT NULL,
    state           TEXT NOT NULL,
    eta             TEXT,
    error           TEXT,
    read_credential TEXT,
    entity_version  INTEGER NOT NULL DEFAULT 0,
    started_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
-- One active recall per (file_id, content version).
CREATE UNIQUE INDEX IF NOT EXISTS recall_active ON recall(file_id, version_id)
    WHERE state NOT IN ('available', 'failed', 'cancelled');

CREATE TABLE IF NOT EXISTS outbox (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id      TEXT NOT NULL UNIQUE,
    lid        INTEGER,
    op         TEXT NOT NULL,
    payload    TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'queued',
    attempts   INTEGER NOT NULL DEFAULT 0,
    result     TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_state ON outbox(state);

-- Audit records for the service (control-api.md section 4.7), kept until
-- the service has them. Capped (Replica::AUDIT_MAX): the oldest go first.
CREATE TABLE IF NOT EXISTS audit (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    record TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commit_clone (
    lid          INTEGER NOT NULL,
    n            INTEGER NOT NULL,
    path         TEXT NOT NULL,
    size         INTEGER NOT NULL,
    blake3       TEXT,
    crc64nvme    INTEGER,
    upload_state TEXT NOT NULL DEFAULT 'pending',
    version_id   INTEGER,
    PRIMARY KEY (lid, n)
);

CREATE TABLE IF NOT EXISTS snap_dir (
    share_id     INTEGER NOT NULL,
    label        TEXT NOT NULL,
    dir_file_id  INTEGER NOT NULL,
    populated_at INTEGER,
    last_access  INTEGER,
    PRIMARY KEY (share_id, label, dir_file_id)
);

CREATE TABLE IF NOT EXISTS conflict (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      INTEGER NOT NULL,
    file_id INTEGER,
    lid     INTEGER,
    op_id   TEXT,
    reason  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conflict_lid ON conflict(lid, at);

CREATE TABLE IF NOT EXISTS feed (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    cursor         TEXT NOT NULL,
    acked_cursor   TEXT,
    snapshot_as_of TEXT,
    snapshot_state TEXT
);
INSERT OR IGNORE INTO feed (id, cursor) VALUES (1, '0.0');
"#;
