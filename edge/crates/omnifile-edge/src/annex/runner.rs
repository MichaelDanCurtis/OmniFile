//! The annex unit's loop (`annex.md` §2–3, phase A1).
//!
//! Every tick:
//! 1. **Scans when due.** The first ever is the baseline: a full scan whose
//!    files are "present when the annex started", never arrivals. Then quick
//!    scans (default hourly), full scans (default daily) and, weekly, the
//!    reconciliation. SMB change notify and `SIGUSR1` ask for a quick scan
//!    now, `SIGHUP` a full scan, `SIGUSR2` a reconciliation.
//! 2. **The quiet period.** A file is reported only once its size, mtime
//!    and ctime are the same across two looks at least Q minutes apart
//!    (default 10), and, for SEG-Y, once its size is whole traces. Then its
//!    NAS ACL is read and an `arrived` or `changed` event queued.
//! 3. **Events** go to the service in batches, in order. Each answer for an
//!    `arrived` / `changed` names the catalog file to copy into.
//! 4. **Copies**, one file at a time (`copy`).
//!
//! **Reconciliation** (§2.3) walks the whole directory fresh, reads the
//! catalog's record of the annex, and compares. What the scans missed is
//! recorded as drift (`missed_arrival`, `missed_departure`,
//! `changed_no_event`, `copy_mismatch`) and then put right: a missed file
//! waits for its quiet period and is copied, a missed delete is reported, a
//! copy that no longer matches is copied again.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use omnifile_control::annex::{AnnexRecord, EdgeAnnex, nanos};
use omnifile_identity::IdMap;
use serde_json::{Value, json};
use tracing::{error, info, warn};

use super::acl::{self, Flavour};
use super::copy::{Copied, Copier};
use super::db::{AnnexDb, Entry, Seen};
use super::folders;
use super::nasfs::{Kind, Root, Stat, open_root};
use super::placer;
use super::preview::{self, Look};
use super::remover;
use super::rules::{self, Decision, Removal, RuleSet, Trigger};
use super::scan::{Scanner, gone_error, parse_rfc3339_ms, rfc3339_ms};
use super::segy::{self, Segy};
use super::service::AnnexService;
use super::{AnnexConfig, now_ms};

/// How long an incomplete SEG-Y file waits before it is copied anyway (with
/// a problem recorded), so a truncated survey is still backed up.
pub const SEGY_GIVE_UP_MS: i64 = 24 * 3600 * 1000;
/// A refused or unreadable file is looked at again after this.
pub const RETRY_MS: i64 = 3600 * 1000;

/// Timings in milliseconds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Timings {
    pub quiet: i64,
    pub quick: i64,
    pub full: i64,
    pub reconcile: i64,
    /// How often the `age` rules (and `remove_after_days`) are looked at.
    pub age: i64,
}

impl Timings {
    pub fn of(a: &EdgeAnnex, cfg: &AnnexConfig) -> Timings {
        let pick = |over_s: u64, minutes: u64| {
            if over_s > 0 {
                over_s as i64 * 1000
            } else {
                minutes as i64 * 60_000
            }
        };
        let s = &a.settings;
        Timings {
            quiet: pick(cfg.quiet_s, u64::from(s.quiet_minutes)),
            quick: pick(cfg.quick_scan_s, u64::from(s.quick_scan_minutes)),
            full: pick(cfg.full_scan_s, u64::from(s.full_scan_minutes)),
            reconcile: pick(cfg.reconcile_s, u64::from(s.reconcile_days) * 24 * 60),
            age: pick(cfg.age_s, 24 * 60),
        }
    }
}

/// Asks from outside the tick: signals and change notify.
#[derive(Clone, Default)]
pub struct Wants {
    pub quick: Arc<AtomicBool>,
    pub full: Arc<AtomicBool>,
    pub reconcile: Arc<AtomicBool>,
}

fn take(f: &AtomicBool) -> bool {
    f.swap(false, Ordering::SeqCst)
}

/// What one tick did (for logs and tests).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Tick {
    pub scanned: Option<&'static str>,
    pub reported: usize,
    pub sent: usize,
    pub copied: usize,
    pub drift: usize,
    /// `would_remove` events queued this tick (removals not live).
    pub would_remove: usize,
    /// The remover's pass (removals live).
    pub removals: remover::Pass,
    /// The placer's pass (placements in hand, phase A3.3).
    pub placements: placer::Pass,
}

pub struct Runner<S: AnnexService> {
    pub svc: S,
    pub db: AnnexDb,
    pub annex: EdgeAnnex,
    /// The mounted NAS (or a directory given), and the annex's path
    /// beneath it, resolved without following a symlink (`nasfs`).
    pub root: PathBuf,
    pub root_rel: String,
    pub journals: PathBuf,
    pub flavour: Flavour,
    pub ids: Arc<dyn IdMap>,
    pub timings: Timings,
    pub batch: usize,
    pub copies_per_tick: usize,
    pub wants: Wants,
    /// A reconciliation the service asked for that hasn't run yet.
    pub reconcile_asked: Option<String>,
    pub clock: Arc<dyn Fn() -> i64 + Send + Sync>,
    /// The approved rule set, parsed; an error stops copying (fail closed).
    pub rules: Result<RuleSet, String>,
    /// `GET annexes`' `served_at` (ms) and when it arrived (monotonic): the
    /// service's clock now is that plus the time since, and age-based
    /// removals wait while it is unknown or too far from the edge's.
    pub served: Option<(i64, std::time::Instant)>,
    /// The read-write mount, while the annex's removals are live (the unit
    /// mounts it, `annex_main.rs`); the remover alone writes through it.
    pub rw_mountpoint: Option<PathBuf>,
    /// The placement mount, while a placement is in hand (A3.3).
    pub pl_mountpoint: Option<PathBuf>,
    /// Why the placement mount couldn't be had, for the next pass to say.
    pub pl_problem: Option<String>,
    place_wait_until: i64,
    /// The staging folder's protected DACL names these (SMB).
    pub placement_sid: Option<String>,
    pub read_sid: Option<String>,
    /// The names to look those up by, when the config doesn't give the SIDs.
    pub placement_name: Option<String>,
    pub read_name: Option<String>,
    /// The read account's NFSv4 name, when it isn't the placement account.
    pub read_who: Option<String>,
    last_place_ask: i64,
    /// The account placements are made with, or why there isn't one (reported
    /// to the service, which registers nothing from it by itself).
    place_as: Result<String, String>,
    last_account_report: Option<i64>,
    /// The removal grace period's override (ms; 0: the annex's).
    pub grace_override_ms: i64,
    /// Check that the mounts are apart before any write (tests on one
    /// directory turn it off).
    pub check_mounts: bool,
    /// Whether a writable mount is checked to still be a mount. Off in tests, where a
    /// directory stands for it, unless a test turns it on.
    pub check_mounted: bool,
    /// What a test says the mount table shows: (a mount there, its device).
    #[cfg(test)]
    pub test_mount: Option<(bool, Option<u64>)>,
}

/// How far the edge's clock may be from the service's before age-based
/// removals stop (a clock run ahead makes every file look older).
pub const MAX_SKEW_MS: i64 = 10 * 60_000;

/// How long one dry run may take before it fails (it runs off the async
/// loop, but holds a thread and the NAS).
pub const DRY_RUN_BUDGET: std::time::Duration = std::time::Duration::from_secs(120);

fn served_of(a: &EdgeAnnex) -> Option<(i64, std::time::Instant)> {
    a.served_at
        .as_deref()
        .and_then(parse_rfc3339_ms)
        .map(|t| (t, std::time::Instant::now()))
}

/// Copied files that would leave the NAS now, decided exactly as the
/// preview decides them (`RuleSet::removal`, under this rule set).
pub fn age_candidates(
    db: &AnnexDb,
    rules: &RuleSet,
    fs: Option<&Root>,
    flavour: Flavour,
    ids: &dyn IdMap,
    now: i64,
) -> rusqlite::Result<Vec<(Entry, u64, String)>> {
    let mut out = Vec::new();
    let mut after = String::new();
    loop {
        let page = db.entries_after(&after, 5000)?;
        let Some(last) = page.last() else { break };
        after = last.path.clone();
        for e in page {
            if e.state != "copied" {
                continue;
            }
            let Some(version) = e.copied_version else {
                continue;
            };
            let f = preview::facts_of(db, &e);
            let mut look = Look {
                db,
                fs,
                flavour,
                ids,
                path: e.path.clone(),
                mtime_ns: e.seen.mtime_ns,
                sniff: false,
            };
            if let Removal::At(t, rule) = rules.removal(&f, &mut look, now, 0)
                && t <= now
            {
                out.push((e, version, rule));
            }
        }
    }
    Ok(out)
}

/// The approved rule set of `GET annexes` (`{version, body}`), parsed.
pub fn rules_of(a: &EdgeAnnex) -> Result<RuleSet, String> {
    let rs = a
        .ruleset
        .as_ref()
        .ok_or("the annex has no approved rule set")?;
    let version = rs
        .get("version")
        .and_then(|v| {
            v.as_u64()
                .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
        })
        .unwrap_or(0);
    rules::parse(version, rs.get("body").unwrap_or(&Value::Null))
}

impl<S: AnnexService> Runner<S> {
    pub fn new(
        svc: S,
        db: AnnexDb,
        annex: EdgeAnnex,
        root: &Path,
        state_dir: &Path,
        cfg: &AnnexConfig,
        ids: Box<dyn IdMap>,
    ) -> Runner<S> {
        let journals = state_dir.join("journals");
        let _ = std::fs::create_dir_all(&journals);
        Runner {
            rules: rules_of(&annex),
            served: served_of(&annex),
            reconcile_asked: None,
            rw_mountpoint: None,
            pl_mountpoint: None,
            pl_problem: None,
            place_wait_until: 0,
            placement_sid: (!cfg.placement_sid.is_empty()).then(|| cfg.placement_sid.clone()),
            read_sid: (!cfg.read_sid.is_empty()).then(|| cfg.read_sid.clone()),
            placement_name: super::mount::placement_name(cfg, &annex.filer),
            read_name: super::mount::read_name(cfg, &annex.filer),
            read_who: (!cfg.read_nfs4_who.is_empty()).then(|| cfg.read_nfs4_who.clone()),
            last_place_ask: 0,
            place_as: place_as(cfg, &annex),
            last_account_report: None,
            grace_override_ms: cfg.grace_s as i64 * 1000,
            check_mounts: true,
            check_mounted: cfg!(not(test)),
            #[cfg(test)]
            test_mount: None,
            timings: Timings::of(&annex, cfg),
            flavour: Flavour::from_protocol(&annex.filer.protocol),
            svc,
            db,
            annex,
            root: root.to_owned(),
            root_rel: String::new(),
            journals,
            ids: Arc::from(ids),
            batch: cfg.batch.clamp(1, 1000),
            copies_per_tick: cfg.copies_per_tick.max(1),
            wants: Wants::default(),
            clock: Arc::new(now_ms),
        }
    }

    fn now(&self) -> i64 {
        (self.clock)()
    }

    fn push(&self, kind: &str, path: Option<&str>, ev: Value) {
        if let Err(e) = self.db.push_event(kind, path, &ev) {
            warn!(error = %e, kind, "annex.db: event not queued");
        }
    }

    /// The mount check, as the filer's health, with whether the server shows its
    /// ACLs at the annex root (decision 64), when the mount could be read.
    /// Always sent, null when it can't be told (a failed mount, or an annex root
    /// that wouldn't open): the service takes a report without it as an edge from
    /// before decision 64, and flags the filer (the re-review of #85).
    pub fn report_mount(&self, mut details: Value) {
        let shown = if details["ok"] == true {
            open_root(&self.root, &self.root_rel)
                .and_then(|r| r.open_dir(""))
                .map(|(f, _)| super::acl::acls_shown(&f, self.flavour))
                .ok()
        } else {
            None
        };
        if let Some(o) = details.as_object_mut() {
            o.insert("acls_shown".into(), json!(shown));
        }
        self.push(
            "mount",
            None,
            json!({"kind": "mount", "details": details,
                                        "observed_at": rfc3339_ms(self.now())}),
        );
    }

    /// Settings changed in the portal (a poll of `GET annexes`).
    pub fn update(&mut self, a: EdgeAnnex, cfg: &AnnexConfig) {
        self.timings = Timings::of(&a, cfg);
        // The annex's mode decides which account it may place with.
        self.place_as = place_as(cfg, &a);
        let rules = rules_of(&a);
        let changed = match (&self.rules, &rules) {
            (Ok(old), Ok(new)) => old.version != new.version,
            (Err(_), Ok(_)) => true,
            _ => false,
        };
        if changed {
            // A new rule set: what a rule excluded is looked at again.
            match self.db.reconsider_excluded() {
                Ok(n) if n > 0 => {
                    info!(annex = a.annex_id, files = n, "excluded files re-evaluated")
                }
                Ok(_) => {}
                Err(e) => warn!(error = %e, "annex.db: excluded files not re-evaluated"),
            }
        }
        if let Err(e) = &rules {
            warn!(annex = a.annex_id, error = %e, "the rule set can't be read: copying stops");
        }
        self.rules = rules;
        if let Some(s) = served_of(&a) {
            self.served = Some(s);
        }
        // Asked for one now: once per ask, answered when a reconciliation has run, so a
        // restart in between asks again and no clock is compared. The service shows an
        // ask only once its backoff has passed.
        if let Some(asked) = a.reconcile_asked_at.clone()
            && self.db.meta(RECONCILE_ANSWERED).ok().flatten().as_deref() != Some(&asked)
        {
            self.reconcile_asked = Some(asked);
        }
        self.annex = a;
    }

    /// Whether the edge's clock can be trusted for an age-based removal.
    pub fn clock_ok(&self) -> Result<(), String> {
        let Some((at, when)) = self.served else {
            return Err("the service's time is not known yet".into());
        };
        let service_now = at + when.elapsed().as_millis() as i64;
        let ahead = self.now() - service_now;
        if ahead.abs() > MAX_SKEW_MS {
            return Err(format!(
                "the edge's clock is {} s off the service's",
                ahead / 1000
            ));
        }
        Ok(())
    }

    /// The annex's root, opened for one scan, quiet check or copy.
    pub fn fs(&self) -> std::io::Result<Root> {
        open_root(&self.root, &self.root_rel)
    }

    pub fn paused(&self) -> bool {
        self.annex.state == "paused"
    }

    // --- scans ---------------------------------------------------------------

    fn meta_i64(&self, k: &str) -> i64 {
        self.db.meta_i64(k).ok().flatten().unwrap_or(0)
    }

