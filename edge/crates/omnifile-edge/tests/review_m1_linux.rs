//! Regressions for the M1 review's daemon findings (B2, S3, S5, S7, S10,
//! and the orphaned-FETCHING nit), end to end: real SEQPACKET socket, real
//! xfs, fake service and blob store. Run as root on the dev VM:
//!
//! ```text
//! sudo -E OMNIFILE_TEST_DIR=/srv/acl/test cargo test -p omnifile-edge --test review_m1_linux
//! ```
#![cfg(target_os = "linux")]

use std::collections::HashSet;
use std::os::fd::AsFd;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use omnifile_edge::checks::startup_checks;
use omnifile_edge::config::Config;
use omnifile_edge::core::RecallEntry;
use omnifile_edge::daemon::Daemon;
use omnifile_edge::fakes::{FakeService, events};
use omnifile_edge::ports::CloseWrite;
use omnifile_fsal_proto::PacketIo;
use omnifile_fsal_proto::socket::SeqPacket;
use omnifile_fsal_proto::wire::{self, *};
use omnifile_marker::xattr;
use omnifile_marker::{Marker, State};
use omnifile_stub::sys;
use tokio::sync::mpsc;

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

fn config(dir: &Path, extra_fsal: &str) -> Config {
    omnifile_edge::init_logging("text", "info");
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
        recall_valid_ms = 3000
        invalidate_timeout_ms = 300
        {extra_fsal}
        [feed]
        wait_s = 1
        retry_s = 1
        [stub]
        interval_s = 1
        max_deferrals = 2
        [fill]
        chunk_mib = 1
        widen_every_mib = 1
        [commit]
        detect = false
        # Writes handed to the commit by hand wait out this period (#80 review).
        quiet_s = 1
        [log]
        format = "text"
        filter = "debug"
        "#,
        d = dir.display(),
        exe = exe.display(),
    );
    std::fs::create_dir_all(dir.join("data")).unwrap();
    Config::from_toml(&toml, Path::new("test.toml")).unwrap()
}

/// A scripted FSAL over the real socket that answers INVALIDATE itself
/// (while `ack` is set) and records every frame.
struct Fsal {
    io: Arc<SeqPacket>,
    rx: mpsc::UnboundedReceiver<Frame>,
    ack: Arc<AtomicBool>,
    next: u64,
}

impl Fsal {
    async fn connect(path: &Path) -> Fsal {
        let io = Arc::new(SeqPacket::connect(path).await.unwrap());
        let (tx, rx) = mpsc::unbounded_channel();
        let ack = Arc::new(AtomicBool::new(true));
        let (rio, rack) = (io.clone(), ack.clone());
        tokio::spawn(async move {
            while let Ok(Some(rec)) = rio.recv().await {
                let f = wire::decode(&rec).expect("daemon sent a malformed record");
                if let Message::Invalidate(inv) = &f.msg
                    && inv.flags & INVALIDATE_ACK_REQUIRED != 0
                    && rack.load(Ordering::SeqCst)
                {
                    let _ = rio
                        .send(&encode(
                            &Message::InvalidateAck {
                                inval_seq: inv.inval_seq,
                            },
                            f.header.req_id,
                        ))
                        .await;
                }
                if tx.send(f).is_err() {
                    return;
                }
            }
        });
        let pio = io.clone();
        tokio::spawn(async move {
            let mut n = 0u64;
            loop {
                tokio::time::sleep(Duration::from_secs(1)).await;
                n += 1;
                if pio
                    .send(&encode(&Message::Ping { mono_ns: n }, 1_000_000 + n))
                    .await
                    .is_err()
                {
                    return;
                }
            }
        });
        let mut f = Fsal {
            io,
            rx,
            ack,
            next: 1,
        };
        f.send(&Message::Hello(Hello {
            proto_major: 0,
            proto_minor: 1,
            features: feature::PARK,
            pid: std::process::id(),
            instance: 5,
            ganesha_version: b"V15.6".to_vec(),
            fsal_build: b"test".to_vec(),
        }))
        .await;
        f.expect(|m| matches!(m, Message::HelloAck(_))).await;
        f.expect(|m| matches!(m, Message::ResyncEnd)).await;
        f
    }

