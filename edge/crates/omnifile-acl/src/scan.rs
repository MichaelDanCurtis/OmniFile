//! ACL scans (nightly full, and on-change batches) and drift detection.
//!
//! A scan walks a dataset's tree without following symlinks or crossing
//! filesystems, snapshots every inode's ACL, and produces:
//! - the distinct ACL bodies for `POST …/acls` (one per sha256);
//! - per-inode `acl_sha256` values for `changes` ops (only the ones that
//!   changed since the last report, through [`DriftTracker`]);
//! - findings: `security.NTACL` present (vfs_acl_xattr was used), unreadable
//!   entries.
//!
//! The scan never writes. Explorer edits are reported, not reverted
//! (access-control.md §4).

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use omnifile_identity::IdMap;

use crate::posix::{Perms, Tag};
use crate::snapshot::{AclSnapshot, AclsRequest, SnapshotError, snapshot};
use crate::sys::{AclFs, Kind};

/// Inode identity on the edge (the replica keys local refs the same way).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct InodeKey {
    pub dev: u64,
    pub ino: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ScannedFile {
    pub path: PathBuf,
    pub key: InodeKey,
    pub acl_sha256: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Finding {
    /// `security.NTACL` on this path: a Windows ACL NFS never enforces.
    NtAcl(PathBuf),
    /// Could not read (vanished during the scan, permission, bad xattr).
    Unreadable(PathBuf, String),
}

#[derive(Clone, Debug, Default)]
pub struct ScanResult {
    pub files: Vec<ScannedFile>,
    /// sha256 → body, each distinct ACL once.
    pub acls: BTreeMap<String, AclSnapshot>,
    pub findings: Vec<Finding>,
    /// For `acl-scans/{id}/complete`.
    pub entries_seen: u64,
}

impl ScanResult {
    /// `POST /v0/edges/{id}/acls` bodies, at most `chunk` ACLs each.
    pub fn acl_requests(&self, dataset_id: u64, chunk: usize) -> Vec<AclsRequest> {
        let all: Vec<AclSnapshot> = self.acls.values().cloned().collect();
        all.chunks(chunk.max(1))
            .map(|c| AclsRequest {
                dataset_id: dataset_id.to_string(),
                acls: c.to_vec(),
            })
            .collect()
    }
}

/// uid, gid, mode, access ACL text, default ACL text.
type AclKey = (u32, u32, u32, Option<String>, Option<String>);

/// Walks `root` (depth-first, sorted, no symlink following, same device).
/// `skip` names to ignore (e.g. `.snapshots`, the daemon's private dirs).
pub fn scan(
    fs: &dyn AclFs,
    idmap: &dyn IdMap,
    root: &Path,
    skip: &[&str],
) -> Result<ScanResult, SnapshotError> {
    let mut out = ScanResult::default();
    let mut stack = vec![root.to_path_buf()];
    let mut root_dev = None;
    let mut by_acl: HashMap<AclKey, String> = HashMap::new();
    while let Some(p) = stack.pop() {
        let f = match fs.read(&p) {
            Ok(f) => f,
            Err(e) => {
                out.findings.push(Finding::Unreadable(p, e.to_string()));
                continue;
            }
        };
        if *root_dev.get_or_insert(f.dev) != f.dev {
            continue; // a mount below the dataset root
        }
        out.entries_seen += 1;
        if f.has_ntacl {
            out.findings.push(Finding::NtAcl(p.clone()));
        }
        if f.kind != Kind::Symlink {
            // Most files share an ACL: reuse the snapshot when uid, gid, mode
            // and both ACLs are byte-identical.
            let k = (
                f.uid,
                f.gid,
                f.mode,
                f.access.as_ref().map(|a| a.to_text()),
                f.default.as_ref().map(|a| a.to_text()),
            );
            let sha = match by_acl.get(&k) {
                Some(s) => s.clone(),
                None => {
                    let s = snapshot(&f, idmap)?;
                    let sha = s.sha256.clone();
                    out.acls.entry(sha.clone()).or_insert(s);
                    by_acl.insert(k, sha.clone());
                    sha
                }
            };
            out.files.push(ScannedFile {
                path: p.clone(),
                key: InodeKey {
                    dev: f.dev,
                    ino: f.ino,
                },
                acl_sha256: sha,
            });
        }
        if f.kind == Kind::Dir {
            match fs.read_dir(&p) {
                Ok(children) => {
                    for c in children.into_iter().rev() {
                        let name = c.file_name().and_then(|n| n.to_str()).unwrap_or("");
                        if !skip.contains(&name) {
                            stack.push(c);
                        }
                    }
                }
                Err(e) => out.findings.push(Finding::Unreadable(p, e.to_string())),
            }
        }
    }
    Ok(out)
}

/// Remembers the last `acl_sha256` reported per inode, so a scan reports only
/// what changed (an Explorer edit, a `setfacl`, a chmod or chown).
#[derive(Clone, Debug, Default)]
pub struct DriftTracker {
    last: HashMap<InodeKey, String>,
}

impl DriftTracker {
    pub fn new() -> Self {
        DriftTracker::default()
    }

    /// Seeds from what the catalog already has (e.g. the replica).
    pub fn seed(&mut self, key: InodeKey, sha: String) {
        self.last.insert(key, sha);
    }

    /// Files whose ACL changed since the last call (or were never reported),
    /// and records them as reported.
    pub fn changed<'a>(&mut self, scan: &'a ScanResult) -> Vec<&'a ScannedFile> {
        let mut v = Vec::new();
        for f in &scan.files {
            if self.last.get(&f.key) != Some(&f.acl_sha256) {
                self.last.insert(f.key, f.acl_sha256.clone());
                v.push(f);
            }
        }
        v
    }

    pub fn forget(&mut self, key: &InodeKey) {
        self.last.remove(key);
    }

    pub fn len(&self) -> usize {
        self.last.len()
    }

    pub fn is_empty(&self) -> bool {
        self.last.is_empty()
    }
}

