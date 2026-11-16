//! A3.4: a placement that replaces the file at its origin
//! (`docs/milestones/results/a34.md`; decisions 82 to 85). Part of the placer,
//! the one module beside the remover that writes to a NAS. It adds no new kind of
//! write: the old file is hidden with the remover's W1, deleted with its W2, put
//! back with its W3, and the new file goes under the name with the placer's P4.
//!
//! The new file is staged in full first (P1 to P3, `go` granted). Then:
//!
//! | step | write | gate before it |
//! |---|---|---|
//! | R1 hide the old file as `.omnifile-replacing-<placement>-<name>` | W1 (no-replace rename) | the edge's checks (a forced `statx` equal to the ticket's inode, size, mtime and ctime, one link, over SMB no stream and its CRC), then `replace_go`, then the edge's checks again |
//! | R2 wait the annex's grace period, check the hidden file again | none | the W2 gates on the hidden file (the ctime R1 left), then `replace_delete_go` (place) |
//! | R3 the new file under the name | P4 (link, never replacing) | a forced `statx` of the staged file against `secured` |
//! | R4 delete the hidden old file | W2 | the W2 gates once more, then `replace_delete_go` (delete) |
//! | R5 owner, pointer, clean up | P5 to P7 | as for any placement |
//!
//! What fails, and what then happens (nothing is deleted but at R4):
//! - before R1, or a refusal at R1: nothing is hidden; the new file takes a `-restored` name;
//! - at R2 (a change, a definite refusal): the old file goes back under its own name (W3,
//!   no-replace), and the new file takes a `-restored` name. A user who made a file of that
//!   name in the grace has it: the old one goes back as `<name>-replaced-<UTC time>`;
//! - at R4 (the new file has the name): the old file goes back as
//!   `<name>-replaced-<UTC time>` (W3, no-replace), with a Problem naming both. Never
//!   `-restored`: that name means a version someone asked for;
//! - the service away, or an answer that isn't definite: wait and ask again; after the
//!   bound the placement is `stuck` with the hidden name, asking hourly;
//! - W3 itself refused: `stuck`, every byte kept;
//! - the placement ending any other way (a `go` refused after a restart, a cancel, an
//!   abandon) first puts the old file back (P7 does), and reports what it couldn't.
//!
//! Each step is journalled (`placement_replace`) before it is taken; a write never runs
//! if that failed. A restart finds the hidden file by its name and inode.

use std::io;

use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};
use tracing::{error, info, warn};

use super::super::nasfs::Root;
use super::super::remover::{self, Check, imp as rm};
use super::super::service::AnnexService;
use super::{Flow, PAUSE_RETRY_MS, Placement, Placer, Ticket, errno};
use crate::annex::db::AnnexDb;

pub const HIDDEN_PREFIX: &str = ".omnifile-replacing-";
/// How long a grace hold waits before it asks again (ms).
const GRACE_ASK_MS: i64 = 60_000;
/// How many `-replaced-` names are tried before the old file stays hidden.
const ASIDE_TRIES: usize = 9;

/// The hidden name of placement `pid`'s old file.
pub fn hidden_name(pid: u64, name: &str) -> String {
    format!("{HIDDEN_PREFIX}{pid}-{name}")
}

/// `<stem>-replaced-<YYYY-MM-DD-HHMMSS>Z<ext>`, then `...Z-2<ext>` and on: the name the
/// old file is kept under when it can't go back under its own (the extension starts at
/// the last dot; a name whose only dot leads it has none; 255 bytes at most).
pub fn aside_name(name: &str, now_ms: i64, n: usize) -> String {
    let at = super::super::scan::rfc3339_ms(now_ms); // 2026-11-11T12:00:00.000Z
    let stamp = format!(
        "{}-{}{}{}Z",
        &at[..10],
        &at[11..13],
        &at[14..16],
        &at[17..19]
    );
    let (mut stem, mut ext) = match name.rfind('.') {
        Some(i) if i > 0 => (name[..i].to_owned(), name[i..].to_owned()),
        _ => (name.to_owned(), String::new()),
    };
    let suffix = if n == 0 {
        format!("-replaced-{stamp}")
    } else {
        format!("-replaced-{stamp}-{}", n + 1)
    };
    if suffix.len() + ext.len() >= 255 {
        stem.push_str(&ext);
        ext.clear();
    }
    let room = 255 - suffix.len() - ext.len();
    while stem.len() > room {
        stem.pop();
    }
    if stem.is_empty() {
        stem.push('_');
    }
    format!("{stem}{suffix}{ext}")
}

/// What the ticket says the file at the name must still be.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Expect {
    pub version_id: u64,
    pub ino: u64,
    pub size: u64,
    pub mtime_ns: i64,
    pub ctime_ns: i64,
    pub crc64: u64,
    pub hidden_name: String,
}

impl Expect {
    pub fn from_json(v: &Value) -> Result<Expect, String> {
        let num = |k: &str| {
            v.get(k).and_then(|x| {
                x.as_i64()
                    .or_else(|| x.as_str().and_then(|s| s.parse().ok()))
            })
        };
        let hidden = v["hidden_name"].as_str().unwrap_or_default().to_owned();
        // Over 255 bytes it is no bad ticket: the replace is given up (E5).
        if !hidden.starts_with(HIDDEN_PREFIX) || hidden.contains('/') || hidden.contains('\0') {
            return Err("replace: hidden_name must be an .omnifile-replacing- name".into());
        }
        Ok(Expect {
            version_id: num("version_id").ok_or("replace: version_id")? as u64,
            ino: v["nas_file_id"]
                .as_str()
                .and_then(|s| s.parse().ok())
                .ok_or("replace: nas_file_id")?,
            size: v["size"].as_u64().ok_or("replace: size")?,
            mtime_ns: num("mtime_ns").ok_or("replace: mtime_ns")?,
            ctime_ns: num("ctime_ns").ok_or("replace: ctime_ns")?,
            crc64: v["crc64"]
                .as_str()
                .and_then(|h| u64::from_str_radix(h, 16).ok())
                .ok_or("replace: crc64")?,
            hidden_name: hidden,
        })
    }

    fn rm(&self) -> remover::Expect {
        remover::Expect {
            ino: self.ino,
            size: self.size,
            mtime_ns: self.mtime_ns,
            ctime_ns: Some(self.ctime_ns),
        }
    }
}

/// A replace as journalled.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Row {
    pub placement_id: u64,
    pub path: String,
    /// `replacing` (W1 may run), `hidden`, `grace_passed` (R3 may run), `deleting`
    /// (W2 may run), `putting_back`, `putting_aside` (W3 may run), or an end:
    /// `refused` (nothing hidden), `old_gone`, `put_back`, `put_aside`, `old_deleted`.
    pub state: String,
    pub hidden_name: String,
    pub ino: u64,
    pub hidden_at: Option<i64>,
    pub hidden_ctime: Option<i64>,
    pub aside_name: Option<String>,
    pub placeholder: Option<(u64, i64)>,
    /// The name W3's placeholder was made at.
    pub placeholder_name: Option<String>,
    /// The inode of the folder the hide was made in.
    pub folder_ino: Option<u64>,
    /// Where that folder is now (below the placement root), if a user moved it.
    pub hidden_folder: Option<String>,
    pub reason: Option<String>,
    pub unreported: Option<String>,
}

/// The states in which the old file may be hidden.
const HIDDEN_STATES: [&str; 6] = [
    "replacing",
    "hidden",
    "grace_passed",
    "deleting",
    "putting_back",
    "putting_aside",
];

fn get(db: &AnnexDb, pid: u64) -> rusqlite::Result<Option<Row>> {
    db.conn()
        .query_row(
            "SELECT placement_id, path, state, hidden_name, ino, hidden_at, hidden_ctime, \
             aside_name, placeholder_ino, placeholder_ctime, reason, unreported, \
             placeholder_name, folder_ino, hidden_folder \
             FROM placement_replace WHERE placement_id = ?1",
            [pid as i64],
            |r| {
                Ok(Row {
                    placement_id: r.get::<_, i64>(0)? as u64,
                    path: r.get(1)?,
                    state: r.get(2)?,
                    hidden_name: r.get(3)?,
                    ino: r.get::<_, i64>(4)? as u64,
                    hidden_at: r.get(5)?,
                    hidden_ctime: r.get(6)?,
                    aside_name: r.get(7)?,
                    placeholder: match (r.get::<_, Option<i64>>(8)?, r.get::<_, Option<i64>>(9)?) {
                        (Some(i), Some(c)) => Some((i as u64, c)),
                        _ => None,
                    },
                    reason: r.get(10)?,
                    unreported: r.get(11)?,
                    placeholder_name: r.get(12)?,
                    folder_ino: r.get::<_, Option<i64>>(13)?.map(|v| v as u64),
                    hidden_folder: r.get(14)?,
                })
            },
        )
        .optional()
}

