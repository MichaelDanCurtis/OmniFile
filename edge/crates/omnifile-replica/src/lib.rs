//! The edge's metadata replica (R2, `docs/design/edge-metadata.md`).
//!
//! A SQLite database in WAL mode, owned by the daemon only. It holds what
//! xfs can't: catalog ids, content versions, locations, recalls, the feed
//! cursor and the outbox. The namespace itself lives in xfs; this crate
//! decides *what* the tree should look like and the materialiser (in
//! `omnifile-stub`) makes it so.
//!
//! Feed apply (§5.1):
//! 1. one SQLite transaction per page: `entity_version`-guarded row updates,
//!    `fs_pending = 1` where the filesystem must change, and the page's
//!    `next` cursor;
//! 2. the filesystem work is read back with [`Replica::pending`] and each
//!    result recorded with [`Replica::record`];
//! 3. on restart, pending rows are simply redone.
//!
//! The feed orders by transaction, not causality, so a child can arrive
//! before its parent (parked as an orphan until the parent arrives) and a
//! directory's tombstone before its children's (the `rmdir` stays pending
//! and is retried after every page).

pub mod feed;
mod schema;

use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{Connection, OptionalExtension, Row, Transaction, params};
use serde_json::Value;
use tracing::{debug, warn};

pub use feed::{Body, Event, FileRow, Kind, RecallRow, ShareRow, parse_event};
pub use schema::SCHEMA_VERSION;

/// One row's share of the reconciliation digest: the first 8 bytes,
/// big-endian, of SHA-256("<file_id>:<entity_version>").
pub fn digest_row(file_id: u64, entity_version: u64) -> u64 {
    let h = ring::digest::digest(
        &ring::digest::SHA256,
        format!("{file_id}:{entity_version}").as_bytes(),
    );
    u64::from_be_bytes(h.as_ref()[..8].try_into().unwrap())
}

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("sqlite: {0}")]
    Sql(#[from] rusqlite::Error),
    #[error("feed: {0}")]
    Feed(String),
    #[error("replica schema version {found} is newer than this daemon ({supported})")]
    SchemaTooNew { found: i64, supported: i64 },
}

pub type Result<T> = std::result::Result<T, Error>;

fn now_s() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Desired stub status of a node.
pub mod stub_status {
    pub const NONE: &str = "none";
    pub const WANTED: &str = "wanted";
    pub const DONE: &str = "done";
    pub const SKIPPED: &str = "skipped";
}

/// One replica row (desired and actual state).
#[derive(Clone, Debug, PartialEq)]
pub struct Node {
    pub lid: i64,
    pub file_id: Option<u64>,
    pub dataset_id: u64,
    pub parent_file_id: Option<u64>,
    pub parent_lid: Option<i64>,
    pub waiting_parent: bool,
    pub name: String,
    pub kind: Kind,
    pub symlink_target: Option<String>,
    pub size: u64,
    pub mtime_ns: i64,
    pub uid: u32,
    pub gid: u32,
    pub mode: u32,
    pub acl_id: Option<u64>,
    pub content_version_id: Option<u64>,
    pub crc64nvme: Option<u64>,
    pub location: Option<Value>,
    pub archived: bool,
    pub online: bool,
    pub stub_version_id: Option<u64>,
    pub stub_resident_bytes: Option<u64>,
    pub stub_status: String,
    pub entity_version: u64,
    pub deleted: bool,
    pub local_dirty: bool,
    pub ino: Option<u64>,
    pub ino_gen: Option<u32>,
    pub fs_parent_lid: Option<i64>,
    pub fs_name: Option<String>,
    pub fs_version_id: Option<u64>,
    pub fs_pending: bool,
    pub fs_conflict: Option<String>,
    pub fs_error: Option<String>,
    /// `st_ctime` (ns) right after the daemon's last write to the inode. A
    /// different ctime means someone else changed the file since.
    pub fs_ctime_ns: Option<i64>,
}

const NODE_COLS: &str = "lid, file_id, dataset_id, parent_file_id, parent_lid, waiting_parent, name, kind, \
    symlink_target, size, mtime_ns, uid, gid, mode, acl_id, content_version_id, crc64nvme, location, archived, \
    online, stub_version_id, stub_resident_bytes, stub_status, entity_version, deleted, local_dirty, ino, ino_gen, \
    fs_parent_lid, fs_name, fs_version_id, fs_pending, fs_conflict, fs_error, fs_ctime_ns";

fn u64_opt(r: &Row, i: usize) -> rusqlite::Result<Option<u64>> {
    Ok(r.get::<_, Option<i64>>(i)?.map(|v| v as u64))
}

fn node_from_row(r: &Row) -> rusqlite::Result<Node> {
    let kind: String = r.get(7)?;
    let location: Option<String> = r.get(17)?;
    Ok(Node {
        lid: r.get(0)?,
        file_id: u64_opt(r, 1)?,
        dataset_id: r.get::<_, i64>(2)? as u64,
        parent_file_id: u64_opt(r, 3)?,
        parent_lid: r.get(4)?,
        waiting_parent: r.get(5)?,
        name: r.get(6)?,
        kind: Kind::parse(&kind).unwrap_or(Kind::File),
        symlink_target: r.get(8)?,
        size: r.get::<_, i64>(9)? as u64,
        mtime_ns: r.get(10)?,
        uid: r.get::<_, i64>(11)? as u32,
        gid: r.get::<_, i64>(12)? as u32,
        mode: r.get::<_, i64>(13)? as u32,
        acl_id: u64_opt(r, 14)?,
        content_version_id: u64_opt(r, 15)?,
        crc64nvme: u64_opt(r, 16)?,
        location: location.and_then(|s| serde_json::from_str(&s).ok()),
        archived: r.get(18)?,
        online: r.get(19)?,
        stub_version_id: u64_opt(r, 20)?,
        stub_resident_bytes: u64_opt(r, 21)?,
        stub_status: r.get(22)?,
        entity_version: r.get::<_, i64>(23)? as u64,
        deleted: r.get(24)?,
        local_dirty: r.get(25)?,
        ino: u64_opt(r, 26)?,
        ino_gen: r.get::<_, Option<i64>>(27)?.map(|v| v as u32),
        fs_parent_lid: r.get(28)?,
        fs_name: r.get(29)?,
        fs_version_id: u64_opt(r, 30)?,
        fs_pending: r.get(31)?,
        fs_conflict: r.get(32)?,
        fs_error: r.get(33)?,
        fs_ctime_ns: r.get(34)?,
    })
}

/// What one feed page did.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ApplyReport {
    pub applied: usize,
    /// Older or equal `entity_version`: skipped.
    pub stale: usize,
    /// This edge's own changes coming back.
    pub echoes: usize,
    /// Updates held back because the edge has unacknowledged local changes,
    /// plus explicit `file.conflict` events.
    pub conflicts: usize,
    pub orphans_parked: usize,
    pub orphans_adopted: usize,
    pub malformed: usize,
    pub unknown: usize,
    /// Recalls whose state changed, for the FSAL push and the filler.
    pub recalls: Vec<RecallRow>,
    pub shares_changed: bool,
    pub stub_wanted: usize,
    /// An ACL's body changed, or a directory now has another ACL: rewind
    /// trees, made with the ACLs of their day, are rebuilt (decision 30).
    pub acls_changed: bool,
}

/// What the filesystem must do for one pending node.
#[derive(Clone, Debug, PartialEq)]
pub enum Action {
    /// Create the object at `path` (a placeholder for files: `O_TMPFILE` +
    /// marker + `linkat`; adopt an existing object that already is this one).
    Create { path: PathBuf },
    /// Bring an existing object up to date: rename it from `rename_from`
    /// first if set, then set attributes; if `content_changed`, the bytes
    /// belong to an older content version and must be narrowed away by the
    /// stub handshake before the new version is recorded.
    Update {
        path: PathBuf,
        rename_from: Option<PathBuf>,
        content_changed: bool,
    },
    /// Unlink or `rmdir` the object at `path`.
    Remove { path: PathBuf },
}

/// A unit of materialisation work. Paths are relative to the data root.
#[derive(Clone, Debug, PartialEq)]
pub struct PendingOp {
    pub node: Node,
    /// The share holding this node (for the marker's `share_id`), if known.
    pub share_id: Option<u64>,
    /// The node is a share's root: its owning group is the share's Owners
    /// group, which the edge sets (`omnifile_edge::shares`) and the catalog
    /// doesn't carry.
    pub share_root: bool,
    pub action: Action,
}

/// Result of a filesystem operation for [`Replica::record`].
#[derive(Clone, Debug, PartialEq)]
pub enum Outcome {
    /// The object now exists as desired. `ctime_ns`, when set, is the
    /// inode's ctime after the daemon's own change (see `Node::fs_ctime_ns`).
    Done {
        ino: u64,
        ino_gen: u32,
        fs_version_id: Option<u64>,
        ctime_ns: Option<i64>,
    },
    /// The object is gone.
    Removed,
    /// Try again on the next pass (for example `ENOTEMPTY`, an unacknowledged
    /// invalidation, or a transient error).
    Retry(String),
    /// Don't retry: something the edge must not overwrite is in the way.
    Conflict(String),
}

/// A desired stub (feed `stub`), ready for the stub handshake.
#[derive(Clone, Debug, PartialEq)]
pub struct StubWant {
    pub node: Node,
    pub path: PathBuf,
    pub version_id: u64,
    pub resident_bytes: u64,
}

/// A name the edge may create: not empty, not `.`/`..`, no `/` or NUL.
pub fn valid_name(name: &str) -> bool {
    !name.is_empty() && name != "." && name != ".." && !name.contains('/') && !name.contains('\0')
}

/// Join only single, valid components (never escapes the data root).
fn join_name(base: &Path, name: &str) -> Option<PathBuf> {
    if !valid_name(name) {
        return None;
    }
    let p = base.join(name);
    debug_assert!(p.components().all(|c| matches!(c, Component::Normal(_))));
    Some(p)
}

