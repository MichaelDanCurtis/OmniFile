//! Scans of the NAS directory (`annex.md` §2.2), read-only.
//!
//! - A **full scan** stats every file. A file rewritten in place doesn't
//!   change its directory's mtime, so only a full scan (or reconciliation)
//!   sees it.
//! - A **quick scan** reads only directories whose mtime changed since the
//!   last scan (and new ones, whole), so it finds arrivals, departures and
//!   renames cheaply.
//! - A file that is gone from where it was, while a file with the same file
//!   id (inode, or SMB file id with `serverino`) appeared in the same scan,
//!   was **renamed**, not deleted and re-created.
//!
//! Each look goes into `annex.db` ([`AnnexDb::observe`]); departures and
//! renames of files the service knows become `left` and `renamed` events in
//! the outbox. Arrivals wait for the quiet period (`runner`).

use std::path::{Path, PathBuf};
use std::time::Instant;

use serde_json::json;

use super::db::{AnnexDb, Change, Entry, Seen};
use super::nasfs::{Kind, Root};

/// How long after its last change a directory's mtime is trusted (ms): a
/// listing made sooner may come from the NFS or SMB client's cache.
pub const SETTLE_MS: i64 = 10_000;

/// A folder's inode and ctime, which a chmod, a chown, an ACL change or a
/// re-created folder moves (decision 63). A ctime that changed moments ago may
/// come from the client's attribute cache, and the ACL read with it may too:
/// then the stamp is one that never matches, so the folder is read again.
pub fn dir_stamp(st: &super::nasfs::Stat, now_ms: i64) -> String {
    if st.ctime_ns > (now_ms - SETTLE_MS).saturating_mul(1_000_000) {
        return "unsettled".into();
    }
    format!("{}:{}", st.ino, st.ctime_ns)
}

/// OmniFile's own files on a NAS: never copied or counted (`annex.md` §4.4).
pub fn own_file(name: &str) -> bool {
    name.ends_with(".omnifile.txt")
        || name.starts_with(".omnifile-removing-")
        || name.starts_with(".omnifile-restoring-")
        || name.starts_with(super::placer::replace::HIDDEN_PREFIX)
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ScanStats {
    pub files: u64,
    pub dirs: u64,
    pub bytes: u64,
    pub new: u64,
    pub changed: u64,
    pub gone: u64,
    pub renamed: u64,
    pub skipped: u64,
    pub millis: u64,
}

impl ScanStats {
    pub fn json(&self, which: &str) -> serde_json::Value {
        json!({
            "scan": which, "files": self.files, "dirs": self.dirs, "bytes": self.bytes,
            "new": self.new, "changed": self.changed, "gone": self.gone,
            "renamed": self.renamed, "skipped": self.skipped, "millis": self.millis,
        })
    }
}

pub fn ns(secs: i64, nsec: i64) -> i64 {
    secs.saturating_mul(1_000_000_000).saturating_add(nsec)
}

/// One look at a regular file.
#[cfg(unix)]
pub fn seen_of(md: &std::fs::Metadata) -> Seen {
    use std::os::unix::fs::MetadataExt;
    let btime = md
        .created()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_nanos() as i64);
    Seen {
        ino: md.ino().to_string(),
        size: md.len(),
        mtime_ns: ns(md.mtime(), md.mtime_nsec()),
        ctime_ns: ns(md.ctime(), md.ctime_nsec()),
        btime_ns: btime,
        nlink: md.nlink(),
    }
}

/// Errors that mean "not there as a directory any more" (gone, replaced by
/// a file or a symlink), as opposed to "there, but can't be read".
pub fn gone_error(e: &std::io::Error) -> bool {
    e.kind() == std::io::ErrorKind::NotFound
        || matches!(e.raw_os_error(), Some(libc::ENOTDIR) | Some(libc::ELOOP))
}

fn rel(parent: &str, name: &str) -> String {
    if parent.is_empty() {
        name.to_owned()
    } else {
        format!("{parent}/{name}")
    }
}

