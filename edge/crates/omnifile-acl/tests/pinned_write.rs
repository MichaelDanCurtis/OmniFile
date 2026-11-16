//! A rename between reading an inode's ACL and writing the new one must
//! not apply the ACL to whatever now has the name (M1 re-review). Linux,
//! on a filesystem with POSIX ACLs (`OMNIFILE_TEST_DIR`, default
//! `/srv/omnifile/data/test`).
#![cfg(target_os = "linux")]

use std::path::PathBuf;

use omnifile_acl::posix::{Perms, PosixAcl, Tag};
use omnifile_acl::sys::{AclFs, SysFs};

fn dir() -> Option<tempfile::TempDir> {
    let base = PathBuf::from(
        std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/omnifile/data/test".into()),
    );
    std::fs::create_dir_all(&base).ok()?;
    tempfile::tempdir_in(&base).ok()
}

#[test]
fn a_renamed_inode_never_gets_the_acl_meant_for_the_old_one() {
    let Some(d) = dir() else {
        eprintln!("skipping: no test directory");
        return;
    };
    let a = d.path().join("a");
    std::fs::write(&a, b"x").unwrap();
    let fs = SysFs;
    let f = fs.read(&a).unwrap();
    let mut acl = PosixAcl::from_mode(0o640);
    acl.set(Tag::Group(4242), Perms(4));
    acl.set(Tag::Mask, Perms(4));
    // Renamed away, and another file takes the name.
    std::fs::rename(&a, d.path().join("b")).unwrap();
    std::fs::write(&a, b"other").unwrap();
    let e = fs.write_access_at(&a, (f.dev, f.ino), &acl).unwrap_err();
    assert_eq!(e.raw_os_error(), Some(libc::ESTALE), "{e}");
    assert!(
        fs.read(&a).unwrap().access.is_none(),
        "the newcomer is untouched"
    );
    // The right inode is written.
    let b = d.path().join("b");
    if let Err(e) = fs.write_access_at(&b, (f.dev, f.ino), &acl) {
        if e.raw_os_error() == Some(libc::EOPNOTSUPP) {
            eprintln!("skipping the positive half: no POSIX ACLs here");
            return;
        }
        panic!("{e}");
    }
    assert!(fs.read(&b).unwrap().access.is_some());
}

/// A share root renamed away and replaced (M7 review B1): the edge's pinned
/// apply gives the newcomer nothing (no group, no ACL), and a symlink in the
/// root's place is refused too.
#[test]
fn a_replaced_share_root_gets_neither_the_group_nor_the_acl() {
    use std::os::unix::fs::MetadataExt;

    use omnifile_acl::{ShareGroups, apply_root_at};

    let Some(d) = dir() else {
        eprintln!("skipping: no test directory");
        return;
    };
    let g = ShareGroups {
        owners: 30003,
        writers: Some(30002),
        readers: Some(30001),
    };
    let root = d.path().join("proj");
    std::fs::create_dir(&root).unwrap();
    let fs = SysFs;
    let made = fs.read(&root).unwrap();
    let pin = (made.dev, made.ino);
    std::fs::rename(&root, d.path().join("proj.old")).unwrap();
    std::fs::create_dir(&root).unwrap();
    std::os::unix::fs::chown(&root, Some(4321), Some(4321)).unwrap();
    let e = apply_root_at(&fs, &root, pin, &g, None).unwrap_err();
    assert_eq!(e.raw_os_error(), Some(libc::ESTALE), "{e}");
    assert_eq!(std::fs::metadata(&root).unwrap().gid(), 4321);
    assert!(fs.read(&root).unwrap().access.is_none());

    std::fs::remove_dir(&root).unwrap();
    std::os::unix::fs::symlink(d.path().join("proj.old"), &root).unwrap();
    assert!(apply_root_at(&fs, &root, pin, &g, None).is_err());

    // The folder itself, back at its name, is given its groups.
    std::fs::remove_file(&root).unwrap();
    std::fs::rename(d.path().join("proj.old"), &root).unwrap();
    match apply_root_at(&fs, &root, pin, &g, None) {
        Ok(_) => assert_eq!(std::fs::metadata(&root).unwrap().gid(), 30003),
        Err(e) if e.raw_os_error() == Some(libc::EOPNOTSUPP) => {
            eprintln!("skipping the positive half: no POSIX ACLs here")
        }
        Err(e) => panic!("{e}"),
    }
}
