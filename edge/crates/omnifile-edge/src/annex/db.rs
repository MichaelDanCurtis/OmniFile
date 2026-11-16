//! `annex.db`: the annex unit's own listing of the NAS directory
//! (`annex.md` §6.2), next to the replica, one database per annex.
//!
//! - `entry`: one row per NAS file: its file id (inode or SMB file id), size
//!   and times as last seen, first and last seen, the quiet-period clock, the
//!   catalog file it maps to, and what was copied. `state` is the copier's:
//!   `new` (waiting for the quiet period), `reported` (an `arrived` or
//!   `changed` event is in the outbox), `ready` (the service gave a
//!   `file_id`: copy it), `copied` (the copy of these attributes is
//!   recorded) or `skipped` (the service refused it, or it can't be read; looked
//!   at again later).
//! - `dir`: each directory's mtime at the last scan: a quick scan reads only
//!   directories whose mtime changed.
//! - `outbox`: events for `POST annex-events`, in order. A row goes once the
//!   service has answered for it, so a crash resends, and the batch key
//!   (first and last `seq`) makes the resend a replay.
//! - `copy_job`: the copy in flight (op id, upload id), journalled before
//!   the upload starts, so a killed unit resumes the same upload and never
//!   commits twice (the op id makes `uploads` answer with the same version).
//! - `meta`: the baseline flag and the scans' times.
//! - `facts`: what the rules look at beyond the listing (`rules.rs`): atime,
//!   owner and group (uid/gid, and the SIDs from the captured ACL), the
//!   sniffed content type, and which rule decided the file under which rule
//!   set version. `excluded` is an entry state: a rule said so; it is
//!   evaluated again when the rule set changes.

use std::path::Path;

use rusqlite::{Connection, OptionalExtension, params};

pub type Result<T> = std::result::Result<T, rusqlite::Error>;

