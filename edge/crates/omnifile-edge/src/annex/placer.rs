//! The placer (phase A3.3; `docs/milestones/results/wp14-a3.md`, "What A3
//! writes to a NAS"; decision 51). With the remover, **the only code that
//! writes to a NAS**: it puts a recalled file back where it came from, or
//! in a restore folder, through the placement mount, which exists only
//! while a placement is in hand. Every write is on a directory fd opened
//! beneath the export's root (`openat2`, no symlink, no other mount).
//!
//! | # | write | how |
//! |---|---|---|
//! | P0 | the missing folders of a restore folder's relative path | `mkdirat`, one at a time, each journalled; each given its parent's ACL (over NFS, its mode, owner and group) |
//! | P1 | the staging file `<token>-<placement>` in `.omnifile-staging-<annex>-<token>/` | `O_CREAT \| O_EXCL \| O_NOFOLLOW`, its name journalled before and its inode after; locked to the two accounts on its fd and read back before a byte is written |
//! | P2 | its bytes | `pwrite` on that fd only, from the version's ranged read, the read credential renewed as it runs out; `filled_to` journalled; a resume needs the same inode, nlink 1 and ACL |
//! | P3 | its group (NFS) and ACL, then the read-back, then its times | the captured descriptor on the fd; protected and made explicit in any folder but its own; a mode's setuid and setgid bits wait for P5 |
//! | P4 | under its final name, never replacing anything | `linkat` from the staging folder, refused at the server if the name exists; where a link is refused, #61's checked placeholder |
//! | P5 | its owner (SMB: owner and group), then what `done` says | after P4, only on an fd that is still the inode, size and mtime `secured` saw; the attributes are journalled here, and never fail the placement: the file is under its name |
//! | P6 | the pointer, when the file is back under its own name | moved into the staging folder, then unlinked only on its recorded inode and text hash, or an older pointer's exact text; put back if it isn't |
//! | P7 | clean up | only names the journal owns, on their inodes; a folder only if empty; what it couldn't do is retried and reported |
//!
//! **Gates.** A ticket from the service for each placement, and the
//! question `go` right before P4, with the folder's ACL as it is now and the
//! staged ACL as it will be with the file's own owner and group. Before P4,
//! a forced `statx` must match what was journalled after P3 (`secured`). A
//! restart at P4 takes all of this again.
//!
//! **Journal.** Each step is written to `annex.db` before it is taken; a
//! write never runs if that failed. A restart resumes from it, and first
//! checks whether the file is under its final name already.

use std::io;
use std::path::Path;

use ring::digest::{SHA256, digest};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};
use tracing::{error, info, warn};

use super::acl::{self, Flavour};
use super::db::AnnexDb;
use super::nasfs::{Kind, Root};
use super::service::{AnnexService, READ_DEFINITE, READ_REFUSED};
use omnifile_identity::{IdMap, Sid};

/// A name for this annex's staging folder, at the root of the export. One per
/// annex, because its journal is the annex's. The token is journalled before the
/// `mkdir`, so a crash or a replay never leaves a name that blocks the next try:
/// that try uses a new name, and nothing is ever adopted.
pub fn staging_dir(annex_id: u64, token: &str) -> String {
    format!(".omnifile-staging-{annex_id}-{token}")
}

/// A name [`staging_dir`] makes (the scanner skips it at the export's root,
/// and only there).
pub fn is_staging_dir(name: &str) -> bool {
    let Some(rest) = name.strip_prefix(".omnifile-staging-") else {
        return false;
    };
    let Some((annex, token)) = rest.split_once('-') else {
        return false;
    };
    !annex.is_empty()
        && annex.bytes().all(|b| b.is_ascii_digit())
        && !token.is_empty()
        && token.bytes().all(|b| b.is_ascii_hexdigit())
}

/// The bytes of one ranged read during the fill.
const CHUNK: u64 = 8 << 20;
/// How often the fill journals how far it got.
#[cfg(not(test))]
const FILL_JOURNAL_EVERY: u64 = 256 << 20;
#[cfg(test)]
const FILL_JOURNAL_EVERY: u64 = 8 << 20;
/// How long before a read credential's expiry a new one is asked for (ms).
const RENEW_BEFORE_MS: i64 = 5 * 60_000;
/// A placement waiting longer than this doesn't keep the placement mount up (ms).
const MOUNT_WAIT_MS: i64 = 60_000;
/// Transient errors are tried again this many times before a placement fails.
pub const MAX_ATTEMPTS: i64 = 6;
const BACKOFF_MS: i64 = 10_000;
/// How often a paused placement asks again.
const PAUSE_RETRY_MS: i64 = 5 * 60_000;
/// How often a stuck placement, or something left to clean up, is tried again.
const STUCK_RETRY_MS: i64 = 3_600_000;

/// One placement's ticket (`POST annex-placement-tickets`).
#[derive(Clone, Debug, PartialEq)]
pub struct Ticket {
    pub placement_id: u64,
    pub file_id: u64,
    pub path: String,
    pub size: u64,
    pub crc64: u64,
    pub mtime_ns: Option<i64>,
    pub folder: String,
    /// The file's own name at the target (not always `names[0]`: an older
    /// version at its origin never takes it).
    pub name: String,
    pub names: Vec<String>,
    pub origin: bool,
    pub same_content: bool,
    pub protect: bool,
    pub acl: Value,
    pub read: Value,
    /// The NAS's own numeric owner and group, as the copy saw them.
    pub uid: Option<u32>,
    pub gid: Option<u32>,
    /// When to have asked for a new read credential by (ms since the epoch).
    pub read_until_ms: Option<i64>,
    /// A3.4: the file at the name may be replaced, if it is still exactly this.
    pub replace: Option<replace::Expect>,
}

fn id(v: &Value) -> Option<u64> {
    v.as_u64()
        .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
}

impl Ticket {
    pub fn from_json(v: &Value) -> Result<Ticket, String> {
        let s = |k: &str| v.get(k).and_then(Value::as_str).map(str::to_owned);
        let names: Vec<String> = v
            .get("names")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|x| x.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default();
        if names.is_empty() || names.iter().any(|n| !plain(n)) {
            return Err("a ticket's names must be plain file names".into());
        }
        let folder = s("folder").unwrap_or_default();
        super::nasfs::check_rel(&folder).map_err(|e| format!("folder: {e}"))?;
        if folder.split('/').any(|p| p.starts_with(".omnifile-")) {
            return Err("the target folder is OmniFile's own".into());
        }
        Ok(Ticket {
            placement_id: id(&v["placement_id"]).ok_or("placement_id")?,
            file_id: id(&v["file_id"]).ok_or("file_id")?,
            path: s("path").unwrap_or_default(),
            size: v["size"].as_u64().ok_or("size")?,
            crc64: u64::from_str_radix(&s("crc64").ok_or("crc64")?, 16)
                .map_err(|_| "crc64 is 16 hex digits")?,
            mtime_ns: v.get("mtime_ns").and_then(|x| {
                x.as_i64()
                    .or_else(|| x.as_str().and_then(|s| s.parse().ok()))
            }),
            folder,
            name: s("name").unwrap_or_else(|| names[0].clone()),
            names,
            origin: v["origin"].as_bool().unwrap_or(false),
            same_content: v["same_content"].as_bool().unwrap_or(false),
            protect: v["protect"].as_bool().unwrap_or(true),
            acl: v.get("acl").cloned().unwrap_or(Value::Null),
            read: v.get("read").cloned().unwrap_or(Value::Null),
            uid: v.get("uid").and_then(Value::as_u64).map(|x| x as u32),
            gid: v.get("gid").and_then(Value::as_u64).map(|x| x as u32),
            read_until_ms: v
                .get("expires_at")
                .and_then(Value::as_str)
                .and_then(super::scan::parse_rfc3339_ms),
            replace: match v.get("replace") {
                None | Some(Value::Null) => None,
                Some(r) => Some(replace::Expect::from_json(r)?),
            },
        })
    }
}

/// A name the placer may make: one component, not OmniFile's own.
pub fn plain(n: &str) -> bool {
    !n.is_empty()
        && n.len() <= 255
        && n != "."
        && n != ".."
        && !n.contains('/')
        && !n.contains('\0')
        && !n.starts_with(".omnifile-")
        && !n.ends_with(super::remover::POINTER_SUFFIX)
}

/// One placement, as journalled.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Placement {
    pub placement_id: u64,
    pub ticket: String,
    pub state: String,
    pub staging_name: String,
    pub staging_ino: Option<u64>,
    pub staging_acl: Option<String>,
    pub filled_to: u64,
    pub read_backs: i64,
    pub secured_ctime: Option<i64>,
    pub secured_acl: Option<String>,
    pub secured_mtime: Option<i64>,
    /// The read-back's CRC-64/NVME (hex), which `go` carries.
    pub read_crc: Option<String>,
    pub final_name: Option<String>,
    pub final_ino: Option<u64>,
    pub placeholder: Option<(u64, i64)>,
    pub made: String,
    pub outcome: Option<String>,
    pub reason: Option<String>,
    pub unreported: Option<String>,
    pub owner_restored: Option<bool>,
    pub pointer_deleted: Option<bool>,
    pub attempts: i64,
    pub next_at: i64,
    /// The step a stuck placement goes on from.
    pub stuck_from: Option<String>,
    /// What `done` reports, as it was at P5 (JSON; null if it isn't the copy).
    pub attrs: Option<String>,
    /// What a failed placement couldn't clean up yet.
    pub leftover: Option<String>,
    /// Where it was placed, below the annex's root (for the age rules).
    pub placed_path: Option<String>,
    /// A folder of P0's whose `mkdir` may have been taken and not journalled.
    pub made_pending: Option<String>,
}

pub const DDL: &str = r#"
CREATE TABLE IF NOT EXISTS placement_replace (
    placement_id INTEGER PRIMARY KEY,
    path         TEXT NOT NULL,
    state        TEXT NOT NULL,
    hidden_name  TEXT NOT NULL,
    ino          INTEGER NOT NULL,
    size         INTEGER NOT NULL,
    mtime_ns     INTEGER NOT NULL,
    ctime_ns     INTEGER NOT NULL,
    crc64        TEXT NOT NULL,
    hidden_at    INTEGER,
    hidden_ctime INTEGER,
    aside_name   TEXT,
    placeholder_ino   INTEGER,
    placeholder_ctime INTEGER,
    placeholder_name  TEXT,
    -- The folder the hide was made in: its inode, and where it is now if a user
    -- moved it (the confirm pass of #98, C1).
    folder_ino   INTEGER,
    hidden_folder TEXT,
    reason       TEXT,
    unreported   TEXT,
    updated_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS placement (
    placement_id INTEGER PRIMARY KEY,
    ticket       TEXT NOT NULL,
    state        TEXT NOT NULL,
    staging_name TEXT NOT NULL,
    staging_ino  INTEGER,
    staging_acl  TEXT,
    filled_to    INTEGER NOT NULL DEFAULT 0,
    read_backs   INTEGER NOT NULL DEFAULT 0,
    secured_ctime INTEGER,
    secured_acl  TEXT,
    secured_mtime INTEGER,
    read_crc     TEXT,
    final_name   TEXT,
    final_ino    INTEGER,
    placeholder_ino   INTEGER,
    placeholder_ctime INTEGER,
    made         TEXT NOT NULL DEFAULT '[]',
    outcome      TEXT,
    reason       TEXT,
    unreported   TEXT,
    owner_restored INTEGER,
    pointer_deleted INTEGER,
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_at      INTEGER NOT NULL DEFAULT 0,
    stuck_from   TEXT,
    attrs        TEXT,
    leftover     TEXT,
    placed_path  TEXT,
    made_pending TEXT,
    updated_at   INTEGER NOT NULL
);
"#;

const COLS: &str = "placement_id, ticket, state, staging_name, staging_ino, staging_acl, \
    filled_to, read_backs, secured_ctime, secured_acl, final_name, final_ino, placeholder_ino, \
    placeholder_ctime, made, outcome, reason, unreported, owner_restored, pointer_deleted, \
    attempts, next_at, secured_mtime, stuck_from, attrs, leftover, placed_path, read_crc, \
    made_pending";

fn row(r: &rusqlite::Row<'_>) -> rusqlite::Result<Placement> {
    let u = |i: usize| -> rusqlite::Result<Option<u64>> {
        Ok(r.get::<_, Option<i64>>(i)?.map(|v| v as u64))
    };
    Ok(Placement {
        placement_id: r.get::<_, i64>(0)? as u64,
        ticket: r.get(1)?,
        state: r.get(2)?,
        staging_name: r.get(3)?,
        staging_ino: u(4)?,
        staging_acl: r.get(5)?,
        filled_to: r.get::<_, i64>(6)? as u64,
        read_backs: r.get(7)?,
        secured_ctime: r.get(8)?,
        secured_acl: r.get(9)?,
        final_name: r.get(10)?,
        final_ino: u(11)?,
        placeholder: match (u(12)?, r.get::<_, Option<i64>>(13)?) {
            (Some(i), Some(c)) => Some((i, c)),
            _ => None,
        },
        made: r.get(14)?,
        outcome: r.get(15)?,
        reason: r.get(16)?,
        unreported: r.get(17)?,
        owner_restored: r.get::<_, Option<i64>>(18)?.map(|v| v != 0),
        pointer_deleted: r.get::<_, Option<i64>>(19)?.map(|v| v != 0),
        attempts: r.get(20)?,
        next_at: r.get(21)?,
        secured_mtime: r.get(22)?,
        stuck_from: r.get(23)?,
        attrs: r.get(24)?,
        leftover: r.get(25)?,
        placed_path: r.get(26)?,
        read_crc: r.get(27)?,
        made_pending: r.get(28)?,
    })
}

/// The journal, in `annex.db`.
pub struct Journal<'a>(pub &'a AnnexDb);

impl Journal<'_> {
    pub fn get(&self, id: u64) -> rusqlite::Result<Option<Placement>> {
        self.0
            .conn()
            .query_row(
                &format!("SELECT {COLS} FROM placement WHERE placement_id = ?1"),
                [id as i64],
                row,
            )
            .optional()
    }

    /// Placements not finished and reported, and those with a replace's fact
    /// not yet reported.
    pub fn open(&self) -> rusqlite::Result<Vec<Placement>> {
        let mut st = self.0.conn().prepare(&format!(
            "SELECT {COLS} FROM placement WHERE state NOT IN ('done', 'failed') \
             OR unreported IS NOT NULL OR leftover IS NOT NULL OR placement_id IN \
             (SELECT placement_id FROM placement_replace WHERE unreported IS NOT NULL) \
             ORDER BY placement_id"
        ))?;
        let rows = st.query_map([], row)?;
        rows.collect()
    }

    /// Whether the placement mount has work: a placement not ended and due
    /// soon, or something left to clean up. One reported only, or waiting
    /// longer (stuck, paused, backing off), needs no writable handle.
    pub fn needs_mount(&self, now: i64) -> bool {
        self.0
            .conn()
            .query_row(
                "SELECT EXISTS (SELECT 1 FROM placement WHERE next_at <= ?1 \
                 AND (state NOT IN ('done', 'failed') OR leftover IS NOT NULL))",
                [now + MOUNT_WAIT_MS],
                |r| r.get::<_, bool>(0),
            )
            .unwrap_or(true)
    }

    /// Every staging name the journal owns.
    pub fn staging_names(&self) -> rusqlite::Result<std::collections::HashSet<String>> {
        let mut st = self
            .0
            .conn()
            .prepare("SELECT staging_name FROM placement")?;
        let rows = st.query_map([], |r| r.get::<_, String>(0))?;
        rows.collect()
    }

    /// The placements in hand (asked for, and not ended).
    pub fn in_hand(&self) -> rusqlite::Result<Vec<u64>> {
        let mut st = self.0.conn().prepare(
            "SELECT placement_id FROM placement WHERE state NOT IN ('done', 'failed') \
             ORDER BY placement_id",
        )?;
        let rows = st.query_map([], |r| r.get::<_, i64>(0))?;
        rows.map(|r| r.map(|v| v as u64)).collect()
    }

    /// A ticket's new read credential: nothing else of the ticket changes
    /// (what was checked for the old one is checked again at `go`).
    pub fn renew_read(&self, pid: u64, fresh: &Value) -> rusqlite::Result<()> {
        let Some(p) = self.get(pid)? else {
            return Ok(());
        };
        let Ok(mut v) = serde_json::from_str::<Value>(&p.ticket) else {
            return Ok(());
        };
        v["read"] = fresh.get("read").cloned().unwrap_or(Value::Null);
        v["expires_at"] = fresh.get("expires_at").cloned().unwrap_or(Value::Null);
        self.0.conn().execute(
            "UPDATE placement SET ticket = ?2 WHERE placement_id = ?1",
            params![pid as i64, v.to_string()],
        )?;
        Ok(())
    }

    pub fn insert(&self, t: &Value, staging: &str, now: i64) -> rusqlite::Result<bool> {
        let pid = id(&t["placement_id"]).unwrap_or(0) as i64;
        let n = self.0.conn().execute(
            "INSERT OR IGNORE INTO placement (placement_id, ticket, state, staging_name, updated_at) \
             VALUES (?1, ?2, 'ticketed', ?3, ?4)",
            params![pid, t.to_string(), staging, now],
        )?;
        Ok(n == 1)
    }

    /// One column set and the state moved, in one write.
    pub fn set(
        &self,
        id: u64,
        state: &str,
        sets: &[(&str, rusqlite::types::Value)],
        now: i64,
    ) -> rusqlite::Result<()> {
        // Progress starts the count of tries again.
        let mut sql = String::from(
            "UPDATE placement SET attempts = CASE WHEN state = ?1 THEN attempts ELSE 0 END, \
             state = ?1, updated_at = ?2",
        );
        for (i, (k, _)) in sets.iter().enumerate() {
            sql.push_str(&format!(", {k} = ?{}", i + 4));
        }
        sql.push_str(" WHERE placement_id = ?3");
        let mut vals: Vec<rusqlite::types::Value> =
            vec![state.to_owned().into(), now.into(), (id as i64).into()];
        vals.extend(sets.iter().map(|(_, v)| v.clone()));
        self.0
            .conn()
            .execute(&sql, rusqlite::params_from_iter(vals))?;
        Ok(())
    }
}

fn int(v: impl Into<i64>) -> rusqlite::types::Value {
    rusqlite::types::Value::Integer(v.into())
}

