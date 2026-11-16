//! The remover (phase A2.2; `docs/milestones/results/wp14-a2.md`, "What
//! writes to the filer"; decisions 51 and 59). **The only code that writes to
//! a NAS**, through the read-write mount that exists only while the annex's
//! removals are live (or, when they stop, while files it hid wait to be put
//! back). Four writes, each on a directory fd opened beneath the annex's root
//! (`openat2`, no symlink, no other mount):
//!
//! | # | write | how |
//! |---|---|---|
//! | W1 | hide `<name>` as `.omnifile-removing-<id>-<name>` | `renameat2(RENAME_NOREPLACE)`; the NFS client refuses rename flags, so there a plain `renameat` once the hidden name is checked absent (it is unique to the removal) |
//! | W2 | delete the hidden name after the grace period | `unlinkat`, once a forced `statx` shows the ticket's file and the ctime W1 left |
//! | W3 | cancel: put the hidden name back | `linkat` to the name (refused atomically, on the server, if the name exists) then `unlinkat` of the hidden name; where a link is refused (an SMB server's protected hard links), an empty placeholder made `O_EXCL` at the name, then the hidden name renamed over it; a name taken since leaves it hidden, `stuck`, raised |
//! | W4 | the pointer `<name>.omnifile.txt` after W2 | `openat(O_CREAT \| O_EXCL \| O_NOFOLLOW)`: never overwrites |
//!
//! **Gates.** The edge never decides a removal alone: each file has a ticket
//! from the service, and the service is asked again right before W1 and W2,
//! checking every gate there. The edge's own checks run before W1 and again
//! before W2:
//! - a forced `statx` matches: inode, size, mtime and ctime (the ticket's
//!   before W1; after it, the ctime W1's own rename left, taken right after
//!   it). A POSIX client can't set a ctime, so a same-size rewrite with its
//!   mtime put back is still seen;
//! - a regular file with one link (decision 42);
//! - over SMB, no alternate data stream (`CIFS_QUERY_INFO`; a failed or empty
//!   answer refuses), and, before W2, the file's bytes read again and their
//!   CRC-64/NVME equal to the verified copy's (SMB lets a client set the
//!   ChangeTime).
//!
//! **Journal.** Each removal's state is written to `annex.db` before the step
//! it names is taken, and a write never runs if that journal write failed. A
//! restart finds the file by inode, also when its folder was renamed. A
//! transient error retries with backoff; a file it hid and can't put back is
//! `stuck`, raised to the service (an owner problem and an alert) and tried
//! again hourly. Every step is reported until the service acknowledges it.

use std::io;
use std::path::Path;

use serde_json::{Value, json};
use tracing::{error, info, warn};

use super::db::{AnnexDb, Removal};
use super::nasfs::{Kind, Root};
use super::service::AnnexService;

pub const HIDDEN_PREFIX: &str = ".omnifile-removing-";
pub const POINTER_SUFFIX: &str = ".omnifile.txt";

/// The text of W3's placeholder, while the hidden file is renamed over it:
/// a user who sees it after a crash knows it isn't theirs, and only this
/// exact text, on the journalled inode, is ever replaced.
pub const PLACEHOLDER_TEXT: &str =
    "OmniFile is restoring this file here; do not edit it. It is replaced in a moment.\n";

/// Why a delete may wait without the removal being cancelled: the hidden
/// file stays hidden (a pause or the window start its grace again).
pub const HOLD_REASONS: [&str; 3] = ["paused", "outside_window", "grace"];

/// A candidate answered "not yet" is asked again after this (ms): a copy
/// not verified yet, or waiting for its in-Azure read.
pub const ASK_AGAIN_MS: i64 = 10_000;

/// Transient errors (EIO, ESTALE, a timeout) are tried again this many
/// times, backing off, before a removal gives up (a cancel, or `stuck`).
pub const MAX_ATTEMPTS: i64 = 8;
const BACKOFF_MS: i64 = 10_000;
const BACKOFF_MAX_MS: i64 = 3_600_000;
/// How often a `stuck` file is tried again (its name may be free by then).
const STUCK_RETRY_MS: i64 = 3_600_000;

/// What a ticket says the file must still be.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Expect {
    pub ino: u64,
    pub size: u64,
    pub mtime_ns: i64,
    pub ctime_ns: Option<i64>,
}

impl From<&Removal> for Expect {
    fn from(r: &Removal) -> Expect {
        Expect {
            ino: r.ino,
            size: r.size,
            mtime_ns: r.mtime_ns,
            ctime_ns: r.ctime_ns,
        }
    }
}

/// `a/b/c.segy` -> (`a/b`, `c.segy`).
pub fn split(path: &str) -> (&str, &str) {
    match path.rfind('/') {
        Some(i) => (&path[..i], &path[i + 1..]),
        None => ("", path),
    }
}

/// The hidden name of removal `id` of `name`.
pub fn hidden_name(id: u64, name: &str) -> String {
    format!("{HIDDEN_PREFIX}{id}-{name}")
}

/// A path as the pointer shows it: control characters (a newline in a name)
/// escaped, so the text can't be made to say something else.
pub fn printable(s: &str) -> String {
    s.chars()
        .flat_map(|c| {
            if c.is_control() {
                c.escape_unicode().collect::<Vec<_>>()
            } else {
                vec![c]
            }
        })
        .collect()
}

/// The pointer's fixed text.
pub fn pointer_text(annex: &str, path: &str, file_id: u64, at: &str) -> String {
    format!(
        "This file was moved to Azure by OmniFile on {at}.\n\
         Annex: {}\nFile: {}\nOmniFile file id: {file_id}\n\n\
         To get it back, open the OmniFile portal, find the file in this annex\n\
         and press Recall. This note is never overwritten and can be deleted.\n",
        printable(annex),
        printable(path),
    )
}

/// Why W1's rename was refused, from its errno: an SMB sharing violation
/// (someone holds the file open without share-delete) is `in_use`.
pub fn w1_refusal(errno: i32) -> &'static str {
    match errno {
        libc::EBUSY | libc::ETXTBSY => "in_use",
        libc::EPERM => "worm",
        libc::EACCES => "denied",
        libc::EEXIST => "hidden_name_taken",
        libc::ENOENT => "gone",
        libc::ENAMETOOLONG => "name_too_long",
        _ => "rename_failed",
    }
}

/// A gate's answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Check {
    Ok,
    /// Not there.
    Gone,
    /// Couldn't be told now (EIO, ESTALE, a timeout): try again later.
    Transient(String),
    /// Must not be removed: why.
    Refuse(String),
}

/// Gate 3, on the edge: the name is the ticket's file, a regular file with
/// one link, and (over SMB) no alternate data stream. `ctime`: the ctime it
/// must have (the ticket's before W1, the one W1 left after it).
pub fn local_gates(dir: &Root, name: &str, e: &Expect, smb: bool, ctime: Option<i64>) -> Check {
    let st = match imp::stat_name(dir, name) {
        Ok(st) => st,
        Err(err) if err.kind() == io::ErrorKind::NotFound => return Check::Gone,
        Err(err) => return Check::Transient(format!("stat: {err}")),
    };
    if st.kind != Kind::File {
        return Check::Refuse("not_a_file".into());
    }
    // A second link (decision 42) first: making one moves the ctime too.
    if st.nlink != 1 {
        return Check::Refuse("hard_linked".into());
    }
    if st.ino != e.ino
        || st.size != e.size
        || st.mtime_ns != e.mtime_ns
        || ctime.is_none_or(|c| c != st.ctime_ns)
    {
        return Check::Refuse("changed".into());
    }
    if smb {
        match imp::streams(dir, name) {
            Ok(0) => {}
            Ok(_) => return Check::Refuse("alternate_data_stream".into()),
            Err(err) => return Check::Refuse(format!("streams_unknown: {err}")),
        }
    }
    Check::Ok
}

/// One pass's tally.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Pass {
    pub ticketed: usize,
    pub hidden: usize,
    pub deleted: usize,
    pub cancelled: usize,
}

