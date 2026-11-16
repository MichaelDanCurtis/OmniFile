//! Path safety of the daemon's root-owned filesystem effects (M1 review B1,
//! S8, N4): nothing a share's users create (a symlink at a name or in any
//! directory component, `..`, a hard link) can redirect a create, chown,
//! chmod, link or note write outside the share. Linux, as root, on xfs:
//!
//! ```text
//! sudo -E cargo test -p omnifile-stub --test path_safety
//! ```
//!
//! `OMNIFILE_TEST_DIR` (default `/srv/omnifile/data/test`) must be on xfs.
#![cfg(target_os = "linux")]

use std::fs::File;
use std::os::fd::AsFd;
use std::os::unix::fs::{MetadataExt, PermissionsExt, symlink};
use std::path::{Path, PathBuf};

use omnifile_marker::Flags;
use omnifile_stub::placeholder::{
    Created, NoteOutcome, PlaceholderSpec, create_dir, create_placeholder, create_symlink, is_note,
    remove_note, write_note,
};
use omnifile_stub::{Layout, MarkerSeq, sys};

struct Env {
    _dir: tempfile::TempDir,
    layout: Layout,
    /// Outside the data volume's tree (a sibling of `data/`).
    outside: PathBuf,
}

fn env() -> Option<Env> {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("skipping: needs root");
        return None;
    }
    let base =
        std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/omnifile/data/test".into());
    std::fs::create_dir_all(&base).ok()?;
    let d = File::open(&base).ok()?;
    if !sys::is_xfs(d.as_fd()).unwrap_or(false) {
        eprintln!("skipping: {base} is not xfs");
        return None;
    }
    if sys::fanotify_guarded(Path::new(&base)).unwrap_or(false) {
        eprintln!(
            "skipping: {base} is on a filesystem a running smb-guard watches (it fails closed \
             on this test's files); set OMNIFILE_TEST_DIR to an unguarded xfs"
        );
        return None;
    }
    let dir = tempfile::tempdir_in(&base).ok()?;
    let data = dir.path().join("data");
    let outside = dir.path().join("outside");
    std::fs::create_dir_all(data.join("proj")).unwrap();
    std::fs::create_dir_all(data.join("other")).unwrap();
    std::fs::create_dir_all(&outside).unwrap();
    std::fs::write(outside.join("victim"), b"root's file\n").unwrap();
    std::fs::set_permissions(
        outside.join("victim"),
        std::fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    Some(Env {
        _dir: dir,
        layout: Layout::new(data),
        outside,
    })
}

fn victim_untouched(e: &Env) {
    let v = e.outside.join("victim");
    let md = std::fs::symlink_metadata(&v).unwrap();
    assert!(md.is_file());
    assert_eq!(std::fs::read(&v).unwrap(), b"root's file\n");
    assert_eq!(md.mode() & 0o7777, 0o600);
    assert_eq!((md.uid(), md.gid()), (0, 0));
}

fn spec(file_id: u64) -> PlaceholderSpec {
    PlaceholderSpec {
        file_id,
        version_id: 1,
        size: 4096,
        share_id: 1,
        flags: Flags::ONLINE,
        uid: 1234,
        gid: 5678,
        mode: 0o666,
        mtime_ns: 1_700_000_000_000_000_000,
    }
}

// --- B1: the recall note.

#[test]
fn note_never_follows_a_symlink_at_its_name() {
    let Some(e) = env() else { return };
    let link = e.layout.path(Path::new("proj/x.segy.RECALLING.txt"));
    symlink(e.outside.join("victim"), &link).unwrap();
    let r = write_note(&e.layout, Path::new("proj/x.segy"), "pwned\n", 1234, 5678).unwrap();
    assert_eq!(r, NoteOutcome::Foreign);
    victim_untouched(&e);
    assert!(std::fs::symlink_metadata(&link).unwrap().is_symlink());
    assert!(!remove_note(&e.layout, Path::new("proj/x.segy")).unwrap());
    assert!(
        std::fs::symlink_metadata(&link).is_ok(),
        "user's symlink kept"
    );
}