fn text(v: impl Into<String>) -> rusqlite::types::Value {
    rusqlite::types::Value::Text(v.into())
}

fn errno(e: &io::Error) -> i32 {
    e.raw_os_error().unwrap_or(0)
}

/// In the unit tests, the NAS away at a named point, before the call; else the call.
#[cfg(test)]
fn away<T>(at: &str, call: impl FnOnce() -> io::Result<T>) -> io::Result<T> {
    if faults::eio(at) {
        let n = faults::NEXT_ERRNO.with(|f| f.get());
        Err(io::Error::from_raw_os_error(if n != 0 {
            n
        } else {
            libc::EIO
        }))
    } else {
        call()
    }
}
#[cfg(not(test))]
#[inline(always)]
fn away<T>(_: &str, call: impl FnOnce() -> io::Result<T>) -> io::Result<T> {
    call()
}

/// The NAS said no, and will say it again. Anything else is an outage until
/// shown otherwise: `EIO`, a timeout, a stale handle, a connection gone, and also
/// a read-only remount, a quota or a full disk, which can clear and which a whole
/// copy isn't deleted for.
fn definite(e: &io::Error) -> bool {
    e.kind() == io::ErrorKind::InvalidInput
        || matches!(
            errno(e),
            libc::EACCES | libc::EPERM | libc::EOPNOTSUPP | libc::EINVAL
        )
}

/// Test hooks, per thread: a journal that can't be written, a stop right
/// after a state is journalled (a crash there), a link refused for want of
/// hard links, a byte flipped before the read-back, a name taken by a user
/// right before P4, and a read-back refused.
#[cfg(test)]
#[allow(dead_code)]
pub(crate) mod faults {
    use std::cell::{Cell, RefCell};
    thread_local! {
        pub static JOURNAL_FAILS: Cell<bool> = const { Cell::new(false) };
        pub static STOP_AT: RefCell<Option<String>> = const { RefCell::new(None) };
        pub static LINK_EACCES: Cell<bool> = const { Cell::new(false) };
        pub static CRC_FLIP: Cell<bool> = const { Cell::new(false) };
        pub static READBACK_EACCES: Cell<bool> = const { Cell::new(false) };
        pub static ENOSPC_AT: Cell<u64> = const { Cell::new(0) };
        pub static BEFORE_P4: RefCell<Option<Box<dyn FnOnce()>>> = const { RefCell::new(None) };
        pub static BEFORE_GO: RefCell<Option<Box<dyn FnOnce()>>> = const { RefCell::new(None) };
        pub static BEFORE_P6: RefCell<Option<Box<dyn FnOnce()>>> = const { RefCell::new(None) };
        /// The server took the link and the reply was lost: EEXIST to a replay.
        pub static LINK_REPLAYED: Cell<bool> = const { Cell::new(false) };
        /// The cleanup can't reach the NAS.
        pub static CLEANUP_FAILS: Cell<bool> = const { Cell::new(false) };
        /// A new staging file that won't take its lock.
        pub static BORN_WIDE: Cell<bool> = const { Cell::new(false) };
        /// P5's open is refused.
        pub static P5_REFUSED: Cell<bool> = const { Cell::new(false) };
        /// P5's open meets the NAS being away, this many more times.
        pub static P5_EIO: Cell<u32> = const { Cell::new(0) };
        /// Runs between P5's look and its open (a swap there).
        pub static BEFORE_P5_OPEN: RefCell<Option<Box<dyn FnOnce()>>> = const { RefCell::new(None) };
        /// The server refuses any open for writing (what a mode does to anyone
        /// but root, so that this holds when the tests run as root too).
        pub static WRITE_OPENS_REFUSED: Cell<bool> = const { Cell::new(false) };
        /// Runs between P5's chown and its setuid restore.
        pub static BEFORE_P5_RESTORE: RefCell<Option<Box<dyn FnOnce()>>> = const { RefCell::new(None) };
        /// How many times P5 went on to set an owner.
        pub static P5_OWNER_SETS: Cell<u32> = const { Cell::new(0) };
        /// The server says EIO for the staging folder (the NAS away).
        pub static STAGING_EIO: Cell<bool> = const { Cell::new(false) };
        /// The target's stat at a resume meets the NAS being away, this many times.
        pub static TARGET_STAT_EIO: Cell<u32> = const { Cell::new(0) };
        /// `go`'s statx against `secured` meets the NAS being away, once.
        pub static GO_STATX_EIO: Cell<bool> = const { Cell::new(false) };
        /// The read-back meets the NAS being away.
        pub static READBACK_EIO: Cell<bool> = const { Cell::new(false) };
        /// The chown at P5 meets the NAS being away.
        pub static P5_SET_EIO: Cell<bool> = const { Cell::new(false) };
        /// The chown at P5 is refused by the NAS (no right to).
        pub static P5_SET_REFUSED: Cell<bool> = const { Cell::new(false) };
        /// The staged ACL's print reads differently, its ctime the same.
        pub static PRINT_DIFFERS: Cell<bool> = const { Cell::new(false) };
        /// The pointer's look meets the NAS being away.
        pub static POINTER_EIO: Cell<bool> = const { Cell::new(false) };
        /// The unlink of the staging name after the link fails.
        pub static UNLINK_AFTER_LINK_EIO: Cell<bool> = const { Cell::new(false) };
        /// A write of the fill meets the NAS being away.
        pub static FILL_EIO: Cell<bool> = const { Cell::new(false) };
        /// What `away` fails with, if not `EIO`.
        pub static NEXT_ERRNO: Cell<i32> = const { Cell::new(0) };
        /// The server hides POSIX ACLs.
        pub static ACLS_HIDDEN: Cell<bool> = const { Cell::new(false) };
        /// A retry's own journal write fails.
        pub static RETRY_NOT_RECORDED: Cell<bool> = const { Cell::new(false) };
        /// A lock can't be set: the NAS is away.
        pub static LOCK_EIO: Cell<bool> = const { Cell::new(false) };
        /// P3's chown of the group doesn't take (a squashing server).
        pub static P3_GROUP_IGNORED: Cell<bool> = const { Cell::new(false) };
        /// Staging's `set_meta` after the mkdir fails.
        pub static MADE_NOT_RECORDED: Cell<bool> = const { Cell::new(false) };
        /// The server took the mkdir and the reply was lost.
        pub static MKDIR_REPLAYED: Cell<bool> = const { Cell::new(false) };
    }
    thread_local! {
        /// Points where the next call meets the NAS being away, once each.
        pub static EIO_AT: RefCell<Vec<&'static str>> = const { RefCell::new(Vec::new()) };
    }
    /// Whether this point was asked to fail, once.
    pub fn eio(at: &str) -> bool {
        EIO_AT.with(|v| {
            let mut v = v.borrow_mut();
            match v.iter().position(|x| *x == at) {
                Some(i) => {
                    v.remove(i);
                    true
                }
                None => false,
            }
        })
    }
    pub fn stop(at: &str) -> bool {
        STOP_AT.with(|s| {
            let mut s = s.borrow_mut();
            if s.as_deref() == Some(at) {
                *s = None;
                true
            } else {
                false
            }
        })
    }
}

/// Where a pass stopped (a crash in a test, or a step that must wait).
#[derive(Debug, PartialEq, Eq)]
enum Flow {
    Next,
    Stop,
}

/// One pass's tally.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Pass {
    pub ticketed: usize,
    pub placed: usize,
    pub failed: usize,
    /// A retry couldn't be journalled: the caller holds off for a while.
    pub journal_failed: bool,
}

/// What a pass works with.
pub struct Placer<'a, S: AnnexService> {
    pub svc: &'a S,
    pub db: &'a AnnexDb,
    /// The export's root on the placement mount.
    pub pl: &'a Root,
    /// The export's root on the read-only mount (the read-back).
    pub ro: &'a Root,
    pub annex_id: u64,
    pub annex_name: &'a str,
    /// The annex's root below the export (its pointers' paths are below it).
    pub annex_rel: &'a str,
    pub flavour: Flavour,
    pub ids: &'a dyn IdMap,
    /// The staging folder's protected DACL over SMB names these two.
    pub placement_sid: Option<String>,
    pub read_sid: Option<String>,
    /// The read account's NFSv4 name, when it isn't the placement account
    /// (over NFSv4 the staging folder's ACL names it).
    pub read_who: Option<String>,
    pub now: i64,
    pub state_dir: &'a Path,
    /// Why the staging ACL's two accounts couldn't be named, if so.
    pub accounts_problem: Option<AccountTrouble>,
    /// A retry couldn't be journalled in this pass.
    pub journal_failed: std::cell::Cell<bool>,
    /// A3.4: the annex's grace period, between hiding a file a placement replaces
    /// and placing the new one (ms).
    pub grace_ms: i64,
    /// The NAS is an SMB share: a file to be replaced is read again for its CRC
    /// (SMB lets a client set the ChangeTime), and has no alternate data stream.
    pub smb: bool,
}

/// The uid and gid a file ends up with over NFS.
type FinalIds = (Option<u32>, Option<u32>);

/// Why the two accounts a lock names aren't known.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AccountTrouble {
    /// Winbind answered: no such account. An owner fixes the config.
    Unknown(String),
    /// Winbind is away: it may clear.
    Lookup(String),
}