/// What a pass works with.
pub struct Remover<'a, S: AnnexService> {
    pub svc: &'a S,
    pub db: &'a AnnexDb,
    /// The annex's root on the read-write mount.
    pub rw: &'a Root,
    pub annex_id: u64,
    pub annex_name: &'a str,
    pub smb: bool,
    pub grace_ms: i64,
    pub pointer_files: bool,
    pub rate_per_hour: u32,
    pub now: i64,
    /// The running rule set's version (sent with each candidate).
    pub rules_version: u64,
    /// Removals aren't live: no new ticket, hide or delete; files this
    /// remover hid are put back (W3), unless `why_not` only holds them.
    pub drain: Option<String>,
    /// Where the debug build's crash points leave their markers.
    pub state_dir: &'a Path,
}

fn errno(e: &io::Error) -> i32 {
    e.raw_os_error().unwrap_or(0)
}

/// Test hooks, per thread (each test runs on its own): a journal that can't
/// be written, W3 failing with EIO, and a filesystem without rename flags.
#[cfg(test)]
#[allow(dead_code)] // the Linux writes use them
pub(crate) mod faults {
    use std::cell::Cell;
    thread_local! {
        pub static JOURNAL_FAILS: Cell<bool> = const { Cell::new(false) };
        pub static W3_EIO: Cell<u32> = const { Cell::new(0) };
        pub static NO_RENAME_FLAGS: Cell<bool> = const { Cell::new(false) };
        pub static LINK_EACCES: Cell<bool> = const { Cell::new(false) };
    }

    use std::path::{Path, PathBuf};
    use std::sync::Mutex;
    type Hook = Box<dyn FnOnce() + Send>;
    /// Directories that stand for an SMB share (no streams), and what runs
    /// right after a CRC read there (a write while it read).
    pub static SMB: Mutex<Vec<(PathBuf, Option<Hook>)>> = Mutex::new(Vec::new());

    pub fn smb_fake(p: &Path) -> bool {
        SMB.lock().unwrap().iter().any(|(d, _)| p.starts_with(d))
    }

    pub fn after_crc(p: &Path) {
        let hook = SMB
            .lock()
            .unwrap()
            .iter_mut()
            .find(|(d, _)| p.starts_with(d))
            .and_then(|(_, h)| h.take());
        if let Some(h) = hook {
            h();
        }
    }
}

