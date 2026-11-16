//! WP9: a file whose close the daemon never saw is still committed.
//!
//! WP3's chaos soak found a file written over NFSv4.1 (fsync and close
//! returned 0) and the daemon SIGKILLed 0.9 s later, inside the commit quiet
//! period. It stayed plain local data: never uploaded, never in the catalog.
//! The detector's close event died with the process, and the namespace
//! sweep left new files to the detector. These tests check the two layers of
//! the fix: the sweep hands such files to the commit pipeline once they are
//! quiet and open for write nowhere, and the detector's group lives in the
//! systemd fd store, so a close that lands while the daemon restarts is
//! still read. Real xfs, fanotify, the fake service; run as root:
//!
//! ```text
//! sudo -E OMNIFILE_TEST_DIR=/srv/omnifile/data/test cargo test -p omnifile-edge --test lost_commit_linux
//! ```
#![cfg(target_os = "linux")]

use std::os::fd::AsFd;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use omnifile_edge::checks::startup_checks;
use omnifile_edge::config::Config;
use omnifile_edge::daemon::Daemon;
use omnifile_edge::fakes::{FakeService, events};
use omnifile_marker::xattr::{self, ReadOutcome};
use omnifile_stub::sys;

type D = Daemon<FakeService, FakeService>;

/// Every detector marks the whole filesystem and reads every close on it,
/// so a test that makes thousands of them slows the others' detectors past
/// their timeouts. It runs alone; the others share.
static FLOOD: tokio::sync::RwLock<()> = tokio::sync::RwLock::const_new(());

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
    if sys::fanotify_guarded(&b).unwrap_or(false) {
        eprintln!(
            "skipping: {} is watched by a running smb-guard",
            b.display()
        );
        return None;
    }
    Some(b)
}

/// The daemon's config: the detector on (unless `extra` turns it off), a
/// short quiet period, namespace sync with a sweep at start.
fn toml(dir: &Path, extra: &str) -> String {
    let exe = std::env::current_exe().unwrap();
    format!(
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
        reconcile_s = 0
        [smbguard]
        enabled = false
        allow_disabled_with_servers = true
        [namespace]
        quiet_ms = 200
        {extra}
        [log]
        format = "text"
        filter = "debug"
        "#,
        d = dir.display(),
        exe = exe.display()
    )
}

fn config(dir: &Path, extra: &str) -> Config {
    std::fs::create_dir_all(dir.join("data")).unwrap();
    Config::from_toml(&toml(dir, extra), Path::new("test.toml")).unwrap()
}

const QUIET_2S: &str = "[commit]\nquiet_s = 2";

async fn start(cfg: Config, svc: &Arc<FakeService>) -> D {
    omnifile_edge::init_logging("text", "debug");
    let vol = startup_checks(&cfg).unwrap();
    Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap()
}

/// A share `proj` (100) with a placeholder `keep.bin` (103).
async fn world(dir: &Path, extra: &str) -> (D, Arc<FakeService>, PathBuf) {
    let cfg = config(dir, extra);
    let root = cfg.data_root.join("proj");
    let svc = Arc::new(FakeService::new());
    let d = start(cfg, &svc).await;
    svc.push_event(events::share(1, 100, "proj", "fail", 1));
    svc.push_event(events::dir(100, None, "", 1));
    svc.push_event(events::file(
        103,
        100,
        "keep.bin",
        10,
        1030,
        Some(1),
        false,
        None,
        1,
    ));
    let k = root.join("keep.bin");
    eventually("the tree materialised", Duration::from_secs(15), || {
        k.exists()
    })
    .await;
    (d, svc, root)
}