    /// The scan due now, if any, and run it.
    pub fn scan_due(&mut self, now: i64) -> rusqlite::Result<Option<&'static str>> {
        let baseline = self.db.meta("baseline_done")?.is_none();
        let which = if baseline {
            "baseline"
        } else if take(&self.wants.full) || now - self.meta_i64("last_full") >= self.timings.full {
            "full"
        } else if take(&self.wants.quick) || now - self.meta_i64("last_quick") >= self.timings.quick
        {
            "quick"
        } else {
            return Ok(None);
        };
        let mut s = Scanner::with_root(&self.db, self.fs(), now, baseline);
        s.at_export_root = self.root_rel.is_empty();
        match which {
            "quick" => s.quick()?,
            _ => s.full()?,
        }
        for (path, why) in s.problems.drain(..) {
            let p = if path.is_empty() { None } else { Some(path) };
            self.push(
                "skipped",
                p.as_deref(),
                json!({"kind": "skipped",
                "path": p.clone().unwrap_or_else(|| ".".into()), "reason": "unreadable",
                "detail": why.chars().take(900).collect::<String>()}),
            );
        }
        let stats = s.stats.clone();
        let at = rfc3339_ms(now);
        if baseline {
            self.push(
                "baseline",
                None,
                json!({"kind": "baseline", "observed_at": at,
                "details": {"files": stats.files, "bytes": stats.bytes}}),
            );
            self.db.set_meta("baseline_done", &now.to_string())?;
            self.db.set_meta("last_reconcile", &now.to_string())?;
        }
        let kind = if which == "quick" { "quick" } else { "full" };
        self.push(
            "scan",
            None,
            json!({"kind": "scan", "observed_at": at,
                                       "details": stats.json(kind)}),
        );
        self.db.set_meta("last_quick", &now.to_string())?;
        if which != "quick" {
            self.db.set_meta("last_full", &now.to_string())?;
            self.folder_pass(now)?;
        } else {
            self.new_folder_pass(now)?;
        }
        info!(
            annex = self.annex.annex_id,
            scan = which,
            ?stats,
            "annex scan"
        );
        Ok(Some(which))
    }

    /// After a full scan: the folders' ACLs and the free space, for where a
    /// recalled file may be placed (`folders.rs`, A3.1). Read-only.
    fn folder_pass(&self, now: i64) -> rusqlite::Result<()> {
        let Ok(export) = open_root(&self.root, "") else {
            return Ok(()); // the mount check says why
        };
        let body = self.annex.ruleset.as_ref().and_then(|r| r.get("body"));
        let extra = folders::extra_locations(body, self.annex.filer.filer_id, &self.root_rel);
        let s = folders::pass(
            &self.db,
            &export,
            &self.root_rel,
            &extra,
            self.flavour,
            &*self.ids,
            now,
        )?;
        if s.truncated {
            warn!(
                annex = self.annex.annex_id,
                limit = folders::MAX_EXTRA_DIRS,
                "folder ACLs: the return locations hold too many folders; the rest aren't read"
            );
        }
        info!(annex = self.annex.annex_id, ?s, "folder ACLs");
        Ok(())
    }

    /// After a quick scan: the folders no ACL was sent for yet (decision 63: an
    /// annexed file is hidden until every folder above it is read).
    fn new_folder_pass(&self, now: i64) -> rusqlite::Result<()> {
        let Ok(export) = open_root(&self.root, "") else {
            return Ok(()); // the mount check says why
        };
        let s = folders::pass_new(
            &self.db,
            &export,
            &self.root_rel,
            self.flavour,
            &*self.ids,
            now,
        )?;
        if s.folders > 0 {
            info!(annex = self.annex.annex_id, ?s, "new folders' ACLs");
        }
        Ok(())
    }

    // --- the quiet period --------------------------------------------------------

    /// Report every file that has been quiet long enough (and is whole, if
    /// SEG-Y). Returns how many were reported.
    pub fn report_quiet(&mut self, now: i64) -> rusqlite::Result<usize> {
        let due = self
            .db
            .entries_due(now - self.timings.quiet, now - RETRY_MS, 2000)?;
        let mut n = 0;
        let Ok(fs) = self.fs() else {
            return Ok(0); // the mount check says why
        };
        for e in due {
            let md = match fs.stat(&e.path, false) {
                Ok(m) if m.is_file() => m,
                Ok(_) | Err(_) => continue, // gone or replaced: the next scan says
            };
            let s = md.seen();
            if !s.same(e.seen.size, e.seen.mtime_ns, e.seen.ctime_ns) || s.ino != e.seen.ino {
                // Not quiet: the clock starts again.
                self.db.observe(&e.path, &s, now, e.baseline, 0)?;
                continue;
            }
            match segy::check_file(&fs, &e.path, s.size) {
                Ok(Segy::Incomplete) => {
                    let first = e
                        .problem_at
                        .filter(|_| e.problem.as_deref() == Some("segy_incomplete"));
                    if first.is_none_or(|t| now - t < SEGY_GIVE_UP_MS) {
                        self.db.conn().execute(
                            "UPDATE entry SET problem = 'segy_incomplete', \
                             problem_at = coalesce(CASE WHEN problem = 'segy_incomplete' \
                             THEN problem_at END, ?2), stable_since = ?2 WHERE path = ?1",
                            rusqlite::params![e.path, now],
                        )?;
                        continue;
                    }
                    self.push(
                        "skipped",
                        Some(&e.path),
                        json!({"kind": "skipped",
                        "path": e.path, "reason": "segy_incomplete",
                        "detail": "not whole traces after 24 hours: copied as it is"}),
                    );
                }
                Ok(Segy::Unrecognised) => {
                    self.push(
                        "skipped",
                        Some(&e.path),
                        json!({"kind": "skipped",
                        "path": e.path, "reason": "segy_unrecognised",
                        "detail": "SEG-Y headers not recognised: copied as any file"}),
                    );
                }
                Ok(_) => {}
                Err(err) => {
                    self.unreadable(&e, &err.to_string(), now)?;
                    continue;
                }
            }
            if self.report(&fs, &e, &s, &md, now)? {
                n += 1;
            }
        }
        Ok(n)
    }

    fn unreadable(&self, e: &Entry, why: &str, now: i64) -> rusqlite::Result<()> {
        self.db
            .set_problem(&e.path, "unreadable", now, Some("skipped"))?;
        self.push(
            "skipped",
            Some(&e.path),
            json!({"kind": "skipped", "path": e.path,
            "reason": "unreadable", "detail": why.chars().take(900).collect::<String>()}),
        );
        Ok(())
    }

    /// Report one quiet file (`arrived` or `changed`); `false` if its ACL
    /// can't be read, when it is skipped (`acl_unreadable`) instead.
    fn report(
        &self,
        fs: &Root,
        e: &Entry,
        s: &Seen,
        md: &Stat,
        now: i64,
    ) -> rusqlite::Result<bool> {
        let acl = match acl::capture(fs, &e.path, self.flavour, md, &*self.ids) {
            Ok(a) => a,
            Err(why) => {
                // Fail closed: never a guessed ACL. Looked at again later.
                self.db
                    .set_problem(&e.path, "acl_unreadable", now, Some("skipped"))?;
                self.push(
                    "skipped",
                    Some(&e.path),
                    json!({"kind": "skipped", "path": e.path, "reason": "acl_unreadable",
                    "detail": why.chars().take(900).collect::<String>()}),
                );
                return Ok(false);
            }
        };
        let (o, g) = acl::owner_group(&acl);
        self.db
            .set_owner_group(&e.path, o.as_deref(), g.as_deref())?;
        let trigger = if e.file_id.is_some() {
            Trigger::Changed
        } else {
            Trigger::Arrived
        };
        let Ok(rules) = &self.rules else {
            return Ok(false); // fail closed: tick() reports the rule set
        };
        let f = preview::facts_of(&self.db, e);
        let mut look = Look {
            db: &self.db,
            fs: Some(fs),
            flavour: self.flavour,
            ids: &*self.ids,
            path: e.path.clone(),
            mtime_ns: e.seen.mtime_ns,
            // A content-type condition reads the first 4 KB (which moves
            // the file's atime; the editor says so).
            sniff: true,
        };
        let decided = match rules.decide(trigger, &f, &mut look, now) {
            Decision::Rule(r) => Some(r.clone()),
            Decision::NoRule => None,
            Decision::Undecided(_) => {
                // A condition can't be read now: neither copied nor excluded
                // by a guess. Looked at again later.
                self.db
                    .set_problem(&e.path, "rules_undecided", now, Some("skipped"))?;
                return Ok(false);
            }
        };
        if trigger == Trigger::Arrived {
            // The arrival's rule, for the record; removals re-decide it
            // under the running rule set, never from this.
            let rule_id = decided.as_ref().map_or("none", |r| r.id.as_str());
            self.db.set_rule(&e.path, rule_id, rules.version)?;
        }
        let Some(rule) = decided.filter(|r| r.actions.copy) else {
            // Excluded (by a rule, or no rule matched): never copied, never in
            // the catalog. A new rule set looks again.
            self.db.set_state(&e.path, "excluded")?;
            return Ok(false);
        };
        let kind = trigger.name();
        let mut ev = json!({
            "kind": kind, "path": e.path, "nas_file_id": s.ino, "size": s.size,
            "mtime_ns": s.mtime_ns.to_string(), "ctime_ns": s.ctime_ns.to_string(),
            "acl": acl, "observed_at": rfc3339_ms(now),
        });
        if let Some(b) = s.btime_ns {
            ev["btime_ns"] = json!(b.to_string());
        }
        if e.baseline && e.file_id.is_none() {
            ev["baseline"] = json!(true);
        }
        ev["rule"] = json!(rule.id);
        if !rule.actions.tags.is_empty() {
            ev["tags"] = json!(
                rule.actions
                    .tags
                    .iter()
                    .map(|(k, v)| json!({"key": k, "value": v}))
                    .collect::<Vec<_>>()
            );
        }
        if self.flavour != Flavour::Smb {
            // Over SMB the client shows the mount's owner, not the file's.
            ev["uid"] = json!(md.uid);
            ev["gid"] = json!(md.gid);
            ev["mode"] = json!(md.mode & 0o7777);
        }
        self.db.push_event(kind, Some(&e.path), &ev)?;
        self.db.set_reported(&e.path, s)?;
        Ok(true)
    }

    // --- events ----------------------------------------------------------------

    /// Send the outbox, in order, until it is empty or the service can't be
    /// reached. Returns how many events were answered.
    pub async fn send(&mut self) -> usize {
        let mut sent = 0;
        loop {
            let batch = match self.db.outbox(self.batch) {
                Ok(b) if !b.is_empty() => b,
                _ => return sent,
            };
            let (first, last) = (batch[0].0, batch[batch.len() - 1].0);
            let events: Vec<Value> = batch.iter().map(|b| b.3.clone()).collect();
            let key = format!("{first}-{last}");
            let results = match self.svc.events(self.annex.annex_id, &events, &key).await {
                Ok(r) => r,
                Err(e) => {
                    if !e.unavailable {
                        warn!(error = %e, "annex events refused whole; dropped");
                        let _ = self.db.drop_events(last);
                        continue;
                    }
                    warn!(error = %e, "annex events: service unreachable; later");
                    return sent;
                }
            };
            let now = self.now();
            for r in &results {
                let Some((_, kind, path, _)) = batch.get(r.index) else {
                    continue;
                };
                let Some(path) = path else { continue };
                match kind.as_str() {
                    "arrived" | "changed" if r.applied() => {
                        if let Some(fid) = r.file_id {
                            let _ = self.db.set_catalog(
                                path,
                                fid,
                                r.content_version_id,
                                r.copy.unwrap_or(true),
                            );
                        }
                    }
                    // A placement may be making this file: said again later,
                    // when its `done` has said what it is.
                    "arrived" | "changed" if r.code.as_deref() == Some("placement_in_progress") => {
                        let _ =
                            self.db
                                .set_problem(path, "placement_in_progress", now, Some("new"));
                    }
                    "arrived" | "changed" => {
                        let code = r.code.clone().unwrap_or_else(|| "rejected".into());
                        let _ = self.db.set_problem(path, &code, now, Some("skipped"));
                        warn!(path, code, detail = ?r.detail, "the service refused a file");
                    }
                    _ if !r.applied() => {
                        warn!(path, kind, code = ?r.code, detail = ?r.detail, "annex event refused");
                    }
                    _ => {}
                }
            }
            let _ = self.db.drop_events(last);
            sent += batch.len();
        }
    }

    // --- copies ------------------------------------------------------------------

    pub async fn copy_ready(&mut self) -> usize {
        let ready = match self.db.entries_in("ready", self.copies_per_tick) {
            Ok(r) => r,
            Err(_) => return 0,
        };
        let mut done = 0;
        for e in ready {
            let now = self.now();
            let c = Copier {
                svc: &self.svc,
                db: &self.db,
                annex_id: self.annex.annex_id,
                root: self.root.clone(),
                root_rel: self.root_rel.clone(),
                journals: self.journals.clone(),
                now,
                read_fault: None,
            };
            let outcome = if e.problem.as_deref() == Some("copy_mismatch") {
                // Copied again under a new op id: the old copy is not to be
                // reused (it no longer matches).
                c.copy_again(&e).await
            } else {
                c.copy(&e).await
            };
            match outcome {
                Copied::Done { version_id } => {
                    done += 1;
                    // Move now: removal follows the copy, decided again under
                    // the running rule set as the preview decides it (never
                    // the rule saved at arrival). A2.1 only says so.
                    if let Some(rule) = self.removal_now(&e, now) {
                        self.would_remove(&e, version_id, &rule, now);
                    }
                }
                Copied::Changed => {
                    if let Ok(md) = self.fs().and_then(|r| r.stat(&e.path, false))
                        && md.is_file()
                    {
                        let _ = self.db.observe(&e.path, &md.seen(), now, e.baseline, 0);
                    }
                    let _ = self.db.end_copy(&e.path);
                }
                Copied::Gone => {
                    let _ = self.db.end_copy(&e.path);
                    let _ = self.db.set_state(&e.path, "new");
                }
                Copied::Later(why) => {
                    warn!(path = e.path, why, "copy: later");
                    break;
                }
                Copied::Failed(why) => {
                    let _ = self.db.end_copy(&e.path);
                    let _ = self
                        .db
                        .set_problem(&e.path, "copy_failed", now, Some("skipped"));
                    self.push(
                        "copy_failed",
                        Some(&e.path),
                        json!({"kind": "copy_failed",
                        "path": e.path, "file_id": e.file_id.map(|f| f.to_string()),
                        "reason": "copy_failed",
                        "detail": why.chars().take(900).collect::<String>()}),
                    );
                }
            }
        }
        done
    }

    // --- reconciliation --------------------------------------------------------------

    /// A fresh walk of the whole directory, compared with the catalog's record.
    /// `only_refused`: copy again only the records the read-back refused, not those
    /// whose read-back is pending (a reconciliation the service asked for).
    pub async fn reconcile(&mut self, now: i64, only_refused: bool) -> Result<usize, String> {
        // An unreadable root aborts: no record is judged against nothing.
        let fs = self.fs().map_err(|e| format!("the annex root: {e}"))?;
        let walk = walk_all(&fs, self.root_rel.is_empty())?;
        let nas = walk.files;
        let under_unreadable = |path: &str| {
            walk.unreadable
                .iter()
                .any(|(d, _)| d.is_empty() || path.starts_with(&format!("{d}/")) || path == d)
        };
        for (dir, why) in &walk.unreadable {
            self.push(
                "skipped",
                Some(dir),
                json!({"kind": "skipped", "path": dir, "reason": "unreadable",
                "detail": format!("reconciliation: {why}").chars().take(900).collect::<String>()}),
            );
        }
        let mut recs: HashMap<String, AnnexRecord> = HashMap::new();
        let mut after = String::new();
        loop {
            let page = self
                .svc
                .records(self.annex.annex_id, &after, 5000)
                .await
                .map_err(|e| e.to_string())?;
            let next = page.next.clone();
            for r in page.records {
                recs.insert(r.path.clone(), r);
            }
            match next {
                Some(n) => after = n,
                None => break,
            }
        }
        let mut drift = 0;
        let db = &self.db;
        let mut push_drift = |path: &str, kind: &str, file: Option<u64>, exp: Value, obs: Value| {
            drift += 1;
            let mut ev = json!({"kind": "drift", "path": path, "observed_at": rfc3339_ms(now),
                                "details": {"drift": kind, "expected": exp, "observed": obs}});
            if let Some(f) = file {
                ev["file_id"] = json!(f.to_string());
            }
            let _ = db.push_event("drift", Some(path), &ev);
        };
        let attrs = |s: &Seen| {
            json!({"size": s.size, "mtime_ns": s.mtime_ns.to_string(),
                                      "ctime_ns": s.ctime_ns.to_string(), "nas_file_id": s.ino})
        };
        for (path, s) in &nas {
            let rec = recs.remove(path);
            let e = db.entry(path).map_err(|e| e.to_string())?;
            let pending = |e: &Option<Entry>| {
                e.as_ref().is_some_and(|e| {
                    e.seen.same(s.size, s.mtime_ns, s.ctime_ns)
                        && matches!(
                            e.state.as_str(),
                            "new" | "reported" | "ready" | "skipped" | "excluded"
                        )
                })
            };
            match rec {
                None => {
                    if pending(&e) {
                        continue; // known, waiting for its quiet period or copy
                    }
                    push_drift(path, "missed_arrival", None, json!(null), attrs(s));
                    if e.is_some() {
                        let _ = db.remove(path);
                    }
                    let _ = db.observe(path, s, now, false, 0);
                }
                Some(r) => {
                    let copied_same = r.copied_size == Some(s.size)
                        && nanos(&r.copied_mtime_ns) == Some(s.mtime_ns)
                        && nanos(&r.copied_ctime_ns).is_none_or(|c| c == s.ctime_ns);
                    if r.nas_file_id != s.ino && !pending(&e) {
                        // Another file under the name, never reported.
                        push_drift(
                            path,
                            "changed_no_event",
                            Some(r.file_id),
                            json!({"nas_file_id": r.nas_file_id}),
                            attrs(s),
                        );
                        let _ = db.remove(path);
                        let _ = db.observe(path, s, now, false, 0);
                        continue;
                    }
                    if !copied_same {
                        if pending(&e) {
                            continue;
                        }
                        let exp = json!({"size": r.copied_size, "mtime_ns": r.copied_mtime_ns,
                                         "ctime_ns": r.copied_ctime_ns});
                        push_drift(path, "changed_no_event", Some(r.file_id), exp, attrs(s));
                        let _ = db.observe(path, s, now, false, 0);
                        let _ = db.conn().execute(
                            "UPDATE entry SET file_id = ?2 WHERE path = ?1 AND file_id IS NULL",
                            rusqlite::params![path, r.file_id as i64],
                        );
                        continue;
                    }
                    // Not a copy an ArchiveAdmin stopped: the same bytes again would
                    // be refused again (the service's migration 0058).
                    if r.copied_version_id.is_some()
                        && !r.copy_ok
                        && !r.copy_stopped
                        && (r.refused || !only_refused)
                    {
                        push_drift(
                            path,
                            "copy_mismatch",
                            Some(r.file_id),
                            json!({"version_id": r.copied_version_id.map(|v| v.to_string())}),
                            json!({"copy_ok": false}),
                        );
                        let _ = db.observe(path, s, now, false, 0);
                        let _ = db.conn().execute(
                            "UPDATE entry SET file_id = ?2, state = 'ready', problem = 'copy_mismatch', \
                             problem_at = ?3, reported_size = size, reported_mtime = mtime_ns, \
                             reported_ctime = ctime_ns WHERE path = ?1",
                            rusqlite::params![path, r.file_id as i64, now],
                        );
                        continue;
                    }
                    if e.is_none() {
                        // annex.db lost the row: put it back as copied.
                        let _ = db.observe(path, s, now, false, 0);
                        let _ = db.conn().execute(
                            "UPDATE entry SET file_id = ?2, state = 'copied', copied_size = size, \
                             copied_mtime = mtime_ns, copied_ctime = ctime_ns, copied_version = ?3 \
                             WHERE path = ?1",
                            rusqlite::params![
                                path,
                                r.file_id as i64,
                                r.copied_version_id.map(|v| v as i64)
                            ],
                        );
                    }
                }
            }
        }
        // In the record, not on the NAS: a delete the scans never reported.
        // Records under a folder the walk couldn't read are not judged: the
        // folder is unreadable, not empty.
        // Nor the files the remover hid or removed (not a user's delete).
        let mut missing: Vec<_> = recs
            .into_values()
            .filter(|r| !under_unreadable(&r.path))
            .filter(|r| {
                !matches!(
                    db.removal_state_of(&r.path).ok().flatten().as_deref(),
                    Some(s) if s != "cancelled"
                )
            })
            // Nor one a placement hid to replace it (A3.4).
            .filter(|r| !super::placer::replacing(db, &r.path))
            .collect();
        missing.sort_by(|a, b| a.path.cmp(&b.path));
        for r in missing {
            let exp = json!({"size": r.size, "nas_file_id": r.nas_file_id});
            push_drift(
                &r.path,
                "missed_departure",
                Some(r.file_id),
                exp,
                json!(null),
            );
            let _ = db.push_event(
                "left",
                Some(&r.path),
                &json!({"kind": "left", "path": r.path,
                "nas_file_id": r.nas_file_id, "reason": "reconciliation",
                "observed_at": rfc3339_ms(now)}),
            );
            let _ = db.remove(&r.path);
        }
        // In annex.db only (never reported) and not on the NAS: forget it.
        let mut after = String::new();
        loop {
            let page = db.entries_after(&after, 5000).map_err(|e| e.to_string())?;
            let Some(last) = page.last() else { break };
            after = last.path.clone();
            for e in page {
                if !nas.contains_key(&e.path)
                    && e.file_id.is_none()
                    && e.state != "reported"
                    && !under_unreadable(&e.path)
                {
                    let _ = db.remove(&e.path);
                }
            }
        }
        drop(push_drift);
        self.push(
            "scan",
            None,
            json!({"kind": "scan", "observed_at": rfc3339_ms(now),
            "details": {"scan": "reconcile", "files": nas.len(), "drift": drift,
                        "unreadable": walk.unreadable.len()}}),
        );
        // The schedule counts only the runs that also copy what isn't `copy_ok`: asks
        // never put off the one that copies a stuck copy again.
        if !only_refused {
            let _ = self.db.set_meta("last_reconcile", &now.to_string());
        }
        if let Some(asked) = self.reconcile_asked.take() {
            let _ = self.db.set_meta(RECONCILE_ANSWERED, &asked);
        }
        info!(
            annex = self.annex.annex_id,
            files = nas.len(),
            drift,
            "annex reconciliation"
        );
        Ok(drift)
    }

    // --- the tick ----------------------------------------------------------------------

    /// Whether the annex's removals are live (`GET annexes`).
    pub fn removals_live(&self) -> bool {
        self.annex.removals.as_ref().is_some_and(|r| r.live)
    }

    /// A file a removing rule now chooses. While removals are live, a
    /// candidate for a ticket (`remover.rs`); otherwise (the dry run, a
    /// pause) a `would_remove` event, with what the edge's own checks say
    /// (gate 6: everything but the write), once per copied version.
    fn would_remove(&self, e: &Entry, version_id: u64, rule: &str, now: i64) {
        if self.removals_live() {
            if let Some(file_id) = e.file_id {
                let v = self.rules.as_ref().map(|r| r.version).unwrap_or(0);
                let _ = self
                    .db
                    .add_removal_candidate(&e.path, file_id, version_id, rule, v);
            }
            return;
        }
        let f = self.db.facts(&e.path).unwrap_or_default();
        if f.would_remove == Some(version_id) {
            return;
        }
        let checks = self.fs().ok().map(|fs| {
            let (d, name) = remover::split(&e.path);
            let expect = remover::Expect {
                ino: e.seen.ino.parse().unwrap_or(0),
                size: e.seen.size,
                mtime_ns: e.seen.mtime_ns,
                ctime_ns: Some(e.seen.ctime_ns),
            };
            match fs.sub(d) {
                Ok(dir) => match remover::local_gates(
                    &dir,
                    name,
                    &expect,
                    self.flavour == Flavour::Smb,
                    expect.ctime_ns,
                ) {
                    remover::Check::Ok => "ok".into(),
                    remover::Check::Gone => "gone".into(),
                    remover::Check::Transient(why) | remover::Check::Refuse(why) => why,
                },
                Err(err) => format!("folder: {err}"),
            }
        });
        let reason = self
            .annex
            .removals
            .as_ref()
            .and_then(|r| r.why_not.clone())
            .unwrap_or_else(|| "removals_not_enabled".into());
        self.push(
            "would_remove",
            Some(&e.path),
            json!({"kind": "would_remove", "path": e.path,
                   "file_id": e.file_id.map(|f| f.to_string()),
                   "version_id": version_id.to_string(), "rule": rule,
                   "reason": reason, "edge_checks": checks, "observed_at": rfc3339_ms(now)}),
        );
        let _ = self.db.set_would_remove(&e.path, version_id);
    }

    /// Whether the read-write mount is needed: removals live, or files the
    /// remover hid waiting to be put back after removals stopped (not for a
    /// pause or the window, which only hold them).
    pub fn needs_rw(&self) -> bool {
        if self.removals_live() {
            return true;
        }
        let hold = self
            .annex
            .removals
            .as_ref()
            .and_then(|r| r.why_not.as_deref())
            .is_some_and(|w| remover::HOLD_REASONS.contains(&w));
        !hold && self.db.removals_on_the_nas().unwrap_or(true)
    }

    /// Before any write: the read-only mount is still read-only, and the
    /// read-write one is another superblock (its own `st_dev`).
    fn mounts_apart(&self, rw: &Path) -> Result<(), String> {
        if !self.check_mounts {
            return Ok(());
        }
        use std::os::unix::fs::MetadataExt;
        let ro = super::mount::vfs_flags(&self.root).map_err(|e| format!("statvfs: {e}"))?;
        if super::mount::flags_problem(ro, true).is_some_and(|p| p.contains("ro")) {
            return Err("the read-only mount is writable".into());
        }
        let a = std::fs::metadata(&self.root)
            .map_err(|e| e.to_string())?
            .dev();
        let b = std::fs::metadata(rw).map_err(|e| e.to_string())?.dev();
        if a == b {
            return Err("the read-write mount shares the read-only mount's superblock".into());
        }
        Ok(())
    }

    /// Whether a writable mount is one. A helper that outlived its SIGKILL can finish an
    /// `umount -l` after the mount was made again, and `mounts_apart` can't tell: a local
    /// directory has its own `st_dev` too. Anything written there would be on the edge's
    /// disk and could be reported as placed on the NAS.
    fn still_mounted(&self, target: &Path) -> bool {
        #[cfg(test)]
        if let Some((there, _)) = self.test_mount {
            return !self.check_mounted || there;
        }
        !self.check_mounted || super::mount::mounted(target)
    }

    /// And the root opened from it is that mount's, since the mount can go between the
    /// check above and the open.
    fn opened_on_mount(&self, target: &Path, root: &Root) -> bool {
        #[cfg(test)]
        if let Some((_, dev)) = self.test_mount {
            return !self.check_mounted || dev == Some(root.dev());
        }
        !self.check_mounted || super::mount::mount_dev(target) == Some(root.dev())
    }

    /// One remover pass (phase A2.2) while the read-write mount is there:
    /// removals when they are live; otherwise putting back what was hidden.
    pub async fn remove_pass(&mut self, now: i64) -> remover::Pass {
        let Some(rw) = self.rw_mountpoint.clone() else {
            return remover::Pass::default();
        };
        if !self.needs_rw() {
            return remover::Pass::default();
        }
        if !self.still_mounted(&rw) {
            error!(
                annex = self.annex.annex_id,
                "ALERT removals: the read-write mount is gone; nothing written; mounting again"
            );
            self.rw_mountpoint = None;
            return remover::Pass::default();
        }
        if let Err(why) = self.mounts_apart(&rw) {
            error!(
                annex = self.annex.annex_id,
                why, "ALERT removals: the mounts aren't apart; nothing written"
            );
            return remover::Pass::default();
        }
        let drain = (!self.removals_live()).then(|| {
            self.annex
                .removals
                .as_ref()
                .and_then(|r| r.why_not.clone())
                .unwrap_or_else(|| "removals_stopped".into())
        });
        let r = self.annex.removals.clone().unwrap_or_default();
        let root = match open_root(&rw, &self.root_rel) {
            Ok(root) => root,
            Err(e) => {
                warn!(annex = self.annex.annex_id, error = %e, "the read-write mount is unreadable");
                return remover::Pass::default();
            }
        };
        if !self.opened_on_mount(&rw, &root) {
            error!(
                annex = self.annex.annex_id,
                "ALERT removals: the read-write mount went as it was opened; nothing written"
            );
            self.rw_mountpoint = None;
            return remover::Pass::default();
        }
        let grace_ms = if self.grace_override_ms > 0 {
            self.grace_override_ms
        } else {
            r.grace_minutes.max(1) as i64 * 60_000
        };
        let state_dir = self
            .journals
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_default();
        let rm = remover::Remover {
            svc: &self.svc,
            db: &self.db,
            rw: &root,
            annex_id: self.annex.annex_id,
            annex_name: &self.annex.name,
            smb: self.flavour == Flavour::Smb,
            grace_ms,
            pointer_files: r.pointer_files,
            rate_per_hour: r.rate_per_hour,
            now,
            rules_version: self.rules.as_ref().map(|r| r.version).unwrap_or(0),
            drain,
            state_dir: &state_dir,
        };
        rm.pass().await
    }

    /// A3.4: the grace between hiding a file a placement replaces and placing the
    /// new one: the annex's removal grace (15 minutes unless set; one at least),
    /// which the service keeps too.
    fn replace_grace_ms(&self) -> i64 {
        if self.grace_override_ms > 0 {
            return self.grace_override_ms;
        }
        let minutes = self
            .annex
            .removals
            .as_ref()
            .map(|r| r.grace_minutes)
            .filter(|m| *m > 0)
            .unwrap_or(15);
        minutes.max(1) as i64 * 60_000
    }

    /// Whether a just-copied file leaves the NAS now, by the running rules.
    fn removal_now(&self, e: &Entry, now: i64) -> Option<String> {
        let rules = self.rules.as_ref().ok()?;
        let fs = self.fs().ok();
        let f = preview::facts_of(&self.db, e);
        let mut look = Look {
            db: &self.db,
            fs: fs.as_ref(),
            flavour: self.flavour,
            ids: &*self.ids,
            path: e.path.clone(),
            mtime_ns: e.seen.mtime_ns,
            sniff: false,
        };
        match rules.removal(&f, &mut look, now, 0) {
            // Removal at the copy itself needs no clock; a later one does.
            Removal::At(t, rule)
                if t <= now && (self.clock_ok().is_ok() || moves_now(rules, &rule)) =>
            {
                Some(rule)
            }
            _ => None,
        }
    }

    /// The daily look at `age` rules and `remove_after_days`: copied files
    /// that would leave the NAS now. It reads the whole listing (and ACLs
    /// not cached), so it runs off the async loop on its own connection.
    pub async fn age_pass(&mut self, now: i64) -> rusqlite::Result<usize> {
        let Ok(rules) = self.rules.clone() else {
            return Ok(0);
        };
        if !rules.removes() {
            return Ok(0);
        }
        if let Err(why) = self.clock_ok() {
            // Every age is measured with this clock: nothing leaves by age
            // until it agrees with the service's. Looked at again next tick.
            warn!(
                annex = self.annex.annex_id,
                why, "annex age rules: waiting for the clock"
            );
            return Ok(0);
        }
        let db = self.db.reopen()?;
        let (root, rel, flavour, ids) = (
            self.root.clone(),
            self.root_rel.clone(),
            self.flavour,
            self.ids.clone(),
        );
        let found = tokio::task::spawn_blocking(move || {
            let fs = open_root(&root, &rel).ok();
            age_candidates(&db, &rules, fs.as_ref(), flavour, &*ids, now)
        })
        .await
        .map_err(|e| rusqlite::Error::ToSqlConversionFailure(Box::new(e)))??;
        let mut n = 0;
        for (e, version, rule) in found {
            let before = self.db.outbox_len()?;
            self.would_remove(&e, version, &rule, now);
            if self.db.outbox_len()? > before {
                n += 1;
            }
        }
        self.db.set_meta("last_age", &now.to_string())?;
        Ok(n)
    }

    /// Answer the dry runs the service asked for (read-only).
    pub async fn dry_runs(&mut self) -> usize {
        let runs = match self.svc.dry_runs(self.annex.annex_id).await {
            Ok(r) => r,
            Err(e) => {
                if !e.unavailable {
                    warn!(error = %e, "annex dry runs: refused");
                }
                return 0;
            }
        };
        let mut done = 0;
        for run in runs
            .into_iter()
            .filter(|r| r.annex_id == self.annex.annex_id)
        {
            // Taken first: a run the service no longer wants (replaced by a
            // newer draft, or expired) is refused here and dropped.
            if let Err(e) = self
                .svc
                .dry_run_report(run.run_id, &json!({"state": "running"}))
                .await
            {
                info!(run = run.run_id, error = %e, "annex dry run: not taken");
                continue;
            }
            let now = self.now();
            let body = match rules::parse(0, &run.body) {
                Err(e) => json!({"state": "failed", "error": e}),
                Ok(rs) => match self.preview_off_loop(rs, now).await {
                    Ok(report) => json!({"state": "done", "report": report}),
                    Err(e) => json!({"state": "failed", "error": e}),
                },
            };
            match self.svc.dry_run_report(run.run_id, &body).await {
                Ok(()) => done += 1,
                Err(e) => warn!(run = run.run_id, error = %e, "annex dry run: report not sent"),
            }
        }
        done
    }

    /// One preview on a blocking thread, on its own `annex.db` connection.
    async fn preview_off_loop(&self, rs: RuleSet, now: i64) -> Result<Value, String> {
        let db = self.db.reopen().map_err(|e| format!("annex.db: {e}"))?;
        let (root, rel, flavour, ids) = (
            self.root.clone(),
            self.root_rel.clone(),
            self.flavour,
            self.ids.clone(),
        );
        let deadline = std::time::Instant::now() + DRY_RUN_BUDGET;
        tokio::task::spawn_blocking(move || {
            let fs = open_root(&root, &rel).ok();
            preview::dry_run(&db, &rs, fs.as_ref(), flavour, &*ids, now, Some(deadline))
        })
        .await
        .map_err(|e| format!("preview task: {e}"))?
    }

    pub async fn tick(&mut self) -> Tick {
        let mut t = Tick::default();
        let now = self.now();
        // A placement's `done` goes before the events of any scan that met its
        // file, and needs no mount.
        placer::report_all(&self.svc, &self.db, now).await;
        self.report_placement_account(now);
        if let Err(why) = &self.rules {
            // Fail closed: scans and events go on, nothing is reported for
            // copying until the rule set reads. Said once per rule set.
            let key = format!(
                "rules_error:{}",
                self.annex
                    .ruleset
                    .as_ref()
                    .and_then(|r| r.get("version"))
                    .map(|v| v.to_string())
                    .unwrap_or_default()
            );
            if self.db.meta("rules_error").ok().flatten().as_deref() != Some(key.as_str()) {
                self.push(
                    "skipped",
                    None,
                    json!({"kind": "skipped", "path": ".", "reason": "rules_unreadable",
                           "detail": why.chars().take(900).collect::<String>()}),
                );
                let _ = self.db.set_meta("rules_error", &key);
            }
        }
        if !self.paused() {
            match self.scan_due(now) {
                Ok(s) => t.scanned = s,
                Err(e) => warn!(error = %e, "annex scan failed"),
            }
            let baseline_done = self.db.meta("baseline_done").ok().flatten().is_some();
            let wanted = take(&self.wants.reconcile);
            let due = now - self.meta_i64("last_reconcile") >= self.timings.reconcile;
            if baseline_done && (wanted || due || self.reconcile_asked.is_some()) {
                // The service's ask alone copies again only what its read-back refused;
                // the schedule or an operator's also what isn't `copy_ok` yet, as before.
                let only_refused = !wanted && !due;
                // The outbox first: the record must include what was sent.
                self.send().await;
                match self.reconcile(now, only_refused).await {
                    Ok(d) => t.drift = d,
                    Err(e) => {
                        // An ask comes back with the next look at the annexes, not
                        // on every tick.
                        self.reconcile_asked = None;
                        warn!(error = %e, "annex reconciliation: later")
                    }
                }
            }
            if self.rules.is_ok() {
                match self.report_quiet(now) {
                    Ok(n) => t.reported = n,
                    Err(e) => warn!(error = %e, "annex quiet check failed"),
                }
                if baseline_done && now - self.meta_i64("last_age") >= self.timings.age {
                    match self.age_pass(now).await {
                        Ok(n) => t.would_remove += n,
                        Err(e) => warn!(error = %e, "annex age rules: later"),
                    }
                }
            }
        }
        t.sent = self.send().await;
        if !self.paused() {
            t.copied = self.copy_ready().await;
            if t.copied > 0 {
                t.sent += self.send().await;
            }
        }
        // Removals go on (or wait) whatever copying does: a paused annex
        // pauses them in the service, which the remover asks before each
        // write.
        let now = self.now();
        t.removals = self.remove_pass(now).await;
        t.placements = self.place_pass(now).await;
        t
    }

    /// Whether the placement mount is needed: a placement with work due. One
    /// only to report, or waiting (stuck, paused, backing off), holds no
    /// writable handle (gate 1).
    pub fn needs_pl(&self) -> bool {
        placer::Journal(&self.db).needs_mount(self.now())
    }

    /// Over SMB, which account this edge places files on this filer with, from
    /// winbind, when the unit starts and hourly: what `PATCH /v1/filers/{id}`
    /// checks a registration against. It is the principal the placement mount
    /// is made with, not the config's `placement_sid`, and it registers nothing.
    pub fn report_placement_account(&mut self, now: i64) {
        if self.flavour != Flavour::Smb {
            return;
        }
        if self
            .last_account_report
            .is_some_and(|at| now - at < ACCOUNT_REPORT_MS)
        {
            return;
        }
        self.last_account_report = Some(now);
        let details = match &self.place_as {
            Err(why) => json!({"error": why}),
            Ok(name) => match self.ids.name_to_sid(name) {
                Ok(Some((sid, kind))) => json!({"sid": sid.to_string(), "kind": kind_word(&kind),
                                                "name": name}),
                Ok(None) => json!({"error": format!("winbind doesn't know {name}")}),
                Err(e) => json!({"error": format!("{name}: {e}")}),
            },
        };
        self.push(
            "placement_account",
            None,
            json!({"kind": "placement_account", "details": details}),
        );
    }

    /// The placement and read accounts' SIDs, over SMB, where the lock names them.
    pub fn staging_accounts(
        &self,
    ) -> Result<(Option<String>, Option<String>), placer::AccountTrouble> {
        if self.flavour != Flavour::Smb {
            return Ok((None, None));
        }
        Ok((
            account_sid(&*self.ids, &self.placement_sid, &self.placement_name)?,
            account_sid(&*self.ids, &self.read_sid, &self.read_name)?,
        ))
    }

    /// Ask for placement tickets, at most every 30 seconds (A3.3).
    pub async fn ask_placements(&mut self, now: i64) -> usize {
        if now - self.last_place_ask < 30_000 {
            return 0;
        }
        self.last_place_ask = now;
        placer::take_tickets(&self.svc, &self.db, self.annex.annex_id, now).await
    }

    /// One placer pass (A3.3) while the placement mount is there.
    pub async fn place_pass(&mut self, now: i64) -> placer::Pass {
        // The journal couldn't take a retry's backoff: a minute before more.
        if now < self.place_wait_until {
            return placer::Pass::default();
        }
        let Some(pl) = self.pl_mountpoint.clone() else {
            if let Some(why) = self.pl_problem.take() {
                placer::no_mount_pass(&self.svc, &self.db, now, &why).await;
            }
            return placer::Pass::default();
        };
        if !self.still_mounted(&pl) {
            let why = "the placement mount is gone";
            error!(
                annex = self.annex.annex_id,
                why, "ALERT placements: nothing written; mounting again"
            );
            self.pl_mountpoint = None;
            placer::no_mount_pass(&self.svc, &self.db, now, why).await;
            return placer::Pass::default();
        }
        if let Err(why) = self.mounts_apart(&pl) {
            error!(
                annex = self.annex.annex_id,
                why, "ALERT placements: the mounts aren't apart; nothing written"
            );
            placer::no_mount_pass(&self.svc, &self.db, now, &why).await;
            return placer::Pass::default();
        }
        let (Ok(pl_root), Ok(ro_root)) = (open_root(&pl, ""), open_root(&self.root, "")) else {
            warn!(
                annex = self.annex.annex_id,
                "a mount is unreadable: no placements now"
            );
            let why = "a mount is unreadable";
            placer::no_mount_pass(&self.svc, &self.db, now, why).await;
            return placer::Pass::default();
        };
        if !self.opened_on_mount(&pl, &pl_root) {
            let why = "the placement mount went as it was opened";
            error!(
                annex = self.annex.annex_id,
                why, "ALERT placements: nothing written; mounting again"
            );
            self.pl_mountpoint = None;
            placer::no_mount_pass(&self.svc, &self.db, now, why).await;
            return placer::Pass::default();
        }
        // The placer's to say, so a placement sticks with an ALERT and still hears
        // of a cancel; nothing is written until they're known.
        let (accounts, accounts_problem) = match self.staging_accounts() {
            Ok(v) => (v, None),
            Err(why) => ((None, None), Some(why)),
        };
        let (placement_sid, read_sid) = accounts;
        let state_dir = self
            .journals
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_default();
        let p = placer::Placer {
            svc: &self.svc,
            db: &self.db,
            pl: &pl_root,
            ro: &ro_root,
            annex_id: self.annex.annex_id,
            annex_name: &self.annex.name,
            annex_rel: &self.root_rel,
            flavour: self.flavour,
            ids: &*self.ids,
            placement_sid,
            read_sid,
            read_who: self.read_who.clone(),
            accounts_problem,
            journal_failed: std::cell::Cell::new(false),
            now,
            state_dir: &state_dir,
            grace_ms: self.replace_grace_ms(),
            smb: self.flavour == Flavour::Smb,
        };
        let out = p.pass().await;
        if out.journal_failed {
            self.place_wait_until = now + 60_000;
        }
        out
    }

    /// The NAS's mount is up and safe to read from.
    pub fn mount_is_back(&self) {
        placer::mount_is_back(&self.db, self.now());
    }

    /// While the NAS can't be mounted at all: what needs no mount goes on, and
    /// a placement's bound runs (`annex_main`'s mount loop).
    pub async fn while_unmounted(&mut self, why: &str) {
        let now = self.now();
        placer::report_all(&self.svc, &self.db, now).await;
        self.report_placement_account(now);
        self.ask_placements(now).await;
        placer::no_mount_pass(&self.svc, &self.db, now, why).await;
        self.send().await;
    }
}