/// Whether a replace may have the file at `path` (below the annex's root) hidden:
/// its departure isn't a user's delete, and isn't reported.
pub fn replacing(db: &AnnexDb, path: &str) -> bool {
    db.conn()
        .query_row(
            "SELECT EXISTS (SELECT 1 FROM placement_replace WHERE path = ?1 AND state IN \
             ('replacing', 'hidden', 'grace_passed', 'deleting', 'putting_back', \
              'putting_aside'))",
            [path],
            |r| r.get::<_, bool>(0),
        )
        // A journal that can't be read says nothing is safe to report.
        .unwrap_or(true)
}

/// The hidden name, if the old file may be hidden now (for a stuck report).
pub fn hidden_now(db: &AnnexDb, pid: u64) -> Option<String> {
    get(db, pid)
        .ok()
        .flatten()
        .filter(|r| HIDDEN_STATES.contains(&r.state.as_str()))
        .map(|r| r.hidden_name)
}

/// What a W3 came to.
#[derive(Debug, PartialEq, Eq)]
enum W3 {
    /// The old file is under the name.
    Done,
    /// The name is someone else's.
    Taken,
    /// Not there to move: a user took it away.
    Gone,
    /// Might clear: tried again later.
    Retry(String),
    /// The NAS refuses it.
    Refused(String),
}

/// Send a replace's facts not yet acknowledged, in order. `false` if the service
/// can't be reached: they stay, and nothing that needs them goes on.
pub async fn report<S: AnnexService>(svc: &S, db: &AnnexDb, pid: u64, now: i64) -> bool {
    #[cfg(test)]
    if faults::REPORT_FAILS.with(|x| {
        let n = x.get();
        x.set(n.saturating_sub(1));
        n > 0
    }) {
        return false;
    }
    let Ok(Some(r)) = get(db, pid) else {
        return true;
    };
    let Some(list) = r.unreported.clone() else {
        return true;
    };
    let mut left: Vec<&str> = list.split(',').filter(|s| !s.is_empty()).collect();
    while let Some(step) = left.first().copied() {
        let body = match step {
            "hidden" => json!({"step": "replace_hidden", "hidden_name": r.hidden_name}),
            "put_back" => json!({"step": "replace_put_back",
                                 "reason": r.reason.clone().unwrap_or_default()}),
            "aside" => json!({"step": "replace_aside", "name": r.aside_name,
                              "reason": r.reason.clone().unwrap_or_default()}),
            "replaced" => json!({"step": "replaced"}),
            // The old file went by a user's hand: nothing of it is hidden, and
            // nothing was deleted by the edge.
            "gone" => json!({"step": "replace_gone"}),
            _ => {
                left.remove(0);
                continue;
            }
        };
        let ask = format!("replace:{step}:{now}");
        if let Err(e) = svc.placement_step(pid, &body, &ask).await {
            warn!(placement = pid, step, error = %e, "replace: report later");
            let rest = left.join(",");
            let _ = db.conn().execute(
                "UPDATE placement_replace SET unreported = ?2 WHERE placement_id = ?1",
                params![pid as i64, rest],
            );
            return false;
        }
        left.remove(0);
    }
    let _ = db.conn().execute(
        "UPDATE placement_replace SET unreported = NULL WHERE placement_id = ?1",
        [pid as i64],
    );
    true
}

/// A test hook: a write to the old file, run at a named point of the replace.
#[cfg(test)]
#[allow(dead_code)] // the Linux tests use them
pub(crate) mod faults {
    use std::cell::RefCell;
    type Hook = Box<dyn FnOnce()>;
    thread_local! {
        pub static AT: RefCell<Vec<(&'static str, Hook)>> = const { RefCell::new(Vec::new()) };
        /// R1's rename fails with this errno (an SMB sharing violation is EBUSY).
        pub static W1_ERRNO: std::cell::Cell<i32> = const { std::cell::Cell::new(0) };
        /// W3 is refused by the NAS (a folder gone read-only).
        pub static W3_REFUSED: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
        /// The next so many reports of the replace's facts don't reach the service.
        pub static REPORT_FAILS: std::cell::Cell<u32> = const { std::cell::Cell::new(0) };
    }
    pub fn at(point: &'static str, h: impl FnOnce() + 'static) {
        AT.with(|v| v.borrow_mut().push((point, Box::new(h))));
    }
    pub fn run(point: &str) {
        let h = AT.with(|v| {
            let mut v = v.borrow_mut();
            v.iter()
                .position(|(p, _)| *p == point)
                .map(|i| v.remove(i).1)
        });
        if let Some(h) = h {
            h();
        }
    }
}

/// A file's whole CRC-64/NVME, read off the async loop (hours for a big file).
async fn crc_off_loop(dir: &Root, name: &str) -> io::Result<u64> {
    let d = dir.try_clone()?;
    let n = name.to_owned();
    tokio::task::spawn_blocking(move || rm::crc_of(&d, &n))
        .await
        .map_err(|e| io::Error::other(e.to_string()))?
}

#[cfg(test)]
fn hook(point: &str) {
    faults::run(point);
}
#[cfg(not(test))]
#[inline(always)]
fn hook(_: &str) {}

impl<S: AnnexService> Placer<'_, S> {
    fn replace_row(&self, p: &Placement) -> Option<Row> {
        get(self.db, p.placement_id).ok().flatten()
    }

    /// One journal write of the replace row; `false` if it failed: nothing follows.
    #[must_use]
    fn rset(&self, pid: u64, sets: &[(&str, rusqlite::types::Value)]) -> bool {
        self.rset_fact(pid, sets, None)
    }

    /// `rset` with a fact for the service added to what is unreported, in the same
    /// write: a crash can't keep the state and lose the fact (the confirm pass, C6).
    #[must_use]
    fn rset_fact(
        &self,
        pid: u64,
        sets: &[(&str, rusqlite::types::Value)],
        fact: Option<&str>,
    ) -> bool {
        #[cfg(test)]
        if super::faults::JOURNAL_FAILS.with(|f| f.get()) {
            error!(placement = pid, "annex.db: replace journal (test fault)");
            return false;
        }
        let mut sql = String::from("UPDATE placement_replace SET updated_at = ?2");
        for (i, (k, _)) in sets.iter().enumerate() {
            sql.push_str(&format!(", {k} = ?{}", i + 3));
        }
        let mut vals: Vec<rusqlite::types::Value> = vec![(pid as i64).into(), self.now.into()];
        vals.extend(sets.iter().map(|(_, v)| v.clone()));
        if let Some(f) = fact {
            let n = vals.len() + 1;
            sql.push_str(&format!(
                ", unreported = CASE WHEN coalesce(unreported, '') = '' THEN ?{n} \
                 ELSE unreported || ',' || ?{n} END"
            ));
            vals.push(super::text(f));
        }
        sql.push_str(" WHERE placement_id = ?1");
        match self
            .db
            .conn()
            .execute(&sql, rusqlite::params_from_iter(vals))
        {
            Ok(_) => true,
            Err(e) => {
                error!(placement = pid, error = %e, "annex.db: replace journal; nothing written");
                false
            }
        }
    }

    fn retry_hidden(&self, p: &Placement, t: &Ticket, why: &str) -> Flow {
        let hidden = self
            .replace_row(p)
            .map(|r| r.hidden_name)
            .unwrap_or_default();
        self.retry_later(
            p,
            &format!("{why}; the old file is hidden as {}/{hidden}", t.folder),
        );
        Flow::Stop
    }

    /// Where a link of the own name meets a file (P4's `EEXIST`): a replace is
    /// begun, once. `true` if the caller stops here (the placement goes on from
    /// its new state).
    pub(super) fn replace_wanted(&self, p: &Placement, t: &Ticket, i: usize, name: &str) -> bool {
        if i != 0 || name != t.name || t.replace.is_none() {
            return false;
        }
        if self.replace_row(p).is_some() {
            return false; // looked at once: a file at the name now is a user's
        }
        // A journal that can't be written: the caller stops, and nothing is written.
        let _ = self.set(p, "replace_start", &[]);
        true
    }