impl<S: AnnexService> Remover<'_, S> {
    /// The debug build's crash points (the exit test kills the unit before
    /// and after each write): `OMNIFILE_ANNEX_CRASH_AT=after_w1` aborts there
    /// once (a marker in the state directory keeps the restart going).
    fn crash_point(&self, at: &str) {
        #[cfg(debug_assertions)]
        if std::env::var("OMNIFILE_ANNEX_CRASH_AT").is_ok_and(|v| v == at) {
            let mark = self.state_dir.join(format!("crashed-{at}"));
            if !mark.exists() {
                let _ = std::fs::write(&mark, b"");
                warn!(at, "crash point: aborting");
                std::process::abort();
            }
        }
        let _ = at;
    }

    /// The debug build's hold before W1, so the exit test can change a file
    /// between its ticket and its hide: `OMNIFILE_ANNEX_HOLD_BEFORE_W1=<s>`.
    async fn hold_before_w1(&self) {
        #[cfg(debug_assertions)]
        if let Some(s) = std::env::var("OMNIFILE_ANNEX_HOLD_BEFORE_W1")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
        {
            tokio::time::sleep(std::time::Duration::from_secs(s)).await;
        }
    }

    /// The journal. `false` if it couldn't be written: no write follows.
    #[must_use]
    fn set(
        &self,
        r: &Removal,
        state: &str,
        reason: Option<&str>,
        unreported: Option<&str>,
    ) -> bool {
        #[cfg(test)]
        if faults::JOURNAL_FAILS.with(|f| f.get()) {
            error!(
                removal = r.removal_id,
                "annex.db: removal journal (test fault)"
            );
            return false;
        }
        match self
            .db
            .set_removal(r.removal_id, state, reason, unreported, self.now)
        {
            Ok(()) => true,
            Err(e) => {
                error!(removal = r.removal_id, error = %e, "annex.db: removal journal; nothing written");
                false
            }
        }
    }

    /// Try again later, backing off; past `MAX_ATTEMPTS`, `give_up`.
    fn retry_later(&self, r: &Removal, why: &str, give_up: GiveUp) {
        let n = r.attempts + 1;
        if n >= MAX_ATTEMPTS {
            warn!(
                removal = r.removal_id,
                why, "removal: giving up after {n} attempts"
            );
            let why = format!("gave_up: {why}");
            match give_up {
                GiveUp::Cancel => self.cancelled(r, &why),
                // W3 on the next pass, its own retries afresh.
                GiveUp::Restore => {
                    let _ = self.set(r, "cancelling", Some(&why), None);
                    let _ = self.db.set_removal_retry(r.removal_id, 0, 0);
                }
                GiveUp::Stuck => self.stuck(r, &why),
            }
            return;
        }
        let wait = (BACKOFF_MS << n.min(20)).min(BACKOFF_MAX_MS);
        warn!(
            removal = r.removal_id,
            why,
            attempt = n,
            "removal: try again later"
        );
        let _ = self.db.set_removal_retry(r.removal_id, n, self.now + wait);
    }

    /// Ask before a write. `None`: the service can't be reached (try later).
    async fn ask(&self, r: &Removal, step: &str) -> Option<(bool, Option<String>)> {
        let ask = format!("{}:{}", self.now, r.updated_at);
        match self
            .svc
            .removal_step(r.removal_id, &json!({"step": step}), &ask)
            .await
        {
            Ok(v) => Some((
                v["proceed"].as_bool().unwrap_or(false),
                v["reason"].as_str().map(str::to_owned),
            )),
            Err(e) => {
                warn!(removal = r.removal_id, step, error = %e, "removal: ask later");
                None
            }
        }
    }

    /// Send the steps taken and not yet acknowledged, in order.
    async fn report_pending(&self, r: &Removal) -> bool {
        let Some(list) = r.unreported.clone() else {
            return true;
        };
        let mut left: Vec<&str> = list.split(',').filter(|s| !s.is_empty()).collect();
        while let Some(step) = left.first().copied() {
            let body = match step {
                "hidden" => json!({"step": "hidden", "hidden_name": r.hidden_name}),
                "pointer" => json!({"step": "pointer", "written": r.pointer.unwrap_or(false)}),
                "cancelled" => json!({"step": "cancelled",
                                      "reason": r.reason.clone().unwrap_or_default(),
                                      "kept_as": r.aside_name}),
                "stuck" => json!({"step": "stuck", "hidden_name": r.hidden_name,
                                  "reason": r.reason.clone().unwrap_or_default()}),
                s => json!({"step": s}),
            };
            if let Err(e) = self.svc.removal_step(r.removal_id, &body, step).await {
                warn!(removal = r.removal_id, step, error = %e, "removal: report later");
                let rest = left.join(",");
                let _ = self
                    .db
                    .set_removal(r.removal_id, &r.state, None, Some(&rest), self.now);
                return false;
            }
            left.remove(0);
        }
        let _ = self.db.reported(r.removal_id);
        if matches!(r.state.as_str(), "done" | "cancelled") {
            let _ = self.db.drop_candidate(&r.path);
        }
        true
    }

    fn dir(&self, r: &Removal) -> io::Result<(Root, String)> {
        let (d, name) = split(&r.path);
        Ok((self.rw.sub(d)?, name.to_owned()))
    }

    /// The hidden file's folder, also after a user renamed or moved it: found
    /// by its unique hidden name and its inode. The journal's path follows.
    fn locate_hidden(&self, r: &Removal) -> io::Result<Option<(Root, String, Removal)>> {
        match self.dir(r) {
            Ok((d, name)) => return Ok(Some((d, name, r.clone()))),
            Err(e) if e.kind() != io::ErrorKind::NotFound => return Err(e),
            Err(_) => {}
        }
        let (_, name) = split(&r.path);
        let Some(dir) = imp::find_hidden(self.rw, &r.hidden_name, r.ino)? else {
            return Ok(None);
        };
        let path = if dir.is_empty() {
            name.to_owned()
        } else {
            format!("{dir}/{name}")
        };
        info!(
            removal = r.removal_id,
            from = r.path,
            to = path,
            "removal: its folder moved"
        );
        self.db
            .set_removal_path(r.removal_id, &path)
            .map_err(|e| io::Error::other(e.to_string()))?;
        let mut moved = r.clone();
        moved.path = path;
        let (d, n) = self.dir(&moved)?;
        Ok(Some((d, n, moved)))
    }

    /// Ask for tickets for the files the rules chose (`removal_candidate`).
    pub async fn ask_tickets(&self) -> usize {
        let Ok(cands) = self.db.removal_candidates(self.now - ASK_AGAIN_MS, 100) else {
            return 0;
        };
        let mut asked = Vec::new();
        let mut body = Vec::new();
        for c in cands {
            let Ok(Some(e)) = self.db.entry(&c.path) else {
                let _ = self.db.drop_candidate(&c.path);
                continue;
            };
            if c.rules_version != self.rules_version {
                // Decided under another rule set: decided again by the next
                // age pass or copy, never asked as it was.
                let _ = self.db.drop_candidate(&c.path);
                continue;
            }
            body.push(json!({"file_id": c.file_id.to_string(),
                "version_id": c.version_id.to_string(), "path": c.path,
                "nas_file_id": e.seen.ino.to_string(), "size": e.seen.size,
                "mtime_ns": e.seen.mtime_ns.to_string(), "ctime_ns": e.seen.ctime_ns.to_string(),
                "rule": c.rule, "rules_version": c.rules_version}));
            asked.push(c);
        }
        if body.is_empty() {
            return 0;
        }
        let ask = self.now.to_string();
        let v = match self
            .svc
            .removal_tickets(
                &json!({"annex_id": self.annex_id.to_string(), "candidates": body}),
                &ask,
            )
            .await
        {
            Ok(v) => v,
            Err(e) => {
                warn!(error = %e, "removal tickets: later");
                return 0;
            }
        };
        let mut n = 0;
        for res in v["results"].as_array().cloned().unwrap_or_default() {
            // Matched by file id: never by position.
            let fid = res["file_id"].as_str().and_then(|s| s.parse::<u64>().ok());
            let Some(c) = asked.iter().find(|c| Some(c.file_id) == fid) else {
                warn!(?res, "removal ticket for a file not asked about: ignored");
                continue;
            };
            match res["status"].as_str() {
                Some("ticket") => {
                    let t = &res["removal"];
                    let id = t["removal_id"].as_str().and_then(|s| s.parse().ok());
                    let ino = t["expect"]["nas_file_id"]
                        .as_str()
                        .and_then(|s| s.parse().ok());
                    let num = |v: &Value| {
                        v.as_str()
                            .and_then(|s| s.parse::<i64>().ok())
                            .or_else(|| v.as_i64())
                    };
                    let (Some(id), Some(ino), Some(size), Some(mtime)) = (
                        id,
                        ino,
                        t["expect"]["size"].as_u64(),
                        num(&t["expect"]["mtime_ns"]),
                    ) else {
                        warn!(path = c.path, "removal ticket unreadable: skipped");
                        continue;
                    };
                    let (_, name) = split(&c.path);
                    let r = Removal {
                        removal_id: id,
                        path: c.path.clone(),
                        file_id: c.file_id,
                        version_id: c.version_id,
                        rule: c.rule.clone(),
                        ino,
                        size,
                        mtime_ns: mtime,
                        ctime_ns: num(&t["expect"]["ctime_ns"]),
                        hidden_name: hidden_name(id, name),
                        state: "ticketed".into(),
                        hidden_at: None,
                        reason: None,
                        unreported: None,
                        pointer: None,
                        updated_at: self.now,
                        hidden_ctime: None,
                        crc64: t["expect"]["crc64"]
                            .as_str()
                            .and_then(|h| u64::from_str_radix(h, 16).ok()),
                        attempts: 0,
                        next_at: 0,
                        placeholder: None,
                        aside_name: None,
                        users_file: false,
                    };
                    if self.db.add_removal(&r).unwrap_or(false) {
                        n += 1;
                    }
                    let _ = self.db.asked_candidate(&c.path, self.now, None);
                }
                Some("refused") => {
                    info!(path = c.path, reason = ?res["reason"], "removal refused");
                    let _ = self.db.drop_candidate(&c.path);
                }
                _ => {
                    let _ = self
                        .db
                        .asked_candidate(&c.path, self.now, res["reason"].as_str());
                }
            }
        }
        n
    }

    /// Every removal in progress, one step further if it may go on.
    pub async fn pass(&self) -> Pass {
        let mut p = Pass::default();
        if self.drain.is_none() {
            p.ticketed = self.ask_tickets().await;
        }
        let Ok(open) = self.db.open_removals() else {
            return p;
        };
        for r in open {
            if !self.report_pending(&r).await {
                continue; // the service first: nothing new until it knows
            }
            let Ok(Some(r)) = self.db.removal(r.removal_id) else {
                continue;
            };
            if r.next_at > self.now {
                continue; // backing off
            }
            if let Some(why) = &self.drain {
                self.drain_one(&r, why, &mut p).await;
            } else {
                match r.state.as_str() {
                    "hiding" => self.resume_hiding(&r).await,
                    "ticketed" => {
                        if self.hide(&r).await {
                            p.hidden += 1;
                        }
                    }
                    "hidden" => match self.delete(&r).await {
                        Some(true) => p.deleted += 1,
                        Some(false) => p.cancelled += 1,
                        None => {}
                    },
                    "deleting" => {
                        if self.resume_deleting(&r).await {
                            p.deleted += 1;
                        }
                    }
                    "deleted" | "pointer" => self.pointer(&r).await,
                    "cancelling" | "stuck" => {
                        self.cancel(&r, r.reason.as_deref().unwrap_or("cancelled"))
                            .await;
                        p.cancelled += 1;
                    }
                    _ => {}
                }
            }
            if let Ok(Some(after)) = self.db.removal(r.removal_id) {
                self.report_pending(&after).await;
            }
        }
        p
    }

    /// Removals stopped (not a hold): nothing new, and what was hidden goes
    /// back under its name. A W2 or W4 under way is finished (the file is
    /// gone already).
    async fn drain_one(&self, r: &Removal, why: &str, p: &mut Pass) {
        match r.state.as_str() {
            "ticketed" => self.cancelled(r, why),
            "hiding" => self.resume_hiding(r).await,
            "hidden" | "cancelling" | "stuck" => {
                self.cancel(r, why).await;
                p.cancelled += 1;
            }
            "deleting" => {
                // Asked and allowed before removals stopped; the service is
                // asked again (every gate), which now refuses: W3.
                if self.resume_deleting(r).await {
                    p.deleted += 1;
                }
            }
            "deleted" | "pointer" => self.pointer(r).await,
            _ => {}
        }
    }

    /// Cancel without a write (nothing was hidden).
    fn cancelled(&self, r: &Removal, reason: &str) {
        info!(
            removal = r.removal_id,
            path = r.path,
            reason,
            "removal cancelled"
        );
        let _ = self.set(r, "cancelled", Some(reason), Some("cancelled"));
    }

    /// A file it hid that can't be put back: raised (the service makes it an
    /// owner problem and an alert), tried again hourly.
    fn stuck(&self, r: &Removal, reason: &str) {
        error!(
            removal = r.removal_id,
            path = r.path,
            hidden = r.hidden_name,
            reason,
            "ALERT removal: the file stays hidden"
        );
        let _ = self.set(r, "stuck", Some(reason), Some("stuck"));
        let _ = self
            .db
            .set_removal_retry(r.removal_id, 0, self.now + STUCK_RETRY_MS);
    }

    /// W1, after every gate.
    async fn hide(&self, r: &Removal) -> bool {
        if self.rate_per_hour > 0
            && self
                .db
                .hides_since(self.now - 3_600_000)
                .unwrap_or(i64::MAX)
                >= self.rate_per_hour as i64
        {
            return false; // the edge's own cap, beside the service's
        }
        let (dir, name) = match self.dir(r) {
            Ok(d) => d,
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                self.cancelled(r, "gone");
                return false;
            }
            Err(e) => {
                self.retry_later(r, &format!("folder: {e}"), GiveUp::Cancel);
                return false;
            }
        };
        let e = Expect::from(r);
        match local_gates(&dir, &name, &e, self.smb, r.ctime_ns) {
            Check::Ok => {}
            Check::Gone => {
                self.cancelled(r, "gone");
                return false;
            }
            Check::Transient(why) => {
                self.retry_later(r, &why, GiveUp::Cancel);
                return false;
            }
            Check::Refuse(why) => {
                self.cancelled(r, &why);
                return false;
            }
        }
        self.hold_before_w1().await;
        match self.ask(r, "hide").await {
            None => return false,
            Some((false, why)) => {
                // The service cancelled it already: nothing to report.
                let why = why.unwrap_or_else(|| "refused".into());
                let _ = self.set(r, "cancelled", Some(&why), None);
                return false;
            }
            Some((true, _)) => {}
        }
        // The gates again, right before the write (the file may have changed
        // while the service was asked).
        match local_gates(&dir, &name, &e, self.smb, r.ctime_ns) {
            Check::Ok => {}
            Check::Gone => {
                self.cancelled(r, "gone");
                return false;
            }
            Check::Transient(why) => {
                self.retry_later(r, &why, GiveUp::Cancel);
                return false;
            }
            Check::Refuse(why) => {
                self.cancelled(r, &why);
                return false;
            }
        }
        if !self.set(r, "hiding", None, None) {
            return false;
        }
        self.crash_point("before_w1");
        match imp::w1_hide(&dir, &name, &r.hidden_name) {
            Ok(()) => {
                self.crash_point("after_w1");
                match imp::stat_name(&dir, &r.hidden_name) {
                    // A user's file W1 hid (a save-by-rename just before it).
                    Ok(st) if st.ino != r.ino => {
                        self.swapped(r, st.ino).await;
                        return false;
                    }
                    _ => {}
                }
                self.hidden(r, &dir);
                info!(removal = r.removal_id, path = r.path, "W1: hidden");
                true
            }
            Err(e) => {
                let why = w1_refusal(errno(&e));
                warn!(removal = r.removal_id, error = %e, why, "W1 refused");
                self.cancelled(r, why);
                false
            }
        }
    }

    /// After W1: the ctime its rename left, taken now, is what the file must
    /// still have before W2 (a POSIX client can't set it back).
    fn hidden(&self, r: &Removal, dir: &Root) {
        let ctime = imp::stat_name(dir, &r.hidden_name)
            .ok()
            .filter(|s| s.ino == r.ino)
            .map(|s| s.ctime_ns);
        let _ = self.db.set_removal_hidden(r.removal_id, self.now, ctime);
        let _ = self.set(r, "hidden", None, Some("hidden"));
    }

    /// A crash between journalling `hiding` and W1's answer: which side of
    /// the rename is the file on?
    async fn resume_hiding(&self, r: &Removal) {
        let (dir, name) = match self.dir(r) {
            Ok(d) => d,
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                self.cancelled(r, "gone");
                return;
            }
            Err(e) => {
                self.retry_later(r, &format!("folder: {e}"), GiveUp::Stuck);
                return;
            }
        };
        let at = |n: &str| imp::stat_name(&dir, n).map(|s| s.ino);
        match (at(&r.hidden_name), at(&name)) {
            (Ok(i), _) if i == r.ino => self.hidden(r, &dir),
            // A user's file under the hidden name (W1 hid it): never "nothing
            // hidden" (E2 of #98's review). It goes back.
            (Ok(i), _) => self.swapped(r, i).await,
            (_, Ok(i)) if i == r.ino => {
                let _ = self.set(r, "ticketed", None, None); // W1 never ran: asked again
            }
            (Err(e), _) | (_, Err(e)) if e.kind() != io::ErrorKind::NotFound => {
                self.retry_later(r, &format!("stat: {e}"), GiveUp::Stuck);
            }
            _ => self.cancelled(r, "gone"),
        }
    }

    /// A user's file is under the hidden name: it is the file to put back now, by
    /// its own inode, under its own name if that is free, else kept aside under a
    /// `-replaced-` name with a Problem; W3 refused, `stuck`. Every byte kept.
    async fn swapped(&self, r: &Removal, ino: u64) {
        warn!(
            removal = r.removal_id,
            path = r.path,
            ino,
            "removal: a user's file is under the hidden name; it goes back"
        );
        if self
            .db
            .set_removal_swapped(r.removal_id, ino, "swapped_at_hide", self.now)
            .is_err()
        {
            return; // nothing written; the next pass looks again
        }
        if let Ok(Some(now)) = self.db.removal(r.removal_id) {
            self.cancel(&now, "swapped_at_hide").await;
        }
    }

    /// After the grace period: the gates again, the service again, then W2
    /// and the pointer. `Some(true)` deleted, `Some(false)` cancelled,
    /// `None` not yet.
    async fn delete(&self, r: &Removal) -> Option<bool> {
        let hidden_at = r.hidden_at.unwrap_or(self.now);
        if self.now - hidden_at < self.grace_ms {
            return None;
        }
        let (dir, _, r) = match self.locate_hidden(r) {
            Ok(Some((d, n, moved))) if moved.path != r.path => {
                // A user moved its folder in its grace: put it back there.
                let _ = n;
                drop(d);
                self.cancel(&moved, "moved_in_grace").await;
                return Some(false);
            }
            Ok(Some(found)) => found,
            Ok(None) => {
                self.cancelled(r, "gone_in_grace");
                return Some(false);
            }
            Err(e) => {
                self.retry_later(r, &format!("folder: {e}"), GiveUp::Restore);
                return None;
            }
        };
        let r = &r;
        if !self.w2_gates(r, &dir).await {
            return None;
        }
        match self.ask(r, "delete").await {
            None => return None,
            Some((true, _)) => {}
            Some((false, Some(why))) if HOLD_REASONS.contains(&why.as_str()) => {
                // Paused or outside the window: it stays hidden, and its grace
                // starts again (the service's says so too). In its grace by
                // the service's clock: wait.
                if why != "grace" {
                    let _ = self.db.set_removal_hidden_at(r.removal_id, self.now);
                }
                return None;
            }
            Some((false, why)) => {
                // The service cancelled it: put it back.
                self.cancel(r, why.as_deref().unwrap_or("refused")).await;
                return Some(false);
            }
        }
        self.w2(r, &dir).await
    }

    /// The file gates before W2 (the forced `statx` and the rest; the CRC is
    /// `w2`'s). `false`: not now (cancelled, put back, or tried again).
    async fn w2_gates(&self, r: &Removal, dir: &Root) -> bool {
        match local_gates(
            dir,
            &r.hidden_name,
            &Expect::from(r),
            self.smb,
            r.hidden_ctime,
        ) {
            Check::Ok => {}
            Check::Gone => {
                self.hidden_gone(r, dir).await;
                return false;
            }
            Check::Transient(why) => {
                self.retry_later(r, &why, GiveUp::Restore);
                return false;
            }
            Check::Refuse(why) => {
                self.cancel(r, &format!("{why}_in_grace")).await;
                return false;
            }
        }
        true
    }

    /// The hidden name is gone: a user renamed it back (its inode under the
    /// original name), or took it away. Nothing of ours to delete.
    async fn hidden_gone(&self, r: &Removal, dir: &Root) {
        let (_, name) = split(&r.path);
        match imp::stat_name(dir, name) {
            Ok(st) if st.ino == r.ino => self.cancelled(r, "renamed_back"),
            Ok(_) => self.cancelled(r, "gone_in_grace"),
            Err(e) if e.kind() == io::ErrorKind::NotFound => self.cancelled(r, "gone_in_grace"),
            Err(e) => self.retry_later(r, &format!("stat: {e}"), GiveUp::Stuck),
        }
    }

    /// SMB lets a client set the ChangeTime, so there the bytes are read
    /// again and their CRC compared with the verified copy's. Once, here.
    async fn crc_matches(&self, r: &Removal, dir: &Root) -> bool {
        let Some(want) = r.crc64 else {
            self.cancel(r, "crc_unknown").await;
            return false;
        };
        let got = match dir.try_clone() {
            Ok(d) => {
                let h = r.hidden_name.clone();
                tokio::task::spawn_blocking(move || imp::crc_of(&d, &h))
                    .await
                    .map_err(|e| io::Error::other(e.to_string()))
                    .and_then(|x| x)
            }
            Err(e) => Err(e),
        };
        match got {
            Ok(c) if c == want => true,
            Ok(c) => {
                error!(
                    removal = r.removal_id,
                    path = r.path,
                    want = format!("{want:016x}"),
                    got = format!("{c:016x}"),
                    "ALERT removal: the hidden file's bytes aren't the verified copy's"
                );
                self.cancel(r, "crc_mismatch_in_grace").await;
                false
            }
            Err(e) => {
                self.retry_later(r, &format!("read: {e}"), GiveUp::Restore);
                false
            }
        }
    }

    async fn w2(&self, r: &Removal, dir: &Root) -> Option<bool> {
        if !self.w2_gates(r, dir).await {
            return None;
        }
        // Over SMB the CRC (minutes for a big file), and then every check
        // again: a write during the read is seen before the unlink.
        if self.smb && (!self.crc_matches(r, dir).await || !self.w2_gates(r, dir).await) {
            return None;
        }
        if !self.set(r, "deleting", None, None) {
            return None;
        }
        self.crash_point("before_w2");
        match imp::w2_delete(dir, &r.hidden_name) {
            Ok(()) => {
                self.crash_point("after_w2");
                info!(removal = r.removal_id, path = r.path, "W2: deleted");
                self.deleted(r).await;
                Some(true)
            }
            Err(e) if errno(&e) == libc::ENOENT => {
                // A retransmit after the delete happened, or the user renamed
                // it back at the last moment: the inode at the original name
                // tells which.
                let (_, name) = split(&r.path);
                match imp::stat_name(dir, name) {
                    Ok(st) if st.ino == r.ino => {
                        self.cancelled(r, "renamed_back");
                        Some(false)
                    }
                    _ => {
                        self.deleted(r).await;
                        Some(true)
                    }
                }
            }
            Err(e) => {
                warn!(removal = r.removal_id, error = %e, "W2 failed");
                let _ = self.set(r, "hidden", None, None);
                self.retry_later(r, &format!("unlink: {e}"), GiveUp::Restore);
                None
            }
        }
    }

    async fn deleted(&self, r: &Removal) {
        let _ = self.set(r, "deleted", None, Some("deleted"));
        if let Ok(Some(after)) = self.db.removal(r.removal_id) {
            self.report_pending(&after).await;
            self.pointer(&after).await;
        }
    }

    /// A crash after journalling `deleting`: a hidden file still there was
    /// not deleted (it goes through every gate again); gone, it was, unless
    /// its inode is back under the original name (renamed back).
    async fn resume_deleting(&self, r: &Removal) -> bool {
        let (dir, _, r) = match self.locate_hidden(r) {
            Ok(Some(found)) => found,
            Ok(None) => {
                // Neither its folder nor the hidden name anywhere: W2 ran.
                self.deleted(r).await;
                return true;
            }
            Err(e) => {
                self.retry_later(r, &format!("folder: {e}"), GiveUp::Stuck);
                return false;
            }
        };
        match imp::stat_name(&dir, &r.hidden_name) {
            Ok(st) if st.ino == r.ino => {
                // Not deleted: back to hidden, and through every gate again.
                let _ = self.set(&r, "hidden", None, None);
                false
            }
            Ok(_) => {
                self.cancelled(&r, "hidden_name_reused");
                false
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                let (_, name) = split(&r.path);
                match imp::stat_name(&dir, name) {
                    Ok(st) if st.ino == r.ino => {
                        self.cancelled(&r, "renamed_back");
                        false
                    }
                    Err(e) if e.kind() != io::ErrorKind::NotFound => {
                        self.retry_later(&r, &format!("stat: {e}"), GiveUp::Stuck);
                        false
                    }
                    _ => {
                        self.deleted(&r).await;
                        true
                    }
                }
            }
            Err(e) => {
                self.retry_later(&r, &format!("stat: {e}"), GiveUp::Stuck);
                false
            }
        }
    }

    /// W4 (if the annex leaves pointers), then done.
    async fn pointer(&self, r: &Removal) {
        if !self.pointer_files {
            let _ = self.set(r, "done", None, Some("done"));
            return;
        }
        let (dir, name) = match self.dir(r) {
            Ok(d) => d,
            Err(_) => {
                // Its folder is gone: no pointer to leave.
                let _ = self.db.set_removal_pointer(r.removal_id, false);
                let _ = self.set(r, "done", None, Some("pointer,done"));
                return;
            }
        };
        let resumed = r.state == "pointer";
        if !self.set(r, "pointer", None, None) {
            return;
        }
        let text = pointer_text(
            self.annex_name,
            &r.path,
            r.file_id,
            &super::scan::rfc3339_ms(self.now),
        );
        self.crash_point("before_w4");
        let written = match imp::w4_pointer(&dir, &format!("{name}{POINTER_SUFFIX}"), &text) {
            Ok(ino) => {
                // Recorded, so a placement back under this name deletes only
                // this very note (A3.3, R6).
                let _ = self.db.set_removal_pointer_file(
                    r.removal_id,
                    ino,
                    &super::placer::sha_hex(text.as_bytes()),
                );
                true
            }
            // Never overwritten. After a crash at this step it is ours.
            Err(e) if errno(&e) == libc::EEXIST => resumed,
            Err(e) => {
                warn!(removal = r.removal_id, error = %e, "W4: no pointer");
                false
            }
        };
        self.crash_point("after_w4");
        let _ = self.db.set_removal_pointer(r.removal_id, written);
        let _ = self.set(r, "done", None, Some("pointer,done"));
    }

    /// W3: the hidden name back to its own, found by inode wherever its
    /// folder went. A name taken since leaves the file hidden: `stuck`.
    async fn cancel(&self, r: &Removal, reason: &str) {
        let (dir, name, r) = match self.locate_hidden(r) {
            Ok(Some(found)) => found,
            Ok(None) => {
                // Nowhere: a user took it away. Nothing to put back.
                self.cancelled(r, &format!("{reason}; the hidden file is gone"));
                return;
            }
            Err(e) => {
                self.retry_later(r, &format!("folder: {e}"), GiveUp::Stuck);
                return;
            }
        };
        let r = &r;
        // A stuck file tried again stays `stuck` until it is back.
        let state = if r.state == "stuck" {
            "stuck"
        } else {
            "cancelling"
        };
        if !self.set(r, state, Some(reason), None) {
            return;
        }
        // A user's file being kept aside (E2): on with that.
        if r.aside_name.is_some() {
            self.users_file_aside(r, &dir, &name, reason);
            return;
        }
        // Resumed between W3's link and its unlink: finish it.
        if imp::finish_link(&dir, &r.hidden_name, &name, r.ino).unwrap_or(false) {
            info!(
                removal = r.removal_id,
                path = r.path,
                "W3: finished after a restart"
            );
            let _ = self.set(r, "cancelled", Some(reason), Some("cancelled"));
            return;
        }
        // Back already: W3 ran before a restart (its rename over our
        // placeholder, or its link and unlink), or a user put it back.
        let back = imp::stat_name(&dir, &name).is_ok_and(|st| st.ino == r.ino)
            && imp::stat_name(&dir, &r.hidden_name)
                .is_err_and(|e| e.kind() == io::ErrorKind::NotFound);
        if back {
            let _ = self.db.set_removal_placeholder(r.removal_id, None);
            let _ = self.set(r, "cancelled", Some(reason), Some("cancelled"));
            return;
        }
        // A placeholder of ours from before: only that inode, untouched.
        if let Some((pino, pctime)) = r.placeholder {
            match imp::placeholder_intact(&dir, &name, pino, pctime) {
                Ok(true) => {
                    self.over_placeholder(r, &dir, &name, reason);
                    return;
                }
                Ok(false) if imp::stat_name(&dir, &name).is_ok() => {
                    // Something else is at the name now: a user's. Never
                    // renamed over.
                    let _ = self.db.set_removal_placeholder(r.removal_id, None);
                    self.stuck(
                        r,
                        &format!(
                            "{reason}; the name is taken: it stays hidden as {}",
                            r.hidden_name
                        ),
                    ); // fmt: skip
                    return;
                }
                Ok(false) => {
                    let _ = self.db.set_removal_placeholder(r.removal_id, None);
                }
                Err(e) => {
                    self.retry_later(r, &format!("{reason}; placeholder: {e}"), GiveUp::Stuck);
                    return;
                }
            }
        }
        self.crash_point("before_w3");
        match imp::w3_link(&dir, &r.hidden_name, &name) {
            Ok(()) => {
                self.crash_point("after_w3");
                info!(
                    removal = r.removal_id,
                    path = r.path,
                    reason,
                    "W3: restored"
                );
                let _ = self.set(r, "cancelled", Some(reason), Some("cancelled"));
            }
            Err(e) if imp::link_refused(&e) => self.w3_placeholder(r, &dir, &name, reason),
            Err(e) if errno(&e) == libc::EEXIST && swapped(r) => {
                self.users_file_aside(r, &dir, &name, reason)
            }
            Err(e) if errno(&e) == libc::EEXIST => self.name_taken(r, reason),
            Err(e) if errno(&e) == libc::ENOENT => {
                // Not there to put back: back already (by a user, or this
                // W3 before a restart), or taken away.
                match imp::stat_name(&dir, &name) {
                    Ok(st) if st.ino == r.ino => {
                        let _ = self.set(r, "cancelled", Some(reason), Some("cancelled"));
                    }
                    _ => self.cancelled(r, &format!("{reason}; the hidden file is gone")),
                }
            }
            Err(e) => {
                self.retry_later(r, &format!("{reason}; rename back: {e}"), GiveUp::Stuck);
            }
        }
    }

    /// A user's file the removal hid, its own name taken: kept under the first
    /// free `-replaced-<UTC time>` name (W3, no-replace), and the service raises a
    /// Problem naming it. Each name is journalled before its link, so a restart
    /// finishes the same one. Never left hidden but when W3 itself fails.
    fn users_file_aside(&self, r: &Removal, dir: &Root, name: &str, reason: &str) {
        let mut names: Vec<String> = r.aside_name.iter().cloned().collect();
        for i in 0..9 {
            let n = super::placer::replace::aside_name(name, self.now, i);
            if !names.contains(&n) {
                names.push(n);
            }
        }
        for n in names {
            if self.db.set_removal_aside(r.removal_id, &n).is_err() {
                return;
            }
            let mut r = r.clone();
            r.aside_name = Some(n.clone());
            let kept = |this: &Self, r: &Removal| {
                error!(
                    removal = r.removal_id,
                    path = r.path,
                    kept_as = n,
                    "ALERT removal: a user's file it hid is kept aside; an owner looks"
                );
                let _ = this.set(r, "cancelled", Some(reason), Some("cancelled"));
            };
            match imp::finish_link(dir, &r.hidden_name, &n, r.ino) {
                Ok(true) => return kept(self, &r),
                Ok(false) => {}
                Err(e) => {
                    return self.retry_later(&r, &format!("{reason}; aside: {e}"), GiveUp::Stuck);
                }
            }
            self.crash_point("before_w3");
            match imp::w3_link(dir, &r.hidden_name, &n) {
                Ok(()) => return kept(self, &r),
                Err(e) if errno(&e) == libc::EEXIST => continue,
                Err(e) if errno(&e) == libc::ENOENT => {
                    return match imp::stat_name(dir, &n) {
                        Ok(st) if st.ino == r.ino => kept(self, &r),
                        _ => self.cancelled(&r, &format!("{reason}; the hidden file is gone")),
                    };
                }
                Err(e) => {
                    return self.retry_later(&r, &format!("{reason}; aside: {e}"), GiveUp::Stuck);
                }
            }
        }
        self.stuck(
            r,
            &format!(
                "{reason}; every -replaced- name is taken: it stays hidden as {}",
                r.hidden_name
            ),
        );
    }

    fn name_taken(&self, r: &Removal, reason: &str) {
        self.stuck(
            r,
            &format!(
                "{reason}; the name is taken: it stays hidden as {}",
                r.hidden_name
            ),
        );
    }

    /// W3 where a link is refused: a placeholder of ours made `O_EXCL` at
    /// the name, its inode and ctime journalled, checked again right before
    /// the rename over it. What is left is one round trip between that
    /// check and the rename, in which a user would have to replace our
    /// placeholder by name; its text says not to.
    fn w3_placeholder(&self, r: &Removal, dir: &Root, name: &str, reason: &str) {
        let (pino, pctime) = match imp::make_placeholder(dir, name) {
            Ok(p) => p,
            Err(e) if errno(&e) == libc::EEXIST => return self.name_taken(r, reason),
            Err(e) => {
                return self.retry_later(r, &format!("{reason}; placeholder: {e}"), GiveUp::Stuck);
            }
        };
        if self
            .db
            .set_removal_placeholder(r.removal_id, Some((pino, pctime)))
            .is_err()
        {
            // Not journalled: take it away again (it is ours, just made).
            if imp::placeholder_intact(dir, name, pino, pctime).unwrap_or(false) {
                let _ = imp::w2_delete(dir, name);
            }
            return;
        }
        let mut r = r.clone();
        r.placeholder = Some((pino, pctime));
        match imp::placeholder_intact(dir, name, pino, pctime) {
            Ok(true) => self.over_placeholder(&r, dir, name, reason),
            Ok(false) => self.name_taken(&r, reason),
            Err(e) => self.retry_later(&r, &format!("{reason}; placeholder: {e}"), GiveUp::Stuck),
        }
    }

    fn over_placeholder(&self, r: &Removal, dir: &Root, name: &str, reason: &str) {
        self.crash_point("before_w3");
        match imp::rename_over(dir, &r.hidden_name, name) {
            Ok(()) => {
                self.crash_point("after_w3");
                let _ = self.db.set_removal_placeholder(r.removal_id, None);
                info!(
                    removal = r.removal_id,
                    path = r.path,
                    reason,
                    "W3: restored (placeholder)"
                );
                let _ = self.set(r, "cancelled", Some(reason), Some("cancelled"));
            }
            Err(e) => self.retry_later(r, &format!("{reason}; rename back: {e}"), GiveUp::Stuck),
        }
    }
}