pub struct Replica {
    conn: Connection,
    edge_id: u64,
}

impl Replica {
    /// Open (or create) the replica at `path` in WAL mode.
    pub fn open(path: &Path) -> Result<Replica> {
        let conn = Connection::open(path)?;
        Self::init(conn)
    }

    pub fn open_in_memory() -> Result<Replica> {
        Self::init(Connection::open_in_memory()?)
    }

    fn init(conn: Connection) -> Result<Replica> {
        conn.pragma_update(None, "journal_mode", "WAL")?;
        // NORMAL in WAL: a power cut may lose the last transactions, never
        // corrupt; the cursor and rows are lost together and simply replayed.
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        conn.pragma_update(None, "foreign_keys", "OFF")?;
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        let v: i64 = conn.pragma_query_value(None, "user_version", |r| r.get(0))?;
        if v > schema::SCHEMA_VERSION {
            return Err(Error::SchemaTooNew {
                found: v,
                supported: schema::SCHEMA_VERSION,
            });
        }
        conn.execute_batch(schema::DDL)?;
        conn.pragma_update(None, "user_version", schema::SCHEMA_VERSION)?;
        let mut r = Replica { conn, edge_id: 0 };
        r.edge_id = r
            .meta_get("edge_id")?
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        Ok(r)
    }

    pub fn conn(&self) -> &Connection {
        &self.conn
    }

    pub fn edge_id(&self) -> u64 {
        self.edge_id
    }

    /// Record this edge's id (from enrolment), for echo suppression.
    pub fn set_edge_id(&mut self, edge_id: u64) -> Result<()> {
        self.meta_set("edge_id", &edge_id.to_string())?;
        self.edge_id = edge_id;
        Ok(())
    }

    pub fn meta_get(&self, key: &str) -> Result<Option<String>> {
        Ok(self
            .conn
            .query_row("SELECT value FROM meta WHERE key = ?1", [key], |r| r.get(0))
            .optional()?)
    }