impl<S: AnnexService> Placer<'_, S> {
    fn journal(&self) -> Journal<'_> {
        Journal(self.db)
    }

    /// The debug build's crash points (the exit test kills the unit at each
    /// step): `OMNIFILE_ANNEX_CRASH_AT=<state>` aborts right after that
    /// state is journalled, once.
    fn crash_point(&self, at: &str) -> Flow {
        #[cfg(test)]
        if faults::stop(at) {
            return Flow::Stop;
        }
        #[cfg(debug_assertions)]
        if std::env::var("OMNIFILE_ANNEX_CRASH_AT").is_ok_and(|v| v == format!("place_{at}")) {
            let mark = self.state_dir.join(format!("crashed-place_{at}"));
            if !mark.exists() {
                let _ = std::fs::write(&mark, b"");
                warn!(at, "crash point: aborting");
                std::process::abort();
            }
        }
        // The exit test holds a placement at a step for a while, the daemon up
        // and scanning (`OMNIFILE_ANNEX_HOLD_AT=<state>`).
        #[cfg(debug_assertions)]
        if std::env::var("OMNIFILE_ANNEX_HOLD_AT").is_ok_and(|v| v == format!("place_{at}")) {
            warn!(at, "hold point: this placement waits");
            let _ = self.db.conn().execute(
                "UPDATE placement SET next_at = ?2 WHERE state = ?1",
                params![at, self.now + 150_000],
            );
            return Flow::Stop;
        }
        let _ = at;
        Flow::Next
    }

    /// Journal a step; `false` if it couldn't be written: nothing follows.
    #[must_use]
    fn set(&self, p: &Placement, state: &str, sets: &[(&str, rusqlite::types::Value)]) -> bool {
        #[cfg(test)]
        if faults::JOURNAL_FAILS.with(|f| f.get()) {
            error!(
                placement = p.placement_id,
                "annex.db: placement journal (test fault)"
            );
            return false;
        }
        match self.journal().set(p.placement_id, state, sets, self.now) {
            Ok(()) => true,
            Err(e) => {
                error!(placement = p.placement_id, error = %e, "annex.db: placement journal; nothing written");
                false
            }
        }
    }

    fn reload(&self, p: &Placement) -> Option<Placement> {
        self.journal().get(p.placement_id).ok().flatten()
    }

    /// Ask the service, or report a step, saying why not.
    async fn ask(&self, p: &Placement, body: Value) -> Result<Value, super::service::SvcError> {
        let key = format!(
            "{}:{}:{}",
            body["step"].as_str().unwrap_or(""),
            self.now,
            p.attempts
        );
        self.svc.placement_step(p.placement_id, &body, &key).await
    }

    /// Ask the service, or report a step. `None`: unreachable (later).
    async fn step(&self, p: &Placement, body: Value) -> Option<Value> {
        let ask = format!(
            "{}:{}:{}",
            body["step"].as_str().unwrap_or(""),
            self.now,
            p.attempts
        );
        match self.svc.placement_step(p.placement_id, &body, &ask).await {
            Ok(v) => Some(v),
            Err(e) => {
                warn!(placement = p.placement_id, error = %e, "placement: the service later");
                None
            }
        }
    }

    // --- the staging folder ------------------------------------------------------------

    fn staging_rel(&self) -> String {
        self.db
            .meta("staging_rel")
            .ok()
            .flatten()
            .unwrap_or_default()
    }

    /// The staging folder as it is, for what only reads or cleans: it may not
    /// be there.
    fn staging_root(&self) -> io::Result<Root> {
        let rel = self
            .db
            .meta("staging_rel")
            .map_err(|e| io::Error::other(format!("annex.db: {e}")))?
            .unwrap_or_default();
        if rel.is_empty() {
            return Err(io::Error::from_raw_os_error(libc::ENOENT));
        }
        self.pl.sub(&rel)
    }

    /// The staging folder, made and locked down once, checked on every use on
    /// the very fd the creates use. Never adopted: its name has a random token,
    /// journalled before the `mkdir`; only a folder whose successful `mkdir` the
    /// journal recorded is ever used. An attempt that wasn't recorded (a crash,
    /// a replayed `EEXIST`, a journal that failed) is removed if empty and the
    /// next try has a new name, so it never holds up a placement.
    pub fn staging(&self) -> Result<Root, String> {
        match &self.accounts_problem {
            Some(AccountTrouble::Unknown(why)) => {
                return Err(format!("staging_no_account: {why}"));
            }
            Some(AccountTrouble::Lookup(why)) => {
                return Err(format!("the accounts' SIDs: {why}"));
            }
            None => {}
        }
        // A journal that can't be read isn't one that says "nothing yet".
        let got = |k: &str| {
            away("meta_read", || self.db.meta(k).map_err(io::Error::other))
                .map(|v| v.filter(|v| !v.is_empty()))
                .map_err(|e| format!("annex.db: {e}"))
        };
        let set = |k: &str, v: &str| {
            #[cfg(test)]
            if faults::JOURNAL_FAILS.with(|x| x.get()) {
                return Err("annex.db: not recorded (test fault)".to_owned());
            }
            self.db.set_meta(k, v).map_err(|e| format!("annex.db: {e}"))
        };
        let forget = || -> Result<(), String> {
            for k in [
                "staging_ino",
                "staging_acl",
                "staging_uid",
                "staging_made",
                "staging_made_uid",
                "staging_rel",
            ] {
                set(k, "")?;
            }
            Ok(())
        };
        if let Some(ino) = got("staging_ino")? {
            let rel = self.staging_rel();
            let opened = self.pl.open_dir(&rel);
            #[cfg(test)]
            let opened = if faults::STAGING_EIO.with(|x| x.get()) {
                Err(io::Error::from_raw_os_error(libc::EIO))
            } else {
                opened
            };
            match opened {
                Ok((f, st)) => {
                    let print = acl::raw_print(&f, self.flavour, &st)
                        .map_err(|e| format!("staging folder's ACL: {e}"))?;
                    if st.ino.to_string() != ino
                        || Some(print) != got("staging_acl")?
                        || Some(st.uid.to_string()) != got("staging_uid")?
                    {
                        return Err(
                            "staging_changed: the staging folder isn't the one OmniFile made"
                                .into(),
                        );
                    }
                    return Root::from_dir(f, self.pl.path().join(&rel), st.dev)
                        .map_err(|e| format!("staging folder: {e}"));
                }
                // Removed by someone. What was in it fails by itself; the next
                // placement gets a new one.
                Err(e) if e.kind() == io::ErrorKind::NotFound => forget()?,
                Err(e) => return Err(format!("staging folder: {e}")),
            }
        } else if got("staging_made")?.is_some() {
            // Made and never locked, and an owner has since removed it.
            match self.pl.stat(&self.staging_rel(), true) {
                Err(e) if e.kind() == io::ErrorKind::NotFound => forget()?,
                _ => {}
            }
        }
        if got("staging_made")?.is_none() {
            // An attempt that never got recorded isn't used, whoever made it.
            if let Some(old) = got("staging_rel")?
                && let Err(e) = imp::rmdir(self.pl, &old)
                && e.kind() != io::ErrorKind::NotFound
            {
                warn!(folder = old, error = %e, "an earlier staging folder stays");
            }
            let rel = staging_dir(self.annex_id, &token());
            set("staging_rel", &rel)?;
            let made = imp::mkdir(self.pl, &rel);
            #[cfg(test)]
            let made = if made.is_ok() && faults::MKDIR_REPLAYED.with(|x| x.replace(false)) {
                Err(io::Error::from_raw_os_error(libc::EEXIST))
            } else {
                made
            };
            // Even `EEXIST` isn't taken for ours: the next try has a new name.
            made.map_err(|e| format!("staging mkdir: {e}"))?;
            let st = self
                .pl
                .stat(&rel, true)
                .map_err(|e| format!("staging folder: {e}"))?;
            #[cfg(test)]
            if faults::MADE_NOT_RECORDED.with(|x| x.replace(false)) {
                return Err("annex.db: not recorded (test fault)".into());
            }
            set("staging_made_uid", &st.uid.to_string())?;
            set("staging_made", &st.ino.to_string())?;
        }
        // The one the journal made: lock it down, read the lock back, remember it.
        let rel = self.staging_rel();
        let (f, st) = self
            .pl
            .open_dir(&rel)
            .map_err(|e| format!("staging folder: {e}"))?;
        if got("staging_made")?.as_deref() != Some(st.ino.to_string().as_str())
            || got("staging_made_uid")?.as_deref() != Some(st.uid.to_string().as_str())
        {
            return Err(format!(
                "staging_unexpected: {rel} isn't the folder OmniFile made"
            ));
        }
        if imp::dir_has_entries(self.pl, &rel).map_err(|e| format!("staging folder: {e}"))? {
            return Err("staging_unexpected: the staging folder isn't empty".into());
        }
        imp::lock_down(
            &f,
            self.flavour,
            self.placement_sid.as_deref(),
            self.read_sid.as_deref(),
            self.read_who.as_deref(),
            true,
        )
        .map_err(|e| {
            if e.kind() == io::ErrorKind::InvalidInput {
                format!("staging_no_account: {e}")
            } else {
                format!("staging lockdown: {e}")
            }
        })?;
        let st2 = imp::fstat(&f).map_err(|e| e.to_string())?;
        let acl = acl::capture_on(&f, self.flavour, &st2, self.ids)
            .map_err(|e| format!("staging folder's ACL: {e}"))?;
        if !imp::locked(
            &acl,
            self.flavour,
            self.placement_sid.as_deref(),
            self.read_sid.as_deref(),
            self.read_who.as_deref(),
        ) {
            return Err("staging_lockdown_failed: its ACL read back isn't the one set".into());
        }
        let print = acl::raw_print(&f, self.flavour, &st2)
            .map_err(|e| format!("staging folder's ACL: {e}"))?;
        set("staging_acl", &print)?;
        set("staging_uid", &st2.uid.to_string())?;
        set("staging_ino", &st2.ino.to_string())?;
        Root::from_dir(f, self.pl.path().join(&rel), st2.dev)
            .map_err(|e| format!("staging folder: {e}"))
    }

    /// P7's other half: a file in the staging folder the journal doesn't name is
    /// never deleted. It is reported, once.
    fn report_orphans(&self) {
        let rel = self.staging_rel();
        if rel.is_empty() || self.db.meta("staging_ino").ok().flatten().is_none() {
            return;
        }
        let Ok((_, items)) = self.pl.read_dir(&rel) else {
            return;
        };
        // A journal that can't be read would make every staged file an orphan.
        let known = away("staging_names", || {
            self.journal().staging_names().map_err(io::Error::other)
        });
        let Ok(known) = known else {
            return;
        };
        for (name, _) in items {
            let name = name.to_string_lossy().into_owned();
            if known.contains(&name) {
                continue;
            }
            let key = format!("orphan:{name}");
            if self.db.meta(&key).ok().flatten().is_some() {
                continue;
            }
            let path = format!("{rel}/{name}");
            warn!(
                annex = self.annex_id,
                path, "ALERT P7: a staging file the journal doesn't name"
            );
            let ev = json!({"kind": "skipped", "path": path, "reason": "staging_orphan",
                            "detail": "a file in the staging folder no placement knows; not deleted"});
            if self.db.push_event("skipped", Some(&path), &ev).is_ok() {
                let _ = self.db.set_meta(&key, "reported");
            }
        }
    }

    /// The staging folder for a step. An owner problem sticks the placement;
    /// anything else (a NAS that's away, a winbind hiccup reading the ACL) is
    /// tried again.
    fn staging_or_wait(&self, p: &Placement) -> Option<Root> {
        match self.staging() {
            Ok(d) => Some(d),
            Err(e) if e.starts_with("staging_") => {
                self.stuck(p, &e);
                None
            }
            Err(e) => {
                self.retry_later(p, &e);
                None
            }
        }
    }

    // --- a pass ---------------------------------------------------------------------------

    /// Every journalled placement not waiting, from where it stopped.
    pub async fn pass(&self) -> Pass {
        let mut out = Pass::default();
        for p in self.journal().open().unwrap_or_default() {
            if p.next_at > self.now {
                continue;
            }
            self.run(p, &mut out).await;
        }
        self.report_orphans();
        out.journal_failed = self.journal_failed.get();
        out
    }

    async fn run(&self, p: Placement, out: &mut Pass) {
        let before = p.state.clone();
        if p.leftover.is_some() {
            self.clean_leftover(&p).await;
        }
        self.drive(p.clone()).await;
        if let Some(now) = self.reload(&p) {
            if now.state == "done" && before != "done" {
                out.placed += 1;
            }
            if now.state == "failed" && before != "failed" {
                out.failed += 1;
            }
            report_pending(self.svc, self.db, &now, self.now).await;
        }
    }

    fn ticket(&self, p: &Placement) -> Result<Ticket, String> {
        let v: Value = serde_json::from_str(&p.ticket).map_err(|e| e.to_string())?;
        Ticket::from_json(&v)
    }

    fn fail(&self, p: &Placement, reason: &str) {
        warn!(placement = p.placement_id, reason, "placement failed");
        // As the journal has it: a step that fails can have just recorded
        // something to clean up (P0's folders) in a row newer than `p`.
        let clean = self.cleanup(self.reload(p).as_ref().unwrap_or(p));
        let _ = self.set(
            p,
            "failed",
            &[
                ("reason", text(reason)),
                ("unreported", text("failed")),
                (
                    "leftover",
                    clean.map_or(rusqlite::types::Value::Null, rusqlite::types::Value::Text),
                ),
                ("next_at", int(self.now + STUCK_RETRY_MS)),
            ],
        );
    }

    /// Something a failed placement couldn't clean up, tried again hourly. The
    /// first time it is also reported: a problem for the annex's owners.
    async fn clean_leftover(&self, p: &Placement) {
        let still = self.cleanup(p);
        let conn = self.db.conn();
        match still {
            None => {
                let _ = conn.execute(
                    "UPDATE placement SET leftover = NULL WHERE placement_id = ?1",
                    [p.placement_id as i64],
                );
            }
            Some(detail) => {
                let _ = self
                    .step(p, json!({"step": "leftover", "detail": detail}))
                    .await;
                let _ = conn.execute(
                    "UPDATE placement SET leftover = ?2, next_at = ?3 WHERE placement_id = ?1",
                    params![p.placement_id as i64, detail, self.now + STUCK_RETRY_MS],
                );
            }
        }
    }

    /// An owner problem: the step it was at is kept, and tried again hourly.
    fn stuck(&self, p: &Placement, reason: &str) {
        error!(
            placement = p.placement_id,
            reason, "ALERT placement stuck: an owner looks"
        );
        let from = if p.state == "stuck" {
            p.stuck_from.clone().unwrap_or_else(|| "ticketed".into())
        } else {
            p.state.clone()
        };
        let _ = self.set(
            p,
            "stuck",
            &[
                ("reason", text(reason)),
                ("stuck_from", text(from)),
                ("unreported", text("stuck")),
                ("next_at", int(self.now + STUCK_RETRY_MS)),
            ],
        );
    }

    /// The hourly look at a stuck placement: the service may have ended it
    /// (cancelled, say), and then what it made goes. Otherwise it goes on from
    /// its real step, and sticks again if nothing changed.
    async fn unstuck(&self, p: &Placement) -> Flow {
        let ans = self.step(p, stuck_body(p)).await;
        if let Some(a) = ans
            && matches!(a["state"].as_str(), Some("failed" | "cancelled" | "placed"))
        {
            // Ended with the file under its name: nothing will finish it, so an
            // owner hears what it was left like.
            if let Some(path) = self.under_its_name(p) {
                let detail = format!(
                    "{path} is under its name, and the step that sets its owner didn't finish: \
                     the placement account may still own it"
                );
                error!(
                    placement = p.placement_id,
                    path, "ALERT placement ended with a file placed"
                );
                let _ = self
                    .step(p, json!({"step": "leftover", "detail": detail}))
                    .await;
            }
            let clean = self.cleanup(p);
            let _ = self.set(
                p,
                "failed",
                &[
                    ("reason", text(a["state"].as_str().unwrap_or("ended"))),
                    (
                        "leftover",
                        clean.map_or(rusqlite::types::Value::Null, rusqlite::types::Value::Text),
                    ),
                    ("next_at", int(self.now + STUCK_RETRY_MS)),
                ],
            );
            return Flow::Stop;
        }
        let from = p.stuck_from.clone().unwrap_or_else(|| "ticketed".into());
        if !self.set(
            p,
            &from,
            &[
                ("reason", rusqlite::types::Value::Null),
                ("stuck_from", rusqlite::types::Value::Null),
            ],
        ) {
            return Flow::Stop;
        }
        // The hourly try is one try: the same trouble is stuck again at once,
        // not another ten minutes of backing off with the mount up.
        let _ = self.db.conn().execute(
            "UPDATE placement SET attempts = ?2 WHERE placement_id = ?1",
            params![p.placement_id as i64, MAX_ATTEMPTS - 1],
        );
        Flow::Next
    }

    /// The file's path, if this placement's file is under its final name.
    fn under_its_name(&self, p: &Placement) -> Option<String> {
        let name = p.final_name.as_deref()?;
        let ino = p.staging_ino?;
        let t = self.ticket(p).ok()?;
        let dir = self.pl.sub(&t.folder).ok()?;
        let st = imp::stat_name(&dir, name).ok()?;
        (st.ino == ino).then(|| format!("{}/{name}", t.folder))
    }

    /// A trouble that might clear: tried again with a backoff, up to a bound, and
    /// then `stuck`, which alerts, reports it and asks the service hourly, so a
    /// cancel or an abandon is seen. `stuck` keeps the bytes. Only a definite
    /// refusal (`fail`), a cancel or an abandon deletes staged bytes; an outage,
    /// of the service, Blob storage or the NAS, never does, however few there are.
    fn retry_later(&self, p: &Placement, why: &str) {
        let n = p.attempts + 1;
        if n >= MAX_ATTEMPTS {
            self.stuck(p, &format!("trouble: {why}"));
            return;
        }
        warn!(
            placement = p.placement_id,
            why,
            attempt = n,
            "placement: again later"
        );
        if !retried(self.db, p, n, self.now + (BACKOFF_MS << n.min(6)), why) {
            self.journal_failed.set(true);
        }
    }

    /// P7: only names the journal owns, on their inodes. What it couldn't do
    /// (the NAS away), for trying again; a name that isn't the journal's file
    /// stays as it is, and isn't that.
    fn cleanup(&self, p: &Placement) -> Option<String> {
        let mut left: Vec<String> = Vec::new();
        // A3.4: an old file this placement hid goes back first, under its own name
        // or kept aside; never deleted here.
        if let Some(l) = self.unwind_old(p) {
            left.push(l);
        }
        #[cfg(test)]
        if faults::CLEANUP_FAILS.with(|f| f.get()) {
            return Some("cleanup: the NAS is away (test fault)".into());
        }
        let staging = self.staging_rel();
        match self.staging_root() {
            Ok(d) => {
                if let Some(ino) = p.staging_ino {
                    match imp::stat_name(&d, &p.staging_name) {
                        // One link, or the second name of a file P4 linked.
                        Ok(st)
                            if st.ino == ino
                                && (st.nlink == 1
                                    || p.final_ino == Some(ino)
                                    || self.under_its_name(p).is_some()) =>
                        {
                            if let Err(e) = imp::unlink(&d, &p.staging_name) {
                                warn!(placement = p.placement_id, error = %e, "P7: staging file kept");
                                left.push(format!("{staging}/{}: {e}", p.staging_name));
                            }
                        }
                        Ok(_) => warn!(
                            placement = p.placement_id,
                            "P7: the staging name isn't the journal's file; left as it is"
                        ),
                        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                        Err(e) => left.push(format!("{staging}/{}: {e}", p.staging_name)),
                    }
                }
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => {
                if p.staging_ino.is_some() {
                    left.push(format!("{staging}: {e}"));
                }
            }
        }
        if let Some((ino, ctime)) = p.placeholder
            && let (Ok(t), Some(name)) = (self.ticket(p), &p.final_name)
        {
            match away("cleanup_placeholder", || {
                self.pl
                    .sub(&t.folder)
                    .and_then(|d| imp::placeholder_intact(&d, name, ino, ctime).map(|i| (d, i)))
            }) {
                Ok((d, true)) => {
                    if let Err(e) = imp::unlink(&d, name)
                        && e.kind() != io::ErrorKind::NotFound
                    {
                        left.push(format!("{}/{name}: {e}", t.folder));
                    }
                }
                Ok((_, false)) => {}
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => left.push(format!("{}/{name}: {e}", t.folder)),
            }
        }
        let made: Vec<(String, u64)> =
            serde_json::from_str::<Vec<(String, u64)>>(&p.made).unwrap_or_default();
        for (path, ino) in made.iter().rev() {
            let (parent, name) = super::remover::split(path);
            match self
                .pl
                .sub(parent)
                .and_then(|d| imp::stat_name(&d, name).map(|st| (d, st)))
            {
                Ok((d, st)) if st.ino == *ino && st.kind == Kind::Dir => {
                    // Refused by the server unless empty.
                    match imp::rmdir(&d, name) {
                        Ok(()) => {}
                        Err(e) if matches!(errno(&e), libc::ENOTEMPTY | libc::EEXIST) => {}
                        Err(e) => left.push(format!("{path}: {e}")),
                    }
                }
                Ok(_) => {}
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => left.push(format!("{path}: {e}")),
            }
        }
        (!left.is_empty()).then(|| left.join("; "))
    }

    // --- the steps -----------------------------------------------------------------------

    async fn drive(&self, mut p: Placement) {
        let t = match self.ticket(&p) {
            Ok(t) => t,
            Err(e) => return self.fail(&p, &format!("bad_ticket: {e}")),
        };
        loop {
            let flow = match p.state.as_str() {
                "ticketed" => self.space_and_folders(&p, &t),
                "create" | "folders_ready" => self.create(&p, &t),
                "created" | "filling" => self.fill(&p, &t).await,
                "filled" => self.secure(&p, &t).await,
                "secured" => self.go_and_link(&p, &t).await,
                "linking" | "placeholder" | "placeholder_make" => self.resume_link(&p, &t).await,
                // A3.4: a replace at the origin (placer/replace.rs).
                "replace_start" => self.replace_begin(&p, &t).await,
                "replace_hiding" => self.resume_hiding(&p, &t).await,
                "replace_hidden" => self.after_grace(&p, &t).await,
                "replace_putting_back" => self.resume_put_back(&p, &t),
                "replace_putting_aside" | "replace_deleting" => self.finish_old(&p, &t).await,
                "linked" if self.replace_pending(&p) => self.finish_old(&p, &t).await,
                "linked" => self.own(&p, &t),
                "owned" => self.pointer(&p, &t),
                "stuck" => self.unstuck(&p).await,
                _ => Flow::Stop,
            };
            if flow == Flow::Stop {
                return;
            }
            match self.reload(&p) {
                Some(n) if n.state != p.state => p = n,
                _ => return,
            }
        }
    }

    /// The space check, and P0 for a restore folder's missing folders.
    fn space_and_folders(&self, p: &Placement, t: &Ticket) -> Flow {
        if self.staging_or_wait(p).is_none() {
            return Flow::Stop;
        }
        let parts: Vec<&str> = t.folder.split('/').filter(|s| !s.is_empty()).collect();
        let mut made: Vec<(String, u64)> = serde_json::from_str(&p.made).unwrap_or_default();
        let mut cur = String::new();
        for (i, part) in parts.iter().enumerate() {
            let next = if cur.is_empty() {
                (*part).to_owned()
            } else {
                format!("{cur}/{part}")
            };
            match self.pl.stat(&next, true) {
                Ok(st) if st.kind == Kind::Dir => {
                    // A crash right after our `mkdir`: only a folder that can
                    // only be ours (the placement account's, as made, empty) is
                    // taken on. Anything else is judged by `go` as it is.
                    let pending = p.made_pending.as_deref() == Some(next.as_str())
                        && !made.iter().any(|(m, _)| *m == next);
                    // An outage isn't "not ours": that 0700 folder would make `go`
                    // refuse a whole copy.
                    let ours = if pending {
                        match self.is_our_new_folder(p.attempts, &next, &st) {
                            Ok(ours) => ours,
                            Err(e) => {
                                self.retry_later(p, &format!("P0: the new folder: {e}"));
                                return Flow::Stop;
                            }
                        }
                    } else {
                        false
                    };
                    if ours {
                        let d = match self.pl.sub(&cur) {
                            Ok(d) => d,
                            Err(e) => {
                                self.retry_later(p, &format!("P0: the parent: {e}"));
                                return Flow::Stop;
                            }
                        };
                        if !self.parents_acl(p, &d, part, &next) {
                            return Flow::Stop;
                        }
                        made.push((next.clone(), st.ino));
                        if !self.set(
                            p,
                            "ticketed",
                            &[(
                                "made",
                                text(serde_json::to_string(&made).unwrap_or_default()),
                            )],
                        ) {
                            return Flow::Stop;
                        }
                    }
                }
                Ok(_) => {
                    self.fail(p, "target_missing");
                    return Flow::Stop;
                }
                Err(e) if e.kind() == io::ErrorKind::NotFound => {
                    if t.origin || i == 0 {
                        // The origin folder, or the location's own, never made.
                        self.fail(p, "target_missing");
                        return Flow::Stop;
                    }
                    let d = match away("p0_parent", || self.pl.sub(&cur)) {
                        Ok(d) => d,
                        Err(e) if e.kind() == io::ErrorKind::NotFound => {
                            self.fail(p, "target_missing");
                            return Flow::Stop;
                        }
                        Err(e) => {
                            self.retry_later(p, &format!("P0: the parent: {e}"));
                            return Flow::Stop;
                        }
                    };
                    if !self.set(
                        p,
                        "ticketed",
                        &[
                            (
                                "made",
                                text(serde_json::to_string(&made).unwrap_or_default()),
                            ),
                            ("made_pending", text(next.clone())),
                        ],
                    ) {
                        return Flow::Stop;
                    }
                    if let Err(e) = imp::mkdir(&d, part) {
                        if errno(&e) != libc::EEXIST {
                            return self.space_error(p, &e, "P0");
                        }
                    } else if self.crash_point("p0_made") == Flow::Stop {
                        return Flow::Stop;
                    } else {
                        // `made_pending` takes it from here if this can't.
                        let st = match away("p0_stat", || imp::stat_name(&d, part)) {
                            Ok(st) => st,
                            Err(e) => {
                                self.retry_later(p, &format!("P0 stat: {e}"));
                                return Flow::Stop;
                            }
                        };
                        // Its parent's ACL (decided, question 4): the requester
                        // may add a file here because they may in the parent.
                        if !self.parents_acl(p, &d, part, &next) {
                            return Flow::Stop;
                        }
                        made.push((next.clone(), st.ino));
                        if !self.set(
                            p,
                            "ticketed",
                            &[(
                                "made",
                                text(serde_json::to_string(&made).unwrap_or_default()),
                            )],
                        ) {
                            return Flow::Stop;
                        }
                    }
                }
                Err(e) => {
                    self.retry_later(p, &format!("target folder: {e}"));
                    return Flow::Stop;
                }
            }
            cur = next;
        }
        // The space, as the placement account sees it: the file plus 5%.
        let need = t.size + t.size / 20;
        match self
            .pl
            .open_dir(&t.folder)
            .and_then(|(f, _)| super::folders::space(&f))
        {
            Ok((free, _)) if free < need => {
                self.fail(p, "no_space");
                return Flow::Stop;
            }
            Ok(_) => {}
            Err(e) => {
                self.retry_later(p, &format!("space: {e}"));
                return Flow::Stop;
            }
        }
        if !self.set(p, "folders_ready", &[]) {
            return Flow::Stop;
        }
        self.crash_point("folders_ready")
    }

    /// A new folder gets its parent's ACL. If the NAS refuses, `go` judges the
    /// folder as it is. If the NAS is away, that's tried again. `false`: stop here.
    fn parents_acl(&self, p: &Placement, parent: &Root, part: &str, folder: &str) -> bool {
        match away("inherit", || {
            imp::inherit_folder(parent, part, self.flavour)
        }) {
            Ok(()) => true,
            Err(e) if definite(&e) => {
                warn!(placement = p.placement_id, folder, error = %e,
                      "P0: the parent's ACL couldn't be given to a new folder");
                true
            }
            Err(e) => {
                self.retry_later(p, &format!("P0: the parent's ACL: {e}"));
                false
            }
        }
    }

    /// A folder a crashed P0 made: over NFS the placement account's, mode 0700
    /// and empty; over SMB (where the mode is the client's) the placement
    /// account's by its descriptor, and empty. A user can't make either.
    fn is_our_new_folder(
        &self,
        attempts: i64,
        rel: &str,
        st: &super::nasfs::Stat,
    ) -> io::Result<bool> {
        // What the placement account can't look into it didn't make: an outage says
        // nothing, and a refusal says so once the bound's tries are up, since a NAS's id
        // map can refuse for a moment, as at P5. Taken as not ours too early, a whole copy
        // goes at `go` and an empty folder stays on the NAS.
        let refused = |e: &io::Error| {
            matches!(errno(e), libc::EACCES | libc::EPERM) && attempts + 1 >= MAX_ATTEMPTS
        };
        match away("our_folder", || imp::dir_has_entries(self.pl, rel)) {
            Ok(true) => return Ok(false),
            Ok(false) => {}
            Err(e) if refused(&e) => return Ok(false),
            Err(e) => return Err(e),
        }
        if self.flavour == Flavour::Smb {
            let (f, _) = match self.pl.open_dir(rel) {
                Ok(v) => v,
                Err(e) if refused(&e) => return Ok(false),
                Err(e) => return Err(e),
            };
            return Ok(imp::sd_owner_is(&f, self.placement_sid.as_deref()));
        }
        Ok(st.mode & 0o7777 == 0o700
            && Some(st.uid.to_string()) == self.db.meta("staging_uid").ok().flatten())
    }

    fn space_error(&self, p: &Placement, e: &io::Error, at: &str) -> Flow {
        match errno(e) {
            libc::ENOSPC => self.fail(p, "no_space"),
            libc::EDQUOT => self.fail(p, "quota"),
            libc::EACCES | libc::EPERM => self.fail(p, "access_denied"),
            _ => self.retry_later(p, &format!("{at}: {e}")),
        }
        Flow::Stop
    }

    /// A write to a whole staged copy that failed. Not as in the fill, where a
    /// full disk ends it with little lost: that can clear, and only a refusal
    /// deletes the copy.
    fn whole_copy_error(&self, p: &Placement, e: &io::Error, at: &str) -> Flow {
        if matches!(errno(e), libc::EACCES | libc::EPERM) {
            self.fail(p, "access_denied");
        } else {
            self.retry_later(p, &format!("{at}: {e}"));
        }
        Flow::Stop
    }

    /// P1: the staging file, made `O_EXCL` in the staging folder, then locked
    /// to the two accounts and read back before a byte is written. Nothing is
    /// taken from what it inherited.
    fn create(&self, p: &Placement, _t: &Ticket) -> Flow {
        let Some(d) = self.staging_or_wait(p) else {
            return Flow::Stop;
        };
        if p.state == "create" {
            // A crash between the create and its journal (R7): only an empty,
            // single-link file of ours is ours.
            match imp::stat_name(&d, &p.staging_name) {
                Ok(st) => {
                    let ours = st.kind == Kind::File
                        && st.size == 0
                        && st.nlink == 1
                        && Some(st.uid.to_string()) == self.db.meta("staging_uid").ok().flatten();
                    if !ours {
                        self.stuck(p, "staging_name_not_ours");
                        return Flow::Stop;
                    }
                    return match imp::open_at(&d, &p.staging_name, true) {
                        Ok(f) => self.lock_and_journal(p, &f),
                        Err(e) => {
                            self.retry_later(p, &format!("staging: {e}"));
                            Flow::Stop
                        }
                    };
                }
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => {
                    self.retry_later(p, &format!("staging: {e}"));
                    return Flow::Stop;
                }
            }
        }
        if !self.set(p, "create", &[]) {
            return Flow::Stop;
        }
        if self.crash_point("create") == Flow::Stop {
            return Flow::Stop;
        }
        let f = match imp::create_excl(&d, &p.staging_name) {
            Ok(f) => f,
            Err(e) => return self.space_error(p, &e, "P1"),
        };
        self.lock_and_journal(p, &f)
    }

    /// The file's own lock, read back; only then is it `created`. Its inode is
    /// journalled first, so that a failure here can clean it up.
    fn lock_and_journal(&self, p: &Placement, f: &std::fs::File) -> Flow {
        match imp::fstat(f) {
            Ok(st) => {
                if !self.set(p, "create", &[("staging_ino", int(st.ino as i64))]) {
                    return Flow::Stop;
                }
            }
            Err(e) => {
                self.retry_later(p, &format!("P1 fstat: {e}"));
                return Flow::Stop;
            }
        }
        let locked = imp::lock_down(
            f,
            self.flavour,
            self.placement_sid.as_deref(),
            self.read_sid.as_deref(),
            self.read_who.as_deref(),
            false,
        );
        #[cfg(test)]
        let locked = if faults::LOCK_EIO.with(|x| x.get()) {
            Err(io::Error::from_raw_os_error(libc::EIO))
        } else {
            locked
        };
        if let Err(e) = locked {
            if !definite(&e) {
                self.retry_later(p, &format!("P1 lock: {e}"));
                return Flow::Stop;
            }
            warn!(placement = p.placement_id, error = %e, "P1: the staging file can't be locked");
            self.fail(p, "staging_file_not_locked");
            return Flow::Stop;
        }
        #[cfg(test)]
        if faults::BORN_WIDE.with(|x| x.replace(false)) {
            use std::os::unix::fs::PermissionsExt;
            let _ = f.set_permissions(std::fs::Permissions::from_mode(0o644));
        }
        let st = match imp::fstat(f) {
            Ok(s) => s,
            Err(e) => {
                self.retry_later(p, &format!("P1 fstat: {e}"));
                return Flow::Stop;
            }
        };
        // A read that fails isn't a lock that failed.
        let acl = match acl::capture_on(f, self.flavour, &st, self.ids) {
            Ok(a) => a,
            Err(e) => {
                self.retry_later(p, &format!("P1 read of the lock: {e}"));
                return Flow::Stop;
            }
        };
        let locked = imp::locked(
            &acl,
            self.flavour,
            self.placement_sid.as_deref(),
            self.read_sid.as_deref(),
            self.read_who.as_deref(),
        );
        if !locked {
            warn!(
                placement = p.placement_id,
                "P1: the staging file's ACL read back isn't the lock"
            );
            self.fail(p, "staging_file_not_locked");
            return Flow::Stop;
        }
        match acl::raw_print(f, self.flavour, &st) {
            Ok(print) => self.created(p, st.ino, &print),
            Err(e) => {
                self.retry_later(p, &format!("P1 ACL: {e}"));
                Flow::Stop
            }
        }
    }

    fn created(&self, p: &Placement, ino: u64, print: &str) -> Flow {
        if !self.set(
            p,
            "created",
            &[
                ("staging_ino", int(ino as i64)),
                ("staging_acl", text(print)),
                ("filled_to", int(0)),
            ],
        ) {
            return Flow::Stop;
        }
        self.crash_point("created")
    }

    /// The staging file reopened: the journal's inode, one link, and the ACL
    /// it was born with (R5), never by ctime, which every write moves. Writing
    /// only for the fill: P3 on needs no write access to the bytes.
    fn reopen(
        &self,
        p: &Placement,
        d: &Root,
        want_acl: Option<&str>,
        write: bool,
    ) -> Result<std::fs::File, String> {
        let f = imp::open_at(d, &p.staging_name, write).map_err(|e| {
            if e.kind() == io::ErrorKind::NotFound {
                "staging_gone".to_owned()
            } else {
                format!("reopen: {e}")
            }
        })?;
        let st = imp::fstat(&f).map_err(|e| e.to_string())?;
        if Some(st.ino) != p.staging_ino || st.nlink != 1 || st.kind != Kind::File {
            return Err("staging_file_changed".into());
        }
        if let Some(want) = want_acl {
            let print = acl::raw_print(&f, self.flavour, &st).map_err(|e| e.to_string())?;
            if print != want {
                return Err("staging_file_changed".into());
            }
        }
        Ok(f)
    }

    /// A new read credential for this placement, journalled, or None.
    async fn renew_read(&self, p: &Placement) -> Option<(Value, Option<i64>)> {
        let in_hand = self.journal().in_hand().unwrap_or_default();
        let ans = self
            .svc
            .placement_tickets(&json!({
                "annex_id": self.annex_id.to_string(),
                "in_hand": in_hand.iter().map(u64::to_string).collect::<Vec<_>>(),
                "renew": [p.placement_id.to_string()],
            }))
            .await
            .ok()?;
        let fresh = ans["tickets"]
            .as_array()?
            .iter()
            .find(|t| id(&t["placement_id"]) == Some(p.placement_id))?
            .clone();
        if let Err(e) = self.journal().renew_read(p.placement_id, &fresh) {
            error!(placement = p.placement_id, error = %e, "annex.db: a new credential not journalled");
            return None;
        }
        let t = Ticket::from_json(&fresh).ok()?;
        Some((t.read, t.read_until_ms))
    }

    /// P2: the bytes, from the version's ranged read.
    async fn fill(&self, p: &Placement, t: &Ticket) -> Flow {
        let Some(d) = self.staging_or_wait(p) else {
            return Flow::Stop;
        };
        let f = match self.reopen(
            p,
            &d,
            p.staging_acl.as_deref().filter(|s| !s.is_empty()),
            true,
        ) {
            Ok(f) => f,
            Err(e) if e == "staging_file_changed" || e == "staging_gone" => {
                self.fail(p, &e);
                return Flow::Stop;
            }
            Err(e) => {
                self.retry_later(p, &e);
                return Flow::Stop;
            }
        };
        let mut at = p.filled_to;
        if let Err(e) = f.set_len(at) {
            return self.space_error(p, &e, "P2 truncate");
        }
        if !self.set(p, "filling", &[]) {
            return Flow::Stop;
        }
        let mut since = 0u64;
        let mut read = t.read.clone();
        let mut until = t.read_until_ms;
        while at < t.size {
            let len = CHUNK.min(t.size - at);
            // The credential is good for less than a long fill takes.
            if until.is_some_and(|u| super::now_ms() + RENEW_BEFORE_MS >= u)
                && let Some((r, u)) = self.renew_read(p).await
            {
                read = r;
                until = u;
            }
            let bytes = match self.svc.read_range(&read, at, len).await {
                Ok(b) if b.len() as u64 == len => b,
                Ok(b) => {
                    self.retry_later(p, &format!("short read: {} of {len}", b.len()));
                    return Flow::Stop;
                }
                // Refused, perhaps for a credential that ran out, or a 404 or 409,
                // which an archive-date move or an expired rehydrated copy can
                // give while the data is safe: once more, with a new credential,
                // from what has been written. Only then does it count.
                Err(e) if matches!(e.code.as_deref(), Some(READ_REFUSED) | Some(READ_DEFINITE)) => {
                    let again = match self.renew_read(p).await {
                        Some((r, u)) => {
                            read = r;
                            until = u;
                            Some(self.svc.read_range(&read, at, len).await)
                        }
                        None => None,
                    };
                    match again {
                        Some(Ok(b)) if b.len() as u64 == len => b,
                        Some(Err(e2)) if e2.code.as_deref() == Some(READ_DEFINITE) => {
                            warn!(placement = p.placement_id, error = %e2,
                                  "P2: the blob can't be read");
                            self.fail(p, READ_DEFINITE);
                            return Flow::Stop;
                        }
                        _ => {
                            self.retry_later(p, &format!("read: {e}"));
                            return Flow::Stop;
                        }
                    }
                }
                // Blob storage away, or a range that failed its CRC: fetched
                // again next time, from what has been written.
                Err(e) => {
                    self.retry_later(p, &format!("read: {e}"));
                    return Flow::Stop;
                }
            };
            #[cfg(test)]
            if faults::ENOSPC_AT.with(|x| x.get()) > 0
                && at + len >= faults::ENOSPC_AT.with(|x| x.get())
            {
                faults::ENOSPC_AT.with(|x| x.set(0));
                return self.space_error(p, &io::Error::from_raw_os_error(libc::ENOSPC), "P2");
            }
            #[cfg(test)]
            if faults::FILL_EIO.with(|x| x.get()) {
                return self.space_error(p, &io::Error::from_raw_os_error(libc::EIO), "P2");
            }
            if let Err(e) = imp::pwrite_all(&f, &bytes, at) {
                return self.space_error(p, &e, "P2");
            }
            at += len;
            // The exit test catches a fill in the act (`OMNIFILE_ANNEX_FILL_PAUSE_MS`).
            #[cfg(debug_assertions)]
            if let Some(ms) = std::env::var("OMNIFILE_ANNEX_FILL_PAUSE_MS")
                .ok()
                .and_then(|v| v.parse().ok())
            {
                std::thread::sleep(std::time::Duration::from_millis(ms));
            }
            since += len;
            if since >= FILL_JOURNAL_EVERY {
                since = 0;
                if let Err(e) = f.sync_data() {
                    return self.space_error(p, &e, "P2 sync");
                }
                if !self.set(p, "filling", &[("filled_to", int(at as i64))]) {
                    return Flow::Stop;
                }
                // Progress starts the count of tries again.
                let _ = self.db.conn().execute(
                    "UPDATE placement SET attempts = 0 WHERE placement_id = ?1",
                    [p.placement_id as i64],
                );
                if self.crash_point("filling") == Flow::Stop {
                    return Flow::Stop;
                }
            }
        }
        if let Err(e) = f.sync_all() {
            return self.space_error(p, &e, "P2 sync");
        }
        if !self.set(p, "filled", &[("filled_to", int(at as i64))]) {
            return Flow::Stop;
        }
        let _ = self.step(p, json!({"step": "filled"})).await;
        self.crash_point("filled")
    }

    /// The final owner and group, as the NAS's ids (NFS), for the staged
    /// ACL read as it will be after P5.
    fn final_ids(&self, t: &Ticket) -> Result<Option<FinalIds>, String> {
        // Over SMB the descriptor says it all: no ids and no lookup, so winbind
        // being away is no matter at P3, `go` or P5. The one test of it.
        if self.flavour == Flavour::Smb {
            return Ok(None);
        }
        // Over NFS the numeric ids are the NAS's own: put back exactly what
        // the file had (the copy saw them). Else the SIDs' ids through winbind:
        // a lookup that fails says nothing, and isn't read as "no such id".
        let sid = |k: &str| {
            t.acl
                .get(k)
                .and_then(Value::as_str)
                .and_then(|s| s.parse::<Sid>().ok())
        };
        let uid = match (t.uid, sid("owner_sid")) {
            (Some(u), _) => Some(u),
            (None, Some(s)) => self.ids.sid_to_uid(&s).map_err(|e| e.to_string())?,
            (None, None) => None,
        };
        let gid = match (t.gid, sid("group_sid")) {
            (Some(g), _) => Some(g),
            (None, Some(s)) => self.ids.sid_to_gid(&s).map_err(|e| e.to_string())?,
            (None, None) => None,
        };
        Ok(Some((uid, gid)))
    }

    /// P3, then the read-back through the read-only mount, then the times;
    /// then `secured` (R2).
    async fn secure(&self, p: &Placement, t: &Ticket) -> Flow {
        let Some(d) = self.staging_or_wait(p) else {
            return Flow::Stop;
        };
        let f = match self.reopen(p, &d, None, false) {
            Ok(f) => f,
            Err(e) if e == "staging_file_changed" || e == "staging_gone" => {
                self.fail(p, &e);
                return Flow::Stop;
            }
            Err(e) => {
                self.retry_later(p, &e);
                return Flow::Stop;
            }
        };
        // The group first, on NFS (review point 1 of the second round): the
        // placement account's primary group never holds the file under its
        // final name. Over SMB the group is the descriptor's, set with the
        // owner in P5: an NT ACL grants nothing to a file's group as such, and
        // no lookup is needed.
        let ids = match self.final_ids(t) {
            Ok(v) => v,
            Err(e) => {
                self.retry_later(p, &format!("P3: identity lookups fail: {e}"));
                return Flow::Stop;
            }
        };
        if let Some((_, gid)) = ids {
            match gid {
                Some(g) => {
                    #[cfg(test)]
                    let ignored = faults::P3_GROUP_IGNORED.with(|x| x.get());
                    #[cfg(not(test))]
                    let ignored = false;
                    if !ignored && let Err(e) = away("p3_group", || imp::fchown(&f, None, Some(g)))
                    {
                        return self.whole_copy_error(p, &e, "P3 group");
                    }
                }
                None => {
                    self.fail(p, "group_unmapped");
                    return Flow::Stop;
                }
            }
        }
        if let Err(e) = away("set_acl", || {
            imp::set_acl(&f, self.flavour, &t.acl, t.protect)
        }) {
            if !definite(&e) {
                self.retry_later(p, &format!("P3 ACL: {e}"));
                return Flow::Stop;
            }
            warn!(placement = p.placement_id, error = %e, "P3: the ACL can't be set");
            self.fail(p, "acl_unsupported");
            return Flow::Stop;
        }
        if self.crash_point("acl_set") == Flow::Stop {
            return Flow::Stop;
        }
        // The read-back: through the read-only mount, its own superblock and
        // connection, so the bytes are the server's.
        #[cfg(test)]
        if faults::CRC_FLIP.with(|x| x.replace(false))
            && let Ok(w) = imp::open_at(&d, &p.staging_name, true)
        {
            let _ = imp::pwrite_all(&w, b"\xff", 0);
            let _ = w.sync_all();
        }
        let rel = format!("{}/{}", self.staging_rel(), p.staging_name);
        let crc = imp::read_back(self.ro, &rel);
        #[cfg(test)]
        let crc = if faults::READBACK_EIO.with(|x| x.get()) {
            Err(io::Error::from_raw_os_error(libc::EIO))
        } else {
            crc
        };
        let crc = match crc {
            Ok(c) => c,
            Err(e) if matches!(errno(&e), libc::EACCES | libc::EPERM) => {
                self.fail(p, "readback_denied");
                return Flow::Stop;
            }
            Err(e) => {
                self.retry_later(p, &format!("read-back: {e}"));
                return Flow::Stop;
            }
        };
        if crc.0 != t.size || crc.1 != t.crc64 {
            warn!(
                placement = p.placement_id,
                got = format!("{:016x}", crc.1),
                want = format!("{:016x}", t.crc64),
                "read-back: CRC mismatch"
            );
            if p.read_backs >= 1 {
                self.fail(p, "crc_mismatch");
                return Flow::Stop;
            }
            // Once more from the start: a new staging file, and P0 again for
            // the folders the cleanup removes. Not before the old file has gone:
            // its name is about to leave the journal.
            if let Some(left) = self.cleanup(p) {
                self.retry_later(p, &format!("read-back cleanup: {left}"));
                return Flow::Stop;
            }
            let _ = self.set(
                p,
                "ticketed",
                &[
                    ("read_backs", int(p.read_backs + 1)),
                    ("staging_ino", rusqlite::types::Value::Null),
                    ("made", text("[]")),
                    (
                        "staging_name",
                        text(format!("{}-{}", token(), p.placement_id)),
                    ),
                ],
            );
            return Flow::Next;
        }
        let _ = self
            .step(
                p,
                json!({"step": "read_back", "crc64": format!("{:016x}", crc.1)}),
            )
            .await;
        if let Some(m) = t.mtime_ns
            && let Err(e) = away("p3_times", || imp::futimens(&f, m))
        {
            return self.whole_copy_error(p, &e, "P3 times");
        }
        let st = match imp::stat_name(&d, &p.staging_name) {
            Ok(s) => s,
            Err(e) => {
                self.retry_later(p, &format!("fstat: {e}"));
                return Flow::Stop;
            }
        };
        let print = match self.staged_print(&rel, &st) {
            Ok(v) => v,
            Err(e) => {
                self.retry_later(p, &format!("staged ACL: {e}"));
                return Flow::Stop;
            }
        };
        if !self.set(
            p,
            "secured",
            &[
                ("secured_ctime", int(st.ctime_ns)),
                ("secured_mtime", int(st.mtime_ns)),
                ("secured_acl", text(print)),
                ("read_crc", text(format!("{:016x}", crc.1))),
            ],
        ) {
            return Flow::Stop;
        }
        self.crash_point("secured")
    }

    /// The staged ACL, read through the read-only mount: its descriptor may
    /// give the placement account nothing, while the read-back has shown the
    /// read account can open it.
    fn staged_acl(&self, rel: &str, st: &super::nasfs::Stat) -> Result<Value, String> {
        let f = self.ro.open_file(rel).map_err(|e| e.to_string())?;
        // Going to `go`: a lookup that fails is no answer, not "no such principal".
        acl::capture_strict(&f, self.flavour, st, self.ids)
    }

    /// Whether this server shows POSIX ACLs on the staged file. One that hides
    /// them looks base-only, and its 0640 may be a hidden mask, so `go` won't take
    /// the mode for the whole ACL unless it does.
    fn staged_shows_acls(&self, rel: &str) -> bool {
        #[cfg(test)]
        if faults::ACLS_HIDDEN.with(|x| x.get()) {
            return false;
        }
        self.ro
            .open_file(rel)
            .is_ok_and(|f| acl::posix_acls_shown(&f))
    }

    /// What says the staged ACL hasn't changed: raw bytes and numeric ids, so a
    /// lookup that fails can't look like a change.
    fn staged_print(&self, rel: &str, st: &super::nasfs::Stat) -> Result<String, String> {
        #[cfg(test)]
        if faults::PRINT_DIFFERS.with(|x| x.replace(false)) {
            return Ok("another print".into());
        }
        let f = self.ro.open_file(rel).map_err(|e| e.to_string())?;
        acl::raw_print(&f, self.flavour, st)
    }

    /// `go`, the `statx` against `secured`, then P4. The way to P4 on a restart
    /// too: a staging file swapped meanwhile, or an approval hours old, never
    /// gets a name.
    async fn go_and_link(&self, p: &Placement, t: &Ticket) -> Flow {
        let Some(d) = self.staging_or_wait(p) else {
            return Flow::Stop;
        };
        let rel = format!("{}/{}", self.staging_rel(), p.staging_name);
        let st = match imp::stat_name(&d, &p.staging_name) {
            Ok(s) => s,
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                self.fail(p, "staging_gone");
                return Flow::Stop;
            }
            Err(e) => {
                self.retry_later(p, &format!("statx: {e}"));
                return Flow::Stop;
            }
        };
        // The staged ACL as it will be once the owner is the file's own. Its group
        // is what it really is now: over NFS P3 set it, and that is checked here
        // rather than assumed. Over SMB the descriptor says it all.
        let mut as_final = st.clone();
        let ids = match self.final_ids(t) {
            Ok(v) => v,
            Err(e) => {
                self.retry_later(p, &format!("go: identity lookups fail: {e}"));
                return Flow::Stop;
            }
        };
        if let Some((uid, gid)) = ids {
            if Some(st.gid) != gid {
                self.fail(p, "group_not_set");
                return Flow::Stop;
            }
            if let Some(u) = uid {
                as_final.uid = u;
            }
        }
        let staged = match self.staged_acl(&rel, &as_final) {
            Ok(v) => v,
            Err(e) => {
                self.retry_later(p, &format!("staged ACL: {e}"));
                return Flow::Stop;
            }
        };
        let folder = match self.ro.open_dir(&t.folder) {
            Ok((f, dst)) => match acl::capture_strict(&f, self.flavour, &dst, self.ids) {
                Ok(v) => v,
                Err(e) => {
                    self.retry_later(p, &format!("folder ACL: {e}"));
                    return Flow::Stop;
                }
            },
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                self.fail(p, "target_missing");
                return Flow::Stop;
            }
            Err(e) => {
                self.retry_later(p, &format!("folder: {e}"));
                return Flow::Stop;
            }
        };
        #[cfg(test)]
        if let Some(h) = faults::BEFORE_GO.with(|x| x.borrow_mut().take()) {
            h();
        }
        let from = p
            .final_name
            .as_ref()
            .and_then(|n| t.names.iter().position(|x| x == n))
            .unwrap_or(0);
        let ans = match self
            .ask(
                p,
                json!({"step": "go", "acl": staged, "folder_acl": folder,
                       "name": t.names[from], "crc64": p.read_crc,
                       "uid": as_final.uid, "gid": as_final.gid,
                       "posix_acls_shown": self.staged_shows_acls(&rel)}),
            )
            .await
        {
            Ok(a) => a,
            Err(e) => {
                self.retry_later(p, &format!("go: {e}"));
                return Flow::Stop;
            }
        };
        if !ans["proceed"].as_bool().unwrap_or(false) {
            let reason = ans["reason"].as_str().unwrap_or("refused").to_owned();
            if reason == "paused" {
                let _ = self.db.conn().execute(
                    "UPDATE placement SET next_at = ?2 WHERE placement_id = ?1",
                    params![p.placement_id as i64, self.now + PAUSE_RETRY_MS],
                );
                return Flow::Stop;
            }
            // The service failed it already: nothing to report, only to clean.
            let clean = self.cleanup(p);
            let _ = self.set(
                p,
                "failed",
                &[
                    ("reason", text(reason)),
                    (
                        "leftover",
                        clean.map_or(rusqlite::types::Value::Null, rusqlite::types::Value::Text),
                    ),
                    ("next_at", int(self.now + STUCK_RETRY_MS)),
                ],
            );
            return Flow::Stop;
        }
        // The statx against `secured`: the same inode, size, ctime, ACL and one link.
        let st = imp::stat_name(&d, &p.staging_name);
        #[cfg(test)]
        let st = if faults::GO_STATX_EIO.with(|x| x.replace(false)) {
            Err(io::Error::from_raw_os_error(libc::EIO))
        } else {
            st
        };
        let st = match st {
            Ok(s) => s,
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                self.fail(p, "staging_gone");
                return Flow::Stop;
            }
            Err(e) => {
                self.retry_later(p, &format!("statx: {e}"));
                return Flow::Stop;
            }
        };
        let print_now = match self.staged_print(&rel, &st) {
            Ok(v) => v,
            Err(e) => {
                self.retry_later(p, &format!("staged ACL: {e}"));
                return Flow::Stop;
            }
        };
        if Some(st.ino) != p.staging_ino
            || st.size != t.size
            || Some(st.ctime_ns) != p.secured_ctime
            || st.nlink != 1
            || Some(print_now) != p.secured_acl
        {
            self.fail(p, "staging_file_changed");
            return Flow::Stop;
        }
        if let Some((ino, ctime)) = p.placeholder
            && let Some(name) = &p.final_name
        {
            let Ok(target) = self.pl.sub(&t.folder) else {
                self.retry_later(p, "target");
                return Flow::Stop;
            };
            return self.over_placeholder(p, &d, &target, name, ino, ctime);
        }
        self.link(p, t, &d, from)
    }

    /// P4 over the names the ticket allows, from `from`.
    fn link(&self, p: &Placement, t: &Ticket, staging: &Root, from: usize) -> Flow {
        let target = match away("link_target", || self.pl.sub(&t.folder)) {
            Ok(d) => d,
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                self.fail(p, "target_missing");
                return Flow::Stop;
            }
            Err(e) => {
                self.retry_later(p, &format!("target: {e}"));
                return Flow::Stop;
            }
        };
        #[cfg(test)]
        if let Some(h) = faults::BEFORE_P4.with(|x| x.borrow_mut().take()) {
            h();
        }
        for (i, name) in t.names.iter().enumerate().skip(from) {
            if !self.set(p, "linking", &[("final_name", text(name.clone()))]) {
                return Flow::Stop;
            }
            if self.crash_point("linking") == Flow::Stop {
                return Flow::Stop;
            }
            match away("p4_link", || {
                imp::link_across(staging, &p.staging_name, &target, name)
            }) {
                Ok(()) => {
                    if self.crash_point("link_taken") == Flow::Stop {
                        return Flow::Stop;
                    }
                    let left = self.staging_name_gone(staging, p);
                    return self.linked(p, name, "link", left);
                }
                Err(e) if errno(&e) == libc::EEXIST => {
                    // The server took our link and the reply was lost (NFS has
                    // no replay cache to count on): the name is already this
                    // inode, and it must not get a second one. If that can't be
                    // told now, it isn't guessed.
                    match away("p4_exist_stat", || imp::stat_name(&target, name)) {
                        Ok(st) if Some(st.ino) == p.staging_ino => {
                            let left = self.staging_name_gone(staging, p);
                            return self.linked(p, name, "link", left);
                        }
                        Ok(_) => {}
                        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                        Err(e) => {
                            self.retry_later(p, &format!("P4 stat: {e}"));
                            return Flow::Stop;
                        }
                    }
                    // A3.4: the file at the own name may be the one to replace.
                    if self.replace_wanted(p, t, i, name) {
                        return Flow::Next;
                    }
                    let same = if i == 0 && t.same_content {
                        match self.same_content(t, name) {
                            Ok(same) => same,
                            Err(e) => {
                                self.retry_later(p, &format!("P4 same content: {e}"));
                                return Flow::Stop;
                            }
                        }
                    } else {
                        false
                    };
                    if same {
                        // What's there is that copy: `done` says what it saw.
                        let attrs = match imp::stat_name(&target, name) {
                            Ok(st) => json!({"nas_file_id": st.ino.to_string(), "size": st.size,
                                             "mtime_ns": st.mtime_ns, "ctime_ns": st.ctime_ns}),
                            Err(e) => {
                                self.retry_later(p, &format!("P4 stat: {e}"));
                                return Flow::Stop;
                            }
                        };
                        let clean = self.cleanup(p);
                        if !self.set(
                            p,
                            "owned",
                            &[
                                ("final_name", text(name.clone())),
                                ("outcome", text("already_there")),
                                ("attrs", text(attrs.to_string())),
                                (
                                    "leftover",
                                    clean.map_or(
                                        rusqlite::types::Value::Null,
                                        rusqlite::types::Value::Text,
                                    ),
                                ),
                            ],
                        ) {
                            return Flow::Stop;
                        }
                        // Its pointer, as for a file placed there.
                        let Some(q) = self.reload(p) else {
                            return Flow::Stop;
                        };
                        return self.pointer(&q, t);
                    }
                }
                Err(e) if imp::link_refused(&e) => {
                    return self.placeholder(p, t, staging, &target, i);
                }
                Err(e) if errno(&e) == libc::EXDEV => {
                    self.fail(p, "cross_volume");
                    return Flow::Stop;
                }
                Err(e) => return self.whole_copy_error(p, &e, "P4"),
            }
        }
        self.fail(p, "name_taken");
        Flow::Stop
    }

    /// P4a: where the server refuses the link, #61's placeholder, made at the
    /// name `O_EXCL`, checked again right before the rename over it. Its name
    /// is journalled first, so a crash before its inode is can't orphan it.
    fn placeholder(
        &self,
        p: &Placement,
        t: &Ticket,
        staging: &Root,
        target: &Root,
        from: usize,
    ) -> Flow {
        for name in t.names.iter().skip(from) {
            if !self.set(p, "placeholder_make", &[("final_name", text(name.clone()))]) {
                return Flow::Stop;
            }
            match imp::make_placeholder(target, name) {
                Ok((ino, ctime)) => {
                    if self.crash_point("placeholder_made") == Flow::Stop {
                        return Flow::Stop;
                    }
                    return self.placeholder_made(p, staging, target, name, ino, ctime);
                }
                Err(e) if errno(&e) == libc::EEXIST => continue,
                Err(e) => return self.whole_copy_error(p, &e, "P4a"),
            }
        }
        self.fail(p, "name_taken");
        Flow::Stop
    }

    fn placeholder_made(
        &self,
        p: &Placement,
        staging: &Root,
        target: &Root,
        name: &str,
        ino: u64,
        ctime: i64,
    ) -> Flow {
        if !self.set(
            p,
            "placeholder",
            &[
                ("final_name", text(name)),
                ("placeholder_ino", int(ino as i64)),
                ("placeholder_ctime", int(ctime)),
            ],
        ) {
            return Flow::Stop;
        }
        if self.crash_point("placeholder") == Flow::Stop {
            return Flow::Stop;
        }
        self.over_placeholder(p, staging, target, name, ino, ctime)
    }

    fn over_placeholder(
        &self,
        p: &Placement,
        staging: &Root,
        target: &Root,
        name: &str,
        ino: u64,
        ctime: i64,
    ) -> Flow {
        match imp::placeholder_intact(target, name, ino, ctime) {
            Ok(true) => {}
            Ok(false) => {
                self.stuck(p, "placeholder_changed");
                return Flow::Stop;
            }
            Err(e) => {
                self.retry_later(p, &format!("placeholder: {e}"));
                return Flow::Stop;
            }
        }
        match imp::rename_across(staging, &p.staging_name, target, name) {
            Ok(()) => self.linked(p, name, "placeholder", None),
            Err(e) => {
                self.retry_later(p, &format!("P4a rename: {e}"));
                Flow::Stop
            }
        }
    }

    /// The staging name, unlinked once the file has its own. If that fails its
    /// second link stays in the staging folder: `leftover`, tried again later,
    /// and the placement goes on, because the file is under its name.
    fn staging_name_gone(&self, staging: &Root, p: &Placement) -> Option<String> {
        #[cfg(test)]
        if faults::UNLINK_AFTER_LINK_EIO.with(|x| x.replace(false)) {
            return Some("the staging name: EIO (test fault)".into());
        }
        match imp::unlink(staging, &p.staging_name) {
            Ok(()) => None,
            Err(e) if e.kind() == io::ErrorKind::NotFound => None,
            Err(e) => {
                warn!(placement = p.placement_id, error = %e, "P4: a second link stays");
                Some(format!("{}: {e}", p.staging_name))
            }
        }
    }

    fn linked(&self, p: &Placement, name: &str, method: &str, left: Option<String>) -> Flow {
        let path = self.ticket(p).ok().map(|t| {
            let full = if t.folder.is_empty() {
                name.to_owned()
            } else {
                format!("{}/{name}", t.folder)
            };
            if self.annex_rel.is_empty() {
                full
            } else {
                full.strip_prefix(&format!("{}/", self.annex_rel))
                    .map(str::to_owned)
                    .unwrap_or_default()
            }
        });
        if !self.set(
            p,
            "linked",
            &[
                ("final_name", text(name)),
                ("final_ino", int(p.staging_ino.unwrap_or(0) as i64)),
                ("placed_path", text(path.unwrap_or_default())),
                (
                    "leftover",
                    left.map_or(rusqlite::types::Value::Null, rusqlite::types::Value::Text),
                ),
            ],
        ) {
            return Flow::Stop;
        }
        info!(
            placement = p.placement_id,
            name, method, "P4: placed under its name"
        );
        self.crash_point("linked")
    }

    /// After a restart in P4: the file under its final name already, or the
    /// whole way to P4 again, `go` included.
    async fn resume_link(&self, p: &Placement, t: &Ticket) -> Flow {
        let target = match self.pl.sub(&t.folder) {
            Ok(d) => d,
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                self.fail(p, "target_missing");
                return Flow::Stop;
            }
            Err(e) => {
                self.retry_later(p, &format!("target: {e}"));
                return Flow::Stop;
            }
        };
        let name = p.final_name.clone().unwrap_or_else(|| t.names[0].clone());
        let at_name = imp::stat_name(&target, &name);
        #[cfg(test)]
        let at_name = if faults::TARGET_STAT_EIO.with(|x| x.get()) > 0 {
            faults::TARGET_STAT_EIO.with(|x| x.set(x.get() - 1));
            Err(io::Error::from_raw_os_error(libc::EIO))
        } else {
            at_name
        };
        // Only "not found" means it isn't there: the link may have been taken,
        // and a failure here must not fail a file that is under its name.
        let at_name = match at_name {
            Ok(st) => Some(st),
            Err(e) if e.kind() == io::ErrorKind::NotFound => None,
            Err(e) => {
                self.retry_later(p, &format!("resume: {e}"));
                return Flow::Stop;
            }
        };
        // Under its name already (the link or the rename was taken).
        if let Some(st) = &at_name
            && Some(st.ino) == p.staging_ino
        {
            let left = match self.staging_root() {
                Ok(staging) => match imp::stat_name(&staging, &p.staging_name) {
                    Ok(s) if Some(s.ino) == p.staging_ino => self.staging_name_gone(&staging, p),
                    Ok(_) => None,
                    Err(e) if e.kind() == io::ErrorKind::NotFound => None,
                    Err(e) => Some(format!("{}: {e}", p.staging_name)),
                },
                Err(e) if e.kind() == io::ErrorKind::NotFound => None,
                Err(e) => Some(format!("{}: {e}", p.staging_name)),
            };
            return self.linked(p, &name, "resumed", left);
        }
        // A crash between making the placeholder and journalling it: the file
        // with exactly its text, one link, is ours. Anything else is a user's,
        // and the next name is tried.
        if p.state == "placeholder_make"
            && let Some(st) = &at_name
            && imp::is_placeholder(&target, &name, st)
        {
            if !self.set(
                p,
                "placeholder",
                &[
                    ("placeholder_ino", int(st.ino as i64)),
                    ("placeholder_ctime", int(st.ctime_ns)),
                ],
            ) {
                return Flow::Stop;
            }
            let Some(q) = self.reload(p) else {
                return Flow::Stop;
            };
            return self.go_and_link(&q, t).await;
        }
        self.go_and_link(p, t).await
    }

    /// P5: the owner (and over SMB the group), then what `done` will say,
    /// journalled. Only on an fd that is still what `secured` saw (its inode, size
    /// and mtime): a user who changed or replaced the file between the link and
    /// here keeps it as it is, and their setuid script never gets ours. Nothing
    /// fails after the link: the file stays under its name. A definite refusal
    /// leaves the placement account as owner, and `done` says so. A trouble that
    /// might clear is tried again, then `stuck`: it isn't `done` while the owner
    /// might yet be set.
    fn own(&self, p: &Placement, t: &Ticket) -> Flow {
        let Some(name) = p.final_name.clone() else {
            self.stuck(p, "no_final_name");
            return Flow::Stop;
        };
        let target = match self.pl.sub(&t.folder) {
            Ok(d) => d,
            // Moved away with its folder: nothing to set.
            Err(e) if e.kind() == io::ErrorKind::NotFound => return self.owned(p, t, false, None),
            Err(e) => return self.own_later(p, &format!("target: {e}")),
        };
        #[cfg(test)]
        if let Some(h) = faults::BEFORE_P5_OPEN.with(|x| x.borrow_mut().take()) {
            h();
        }
        let opened = imp::open_at(&target, &name, false);
        #[cfg(test)]
        let opened = if faults::P5_REFUSED.with(|x| x.get()) {
            Err(io::Error::from_raw_os_error(libc::EACCES))
        } else if faults::P5_EIO.with(|x| x.get()) > 0 {
            faults::P5_EIO.with(|x| x.set(x.get() - 1));
            Err(io::Error::from_raw_os_error(libc::EIO))
        } else {
            opened
        };
        let is_copy = |st: &super::nasfs::Stat| {
            Some(st.ino) == p.final_ino && st.size == t.size && p.secured_mtime == Some(st.mtime_ns)
        };
        let attrs_of = |st: &super::nasfs::Stat| {
            is_copy(st).then(|| {
                json!({"nas_file_id": st.ino.to_string(), "size": st.size,
                       "mtime_ns": st.mtime_ns, "ctime_ns": st.ctime_ns})
            })
        };
        let f = match opened {
            Ok(f) => f,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return self.owned(p, t, false, None),
            Err(e) if matches!(errno(&e), libc::EACCES | libc::EPERM) => {
                // Not ours to open now: nothing is changed, and what's there is
                // still described if it can be.
                warn!(placement = p.placement_id, error = %e,
                      "P5: the owner isn't set; the placement account keeps it");
                return match imp::stat_name(&target, &name) {
                    Ok(st) => self.owned(p, t, false, attrs_of(&st)),
                    Err(e) if e.kind() == io::ErrorKind::NotFound => self.owned(p, t, false, None),
                    Err(e) => self.own_later(p, &format!("P5 stat: {e}")),
                };
            }
            Err(e) => return self.own_later(p, &format!("P5 open: {e}")),
        };
        let st = match imp::fstat(&f) {
            Ok(st) => st,
            Err(e) => return self.own_later(p, &format!("P5 fstat: {e}")),
        };
        if !is_copy(&st) {
            // Changed or replaced since `secured`: a user's now, not ours to touch.
            warn!(
                placement = p.placement_id,
                "P5: the file isn't what was secured; nothing is set on it"
            );
            return self.owned(p, t, false, None);
        }
        let ids = match self.final_ids(t) {
            Ok(v) => v,
            Err(e) => return self.own_later(p, &format!("P5: identity lookups fail: {e}")),
        };
        #[cfg(test)]
        faults::P5_OWNER_SETS.with(|x| x.set(x.get() + 1));
        let res = away("p5_owner", || match ids {
            None => imp::set_owner_sd(&f, &t.acl, t.protect),
            Some((Some(u), gid)) => imp::fchown(&f, Some(u), gid),
            Some((None, _)) => Err(io::Error::from_raw_os_error(libc::EINVAL)),
        });
        #[cfg(test)]
        let res = if faults::P5_SET_EIO.with(|x| x.get()) {
            Err(io::Error::from_raw_os_error(libc::EIO))
        } else if faults::P5_SET_REFUSED.with(|x| x.get()) {
            Err(io::Error::from_raw_os_error(libc::EPERM))
        } else {
            res
        };
        let restored = match res {
            Ok(()) => true,
            // A NAS's id map can refuse for a moment, and this one leaves the owner
            // wrong for good: the bound's tries first, as for other trouble here.
            Err(e)
                if matches!(errno(&e), libc::EACCES | libc::EPERM)
                    && p.attempts + 1 < MAX_ATTEMPTS =>
            {
                return self.own_later(p, &format!("P5 owner: {e}"));
            }
            Err(e) if definite(&e) => {
                warn!(placement = p.placement_id, error = %e,
                      "P5: the owner isn't set; the placement account keeps it");
                false
            }
            Err(e) => return self.own_later(p, &format!("P5 owner: {e}")),
        };
        // A chown clears setuid and setgid, so they come back after it, and only
        // onto the same bytes, with the file's own owner in place.
        #[cfg(test)]
        if let Some(h) = faults::BEFORE_P5_RESTORE.with(|x| x.borrow_mut().take()) {
            h();
        }
        let st = match imp::fstat(&f) {
            Ok(st) => st,
            Err(e) => return self.own_later(p, &format!("P5 fstat: {e}")),
        };
        if restored
            && is_copy(&st)
            && let Err(e) = imp::restore_mode_bits(&f, self.flavour, &t.acl)
        {
            if !definite(&e) {
                return self.own_later(p, &format!("P5 setuid: {e}"));
            }
            warn!(placement = p.placement_id, error = %e, "P5: setuid and setgid aren't restored");
        }
        let st = match imp::fstat(&f) {
            Ok(st) => st,
            Err(e) => return self.own_later(p, &format!("P5 fstat: {e}")),
        };
        self.owned(p, t, restored, attrs_of(&st))
    }

    /// A trouble at P5 that might clear. It never fails: the file is under its name.
    fn own_later(&self, p: &Placement, why: &str) -> Flow {
        self.retry_later(p, why);
        Flow::Stop
    }

    fn owned(&self, p: &Placement, t: &Ticket, restored: bool, attrs: Option<Value>) -> Flow {
        let own = p.final_name.as_deref() == Some(t.name.as_str());
        let outcome = match (own, self.replace_outcome(p)) {
            (true, Some(o)) => o,
            (true, None) => "placed",
            (false, _) => "restored_name",
        };
        if !self.set(
            p,
            "owned",
            &[
                ("owner_restored", int(restored as i64)),
                ("attrs", text(attrs.unwrap_or(Value::Null).to_string())),
                ("outcome", text(outcome)),
            ],
        ) {
            return Flow::Stop;
        }
        self.crash_point("owned")
    }

    /// P6: back under its own name where it came from, its pointer goes, only
    /// on its recorded inode and text hash (or an older pointer's exact text).
    /// A note a user edited is left alone. It is moved into the staging folder
    /// first, where nobody else can touch it, and checked there, so a swap
    /// after the look never deletes a user's file: that goes back.
    fn pointer(&self, p: &Placement, t: &Ticket) -> Flow {
        // Not over a file a user replaced ours with (`attrs` says).
        let placed = p
            .attrs
            .as_deref()
            .is_some_and(|a| a != "null" && !a.is_empty());
        let deleted = if t.origin && placed && p.final_name.as_deref() == Some(t.name.as_str()) {
            let pname = format!("{}{}", t.name, super::remover::POINTER_SUFFIX);
            let gone = self.pl.sub(&t.folder).and_then(|target| {
                if self.pointer_is_ours(&target, &pname, t)? {
                    self.pointer_delete(p, &target, &pname, t)
                } else {
                    Ok(false)
                }
            });
            match gone {
                Ok(d) => d,
                Err(e) if e.kind() == io::ErrorKind::NotFound => false,
                // Only "not found" means it isn't there: the note stays for now.
                Err(e) => {
                    self.retry_later(p, &format!("P6: {e}"));
                    return Flow::Stop;
                }
            }
        } else {
            false
        };
        if !self.set(p, "owned", &[("pointer_deleted", int(deleted as i64))]) {
            return Flow::Stop;
        }
        self.done(p)
    }

    fn pointer_is_ours(&self, dir: &Root, name: &str, t: &Ticket) -> io::Result<bool> {
        #[cfg(test)]
        if faults::POINTER_EIO.with(|x| x.replace(false)) {
            return Err(io::Error::from_raw_os_error(libc::EIO));
        }
        let st = match imp::stat_name(dir, name) {
            Ok(st) => st,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
            Err(e) => return Err(e),
        };
        if st.kind != Kind::File || st.nlink != 1 || st.size > 4096 {
            return Ok(false);
        }
        let body = match imp::read_small(dir, name) {
            Ok(b) => b,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
            // A note someone locked isn't one this account wrote.
            Err(e) if definite(&e) => return Ok(false),
            Err(e) => return Err(e),
        };
        let recorded: Option<(i64, String)> = self
            .db
            .conn()
            .query_row(
                "SELECT pointer_ino, pointer_sha FROM removal WHERE path = ?1 \
                 AND pointer_ino IS NOT NULL ORDER BY removal_id DESC LIMIT 1",
                [&t.path],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(io::Error::other)?;
        Ok(match recorded {
            Some((ino, want)) => ino as u64 == st.ino && want == sha_hex(&body),
            None => is_pointer_text(&body, self.annex_name, &t.path, t.file_id),
        })
    }

    fn pointer_delete(
        &self,
        p: &Placement,
        target: &Root,
        pname: &str,
        t: &Ticket,
    ) -> io::Result<bool> {
        let staging = self.staging_root()?;
        let hidden = format!("{}-pointer-{}", token(), p.placement_id);
        #[cfg(test)]
        if let Some(h) = faults::BEFORE_P6.with(|x| x.borrow_mut().take()) {
            h();
        }
        if let Err(e) = away("p6_rename", || {
            imp::rename_across(target, pname, &staging, &hidden)
        }) {
            return if definite(&e) { Ok(false) } else { Err(e) };
        }
        // Whatever this says, it isn't in its place now: if it can't be told, it
        // goes back rather than being taken for ours.
        if self.pointer_is_ours(&staging, &hidden, t).unwrap_or(false) {
            return Ok(match imp::unlink(&staging, &hidden) {
                Ok(()) => true,
                Err(e) => {
                    error!(placement = p.placement_id, error = %e, hidden,
                           "P6: the pointer stays in the staging folder");
                    false
                }
            });
        }
        // Swapped after the look: a user's file, back where it was. Never over
        // whatever has that name by now.
        if let Err(e) = imp::link_across(&staging, &hidden, target, pname) {
            error!(placement = p.placement_id, error = %e, hidden,
                   "ALERT P6: a user's file is in the staging folder; an owner looks");
            return Ok(false);
        }
        if let Err(e) = imp::unlink(&staging, &hidden) {
            error!(placement = p.placement_id, error = %e, hidden,
                   "P6: a user's file put back, and a second link to it stays in the staging folder");
        }
        Ok(false)
    }

    fn done(&self, p: &Placement) -> Flow {
        if !self.set(p, "done", &[("unreported", text("done"))]) {
            return Flow::Stop;
        }
        Flow::Stop
    }

    /// Same size and CRC, read through the read-only mount.
    fn same_content(&self, t: &Ticket, name: &str) -> io::Result<bool> {
        let rel = if t.folder.is_empty() {
            name.to_owned()
        } else {
            format!("{}/{name}", t.folder)
        };
        match away("same_content", || imp::read_back(self.ro, &rel)) {
            Ok((n, c)) => Ok(n == t.size && c == t.crc64),
            // Someone else's file, that this account can't read: not known the same.
            Err(e) if definite(&e) || e.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(e) => Err(e),
        }
    }
}