async fn eventually(what: &str, within: Duration, mut f: impl FnMut() -> bool) {
    let deadline = Instant::now() + within;
    while !f() {
        assert!(Instant::now() < deadline, "timed out: {what}");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

/// Write as a user would: from another process (the detector skips the
/// daemon's own writes, and these tests run in the daemon's process).
fn write_as_user(path: &Path, data: &str) {
    let st = std::process::Command::new("sh")
        .arg("-c")
        .arg(format!("printf '%s' '{data}' > '{}'", path.display()))
        .status()
        .unwrap();
    assert!(st.success());
}

/// `(file_id, version_id)` once `path` is committed: a replica row with a
/// content version, and a `RESIDENT` marker naming that version.
async fn committed(d: &D, path: &Path) -> Option<(u64, u64)> {
    let ro = sys::open_ro(path).ok()?;
    let key = sys::inode_key(ro.as_fd()).ok()?;
    let n = d
        .core
        .replica
        .lock()
        .await
        .node_by_ino(key.ino, key.ino_gen)
        .ok()??;
    let v = n.content_version_id?;
    match xattr::read(ro.as_fd()) {
        Ok(ReadOutcome::Marker(m)) if m.is_fully_resident() && m.version_id == v => {
            Some((n.file_id?, v))
        }
        _ => None,
    }
}

async fn wait_committed(d: &D, path: &Path, within: Duration) -> (u64, u64) {
    let deadline = Instant::now() + within;
    loop {
        if let Some(c) = committed(d, path).await {
            return c;
        }
        assert!(
            Instant::now() < deadline,
            "{} was never committed",
            path.display()
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

fn count(svc: &FakeService, call: &str) -> usize {
    svc.calls().iter().filter(|c| *c == call).count()
}

/// Committed exactly once: one create, one upload, the bytes in the store.
fn once(svc: &FakeService, parent: u64, name: &str, (file_id, version): (u64, u64), data: &str) {
    assert_eq!(
        count(svc, &format!("create {parent} {name}")),
        1,
        "{name}: one create; calls {:?}",
        svc.calls()
    );
    assert_eq!(
        count(svc, &format!("upload_start {file_id}")),
        1,
        "{name}: one upload; calls {:?}",
        svc.calls()
    );
    assert_eq!(
        svc.blob(file_id, version).as_deref(),
        Some(data.as_bytes()),
        "{name}: uploaded bytes"
    );
}

/// The soak's case: closed, then the daemon SIGKILLed 1 s later (inside the
/// quiet period). The daemon runs as its own process (the real binary), so
/// the kill is real; the next instance is this test's, on the same replica.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_close_one_second_before_a_sigkill_is_committed_after_the_restart() {
    let _shared = FLOOD.read().await;
    let Some(b) = base() else { return };
    let dir = tempfile::tempdir_in(b).unwrap();
    // The share, materialised by a first daemon, which stops cleanly.
    let (d, svc, root) = world(dir.path(), "[commit]\nquiet_s = 3").await;
    d.shutdown().await;
    // The daemon as a process, with the detector and a 3 s quiet period.
    let conf = dir.path().join("edge.toml");
    std::fs::write(&conf, toml(dir.path(), "[commit]\nquiet_s = 3")).unwrap();
    let log = std::fs::File::create(dir.path().join("child.log")).unwrap();
    let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_omnifile-edge"))
        .arg("--config")
        .arg(&conf)
        .stdout(log.try_clone().unwrap())
        .stderr(log)
        .spawn()
        .unwrap();
    let log_path = dir.path().join("child.log");
    eventually(
        "the child's detector is armed",
        Duration::from_secs(20),
        || std::fs::read_to_string(&log_path).is_ok_and(|l| l.contains("write detector armed")),
    )
    .await;
    let p = root.join("line_1042.segy");
    write_as_user(&p, "seismic");
    tokio::time::sleep(Duration::from_secs(1)).await;
    // SAFETY: plain syscall on the child's pid.
    assert_eq!(unsafe { libc::kill(child.id() as i32, libc::SIGKILL) }, 0);
    child.wait().unwrap();
    assert!(
        !matches!(
            xattr::read(sys::open_ro(&p).unwrap().as_fd()).unwrap(),
            ReadOutcome::Marker(_)
        ),
        "the killed daemon had not committed it yet"
    );
    // The next instance: the close is gone with the killed one.
    let d = start(config(dir.path(), "[commit]\nquiet_s = 3"), &svc).await;
    let c = wait_committed(&d, &p, Duration::from_secs(30)).await;
    once(&svc, 100, "line_1042.segy", c, "seismic");
    d.shutdown().await;
}

/// Files written while the daemon is stopped, in the share and in a folder
/// made meanwhile, are all committed once it is back, each exactly once,
/// and a second restart commits none of them again. A file the detector
/// commits while the daemon runs is not committed again by the sweep.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn files_written_while_the_daemon_was_stopped_are_all_committed_once() {
    let _shared = FLOOD.read().await;
    let Some(b) = base() else { return };
    let dir = tempfile::tempdir_in(b).unwrap();
    let (d, svc, root) = world(dir.path(), QUIET_2S).await;
    // While it runs: the detector commits it; the sweep passes leave it.
    let live = root.join("live.bin");
    write_as_user(&live, "live");
    let c_live = wait_committed(&d, &live, Duration::from_secs(20)).await;
    d.shutdown().await;

    let new_dir = root.join("survey");
    std::fs::create_dir(&new_dir).unwrap();
    let files: Vec<(PathBuf, String)> = (0..5)
        .map(|i| (root.join(format!("down_{i}.segy")), format!("down {i}")))
        .chain((0..3).map(|i| (new_dir.join(format!("deep_{i}.segy")), format!("deep {i}"))))
        .collect();
    for (p, data) in &files {
        write_as_user(p, data);
    }
    let d = start(config(dir.path(), QUIET_2S), &svc).await;
    let mut done = Vec::new();
    for (p, _) in &files {
        done.push(wait_committed(&d, p, Duration::from_secs(30)).await);
    }
    let survey_id = d
        .core
        .replica
        .lock()
        .await
        .node_by_ino(
            std::os::unix::fs::MetadataExt::ino(&std::fs::metadata(&new_dir).unwrap()),
            sys::inode_key(sys::open_dir(&new_dir).unwrap().as_fd())
                .unwrap()
                .ino_gen,
        )
        .unwrap()
        .and_then(|n| n.file_id)
        .expect("the new folder is in the catalog");
    // Let any duplicate show up before counting.
    tokio::time::sleep(Duration::from_secs(5)).await;
    for ((p, data), c) in files.iter().zip(&done) {
        let parent = if p.parent() == Some(new_dir.as_path()) {
            survey_id
        } else {
            100
        };
        once(
            &svc,
            parent,
            p.file_name().unwrap().to_str().unwrap(),
            *c,
            data,
        );
    }
    once(&svc, 100, "live.bin", c_live, "live");
    d.shutdown().await;

    // Another restart: everything is known and committed; nothing again.
    let calls = svc.calls().len();
    let d = start(config(dir.path(), QUIET_2S), &svc).await;
    tokio::time::sleep(Duration::from_secs(8)).await;
    let again: Vec<String> = svc.calls()[calls..]
        .iter()
        .filter(|c| c.starts_with("create") || c.starts_with("upload"))
        .cloned()
        .collect();
    assert!(again.is_empty(), "committed again: {again:?}");
    d.shutdown().await;
}

/// A file still open for write when the daemon starts is not committed
/// while it stays open, however old its last write; once it is closed and
/// quiet it is. The detector is off: this is the sweep alone.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_file_open_for_write_at_restart_waits_for_its_close() {
    let _shared = FLOOD.read().await;
    let Some(b) = base() else { return };
    let dir = tempfile::tempdir_in(b).unwrap();
    let extra = "[commit]\nquiet_s = 1\ndetect = false";
    let (d, svc, root) = world(dir.path(), extra).await;
    d.shutdown().await;
    let p = root.join("held.segy");
    // Written once, then held open for write (fd 3) with no more writes.
    let mut writer = std::process::Command::new("sh")
        .arg("-c")
        .arg(format!(
            "exec 3>'{}'; printf held >&3; sleep 12",
            p.display()
        ))
        .spawn()
        .unwrap();
    tokio::time::sleep(Duration::from_secs(4)).await;
    let d = start(config(dir.path(), extra), &svc).await;
    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(6) {
        assert!(
            committed(&d, &p).await.is_none(),
            "committed while open for write"
        );
        assert_eq!(
            count(&svc, "create 100 held.segy"),
            0,
            "registered while open"
        );
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    writer.wait().unwrap();
    let c = wait_committed(&d, &p, Duration::from_secs(60)).await;
    once(&svc, 100, "held.segy", c, "held");
    d.shutdown().await;
}

/// A commit cut short after its `create` (the daemon stopped while it
/// cloned or hashed): the replica knows the file, with no version. The sweep
/// finishes it, with no second `create`.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_commit_cut_short_after_its_create_is_finished() {
    let _shared = FLOOD.read().await;
    let Some(b) = base() else { return };
    let dir = tempfile::tempdir_in(b).unwrap();
    let extra = "[commit]\nquiet_s = 1\ndetect = false";
    let (d, svc, root) = world(dir.path(), extra).await;
    let p = root.join("half.segy");
    write_as_user(&p, "half");
    // What `commit` had done when it stopped: registered and created.
    let file_id = {
        let ro = sys::open_ro(&p).unwrap();
        let key = sys::inode_key(ro.as_fd()).unwrap();
        let mut rep = d.core.replica.lock().await;
        let share = rep.node_by_file_id(100).unwrap().unwrap();
        let lid = rep
            .register_local(
                Some(share.lid),
                "half.segy",
                omnifile_replica::Kind::File,
                key.ino,
                key.ino_gen,
                omnifile_replica::LocalAttrs {
                    size: 4,
                    mtime_ns: 0,
                    uid: 0,
                    gid: 0,
                    mode: 0o644,
                },
            )
            .unwrap();
        let file_id = svc.next_id() + 50_000;
        rep.assign_file_id(lid, file_id, 1).unwrap();
        file_id
    };
    d.shutdown().await;
    let d = start(config(dir.path(), extra), &svc).await;
    let c = wait_committed(&d, &p, Duration::from_secs(30)).await;
    assert_eq!(c.0, file_id, "the registered file, not a new one");
    assert_eq!(count(&svc, "create 100 half.segy"), 0, "no second create");
    assert_eq!(count(&svc, &format!("upload_start {file_id}")), 1);
    d.shutdown().await;
}

/// What the edge makes itself is never committed by the sweep: placeholders
/// (marked), a file with a marker the replica doesn't know, a recall note,
/// and anything under `.omni/` (commit clones, rewind trees). A user's file
/// named like a note is committed.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn the_edges_own_files_are_never_committed_by_the_sweep() {
    let _shared = FLOOD.read().await;
    let Some(b) = base() else { return };
    let dir = tempfile::tempdir_in(b).unwrap();
    let extra = "[commit]\nquiet_s = 1\ndetect = false";
    let (d, svc, root) = world(dir.path(), extra).await;
    let data_root = d.core.cfg.data_root.clone();
    d.shutdown().await;
    // A recall note, written as the daemon writes them.
    let layout = omnifile_stub::Layout::new(&data_root);
    omnifile_stub::placeholder::write_note(&layout, Path::new("proj/keep.bin"), "recalling", 0, 0)
        .unwrap();
    let note = root.join(omnifile_stub::note_name(std::ffi::OsStr::new("keep.bin")));
    assert!(note.exists(), "{}", note.display());
    // A stub the replica lost track of: a partial marker, no node.
    let stray = root.join("stray_stub.segy");
    std::fs::write(&stray, vec![0u8; 4096]).unwrap();
    let m =
        omnifile_marker::Marker::partial(999, 9990, 4096, 0, 1, omnifile_marker::Flags::empty());
    xattr::write(sys::open_ro(&stray).unwrap().as_fd(), &m).unwrap();
    // The daemon's own trees.
    for rel in [
        ".omni/commit/77.1",
        ".omni/snap/proj/2026-11-03T0000Z/x.segy",
    ] {
        let p = data_root.join(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        write_as_user(&p, "internal");
    }
    // A user's file whose name only looks like a note.
    let lookalike = root.join("mine.RECALLING.txt");
    write_as_user(&lookalike, "mine");
    tokio::time::sleep(Duration::from_secs(4)).await;

    let d = start(config(dir.path(), extra), &svc).await;
    let c = wait_committed(&d, &lookalike, Duration::from_secs(30)).await;
    once(&svc, 100, "mine.RECALLING.txt", c, "mine");
    tokio::time::sleep(Duration::from_secs(6)).await;
    let creates: Vec<String> = svc
        .calls()
        .into_iter()
        .filter(|c| c.starts_with("create"))
        .collect();
    assert_eq!(
        creates,
        vec!["create 100 mine.RECALLING.txt".to_string()],
        "only the user's file"
    );
    assert!(
        !svc.calls()
            .iter()
            .any(|c| c.starts_with("upload_start 103")),
        "the placeholder is not uploaded"
    );
    // The edge's files are as they were.
    assert!(matches!(
        xattr::read(sys::open_ro(&stray).unwrap().as_fd()).unwrap(),
        ReadOutcome::Marker(m) if !m.is_fully_resident()
    ));
    // A clone no upload names (the fake service has no outbox) is an orphan
    // of a commit cut short: removed at start.
    assert!(
        !data_root.join(".omni/commit/77.1").exists(),
        "orphaned commit clone removed"
    );
    d.shutdown().await;
}

/// The lead's case (WP9): a file already committed is overwritten, and the
/// daemon (the real binary, as a child) is SIGKILLed 1 s after the close,
/// inside the quiet period. The sweep can't tell this from an unchanged
/// file, and there is no fd store outside systemd: only the log of pending
/// closes has it. The next instance commits the new content.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn an_overwrite_closed_one_second_before_a_sigkill_is_committed() {
    let _shared = FLOOD.read().await;
    let Some(b) = base() else { return };
    let dir = tempfile::tempdir_in(b).unwrap();
    let extra = "[commit]\nquiet_s = 3";
    let (d, svc, root) = world(dir.path(), extra).await;
    // The child's own fake service must not repeat our version ids.
    for _ in 0..5000 {
        svc.next_id();
    }
    let p = root.join("survey_7.segy");
    write_as_user(&p, "first take");
    let (file_id, v1) = wait_committed(&d, &p, Duration::from_secs(20)).await;
    d.shutdown().await;

    let conf = dir.path().join("edge.toml");
    std::fs::write(&conf, toml(dir.path(), extra)).unwrap();
    let log_path = dir.path().join("child.log");
    let log = std::fs::File::create(&log_path).unwrap();
    let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_omnifile-edge"))
        .arg("--config")
        .arg(&conf)
        .stdout(log.try_clone().unwrap())
        .stderr(log)
        .spawn()
        .unwrap();
    eventually(
        "the child's detector is armed",
        Duration::from_secs(20),
        || std::fs::read_to_string(&log_path).is_ok_and(|l| l.contains("write detector armed")),
    )
    .await;
    write_as_user(&p, "second take, longer");
    tokio::time::sleep(Duration::from_secs(1)).await;
    // SAFETY: plain syscall on the child's pid.
    assert_eq!(unsafe { libc::kill(child.id() as i32, libc::SIGKILL) }, 0);
    child.wait().unwrap();
    assert!(
        matches!(
            xattr::read(sys::open_ro(&p).unwrap().as_fd()).unwrap(),
            ReadOutcome::Marker(m) if m.version_id == v1
        ),
        "the killed daemon had not committed the overwrite"
    );

    let d = start(config(dir.path(), extra), &svc).await;
    let deadline = Instant::now() + Duration::from_secs(30);
    let v2 = loop {
        if let Some((f, v)) = committed(&d, &p).await
            && v != v1
        {
            assert_eq!(f, file_id, "the same catalog file");
            break v;
        }
        assert!(
            Instant::now() < deadline,
            "the overwrite was never committed"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    };
    assert_eq!(
        svc.blob(file_id, v2).as_deref(),
        Some(&b"second take, longer"[..]),
        "the new bytes are in the store"
    );
    assert_eq!(count(&svc, "create 100 survey_7.segy"), 1, "one create");
    assert_eq!(
        count(&svc, &format!("upload_start {file_id}")),
        2,
        "the first take and the overwrite, nothing more"
    );
    d.shutdown().await;
}

/// The detector's group in the systemd fd store: the daemon runs as a
/// transient systemd service that keeps its store across restarts. It is
/// SIGKILLed, and a committed file is overwritten before systemd restarts
/// it. The overwrite's close is queued in the stored group, so the next
/// instance commits the new content. (The namespace sweep can't find this
/// one: the file is known and committed.)
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_close_while_the_daemon_restarts_is_read_from_the_fd_store() {
    let _shared = FLOOD.read().await;
    let Some(b) = base() else { return };
    if !Path::new("/run/systemd/system").is_dir() {
        eprintln!("skipping: needs systemd");
        return;
    }
    let dir = tempfile::tempdir_in(b).unwrap();
    let (d, svc, root) = world(dir.path(), QUIET_2S).await;
    // Version ids from the child's own fake service must not repeat ours.
    for _ in 0..5000 {
        svc.next_id();
    }
    let p = root.join("overwrite.segy");
    write_as_user(&p, "first");
    let (_, v1) = wait_committed(&d, &p, Duration::from_secs(20)).await;
    d.shutdown().await;

    let conf = dir.path().join("edge.toml");
    // The sweep at start stays off: only the stored group can see this close.
    std::fs::write(
        &conf,
        toml(
            dir.path(),
            "sweep_at_start = false\nsweep_s = 0\n[commit]\nquiet_s = 2",
        ),
    )
    .unwrap();
    let unit = format!("omnifile-wp9-fdstore-{}.service", std::process::id());
    let st = std::process::Command::new("systemd-run")
        .args([
            "--quiet",
            "--unit",
            &unit,
            "--property=Restart=always",
            "--property=RestartSec=3",
            "--property=RestartMode=direct",
            "--property=FileDescriptorStoreMax=8",
            "--property=FileDescriptorStorePreserve=yes",
            "--property=NotifyAccess=main",
            "--property=KillMode=mixed",
        ])
        .arg(env!("CARGO_BIN_EXE_omnifile-edge"))
        .arg("--config")
        .arg(&conf)
        .status()
        .unwrap();
    assert!(st.success(), "systemd-run");
    struct Stop(String);
    impl Drop for Stop {
        fn drop(&mut self) {
            let _ = std::process::Command::new("systemctl")
                .args(["stop", &self.0])
                .status();
            let _ = std::process::Command::new("systemctl")
                .args(["clean", "--what=fdstore", &self.0])
                .status();
        }
    }
    let _stop = Stop(unit.clone());
    let show = |prop: &str| -> String {
        let out = std::process::Command::new("systemctl")
            .args(["show", "-p", prop, "--value", &unit])
            .output()
            .unwrap();
        String::from_utf8_lossy(&out.stdout).trim().to_string()
    };
    eventually(
        "the group is in the fd store",
        Duration::from_secs(30),
        || show("NFileDescriptorStore") == "1",
    )
    .await;
    let pid1 = show("MainPID");
    let st = std::process::Command::new("systemctl")
        .args(["kill", "--signal=SIGKILL", "--kill-whom=main", &unit])
        .status()
        .unwrap();
    assert!(st.success());
    // Overwritten while the daemon is down (RestartSec=3).
    tokio::time::sleep(Duration::from_millis(300)).await;
    write_as_user(&p, "second!");
    eventually("restarted", Duration::from_secs(30), || {
        let pid = show("MainPID");
        pid != "0" && pid != pid1
    })
    .await;
    assert_eq!(
        show("NFileDescriptorStore"),
        "1",
        "the same group, not a second"
    );
    // The new instance commits the overwrite: a new RESIDENT version.
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        if let Ok(ReadOutcome::Marker(m)) = xattr::read(sys::open_ro(&p).unwrap().as_fd())
            && m.is_fully_resident()
            && m.version_id != v1
            && m.size == 7
        {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "the close queued while the daemon was down was never committed"
        );
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
}

/// WP3's soak, `restart_mid_fill`: `systemctl restart`, so SIGTERM and a
/// clean shutdown, 1 s after two closes: a new file and an overwrite of a
/// committed one. A clean stop doesn't wait out the quiet period; both are
/// still pending in the log, and the next instance commits them.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn closes_one_second_before_a_clean_restart_are_committed() {
    let _shared = FLOOD.read().await;
    let Some(b) = base() else { return };
    let dir = tempfile::tempdir_in(b).unwrap();
    let extra = "[commit]\nquiet_s = 3";
    let (d, svc, root) = world(dir.path(), extra).await;
    for _ in 0..5000 {
        svc.next_id();
    }
    let old = root.join("f00172.bin");
    write_as_user(&old, "the old version");
    let (file_id, v1) = wait_committed(&d, &old, Duration::from_secs(20)).await;
    d.shutdown().await;

    let conf = dir.path().join("edge.toml");
    std::fs::write(&conf, toml(dir.path(), extra)).unwrap();
    let log_path = dir.path().join("child.log");
    let log = std::fs::File::create(&log_path).unwrap();
    let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_omnifile-edge"))
        .arg("--config")
        .arg(&conf)
        .stdout(log.try_clone().unwrap())
        .stderr(log)
        .spawn()
        .unwrap();
    eventually(
        "the child's detector is armed",
        Duration::from_secs(20),
        || std::fs::read_to_string(&log_path).is_ok_and(|l| l.contains("write detector armed")),
    )
    .await;
    let new = root.join("s00300.bin");
    write_as_user(&new, "a new file");
    write_as_user(&old, "the overwrite, longer");
    tokio::time::sleep(Duration::from_secs(1)).await;
    // SAFETY: plain syscall on the child's pid.
    assert_eq!(unsafe { libc::kill(child.id() as i32, libc::SIGTERM) }, 0);
    let st = child.wait().unwrap();
    assert!(st.success(), "a clean shutdown: {st:?}");
    assert!(
        std::fs::read_to_string(&log_path).is_ok_and(|l| l.contains("stopped")),
        "the child stopped cleanly"
    );

    let d = start(config(dir.path(), extra), &svc).await;
    let c = wait_committed(&d, &new, Duration::from_secs(30)).await;
    once(&svc, 100, "s00300.bin", c, "a new file");
    let deadline = Instant::now() + Duration::from_secs(30);
    let v2 = loop {
        if let Some((f, v)) = committed(&d, &old).await
            && v != v1
        {
            assert_eq!(f, file_id);
            break v;
        }
        assert!(
            Instant::now() < deadline,
            "the overwrite was never committed"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    };
    assert_eq!(
        svc.blob(file_id, v2).as_deref(),
        Some(&b"the overwrite, longer"[..])
    );
    assert_eq!(count(&svc, &format!("upload_start {file_id}")), 2);
    d.shutdown().await;
}

/// Evict the dentry and inode caches, as a reboot does: an inode opened by
/// handle then comes back disconnected, with no path `/proc` can name.
fn cold_caches() {
    // SAFETY: sync has no preconditions.
    unsafe { libc::sync() };
    std::fs::write("/proc/sys/vm/drop_caches", "2").unwrap();
}

/// Start the real binary as a child on `dir`'s config, wait for its
/// detector, and return it.
async fn child_daemon(dir: &Path, extra: &str) -> std::process::Child {
    let conf = dir.join("edge.toml");
    std::fs::write(&conf, toml(dir, extra)).unwrap();
    let log_path = dir.join("child.log");
    let log = std::fs::File::create(&log_path).unwrap();
    let child = std::process::Command::new(env!("CARGO_BIN_EXE_omnifile-edge"))
        .arg("--config")
        .arg(&conf)
        .stdout(log.try_clone().unwrap())
        .stderr(log)
        .spawn()
        .unwrap();
    eventually(
        "the child's detector is armed",
        Duration::from_secs(20),
        || std::fs::read_to_string(&log_path).is_ok_and(|l| l.contains("write detector armed")),
    )
    .await;
    child
}

/// Wait until `p` is committed as a version other than `old`.
async fn wait_new_version(d: &D, p: &Path, old: u64) -> (u64, u64) {
    let deadline = Instant::now() + Duration::from_secs(60);
    loop {
        if let Some((f, v)) = committed(d, p).await
            && v != old
        {
            return (f, v);
        }
        assert!(
            Instant::now() < deadline,
            "the overwrite of {} was never committed",
            p.display()
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

/// WP9 review, finding 1: after a reboot (here, evicted caches) a pending
/// overwrite opened by handle is disconnected. It is found through the
/// replica's path instead and committed. A second one, renamed while the
/// daemon was down, isn't at the replica's path at start: it is looked for
/// again once the namespace sweep has moved its node, and committed too.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn pending_overwrites_are_replayed_with_cold_caches() {
    let _shared = FLOOD.read().await;
    let Some(b) = base() else { return };
    let dir = tempfile::tempdir_in(b).unwrap();
    let extra = "[commit]\nquiet_s = 3";
    let (d, svc, root) = world(dir.path(), extra).await;
    for _ in 0..5000 {
        svc.next_id();
    }
    let stay = root.join("stays.segy");
    let moved = root.join("moves.segy");
    write_as_user(&stay, "first");
    write_as_user(&moved, "first");
    let (f1, v1) = wait_committed(&d, &stay, Duration::from_secs(20)).await;
    let (f2, v2) = wait_committed(&d, &moved, Duration::from_secs(20)).await;
    d.shutdown().await;

    let mut child = child_daemon(dir.path(), extra).await;
    write_as_user(&stay, "second, longer");
    write_as_user(&moved, "second, longer");
    tokio::time::sleep(Duration::from_secs(1)).await;
    // SAFETY: plain syscall on the child's pid.
    assert_eq!(unsafe { libc::kill(child.id() as i32, libc::SIGKILL) }, 0);
    child.wait().unwrap();
    let renamed = root.join("moved_while_down.segy");
    std::fs::rename(&moved, &renamed).unwrap();
    cold_caches();

    let d = start(config(dir.path(), extra), &svc).await;
    let (g1, w1) = wait_new_version(&d, &stay, v1).await;
    let (g2, w2) = wait_new_version(&d, &renamed, v2).await;
    assert_eq!((g1, g2), (f1, f2), "the same catalog files");
    for (f, v) in [(f1, w1), (f2, w2)] {
        assert_eq!(svc.blob(f, v).as_deref(), Some(&b"second, longer"[..]));
        assert_eq!(count(&svc, &format!("upload_start {f}")), 2);
    }
    d.shutdown().await;
}

/// WP9 review, finding 3, and re-review: a close for a file whose upload the
/// outbox still holds is not committed a second time, and it stays pending
/// in the log. Here the daemon stops while the outbox holds the upload (its
/// own re-check of the live file, in memory, dies with it); the close is
/// replayed at the next start and the new bytes are committed, once.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_close_for_a_file_held_in_the_outbox_waits_and_survives_a_restart() {
    let _shared = FLOOD.read().await;
    let Some(b) = base() else { return };
    let dir = tempfile::tempdir_in(b).unwrap();
    let extra = "[commit]\nquiet_s = 1";
    let (d, svc, root) = world(dir.path(), extra).await;
    let p = root.join("held_upload.segy");
    write_as_user(&p, "first");
    let (file_id, v1) = wait_committed(&d, &p, Duration::from_secs(20)).await;
    // The first write's closes are all done before the outbox holds it.
    eventually("the first close is done", Duration::from_secs(10), || {
        d.core.pending.get().is_some_and(|l| l.is_empty())
    })
    .await;
    svc.set_outbox_holds(file_id, true);
    write_as_user(&p, "rewritten while held");
    let asked = format!("outbox_holds {file_id}");
    eventually(
        "the overwrite's close reached the commit",
        Duration::from_secs(10),
        || count(&svc, &asked) >= 1,
    )
    .await;
    tokio::time::sleep(Duration::from_secs(1)).await;
    assert_eq!(
        count(&svc, &format!("upload_start {file_id}")),
        1,
        "not uploaded again while the outbox holds it"
    );
    assert_eq!(
        d.core.pending.get().map(|l| l.len()),
        Some(1),
        "the close is still pending"
    );
    d.shutdown().await;
    // The outbox finishes (in the fake: lets go) while the daemon is down.
    svc.set_outbox_holds(file_id, false);
    let d = start(config(dir.path(), extra), &svc).await;
    let (f, v2) = wait_new_version(&d, &p, v1).await;
    assert_eq!(f, file_id);
    assert_eq!(
        svc.blob(file_id, v2).as_deref(),
        Some(&b"rewritten while held"[..])
    );
    eventually("the close is done", Duration::from_secs(10), || {
        d.core.pending.get().is_some_and(|l| l.is_empty())
    })
    .await;
    tokio::time::sleep(Duration::from_secs(3)).await;
    assert_eq!(count(&svc, &format!("upload_start {file_id}")), 2, "once");
    d.shutdown().await;
}

/// WP9 re-review: an outbox op whose finish fails (here its file can't be
/// reached: a folder on its path was swapped for a symlink) is kept, not
/// forgotten, and finished on a later pass once it can be.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn an_outbox_op_whose_finish_fails_is_kept_and_finished_later() {
    let _shared = FLOOD.read().await;
    let Some(b) = base() else { return };
    let dir = tempfile::tempdir_in(b).unwrap();
    // A 1 s finish backoff: every 10 s pass may retry the op (#52 review).
    let extra =
        "enabled = false\n[commit]\nquiet_s = 1\ndetect = false\noutbox_finish_backoff_s = 1";
    let cfg = config(dir.path(), extra);
    let root = cfg.data_root.join("proj");
    let svc = Arc::new(FakeService::new());
    let d = start(cfg, &svc).await;
    svc.push_event(events::share(1, 100, "proj", "fail", 1));
    svc.push_event(events::dir(100, None, "", 1));
    eventually("the share", Duration::from_secs(15), || root.is_dir()).await;
    let sub = root.join("sub");
    std::fs::create_dir(&sub).unwrap();
    let p = sub.join("f.segy");
    write_as_user(&p, "bytes");
    d.commits
        .send(omnifile_edge::ports::CloseWrite::new(p.clone(), 0))
        .await
        .unwrap();
    let (file_id, v) = wait_committed(&d, &p, Duration::from_secs(20)).await;
    // The folder swapped for a symlink to it: the path no longer resolves
    // without following one.
    let real = root.join("sub.real");
    std::fs::rename(&sub, &real).unwrap();
    std::os::unix::fs::symlink("sub.real", &sub).unwrap();
    let op = "7-424242";
    svc.set_outbox_finished(vec![(
        omnifile_edge::ports::OutboxUpload {
            op_id: op.into(),
            file_id,
            clone: d.core.cfg.data_root.join(".omni/commit/held.1"),
            size: 5,
            mtime_ns: 0,
            blake3_hex: String::new(),
            crc64nvme: 0,
            block_size: 4 << 20,
            block_crcs: vec![],
            base_version_id: Some(v),
            uid: 0,
            gid: 0,
            mode: 0o644,
        },
        omnifile_edge::ports::OutboxOutcome::Committed { version_id: v },
    )]);
    let forgot = format!("outbox_forget {op}");
    // The commit loop sees the outbox from its next event on (a close for a
    // name that isn't there: nothing to commit); then drives it every 10 s.
    d.commits
        .send(omnifile_edge::ports::CloseWrite::new(root.join("nudge"), 0))
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_secs(14)).await;
    assert_eq!(count(&svc, &forgot), 0, "kept while it can't be finished");
    std::fs::remove_file(&sub).unwrap();
    std::fs::rename(&real, &sub).unwrap();
    eventually("finished once reachable", Duration::from_secs(25), || {
        count(&svc, &forgot) == 1
    })
    .await;
    d.shutdown().await;
}

/// WP9 review, finding 4: names an earlier run's hard-link split or removal
/// left behind are never committed, and are removed where no bytes go with
/// them; the last link of a catalogued file is kept.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn split_leftovers_are_cleaned_up_not_committed() {
    let _shared = FLOOD.read().await;
    let Some(b) = base() else { return };
    let dir = tempfile::tempdir_in(b).unwrap();
    let extra = "[commit]\nquiet_s = 1\ndetect = false";
    let (d, svc, root) = world(dir.path(), extra).await;
    let kept_src = root.join("only_copy.segy");
    write_as_user(&kept_src, "catalogued");
    d.commits
        .send(omnifile_edge::ports::CloseWrite::new(kept_src.clone(), 0))
        .await
        .unwrap();
    wait_committed(&d, &kept_src, Duration::from_secs(20)).await;
    d.shutdown().await;
    // A split's copy (one link, unknown), and a moved-aside second link.
    let copy = root.join(".omnifile-split-1-123");
    write_as_user(&copy, "a copy");
    let user = root.join("user.segy");
    write_as_user(&user, "user data");
    let alias = root.join(".omnifile-unlink-1-456");
    std::fs::hard_link(&user, &alias).unwrap();
    // A catalogued file's last link under a temporary name: kept.
    let last = root.join(".omnifile-unlink-1-789");
    std::fs::rename(&kept_src, &last).unwrap();
    tokio::time::sleep(Duration::from_secs(3)).await;

    let d = start(config(dir.path(), extra), &svc).await;
    let c = wait_committed(&d, &user, Duration::from_secs(30)).await;
    once(&svc, 100, "user.segy", c, "user data");
    eventually("leftovers removed", Duration::from_secs(20), || {
        !copy.exists() && !alias.exists()
    })
    .await;
    tokio::time::sleep(Duration::from_secs(4)).await;
    assert!(last.exists(), "the last link of a catalogued file is kept");
    let temps: Vec<String> = svc
        .calls()
        .into_iter()
        .filter(|c| c.contains(".omnifile-"))
        .collect();
    assert!(
        temps.is_empty(),
        "nothing sent for a temporary name: {temps:?}"
    );
    d.shutdown().await;
}