pub struct Scanner<'a> {
    pub db: &'a AnnexDb,
    pub root: PathBuf,
    /// The root, held open; every read is beneath it (`nasfs`).
    fs: Result<Root, String>,
    pub now: i64,
    pub mark: i64,
    pub baseline: bool,
    pub stats: ScanStats,
    /// Paths the scan couldn't read, with why (sent as `skipped`).
    pub problems: Vec<(String, String)>,
    /// This scan's root is the export's: the placers' staging folders are at
    /// its top, and only there are they skipped. A user's own folder of that
    /// name anywhere else is scanned like any other.
    pub at_export_root: bool,
}

impl<'a> Scanner<'a> {
    /// A folder at the export's top with a staging folder's name that isn't this
    /// annex's. It stays out of the scan, in case it holds another annex's staged
    /// bytes, and it is said once, because it could be someone's.
    fn note_staging_like(&self, name: &str) {
        let ours = self.db.meta("staging_rel").ok().flatten();
        if ours.as_deref() == Some(name) {
            return;
        }
        let key = format!("staging_like:{name}");
        if self.db.meta(&key).ok().flatten().is_some() {
            return;
        }
        tracing::warn!(
            folder = name,
            "a folder named like a staging folder isn't this annex's; not scanned"
        );
        let ev = serde_json::json!({"kind": "skipped", "path": name, "reason": "staging_like_folder",
            "detail": "named like a staging folder and not this annex's: another annex's, or \
                       someone's; not scanned or copied"});
        if self.db.push_event("skipped", Some(name), &ev).is_ok() {
            let _ = self.db.set_meta(&key, "reported");
        }
    }

    pub fn new(db: &'a AnnexDb, root: &Path, now: i64, baseline: bool) -> Scanner<'a> {
        Self::with_root(db, Root::open(root), now, baseline)
    }

    /// A scan of an opened root (or of one that couldn't be opened: then
    /// nothing is judged gone).
    pub fn with_root(
        db: &'a AnnexDb,
        fs: std::io::Result<Root>,
        now: i64,
        baseline: bool,
    ) -> Scanner<'a> {
        Scanner {
            db,
            root: fs.as_ref().map(|r| r.path().to_owned()).unwrap_or_default(),
            fs: fs.map_err(|e| format!("the annex root: {e}")),
            now,
            // Unique per scan: a time in ms, so no two scans share one.
            mark: now,
            baseline,
            stats: ScanStats::default(),
            problems: Vec::new(),
            at_export_root: false,
        }
    }

