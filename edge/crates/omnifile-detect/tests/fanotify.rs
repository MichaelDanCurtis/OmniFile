//! fanotify end to end (Linux, root). Run in the dev VM:
//! `limactl shell omnifile-edge -- bash -lc 'cd /work/edge && sudo -E cargo test -p omnifile-detect'`.
//! Without CAP_SYS_ADMIN the tests skip, unless `OMNIFILE_REQUIRE_FANOTIFY=1`.
//! The scratch directory must be on a filesystem with fanotify filesystem
//! marks (ext4/xfs/btrfs): `OMNIFILE_DETECT_DIR`, default `/var/tmp`.

#![cfg(target_os = "linux")]

use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use omnifile_detect::{CloseWriteEvent, DetectConfig, run};
use tokio::sync::mpsc;

const QUIET: Duration = Duration::from_millis(300);

struct Rig {
    dir: tempfile::TempDir,
    rx: mpsc::Receiver<CloseWriteEvent>,
    task: tokio::task::JoinHandle<Result<(), omnifile_detect::DetectError>>,
}

impl Drop for Rig {
    fn drop(&mut self) {
        self.task.abort();
    }
}

async fn rig(watch_modify: bool) -> Option<Rig> {
    let base = std::env::var("OMNIFILE_DETECT_DIR").unwrap_or_else(|_| "/var/tmp".into());
    let dir = tempfile::tempdir_in(base).unwrap();
    let root = dir.path().canonicalize().unwrap();
    let mut cfg = DetectConfig::new(&root);
    cfg.quiet = QUIET;
    cfg.watch_modify = watch_modify;
    cfg.tick = Some(Duration::from_millis(50));
    let (tx, rx) = mpsc::channel(64);
    let task = tokio::spawn(run(cfg, tx));
    tokio::time::sleep(Duration::from_millis(200)).await;
    if task.is_finished() {
        let res = task.await.unwrap();
        if std::env::var("OMNIFILE_REQUIRE_FANOTIFY").as_deref() == Ok("1") {
            panic!("detector failed to start: {res:?}");
        }
        eprintln!("skipping: fanotify unavailable here ({res:?}); run as root in the dev VM");
        return None;
    }
    Some(Rig { dir, rx, task })
}

impl Rig {
    fn root(&self) -> PathBuf {
        self.dir.path().canonicalize().unwrap()
    }

    async fn next(&mut self, within: Duration) -> Option<CloseWriteEvent> {
        tokio::time::timeout(within, self.rx.recv())
            .await
            .ok()
            .flatten()
    }
}

/// Write from a child process: the detector ignores its own pid.
fn write_external(path: &Path, data: &str) {
    let status = std::process::Command::new("sh")
        .arg("-c")
        .arg(format!("printf '%s' '{data}' > '{}'", path.display()))
        .status()
        .unwrap();
    assert!(status.success());
}

/// Decision 67: files under `slow/` wait `slow`, the rest `QUIET`.
struct Periods {
    slow: std::sync::Mutex<Duration>,
    generation: std::sync::atomic::AtomicU64,
}

impl omnifile_detect::QuietSource for Periods {
    fn quiet_of(&self, path: &Path) -> Duration {
        let slow = path.components().any(|c| c.as_os_str() == "slow");
        if slow {
            *self.slow.lock().unwrap()
        } else {
            QUIET
        }
    }
    fn generation(&self) -> u64 {
        self.generation.load(std::sync::atomic::Ordering::SeqCst)
    }
}