    async fn send(&mut self, m: &Message) -> u64 {
        let id = self.next;
        self.next += 1;
        self.io.send(&encode(m, id)).await.unwrap();
        id
    }

    async fn expect(&mut self, pred: impl Fn(&Message) -> bool) -> Message {
        let deadline = Instant::now() + Duration::from_secs(15);
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            match tokio::time::timeout(left, self.rx.recv()).await {
                Ok(Some(f)) if pred(&f.msg) => return f.msg,
                Ok(Some(_)) => continue,
                _ => panic!("timed out waiting for a message"),
            }
        }
    }

    /// Every RECALL_STATE for `file_id` until `pred` matches one, inclusive.
    async fn states_until(
        &mut self,
        file_id: u64,
        pred: impl Fn(&RecallState) -> bool,
    ) -> Vec<RecallState> {
        let mut seen = Vec::new();
        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            assert!(
                Instant::now() < deadline,
                "no matching RECALL_STATE for {file_id}: {seen:?}"
            );
            if let Message::RecallState(s) = self
                .expect(|m| matches!(m, Message::RecallState(s) if s.obj.file_id == file_id))
                .await
            {
                let done = pred(&s);
                seen.push(s);
                if done {
                    return seen;
                }
            }
        }
    }
}

async fn eventually(what: &str, mut f: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(15);
    while !f() {
        assert!(Instant::now() < deadline, "timed out: {what}");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

fn marker(p: &Path) -> Option<Marker> {
    let fd = sys::open_ro(p).ok()?;
    xattr::read_required(fd.as_fd()).ok()
}

fn obj_of(p: &Path, file_id: u64) -> ObjRef {
    let fd = sys::open_ro(p).unwrap();
    let k = sys::inode_key(fd.as_fd()).unwrap();
    ObjRef {
        fsid: k.fsid,
        ino: k.ino,
        ino_gen: k.ino_gen,
        export_id: 1,
        file_id,
    }
}

fn intent(p: &Path, file_id: u64, version_id: u64) -> ReadIntent {
    let m = marker(p).unwrap();
    ReadIntent {
        obj: obj_of(p, file_id),
        version_id,
        offset: 0,
        length: 65536,
        size: m.size,
        resident_bytes: m.resident_bytes,
        op: ReadOp::Read,
        policy: Policy::FailFast,
        marker_flags: m.flags.bits(),
        quick_ms: 0,
        parked: false,
        identity: Identity {
            uid: 41023,
            sec: 1,
            addr_family: 4,
            addr: [10, 1, 2, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            origin: Origin::Request,
            ..Identity::default()
        },
        principal: vec![],
        path_hint: p.to_string_lossy().as_bytes().to_vec(),
    }
}

fn pattern(n: usize) -> Vec<u8> {
    (0..n).map(|i| (i * 31 + i / 997) as u8).collect()
}

fn called(svc: &FakeService, prefix: &str) -> bool {
    svc.calls().iter().any(|c| c.starts_with(prefix))
}

/// Wait for a service call starting with `prefix`; on timeout, show every
/// call made.
async fn wait_call(svc: &FakeService, prefix: &str) {
    let deadline = Instant::now() + Duration::from_secs(15);
    while !called(svc, prefix) {
        assert!(
            Instant::now() < deadline,
            "timed out waiting for {prefix}; calls: {:?}",
            svc.calls()
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

/// The share `proj` (failfast) with its root directory.
fn share(svc: &FakeService) {
    svc.push_event(events::share(1, 100, "proj", "fail", 1));
    svc.push_event(events::dir(100, None, "", 1));
}

/// Commit a new local file; returns its path and marker.
async fn committed(
    d: &Daemon<FakeService, FakeService>,
    data_root: &Path,
    name: &str,
    data: &[u8],
) -> (PathBuf, Marker) {
    let b = data_root.join("proj").join(name);
    eventually("share dir", || data_root.join("proj").is_dir()).await;
    std::fs::write(&b, data).unwrap();
    d.commits
        .send(CloseWrite {
            path: b.clone(),
            ino: std::fs::metadata(&b).unwrap().ino(),
            swept_gen: None,
            token: None,
            seen: None,
        })
        .await
        .unwrap();
    eventually("commit", || marker(&b).is_some()).await;
    let m = marker(&b).unwrap();
    assert_eq!(m.state, State::Resident);
    // The marker is written before the commit is recorded: until the
    // replica clears `local_dirty`, a feed event for the file is held as a
    // conflict (the edge's own change wins), so a stub instruction pushed
    // now would be dropped. Wait for the commit to be fully recorded.
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let n = d
            .core
            .replica
            .lock()
            .await
            .node_by_file_id(m.file_id)
            .unwrap();
        if n.is_some_and(|n| !n.local_dirty && n.fs_ctime_ns.is_some()) {
            break;
        }
        assert!(Instant::now() < deadline, "commit never recorded");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    (b, m)
}

fn want_stub(svc: &FakeService, m: &Marker, name: &str, size: u64, ev: u64) {
    svc.push_event(events::file(
        m.file_id,
        100,
        name,
        size,
        m.version_id,
        None,
        false,
        Some((m.version_id, 0)),
        ev,
    ));
}

/// B2: with no FSAL connected nothing is narrowed (silence is not an ack);
/// once one connects and acks, the stub goes through.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn b2_no_narrowing_until_an_fsal_is_ready() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path(), "");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let (data_root, socket) = (cfg.data_root.clone(), cfg.socket_path.clone());
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    share(&svc);
    let data = pattern(3 << 20);
    // Committed while an FSAL is there to say no NFSv4 client holds it
    // open (#54); then Ganesha goes away.
    let first = Fsal::connect(&socket).await;
    let (b, m) = committed(&d, &data_root, "b2.bin", &data).await;
    first.io.shutdown().unwrap();
    drop(first);
    eventually("the FSAL gone", || d.server.ready_connections() == 0).await;
    want_stub(&svc, &m, "b2.bin", data.len() as u64, 10);
    // Several stub passes with no FSAL.
    tokio::time::sleep(Duration::from_millis(3500)).await;
    assert_eq!(marker(&b).unwrap().state, State::Resident, "not narrowed");
    assert_eq!(std::fs::read(&b).unwrap(), data, "nothing punched");
    assert!(!called(&svc, &format!("stub_skip {}", m.file_id)));
    assert!(!called(&svc, &format!("stub_commit {}", m.file_id)));
    // An FSAL connects: the stub goes through.
    let _fsal = Fsal::connect(&socket).await;
    wait_call(&svc, &format!("stub_commit {}", m.file_id)).await;
    assert_eq!(marker(&b).unwrap().resident_bytes, 0);
    d.shutdown().await;
}

/// B2: an edge configured with no Ganesha stubs without any FSAL.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn b2_an_edge_without_ganesha_stubs_without_an_fsal() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path(), "no_ganesha = true");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    share(&svc);
    let data = pattern(2 << 20);
    let (b, m) = committed(&d, &data_root, "nog.bin", &data).await;
    want_stub(&svc, &m, "nog.bin", data.len() as u64, 10);
    wait_call(&svc, &format!("stub_commit {}", m.file_id)).await;
    assert_eq!(marker(&b).unwrap().resident_bytes, 0);
    d.shutdown().await;
}

/// S5 (stubber): a stub whose commit call failed is retried and committed,
/// never reported skipped although its bytes are punched.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn s5_a_failed_stub_commit_is_retried_not_skipped() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path(), "");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let (data_root, socket) = (cfg.data_root.clone(), cfg.socket_path.clone());
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let _fsal = Fsal::connect(&socket).await;
    share(&svc);
    let data = pattern(2 << 20);
    let (b, m) = committed(&d, &data_root, "s5.bin", &data).await;
    svc.fail_stub_commits(1);
    want_stub(&svc, &m, "s5.bin", data.len() as u64, 10);
    eventually("second stub_commit", || {
        svc.calls()
            .iter()
            .filter(|c| c.starts_with(&format!("stub_commit {}", m.file_id)))
            .count()
            >= 2
    })
    .await;
    assert!(
        !called(&svc, &format!("stub_skip {}", m.file_id)),
        "{:?}",
        svc.calls()
    );
    assert_eq!(marker(&b).unwrap().resident_bytes, 0);
    d.shutdown().await;
}

/// A placeholder for an online object, filled slowly (`delay` per MiB).
async fn slow_placeholder(
    svc: &FakeService,
    data_root: &Path,
    file_id: u64,
    version_id: u64,
    mib: usize,
) -> (PathBuf, Vec<u8>) {
    let data = pattern(mib << 20);
    let crc = omnifile_integrity::crc64_nvme(&data);
    svc.put_blob(file_id, version_id, data.clone());
    let name = format!("f{file_id}.segy");
    svc.push_event(events::file(
        file_id,
        100,
        &name,
        data.len() as u64,
        version_id,
        Some(crc),
        false,
        None,
        1,
    ));
    let p = data_root.join("proj").join(&name);
    eventually("placeholder", || marker(&p).is_some()).await;
    (p, data)
}

/// S3: a recall cancelled (or failed) while its fill writes: the FSAL's
/// FETCHING fence stays until the fill has stopped writing; only then is
/// the terminal state pushed.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn s3_a_terminal_state_waits_for_the_fill_to_stop() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path(), "");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let (data_root, socket) = (cfg.data_root.clone(), cfg.socket_path.clone());
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let mut fsal = Fsal::connect(&socket).await;
    share(&svc);
    let (p, _data) = slow_placeholder(&svc, &data_root, 701, 7010, 16).await;
    svc.set_range_delay_ms(150);
    fsal.send(&Message::ReadIntent(intent(&p, 701, 7010))).await;
    fsal.states_until(701, |s| s.state == RecallPhase::Fetching)
        .await;
    // Let it write a little, then the portal cancels the recall.
    eventually("some bytes resident", || {
        marker(&p).is_some_and(|m| m.resident_bytes >= 1 << 20)
    })
    .await;
    svc.push_event(events::recall(9, 701, 7010, "cancelled", None, 2));
    let seen = fsal.states_until(701, |s| s.state.is_terminal()).await;
    let last = seen.last().unwrap();
    assert_eq!(last.state, RecallPhase::Cancelled, "{seen:?}");
    // Once the terminal state is out, the fill writes nothing more.
    let at_end = marker(&p).unwrap();
    tokio::time::sleep(Duration::from_millis(800)).await;
    assert_eq!(
        marker(&p).unwrap(),
        at_end,
        "the fill wrote after its fence ended"
    );
    assert_eq!(at_end.state, State::Partial, "cancelled before the end");
    d.shutdown().await;
}

/// S10: shutdown stops running fills, which end their FETCHING with FAILED
/// while the FSAL is still connected.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn s10_shutdown_stops_fills_and_ends_their_fence() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path(), "");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let (data_root, socket) = (cfg.data_root.clone(), cfg.socket_path.clone());
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let mut fsal = Fsal::connect(&socket).await;
    share(&svc);
    // 64 chunks at 150 ms: ~10 s, longer than the old 5 s lock wait.
    let (p, _data) = slow_placeholder(&svc, &data_root, 702, 7020, 64).await;
    svc.set_range_delay_ms(150);
    fsal.send(&Message::ReadIntent(intent(&p, 702, 7020))).await;
    fsal.states_until(702, |s| s.state == RecallPhase::Fetching)
        .await;
    let t = Instant::now();
    let stopping = tokio::spawn(d.shutdown());
    let seen = fsal.states_until(702, |s| s.state.is_terminal()).await;
    assert_eq!(seen.last().unwrap().state, RecallPhase::Failed, "{seen:?}");
    stopping.await.unwrap();
    assert!(
        t.elapsed() < Duration::from_secs(8),
        "shutdown waited for the fill"
    );
    let at_end = marker(&p).unwrap();
    assert_eq!(at_end.state, State::Partial);
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert_eq!(marker(&p).unwrap(), at_end, "no write after shutdown");
}

/// Nit: an entry left at FETCHING with no fill behind it (its task
/// panicked) ends with FAILED instead of being re-pushed forever.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn an_orphaned_fetching_state_ends_failed() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path(), "");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let mut fsal = Fsal::connect(&socket).await;
    let obj = ObjRef {
        fsid: 1,
        ino: 4242,
        ino_gen: 1,
        export_id: 0,
        file_id: 703,
    };
    d.core.recalls.lock().unwrap().insert(
        (703, 7030, 0),
        RecallEntry {
            obj,
            version_id: 7030,
            recall_id: 0,
            phase: RecallPhase::Fetching,
            answer: Decision::Delay,
            eta_s: 0,
            resident: 0,
            online: true,
            requesters: HashSet::new(),
        },
    );
    let seen = fsal
        .states_until(703, |s| s.state != RecallPhase::Fetching)
        .await;
    assert_eq!(
        (seen.last().unwrap().state, seen.last().unwrap().answer),
        (RecallPhase::Failed, Decision::Eio)
    );
    d.shutdown().await;
}