const DDL: &str = r#"
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entry (
    path            TEXT PRIMARY KEY,
    ino             TEXT NOT NULL,
    size            INTEGER NOT NULL,
    mtime_ns        INTEGER NOT NULL,
    ctime_ns        INTEGER NOT NULL,
    btime_ns        INTEGER,
    nlink           INTEGER NOT NULL DEFAULT 1,
    first_seen      INTEGER NOT NULL,
    last_seen       INTEGER NOT NULL,
    stable_since    INTEGER NOT NULL,
    looks           INTEGER NOT NULL DEFAULT 1,
    baseline        INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL DEFAULT 'new',
    file_id         INTEGER,
    content_version INTEGER,
    reported_size   INTEGER,
    reported_mtime  INTEGER,
    reported_ctime  INTEGER,
    copied_size     INTEGER,
    copied_mtime    INTEGER,
    copied_ctime    INTEGER,
    copied_version  INTEGER,
    problem         TEXT,
    problem_at      INTEGER,
    scan_mark       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS entry_state ON entry (state);
CREATE INDEX IF NOT EXISTS entry_ino ON entry (ino);
CREATE TABLE IF NOT EXISTS dir (
    path      TEXT PRIMARY KEY,
    mtime_ns  INTEGER NOT NULL,
    scan_mark INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outbox (
    seq   INTEGER PRIMARY KEY AUTOINCREMENT,
    path  TEXT,
    kind  TEXT NOT NULL,
    event TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    path          TEXT PRIMARY KEY,
    atime_ns      INTEGER,
    uid           INTEGER,
    gid           INTEGER,
    owner_sid     TEXT,
    group_sid     TEXT,
    content_type  TEXT,
    ct_mtime      INTEGER,
    rule_id       TEXT,
    rules_version INTEGER,
    would_remove  INTEGER
);
-- Removals (phase A2.2): files the rules chose, waiting for a ticket, and the
-- journal of each removal, written before every step is taken (remover.rs).
CREATE TABLE IF NOT EXISTS removal_candidate (
    path       TEXT PRIMARY KEY,
    file_id    INTEGER NOT NULL,
    version_id INTEGER NOT NULL,
    rule       TEXT NOT NULL,
    -- The rule set version the removal was decided under.
    rules_version INTEGER NOT NULL DEFAULT 0,
    asked_at   INTEGER NOT NULL DEFAULT 0,
    reason     TEXT
);
CREATE TABLE IF NOT EXISTS removal (
    removal_id  INTEGER PRIMARY KEY,
    path        TEXT NOT NULL,
    file_id     INTEGER NOT NULL,
    version_id  INTEGER NOT NULL,
    rule        TEXT NOT NULL,
    ino         INTEGER NOT NULL,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    ctime_ns    INTEGER,
    hidden_name TEXT NOT NULL,
    state       TEXT NOT NULL,
    hidden_at   INTEGER,
    reason      TEXT,
    -- A step taken but not yet acknowledged by the service.
    unreported  TEXT,
    pointer     INTEGER,
    updated_at  INTEGER NOT NULL,
    -- The ctime W1's rename left (taken right after it), which W2 checks.
    hidden_ctime INTEGER,
    -- The verified copy's CRC-64/NVME, for the read again before W2 (SMB).
    crc64       TEXT,
    -- Transient errors: tries so far, and not before.
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_at     INTEGER NOT NULL DEFAULT 0,
    -- W3's placeholder at the name, while the hidden file is renamed over
    -- it: only this inode, with this ctime, is ever replaced.
    placeholder_ino   INTEGER,
    placeholder_ctime INTEGER
);
-- Folder ACLs sent to the service (phase A3.1, folders.rs), by hash of the
-- last one sent; paths are relative to the filer's export.
CREATE TABLE IF NOT EXISTS folder_acl (
    path TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS copy_job (
    path       TEXT PRIMARY KEY,
    file_id    INTEGER NOT NULL,
    op_id      TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    ctime_ns   INTEGER NOT NULL,
    upload_id  INTEGER,
    version_id INTEGER,
    started_at INTEGER NOT NULL
);
"#;

/// Columns added to a table after its first release: (table, column, type).
const ADDED_COLUMNS: &[(&str, &str, &str)] = &[
    // W4's pointer, recorded so a placement deletes only that note (A3.3).
    ("removal", "pointer_ino", "INTEGER"),
    ("removal", "pointer_sha", "TEXT"),
    // Decision 63: a folder's inode and ctime as the last scan saw them, and as
    // they were when its ACL was read, so a chmod or a re-created folder is read
    // again after the next quick scan, not the next full one (the review of #83).
    ("dir", "stamp", "TEXT"),
    ("folder_acl", "stamp", "TEXT"),
    // A user's file a removal's W1 hid by mistake (a save-by-rename just before
    // it), kept under this `-replaced-` name when its own is taken (#98, E2).
    ("removal", "aside_name", "TEXT"),
    // ...and that the hidden file is a user's, whatever its reason says later
    // (the confirm pass of #98, C3).
    ("removal", "users_file", "INTEGER"),
];

/// One queued event: sequence number, kind, path and the event.
pub type Queued = (i64, String, Option<String>, serde_json::Value);

/// A NAS file's attributes, as one stat saw them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Seen {
    pub ino: String,
    pub size: u64,
    pub mtime_ns: i64,
    pub ctime_ns: i64,
    pub btime_ns: Option<i64>,
    pub nlink: u64,
}

impl Seen {
    pub fn same(&self, size: u64, mtime_ns: i64, ctime_ns: i64) -> bool {
        (self.size, self.mtime_ns, self.ctime_ns) == (size, mtime_ns, ctime_ns)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    pub path: String,
    pub seen: Seen,
    pub first_seen: i64,
    pub last_seen: i64,
    pub stable_since: i64,
    pub looks: i64,
    pub baseline: bool,
    pub state: String,
    pub file_id: Option<u64>,
    pub content_version: Option<u64>,
    pub reported: Option<(u64, i64, i64)>,
    pub copied: Option<(u64, i64, i64)>,
    pub copied_version: Option<u64>,
    pub problem: Option<String>,
    pub problem_at: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CopyJob {
    pub path: String,
    pub file_id: u64,
    pub op_id: String,
    pub size: u64,
    pub mtime_ns: i64,
    pub ctime_ns: i64,
    pub upload_id: Option<u64>,
    pub version_id: Option<u64>,
}

/// What a scan did to the listing.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum Change {
    #[default]
    Same,
    New,
    Changed,
}

/// A file the rules chose for removal, waiting for its ticket.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RemovalCandidate {
    pub path: String,
    pub file_id: u64,
    pub version_id: u64,
    pub rule: String,
    pub rules_version: u64,
}

/// One removal, as journalled (remover.rs).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Removal {
    pub removal_id: u64,
    pub path: String,
    pub file_id: u64,
    pub version_id: u64,
    pub rule: String,
    pub ino: u64,
    pub size: u64,
    pub mtime_ns: i64,
    pub ctime_ns: Option<i64>,
    pub hidden_name: String,
    pub state: String,
    pub hidden_at: Option<i64>,
    pub reason: Option<String>,
    pub unreported: Option<String>,
    pub pointer: Option<bool>,
    pub updated_at: i64,
    pub hidden_ctime: Option<i64>,
    pub crc64: Option<u64>,
    pub attempts: i64,
    pub next_at: i64,
    pub placeholder: Option<(u64, i64)>,
    /// A user's file W1 hid by mistake, kept under this `-replaced-` name.
    pub aside_name: Option<String>,
    /// The hidden file is a user's, which W1 hid by mistake: kept aside, never
    /// left hidden, on every later pass.
    pub users_file: bool,
}

const REMOVAL_COLS: &str = "removal_id, path, file_id, version_id, rule, ino, size, mtime_ns, \
    ctime_ns, hidden_name, state, hidden_at, reason, unreported, pointer, updated_at, \
    hidden_ctime, crc64, attempts, next_at, placeholder_ino, placeholder_ctime, aside_name, users_file";

fn removal_row(r: &rusqlite::Row<'_>) -> Result<Removal> {
    Ok(Removal {
        removal_id: r.get::<_, i64>(0)? as u64,
        path: r.get(1)?,
        file_id: r.get::<_, i64>(2)? as u64,
        version_id: r.get::<_, i64>(3)? as u64,
        rule: r.get(4)?,
        ino: r.get::<_, i64>(5)? as u64,
        size: r.get::<_, i64>(6)? as u64,
        mtime_ns: r.get(7)?,
        ctime_ns: r.get(8)?,
        hidden_name: r.get(9)?,
        state: r.get(10)?,
        hidden_at: r.get(11)?,
        reason: r.get(12)?,
        unreported: r.get(13)?,
        pointer: r.get::<_, Option<i64>>(14)?.map(|v| v != 0),
        updated_at: r.get(15)?,
        hidden_ctime: r.get(16)?,
        crc64: r
            .get::<_, Option<String>>(17)?
            .and_then(|h| u64::from_str_radix(&h, 16).ok()),
        attempts: r.get(18)?,
        next_at: r.get(19)?,
        placeholder: match (r.get::<_, Option<i64>>(20)?, r.get::<_, Option<i64>>(21)?) {
            (Some(i), Some(c)) => Some((i as u64, c)),
            _ => None,
        },
        aside_name: r.get(22)?,
        users_file: r.get::<_, Option<i64>>(23)?.unwrap_or(0) != 0,
    })
}

pub struct AnnexDb {
    conn: Connection,
    /// What `reopen` opens: the file, or a named shared in-memory database.
    name: String,
}

static MEM_DBS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// One `facts` row.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct FactRow {
    pub atime_ns: Option<i64>,
    pub uid: Option<u32>,
    pub gid: Option<u32>,
    pub owner_sid: Option<String>,
    pub group_sid: Option<String>,
    pub content_type: Option<String>,
    pub ct_mtime: Option<i64>,
    pub rule_id: Option<String>,
    pub rules_version: Option<u64>,
    /// The copied version a `would_remove` was sent for.
    pub would_remove: Option<u64>,
}

fn opt_u64(v: Option<i64>) -> Option<u64> {
    v.map(|x| x as u64)
}