#[test]
fn note_never_follows_a_symlinked_directory() {
    let Some(e) = env() else { return };
    symlink(&e.outside, e.layout.path(Path::new("proj/d"))).unwrap();
    let err = write_note(&e.layout, Path::new("proj/d/victim"), "x", 0, 0).unwrap_err();
    assert_eq!(err.raw_os_error(), Some(libc::ELOOP), "{err}");
    assert!(!e.outside.join("victim.RECALLING.txt").exists());
    // Nor `..` out of the share.
    let err = write_note(&e.layout, Path::new("proj/../../outside/victim"), "x", 0, 0).unwrap_err();
    assert_eq!(err.kind(), std::io::ErrorKind::InvalidInput);
    assert!(!e.outside.join("victim.RECALLING.txt").exists());
    victim_untouched(&e);
}

#[test]
fn note_never_writes_through_a_hard_link() {
    let Some(e) = env() else { return };
    // Another share's file, hard-linked at the note's name.
    let other = e.layout.path(Path::new("other/data.bin"));
    std::fs::write(&other, b"other share\n").unwrap();
    std::fs::hard_link(
        &other,
        e.layout.path(Path::new("proj/x.segy.RECALLING.txt")),
    )
    .unwrap();
    let r = write_note(&e.layout, Path::new("proj/x.segy"), "pwned\n", 0, 0).unwrap();
    assert_eq!(r, NoteOutcome::Foreign);
    assert_eq!(std::fs::read(&other).unwrap(), b"other share\n");
}

#[test]
fn note_is_a_new_marked_inode_owned_by_the_file_owner() {
    let Some(e) = env() else { return };
    let rel = Path::new("proj/x.segy");
    let p = e.layout.path(Path::new("proj/x.segy.RECALLING.txt"));
    let r = write_note(&e.layout, rel, "first\n", 1234, 5678).unwrap();
    assert_eq!(r, NoteOutcome::Created);
    let md = std::fs::symlink_metadata(&p).unwrap();
    assert!(md.is_file());
    assert_eq!(
        (md.uid(), md.gid(), md.mode() & 0o7777),
        (1234, 5678, 0o644)
    );
    assert_eq!(std::fs::read(&p).unwrap(), b"first\n");
    let f = File::open(&p).unwrap();
    assert!(is_note(f.as_fd(), p.file_name().unwrap()));
    // A later note rewrites the daemon's own inode in place.
    let r = write_note(&e.layout, rel, "second\n", 1234, 5678).unwrap();
    assert_eq!(r, NoteOutcome::Rewritten);
    assert_eq!(std::fs::symlink_metadata(&p).unwrap().ino(), md.ino());
    assert_eq!(std::fs::read(&p).unwrap(), b"second\n");
    assert!(remove_note(&e.layout, rel).unwrap());
    assert!(!p.exists());
}

// --- N4: a user's file that merely ends in `.RECALLING.txt`.

#[test]
fn user_files_with_the_note_suffix_are_not_notes() {
    let Some(e) = env() else { return };
    let p = e.layout.path(Path::new("proj/y.segy.RECALLING.txt"));
    std::fs::write(&p, b"user data\n").unwrap();
    let f = File::open(&p).unwrap();
    assert!(!is_note(f.as_fd(), p.file_name().unwrap()));
    let r = write_note(&e.layout, Path::new("proj/y.segy"), "x\n", 0, 0).unwrap();
    assert_eq!(r, NoteOutcome::Foreign);
    assert_eq!(std::fs::read(&p).unwrap(), b"user data\n");
    assert!(!remove_note(&e.layout, Path::new("proj/y.segy")).unwrap());
    assert!(p.exists());
    // The daemon's own note renamed to another name is not a note either.
    write_note(&e.layout, Path::new("proj/z.segy"), "x\n", 0, 0).unwrap();
    let z = e.layout.path(Path::new("proj/z.segy.RECALLING.txt"));
    let renamed = e.layout.path(Path::new("proj/z.data"));
    std::fs::rename(&z, &renamed).unwrap();
    let f = File::open(&renamed).unwrap();
    assert!(!is_note(f.as_fd(), renamed.file_name().unwrap()));
}

// --- S8: placeholders, directories, symlinks, attributes.

#[test]
fn creates_never_follow_a_symlinked_parent() {
    let Some(e) = env() else { return };
    symlink(&e.outside, e.layout.path(Path::new("proj/d"))).unwrap();
    let seq = MarkerSeq::new();
    let err = create_placeholder(&e.layout, Path::new("proj/d/new"), &spec(7), &seq).unwrap_err();
    assert_eq!(err.raw_os_error(), Some(libc::ELOOP));
    let err = create_dir(&e.layout, Path::new("proj/d/newdir"), 8, 1, 1, 0o777, 0).unwrap_err();
    assert_eq!(err.raw_os_error(), Some(libc::ELOOP));
    let err = create_symlink(&e.layout, Path::new("proj/d/ln"), 9, "x", 1, 1, 0).unwrap_err();
    assert_eq!(err.raw_os_error(), Some(libc::ELOOP));
    assert_eq!(std::fs::read_dir(&e.outside).unwrap().count(), 1);
    victim_untouched(&e);
}