    /// Read one directory: observe its files; return its subdirectories.
    /// `None` if it is no longer a directory there; an unreadable one keeps
    /// what was known under it (no departures from there).
    fn read_dir(&mut self, dir: &str) -> rusqlite::Result<Option<Vec<String>>> {
        let listing = match &self.fs {
            Ok(fs) => fs.read_dir(dir),
            Err(e) => Err(std::io::Error::other(e.clone())),
        };
        let (me, items) = match listing {
            Ok(l) => l,
            Err(e) if gone_error(&e) && !dir.is_empty() => return Ok(None),
            Err(e) => {
                self.problems
                    .push((dir.to_owned(), format!("read_dir: {e}")));
                self.mark_children(dir)?;
                return Ok(Some(Vec::new()));
            }
        };
        self.stats.dirs += 1;
        let mut subdirs = Vec::new();
        for (name, st) in items {
            let Some(name) = name.to_str().map(str::to_owned) else {
                self.stats.skipped += 1;
                self.problems
                    .push((dir.to_owned(), "a name that isn't UTF-8".into()));
                continue;
            };
            if own_file(&name) {
                continue;
            }
            if self.at_export_root && dir.is_empty() && super::placer::is_staging_dir(&name) {
                self.note_staging_like(&name);
                continue;
            }
            let child = rel(dir, &name);
            let st = match st {
                Ok(s) => s,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                Err(e) => {
                    self.problems.push((child.clone(), format!("stat: {e}")));
                    // Known files there stay as they were.
                    self.mark_children(&child)?;
                    self.db.conn().execute(
                        "UPDATE entry SET scan_mark = ?1 WHERE path = ?2",
                        rusqlite::params![self.mark, child],
                    )?;
                    continue;
                }
            };
            if st.kind == Kind::Dir {
                subdirs.push(child);
            } else if st.kind == Kind::File {
                let s = st.seen();
                self.db
                    .set_stat_facts(&child, st.atime_ns, st.uid, st.gid)?;
                self.stats.files += 1;
                self.stats.bytes += s.size;
                match self
                    .db
                    .observe(&child, &s, self.now, self.baseline, self.mark)?
                {
                    Change::New => self.stats.new += 1,
                    Change::Changed => self.stats.changed += 1,
                    Change::Same => {}
                }
            } else {
                // Symlinks, devices, sockets and mounts below the root: not
                // copied or followed (a link's target is copied where it
                // lives, if that is in the annex).
                self.stats.skipped += 1;
            }
        }
        // A directory changed moments ago may have been listed from the
        // client's cache (NFS and SMB clients keep attributes and listings
        // for a while): its mtime isn't trusted until it settles, so quick
        // scans read it again until then.
        let m = me.mtime_ns;
        let settled = m <= (self.now - SETTLE_MS).saturating_mul(1_000_000);
        self.db
            .set_dir(dir, if settled { m } else { 0 }, self.mark)?;
        self.db.set_dir_stamp(dir, &dir_stamp(&me, self.now))?;
        Ok(Some(subdirs))
    }

    /// An unreadable directory's files stay as they were.
    fn mark_children(&self, dir: &str) -> rusqlite::Result<()> {
        let prefix = if dir.is_empty() {
            String::new()
        } else {
            format!("{dir}/")
        };
        self.db.conn().execute(
            "UPDATE entry SET scan_mark = ?1 WHERE substr(path, 1, length(?2)) = ?2",
            rusqlite::params![self.mark, prefix],
        )?;
        self.db.conn().execute(
            "UPDATE dir SET scan_mark = ?1 WHERE path = ?2 OR substr(path, 1, length(?3)) = ?3",
            rusqlite::params![self.mark, dir, prefix],
        )?;
        Ok(())
    }

    fn walk(&mut self, top: &str) -> rusqlite::Result<()> {
        let mut stack = vec![top.to_owned()];
        while let Some(d) = stack.pop() {
            if let Some(sub) = self.read_dir(&d)? {
                stack.extend(sub);
            }
        }
        Ok(())
    }

    /// Every file under the root.
    pub fn full(&mut self) -> rusqlite::Result<()> {
        let t = Instant::now();
        self.walk("")?;
        let gone = self.db.unmarked("", self.mark)?;
        self.departures(gone)?;
        self.db
            .conn()
            .execute("DELETE FROM dir WHERE scan_mark <> ?1", [self.mark])?;
        self.stats.millis = t.elapsed().as_millis() as u64;
        Ok(())
    }

