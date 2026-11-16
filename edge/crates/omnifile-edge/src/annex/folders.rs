//! Folder ACLs and free space for the return path (`annex.md` §4.6, phase
//! A3.1), read-only.
//!
//! The service decides where a recalled file may be placed. A requester may
//! have it placed only in a folder they could add a file to themselves,
//! because the edge writes with its own account (§4.6). So the service needs
//! each folder's ACL, as it has each file's. After a full scan, the edge
//! reads the ACL of every folder it listed, plus every folder below the return
//! rules' own NAS locations on this filer (a restore folder outside the
//! annex), and sends the ones that changed:
//!
//! - `folder_acl` `{path, acl}`, or `{path, error}` when the ACL can't be read
//!   (the service then lets nobody place a file there: fail closed), or
//!   `{path, gone: true}` for a folder no longer there;
//! - `target_space` `{path, free_bytes, total_bytes}` for the annex root and
//!   each location, as seen through the read-only mount. That is only a
//!   preview: the placement checks the target's space again with its own
//!   account (A3.3).
//!
//! Paths are relative to the filer's export, so one folder has one name
//! whichever annex reports it. Only changes are sent: each folder's last ACL
//! is kept in `annex.db` by hash.
//!
//! Decision 63: who may read an annexed file takes the folders above it into
//! account, up to the export's root, so the service needs every one of them,
//! and hides a file until it has them (fail closed):
//!
//! - the pass also reads the folders above the annex's root ([`ancestors`]);
//!   one the edge can't read is sent as `{path, error}`, which hides
//!   everything below it, and the service says so;
//! - after a quick scan, [`pass_new`] reads the folders no ACL was sent for
//!   yet, so a new folder's files aren't hidden until the next full scan, and
//!   those whose inode or ctime changed since (a chmod, or a folder deleted and
//!   made again), so a folder closed between full scans doesn't keep showing
//!   what is below it (the review of #83);
//! - after an update that brings this, every folder is sent again once
//!   ([`EPOCH`]), since ACLs sent before carry no search sets;
//! - `folders_read` `{details: {folders, unreadable, ancestors, complete}}`
//!   says a pass read every folder; the service shows the annex's files from
//!   the first one.

use std::collections::HashSet;

use ring::digest::{SHA256, digest};
use rusqlite::{OptionalExtension, params};
use serde_json::{Value, json};

use super::acl::{self, Flavour};
use super::db::AnnexDb;
use super::nasfs::{Kind, Root};
use super::scan::{own_file, rfc3339_ms};
use omnifile_identity::IdMap;

/// Folders read below the return rules' locations outside the annex, at most.
pub const MAX_EXTRA_DIRS: usize = 10_000;

/// Every folder's ACL is sent again once when this changes (decision 63).
pub const EPOCH: &str = "63";
const EPOCH_KEY: &str = "folder_acl_epoch";
const READ_KEY: &str = "folders_read_epoch";

/// The folders above the annex's root, from the export's root (`""`) down to
/// its parent. None when the annex is the whole export.
pub fn ancestors(annex_rel: &str) -> Vec<String> {
    if annex_rel.is_empty() {
        return Vec::new();
    }
    let parts: Vec<&str> = annex_rel.split('/').collect();
    (0..parts.len()).map(|i| parts[..i].join("/")).collect()
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct FolderStats {
    pub folders: u64,
    pub changed: u64,
    pub gone: u64,
    pub unreadable: u64,
    /// The extra locations' walk stopped at [`MAX_EXTRA_DIRS`].
    pub truncated: bool,
    /// Folders above the annex's root.
    pub ancestors: u64,
}

/// `a/b` joined: either side may be empty.
pub fn join(a: &str, b: &str) -> String {
    match (a.is_empty(), b.is_empty()) {
        (true, _) => b.to_owned(),
        (_, true) => a.to_owned(),
        _ => format!("{a}/{b}"),
    }
}

/// The `nas_folder` locations outside the annex, on this filer, that the
/// rule set's return rules name (`place_at` and `let_requester_choose`), as
/// export-relative paths. A location inside the annex is listed by the scan.
pub fn extra_locations(body: Option<&Value>, filer_id: u64, annex_rel: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let Some(rules) = body.and_then(|b| b.get("rules")).and_then(Value::as_array) else {
        return out;
    };
    let mut add = |loc: &Value| {
        if loc.get("kind").and_then(Value::as_str) != Some("nas_folder") {
            return;
        }
        let fid = loc.get("filer_id").and_then(|v| {
            v.as_u64()
                .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
        });
        if fid != Some(filer_id) {
            return;
        }
        let Some(p) = loc.get("path").and_then(Value::as_str) else {
            return;
        };
        let p = p.trim_matches('/');
        if super::nasfs::check_rel(p).is_err() {
            return;
        }
        let inside =
            annex_rel.is_empty() || p == annex_rel || p.starts_with(&format!("{annex_rel}/"));
        if !inside && !out.iter().any(|x| x == p) {
            out.push(p.to_owned());
        }
    };
    for r in rules {
        if r.get("trigger").and_then(Value::as_str) != Some("recall_completed") {
            continue;
        }
        for a in r
            .get("actions")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            if let Some(loc) = a.get("place_at") {
                add(loc);
            }
            for loc in a
                .get("let_requester_choose")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                add(loc);
            }
        }
    }
    out
}