/// S7: the feed's redo never holds the replica lock while it waits for
/// FSAL acks (a content-version change of a placeholder).
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn s7_redo_does_not_hold_the_replica_across_invalidate_waits() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path(), "");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let (data_root, socket) = (cfg.data_root.clone(), cfg.socket_path.clone());
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let mut fsal = Fsal::connect(&socket).await;
    share(&svc);
    let (p, data) = slow_placeholder(&svc, &data_root, 704, 7040, 1).await;
    let ino = std::fs::metadata(&p).unwrap().ino();
    // The source object is rewritten: a content-version change, which
    // needs acknowledged invalidations. The FSAL stops acking.
    fsal.ack.store(false, Ordering::SeqCst);
    svc.push_event(events::file(
        704,
        100,
        "f704.segy",
        data.len() as u64 + 1,
        7041,
        None,
        false,
        None,
        2,
    ));
    fsal.expect(|m| {
        matches!(m, Message::Invalidate(i) if i.obj.ino == ino && i.flags & INVALIDATE_ACK_REQUIRED != 0)
    })
    .await;
    // The daemon now waits up to 300 ms for an ack that never comes.
    let mut free = false;
    for _ in 0..10 {
        if d.core.replica.try_lock().is_ok() {
            free = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(free, "the replica was locked across the INVALIDATE wait");
    fsal.ack.store(true, Ordering::SeqCst);
    eventually("new version", || {
        marker(&p).is_some_and(|m| m.version_id == 7041)
    })
    .await;
    d.shutdown().await;
}

/// A fill task aborted mid-way (or panicking) still ends its FETCHING
/// fence with FAILED: the FSAL never sweeps FETCHING by age.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn an_aborted_fill_still_ends_its_fence() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path(), "");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let (data_root, socket) = (cfg.data_root.clone(), cfg.socket_path.clone());
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let mut fsal = Fsal::connect(&socket).await;
    share(&svc);
    let (p, _data) = slow_placeholder(&svc, &data_root, 705, 7050, 16).await;
    svc.set_range_delay_ms(150);
    let node = d
        .core
        .replica
        .lock()
        .await
        .node_by_file_id(705)
        .unwrap()
        .unwrap();
    let task = tokio::spawn(d.core.clone().run_fill(
        (705, 7050, 0),
        node,
        omnifile_edge::ports::ReadUrl::azure("mem://705/7050"),
        None,
    ));
    fsal.states_until(705, |s| s.state == RecallPhase::Fetching)
        .await;
    eventually("some bytes resident", || {
        marker(&p).is_some_and(|m| m.resident_bytes >= 1 << 20)
    })
    .await;
    task.abort();
    let seen = fsal.states_until(705, |s| s.state.is_terminal()).await;
    assert_eq!(seen.last().unwrap().state, RecallPhase::Failed, "{seen:?}");
    assert!(!d.core.fills.is_running((705, 7050, 0)));
    d.shutdown().await;
}