    /// Only directories whose mtime changed, and new ones whole. Without a
    /// listing yet, a full scan.
    pub fn quick(&mut self) -> rusqlite::Result<()> {
        let t = Instant::now();
        let known = self.db.dirs()?;
        if known.is_empty() {
            return self.full();
        }
        let mut gone: Vec<Entry> = Vec::new();
        let mut seen_dirs: std::collections::HashSet<String> = Default::default();
        for (dir, mtime) in &known {
            if seen_dirs.contains(dir) {
                continue;
            }
            let st = match &self.fs {
                Ok(fs) => fs.stat(dir, false),
                Err(e) => Err(std::io::Error::other(e.clone())),
            };
            let md = match st {
                Ok(m) if m.is_dir() => Some(m),
                Ok(_) => None,
                Err(e) if gone_error(&e) && !dir.is_empty() => None,
                Err(e) => {
                    // There, but unreadable now (or the root is): what was
                    // known under it stays; never a departure.
                    self.problems.push((dir.clone(), format!("stat: {e}")));
                    self.mark_children(dir)?;
                    seen_dirs.insert(dir.clone());
                    continue;
                }
            };
            match md {
                None => {
                    // The directory went (or was renamed): everything under it
                    // is gone, unless found again by file id below.
                    let prefix = format!("{dir}/");
                    gone.extend(self.db.unmarked(&prefix, self.mark)?);
                    self.db.remove_dir_tree(dir)?;
                }
                Some(m) if m.mtime_ns == *mtime => {
                    // A chmod moves a folder's ctime, not its mtime (decision 63).
                    self.db.set_dir_stamp(dir, &dir_stamp(&m, self.now))?;
                }
                Some(_) => {
                    let Some(sub) = self.read_dir(dir)? else {
                        continue;
                    };
                    seen_dirs.insert(dir.clone());
                    for s in sub {
                        if self.db.dir_mtime(&s)?.is_none() {
                            self.walk(&s)?; // new: read it whole
                            seen_dirs.insert(s);
                        }
                    }
                    gone.extend(self.db.unmarked_children(dir, self.mark)?);
                }
            }
        }
        // A subdirectory that vanished from a changed parent: its own row
        // is still there and handled above; files under a known directory
        // whose mtime didn't change aren't touched.
        gone.sort_by(|a, b| a.path.cmp(&b.path));
        gone.dedup_by(|a, b| a.path == b.path);
        self.departures(gone)?;
        self.stats.millis = t.elapsed().as_millis() as u64;
        Ok(())
    }

    /// Files not seen where they were: renamed if their file id turned up at
    /// a new path in this scan, else gone.
    fn departures(&mut self, gone: Vec<Entry>) -> rusqlite::Result<()> {
        for e in gone {
            if self.db.entry(&e.path)?.is_none() {
                continue; // already moved by an earlier rename in this batch
            }
            // Hidden or removed by the remover: not a user's delete. In
            // flight, the file is still the annex's (a cancel brings it
            // back); removed, it lives in Azure now (the service knows).
            match self.db.removal_state_of(&e.path)?.as_deref() {
                Some("done") => {
                    self.db.remove(&e.path)?;
                    continue;
                }
                Some("cancelled") | None => {}
                Some(_) => continue,
            }
            // Hidden by a placement that replaces it (A3.4): still the annex's file.
            if super::placer::replacing(self.db, &e.path) {
                continue;
            }
            let moved = self.db.by_ino(&e.seen.ino)?.into_iter().find(|n| {
                n.path != e.path
                    && n.first_seen == self.now
                    && n.state == "new"
                    && n.file_id.is_none()
            });
            let known = e.file_id.is_some() || e.state == "reported";
            match moved {
                Some(n) => {
                    self.stats.renamed += 1;
                    self.stats.new = self.stats.new.saturating_sub(1);
                    if known {
                        self.db.push_event(
                            "renamed",
                            Some(&n.path),
                            &json!({"kind": "renamed", "path": n.path, "old_path": e.path,
                                    "nas_file_id": e.seen.ino,
                                    "observed_at": rfc3339_ms(self.now)}),
                        )?;
                    }
                    // The catalog's file moves with it: keep its row's state.
                    self.db.rename(&e.path, &n.path)?;
                    self.db.conn().execute(
                        "UPDATE entry SET scan_mark = ?2, last_seen = ?3 WHERE path = ?1",
                        rusqlite::params![n.path, self.mark, self.now],
                    )?;
                    if !n.seen.same(e.seen.size, e.seen.mtime_ns, e.seen.ctime_ns) {
                        self.db
                            .observe(&n.path, &n.seen, self.now, false, self.mark)?;
                    }
                }
                None => {
                    self.stats.gone += 1;
                    if known {
                        self.db.push_event(
                            "left",
                            Some(&e.path),
                            &json!({"kind": "left", "path": e.path, "nas_file_id": e.seen.ino,
                                    "observed_at": rfc3339_ms(self.now)}),
                        )?;
                    }
                    self.db.remove(&e.path)?;
                }
            }
        }
        Ok(())
    }
}