/// Whether `rule` removes a file at its copy (Move now), which no clock
/// decides: an `arrived` rule with `remove_from_source`. An age rule has the
/// same action, but its whole decision is the clock's.
fn moves_now(rules: &RuleSet, rule: &str) -> bool {
    rules
        .rule(rule)
        .is_some_and(|r| r.trigger == Trigger::Arrived && r.actions.remove_from_source)
}

/// Reconciliation's fresh walk: every regular file under the root, by
/// path, and the folders it couldn't read (with why).
#[derive(Debug, Default)]
pub struct Walk {
    pub files: BTreeMap<String, Seen>,
    pub unreadable: Vec<(String, String)>,
}

/// The service's last ask for a reconciliation that one has answered.
const RECONCILE_ANSWERED: &str = "reconcile_answered";

/// How often the placement account is reported (`report_placement_account`).
const ACCOUNT_REPORT_MS: i64 = 3_600_000;

/// The principal this annex would place files with, as the placement mount
/// takes it (`mount::placement_account`), or why it has none.
fn place_as(cfg: &AnnexConfig, annex: &EdgeAnnex) -> Result<String, String> {
    let (_, principal, _) = super::mount::placement_account(cfg, &annex.filer, &annex.mode)?;
    if !principal.is_empty() {
        return Ok(principal);
    }
    // No account of its own: the read account places too.
    super::mount::read_name(cfg, &annex.filer)
        .ok_or_else(|| "this filer's mount names no account".to_owned())
}