/// A rig whose periods come from `periods`.
async fn rig_with(periods: Arc<Periods>) -> Option<Rig> {
    let base = std::env::var("OMNIFILE_DETECT_DIR").unwrap_or_else(|_| "/var/tmp".into());
    let dir = tempfile::tempdir_in(base).unwrap();
    let root = dir.path().canonicalize().unwrap();
    std::fs::create_dir(root.join("slow")).unwrap();
    let mut cfg = DetectConfig::new(&root);
    cfg.quiet = QUIET;
    cfg.tick = Some(Duration::from_millis(50));
    let (tx, rx) = mpsc::channel(64);
    let watch = omnifile_detect::Watch {
        periods: Some(periods),
        ..Default::default()
    };
    let task = tokio::spawn(omnifile_detect::run_full(cfg, None, watch, tx));
    tokio::time::sleep(Duration::from_millis(200)).await;
    if task.is_finished() {
        let res = task.await.unwrap();
        if std::env::var("OMNIFILE_REQUIRE_FANOTIFY").as_deref() == Ok("1") {
            panic!("detector failed to start: {res:?}");
        }
        eprintln!("skipping: fanotify unavailable here ({res:?}); run as root in the dev VM");
        return None;
    }
    Some(Rig { dir, rx, task })
}

/// Each file waits its own period; a period changed while a file waits applies
/// to it, longer and shorter, with no new write.
#[tokio::test(flavor = "multi_thread")]
async fn a_file_waits_its_own_period_and_a_change_reaches_it() {
    let periods = Arc::new(Periods {
        slow: std::sync::Mutex::new(Duration::from_secs(600)),
        generation: Default::default(),
    });
    let Some(mut r) = rig_with(periods.clone()).await else {
        return;
    };
    let fast = r.root().join("fast.bin");
    let slow = r.root().join("slow/slow.bin");
    write_external(&fast, "a");
    write_external(&slow, "b");
    let ev = r.next(Duration::from_secs(5)).await.expect("the fast file");
    assert_eq!(ev.path, fast);
    assert!(r.next(QUIET * 3).await.is_none(), "the slow file waits on");
    // Its period is cut: it is past the new one, and is reported.
    *periods.slow.lock().unwrap() = QUIET;
    periods
        .generation
        .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let ev = r.next(Duration::from_secs(5)).await.expect("the slow file");
    assert_eq!(ev.path, slow);
    // A longer period reaches a file already waiting.
    write_external(&slow, "c");
    tokio::time::sleep(QUIET / 3).await;
    *periods.slow.lock().unwrap() = Duration::from_secs(600);
    periods
        .generation
        .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    assert!(r.next(QUIET * 3).await.is_none(), "it waits the longer one");
}

/// How many of this process's descriptors name `name` (the detector runs in
/// this process, so its held files show here, `(deleted)` ones included).
fn held_fds(name: &str) -> usize {
    std::fs::read_dir("/proc/self/fd")
        .unwrap()
        .filter_map(|e| std::fs::read_link(e.ok()?.path()).ok())
        .filter(|l| l.to_string_lossy().contains(name))
        .count()
}