const ENTRY_COLS: &str = "path, ino, size, mtime_ns, ctime_ns, btime_ns, nlink, first_seen, \
    last_seen, stable_since, looks, baseline, state, file_id, content_version, reported_size, \
    reported_mtime, reported_ctime, copied_size, copied_mtime, copied_ctime, copied_version, \
    problem, problem_at";

fn entry_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<Entry> {
    let pair = |s: Option<i64>, m: Option<i64>, c: Option<i64>| match (s, m, c) {
        (Some(s), Some(m), Some(c)) => Some((s as u64, m, c)),
        _ => None,
    };
    Ok(Entry {
        path: r.get(0)?,
        seen: Seen {
            ino: r.get(1)?,
            size: r.get::<_, i64>(2)? as u64,
            mtime_ns: r.get(3)?,
            ctime_ns: r.get(4)?,
            btime_ns: r.get(5)?,
            nlink: r.get::<_, i64>(6)? as u64,
        },
        first_seen: r.get(7)?,
        last_seen: r.get(8)?,
        stable_since: r.get(9)?,
        looks: r.get(10)?,
        baseline: r.get::<_, i64>(11)? != 0,
        state: r.get(12)?,
        file_id: opt_u64(r.get(13)?),
        content_version: opt_u64(r.get(14)?),
        reported: pair(r.get(15)?, r.get(16)?, r.get(17)?),
        copied: pair(r.get(18)?, r.get(19)?, r.get(20)?),
        copied_version: opt_u64(r.get(21)?),
        problem: r.get(22)?,
        problem_at: r.get(23)?,
    })
}

impl AnnexDb {
    pub fn open(path: &Path) -> Result<AnnexDb> {
        Self::init(Connection::open(path)?, path.to_string_lossy().into_owned())
    }

    /// A private in-memory database (tests), which `reopen` can share while
    /// this connection is open.
    pub fn open_in_memory() -> Result<AnnexDb> {
        let n = MEM_DBS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let name = format!(
            "file:annex-mem-{}-{n}?mode=memory&cache=shared",
            std::process::id()
        );
        Self::init(Self::connect(&name)?, name)
    }

    fn connect(name: &str) -> Result<Connection> {
        use rusqlite::OpenFlags as F;
        if name.starts_with("file:") {
            Connection::open_with_flags(
                name,
                F::SQLITE_OPEN_READ_WRITE
                    | F::SQLITE_OPEN_CREATE
                    | F::SQLITE_OPEN_URI
                    | F::SQLITE_OPEN_NO_MUTEX,
            )
        } else {
            Connection::open(name)
        }
    }

    /// A second connection to the same database, for work run off the async
    /// loop (`spawn_blocking`): previews and the age pass.
    pub fn reopen(&self) -> Result<AnnexDb> {
        Self::init(Self::connect(&self.name)?, self.name.clone())
    }

    fn init(conn: Connection, name: String) -> Result<AnnexDb> {
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        conn.execute_batch(DDL)?;
        conn.execute_batch(super::placer::DDL)?;
        // Columns added since a table was first made (annex.db lives on).
        for (table, col, ty) in ADDED_COLUMNS {
            let has: bool = conn
                .prepare(&format!(
                    "SELECT 1 FROM pragma_table_info('{table}') WHERE name = ?1"
                ))?
                .exists([col])?;
            if !has {
                conn.execute_batch(&format!("ALTER TABLE {table} ADD COLUMN {col} {ty}"))?;
            }
        }
        Ok(AnnexDb { conn, name })
    }

    pub fn conn(&self) -> &Connection {
        &self.conn
    }