/// A retry's journal write. If it fails it says so: with no `next_at` written this
/// placement would be tried again at once, so the caller holds off a while.
fn retried(db: &AnnexDb, p: &Placement, attempts: i64, next_at: i64, why: &str) -> bool {
    #[cfg(test)]
    if faults::RETRY_NOT_RECORDED.with(|x| x.get()) {
        error!(
            placement = p.placement_id,
            why, "annex.db: a retry not journalled (test fault)"
        );
        return false;
    }
    match db.conn().execute(
        "UPDATE placement SET attempts = ?2, next_at = ?3 WHERE placement_id = ?1",
        params![p.placement_id as i64, attempts, next_at],
    ) {
        Ok(_) => true,
        Err(e) => {
            error!(placement = p.placement_id, why, error = %e, "annex.db: a retry not journalled");
            false
        }
    }
}

/// A stuck report says where it is, for an abandon to say what it left.
fn stuck_body(p: &Placement) -> Value {
    json!({"step": "stuck", "reason": p.reason.clone().unwrap_or_default(),
           "at": p.stuck_from, "name": p.final_name})
}

/// Send what was done and not yet acknowledged. Everything it says is in the
/// journal, so it needs no mount, and it goes first in a tick: `done` before
/// the events of a scan that met the file.
pub async fn report_pending<S: AnnexService>(
    svc: &S,
    db: &AnnexDb,
    p: &Placement,
    now: i64,
) -> bool {
    // A3.4's facts about the old file first: the service hears a hide before a done.
    if !replace::report(svc, db, p.placement_id, now).await {
        return false;
    }
    let Some(list) = p.unreported.clone() else {
        return true;
    };
    for s in list.split(',').filter(|s| !s.is_empty()) {
        let body = match s {
            "done" => {
                let attrs = p
                    .attrs
                    .as_deref()
                    .and_then(|a| serde_json::from_str::<Value>(a).ok())
                    .unwrap_or(Value::Null);
                json!({"step": "done", "outcome": p.outcome.clone().unwrap_or_default(),
                    "name": p.final_name, "owner_restored": p.owner_restored,
                    "pointer_deleted": p.pointer_deleted, "attrs": attrs})
            }
            "failed" => {
                json!({"step": "failed", "reason": p.reason.clone().unwrap_or_default()})
            }
            "stuck" => stuck_body(p),
            other => json!({"step": other}),
        };
        let ask = format!(
            "{}:{}:{}",
            body["step"].as_str().unwrap_or(""),
            now,
            p.attempts
        );
        match svc.placement_step(p.placement_id, &body, &ask).await {
            Err(e) => {
                warn!(placement = p.placement_id, error = %e, "placement: the service later");
                return false;
            }
            // Ended while it was trying: heard now, not at its next hourly ask.
            Ok(a)
                if s == "stuck"
                    && matches!(a["state"].as_str(), Some("failed" | "cancelled" | "placed")) =>
            {
                let res = db.conn().execute(
                    "UPDATE placement SET next_at = ?1 WHERE placement_id = ?2",
                    [now, p.placement_id as i64],
                );
                if let Err(e) = res {
                    error!(placement = p.placement_id, error = %e, "annex.db: placement journal");
                }
            }
            Ok(_) => {}
        }
    }
    let _ = db.conn().execute(
        "UPDATE placement SET unreported = NULL WHERE placement_id = ?1",
        [p.placement_id as i64],
    );
    true
}