fn hash(v: &Value) -> String {
    let d = digest(&SHA256, v.to_string().as_bytes());
    d.as_ref().iter().map(|b| format!("{b:02x}")).collect()
}

/// `fstatvfs` of an open folder: (free bytes for the caller, total bytes).
// The statvfs fields are u32 on some platforms and u64 on others.
#[allow(clippy::unnecessary_cast)]
pub fn space(f: &std::fs::File) -> std::io::Result<(u64, u64)> {
    use std::os::fd::AsRawFd;
    let mut st = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: a valid fd; st is written by fstatvfs on success.
    if unsafe { libc::fstatvfs(f.as_raw_fd(), st.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: fstatvfs returned 0, so st is initialised.
    let st = unsafe { st.assume_init() };
    let unit = st.f_frsize as u64;
    Ok((
        (st.f_bavail as u64).saturating_mul(unit),
        (st.f_blocks as u64).saturating_mul(unit),
    ))
}

/// Folders below `top` (export-relative), `top` first, dirs only, never
/// through a symlink, another device or OmniFile's own names; at most
/// `budget` of them.
fn walk(export: &Root, top: &str, budget: &mut usize, out: &mut Vec<String>) -> bool {
    let mut stack = vec![top.to_owned()];
    while let Some(d) = stack.pop() {
        if *budget == 0 {
            return true;
        }
        // A location not there (yet) has no ACL to send.
        let Ok((_, items)) = export.read_dir(&d) else {
            continue;
        };
        *budget -= 1;
        out.push(d.clone());
        for (name, st) in items {
            let Ok(st) = st else { continue };
            let name = name.to_string_lossy().into_owned();
            if st.kind == Kind::Dir && !own_file(&name) && !name.starts_with(".omnifile-") {
                stack.push(join(&d, &name));
            }
        }
    }
    false
}

/// One pass: read every folder's ACL, send the changes and the free space.
pub fn pass(
    db: &AnnexDb,
    export: &Root,
    annex_rel: &str,
    extra: &[String],
    flavour: Flavour,
    ids: &dyn IdMap,
    now: i64,
) -> rusqlite::Result<FolderStats> {
    run(db, export, annex_rel, extra, flavour, ids, now, true)
}

/// A folder's [`dir_stamp`](super::scan::dir_stamp): from the scan's `dir` row
/// for one in the annex, else stat-ed now. None when it can't be told.
fn folder_stamp(
    db: &AnnexDb,
    export: &Root,
    annex_rel: &str,
    p: &str,
    now: i64,
) -> rusqlite::Result<Option<String>> {
    let inside = if p == annex_rel {
        Some("")
    } else if annex_rel.is_empty() {
        Some(p)
    } else {
        p.strip_prefix(annex_rel).and_then(|r| r.strip_prefix('/'))
    };
    if let Some(rel) = inside
        && let Some(s) = db.dir_stamp(rel)?
    {
        return Ok(Some(s));
    }
    Ok(export
        .stat(p, false)
        .ok()
        .filter(|st| st.is_dir())
        .map(|st| super::scan::dir_stamp(&st, now)))
}

/// After a quick scan: the folders (the annex's, and those above its root) no
/// ACL was sent for yet, or whose inode or ctime changed since it was. Nothing
/// is said gone, and no space is read.
pub fn pass_new(
    db: &AnnexDb,
    export: &Root,
    annex_rel: &str,
    flavour: Flavour,
    ids: &dyn IdMap,
    now: i64,
) -> rusqlite::Result<FolderStats> {
    run(db, export, annex_rel, &[], flavour, ids, now, false)
}

#[allow(clippy::too_many_arguments)]
fn run(
    db: &AnnexDb,
    export: &Root,
    annex_rel: &str,
    extra: &[String],
    flavour: Flavour,
    ids: &dyn IdMap,
    now: i64,
    full: bool,
) -> rusqlite::Result<FolderStats> {
    if db.meta(EPOCH_KEY)?.as_deref() != Some(EPOCH) {
        db.conn().execute("DELETE FROM folder_acl", [])?;
        db.set_meta(EPOCH_KEY, EPOCH)?;
    }
    let mut stats = FolderStats::default();
    let mut folders = ancestors(annex_rel);
    stats.ancestors = folders.len() as u64;
    folders.push(annex_rel.to_owned());
    {
        let mut st = db.conn().prepare("SELECT path FROM dir ORDER BY path")?;
        let rows = st.query_map([], |r| r.get::<_, String>(0))?;
        for p in rows {
            let p = join(annex_rel, &p?);
            if p != annex_rel {
                folders.push(p);
            }
        }
    }
    let mut budget = MAX_EXTRA_DIRS;
    for top in extra.iter().filter(|_| full) {
        if walk(export, top, &mut budget, &mut folders) {
            stats.truncated = true;
            break;
        }
    }
    let at = rfc3339_ms(now);
    let mut seen: HashSet<String> = HashSet::new();
    for p in &folders {
        if !seen.insert(p.clone()) {
            continue;
        }
        // Its inode and ctime: the scan's for a folder in the annex, read now for
        // one above it (a few). Taken before its ACL, so a change after is seen.
        let stamp = folder_stamp(db, export, annex_rel, p, now)?;
        if !full {
            let sent: Option<Option<String>> = db
                .conn()
                .query_row("SELECT stamp FROM folder_acl WHERE path = ?1", [p], |r| {
                    r.get(0)
                })
                .optional()?;
            // Read again only if it was never sent, or it changed since (the
            // review of #83: a chmod, or a folder deleted and made again).
            if let Some(old) = sent
                && stamp.is_some()
                && old == stamp
                && stamp.as_deref() != Some("unsettled")
            {
                continue;
            }
        }
        stats.folders += 1;
        let (ev, h) = match acl::capture_dir(export, p, flavour, ids) {
            Ok((v, _)) => {
                let h = hash(&v);
                (
                    json!({"kind": "folder_acl", "path": p, "acl": v, "observed_at": at}),
                    h,
                )
            }
            Err(e) => {
                stats.unreadable += 1;
                let e: String = e.chars().take(500).collect();
                let h = format!("error:{e}");
                (
                    json!({"kind": "folder_acl", "path": p, "error": e, "observed_at": at}),
                    h,
                )
            }
        };
        let old: Option<String> = db
            .conn()
            .query_row("SELECT hash FROM folder_acl WHERE path = ?1", [p], |r| {
                r.get(0)
            })
            .optional()?;
        if old.as_deref() != Some(h.as_str()) {
            db.push_event("folder_acl", Some(p), &ev)?;
            db.conn().execute(
                "INSERT INTO folder_acl (path, hash, at, stamp) VALUES (?1, ?2, ?3, ?4) \
                 ON CONFLICT (path) DO UPDATE SET hash = ?2, at = ?3, stamp = ?4",
                params![p, h, now, stamp],
            )?;
            stats.changed += 1;
        } else {
            db.conn().execute(
                "UPDATE folder_acl SET stamp = ?2 WHERE path = ?1",
                params![p, stamp],
            )?;
        }
    }
    let read = |db: &AnnexDb, stats: &FolderStats, total: usize| -> rusqlite::Result<()> {
        // Said when something changed, and once after the update.
        if stats.changed + stats.gone > 0 || db.meta(READ_KEY)?.as_deref() != Some(EPOCH) {
            db.push_event(
                "folders_read",
                None,
                &json!({"kind": "folders_read", "observed_at": rfc3339_ms(now),
                        "details": {"folders": total, "unreadable": stats.unreadable,
                                    "ancestors": stats.ancestors, "complete": true}}),
            )?;
            db.set_meta(READ_KEY, EPOCH)?;
        }
        Ok(())
    };
    if !full {
        read(db, &stats, seen.len())?;
        return Ok(stats);
    }
    // Folders no longer there.
    let gone: Vec<String> = {
        let mut st = db.conn().prepare("SELECT path FROM folder_acl")?;
        let rows = st.query_map([], |r| r.get::<_, String>(0))?;
        rows.filter_map(Result::ok)
            .filter(|p| !seen.contains(p))
            .collect()
    };
    for p in gone {
        db.push_event(
            "folder_acl",
            Some(&p),
            &json!({"kind": "folder_acl", "path": p, "gone": true, "observed_at": at}),
        )?;
        db.conn()
            .execute("DELETE FROM folder_acl WHERE path = ?1", [&p])?;
        stats.gone += 1;
    }
    read(db, &stats, seen.len())?;
    // Free space, for previews: the annex root and each extra location.
    let mut tops = vec![annex_rel.to_owned()];
    tops.extend(extra.iter().cloned());
    for t in tops {
        if let Ok((f, _)) = export.open_dir(&t)
            && let Ok((free, total)) = space(&f)
        {
            db.push_event(
                "target_space",
                Some(&t),
                &json!({"kind": "target_space", "path": t, "free_bytes": free,
                        "total_bytes": total, "observed_at": at}),
            )?;
        }
    }
    Ok(stats)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::annex::nasfs::open_root;
    use crate::annex::scan::Scanner;
    use omnifile_identity::fake::FakeIdMap;

    fn events(db: &AnnexDb, kind: &str) -> Vec<Value> {
        db.outbox(10_000)
            .unwrap()
            .into_iter()
            .filter(|(_, k, _, _)| k == kind)
            .map(|(_, _, _, e)| e)
            .collect()
    }

    fn drain(db: &AnnexDb) {
        db.drop_events(i64::MAX).unwrap();
    }

    /// A test clock an hour ahead of the real one, so the folders a test makes
    /// have settled ([`super::super::scan::dir_stamp`]).
    fn at(ms: i64) -> i64 {
        crate::annex::now_ms() + 3_600_000 + ms
    }

    #[test]
    fn folder_acls_are_sent_once_per_change_and_gone_folders_are_said_to_be() {
        let t = tempfile::tempdir().unwrap();
        let annex = t.path().join("geo");
        std::fs::create_dir_all(annex.join("surveys/raw")).unwrap();
        std::fs::create_dir_all(t.path().join("restore/old")).unwrap();
        std::fs::create_dir_all(t.path().join("restore/.omnifile-staging")).unwrap();
        std::fs::write(annex.join("surveys/raw/a.segy"), b"x").unwrap();
        let db = AnnexDb::open_in_memory().unwrap();
        let root = open_root(&annex, "").unwrap();
        Scanner::with_root(&db, Ok(root), 1_000, true)
            .full()
            .unwrap();
        drain(&db);
        let export = open_root(t.path(), "").unwrap();
        let ids = FakeIdMap::default();
        let extra = vec!["restore".to_owned()];
        let s = pass(&db, &export, "geo", &extra, Flavour::Nfs3, &ids, 2_000).unwrap();
        let acls = events(&db, "folder_acl");
        let paths: HashSet<String> = acls
            .iter()
            .map(|e| e["path"].as_str().unwrap().to_owned())
            .collect();
        for p in [
            "",
            "geo",
            "geo/surveys",
            "geo/surveys/raw",
            "restore",
            "restore/old",
        ] {
            assert!(paths.contains(p), "{p} in {paths:?}");
        }
        assert!(!paths.iter().any(|p| p.contains(".omnifile-")), "{paths:?}");
        assert!(acls.iter().all(|e| e["acl"]["kind"] == "posix"));
        assert_eq!(s.changed, s.folders);
        assert_eq!(events(&db, "target_space").len(), 2);
        drain(&db);
        // Nothing changed: nothing sent.
        let s = pass(&db, &export, "geo", &extra, Flavour::Nfs3, &ids, 3_000).unwrap();
        assert_eq!((s.changed, s.gone), (0, 0));
        assert!(events(&db, "folder_acl").is_empty());
        drain(&db);
        // A changed mode is a new ACL; a vanished folder is gone.
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(
            t.path().join("restore"),
            std::fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        std::fs::remove_dir(t.path().join("restore/old")).unwrap();
        let s = pass(&db, &export, "geo", &extra, Flavour::Nfs3, &ids, 4_000).unwrap();
        assert_eq!((s.changed, s.gone), (1, 1));
        let acls = events(&db, "folder_acl");
        assert!(
            acls.iter()
                .any(|e| e["path"] == "restore" && e["acl"]["mode"] == 0o700)
        );
        assert!(
            acls.iter()
                .any(|e| e["path"] == "restore/old" && e["gone"] == true)
        );
    }

    #[test]
    fn the_folders_above_an_annex_root_go_up_to_the_exports_root() {
        assert!(ancestors("").is_empty());
        assert_eq!(ancestors("geo"), vec![String::new()]);
        assert_eq!(
            ancestors("projects/geo/raw"),
            vec![
                "".to_owned(),
                "projects".to_owned(),
                "projects/geo".to_owned()
            ]
        );
    }

    fn setup() -> (tempfile::TempDir, AnnexDb, Root) {
        let t = tempfile::tempdir().unwrap();
        let annex = t.path().join("projects/geo");
        std::fs::create_dir_all(annex.join("surveys")).unwrap();
        std::fs::write(annex.join("surveys/a.segy"), b"x").unwrap();
        let db = AnnexDb::open_in_memory().unwrap();
        let root = open_root(&annex, "").unwrap();
        Scanner::with_root(&db, Ok(root), at(1_000), true)
            .full()
            .unwrap();
        drain(&db);
        let export = open_root(t.path(), "").unwrap();
        (t, db, export)
    }

    #[test]
    fn a_pass_reads_the_folders_above_the_annex_and_says_when_it_has_them_all() {
        // Decision 63: a file's readers are only those every folder above it lets
        // through, up to the export's root.
        let (_t, db, export) = setup();
        let ids = FakeIdMap::default();
        let s = pass(
            &db,
            &export,
            "projects/geo",
            &[],
            Flavour::Nfs3,
            &ids,
            at(2_000),
        )
        .unwrap();
        assert_eq!(s.ancestors, 2);
        let paths: HashSet<String> = events(&db, "folder_acl")
            .iter()
            .map(|e| e["path"].as_str().unwrap().to_owned())
            .collect();
        for p in ["", "projects", "projects/geo", "projects/geo/surveys"] {
            assert!(paths.contains(p), "{p:?} in {paths:?}");
        }
        let read = events(&db, "folders_read");
        assert_eq!(read.len(), 1);
        assert_eq!(read[0]["details"]["complete"], true);
        assert_eq!(read[0]["details"]["folders"], 4);
        assert_eq!(read[0]["details"]["ancestors"], 2);
        drain(&db);
        // Nothing changed: no word.
        pass(
            &db,
            &export,
            "projects/geo",
            &[],
            Flavour::Nfs3,
            &ids,
            at(3_000),
        )
        .unwrap();
        assert!(events(&db, "folders_read").is_empty());
    }

    #[test]
    fn after_a_quick_scan_only_new_folders_are_read() {
        let (t, db, export) = setup();
        let ids = FakeIdMap::default();
        pass(
            &db,
            &export,
            "projects/geo",
            &[],
            Flavour::Nfs3,
            &ids,
            at(2_000),
        )
        .unwrap();
        drain(&db);
        std::fs::create_dir_all(t.path().join("projects/geo/new")).unwrap();
        std::fs::write(t.path().join("projects/geo/new/b.segy"), b"y").unwrap();
        std::fs::remove_dir_all(t.path().join("projects/geo/surveys")).unwrap();
        let root = open_root(&t.path().join("projects/geo"), "").unwrap();
        Scanner::with_root(&db, Ok(root), at(2_500), false)
            .quick()
            .unwrap();
        drain(&db);
        let s = pass_new(&db, &export, "projects/geo", Flavour::Nfs3, &ids, at(3_000)).unwrap();
        let acls = events(&db, "folder_acl");
        // The new folder, and the annex root, whose ctime its creation moved.
        assert_eq!(s.folders, 2, "{acls:?}");
        assert_eq!(acls.len(), 1);
        assert_eq!(acls[0]["path"], "projects/geo/new");
        // Gone folders are the full pass's to say.
        assert!(!acls.iter().any(|e| e["gone"] == true));
        assert_eq!(events(&db, "folders_read").len(), 1);
        assert!(events(&db, "target_space").is_empty());
    }

    fn mode(t: &tempfile::TempDir, rel: &str, m: u32) {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(t.path().join(rel), std::fs::Permissions::from_mode(m)).unwrap();
    }

    fn quick_then_new(t: &tempfile::TempDir, db: &AnnexDb, export: &Root, now: i64) -> u64 {
        let ids = FakeIdMap::default();
        let root = open_root(&t.path().join("projects/geo"), "").unwrap();
        Scanner::with_root(db, Ok(root), now, false)
            .quick()
            .unwrap();
        pass_new(db, export, "projects/geo", Flavour::Nfs3, &ids, now)
            .unwrap()
            .folders
    }

    #[test]
    fn a_folder_chmod_is_read_after_the_next_quick_scan() {
        // The review of #83: a chmod moves a folder's ctime, not its mtime, so the
        // quick scan doesn't list it again; its ACL is read again all the same.
        let (t, db, export) = setup();
        pass(
            &db,
            &export,
            "projects/geo",
            &[],
            Flavour::Nfs3,
            &FakeIdMap::default(),
            at(2_000),
        )
        .unwrap();
        drain(&db);
        assert_eq!(quick_then_new(&t, &db, &export, at(2_500)), 0);
        mode(&t, "projects/geo/surveys", 0o700);
        assert_eq!(quick_then_new(&t, &db, &export, at(3_000)), 1);
        let acls = events(&db, "folder_acl");
        assert_eq!(acls.len(), 1, "{acls:?}");
        assert_eq!(acls[0]["path"], "projects/geo/surveys");
        assert_eq!(acls[0]["acl"]["mode"], 0o700);
        drain(&db);
        assert_eq!(quick_then_new(&t, &db, &export, at(3_500)), 0);
        // One above the annex's root, which no scan lists: stat-ed by the pass.
        mode(&t, "projects", 0o750);
        assert_eq!(quick_then_new(&t, &db, &export, at(4_000)), 1);
        assert_eq!(events(&db, "folder_acl")[0]["path"], "projects");
        drain(&db);
        // A chmod and a new file in the same folder: the quick scan lists it again,
        // and the stamp it takes then is what the pass compares.
        assert_eq!(quick_then_new(&t, &db, &export, at(4_500)), 0);
        mode(&t, "projects/geo/surveys", 0o750);
        std::fs::write(t.path().join("projects/geo/surveys/c.segy"), b"z").unwrap();
        assert_eq!(quick_then_new(&t, &db, &export, at(5_000)), 1);
        assert_eq!(events(&db, "folder_acl")[0]["acl"]["mode"], 0o750);
    }

    #[test]
    fn a_folder_deleted_and_made_again_is_read_again() {
        let (t, db, export) = setup();
        pass(
            &db,
            &export,
            "projects/geo",
            &[],
            Flavour::Nfs3,
            &FakeIdMap::default(),
            at(2_000),
        )
        .unwrap();
        drain(&db);
        std::fs::remove_dir_all(t.path().join("projects/geo/surveys")).unwrap();
        std::fs::create_dir(t.path().join("projects/geo/surveys")).unwrap();
        mode(&t, "projects/geo/surveys", 0o700);
        // It, and its parent, whose ctime moved too (its ACL is the same: not sent).
        assert_eq!(quick_then_new(&t, &db, &export, at(3_000)), 2);
        let acls = events(&db, "folder_acl");
        assert_eq!(acls.len(), 1, "{acls:?}");
        assert_eq!(acls[0]["acl"]["mode"], 0o700);
    }

    #[test]
    fn a_folder_changed_moments_ago_is_read_again_until_it_settles() {
        // Its ctime and ACL may come from the client's cache then.
        let (t, db, export) = setup();
        pass(
            &db,
            &export,
            "projects/geo",
            &[],
            Flavour::Nfs3,
            &FakeIdMap::default(),
            at(2_000),
        )
        .unwrap();
        drain(&db);
        mode(&t, "projects/geo/surveys", 0o700);
        // On the real clock every folder of the test changed moments ago.
        let now = crate::annex::now_ms();
        let first = quick_then_new(&t, &db, &export, now);
        assert!(first >= 1);
        assert_eq!(
            db.dir_stamp("surveys").unwrap().as_deref(),
            Some("unsettled")
        );
        assert_eq!(
            quick_then_new(&t, &db, &export, now + 1),
            first,
            "read again while unsettled"
        );
        assert_eq!(
            quick_then_new(&t, &db, &export, at(9_000)),
            first,
            "settled: once more"
        );
        assert_eq!(quick_then_new(&t, &db, &export, at(9_500)), 0);
    }

    #[test]
    fn after_the_update_every_folder_is_sent_again_once() {
        let (_t, db, export) = setup();
        let ids = FakeIdMap::default();
        pass(
            &db,
            &export,
            "projects/geo",
            &[],
            Flavour::Nfs3,
            &ids,
            at(2_000),
        )
        .unwrap();
        drain(&db);
        // As an annex.db from before decision 63 would be.
        db.set_meta(EPOCH_KEY, "51").unwrap();
        db.set_meta(READ_KEY, "51").unwrap();
        let s = pass_new(&db, &export, "projects/geo", Flavour::Nfs3, &ids, at(3_000)).unwrap();
        assert_eq!(s.folders, 4);
        assert_eq!(events(&db, "folder_acl").len(), 4);
        assert_eq!(events(&db, "folders_read").len(), 1);
        drain(&db);
        let s = pass_new(&db, &export, "projects/geo", Flavour::Nfs3, &ids, at(4_000)).unwrap();
        assert_eq!(s.folders, 0);
        assert!(events(&db, "folder_acl").is_empty());
    }

    #[test]
    fn only_this_filers_return_locations_outside_the_annex_are_walked() {
        let body = json!({"rules": [
            {"id": "copy", "trigger": "arrived", "actions": [{"copy_to": {}}]},
            {"id": "b", "trigger": "recall_completed", "conditions": {"size_gte": 1},
             "actions": [{"place_at": {"kind": "nas_folder", "filer_id": "7", "path": "restore"}},
                         {"let_requester_choose": [
                             {"kind": "nas_folder", "filer_id": 7, "path": "geo/in"},
                             {"kind": "nas_folder", "filer_id": 8, "path": "elsewhere"},
                             {"kind": "share_folder", "share_id": 1, "path": "r"},
                             {"kind": "nas_folder", "filer_id": 7, "path": "../up"}]}]},
            {"id": "c", "trigger": "recall_completed",
             "actions": [{"place_at": {"kind": "nas_folder", "origin": true}}]}]});
        assert_eq!(
            extra_locations(Some(&body), 7, "geo"),
            vec!["restore".to_owned()]
        );
        assert!(extra_locations(None, 7, "geo").is_empty());
    }

    #[test]
    fn the_walk_below_extra_locations_is_bounded() {
        let t = tempfile::tempdir().unwrap();
        for i in 0..5 {
            std::fs::create_dir_all(t.path().join(format!("r/{i}"))).unwrap();
        }
        let export = open_root(t.path(), "").unwrap();
        let mut out = Vec::new();
        let mut budget = 3;
        assert!(walk(&export, "r", &mut budget, &mut out));
        assert_eq!(out.len(), 3);
    }
}
