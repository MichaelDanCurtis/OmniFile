//! Commit-side path safety (M1 review N4, S8) with the real daemon on xfs,
//! as root:
//!
//! ```text
//! sudo -E cargo test -p omnifile-edge --test path_safety_linux
//! ```
#![cfg(target_os = "linux")]

use std::os::fd::AsFd;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use omnifile_edge::checks::startup_checks;
use omnifile_edge::config::Config;
use omnifile_edge::daemon::Daemon;
use omnifile_edge::fakes::{FakeService, events};
use omnifile_edge::ports::CloseWrite;
use omnifile_marker::xattr;
use omnifile_stub::{Layout, sys};

fn base() -> Option<PathBuf> {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("skipping: needs root");
        return None;
    }
    let b = PathBuf::from(
        std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/omnifile/data/test".into()),
    );
    std::fs::create_dir_all(&b).ok()?;
    if omnifile_stub::sys::fanotify_guarded(&b).unwrap_or(false) {
        eprintln!(
            "skipping: {} is on a filesystem a running smb-guard watches (it fails closed on \
             this test's files); set OMNIFILE_TEST_DIR to an unguarded xfs",
            b.display()
        );
        return None;
    }
    Some(b)
}

fn config(dir: &Path) -> Config {
    let exe = std::env::current_exe().unwrap();
    let toml = format!(
        r#"
        data_root = "{d}/data"
        replica_path = "{d}/state/replica.db"
        socket_path = "{d}/run/fsal.sock"
        edge_id = 7
        backend = "fake"
        [fsal]
        required_exe = "{exe}"
        # No FSAL connects in these tests: commits would otherwise wait for
        # one to say no NFSv4 client holds the file open (#54).
        no_ganesha = true
        [feed]
        wait_s = 1
        retry_s = 1
        [commit]
        detect = false
        # Writes handed to the commit by hand wait out this period (#80 review).
        quiet_s = 1
        [log]
        format = "text"
        filter = "debug"
        "#,
        d = dir.display(),
        exe = exe.display()
    );
    std::fs::create_dir_all(dir.join("data")).unwrap();
    Config::from_toml(&toml, Path::new("test.toml")).unwrap()
}

async fn eventually(what: &str, mut f: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !f() {
        assert!(Instant::now() < deadline, "timed out: {what}");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

fn has_marker(p: &Path) -> bool {
    sys::open_ro(p)
        .ok()
        .and_then(|fd| xattr::read_required(fd.as_fd()).ok())
        .is_some()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn notes_are_skipped_by_inode_and_symlinked_dirs_are_never_read() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path());
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.push_event(events::share(1, 100, "proj", "fail", 1));
    svc.push_event(events::dir(100, None, "", 1));
    eventually("share root", || data_root.join("proj").is_dir()).await;
    let commit = |p: &Path| CloseWrite {
        path: p.to_path_buf(),
        ino: std::fs::symlink_metadata(p).map(|m| m.ino()).unwrap_or(0),
        swept_gen: None,
        token: None,
        seen: None,
    };

    // N4: a user's own file named like a note is committed.
    let user = data_root.join("proj/results.RECALLING.txt");
    std::fs::write(&user, b"user data\n").unwrap();
    d.commits.send(commit(&user)).await.unwrap();
    eventually("user file committed", || has_marker(&user)).await;

    // The daemon's own note is not.
    let layout = Layout::new(&data_root);
    omnifile_stub::placeholder::write_note(&layout, Path::new("proj/x.segy"), "note\n", 0, 0)
        .unwrap();
    let note = data_root.join("proj/x.segy.RECALLING.txt");
    let calls = svc.calls().len();
    d.commits.send(commit(&note)).await.unwrap();
    // A later, ordinary commit proves the note's event was processed first.
    let probe = data_root.join("proj/probe.bin");
    std::fs::write(&probe, b"probe").unwrap();
    d.commits.send(commit(&probe)).await.unwrap();
    eventually("probe committed", || has_marker(&probe)).await;
    assert!(!has_marker(&note), "the daemon's note was committed");
    assert!(svc.calls().len() > calls);

    // S8: a directory swapped for a symlink after the write: root must not
    // read (and upload) the file it now points at.
    let outside = dir.path().join("outside");
    std::fs::create_dir_all(&outside).unwrap();
    std::fs::write(outside.join("secret"), b"root only\n").unwrap();
    let sub = data_root.join("proj/sub");
    std::fs::create_dir(&sub).unwrap();
    std::fs::write(sub.join("secret"), b"user\n").unwrap();
    std::fs::remove_file(sub.join("secret")).unwrap();
    std::fs::remove_dir(&sub).unwrap();
    std::os::unix::fs::symlink(&outside, &sub).unwrap();
    let calls = svc.calls();
    d.commits
        .send(CloseWrite {
            path: sub.join("secret"),
            ino: 0,
            swept_gen: None,
            token: None,
            seen: None,
        })
        .await
        .unwrap();
    let probe2 = data_root.join("proj/probe2.bin");
    std::fs::write(&probe2, b"probe2").unwrap();
    d.commits.send(commit(&probe2)).await.unwrap();
    eventually("probe2 committed", || has_marker(&probe2)).await;
    assert!(!has_marker(&outside.join("secret")), "followed the symlink");
    let new_calls: Vec<String> = svc.calls().into_iter().skip(calls.len()).collect();
    assert!(
        !new_calls.iter().any(|c| c.contains("secret")),
        "{new_calls:?}"
    );
    d.shutdown().await;
}