/// After a share's group changes (e.g. a new Readers group), rewrites the
/// inherited entries below `root`: every `group:<from>` entry, in access and
/// default ACLs, becomes `group:<to>` with the same rights (or is removed when
/// `to` is `None`). Entries that name other ids are untouched. Returns the
/// number of inodes rewritten.
pub fn retarget_group(
    fs: &dyn AclFs,
    root: &Path,
    from: u32,
    to: Option<u32>,
) -> std::io::Result<u64> {
    let mut n = 0;
    let mut stack = vec![root.to_path_buf()];
    let mut root_dev = None;
    while let Some(p) = stack.pop() {
        let f = fs.read(&p)?;
        if *root_dev.get_or_insert(f.dev) != f.dev || f.kind == Kind::Symlink {
            continue;
        }
        let fix = |a: &crate::posix::PosixAcl| -> Option<crate::posix::PosixAcl> {
            let p: Perms = a.get(Tag::Group(from))?;
            let mut b = a.clone();
            b.remove(Tag::Group(from));
            if let Some(t) = to {
                let keep = b.get(Tag::Group(t)).map(|q| q.bits()).unwrap_or(0);
                b.set(Tag::Group(t), Perms(keep | p.bits()));
            }
            // Keep the mask the object had unless no named entries remain.
            if !b.has_named() {
                b.remove(Tag::Mask);
            }
            Some(b)
        };
        let mut touched = false;
        if let Some(new) = f.access.as_ref().and_then(fix) {
            fs.write_access_at(&p, (f.dev, f.ino), &new)?;
            touched = true;
        }
        if let Some(new) = f.default.as_ref().and_then(fix) {
            fs.write_default_at(&p, (f.dev, f.ino), Some(&new))?;
            touched = true;
        }
        n += touched as u64;
        if f.kind == Kind::Dir {
            stack.extend(fs.read_dir(&p)?);
        }
    }
    Ok(n)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::posix::PosixAcl;
    use crate::share::{ShareGroups, apply_root, check_root};
    use crate::sys::FileAcl;
    use omnifile_identity::fake::FakeIdMap;
    use std::io;
    use std::sync::Mutex;

    /// An in-memory filesystem that applies default-ACL inheritance like the
    /// kernel does on create (for macOS tests; Linux tests use the real one).
    #[derive(Default)]
    pub struct MemFs {
        pub nodes: Mutex<BTreeMap<PathBuf, FileAcl>>,
        next_ino: Mutex<u64>,
    }

    impl MemFs {
        pub fn mkdir(&self, p: &str, uid: u32, gid: u32) {
            self.create(p, Kind::Dir, uid, gid, 0o777);
        }
        pub fn touch(&self, p: &str, uid: u32, gid: u32) {
            self.create(p, Kind::File, uid, gid, 0o666);
        }
        fn create(&self, p: &str, kind: Kind, uid: u32, gid: u32, req_mode: u32) {
            let p = PathBuf::from(p);
            let parent_default = p
                .parent()
                .and_then(|pp| self.nodes.lock().unwrap().get(pp).cloned())
                .and_then(|f| f.default);
            let ino = {
                let mut n = self.next_ino.lock().unwrap();
                *n += 1;
                *n
            };
            let (mode, access, default) = match parent_default {
                Some(d) => {
                    // posix_acl_create: the default ACL masked by the create mode.
                    let mut a = d.clone();
                    let m = |a: &PosixAcl, t, bits: u32| {
                        a.get(t).map(|p| Perms(p.bits() & bits as u8)).unwrap()
                    };
                    let u = m(&a, Tag::UserObj, (req_mode >> 6) & 7);
                    let o = m(&a, Tag::Other, req_mode & 7);
                    let gt = if a.get(Tag::Mask).is_some() {
                        Tag::Mask
                    } else {
                        Tag::GroupObj
                    };
                    let g = m(&a, gt, (req_mode >> 3) & 7);
                    a.set(Tag::UserObj, u);
                    a.set(Tag::Other, o);
                    a.set(gt, g);
                    (a.mode_bits(), Some(a), (kind == Kind::Dir).then_some(d))
                }
                None => (req_mode & 0o755, None, None),
            };
            self.nodes.lock().unwrap().insert(
                p,
                FileAcl {
                    dev: 1,
                    ino,
                    kind,
                    uid,
                    gid,
                    mode,
                    access,
                    default,
                    has_ntacl: false,
                },
            );
        }
    }

    impl AclFs for MemFs {
        fn read(&self, path: &Path) -> io::Result<FileAcl> {
            self.nodes
                .lock()
                .unwrap()
                .get(path)
                .cloned()
                .ok_or_else(|| io::ErrorKind::NotFound.into())
        }
        fn write_access(&self, path: &Path, acl: &PosixAcl) -> io::Result<()> {
            acl.validate()
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;
            let mut n = self.nodes.lock().unwrap();
            let f = n.get_mut(path).ok_or(io::ErrorKind::NotFound)?;
            f.mode = (f.mode & !0o777) | acl.mode_bits();
            f.access = (!acl.is_minimal()).then(|| acl.clone());
            Ok(())
        }
        fn write_default(&self, path: &Path, acl: Option<&PosixAcl>) -> io::Result<()> {
            let mut n = self.nodes.lock().unwrap();
            let f = n.get_mut(path).ok_or(io::ErrorKind::NotFound)?;
            f.default = acl.cloned();
            Ok(())
        }
        fn chown(&self, path: &Path, uid: Option<u32>, gid: Option<u32>) -> io::Result<()> {
            let mut n = self.nodes.lock().unwrap();
            let f = n.get_mut(path).ok_or(io::ErrorKind::NotFound)?;
            if let Some(u) = uid {
                f.uid = u;
            }
            if let Some(g) = gid {
                f.gid = g;
            }
            Ok(())
        }
        fn read_dir(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
            Ok(self
                .nodes
                .lock()
                .unwrap()
                .keys()
                .filter(|k| k.parent() == Some(path))
                .cloned()
                .collect())
        }
    }

    const G: ShareGroups = ShareGroups {
        owners: 30003,
        writers: Some(30002),
        readers: Some(30001),
    };

    fn share() -> MemFs {
        let fs = MemFs::default();
        fs.mkdir("/s", 0, 0);
        apply_root(&fs, Path::new("/s"), &G, None).unwrap();
        fs.mkdir("/s/proj", 20002, 10000);
        fs.touch("/s/proj/line_1042.segy", 20002, 10000);
        fs.touch("/s/readme.txt", 20005, 10000);
        fs
    }

    #[test]
    fn apply_then_inherit_then_scan() {
        let fs = share();
        let root = fs.read(Path::new("/s")).unwrap();
        assert!(check_root(&root, &G).is_clean());
        assert_eq!(root.gid, 30003);
        // A new file inherits the share groups; Domain Users (owning group)
        // gets nothing.
        let f = fs.read(Path::new("/s/proj/line_1042.segy")).unwrap();
        let a = f.access.clone().unwrap();
        assert_eq!(a.effective(Tag::Group(30001)), Some(Perms::R));
        assert_eq!(a.effective(Tag::Group(30002)), Some(Perms::RW));
        assert_eq!(a.effective(Tag::GroupObj), Some(Perms::NONE));
        assert_eq!(a.get(Tag::Other), Some(Perms::NONE));

        let m = FakeIdMap::dev_ad();
        let r = scan(&fs, &m, Path::new("/s"), &[]).unwrap();
        assert_eq!(r.entries_seen, 4);
        assert_eq!(r.files.len(), 4);
        assert!(r.findings.is_empty());
        // Root, the subdir (different owner, same ACL → different sha), and
        // two files with different owners.
        assert!(r.acls.len() >= 3);
        let reqs = r.acl_requests(7, 2);
        assert_eq!(
            reqs.iter().map(|q| q.acls.len()).sum::<usize>(),
            r.acls.len()
        );
        assert!(reqs.iter().all(|q| q.dataset_id == "7"));
    }

    #[test]
    fn drift_reports_only_changes() {
        let fs = share();
        let m = FakeIdMap::dev_ad();
        let mut t = DriftTracker::new();
        let first = scan(&fs, &m, Path::new("/s"), &[]).unwrap();
        assert_eq!(t.changed(&first).len(), 4);
        let again = scan(&fs, &m, Path::new("/s"), &[]).unwrap();
        assert!(t.changed(&again).is_empty());

        // An Explorer edit: deny-by-removal of Readers on one file.
        let p = Path::new("/s/proj/line_1042.segy");
        let mut a = fs.read(p).unwrap().access.unwrap();
        a.remove(Tag::Group(30001));
        fs.write_access(p, &a).unwrap();
        let after = scan(&fs, &m, Path::new("/s"), &[]).unwrap();
        let ch = t.changed(&after);
        assert_eq!(ch.len(), 1);
        assert_eq!(ch[0].path, p);
        assert!(after.acls.contains_key(&ch[0].acl_sha256));
    }

    #[test]
    fn ntacl_and_skip() {
        let fs = share();
        fs.nodes
            .lock()
            .unwrap()
            .get_mut(Path::new("/s/readme.txt"))
            .unwrap()
            .has_ntacl = true;
        fs.mkdir("/s/.snapshots", 0, 0);
        let m = FakeIdMap::dev_ad();
        let r = scan(&fs, &m, Path::new("/s"), &[".snapshots"]).unwrap();
        assert_eq!(r.entries_seen, 4);
        assert_eq!(
            r.findings,
            vec![Finding::NtAcl(PathBuf::from("/s/readme.txt"))]
        );
    }

    #[test]
    fn group_change_retargets_inherited_entries() {
        let fs = share();
        let new = ShareGroups {
            readers: Some(30005),
            ..G
        };
        apply_root(&fs, Path::new("/s"), &new, Some(&G)).unwrap();
        assert!(check_root(&fs.read(Path::new("/s")).unwrap(), &new).is_clean());
        let n = retarget_group(&fs, Path::new("/s"), 30001, Some(30005)).unwrap();
        assert_eq!(n, 3, "subdir (access+default) and two files");
        let f = fs.read(Path::new("/s/proj/line_1042.segy")).unwrap();
        let a = f.access.unwrap();
        assert_eq!(a.get(Tag::Group(30001)), None);
        assert_eq!(a.effective(Tag::Group(30005)), Some(Perms::R));
        let d = fs.read(Path::new("/s/proj")).unwrap().default.unwrap();
        assert_eq!(d.get(Tag::Group(30005)), Some(Perms::RX));
        // Removing a group entirely.
        retarget_group(&fs, Path::new("/s"), 30005, None).unwrap();
        let a = fs.read(Path::new("/s/readme.txt")).unwrap().access.unwrap();
        assert_eq!(a.get(Tag::Group(30005)), None);
        a.validate().unwrap();
    }

    #[test]
    fn explorer_edit_on_root_keeps_extra_entries_on_reapply() {
        let fs = share();
        let p = Path::new("/s");
        let mut a = fs.read(p).unwrap().access.unwrap();
        a.set(Tag::User(20003), Perms::RX);
        fs.write_access(p, &a).unwrap();
        let new = ShareGroups { writers: None, ..G };
        apply_root(&fs, p, &new, Some(&G)).unwrap();
        let a = fs.read(p).unwrap().access.unwrap();
        assert_eq!(
            a.get(Tag::User(20003)),
            Some(Perms::RX),
            "Explorer grant kept"
        );
        assert_eq!(a.get(Tag::Group(30002)), None, "old Writers removed");
    }
}