/// RFC 3339 (`2026-11-04T10:00:00.123Z`, or with an offset) as milliseconds
/// since the epoch.
pub fn parse_rfc3339_ms(s: &str) -> Option<i64> {
    let b = s.as_bytes();
    if b.len() < 20 || b[4] != b'-' || b[7] != b'-' || !matches!(b[10], b'T' | b't' | b' ') {
        return None;
    }
    let num = |r: std::ops::Range<usize>| -> Option<i64> { s.get(r)?.parse().ok() };
    let (y, mo, d) = (num(0..4)?, num(5..7)?, num(8..10)?);
    let (h, mi, sec) = (num(11..13)?, num(14..16)?, num(17..19)?);
    if !(1..=12).contains(&mo) || !(1..=31).contains(&d) || h > 23 || mi > 59 || sec > 60 {
        return None;
    }
    let mut i = 19;
    let mut ms = 0i64;
    if b.get(i) == Some(&b'.') {
        i += 1;
        let start = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        let frac = s.get(start..i)?;
        if frac.is_empty() {
            return None;
        }
        let f3 = format!("{:0<3}", &frac[..frac.len().min(3)]);
        ms = f3.parse().ok()?;
    }
    let off_min = match s.get(i..)? {
        "Z" | "z" => 0,
        o if o.len() == 6 && matches!(&o[..1], "+" | "-") && &o[3..4] == ":" => {
            let v = o[1..3].parse::<i64>().ok()? * 60 + o[4..6].parse::<i64>().ok()?;
            if &o[..1] == "-" { -v } else { v }
        }
        _ => return None,
    };
    // Days from civil (Howard Hinnant's algorithm).
    let y = if mo <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (mo + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    Some(((days * 86_400 + h * 3600 + mi * 60 + sec) - off_min * 60) * 1000 + ms)
}

/// Milliseconds since the epoch as RFC 3339 UTC.
pub fn rfc3339_ms(ms: i64) -> String {
    let secs = ms.div_euclid(1000);
    let milli = ms.rem_euclid(1000);
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let (h, m, s) = (rem / 3600, rem % 3600 / 60, rem % 60);
    // Civil from days (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mo = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if mo <= 2 { y + 1 } else { y };
    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{m:02}:{s:02}.{milli:03}Z")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rfc3339_parses_what_it_prints() {
        for ms in [0i64, 1_762_250_400_123, 951_782_400_000, 4_102_444_799_999] {
            assert_eq!(parse_rfc3339_ms(&rfc3339_ms(ms)), Some(ms));
        }
        assert_eq!(
            parse_rfc3339_ms("2026-11-04T12:00:00+02:00"),
            parse_rfc3339_ms("2026-11-04T10:00:00Z")
        );
        assert_eq!(parse_rfc3339_ms("2026-13-04T10:00:00Z"), None);
        assert_eq!(parse_rfc3339_ms("yesterday"), None);
    }

    /// A minute from now: every directory the tests make has settled.
    static BASE: std::sync::LazyLock<i64> =
        std::sync::LazyLock::new(|| crate::annex::now_ms() + 60_000);

    fn write(root: &Path, rel: &str, data: &[u8]) {
        let p = root.join(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, data).unwrap();
    }

    fn known(db: &AnnexDb, path: &str, file_id: u64) {
        let e = db.entry(path).unwrap().unwrap();
        db.set_reported(path, &e.seen).unwrap();
        db.set_catalog(path, file_id, None, true).unwrap();
    }

    #[test]
    fn times_print_as_rfc3339() {
        assert_eq!(rfc3339_ms(0), "1970-01-01T00:00:00.000Z");
        assert_eq!(rfc3339_ms(1_700_000_000_123), "2023-11-14T22:13:20.123Z");
    }

    #[test]
    fn a_staging_folder_is_skipped_only_at_the_top_of_the_export() {
        // E10: an annex at the export's root scanned staged files, copied a
        // quiet one to Azure, then reported it gone.
        let t = tempfile::tempdir().unwrap();
        write(t.path(), ".omnifile-staging-7-ab12/abc-1", b"staged");
        write(
            t.path(),
            "a/.omnifile-staging-7-ab12/mine",
            b"a user's own folder",
        );
        write(t.path(), "two", b"22");
        let db = AnnexDb::open_in_memory().unwrap();
        let mut s = Scanner::new(&db, t.path(), *BASE + 1000, true);
        s.at_export_root = true;
        s.full().unwrap();
        assert!(
            db.entry(".omnifile-staging-7-ab12/abc-1")
                .unwrap()
                .is_none()
        );
        assert!(
            db.entry("a/.omnifile-staging-7-ab12/mine")
                .unwrap()
                .is_some()
        );
        assert!(db.entry("two").unwrap().is_some());
        // Not this annex's, so it is said, once; it could be someone's.
        let said = |db: &AnnexDb| {
            db.outbox(100)
                .unwrap()
                .into_iter()
                .filter(|e| e.3["reason"] == "staging_like_folder")
                .count()
        };
        assert_eq!(said(&db), 1);
        let mut s = Scanner::new(&db, t.path(), *BASE + 2000, false);
        s.at_export_root = true;
        s.full().unwrap();
        assert_eq!(said(&db), 1, "once");
        // This annex's own says nothing.
        let db2 = AnnexDb::open_in_memory().unwrap();
        db2.set_meta("staging_rel", ".omnifile-staging-7-ab12")
            .unwrap();
        let mut s = Scanner::new(&db2, t.path(), *BASE + 1000, true);
        s.at_export_root = true;
        s.full().unwrap();
        assert_eq!(said(&db2), 0);
        // Below the export's root, no folder there is OmniFile's.
        let db = AnnexDb::open_in_memory().unwrap();
        let mut s = Scanner::new(&db, t.path(), *BASE + 1000, true);
        s.full().unwrap();
        assert!(
            db.entry(".omnifile-staging-7-ab12/abc-1")
                .unwrap()
                .is_some()
        );
    }

    #[test]
    fn a_full_scan_finds_files_and_skips_omnifiles_own() {
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "a/b/one.segy", b"1");
        write(t.path(), "two", b"22");
        write(t.path(), "a/x.omnifile.txt", b"pointer");
        let db = AnnexDb::open_in_memory().unwrap();
        let mut s = Scanner::new(&db, t.path(), *BASE + 1000, true);
        s.full().unwrap();
        assert_eq!((s.stats.files, s.stats.bytes, s.stats.new), (2, 3, 2));
        assert!(db.entry("a/b/one.segy").unwrap().unwrap().baseline);
        assert!(db.entry("a/x.omnifile.txt").unwrap().is_none());
    }

    #[test]
    fn a_quick_scan_reads_changed_directories_and_tells_renames_from_deletes() {
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "d/keep", b"k");
        write(t.path(), "d/old-name", b"r");
        write(t.path(), "d/doomed", b"x");
        write(t.path(), "other/still", b"s");
        let db = AnnexDb::open_in_memory().unwrap();
        Scanner::new(&db, t.path(), *BASE + 1000, true)
            .full()
            .unwrap();
        known(&db, "d/old-name", 7);
        known(&db, "d/doomed", 8);
        std::thread::sleep(std::time::Duration::from_millis(20));
        std::fs::rename(t.path().join("d/old-name"), t.path().join("d/new-name")).unwrap();
        std::fs::remove_file(t.path().join("d/doomed")).unwrap();
        write(t.path(), "d/fresh/inner", b"f");
        let mut s = Scanner::new(&db, t.path(), *BASE + 2000, false);
        s.quick().unwrap();
        assert_eq!((s.stats.renamed, s.stats.gone), (1, 1), "{:?}", s.stats);
        let moved = db.entry("d/new-name").unwrap().unwrap();
        assert_eq!(
            moved.file_id,
            Some(7),
            "the catalog file follows the rename"
        );
        assert!(db.entry("d/old-name").unwrap().is_none());
        assert!(db.entry("d/doomed").unwrap().is_none());
        assert_eq!(db.entry("d/fresh/inner").unwrap().unwrap().state, "new");
        let kinds: Vec<_> = db.outbox(10).unwrap().into_iter().map(|e| e.1).collect();
        assert_eq!(kinds, vec!["left", "renamed"]);
        // A quick scan doesn't read directories that didn't change.
        assert_eq!(
            db.entry("other/still").unwrap().unwrap().last_seen,
            *BASE + 1000
        );
    }

    #[test]
    fn a_change_in_place_is_seen_by_a_full_scan_only() {
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "d/f", b"one");
        let db = AnnexDb::open_in_memory().unwrap();
        Scanner::new(&db, t.path(), *BASE + 1000, true)
            .full()
            .unwrap();
        // Rewritten in place: the directory's mtime doesn't move.
        let dir_m = std::fs::metadata(t.path().join("d"))
            .unwrap()
            .modified()
            .unwrap();
        std::thread::sleep(std::time::Duration::from_millis(20));
        std::fs::write(t.path().join("d/f"), b"two!").unwrap();
        assert_eq!(
            std::fs::metadata(t.path().join("d"))
                .unwrap()
                .modified()
                .unwrap(),
            dir_m
        );
        let mut q = Scanner::new(&db, t.path(), *BASE + 2000, false);
        q.quick().unwrap();
        assert_eq!(q.stats.changed, 0);
        let mut f = Scanner::new(&db, t.path(), *BASE + 3000, false);
        f.full().unwrap();
        assert_eq!(f.stats.changed, 1);
        assert_eq!(db.entry("d/f").unwrap().unwrap().seen.size, 4);
    }

    #[test]
    fn a_directory_changed_moments_ago_is_read_again_until_it_settles() {
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "d/a", b"1");
        let db = AnnexDb::open_in_memory().unwrap();
        let now = crate::annex::now_ms();
        Scanner::new(&db, t.path(), now, true).full().unwrap();
        // The listing may have come from the client's cache: not trusted yet.
        assert_eq!(db.dir_mtime("d").unwrap(), Some(0));
        let mut q = Scanner::new(&db, t.path(), now + 1, false);
        q.quick().unwrap();
        assert_eq!(q.stats.dirs, 2, "both directories are read again");
        let mut later = Scanner::new(&db, t.path(), now + SETTLE_MS + 5000, false);
        later.quick().unwrap();
        assert!(db.dir_mtime("d").unwrap().unwrap() > 0, "settled now");
        let mut quiet = Scanner::new(&db, t.path(), now + SETTLE_MS + 6000, false);
        quiet.quick().unwrap();
        assert_eq!(quiet.stats.dirs, 0);
    }

    #[test]
    fn a_vanished_directory_takes_its_files_with_it() {
        let t = tempfile::tempdir().unwrap();
        write(t.path(), "gone/a", b"1");
        write(t.path(), "gone/sub/b", b"2");
        let db = AnnexDb::open_in_memory().unwrap();
        Scanner::new(&db, t.path(), *BASE + 1000, false)
            .full()
            .unwrap();
        known(&db, "gone/a", 1);
        std::fs::remove_dir_all(t.path().join("gone")).unwrap();
        let mut s = Scanner::new(&db, t.path(), *BASE + 2000, false);
        s.quick().unwrap();
        assert_eq!(s.stats.gone, 2);
        assert_eq!(db.count(None).unwrap(), 0);
        let kinds: Vec<_> = db.outbox(10).unwrap().into_iter().map(|e| e.1).collect();
        assert_eq!(
            kinds,
            vec!["left"],
            "only the file the service knew is reported"
        );
    }
}