#[test]
fn create_dir_never_adopts_a_symlink_to_a_directory() {
    let Some(e) = env() else { return };
    symlink(&e.outside, e.layout.path(Path::new("proj/sub"))).unwrap();
    let before = std::fs::metadata(&e.outside).unwrap();
    let r = create_dir(&e.layout, Path::new("proj/sub"), 8, 1234, 5678, 0o777, 0).unwrap();
    assert!(matches!(r, Created::Conflict(_)), "{r:?}");
    let after = std::fs::metadata(&e.outside).unwrap();
    assert_eq!(
        (after.uid(), after.gid(), after.mode()),
        (before.uid(), before.gid(), before.mode())
    );
}

#[test]
fn placeholder_and_symlink_at_a_planted_name_are_conflicts() {
    let Some(e) = env() else { return };
    let seq = MarkerSeq::new();
    symlink(
        e.outside.join("victim"),
        e.layout.path(Path::new("proj/a.segy")),
    )
    .unwrap();
    let r = create_placeholder(&e.layout, Path::new("proj/a.segy"), &spec(7), &seq).unwrap();
    assert!(matches!(r, Created::Conflict(_)), "{r:?}");
    // A symlink the catalog wants, already there: chown applies to the link
    // itself, never to its target.
    let target = e.outside.join("victim").to_string_lossy().into_owned();
    symlink(&target, e.layout.path(Path::new("proj/ln"))).unwrap();
    let r = create_symlink(&e.layout, Path::new("proj/ln"), 9, &target, 1234, 5678, 0).unwrap();
    assert!(matches!(r, Created::Adopted { .. }), "{r:?}");
    let l = std::fs::symlink_metadata(e.layout.path(Path::new("proj/ln"))).unwrap();
    assert_eq!((l.uid(), l.gid()), (1234, 5678));
    victim_untouched(&e);
}

#[test]
fn set_attrs_refuses_a_name_swapped_for_a_symlink() {
    let Some(e) = env() else { return };
    let p = e.layout.path(Path::new("proj/f"));
    std::fs::write(&p, b"x").unwrap();
    let at = sys::resolve_beneath(&e.layout.data_root, Path::new("proj/f")).unwrap();
    at.set_attrs(1234, 5678, Some(0o640), 0).unwrap();
    let md = std::fs::symlink_metadata(&p).unwrap();
    assert_eq!(
        (md.uid(), md.gid(), md.mode() & 0o7777),
        (1234, 5678, 0o640)
    );
    // The user swaps the name for a symlink: chmod/chown must not follow.
    std::fs::remove_file(&p).unwrap();
    symlink(e.outside.join("victim"), &p).unwrap();
    let err = at.set_attrs(1234, 5678, Some(0o777), 0).unwrap_err();
    assert_eq!(err.kind(), std::io::ErrorKind::InvalidInput, "{err}");
    victim_untouched(&e);
}

#[test]
fn resolve_rejects_escapes_and_renames_stay_in_their_directory() {
    let Some(e) = env() else { return };
    for bad in [
        "../outside/victim",
        "/etc/passwd",
        "proj/../../outside/victim",
        "",
    ] {
        let err = sys::resolve_beneath(&e.layout.data_root, Path::new(bad)).unwrap_err();
        assert_eq!(err.kind(), std::io::ErrorKind::InvalidInput, "{bad:?}");
    }
    std::fs::write(e.layout.path(Path::new("proj/f")), b"x").unwrap();
    symlink(&e.outside, e.layout.path(Path::new("proj/d"))).unwrap();
    let from = sys::resolve_beneath(&e.layout.data_root, Path::new("proj/f")).unwrap();
    assert!(sys::resolve_beneath(&e.layout.data_root, Path::new("proj/d/f")).is_err());
    let to = sys::resolve_beneath(&e.layout.data_root, Path::new("proj/g")).unwrap();
    sys::rename_noreplace(&from, &to).unwrap();
    assert!(e.layout.path(Path::new("proj/g")).exists());
    assert_eq!(std::fs::read_dir(&e.outside).unwrap().count(), 1);
}
