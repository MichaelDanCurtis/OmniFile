//! The namespace watcher on a real filesystem (root: FAN_MARK_FILESYSTEM).
#![cfg(target_os = "linux")]

use std::time::Duration;

use omnifile_detect::ns::{EntryOp, NsEvent, watch};

/// The kernel's handle for a path (what the events carry).
fn handle_of(p: &std::path::Path) -> Vec<u8> {
    #[repr(C)]
    struct H {
        bytes: libc::c_uint,
        ty: libc::c_int,
        f: [u8; 128],
    }
    let mut h = H {
        bytes: 128,
        ty: 0,
        f: [0; 128],
    };
    let mut mount_id: libc::c_int = 0;
    let c = std::ffi::CString::new(p.as_os_str().as_encoded_bytes()).unwrap();
    // SAFETY: valid path and out-parameters.
    let r = unsafe {
        libc::name_to_handle_at(
            libc::AT_FDCWD,
            c.as_ptr(),
            (&mut h as *mut H).cast(),
            &mut mount_id,
            0,
        )
    };
    assert_eq!(r, 0);
    h.f[..h.bytes as usize].to_vec()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn creates_renames_deletes_and_chowns_name_their_directory() {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("skipping: needs root");
        return;
    }
    let base =
        std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/omnifile/data/test".into());
    std::fs::create_dir_all(&base).unwrap();
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (tx, mut rx) = tokio::sync::mpsc::channel(1024);
    let root = dir.path().to_path_buf();
    tokio::spawn(async move { watch(&root, tx).await });
    tokio::time::sleep(Duration::from_millis(300)).await;
    let p = dir.path();
    std::fs::create_dir(p.join("d")).unwrap();
    std::fs::write(p.join("d/f"), b"x").unwrap();
    std::fs::rename(p.join("d/f"), p.join("d/g")).unwrap();
    std::os::unix::fs::chown(p.join("d/g"), Some(1234), None).unwrap();
    std::fs::remove_file(p.join("d/g")).unwrap();
    let _keep = (handle_of(p), handle_of(&p.join("d")));
    // fanotify merges unread events on the same object, and here the object
    // is the directory: every change in one directory may be one event. What
    // matters is that each directory that changed is named.
    let mut dirs = std::collections::HashSet::new();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
    while let Ok(Some(ev)) = tokio::time::timeout_at(deadline, rx.recv()).await {
        match ev {
            NsEvent::Dir(d) => {
                assert!(!d.handle.is_empty());
                dirs.insert(d.handle);
            }
            NsEvent::Entry(_) => {}
            NsEvent::Overflow => panic!("overflow"),
        }
    }
    // Other tests on this filesystem may add their own directories.
    assert!(dirs.contains(&handle_of(p)), "the root (mkdir d)");
    assert!(
        dirs.contains(&handle_of(&p.join("d"))),
        "d (create, rename, chown, delete)"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_chown_names_the_files_directory() {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        return;
    }
    let base =
        std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/omnifile/data/test".into());
    let dir = tempfile::tempdir_in(&base).unwrap();
    std::fs::create_dir(dir.path().join("d")).unwrap();
    std::fs::write(dir.path().join("d/f"), b"x").unwrap();
    let (tx, mut rx) = tokio::sync::mpsc::channel(1024);
    let root = dir.path().to_path_buf();
    tokio::spawn(async move { watch(&root, tx).await });
    tokio::time::sleep(Duration::from_millis(300)).await;
    std::os::unix::fs::chown(dir.path().join("d/f"), Some(1234), None).unwrap();
    let ev = tokio::time::timeout(Duration::from_secs(3), rx.recv()).await;
    assert!(matches!(ev, Ok(Some(NsEvent::Dir(_)))), "{ev:?}");
}

/// Names mode (Linux 5.17): each change also names its entry and carries
/// the entry's own handle, which the daemon uses to say who made it.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn entries_are_named_with_their_own_handles() {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        return;
    }
    let base =
        std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/omnifile/data/test".into());
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (tx, mut rx) = tokio::sync::mpsc::channel(1024);
    let root = dir.path().to_path_buf();
    tokio::spawn(async move { watch(&root, tx).await });
    tokio::time::sleep(Duration::from_millis(300)).await;
    let p = dir.path();
    std::fs::create_dir(p.join("d")).unwrap();
    std::fs::write(p.join("d/f"), b"x").unwrap();
    std::fs::rename(p.join("d/f"), p.join("d/g")).unwrap();
    let g = handle_of(&p.join("d/g"));
    std::os::unix::fs::chown(p.join("d/g"), Some(1234), None).unwrap();
    std::fs::remove_file(p.join("d/g")).unwrap();
    let (root_h, d_h) = (handle_of(p), handle_of(&p.join("d")));
    let mut seen = Vec::new();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
    while let Ok(Some(ev)) = tokio::time::timeout_at(deadline, rx.recv()).await {
        if let NsEvent::Entry(e) = ev {
            seen.push(e);
        }
    }
    let me = std::process::id() as i32;
    let has = |op: EntryOp, dir: &[u8], name: &str, child: Option<&[u8]>| {
        seen.iter().any(|e| {
            e.op == op
                && e.pid == me
                && e.dir.as_ref().is_some_and(|(_, h)| h == dir)
                && e.name == name.as_bytes()
                && child.is_none_or(|c| e.child.as_ref().is_some_and(|(_, h)| h == c))
        })
    };
    assert!(
        has(EntryOp::Create, &root_h, "d", Some(&d_h)),
        "mkdir d: {seen:?}"
    );
    assert!(
        seen.iter().any(|e| e.op == EntryOp::Create && e.is_dir),
        "{seen:?}"
    );
    assert!(
        has(EntryOp::MovedTo, &d_h, "g", Some(&g)),
        "mv f g: {seen:?}"
    );
    assert!(
        has(EntryOp::Attrib, &d_h, "g", Some(&g)),
        "chown g: {seen:?}"
    );
    assert!(has(EntryOp::Delete, &d_h, "g", Some(&g)), "rm g: {seen:?}");
}