/// The handshake keeps whole pages (an unaligned resident size is rounded
/// up), but `stub/commit` reports the size the lease was minted for: the
/// service refuses any other.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn stub_commit_reports_the_leased_size_not_the_page_rounded_one() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path(), "");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let (data_root, socket) = (cfg.data_root.clone(), cfg.socket_path.clone());
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let _fsal = Fsal::connect(&socket).await;
    share(&svc);
    let data = pattern(3 << 20);
    let (b, m) = committed(&d, &data_root, "pg.bin", &data).await;
    let asked: u64 = (1 << 20) + 3600;
    svc.push_event(events::file(
        m.file_id,
        100,
        "pg.bin",
        data.len() as u64,
        m.version_id,
        None,
        false,
        Some((m.version_id, asked)),
        10,
    ));
    wait_call(&svc, &format!("stub_commit {}", m.file_id)).await;
    let call = svc
        .calls()
        .into_iter()
        .find(|c| c.starts_with(&format!("stub_commit {}", m.file_id)))
        .unwrap();
    assert!(call.ends_with(&format!(" {asked}")), "{call}");
    let kept = marker(&b).unwrap().resident_bytes;
    assert!(kept >= asked && kept.is_multiple_of(4096), "{kept}");
    d.shutdown().await;
}

fn key_of(p: &Path, file_id: u64) -> omnifile_stub::InodeKey {
    let o = obj_of(p, file_id);
    omnifile_stub::InodeKey {
        fsid: o.fsid,
        ino: o.ino,
        ino_gen: o.ino_gen,
        file_id,
    }
}

