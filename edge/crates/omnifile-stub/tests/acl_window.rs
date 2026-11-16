//! M2 review S4: a new placeholder or directory has its final permissions
//! (or owner-only) from the moment its name exists, so no user can open it
//! under looser permissions and keep that fd after the ACL lands. Linux, as
//! root, on xfs (POSIX ACLs):
//!
//! ```text
//! sudo -E cargo test -p omnifile-stub --test acl_window
//! ```
#![cfg(target_os = "linux")]

use std::fs::File;
use std::os::fd::AsFd;
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use omnifile_marker::Flags;
use omnifile_marker::xattr;
use omnifile_replica::{Kind, Node, Replica};
use omnifile_stub::materialize::{Materializer, PermsSource, run_redo};
use omnifile_stub::placeholder::{
    Created, NewPerms, PlaceholderSpec, XATTR_ACL_ACCESS, XATTR_ACL_DEFAULT, create_dir_with_perms,
    create_placeholder_with_perms,
};
use omnifile_stub::{FileLocks, FsalNotifier, InodeKey, Layout, MarkerSeq, sys};
use serde_json::json;

fn env() -> Option<(tempfile::TempDir, Layout)> {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("skipping: needs root");
        return None;
    }
    let base =
        std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/omnifile/data/test".into());
    std::fs::create_dir_all(&base).ok()?;
    if !sys::is_xfs(File::open(&base).ok()?.as_fd()).unwrap_or(false) {
        eprintln!("skipping: {base} is not xfs");
        return None;
    }
    let dir = tempfile::tempdir_in(&base).ok()?;
    let layout = Layout::new(dir.path().join("data"));
    std::fs::create_dir_all(layout.path(Path::new("proj"))).unwrap();
    Some((dir, layout))
}

/// `system.posix_acl_*` encoding: version 2, then (tag u16, perm u16, id u32).
fn acl(entries: &[(u16, u16, u32)]) -> Vec<u8> {
    let mut v = 2u32.to_le_bytes().to_vec();
    for (t, p, id) in entries {
        v.extend(t.to_le_bytes());
        v.extend(p.to_le_bytes());
        v.extend(id.to_le_bytes());
    }
    v
}
const U: u32 = u32::MAX;
const USER_OBJ: u16 = 0x01;
const GROUP_OBJ: u16 = 0x04;
const GROUP: u16 = 0x08;
const MASK: u16 = 0x10;
const OTHER: u16 = 0x20;

/// The catalog ACL: owner rw, group:30001 r, others nothing.
fn catalog_acl() -> Vec<u8> {
    acl(&[
        (USER_OBJ, 6, U),
        (GROUP_OBJ, 0, U),
        (GROUP, 4, 30001),
        (MASK, 4, U),
        (OTHER, 0, U),
    ])
}

fn get(p: &Path, name: &str) -> Option<Vec<u8>> {
    let fd = sys::open_ro(p).or_else(|_| sys::open_dir(p)).unwrap();
    xattr::get_raw(fd.as_fd(), name, 4096).unwrap()
}

fn spec(file_id: u64, mode: u32) -> PlaceholderSpec {
    PlaceholderSpec {
        file_id,
        version_id: 1,
        size: 4096,
        share_id: 1,
        flags: Flags::ONLINE,
        uid: 1234,
        gid: 5678,
        mode,
        mtime_ns: 1_700_000_000_000_000_000,
    }
}

#[test]
fn placeholder_has_its_acl_when_its_name_appears() {
    let Some((_d, layout)) = env() else { return };
    // Watch the share directory: the first moment the name exists, its
    // permissions must already be the final ones (never 0666).
    let stop = Arc::new(AtomicBool::new(false));
    let seen = Arc::new(Mutex::new(Vec::<u32>::new()));
    let p = layout.path(Path::new("proj/a.segy"));
    let watcher = {
        let (stop, seen, p) = (stop.clone(), seen.clone(), p.clone());
        std::thread::spawn(move || {
            while !stop.load(Ordering::SeqCst) {
                if let Ok(md) = std::fs::symlink_metadata(&p) {
                    seen.lock().unwrap().push(md.mode() & 0o7777);
                }
            }
        })
    };
    let perms = NewPerms::Acl {
        access: catalog_acl(),
        default: None,
    };
    let r = create_placeholder_with_perms(
        &layout,
        Path::new("proj/a.segy"),
        &spec(7, 0o666),
        1,
        &perms,
    )
    .unwrap();
    stop.store(true, Ordering::SeqCst);
    watcher.join().unwrap();
    assert!(matches!(r, Created::Created(_)));
    assert_eq!(get(&p, XATTR_ACL_ACCESS), Some(catalog_acl()));
    let md = std::fs::metadata(&p).unwrap();
    assert_eq!(
        (md.uid(), md.gid(), md.mode() & 0o7777),
        (1234, 5678, 0o640)
    );
    for m in seen.lock().unwrap().iter() {
        assert_eq!(*m, 0o640, "the name was visible with mode {m:o}");
    }
}

#[test]
fn owner_only_until_the_acl_can_be_mapped() {
    let Some((_d, layout)) = env() else { return };
    let rel = Path::new("proj/b.segy");
    create_placeholder_with_perms(&layout, rel, &spec(8, 0o666), 1, &NewPerms::OwnerOnly).unwrap();
    let md = std::fs::metadata(layout.path(rel)).unwrap();
    assert_eq!(md.mode() & 0o7777, 0o600);
    assert_eq!(get(&layout.path(rel), XATTR_ACL_ACCESS), None);
}