/// A removal whose hidden file is a user's (W1 hid it by mistake).
fn swapped(r: &Removal) -> bool {
    r.users_file
}

/// The test hook forcing W3's placeholder path.
#[allow(dead_code)]
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

/// What a removal does once its retries run out.
#[derive(Clone, Copy)]
enum GiveUp {
    /// Nothing hidden yet: cancel.
    Cancel,
    /// Hidden: put it back (W3).
    Restore,
    /// Putting it back failed: it stays hidden, raised.
    Stuck,
}

#[cfg(target_os = "linux")]
pub(super) mod imp {
    use std::ffi::CString;
    use std::io::{self, Read, Write};
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};

    use super::super::nasfs::{Kind, Root, Stat, statx};

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

    /// A forced `statx` of a name in `dir` (no symlink followed).
    pub fn stat_name(dir: &Root, n: &str) -> io::Result<Stat> {
        statx(dir.dir_fd(), &name(n)?, libc::AT_STATX_FORCE_SYNC)
    }

    /// W1. `renameat2(RENAME_NOREPLACE)`; where the filesystem refuses the
    /// flag (the NFS client: `EINVAL`), a plain `renameat` once the hidden
    /// name is checked absent. The hidden name is unique to the removal, so
    /// nothing a user made is replaced; the original name is never unlinked
    /// (a user's save onto it in between is the user's), and W2's gates and
    /// W3 cover the inode that moved.
    pub fn w1_hide(dir: &Root, n: &str, hidden: &str) -> io::Result<()> {
        let (f, t) = (name(n)?, name(hidden)?);
        let fd = dir.dir_fd().as_raw_fd();
        #[cfg(test)]
        let flags_ok = !super::faults::NO_RENAME_FLAGS.with(|f| f.get());
        #[cfg(not(test))]
        let flags_ok = true;
        if flags_ok {
            // SAFETY: a valid dir fd and two NUL-terminated names.
            let r =
                unsafe { libc::renameat2(fd, f.as_ptr(), fd, t.as_ptr(), libc::RENAME_NOREPLACE) };
            if r == 0 {
                return Ok(());
            }
            let e = io::Error::last_os_error();
            if e.raw_os_error() != Some(libc::EINVAL) {
                return Err(e);
            }
        }
        match stat_name(dir, hidden) {
            Ok(_) => return Err(io::Error::from_raw_os_error(libc::EEXIST)),
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return Err(e),
        }
        // SAFETY: as above.
        ok(unsafe { libc::renameat(fd, f.as_ptr(), fd, t.as_ptr()) })
    }

    pub fn w2_delete(dir: &Root, hidden: &str) -> io::Result<()> {
        let h = name(hidden)?;
        // SAFETY: a valid dir fd and a NUL-terminated name.
        ok(unsafe { libc::unlinkat(dir.dir_fd().as_raw_fd(), h.as_ptr(), 0) })
    }

    /// Whether a link was refused for want of hard links (protected hard
    /// links on the server, an SMB server without them), not for the name.
    pub fn link_refused(e: &io::Error) -> bool {
        matches!(
            e.raw_os_error(),
            Some(libc::EACCES | libc::EPERM | libc::EOPNOTSUPP | libc::EMLINK | libc::EXDEV)
        )
    }

    /// W3's placeholder: made `O_EXCL` at the name (refused, on the server,
    /// if the name exists), holding `PLACEHOLDER_TEXT`, synced. Its inode
    /// and ctime, for the journal and the checks before the rename.
    pub fn make_placeholder(dir: &Root, n: &str) -> io::Result<(u64, i64)> {
        let t = name(n)?;
        // SAFETY: a valid dir fd and a NUL-terminated name.
        let p = unsafe {
            libc::openat(
                dir.dir_fd().as_raw_fd(),
                t.as_ptr(),
                libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_WRONLY | libc::O_CLOEXEC,
                0o600 as libc::c_uint,
            )
        };
        if p < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: a fresh fd we own.
        let mut f = std::fs::File::from(unsafe { OwnedFd::from_raw_fd(p) });
        f.write_all(super::PLACEHOLDER_TEXT.as_bytes())?;
        f.sync_all()?;
        use std::os::fd::AsFd;
        let st = statx(
            f.as_fd(),
            &CString::new("").unwrap(),
            libc::AT_EMPTY_PATH | libc::AT_STATX_FORCE_SYNC,
        )?;
        Ok((st.ino, st.ctime_ns))
    }

    /// The placeholder at the name is still ours, untouched: its inode, its
    /// ctime, and exactly its text. Anything else is a user's.
    pub fn placeholder_intact(dir: &Root, n: &str, ino: u64, ctime: i64) -> io::Result<bool> {
        let st = match stat_name(dir, n) {
            Ok(st) => st,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
            Err(e) => return Err(e),
        };
        let text = super::PLACEHOLDER_TEXT.as_bytes();
        if st.kind != Kind::File
            || st.ino != ino
            || st.ctime_ns != ctime
            || st.size != text.len() as u64
            || st.nlink != 1
        {
            return Ok(false);
        }
        let mut got = Vec::new();
        open_read(dir, n)?
            .take(text.len() as u64 + 1)
            .read_to_end(&mut got)?;
        Ok(got == text)
    }

    /// The hidden name renamed over the placeholder (which replaces it).
    pub fn rename_over(dir: &Root, hidden: &str, n: &str) -> io::Result<()> {
        let (h, t) = (name(hidden)?, name(n)?);
        let fd = dir.dir_fd().as_raw_fd();
        // SAFETY: a valid dir fd and two NUL-terminated names.
        ok(unsafe { libc::renameat(fd, h.as_ptr(), fd, t.as_ptr()) })
    }

    /// W3: a link to the original name, which the server refuses atomically
    /// if the name exists (on cifs, `ReplaceIfExists` false; on NFS, `LINK`
    /// refuses an existing name), then the hidden name's unlink. A crash
    /// between the two leaves both names on the inode: `finish_link`.
    pub fn w3_link(dir: &Root, hidden: &str, n: &str) -> io::Result<()> {
        #[cfg(test)]
        {
            let left = super::faults::W3_EIO.with(|f| f.get());
            if left > 0 {
                super::faults::W3_EIO.with(|f| f.set(left - 1));
                return Err(io::Error::from_raw_os_error(libc::EIO));
            }
        }
        if super::faults_link_eacces() {
            return Err(io::Error::from_raw_os_error(libc::EACCES));
        }
        let (h, t) = (name(hidden)?, name(n)?);
        let fd = dir.dir_fd().as_raw_fd();
        // SAFETY: a valid dir fd and two NUL-terminated names.
        ok(unsafe { libc::linkat(fd, h.as_ptr(), fd, t.as_ptr(), 0) })?;
        // SAFETY: as above.
        ok(unsafe { libc::unlinkat(fd, h.as_ptr(), 0) })
    }

    /// W3 cut short between its link and its unlink: both names on `ino`.
    /// Unlinks the hidden one; `true` if it did.
    pub fn finish_link(dir: &Root, hidden: &str, n: &str, ino: u64) -> io::Result<bool> {
        match (stat_name(dir, hidden), stat_name(dir, n)) {
            (Ok(x), Ok(y)) if x.ino == ino && y.ino == ino => {
                let h = name(hidden)?;
                // SAFETY: a valid dir fd and a NUL-terminated name.
                ok(unsafe { libc::unlinkat(dir.dir_fd().as_raw_fd(), h.as_ptr(), 0) })?;
                Ok(true)
            }
            _ => Ok(false),
        }
    }

    pub fn w4_pointer(dir: &Root, n: &str, text: &str) -> io::Result<u64> {
        let c = name(n)?;
        // SAFETY: a valid dir fd and a NUL-terminated name.
        let fd = unsafe {
            libc::openat(
                dir.dir_fd().as_raw_fd(),
                c.as_ptr(),
                libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_WRONLY | libc::O_CLOEXEC,
                0o644 as libc::c_uint,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: a fresh fd we own.
        let mut f = std::fs::File::from(unsafe { OwnedFd::from_raw_fd(fd) });
        f.write_all(text.as_bytes())?;
        f.sync_all()?;
        use std::os::fd::AsFd;
        let st = statx(f.as_fd(), &CString::new("").unwrap(), libc::AT_EMPTY_PATH)?;
        Ok(st.ino)
    }

    fn open_read(dir: &Root, n: &str) -> io::Result<std::fs::File> {
        let c = name(n)?;
        // SAFETY: a valid dir fd and a NUL-terminated name.
        let fd = unsafe {
            libc::openat(
                dir.dir_fd().as_raw_fd(),
                c.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: a fresh fd we own.
        Ok(std::fs::File::from(unsafe { OwnedFd::from_raw_fd(fd) }))
    }

    /// The file's CRC-64/NVME, read whole.
    pub fn crc_of(dir: &Root, n: &str) -> io::Result<u64> {
        let mut f = open_read(dir, n)?;
        let mut crc = omnifile_integrity::crc64::Crc64::new();
        let mut buf = vec![0u8; 8 << 20];
        loop {
            let k = f.read(&mut buf)?;
            if k == 0 {
                #[cfg(test)]
                super::faults::after_crc(dir.path());
                return Ok(crc.finalize());
            }
            crc.update(&buf[..k]);
        }
    }

    /// The folder (relative to `root`) holding `hidden` with `ino`: a hidden
    /// file whose folder a user renamed or moved. Bounded; no other mount.
    pub fn find_hidden(root: &Root, hidden: &str, ino: u64) -> io::Result<Option<String>> {
        const MAX_DIRS: usize = 200_000;
        let mut stack = vec![String::new()];
        let mut seen = 0usize;
        while let Some(dir) = stack.pop() {
            seen += 1;
            if seen > MAX_DIRS {
                return Err(io::Error::other("the annex is too large to search"));
            }
            let items = match root.read_dir(&dir) {
                Ok((_, items)) => items,
                Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
                Err(e) => return Err(e),
            };
            for (n, st) in items {
                let Some(n) = n.to_str() else { continue };
                let rel = if dir.is_empty() {
                    n.to_owned()
                } else {
                    format!("{dir}/{n}")
                };
                match st {
                    Ok(s) if n == hidden && s.ino == ino && s.kind == Kind::File => {
                        return Ok(Some(dir));
                    }
                    Ok(s) if s.kind == Kind::Dir => stack.push(rel),
                    _ => {}
                }
            }
        }
        Ok(None)
    }

    /// `struct smb_query_info` (linux/fs/smb/client/cifs_ioctl.h), then the
    /// answer.
    #[repr(C)]
    struct QueryInfo {
        info_type: u32,
        file_info_class: u32,
        additional_information: u32,
        flags: u32,
        input_buffer_length: u32,
        output_buffer_length: u32,
    }
    /// `_IOWR(0xCF, 7, struct smb_query_info)`.
    const CIFS_QUERY_INFO: libc::c_ulong = 0xC018_CF07;
    const SMB2_O_INFO_FILE: u32 = 1;
    const FILE_STREAM_INFORMATION: u32 = 22;
    /// The answer's room. The ioctl's `input_buffer_length` is that room
    /// (the request's OutputBufferLength); `output_buffer_length` is the
    /// request's own input, none for a query (as cifs-utils' smbinfo).
    const OUT: usize = 16 * 1024;

    /// The file's alternate data streams over SMB (`::$DATA` is the file
    /// itself, not one): SMB2 QUERY_INFO `FileStreamInformation`, passed
    /// through by the cifs client.
    pub fn streams(dir: &Root, n: &str) -> io::Result<usize> {
        #[cfg(test)]
        if super::faults::smb_fake(dir.path()) {
            return Ok(0);
        }
        let f = open_read(dir, n)?;
        let hdr = std::mem::size_of::<QueryInfo>();
        let mut buf = vec![0u8; hdr + OUT];
        let qi = QueryInfo {
            info_type: SMB2_O_INFO_FILE,
            file_info_class: FILE_STREAM_INFORMATION,
            additional_information: 0,
            flags: 0, // PASSTHRU_QUERY_INFO
            input_buffer_length: OUT as u32,
            output_buffer_length: 0,
        };
        // SAFETY: `buf` holds at least a QueryInfo.
        unsafe { std::ptr::write_unaligned(buf.as_mut_ptr().cast::<QueryInfo>(), qi) };
        // SAFETY: a valid fd and a buffer of the size the ioctl reads and writes.
        let r = unsafe { libc::ioctl(f.as_raw_fd(), CIFS_QUERY_INFO, buf.as_mut_ptr()) };
        if r < 0 {
            return Err(io::Error::last_os_error());
        }
        // The kernel puts the answer's length in input_buffer_length.
        let len = u32::from_le_bytes(buf[16..20].try_into().unwrap()) as usize;
        parse_streams(&buf[hdr..hdr + len.min(OUT)])
    }

    /// FILE_STREAM_INFORMATION entries; the streams other than `::$DATA`.
    /// An empty answer fails closed (a file always has its `::$DATA`).
    pub fn parse_streams(b: &[u8]) -> io::Result<usize> {
        let bad = || io::Error::new(io::ErrorKind::InvalidData, "stream information unreadable");
        if b.is_empty() {
            return Err(bad());
        }
        let mut off = 0usize;
        let mut n = 0;
        loop {
            let e = b.get(off..off + 24).ok_or_else(bad)?;
            let next = u32::from_le_bytes(e[0..4].try_into().unwrap()) as usize;
            let nlen = u32::from_le_bytes(e[4..8].try_into().unwrap()) as usize;
            let raw = b.get(off + 24..off + 24 + nlen).ok_or_else(bad)?;
            let units: Vec<u16> = (0..raw.len() / 2)
                .map(|i| u16::from_le_bytes([raw[2 * i], raw[2 * i + 1]]))
                .collect();
            let name = String::from_utf16_lossy(&units);
            if name != "::$DATA" {
                n += 1;
            }
            if next == 0 {
                return Ok(n);
            }
            off += next;
        }
    }
}

#[cfg(not(target_os = "linux"))]
pub(super) mod imp {
    //! The remover writes only on Linux (the appliance); elsewhere every
    //! write is refused.
    use std::io;

    use super::super::nasfs::{Root, Stat};

    fn no() -> io::Error {
        io::Error::new(io::ErrorKind::Unsupported, "the remover runs on Linux only")
    }
    pub fn stat_name(dir: &Root, n: &str) -> io::Result<Stat> {
        dir.stat(n, true)
    }
    pub fn w1_hide(_: &Root, _: &str, _: &str) -> io::Result<()> {
        Err(no())
    }
    pub fn w2_delete(_: &Root, _: &str) -> io::Result<()> {
        Err(no())
    }
    pub fn w3_link(_: &Root, _: &str, _: &str) -> io::Result<()> {
        Err(no())
    }
    pub fn link_refused(_: &io::Error) -> bool {
        false
    }
    pub fn make_placeholder(_: &Root, _: &str) -> io::Result<(u64, i64)> {
        Err(no())
    }
    pub fn placeholder_intact(_: &Root, _: &str, _: u64, _: i64) -> io::Result<bool> {
        Err(no())
    }
    pub fn rename_over(_: &Root, _: &str, _: &str) -> io::Result<()> {
        Err(no())
    }
    pub fn finish_link(_: &Root, _: &str, _: &str, _: u64) -> io::Result<bool> {
        Err(no())
    }
    pub fn w4_pointer(_: &Root, _: &str, _: &str) -> io::Result<u64> {
        Err(no())
    }
    pub fn streams(_: &Root, _: &str) -> io::Result<usize> {
        Err(no())
    }
    pub fn crc_of(_: &Root, _: &str) -> io::Result<u64> {
        Err(no())
    }
    pub fn find_hidden(_: &Root, _: &str, _: u64) -> io::Result<Option<String>> {
        Err(no())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_and_paths() {
        assert_eq!(split("a/b/c.segy"), ("a/b", "c.segy"));
        assert_eq!(split("c.segy"), ("", "c.segy"));
        assert_eq!(hidden_name(7, "c.segy"), ".omnifile-removing-7-c.segy");
        assert!(super::super::scan::own_file(&hidden_name(7, "c.segy")));
        assert!(super::super::scan::own_file(&format!(
            "c.segy{POINTER_SUFFIX}"
        )));
    }

    #[test]
    fn a_pointer_never_says_more_than_the_path() {
        let t = pointer_text("a\\nnex", "rm/x\nFile: forged\r.bin", 7, "2026-11-05");
        assert_eq!(
            t.matches('\n').count(),
            pointer_text("a", "rm/x.bin", 7, "2026-11-05")
                .matches('\n')
                .count()
        );
        assert!(t.contains("rm/x\\u{a}File: forged\\u{d}.bin"));
    }

    #[test]
    fn an_smb_sharing_violation_is_in_use() {
        assert_eq!(w1_refusal(libc::EBUSY), "in_use");
        assert_eq!(w1_refusal(libc::ETXTBSY), "in_use");
        assert_eq!(w1_refusal(libc::EPERM), "worm");
        assert_eq!(w1_refusal(libc::EIO), "rename_failed");
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn stream_information_counts_only_alternate_streams() {
        fn entry(name: &str, last: bool) -> Vec<u8> {
            let u: Vec<u8> = name.encode_utf16().flat_map(|c| c.to_le_bytes()).collect();
            let len = 24 + u.len();
            let pad = (8 - len % 8) % 8;
            let next = if last { 0 } else { (len + pad) as u32 };
            let mut v = Vec::new();
            v.extend_from_slice(&next.to_le_bytes());
            v.extend_from_slice(&(u.len() as u32).to_le_bytes());
            v.extend_from_slice(&[0u8; 16]);
            v.extend_from_slice(&u);
            if !last {
                v.extend(std::iter::repeat_n(0u8, pad));
            }
            v
        }
        let one = entry("::$DATA", true);
        assert_eq!(imp::parse_streams(&one).unwrap(), 0);
        let mut two = entry("::$DATA", false);
        two.extend(entry(":Zone.Identifier:$DATA", true));
        assert_eq!(imp::parse_streams(&two).unwrap(), 1);
        assert!(
            imp::parse_streams(&two[..30]).is_err(),
            "a short answer fails closed"
        );
        assert!(
            imp::parse_streams(&[]).is_err(),
            "an empty answer fails closed"
        );
    }
}