    /// What `done` says of a replace: `replaced`, `replaced_old_kept` or nothing.
    pub(super) fn replace_outcome(&self, p: &Placement) -> Option<&'static str> {
        match self.replace_row(p)?.state.as_str() {
            "old_deleted" | "old_gone" => Some("replaced"),
            "put_aside" => Some("replaced_old_kept"),
            _ => None,
        }
    }

    /// The old file still to be dealt with after the new one has its name (R4).
    pub(super) fn replace_pending(&self, p: &Placement) -> bool {
        self.replace_row(p).is_some_and(|r| {
            matches!(
                r.state.as_str(),
                "grace_passed" | "deleting" | "putting_aside" | "hidden" | "putting_back"
            )
        })
    }

    /// The folder the old file is in: the ticket's, or where a user moved it (C1).
    fn target(&self, p: &Placement, t: &Ticket) -> Result<Root, Flow> {
        let folder = self
            .replace_row(p)
            .and_then(|r| r.hidden_folder)
            .unwrap_or_else(|| t.folder.clone());
        match self.pl.sub(&folder) {
            Ok(d) => Ok(d),
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                self.fail(p, "target_missing");
                Err(Flow::Stop)
            }
            Err(e) => {
                self.retry_later(p, &format!("target: {e}"));
                Err(Flow::Stop)
            }
        }
    }

    /// The edge's own checks on the file at the name, before R1: the ticket's
    /// file, one link, no stream, and over SMB its CRC read again.
    async fn old_gates(&self, dir: &Root, name: &str, e: &Expect, ctime: i64) -> Check {
        match remover::local_gates(dir, name, &e.rm(), self.smb, Some(ctime)) {
            Check::Ok => {}
            other => return other,
        }
        hook("crc");
        if self.smb {
            match crc_off_loop(dir, name).await {
                Ok(c) if c == e.crc64 => {}
                Ok(_) => return Check::Refuse("crc_mismatch".into()),
                Err(err) if err.kind() == io::ErrorKind::NotFound => return Check::Gone,
                Err(err) => return Check::Transient(format!("read: {err}")),
            }
            // A write while it was read is seen now.
            return remover::local_gates(dir, name, &e.rm(), self.smb, Some(ctime));
        }
        Check::Ok
    }

    /// No replace after all: nothing is hidden. The service hears why, and the new
    /// file takes a `-restored` name.
    fn not_replaced(&self, p: &Placement, t: &Ticket, staging: &Root, why: &str) -> Flow {
        let e = t.replace.as_ref().expect("a replace ticket");
        let ok = self
            .db
            .conn()
            .execute(
                "INSERT OR REPLACE INTO placement_replace (placement_id, path, state, \
                 hidden_name, ino, size, mtime_ns, ctime_ns, crc64, reason, unreported, \
                 updated_at) VALUES (?1, ?2, 'refused', ?3, ?4, ?5, ?6, ?7, ?8, ?9, 'put_back', ?10)",
                params![
                    p.placement_id as i64,
                    t.path,
                    e.hidden_name,
                    e.ino as i64,
                    e.size as i64,
                    e.mtime_ns,
                    e.ctime_ns,
                    format!("{:016x}", e.crc64),
                    why,
                    self.now
                ],
            )
            .is_ok();
        if !ok {
            return Flow::Stop;
        }
        info!(
            placement = p.placement_id,
            why, "replace: not replaced; a -restored name"
        );
        self.link(p, t, staging, 1)
    }

    /// R1, with its gates (placer state `replace_start`).
    pub(super) async fn replace_begin(&self, p: &Placement, t: &Ticket) -> Flow {
        let Some(e) = t.replace.clone() else {
            return Flow::Stop;
        };
        if self.crash_point("replace_start") == Flow::Stop {
            return Flow::Stop;
        }
        let Some(staging) = self.staging_or_wait(p) else {
            return Flow::Stop;
        };
        if let Some(r) = self.replace_row(p) {
            // Begun before a restart.
            return match r.state.as_str() {
                "replacing" => self.resume_hiding(p, t).await,
                "refused" | "put_back" => self.link(p, t, &staging, 1),
                _ => self.link(p, t, &staging, 0),
            };
        }
        // A name too long to hide under (E5): no replace, as the service refuses it.
        if e.hidden_name.len() > 255 {
            return self.not_replaced(p, t, &staging, "name_too_long");
        }
        let dir = match self.target(p, t) {
            Ok(d) => d,
            Err(f) => return f,
        };
        match self.old_gates(&dir, &t.name, &e, e.ctime_ns).await {
            Check::Ok => {}
            Check::Gone => return self.name_freed(p, t, &staging, &e),
            Check::Transient(why) => {
                self.retry_later(p, &format!("replace: {why}"));
                return Flow::Stop;
            }
            Check::Refuse(why) => return self.not_replaced(p, t, &staging, &why),
        }
        let ans = match self
            .ask(
                p,
                json!({"step": "replace_go", "hidden_name": e.hidden_name}),
            )
            .await
        {
            Ok(a) => a,
            Err(err) => {
                self.retry_later(p, &format!("replace_go: {err}"));
                return Flow::Stop;
            }
        };
        if !ans["proceed"].as_bool().unwrap_or(false) {
            if ans["hold"].as_bool().unwrap_or(false) {
                let _ = self.db.conn().execute(
                    "UPDATE placement SET next_at = ?2 WHERE placement_id = ?1",
                    params![p.placement_id as i64, self.now + PAUSE_RETRY_MS],
                );
                return Flow::Stop;
            }
            let why = ans["reason"].as_str().unwrap_or("refused").to_owned();
            if ans["state"].as_str() == Some("failed") {
                // The placement ended: nothing was hidden; clean up as `go` refused.
                return self.ended(p, &why);
            }
            return self.not_replaced(p, t, &staging, &why);
        }
        // The gates again, right before the write.
        hook("after_replace_go");
        match self.old_gates(&dir, &t.name, &e, e.ctime_ns).await {
            Check::Ok => {}
            Check::Gone => return self.name_freed(p, t, &staging, &e),
            Check::Transient(why) => {
                self.retry_later(p, &format!("replace: {why}"));
                return Flow::Stop;
            }
            Check::Refuse(why) => return self.not_replaced(p, t, &staging, &why),
        }
        // The folder the hide is made in, by inode: a hidden file is never taken as
        // gone because a user moved its folder (C1).
        let folder_ino = match dir.stat("", false) {
            Ok(st) => st.ino,
            Err(err) => {
                self.retry_later(p, &format!("replace: folder: {err}"));
                return Flow::Stop;
            }
        };
        // Journalled before the write: the row first (what a restart reads), then
        // the state.
        let row = self.db.conn().execute(
            "INSERT INTO placement_replace (placement_id, path, state, hidden_name, ino, size, \
             mtime_ns, ctime_ns, crc64, updated_at, folder_ino) \
             VALUES (?1, ?2, 'replacing', ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
            params![
                p.placement_id as i64,
                t.path,
                e.hidden_name,
                e.ino as i64,
                e.size as i64,
                e.mtime_ns,
                e.ctime_ns,
                format!("{:016x}", e.crc64),
                self.now,
                folder_ino as i64
            ],
        );
        #[cfg(test)]
        let row = if super::faults::JOURNAL_FAILS.with(|f| f.get()) {
            Err(rusqlite::Error::InvalidQuery)
        } else {
            row
        };
        if let Err(err) = row {
            error!(placement = p.placement_id, error = %err, "annex.db: replace journal; nothing hidden");
            return Flow::Stop;
        }
        if !self.set(p, "replace_hiding", &[]) {
            return Flow::Stop;
        }
        if self.crash_point("replace_hiding") == Flow::Stop {
            return Flow::Stop;
        }
        hook("before_w1");
        #[cfg(test)]
        let w1 = match faults::W1_ERRNO.with(|x| x.replace(0)) {
            0 => rm::w1_hide(&dir, &t.name, &e.hidden_name),
            n => Err(io::Error::from_raw_os_error(n)),
        };
        #[cfg(not(test))]
        let w1 = rm::w1_hide(&dir, &t.name, &e.hidden_name);
        match w1 {
            Ok(()) => {
                if self.crash_point("replace_w1_taken") == Flow::Stop {
                    return Flow::Stop;
                }
                self.hidden(p, t, &dir, None)
            }
            Err(err)
                if matches!(
                    errno(&err),
                    libc::EBUSY
                        | libc::ETXTBSY
                        | libc::EPERM
                        | libc::EACCES
                        | libc::EEXIST
                        | libc::ENAMETOOLONG
                ) =>
            {
                // Definite: nothing moved (an SMB sharing violation is `in_use`).
                let why = remover::w1_refusal(errno(&err));
                warn!(placement = p.placement_id, error = %err, why, "R1 refused");
                if !self.rset(
                    p.placement_id,
                    &[
                        ("state", super::text("refused")),
                        ("reason", super::text(why)),
                        ("unreported", super::text("put_back")),
                    ],
                ) {
                    return Flow::Stop;
                }
                self.link(p, t, &staging, 1)
            }
            // Anything else: where the file is now says whether it moved.
            Err(err) => {
                warn!(placement = p.placement_id, error = %err, "R1: where is it?");
                self.resume_hiding(p, t).await
            }
        }
    }

    /// The name was free after all (the file went): an ordinary link, and a file at
    /// the name after this is a user's.
    fn name_freed(&self, p: &Placement, t: &Ticket, staging: &Root, e: &Expect) -> Flow {
        let _ = self.db.conn().execute(
            "INSERT OR REPLACE INTO placement_replace (placement_id, path, state, hidden_name, \
             ino, size, mtime_ns, ctime_ns, crc64, unreported, updated_at) \
             VALUES (?1, ?2, 'old_gone', ?3, ?4, ?5, ?6, ?7, ?8, 'gone', ?9)",
            params![
                p.placement_id as i64,
                t.path,
                e.hidden_name,
                e.ino as i64,
                e.size as i64,
                e.mtime_ns,
                e.ctime_ns,
                format!("{:016x}", e.crc64),
                self.now
            ],
        );
        self.link(p, t, staging, 0)
    }

    /// The placement ended by the service's word: what was hidden goes back, what
    /// was staged goes (P7, which puts the old file back first).
    fn ended(&self, p: &Placement, why: &str) -> Flow {
        let clean = self.cleanup(p);
        let _ = self.set(
            p,
            "failed",
            &[
                ("reason", super::text(why)),
                (
                    "leftover",
                    clean.map_or(rusqlite::types::Value::Null, rusqlite::types::Value::Text),
                ),
                ("next_at", super::int(self.now + super::STUCK_RETRY_MS)),
            ],
        );
        Flow::Stop
    }

    /// After R1: the ctime its rename left, taken now, is what the hidden file must
    /// still have (a POSIX client can't set it back). `ctime`: one taken already
    /// (after a restart, with the bytes read again). A different inode under the
    /// hidden name is a user's file W1 hid (a save-by-rename just before it): put
    /// back, never treated as nothing hidden (E2).
    fn hidden(&self, p: &Placement, t: &Ticket, dir: &Root, ctime: Option<i64>) -> Flow {
        let Some(r) = self.replace_row(p) else {
            return Flow::Stop;
        };
        let st = match rm::stat_name(dir, &r.hidden_name) {
            Ok(st) if st.ino == r.ino => st,
            Ok(st) => return self.swapped(p, t, st.ino),
            Err(e) => return self.retry_hidden(p, t, &format!("R1 stat: {e}")),
        };
        if !self.rset_fact(
            p.placement_id,
            &[
                ("state", super::text("hidden")),
                ("hidden_at", super::int(self.now)),
                ("hidden_ctime", super::int(ctime.unwrap_or(st.ctime_ns))),
            ],
            Some("hidden"),
        ) {
            return Flow::Stop;
        }
        info!(
            placement = p.placement_id,
            hidden = r.hidden_name,
            "R1: the old file is hidden"
        );
        if !self.set(
            p,
            "replace_hidden",
            &[("next_at", super::int(self.now + self.grace_ms))],
        ) {
            return Flow::Stop;
        }
        self.crash_point("replace_hidden");
        Flow::Stop
    }

    /// A user's file is under the hidden name (W1 hid it, or it was put there): it
    /// goes back, under its own name if that is free, else kept aside with a
    /// Problem; W3 refused, `stuck` with every byte kept. The replace is given up,
    /// and the new file takes a `-restored` name.
    fn swapped(&self, p: &Placement, t: &Ticket, ino: u64) -> Flow {
        warn!(
            placement = p.placement_id,
            ino, "replace: a user's file is under the hidden name; it goes back"
        );
        if !self.rset(p.placement_id, &[("ino", super::int(ino as i64))]) {
            return Flow::Stop;
        }
        self.put_back(p, t, "swapped_at_hide")
    }

    /// A crash between journalling `replacing` and W1's answer: which side of the
    /// rename is the file on?
    pub(super) async fn resume_hiding(&self, p: &Placement, t: &Ticket) -> Flow {
        let Some(r) = self.replace_row(p) else {
            let _ = self.set(p, "replace_start", &[]);
            return Flow::Next;
        };
        let dir = match self.target(p, t) {
            Ok(d) => d,
            Err(f) => return f,
        };
        let at = |n: &str| rm::stat_name(&dir, n).map(|s| s.ino);
        match (at(&r.hidden_name), at(&t.name)) {
            (Ok(i), _) if i == r.ino => self.recovered(p, t, &dir).await,
            (Ok(i), _) => self.swapped(p, t, i),
            (_, Ok(i)) if i == r.ino => {
                // W1 never ran: asked again, every gate again.
                let _ = self.db.conn().execute(
                    "DELETE FROM placement_replace WHERE placement_id = ?1",
                    [p.placement_id as i64],
                );
                let _ = self.set(p, "replace_start", &[]);
                Flow::Next
            }
            (Err(e), _) | (_, Err(e)) if e.kind() != io::ErrorKind::NotFound => {
                self.retry_hidden(p, t, &format!("R1 stat: {e}"))
            }
            _ => {
                // The hidden name is empty here: found where a user moved its
                // folder, else gone (C1).
                self.went(p, t, false)
            }
        }
    }

    /// E3: found hidden after a restart, before its ctime was journalled. Whatever
    /// happened while the edge was down would become the baseline, so the bytes
    /// are read again whole (NFS and SMB) and must be the verified copy's, with the
    /// ticket's size and mtime and one link. Otherwise, or if the read fails, the
    /// old file goes back and the new one takes a `-restored` name.
    async fn recovered(&self, p: &Placement, t: &Ticket, dir: &Root) -> Flow {
        let e = t.replace.as_ref().expect("a replace ticket");
        let Some(r) = self.replace_row(p) else {
            return Flow::Stop;
        };
        let before = match rm::stat_name(dir, &r.hidden_name) {
            Ok(st) => st,
            Err(err) => return self.retry_hidden(p, t, &format!("R1 stat: {err}")),
        };
        if before.ino != r.ino
            || before.nlink != 1
            || before.size != e.size
            || before.mtime_ns != e.mtime_ns
        {
            return self.put_back(p, t, "changed_while_down");
        }
        hook("recovered_crc");
        match crc_off_loop(dir, &r.hidden_name).await {
            Ok(c) if c == e.crc64 => {}
            Ok(_) => return self.put_back(p, t, "crc_mismatch_while_down"),
            Err(err) => {
                warn!(placement = p.placement_id, error = %err, "R1: the hidden file can't be read");
                return self.put_back(p, t, "unreadable_while_down");
            }
        }
        // Nothing changed while it was read: the ctime before the read holds.
        match remover::local_gates(
            dir,
            &r.hidden_name,
            &e.rm(),
            self.smb,
            Some(before.ctime_ns),
        ) {
            Check::Ok => self.hidden(p, t, dir, Some(before.ctime_ns)),
            Check::Transient(why) => self.retry_hidden(p, t, &why),
            Check::Gone | Check::Refuse(_) => self.put_back(p, t, "changed_while_down"),
        }
    }

    /// Where the hidden file is, and whether it is still exactly the copy.
    async fn check_hidden(&self, dir: &Root, r: &Row, t: &Ticket) -> HiddenCheck {
        let e = t.replace.as_ref().expect("a replace ticket");
        let Some(ctime) = r.hidden_ctime else {
            return HiddenCheck::Changed("no_hidden_ctime".into());
        };
        match self.old_gates(dir, &r.hidden_name, e, ctime).await {
            Check::Ok => HiddenCheck::Ok,
            Check::Transient(why) => HiddenCheck::Transient(why),
            Check::Refuse(why) => HiddenCheck::Changed(format!("{why}_in_grace")),
            Check::Gone => match rm::stat_name(dir, &t.name) {
                Ok(st) if st.ino == r.ino => HiddenCheck::Back,
                Ok(_) => HiddenCheck::Gone,
                Err(e) if e.kind() == io::ErrorKind::NotFound => HiddenCheck::Gone,
                Err(e) => HiddenCheck::Transient(format!("stat: {e}")),
            },
        }
    }

    /// R2, after the grace (placer state `replace_hidden`).
    pub(super) async fn after_grace(&self, p: &Placement, t: &Ticket) -> Flow {
        let Some(r) = self.replace_row(p) else {
            return Flow::Stop;
        };
        let hidden_at = r.hidden_at.unwrap_or(self.now);
        if self.now < hidden_at + self.grace_ms {
            let _ = self.db.conn().execute(
                "UPDATE placement SET next_at = ?2 WHERE placement_id = ?1",
                params![p.placement_id as i64, hidden_at + self.grace_ms],
            );
            return Flow::Stop;
        }
        // The service hears the hide before it is asked about it.
        if !report(self.svc, self.db, p.placement_id, self.now).await {
            return self.retry_hidden(p, t, "replace: the service later");
        }
        let dir = match self.target(p, t) {
            Ok(d) => d,
            Err(f) => return f,
        };
        hook("r2");
        match self.check_hidden(&dir, &r, t).await {
            HiddenCheck::Ok => {}
            HiddenCheck::Transient(why) => return self.retry_hidden(p, t, &why),
            HiddenCheck::Back => return self.back_by_a_user(p, t),
            HiddenCheck::Gone => return self.old_went(p, t),
            HiddenCheck::Changed(why) => return self.put_back(p, t, &why),
        }
        let ans = match self
            .ask(p, json!({"step": "replace_delete_go", "phase": "place"}))
            .await
        {
            Ok(a) => a,
            Err(e) => return self.retry_hidden(p, t, &format!("replace_delete_go: {e}")),
        };
        if !ans["proceed"].as_bool().unwrap_or(false) {
            if ans["hold"].as_bool().unwrap_or(false) {
                let wait = if ans["reason"] == "grace" {
                    GRACE_ASK_MS
                } else {
                    PAUSE_RETRY_MS
                };
                let _ = self.db.conn().execute(
                    "UPDATE placement SET next_at = ?2 WHERE placement_id = ?1",
                    params![p.placement_id as i64, self.now + wait],
                );
                return Flow::Stop;
            }
            let why = ans["reason"].as_str().unwrap_or("refused").to_owned();
            if ans["state"].as_str() == Some("failed") {
                return self.ended(p, &why);
            }
            return self.put_back(p, t, &why);
        }
        // R3's gate: the staged file is still what was secured.
        let Some(staging) = self.staging_or_wait(p) else {
            return Flow::Stop;
        };
        match self.still_secured(p, t, &staging) {
            Ok(true) => {}
            Ok(false) => {
                self.fail(p, "staging_file_changed");
                return Flow::Stop;
            }
            Err(why) => return self.retry_hidden(p, t, &why),
        }
        if !self.rset(p.placement_id, &[("state", super::text("grace_passed"))]) {
            return Flow::Stop;
        }
        if self.crash_point("replace_grace_passed") == Flow::Stop {
            return Flow::Stop;
        }
        self.link(p, t, &staging, 0)
    }

    /// The forced `statx` of the staged file against `secured`, as at `go`.
    fn still_secured(&self, p: &Placement, t: &Ticket, staging: &Root) -> Result<bool, String> {
        let st = match rm::stat_name(staging, &p.staging_name) {
            Ok(s) => s,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
            Err(e) => return Err(format!("statx: {e}")),
        };
        let rel = format!("{}/{}", self.staging_rel(), p.staging_name);
        let print = self.staged_print(&rel, &st)?;
        Ok(Some(st.ino) == p.staging_ino
            && st.size == t.size
            && Some(st.ctime_ns) == p.secured_ctime
            && st.nlink == 1
            && Some(print) == p.secured_acl)
    }

    /// A user renamed the hidden file back under its own name in the grace: it is
    /// theirs as it is, and the new file takes a `-restored` name.
    fn back_by_a_user(&self, p: &Placement, t: &Ticket) -> Flow {
        if !self.rset_fact(
            p.placement_id,
            &[
                ("state", super::text("put_back")),
                ("reason", super::text("renamed_back")),
            ],
            Some("put_back"),
        ) {
            return Flow::Stop;
        }
        let Some(staging) = self.staging_or_wait(p) else {
            return Flow::Stop;
        };
        self.link(p, t, &staging, 1)
    }

    /// The hidden file went in the grace (a user took it away): nothing to delete,
    /// and the name is the new file's.
    fn old_went(&self, p: &Placement, t: &Ticket) -> Flow {
        self.went(p, t, false)
    }

    /// The hidden name is empty where the old file was hidden. Never taken as gone
    /// while it may be elsewhere (the confirm pass, C1, C5): kept aside already (a
    /// W3 whose reply was lost), or in the folder a user moved, where it goes back
    /// and the replace is given up. Gone only when it is in neither place; the
    /// hide's folder nowhere to be found is `stuck`, with a Problem.
    fn went(&self, p: &Placement, t: &Ticket, linked: bool) -> Flow {
        let Some(r) = self.replace_row(p) else {
            return Flow::Stop;
        };
        match self.locate(&r, t) {
            Where::Nowhere => {
                // A placeholder of ours goes first: it never squats on the name (C4).
                if r.placeholder.is_some() {
                    let dir = match self.target(p, t) {
                        Ok(d) => d,
                        Err(f) => return f,
                    };
                    if let Err(why) = self.drop_placeholder(&r, &dir, &t.name) {
                        return self.retry_hidden(p, t, &why);
                    }
                }
                if !self.rset_fact(
                    p.placement_id,
                    &[("state", super::text("old_gone"))],
                    Some("gone"),
                ) {
                    return Flow::Stop;
                }
                if linked {
                    return self.then_own(p, t);
                }
                let Some(staging) = self.staging_or_wait(p) else {
                    return Flow::Stop;
                };
                match self.reload(p) {
                    Some(q) => self.link(&q, t, &staging, 0),
                    None => Flow::Stop,
                }
            }
            Where::Aside => {
                if !self.rset_fact(
                    p.placement_id,
                    &[("state", super::text("put_aside"))],
                    Some("aside"),
                ) {
                    return Flow::Stop;
                }
                self.after_old(p, t, linked)
            }
            Where::Back => {
                if !self.rset_fact(
                    p.placement_id,
                    &[("state", super::text("put_back"))],
                    Some("put_back"),
                ) {
                    return Flow::Stop;
                }
                self.after_old(p, t, linked)
            }
            Where::Moved(folder) => {
                warn!(
                    placement = p.placement_id,
                    folder, "replace: the old file's folder moved"
                );
                if !self.rset(p.placement_id, &[("hidden_folder", super::text(folder))]) {
                    return Flow::Stop;
                }
                if linked {
                    self.keep_old(p, t, "folder_moved")
                } else {
                    self.put_back(p, t, "folder_moved")
                }
            }
            Where::Lost(why) => self.hidden_stuck(p, t, &why),
            Where::Later(why) => self.retry_hidden(p, t, &why),
        }
    }

    /// Where the old file is, its hidden name being empty where it was looked for.
    fn locate(&self, r: &Row, t: &Ticket) -> Where {
        let here = r.hidden_folder.clone().unwrap_or_else(|| t.folder.clone());
        let dir = match self.pl.sub(&here) {
            Ok(d) => Some(d),
            Err(e) if e.kind() == io::ErrorKind::NotFound => None,
            Err(e) => return Where::Later(format!("folder: {e}")),
        };
        let mut same_folder = false;
        if let Some(d) = &dir {
            same_folder = match d.stat("", false) {
                Ok(st) => r.folder_ino.is_none_or(|i| i == st.ino),
                Err(e) => return Where::Later(format!("folder: {e}")),
            };
        }
        if same_folder && let Some(d) = &dir {
            let at = |n: &str| rm::stat_name(d, n).map(|st| st.ino);
            match at(&r.hidden_name) {
                Ok(i) if i == r.ino => return Where::Later("it is still hidden".into()),
                Ok(_) => {}
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => return Where::Later(format!("stat: {e}")),
            }
            if let Some(a) = &r.aside_name
                && at(a).is_ok_and(|i| i == r.ino)
            {
                return Where::Aside;
            }
            if at(&t.name).is_ok_and(|i| i == r.ino) {
                return Where::Back;
            }
        }
        match rm::find_hidden(self.pl, &r.hidden_name, r.ino) {
            Ok(Some(f)) if f != here => Where::Moved(f),
            Ok(Some(_)) => Where::Later("found where it was looked for".into()),
            Ok(None) if same_folder => Where::Nowhere,
            Ok(None) => Where::Lost(format!(
                "the folder the old file was hidden in ({here}) was moved or removed, and \
                 {} isn't found",
                r.hidden_name
            )),
            Err(e) => Where::Later(format!("search: {e}")),
        }
    }

    /// A gate failed at R2: the old file goes back under its own name (W3), and the
    /// new file takes a `-restored` name. A user's file made at the name in the
    /// grace keeps it: the old one is kept aside instead.
    pub(super) fn put_back(&self, p: &Placement, t: &Ticket, why: &str) -> Flow {
        if !self.rset(
            p.placement_id,
            &[
                ("state", super::text("putting_back")),
                ("reason", super::text(why)),
            ],
        ) || !self.set(p, "replace_putting_back", &[])
        {
            return Flow::Stop;
        }
        if self.crash_point("replace_putting_back") == Flow::Stop {
            return Flow::Stop;
        }
        // As journalled now: a trouble from here sticks at this step, not the last.
        match self.reload(p) {
            Some(q) => self.resume_put_back(&q, t),
            None => Flow::Stop,
        }
    }

    pub(super) fn resume_put_back(&self, p: &Placement, t: &Ticket) -> Flow {
        let dir = match self.target(p, t) {
            Ok(d) => d,
            Err(f) => return f,
        };
        let Some(r) = self.replace_row(p) else {
            return Flow::Stop;
        };
        match self.w3(&r, &dir, &t.name) {
            W3::Done => {
                info!(
                    placement = p.placement_id,
                    "W3: the old file is back under its name"
                );
                if !self.rset_fact(
                    p.placement_id,
                    &[("state", super::text("put_back"))],
                    Some("put_back"),
                ) {
                    return Flow::Stop;
                }
                let Some(staging) = self.staging_or_wait(p) else {
                    return Flow::Stop;
                };
                self.link(p, t, &staging, 1)
            }
            W3::Taken => self.aside(p, t, r.reason.as_deref().unwrap_or("name_taken")),
            W3::Gone => self.old_went(p, t),
            W3::Retry(why) => self.retry_hidden(p, t, &why),
            W3::Refused(why) => self.hidden_stuck(p, t, &why),
        }
    }

    fn hidden_stuck(&self, p: &Placement, t: &Ticket, why: &str) -> Flow {
        let hidden = self
            .replace_row(p)
            .map(|r| r.hidden_name)
            .unwrap_or_default();
        self.stuck(
            p,
            &format!("{why}: the old file stays hidden as {}/{hidden}", t.folder),
        );
        Flow::Stop
    }

    /// R4 (placer state `linked` with the old file still hidden, or
    /// `replace_deleting`, `replace_putting_aside`).
    pub(super) async fn finish_old(&self, p: &Placement, t: &Ticket) -> Flow {
        let Some(r) = self.replace_row(p) else {
            return self.own(p, t);
        };
        let dir = match self.target(p, t) {
            Ok(d) => d,
            Err(f) => return f,
        };
        match r.state.as_str() {
            "putting_aside" => {
                return self.aside(p, t, r.reason.as_deref().unwrap_or("changed"));
            }
            "hidden" | "putting_back" => {
                // A link was made before W3 could run (a restart): the old file goes
                // back under its own name if that is free, else it is kept aside.
                return self.keep_old(p, t, r.reason.as_deref().unwrap_or("changed"));
            }
            "deleting" => match rm::stat_name(&dir, &r.hidden_name) {
                Ok(st) if st.ino == r.ino => {
                    if !self.rset(p.placement_id, &[("state", super::text("grace_passed"))]) {
                        return Flow::Stop;
                    }
                }
                Ok(st) => {
                    // A user's file under our hidden name (C2): never taken as
                    // deleted, and never left hidden. It is kept, with a Problem.
                    if !self.rset(
                        p.placement_id,
                        &[
                            ("ino", super::int(st.ino as i64)),
                            ("reason", super::text("swapped_at_delete")),
                        ],
                    ) {
                        return Flow::Stop;
                    }
                    return self.keep_old(p, t, "swapped_at_delete");
                }
                Err(e) if e.kind() == io::ErrorKind::NotFound => return self.old_deleted(p, t),
                Err(e) => return self.retry_hidden(p, t, &format!("R4 stat: {e}")),
            },
            "grace_passed" => {}
            _ => return self.own(p, t),
        }
        let r = self.replace_row(p).unwrap_or(r);
        // E1: the old file is deleted only where the new file holds its own name,
        // as this placement's inode. Anything else (a user's file made at the name
        // in the grace, a `-restored` name taken instead): the old file goes back.
        match self.new_holds_name(p, t, &dir) {
            Ok(true) => {}
            Ok(false) => return self.keep_old(p, t, "name_not_replaced"),
            Err(why) => return self.retry_hidden(p, t, &why),
        }
        // The hidden file is checked after the service's answer, right before the
        // delete (below): a change before the question is seen there too.
        hook("r4");
        if !report(self.svc, self.db, p.placement_id, self.now).await {
            return self.retry_hidden(p, t, "replace: the service later");
        }
        let ans = match self
            .ask(
                p,
                json!({"step": "replace_delete_go", "phase": "delete",
                       "name": p.final_name}),
            )
            .await
        {
            Ok(a) => a,
            Err(e) => return self.retry_hidden(p, t, &format!("replace_delete_go: {e}")),
        };
        if !ans["proceed"].as_bool().unwrap_or(false) {
            if ans["hold"].as_bool().unwrap_or(false) {
                let _ = self.db.conn().execute(
                    "UPDATE placement SET next_at = ?2 WHERE placement_id = ?1",
                    params![p.placement_id as i64, self.now + PAUSE_RETRY_MS],
                );
                return Flow::Stop;
            }
            let why = ans["reason"].as_str().unwrap_or("refused").to_owned();
            return self.aside(p, t, &why);
        }
        // The gates once more, right before the delete.
        hook("before_w2");
        match self.check_hidden(&dir, &r, t).await {
            HiddenCheck::Ok => {}
            HiddenCheck::Transient(why) => return self.retry_hidden(p, t, &why),
            HiddenCheck::Back | HiddenCheck::Gone => return self.went(p, t, true),
            HiddenCheck::Changed(why) => return self.aside(p, t, &why),
        }
        hook("before_w2_name");
        match self.new_holds_name(p, t, &dir) {
            Ok(true) => {}
            Ok(false) => return self.keep_old(p, t, "name_not_replaced"),
            Err(why) => return self.retry_hidden(p, t, &why),
        }
        if !self.rset(p.placement_id, &[("state", super::text("deleting"))])
            || !self.set(p, "replace_deleting", &[])
        {
            return Flow::Stop;
        }
        if self.crash_point("replace_deleting") == Flow::Stop {
            return Flow::Stop;
        }
        match rm::w2_delete(&dir, &r.hidden_name) {
            Ok(()) => {
                info!(placement = p.placement_id, "R4: the old file is deleted");
                self.old_deleted(p, t)
            }
            Err(e) if errno(&e) == libc::ENOENT => self.old_deleted(p, t),
            Err(e) => {
                warn!(placement = p.placement_id, error = %e, "R4: W2 failed");
                let _ = self.rset(p.placement_id, &[("state", super::text("grace_passed"))]);
                let _ = self.set(p, "linked", &[]);
                self.retry_hidden(p, t, &format!("R4 unlink: {e}"))
            }
        }
    }

    /// The new file holds the file's own name, as the inode this placement linked.
    fn new_holds_name(&self, p: &Placement, t: &Ticket, dir: &Root) -> Result<bool, String> {
        if p.final_name.as_deref() != Some(t.name.as_str()) || p.final_ino.is_none() {
            return Ok(false);
        }
        match rm::stat_name(dir, &t.name) {
            Ok(st) => Ok(Some(st.ino) == p.final_ino),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(e) => Err(format!("R4 stat: {e}")),
        }
    }

    /// The old file back where the new one didn't replace it (E1): under its own
    /// name if that is free (W3, no-replace), else kept aside under a `-replaced-`
    /// name with a Problem. Nothing is deleted.
    fn keep_old(&self, p: &Placement, t: &Ticket, why: &str) -> Flow {
        if !self.rset(
            p.placement_id,
            &[
                ("state", super::text("putting_back")),
                ("reason", super::text(why)),
            ],
        ) {
            return Flow::Stop;
        }
        let dir = match self.target(p, t) {
            Ok(d) => d,
            Err(f) => return f,
        };
        let Some(r) = self.replace_row(p) else {
            return Flow::Stop;
        };
        let linked = p.final_ino.is_some();
        match self.w3(&r, &dir, &t.name) {
            W3::Done => {
                info!(
                    placement = p.placement_id,
                    why, "W3: the old file is back under its name; nothing replaced"
                );
                if !self.rset_fact(
                    p.placement_id,
                    &[("state", super::text("put_back"))],
                    Some("put_back"),
                ) {
                    return Flow::Stop;
                }
                self.after_old(p, t, linked)
            }
            W3::Taken => self.aside(p, t, why),
            W3::Gone => self.went(p, t, linked),
            W3::Retry(e) => self.retry_hidden(p, t, &e),
            W3::Refused(e) => self.hidden_stuck(p, t, &e),
        }
    }

    fn old_deleted(&self, p: &Placement, t: &Ticket) -> Flow {
        if !self.rset_fact(
            p.placement_id,
            &[("state", super::text("old_deleted"))],
            Some("replaced"),
        ) {
            return Flow::Stop;
        }
        if self.crash_point("replace_old_deleted") == Flow::Stop {
            return Flow::Stop;
        }
        self.then_own(p, t)
    }

    /// The names the old file may be kept under, in order: the one journalled first
    /// (a restart), then `-replaced-<now>` and its numbered variants.
    fn aside_names(&self, r: &Row, t: &Ticket) -> Vec<String> {
        let mut names: Vec<String> = r.aside_name.iter().cloned().collect();
        for i in 0..ASIDE_TRIES {
            let n = aside_name(&t.name, self.now, i);
            if !names.contains(&n) {
                names.push(n);
            }
        }
        names
    }

    /// After R4's end, on to P5 with the placement's state as it is now.
    fn then_own(&self, p: &Placement, t: &Ticket) -> Flow {
        if p.state != "linked" && !self.set(p, "linked", &[]) {
            return Flow::Stop;
        }
        match self.reload(p) {
            Some(q) => self.own(&q, t),
            None => Flow::Stop,
        }
    }

    /// The old file can't go back under its own name: it is kept under the first
    /// free `-replaced-` name (W3, no-replace), with a Problem naming both. Nothing
    /// is deleted. If the new file has its name already (R4), on to P5; if not
    /// (R2, a user took the name in the grace), the new file takes a `-restored` one.
    fn aside(&self, p: &Placement, t: &Ticket, why: &str) -> Flow {
        let Some(r) = self.replace_row(p) else {
            return Flow::Stop;
        };
        let linked = p.final_ino.is_some();
        let dir = match self.target(p, t) {
            Ok(d) => d,
            Err(f) => return f,
        };
        for name in self.aside_names(&r, t) {
            if !self.rset(
                p.placement_id,
                &[
                    ("state", super::text("putting_aside")),
                    ("aside_name", super::text(name.clone())),
                    ("reason", super::text(why)),
                ],
            ) {
                return Flow::Stop;
            }
            if p.state != "replace_putting_aside" && !self.set(p, "replace_putting_aside", &[]) {
                return Flow::Stop;
            }
            if self.crash_point("replace_putting_aside") == Flow::Stop {
                return Flow::Stop;
            }
            // As journalled now: a trouble from here sticks at this step.
            let Some(cur) = self.reload(p) else {
                return Flow::Stop;
            };
            let p = &cur;
            let Some(r) = self.replace_row(p) else {
                return Flow::Stop;
            };
            match self.w3(&r, &dir, &name) {
                W3::Done => {
                    error!(
                        placement = p.placement_id,
                        why,
                        kept_as = name,
                        "ALERT replace: the old file is kept aside; an owner looks"
                    );
                    if !self.rset_fact(
                        p.placement_id,
                        &[("state", super::text("put_aside"))],
                        Some("aside"),
                    ) {
                        return Flow::Stop;
                    }
                    return self.after_old(p, t, linked);
                }
                W3::Taken => continue,
                W3::Gone => return self.went(p, t, linked),
                W3::Retry(e) => return self.retry_hidden(p, t, &e),
                W3::Refused(e) => return self.hidden_stuck(p, t, &e),
            }
        }
        self.hidden_stuck(p, t, "every -replaced- name is taken")
    }

    /// The old file dealt with: P5 if the new file has its name, else a `-restored`
    /// name for it.
    fn after_old(&self, p: &Placement, t: &Ticket, linked: bool) -> Flow {
        if linked {
            return self.then_own(p, t);
        }
        let Some(staging) = self.staging_or_wait(p) else {
            return Flow::Stop;
        };
        match self.reload(p) {
            Some(q) => self.link(&q, t, &staging, 1),
            None => Flow::Stop,
        }
    }

    /// W3: the hidden file under `name`, never replacing anything: a link (refused on
    /// the server if the name exists) then the hidden name's unlink; where a link is
    /// refused, #61's checked placeholder. Resumable at each point.
    fn w3(&self, r: &Row, dir: &Root, name: &str) -> W3 {
        #[cfg(test)]
        if faults::W3_REFUSED.with(|x| x.get()) {
            return W3::Refused("W3: EROFS (test fault)".into());
        }
        // Cut short between its link and its unlink: finish it.
        match rm::finish_link(dir, &r.hidden_name, name, r.ino) {
            Ok(true) => return W3::Done,
            Ok(false) => {}
            Err(e) => return W3::Retry(format!("W3: {e}")),
        }
        let at_name = rm::stat_name(dir, name);
        let at_hidden = rm::stat_name(dir, &r.hidden_name);
        if at_name.as_ref().is_ok_and(|st| st.ino == r.ino)
            && at_hidden
                .as_ref()
                .is_err_and(|e| e.kind() == io::ErrorKind::NotFound)
        {
            return W3::Done; // back already
        }
        if let Err(e) = &at_hidden
            && e.kind() == io::ErrorKind::NotFound
        {
            // Nothing to put back: a placeholder of ours goes first, so it never
            // squats on a name (C4).
            if let Err(why) = self.drop_placeholder(r, dir, name) {
                return W3::Retry(why);
            }
            return W3::Gone;
        }
        if let Some((pino, pctime)) = r.placeholder {
            let made_at = r.placeholder_name.as_deref().unwrap_or(name);
            if made_at != name {
                // Left at a name given up (E7).
                if let Err(why) = self.drop_placeholder(r, dir, name) {
                    return W3::Retry(why);
                }
            } else {
                match rm::placeholder_intact(dir, name, pino, pctime) {
                    Ok(true) => return self.old_over_placeholder(r, dir, name),
                    Ok(false) => {
                        let _ = self.clear_placeholder(r.placement_id);
                        if at_name.is_ok() {
                            return W3::Taken;
                        }
                    }
                    Err(e) => return W3::Retry(format!("W3 placeholder: {e}")),
                }
            }
        }
        if self.crash_point("replace_w3") == Flow::Stop {
            return W3::Retry("stopped".into());
        }
        match rm::w3_link(dir, &r.hidden_name, name) {
            Ok(()) => W3::Done,
            Err(e) if errno(&e) == libc::EEXIST => W3::Taken,
            Err(e) if errno(&e) == libc::ENOENT => match rm::stat_name(dir, name) {
                Ok(st) if st.ino == r.ino => W3::Done,
                _ => W3::Gone,
            },
            Err(e) if rm::link_refused(&e) => self.w3_placeholder(r, dir, name),
            Err(e) if super::definite(&e) || errno(&e) == libc::EROFS => {
                W3::Refused(format!("W3: {e}"))
            }
            Err(e) => W3::Retry(format!("W3: {e}")),
        }
    }

    /// Our placeholder, removed if it is still exactly ours (a user's file there now
    /// is theirs), then forgotten.
    fn drop_placeholder(&self, r: &Row, dir: &Root, name: &str) -> Result<(), String> {
        let Some((pino, pctime)) = r.placeholder else {
            return Ok(());
        };
        let made_at = r.placeholder_name.as_deref().unwrap_or(name);
        match rm::placeholder_intact(dir, made_at, pino, pctime) {
            Ok(true) => match rm::w2_delete(dir, made_at) {
                Ok(()) => {}
                Err(e) if errno(&e) == libc::ENOENT => {}
                Err(e) => return Err(format!("W3 placeholder: {e}")),
            },
            Ok(false) => {}
            Err(e) => return Err(format!("W3 placeholder: {e}")),
        }
        if !self.clear_placeholder(r.placement_id) {
            return Err("journal".into());
        }
        Ok(())
    }

    fn clear_placeholder(&self, pid: u64) -> bool {
        self.rset(
            pid,
            &[
                ("placeholder_ino", rusqlite::types::Value::Null),
                ("placeholder_ctime", rusqlite::types::Value::Null),
                ("placeholder_name", rusqlite::types::Value::Null),
            ],
        )
    }

    fn w3_placeholder(&self, r: &Row, dir: &Root, name: &str) -> W3 {
        let (pino, pctime) = match rm::make_placeholder(dir, name) {
            Ok(v) => v,
            Err(e) if errno(&e) == libc::EEXIST => return W3::Taken,
            Err(e) if super::definite(&e) || errno(&e) == libc::EROFS => {
                return W3::Refused(format!("W3 placeholder: {e}"));
            }
            Err(e) => return W3::Retry(format!("W3 placeholder: {e}")),
        };
        if !self.rset(
            r.placement_id,
            &[
                ("placeholder_ino", super::int(pino as i64)),
                ("placeholder_ctime", super::int(pctime)),
                ("placeholder_name", super::text(name)),
            ],
        ) {
            // Not journalled: take it away again (it is ours, just made).
            if rm::placeholder_intact(dir, name, pino, pctime).unwrap_or(false) {
                let _ = rm::w2_delete(dir, name);
            }
            return W3::Retry("journal".into());
        }
        let mut r = r.clone();
        r.placeholder = Some((pino, pctime));
        r.placeholder_name = Some(name.to_owned());
        match rm::placeholder_intact(dir, name, pino, pctime) {
            Ok(true) => self.old_over_placeholder(&r, dir, name),
            Ok(false) => W3::Taken,
            Err(e) => W3::Retry(format!("W3 placeholder: {e}")),
        }
    }

    fn old_over_placeholder(&self, r: &Row, dir: &Root, name: &str) -> W3 {
        match rm::rename_over(dir, &r.hidden_name, name) {
            Ok(()) => {
                let _ = self.clear_placeholder(r.placement_id);
                W3::Done
            }
            Err(e) => W3::Retry(format!("W3 rename: {e}")),
        }
    }

    /// P7's first step: a placement that ends with its old file hidden puts it back,
    /// under its own name, else kept aside. What it couldn't do, for the leftover.
    pub(super) fn unwind_old(&self, p: &Placement) -> Option<String> {
        let r = self.replace_row(p)?;
        if !HIDDEN_STATES.contains(&r.state.as_str()) {
            return None;
        }
        let t = self.ticket(p).ok()?;
        let folder = r.hidden_folder.clone().unwrap_or_else(|| t.folder.clone());
        let says = |why: &str| {
            Some(format!(
                "the old file stays hidden as {folder}/{}: {why}",
                r.hidden_name
            ))
        };
        let dir = match self.pl.sub(&folder) {
            Ok(d) => d,
            Err(e) => return says(&e.to_string()),
        };
        if r.state == "replacing" {
            match rm::stat_name(&dir, &r.hidden_name) {
                Ok(st) if st.ino == r.ino => {}
                Ok(st) => {
                    // A user's file W1 hid (E2, E6): it goes back like ours.
                    if !self.rset(
                        p.placement_id,
                        &[
                            ("ino", super::int(st.ino as i64)),
                            ("reason", super::text("swapped_at_hide")),
                        ],
                    ) {
                        return says("journal");
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::NotFound => {
                    // W1 never ran (the file is under its name), or the hidden name
                    // is empty: looked for as anywhere else (C1).
                    if rm::stat_name(&dir, &t.name).is_ok_and(|st| st.ino == r.ino) {
                        let _ = self.rset_fact(
                            p.placement_id,
                            &[
                                ("state", super::text("refused")),
                                ("reason", super::text("never_hidden")),
                            ],
                            Some("put_back"),
                        );
                        return None;
                    }
                    return self.unwind_gone(p, &r, &t, &says);
                }
                Err(e) => return says(&e.to_string()),
            }
        }
        let Some(r) = self.replace_row(p) else {
            return says("journal");
        };
        // Kept aside already, by a W3 whose reply was lost (C5).
        if let Some(a) = &r.aside_name
            && rm::stat_name(&dir, a).is_ok_and(|st| st.ino == r.ino)
            && rm::stat_name(&dir, &r.hidden_name)
                .is_err_and(|e| e.kind() == io::ErrorKind::NotFound)
        {
            let _ = self.rset_fact(
                p.placement_id,
                &[("state", super::text("put_aside"))],
                Some("aside"),
            );
            return None;
        }
        let why = r.reason.clone().unwrap_or_else(|| "ended".into());
        let mut names = vec![t.name.clone()];
        names.extend(self.aside_names(&r, &t));
        for (i, name) in names.iter().enumerate() {
            let mut sets = vec![
                (
                    "state",
                    super::text(if i == 0 {
                        "putting_back"
                    } else {
                        "putting_aside"
                    }),
                ),
                ("reason", super::text(why.clone())),
            ];
            if i > 0 {
                sets.push(("aside_name", super::text(name.clone())));
            }
            if !self.rset(p.placement_id, &sets) {
                return says("journal");
            }
            let Some(r) = self.replace_row(p) else {
                return says("journal");
            };
            match self.w3(&r, &dir, name) {
                W3::Done => {
                    let (end, fact) = if i == 0 {
                        ("put_back", "put_back")
                    } else {
                        ("put_aside", "aside")
                    };
                    let _ =
                        self.rset_fact(p.placement_id, &[("state", super::text(end))], Some(fact));
                    return None;
                }
                W3::Taken => continue,
                W3::Gone => return self.unwind_gone(p, &r, &t, &says),
                W3::Retry(e) | W3::Refused(e) => return says(&e),
            }
        }
        says("every -replaced- name is taken")
    }

    /// The hidden name empty at P7: where the old file is (C1, C5).
    fn unwind_gone(
        &self,
        p: &Placement,
        r: &Row,
        t: &Ticket,
        says: &dyn Fn(&str) -> Option<String>,
    ) -> Option<String> {
        let (state, fact) = match self.locate(r, t) {
            Where::Nowhere => {
                if r.placeholder.is_some() {
                    let folder = r.hidden_folder.clone().unwrap_or_else(|| t.folder.clone());
                    let dropped = self
                        .pl
                        .sub(&folder)
                        .map_err(|e| e.to_string())
                        .and_then(|d| self.drop_placeholder(r, &d, &t.name));
                    if let Err(why) = dropped {
                        return says(&why);
                    }
                }
                ("old_gone", "gone")
            }
            Where::Aside => ("put_aside", "aside"),
            Where::Back => ("put_back", "put_back"),
            Where::Moved(f) => {
                if !self.rset(p.placement_id, &[("hidden_folder", super::text(f))]) {
                    return says("journal");
                }
                return self.unwind_old(p);
            }
            Where::Lost(why) | Where::Later(why) => return says(&why),
        };
        let _ = self.rset_fact(p.placement_id, &[("state", super::text(state))], Some(fact));
        None
    }
}

/// Where an old file is whose hidden name is empty (`locate`).
#[derive(Debug, PartialEq, Eq)]
enum Where {
    /// In neither place: a user took it away.
    Nowhere,
    /// Under its journalled `-replaced-` name already.
    Aside,
    /// Under its own name again.
    Back,
    /// Hidden in the folder a user moved, now here (below the placement root).
    Moved(String),
    /// The hide's folder can't be found: `stuck`, with a Problem.
    Lost(String),
    /// Couldn't be told now.
    Later(String),
}

#[derive(Debug, PartialEq, Eq)]
enum HiddenCheck {
    Ok,
    /// Under its own name again, put there by a user.
    Back,
    /// Taken away.
    Gone,
    Changed(String),
    Transient(String),
}