/// What the service is told an account is. Winbind calls an AD machine account a
/// user; Samba's type 9 is a computer.
fn kind_word(kind: &omnifile_identity::SidKind) -> String {
    use omnifile_identity::SidKind as K;
    match kind {
        K::User => "user".into(),
        K::Group => "group".into(),
        K::Domain => "domain".into(),
        K::Alias => "alias".into(),
        K::WellKnownGroup => "well_known".into(),
        K::Other(9) => "computer".into(),
        K::Other(n) => format!("other_{n}"),
    }
}

/// One of the staging ACL's two accounts: the config's SID, else looked up by
/// name. A lookup that fails or finds nothing is an error, never "no such
/// account": the folder would be locked without it, and recorded for good.
pub fn account_sid(
    ids: &dyn IdMap,
    given: &Option<String>,
    name: &Option<String>,
) -> Result<Option<String>, placer::AccountTrouble> {
    if given.is_some() {
        return Ok(given.clone());
    }
    let Some(n) = name.as_deref() else {
        return Ok(None);
    };
    match ids.name_to_sid(n) {
        Ok(Some((sid, _))) => Ok(Some(sid.to_string())),
        Ok(None) => Err(placer::AccountTrouble::Unknown(format!("{n} has no SID"))),
        Err(e) => Err(placer::AccountTrouble::Lookup(format!("{n}: {e}"))),
    }
}