/// 2026-11-08 regression: a file deleted while it waits is let go within
/// seconds, whatever its period, and its pending-log token is finished. The
/// detector holds a waiting file open, so before this the file's blocks stayed
/// allocated until the period ended (a 62 GiB file kept a volume full for
/// 600 s). It is never reported.
#[tokio::test(flavor = "multi_thread")]
async fn a_file_deleted_while_it_waits_is_let_go() {
    use std::sync::Mutex;
    struct Log(Mutex<Vec<u64>>);
    impl omnifile_detect::PendingLog for Log {
        fn observed(&self, _f: &std::fs::File) -> u64 {
            41
        }
        fn finished(&self, token: u64) {
            self.0.lock().unwrap().push(token);
        }
    }
    let Some(r) = rig(true).await else { return };
    let root = r.root();
    r.task.abort();
    std::fs::create_dir(root.join("slow")).unwrap();
    let mut cfg = DetectConfig::new(&root);
    cfg.quiet = QUIET;
    cfg.tick = Some(Duration::from_millis(50));
    cfg.gone_every = Duration::from_millis(500);
    let log = Arc::new(Log(Mutex::new(Vec::new())));
    let watch = omnifile_detect::Watch {
        log: Some(log.clone()),
        periods: Some(Arc::new(Periods {
            slow: Mutex::new(Duration::from_secs(600)),
            generation: Default::default(),
        })),
        ..Default::default()
    };
    let (tx, mut rx) = mpsc::channel(64);
    let task = tokio::spawn(omnifile_detect::run_full(cfg, None, watch, tx));
    tokio::time::sleep(Duration::from_millis(200)).await;
    let p = root.join("slow/deleted-while-waiting.bin");
    write_external(&p, "some bytes");
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while held_fds("deleted-while-waiting.bin") == 0 {
        assert!(std::time::Instant::now() < deadline, "never waited on");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    std::fs::remove_file(&p).unwrap();
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    while held_fds("deleted-while-waiting.bin") != 0 {
        assert!(
            std::time::Instant::now() < deadline,
            "still held open after its delete: its blocks stay allocated"
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(*log.0.lock().unwrap(), vec![41], "its token is finished");
    let reported = tokio::time::timeout(QUIET * 3, rx.recv()).await;
    assert!(reported.is_err(), "a deleted file is never reported");
    // A file still there waits on, untouched by the check.
    let kept = root.join("slow/kept.bin");
    write_external(&kept, "x");
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert_eq!(
        held_fds("kept.bin"),
        1,
        "a file still there is still waited on"
    );
    task.abort();
}

/// The daemon's setting (`min_depth = 2`): a file directly under the root is
/// not waited on, so it is never held open.
#[tokio::test(flavor = "multi_thread")]
async fn a_file_directly_under_the_root_is_not_waited_on_at_depth_two() {
    let Some(r) = rig(true).await else { return };
    let root = r.root();
    r.task.abort();
    std::fs::create_dir(root.join("share")).unwrap();
    let mut cfg = DetectConfig::new(&root);
    cfg.quiet = Duration::from_secs(600);
    cfg.tick = Some(Duration::from_millis(50));
    cfg.min_depth = 2;
    let (tx, _rx) = mpsc::channel(64);
    let task = tokio::spawn(omnifile_detect::run_full(
        cfg,
        None,
        omnifile_detect::Watch::default(),
        tx,
    ));
    tokio::time::sleep(Duration::from_millis(200)).await;
    write_external(&root.join("stray-at-root.bin"), "x");
    write_external(&root.join("share/in-a-share.bin"), "x");
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while held_fds("in-a-share.bin") == 0 {
        assert!(std::time::Instant::now() < deadline, "never waited on");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(
        held_fds("stray-at-root.bin"),
        0,
        "held open, though never committed"
    );
    task.abort();
}

/// A file moved while it waited, into a share that waits longer, waits that
/// share's period.
#[tokio::test(flavor = "multi_thread")]
async fn a_file_moved_into_a_slower_share_waits_its_period() {
    let periods = Arc::new(Periods {
        slow: std::sync::Mutex::new(Duration::from_secs(2)),
        generation: Default::default(),
    });
    let Some(mut r) = rig_with(periods).await else {
        return;
    };
    let a = r.root().join("moving.bin");
    let b = r.root().join("slow/moving.bin");
    write_external(&a, "a");
    // The detector reads the write while it is still at the fast path.
    tokio::time::sleep(QUIET / 3).await;
    std::fs::rename(&a, &b).unwrap();
    assert!(r.next(QUIET * 3).await.is_none(), "not at the old period");
    let ev = r
        .next(Duration::from_secs(5))
        .await
        .expect("the moved file");
    assert_eq!(ev.path, b);
}

#[tokio::test(flavor = "multi_thread")]
async fn close_write_then_quiet_emits_once() {
    let Some(mut r) = rig(true).await else { return };
    let p = r.root().join("line_1042.segy");
    write_external(&p, "hello");
    let ev = r.next(Duration::from_secs(5)).await.expect("event");
    assert_eq!(ev.path, p);
    assert_eq!(ev.size, 5);
    assert!(ev.closed);
    assert!(r.next(QUIET * 3).await.is_none(), "only once");
}

#[tokio::test(flavor = "multi_thread")]
async fn repeated_writes_within_the_quiet_period_coalesce() {
    let Some(mut r) = rig(true).await else { return };
    let p = r.root().join("f.bin");
    for i in 0..5 {
        write_external(&p, &"x".repeat(i + 1));
        tokio::time::sleep(QUIET / 3).await;
    }
    let ev = r.next(Duration::from_secs(5)).await.expect("event");
    assert_eq!(ev.size, 5, "the last version");
    assert!(r.next(QUIET * 3).await.is_none());
}

#[tokio::test(flavor = "multi_thread")]
async fn ignored_and_deleted_files_are_not_reported() {
    let Some(mut r) = rig(true).await else { return };
    let omni = r.root().join(".omni/commit");
    std::fs::create_dir_all(&omni).unwrap();
    write_external(&omni.join("clone"), "x");
    let gone = r.root().join("tmp.part");
    write_external(&gone, "x");
    std::fs::remove_file(&gone).unwrap();
    assert!(r.next(QUIET * 4).await.is_none());
}

#[tokio::test(flavor = "multi_thread")]
async fn renamed_file_is_reported_at_its_new_path() {
    let Some(mut r) = rig(true).await else { return };
    let a = r.root().join("upload.tmp");
    let b = r.root().join("final.segy");
    write_external(&a, "abc");
    std::fs::rename(&a, &b).unwrap();
    let ev = r.next(Duration::from_secs(5)).await.expect("event");
    assert_eq!(ev.path, b);
}

#[tokio::test(flavor = "multi_thread")]
async fn held_open_writer_goes_quiet_on_modify() {
    let Some(mut r) = rig(true).await else { return };
    let p = r.root().join("held.bin");
    // A child that writes, then keeps the file open for a while.
    let mut child = std::process::Command::new("sh")
        .arg("-c")
        .arg(format!("exec 3>'{}'; printf abc >&3; sleep 3", p.display()))
        .spawn()
        .unwrap();
    let ev = r
        .next(Duration::from_secs(2))
        .await
        .expect("event while still open");
    assert!(!ev.closed);
    assert_eq!(ev.size, 3);
    child.wait().unwrap();
    let ev = r
        .next(Duration::from_secs(3))
        .await
        .expect("event after close");
    assert!(ev.closed);
}

/// A file the detector's process wrote, closed last by a child that
/// inherited the descriptor (as a spawned `wbinfo` does between fork and
/// exec): still its own write, though the CLOSE_WRITE carries the child's pid.
#[tokio::test(flavor = "multi_thread")]
async fn own_writes_closed_by_a_child_are_ignored() {
    let Some(mut r) = rig(true).await else { return };
    let p = r.root().join("inherited");
    let mut f = std::fs::File::create(&p).unwrap();
    f.write_all(b"daemon").unwrap();
    // The child holds the file as its stdin and closes it last.
    let mut child = std::process::Command::new("sleep")
        .arg("0.2")
        .stdin(f)
        .spawn()
        .unwrap();
    child.wait().unwrap();
    assert!(r.next(QUIET * 4).await.is_none());
    // A child that writes is still a user's write.
    write_external(&p, "user");
    let ev = r.next(Duration::from_secs(5)).await.expect("event");
    assert_eq!(ev.path, p);
}

/// WP9: a close that lands while no detector reads the group (the daemon
/// restarting, its group held in the systemd fd store) is reported by the
/// next detector on the same group.
#[tokio::test(flavor = "multi_thread")]
async fn a_close_while_no_one_reads_is_reported_on_the_same_group() {
    let Some(r) = rig(true).await else { return };
    let root = r.root();
    // Only its scratch tree is used; this test runs its own detectors.
    r.task.abort();
    let mut cfg = DetectConfig::new(&root);
    cfg.quiet = QUIET;
    cfg.tick = Some(Duration::from_millis(50));
    let group = omnifile_detect::create_group().unwrap();
    assert!(omnifile_detect::usable_group(&group));
    // What systemd keeps: a second descriptor for the same group.
    let stored = group.try_clone().unwrap();
    let (tx, mut rx) = mpsc::channel(64);
    let first = tokio::spawn(omnifile_detect::run_with(cfg.clone(), Some(group), tx));
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(!first.is_finished(), "armed");
    first.abort();
    let _ = first.await;
    assert!(rx.recv().await.is_none(), "the first detector is gone");
    // Written and closed while no detector runs.
    let p = root.join("while_down.segy");
    write_external(&p, "queued");
    tokio::time::sleep(Duration::from_millis(100)).await;
    let (tx, mut rx) = mpsc::channel(64);
    let second = tokio::spawn(omnifile_detect::run_with(cfg, Some(stored), tx));
    let ev = tokio::time::timeout(Duration::from_secs(5), rx.recv())
        .await
        .ok()
        .flatten()
        .expect("the queued close is reported");
    assert_eq!(ev.path, p);
    assert!(ev.closed);
    second.abort();
}

/// WP9: every file the detector starts waiting on goes to the pending log
/// before it is reported, with the token in the event; and a file a previous
/// run was waiting on (`Watch::initial`) is reported after a quiet period
/// with its own token, with no new event for it.
#[tokio::test(flavor = "multi_thread")]
async fn waited_on_files_are_logged_and_replayed() {
    use std::sync::{Arc, Mutex};
    struct Log(Mutex<Vec<u64>>, Mutex<u32>);
    impl omnifile_detect::PendingLog for Log {
        fn observed(&self, _f: &std::fs::File) -> u64 {
            let mut v = self.0.lock().unwrap();
            let t = 100 + v.len() as u64;
            v.push(t);
            t
        }
        fn sync(&self) {
            *self.1.lock().unwrap() += 1;
        }
    }
    let Some(r) = rig(true).await else { return };
    let root = r.root();
    r.task.abort();
    let old = root.join("from_last_run.segy");
    write_external(&old, "pending");
    tokio::time::sleep(Duration::from_millis(200)).await;
    let mut cfg = DetectConfig::new(&root);
    cfg.quiet = QUIET;
    cfg.tick = Some(Duration::from_millis(50));
    let log = Arc::new(Log(Mutex::new(Vec::new()), Mutex::new(0)));
    let watch = omnifile_detect::Watch {
        initial: vec![(std::fs::File::open(&old).unwrap(), 7)],
        log: Some(log.clone()),
        inject: None,
        periods: None,
    };
    let (tx, mut rx) = mpsc::channel(64);
    let task = tokio::spawn(omnifile_detect::run_full(cfg, None, watch, tx));
    let ev = tokio::time::timeout(Duration::from_secs(5), rx.recv())
        .await
        .ok()
        .flatten()
        .expect("the replayed file");
    assert_eq!((ev.path.clone(), ev.token), (old, Some(7)));
    let p = root.join("new.segy");
    write_external(&p, "new");
    let ev = tokio::time::timeout(Duration::from_secs(5), rx.recv())
        .await
        .ok()
        .flatten()
        .expect("the new file");
    assert_eq!(ev.path, p);
    assert_eq!(ev.token, Some(100));
    assert_eq!(*log.0.lock().unwrap(), vec![100], "logged once");
    assert!(*log.1.lock().unwrap() >= 1, "made durable");
    task.abort();
}

/// WP9 review: a consumer that doesn't keep up (the commit pipeline busy
/// with a big upload) must not stop the reading. Every close is still read
/// and recorded in the pending log, with the receiver never read at all.
#[tokio::test(flavor = "multi_thread")]
async fn a_full_receiver_does_not_stop_the_reading() {
    use std::sync::{Arc, Mutex};
    struct Log(Mutex<u64>);
    impl omnifile_detect::PendingLog for Log {
        fn observed(&self, _f: &std::fs::File) -> u64 {
            let mut n = self.0.lock().unwrap();
            *n += 1;
            *n
        }
    }
    let Some(r) = rig(true).await else { return };
    let root = r.root();
    r.task.abort();
    let mut cfg = DetectConfig::new(&root);
    cfg.quiet = Duration::from_millis(100);
    cfg.tick = Some(Duration::from_millis(20));
    let log = Arc::new(Log(Mutex::new(0)));
    let watch = omnifile_detect::Watch {
        log: Some(log.clone()),
        ..Default::default()
    };
    // Room for one event, and nobody reads it.
    let (tx, _rx) = mpsc::channel(1);
    let task = tokio::spawn(omnifile_detect::run_full(cfg, None, watch, tx));
    tokio::time::sleep(Duration::from_millis(200)).await;
    for i in 0..6 {
        write_external(&root.join(format!("busy_{i}.segy")), "x");
        // Each one quiet (and reported) before the next is written.
        tokio::time::sleep(Duration::from_millis(400)).await;
    }
    assert_eq!(*log.0.lock().unwrap(), 6, "every close was read and logged");
    task.abort();
}

/// WP9 re-review: while the receiver is full, repeated closes of one file
/// are kept as one report (the newest), so the queue doesn't grow with them.
#[tokio::test(flavor = "multi_thread")]
async fn repeated_closes_of_one_file_wait_as_one_report() {
    let Some(r) = rig(true).await else { return };
    let root = r.root();
    r.task.abort();
    let mut cfg = DetectConfig::new(&root);
    cfg.quiet = Duration::from_millis(100);
    cfg.tick = Some(Duration::from_millis(20));
    let (tx, mut rx) = mpsc::channel(1);
    let task = tokio::spawn(omnifile_detect::run_full(
        cfg,
        None,
        omnifile_detect::Watch::default(),
        tx,
    ));
    tokio::time::sleep(Duration::from_millis(200)).await;
    // Fill the receiver's one slot.
    write_external(&root.join("first.segy"), "x");
    tokio::time::sleep(Duration::from_millis(400)).await;
    let p = root.join("again.segy");
    for i in 1..=5 {
        write_external(&p, &"y".repeat(i));
        tokio::time::sleep(Duration::from_millis(300)).await;
    }
    let mut got = Vec::new();
    while let Ok(Some(ev)) = tokio::time::timeout(Duration::from_millis(500), rx.recv()).await {
        got.push(ev);
    }
    let again: Vec<_> = got.iter().filter(|e| e.path == p).collect();
    assert_eq!(again.len(), 1, "one report for the five closes: {got:?}");
    assert_eq!(again[0].size, 5, "the newest");
    task.abort();
}

/// A group of another kind (bounded queue) is not taken for the detector's.
#[test]
fn a_bounded_group_is_not_usable() {
    // SAFETY: plain syscall; the result is checked.
    let fd = unsafe {
        libc::syscall(libc::SYS_fanotify_init, 0x1u32 /* CLOEXEC */, 0u32)
    };
    if fd < 0 {
        if std::env::var("OMNIFILE_REQUIRE_FANOTIFY").as_deref() == Ok("1") {
            panic!("fanotify_init: {}", std::io::Error::last_os_error());
        }
        eprintln!("skipping: fanotify unavailable here; run as root in the dev VM");
        return;
    }
    // SAFETY: a new descriptor we own.
    let fd = unsafe { <std::os::fd::OwnedFd as std::os::fd::FromRawFd>::from_raw_fd(fd as i32) };
    assert!(!omnifile_detect::usable_group(&fd));
}

#[tokio::test(flavor = "multi_thread")]
async fn own_writes_are_ignored() {
    let Some(mut r) = rig(true).await else { return };
    let mut f = std::fs::File::create(r.root().join("mine")).unwrap();
    f.write_all(b"daemon").unwrap();
    drop(f);
    assert!(r.next(QUIET * 4).await.is_none());
}

// --- only a file with no name left is let go (#94 review) -------------------------
//
// The check that lets a waiting file go is the commit decision for it: a file it
// drops is never reported. So it must drop on `nlink == 0` and nothing wider. Each
// test below waits a 2 s period under a 100 ms check, so the check has run many
// times over the file before its period ends.

/// A detector over `root/slow` (2 s period) with a 100 ms check for deleted files.
async fn checking_rig() -> Option<(
    PathBuf,
    mpsc::Receiver<CloseWriteEvent>,
    tokio::task::JoinHandle<Result<(), omnifile_detect::DetectError>>,
    Rig,
)> {
    // `rig` skips where fanotify is not to be had; its scratch directory is kept.
    let r = rig(true).await?;
    let root = r.root();
    r.task.abort();
    std::fs::create_dir(root.join("slow")).unwrap();
    let mut cfg = DetectConfig::new(&root);
    cfg.quiet = QUIET;
    cfg.tick = Some(Duration::from_millis(50));
    cfg.gone_every = Duration::from_millis(100);
    let watch = omnifile_detect::Watch {
        periods: Some(Arc::new(Periods {
            slow: std::sync::Mutex::new(Duration::from_secs(2)),
            generation: Default::default(),
        })),
        ..Default::default()
    };
    let (tx, rx) = mpsc::channel(64);
    let task = tokio::spawn(omnifile_detect::run_full(cfg, None, watch, tx));
    tokio::time::sleep(Duration::from_millis(200)).await;
    Some((root, rx, task, r))
}

async fn until_held(name: &str, n: usize) {
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while held_fds(name) != n {
        assert!(
            std::time::Instant::now() < deadline,
            "{name}: {} descriptors held, not {n}",
            held_fds(name)
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

fn ino_of(p: &Path) -> u64 {
    use std::os::unix::fs::MetadataExt;
    std::fs::metadata(p).unwrap().ino()
}

/// A file renamed while it waits still has a name: it is reported, under its
/// new one.
#[tokio::test(flavor = "multi_thread")]
async fn a_file_renamed_while_it_waits_is_still_reported() {
    let Some((root, mut rx, task, _dir)) = checking_rig().await else {
        return;
    };
    let first = root.join("slow/renamed-first.bin");
    let second = root.join("slow/renamed-second.bin");
    write_external(&first, "bytes");
    until_held("renamed-first.bin", 1).await;
    let ino = ino_of(&first);
    std::fs::rename(&first, &second).unwrap();
    let ev = tokio::time::timeout(Duration::from_secs(6), rx.recv())
        .await
        .expect("a renamed file was let go: it is never committed")
        .unwrap();
    assert_eq!((ev.ino, &ev.path), (ino, &second));
    task.abort();
}

/// A file renamed over another: the one it replaced has no name left and is
/// let go, never reported; the new one is reported, under the name it took.
#[tokio::test(flavor = "multi_thread")]
async fn the_file_renamed_over_another_is_still_reported() {
    let Some((root, mut rx, task, _dir)) = checking_rig().await else {
        return;
    };
    let target = root.join("slow/over-target.bin");
    let tmp = root.join("slow/over-incoming.tmp");
    write_external(&target, "the old bytes");
    until_held("over-target.bin", 1).await;
    write_external(&tmp, "the new bytes");
    until_held("over-incoming.tmp", 1).await;
    let (old_ino, new_ino) = (ino_of(&target), ino_of(&tmp));
    std::fs::rename(&tmp, &target).unwrap();
    // Both descriptors now read `…/over-target.bin` (the old one with
    // ` (deleted)`): the old inode's goes within a few checks.
    until_held("over-target.bin", 1).await;
    let ev = tokio::time::timeout(Duration::from_secs(6), rx.recv())
        .await
        .expect("the file renamed over another was let go: it is never committed")
        .unwrap();
    assert_eq!((ev.ino, &ev.path), (new_ino, &target));
    let more = tokio::time::timeout(Duration::from_secs(3), rx.recv()).await;
    assert!(
        more.is_err(),
        "the replaced file (inode {old_ino}) was reported: {more:?}"
    );
    task.abort();
}

/// A file with two names loses one: it still has a name, so it is still held
/// and reported. (Shares refuse hard links over NFS, decision 42; a local
/// process can still make one.)
#[tokio::test(flavor = "multi_thread")]
async fn a_file_that_loses_one_of_two_hard_links_is_still_reported() {
    let Some((root, mut rx, task, _dir)) = checking_rig().await else {
        return;
    };
    let name = root.join("slow/linked.bin");
    let other = root.join("slow/linked-other-name.bin");
    write_external(&name, "bytes");
    until_held("linked.bin", 1).await;
    let ino = ino_of(&name);
    std::fs::hard_link(&name, &other).unwrap();
    std::fs::remove_file(&other).unwrap();
    let ev = tokio::time::timeout(Duration::from_secs(6), rx.recv())
        .await
        .expect("a file that lost one of two names was let go: it is never committed")
        .unwrap();
    assert_eq!((ev.ino, &ev.path), (ino, &name));
    task.abort();
}