async fn start_daemon(
    dir: &Path,
) -> (
    Daemon<FakeService, FakeService>,
    Arc<FakeService>,
    PathBuf,
    Fsal,
) {
    let cfg = config(dir, "");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let (data_root, socket) = (cfg.data_root.clone(), cfg.socket_path.clone());
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let fsal = Fsal::connect(&socket).await;
    share(&svc);
    (d, svc, data_root, fsal)
}

/// M2 B2: a PARTIAL file the guard saw written is marked dirty (never
/// stubbed or recorded as the version), filled whole, then committed.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn m2_b2_a_partial_file_written_is_filled_then_committed() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, data_root, _fsal) = start_daemon(dir.path()).await;
    let (p, data) = slow_placeholder(&svc, &data_root, 801, 8010, 2).await;
    let m = marker(&p).unwrap();
    d.core.on_partial_modified(key_of(&p, 801), &m);
    wait_call(&svc, "upload_start 801").await;
    eventually("resident", || {
        marker(&p).is_some_and(|m| m.state == State::Resident && m.version_id != 8010)
    })
    .await;
    assert_eq!(std::fs::read(&p).unwrap(), data);
    d.shutdown().await;
}

/// M2 S1: while the daemon holds the file (a content change or repair
/// runs), or a content-version change is still to be applied (a crash in
/// the middle of one), the guard must not act on a size mismatch.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn m2_s1_no_size_mismatch_verdict_while_the_daemon_resizes() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, data_root, fsal) = start_daemon(dir.path()).await;
    let (p, _data) = slow_placeholder(&svc, &data_root, 802, 8020, 1).await;
    let (k, m) = (key_of(&p, 802), marker(&p).unwrap());
    let core = d.core.clone();
    let may = move |k, m| {
        let c = core.clone();
        std::thread::spawn(move || c.may_act_on_size_mismatch(k, &m))
            .join()
            .unwrap()
    };
    assert!(may(k, m), "nothing running: the guard may act");
    {
        let _held = d.core.locks.try_lock(802).unwrap();
        assert!(!may(k, m), "the daemon holds the file");
    }
    // A content change the FSAL never acknowledges stays pending.
    fsal.ack.store(false, Ordering::SeqCst);
    svc.push_event(events::file(
        802,
        100,
        "f802.segy",
        3 << 20,
        8021,
        None,
        false,
        None,
        2,
    ));
    let deadline = Instant::now() + Duration::from_secs(10);
    while may(k, m) {
        assert!(Instant::now() < deadline, "the change never became pending");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    fsal.ack.store(true, Ordering::SeqCst);
    eventually("applied", || {
        marker(&p).is_some_and(|m| m.version_id == 8021)
    })
    .await;
    let m2 = marker(&p).unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while !may(k, m2) {
        assert!(Instant::now() < deadline, "still vetoed after the change");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    d.shutdown().await;
}

/// M2 S3: an O_APPEND-extended stub is repaired by the daemon (the remote
/// range fetched and verified, the user's tail kept) and committed as a
/// new version.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn m2_s3_an_extended_stub_is_repaired_and_committed() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, data_root, _fsal) = start_daemon(dir.path()).await;
    let (p, data) = slow_placeholder(&svc, &data_root, 803, 8030, 2).await;
    let m = marker(&p).unwrap();
    {
        use std::io::Write;
        let mut f = std::fs::OpenOptions::new().append(true).open(&p).unwrap();
        f.write_all(b"TAIL").unwrap();
    }
    let st = std::fs::metadata(&p).unwrap().len();
    assert!(d.core.on_stub_extended(key_of(&p, 803), &m, st));
    wait_call(&svc, "upload_start 803").await;
    let mut want = data.clone();
    want.extend_from_slice(b"TAIL");
    assert_eq!(std::fs::read(&p).unwrap(), want);
    eventually("new version", || {
        marker(&p).is_some_and(|m| m.state == State::Resident && m.size == want.len() as u64)
    })
    .await;
    d.shutdown().await;
}

