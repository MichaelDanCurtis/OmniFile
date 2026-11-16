//! Keeping every inode in a share inside the one permission model, whatever
//! Unix tools do to its mode (access-control.md §1.1).
//!
//! Two things a `chmod` (or `cp -p`, `tar -p`, `rsync -p`, an application
//! creating a file with mode 0644) does to a file that carries the share's
//! ACL, and why the edge undoes them:
//!
//! 1. **It opens `other`** (`other::r--`). In a share every AD user matches the
//!    owning group (*Domain Users*), so the kernel never uses `other` for them;
//!    but Ganesha's POSIX→NFSv4 mapping turns "other has a right `group::`
//!    lacks" into `DENY GROUP@` ahead of the named-group `ALLOW`s. Over NFS the
//!    share's Readers and Writers are then locked out of the file, while SMB
//!    and local access still let them in: two answers for one file.
//! 2. **It narrows the mask** (`mask::r--`), silently taking write away from
//!    Writers and Owners although Explorer and the portal still show them as
//!    granted. AD groups are the access model (decision 28); mode bits from a
//!    Unix client must not override them.
//!
//! So on every inode in a share: `other` (access and default ACL) is `---`,
//! and when the ACL has named entries, its mask is the union of the group
//! class, without `x` on a regular file whose owner has no `x` (so files do
//! not all become executable; the create mode's exec bit is kept). Named
//! entries, the owner entry and the owning-group entry are never changed. An
//! inode without an extended ACL only loses its `other` bits.

use std::io;
use std::path::Path;

use crate::posix::{Perms, PosixAcl, Tag};
use crate::sys::{AclFs, Kind};

/// The normalised form of one ACL, or `None` when it already is. `is_dir`
/// selects directory rules (a mask with `x`, needed to traverse).
pub fn normalize(acl: &PosixAcl, is_dir: bool) -> Option<PosixAcl> {
    let mut a = acl.clone();
    a.set(Tag::Other, Perms::NONE);
    a.recompute_mask();
    let owner_x = a.get(Tag::UserObj).is_some_and(|p| p.exec());
    if let Some(m) = a.get(Tag::Mask)
        && !is_dir
        && !owner_x
    {
        a.set(Tag::Mask, Perms(m.bits() & !1));
    }
    // A minimal ACL stays minimal: recompute_mask removes the mask when there
    // are no named entries.
    (a != *acl).then_some(a)
}

/// What [`normalize_inode`] changed.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Normalized {
    pub access: bool,
    pub default: bool,
}

impl Normalized {
    pub fn any(self) -> bool {
        self.access || self.default
    }
}

/// Normalises one inode in place. Symlinks are skipped.
pub fn normalize_inode(fs: &dyn AclFs, path: &Path) -> io::Result<Normalized> {
    let f = fs.read(path)?;
    let mut n = Normalized::default();
    if f.kind == Kind::Symlink {
        return Ok(n);
    }
    let is_dir = f.kind == Kind::Dir;
    if let Some(a) = normalize(&f.effective_access(), is_dir) {
        fs.write_access_at(path, (f.dev, f.ino), &a)?;
        n.access = true;
    }
    if let Some(d) = f.default.as_ref().and_then(|d| normalize(d, true)) {
        fs.write_default_at(path, (f.dev, f.ino), Some(&d))?;
        n.default = true;
    }
    Ok(n)
}

/// Normalises a whole tree (same filesystem, no symlink following). Returns
/// the number of inodes changed. Used by the nightly scan's repair pass; the
/// daemon calls [`normalize_inode`] for files it sees created or changed.
pub fn normalize_tree(fs: &dyn AclFs, root: &Path, skip: &[&str]) -> io::Result<u64> {
    let mut changed = 0;
    let mut stack = vec![root.to_path_buf()];
    let mut root_dev = None;
    while let Some(p) = stack.pop() {
        let f = match fs.read(&p) {
            Ok(f) => f,
            Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
            Err(e) => return Err(e),
        };
        if *root_dev.get_or_insert(f.dev) != f.dev {
            continue;
        }
        changed += normalize_inode(fs, &p)?.any() as u64;
        if f.kind == Kind::Dir {
            for c in fs.read_dir(&p)? {
                let name = c.file_name().and_then(|n| n.to_str()).unwrap_or("");
                if !skip.contains(&name) {
                    stack.push(c);
                }
            }
        }
    }
    Ok(changed)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scan::tests::MemFs;
    use crate::share::{ShareGroups, apply_root};

    const G: ShareGroups = ShareGroups {
        owners: 30003,
        writers: Some(30002),
        readers: Some(30001),
    };

    #[test]
    fn chmod_644_is_undone() {
        // A share file after `chmod 644`: mask r--, other r--.
        let a = PosixAcl::from_text(
            "user::rw-,group::---,group:30001:r-x,group:30002:rwx,group:30003:rwx,mask::r--,other::r--",
        )
        .unwrap();
        let n = normalize(&a, false).unwrap();
        assert_eq!(
            n.to_text(),
            "user::rw-,group::---,group:30001:r-x,group:30002:rwx,group:30003:rwx,mask::rw-,other::---"
        );
        assert_eq!(n.effective(Tag::Group(30002)), Some(Perms::RW));
        assert_eq!(n.effective(Tag::Group(30001)), Some(Perms::R));
        assert!(normalize(&n, false).is_none(), "idempotent");
        // An executable (owner has x) keeps x for the groups; a directory too.
        let mut x = a.clone();
        x.set(Tag::UserObj, Perms::RWX);
        assert_eq!(
            normalize(&x, false).unwrap().get(Tag::Mask),
            Some(Perms::RWX)
        );
        assert_eq!(
            normalize(&a, true).unwrap().get(Tag::Mask),
            Some(Perms::RWX)
        );
    }

    #[test]
    fn minimal_acl_only_loses_other() {
        let a = PosixAcl::from_mode(0o644);
        let n = normalize(&a, false).unwrap();
        assert!(n.is_minimal());
        assert_eq!(n.mode_bits(), 0o640);
        assert!(normalize(&PosixAcl::from_mode(0o750), true).is_none());
    }

    #[test]
    fn named_and_owner_entries_untouched() {
        let a = PosixAcl::from_text("user::r--,user:20003:r--,group::---,mask::r--,other::---")
            .unwrap();
        assert!(normalize(&a, false).is_none());
    }

    #[test]
    fn tree_repair() {
        let fs = MemFs::default();
        fs.mkdir("/s", 0, 0);
        apply_root(&fs, Path::new("/s"), &G, None).unwrap();
        fs.touch("/s/a", 20002, 10000);
        fs.touch("/s/b", 20002, 10000);
        let p = Path::new("/s/a");
        let mut a = fs.read(p).unwrap().access.unwrap();
        a.set(Tag::Mask, Perms::R);
        a.set(Tag::Other, Perms::R);
        fs.write_access(p, &a).unwrap();
        assert_eq!(
            normalize_tree(&fs, Path::new("/s"), &[]).unwrap(),
            1,
            "only a: b is as inherited"
        );
        assert_eq!(normalize_tree(&fs, Path::new("/s"), &[]).unwrap(), 0);
        let a = fs.read(p).unwrap().access.unwrap();
        assert_eq!(a.effective(Tag::Group(30002)), Some(Perms::RW));
        assert_eq!(a.get(Tag::Other), Some(Perms::NONE));
    }
}