/// Everything unreported, with or without the placement mount.
pub async fn report_all<S: AnnexService>(svc: &S, db: &AnnexDb, now: i64) {
    for p in Journal(db).open().unwrap_or_default() {
        if p.unreported.is_some() {
            report_pending(svc, db, &p, now).await;
        }
    }
}

/// The placement mount can't be had (a keytab, a share, no account for a Copy
/// annex). It is a trouble like any other: each placement due backs off, sticks
/// after the bound with an ALERT and a report, and then asks the service hourly,
/// so a cancel is heard. What it made stays until a mount can clean it.
pub async fn no_mount_pass<S: AnnexService>(svc: &S, db: &AnnexDb, now: i64, why: &str) {
    let j = Journal(db);
    for p in j.open().unwrap_or_default() {
        if p.next_at > now {
            continue;
        }
        if p.state == "done" || p.state == "failed" {
            // Something left to clean, and no mount to clean it with: hourly, or
            // it asks for a mount, with a kinit, on every tick.
            if p.leftover.is_some() {
                retried(db, &p, p.attempts, now + STUCK_RETRY_MS, why);
            }
            continue;
        }
        if p.state == "stuck" {
            let body = stuck_body(&p);
            let ask = format!("stuck:{now}:{}", p.attempts);
            let ended = svc
                .placement_step(p.placement_id, &body, &ask)
                .await
                .ok()
                .and_then(|a| a["state"].as_str().map(str::to_owned))
                .filter(|s| matches!(s.as_str(), "failed" | "cancelled" | "placed"));
            let res = match ended {
                Some(state) => j.set(
                    p.placement_id,
                    "failed",
                    &[
                        ("reason", text(state)),
                        (
                            "leftover",
                            if p.staging_ino.is_some()
                                || p.placeholder.is_some()
                                || replace::hidden_now(db, p.placement_id).is_some()
                                || !matches!(p.made.as_str(), "" | "[]")
                            {
                                text(format!("no placement mount: {why}"))
                            } else {
                                // Nothing was made, so nothing needs a mount.
                                rusqlite::types::Value::Null
                            },
                        ),
                        ("next_at", int(now + STUCK_RETRY_MS)),
                    ],
                    now,
                ),
                None => j.set(
                    p.placement_id,
                    "stuck",
                    &[("next_at", int(now + STUCK_RETRY_MS))],
                    now,
                ),
            };
            if let Err(e) = res {
                error!(placement = p.placement_id, error = %e, "annex.db: placement journal");
            }
            continue;
        }
        let n = p.attempts + 1;
        if n >= MAX_ATTEMPTS {
            error!(
                placement = p.placement_id,
                why, "ALERT placement stuck: no placement mount; an owner looks"
            );
            let res = j.set(
                p.placement_id,
                "stuck",
                &[
                    (
                        "reason",
                        text(format!("trouble: no placement mount: {why}")),
                    ),
                    ("stuck_from", text(p.state.clone())),
                    ("unreported", text("stuck")),
                    ("next_at", int(now + STUCK_RETRY_MS)),
                ],
                now,
            );
            if let Err(e) = res {
                error!(placement = p.placement_id, error = %e, "annex.db: placement journal");
            }
            continue;
        }
        warn!(
            placement = p.placement_id,
            why,
            attempt = n,
            "placement: no mount; again later"
        );
        retried(db, &p, n, now + (BACKOFF_MS << n.min(6)), why);
    }
}