#[test]
fn new_directory_gets_its_acl_before_its_mode_opens() {
    let Some((_d, layout)) = env() else { return };
    let def = acl(&[
        (USER_OBJ, 7, U),
        (GROUP_OBJ, 5, U),
        (GROUP, 5, 30001),
        (MASK, 5, U),
        (OTHER, 0, U),
    ]);
    let perms = NewPerms::Acl {
        access: def.clone(),
        default: Some(def.clone()),
    };
    let rel = Path::new("proj/sub");
    let r = create_dir_with_perms(&layout, rel, 9, 1234, 5678, 0o777, 0, &perms).unwrap();
    assert!(matches!(r, Created::Created(_)));
    let p = layout.path(rel);
    assert_eq!(get(&p, XATTR_ACL_ACCESS), Some(def.clone()));
    assert_eq!(get(&p, XATTR_ACL_DEFAULT), Some(def));
    // 0777 was asked for, but the ACL decides: other has nothing.
    assert_eq!(std::fs::metadata(&p).unwrap().mode() & 0o777, 0o750);
    // Owner-only directory: closed even if the parent has a default ACL.
    let r = create_dir_with_perms(
        &layout,
        Path::new("proj/sub/inner"),
        10,
        1234,
        5678,
        0o777,
        0,
        &NewPerms::OwnerOnly,
    )
    .unwrap();
    assert!(matches!(r, Created::Created(_)));
    let inner = layout.path(Path::new("proj/sub/inner"));
    assert_eq!(std::fs::metadata(&inner).unwrap().mode() & 0o077, 0);
}

// --- Through the materializer: the source decides, from the node and the
// directory's default ACL.

struct NoFsal;
impl FsalNotifier for NoFsal {
    async fn invalidate(&self, _: InodeKey, _: u32, _: bool) -> bool {
        true
    }
    fn fill_progress(&self, _: InodeKey, _: u64, _: u64) {}
    async fn begin_fill(&self, _: InodeKey, _: u64) -> Option<u64> {
        Some(0)
    }
    fn fence_holds(&self, _: u64) -> bool {
        true
    }
}

struct Recorder(Mutex<Vec<(String, Option<Vec<u8>>)>>);
impl PermsSource for Recorder {
    fn new_perms(&self, node: &Node, parent_default: Option<&[u8]>) -> NewPerms {
        self.0
            .lock()
            .unwrap()
            .push((node.name.clone(), parent_default.map(<[u8]>::to_vec)));
        match node.kind {
            Kind::File => NewPerms::Acl {
                access: catalog_acl(),
                default: None,
            },
            _ => NewPerms::OwnerOnly,
        }
    }
}

#[tokio::test]
async fn materializer_asks_the_source_before_creating() {
    let Some((d, layout)) = env() else { return };
    let mut r = Replica::open(&d.path().join("replica.db")).unwrap();
    let ev = |id: u64, parent: Option<u64>, name: &str, kind: &str, version: u64| {
        json!({"type": "file.upsert", "entity_id": id.to_string(), "entity_version": "1",
               "file": {"file_id": id.to_string(), "dataset_id": "1",
                        "parent_id": parent.map(|p| p.to_string()),
                        "name": name, "kind": kind, "size": 4096, "mtime_ns": "1700000000000000000",
                        "uid": 0, "gid": 0, "mode": 0o666,
                        "version_id": version.to_string(),
                        "location": {"backend_kind": "s3", "container": "b", "object_key": "k",
                                     "logical_tier": "hot", "archived": false, "restore_state": "none"},
                        "entity_version": "1"}})
    };
    r.apply_page(
        &[
            json!({"type": "share.upsert", "entity_id": "1", "entity_version": "1",
                   "share": {"share_id": "1", "dataset_id": "1", "name": "proj", "root_dir_id": "100",
                             "on_open": "wait", "worm": {"enabled": false}, "entity_version": "1"}}),
            ev(100, None, "", "dir", 1),
            ev(101, Some(100), "d", "dir", 1),
            ev(102, Some(101), "f.segy", "file", 5),
        ],
        "1.0",
    )
    .unwrap();
    let (seq, locks, src) = (
        MarkerSeq::new(),
        FileLocks::new(),
        Recorder(Mutex::new(vec![])),
    );
    let m = Materializer {
        layout: &layout,
        notifier: &NoFsal,
        seq: &seq,
        locks: &locks,
        perms: &src,
    };
    run_redo(&mut r, &m, 100, 10).await.unwrap();
    let f = layout.path(Path::new("proj/d/f.segy"));
    assert_eq!(get(&f, XATTR_ACL_ACCESS), Some(catalog_acl()));
    assert_eq!(std::fs::metadata(&f).unwrap().mode() & 0o777, 0o640);
    let dmode = std::fs::metadata(layout.path(Path::new("proj/d")))
        .unwrap()
        .mode();
    assert_eq!(dmode & 0o077, 0, "owner-only directory");
    let asked: Vec<String> = src
        .0
        .lock()
        .unwrap()
        .iter()
        .map(|(n, _)| n.clone())
        .collect();
    assert!(
        asked.contains(&"d".to_string()) && asked.contains(&"f.segy".to_string()),
        "{asked:?}"
    );
}