/// WP9 re-review: a stop while a long upload runs and the commit queue is
/// full behind it (thousands of closes) doesn't wait for room in it. The
/// hand-over gives up at once, the detector drains its group into the log,
/// and what wasn't committed is still pending there for the next start.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_stop_with_a_full_commit_queue_is_prompt_and_keeps_the_closes() {
    // Thousands of closes on the shared filesystem: alone.
    let _alone = FLOOD.write().await;
    let Some(b) = base() else { return };
    let dir = tempfile::tempdir_in(b).unwrap();
    let extra = "enabled = false\n[commit]\nquiet_s = 1";
    let (d, svc, root) = world(dir.path(), extra).await;
    // One upload that outlasts the stop's 10 s wait for the commit task,
    // with the queue full behind it.
    svc.set_upload_delay_ms(60_000);
    let many = root.join("many");
    std::fs::create_dir(&many).unwrap();
    // More closes than the commit queue holds (4096), from one process.
    let st = std::process::Command::new("sh")
        .arg("-c")
        .arg(format!(
            "cd '{}' && i=0; while [ $i -lt 4600 ]; do echo $i > f$i; i=$((i+1)); done",
            many.display()
        ))
        .status()
        .unwrap();
    assert!(st.success());
    // Quiet, reported (each normalised on the way), and the queue full
    // behind the one long upload.
    tokio::time::sleep(Duration::from_secs(15)).await;
    let replica = d.core.cfg.replica_path.clone();
    let t0 = Instant::now();
    d.shutdown().await;
    let took = t0.elapsed();
    assert!(
        // The commit task's 10 s, and not another 10 s for the hand-over.
        took < Duration::from_secs(17),
        "the stop waited on the commit queue: {took:?}"
    );
    let (_, pending) = omnifile_edge::pending::PendingCloses::open(
        &omnifile_edge::pending::PendingCloses::path_for(&replica),
    )
    .unwrap();
    assert!(
        !pending.is_empty(),
        "the uncommitted closes are still pending"
    );
}