    pub fn meta_set(&self, key: &str, value: &str) -> Result<()> {
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?1, ?2) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![key, value],
        )?;
        Ok(())
    }

    // ------------------------------------------------------------ feed

    /// The stored cursor (`"0.0"` on a new replica).
    pub fn cursor(&self) -> Result<String> {
        Ok(self
            .conn
            .query_row("SELECT cursor FROM feed WHERE id = 1", [], |r| r.get(0))?)
    }

    pub fn set_acked_cursor(&self, cursor: &str) -> Result<()> {
        self.conn
            .execute("UPDATE feed SET acked_cursor = ?1 WHERE id = 1", [cursor])?;
        Ok(())
    }

    /// Apply one feed page and store `next`, in one transaction.
    pub fn apply_page(&mut self, events: &[Value], next: &str) -> Result<ApplyReport> {
        let edge_id = self.edge_id;
        let tx = self.conn.transaction()?;
        let mut rep = ApplyReport::default();
        for v in events {
            match parse_event(v) {
                Ok(ev) => apply_event(&tx, edge_id, &ev, &mut rep)?,
                Err(e) => {
                    warn!(err = %e, "skipping malformed feed event");
                    rep.malformed += 1;
                }
            }
        }
        tx.execute("UPDATE feed SET cursor = ?1 WHERE id = 1", [next])?;
        tx.commit()?;
        Ok(rep)
    }

    /// Apply snapshot rows (the `file.upsert` row shape), without a cursor.
    pub fn apply_snapshot_rows(&mut self, rows: &[Value]) -> Result<ApplyReport> {
        let tx = self.conn.transaction()?;
        let mut rep = ApplyReport::default();
        for r in rows {
            match feed::parse_file_row(r) {
                Ok(row) => {
                    let ev = Event {
                        entity_id: row.file_id,
                        entity_version: row.entity_version,
                        origin: None,
                        body: if row.deleted {
                            Body::FileTombstone(row)
                        } else {
                            Body::FileUpsert(row)
                        },
                    };
                    apply_event(&tx, 0, &ev, &mut rep)?;
                }
                Err(_) => rep.malformed += 1,
            }
        }
        tx.commit()?;
        Ok(rep)
    }

    // ------------------------------------------------- re-snapshot

    /// Begin a re-snapshot after `reset_required` (a restored catalog,
    /// `edge-metadata.md` §5.2): file ids the snapshot names are noted, so
    /// [`Replica::resnapshot_lost`] can find what it lacks.
    pub fn resnapshot_begin(&mut self) -> Result<()> {
        self.conn.execute_batch(
            "CREATE TEMP TABLE IF NOT EXISTS resnap_seen (file_id INTEGER PRIMARY KEY);
             CREATE TEMP TABLE IF NOT EXISTS resnap_newer (lid INTEGER PRIMARY KEY, row TEXT);
             DELETE FROM resnap_seen; DELETE FROM resnap_newer;",
        )?;
        Ok(())
    }

    /// Apply a snapshot's dataset, share and ACL rows **whatever their
    /// `entity_version`**: the restore wound the versions back, and the
    /// feed's later events start from those.
    pub fn resnapshot_meta(
        &mut self,
        dataset: Option<&Value>,
        shares: &[Value],
        acls: &[Value],
    ) -> Result<ApplyReport> {
        let tx = self.conn.transaction()?;
        let mut rep = ApplyReport::default();
        let mut events: Vec<Value> = Vec::new();
        let env = |ty: &str, key: &str, id: &str, row: &Value| {
            serde_json::json!({"type": ty, "entity_id": row[id].clone(),
                               "entity_version": row["entity_version"].clone(), key: row})
        };
        if let Some(d) = dataset {
            tx.execute(
                "UPDATE dataset SET entity_version = 0 WHERE dataset_id = ?1",
                [d["dataset_id"]
                    .as_str()
                    .unwrap_or("0")
                    .parse::<i64>()
                    .unwrap_or(0)],
            )?;
            events.push(env("dataset.upsert", "dataset", "dataset_id", d));
        }
        for sh in shares {
            tx.execute(
                "UPDATE share SET entity_version = 0 WHERE share_id = ?1",
                [sh["share_id"]
                    .as_str()
                    .unwrap_or("0")
                    .parse::<i64>()
                    .unwrap_or(0)],
            )?;
            events.push(env("share.upsert", "share", "share_id", sh));
        }
        for a in acls {
            tx.execute(
                "UPDATE acl SET entity_version = 0 WHERE acl_id = ?1",
                [a["acl_id"]
                    .as_str()
                    .unwrap_or("0")
                    .parse::<i64>()
                    .unwrap_or(0)],
            )?;
            events.push(env("acl.upsert", "acl", "acl_id", a));
        }
        for v in &events {
            match parse_event(v) {
                Ok(ev) => apply_event(&tx, 0, &ev, &mut rep)?,
                Err(_) => rep.malformed += 1,
            }
        }
        tx.commit()?;
        Ok(rep)
    }

    /// Apply a page of snapshot file rows whatever their `entity_version`,
    /// noting each id. A node with local unsent changes keeps them (the
    /// conflict hold, as for a feed update).
    pub fn resnapshot_files(&mut self, rows: &[Value]) -> Result<ApplyReport> {
        let tx = self.conn.transaction()?;
        let mut rep = ApplyReport::default();
        for r in rows {
            let row = match feed::parse_file_row(r) {
                Ok(row) => row,
                Err(_) => {
                    rep.malformed += 1;
                    continue;
                }
            };
            tx.execute(
                "INSERT OR IGNORE INTO resnap_seen (file_id) VALUES (?1)",
                [row.file_id as i64],
            )?;
            // The edge holds a newer content version than the restored
            // catalog: one committed after the restore point (version ids
            // only grow, and jump at a restore). Held for the caller, who
            // commits local bytes again or, for a stub, applies the row.
            let newer: Option<i64> = tx
                .query_row(
                    "SELECT lid FROM node WHERE file_id = ?1 AND kind = 'file' AND deleted = 0
                        AND content_version_id IS NOT NULL AND content_version_id > ?2",
                    params![row.file_id as i64, row.version_id.unwrap_or(0) as i64],
                    |r| r.get(0),
                )
                .optional()?;
            if let Some(lid) = newer {
                tx.execute(
                    "INSERT OR REPLACE INTO resnap_newer (lid, row) VALUES (?1, ?2)",
                    params![lid, r.to_string()],
                )?;
                continue;
            }
            tx.execute(
                "UPDATE node SET entity_version = 0 WHERE file_id = ?1",
                [row.file_id as i64],
            )?;
            let ev = Event {
                entity_id: row.file_id,
                entity_version: row.entity_version,
                origin: None,
                body: if row.deleted {
                    Body::FileTombstone(row)
                } else {
                    Body::FileUpsert(row)
                },
            };
            apply_event(&tx, 0, &ev, &mut rep)?;
        }
        tx.commit()?;
        Ok(rep)
    }

    /// The live entries of `dataset_id` the snapshot didn't name: created,
    /// or numbered, after the restore point, so the restored catalog never
    /// had them (its ids moved on past them, migration 0019). Their rows are
    /// removed from the replica, never anything on disk: the caller commits
    /// resident files again as new ones. Returned with their paths, as the
    /// tree has them now.
    pub fn resnapshot_lost(&mut self, dataset_id: u64) -> Result<Vec<(Node, Option<PathBuf>)>> {
        self.resnapshot_lost_inner(dataset_id)
    }

    /// Files whose replica version is newer than the snapshot's (see
    /// [`Replica::resnapshot_files`]), with their paths and the held row.
    pub fn resnapshot_newer(&mut self) -> Result<Vec<(Node, Option<PathBuf>, Value)>> {
        let held: Vec<(i64, String)> = {
            let mut st = self
                .conn
                .prepare("SELECT lid, row FROM resnap_newer ORDER BY lid")?;
            st.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
                .collect::<rusqlite::Result<_>>()?
        };
        let mut out = Vec::new();
        for (lid, row) in held {
            if let Some(n) = self.node(lid)? {
                let path = self.fs_path(lid)?;
                out.push((n, path, serde_json::from_str(&row).unwrap_or(Value::Null)));
            }
        }
        Ok(out)
    }

    /// Apply one held row whatever its `entity_version` (a stub of a lost
    /// version: it shows the restored catalog's version instead).
    pub fn resnapshot_apply_held(&mut self, row: &Value) -> Result<ApplyReport> {
        let tx = self.conn.transaction()?;
        let mut rep = ApplyReport::default();
        if let Ok(row) = feed::parse_file_row(row) {
            tx.execute(
                "UPDATE node SET entity_version = 0 WHERE file_id = ?1",
                [row.file_id as i64],
            )?;
            let ev = Event {
                entity_id: row.file_id,
                entity_version: row.entity_version,
                origin: None,
                body: Body::FileUpsert(row),
            };
            apply_event(&tx, 0, &ev, &mut rep)?;
        } else {
            rep.malformed += 1;
        }
        tx.commit()?;
        Ok(rep)
    }

    fn resnapshot_lost_inner(&mut self, dataset_id: u64) -> Result<Vec<(Node, Option<PathBuf>)>> {
        let lost: Vec<Node> = {
            let mut st = self.conn.prepare(&format!(
                "SELECT {NODE_COLS} FROM node
                  WHERE dataset_id = ?1 AND deleted = 0 AND file_id IS NOT NULL
                    AND file_id NOT IN (SELECT file_id FROM resnap_seen)"
            ))?;
            st.query_map([dataset_id as i64], node_from_row)?
                .collect::<rusqlite::Result<_>>()?
        };
        let mut out = Vec::with_capacity(lost.len());
        for n in lost {
            let path = self.fs_path(n.lid)?;
            out.push((n, path));
        }
        let tx = self.conn.transaction()?;
        for (n, _) in &out {
            tx.execute("DELETE FROM node WHERE lid = ?1", [n.lid])?;
        }
        tx.commit()?;
        Ok(out)
    }

    /// End a re-snapshot: the feed resumes from the snapshot's `as_of`.
    pub fn resnapshot_finish(&mut self, as_of: &str) -> Result<()> {
        self.conn
            .execute("UPDATE feed SET cursor = ?1 WHERE id = 1", [as_of])?;
        self.conn.execute("DELETE FROM resnap_seen", [])?;
        self.conn.execute("DELETE FROM resnap_newer", [])?;
        Ok(())
    }

    // ------------------------------------------------------------ nodes

    pub fn node(&self, lid: i64) -> Result<Option<Node>> {
        Ok(self
            .conn
            .query_row(
                &format!("SELECT {NODE_COLS} FROM node WHERE lid = ?1"),
                [lid],
                node_from_row,
            )
            .optional()?)
    }

    pub fn node_by_file_id(&self, file_id: u64) -> Result<Option<Node>> {
        Ok(self
            .conn
            .query_row(
                &format!("SELECT {NODE_COLS} FROM node WHERE file_id = ?1"),
                [file_id as i64],
                node_from_row,
            )
            .optional()?)
    }

    /// A live, materialised node by inode.
    pub fn node_by_ino(&self, ino: u64, ino_gen: u32) -> Result<Option<Node>> {
        Ok(self
            .conn
            .query_row(
                &format!(
                    "SELECT {NODE_COLS} FROM node WHERE ino = ?1 AND ino_gen = ?2 AND deleted = 0"
                ),
                params![ino as i64, ino_gen as i64],
                node_from_row,
            )
            .optional()?)
    }

    /// Where the object is on disk now (relative to the data root), from the
    /// actual state; `None` if it, or an ancestor, isn't materialised.
    pub fn fs_path(&self, lid: i64) -> Result<Option<PathBuf>> {
        let mut names = Vec::new();
        let mut cur = Some(lid);
        for _ in 0..4096 {
            let Some(l) = cur else { break };
            let row: Option<(Option<i64>, Option<String>, Option<i64>)> = self
                .conn
                .query_row(
                    "SELECT fs_parent_lid, fs_name, ino FROM node WHERE lid = ?1",
                    [l],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .optional()?;
            let Some((parent, name, ino)) = row else {
                return Ok(None);
            };
            let (Some(name), Some(_)) = (name, ino) else {
                return Ok(None);
            };
            names.push(name);
            cur = parent;
            if cur.is_none() {
                let mut p = PathBuf::new();
                for n in names.iter().rev() {
                    match join_name(&p, n) {
                        Some(q) => p = q,
                        None => return Ok(None),
                    }
                }
                return Ok(Some(p));
            }
        }
        Ok(None)
    }

    /// The name a root directory has on disk: the share's name.
    fn root_name(&self, file_id: Option<u64>) -> Result<Option<String>> {
        let Some(fid) = file_id else { return Ok(None) };
        Ok(self
            .conn
            .query_row(
                "SELECT name FROM share WHERE root_dir_id = ?1 ORDER BY share_id LIMIT 1",
                [fid as i64],
                |r| r.get(0),
            )
            .optional()?)
    }

    /// The share whose root is above this node.
    ///
    /// The nearest one: a file under a shared folder belongs to that folder's
    /// share (its open policy, its marker), not to the dataset root's.
    pub fn share_of(&self, lid: i64) -> Result<Option<ShareInfo>> {
        let mut cur = Some(lid);
        for _ in 0..4096 {
            let Some(l) = cur else { return Ok(None) };
            let row: Option<(Option<i64>, Option<i64>, Option<i64>)> = self
                .conn
                .query_row(
                    "SELECT n.parent_lid, n.file_id, \
                     (SELECT share_id FROM share s WHERE s.root_dir_id = n.file_id \
                       ORDER BY share_id LIMIT 1) \
                     FROM node n WHERE n.lid = ?1",
                    [l],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .optional()?;
            let Some((parent, fid, shared)) = row else {
                return Ok(None);
            };
            if let (Some(fid), Some(_)) = (fid, shared) {
                return self.share_by_root(fid as u64);
            }
            if parent.is_none() {
                return Ok(None);
            }
            cur = parent;
        }
        Ok(None)
    }

    pub fn share_by_root(&self, root_dir_id: u64) -> Result<Option<ShareInfo>> {
        Ok(self
            .conn
            .query_row(
                "SELECT share_id, dataset_id, name, root_dir_id, on_open, wait_threshold_minutes, worm \
                 FROM share WHERE root_dir_id = ?1 ORDER BY share_id LIMIT 1",
                [root_dir_id as i64],
                share_info_from_row,
            )
            .optional()?)
    }

    pub fn share(&self, share_id: u64) -> Result<Option<ShareInfo>> {
        Ok(self
            .conn
            .query_row(
                "SELECT share_id, dataset_id, name, root_dir_id, on_open, wait_threshold_minutes, worm \
                 FROM share WHERE share_id = ?1",
                [share_id as i64],
                share_info_from_row,
            )
            .optional()?)
    }

    pub fn shares(&self) -> Result<Vec<ShareInfo>> {
        let mut st = self.conn.prepare(
            "SELECT share_id, dataset_id, name, root_dir_id, on_open, wait_threshold_minutes, worm FROM share ORDER BY share_id",
        )?;
        let rows = st
            .query_map([], share_info_from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// Every share's `share.upsert` row as the service sent it, with its
    /// version, by share id (the config renderer, `omnifile-edge::shares`).
    pub fn share_rows(&self) -> Result<Vec<(u64, u64, String)>> {
        let mut st = self
            .conn
            .prepare("SELECT share_id, entity_version, body FROM share ORDER BY share_id")?;
        let rows = st
            .query_map([], |r| {
                Ok((
                    r.get::<_, i64>(0)? as u64,
                    r.get::<_, i64>(1)? as u64,
                    r.get(2)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// Where a catalog directory is on disk, relative to the data root, once
    /// it is materialised.
    pub fn dir_path(&self, file_id: u64) -> Result<Option<PathBuf>> {
        match self.node_by_file_id(file_id)? {
            Some(n) => self.fs_path(n.lid),
            None => Ok(None),
        }
    }

    /// Ready pending work, parents before children for creates and updates,
    /// children before parents for removals. Nodes whose parent isn't on disk
    /// yet (orphans included) are not returned.
    pub fn pending(&self, limit: usize) -> Result<Vec<PendingOp>> {
        let mut st = self.conn.prepare(&format!(
            "SELECT {NODE_COLS} FROM node WHERE fs_pending = 1 AND fs_conflict IS NULL ORDER BY lid LIMIT ?1"
        ))?;
        let nodes = st
            .query_map([limit as i64 * 4], node_from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let mut ops = Vec::new();
        for n in nodes {
            if let Some(op) = self.pending_op(n)? {
                ops.push(op);
            }
        }
        let depth = |op: &PendingOp| -> usize {
            match &op.action {
                Action::Create { path } | Action::Update { path, .. } | Action::Remove { path } => {
                    path.components().count()
                }
            }
        };
        ops.sort_by_key(|op| match op.action {
            Action::Remove { .. } => (1, usize::MAX - depth(op)),
            _ => (0, depth(op)),
        });
        ops.truncate(limit);
        Ok(ops)
    }

    fn pending_op(&self, n: Node) -> Result<Option<PendingOp>> {
        let current = if n.ino.is_some() {
            self.fs_path(n.lid)?
        } else {
            None
        };
        if n.deleted {
            return Ok(match current {
                Some(path) => Some(PendingOp {
                    share_id: None,
                    share_root: false,
                    action: Action::Remove { path },
                    node: n,
                }),
                None if n.ino.is_none() => {
                    // Nothing on disk: just clear the flag.
                    self.conn
                        .execute("UPDATE node SET fs_pending = 0 WHERE lid = ?1", [n.lid])?;
                    None
                }
                None => None,
            });
        }
        if n.waiting_parent {
            return Ok(None);
        }
        let desired = match n.parent_lid {
            None => match self.root_name(n.file_id)? {
                Some(name) => join_name(Path::new(""), &name),
                None => None,
            },
            Some(p) => match self.fs_path(p)? {
                Some(pp) => join_name(&pp, &n.name),
                None => None,
            },
        };
        let Some(desired) = desired else {
            return Ok(None);
        };
        let share_id = self.share_of(n.lid)?.map(|s| s.share_id);
        let action = match current {
            None if n.ino.is_none() => Action::Create { path: desired },
            None => return Ok(None),
            Some(cur) => Action::Update {
                rename_from: if cur != desired { Some(cur) } else { None },
                content_changed: n.kind == Kind::File
                    && n.fs_version_id.is_some()
                    && n.content_version_id.is_some()
                    && n.fs_version_id != n.content_version_id,
                path: desired,
            },
        };
        let share_root = match n.file_id {
            Some(f) => self
                .conn
                .query_row(
                    "SELECT 1 FROM share WHERE root_dir_id = ?1 LIMIT 1",
                    [f as i64],
                    |_| Ok(()),
                )
                .optional()?
                .is_some(),
            None => false,
        };
        Ok(Some(PendingOp {
            node: n,
            share_id,
            share_root,
            action,
        }))
    }

    /// Record the result of a pending op. If the row changed after the op
    /// was computed (a newer feed event), the filesystem state is still
    /// recorded but the node stays pending for another pass.
    pub fn record(&mut self, op: &PendingOp, outcome: Outcome) -> Result<()> {
        let n = &op.node;
        let ev = n.entity_version as i64;
        match outcome {
            Outcome::Done {
                ino,
                ino_gen,
                fs_version_id,
                ctime_ns,
            } => {
                let fs_name = match n.parent_lid {
                    None => self.root_name(n.file_id)?.unwrap_or_else(|| n.name.clone()),
                    Some(_) => n.name.clone(),
                };
                self.conn.execute(
                    "UPDATE node SET ino = ?2, ino_gen = ?3, fs_parent_lid = ?4, fs_name = ?5, \
                     fs_version_id = COALESCE(?6, fs_version_id), fs_ctime_ns = COALESCE(?10, fs_ctime_ns), \
                     fs_error = NULL, fs_attempts = 0, \
                     fs_pending = CASE WHEN entity_version = ?7 AND deleted = ?8 AND \
                         parent_lid IS ?4 AND name = ?9 THEN 0 ELSE 1 END \
                     WHERE lid = ?1",
                    params![
                        n.lid,
                        ino as i64,
                        ino_gen as i64,
                        n.parent_lid,
                        fs_name,
                        fs_version_id.map(|v| v as i64),
                        ev,
                        n.deleted,
                        n.name,
                        ctime_ns
                    ],
                )?;
            }
            Outcome::Removed => {
                self.conn.execute(
                    "UPDATE node SET ino = NULL, ino_gen = NULL, fs_parent_lid = NULL, fs_name = NULL, \
                     fs_version_id = NULL, fs_error = NULL, fs_attempts = 0, \
                     fs_pending = CASE WHEN deleted = 1 THEN 0 ELSE 1 END WHERE lid = ?1",
                    [n.lid],
                )?;
            }
            Outcome::Retry(why) => {
                debug!(lid = n.lid, file_id = ?n.file_id, why, "materialisation will be retried");
                self.conn.execute(
                    "UPDATE node SET fs_error = ?2, fs_attempts = fs_attempts + 1 WHERE lid = ?1",
                    params![n.lid, why],
                )?;
            }
            Outcome::Conflict(why) => {
                warn!(lid = n.lid, file_id = ?n.file_id, why, "materialisation conflict; leaving local data alone");
                self.conn.execute(
                    "UPDATE node SET fs_conflict = ?2, fs_pending = 0 WHERE lid = ?1",
                    params![n.lid, why],
                )?;
                self.conn.execute(
                    "INSERT INTO conflict (at, file_id, lid, reason) VALUES (?1, ?2, ?3, ?4)",
                    params![now_s(), n.file_id.map(|v| v as i64), n.lid, why],
                )?;
            }
        }
        Ok(())
    }

    /// Orphans parked longer than `secs` (reported: the reconciliation
    /// digest then finds the gap).
    pub fn stale_orphans(&self, secs: i64) -> Result<Vec<Node>> {
        let mut st = self.conn.prepare(&format!(
            "SELECT {NODE_COLS} FROM node WHERE waiting_parent = 1 AND deleted = 0 AND parked_at < ?1"
        ))?;
        let rows = st
            .query_map([now_s() - secs], node_from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// Outbox ops not yet acknowledged by the service.
    pub fn outbox_depth(&self) -> Result<u64> {
        Ok(self.conn.query_row(
            "SELECT COUNT(*) FROM outbox WHERE state IN ('queued', 'sent')",
            [],
            |r| r.get::<_, i64>(0),
        )? as u64)
    }

    pub fn pending_count(&self) -> Result<u64> {
        Ok(self
            .conn
            .query_row("SELECT COUNT(*) FROM node WHERE fs_pending = 1", [], |r| {
                r.get::<_, i64>(0)
            })? as u64)
    }

    // ------------------------------------------------------------ edge-originated

    /// Record a file or directory created on the edge (no `file_id` yet).
    pub fn register_local(
        &mut self,
        parent_lid: Option<i64>,
        name: &str,
        kind: Kind,
        ino: u64,
        ino_gen: u32,
        attrs: LocalAttrs,
    ) -> Result<i64> {
        self.conn.execute(
            "INSERT INTO node (parent_lid, name, kind, size, mtime_ns, uid, gid, mode, ino, ino_gen, \
             fs_parent_lid, fs_name, local_dirty, dataset_id, parent_file_id) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?1, ?2, 1, \
                     COALESCE((SELECT dataset_id FROM node WHERE lid = ?1), 0), \
                     (SELECT file_id FROM node WHERE lid = ?1))",
            params![
                parent_lid,
                name,
                kind.as_str(),
                attrs.size as i64,
                attrs.mtime_ns,
                attrs.uid as i64,
                attrs.gid as i64,
                attrs.mode as i64,
                ino as i64,
                ino_gen as i64
            ],
        )?;
        Ok(self.conn.last_insert_rowid())
    }

    /// The service numbered a local node (`changes` response).
    pub fn assign_file_id(&mut self, lid: i64, file_id: u64, entity_version: u64) -> Result<()> {
        self.conn.execute(
            "UPDATE node SET file_id = ?2, entity_version = MAX(entity_version, ?3) WHERE lid = ?1",
            params![lid, file_id as i64, entity_version as i64],
        )?;
        Ok(())
    }

    /// A version was committed and the marker written: the local bytes are
    /// this content version.
    pub fn set_committed_version(
        &mut self,
        lid: i64,
        version_id: u64,
        size: u64,
        crc64nvme: Option<u64>,
    ) -> Result<()> {
        self.conn.execute(
            "UPDATE node SET content_version_id = ?2, fs_version_id = ?2, size = ?3, crc64nvme = ?4 WHERE lid = ?1",
            params![lid, version_id as i64, size as i64, crc64nvme.map(|v| v as i64)],
        )?;
        Ok(())
    }

    /// Record the inode's ctime after a daemon write (commit marker, fill).
    pub fn set_fs_ctime(&mut self, lid: i64, ctime_ns: i64) -> Result<()> {
        self.conn.execute(
            "UPDATE node SET fs_ctime_ns = ?2 WHERE lid = ?1",
            params![lid, ctime_ns],
        )?;
        Ok(())
    }

    /// Forget the recorded ctime: the local bytes are no longer known to be
    /// the committed version, so the stubber leaves the file alone until a
    /// commit records a new one.
    pub fn clear_fs_ctime(&mut self, lid: i64) -> Result<()> {
        self.conn.execute(
            "UPDATE node SET fs_ctime_ns = NULL WHERE lid = ?1",
            params![lid],
        )?;
        Ok(())
    }

    /// The edge has (or no longer has) changes the service hasn't acknowledged.
    pub fn set_local_dirty(&mut self, lid: i64, dirty: bool) -> Result<()> {
        self.conn.execute(
            "UPDATE node SET local_dirty = ?2 WHERE lid = ?1",
            params![lid, dirty],
        )?;
        Ok(())
    }

    // ------------------------------------------------------------ local namespace changes
    //
    // A user's rename, delete or chown on the edge (docs/design/namespace-sync.md):
    // the replica follows the disk at once, and the op is queued in the
    // outbox, naming nodes by lid, in the same transaction.

    /// Live children the replica has on disk in a directory.
    pub fn fs_children(&self, dir_lid: i64) -> Result<Vec<Node>> {
        let mut st = self.conn.prepare(&format!(
            "SELECT {NODE_COLS} FROM node WHERE fs_parent_lid = ?1 AND deleted = 0 AND ino IS NOT NULL"
        ))?;
        let rows = st
            .query_map([dir_lid], node_from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// A node and every live node below it, deepest first.
    pub fn subtree_postorder(&self, lid: i64) -> Result<Vec<Node>> {
        let mut st = self.conn.prepare(&format!(
            "WITH RECURSIVE t(lid, depth) AS ( \
               SELECT ?1, 0 \
               UNION ALL SELECT n.lid, t.depth + 1 FROM node n JOIN t ON n.parent_lid = t.lid \
                 WHERE n.deleted = 0 AND t.depth < 4096) \
             SELECT {} FROM node JOIN t USING (lid) ORDER BY t.depth DESC, node.lid",
            NODE_COLS
                .split(", ")
                .map(|c| format!("node.{}", c.trim()))
                .collect::<Vec<_>>()
                .join(", ")
        ))?;
        let rows = st
            .query_map([lid], node_from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// Renamed or moved on the edge: the catalog name follows the disk (so
    /// the materialiser leaves it), and a `rename` is queued, with who made
    /// it (`actor`, control-api.md §4.1) when known.
    pub fn local_moved(
        &mut self,
        lid: i64,
        new_parent_lid: i64,
        new_name: &str,
        op_id: &str,
        actor: Option<&Value>,
    ) -> Result<()> {
        let tx = self.conn.transaction()?;
        // The name it frees, so a create that wants it can wait for this.
        let (old_parent, old_name): (Option<i64>, String) = tx.query_row(
            "SELECT parent_lid, name FROM node WHERE lid = ?1",
            [lid],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        tx.execute(
            "UPDATE node SET parent_lid = ?2, parent_file_id = (SELECT file_id FROM node WHERE lid = ?2), \
             waiting_parent = 0, name = ?3, fs_parent_lid = ?2, fs_name = ?3, fs_pending = 0, fs_conflict = NULL \
             WHERE lid = ?1",
            params![lid, new_parent_lid, new_name],
        )?;
        let mut payload = serde_json::json!({"new_parent_lid": new_parent_lid, "new_name": new_name,
                                             "old_parent_lid": old_parent, "old_name": old_name});
        if let Some(a) = actor {
            payload["actor"] = a.clone();
        }
        tx.execute(
            "INSERT OR IGNORE INTO outbox (op_id, lid, op, payload, created_at) VALUES (?1, ?2, 'rename', ?3, ?4)",
            params![op_id, lid, payload.to_string(), now_s()],
        )?;
        tx.commit()?;
        Ok(())
    }

    /// The live node the replica names `name` in a directory, if any.
    pub fn live_child_named(&self, parent_lid: i64, name: &str) -> Result<Option<i64>> {
        Ok(self
            .conn
            .query_row(
                "SELECT lid FROM node WHERE parent_lid = ?1 AND name = ?2 AND deleted = 0 LIMIT 1",
                params![parent_lid, name],
                |r| r.get(0),
            )
            .optional()?)
    }

    /// A `rename` for the service only: the node stays where the replica
    /// has it. It moves a node out of the way of another's rename when
    /// names were swapped (namespace-sync.md); the node's own pass sends
    /// its final rename after it.
    pub fn queue_rename(
        &mut self,
        lid: i64,
        new_parent_lid: i64,
        new_name: &str,
        op_id: &str,
        actor: Option<&Value>,
    ) -> Result<()> {
        let mut payload =
            serde_json::json!({"new_parent_lid": new_parent_lid, "new_name": new_name});
        if let Some(a) = actor {
            payload["actor"] = a.clone();
        }
        self.conn.execute(
            "INSERT OR IGNORE INTO outbox (op_id, lid, op, payload, created_at) VALUES (?1, ?2, 'rename', ?3, ?4)",
            params![op_id, lid, payload.to_string(), now_s()],
        )?;
        Ok(())
    }

    /// Deleted on the edge: each node (deepest first, as
    /// [`Replica::subtree_postorder`] gives them) is marked deleted, so
    /// nothing puts it back, and an `unlink` is queued for each, with who
    /// made it (`actor`) when known.
    pub fn local_deleted(&mut self, nodes: &[(i64, String, Option<Value>)]) -> Result<()> {
        let tx = self.conn.transaction()?;
        for (lid, op_id, actor) in nodes {
            let payload = match actor {
                Some(a) => serde_json::json!({"actor": a}),
                None => serde_json::json!({}),
            };
            tx.execute(
                "UPDATE node SET deleted = 1, ino = NULL, ino_gen = NULL, fs_parent_lid = NULL, fs_name = NULL, \
                 fs_pending = 0, fs_conflict = NULL, stub_status = 'none' WHERE lid = ?1",
                [lid],
            )?;
            tx.execute(
                "INSERT OR IGNORE INTO outbox (op_id, lid, op, payload, created_at) VALUES (?1, ?2, 'unlink', ?3, ?4)",
                params![op_id, lid, payload.to_string(), now_s()],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    /// `chown` on the edge: the new owner and group, and an `owner` op.
    pub fn local_owner(
        &mut self,
        lid: i64,
        uid: u32,
        gid: u32,
        op_id: &str,
        actor: Option<&Value>,
    ) -> Result<()> {
        let tx = self.conn.transaction()?;
        tx.execute(
            "UPDATE node SET uid = ?2, gid = ?3 WHERE lid = ?1",
            params![lid, uid as i64, gid as i64],
        )?;
        let mut payload = serde_json::json!({"uid": uid, "gid": gid});
        if let Some(a) = actor {
            payload["actor"] = a.clone();
        }
        tx.execute(
            "INSERT OR IGNORE INTO outbox (op_id, lid, op, payload, created_at) VALUES (?1, ?2, 'owner', ?3, ?4)",
            params![op_id, lid, payload.to_string(), now_s()],
        )?;
        tx.commit()?;
        Ok(())
    }

    /// Namespace ops not yet acknowledged, oldest first, with the creates
    /// whose answer was lost (sent more than `create_after_s` ago and never
    /// acknowledged), which they may depend on.
    pub fn ns_unacked(&self, limit: usize, create_after_s: i64) -> Result<Vec<OutboxOp>> {
        let mut st = self.conn.prepare(
            "SELECT op_id, lid, op, payload, state, attempts FROM outbox \
             WHERE state IN ('queued', 'sent') \
               AND (op IN ('rename', 'unlink', 'owner') OR (op = 'create' AND created_at <= ?2)) \
             ORDER BY seq LIMIT ?1",
        )?;
        let rows = st
            .query_map(params![limit as i64, now_s() - create_after_s], |r| {
                let payload: String = r.get(3)?;
                Ok(OutboxOp {
                    op_id: r.get(0)?,
                    lid: r.get(1)?,
                    op: r.get(2)?,
                    payload: serde_json::from_str(&payload).unwrap_or(Value::Null),
                    state: r.get(4)?,
                    attempts: r.get::<_, i64>(5)? as u32,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    /// A create for this node is queued or sent and not yet answered.
    pub fn create_pending(&self, lid: i64) -> Result<bool> {
        Ok(self.conn.query_row(
            "SELECT EXISTS (SELECT 1 FROM outbox WHERE lid = ?1 AND op = 'create' AND state IN ('queued', 'sent'))",
            [lid],
            |r| r.get(0),
        )?)
    }

    /// The materialiser has work for this directory or something in it: a
    /// reconcile now could take its half-done work for the user's.
    pub fn dir_busy(&self, lid: i64) -> Result<bool> {
        Ok(self.conn.query_row(
            "SELECT EXISTS (SELECT 1 FROM node WHERE fs_pending = 1 \
               AND (lid = ?1 OR parent_lid = ?1 OR fs_parent_lid = ?1))",
            [lid],
            |r| r.get(0),
        )?)
    }

    /// Whether a catalog folder is a share's root (its group is the edge's).
    pub fn is_share_root(&self, file_id: u64) -> Result<bool> {
        Ok(self.conn.query_row(
            "SELECT EXISTS (SELECT 1 FROM share WHERE root_dir_id = ?1)",
            [file_id as i64],
            |r| r.get(0),
        )?)
    }

    /// Live `ONLINE` files on disk with a lid above `after`, in lid order:
    /// the placeholders the edge's chunk cache may hold bytes of (its
    /// startup index).
    pub fn online_files_on_disk_after(&self, after: i64, limit: usize) -> Result<Vec<Node>> {
        let mut st = self.conn.prepare(&format!(
            "SELECT {NODE_COLS} FROM node WHERE lid > ?1 AND kind = 'file' AND deleted = 0 \
             AND ino IS NOT NULL AND online = 1 ORDER BY lid LIMIT ?2"
        ))?;
        let rows = st
            .query_map(params![after, limit as i64], node_from_row)?
            .collect::<rusqlite::Result<Vec<Node>>>()?;
        Ok(rows)
    }

    /// Live directories on disk with a lid above `after`, in lid order (the
    /// namespace sweep's cursor).
    pub fn dirs_on_disk_after(&self, after: i64, limit: usize) -> Result<Vec<i64>> {
        let mut st = self.conn.prepare(
            "SELECT lid FROM node WHERE lid > ?1 AND kind = 'dir' AND deleted = 0 AND ino IS NOT NULL \
             ORDER BY lid LIMIT ?2",
        )?;
        let rows = st
            .query_map(params![after, limit as i64], |r| r.get(0))?
            .collect::<rusqlite::Result<Vec<i64>>>()?;
        Ok(rows)
    }

    /// The mode on disk, recorded once the ACL scan has been asked to report
    /// it, so the next pass doesn't ask again.
    pub fn local_mode(&mut self, lid: i64, mode: u32) -> Result<()> {
        self.conn.execute(
            "UPDATE node SET mode = ?2 WHERE lid = ?1",
            params![lid, mode as i64],
        )?;
        Ok(())
    }

    /// The service applied a namespace op: the node takes its version (the
    /// op's echo in the feed is then skipped as the edge's own).
    /// Returns whether a feed event for the node was held (dropped) while
    /// the op was in flight: the caller fetches the node's row again, since
    /// the op's echo is skipped and its entity version now covers what was
    /// dropped (M7 review S3).
    pub fn ns_applied(
        &mut self,
        op_id: &str,
        lid: i64,
        entity_version: Option<u64>,
    ) -> Result<bool> {
        let tx = self.conn.transaction()?;
        let held: bool = tx.query_row(
            "SELECT EXISTS (SELECT 1 FROM conflict c WHERE c.lid = ?2 \
               AND c.reason = 'local_unacknowledged_changes' \
               AND c.at >= (SELECT created_at - 1 FROM outbox WHERE op_id = ?1))",
            params![op_id, lid],
            |r| r.get(0),
        )?;
        tx.execute(
            "UPDATE outbox SET state = 'acked' WHERE op_id = ?1",
            [op_id],
        )?;
        // After a hold, the node's version stays where it was: the row
        // fetched again (or the next event, or the daily digest) then isn't
        // taken for stale.
        if let Some(ev) = entity_version
            && !held
        {
            tx.execute(
                "UPDATE node SET entity_version = MAX(entity_version, ?2) WHERE lid = ?1",
                params![lid, ev as i64],
            )?;
        }
        tx.commit()?;
        Ok(held)
    }

    /// A create the service can never be told: its node is gone from the
    /// disk, or its parent will never be in the catalog. It stops holding
    /// the queue (M7 review B2); what depends on it resolves without it.
    pub fn abandon_create(&mut self, op_id: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE outbox SET state = 'acked', result = '{\"abandoned\":true}' WHERE op_id = ?1",
            [op_id],
        )?;
        Ok(())
    }

    /// Record a conflict the edge can't resolve by itself (an alert reads
    /// the table).
    pub fn note_conflict(&mut self, lid: i64, reason: &str) -> Result<()> {
        self.conn.execute(
            "INSERT INTO conflict (at, file_id, lid, reason) \
             VALUES (?1, (SELECT file_id FROM node WHERE lid = ?2), ?2, ?3)",
            params![now_s(), lid, reason],
        )?;
        Ok(())
    }

    /// Whether another node's unacknowledged unlink or rename frees the name
    /// `name` in the folder `parent_lid`: a create that wants that name waits
    /// for it (`rm a; cp x a`).
    pub fn name_being_freed(&self, lid: i64, parent_lid: i64, name: &str) -> Result<bool> {
        Ok(self.conn.query_row(
            "SELECT EXISTS (SELECT 1 FROM outbox o JOIN node n ON n.lid = o.lid \
               WHERE o.state IN ('queued', 'sent') AND o.lid <> ?1 AND ( \
                 (o.op = 'unlink' AND n.parent_lid = ?2 AND n.name = ?3) \
                 OR (o.op = 'rename' AND json_extract(o.payload, '$.old_parent_lid') = ?2 \
                     AND json_extract(o.payload, '$.old_name') = ?3)))",
            params![lid, parent_lid, name],
            |r| r.get(0),
        )?)
    }

    /// The service refused a namespace op: the edge keeps the user's tree,
    /// and a conflict row says so (`edge-metadata.md` section 7).
    pub fn ns_refused(&mut self, op_id: &str, lid: i64, reason: &str) -> Result<()> {
        let tx = self.conn.transaction()?;
        tx.execute(
            "UPDATE outbox SET state = 'conflict', result = ?2 WHERE op_id = ?1",
            params![op_id, reason],
        )?;
        tx.execute(
            "INSERT INTO conflict (at, file_id, lid, op_id, reason) \
             VALUES (?1, (SELECT file_id FROM node WHERE lid = ?2), ?2, ?3, ?4)",
            params![now_s(), lid, op_id, reason],
        )?;
        tx.commit()?;
        Ok(())
    }

    // ------------------------------------------------------------ stubs

    /// Files the service wants stubbed whose local content is that version.
    ///
    /// Never a file with local changes the service hasn't acknowledged
    /// (`local_dirty`), nor one without a recorded ctime: without it the
    /// stubber cannot tell the bytes are still the committed version (a
    /// write landed after the commit's marker, or the file was never
    /// committed by this edge), so it does not try.
    pub fn stub_wanted(&self, limit: usize) -> Result<Vec<StubWant>> {
        let mut st = self.conn.prepare(&format!(
            "SELECT {NODE_COLS} FROM node WHERE stub_status = 'wanted' AND deleted = 0 AND ino IS NOT NULL \
             AND fs_pending = 0 AND local_dirty = 0 AND fs_ctime_ns IS NOT NULL ORDER BY lid LIMIT ?1"
        ))?;
        let nodes = st
            .query_map([limit as i64], node_from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let mut out = Vec::new();
        for n in nodes {
            let (Some(v), Some(r)) = (n.stub_version_id, n.stub_resident_bytes) else {
                continue;
            };
            if let Some(path) = self.fs_path(n.lid)? {
                out.push(StubWant {
                    node: n,
                    path,
                    version_id: v,
                    resident_bytes: r,
                });
            }
        }
        Ok(out)
    }

    pub fn set_stub_status(&mut self, lid: i64, status: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE node SET stub_status = ?2 WHERE lid = ?1",
            params![lid, status],
        )?;
        Ok(())
    }

    // ------------------------------------------------------------ recalls

    pub fn active_recalls(&self) -> Result<Vec<RecallRow>> {
        let mut st = self.conn.prepare(
            "SELECT recall_id, file_id, version_id, state, eta, error, read_credential, entity_version FROM recall \
             WHERE state NOT IN ('available', 'failed', 'cancelled') ORDER BY recall_id",
        )?;
        let rows = st
            .query_map([], recall_from_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    pub fn recall(&self, recall_id: u64) -> Result<Option<RecallRow>> {
        Ok(self
            .conn
            .query_row(
                "SELECT recall_id, file_id, version_id, state, eta, error, read_credential, entity_version \
                 FROM recall WHERE recall_id = ?1",
                [recall_id as i64],
                recall_from_row,
            )
            .optional()?)
    }

    /// The active recall for a `(file_id, content version)`.
    pub fn active_recall_for(&self, file_id: u64, version_id: u64) -> Result<Option<RecallRow>> {
        Ok(self
            .conn
            .query_row(
                "SELECT recall_id, file_id, version_id, state, eta, error, read_credential, entity_version \
                 FROM recall WHERE file_id = ?1 AND version_id = ?2 \
                 AND state NOT IN ('available', 'failed', 'cancelled')",
                params![file_id as i64, version_id as i64],
                recall_from_row,
            )
            .optional()?)
    }

    /// Record a recall the daemon learnt from `POST …/recalls` (or a local
    /// state change such as `available` after a fill). Guarded by
    /// `entity_version` like feed rows; local rows use the stored version.
    pub fn upsert_recall(&mut self, row: &RecallRow) -> Result<bool> {
        let tx = self.conn.transaction()?;
        let changed = upsert_recall_tx(&tx, row, true)?;
        tx.commit()?;
        Ok(changed)
    }

    // ------------------------------------------------------------ outbox

    /// The reconciliation digest of a dataset's live rows (`control-api.md`
    /// §6): how many, and the XOR of [`digest_row`] over them. Rows the
    /// catalog does not know yet (no `file_id`) are not counted.
    pub fn digest(&self, dataset_id: u64) -> Result<(u64, u64)> {
        let mut st = self.conn.prepare(
            "SELECT file_id, entity_version FROM node
              WHERE dataset_id = ?1 AND file_id IS NOT NULL AND deleted = 0",
        )?;
        let rows = st.query_map([dataset_id as i64], |r| {
            Ok((r.get::<_, i64>(0)? as u64, r.get::<_, i64>(1)? as u64))
        })?;
        let (mut n, mut x) = (0u64, 0u64);
        for row in rows {
            let (fid, ev) = row?;
            n += 1;
            x ^= digest_row(fid, ev);
        }
        Ok((n, x))
    }

    /// Audit records kept while the service is unreachable, at most.
    pub const AUDIT_MAX: i64 = 100_000;

    /// Keep audit records until [`Replica::audit_ack`]. Past
    /// [`Replica::AUDIT_MAX`] the oldest are dropped, and the drop is itself
    /// recorded (`audit.dropped`, dated `now`), so a gap in the trail is
    /// never silent.
    pub fn audit_push(&mut self, records: &[Value], now: &str) -> Result<()> {
        let tx = self.conn.transaction()?;
        for r in records {
            tx.execute("INSERT INTO audit (record) VALUES (?1)", [r.to_string()])?;
        }
        let n: i64 = tx.query_row("SELECT count(*) FROM audit", [], |r| r.get(0))?;
        if n > Self::AUDIT_MAX {
            let drop = n - Self::AUDIT_MAX + 1;
            tx.execute(
                "DELETE FROM audit WHERE seq IN (SELECT seq FROM audit ORDER BY seq LIMIT ?1)",
                [drop],
            )?;
            let note = serde_json::json!({
                "at": now,
                "action": "audit.dropped",
                "decision": "info",
                "details": {"dropped": drop},
            });
            tx.execute("INSERT INTO audit (record) VALUES (?1)", [note.to_string()])?;
        }
        tx.commit()?;
        Ok(())
    }

    /// The oldest kept audit records, with their sequence numbers.
    pub fn audit_peek(&self, limit: usize) -> Result<Vec<(i64, Value)>> {
        let mut st = self
            .conn
            .prepare("SELECT seq, record FROM audit ORDER BY seq LIMIT ?1")?;
        let rows = st.query_map([limit as i64], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))
        })?;
        let mut out = Vec::new();
        for row in rows {
            let (seq, raw) = row?;
            out.push((seq, serde_json::from_str(&raw).unwrap_or(Value::Null)));
        }
        Ok(out)
    }

    /// The service has every record up to `seq`.
    pub fn audit_ack(&mut self, seq: i64) -> Result<()> {
        self.conn
            .execute("DELETE FROM audit WHERE seq <= ?1", [seq])?;
        Ok(())
    }

    pub fn outbox_enqueue(
        &mut self,
        op_id: &str,
        lid: Option<i64>,
        op: &str,
        payload: &Value,
    ) -> Result<()> {
        self.conn.execute(
            "INSERT OR IGNORE INTO outbox (op_id, lid, op, payload, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![op_id, lid, op, payload.to_string(), now_s()],
        )?;
        Ok(())
    }

    /// Queued (or sent but unanswered) ops, oldest first.
    pub fn outbox_unacked(&self, limit: usize) -> Result<Vec<OutboxOp>> {
        let mut st = self.conn.prepare(
            "SELECT op_id, lid, op, payload, state, attempts FROM outbox WHERE state IN ('queued', 'sent') \
             ORDER BY seq LIMIT ?1",
        )?;
        let rows = st
            .query_map([limit as i64], |r| {
                let payload: String = r.get(3)?;
                Ok(OutboxOp {
                    op_id: r.get(0)?,
                    lid: r.get(1)?,
                    op: r.get(2)?,
                    payload: serde_json::from_str(&payload).unwrap_or(Value::Null),
                    state: r.get(4)?,
                    attempts: r.get::<_, i64>(5)? as u32,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    pub fn outbox_set_state(
        &mut self,
        op_id: &str,
        state: &str,
        result: Option<&Value>,
    ) -> Result<()> {
        self.conn.execute(
            "UPDATE outbox SET state = ?2, result = COALESCE(?3, result), \
             attempts = attempts + CASE WHEN ?2 = 'sent' THEN 1 ELSE 0 END WHERE op_id = ?1",
            params![op_id, state, result.map(|v| v.to_string())],
        )?;
        Ok(())
    }

    /// Next value of a persistent counter (op ids, marker_seq).
    pub fn next_counter(&mut self, name: &str) -> Result<u64> {
        let key = format!("counter.{name}");
        let tx = self.conn.transaction()?;
        let cur: Option<String> = tx
            .query_row("SELECT value FROM meta WHERE key = ?1", [&key], |r| {
                r.get(0)
            })
            .optional()?;
        let next = cur.and_then(|s| s.parse::<u64>().ok()).unwrap_or(0) + 1;
        tx.execute(
            "INSERT INTO meta (key, value) VALUES (?1, ?2) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![key, next.to_string()],
        )?;
        tx.commit()?;
        Ok(next)
    }

    /// Reserve `n` values of a counter at once; returns the first.
    pub fn reserve_counter(&mut self, name: &str, n: u64) -> Result<u64> {
        let key = format!("counter.{name}");
        let tx = self.conn.transaction()?;
        let cur: Option<String> = tx
            .query_row("SELECT value FROM meta WHERE key = ?1", [&key], |r| {
                r.get(0)
            })
            .optional()?;
        let first = cur.and_then(|s| s.parse::<u64>().ok()).unwrap_or(0) + 1;
        tx.execute(
            "INSERT INTO meta (key, value) VALUES (?1, ?2) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![key, (first + n - 1).to_string()],
        )?;
        tx.commit()?;
        Ok(first)
    }

    pub fn conflicts(&self) -> Result<Vec<ConflictRecord>> {
        let mut st = self
            .conn
            .prepare("SELECT file_id, op_id, reason FROM conflict ORDER BY id")?;
        let rows = st
            .query_map([], |r| {
                Ok(ConflictRecord {
                    file_id: u64_opt(r, 0)?,
                    op_id: r.get(1)?,
                    reason: r.get(2)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }
}

/// A recorded conflict (for alerts and the appliance status).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConflictRecord {
    pub file_id: Option<u64>,
    pub op_id: Option<String>,
    pub reason: String,
}

/// Attributes of an edge-created object.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct LocalAttrs {
    pub size: u64,
    pub mtime_ns: i64,
    pub uid: u32,
    pub gid: u32,
    pub mode: u32,
}

#[derive(Clone, Debug, PartialEq)]
pub struct OutboxOp {
    pub op_id: String,
    pub lid: Option<i64>,
    pub op: String,
    pub payload: Value,
    pub state: String,
    pub attempts: u32,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ShareInfo {
    pub share_id: u64,
    pub dataset_id: u64,
    pub name: String,
    pub root_dir_id: Option<u64>,
    pub on_open: String,
    pub wait_threshold_minutes: Option<u64>,
    pub worm: bool,
}

fn share_info_from_row(r: &Row) -> rusqlite::Result<ShareInfo> {
    Ok(ShareInfo {
        share_id: r.get::<_, i64>(0)? as u64,
        dataset_id: r.get::<_, i64>(1)? as u64,
        name: r.get(2)?,
        root_dir_id: u64_opt(r, 3)?,
        on_open: r.get(4)?,
        wait_threshold_minutes: u64_opt(r, 5)?,
        worm: r.get(6)?,
    })
}

fn recall_from_row(r: &Row) -> rusqlite::Result<RecallRow> {
    let cred: Option<String> = r.get(6)?;
    Ok(RecallRow {
        recall_id: r.get::<_, i64>(0)? as u64,
        file_id: r.get::<_, i64>(1)? as u64,
        version_id: r.get::<_, i64>(2)? as u64,
        state: r.get(3)?,
        eta: r.get(4)?,
        error: r.get(5)?,
        read_credential: cred.and_then(|s| serde_json::from_str(&s).ok()),
        entity_version: r.get::<_, i64>(7)? as u64,
    })
}

// ---------------------------------------------------------------- apply

fn is_echo(tx: &Transaction, edge_id: u64, ev: &Event) -> Result<bool> {
    let Some(o) = &ev.origin else {
        return Ok(false);
    };
    if edge_id == 0 || o.edge_id != edge_id {
        return Ok(false);
    }
    // Ours if the outbox holds the op (sent or acked): the changes response
    // carries the same result, so the echo adds nothing.
    let known: Option<String> = tx
        .query_row(
            "SELECT state FROM outbox WHERE op_id = ?1",
            [&o.op_id],
            |r| r.get(0),
        )
        .optional()?;
    Ok(matches!(known.as_deref(), Some("sent" | "acked")))
}

fn apply_event(tx: &Transaction, edge_id: u64, ev: &Event, rep: &mut ApplyReport) -> Result<()> {
    if matches!(
        ev.body,
        Body::FileUpsert(_) | Body::FileRename(_) | Body::FileTombstone(_)
    ) && is_echo(tx, edge_id, ev)?
    {
        rep.echoes += 1;
        return Ok(());
    }
    match &ev.body {
        Body::FileUpsert(row) | Body::FileRename(row) => {
            if row.deleted {
                apply_tombstone(tx, row, rep)
            } else {
                apply_upsert(tx, row, rep)
            }
        }
        Body::FileTombstone(row) => apply_tombstone(tx, row, rep),
        Body::FileConflict {
            file_id,
            op_id,
            reason,
            ..
        } => {
            tx.execute(
                "UPDATE outbox SET state = 'conflict', result = ?2 WHERE op_id = ?1",
                params![op_id, reason],
            )?;
            tx.execute(
                "INSERT INTO conflict (at, file_id, op_id, reason) VALUES (?1, ?2, ?3, ?4)",
                params![now_s(), *file_id as i64, op_id, reason],
            )?;
            rep.conflicts += 1;
            Ok(())
        }
        Body::AclUpsert(a) => {
            let n = tx.execute(
                "INSERT INTO acl (acl_id, dataset_id, sha256, entity_version, body) VALUES (?1, ?2, ?3, ?4, ?5) \
                 ON CONFLICT(acl_id) DO UPDATE SET dataset_id = excluded.dataset_id, sha256 = excluded.sha256, \
                 entity_version = excluded.entity_version, body = excluded.body \
                 WHERE excluded.entity_version > acl.entity_version",
                params![a.acl_id as i64, a.dataset_id as i64, a.sha256, a.entity_version as i64, a.raw.to_string()],
            )?;
            rep.acls_changed |= n > 0;
            count(rep, n);
            Ok(())
        }
        Body::ShareUpsert(s) => {
            let n = tx.execute(
                "INSERT INTO share (share_id, dataset_id, name, root_dir_id, on_open, wait_threshold_minutes, worm, \
                 entity_version, body) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9) \
                 ON CONFLICT(share_id) DO UPDATE SET dataset_id = excluded.dataset_id, name = excluded.name, \
                 root_dir_id = excluded.root_dir_id, on_open = excluded.on_open, \
                 wait_threshold_minutes = excluded.wait_threshold_minutes, worm = excluded.worm, \
                 entity_version = excluded.entity_version, body = excluded.body \
                 WHERE excluded.entity_version > share.entity_version",
                params![
                    s.share_id as i64,
                    s.dataset_id as i64,
                    s.name,
                    s.root_dir_id.map(|v| v as i64),
                    s.on_open,
                    s.wait_threshold_minutes.map(|v| v as i64),
                    s.worm_enabled,
                    s.entity_version as i64,
                    s.raw.to_string()
                ],
            )?;
            if n > 0 {
                rep.shares_changed = true;
                // A root may now be materialisable (or renamed).
                if let Some(root) = s.root_dir_id {
                    tx.execute(
                        "UPDATE node SET fs_pending = 1 WHERE file_id = ?1",
                        [root as i64],
                    )?;
                }
            }
            count(rep, n);
            Ok(())
        }
        Body::ShareDelete {
            share_id,
            entity_version,
        } => {
            // Only a delete newer than the row: a re-sent upsert of the same
            // share never brings it back after this (share ids aren't reused).
            let n = tx.execute(
                "DELETE FROM share WHERE share_id = ?1 AND entity_version < ?2",
                params![*share_id as i64, *entity_version as i64],
            )?;
            if n > 0 {
                rep.shares_changed = true;
            }
            count(rep, n);
            Ok(())
        }
        Body::DatasetUpsert(d) => {
            let n = tx.execute(
                "INSERT INTO dataset (dataset_id, name, backend_kind, migration_state, entity_version, body) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6) ON CONFLICT(dataset_id) DO UPDATE SET name = excluded.name, \
                 backend_kind = excluded.backend_kind, migration_state = excluded.migration_state, \
                 entity_version = excluded.entity_version, body = excluded.body \
                 WHERE excluded.entity_version > dataset.entity_version",
                params![
                    d.dataset_id as i64,
                    d.name,
                    d.backend_kind,
                    d.migration_state,
                    d.entity_version as i64,
                    d.raw.to_string()
                ],
            )?;
            count(rep, n);
            Ok(())
        }
        Body::RecallState(r) => {
            if upsert_recall_tx(tx, r, false)? {
                rep.applied += 1;
                rep.recalls.push(r.clone());
            } else {
                rep.stale += 1;
            }
            Ok(())
        }
        Body::Unknown { ty } => {
            debug!(ty, "skipping unknown feed event type");
            rep.unknown += 1;
            Ok(())
        }
    }
}

fn count(rep: &mut ApplyReport, n: usize) {
    if n > 0 {
        rep.applied += 1;
    } else {
        rep.stale += 1;
    }
}

fn upsert_recall_tx(tx: &Transaction, r: &RecallRow, local: bool) -> Result<bool> {
    let existing: Option<i64> = tx
        .query_row(
            "SELECT entity_version FROM recall WHERE recall_id = ?1",
            [r.recall_id as i64],
            |row| row.get(0),
        )
        .optional()?;
    if let Some(ev) = existing {
        // Local updates carry the stored version; feed rows must be newer.
        let newer = if local {
            r.entity_version as i64 >= ev
        } else {
            r.entity_version as i64 > ev
        };
        if !newer {
            return Ok(false);
        }
    }
    if r.is_active() {
        // One active recall per (file_id, content version): a newer recall id
        // for the same key supersedes the old one.
        tx.execute(
            "UPDATE recall SET state = 'cancelled', updated_at = ?4 WHERE file_id = ?1 AND version_id = ?2 \
             AND recall_id != ?3 AND state NOT IN ('available', 'failed', 'cancelled')",
            params![r.file_id as i64, r.version_id as i64, r.recall_id as i64, now_s()],
        )?;
    }
    tx.execute(
        "INSERT INTO recall (recall_id, file_id, version_id, state, eta, error, read_credential, entity_version, \
         started_at, updated_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?9) \
         ON CONFLICT(recall_id) DO UPDATE SET state = excluded.state, eta = excluded.eta, error = excluded.error, \
         read_credential = COALESCE(excluded.read_credential, recall.read_credential), \
         entity_version = excluded.entity_version, updated_at = excluded.updated_at",
        params![
            r.recall_id as i64,
            r.file_id as i64,
            r.version_id as i64,
            r.state,
            r.eta,
            r.error,
            r.read_credential.as_ref().map(|v| v.to_string()),
            r.entity_version as i64,
            now_s()
        ],
    )?;
    Ok(true)
}

fn existing_node(tx: &Transaction, file_id: u64) -> Result<Option<(i64, u64, bool)>> {
    Ok(tx
        .query_row(
            // Held: uncommitted writes, or a local rename, delete or chown the
            // service hasn't acknowledged (docs/design/namespace-sync.md).
            "SELECT lid, entity_version, local_dirty OR EXISTS (SELECT 1 FROM outbox o \
             WHERE o.lid = node.lid AND o.state IN ('queued', 'sent') AND o.op IN ('rename', 'unlink', 'owner')) \
             FROM node WHERE file_id = ?1",
            [file_id as i64],
            |r| Ok((r.get(0)?, r.get::<_, i64>(1)? as u64, r.get(2)?)),
        )
        .optional()?)
}

fn conflict_hold(tx: &Transaction, lid: i64, file_id: u64, rep: &mut ApplyReport) -> Result<()> {
    tx.execute(
        "INSERT INTO conflict (at, file_id, lid, reason) VALUES (?1, ?2, ?3, 'local_unacknowledged_changes')",
        params![now_s(), file_id as i64, lid],
    )?;
    rep.conflicts += 1;
    Ok(())
}

fn apply_upsert(tx: &Transaction, row: &FileRow, rep: &mut ApplyReport) -> Result<()> {
    let existing = existing_node(tx, row.file_id)?;
    if let Some((lid, ev, dirty)) = existing {
        if ev >= row.entity_version {
            rep.stale += 1;
            return Ok(());
        }
        if dirty {
            // The edge's own change wins until the service acknowledges it.
            return conflict_hold(tx, lid, row.file_id, rep);
        }
    }
    // Resolve the parent.
    let (parent_lid, waiting) = match row.parent_id {
        None => (None, false),
        Some(pid) => {
            let p: Option<(i64, bool)> = tx
                .query_row(
                    "SELECT lid, deleted FROM node WHERE file_id = ?1",
                    [pid as i64],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .optional()?;
            match p {
                Some((plid, false)) => (Some(plid), false),
                _ => (None, true),
            }
        }
    };
    if waiting {
        rep.orphans_parked += 1;
    }
    let (archived, online) = match &row.location {
        Some(l) => (l.archived, !l.archived || l.restore_state == "completed"),
        None => (false, false),
    };
    let stub_status = match row.stub {
        Some(_) => stub_status::WANTED,
        None => stub_status::NONE,
    };
    let location = row.location.as_ref().map(|l| l.raw.to_string());
    if let (Some((lid, _, _)), true) = (existing, row.kind.as_str() == "dir") {
        let old: Option<Option<i64>> = tx
            .query_row("SELECT acl_id FROM node WHERE lid = ?1", [lid], |r| {
                r.get(0)
            })
            .optional()?;
        if old.is_some_and(|a| a != row.acl_id.map(|v| v as i64)) {
            rep.acls_changed = true;
        }
    }
    let lid = match existing {
        Some((lid, _, _)) => {
            tx.execute(
                "UPDATE node SET dataset_id = ?2, parent_file_id = ?3, parent_lid = ?4, waiting_parent = ?5, \
                 parked_at = CASE WHEN ?5 THEN COALESCE(parked_at, ?6) ELSE NULL END, name = ?7, kind = ?8, \
                 symlink_target = ?9, size = ?10, mtime_ns = ?11, uid = ?12, gid = ?13, mode = ?14, owner_sid = ?15, \
                 acl_id = ?16, catalog_state = ?17, content_version_id = ?18, crc64nvme = ?19, location = ?20, \
                 archived = ?21, online = ?22, stub_version_id = ?23, stub_resident_bytes = ?24, \
                 stub_status = CASE \
                     WHEN ?23 IS NULL THEN 'none' \
                     WHEN stub_status IN ('done', 'skipped') AND stub_version_id IS ?23 \
                          AND stub_resident_bytes IS ?24 THEN stub_status \
                     ELSE ?25 END, \
                 entity_version = ?26, deleted = 0, fs_pending = 1, fs_conflict = NULL WHERE lid = ?1",
                params![
                    lid,
                    row.dataset_id as i64,
                    row.parent_id.map(|v| v as i64),
                    parent_lid,
                    waiting,
                    now_s(),
                    row.name,
                    row.kind.as_str(),
                    row.symlink_target,
                    row.size as i64,
                    row.mtime_ns,
                    row.uid as i64,
                    row.gid as i64,
                    row.mode as i64,
                    row.owner_sid,
                    row.acl_id.map(|v| v as i64),
                    row.state,
                    row.version_id.map(|v| v as i64),
                    row.crc64nvme.map(|v| v as i64),
                    location,
                    archived,
                    online,
                    row.stub.map(|s| s.0 as i64),
                    row.stub.map(|s| s.1 as i64),
                    stub_status,
                    row.entity_version as i64
                ],
            )?;
            lid
        }
        None => {
            tx.execute(
                "INSERT INTO node (file_id, dataset_id, parent_file_id, parent_lid, waiting_parent, parked_at, name, \
                 kind, symlink_target, size, mtime_ns, uid, gid, mode, owner_sid, acl_id, catalog_state, \
                 content_version_id, crc64nvme, location, archived, online, stub_version_id, stub_resident_bytes, \
                 stub_status, entity_version, fs_pending) VALUES (?1, ?2, ?3, ?4, ?5, \
                 CASE WHEN ?5 THEN ?6 ELSE NULL END, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, \
                 ?20, ?21, ?22, ?23, ?24, ?25, ?26, 1)",
                params![
                    row.file_id as i64,
                    row.dataset_id as i64,
                    row.parent_id.map(|v| v as i64),
                    parent_lid,
                    waiting,
                    now_s(),
                    row.name,
                    row.kind.as_str(),
                    row.symlink_target,
                    row.size as i64,
                    row.mtime_ns,
                    row.uid as i64,
                    row.gid as i64,
                    row.mode as i64,
                    row.owner_sid,
                    row.acl_id.map(|v| v as i64),
                    row.state,
                    row.version_id.map(|v| v as i64),
                    row.crc64nvme.map(|v| v as i64),
                    location,
                    archived,
                    online,
                    row.stub.map(|s| s.0 as i64),
                    row.stub.map(|s| s.1 as i64),
                    stub_status,
                    row.entity_version as i64
                ],
            )?;
            tx.last_insert_rowid()
        }
    };
    if row.stub.is_some() {
        rep.stub_wanted += 1;
    }
    rep.applied += 1;
    // Children that arrived before this directory.
    if row.kind == Kind::Dir {
        let n = tx.execute(
            "UPDATE node SET parent_lid = ?1, waiting_parent = 0, parked_at = NULL, fs_pending = 1 \
             WHERE parent_file_id = ?2 AND waiting_parent = 1",
            params![lid, row.file_id as i64],
        )?;
        rep.orphans_adopted += n;
    }
    Ok(())
}

fn apply_tombstone(tx: &Transaction, row: &FileRow, rep: &mut ApplyReport) -> Result<()> {
    match existing_node(tx, row.file_id)? {
        Some((lid, ev, dirty)) => {
            if ev >= row.entity_version {
                rep.stale += 1;
                return Ok(());
            }
            if dirty {
                return conflict_hold(tx, lid, row.file_id, rep);
            }
            tx.execute(
                "UPDATE node SET deleted = 1, entity_version = ?2, stub_status = 'none', \
                 fs_pending = CASE WHEN ino IS NULL THEN 0 ELSE 1 END WHERE lid = ?1",
                params![lid, row.entity_version as i64],
            )?;
            // A tombstoned child may be what kept its directory's rmdir pending:
            // the redo pass after this page retries it.
        }
        None => {
            // Never seen: remember the tombstone so an older upsert that
            // arrives later is a no-op.
            tx.execute(
                "INSERT INTO node (file_id, dataset_id, parent_file_id, name, kind, entity_version, deleted) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, 1)",
                params![
                    row.file_id as i64,
                    row.dataset_id as i64,
                    row.parent_id.map(|v| v as i64),
                    row.name,
                    row.kind.as_str(),
                    row.entity_version as i64
                ],
            )?;
        }
    }
    rep.applied += 1;
    Ok(())
}

#[cfg(test)]
mod tests;