/// A recall the service ended `superseded` (its file was replaced or deleted
/// while it was recalled, control API §4.4) while its fill writes: the fill
/// is stopped as for a cancelled recall, and nothing is reported. Before the
/// edge knew the state it was not terminal to it, so the fill ran on to the
/// end.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_superseded_recall_stops_its_fill_and_reports_nothing() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path(), "");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let (data_root, socket) = (cfg.data_root.clone(), cfg.socket_path.clone());
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let mut fsal = Fsal::connect(&socket).await;
    share(&svc);
    let (p, _data) = slow_placeholder(&svc, &data_root, 703, 7030, 16).await;
    svc.set_range_delay_ms(150);
    fsal.send(&Message::ReadIntent(intent(&p, 703, 7030))).await;
    fsal.states_until(703, |s| s.state == RecallPhase::Fetching)
        .await;
    eventually("some bytes resident", || {
        marker(&p).is_some_and(|m| m.resident_bytes >= 1 << 20)
    })
    .await;
    svc.push_event(events::recall(9, 703, 7030, "superseded", None, 2));
    let seen = fsal.states_until(703, |s| s.state.is_terminal()).await;
    let last = seen.last().unwrap();
    assert_eq!(last.state, RecallPhase::Cancelled, "{seen:?}");
    let at_end = marker(&p).unwrap();
    tokio::time::sleep(Duration::from_millis(800)).await;
    assert_eq!(
        marker(&p).unwrap(),
        at_end,
        "the fill wrote after its fence ended"
    );
    assert_eq!(at_end.state, State::Partial, "stopped before the end");
    let calls = svc.calls();
    assert!(
        !calls
            .iter()
            .any(|c| c.starts_with("recall_failed") || c.starts_with("recall_filled")),
        "{calls:?}"
    );
    d.shutdown().await;
}