/// WP9 re-review: an outbox op whose finish keeps failing is tried again
/// with a doubling backoff, not at every 10 s pass, and after its last
/// attempt it is forgotten (its close stays pending in the log, and its file
/// is committed again from the live copy).
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_failing_outbox_finish_backs_off_then_is_given_up() {
    let _shared = FLOOD.read().await;
    let Some(b) = base() else { return };
    let dir = tempfile::tempdir_in(b).unwrap();
    let extra = "enabled = false\n[commit]\nquiet_s = 1\ndetect = false\n\
                 outbox_finish_backoff_s = 12\noutbox_finish_attempts = 3";
    let cfg = config(dir.path(), extra);
    let root = cfg.data_root.join("proj");
    let svc = Arc::new(FakeService::new());
    let d = start(cfg, &svc).await;
    svc.push_event(events::share(1, 100, "proj", "fail", 1));
    svc.push_event(events::dir(100, None, "", 1));
    eventually("the share", Duration::from_secs(15), || root.is_dir()).await;
    let sub = root.join("sub");
    std::fs::create_dir(&sub).unwrap();
    let p = sub.join("f.segy");
    write_as_user(&p, "bytes");
    d.commits
        .send(omnifile_edge::ports::CloseWrite::new(p.clone(), 0))
        .await
        .unwrap();
    let (file_id, v) = wait_committed(&d, &p, Duration::from_secs(20)).await;
    // Unreachable for good: the folder swapped for a symlink to it.
    let real = root.join("sub.real");
    std::fs::rename(&sub, &real).unwrap();
    std::os::unix::fs::symlink("sub.real", &sub).unwrap();
    let op = "7-515151";
    svc.set_outbox_finished(vec![(
        omnifile_edge::ports::OutboxUpload {
            op_id: op.into(),
            file_id,
            clone: d.core.cfg.data_root.join(".omni/commit/held.2"),
            size: 5,
            mtime_ns: 0,
            blake3_hex: String::new(),
            crc64nvme: 0,
            block_size: 4 << 20,
            block_crcs: vec![],
            base_version_id: Some(v),
            uid: 0,
            gid: 0,
            mode: 0o644,
        },
        omnifile_edge::ports::OutboxOutcome::Committed { version_id: v },
    )]);
    let forgot = format!("outbox_forget {op}");
    let t0 = Instant::now();
    d.commits
        .send(omnifile_edge::ports::CloseWrite::new(root.join("nudge"), 0))
        .await
        .unwrap();
    // Passes every 10 s: failures at about 0, 20 and 50 s with the backoff
    // (12 s, then 24 s); at 0, 10 and 20 s without it.
    tokio::time::sleep(Duration::from_secs(32)).await;
    assert_eq!(count(&svc, &forgot), 0, "backing off, not given up yet");
    eventually(
        "given up after its last attempt",
        Duration::from_secs(40),
        || count(&svc, &forgot) == 1,
    )
    .await;
    assert!(t0.elapsed() > Duration::from_secs(40), "{:?}", t0.elapsed());
    tokio::time::sleep(Duration::from_secs(12)).await;
    assert_eq!(count(&svc, &forgot), 1, "forgotten once");
    d.shutdown().await;
}