    pub fn tx<T>(&mut self, f: impl FnOnce(&rusqlite::Transaction<'_>) -> Result<T>) -> Result<T> {
        let tx = self.conn.transaction()?;
        let out = f(&tx)?;
        tx.commit()?;
        Ok(out)
    }

    // --- meta ---------------------------------------------------------------

    pub fn meta(&self, key: &str) -> Result<Option<String>> {
        self.conn
            .query_row("SELECT value FROM meta WHERE key = ?1", [key], |r| r.get(0))
            .optional()
    }

    pub fn set_meta(&self, key: &str, value: &str) -> Result<()> {
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?1, ?2) \
             ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            params![key, value],
        )?;
        Ok(())
    }

    pub fn meta_i64(&self, key: &str) -> Result<Option<i64>> {
        Ok(self.meta(key)?.and_then(|v| v.parse().ok()))
    }

    // --- entries ------------------------------------------------------------

    pub fn entry(&self, path: &str) -> Result<Option<Entry>> {
        self.conn
            .query_row(
                &format!("SELECT {ENTRY_COLS} FROM entry WHERE path = ?1"),
                [path],
                entry_row,
            )
            .optional()
    }

    pub fn entries_in(&self, state: &str, limit: usize) -> Result<Vec<Entry>> {
        let mut st = self.conn.prepare_cached(&format!(
            "SELECT {ENTRY_COLS} FROM entry WHERE state = ?1 ORDER BY path LIMIT ?2"
        ))?;
        st.query_map(params![state, limit as i64], entry_row)?
            .collect()
    }

    /// `new` entries whose quiet clock started at or before `before`, oldest
    /// first, and `skipped` ones whose problem is older than `retry_before`.
    pub fn entries_due(&self, before: i64, retry_before: i64, limit: usize) -> Result<Vec<Entry>> {
        let mut st = self.conn.prepare_cached(&format!(
            "SELECT {ENTRY_COLS} FROM entry WHERE (state = 'new' AND stable_since <= ?1) \
             OR (state = 'skipped' AND coalesce(problem_at, 0) <= ?2) \
             ORDER BY stable_since LIMIT ?3"
        ))?;
        st.query_map(params![before, retry_before, limit as i64], entry_row)?
            .collect()
    }

    /// Entries in `state` whose path sorts after `after` (paging).
    pub fn entries_after(&self, after: &str, limit: usize) -> Result<Vec<Entry>> {
        let mut st = self.conn.prepare_cached(&format!(
            "SELECT {ENTRY_COLS} FROM entry WHERE path > ?1 ORDER BY path LIMIT ?2"
        ))?;
        st.query_map(params![after, limit as i64], entry_row)?
            .collect()
    }

    pub fn count(&self, state: Option<&str>) -> Result<i64> {
        match state {
            Some(s) => {
                self.conn
                    .query_row("SELECT count(*) FROM entry WHERE state = ?1", [s], |r| {
                        r.get(0)
                    })
            }
            None => self
                .conn
                .query_row("SELECT count(*) FROM entry", [], |r| r.get(0)),
        }
    }

    /// Record one look at a file. A new file, or new attributes, restart its
    /// quiet clock; the same attributes add a look.
    pub fn observe(
        &self,
        path: &str,
        s: &Seen,
        now: i64,
        baseline: bool,
        mark: i64,
    ) -> Result<Change> {
        let cur = self.entry(path)?;
        match cur {
            None => {
                self.conn.execute(
                    "INSERT INTO entry (path, ino, size, mtime_ns, ctime_ns, btime_ns, nlink, \
                     first_seen, last_seen, stable_since, looks, baseline, state, scan_mark) \
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8, ?8, 1, ?9, 'new', ?10)",
                    params![
                        path,
                        s.ino,
                        s.size as i64,
                        s.mtime_ns,
                        s.ctime_ns,
                        s.btime_ns,
                        s.nlink as i64,
                        now,
                        baseline as i64,
                        mark
                    ],
                )?;
                Ok(Change::New)
            }
            Some(e) if e.seen.same(s.size, s.mtime_ns, s.ctime_ns) && e.seen.ino == s.ino => {
                self.conn.execute(
                    "UPDATE entry SET last_seen = ?2, looks = looks + 1, nlink = ?3, \
                     scan_mark = ?4 WHERE path = ?1",
                    params![path, now, s.nlink as i64, mark],
                )?;
                Ok(Change::Same)
            }
            Some(e) => {
                // Changed (or another file under the name): the quiet clock
                // restarts. A `reported` or `ready` file goes back to `new`;
                // the copy, if any, stays recorded.
                let replaced = e.seen.ino != s.ino;
                self.conn.execute(
                    "UPDATE entry SET ino = ?2, size = ?3, mtime_ns = ?4, ctime_ns = ?5, \
                     btime_ns = ?6, nlink = ?7, last_seen = ?8, stable_since = ?8, looks = 1, \
                     state = 'new', problem = NULL, problem_at = NULL, scan_mark = ?9 \
                     WHERE path = ?1",
                    params![
                        path,
                        s.ino,
                        s.size as i64,
                        s.mtime_ns,
                        s.ctime_ns,
                        s.btime_ns,
                        s.nlink as i64,
                        now,
                        mark
                    ],
                )?;
                if replaced {
                    // Another NAS file: nothing of the old one carries over.
                    self.conn.execute(
                        "UPDATE entry SET file_id = NULL, content_version = NULL, \
                         copied_size = NULL, copied_mtime = NULL, copied_ctime = NULL, \
                         copied_version = NULL, reported_size = NULL, reported_mtime = NULL, \
                         reported_ctime = NULL, baseline = 0 WHERE path = ?1",
                        [path],
                    )?;
                }
                Ok(if replaced {
                    Change::New
                } else {
                    Change::Changed
                })
            }
        }
    }

    pub fn set_state(&self, path: &str, state: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE entry SET state = ?2 WHERE path = ?1",
            params![path, state],
        )?;
        Ok(())
    }

    pub fn set_reported(&self, path: &str, s: &Seen) -> Result<()> {
        self.conn.execute(
            "UPDATE entry SET state = 'reported', reported_size = ?2, reported_mtime = ?3, \
             reported_ctime = ?4 WHERE path = ?1",
            params![path, s.size as i64, s.mtime_ns, s.ctime_ns],
        )?;
        Ok(())
    }

    /// The service's answer to an `arrived` / `changed`: copy, or not.
    pub fn set_catalog(
        &self,
        path: &str,
        file_id: u64,
        content_version: Option<u64>,
        copy: bool,
    ) -> Result<()> {
        self.conn.execute(
            "UPDATE entry SET file_id = ?2, content_version = ?3, \
             state = CASE WHEN state = 'reported' THEN (CASE WHEN ?4 THEN 'ready' ELSE 'copied' END) \
                          ELSE state END, \
             copied_size = CASE WHEN ?4 OR state <> 'reported' THEN copied_size ELSE reported_size END, \
             copied_mtime = CASE WHEN ?4 OR state <> 'reported' THEN copied_mtime ELSE reported_mtime END, \
             copied_ctime = CASE WHEN ?4 OR state <> 'reported' THEN copied_ctime ELSE reported_ctime END \
             WHERE path = ?1",
            params![path, file_id as i64, content_version.map(|v| v as i64), copy],
        )?;
        Ok(())
    }

    pub fn set_copied(
        &self,
        path: &str,
        size: u64,
        mtime: i64,
        ctime: i64,
        version: u64,
    ) -> Result<()> {
        self.conn.execute(
            "UPDATE entry SET copied_size = ?2, copied_mtime = ?3, copied_ctime = ?4, \
             copied_version = ?5, content_version = ?5, \
             state = CASE WHEN size = ?2 AND mtime_ns = ?3 AND ctime_ns = ?4 THEN 'copied' \
                          ELSE 'new' END \
             WHERE path = ?1",
            params![path, size as i64, mtime, ctime, version as i64],
        )?;
        Ok(())
    }

    pub fn set_problem(
        &self,
        path: &str,
        problem: &str,
        now: i64,
        state: Option<&str>,
    ) -> Result<()> {
        self.conn.execute(
            "UPDATE entry SET problem = ?2, problem_at = ?3, state = coalesce(?4, state) \
             WHERE path = ?1",
            params![path, problem, now, state],
        )?;
        Ok(())
    }

    pub fn remove(&self, path: &str) -> Result<()> {
        self.conn
            .execute("DELETE FROM entry WHERE path = ?1", [path])?;
        self.conn
            .execute("DELETE FROM facts WHERE path = ?1", [path])?;
        Ok(())
    }

    pub fn rename(&self, from: &str, to: &str) -> Result<()> {
        self.conn
            .execute("DELETE FROM entry WHERE path = ?1", [to])?;
        self.conn.execute(
            "UPDATE entry SET path = ?2 WHERE path = ?1",
            params![from, to],
        )?;
        self.conn
            .execute("DELETE FROM facts WHERE path = ?1", [to])?;
        self.conn.execute(
            "UPDATE facts SET path = ?2 WHERE path = ?1",
            params![from, to],
        )?;
        Ok(())
    }

    // --- facts ----------------------------------------------------------------

    pub fn facts(&self, path: &str) -> Result<FactRow> {
        Ok(self
            .conn
            .query_row(
                "SELECT atime_ns, uid, gid, owner_sid, group_sid, content_type, ct_mtime, \
                 rule_id, rules_version, would_remove FROM facts WHERE path = ?1",
                [path],
                |r| {
                    Ok(FactRow {
                        atime_ns: r.get(0)?,
                        uid: r.get::<_, Option<i64>>(1)?.map(|v| v as u32),
                        gid: r.get::<_, Option<i64>>(2)?.map(|v| v as u32),
                        owner_sid: r.get(3)?,
                        group_sid: r.get(4)?,
                        content_type: r.get(5)?,
                        ct_mtime: r.get(6)?,
                        rule_id: r.get(7)?,
                        rules_version: opt_u64(r.get(8)?),
                        would_remove: opt_u64(r.get(9)?),
                    })
                },
            )
            .optional()?
            .unwrap_or_default())
    }

    /// The stat facts from a scan, written only when they differ (a scan of
    /// 10^8 files mustn't rewrite every row).
    pub fn set_stat_facts(&self, path: &str, atime_ns: i64, uid: u32, gid: u32) -> Result<()> {
        self.conn.execute(
            "INSERT INTO facts (path, atime_ns, uid, gid) VALUES (?1, ?2, ?3, ?4) \
             ON CONFLICT (path) DO UPDATE SET atime_ns = excluded.atime_ns, uid = excluded.uid, \
             gid = excluded.gid WHERE facts.atime_ns IS NOT excluded.atime_ns \
             OR facts.uid IS NOT excluded.uid OR facts.gid IS NOT excluded.gid",
            params![path, atime_ns, uid as i64, gid as i64],
        )?;
        Ok(())
    }

    pub fn set_owner_group(
        &self,
        path: &str,
        owner: Option<&str>,
        group: Option<&str>,
    ) -> Result<()> {
        self.conn.execute(
            "INSERT INTO facts (path, owner_sid, group_sid) VALUES (?1, ?2, ?3) \
             ON CONFLICT (path) DO UPDATE SET owner_sid = excluded.owner_sid, \
             group_sid = excluded.group_sid",
            params![path, owner, group],
        )?;
        Ok(())
    }

    pub fn set_content_type(&self, path: &str, ct: Option<&str>, mtime_ns: i64) -> Result<()> {
        self.conn.execute(
            "INSERT INTO facts (path, content_type, ct_mtime) VALUES (?1, ?2, ?3) \
             ON CONFLICT (path) DO UPDATE SET content_type = excluded.content_type, \
             ct_mtime = excluded.ct_mtime",
            params![path, ct, mtime_ns],
        )?;
        Ok(())
    }

    pub fn set_rule(&self, path: &str, rule_id: &str, version: u64) -> Result<()> {
        self.conn.execute(
            "INSERT INTO facts (path, rule_id, rules_version) VALUES (?1, ?2, ?3) \
             ON CONFLICT (path) DO UPDATE SET rule_id = excluded.rule_id, \
             rules_version = excluded.rules_version",
            params![path, rule_id, version as i64],
        )?;
        Ok(())
    }

    pub fn set_would_remove(&self, path: &str, version: u64) -> Result<()> {
        self.conn.execute(
            "UPDATE facts SET would_remove = ?2 WHERE path = ?1",
            params![path, version as i64],
        )?;
        Ok(())
    }

    // --- removals (phase A2.2) -------------------------------------------------

    pub fn add_removal_candidate(
        &self,
        path: &str,
        file_id: u64,
        version_id: u64,
        rule: &str,
        rules_version: u64,
    ) -> Result<()> {
        self.conn.execute(
            "INSERT INTO removal_candidate (path, file_id, version_id, rule, rules_version) \
             VALUES (?1, ?2, ?3, ?4, ?5) \
             ON CONFLICT (path) DO UPDATE SET file_id = excluded.file_id, \
             version_id = excluded.version_id, rule = excluded.rule, \
             rules_version = excluded.rules_version, asked_at = 0 \
             WHERE removal_candidate.version_id <> excluded.version_id \
             OR removal_candidate.rules_version <> excluded.rules_version",
            params![
                path,
                file_id as i64,
                version_id as i64,
                rule,
                rules_version as i64
            ],
        )?;
        Ok(())
    }

    /// Candidates not asked about since `before`, and not being removed.
    pub fn removal_candidates(&self, before: i64, limit: usize) -> Result<Vec<RemovalCandidate>> {
        let mut st = self.conn.prepare_cached(
            "SELECT c.path, c.file_id, c.version_id, c.rule, c.rules_version \
             FROM removal_candidate c \
             WHERE c.asked_at <= ?1 AND NOT EXISTS (SELECT 1 FROM removal r \
             WHERE r.path = c.path AND r.state NOT IN ('done', 'cancelled')) \
             ORDER BY c.asked_at, c.path LIMIT ?2",
        )?;
        st.query_map(params![before, limit as i64], |r| {
            Ok(RemovalCandidate {
                path: r.get(0)?,
                file_id: r.get::<_, i64>(1)? as u64,
                version_id: r.get::<_, i64>(2)? as u64,
                rule: r.get(3)?,
                rules_version: r.get::<_, i64>(4)? as u64,
            })
        })?
        .collect()
    }

    pub fn asked_candidate(&self, path: &str, now: i64, reason: Option<&str>) -> Result<()> {
        self.conn.execute(
            "UPDATE removal_candidate SET asked_at = ?2, reason = ?3 WHERE path = ?1",
            params![path, now, reason],
        )?;
        Ok(())
    }

    pub fn drop_candidate(&self, path: &str) -> Result<()> {
        self.conn
            .execute("DELETE FROM removal_candidate WHERE path = ?1", [path])?;
        Ok(())
    }

    /// A ticket, journalled before anything is done with it (asked again
    /// after a restart, the service returns the same removal: kept as is).
    pub fn add_removal(&self, r: &Removal) -> Result<bool> {
        let n = self.conn.execute(
            "INSERT OR IGNORE INTO removal (removal_id, path, file_id, version_id, rule, ino, \
             size, mtime_ns, ctime_ns, hidden_name, state, updated_at, crc64) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
            params![
                r.removal_id as i64,
                r.path,
                r.file_id as i64,
                r.version_id as i64,
                r.rule,
                r.ino as i64,
                r.size as i64,
                r.mtime_ns,
                r.ctime_ns,
                r.hidden_name,
                r.state,
                r.updated_at,
                r.crc64.map(|c| format!("{c:016x}")),
            ],
        )?;
        Ok(n == 1)
    }

    /// Removals still in progress (not done or cancelled), oldest first.
    pub fn open_removals(&self) -> Result<Vec<Removal>> {
        let mut st = self.conn.prepare_cached(&format!(
            "SELECT {REMOVAL_COLS} FROM removal WHERE state NOT IN ('done', 'cancelled') \
             OR unreported IS NOT NULL ORDER BY removal_id"
        ))?;
        st.query_map([], removal_row)?.collect()
    }

    pub fn removal(&self, id: u64) -> Result<Option<Removal>> {
        self.conn
            .query_row(
                &format!("SELECT {REMOVAL_COLS} FROM removal WHERE removal_id = ?1"),
                [id as i64],
                removal_row,
            )
            .optional()
    }

    /// The journal: the state a removal is in (written before the step it
    /// names is taken), and the step to report.
    pub fn set_removal(
        &self,
        id: u64,
        state: &str,
        reason: Option<&str>,
        unreported: Option<&str>,
        now: i64,
    ) -> Result<()> {
        // A new state starts its retries afresh.
        self.conn.execute(
            "UPDATE removal SET state = ?2, reason = coalesce(?3, reason), unreported = ?4, \
             updated_at = ?5, attempts = CASE WHEN state = ?2 THEN attempts ELSE 0 END, \
             next_at = CASE WHEN state = ?2 THEN next_at ELSE 0 END WHERE removal_id = ?1",
            params![id as i64, state, reason, unreported, now],
        )?;
        Ok(())
    }

    /// W1 done: when, and the ctime its rename left.
    /// A user's file under the hidden name (E2 of #98's review): it is the file
    /// to put back now, by its own inode. One statement, so a restart sees both.
    pub fn set_removal_swapped(&self, id: u64, ino: u64, reason: &str, now: i64) -> Result<()> {
        self.conn.execute(
            "UPDATE removal SET ino = ?2, state = 'cancelling', reason = ?3, unreported = NULL, users_file = 1, \
             updated_at = ?4, attempts = 0, next_at = 0 WHERE removal_id = ?1",
            params![id as i64, ino as i64, reason, now],
        )?;
        Ok(())
    }

    pub fn set_removal_aside(&self, id: u64, name: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE removal SET aside_name = ?2 WHERE removal_id = ?1",
            params![id as i64, name],
        )?;
        Ok(())
    }

    pub fn set_removal_hidden(&self, id: u64, at: i64, ctime: Option<i64>) -> Result<()> {
        self.conn.execute(
            "UPDATE removal SET hidden_at = ?2, hidden_ctime = ?3 WHERE removal_id = ?1",
            params![id as i64, at, ctime],
        )?;
        Ok(())
    }

    /// The hidden file's folder moved (a user renamed it): its path now.
    pub fn set_removal_path(&self, id: u64, path: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE removal SET path = ?2 WHERE removal_id = ?1",
            params![id as i64, path],
        )?;
        Ok(())
    }

    /// W3's placeholder, journalled before the rename over it.
    pub fn set_removal_placeholder(&self, id: u64, p: Option<(u64, i64)>) -> Result<()> {
        self.conn.execute(
            "UPDATE removal SET placeholder_ino = ?2, placeholder_ctime = ?3 WHERE removal_id = ?1",
            params![id as i64, p.map(|x| x.0 as i64), p.map(|x| x.1)],
        )?;
        Ok(())
    }

    /// A transient error: tries so far, and not before `next_at`.
    pub fn set_removal_retry(&self, id: u64, attempts: i64, next_at: i64) -> Result<()> {
        self.conn.execute(
            "UPDATE removal SET attempts = ?2, next_at = ?3 WHERE removal_id = ?1",
            params![id as i64, attempts, next_at],
        )?;
        Ok(())
    }

    /// Files the remover hid (or is hiding) and hasn't finished with: the
    /// read-write mount stays while they wait to be put back.
    pub fn removals_on_the_nas(&self) -> Result<bool> {
        self.conn.query_row(
            "SELECT EXISTS (SELECT 1 FROM removal WHERE state IN \
             ('hiding', 'hidden', 'deleting', 'cancelling', 'stuck', 'deleted', 'pointer'))",
            [],
            |r| r.get(0),
        )
    }

    pub fn set_removal_hidden_at(&self, id: u64, at: i64) -> Result<()> {
        self.conn.execute(
            "UPDATE removal SET hidden_at = ?2 WHERE removal_id = ?1",
            params![id as i64, at],
        )?;
        Ok(())
    }

    pub fn set_removal_pointer(&self, id: u64, written: bool) -> Result<()> {
        self.conn.execute(
            "UPDATE removal SET pointer = ?2 WHERE removal_id = ?1",
            params![id as i64, written as i64],
        )?;
        Ok(())
    }

    /// W4's pointer: its inode and the SHA-256 of its text (A3.3: a placement
    /// back under the file's own name deletes only that note).
    pub fn set_removal_pointer_file(&self, id: u64, ino: u64, sha: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE removal SET pointer_ino = ?2, pointer_sha = ?3 WHERE removal_id = ?1",
            params![id as i64, ino as i64, sha],
        )?;
        Ok(())
    }

    pub fn reported(&self, id: u64) -> Result<()> {
        self.conn.execute(
            "UPDATE removal SET unreported = NULL WHERE removal_id = ?1",
            [id as i64],
        )?;
        Ok(())
    }

    /// Removals resumed after a pause: every hidden file's grace starts again.
    pub fn restart_grace(&self, now: i64) -> Result<usize> {
        self.conn.execute(
            "UPDATE removal SET hidden_at = ?1 WHERE state = 'hidden'",
            [now],
        )
    }

    /// The latest removal's state for a path, if it has one.
    pub fn removal_state_of(&self, path: &str) -> Result<Option<String>> {
        self.conn
            .query_row(
                "SELECT state FROM removal WHERE path = ?1 ORDER BY removal_id DESC LIMIT 1",
                [path],
                |r| r.get(0),
            )
            .optional()
    }

    /// Hides (W1) taken in the last hour: the edge's own cap on the rate.
    pub fn hides_since(&self, since: i64) -> Result<i64> {
        self.conn.query_row(
            "SELECT count(*) FROM removal WHERE hidden_at >= ?1",
            [since],
            |r| r.get(0),
        )
    }

    /// A new rule set: files a rule excluded are looked at again now.
    pub fn reconsider_excluded(&self) -> Result<usize> {
        self.conn.execute(
            "UPDATE entry SET state = 'new', stable_since = 0 WHERE state = 'excluded'",
            [],
        )
    }

    /// Entries a scan didn't mark under `prefix` (`""` = all): gone from the
    /// NAS, unless renamed.
    pub fn unmarked(&self, prefix: &str, mark: i64) -> Result<Vec<Entry>> {
        let mut st = self.conn.prepare_cached(&format!(
            "SELECT {ENTRY_COLS} FROM entry WHERE scan_mark <> ?1 \
             AND (?2 = '' OR substr(path, 1, length(?2)) = ?2) ORDER BY path"
        ))?;
        st.query_map(params![mark, prefix], entry_row)?.collect()
    }

    /// Children of a directory a quick scan reads (one level), unmarked:
    /// gone, unless renamed.
    pub fn unmarked_children(&self, dir: &str, mark: i64) -> Result<Vec<Entry>> {
        let prefix = if dir.is_empty() {
            String::new()
        } else {
            format!("{dir}/")
        };
        let mut st = self.conn.prepare_cached(&format!(
            "SELECT {ENTRY_COLS} FROM entry WHERE scan_mark <> ?1 \
             AND substr(path, 1, length(?2)) = ?2 AND instr(substr(path, length(?2) + 1), '/') = 0 \
             ORDER BY path"
        ))?;
        st.query_map(params![mark, prefix], entry_row)?.collect()
    }

    pub fn by_ino(&self, ino: &str) -> Result<Vec<Entry>> {
        let mut st = self
            .conn
            .prepare_cached(&format!("SELECT {ENTRY_COLS} FROM entry WHERE ino = ?1"))?;
        st.query_map([ino], entry_row)?.collect()
    }

    // --- directories ---------------------------------------------------------

    pub fn dir_mtime(&self, path: &str) -> Result<Option<i64>> {
        self.conn
            .query_row("SELECT mtime_ns FROM dir WHERE path = ?1", [path], |r| {
                r.get(0)
            })
            .optional()
    }

    pub fn set_dir(&self, path: &str, mtime_ns: i64, mark: i64) -> Result<()> {
        self.conn.execute(
            "INSERT INTO dir (path, mtime_ns, scan_mark) VALUES (?1, ?2, ?3) \
             ON CONFLICT (path) DO UPDATE SET mtime_ns = excluded.mtime_ns, \
             scan_mark = excluded.scan_mark",
            params![path, mtime_ns, mark],
        )?;
        Ok(())
    }

    /// A folder's [`dir_stamp`](super::scan::dir_stamp), as the last scan that
    /// stat-ed it saw it.
    pub fn set_dir_stamp(&self, path: &str, stamp: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE dir SET stamp = ?2 WHERE path = ?1",
            params![path, stamp],
        )?;
        Ok(())
    }

    pub fn dir_stamp(&self, path: &str) -> Result<Option<String>> {
        Ok(self
            .conn
            .query_row("SELECT stamp FROM dir WHERE path = ?1", [path], |r| {
                r.get::<_, Option<String>>(0)
            })
            .optional()?
            .flatten())
    }

    pub fn dirs(&self) -> Result<Vec<(String, i64)>> {
        let mut st = self
            .conn
            .prepare_cached("SELECT path, mtime_ns FROM dir ORDER BY path")?;
        st.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
    }

    pub fn remove_dir_tree(&self, path: &str) -> Result<()> {
        self.conn.execute(
            "DELETE FROM dir WHERE path = ?1 OR substr(path, 1, length(?1) + 1) = ?1 || '/'",
            [path],
        )?;
        Ok(())
    }

    // --- outbox --------------------------------------------------------------

    pub fn push_event(
        &self,
        kind: &str,
        path: Option<&str>,
        event: &serde_json::Value,
    ) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO outbox (path, kind, event) VALUES (?1, ?2, ?3)",
            params![path, kind, event.to_string()],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    pub fn outbox(&self, limit: usize) -> Result<Vec<Queued>> {
        let mut st = self
            .conn
            .prepare_cached("SELECT seq, kind, path, event FROM outbox ORDER BY seq LIMIT ?1")?;
        st.query_map([limit as i64], |r| {
            let raw: String = r.get(3)?;
            Ok((
                r.get(0)?,
                r.get(1)?,
                r.get(2)?,
                serde_json::from_str(&raw).unwrap_or(serde_json::Value::Null),
            ))
        })?
        .collect()
    }

    pub fn outbox_len(&self) -> Result<i64> {
        self.conn
            .query_row("SELECT count(*) FROM outbox", [], |r| r.get(0))
    }

    pub fn drop_events(&self, upto: i64) -> Result<()> {
        self.conn
            .execute("DELETE FROM outbox WHERE seq <= ?1", [upto])?;
        Ok(())
    }

    // --- copy journal ---------------------------------------------------------

    pub fn copy_job(&self, path: &str) -> Result<Option<CopyJob>> {
        self.conn
            .query_row(
                "SELECT path, file_id, op_id, size, mtime_ns, ctime_ns, upload_id, version_id \
                 FROM copy_job WHERE path = ?1",
                [path],
                |r| {
                    Ok(CopyJob {
                        path: r.get(0)?,
                        file_id: r.get::<_, i64>(1)? as u64,
                        op_id: r.get(2)?,
                        size: r.get::<_, i64>(3)? as u64,
                        mtime_ns: r.get(4)?,
                        ctime_ns: r.get(5)?,
                        upload_id: opt_u64(r.get(6)?),
                        version_id: opt_u64(r.get(7)?),
                    })
                },
            )
            .optional()
    }

    pub fn copy_jobs(&self) -> Result<Vec<String>> {
        let mut st = self
            .conn
            .prepare_cached("SELECT path FROM copy_job ORDER BY path")?;
        st.query_map([], |r| r.get(0))?.collect()
    }

    pub fn start_copy(&self, j: &CopyJob, now: i64) -> Result<()> {
        self.conn.execute(
            "INSERT INTO copy_job (path, file_id, op_id, size, mtime_ns, ctime_ns, started_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7) ON CONFLICT (path) DO UPDATE SET \
             file_id = excluded.file_id, op_id = excluded.op_id, size = excluded.size, \
             mtime_ns = excluded.mtime_ns, ctime_ns = excluded.ctime_ns, upload_id = NULL, \
             version_id = NULL, started_at = excluded.started_at",
            params![
                j.path,
                j.file_id as i64,
                j.op_id,
                j.size as i64,
                j.mtime_ns,
                j.ctime_ns,
                now
            ],
        )?;
        Ok(())
    }

    pub fn copy_upload(&self, path: &str, upload_id: u64, version_id: u64) -> Result<()> {
        self.conn.execute(
            "UPDATE copy_job SET upload_id = ?2, version_id = ?3 WHERE path = ?1",
            params![path, upload_id as i64, version_id as i64],
        )?;
        Ok(())
    }

    pub fn end_copy(&self, path: &str) -> Result<()> {
        self.conn
            .execute("DELETE FROM copy_job WHERE path = ?1", [path])?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seen(size: u64, m: i64) -> Seen {
        Seen {
            ino: "7".into(),
            size,
            mtime_ns: m,
            ctime_ns: m,
            btime_ns: None,
            nlink: 1,
        }
    }

    #[test]
    fn a_look_at_the_same_attributes_adds_a_look_and_a_change_restarts_the_clock() {
        let db = AnnexDb::open_in_memory().unwrap();
        assert_eq!(
            db.observe("a", &seen(1, 10), 100, false, 1).unwrap(),
            Change::New
        );
        assert_eq!(
            db.observe("a", &seen(1, 10), 200, false, 2).unwrap(),
            Change::Same
        );
        let e = db.entry("a").unwrap().unwrap();
        assert_eq!((e.looks, e.stable_since, e.last_seen), (2, 100, 200));
        db.set_reported("a", &e.seen).unwrap();
        db.set_catalog("a", 5, None, true).unwrap();
        assert_eq!(db.entry("a").unwrap().unwrap().state, "ready");
        assert_eq!(
            db.observe("a", &seen(2, 11), 300, false, 3).unwrap(),
            Change::Changed
        );
        let e = db.entry("a").unwrap().unwrap();
        assert_eq!((e.state.as_str(), e.looks, e.stable_since), ("new", 1, 300));
        assert_eq!(e.file_id, Some(5), "the catalog file stays");
    }

    #[test]
    fn a_copy_of_attributes_that_changed_meanwhile_leaves_the_file_new() {
        let db = AnnexDb::open_in_memory().unwrap();
        db.observe("a", &seen(1, 10), 100, false, 1).unwrap();
        db.set_copied("a", 1, 10, 10, 9).unwrap();
        assert_eq!(db.entry("a").unwrap().unwrap().state, "copied");
        db.observe("a", &seen(2, 20), 200, false, 2).unwrap();
        db.set_copied("a", 1, 10, 10, 9).unwrap();
        assert_eq!(db.entry("a").unwrap().unwrap().state, "new");
    }

    #[test]
    fn an_answer_without_a_copy_records_the_reported_attributes_as_copied() {
        let db = AnnexDb::open_in_memory().unwrap();
        db.observe("a", &seen(1, 10), 100, false, 1).unwrap();
        db.set_reported("a", &seen(1, 10)).unwrap();
        db.set_catalog("a", 5, Some(3), false).unwrap();
        let e = db.entry("a").unwrap().unwrap();
        assert_eq!(e.state, "copied");
        assert_eq!(e.copied, Some((1, 10, 10)));
    }

    #[test]
    fn unmarked_children_are_one_level_only() {
        let db = AnnexDb::open_in_memory().unwrap();
        db.observe("d/a", &seen(1, 1), 1, false, 1).unwrap();
        db.observe("d/e/b", &seen(1, 1), 1, false, 1).unwrap();
        db.observe("x", &seen(1, 1), 1, false, 1).unwrap();
        let gone: Vec<_> = db
            .unmarked_children("d", 2)
            .unwrap()
            .into_iter()
            .map(|e| e.path)
            .collect();
        assert_eq!(gone, vec!["d/a"]);
        let top: Vec<_> = db
            .unmarked_children("", 2)
            .unwrap()
            .into_iter()
            .map(|e| e.path)
            .collect();
        assert_eq!(top, vec!["x"]);
    }

    #[test]
    fn the_outbox_keeps_order_and_drops_what_was_answered() {
        let db = AnnexDb::open_in_memory().unwrap();
        for i in 0..3 {
            db.push_event("arrived", Some("p"), &serde_json::json!({ "i": i }))
                .unwrap();
        }
        let o = db.outbox(10).unwrap();
        assert_eq!(o.len(), 3);
        db.drop_events(o[1].0).unwrap();
        let o2 = db.outbox(10).unwrap();
        assert_eq!(o2.len(), 1);
        assert_eq!(o2[0].3["i"], 2);
    }
}