/// The NAS is mounted again: what was stuck for want of a mount doesn't wait out
/// its hour. Other trouble keeps its hour, since a mount says nothing about it.
pub fn mount_is_back(db: &AnnexDb, now: i64) {
    let res = db.conn().execute(
        "UPDATE placement SET next_at = ?1 WHERE next_at > ?1 AND (
            (state = 'stuck' AND reason LIKE 'trouble: no placement mount%')
         OR (state = 'failed' AND leftover LIKE 'no placement mount%'))",
        [now],
    );
    if let Err(e) = res {
        error!(error = %e, "annex.db: placement journal");
    }
}

/// When a file at this path (below the annex's root) was placed, for the age
/// rules: its age counts from the placement (section 4.6).
pub fn placed_at(db: &AnnexDb, path: &str) -> Option<i64> {
    db.conn()
        .query_row(
            "SELECT max(updated_at) FROM placement WHERE placed_path = ?1 AND state = 'done'",
            [path],
            |r| r.get::<_, Option<i64>>(0),
        )
        .ok()
        .flatten()
}

/// Ask the service for tickets and journal the new ones (no NAS needed for
/// that; the placement mount comes when there is one in hand). The count of
/// new ones. What is in hand is said, so no credential is minted for it.
pub async fn take_tickets<S: AnnexService>(
    svc: &S,
    db: &AnnexDb,
    annex_id: u64,
    now: i64,
) -> usize {
    let j = Journal(db);
    let in_hand = j.in_hand().unwrap_or_default();
    let tickets = match svc
        .placement_tickets(&json!({
            "annex_id": annex_id.to_string(),
            "in_hand": in_hand.iter().map(u64::to_string).collect::<Vec<_>>(),
        }))
        .await
    {
        Ok(v) => v,
        Err(e) => {
            warn!(error = %e, "placements: no tickets now");
            return 0;
        }
    };
    let mut n = 0;
    for t in tickets["tickets"].as_array().cloned().unwrap_or_default() {
        let Some(pid) = id(&t["placement_id"]) else {
            continue;
        };
        if j.get(pid).ok().flatten().is_some() {
            // In hand, and handed over again: only its new credential is taken.
            if let Err(e) = j.renew_read(pid, &t) {
                error!(placement = pid, error = %e, "annex.db: a new credential not journalled");
            }
            continue;
        }
        match j.insert(&t, &format!("{}-{pid}", token()), now) {
            Ok(true) => n += 1,
            Ok(false) => {}
            Err(e) => error!(placement = pid, error = %e, "annex.db: no journal, no placement"),
        }
    }
    n
}