/// Walk the whole annex beneath `root` (`nasfs`: no symlink followed, no
/// mount below crossed). An unreadable root is an error: the caller judges
/// nothing against it.
pub fn walk_all(fs: &Root, at_export_root: bool) -> Result<Walk, String> {
    let mut out = Walk::default();
    let mut stack = vec![String::new()];
    while let Some(dir) = stack.pop() {
        let items = match fs.read_dir(&dir) {
            Ok((_, items)) => items,
            Err(e) if dir.is_empty() => return Err(format!("the annex root: {e}")),
            // Gone since its parent was listed: nothing there.
            Err(e) if gone_error(&e) => continue,
            Err(e) => {
                out.unreadable.push((dir, e.to_string()));
                continue;
            }
        };
        for (name, st) in items {
            let Some(name) = name.to_str().map(str::to_owned) else {
                continue;
            };
            if super::scan::own_file(&name) {
                continue;
            }
            // As the scanner: the placers' staging folders, at the top only.
            if at_export_root && dir.is_empty() && placer::is_staging_dir(&name) {
                continue;
            }
            let rel = if dir.is_empty() {
                name
            } else {
                format!("{dir}/{name}")
            };
            match st {
                Ok(m) if m.kind == Kind::Dir => stack.push(rel),
                Ok(m) if m.kind == Kind::File => {
                    out.files.insert(rel, m.seen());
                }
                Ok(_) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                // Can't even stat it: whatever it is, it isn't judged.
                Err(e) => out.unreadable.push((rel, e.to_string())),
            }
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::super::service::{Fake, FakeState};
    use super::*;
    use omnifile_control::annex::{AnnexFiler, AnnexSettings};
    #[cfg(target_os = "linux")]
    use std::os::unix::fs::MetadataExt;
    use std::sync::atomic::AtomicI64;

    struct Clock(Arc<AtomicI64>);

    impl Clock {
        fn advance(&self, ms: i64) {
            self.0.fetch_add(ms, Ordering::SeqCst);
        }
    }

    fn annex() -> EdgeAnnex {
        EdgeAnnex {
            annex_id: 4,
            name: "lts".into(),
            mode: "copy".into(),
            state: "active".into(),
            path: "".into(),
            dataset_id: 9,
            filer: AnnexFiler {
                filer_id: 1,
                vendor: "generic".into(),
                host: "nas".into(),
                protocol: "nfs3".into(),
                export: "/x".into(),
                event_source: "scan".into(),
                credential_mode: "sec_sys".into(),
            },
            settings: AnnexSettings {
                quiet_minutes: 10,
                quick_scan_minutes: 60,
                full_scan_minutes: 1440,
                reconcile_days: 7,
            },
            ruleset: Some(copy_ruleset()),
            baseline_at: None,
            entity_version: 1,
            served_at: Some(rfc3339_ms(now_ms())),
            removals: None,
            reconcile_asked_at: None,
        }
    }

    /// Identity lookups that all fail, as with winbind away.
    struct NoWinbind;

    impl IdMap for NoWinbind {
        fn uid_to_sid(
            &self,
            _: u32,
        ) -> Result<Option<omnifile_identity::Sid>, omnifile_identity::IdMapError> {
            Err(omnifile_identity::IdMapError::Unavailable("away".into()))
        }
        fn gid_to_sid(
            &self,
            _: u32,
        ) -> Result<Option<omnifile_identity::Sid>, omnifile_identity::IdMapError> {
            Err(omnifile_identity::IdMapError::Unavailable("away".into()))
        }
        fn sid_to_uid(
            &self,
            _: &omnifile_identity::Sid,
        ) -> Result<Option<u32>, omnifile_identity::IdMapError> {
            Err(omnifile_identity::IdMapError::Unavailable("away".into()))
        }
        fn sid_to_gid(
            &self,
            _: &omnifile_identity::Sid,
        ) -> Result<Option<u32>, omnifile_identity::IdMapError> {
            Err(omnifile_identity::IdMapError::Unavailable("away".into()))
        }
        fn name_to_sid(
            &self,
            _: &str,
        ) -> Result<
            Option<(omnifile_identity::Sid, omnifile_identity::SidKind)>,
            omnifile_identity::IdMapError,
        > {
            Err(omnifile_identity::IdMapError::Unavailable("away".into()))
        }
        fn sid_to_name(
            &self,
            _: &omnifile_identity::Sid,
        ) -> Result<Option<String>, omnifile_identity::IdMapError> {
            Err(omnifile_identity::IdMapError::Unavailable("away".into()))
        }
        fn user_token_sids(
            &self,
            _: &omnifile_identity::Sid,
        ) -> Result<Option<Vec<omnifile_identity::Sid>>, omnifile_identity::IdMapError> {
            Err(omnifile_identity::IdMapError::Unavailable("away".into()))
        }
    }

    fn smb_runner(cfg: &AnnexConfig, mode: &str, ids: Box<dyn IdMap>, dir: &Path) -> Runner<Fake> {
        let mut a = annex();
        a.filer.protocol = "smb".into();
        a.filer.credential_mode = "service_keytab".into();
        a.mode = mode.into();
        Runner::new(
            Fake::default(),
            AnnexDb::open_in_memory().unwrap(),
            a,
            dir,
            dir,
            cfg,
            ids,
        )
    }

    fn account_events(r: &Runner<Fake>) -> Vec<Value> {
        r.db.outbox(100)
            .unwrap()
            .into_iter()
            .filter(|e| e.3["kind"] == "placement_account")
            .map(|e| e.3["details"].clone())
            .collect()
    }

    fn known() -> omnifile_identity::fake::FakeIdMap {
        let mut ids = omnifile_identity::fake::FakeIdMap::new("OMNI");
        ids.add_user("restorer", 2002, "S-1-5-21-1-2-3-1112")
            .add_user("reader", 2001, "S-1-5-21-1-2-3-1108")
            .add_group("geo", 3000, "S-1-5-21-1-2-3-2001");
        ids
    }

    #[test]
    fn the_edge_reports_the_account_it_holds_hourly_and_only_over_smb() {
        let d = tempfile::tempdir().unwrap();
        let cfg = AnnexConfig {
            service_principal: "reader@OMNI.TEST".into(),
            restore_principal: "restorer@OMNI.TEST".into(),
            restore_uid: 2002,
            // What it holds, not what the config claims.
            placement_sid: "S-1-5-21-1-2-3-9999".into(),
            ..Default::default()
        };
        let mut r = smb_runner(&cfg, "copy", Box::new(known()), d.path());
        r.report_placement_account(1_000);
        assert_eq!(
            account_events(&r),
            vec![json!({"sid": "S-1-5-21-1-2-3-1112", "kind": "user",
                        "name": "restorer@OMNI.TEST"})]
        );
        r.report_placement_account(1_000 + ACCOUNT_REPORT_MS - 1);
        assert_eq!(account_events(&r).len(), 1, "not before the hour");
        r.report_placement_account(1_000 + ACCOUNT_REPORT_MS);
        assert_eq!(account_events(&r).len(), 2);
        // No account of its own: the read account places too.
        let cfg = AnnexConfig {
            service_principal: "reader@OMNI.TEST".into(),
            ..Default::default()
        };
        let mut r = smb_runner(&cfg, "copy", Box::new(known()), d.path());
        r.report_placement_account(1_000);
        assert_eq!(account_events(&r)[0]["sid"], "S-1-5-21-1-2-3-1108");
        // NFS has no NT owner to swap, and reports nothing.
        let (mut r, _) = runner(d.path(), d.path());
        r.report_placement_account(1_000);
        assert!(account_events(&r).is_empty());
    }

    #[test]
    fn a_change_of_mode_changes_which_account_it_reports() {
        // A Copy annex never places with the remove-rights account; Archive may.
        let d = tempfile::tempdir().unwrap();
        let mut ids = known();
        ids.add_user("remover", 2003, "S-1-5-21-1-2-3-1113");
        let cfg = AnnexConfig {
            service_principal: "reader@OMNI.TEST".into(),
            remove_principal: "remover@OMNI.TEST".into(),
            remove_uid: 2003,
            ..Default::default()
        };
        let mut r = smb_runner(&cfg, "copy", Box::new(ids), d.path());
        r.report_placement_account(1_000);
        assert!(account_events(&r)[0].get("error").is_some());
        let mut a = r.annex.clone();
        a.mode = "archive_after".into();
        r.update(a, &cfg);
        r.report_placement_account(1_000 + ACCOUNT_REPORT_MS);
        assert_eq!(account_events(&r)[1]["sid"], "S-1-5-21-1-2-3-1113");
    }

    #[tokio::test]
    async fn the_tick_reports_it() {
        let d = tempfile::tempdir().unwrap();
        let cfg = AnnexConfig {
            service_principal: "reader@OMNI.TEST".into(),
            ..Default::default()
        };
        let mut r = smb_runner(&cfg, "copy", Box::new(known()), d.path());
        r.tick().await;
        let sent = r.svc.state.lock().unwrap().events.clone();
        let in_outbox = account_events(&r).len();
        let told = sent
            .iter()
            .filter(|e| e["kind"] == "placement_account")
            .count();
        assert_eq!(in_outbox + told, 1, "{sent:?}");
    }

    #[test]
    fn what_the_edge_cant_name_it_says_and_a_group_is_called_one() {
        let d = tempfile::tempdir().unwrap();
        let cfg = AnnexConfig {
            service_principal: "reader@OMNI.TEST".into(),
            restore_principal: "restorer@OMNI.TEST".into(),
            restore_uid: 2002,
            ..Default::default()
        };
        // Winbind away.
        let mut r = smb_runner(&cfg, "copy", Box::new(NoWinbind), d.path());
        r.report_placement_account(1_000);
        let e = &account_events(&r)[0];
        assert!(e.get("sid").is_none(), "{e}");
        assert!(e["error"].as_str().unwrap().contains("restorer"), "{e}");
        // Winbind answers: nobody of that name.
        let none = omnifile_identity::fake::FakeIdMap::new("OMNI");
        let mut r = smb_runner(&cfg, "copy", Box::new(none), d.path());
        r.report_placement_account(1_000);
        assert!(
            account_events(&r)[0]["error"]
                .as_str()
                .unwrap()
                .contains("doesn't know")
        );
        // A Copy annex with only the remove-rights account has no placement account.
        let cfg2 = AnnexConfig {
            service_principal: "reader@OMNI.TEST".into(),
            remove_principal: "remover@OMNI.TEST".into(),
            remove_uid: 2001,
            ..Default::default()
        };
        let mut r = smb_runner(&cfg2, "copy", Box::new(known()), d.path());
        r.report_placement_account(1_000);
        assert!(
            account_events(&r)[0]["error"]
                .as_str()
                .unwrap()
                .contains("restore_principal")
        );
        // What the principal is, is said as winbind says it.
        let cfg3 = AnnexConfig {
            service_principal: "reader@OMNI.TEST".into(),
            restore_principal: "geo@OMNI.TEST".into(),
            restore_uid: 2002,
            ..Default::default()
        };
        let mut r = smb_runner(&cfg3, "copy", Box::new(known()), d.path());
        r.report_placement_account(1_000);
        assert_eq!(account_events(&r)[0]["kind"], "group");
        assert_eq!(kind_word(&omnifile_identity::SidKind::Other(9)), "computer");
    }

    #[test]
    fn a_failed_account_lookup_is_never_read_as_no_such_account() {
        // F7: a failed read_sid lookup read as "no second account", and the
        // folder was locked without it, for good.
        let sid = Some("S-1-5-21-1-2-3-1105".to_owned());
        let name = Some("reader@OMNI.TEST".to_owned());
        assert!(matches!(
            account_sid(&NoWinbind, &None, &name),
            Err(placer::AccountTrouble::Lookup(_))
        ));
        let known = omnifile_identity::fake::FakeIdMap::new("OMNI");
        assert!(
            matches!(
                account_sid(&known, &None, &name),
                Err(placer::AccountTrouble::Unknown(_))
            ),
            "no such name"
        );
        // What the config gives needs no lookup; nothing named asks nothing.
        assert_eq!(account_sid(&NoWinbind, &sid, &name).unwrap(), sid);
        assert_eq!(account_sid(&NoWinbind, &None, &None).unwrap(), None);
    }

    #[test]
    fn the_reconciliation_walk_skips_staging_folders_only_at_the_export_root() {
        // F2: at an export-root annex, staged bytes reached Azure and were then
        // reported gone.
        let t = tempfile::tempdir().unwrap();
        let top = placer::staging_dir(4, "ab12");
        std::fs::create_dir_all(t.path().join(&top)).unwrap();
        std::fs::write(t.path().join(&top).join("x-1"), b"staged").unwrap();
        std::fs::create_dir_all(t.path().join("a").join(&top)).unwrap();
        std::fs::write(t.path().join("a").join(&top).join("mine"), b"a user's").unwrap();
        std::fs::write(t.path().join("two"), b"22").unwrap();
        let fs = open_root(t.path(), "").unwrap();
        let w = walk_all(&fs, true).unwrap();
        assert!(
            !w.files.keys().any(|k| k.starts_with(&top)),
            "{:?}",
            w.files.keys()
        );
        assert!(w.files.contains_key(&format!("a/{top}/mine")));
        assert!(w.files.contains_key("two"));
        // Below the export's root, no folder there is OmniFile's.
        let w = walk_all(&fs, false).unwrap();
        assert!(w.files.contains_key(&format!("{top}/x-1")));
    }

    /// A1's Copy template, as `GET annexes` sends it.
    fn copy_ruleset() -> Value {
        json!({"version": "1", "body": {"template": "copy", "rules": [
            {"trigger": "arrived", "conditions": {}, "actions": [{"copy_to": {"kind": "tier"}}]},
            {"trigger": "changed", "conditions": {}, "actions": [{"copy_to": {"kind": "tier"}}]},
            {"trigger": "deleted_at_source", "conditions": {}, "actions": [{"keep_copy": true}]},
        ]}})
    }

    fn runner(root: &Path, state: &Path) -> (Runner<Fake>, Clock) {
        let now = Arc::new(AtomicI64::new(crate::annex::now_ms() + 60_000));
        let mut r = Runner::new(
            Fake::default(),
            AnnexDb::open_in_memory().unwrap(),
            annex(),
            root,
            state,
            &AnnexConfig::default(),
            Box::new(omnifile_identity::fake::FakeIdMap::default()),
        );
        let n = now.clone();
        r.clock = Arc::new(move || n.load(Ordering::SeqCst));
        (r, Clock(now))
    }

    const Q: i64 = 10 * 60_000;

    #[tokio::test]
    async fn a_file_a_placement_may_be_making_is_said_again_after_its_done() {
        // S11: refused while a placement is between its `go` and its `done`,
        // it must not be dropped as `skipped`, and `done` goes first in a tick.
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        r.tick().await; // baseline of an empty directory
        std::fs::write(nas.path().join("placed.bin"), b"placed").unwrap();
        r.svc.state.lock().unwrap().reject_arrivals = Some("placement_in_progress".into());
        r.wants.full.store(true, Ordering::SeqCst);
        clock.advance(1000);
        r.tick().await;
        clock.advance(Q + 1);
        r.tick().await;
        let e = r.db.entry("placed.bin").unwrap().unwrap();
        assert_eq!(e.state, "new", "not skipped: {e:?}");
        // The placement's `done` is waiting too (the service was away). It goes
        // before the event that says the file again.
        let t = json!({"placement_id": "1", "file_id": "9", "size": 1, "crc64": "0000000000000000",
                       "folder": "", "names": ["placed.bin"]});
        let j = placer::Journal(&r.db);
        j.insert(&t, "x-1", 0).unwrap();
        j.set(
            1,
            "done",
            &[
                ("unreported", "done".to_owned().into()),
                ("outcome", "placed".to_owned().into()),
            ],
            0,
        )
        .unwrap();
        {
            let mut s = r.svc.state.lock().unwrap();
            s.reject_arrivals = None;
            s.events.clear();
        }
        clock.advance(Q + 1);
        r.tick().await;
        let s = r.svc.state.lock().unwrap();
        assert_eq!(s.events_at_done, vec![0], "`done` first");
        assert!(
            s.events
                .iter()
                .any(|e| e["kind"] == "arrived" && e["path"] == "placed.bin")
        );
    }

    #[tokio::test]
    async fn baseline_then_quiet_then_copy_then_change_then_delete() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(nas.path().join("surveys")).unwrap();
        std::fs::write(nas.path().join("surveys/a.segy"), segy::synthetic(4, 50, 1)).unwrap();
        std::fs::write(nas.path().join("notes.txt"), b"hello").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());

        let t = r.tick().await;
        assert_eq!(t.scanned, Some("baseline"));
        assert_eq!(
            (t.reported, t.copied),
            (0, 0),
            "nothing is copied before its quiet period"
        );
        // After a full scan, the folders' ACLs and free space (A3.1), and that every
        // folder above the files has been read (decision 63).
        assert_eq!(
            r.svc.kinds(),
            vec![
                "baseline",
                "scan",
                "folder_acl",
                "folder_acl",
                "folders_read",
                "target_space"
            ]
        );

        clock.advance(Q);
        let t = r.tick().await;
        assert_eq!((t.reported, t.copied), (2, 2));
        let k = r.svc.kinds();
        assert_eq!(k.iter().filter(|k| *k == "arrived").count(), 2);
        assert_eq!(k.iter().filter(|k| *k == "copied").count(), 2);
        let live = r.svc.live();
        assert!(live.iter().all(|f| f.copied.is_some()));
        let first = r
            .svc
            .state
            .lock()
            .unwrap()
            .events
            .iter()
            .find(|e| e["kind"] == "arrived")
            .cloned()
            .unwrap();
        assert_eq!(
            first["baseline"], true,
            "present at the start: not an arrival"
        );
        assert_eq!(first["acl"]["kind"], "posix");

        // Changed in place: a full scan sees it, it waits, then a new version.
        std::fs::write(nas.path().join("notes.txt"), b"hello, again").unwrap();
        r.wants.full.store(true, Ordering::SeqCst);
        clock.advance(1000);
        let t = r.tick().await;
        assert_eq!((t.scanned, t.copied), (Some("full"), 0));
        clock.advance(Q);
        let t = r.tick().await;
        assert_eq!(t.copied, 1);
        assert_eq!(r.svc.kinds().iter().filter(|k| *k == "changed").count(), 1);
        assert_eq!(r.svc.state.lock().unwrap().commits, 3);

        // Deleted on the NAS: a `left`, and the copy stays in the service.
        std::fs::remove_file(nas.path().join("notes.txt")).unwrap();
        r.wants.quick.store(true, Ordering::SeqCst);
        clock.advance(1000);
        r.tick().await;
        assert!(r.svc.kinds().contains(&"left".to_owned()));
        assert_eq!(r.svc.live().len(), 1);
    }

    #[tokio::test]
    async fn a_slow_writer_is_never_copied_mid_write() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        r.tick().await; // baseline of an empty directory
        let p = nas.path().join("line.segy");
        let whole = segy::synthetic(10, 100, 3);
        std::fs::write(&p, &whole[..5000]).unwrap();
        r.wants.quick.store(true, Ordering::SeqCst);
        clock.advance(1000);
        r.tick().await;
        for i in 0..3 {
            clock.advance(Q / 2);
            // Still growing: every look restarts the clock.
            std::fs::write(&p, &whole[..6000 + i * 1000]).unwrap();
            let t = r.tick().await;
            assert_eq!(t.copied, 0);
        }
        // Quiet, but not whole traces: still not copied.
        clock.advance(Q * 2);
        assert_eq!(r.tick().await.copied, 0);
        std::fs::write(&p, &whole).unwrap();
        r.wants.full.store(true, Ordering::SeqCst);
        clock.advance(1000);
        r.tick().await;
        clock.advance(Q);
        assert_eq!(r.tick().await.copied, 1);
        let blobs = r.svc.state.lock().unwrap().blobs.clone();
        assert!(
            blobs.values().any(|b| *b == whole),
            "the whole file, never a part"
        );
    }

    #[tokio::test]
    async fn reconciliation_finds_what_the_scans_missed() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(nas.path().join("d")).unwrap();
        std::fs::write(nas.path().join("d/changed"), b"one").unwrap();
        std::fs::write(nas.path().join("d/deleted"), b"two").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        r.tick().await;
        clock.advance(Q);
        assert_eq!(r.tick().await.copied, 2);
        // Missed by quick scans: a rewrite in place, a delete and an arrival
        // whose directory mtime was put back.
        let dir = nas.path().join("d");
        let m = std::fs::metadata(&dir).unwrap().modified().unwrap();
        std::fs::write(dir.join("changed"), b"one, rewritten").unwrap();
        std::fs::remove_file(dir.join("deleted")).unwrap();
        std::fs::write(dir.join("arrived"), b"three").unwrap();
        std::fs::File::open(&dir).unwrap().set_modified(m).unwrap();
        r.wants.quick.store(true, Ordering::SeqCst);
        clock.advance(1000);
        r.tick().await;
        assert!(
            !r.svc.kinds().contains(&"left".to_owned()),
            "the quick scan missed them"
        );
        r.wants.reconcile.store(true, Ordering::SeqCst);
        clock.advance(1000);
        let t = r.tick().await;
        assert_eq!(t.drift, 3);
        let drift: Vec<String> = r
            .svc
            .state
            .lock()
            .unwrap()
            .events
            .iter()
            .filter(|e| e["kind"] == "drift")
            .map(|e| {
                format!(
                    "{} {}",
                    e["details"]["drift"].as_str().unwrap(),
                    e["path"].as_str().unwrap()
                )
            })
            .collect();
        assert_eq!(
            drift,
            vec![
                "missed_arrival d/arrived",
                "changed_no_event d/changed",
                "missed_departure d/deleted"
            ]
        );
        clock.advance(Q);
        assert_eq!(
            r.tick().await.copied,
            2,
            "the missed arrival and change are copied"
        );
        assert_eq!(r.svc.live().len(), 2);
    }

    #[tokio::test]
    async fn reconciliation_reports_nothing_of_a_file_a_placement_hid_to_replace() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(nas.path().join("d")).unwrap();
        std::fs::write(nas.path().join("d/old"), b"one").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        r.tick().await;
        clock.advance(Q);
        assert_eq!(r.tick().await.copied, 1);
        // A3.4: hidden by a placement that replaces it.
        r.db.conn()
            .execute(
                "INSERT INTO placement_replace (placement_id, path, state, hidden_name, ino, \
                 size, mtime_ns, ctime_ns, crc64, updated_at) VALUES (1, 'd/old', 'hidden', \
                 '.omnifile-replacing-1-old', 1, 3, 1, 1, '00', 1)",
                [],
            )
            .unwrap();
        std::fs::rename(
            nas.path().join("d/old"),
            nas.path().join("d/.omnifile-replacing-1-old"),
        )
        .unwrap();
        let before = r.svc.state.lock().unwrap().events.len();
        r.wants.reconcile.store(true, Ordering::SeqCst);
        clock.advance(1000);
        r.tick().await;
        let evs: Vec<String> = r.svc.state.lock().unwrap().events[before..]
            .iter()
            .map(|e| format!("{} {}", e["kind"], e["path"]))
            .collect();
        assert!(
            evs.iter().any(|e| e.starts_with("\"scan\"")),
            "it ran: {evs:?}"
        );
        assert!(
            evs.iter()
                .all(|e| !e.contains("d/old") && !e.contains("replacing")),
            "{evs:?}"
        );
    }

    fn kinds_of(r: &Runner<Fake>, kind: &str) -> Vec<String> {
        r.svc
            .state
            .lock()
            .unwrap()
            .events
            .iter()
            .filter(|e| e["kind"] == kind)
            .map(|e| e["path"].as_str().unwrap_or("").to_owned())
            .collect()
    }

    fn is_root() -> bool {
        // SAFETY: no arguments.
        unsafe { libc::geteuid() == 0 }
    }

    #[tokio::test]
    async fn an_unreadable_folder_is_never_a_delete() {
        if is_root() {
            return; // root reads a mode-000 folder: nothing to test
        }
        use std::os::unix::fs::PermissionsExt;
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(nas.path().join("d/e")).unwrap();
        std::fs::write(nas.path().join("d/one"), b"1").unwrap();
        std::fs::write(nas.path().join("d/e/two"), b"2").unwrap();
        std::fs::write(nas.path().join("top"), b"3").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        r.tick().await;
        clock.advance(Q);
        assert_eq!(r.tick().await.copied, 3);
        let d = nas.path().join("d");
        std::fs::set_permissions(&d, std::fs::Permissions::from_mode(0o000)).unwrap();
        // Quick, full and reconciliation all meet the unreadable folder.
        let w = r.wants.clone();
        for w in [&w.quick, &w.full, &w.reconcile] {
            w.store(true, Ordering::SeqCst);
            clock.advance(1000);
            r.tick().await;
        }
        std::fs::set_permissions(&d, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert!(
            kinds_of(&r, "left").is_empty(),
            "{:?}",
            kinds_of(&r, "left")
        );
        let drift: Vec<_> = r
            .svc
            .state
            .lock()
            .unwrap()
            .events
            .iter()
            .filter(|e| e["kind"] == "drift")
            .map(|e| e["details"]["drift"].clone())
            .collect();
        assert!(drift.is_empty(), "{drift:?}");
        assert!(kinds_of(&r, "skipped").iter().any(|p| p == "d"));
        assert_eq!(r.svc.live().len(), 3);
    }

    #[tokio::test]
    async fn reconciliation_with_an_unreadable_root_judges_nothing() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("f"), b"1").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        r.tick().await;
        clock.advance(Q);
        assert_eq!(r.tick().await.copied, 1);
        r.root = nas.path().join("not-there");
        assert!(
            r.reconcile(clock.0.load(Ordering::SeqCst), false)
                .await
                .is_err()
        );
        assert!(kinds_of(&r, "left").is_empty());
    }

    #[tokio::test]
    async fn symlinks_on_the_nas_are_never_followed_or_copied() {
        let nas = tempfile::tempdir().unwrap();
        let out = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(out.path().join("secret"), b"outside").unwrap();
        std::fs::write(nas.path().join("real"), b"inside").unwrap();
        std::os::unix::fs::symlink(out.path(), nas.path().join("away")).unwrap();
        std::os::unix::fs::symlink(out.path().join("secret"), nas.path().join("link")).unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        r.tick().await;
        clock.advance(Q);
        assert_eq!(r.tick().await.copied, 1);
        r.wants.reconcile.store(true, Ordering::SeqCst);
        clock.advance(1000);
        assert_eq!(r.tick().await.drift, 0);
        let paths: Vec<_> = r.svc.live().iter().map(|f| f.path.clone()).collect();
        assert_eq!(paths, vec!["real"]);
        let blobs = r.svc.state.lock().unwrap().blobs.clone();
        assert!(blobs.values().all(|b| b == b"inside"));
    }

    fn with_rules(r: &mut Runner<Fake>, version: u64, rules: Value) {
        let mut a = r.annex.clone();
        a.ruleset = Some(json!({"version": version.to_string(), "body": {"rules": rules}}));
        a.served_at = Some(rfc3339_ms(r.now()));
        r.update(a, &AnnexConfig::default());
    }

    /// `GET annexes` answered again now: the service's clock agrees.
    fn served_now(r: &mut Runner<Fake>) {
        let mut a = r.annex.clone();
        a.served_at = Some(rfc3339_ms(r.now()));
        r.update(a, &AnnexConfig::default());
    }

    fn archive_rules(days: u64) -> Value {
        json!([
            {"id": "scratch", "trigger": "arrived", "conditions": {"path": "**/scratch/*"},
             "actions": [{"exclude": true}]},
            {"id": "copy", "trigger": "arrived", "conditions": {}, "actions": [{"copy_to": {}}]},
            {"id": "recopy", "trigger": "changed", "conditions": {}, "actions": [{"copy_to": {}}]},
            {"id": "old", "trigger": "age", "conditions": {"age_since_arrival_days_gt": days},
             "actions": [{"remove_from_source": true}]},
            {"id": "keep", "trigger": "age", "conditions": {}, "actions": [{"exclude": true}]},
        ])
    }

    #[tokio::test]
    async fn rules_decide_what_is_copied_and_removals_are_only_announced() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(nas.path().join("s/scratch")).unwrap();
        std::fs::write(nas.path().join("s/scratch/tmp.bin"), b"scratch").unwrap();
        std::fs::write(nas.path().join("s/line.bin"), b"keep me").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        with_rules(&mut r, 2, archive_rules(30));
        r.tick().await;
        clock.advance(Q);
        assert_eq!(r.tick().await.copied, 1, "the scratch file is excluded");
        let paths: Vec<_> = r.svc.live().iter().map(|f| f.path.clone()).collect();
        assert_eq!(paths, vec!["s/line.bin"]);
        assert_eq!(
            r.db.entry("s/scratch/tmp.bin").unwrap().unwrap().state,
            "excluded"
        );
        let arrived = r
            .svc
            .state
            .lock()
            .unwrap()
            .events
            .iter()
            .find(|e| e["kind"] == "arrived")
            .cloned()
            .unwrap();
        assert_eq!(arrived["rule"], "copy");
        // 31 days on, the age rule chooses it: announced once, never removed.
        clock.advance(31 * 24 * 3600 * 1000);
        served_now(&mut r);
        let t = r.tick().await;
        assert_eq!(t.would_remove, 1);
        r.wants.full.store(true, Ordering::SeqCst);
        clock.advance(25 * 3600 * 1000);
        served_now(&mut r);
        assert_eq!(r.tick().await.would_remove, 0, "once per copied version");
        assert!(
            nas.path().join("s/line.bin").exists(),
            "A2.1 removes nothing"
        );
        assert_eq!(kinds_of(&r, "would_remove"), vec!["s/line.bin"]);
    }

    #[tokio::test]
    async fn move_now_announces_the_removal_after_the_copy() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("drop.bin"), b"leave").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        with_rules(
            &mut r,
            2,
            json!([{"id": "move", "trigger": "arrived", "conditions": {},
                    "actions": [{"copy_to": {}}, {"remove_from_source": true}]}]),
        );
        r.tick().await;
        clock.advance(Q);
        assert_eq!(r.tick().await.copied, 1);
        r.send().await;
        assert_eq!(kinds_of(&r, "would_remove"), vec!["drop.bin"]);
        assert!(nas.path().join("drop.bin").exists());
    }

    #[tokio::test]
    async fn a_rule_set_the_edge_cant_read_stops_copying() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("f"), b"x").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        with_rules(
            &mut r,
            2,
            json!([{"trigger": "recall_requested", "conditions": {}, "actions": []}]),
        );
        r.tick().await;
        clock.advance(Q);
        let t = r.tick().await;
        assert_eq!((t.reported, t.copied), (0, 0));
        let skipped = r
            .svc
            .state
            .lock()
            .unwrap()
            .events
            .iter()
            .filter(|e| e["kind"] == "skipped" && e["reason"] == "rules_unreadable")
            .count();
        assert_eq!(skipped, 1, "said once");
    }

    #[tokio::test]
    async fn a_new_rule_set_looks_again_at_excluded_files() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("a.tmp"), b"x").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        with_rules(
            &mut r,
            2,
            json!([
                {"trigger": "arrived", "conditions": {"extension": ["tmp"]}, "actions": [{"exclude": true}]},
                {"trigger": "arrived", "conditions": {}, "actions": [{"copy_to": {}}]},
            ]),
        );
        r.tick().await;
        clock.advance(Q);
        assert_eq!(r.tick().await.copied, 0);
        with_rules(
            &mut r,
            3,
            json!([{"trigger": "arrived", "conditions": {}, "actions": [{"copy_to": {}}]}]),
        );
        clock.advance(1000);
        assert_eq!(r.tick().await.copied, 1);
    }

    #[tokio::test]
    async fn a_dry_run_reports_without_touching_anything() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("a.segy"), vec![0u8; 100]).unwrap();
        std::fs::write(nas.path().join("b.txt"), vec![0u8; 10]).unwrap();
        let (mut r, _clock) = runner(nas.path(), st.path());
        r.tick().await; // baseline listing
        let before: Vec<_> = r.db.entries_after("", 10).unwrap();
        r.svc.state.lock().unwrap().dry_runs = vec![
            omnifile_control::annex::AnnexDryRun {
                run_id: 9,
                annex_id: 4,
                body: json!({"rules": [
                    {"id": "segy", "trigger": "arrived", "conditions": {"extension": ["segy"]},
                     "actions": [{"copy_to": {}}, {"remove_after_days": 30}]},
                    {"id": "rest", "trigger": "arrived", "conditions": {}, "actions": [{"exclude": true}]},
                ]}),
            },
            omnifile_control::annex::AnnexDryRun {
                run_id: 10,
                annex_id: 4,
                body: json!({"rules": [{"trigger": "teleport"}]}),
            },
        ];
        assert_eq!(r.dry_runs().await, 2);
        assert_eq!(r.svc.state.lock().unwrap().dry_runs_taken, vec![9, 10]);
        let reports = r.svc.state.lock().unwrap().dry_run_reports.clone();
        let (id, rep) = &reports[0];
        assert_eq!(*id, 9);
        assert_eq!(rep["state"], "done");
        assert_eq!(rep["report"]["copied"]["files"], 1);
        assert_eq!(rep["report"]["removals"]["files"], 1);
        assert_eq!(reports[1].1["state"], "failed");
        // Read-only: the listing and the NAS are as they were, nothing queued.
        assert_eq!(r.db.entries_after("", 10).unwrap(), before);
        assert!(r.svc.live().is_empty());
    }

    #[tokio::test]
    async fn a_dry_run_replaced_by_a_newer_draft_is_dropped_and_a_slow_one_fails() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("a.segy"), vec![0u8; 100]).unwrap();
        let (mut r, _clock) = runner(nas.path(), st.path());
        r.tick().await;
        let run = |id| omnifile_control::annex::AnnexDryRun {
            run_id: id,
            annex_id: 4,
            body: json!({"rules": [{"trigger": "arrived", "conditions": {}, "actions": [{"copy_to": {}}]}]}),
        };
        {
            let mut s = r.svc.state.lock().unwrap();
            s.dry_runs = vec![run(11), run(12)];
            s.dry_runs_superseded = vec![11];
        }
        assert_eq!(r.dry_runs().await, 1);
        let reports = r.svc.state.lock().unwrap().dry_run_reports.clone();
        assert_eq!(reports.len(), 1, "the replaced run is never evaluated");
        assert_eq!(reports[0].0, 12);
        // Past its budget, a preview fails instead of holding the edge.
        let rs = rules::parse(1, &run(1).body).unwrap();
        let db = r.db.reopen().unwrap();
        let past = std::time::Instant::now();
        let ids = omnifile_identity::fake::FakeIdMap::default();
        let err =
            preview::dry_run(&db, &rs, None, Flavour::Nfs3, &ids, r.now(), Some(past)).unwrap_err();
        assert!(err.contains("ran out of time"), "{err}");
    }

    #[tokio::test]
    async fn age_removals_wait_while_the_edge_clock_is_far_from_the_services() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("line.bin"), b"keep me").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        with_rules(&mut r, 2, archive_rules(30));
        r.tick().await;
        clock.advance(Q);
        assert_eq!(r.tick().await.copied, 1);
        // The edge's clock jumps 31 days ahead; the service's hasn't moved.
        clock.advance(31 * 24 * 3600 * 1000);
        assert!(r.clock_ok().is_err());
        assert_eq!(
            r.tick().await.would_remove,
            0,
            "no age judged by a wrong clock"
        );
        assert!(kinds_of(&r, "would_remove").is_empty());
        // The service agrees again: judged.
        served_now(&mut r);
        r.db.set_meta("last_age", "0").unwrap();
        assert_eq!(r.tick().await.would_remove, 1);
    }

    #[tokio::test]
    async fn removals_are_decided_under_the_running_rule_set_not_the_arrivals() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("a.dat"), b"seismic").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        // v2: arrived.1 copies SEG-Y and keeps it.
        with_rules(
            &mut r,
            2,
            json!([{"trigger": "arrived", "conditions": {"extension": ["dat"]},
                    "actions": [{"copy_to": {}}]},
                   {"trigger": "arrived", "conditions": {}, "actions": [{"exclude": true}]}]),
        );
        r.tick().await;
        clock.advance(Q);
        assert_eq!(r.tick().await.copied, 1);
        assert_eq!(
            r.db.facts("a.dat").unwrap().rule_id.as_deref(),
            Some("arrived.1")
        );
        // v3: arrived.1 is now another rule, one that removes after a day;
        // the SEG-Y file is decided by arrived.2, which keeps it. The
        // preview says nothing leaves, so nothing may.
        with_rules(
            &mut r,
            3,
            json!([{"trigger": "arrived", "conditions": {"path": "other/**"},
                    "actions": [{"copy_to": {}}, {"remove_after_days": 1}]},
                   {"trigger": "arrived", "conditions": {}, "actions": [{"copy_to": {}}]}]),
        );
        clock.advance(2 * 24 * 3600 * 1000);
        served_now(&mut r);
        let rs = r.rules.clone().unwrap();
        let db = r.db.reopen().unwrap();
        let ids = omnifile_identity::fake::FakeIdMap::default();
        let preview = preview::dry_run(&db, &rs, None, Flavour::Nfs3, &ids, r.now(), None).unwrap();
        assert_eq!(preview["removals"]["files"], 0);
        assert_eq!(r.age_pass(r.now()).await.unwrap(), 0);
        assert!(kinds_of(&r, "would_remove").is_empty());
    }

    #[tokio::test]
    async fn a_change_never_overwrites_the_arrival_rule() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("line.bin"), b"one").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        with_rules(&mut r, 2, archive_rules(30));
        r.tick().await;
        clock.advance(Q);
        assert_eq!(r.tick().await.copied, 1);
        std::fs::write(nas.path().join("line.bin"), b"two, longer").unwrap();
        r.wants.full.store(true, Ordering::SeqCst);
        clock.advance(1000);
        r.tick().await;
        clock.advance(Q);
        r.tick().await;
        assert_eq!(
            r.db.facts("line.bin").unwrap().rule_id.as_deref(),
            Some("copy"),
            "the changed rule (recopy) is not the arrival's"
        );
    }

    #[tokio::test]
    async fn an_age_removal_at_the_copy_still_waits_for_the_clock() {
        // The re-review's probe: first seen, then the edge clock jumps 31
        // days with no fresh `served_at`. The copy that follows must not
        // announce the age rule's removal.
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("line.bin"), b"keep me").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        with_rules(&mut r, 2, archive_rules(30));
        r.tick().await;
        clock.advance(31 * 24 * 3600 * 1000);
        assert!(r.clock_ok().is_err());
        assert_eq!(r.tick().await.copied, 1);
        r.send().await;
        assert!(
            kinds_of(&r, "would_remove").is_empty(),
            "an age rule's removal is the clock's to decide"
        );
        // Move now still needs no clock: removal follows the copy.
        let nas2 = tempfile::tempdir().unwrap();
        let st2 = tempfile::tempdir().unwrap();
        std::fs::write(nas2.path().join("drop.bin"), b"leave").unwrap();
        let (mut m, clock2) = runner(nas2.path(), st2.path());
        with_rules(
            &mut m,
            2,
            json!([{"id": "move", "trigger": "arrived", "conditions": {},
                    "actions": [{"copy_to": {}}, {"remove_from_source": true}]}]),
        );
        m.tick().await;
        clock2.advance(31 * 24 * 3600 * 1000);
        assert!(m.clock_ok().is_err());
        assert_eq!(m.tick().await.copied, 1);
        m.send().await;
        assert_eq!(kinds_of(&m, "would_remove"), vec!["drop.bin"]);
    }

    // --- A2.2: the remover (W1-W4 and their gates; Linux: the writes) -------------

    fn move_now(r: &mut Runner<Fake>) {
        with_rules(
            r,
            3,
            json!([{"id": "move", "trigger": "arrived", "conditions": {},
                    "actions": [{"copy_to": {}}, {"remove_from_source": true}]}]),
        );
    }

    /// Removals live, the NAS directory as the read-write mount, a grace of
    /// `grace_ms`.
    #[cfg(target_os = "linux")]
    fn go_live(r: &mut Runner<Fake>, nas: &Path, grace_ms: i64, pointers: bool) {
        let mut a = r.annex.clone();
        a.removals = Some(omnifile_control::annex::AnnexRemovals {
            live: true,
            why_not: None,
            dry_run: false,
            grace_minutes: 15,
            pointer_files: pointers,
            rate_per_hour: 1000,
        });
        a.served_at = Some(rfc3339_ms(r.now()));
        r.update(a, &AnnexConfig::default());
        r.rw_mountpoint = Some(nas.to_owned());
        r.grace_override_ms = grace_ms;
        // One directory stands for both mounts here.
        r.check_mounts = false;
    }

    fn steps(r: &Runner<Fake>) -> Vec<String> {
        r.svc
            .state
            .lock()
            .unwrap()
            .removal_steps
            .iter()
            .map(|(_, b)| b["step"].as_str().unwrap_or("").to_owned())
            .collect()
    }

    #[cfg(target_os = "linux")]
    fn journal(r: &Runner<Fake>, path: &str) -> crate::annex::db::Removal {
        let id: i64 =
            r.db.conn()
                .query_row(
                    "SELECT removal_id FROM removal WHERE path = ?1 ORDER BY removal_id DESC",
                    [path],
                    |x| x.get(0),
                )
                .unwrap();
        r.db.removal(id as u64).unwrap().unwrap()
    }

    /// A file copied under Move now with removals live: ticketed and hidden
    /// (W1) in the copy's tick.
    #[cfg(target_os = "linux")]
    async fn hidden_file(
        name: &str,
    ) -> (tempfile::TempDir, tempfile::TempDir, Runner<Fake>, Clock) {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join(name), b"leave me").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        move_now(&mut r);
        go_live(&mut r, nas.path(), 60_000, true);
        r.tick().await;
        clock.advance(Q);
        let t = r.tick().await;
        assert_eq!(t.copied, 1);
        (nas, st, r, clock)
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_removal_hides_waits_its_grace_deletes_and_leaves_a_pointer() {
        let (nas, _st, mut r, clock) = hidden_file("drop.bin").await;
        let j = journal(&r, "drop.bin");
        assert_eq!(j.state, "hidden");
        assert!(!nas.path().join("drop.bin").exists());
        assert!(
            nas.path().join(&j.hidden_name).exists(),
            "W1: hidden in its folder"
        );
        // Within the grace: nothing more.
        r.tick().await;
        assert!(nas.path().join(&j.hidden_name).exists());
        clock.advance(61_000);
        r.tick().await;
        assert!(!nas.path().join(&j.hidden_name).exists(), "W2");
        let ptr = std::fs::read_to_string(nas.path().join("drop.bin.omnifile.txt"))
            .unwrap_or_else(|e| panic!("{e}: {:?} {:?}", journal(&r, "drop.bin"), steps(&r)));
        assert!(ptr.contains("moved to Azure"), "W4");
        assert_eq!(journal(&r, "drop.bin").state, "done");
        assert_eq!(
            steps(&r),
            vec!["hide", "hidden", "delete", "deleted", "pointer", "done"]
        );
        // Never removed twice, and the scans don't call it a user's delete.
        r.wants.full.store(true, Ordering::SeqCst);
        clock.advance(61_000);
        r.tick().await;
        assert_eq!(steps(&r).len(), 6);
        r.send().await;
        assert!(kinds_of(&r, "left").is_empty());
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn w1_is_blocked_by_a_change_a_hard_link_or_the_service() {
        // A change between the ticket and the hide.
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        for f in ["changed.bin", "linked.bin", "refused.bin"] {
            std::fs::write(nas.path().join(f), f.as_bytes()).unwrap();
        }
        let (mut r, clock) = runner(nas.path(), st.path());
        move_now(&mut r);
        r.tick().await;
        clock.advance(Q);
        assert_eq!(r.tick().await.copied, 3);
        // Not live yet: candidates are would_remove events, no write at all.
        assert!(nas.path().join("changed.bin").exists());
        go_live(&mut r, nas.path(), 60_000, true);
        // The copies decided before going live re-decide now (age pass).
        r.db.set_meta("last_age", "0").unwrap();
        std::fs::write(nas.path().join("changed.bin"), b"changed since its copy").unwrap();
        std::fs::hard_link(nas.path().join("linked.bin"), nas.path().join("other-name")).unwrap();
        r.svc
            .state
            .lock()
            .unwrap()
            .refuse_step
            .insert("hide".into(), "legal_hold".into());
        for (f, _) in [("changed.bin", 0), ("linked.bin", 0), ("refused.bin", 0)] {
            let e = r.db.entry(f).unwrap().unwrap();
            r.db.add_removal_candidate(f, e.file_id.unwrap(), e.copied_version.unwrap(), "move", 3)
                .unwrap();
        }
        let now = r.now();
        r.remove_pass(now).await;
        assert_eq!(
            journal(&r, "changed.bin").reason.as_deref(),
            Some("changed")
        );
        assert_eq!(
            journal(&r, "linked.bin").reason.as_deref(),
            Some("hard_linked")
        );
        assert_eq!(
            journal(&r, "refused.bin").reason.as_deref(),
            Some("legal_hold")
        );
        for f in ["changed.bin", "linked.bin", "refused.bin"] {
            assert!(nas.path().join(f).exists(), "{f}: never hidden");
            assert_eq!(journal(&r, f).state, "cancelled");
        }
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn w2_is_blocked_by_a_change_in_the_grace_a_rename_back_or_the_service() {
        // A change in the grace: W3 brings it back.
        let (nas, _st, mut r, clock) = hidden_file("a.bin").await;
        let j = journal(&r, "a.bin");
        std::fs::write(nas.path().join(&j.hidden_name), b"written in its grace").unwrap();
        clock.advance(61_000);
        r.tick().await;
        let j = journal(&r, "a.bin");
        assert_eq!(
            (j.state.as_str(), j.reason.as_deref()),
            ("cancelled", Some("changed_in_grace"))
        );
        assert!(nas.path().join("a.bin").exists(), "W3: back under its name");

        // A user renames it back in its grace: cancelled, nothing written.
        let (nas, _st, mut r, clock) = hidden_file("b.bin").await;
        let j = journal(&r, "b.bin");
        std::fs::rename(nas.path().join(&j.hidden_name), nas.path().join("b.bin")).unwrap();
        clock.advance(61_000);
        r.tick().await;
        assert_eq!(journal(&r, "b.bin").reason.as_deref(), Some("renamed_back"));
        assert!(nas.path().join("b.bin").exists());

        // Paused: it stays hidden, and its grace starts again.
        let (nas, _st, mut r, clock) = hidden_file("c.bin").await;
        let j = journal(&r, "c.bin");
        r.svc
            .state
            .lock()
            .unwrap()
            .refuse_step
            .insert("delete".into(), "paused".into());
        clock.advance(61_000);
        r.tick().await;
        assert!(
            nas.path().join(&j.hidden_name).exists(),
            "no W2 while paused"
        );
        assert_eq!(journal(&r, "c.bin").state, "hidden");
        r.svc.state.lock().unwrap().refuse_step.clear();
        r.tick().await;
        assert!(
            nas.path().join(&j.hidden_name).exists(),
            "a new grace after the pause"
        );
        clock.advance(61_000);
        r.tick().await;
        assert!(!nas.path().join(&j.hidden_name).exists());

        // Refused for good (a hold set since): W3.
        let (nas, _st, mut r, clock) = hidden_file("d.bin").await;
        r.svc
            .state
            .lock()
            .unwrap()
            .refuse_step
            .insert("delete".into(), "legal_hold".into());
        clock.advance(61_000);
        r.tick().await;
        assert!(nas.path().join("d.bin").exists());
        assert_eq!(journal(&r, "d.bin").state, "cancelled");
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn w3_never_takes_a_name_back_from_someone_else() {
        let (nas, _st, mut r, clock) = hidden_file("e.bin").await;
        let j = journal(&r, "e.bin");
        std::fs::write(nas.path().join(&j.hidden_name), b"changed in its grace").unwrap();
        std::fs::write(nas.path().join("e.bin"), b"a new file with the old name").unwrap();
        clock.advance(61_000);
        r.tick().await;
        // Nothing overwritten, nothing deleted: it stays hidden, raised.
        assert_eq!(
            std::fs::read(nas.path().join("e.bin")).unwrap(),
            b"a new file with the old name"
        );
        assert!(nas.path().join(&j.hidden_name).exists());
        let j = journal(&r, "e.bin");
        assert_eq!(
            j.state, "stuck",
            "raised to the service: an owner problem and an alert"
        );
        assert!(j.reason.unwrap().contains("the name is taken"));
        assert!(steps(&r).contains(&"stuck".to_owned()));
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn w4_never_overwrites_and_only_when_the_annex_leaves_pointers() {
        let (nas, _st, mut r, clock) = hidden_file("f.bin").await;
        std::fs::write(nas.path().join("f.bin.omnifile.txt"), b"a user's own note").unwrap();
        clock.advance(61_000);
        r.tick().await;
        assert_eq!(
            std::fs::read(nas.path().join("f.bin.omnifile.txt")).unwrap(),
            b"a user's own note"
        );
        assert_eq!(journal(&r, "f.bin").pointer, Some(false));
        // An annex without pointers writes none.
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("g.bin"), b"no pointer").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        move_now(&mut r);
        go_live(&mut r, nas.path(), 1, false);
        r.tick().await;
        clock.advance(Q);
        r.tick().await;
        clock.advance(1000);
        r.tick().await;
        assert_eq!(journal(&r, "g.bin").state, "done");
        assert!(!nas.path().join("g.bin.omnifile.txt").exists());
        assert_eq!(std::fs::read_dir(nas.path()).unwrap().count(), 0);
    }

    /// #98's review, E2: a save-by-rename just before W1 means W1 hid the user's
    /// new file. Found under the hidden name after a restart, it is never taken as
    /// "nothing hidden": it goes back under its own name, or, that taken, under a
    /// visible `-replaced-` name the service is told of.
    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_users_file_w1_hid_goes_back_and_is_never_left_hidden() {
        for taken in [false, true] {
            let (nas, _st, mut r, _clock) = hidden_file("s.bin").await;
            let j = journal(&r, "s.bin");
            // The user's new file under the hidden name (another inode).
            let tmp = nas.path().join("s.bin.tmp");
            std::fs::write(&tmp, b"the user's newer save").unwrap();
            std::fs::rename(&tmp, nas.path().join(&j.hidden_name)).unwrap();
            if taken {
                std::fs::write(nas.path().join("s.bin"), b"made at the name since").unwrap();
            }
            r.db.set_removal(j.removal_id, "hiding", None, None, 0)
                .unwrap();
            r.tick().await;
            let after = journal(&r, "s.bin");
            assert_eq!(after.state, "cancelled", "{taken}: {after:?}");
            assert_eq!(after.reason.as_deref(), Some("swapped_at_hide"));
            assert!(
                !nas.path().join(&j.hidden_name).exists(),
                "{taken}: never left hidden"
            );
            let names: Vec<String> = std::fs::read_dir(nas.path())
                .unwrap()
                .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
                .collect();
            let kept = if taken {
                let k = names
                    .iter()
                    .find(|n| n.starts_with("s-replaced-") && n.ends_with("Z.bin"))
                    .unwrap_or_else(|| panic!("kept aside: {names:?}"))
                    .clone();
                assert_eq!(
                    std::fs::read(nas.path().join("s.bin")).unwrap(),
                    b"made at the name since"
                );
                k
            } else {
                "s.bin".to_owned()
            };
            assert_eq!(
                std::fs::read(nas.path().join(&kept)).unwrap(),
                b"the user's newer save",
                "{taken}"
            );
            let last = r
                .svc
                .state
                .lock()
                .unwrap()
                .removal_steps
                .last()
                .unwrap()
                .1
                .clone();
            assert_eq!(last["step"], "cancelled");
            assert_eq!(
                last["kept_as"],
                if taken { json!(kept) } else { Value::Null },
                "{taken}"
            );
        }
    }

    /// The confirm pass of #98, C3: that the hidden file is a user's is journalled
    /// apart from the reason, which a give-up or a drain rewrites: a later pass still
    /// keeps it aside, never leaves it hidden as `stuck`.
    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_users_file_stays_a_users_file_whatever_the_reason_says() {
        let (nas, _st, mut r, clock) = hidden_file("u.bin").await;
        let j = journal(&r, "u.bin");
        let tmp = nas.path().join("u.bin.tmp");
        std::fs::write(&tmp, b"the user's newer save").unwrap();
        std::fs::rename(&tmp, nas.path().join(&j.hidden_name)).unwrap();
        std::fs::write(nas.path().join("u.bin"), b"made at the name since").unwrap();
        r.db.set_removal(j.removal_id, "hiding", None, None, 0)
            .unwrap();
        // Its first W3 fails, so nothing is kept aside yet.
        remover::faults::W3_EIO.with(|f| f.set(1));
        r.tick().await;
        assert!(nas.path().join(&j.hidden_name).exists());
        // A give-up rewrites the reason.
        r.db.conn()
            .execute(
                "UPDATE removal SET reason = 'gave_up: test', state = 'stuck', next_at = 0 \
                 WHERE removal_id = ?1",
                [j.removal_id as i64],
            )
            .unwrap();
        clock.advance(61_000);
        r.tick().await;
        assert!(
            !nas.path().join(&j.hidden_name).exists(),
            "never left hidden"
        );
        let kept = std::fs::read_dir(nas.path())
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .find(|n| n.starts_with("u-replaced-"))
            .expect("kept aside");
        assert_eq!(
            std::fs::read(nas.path().join(kept)).unwrap(),
            b"the user's newer save"
        );
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_restart_at_any_step_resumes_and_never_deletes_twice() {
        // After W1, before its journal said so: found hidden by inode.
        let (nas, _st, mut r, clock) = hidden_file("h.bin").await;
        let j = journal(&r, "h.bin");
        r.db.set_removal(j.removal_id, "hiding", None, None, 0)
            .unwrap();
        r.tick().await;
        assert_eq!(journal(&r, "h.bin").state, "hidden");
        // Before W1 (journal `hiding`, still under its name): W1 never ran,
        // so the ticket goes back to be asked again, every gate with it.
        let (nas2, _st2, mut r2, _c2) = hidden_file("i.bin").await;
        let j2 = journal(&r2, "i.bin");
        std::fs::rename(nas2.path().join(&j2.hidden_name), nas2.path().join("i.bin")).unwrap();
        r2.db
            .set_removal(j2.removal_id, "hiding", None, None, 0)
            .unwrap();
        r2.tick().await;
        let after = journal(&r2, "i.bin");
        assert!(
            after.state == "ticketed" || after.state == "cancelled",
            "{after:?}"
        );
        assert!(
            nas2.path().join("i.bin").exists(),
            "not hidden without its gates"
        );
        // After W2, before its journal: not deleted twice, reported once.
        clock.advance(61_000);
        let before = steps(&r).iter().filter(|s| *s == "deleted").count();
        std::fs::remove_file(nas.path().join(&j.hidden_name)).unwrap();
        r.db.set_removal(j.removal_id, "deleting", None, None, 0)
            .unwrap();
        r.tick().await;
        let j = journal(&r, "h.bin");
        assert_eq!(j.state, "done");
        assert_eq!(
            steps(&r).iter().filter(|s| *s == "deleted").count(),
            before + 1
        );
        // Before W2 (journal `deleting`, still hidden): its checks run again.
        let (nas3, _st3, mut r3, clock3) = hidden_file("k.bin").await;
        let j3 = journal(&r3, "k.bin");
        std::fs::write(nas3.path().join(&j3.hidden_name), b"changed").unwrap();
        r3.db
            .set_removal(j3.removal_id, "deleting", None, None, 0)
            .unwrap();
        clock3.advance(61_000);
        r3.tick().await; // not deleted: back to hidden, through every gate again
        r3.tick().await;
        assert!(
            nas3.path().join("k.bin").exists(),
            "changed: restored, not deleted"
        );
        // A step the service didn't hear is sent again.
        let (_nas4, _st4, mut r4, _c4) = hidden_file("l.bin").await;
        r4.svc.state.lock().unwrap().down = true;
        let j4 = journal(&r4, "l.bin");
        r4.db
            .set_removal(j4.removal_id, "hidden", None, Some("hidden"), 0)
            .unwrap();
        r4.tick().await;
        assert_eq!(journal(&r4, "l.bin").unreported.as_deref(), Some("hidden"));
        r4.svc.state.lock().unwrap().down = false;
        r4.tick().await;
        assert_eq!(journal(&r4, "l.bin").unreported, None);
    }

    #[tokio::test]
    async fn in_the_dry_run_nothing_is_written_and_the_edge_checks_are_reported() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("m.bin"), b"stay").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        move_now(&mut r);
        let mut a = r.annex.clone();
        a.removals = Some(omnifile_control::annex::AnnexRemovals {
            live: false,
            why_not: Some("dry_run".into()),
            dry_run: true,
            ..Default::default()
        });
        r.update(a, &AnnexConfig::default());
        // Even with a writable directory given, no write without live removals.
        r.rw_mountpoint = Some(nas.path().to_owned());
        r.tick().await;
        clock.advance(Q);
        r.tick().await;
        r.send().await;
        assert!(nas.path().join("m.bin").exists());
        let ev = r
            .svc
            .state
            .lock()
            .unwrap()
            .events
            .iter()
            .find(|e| e["kind"] == "would_remove")
            .cloned()
            .unwrap();
        assert_eq!(ev["reason"], "dry_run");
        assert!(ev["edge_checks"].is_string());
        assert!(steps(&r).is_empty() && r.svc.state.lock().unwrap().removal_tickets.is_empty());
    }

    // --- #61's review --------------------------------------------------------------

    /// The removal's journal row, by path, after its state changed.
    #[cfg(target_os = "linux")]
    fn state(r: &Runner<Fake>, path: &str) -> (String, Option<String>) {
        let j = journal(r, path);
        (j.state, j.reason)
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn w2_sees_a_same_size_rewrite_with_its_mtime_put_back() {
        let (nas, _st, mut r, clock) = hidden_file("s.bin").await;
        let j = journal(&r, "s.bin");
        assert!(j.hidden_ctime.is_some(), "the ctime W1 left, journalled");
        let p = nas.path().join(&j.hidden_name);
        let mtime = std::fs::metadata(&p).unwrap().modified().unwrap();
        // Same size, other bytes, and the mtime set back (touch -d, cp -p).
        std::fs::write(&p, b"LEAVE ME").unwrap();
        std::fs::File::options()
            .write(true)
            .open(&p)
            .unwrap()
            .set_modified(mtime)
            .unwrap();
        clock.advance(61_000);
        r.tick().await;
        let (st, why) = state(&r, "s.bin");
        assert_eq!(
            (st.as_str(), why.as_deref()),
            ("cancelled", Some("changed_in_grace"))
        );
        assert_eq!(
            std::fs::read(nas.path().join("s.bin")).unwrap(),
            b"LEAVE ME"
        );
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_folder_renamed_in_the_grace_is_followed_by_inode() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(nas.path().join("rm")).unwrap();
        std::fs::write(nas.path().join("rm/f.bin"), b"in a folder").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        move_now(&mut r);
        go_live(&mut r, nas.path(), 60_000, true);
        r.tick().await;
        clock.advance(Q);
        r.tick().await;
        let j = journal(&r, "rm/f.bin");
        assert_eq!(j.state, "hidden");
        std::fs::rename(nas.path().join("rm"), nas.path().join("renamed")).unwrap();
        clock.advance(61_000);
        r.tick().await;
        let j = journal(&r, "renamed/f.bin");
        assert_eq!(
            (j.state.as_str(), j.reason.as_deref()),
            ("cancelled", Some("moved_in_grace"))
        );
        assert!(
            nas.path().join("renamed/f.bin").exists(),
            "put back where its folder went"
        );
        assert!(!nas.path().join("renamed").join(&j.hidden_name).exists());
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn w3_tries_again_after_a_transient_error_and_raises_when_it_cant() {
        use crate::annex::remover::faults::W3_EIO;
        let (nas, _st, mut r, clock) = hidden_file("t.bin").await;
        let j = journal(&r, "t.bin");
        std::fs::write(nas.path().join(&j.hidden_name), b"changed: put it back").unwrap();
        W3_EIO.with(|f| f.set(2));
        clock.advance(61_000);
        r.tick().await;
        let j1 = journal(&r, "t.bin");
        assert_eq!(j1.state, "cancelling", "an EIO isn't a cancel: tried again");
        assert!(j1.attempts >= 1 && j1.next_at > r.now());
        for _ in 0..4 {
            clock.advance(3_600_000);
            r.tick().await;
        }
        assert_eq!(journal(&r, "t.bin").state, "cancelled");
        assert!(nas.path().join("t.bin").exists());
        W3_EIO.with(|f| f.set(0));
        // Never put back: stuck, raised.
        let (nas, _st, mut r, clock) = hidden_file("u.bin").await;
        let j = journal(&r, "u.bin");
        std::fs::write(nas.path().join(&j.hidden_name), b"changed: put it back").unwrap();
        W3_EIO.with(|f| f.set(1000));
        clock.advance(61_000);
        for _ in 0..12 {
            r.tick().await;
            clock.advance(3_600_000);
        }
        W3_EIO.with(|f| f.set(0));
        assert_eq!(journal(&r, "u.bin").state, "stuck");
        assert!(steps(&r).contains(&"stuck".to_owned()));
        assert!(nas.path().join(&j.hidden_name).exists(), "nothing deleted");
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn when_removals_stop_hidden_files_go_back_and_a_pause_only_holds_them() {
        let (nas, _st, mut r, clock) = hidden_file("v.bin").await;
        let mut a = r.annex.clone();
        a.removals = Some(omnifile_control::annex::AnnexRemovals {
            live: false,
            why_not: Some("paused".into()),
            ..Default::default()
        });
        r.update(a.clone(), &AnnexConfig::default());
        assert!(!r.needs_rw(), "a pause holds them: no read-write mount");
        clock.advance(61_000);
        r.tick().await;
        assert_eq!(journal(&r, "v.bin").state, "hidden");
        a.removals.as_mut().unwrap().why_not = Some("no_removing_rules".into());
        r.update(a, &AnnexConfig::default());
        assert!(r.needs_rw(), "kept for the cancels");
        r.tick().await;
        let j = journal(&r, "v.bin");
        assert_eq!(
            (j.state.as_str(), j.reason.as_deref()),
            ("cancelled", Some("no_removing_rules"))
        );
        assert!(nas.path().join("v.bin").exists(), "W3: put back");
        assert!(!r.needs_rw(), "and then no read-write mount");
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn no_write_follows_a_journal_that_cant_be_written() {
        use crate::annex::remover::faults::JOURNAL_FAILS;
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("w.bin"), b"journal first").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        move_now(&mut r);
        go_live(&mut r, nas.path(), 60_000, true);
        r.tick().await;
        JOURNAL_FAILS.with(|f| f.set(true));
        clock.advance(Q);
        r.tick().await;
        JOURNAL_FAILS.with(|f| f.set(false));
        assert!(nas.path().join("w.bin").exists(), "never hidden");
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn w1_without_rename_flags_never_replaces_or_unlinks_the_original() {
        use crate::annex::remover::faults::NO_RENAME_FLAGS;
        NO_RENAME_FLAGS.with(|f| f.set(true));
        let (nas, _st, r, _clock) = hidden_file("x.bin").await;
        let j = journal(&r, "x.bin");
        assert_eq!(
            j.state, "hidden",
            "the NFS path: a plain rename after the check"
        );
        assert!(nas.path().join(&j.hidden_name).exists() && !nas.path().join("x.bin").exists());
        // The hidden name taken: refused, nothing replaced.
        let nas2 = tempfile::tempdir().unwrap();
        let st2 = tempfile::tempdir().unwrap();
        std::fs::write(nas2.path().join("y.bin"), b"y").unwrap();
        let (mut r2, clock2) = runner(nas2.path(), st2.path());
        move_now(&mut r2);
        r2.tick().await;
        clock2.advance(Q);
        r2.tick().await; // copied, not live
        let e = r2.db.entry("y.bin").unwrap().unwrap();
        let st = std::fs::metadata(nas2.path().join("y.bin")).unwrap();
        use std::os::unix::fs::MetadataExt as _;
        let taken = remover::hidden_name(77, "y.bin");
        std::fs::write(nas2.path().join(&taken), b"not ours").unwrap();
        r2.db
            .add_removal(&crate::annex::db::Removal {
                removal_id: 77,
                path: "y.bin".into(),
                file_id: e.file_id.unwrap(),
                version_id: e.copied_version.unwrap(),
                rule: "move".into(),
                ino: st.ino(),
                size: st.size(),
                mtime_ns: st.mtime() * 1_000_000_000 + st.mtime_nsec(),
                ctime_ns: Some(st.ctime() * 1_000_000_000 + st.ctime_nsec()),
                hidden_name: taken.clone(),
                state: "ticketed".into(),
                hidden_at: None,
                reason: None,
                unreported: None,
                pointer: None,
                updated_at: 0,
                hidden_ctime: None,
                crc64: None,
                attempts: 0,
                next_at: 0,
                placeholder: None,
                aside_name: None,
                users_file: false,
            })
            .unwrap();
        go_live(&mut r2, nas2.path(), 60_000, true);
        let now = r2.now();
        r2.remove_pass(now).await;
        NO_RENAME_FLAGS.with(|f| f.set(false));
        assert_eq!(std::fs::read(nas2.path().join("y.bin")).unwrap(), b"y");
        assert_eq!(
            std::fs::read(nas2.path().join(&taken)).unwrap(),
            b"not ours"
        );
        assert_eq!(
            journal(&r2, "y.bin").reason.as_deref(),
            Some("hidden_name_taken")
        );
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_w3_cut_between_its_link_and_unlink_is_finished() {
        let (nas, _st, mut r, _clock) = hidden_file("z.bin").await;
        let j = journal(&r, "z.bin");
        std::fs::hard_link(nas.path().join(&j.hidden_name), nas.path().join("z.bin")).unwrap();
        r.db.set_removal(
            j.removal_id,
            "cancelling",
            Some("changed_in_grace"),
            None,
            0,
        )
        .unwrap();
        r.tick().await;
        assert_eq!(journal(&r, "z.bin").state, "cancelled");
        assert!(nas.path().join("z.bin").exists());
        assert!(
            !nas.path().join(&j.hidden_name).exists(),
            "the hidden link gone"
        );
        assert_eq!(
            std::fs::metadata(nas.path().join("z.bin")).unwrap().nlink(),
            1,
            "one link again"
        );
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_restart_in_deleting_with_the_file_renamed_back_never_counts_it_deleted() {
        let (nas, _st, mut r, _clock) = hidden_file("q.bin").await;
        let j = journal(&r, "q.bin");
        std::fs::rename(nas.path().join(&j.hidden_name), nas.path().join("q.bin")).unwrap();
        r.db.set_removal(j.removal_id, "deleting", None, None, 0)
            .unwrap();
        r.tick().await;
        let (st, why) = state(&r, "q.bin");
        assert_eq!(
            (st.as_str(), why.as_deref()),
            ("cancelled", Some("renamed_back"))
        );
        assert!(!steps(&r).contains(&"deleted".to_owned()));
        assert!(nas.path().join("q.bin").exists());
    }

    fn a_ticket() -> Value {
        json!({
            "placement_id": "1", "file_id": "9", "version_id": "3",
            "path": "surveys/a.segy", "size": 3, "crc64": "0000000000000000",
            "mtime_ns": "1700000000000000000", "folder": "lts",
            "names": ["a.segy"], "origin": true, "same_content": false, "protect": false,
            "acl": {"kind": "posix", "mode": 0o640},
            "read": {"mode": "azure_user_delegation_sas", "url": "https://sim/blob/1"},
            "uid": null, "gid": null, "expires_at": "2099-01-01T00:00:00Z"
        })
    }

    async fn ticketed(r: &mut Runner<Fake>) {
        r.svc
            .state
            .lock()
            .unwrap()
            .placement_tickets
            .push(a_ticket());
        let now = r.now();
        assert_eq!(r.ask_placements(now).await, 1);
    }

    fn attempts(r: &Runner<Fake>) -> i64 {
        placer::Journal(&r.db).get(1).unwrap().unwrap().attempts
    }

    #[tokio::test]
    async fn a_placement_mount_that_failed_counts_against_the_bound() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        let (mut r, _clock) = runner(nas.path(), st.path());
        ticketed(&mut r).await;
        r.pl_problem = Some("no such keytab".into());
        let now = r.now();
        r.place_pass(now).await;
        assert_eq!(attempts(&r), 1);
        assert!(r.pl_problem.is_none());
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn mounts_that_arent_apart_count_against_the_bound_and_write_nothing() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        let (mut r, _clock) = runner(nas.path(), st.path());
        ticketed(&mut r).await;
        r.check_mounts = true; // the "read-only" mount here is a writable directory
        r.pl_mountpoint = Some(nas.path().to_owned());
        let now = r.now();
        r.place_pass(now).await;
        assert_eq!(attempts(&r), 1);
        assert_eq!(std::fs::read_dir(nas.path()).unwrap().count(), 0);
    }

    #[tokio::test]
    async fn a_mount_that_cant_be_opened_counts_against_the_bound() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        let (mut r, _clock) = runner(nas.path(), st.path());
        ticketed(&mut r).await;
        r.check_mounts = false;
        r.pl_mountpoint = Some(nas.path().join("not-there"));
        let now = r.now();
        r.place_pass(now).await;
        assert_eq!(attempts(&r), 1);
    }

    #[tokio::test]
    async fn an_account_nobody_can_look_up_doesnt_end_the_pass_silently() {
        let d = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        let cfg = AnnexConfig::default();
        // Winbind away: tried again, and it counts.
        let mut r = smb_runner(&cfg, "copy", Box::new(NoWinbind), st.path());
        r.check_mounts = false;
        r.read_name = Some("reader@OMNI.TEST".into());
        ticketed(&mut r).await;
        r.pl_mountpoint = Some(d.path().to_owned());
        let now = r.now();
        r.place_pass(now).await;
        assert_eq!(attempts(&r), 1);
        assert_eq!(std::fs::read_dir(d.path()).unwrap().count(), 0);
        // Winbind says no such account: an owner has to fix it, so it sticks now.
        let mut r = smb_runner(
            &cfg,
            "copy",
            Box::new(omnifile_identity::fake::FakeIdMap::new("OMNI")),
            st.path(),
        );
        r.check_mounts = false;
        r.read_name = Some("reader@OMNI.TEST".into());
        ticketed(&mut r).await;
        r.pl_mountpoint = Some(d.path().to_owned());
        let now = r.now();
        r.place_pass(now).await;
        let p = placer::Journal(&r.db).get(1).unwrap().unwrap();
        assert_eq!(p.state, "stuck", "{p:?}");
    }

    #[tokio::test]
    async fn a_placement_mount_thats_gone_writes_nothing_and_claims_nothing() {
        // A stale `umount -l` left a plain directory there. With an empty target folder
        // the whole placement would run on the edge's disk and report `placed`.
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        let (mut r, _clock) = runner(nas.path(), st.path());
        r.check_mounts = false;
        ticketed(&mut r).await;
        std::fs::create_dir(nas.path().join("lts")).unwrap();
        r.svc
            .state
            .lock()
            .unwrap()
            .read_blobs
            .insert("https://sim/blob/1".into(), b"abc".to_vec());
        r.pl_mountpoint = Some(nas.path().to_owned());
        r.check_mounted = true;
        let now = r.now();
        r.place_pass(now).await;
        assert_eq!(attempts(&r), 1, "it counts against the bound");
        assert!(r.pl_mountpoint.is_none(), "so that it is mounted again");
        let names: Vec<_> = std::fs::read_dir(nas.path()).unwrap().collect();
        assert_eq!(
            names.len(),
            1,
            "nothing but the folder that was there: {names:?}"
        );
        let steps = r.svc.state.lock().unwrap().placement_steps.clone();
        assert!(steps.is_empty(), "nothing said: {steps:?}");
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_read_write_mount_thats_gone_removes_nothing() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("m1.bin"), b"one").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        move_now(&mut r);
        r.tick().await;
        clock.advance(Q);
        r.tick().await;
        go_live(&mut r, nas.path(), 60_000, true);
        r.check_mounted = true;
        let now = r.now();
        r.remove_pass(now).await;
        assert!(r.rw_mountpoint.is_none(), "so that it is mounted again");
        assert_eq!(std::fs::read(nas.path().join("m1.bin")).unwrap(), b"one");
        assert!(steps(&r).is_empty(), "{:?}", steps(&r));
    }

    #[tokio::test]
    async fn a_placement_mount_that_goes_as_it_is_opened_writes_nothing() {
        // The check passed, and the mount went before the open: the root is the
        // directory under it.
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        let (mut r, _clock) = runner(nas.path(), st.path());
        r.check_mounts = false;
        ticketed(&mut r).await;
        std::fs::create_dir(nas.path().join("lts")).unwrap();
        r.pl_mountpoint = Some(nas.path().to_owned());
        r.check_mounted = true;
        let dev = open_root(nas.path(), "").unwrap().dev();
        r.test_mount = Some((true, Some(dev + 1)));
        let now = r.now();
        r.place_pass(now).await;
        assert_eq!(attempts(&r), 1);
        assert!(r.pl_mountpoint.is_none());
        assert_eq!(std::fs::read_dir(nas.path()).unwrap().count(), 1);
        assert!(r.svc.state.lock().unwrap().placement_steps.is_empty());
        // On the mount, it goes on.
        r.pl_mountpoint = Some(nas.path().to_owned());
        r.test_mount = Some((true, Some(dev)));
        let now = r.now();
        r.place_pass(now).await;
        assert!(r.pl_mountpoint.is_some(), "a mount, so it stays");
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_read_write_mount_that_goes_as_it_is_opened_removes_nothing() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("m1.bin"), b"one").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        move_now(&mut r);
        r.tick().await;
        clock.advance(Q);
        r.tick().await;
        go_live(&mut r, nas.path(), 60_000, true);
        r.check_mounted = true;
        let dev = open_root(nas.path(), "").unwrap().dev();
        r.test_mount = Some((true, Some(dev + 1)));
        let now = r.now();
        r.remove_pass(now).await;
        assert!(r.rw_mountpoint.is_none());
        assert_eq!(std::fs::read(nas.path().join("m1.bin")).unwrap(), b"one");
        assert!(steps(&r).is_empty(), "{:?}", steps(&r));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_root_opened_off_the_mount_isnt_on_it() {
        // The mount can go between the check and the open: then the root is the local
        // directory under it, with its own device.
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        let (mut r, _clock) = runner(nas.path(), st.path());
        r.check_mounted = true;
        let proc_root = open_root(Path::new("/proc"), "").unwrap();
        let tmp_root = open_root(nas.path(), "").unwrap();
        assert!(r.opened_on_mount(Path::new("/proc"), &proc_root));
        assert!(!r.opened_on_mount(Path::new("/proc"), &tmp_root));
        assert!(
            !r.opened_on_mount(nas.path(), &tmp_root),
            "not a mount at all"
        );
    }

    #[tokio::test]
    async fn a_quick_scan_reads_the_acls_of_folders_it_finds() {
        // Decision 63: an annexed file is hidden until every folder above it is read,
        // so a new folder's can't wait for the next full scan.
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("a.bin"), b"a").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        r.tick().await; // baseline, with its full folder pass
        r.send().await;
        std::fs::create_dir(nas.path().join("new")).unwrap();
        std::fs::write(nas.path().join("new/b.bin"), b"b").unwrap();
        r.wants.quick.store(true, Ordering::SeqCst);
        clock.advance(1000);
        assert_eq!(r.tick().await.scanned, Some("quick"));
        r.send().await;
        let sent = r.svc.state.lock().unwrap().events.clone();
        assert!(
            sent.iter()
                .any(|e| e["kind"] == "folder_acl" && e["path"] == "new"),
            "{sent:?}"
        );
    }

    #[test]
    fn every_mount_report_says_whether_the_server_shows_its_acls() {
        // Decision 64: a server that shows no ACLs makes the mode's group bits look
        // like the owning group's when a hidden mask may say otherwise.
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        let (r, _clock) = runner(nas.path(), st.path());
        r.report_mount(json!({"ok": true, "read_only": true}));
        r.report_mount(json!({"ok": false, "error": "no"}));
        let mounts: Vec<Value> =
            r.db.outbox(100)
                .unwrap()
                .into_iter()
                .filter(|(_, k, _, _)| k == "mount")
                .map(|(_, _, _, e)| e)
                .collect();
        let local = std::fs::File::open(nas.path()).unwrap();
        assert_eq!(
            mounts[0]["details"]["acls_shown"],
            json!(super::super::acl::acls_shown(&local, r.flavour))
        );
        assert!(mounts[0]["details"]["acls_shown"].is_boolean());
        assert!(
            mounts[1]["details"]["acls_shown"].is_null()
                && mounts[1]["details"].get("acls_shown").is_some(),
            "null when it can't be read, never left out"
        );
    }

    #[test]
    fn a_local_filesystem_shows_posix_acls_and_no_nt_descriptor() {
        let d = tempfile::tempdir().unwrap();
        let f = std::fs::File::open(d.path()).unwrap();
        // xfs and ext4 answer "no ACL" for POSIX; nothing local has a CIFS descriptor.
        assert!(super::super::acl::acls_shown(&f, Flavour::Nfs3));
        assert!(!super::super::acl::acls_shown(&f, Flavour::Smb));
    }

    #[tokio::test]
    async fn the_service_asking_for_a_reconciliation_runs_one_once() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("a.bin"), b"a").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        r.tick().await; // baseline
        let runs = |r: &Runner<Fake>| {
            r.svc
                .state
                .lock()
                .unwrap()
                .events
                .iter()
                .filter(|e| e["kind"] == "scan" && e["details"]["scan"] == "reconcile")
                .count()
        };
        r.send().await;
        let scheduled = r.db.meta("last_reconcile").unwrap();
        let before = runs(&r);
        let mut a = r.annex.clone();
        a.reconcile_asked_at = Some("2026-11-07T10:00:00.000Z".into());
        r.update(a.clone(), &AnnexConfig::default());
        clock.advance(1000);
        r.tick().await;
        r.send().await;
        let after = runs(&r);
        assert_eq!(after, before + 1, "it ran");
        // An ask doesn't put off the scheduled one.
        assert_eq!(r.db.meta("last_reconcile").unwrap(), scheduled);
        // The same ask again: answered, so nothing more.
        r.update(a.clone(), &AnnexConfig::default());
        clock.advance(1000);
        r.tick().await;
        r.send().await;
        assert_eq!(runs(&r), after);
        // A new one: once more.
        a.reconcile_asked_at = Some("2026-11-07T11:00:00.000Z".into());
        r.update(a, &AnnexConfig::default());
        clock.advance(1000);
        r.tick().await;
        r.send().await;
        assert_eq!(runs(&r), after + 1);
    }

    #[tokio::test]
    async fn an_ask_that_fails_waits_for_the_next_look_at_the_annexes() {
        // Not on every tick: a service that can't answer would be asked for its
        // records again every few seconds.
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("a.bin"), b"a").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        r.tick().await; // baseline
        let mut a = r.annex.clone();
        a.reconcile_asked_at = Some("2026-11-07T10:00:00.000Z".into());
        r.update(a.clone(), &AnnexConfig::default());
        r.svc.state.lock().unwrap().down = true;
        clock.advance(1000);
        r.tick().await;
        assert!(
            r.reconcile_asked.is_none(),
            "dropped until the annexes are read again"
        );
        assert_ne!(
            r.db.meta(RECONCILE_ANSWERED).unwrap().as_deref(),
            Some("2026-11-07T10:00:00.000Z")
        );
        r.svc.state.lock().unwrap().down = false;
        r.update(a, &AnnexConfig::default());
        assert!(
            r.reconcile_asked.is_some(),
            "asked again: it was never answered"
        );
        clock.advance(1000);
        r.tick().await;
        assert_eq!(
            r.db.meta(RECONCILE_ANSWERED).unwrap().as_deref(),
            Some("2026-11-07T10:00:00.000Z")
        );
    }

    #[tokio::test]
    async fn an_asked_reconciliation_copies_again_only_what_was_refused() {
        // Review of #81: a copy whose read-back is only pending isn't `copy_ok` either,
        // and copying the whole backlog again would add read-backs.
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("pending.bin"), b"p").unwrap();
        std::fs::write(nas.path().join("refused.bin"), b"r").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        r.tick().await; // baseline
        for _ in 0..3 {
            clock.advance(r.timings.quiet + 1000);
            r.tick().await;
        }
        let state = |r: &Runner<Fake>, p: &str| r.db.entry(p).unwrap().unwrap().state;
        assert_eq!(state(&r, "pending.bin"), "copied");
        assert_eq!(state(&r, "refused.bin"), "copied");
        {
            let mut s = r.svc.state.lock().unwrap();
            let id =
                |s: &FakeState, p: &str| s.files.values().find(|f| f.path == p).unwrap().file_id;
            let (pending, refused) = (id(&s, "pending.bin"), id(&s, "refused.bin"));
            s.unverified.insert(pending);
            s.refused.insert(refused);
        }
        let mut a = r.annex.clone();
        a.reconcile_asked_at = Some("2026-11-07T10:00:00.000Z".into());
        r.update(a, &AnnexConfig::default());
        clock.advance(1000);
        r.tick().await;
        r.send().await;
        // Copied again: a copy_mismatch, and the entry back to `ready` (the same tick
        // may copy it once more).
        let again = |r: &Runner<Fake>, p: &str| {
            r.svc
                .state
                .lock()
                .unwrap()
                .events
                .iter()
                .filter(|e| e["kind"] == "drift" && e["path"] == p)
                .filter(|e| e["details"]["drift"] == "copy_mismatch")
                .count()
        };
        assert_eq!(
            again(&r, "refused.bin"),
            1,
            "the refused copy is copied again"
        );
        assert_eq!(
            again(&r, "pending.bin"),
            0,
            "a pending read-back is left alone"
        );
        assert_eq!(state(&r, "pending.bin"), "copied");
        // The schedule's own run still copies what isn't `copy_ok`, as before.
        r.svc.state.lock().unwrap().refused.clear();
        r.wants.reconcile.store(true, Ordering::SeqCst);
        clock.advance(1000);
        r.tick().await;
        r.send().await;
        assert_eq!(again(&r, "pending.bin"), 1);
    }

    #[tokio::test]
    async fn a_copy_an_admin_stopped_isnt_copied_again_until_resumed() {
        // #81's follow-up: the edge's own reconciliation copied an always-refused file
        // again every `reconcile_days`, a full upload each time.
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("big.bin"), b"b").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        r.tick().await; // baseline
        for _ in 0..3 {
            clock.advance(r.timings.quiet + 1000);
            r.tick().await;
        }
        assert_eq!(r.db.entry("big.bin").unwrap().unwrap().state, "copied");
        let id = {
            let s = r.svc.state.lock().unwrap();
            s.files
                .values()
                .find(|f| f.path == "big.bin")
                .unwrap()
                .file_id
        };
        let again = |r: &Runner<Fake>| {
            r.svc
                .state
                .lock()
                .unwrap()
                .events
                .iter()
                .filter(|e| e["kind"] == "drift" && e["path"] == "big.bin")
                .filter(|e| e["details"]["drift"] == "copy_mismatch")
                .count()
        };
        {
            let mut s = r.svc.state.lock().unwrap();
            s.refused.insert(id);
            s.copy_stopped.insert(id);
        }
        // The schedule's run, and one the service asks for: neither copies it.
        r.wants.reconcile.store(true, Ordering::SeqCst);
        clock.advance(1000);
        r.tick().await;
        r.send().await;
        let mut a = r.annex.clone();
        a.reconcile_asked_at = Some("2026-11-07T10:00:00.000Z".into());
        r.update(a, &AnnexConfig::default());
        clock.advance(1000);
        r.tick().await;
        r.send().await;
        assert_eq!(again(&r), 0, "stopped: not copied again");
        assert_eq!(r.db.entry("big.bin").unwrap().unwrap().state, "copied");
        // Resumed: the next reconciliation copies it again.
        r.svc.state.lock().unwrap().copy_stopped.clear();
        r.wants.reconcile.store(true, Ordering::SeqCst);
        clock.advance(1000);
        r.tick().await;
        r.send().await;
        assert_eq!(again(&r), 1, "resumed: copied again");
    }

    #[tokio::test]
    async fn a_journal_that_cant_take_a_retry_stops_the_placer_a_minute() {
        // Else it does the whole step again, on every tick.
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        r.check_mounts = false;
        ticketed(&mut r).await;
        std::fs::create_dir(nas.path().join("lts")).unwrap();
        r.svc.state.lock().unwrap().read_status = Some(503);
        r.pl_mountpoint = Some(nas.path().to_owned());
        placer::faults::RETRY_NOT_RECORDED.with(|f| f.set(true));
        let now = r.now();
        let out = r.place_pass(now).await;
        let row = placer::Journal(&r.db).get(1).unwrap();
        assert!(out.journal_failed, "{row:?}");
        assert_eq!(r.place_wait_until, now + 60_000);
        // Nothing for that minute, not even with the journal back.
        placer::faults::RETRY_NOT_RECORDED.with(|f| f.set(false));
        clock.advance(59_000);
        let tried = attempts(&r);
        let now = r.now();
        r.place_pass(now).await;
        assert_eq!(attempts(&r), tried, "held off");
        clock.advance(2_000);
        let now = r.now();
        r.place_pass(now).await;
        assert_eq!(attempts(&r), tried + 1, "and then it goes on");
    }

    #[tokio::test]
    async fn with_the_nas_away_at_start_asks_reports_and_the_bound_still_run() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        let (mut r, _clock) = runner(nas.path(), st.path());
        r.svc
            .state
            .lock()
            .unwrap()
            .placement_tickets
            .push(a_ticket());
        r.report_mount(json!({"ok": false}));
        r.while_unmounted("mount: timed out").await;
        // Asked, counted, and what was waiting went out.
        assert_eq!(attempts(&r), 1);
        assert!(!kinds_of(&r, "mount").is_empty());
        assert_eq!(r.db.outbox_len().unwrap(), 0);
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn tickets_are_matched_to_files_by_id_not_by_position() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("m1.bin"), b"one").unwrap();
        std::fs::write(nas.path().join("m2.bin"), b"two, longer").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        move_now(&mut r);
        r.tick().await;
        clock.advance(Q);
        r.tick().await; // copied, not live: would_remove only
        go_live(&mut r, nas.path(), 60_000, true);
        let e1 = r.db.entry("m1.bin").unwrap().unwrap();
        let e2 = r.db.entry("m2.bin").unwrap().unwrap();
        // A candidate with no entry first (skipped), then the two.
        r.db.add_removal_candidate("gone.bin", 999, 1, "move", 3)
            .unwrap();
        for e in [&e1, &e2] {
            r.db.add_removal_candidate(
                &e.path,
                e.file_id.unwrap(),
                e.copied_version.unwrap(),
                "move",
                3,
            )
            .unwrap();
        }
        // m1 refused by the service; only m2 gets a ticket.
        r.svc
            .state
            .lock()
            .unwrap()
            .ticket_answer
            .insert("m1.bin".into(), ("refused".into(), "legal_hold".into()));
        let now = r.now();
        r.remove_pass(now).await;
        assert!(nas.path().join("m1.bin").exists(), "m1 never hidden");
        assert_eq!(journal(&r, "m2.bin").state, "hidden");
        assert_eq!(journal(&r, "m2.bin").size, 11, "m2's own ticket");
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn no_write_while_the_mounts_arent_apart() {
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("n.bin"), b"same superblock").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        move_now(&mut r);
        go_live(&mut r, nas.path(), 60_000, true);
        r.check_mounts = true; // the "read-only" one is writable, and one superblock
        r.tick().await;
        clock.advance(Q);
        r.tick().await;
        assert!(nas.path().join("n.bin").exists());
        assert!(
            r.db.open_removals().unwrap().is_empty() || journal(&r, "n.bin").state == "ticketed"
        );
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn w3_without_hard_links_goes_through_a_placeholder_and_never_replaces() {
        use crate::annex::remover::faults::LINK_EACCES;
        LINK_EACCES.with(|f| f.set(true));
        let (nas, _st, mut r, clock) = hidden_file("p.bin").await;
        let j = journal(&r, "p.bin");
        std::fs::write(nas.path().join(&j.hidden_name), b"changed: put back").unwrap();
        clock.advance(61_000);
        r.tick().await;
        assert_eq!(journal(&r, "p.bin").state, "cancelled");
        assert_eq!(
            std::fs::read(nas.path().join("p.bin")).unwrap(),
            b"changed: put back"
        );
        assert!(!nas.path().join(&j.hidden_name).exists());
        // The name taken: the placeholder is refused, nothing replaced.
        let (nas, _st, mut r, clock) = hidden_file("o.bin").await;
        let j = journal(&r, "o.bin");
        std::fs::write(nas.path().join(&j.hidden_name), b"changed").unwrap();
        std::fs::write(nas.path().join("o.bin"), b"a user's new file").unwrap();
        clock.advance(61_000);
        r.tick().await;
        assert_eq!(journal(&r, "o.bin").state, "stuck");
        assert_eq!(
            std::fs::read(nas.path().join("o.bin")).unwrap(),
            b"a user's new file"
        );
        // Cut between the placeholder and the rename: finished on resume,
        // for our journalled placeholder only.
        let (nas, _st, mut r, _clock) = hidden_file("n2.bin").await;
        let j = journal(&r, "n2.bin");
        std::fs::write(nas.path().join("n2.bin"), remover::PLACEHOLDER_TEXT).unwrap();
        let ph = std::fs::metadata(nas.path().join("n2.bin")).unwrap();
        r.db.set_removal_placeholder(
            j.removal_id,
            Some((ph.ino(), ph.ctime() * 1_000_000_000 + ph.ctime_nsec())),
        )
        .unwrap();
        r.db.set_removal(
            j.removal_id,
            "cancelling",
            Some("changed_in_grace"),
            None,
            0,
        )
        .unwrap();
        r.tick().await;
        LINK_EACCES.with(|f| f.set(false));
        assert_eq!(journal(&r, "n2.bin").state, "cancelled");
        assert_eq!(
            std::fs::read(nas.path().join("n2.bin")).unwrap(),
            b"leave me"
        );
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_users_empty_file_at_the_name_is_never_replaced() {
        use crate::annex::remover::faults::LINK_EACCES;
        LINK_EACCES.with(|f| f.set(true));
        let (nas, _st, mut r, clock) = hidden_file("e0.bin").await;
        let j = journal(&r, "e0.bin");
        std::fs::write(nas.path().join(&j.hidden_name), b"changed: put back").unwrap();
        std::fs::write(nas.path().join("e0.bin"), b"a user's file").unwrap();
        clock.advance(61_000);
        r.tick().await;
        assert_eq!(journal(&r, "e0.bin").state, "stuck");
        // The user's file is emptied (touch, a lock file, an O_TRUNC rewrite):
        // the hourly retry must leave it alone.
        std::fs::write(nas.path().join("e0.bin"), b"").unwrap();
        for _ in 0..3 {
            clock.advance(3_600_000);
            r.tick().await;
        }
        LINK_EACCES.with(|f| f.set(false));
        assert_eq!(journal(&r, "e0.bin").state, "stuck");
        assert_eq!(std::fs::read(nas.path().join("e0.bin")).unwrap(), b"");
        assert!(
            nas.path().join(&j.hidden_name).exists(),
            "still hidden, nothing lost"
        );
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_placeholder_a_user_changed_is_never_replaced() {
        let (nas, _st, mut r, _clock) = hidden_file("e1.bin").await;
        let j = journal(&r, "e1.bin");
        // Our placeholder, journalled, then a user writes into it.
        std::fs::write(nas.path().join("e1.bin"), remover::PLACEHOLDER_TEXT).unwrap();
        let ph = std::fs::metadata(nas.path().join("e1.bin")).unwrap();
        r.db.set_removal_placeholder(
            j.removal_id,
            Some((ph.ino(), ph.ctime() * 1_000_000_000 + ph.ctime_nsec())),
        )
        .unwrap();
        r.db.set_removal(
            j.removal_id,
            "cancelling",
            Some("changed_in_grace"),
            None,
            0,
        )
        .unwrap();
        std::fs::write(nas.path().join("e1.bin"), b"the user's words, same inode").unwrap();
        r.tick().await;
        assert_eq!(journal(&r, "e1.bin").state, "stuck");
        assert_eq!(
            std::fs::read(nas.path().join("e1.bin")).unwrap(),
            b"the user's words, same inode"
        );
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn over_smb_a_write_during_the_crc_read_is_seen_before_the_unlink() {
        use crate::annex::remover::faults::SMB;
        let nas = tempfile::tempdir().unwrap();
        let st = tempfile::tempdir().unwrap();
        std::fs::write(nas.path().join("big.bin"), b"read for a long time").unwrap();
        let (mut r, clock) = runner(nas.path(), st.path());
        move_now(&mut r);
        r.tick().await;
        clock.advance(Q);
        r.tick().await; // copied (as NFS: the ACL capture is SMB's otherwise)
        // Now an SMB share: no streams (faked), and the CRC read before W2.
        r.flavour = Flavour::Smb;
        SMB.lock().unwrap().push((nas.path().to_owned(), None));
        go_live(&mut r, nas.path(), 60_000, true);
        let e = r.db.entry("big.bin").unwrap().unwrap();
        r.db.add_removal_candidate(
            "big.bin",
            e.file_id.unwrap(),
            e.copied_version.unwrap(),
            "move",
            3,
        )
        .unwrap();
        let now = r.now();
        r.remove_pass(now).await;
        let j = journal(&r, "big.bin");
        assert_eq!(j.state, "hidden");
        let crc = omnifile_integrity::crc64::crc64_nvme(b"read for a long time");
        r.db.conn()
            .execute(
                "UPDATE removal SET crc64 = ?1 WHERE removal_id = ?2",
                rusqlite::params![format!("{crc:016x}"), j.removal_id as i64],
            )
            .unwrap();
        // A user writes while the CRC is read (the read itself saw the old bytes).
        let hidden = nas.path().join(&j.hidden_name);
        {
            let mut smb = SMB.lock().unwrap();
            let e = smb.iter_mut().find(|(d, _)| d == nas.path()).unwrap();
            e.1 = Some(Box::new(move || {
                use std::io::Write;
                let mut f = std::fs::OpenOptions::new()
                    .append(true)
                    .open(&hidden)
                    .unwrap();
                f.write_all(b" and then some").unwrap();
            }));
        }
        clock.advance(61_000);
        r.tick().await;
        SMB.lock().unwrap().retain(|(d, _)| d != nas.path());
        let j = journal(&r, "big.bin");
        assert_eq!(
            (j.state.as_str(), j.reason.as_deref()),
            ("cancelled", Some("changed_in_grace"))
        );
        assert_eq!(
            std::fs::read(nas.path().join("big.bin")).unwrap(),
            b"read for a long time and then some"
        );
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn a_restart_after_w3_renamed_over_its_placeholder_is_cancelled_not_stuck() {
        let (nas, _st, mut r, _clock) = hidden_file("e2.bin").await;
        let j = journal(&r, "e2.bin");
        // The placeholder made and journalled, the rename over it done, and
        // then the crash: the journal still says cancelling, placeholder set.
        std::fs::write(nas.path().join("e2.bin"), remover::PLACEHOLDER_TEXT).unwrap();
        let ph = std::fs::metadata(nas.path().join("e2.bin")).unwrap();
        r.db.set_removal_placeholder(
            j.removal_id,
            Some((ph.ino(), ph.ctime() * 1_000_000_000 + ph.ctime_nsec())),
        )
        .unwrap();
        std::fs::rename(nas.path().join(&j.hidden_name), nas.path().join("e2.bin")).unwrap();
        r.db.set_removal(
            j.removal_id,
            "cancelling",
            Some("changed_in_grace"),
            None,
            0,
        )
        .unwrap();
        r.tick().await;
        assert_eq!(journal(&r, "e2.bin").state, "cancelled");
        assert_eq!(
            std::fs::read(nas.path().join("e2.bin")).unwrap(),
            b"leave me"
        );
    }
}