/// 128 random bits, hex: a staging name nobody can guess.
pub fn token() -> String {
    use ring::rand::SecureRandom;
    let mut b = [0u8; 16];
    let _ = ring::rand::SystemRandom::new().fill(&mut b);
    b.iter().map(|x| format!("{x:02x}")).collect()
}

pub fn sha_hex(b: &[u8]) -> String {
    digest(&SHA256, b)
        .as_ref()
        .iter()
        .map(|x| format!("{x:02x}"))
        .collect()
}

/// Exactly the text `pointer_text` wrote for this file, with the date it states.
pub fn is_pointer_text(body: &[u8], annex: &str, path: &str, file_id: u64) -> bool {
    let Ok(s) = std::str::from_utf8(body) else {
        return false;
    };
    let Some(first) = s.lines().next() else {
        return false;
    };
    let Some(at) = first
        .strip_prefix("This file was moved to Azure by OmniFile on ")
        .and_then(|r| r.strip_suffix('.'))
    else {
        return false;
    };
    s == super::remover::pointer_text(annex, path, file_id, at)
}

#[cfg(target_os = "linux")]
mod imp {
    use std::ffi::CString;
    use std::io::{self, Read};
    use std::os::fd::{AsFd, AsRawFd, FromRawFd, OwnedFd};
    use std::os::unix::fs::FileExt;

    use serde_json::Value;

    use super::super::acl::Flavour;
    use super::super::nasfs::{Root, Stat, drop_cache, statx};

    fn name(n: &str) -> io::Result<CString> {
        if n.is_empty() || n == "." || n == ".." || n.contains('/') {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "not a plain name",
            ));
        }
        CString::new(n).map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "NUL in name"))
    }

    fn ok(r: libc::c_int) -> io::Result<()> {
        if r == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    fn fd_of(dir: &Root) -> libc::c_int {
        dir.dir_fd().as_raw_fd()
    }

    pub fn stat_name(dir: &Root, n: &str) -> io::Result<Stat> {
        statx(
            dir.dir_fd(),
            &name(n)?,
            libc::AT_SYMLINK_NOFOLLOW | libc::AT_STATX_FORCE_SYNC,
        )
    }

    pub fn fstat(f: &std::fs::File) -> io::Result<Stat> {
        statx(
            f.as_fd(),
            &CString::new("").unwrap(),
            libc::AT_EMPTY_PATH | libc::AT_STATX_FORCE_SYNC,
        )
    }

    pub fn mkdir(dir: &Root, n: &str) -> io::Result<()> {
        let c = name(n)?;
        // SAFETY: a valid dir fd and a NUL-terminated name.
        ok(unsafe { libc::mkdirat(fd_of(dir), c.as_ptr(), 0o700) })
    }

    /// P0: a new folder gets what its parent has, so whoever may add a file to
    /// the parent may add one here (decided, question 4). Over SMB and NFSv4
    /// the parent's ACL; over NFSv3 its mode, owner and group (best effort for
    /// the owner: `go` reads the result, and refuses it if it doesn't do).
    pub fn inherit_folder(dir: &Root, n: &str, flavour: Flavour) -> io::Result<()> {
        let parent = open_flags(dir, ".", libc::O_RDONLY | libc::O_DIRECTORY, 0).or_else(|_| {
            // SAFETY: a valid fd, duplicated, reopened by its O_PATH handle.
            let p = format!("/proc/self/fd/{}", fd_of(dir));
            std::fs::File::open(p)
        })?;
        let child = open_flags(dir, n, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
        match flavour {
            Flavour::Smb | Flavour::Nfs4 => {
                let key = if flavour == Flavour::Smb {
                    "system.cifs_acl"
                } else {
                    "system.nfs4_acl"
                };
                let raw = super::super::nasfs::fgetxattr(&parent, key)?
                    .ok_or_else(|| io::Error::from_raw_os_error(libc::ENODATA))?;
                fsetxattr(&child, key, &raw)
            }
            Flavour::Nfs3 => {
                let st = fstat(&parent)?;
                let _ = fchown(&child, Some(st.uid), Some(st.gid));
                // SAFETY: a valid fd.
                ok(unsafe { libc::fchmod(child.as_raw_fd(), (st.mode & 0o7777) as libc::mode_t) })
            }
        }
    }

    pub fn rmdir(dir: &Root, n: &str) -> io::Result<()> {
        let c = name(n)?;
        // SAFETY: as above.
        ok(unsafe { libc::unlinkat(fd_of(dir), c.as_ptr(), libc::AT_REMOVEDIR) })
    }

    pub fn unlink(dir: &Root, n: &str) -> io::Result<()> {
        let c = name(n)?;
        // SAFETY: as above.
        ok(unsafe { libc::unlinkat(fd_of(dir), c.as_ptr(), 0) })
    }

    pub fn dir_has_entries(root: &Root, rel: &str) -> io::Result<bool> {
        Ok(!root.read_dir(rel)?.1.is_empty())
    }

    fn open_flags(dir: &Root, n: &str, flags: libc::c_int, mode: u32) -> io::Result<std::fs::File> {
        let c = name(n)?;
        // SAFETY: a valid dir fd and a NUL-terminated name.
        let fd = unsafe {
            libc::openat(
                fd_of(dir),
                c.as_ptr(),
                flags | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                mode as libc::c_uint,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: a fresh fd we own.
        Ok(std::fs::File::from(unsafe { OwnedFd::from_raw_fd(fd) }))
    }

    pub fn create_excl(dir: &Root, n: &str) -> io::Result<std::fs::File> {
        open_flags(dir, n, libc::O_CREAT | libc::O_EXCL | libc::O_RDWR, 0o600)
    }

    pub fn open_at(dir: &Root, n: &str, write: bool) -> io::Result<std::fs::File> {
        #[cfg(test)]
        if write && super::faults::WRITE_OPENS_REFUSED.with(|f| f.get()) {
            return Err(io::Error::from_raw_os_error(libc::EACCES));
        }
        // A file made in it, then given its descriptor, may be read-only to the
        // account: only the fill opens it for writing.
        open_flags(
            dir,
            n,
            if write {
                libc::O_RDWR
            } else {
                libc::O_RDONLY | libc::O_NONBLOCK
            },
            0,
        )
    }

    pub fn read_small(dir: &Root, n: &str) -> io::Result<Vec<u8>> {
        let mut out = Vec::new();
        open_at(dir, n, false)?.take(4097).read_to_end(&mut out)?;
        Ok(out)
    }

    pub fn pwrite_all(f: &std::fs::File, b: &[u8], at: u64) -> io::Result<()> {
        f.write_all_at(b, at)
    }

    pub fn fchown(f: &std::fs::File, uid: Option<u32>, gid: Option<u32>) -> io::Result<()> {
        let u = uid
            .map(|u| u as libc::uid_t)
            .unwrap_or(u32::MAX as libc::uid_t);
        let g = gid
            .map(|g| g as libc::gid_t)
            .unwrap_or(u32::MAX as libc::gid_t);
        // SAFETY: a valid fd.
        ok(unsafe { libc::fchown(f.as_raw_fd(), u, g) })
    }

    pub fn futimens(f: &std::fs::File, ns: i64) -> io::Result<()> {
        let ts = libc::timespec {
            tv_sec: ns.div_euclid(1_000_000_000) as _,
            tv_nsec: ns.rem_euclid(1_000_000_000) as _,
        };
        let both = [ts, ts];
        // SAFETY: a valid fd and two timespecs.
        ok(unsafe { libc::futimens(f.as_raw_fd(), both.as_ptr()) })
    }

    fn fsetxattr(f: &std::fs::File, key: &str, v: &[u8]) -> io::Result<()> {
        let k = CString::new(key)?;
        // SAFETY: a valid fd, a NUL-terminated key and a buffer of v.len().
        ok(unsafe { libc::fsetxattr(f.as_raw_fd(), k.as_ptr(), v.as_ptr().cast(), v.len(), 0) })
    }

    /// An NT descriptor for `system.cifs_acl` / `cifs_ntsd`: in a folder not
    /// its own, inherited entries made explicit and the DACL protected.
    pub fn nt_descriptor(raw: &[u8], protect: bool) -> io::Result<Vec<u8>> {
        let bad = || io::Error::new(io::ErrorKind::InvalidData, "a bad NT descriptor");
        if raw.len() < 20 {
            return Err(bad());
        }
        let mut sd = raw.to_vec();
        if protect {
            let control = u16::from_le_bytes([sd[2], sd[3]]) | 0x1000; // SE_DACL_PROTECTED
            sd[2..4].copy_from_slice(&control.to_le_bytes());
            let o_dacl = u32::from_le_bytes(sd[16..20].try_into().unwrap()) as usize;
            if o_dacl != 0 {
                if sd.len() < o_dacl + 8 {
                    return Err(bad());
                }
                let size = u16::from_le_bytes([sd[o_dacl + 2], sd[o_dacl + 3]]) as usize;
                let count = u16::from_le_bytes([sd[o_dacl + 4], sd[o_dacl + 5]]) as usize;
                let mut at = o_dacl + 8;
                for _ in 0..count {
                    if at + 4 > o_dacl + size || at + 4 > sd.len() {
                        return Err(bad());
                    }
                    sd[at + 1] &= !0x10; // INHERITED_ACE
                    let len = u16::from_le_bytes([sd[at + 2], sd[at + 3]]) as usize;
                    if len < 8 {
                        return Err(bad());
                    }
                    at += len;
                }
            }
        }
        Ok(sd)
    }

    fn b64(acl: &Value) -> io::Result<Vec<u8>> {
        use base64::Engine;
        let s = acl
            .get("raw")
            .and_then(Value::as_str)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "no raw descriptor"))?;
        base64::engine::general_purpose::STANDARD
            .decode(s)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "raw is not base64"))
    }

    /// P3's ACL: the DACL over SMB (owner left alone, R3), the NFSv4 ACL, or
    /// the mode (a POSIX ACL with named entries isn't put back in v1).
    pub fn set_acl(
        f: &std::fs::File,
        flavour: Flavour,
        acl: &Value,
        protect: bool,
    ) -> io::Result<()> {
        match (flavour, acl.get("kind").and_then(Value::as_str)) {
            (Flavour::Smb, Some("ntfs_sd")) => {
                fsetxattr(f, "system.cifs_acl", &nt_descriptor(&b64(acl)?, protect)?)
            }
            (Flavour::Nfs4, Some("nfs4")) => {
                let mut raw = b64(acl)?;
                if protect {
                    clear_nfs4_inherited(&mut raw);
                }
                fsetxattr(f, "system.nfs4_acl", &raw)
            }
            (Flavour::Nfs3, Some("posix")) => {
                // No setuid or setgid until its owner is the file's: until P5
                // it would run as the placement account, root over `sec=sys`.
                let mode = acl.get("mode").and_then(Value::as_u64).unwrap_or(0o600) as u32 & 0o777;
                // SAFETY: a valid fd.
                ok(unsafe { libc::fchmod(f.as_raw_fd(), mode as libc::mode_t) })
            }
            _ => Err(io::Error::from_raw_os_error(libc::EOPNOTSUPP)),
        }
    }

    /// After the chown of P5: the mode's setuid and setgid bits, if it had any.
    pub fn restore_mode_bits(f: &std::fs::File, flavour: Flavour, acl: &Value) -> io::Result<()> {
        if flavour != Flavour::Nfs3 {
            return Ok(());
        }
        let mode = acl.get("mode").and_then(Value::as_u64).unwrap_or(0) as u32 & 0o7777;
        if mode & 0o7000 == 0 {
            return Ok(());
        }
        // SAFETY: a valid fd.
        ok(unsafe { libc::fchmod(f.as_raw_fd(), mode as libc::mode_t) })
    }

    /// NFSv4 ACE flag ACE4_INHERITED_ACE (0x80) off in each ACE of the XDR.
    fn clear_nfs4_inherited(raw: &mut [u8]) {
        if raw.len() < 4 {
            return;
        }
        let n = u32::from_be_bytes(raw[0..4].try_into().unwrap()) as usize;
        let mut at = 4;
        for _ in 0..n {
            if at + 16 > raw.len() {
                return;
            }
            let flag = u32::from_be_bytes(raw[at + 4..at + 8].try_into().unwrap()) & !0x80;
            raw[at + 4..at + 8].copy_from_slice(&flag.to_be_bytes());
            let len = u32::from_be_bytes(raw[at + 12..at + 16].try_into().unwrap()) as usize;
            at += 16 + len.div_ceil(4) * 4;
        }
    }

    /// P5 over SMB: owner, group and DACL, the owner needing the restore right.
    pub fn set_owner_sd(f: &std::fs::File, acl: &Value, protect: bool) -> io::Result<()> {
        fsetxattr(f, "system.cifs_ntsd", &nt_descriptor(&b64(acl)?, protect)?)
    }

    /// The lock on the staging folder or on a file in it: over SMB a protected
    /// DACL naming only the placement and read accounts (full, and read with
    /// traverse); over NFSv4 an ACL naming only the owner and the read account,
    /// or the mode where the server has no ACLs; over NFSv3 the mode. For a
    /// folder the entries are inherited by what is made in it.
    pub fn lock_down(
        f: &std::fs::File,
        flavour: Flavour,
        placement: Option<&str>,
        read: Option<&str>,
        read_who: Option<&str>,
        folder: bool,
    ) -> io::Result<()> {
        let mode = if folder { 0o700 } else { 0o600 };
        match flavour {
            Flavour::Nfs3 => {
                // SAFETY: a valid fd.
                ok(unsafe { libc::fchmod(f.as_raw_fd(), mode) })
            }
            Flavour::Nfs4 => {
                // SAFETY: a valid fd.
                ok(unsafe { libc::fchmod(f.as_raw_fd(), mode) })?;
                match fsetxattr(f, "system.nfs4_acl", &nfs4_lock(read_who, folder)) {
                    Err(e) if e.raw_os_error() == Some(libc::EOPNOTSUPP) => Ok(()),
                    other => other,
                }
            }
            Flavour::Smb => {
                let p = placement.ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "the placement account's SID")
                })?;
                let mut aces = vec![(p.to_owned(), 0x001F_01FFu32)];
                if let Some(r) = read
                    && r != p
                {
                    aces.push((r.to_owned(), 0x0012_00A9)); // read, execute (traverse)
                }
                fsetxattr(f, "system.cifs_acl", &dacl_sd(&aces, folder)?)
            }
        }
    }

    const ACE4_FULL: u32 = 0x001F_01FF;
    const ACE4_READ: u32 = 0x0012_00A9; // read, list, execute (traverse), attributes, ACL
    /// What `GROUP@` and `EVERYONE@` may keep in the lock: `READ_ATTRIBUTES`
    /// (0x80), `READ_ACL` (0x20000) and `SYNCHRONIZE` (0x100000), none of which
    /// gives access to the data. A server that derives an ACL from the mode can
    /// leave these (annex.md section 5). Read, write, execute, `WRITE_ACL` and
    /// `WRITE_OWNER` still fail the lock. No server has confirmed the shape yet.
    pub const NFS4_NO_DATA_ACCESS: u32 = 0x0012_0080;
    const FILE_INHERIT: u32 = 0x01;
    const DIR_INHERIT: u32 = 0x02;

    /// The XDR of the NFSv4 lock: `OWNER@` full, and the read account, if it
    /// is another, read and traverse. Nobody else is named.
    pub fn nfs4_lock(read_who: Option<&str>, folder: bool) -> Vec<u8> {
        let flag = if folder {
            FILE_INHERIT | DIR_INHERIT
        } else {
            0
        };
        let mut aces: Vec<(u32, u32, &str)> = vec![(flag, ACE4_FULL, "OWNER@")];
        if let Some(r) = read_who {
            aces.push((flag, ACE4_READ, r));
        }
        let mut out = (aces.len() as u32).to_be_bytes().to_vec();
        for (flag, mask, who) in aces {
            out.extend_from_slice(&0u32.to_be_bytes()); // ALLOWED
            out.extend_from_slice(&flag.to_be_bytes());
            out.extend_from_slice(&mask.to_be_bytes());
            out.extend_from_slice(&(who.len() as u32).to_be_bytes());
            out.extend_from_slice(who.as_bytes());
            out.resize(out.len() + (4 - who.len() % 4) % 4, 0);
        }
        out
    }

    /// The lock as read back. Over SMB: protected, and every ACE that grants
    /// anything names one of its two accounts, or is an inherit-only `CREATOR
    /// OWNER` (a server that maps NT ACLs to POSIX ones adds one; what it grants
    /// goes to whoever creates there, the placement account). An inherit-only
    /// ACE counts, because it is what new files get. Over NFSv4: every allow,
    /// inherited or not, names `OWNER@` or the read account. Over NFSv3 and on
    /// an NFSv4 server without ACLs: no mode bits for the group or others, and
    /// no named entry that grants anything.
    pub fn locked(
        acl: &Value,
        flavour: Flavour,
        placement: Option<&str>,
        read: Option<&str>,
        read_who: Option<&str>,
    ) -> bool {
        match acl.get("kind").and_then(Value::as_str) {
            Some("ntfs_sd") if flavour == Flavour::Smb => {}
            Some("nfs4") => {
                use base64::Engine;
                let Some(raw) = acl.get("raw").and_then(Value::as_str) else {
                    return false;
                };
                let Ok(b) = base64::engine::general_purpose::STANDARD.decode(raw) else {
                    return false;
                };
                let Ok(aces) = super::super::acl::parse_nfs4(&b) else {
                    return false;
                };
                return aces.iter().any(|a| a.who == "OWNER@")
                    && aces.iter().all(|a| {
                        a.ace_type != super::super::acl::ACE4_ACCESS_ALLOWED
                            || a.mask == 0
                            || a.who == "OWNER@"
                            || Some(a.who.as_str()) == read_who
                            || (matches!(a.who.as_str(), "GROUP@" | "EVERYONE@")
                                && a.mask & !NFS4_NO_DATA_ACCESS == 0)
                    });
            }
            _ if flavour != Flavour::Smb => {
                let no_bits = acl
                    .get("mode")
                    .and_then(Value::as_u64)
                    .is_some_and(|m| m & 0o077 == 0);
                let no_named = acl
                    .get("entries")
                    .and_then(Value::as_array)
                    .is_none_or(|es| {
                        es.iter().all(|e| {
                            let tag = e.get("tag").and_then(Value::as_str).unwrap_or("");
                            let perms = e.get("perms").and_then(Value::as_str).unwrap_or("---");
                            !matches!(tag, "user" | "group") || perms == "---"
                        })
                    });
                return no_bits && no_named;
            }
            _ => return false,
        }
        let Some(raw) = acl.get("raw").and_then(Value::as_str) else {
            return false;
        };
        use base64::Engine;
        let Ok(b) = base64::engine::general_purpose::STANDARD.decode(raw) else {
            return false;
        };
        let want: Vec<&str> = [placement, read].into_iter().flatten().collect();
        let Ok(aces) = sd_aces(&b) else {
            return false;
        };
        let control = u16::from_le_bytes([b[2], b[3]]);
        let granting: Vec<&(String, u8, u32, u8)> = aces
            .iter()
            .filter(|(_, kind, mask, _)| *kind == 0 && *mask != 0)
            .collect();
        control & 0x1000 != 0
            && granting.iter().any(|(_, _, _, flags)| flags & 0x08 == 0)
            && granting.iter().all(|(s, _, _, flags)| {
                want.contains(&s.as_str()) || (s == "S-1-3-0" && flags & 0x08 != 0)
            })
    }

    /// Whether a descriptor's owner is this account.
    pub fn sd_owner_is(f: &std::fs::File, sid: Option<&str>) -> bool {
        let Ok(Some(b)) = super::super::nasfs::fgetxattr(f, "system.cifs_ntsd") else {
            return false;
        };
        sd_owner_matches(&b, sid)
    }

    pub fn sd_owner_matches(b: &[u8], sid: Option<&str>) -> bool {
        sid.is_some_and(|want| sd_owner(b).as_deref() == Some(want))
    }

    /// The owner SID of a self-relative security descriptor.
    pub fn sd_owner(b: &[u8]) -> Option<String> {
        if b.len() < 20 {
            return None;
        }
        let o = u32::from_le_bytes(b[4..8].try_into().ok()?) as usize;
        if o == 0 || b.len() < o + 8 {
            return None;
        }
        let n = b[o + 1] as usize;
        if b.len() < o + 8 + 4 * n {
            return None;
        }
        let auth = u64::from_be_bytes([
            0,
            0,
            b[o + 2],
            b[o + 3],
            b[o + 4],
            b[o + 5],
            b[o + 6],
            b[o + 7],
        ]);
        let mut owner = format!("S-1-{auth}");
        for i in 0..n {
            let q = o + 8 + 4 * i;
            owner.push_str(&format!(
                "-{}",
                u32::from_le_bytes(b[q..q + 4].try_into().ok()?)
            ));
        }
        Some(owner)
    }

    fn sid_bytes(sid: &str) -> io::Result<Vec<u8>> {
        let bad = || io::Error::new(io::ErrorKind::InvalidInput, "a bad SID");
        let parts: Vec<&str> = sid.split('-').collect();
        if parts.len() < 3 || parts[0] != "S" || parts[1] != "1" {
            return Err(bad());
        }
        let auth: u64 = parts[2].parse().map_err(|_| bad())?;
        let subs: Vec<u32> = parts[3..]
            .iter()
            .map(|s| s.parse().map_err(|_| bad()))
            .collect::<io::Result<_>>()?;
        let mut out = vec![1u8, subs.len() as u8];
        out.extend_from_slice(&auth.to_be_bytes()[2..]);
        for s in subs {
            out.extend_from_slice(&s.to_le_bytes());
        }
        Ok(out)
    }

    /// A self-relative descriptor with only a protected DACL of allow ACEs,
    /// on a folder inherited by files and folders made below.
    pub fn dacl_sd(aces: &[(String, u32)], folder: bool) -> io::Result<Vec<u8>> {
        let mut body = Vec::new();
        for (sid, mask) in aces {
            let s = sid_bytes(sid)?;
            let size = 8 + s.len();
            body.push(0u8); // ACCESS_ALLOWED
            body.push(if folder { 0x03 } else { 0 }); // OBJECT_INHERIT | CONTAINER_INHERIT
            body.extend_from_slice(&(size as u16).to_le_bytes());
            body.extend_from_slice(&mask.to_le_bytes());
            body.extend_from_slice(&s);
        }
        let mut sd = vec![1u8, 0];
        sd.extend_from_slice(&(0x8004u16 | 0x1000).to_le_bytes()); // SELF_RELATIVE|DACL_PRESENT|PROTECTED
        sd.extend_from_slice(&0u32.to_le_bytes()); // owner
        sd.extend_from_slice(&0u32.to_le_bytes()); // group
        sd.extend_from_slice(&0u32.to_le_bytes()); // sacl
        sd.extend_from_slice(&20u32.to_le_bytes()); // dacl
        sd.extend_from_slice(&[2u8, 0]);
        sd.extend_from_slice(&((8 + body.len()) as u16).to_le_bytes());
        sd.extend_from_slice(&(aces.len() as u16).to_le_bytes());
        sd.extend_from_slice(&[0, 0]);
        sd.extend_from_slice(&body);
        Ok(sd)
    }

    /// Each ACE of a descriptor's DACL: (SID, type, mask, flags).
    fn sd_aces(b: &[u8]) -> io::Result<Vec<(String, u8, u32, u8)>> {
        let bad = || io::Error::new(io::ErrorKind::InvalidData, "a bad descriptor");
        if b.len() < 20 {
            return Err(bad());
        }
        let o = u32::from_le_bytes(b[16..20].try_into().unwrap()) as usize;
        if o == 0 || b.len() < o + 8 {
            return Err(bad());
        }
        let count = u16::from_le_bytes([b[o + 4], b[o + 5]]) as usize;
        let mut at = o + 8;
        let mut out = Vec::new();
        for _ in 0..count {
            if at + 8 > b.len() {
                return Err(bad());
            }
            let len = u16::from_le_bytes([b[at + 2], b[at + 3]]) as usize;
            let s = at + 8;
            if s + 8 > b.len() || len < 8 {
                return Err(bad());
            }
            let n = b[s + 1] as usize;
            let auth = u64::from_be_bytes([
                0,
                0,
                b[s + 2],
                b[s + 3],
                b[s + 4],
                b[s + 5],
                b[s + 6],
                b[s + 7],
            ]);
            let mut sid = format!("S-1-{auth}");
            for i in 0..n {
                let q = s + 8 + 4 * i;
                if q + 4 > b.len() {
                    return Err(bad());
                }
                sid.push_str(&format!(
                    "-{}",
                    u32::from_le_bytes(b[q..q + 4].try_into().unwrap())
                ));
            }
            let mask = u32::from_le_bytes(b[at + 4..at + 8].try_into().unwrap());
            out.push((sid, b[at], mask, b[at + 1]));
            at += len;
        }
        Ok(out)
    }

    /// (bytes, CRC-64/NVME) of a file read whole through `root` (the
    /// read-only mount), after dropping the page cache.
    pub fn read_back(root: &Root, rel: &str) -> io::Result<(u64, u64)> {
        #[cfg(test)]
        if super::faults::READBACK_EACCES.with(|x| x.replace(false)) {
            return Err(io::Error::from_raw_os_error(libc::EACCES));
        }
        let mut f = root.open_file(rel)?;
        drop_cache(&f);
        let mut crc = omnifile_integrity::crc64::Crc64::new();
        let mut buf = vec![0u8; 8 << 20];
        let mut n = 0u64;
        loop {
            let k = f.read(&mut buf)?;
            if k == 0 {
                return Ok((n, crc.finalize()));
            }
            crc.update(&buf[..k]);
            n += k as u64;
        }
    }

    pub fn link_refused(e: &io::Error) -> bool {
        if super::faults_link_eacces() {
            return true;
        }
        matches!(
            e.raw_os_error(),
            Some(libc::EACCES | libc::EPERM | libc::EOPNOTSUPP | libc::EMLINK)
        )
    }

    /// P4: `linkat` from the staging folder to the target, refused at the
    /// server if the name exists.
    pub fn link_across(from: &Root, n: &str, to: &Root, t: &str) -> io::Result<()> {
        if super::faults_link_eacces() {
            return Err(io::Error::from_raw_os_error(libc::EACCES));
        }
        let (a, b) = (name(n)?, name(t)?);
        // SAFETY: valid dir fds and NUL-terminated names.
        let r = ok(unsafe { libc::linkat(fd_of(from), a.as_ptr(), fd_of(to), b.as_ptr(), 0) });
        #[cfg(test)]
        if r.is_ok() && super::faults::LINK_REPLAYED.with(|f| f.replace(false)) {
            return Err(io::Error::from_raw_os_error(libc::EEXIST));
        }
        r
    }

    pub fn rename_across(from: &Root, n: &str, to: &Root, t: &str) -> io::Result<()> {
        let (a, b) = (name(n)?, name(t)?);
        // SAFETY: as above.
        ok(unsafe { libc::renameat(fd_of(from), a.as_ptr(), fd_of(to), b.as_ptr()) })
    }

    pub fn make_placeholder(dir: &Root, n: &str) -> io::Result<(u64, i64)> {
        use std::io::Write;
        let mut f = open_flags(dir, n, libc::O_CREAT | libc::O_EXCL | libc::O_WRONLY, 0o600)?;
        f.write_all(super::super::remover::PLACEHOLDER_TEXT.as_bytes())?;
        f.sync_all()?;
        let st = fstat(&f)?;
        Ok((st.ino, st.ctime_ns))
    }

    /// A file at this name that can only be our placeholder: exactly its text,
    /// one link.
    pub fn is_placeholder(dir: &Root, n: &str, st: &Stat) -> bool {
        let text = super::super::remover::PLACEHOLDER_TEXT.as_bytes();
        st.nlink == 1 && st.size == text.len() as u64 && read_small(dir, n).is_ok_and(|b| b == text)
    }

    pub fn placeholder_intact(dir: &Root, n: &str, ino: u64, ctime: i64) -> io::Result<bool> {
        let st = match stat_name(dir, n) {
            Ok(s) => s,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
            Err(e) => return Err(e),
        };
        let text = super::super::remover::PLACEHOLDER_TEXT.as_bytes();
        if st.ino != ino || st.ctime_ns != ctime || st.size != text.len() as u64 || st.nlink != 1 {
            return Ok(false);
        }
        Ok(read_small(dir, n)? == text)
    }
}

#[cfg(not(target_os = "linux"))]
mod imp {
    use std::io;

    use serde_json::Value;

    use super::super::acl::Flavour;
    use super::super::nasfs::{Root, Stat};

    fn no() -> io::Error {
        io::Error::from_raw_os_error(libc::ENOSYS)
    }
    pub fn stat_name(_: &Root, _: &str) -> io::Result<Stat> {
        Err(no())
    }
    pub fn fstat(_: &std::fs::File) -> io::Result<Stat> {
        Err(no())
    }
    pub fn mkdir(_: &Root, _: &str) -> io::Result<()> {
        Err(no())
    }
    pub fn inherit_folder(_: &Root, _: &str, _: Flavour) -> io::Result<()> {
        Err(no())
    }
    pub fn rmdir(_: &Root, _: &str) -> io::Result<()> {
        Err(no())
    }
    pub fn unlink(_: &Root, _: &str) -> io::Result<()> {
        Err(no())
    }
    pub fn dir_has_entries(_: &Root, _: &str) -> io::Result<bool> {
        Err(no())
    }
    pub fn create_excl(_: &Root, _: &str) -> io::Result<std::fs::File> {
        Err(no())
    }
    pub fn open_at(_: &Root, _: &str, _: bool) -> io::Result<std::fs::File> {
        Err(no())
    }
    pub fn read_small(_: &Root, _: &str) -> io::Result<Vec<u8>> {
        Err(no())
    }
    pub fn pwrite_all(_: &std::fs::File, _: &[u8], _: u64) -> io::Result<()> {
        Err(no())
    }
    pub fn fchown(_: &std::fs::File, _: Option<u32>, _: Option<u32>) -> io::Result<()> {
        Err(no())
    }
    pub fn futimens(_: &std::fs::File, _: i64) -> io::Result<()> {
        Err(no())
    }
    pub fn set_acl(_: &std::fs::File, _: Flavour, _: &Value, _: bool) -> io::Result<()> {
        Err(no())
    }
    pub fn set_owner_sd(_: &std::fs::File, _: &Value, _: bool) -> io::Result<()> {
        Err(no())
    }
    pub fn restore_mode_bits(_: &std::fs::File, _: Flavour, _: &Value) -> io::Result<()> {
        Err(no())
    }
    pub fn lock_down(
        _: &std::fs::File,
        _: Flavour,
        _: Option<&str>,
        _: Option<&str>,
        _: Option<&str>,
        _: bool,
    ) -> io::Result<()> {
        Err(no())
    }
    pub fn locked(
        _: &Value,
        _: Flavour,
        _: Option<&str>,
        _: Option<&str>,
        _: Option<&str>,
    ) -> bool {
        false
    }
    pub fn is_placeholder(_: &Root, _: &str, _: &Stat) -> bool {
        false
    }
    pub fn sd_owner_is(_: &std::fs::File, _: Option<&str>) -> bool {
        false
    }
    pub fn read_back(_: &Root, _: &str) -> io::Result<(u64, u64)> {
        Err(no())
    }
    pub fn link_refused(_: &io::Error) -> bool {
        false
    }
    pub fn link_across(_: &Root, _: &str, _: &Root, _: &str) -> io::Result<()> {
        Err(no())
    }
    pub fn rename_across(_: &Root, _: &str, _: &Root, _: &str) -> io::Result<()> {
        Err(no())
    }
    pub fn make_placeholder(_: &Root, _: &str) -> io::Result<(u64, i64)> {
        Err(no())
    }
    pub fn placeholder_intact(_: &Root, _: &str, _: u64, _: i64) -> io::Result<bool> {
        Err(no())
    }
}

#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn faults_link_eacces() -> bool {
    #[cfg(test)]
    {
        faults::LINK_EACCES.with(|f| f.get())
    }
    #[cfg(not(test))]
    {
        false
    }
}

pub mod replace;
pub use replace::replacing;

#[cfg(test)]
mod tests;
