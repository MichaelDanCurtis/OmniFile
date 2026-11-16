//! The daemon end to end on the dev VM: real SEQPACKET socket, real xfs,
//! fake service and blob store. Run as root (trusted.* xattrs):
//!
//! ```text
//! limactl shell omnifile-edge -- sudo -E cargo test -p omnifile-edge
//! ```
#![cfg(target_os = "linux")]

use std::os::fd::AsFd;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::time::{Duration, Instant};

use omnifile_edge::checks::{CheckError, startup_checks};
use omnifile_edge::config::Config;
use omnifile_edge::daemon::Daemon;
use omnifile_edge::fakes::{FakeService, events};
use omnifile_edge::ports::{CloseWrite, Seen};
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

fn config(dir: &Path) -> Config {
    let exe = std::env::current_exe().unwrap();
    config_fsal(dir, &exe.display().to_string(), "")
}

/// [`config`] with this `fsal.required_exe` and more `[fsal]` lines.
fn config_fsal(dir: &Path, exe: &str, fsal_extra: &str) -> Config {
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
        {fsal_extra}
        [feed]
        wait_s = 1
        retry_s = 1
        reconcile_s = 0
        reconcile_quiet_s = 0
        [stub]
        interval_s = 1
        max_deferrals = 2
        [fill]
        chunk_mib = 1
        widen_every_mib = 2
        [commit]
        detect = false
        # Writes handed to the commit by hand wait out this period (#80 review).
        quiet_s = 1
        [namespace]
        # These tests change the tree by hand; tests/nsync_linux.rs covers
        # users' renames and deletes reaching the service.
        enabled = false
        [log]
        format = "text"
        filter = "debug"
        "#,
        d = dir.display(),
        // The peer check pins the full path of the FSAL's executable.
    );
    std::fs::create_dir_all(dir.join("data")).unwrap();
    Config::from_toml(&toml, Path::new("test.toml")).unwrap()
}

/// A scripted FSAL over the real socket that answers INVALIDATE itself.
struct Fsal {
    io: Arc<SeqPacket>,
    rx: mpsc::UnboundedReceiver<Frame>,
    ack: Arc<AtomicBool>,
    /// What it answers to `OPEN_STATE_QUERY` (with `OPEN_STATE` offered).
    open_writes: Arc<AtomicU32>,
    next: u64,
}

impl Fsal {
    async fn connect(path: &Path) -> Fsal {
        Fsal::connect_with(path, feature::PARK).await
    }

    /// [`Fsal::connect`] offering `features` in its `HELLO`.
    async fn connect_with(path: &Path, features: u32) -> Fsal {
        Fsal::connect_instance(path, features, 5).await
    }

    /// [`Fsal::connect_with`] as Ganesha instance `instance` (a restarted
    /// Ganesha has a new one).
    async fn connect_instance(path: &Path, features: u32, instance: u64) -> Fsal {
        let io = Arc::new(SeqPacket::connect(path).await.unwrap());
        let (tx, rx) = mpsc::unbounded_channel();
        let ack = Arc::new(AtomicBool::new(true));
        let open_writes = Arc::new(AtomicU32::new(0));
        let (rio, rack, rwo) = (io.clone(), ack.clone(), open_writes.clone());
        tokio::spawn(async move {
            while let Ok(Some(rec)) = rio.recv().await {
                let f = wire::decode(&rec).expect("daemon sent a malformed record");
                if let Message::OpenStateQuery { .. } = &f.msg {
                    let _ = rio
                        .send(&encode(
                            &Message::OpenState {
                                write_opens: rwo.load(Ordering::SeqCst),
                            },
                            f.header.req_id,
                        ))
                        .await;
                }
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
        // Like the real FSAL: PING every second so the daemon sees a live peer.
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
            open_writes,
            next: 1,
        };
        f.send(&Message::Hello(Hello {
            proto_major: 0,
            proto_minor: 1,
            features,
            pid: std::process::id(),
            instance,
            ganesha_version: b"V15.6".to_vec(),
            fsal_build: b"test".to_vec(),
        }))
        .await;
        f.expect(|m| matches!(m, Message::HelloAck(_))).await;
        f.expect(|m| matches!(m, Message::ResyncEnd)).await;
        f
    }

    /// End the connection, as a Ganesha that stops does.
    fn close(self) {
        self.io.shutdown().unwrap();
    }

    async fn send(&mut self, m: &Message) -> u64 {
        let id = self.next;
        self.next += 1;
        self.io.send(&encode(m, id)).await.unwrap();
        id
    }

    /// Wait for a message matching `pred`, skipping others.
    async fn expect(&mut self, pred: impl Fn(&Message) -> bool) -> Message {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            match tokio::time::timeout(left, self.rx.recv()).await {
                Ok(Some(f)) if pred(&f.msg) => return f.msg,
                Ok(Some(_)) => continue,
                _ => panic!("timed out waiting for a message"),
            }
        }
    }
}

async fn eventually(what: &str, mut f: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
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

fn intent(p: &Path, file_id: u64, version_id: u64, policy: Policy) -> ReadIntent {
    let m = marker(p).unwrap();
    ReadIntent {
        obj: obj_of(p, file_id),
        version_id,
        offset: 0,
        length: 65536,
        size: m.size,
        resident_bytes: m.resident_bytes,
        op: ReadOp::Read,
        policy,
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

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn fetch_stub_commit_recall_and_shutdown() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path());
    omnifile_edge::init_logging("text", "info");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let mut fsal = Fsal::connect(&socket).await;

    // --- The feed materialises an online object as a placeholder.
    let data = pattern((5 << 20) + 77);
    let crc = omnifile_integrity::crc64_nvme(&data);
    svc.put_blob(501, 5010, data.clone());
    svc.push_event(events::share(1, 100, "proj", "fail", 1));
    svc.push_event(events::dir(100, None, "", 1));
    svc.push_event(events::file(
        501,
        100,
        "a.segy",
        data.len() as u64,
        5010,
        Some(crc),
        false,
        None,
        1,
    ));
    let a = data_root.join("proj/a.segy");
    eventually("placeholder", || marker(&a).is_some()).await;
    let m = marker(&a).unwrap();
    assert_eq!(
        (
            m.state,
            m.resident_bytes,
            m.file_id,
            m.version_id,
            m.share_id
        ),
        (State::Partial, 0, 501, 5010, 1)
    );
    assert_eq!(std::fs::metadata(&a).unwrap().blocks(), 0);
    assert_eq!(
        obj_of(&a, 501).fsid,
        vol.fsid,
        "the daemon's fsid is the FSAL's"
    );

    // --- A READ of the ONLINE range parks, the daemon fetches, then DONE.
    let id = fsal
        .send(&Message::ReadIntent(intent(
            &a,
            501,
            5010,
            Policy::FailFast,
        )))
        .await;
    let Message::ReadDecision(dec) = fsal.expect(|m| matches!(m, Message::ReadDecision(_))).await
    else {
        panic!()
    };
    assert_eq!(
        dec.decision,
        Decision::Park,
        "online data is parked, never EIO"
    );
    let _ = id;
    fsal.expect(|m| matches!(m, Message::RecallState(s) if s.state == RecallPhase::Done && s.version_id == 5010)).await;
    assert_eq!(
        std::fs::read(&a).unwrap(),
        data,
        "byte-identical after the fill"
    );
    assert_eq!(marker(&a).unwrap().state, State::Resident);
    assert!(
        svc.calls()
            .iter()
            .any(|c| c.starts_with("recall_start 501 5010 uid=Some(41023)"))
    );

    // --- The service wants it stubbed: lease, acked invalidation, punch, commit.
    svc.push_event(events::file(
        501,
        100,
        "a.segy",
        data.len() as u64,
        5010,
        Some(crc),
        false,
        Some((5010, 1 << 20)),
        2,
    ));
    fsal.expect(|m| matches!(m, Message::Invalidate(i) if i.obj.file_id == 501 && i.flags & INVALIDATE_ACK_REQUIRED != 0)).await;
    eventually("stub_commit", || {
        svc.calls().iter().any(|c| c.starts_with("stub_commit 501"))
    })
    .await;
    let m = marker(&a).unwrap();
    assert_eq!((m.state, m.resident_bytes), (State::Partial, 1 << 20));
    assert!(
        std::fs::metadata(&a).unwrap().blocks() * 512 <= (1 << 20) + 65536,
        "holes punched"
    );

    // --- A new local file is committed: create, upload, RESIDENT marker.
    let b = data_root.join("proj/new.bin");
    let bdata = pattern(3 << 20);
    std::fs::write(&b, &bdata).unwrap();
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
    let mb = marker(&b).unwrap();
    assert_eq!((mb.state, mb.size), (State::Resident, bdata.len() as u64));
    assert_eq!(
        svc.blob(mb.file_id, mb.version_id).unwrap(),
        bdata,
        "plain blob, original bytes"
    );
    // The marker is written before the clone is removed: wait for it.
    eventually("clone removed", || {
        std::fs::read_dir(data_root.join(".omni/commit"))
            .unwrap()
            .next()
            .is_none()
    })
    .await;
    // Committing again without a change is a no-op.
    let calls_before = svc.calls().len();
    d.commits
        .send(CloseWrite {
            path: b.clone(),
            ino: 0,
            swept_gen: None,
            token: None,
            seen: None,
        })
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(svc.calls().len(), calls_before);

    // --- Stubbing with an FSAL that never acks: narrowed, not punched, then skipped.
    fsal.ack.store(false, Ordering::SeqCst);
    let ev = mb.file_id;
    svc.push_event(events::file(
        ev,
        100,
        "new.bin",
        bdata.len() as u64,
        mb.version_id,
        None,
        false,
        Some((mb.version_id, 0)),
        10,
    ));
    eventually("stub_skip", || {
        svc.calls()
            .iter()
            .any(|c| c.starts_with(&format!("stub_skip {ev}")))
    })
    .await;
    assert_eq!(marker(&b).unwrap().resident_bytes, 0, "narrowed");
    assert_eq!(std::fs::read(&b).unwrap(), bdata, "but nothing punched");
    assert!(
        !svc.calls()
            .iter()
            .any(|c| c.starts_with(&format!("stub_commit {ev}")))
    );
    fsal.ack.store(true, Ordering::SeqCst);

    // --- An archived file on a failfast read: EIO, recall pushed, note written,
    // then the feed says downloading and the daemon fills it.
    svc.set_archived(true);
    let cdata = pattern(2 << 20);
    svc.push_event(events::file(
        601,
        100,
        "old.segy",
        cdata.len() as u64,
        6010,
        // A fill needs the catalog's whole-file CRC (S5).
        Some(omnifile_integrity::crc64_nvme(&cdata)),
        true,
        None,
        1,
    ));
    let c = data_root.join("proj/old.segy");
    eventually("archived placeholder", || marker(&c).is_some()).await;
    assert!(
        marker(&c)
            .unwrap()
            .flags
            .contains(omnifile_marker::Flags::ARCHIVED)
    );
    fsal.send(&Message::ReadIntent(intent(
        &c,
        601,
        6010,
        Policy::FailFast,
    )))
    .await;
    let Message::ReadDecision(dec) = fsal.expect(|m| matches!(m, Message::ReadDecision(_))).await
    else {
        panic!()
    };
    assert_eq!(dec.decision, Decision::Eio);
    let Message::RecallState(rs) = fsal
        .expect(
            |m| matches!(m, Message::RecallState(s) if s.obj.file_id == 601 && s.recall_id != 0),
        )
        .await
    else {
        panic!()
    };
    assert_eq!(
        (rs.state, rs.answer),
        (RecallPhase::Restoring, Decision::Eio)
    );
    let note = data_root.join("proj/old.segy.RECALLING.txt");
    eventually("note file", || note.exists()).await;
    svc.put_blob(601, 6010, cdata.clone());
    svc.push_event(events::recall(
        rs.recall_id,
        601,
        6010,
        "downloading",
        Some("mem://601/6010"),
        5,
    ));
    fsal.expect(|m| matches!(m, Message::RecallState(s) if s.obj.file_id == 601 && s.state == RecallPhase::Done)).await;
    assert_eq!(std::fs::read(&c).unwrap(), cdata);
    eventually("recall_filled", || {
        svc.calls()
            .iter()
            .any(|c| c.starts_with(&format!("recall_filled {}", rs.recall_id)))
    })
    .await;
    eventually("note removed", || !note.exists()).await;

    // --- Shutdown: every FSAL is told and disconnected; the socket is gone.
    d.shutdown().await;
    let Message::Error { code, .. } = fsal.expect(|m| matches!(m, Message::Error { .. })).await
    else {
        panic!()
    };
    assert_eq!(code, error_code::SHUTTING_DOWN);
    assert!(!socket.exists(), "socket file removed");
}

/// Rewind trees (decision 30): the share's labels appear under
/// `.omni/snap/<share>/`, unpopulated; `POPULATE_DIR` fills one from the
/// service's as-of listing with read-only placeholders of the versions valid
/// then, and subdirectories that are themselves unpopulated.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_rewind_label_is_populated_from_the_asof_listing() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path());
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.push_event(events::share_with_rewind(1, 100, "proj", 2, 1));
    svc.push_event(events::dir(100, None, "", 1));
    let snap = data_root.join(".omni/snap/proj");
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let today = omnifile_edge::rewind::label_of(now - now.rem_euclid(86_400));
    let label = snap.join(&today);
    eventually("label directories", || label.is_dir()).await;
    // Daily at 00:00 within 2 days of now: today's and yesterday's (the day
    // before's 00:00 is more than 2 days ago).
    let n = std::fs::read_dir(&snap).unwrap().count();
    assert_eq!(
        n,
        2,
        "{:?}",
        std::fs::read_dir(&snap).unwrap().collect::<Vec<_>>()
    );
    let m = marker(&label).unwrap();
    assert!(m.flags.contains(omnifile_marker::Flags::DIR_UNPOPULATED));
    assert!(m.flags.contains(omnifile_marker::Flags::READONLY));
    assert_eq!(m.file_id, 100);

    let t = omnifile_edge::rewind::label_time(&today).unwrap();
    let crc = omnifile_integrity::crc64_nvme(b"as it was");
    let mut old =
        events::file(901, 100, "a.segy", 9, 9010, Some(crc), false, None, 1)["file"].clone();
    old["mode"] = serde_json::json!(0o664);
    old["state"] = serde_json::json!("remote");
    let sub = events::dir(902, Some(100), "sub", 1)["file"].clone();
    svc.set_asof(100, &t, vec![old, sub]);

    let mut fsal = Fsal::connect(&socket).await;
    let populate = |p: &Path, file_id| {
        Message::PopulateDir(omnifile_fsal_proto::wire::PopulateDir {
            obj: obj_of(p, file_id),
            identity: Identity::default(),
            principal: vec![],
            path_hint: vec![],
        })
    };
    fsal.send(&populate(&label, 100)).await;
    let Message::PopulateResult(r) = fsal
        .expect(|m| matches!(m, Message::PopulateResult(_)))
        .await
    else {
        panic!()
    };
    assert_eq!(r.result, 1, "DONE");
    let m = marker(&label).unwrap();
    assert!(!m.flags.contains(omnifile_marker::Flags::DIR_UNPOPULATED));
    let a = label.join("a.segy");
    let am = marker(&a).unwrap();
    assert_eq!(
        (am.file_id, am.version_id, am.size, am.resident_bytes),
        (901, 9010, 9, 0)
    );
    assert!(am.flags.contains(omnifile_marker::Flags::READONLY));
    use std::os::unix::fs::PermissionsExt;
    let mode = std::fs::metadata(&a).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode & 0o222, 0, "read-only: {mode:o}");
    let sm = marker(&label.join("sub")).unwrap();
    assert!(sm.flags.contains(omnifile_marker::Flags::DIR_UNPOPULATED));
    assert_eq!(sm.file_id, 902);
    // Populated once: asking again needs no service call.
    let asked = svc
        .calls()
        .iter()
        .filter(|c| c.starts_with("asof "))
        .count();
    fsal.send(&populate(&label, 100)).await;
    let Message::PopulateResult(r) = fsal
        .expect(|m| matches!(m, Message::PopulateResult(_)))
        .await
    else {
        panic!()
    };
    assert_eq!(r.result, 1);
    assert_eq!(
        svc.calls()
            .iter()
            .filter(|c| c.starts_with("asof "))
            .count(),
        asked
    );
    // Eviction: an idle directory is emptied and marked unpopulated; the
    // next access fills it anew.
    assert_eq!(d.core.evict_idle_rewind(Duration::ZERO).await, 1);
    let m = marker(&label).unwrap();
    assert!(m.flags.contains(omnifile_marker::Flags::DIR_UNPOPULATED));
    assert!(!label.join("a.segy").exists() && !label.join("sub").exists());
    fsal.send(&populate(&label, 100)).await;
    let Message::PopulateResult(r) = fsal
        .expect(|m| matches!(m, Message::PopulateResult(_)))
        .await
    else {
        panic!()
    };
    assert_eq!(r.result, 1);
    assert!(label.join("a.segy").exists());
    assert_eq!(
        svc.calls()
            .iter()
            .filter(|c| c.starts_with("asof "))
            .count(),
        asked + 1
    );
    let am = marker(&a).unwrap();

    // Reading the old version: a fetch of that version into the rewind
    // placeholder, never into (or reported as) the live file.
    let old_bytes = b"as it was".to_vec();
    svc.put_blob(901, 9010, old_bytes.clone());
    let mut ri = intent(&a, 901, 9010, Policy::Wait);
    ri.marker_flags = am.flags.bits();
    fsal.send(&Message::ReadIntent(ri)).await;
    fsal.expect(|m| matches!(m, Message::RecallState(s) if s.obj.file_id == 901 && s.state == RecallPhase::Done)).await;
    assert_eq!(std::fs::read(&a).unwrap(), old_bytes);
    let am = marker(&a).unwrap();
    assert_eq!((am.resident_bytes, am.version_id), (9, 9010));
    assert!(
        !svc.calls().iter().any(|c| c.starts_with("file_filled 901")),
        "a rewind fill is not the live file's: {:?}",
        svc.calls()
    );
    // A directory a read is using is not evicted.
    assert_eq!(d.core.evict_idle_rewind(Duration::ZERO).await, 0);
    assert!(a.exists());
    // An ordinary directory is not a rewind directory: EIO.
    let live = data_root.join("proj");
    fsal.send(&populate(&live, 100)).await;
    let Message::PopulateResult(r) = fsal
        .expect(|m| matches!(m, Message::PopulateResult(_)))
        .await
    else {
        panic!()
    };
    assert_eq!(r.result, 3);
    d.shutdown().await;
}

/// An S3-resident file (M5): the recall's credential is an S3 presigned
/// URL, and the fill reads it as S3 (no range CRC; whole-file verified).
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_s3_presigned_recall_fills_through_the_s3_reader() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path());
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.set_archived(true);
    svc.push_event(events::share(1, 100, "proj", "wait", 1));
    svc.push_event(events::dir(100, None, "", 1));
    let data = pattern(3 << 20);
    svc.push_event(events::file(
        801,
        100,
        "s3.segy",
        data.len() as u64,
        8010,
        Some(omnifile_integrity::crc64_nvme(&data)),
        true,
        None,
        1,
    ));
    let x = data_root.join("proj/s3.segy");
    eventually("placeholder", || marker(&x).is_some()).await;
    let mut fsal = Fsal::connect(&socket).await;
    fsal.send(&Message::ReadIntent(intent(&x, 801, 8010, Policy::Wait)))
        .await;
    let Message::RecallState(rs) = fsal
        .expect(
            |m| matches!(m, Message::RecallState(s) if s.obj.file_id == 801 && s.recall_id != 0),
        )
        .await
    else {
        panic!()
    };
    svc.put_blob(801, 8010, data.clone());
    svc.push_event(events::recall_with_mode(
        rs.recall_id,
        801,
        8010,
        "downloading",
        Some("mem://801/8010"),
        "s3_presigned_url",
        5,
    ));
    fsal.expect(|m| matches!(m, Message::RecallState(s) if s.obj.file_id == 801 && s.state == RecallPhase::Done)).await;
    assert_eq!(std::fs::read(&x).unwrap(), data);
    let kinds = svc.range_sources();
    assert!(!kinds.is_empty());
    assert!(
        kinds
            .iter()
            .all(|k| *k == omnifile_stub::SourceKind::S3Presigned),
        "{kinds:?}"
    );
    d.shutdown().await;
}

/// A seeded S3 file (M5 e2e): the replica has no CRC for it, and the S3
/// credential carries the one the service took from S3. The fill is
/// verified against it; a credential with the wrong CRC never exposes a byte.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_s3_fill_is_verified_against_the_credentials_crc_when_the_replica_has_none() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path());
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.set_archived(true);
    svc.push_event(events::share(1, 100, "proj", "wait", 1));
    svc.push_event(events::dir(100, None, "", 1));
    let data = pattern(3 << 20);
    let crc = omnifile_integrity::crc64_nvme(&data);
    let mut fsal = Fsal::connect(&socket).await;
    for (file_id, name, sent, ev) in [(811, "bad.segy", crc ^ 1, 2), (812, "good.segy", crc, 4)] {
        let version_id = file_id * 10;
        svc.push_event(events::file(
            file_id,
            100,
            name,
            data.len() as u64,
            version_id,
            None, // seeded from a listing: no CRC in the catalog yet
            true,
            None,
            ev,
        ));
        let x = data_root.join("proj").join(name);
        eventually("placeholder", || marker(&x).is_some()).await;
        fsal.send(&Message::ReadIntent(intent(
            &x,
            file_id,
            version_id,
            Policy::Wait,
        )))
        .await;
        let Message::RecallState(rs) = fsal
            .expect(|m| {
                matches!(m, Message::RecallState(s) if s.obj.file_id == file_id && s.recall_id != 0)
            })
            .await
        else {
            panic!()
        };
        svc.put_blob(file_id, version_id, data.clone());
        let mut e = events::recall_with_mode(
            rs.recall_id,
            file_id,
            version_id,
            "downloading",
            Some(&format!("mem://{file_id}/{version_id}")),
            "s3_presigned_url",
            ev + 1,
        );
        e["recall"]["read_credential"]["crc64nvme"] = format!("{sent:016x}").into();
        svc.push_event(e);
        let want = if sent == crc {
            RecallPhase::Done
        } else {
            RecallPhase::Failed
        };
        fsal.expect(
            |m| matches!(m, Message::RecallState(s) if s.obj.file_id == file_id && s.state == want),
        )
        .await;
        let m = marker(&x).unwrap();
        if sent == crc {
            assert_eq!(std::fs::read(&x).unwrap(), data);
        } else {
            assert_eq!(
                m.resident_bytes, 0,
                "no byte of a mismatched S3 fill is exposed"
            );
        }
    }
    d.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn service_down_answers_from_local_state() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path());
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.push_event(events::share(1, 100, "proj", "wait", 1));
    svc.push_event(events::dir(100, None, "", 1));
    svc.push_event(events::file(
        701, 100, "x.segy", 4096, 7010, None, true, None, 1,
    ));
    let x = data_root.join("proj/x.segy");
    eventually("placeholder", || marker(&x).is_some()).await;
    svc.set_unavailable(true);
    let mut fsal = Fsal::connect(&socket).await;
    fsal.send(&Message::ReadIntent(intent(&x, 701, 7010, Policy::Wait)))
        .await;
    let Message::ReadDecision(dec) = fsal.expect(|m| matches!(m, Message::ReadDecision(_))).await
    else {
        panic!()
    };
    assert_eq!(
        dec.decision,
        Decision::Delay,
        "wait share: DELAY while the service is down"
    );
    let Message::RecallState(rs) = fsal
        .expect(|m| matches!(m, Message::RecallState(s) if s.obj.file_id == 701))
        .await
    else {
        panic!()
    };
    assert_eq!(
        (rs.state, rs.answer),
        (RecallPhase::Blocked, Decision::Delay)
    );
    // The service comes back: the queued recall goes through.
    svc.set_archived(true);
    svc.set_unavailable(false);
    eventually("queued recall sent", || {
        svc.calls()
            .iter()
            .any(|c| c.starts_with("recall_start 701"))
    })
    .await;
    d.shutdown().await;
}

#[test]
fn refuses_to_start_off_xfs() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let mut cfg = config(dir.path());
    cfg.data_root = "/run".into(); // tmpfs
    assert!(matches!(
        startup_checks(&cfg),
        Err(CheckError::NotXfs { .. })
    ));
    let mut cfg = config(dir.path());
    cfg.fill_root = Some("/run".into());
    assert!(matches!(
        startup_checks(&cfg),
        Err(CheckError::FillRootElsewhere { .. })
    ));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn detector_commits_a_file_written_by_another_process() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let mut cfg = config(dir.path());
    cfg.commit.detect = true;
    cfg.commit.quiet_s = 1;
    // No FSAL here: nothing to ask about NFSv4 opens (#54).
    cfg.fsal.no_ganesha = true;
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.push_event(events::share(1, 100, "proj", "fail", 1));
    svc.push_event(events::dir(100, None, "", 1));
    eventually("share root", || data_root.join("proj").is_dir()).await;
    tokio::time::sleep(Duration::from_millis(300)).await;
    // The detector ignores this process's own writes, so write from a child.
    let f = data_root.join("proj/from-nfs.bin");
    let st = std::process::Command::new("sh")
        .arg("-c")
        .arg(format!("head -c 3000000 /dev/urandom > '{}'", f.display()))
        .status()
        .unwrap();
    assert!(st.success());
    let deadline = Instant::now() + Duration::from_secs(20);
    while marker(&f).is_none() {
        assert!(
            Instant::now() < deadline,
            "the detector never committed the file"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let m = marker(&f).unwrap();
    assert_eq!((m.state, m.size), (State::Resident, 3_000_000));
    assert_eq!(
        svc.blob(m.file_id, m.version_id).unwrap(),
        std::fs::read(&f).unwrap()
    );
    d.shutdown().await;
}

/// Review B1: a recall row for an old version (a rewind read whose daemon
/// restarted, so nothing names its placeholder any more) is never filled
/// into the live file, which holds another version.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_old_versions_recall_row_never_fills_the_live_file() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path());
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.set_archived(true);
    svc.push_event(events::share(1, 100, "proj", "wait", 1));
    svc.push_event(events::dir(100, None, "", 1));
    let cur = pattern(8 << 20);
    svc.push_event(events::file(
        801,
        100,
        "live.segy",
        cur.len() as u64,
        8010,
        Some(omnifile_integrity::crc64_nvme(&cur)),
        true,
        None,
        1,
    ));
    let x = data_root.join("proj/live.segy");
    eventually("placeholder", || marker(&x).is_some()).await;
    let _fsal = Fsal::connect(&socket).await;
    // Version 7000's bytes, ready to download.
    svc.put_blob(801, 7000, vec![0xABu8; 5 << 20]);
    svc.push_event(events::recall(
        77,
        801,
        7000,
        "downloading",
        Some("mem://801/7000"),
        5,
    ));
    tokio::time::sleep(Duration::from_millis(1500)).await;
    let m = marker(&x).unwrap();
    assert_eq!((m.version_id, m.resident_bytes), (8010, 0));
    let bytes = std::fs::read(&x).unwrap_or_default();
    assert!(!bytes.contains(&0xAB) || bytes.iter().all(|b| *b == 0));
    d.shutdown().await;
}

/// Review B2: a rewind placeholder of the file's current version (an
/// unchanged file, the common case) is filled itself, not the live file.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_rewind_placeholder_of_the_current_version_is_filled_itself() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path());
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.push_event(events::share_with_rewind(1, 100, "proj", 2, 1));
    svc.push_event(events::dir(100, None, "", 1));
    let data = b"same bytes now and then".to_vec();
    let crc = omnifile_integrity::crc64_nvme(&data);
    let file = || {
        events::file(
            901,
            100,
            "a.segy",
            data.len() as u64,
            9010,
            Some(crc),
            false,
            None,
            1,
        )
    };
    svc.push_event(file());
    svc.put_blob(901, 9010, data.clone());
    let live = data_root.join("proj/a.segy");
    eventually("live placeholder", || marker(&live).is_some()).await;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let today = omnifile_edge::rewind::label_of(now - now.rem_euclid(86_400));
    let label = data_root.join(".omni/snap/proj").join(&today);
    eventually("label", || label.is_dir()).await;
    let t = omnifile_edge::rewind::label_time(&today).unwrap();
    svc.set_asof(100, &t, vec![file()["file"].clone()]);
    let mut fsal = Fsal::connect(&socket).await;
    fsal.send(&Message::PopulateDir(
        omnifile_fsal_proto::wire::PopulateDir {
            obj: obj_of(&label, 100),
            identity: Identity::default(),
            principal: vec![],
            path_hint: vec![],
        },
    ))
    .await;
    fsal.expect(|m| matches!(m, Message::PopulateResult(_)))
        .await;
    let a = label.join("a.segy");
    let am = marker(&a).unwrap();
    let mut ri = intent(&a, 901, 9010, Policy::Wait);
    ri.marker_flags = am.flags.bits();
    let ino = ri.obj.ino;
    fsal.send(&Message::ReadIntent(ri)).await;
    fsal.expect(|m| {
        matches!(m, Message::RecallState(s)
            if s.obj.ino == ino && s.state == RecallPhase::Done)
    })
    .await;
    let am = marker(&a).unwrap();
    assert_eq!(am.resident_bytes, am.size);
    assert_eq!(std::fs::read(&a).unwrap(), data);
    assert_eq!(
        marker(&live).unwrap().resident_bytes,
        0,
        "the live file untouched"
    );
    // The reader asked for that version itself: its recall is pinned, so the
    // service does not end it when the live file is replaced (control API §4.4).
    let calls = svc.calls();
    assert!(
        calls
            .iter()
            .any(|c| c.starts_with("recall_start 901 9010") && c.ends_with(" pinned")),
        "{calls:?}"
    );
    d.shutdown().await;
}

/// Review S2: after an ACL change, populated rewind directories are emptied
/// (they fill again with the ACLs of now) and the labels get the live share
/// root's permissions again.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_acl_change_rebuilds_the_rewind_trees_permissions() {
    use std::os::unix::fs::PermissionsExt;
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path());
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.push_event(events::share_with_rewind(1, 100, "proj", 2, 1));
    svc.push_event(events::dir(100, None, "", 1));
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let today = omnifile_edge::rewind::label_of(now - now.rem_euclid(86_400));
    let label = data_root.join(".omni/snap/proj").join(&today);
    eventually("label", || label.is_dir()).await;
    let made = std::fs::metadata(&label).unwrap().permissions().mode() & 0o777;
    let t = omnifile_edge::rewind::label_time(&today).unwrap();
    let crc = omnifile_integrity::crc64_nvme(b"as it was");
    let old = events::file(901, 100, "a.segy", 9, 9010, Some(crc), false, None, 1)["file"].clone();
    svc.set_asof(100, &t, vec![old]);
    let mut fsal = Fsal::connect(&socket).await;
    fsal.send(&Message::PopulateDir(
        omnifile_fsal_proto::wire::PopulateDir {
            obj: obj_of(&label, 100),
            identity: Identity::default(),
            principal: vec![],
            path_hint: vec![],
        },
    ))
    .await;
    fsal.expect(|m| matches!(m, Message::PopulateResult(_)))
        .await;
    assert!(label.join("a.segy").exists());
    // The label's permissions of before (as an ACL of before would leave them).
    std::fs::set_permissions(&label, std::fs::Permissions::from_mode(0o755)).unwrap();

    d.core
        .rewind_acls_stale
        .store(true, std::sync::atomic::Ordering::SeqCst);
    d.core.refresh_rewind_labels().await;
    let mode = std::fs::metadata(&label).unwrap().permissions().mode() & 0o777;
    assert_eq!(
        mode, made,
        "the label has the live root's permissions again"
    );
    assert!(!label.join("a.segy").exists(), "emptied");
    let m = marker(&label).unwrap();
    assert!(m.flags.contains(omnifile_marker::Flags::DIR_UNPOPULATED));
    d.shutdown().await;
}

/// Review S3: a presigned URL that expires mid-fill (403) is replaced by a
/// new read credential and the fill carries on, rather than failing.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_expired_read_url_mid_fill_is_replaced() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let mut cfg = config(dir.path());
    cfg.fill.chunk_mib = 1;
    cfg.fill.widen_every_mib = 1;
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.set_archived(true);
    svc.push_event(events::share(1, 100, "proj", "wait", 1));
    svc.push_event(events::dir(100, None, "", 1));
    let data = pattern(4 << 20);
    svc.push_event(events::file(
        802,
        100,
        "expiring.segy",
        data.len() as u64,
        8020,
        Some(omnifile_integrity::crc64_nvme(&data)),
        true,
        None,
        1,
    ));
    let x = data_root.join("proj/expiring.segy");
    eventually("placeholder", || marker(&x).is_some()).await;
    let mut fsal = Fsal::connect(&socket).await;
    fsal.send(&Message::ReadIntent(intent(&x, 802, 8020, Policy::Wait)))
        .await;
    let Message::RecallState(rs) = fsal
        .expect(
            |m| matches!(m, Message::RecallState(s) if s.obj.file_id == 802 && s.recall_id != 0),
        )
        .await
    else {
        panic!()
    };
    svc.put_blob(802, 8020, data.clone());
    svc.expire_url_at("mem://802/8020", 2 << 20);
    svc.push_event(events::recall_with_mode(
        rs.recall_id,
        802,
        8020,
        "downloading",
        Some("mem://802/8020"),
        "s3_presigned_url",
        5,
    ));
    fsal.expect(|m| {
        matches!(m, Message::RecallState(s)
            if s.obj.file_id == 802 && s.state == RecallPhase::Done)
    })
    .await;
    assert_eq!(std::fs::read(&x).unwrap(), data);
    assert!(svc.calls().iter().any(|c| c == "read_url 802 8020"));
    d.shutdown().await;
}

/// Review nits: an entry the edge can't make is skipped rather than failing
/// its directory, history is never set-id, and a removed share's tree goes.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_rewind_directory_skips_what_it_cannot_make() {
    use std::os::unix::fs::PermissionsExt;
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path());
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.push_event(events::share_with_rewind(1, 100, "proj", 2, 1));
    svc.push_event(events::dir(100, None, "", 1));
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let today = omnifile_edge::rewind::label_of(now - now.rem_euclid(86_400));
    let snap = data_root.join(".omni/snap");
    let label = snap.join("proj").join(&today);
    eventually("label", || label.is_dir()).await;
    let t = omnifile_edge::rewind::label_time(&today).unwrap();
    let crc = omnifile_integrity::crc64_nvme(b"x");
    let row = |id: u64, name: &str| {
        events::file(id, 100, name, 1, id * 10, Some(crc), false, None, 1)["file"].clone()
    };
    let long = "a".repeat(300);
    let mut setid = row(903, "b.segy");
    setid["mode"] = serde_json::json!(0o6755);
    let mut link = row(904, "c-link");
    link["kind"] = serde_json::json!("symlink");
    link["symlink_target"] = serde_json::json!("");
    svc.set_asof(
        100,
        &t,
        vec![row(901, &long), setid, link, row(905, "d.segy")],
    );
    let mut fsal = Fsal::connect(&socket).await;
    fsal.send(&Message::PopulateDir(
        omnifile_fsal_proto::wire::PopulateDir {
            obj: obj_of(&label, 100),
            identity: Identity::default(),
            principal: vec![],
            path_hint: vec![],
        },
    ))
    .await;
    let Message::PopulateResult(r) = fsal
        .expect(|m| matches!(m, Message::PopulateResult(_)))
        .await
    else {
        panic!()
    };
    assert_eq!(r.result, 1, "DONE despite the bad entries");
    let names: Vec<String> = std::fs::read_dir(&label)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    let mut names = names;
    names.sort();
    assert_eq!(names, ["b.segy", "d.segy"]);
    let mode = std::fs::metadata(label.join("b.segy"))
        .unwrap()
        .permissions()
        .mode();
    assert_eq!(mode & 0o6000, 0);

    // A tree of a share that is gone (a rewind directory of ours), and a
    // directory that is not ours: only the first is removed.
    let gone = snap.join("gone");
    std::fs::create_dir(&gone).unwrap();
    let raw = {
        let f = std::fs::File::open(snap.join("proj")).unwrap();
        xattr::get_raw(f.as_fd(), "trusted.omnifile", 256)
            .unwrap()
            .unwrap()
    };
    let g = std::fs::File::open(&gone).unwrap();
    xattr::set_raw(g.as_fd(), "trusted.omnifile", &raw, 0).unwrap();
    let foreign = snap.join("foreign");
    std::fs::create_dir(&foreign).unwrap();
    d.core
        .rewind_acls_stale
        .store(true, std::sync::atomic::Ordering::SeqCst);
    d.core.refresh_rewind_labels().await;
    assert!(!gone.exists() && foreign.exists() && label.exists());
    d.shutdown().await;
}

/// After a restore of the catalog the feed answers `reset_required`; the
/// edge re-snapshots in place (`edge-metadata.md` §5.2): rows wound back by
/// the restore win over newer ones, the inode stays, a file the restored
/// catalog never had is committed again as a new file, and the feed resumes
/// from the snapshot's `as_of`.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_restored_catalog_is_resnapshotted_in_place() {
    resnapshot_scenario(Load::Paged).await;
}

/// The same restore, loaded from a bulk export (`control-api.md` §6): two
/// parts, each fetched, checked against its SHA-256 and applied; no page.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_restored_catalog_is_loaded_from_a_bulk_export() {
    resnapshot_scenario(Load::Bulk).await;
}

/// A part whose bytes do not match its SHA-256 is never used: the edge
/// pages through the snapshot instead, and ends in the same state.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_corrupt_export_part_falls_back_to_paging() {
    resnapshot_scenario(Load::CorruptPart).await;
}

#[derive(Clone, Copy, PartialEq)]
enum Load {
    /// The service has no export for the dataset (`layout_conflict`).
    Paged,
    Bulk,
    CorruptPart,
}

async fn resnapshot_scenario(load: Load) {
    use omnifile_edge::fakes::FakeSnapshot;
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let mut cfg = config(dir.path());
    // No FSAL here: the local file is committed again without one (#54).
    cfg.fsal.no_ganesha = true;
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.push_event(events::share(1, 100, "proj", "wait", 3));
    svc.push_event(events::dir(100, None, "", 1));
    let crc = omnifile_integrity::crc64_nvme(b"x");
    let a_row =
        |name: &str, ev: u64| events::file(801, 100, name, 1, 8010, Some(crc), false, None, ev);
    // Renamed after the restore point: the restored catalog has the old name.
    svc.push_event(a_row("a-new.segy", 5));
    let a = data_root.join("proj/a-new.segy");
    eventually("placeholder", || marker(&a).is_some()).await;
    let a_ino = std::fs::metadata(&a).unwrap().ino();

    // A local file committed after the restore point: the catalog lost it.
    let b = data_root.join("proj/late.bin");
    let bdata = pattern(1 << 20);
    std::fs::write(&b, &bdata).unwrap();
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
    eventually("commit", || {
        marker(&b).is_some_and(|m| m.state == State::Resident)
    })
    .await;
    let lost_id = marker(&b).unwrap().file_id;
    // A file whose latest version was committed after the restore point:
    // the catalog has it at an older version.
    let k = data_root.join("proj/kept.bin");
    let kdata = pattern(1 << 19);
    std::fs::write(&k, &kdata).unwrap();
    d.commits
        .send(CloseWrite {
            path: k.clone(),
            ino: std::fs::metadata(&k).unwrap().ino(),
            swept_gen: None,
            token: None,
            seen: None,
        })
        .await
        .unwrap();
    eventually("commit kept", || {
        marker(&k).is_some_and(|m| m.state == State::Resident)
    })
    .await;
    let km = marker(&k).unwrap();
    let mut k_row = events::file(
        km.file_id,
        100,
        "kept.bin",
        kdata.len() as u64,
        km.version_id - 1,
        Some(omnifile_integrity::crc64_nvme(&kdata)),
        false,
        None,
        1,
    )["file"]
        .clone();
    k_row["state"] = serde_json::json!("remote");

    // The restore.
    let pos = svc.feed_position();
    svc.set_snapshot(
        1,
        FakeSnapshot {
            as_of: format!("{pos}.0.1"),
            dataset: None,
            // The restored catalog has the share's earlier policy.
            shares: vec![events::share(1, 100, "proj", "fail", 1)["share"].clone()],
            acls: vec![],
            files: vec![
                events::dir(100, None, "", 1)["file"].clone(),
                a_row("a-old.segy", 2)["file"].clone(),
                k_row,
            ],
        },
    );
    if load != Load::Paged {
        // share, root, a-old, kept: two lines per part, two parts.
        svc.set_exports(2);
    }
    if load == Load::CorruptPart {
        svc.set_export_corrupt(Some(1));
    }
    svc.set_reset_required(true);

    let old = data_root.join("proj/a-old.segy");
    eventually("the restored name", || old.exists()).await;
    assert_eq!(std::fs::metadata(&old).unwrap().ino(), a_ino, "same inode");
    assert!(!a.exists());
    // late.bin: kept, and committed again as a new file.
    eventually("committed again", || {
        marker(&b).is_some_and(|m| m.state == State::Resident && m.file_id != lost_id)
    })
    .await;
    assert_eq!(std::fs::read(&b).unwrap(), bdata);
    let m = marker(&b).unwrap();
    assert_eq!(svc.blob(m.file_id, m.version_id).unwrap(), bdata);
    // kept.bin: same file, its local bytes committed again as a new version.
    eventually("kept committed again", || {
        marker(&k).is_some_and(|m| m.state == State::Resident && m.version_id > km.version_id)
    })
    .await;
    let m = marker(&k).unwrap();
    assert_eq!(m.file_id, km.file_id);
    assert_eq!(std::fs::read(&k).unwrap(), kdata);
    assert_eq!(svc.blob(m.file_id, m.version_id).unwrap(), kdata);
    // The feed resumes from as_of: a later event applies.
    svc.push_event(events::file(
        802,
        100,
        "after.segy",
        1,
        8020,
        Some(crc),
        false,
        None,
        1,
    ));
    eventually("after", || data_root.join("proj/after.segy").exists()).await;
    let shares = d.core.replica.lock().await.shares().unwrap();
    assert_eq!(shares.len(), 1);
    assert_eq!(
        shares[0].on_open, "fail",
        "the share as the snapshot has it"
    );
    let calls = svc.calls();
    let paged = calls.iter().any(|c| c.starts_with("snapshot 1 "));
    let asked = calls.iter().any(|c| c == "snapshot_export_start 1");
    let polled = calls.iter().any(|c| c.starts_with("snapshot_export "));
    match load {
        Load::Paged => assert!(asked && !polled && paged, "{calls:?}"),
        Load::Bulk => assert!(asked && polled && !paged, "{calls:?}"),
        Load::CorruptPart => assert!(asked && polled && paged, "{calls:?}"),
    }
    d.shutdown().await;
}

/// A read the edge refuses itself (failfast on archived data) is audited
/// (`control-api.md` §4.7): one record per file, requester and reason
/// however often the client retries, with who asked and why, kept in the
/// replica and sent to the service. Another requester is another record.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn refused_reads_are_audited_once_per_requester() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path());
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let mut fsal = Fsal::connect(&socket).await;
    svc.set_archived(true);
    svc.push_event(events::share(1, 100, "proj", "fail", 1));
    svc.push_event(events::dir(100, None, "", 1));
    let crc = omnifile_integrity::crc64_nvme(b"old bytes");
    svc.push_event(events::file(
        701,
        100,
        "old.segy",
        9,
        7010,
        Some(crc),
        true,
        None,
        1,
    ));
    let p = data_root.join("proj/old.segy");
    eventually("archived placeholder", || marker(&p).is_some()).await;

    let mut ri = intent(&p, 701, 7010, Policy::FailFast);
    for _ in 0..3 {
        fsal.send(&Message::ReadIntent(ri.clone())).await;
        let Message::ReadDecision(dec) =
            fsal.expect(|m| matches!(m, Message::ReadDecision(_))).await
        else {
            panic!()
        };
        assert_eq!(dec.decision, Decision::Eio);
    }
    eventually("the refusal reaches the service", || {
        !svc.audits().is_empty()
    })
    .await;
    let a = svc.audits();
    assert_eq!(a.len(), 1, "one record for three tries: {a:?}");
    let r = &a[0];
    assert_eq!(
        (&r["action"], &r["decision"], &r["file_id"]),
        (&"read.refused".into(), &"deny".into(), &"701".into())
    );
    assert_eq!(r["uid"], 41023);
    assert_eq!(r["proof"], "claimed");
    assert_eq!(r["client_addr"], "10.1.2.3");
    assert_eq!(r["details"]["reason"], "failfast");
    assert_eq!(r["details"]["via"], "nfs");
    assert_eq!(r["details"]["version_id"], "7010");

    // Someone else's refused read is their own record.
    ri.identity.uid = 41024;
    fsal.send(&Message::ReadIntent(ri)).await;
    fsal.expect(|m| matches!(m, Message::ReadDecision(_))).await;
    eventually("the second requester's record", || svc.audits().len() == 2).await;
    assert_eq!(svc.audits()[1]["uid"], 41024);
    d.shutdown().await;
}

/// The reconciliation digest (`control-api.md` §6): a replica that matches
/// the catalog is left alone; one that lacks a row the catalog has (a lost
/// event) is found, and the feed loop's re-snapshot brings the row in.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_replica_that_differs_from_the_catalog_digest_is_resnapshotted() {
    use omnifile_edge::fakes::FakeSnapshot;
    use omnifile_edge::feed_loop::reconcile_once;
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path());
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let crc = omnifile_integrity::crc64_nvme(b"x");
    let share = events::share(1, 100, "proj", "wait", 1);
    let root = events::dir(100, None, "", 1);
    let a = events::file(801, 100, "a.segy", 1, 8010, Some(crc), false, None, 3);
    for e in [&share, &root, &a] {
        svc.push_event(e.clone());
    }
    eventually("placeholder", || data_root.join("proj/a.segy").exists()).await;
    let pos = svc.feed_position();
    let catalog = |files: Vec<serde_json::Value>| FakeSnapshot {
        as_of: format!("{pos}.0"),
        dataset: None,
        shares: vec![share["share"].clone()],
        acls: vec![],
        files,
    };
    svc.set_snapshot(1, catalog(vec![root["file"].clone(), a["file"].clone()]));
    assert_eq!(reconcile_once(&d.core).await.unwrap(), Vec::<u64>::new());

    // The catalog has a file the edge never heard of.
    let lost = events::file(802, 100, "lost.segy", 1, 8020, Some(crc), false, None, 1);
    svc.set_snapshot(
        1,
        catalog(vec![
            root["file"].clone(),
            a["file"].clone(),
            lost["file"].clone(),
        ]),
    );
    // While the dataset is busy (its digest moves), no verdict: next round.
    svc.set_digest_drift(true);
    assert_eq!(reconcile_once(&d.core).await.unwrap(), Vec::<u64>::new());
    assert!(!data_root.join("proj/lost.segy").exists());
    svc.set_digest_drift(false);
    assert_eq!(reconcile_once(&d.core).await.unwrap(), vec![1]);
    eventually("the lost file arrives", || {
        data_root.join("proj/lost.segy").exists()
    })
    .await;
    assert!(
        svc.calls().iter().any(|c| c.starts_with("snapshot 1 ")),
        "{:?}",
        svc.calls()
    );
    // And now they agree.
    assert_eq!(reconcile_once(&d.core).await.unwrap(), Vec::<u64>::new());
    d.shutdown().await;
}

/// The heartbeat (`control-api.md` §4.7) goes out as soon as the daemon
/// starts and carries real gauges, and every beat asks whether the client
/// certificate is due, passing on the service's `certificate_renew_after`.
/// Without it the 30-day certificate expired and the edge needed a new
/// enrolment bundle.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_heartbeat_reports_health_and_renews_the_certificate() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let mut cfg = config(dir.path());
    cfg.feed.heartbeat_s = 1;
    // Decision 50: the Roles Anywhere certificate renews on the same beat,
    // and the signing helper restarts once a new one is installed.
    let restarted = dir.path().join("helper-restarted");
    cfg.s3.roles_anywhere_dir = dir.path().join("roles-anywhere");
    cfg.s3.helper_restart = vec!["touch".into(), restarted.display().to_string()];
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    svc.set_renew_after(Some("2026-10-30T00:00:00Z"));
    svc.set_ra_installs(true);
    let ra_dir = cfg.s3.roles_anywhere_dir.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    eventually("a heartbeat at start", || !svc.heartbeats().is_empty()).await;
    let h = svc.heartbeats()[0].clone();
    assert_eq!(h.version, env!("CARGO_PKG_VERSION"));
    assert!(h.kernel.as_deref().is_some_and(|k| !k.is_empty()), "{h:?}");
    let (free, cap) = (h.data_free_bytes.unwrap(), h.data_capacity_bytes.unwrap());
    assert!(cap > 0 && free <= cap, "{h:?}");
    assert_eq!(h.fills_running, 0);
    assert_eq!(h.recalls_waiting, 0);
    // Decision 67: the defaults a share with no period of its own gets here, as
    // this edge's config sets them (`config()`: 1 s, and the WORM default 60 s).
    assert_eq!(
        (h.quiet_default_s, h.quiet_worm_default_s),
        (Some(1), Some(60))
    );
    assert_eq!(h.quiet_stale_s, Some(0), "the quiet periods are read");
    // It knows the recall state `superseded`: the service sends `cancelled` in its
    // place to an edge that doesn't say so (control API §4.4).
    assert_eq!(h.recall_superseded, Some(1));
    // The config enables the smb-guard (the default) but these tests never
    // start it: the edge says so, as an operator must see it.
    assert_eq!(h.guard_state.as_deref(), Some("down"));
    eventually("the renewal check, with the service's time", || {
        svc.renewals()
            .iter()
            .any(|r| r.as_deref() == Some("2026-10-30T00:00:00Z"))
    })
    .await;
    eventually("the Roles Anywhere check, then the helper restart", || {
        svc.ra_renewals().contains(&ra_dir) && restarted.exists()
    })
    .await;
    // A connected FSAL is counted, on a later beat.
    let _fsal = Fsal::connect(&socket).await;
    eventually("the FSAL connection counted", || {
        svc.heartbeats().iter().any(|h| h.fsal_connections == 1)
    })
    .await;
    d.shutdown().await;
}

/// M6 (docs/design/share-provisioning.md): a share added in the portal
/// reaches the edge as a `share.upsert`, and the daemon renders it into the
/// Samba and Ganesha files, reloads the servers, and gives the root its
/// groups' ACL. A change re-renders and moves the ACL to the new groups; a
/// pass that changes nothing reloads nothing; a share the edge can't serve
/// is left out and the others stay.
#[tokio::test(flavor = "multi_thread")]
async fn a_share_from_the_feed_is_rendered_reloaded_and_its_root_given_its_groups() {
    use omnifile_acl::{AclFs, Perms, SysFs, Tag};
    use omnifile_identity::fake::{FakeIdMap, FakeResolver};

    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let mut cfg = config(dir.path());
    let reloads = dir.path().join("reloads");
    let (smb_conf, ganesha_conf) = (
        dir.path().join("state/shares/smb.conf"),
        dir.path().join("state/shares/ganesha.conf"),
    );
    cfg.shares.render = true;
    cfg.shares.smb_conf = smb_conf.clone();
    cfg.shares.ganesha_conf = ganesha_conf.clone();
    cfg.shares.reload = vec![
        "sh".into(),
        "-c".into(),
        format!("echo reload >> {}", reloads.display()),
    ];
    let restarts = dir.path().join("restarts");
    cfg.shares.restart = vec![
        "sh".into(),
        "-c".into(),
        format!("echo restart >> {}", restarts.display()),
    ];
    cfg.shares.smb_prevver = "off".into();
    let cfg0 = cfg.clone();
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    const D: &str = "S-1-5-21-11-22-33";
    let mut map = FakeIdMap::new("CORP");
    map.add_group("owners", 30003, &format!("{D}-1100"))
        .add_group("readers", 30001, &format!("{D}-1101"))
        .add_group("auditors", 30005, &format!("{D}-1102"));
    let identity = omnifile_edge::guard::EdgeIdentity {
        resolver: Arc::new(FakeResolver::default()),
        idmap: Arc::new(map),
    };
    let _ = d.core.identity.set(identity.clone());
    let share = |ev: u64, readers: &str, encrypt: bool, depth: u32| {
        serde_json::json!({"type": "share.upsert", "entity_id": "1", "entity_version": ev.to_string(),
            "share": {"share_id": "1", "dataset_id": "1", "name": "seismic", "root_dir_id": "100",
                      "protocols": ["nfs4", "smb"], "nfs_sec": ["krb5p"], "nfs_clients": [],
                      "root_squash": true, "trust_level": "proven", "host_trust_override": false,
                      "nfs_export_id": 3, "smb_encrypt": encrypt,
                      "owners_group_sid": format!("{D}-1100"), "readers_group_sid": format!("{D}-{readers}"),
                      "on_open": "wait", "rewind": {"depth_days": depth, "max_days": depth, "labels": {"daily_at_utc": "00:00"}},
                      "worm": {"enabled": false}, "entity_version": ev.to_string()}})
    };
    svc.push_event(share(1, "1101", false, 0));
    svc.push_event(events::dir(100, None, "", 1));

    let read = |p: &Path| std::fs::read_to_string(p).unwrap_or_default();
    let count = |p: &Path| read(p).lines().count();
    // Both files: the share loop replaces smb.conf, then ganesha.conf, so a
    // read between the two sees the new one and the old other.
    eventually("the share rendered", || {
        read(&smb_conf).contains("[seismic]")
            && read(&ganesha_conf).contains("Pseudo = \"/seismic\";")
    })
    .await;
    let root = data_root.join("seismic");
    let smb = read(&smb_conf);
    assert!(smb.contains(&format!("path = {}", root.display())), "{smb}");
    assert!(
        smb.contains(&format!("valid users = {D}-1100 {D}-1101")),
        "{smb}"
    );
    assert!(!smb.contains("smb encrypt"));
    let ganesha = read(&ganesha_conf);
    assert!(ganesha.contains("Export_Id = 3;") && ganesha.contains("Pseudo = \"/seismic\";"));
    assert!(ganesha.contains("SecType = krb5p;"), "{ganesha}");
    // (A first pass, before the root was on disk, may have reloaded too.)
    eventually("the servers reloaded", || count(&reloads) >= 1).await;

    let acl = |p: &Path| SysFs.read(p).unwrap();
    eventually("the root has its groups' ACL", || {
        acl(&root)
            .access
            .is_some_and(|a| a.get(Tag::Group(30003)) == Some(Perms::RWX))
    })
    .await;
    let a = acl(&root);
    assert_eq!(a.gid, 30003, "the root's group is Owners");
    assert_eq!(
        a.access.as_ref().unwrap().get(Tag::Group(30001)),
        Some(Perms::RX)
    );
    assert_eq!(
        a.access.as_ref().unwrap().get(Tag::GroupObj),
        Some(Perms::NONE)
    );
    assert!(
        a.default
            .as_ref()
            .is_some_and(|d| d.get(Tag::Group(30003)) == Some(Perms::RWX))
    );

    // Nothing changed: no further reload.
    let n = count(&reloads);
    tokio::time::sleep(Duration::from_secs(7)).await;
    assert_eq!(count(&reloads), n, "an unchanged pass reloads nothing");

    // New readers and SMB encryption: re-rendered, reloaded, ACL moved.
    svc.push_event(share(2, "1102", true, 0));
    eventually("re-rendered", || {
        read(&smb_conf).contains("smb encrypt = required")
    })
    .await;
    eventually("reloaded again", || count(&reloads) == n + 1).await;
    eventually("the readers moved", || {
        acl(&root).access.is_some_and(|a| {
            a.get(Tag::Group(30005)) == Some(Perms::RX) && a.get(Tag::Group(30001)).is_none()
        })
    })
    .await;

    // An edit that leaves the groups alone re-materialises the root from the
    // catalog (gid 0): the root keeps the Owners group (review finding 6).
    svc.push_event(share(3, "1102", false, 0));
    eventually("re-rendered without encryption", || {
        !read(&smb_conf).contains("smb encrypt")
    })
    .await;
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert_eq!(acl(&root).gid, 30003, "the root keeps its Owners group");

    // Readers replaced by a group winbind can't map: the old readers lose
    // their entries now, the rest stays (review finding 2).
    svc.push_event(share(4, "1199", false, 0));
    eventually("the old readers revoked", || {
        acl(&root).access.is_some_and(|a| {
            a.get(Tag::Group(30005)).is_none() && a.get(Tag::Group(30003)) == Some(Perms::RWX)
        })
    })
    .await;

    // Rewind depth from 0: the export gains a Snap_Dir, which Ganesha takes
    // only at a restart (review finding 3).
    assert!(!restarts.exists());
    svc.push_event(share(5, "1199", false, 7));
    eventually("Snap_Dir rendered", || {
        read(&ganesha_conf).contains("Snap_Dir")
    })
    .await;
    eventually("Ganesha restarted", || count(&restarts) == 1).await;
    assert!(!ganesha_conf.with_extension("restart-owed").exists());

    // The root loses its ACL and group behind the daemon's back (a root made
    // again, an admin's chown): the record says "applied", but the next pass
    // checks the root itself and applies again (review finding 7).
    SysFs.write_default(&root, None).unwrap();
    SysFs.chown(&root, None, Some(0)).unwrap();
    svc.push_event(share(6, "1199", true, 7));
    eventually("the root's ACL and group given again", || {
        let a = acl(&root);
        a.gid == 30003
            && a.default
                .as_ref()
                .is_some_and(|d| d.get(Tag::Group(30003)) == Some(Perms::RWX))
    })
    .await;

    // A share of a folder: rendered at the folder, given its groups. Then
    // removed (share.delete): no longer served, the servers reload, and the
    // folder and what it holds stay.
    svc.push_event(events::dir(101, Some(100), "projects", 1));
    let mut folder = share(1, "1101", false, 0);
    folder["entity_id"] = "3".into();
    folder["share"]["share_id"] = "3".into();
    folder["share"]["name"] = "proj".into();
    folder["share"]["root_dir_id"] = "101".into();
    folder["share"]["nfs_export_id"] = 4.into();
    svc.push_event(folder);
    let projects = root.join("projects");
    eventually("the folder share rendered", || {
        read(&smb_conf).contains("[proj]") && read(&ganesha_conf).contains("Pseudo = \"/proj\";")
    })
    .await;
    assert!(read(&smb_conf).contains(&format!("path = {}", projects.display())));
    assert!(read(&ganesha_conf).contains("Pseudo = \"/proj\";"));
    eventually("the folder has its share's groups", || {
        let a = acl(&projects);
        a.gid == 30003
            && a.access
                .is_some_and(|x| x.get(Tag::Group(30001)) == Some(Perms::RX))
    })
    .await;

    // A Writer of the parent share renames the shared folder away and makes
    // their own in its place (M7 review B1). The servers take a root by path,
    // so the share stops being served, and the impostor never gets the share's
    // group or ACL. A symlink in its place is refused the same way. The
    // folder put back is served again.
    use std::os::unix::fs::MetadataExt;
    let away = root.join("projects.old");
    std::fs::rename(&projects, &away).unwrap();
    std::fs::create_dir(&projects).unwrap();
    std::os::unix::fs::chown(&projects, Some(4321), Some(4321)).unwrap();
    eventually("a replaced share root is not served", || {
        !read(&smb_conf).contains("[proj]") && !read(&ganesha_conf).contains("/proj")
    })
    .await;
    assert!(
        read(&smb_conf).contains("[seismic]"),
        "the parent share stays"
    );
    tokio::time::sleep(Duration::from_secs(7)).await;
    let imp = acl(&projects);
    assert_eq!(
        imp.gid, 4321,
        "the impostor is never given the Owners group"
    );
    assert!(
        imp.access
            .as_ref()
            .is_none_or(|a| a.get(Tag::Group(30001)).is_none()),
        "nor the share's groups"
    );
    assert!(!read(&smb_conf).contains("[proj]"));
    std::fs::remove_dir(&projects).unwrap();
    std::os::unix::fs::symlink("/", &projects).unwrap();
    tokio::time::sleep(Duration::from_secs(7)).await;
    assert!(
        !read(&smb_conf).contains("[proj]"),
        "a symlink is never served"
    );
    assert!(!read(&ganesha_conf).contains("/proj"));
    std::fs::remove_file(&projects).unwrap();
    std::fs::rename(&away, &projects).unwrap();
    eventually("the folder put back is served again", || {
        read(&smb_conf).contains("[proj]")
    })
    .await;
    assert_eq!(std::fs::metadata(&projects).unwrap().gid(), 30003);

    let n = count(&reloads);
    svc.push_event(serde_json::json!({"type": "share.delete", "entity_id": "3",
                                      "entity_version": "2", "share_id": "3"}));
    eventually("the folder share no longer served", || {
        !read(&smb_conf).contains("[proj]") && !read(&ganesha_conf).contains("/proj")
    })
    .await;
    eventually("reloaded for it", || count(&reloads) > n).await;
    assert!(projects.is_dir(), "the folder stays");
    assert!(read(&smb_conf).contains("[seismic]"));

    // A share the edge can't serve (no export id: an older service) is left
    // out; the good one stays.
    let mut bad = share(1, "1101", false, 0);
    bad["entity_id"] = "2".into();
    bad["share"]["share_id"] = "2".into();
    bad["share"]["name"] = "broken".into();
    bad["share"]["root_dir_id"] = "200".into();
    bad["share"]
        .as_object_mut()
        .unwrap()
        .remove("nfs_export_id");
    svc.push_event(bad);
    let mut root2 = events::dir(200, None, "", 1);
    root2["file"]["dataset_id"] = "2".into();
    svc.push_event(root2);
    eventually("the second root on disk", || {
        data_root.join("broken").is_dir()
    })
    .await;
    tokio::time::sleep(Duration::from_secs(7)).await;
    let smb = read(&smb_conf);
    assert!(
        smb.contains("[seismic]") && !smb.contains("[broken]"),
        "{smb}"
    );
    d.shutdown().await;

    // A restarted daemon reloads once, though the files are unchanged: a
    // reload its predecessor owed is never lost (review finding 1).
    let n = count(&reloads);
    let vol = startup_checks(&cfg0).unwrap();
    let d = Daemon::start(cfg0, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let _ = d.core.identity.set(identity);
    eventually("reloaded at start", || count(&reloads) == n + 1).await;
    assert_eq!(count(&restarts), 1, "no restart is owed");
    d.shutdown().await;
}

/// The fill fence after a daemon restart (M2 step 10), set up: a daemon
/// materialises an archived placeholder and stops; the next one starts, and
/// is told to download it before any FSAL has connected to it.
struct Restarted {
    _dir: tempfile::TempDir,
    svc: Arc<FakeService>,
    path: PathBuf,
    data: Vec<u8>,
    socket: PathBuf,
    daemon: Daemon<FakeService, FakeService>,
}

async fn restarted(exe: &str, fsal_extra: &str) -> Option<Restarted> {
    let base = base()?;
    omnifile_edge::init_logging("text", "info");
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config_fsal(dir.path(), exe, fsal_extra);
    let svc = Arc::new(FakeService::new());
    let path = cfg.data_root.join("proj/r.segy");
    let socket = cfg.socket_path.clone();
    svc.set_archived(true);
    let data = pattern((3 << 20) + 5);
    svc.push_event(events::share(1, 100, "proj", "wait", 1));
    svc.push_event(events::dir(100, None, "", 1));
    svc.push_event(events::file(
        901,
        100,
        "r.segy",
        data.len() as u64,
        9010,
        Some(omnifile_integrity::crc64_nvme(&data)),
        true,
        None,
        1,
    ));
    let vol = startup_checks(&cfg).unwrap();
    let first = Daemon::start(cfg.clone(), vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    eventually("placeholder", || marker(&path).is_some()).await;
    first.shutdown().await;

    let vol = startup_checks(&cfg).unwrap();
    let daemon = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.put_blob(901, 9010, data.clone());
    svc.push_event(events::recall(
        91,
        901,
        9010,
        "downloading",
        Some("mem://901/9010"),
        5,
    ));
    Some(Restarted {
        _dir: dir,
        svc,
        path,
        data,
        socket,
        daemon,
    })
}

fn filled(svc: &FakeService) -> bool {
    svc.calls()
        .iter()
        .any(|c| c.starts_with("recall_filled 91 "))
}

/// The restarted daemon's fill waits for Ganesha's FSAL (here: this test
/// process, whose binary is `required_exe`), then fences and fills. The FSAL
/// comes back after 5 s: later than a recall fill's own retries of a broken
/// fence (about 3 s) reach.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_restarted_daemon_fills_once_the_fsal_reconnects() {
    let exe = std::env::current_exe().unwrap().display().to_string();
    let Some(r) = restarted(&exe, "reconnect_wait_ms = 20000").await else {
        return;
    };
    tokio::time::sleep(Duration::from_secs(5)).await;
    let m = marker(&r.path).unwrap();
    assert_eq!(
        (m.state, m.resident_bytes),
        (State::Partial, 0),
        "nothing written before an FSAL confirmed the fence"
    );
    assert!(!filled(&r.svc));
    let mut fsal = Fsal::connect(&r.socket).await;
    fsal.expect(|m| {
        matches!(m, Message::RecallState(s) if s.obj.file_id == 901 && s.state == RecallPhase::Done)
    })
    .await;
    assert_eq!(std::fs::read(&r.path).unwrap(), r.data, "the right bytes");
    assert_eq!(marker(&r.path).unwrap().state, State::Resident);
    eventually("recall_filled", || filled(&r.svc)).await;
    r.daemon.shutdown().await;
}

/// No FSAL reconnects within `reconnect_wait_ms`: the fill fails closed
/// (FAILED, nothing written) once the bound has passed, not before. The
/// recall's fill retries a broken fence 5 times, but the bound is the
/// fill's: FAILED comes after one bound plus the retries' backoff (3 s),
/// not after six bounds.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_restarted_daemon_fails_closed_when_no_fsal_reconnects_in_time() {
    let exe = std::env::current_exe().unwrap().display().to_string();
    let Some(r) = restarted(&exe, "reconnect_wait_ms = 1500").await else {
        return;
    };
    let t0 = Instant::now();
    let key = (901, 9010, 0);
    let phase = || {
        r.daemon
            .core
            .recalls
            .lock()
            .unwrap()
            .get(&key)
            .map(|e| e.phase)
    };
    let mut first_failed = None;
    while t0.elapsed() < Duration::from_secs(25) {
        if phase() == Some(RecallPhase::Failed) {
            first_failed = Some(t0.elapsed());
            break;
        }
        let m = marker(&r.path).unwrap();
        assert_eq!(
            (m.state, m.resident_bytes),
            (State::Partial, 0),
            "never filled on nobody's word"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let failed = first_failed.expect("the fill failed closed once the bound passed");
    assert!(
        failed >= Duration::from_millis(1500),
        "it waited the bound first: {failed:?}"
    );
    // The bound, the retries' backoff (0.2 + 0.4 + ... + 1.0 s) and the
    // feed's delivery of the recall row (up to 1 s), with a margin.
    assert!(
        failed < Duration::from_millis(1500 + 3000 + 1000 + 1500),
        "one bound for the fill, not one per retry: {failed:?}"
    );
    let m = marker(&r.path).unwrap();
    assert_eq!((m.state, m.resident_bytes), (State::Partial, 0));
    assert!(!filled(&r.svc));
    r.daemon.shutdown().await;
}

/// No Ganesha runs (no process has `required_exe`): zero FSAL connections
/// is legitimate, and the restarted daemon fills at once.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_restarted_daemon_fills_with_no_fsal_when_no_ganesha_runs() {
    let Some(r) = restarted("/nonexistent/ganesha.nfsd", "reconnect_wait_ms = 20000").await else {
        return;
    };
    let t0 = Instant::now();
    eventually("recall_filled", || filled(&r.svc)).await;
    assert!(t0.elapsed() < Duration::from_secs(10));
    assert_eq!(std::fs::read(&r.path).unwrap(), r.data);
    assert_eq!(marker(&r.path).unwrap().state, State::Resident);
    r.daemon.shutdown().await;
}

/// An archived placeholder `name` (file `fid`, version `vid`) with its bytes
/// in the fake store, in a share whose open policy is `wait`; `fsal_extra`
/// are more `[fsal]` lines.
async fn archived_world(
    dir: &Path,
    fid: u64,
    vid: u64,
    name: &str,
    data: &[u8],
    good_blob: bool,
    fsal_extra: &str,
) -> (
    Daemon<FakeService, FakeService>,
    Arc<FakeService>,
    PathBuf,
    PathBuf,
) {
    let exe = std::env::current_exe().unwrap();
    let cfg = config_fsal(dir, &exe.display().to_string(), fsal_extra);
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.set_archived(true);
    svc.push_event(events::share(1, 100, "proj", "wait", 1));
    svc.push_event(events::dir(100, None, "", 1));
    svc.push_event(events::file(
        fid,
        100,
        name,
        data.len() as u64,
        vid,
        Some(omnifile_integrity::crc64_nvme(data)),
        true,
        None,
        1,
    ));
    let mut stored = data.to_vec();
    if !good_blob {
        // Bytes that fail the whole-file CRC: a fill that can never succeed.
        stored[0] ^= 0xff;
    }
    svc.put_blob(fid, vid, stored);
    let p = data_root.join("proj").join(name);
    eventually("placeholder", || marker(&p).is_some()).await;
    (d, svc, p, socket)
}

/// recall-resume (WP3's soak): a read answered with a recall that is
/// already `downloading`, with no fill of it running here (its earlier fill
/// failed, or the daemon restarted since), starts the fill. Before, the
/// daemon recorded the state and waited for a feed event that had already
/// come, so the recall stayed `downloading` and every read got EIO.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_read_that_joins_a_downloading_recall_starts_its_fill() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let data = pattern(3 << 20);
    let (d, svc, p, socket) =
        archived_world(dir.path(), 901, 9010, "joined.segy", &data, true, "").await;
    // The catalog's recall of this version is already `downloading`: its
    // event came and its fill failed (the soak's full disk), or the daemon
    // that filled it stopped. No new event comes for it.
    let rid = svc.add_recall(901, 9010, "downloading");
    let mut fsal = Fsal::connect(&socket).await;
    fsal.send(&Message::ReadIntent(intent(&p, 901, 9010, Policy::Wait)))
        .await;
    fsal.expect(|m| {
        matches!(m, Message::RecallState(s) if s.obj.file_id == 901 && s.state == RecallPhase::Done)
    })
    .await;
    assert_eq!(std::fs::read(&p).unwrap(), data);
    let filled = format!("recall_filled {rid} 9010 {}", data.len());
    eventually("reported", || svc.calls().contains(&filled)).await;
    assert_eq!(svc.recall_state(rid).as_deref(), Some("available"));
    d.shutdown().await;
}

/// recall-resume: a recall's fill that can't succeed (here the bytes fail
/// their whole-file CRC; in the soak, the edge's disk was full) is reported,
/// and the recall becomes `failed` instead of staying `downloading`.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_failed_recall_fill_is_reported_failed() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let data = pattern(2 << 20);
    let (d, svc, p, socket) =
        archived_world(dir.path(), 902, 9020, "corrupt.segy", &data, false, "").await;
    let mut fsal = Fsal::connect(&socket).await;
    fsal.send(&Message::ReadIntent(intent(&p, 902, 9020, Policy::Wait)))
        .await;
    let Message::RecallState(rs) = fsal
        .expect(
            |m| matches!(m, Message::RecallState(s) if s.obj.file_id == 902 && s.recall_id != 0),
        )
        .await
    else {
        panic!()
    };
    svc.set_recall_state(rs.recall_id, "downloading");
    svc.push_event(events::recall(
        rs.recall_id,
        902,
        9020,
        "downloading",
        Some("mem://902/9020"),
        5,
    ));
    fsal.expect(|m| {
        matches!(m, Message::RecallState(s) if s.obj.file_id == 902 && s.state == RecallPhase::Failed)
    })
    .await;
    let failed = format!("recall_failed {} 9020 ", rs.recall_id);
    eventually("reported failed", || {
        svc.calls()
            .iter()
            .any(|c| c.starts_with(&failed) && c.contains("CRC"))
    })
    .await;
    assert_eq!(svc.recall_state(rs.recall_id).as_deref(), Some("failed"));
    assert!(!svc.calls().iter().any(|c| c.starts_with("recall_filled")));
    // Its file stays a stub, never the wrong bytes.
    assert_eq!(marker(&p).unwrap().resident_bytes, 0);
    d.shutdown().await;
}

/// recall-resume (WP3's M2 step 10 and the soak's restarts): a recall's
/// fill that starts before any FSAL is connected can't pass the fill fence.
/// It is only interrupted: it really fails (FAILED, after the 1.5 s
/// reconnect wait), and is resumed on a later attempt once an FSAL is back,
/// not left `downloading`, and not reported failed.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_interrupted_recall_fill_is_resumed() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let data = pattern(2 << 20);
    let (d, svc, p, socket) = archived_world(
        dir.path(),
        903,
        9030,
        "fenced.segy",
        &data,
        true,
        "reconnect_wait_ms = 1500",
    )
    .await;
    // The feed says `downloading` while no FSAL is connected (as right after
    // a daemon restart): the fill fence can't be confirmed.
    svc.push_event(events::recall(
        777_001,
        903,
        9030,
        "downloading",
        Some("mem://903/9030"),
        5,
    ));
    let key = (903, 9030, 0);
    eventually("the first fill failed", || {
        d.core
            .recalls
            .lock()
            .unwrap()
            .get(&key)
            .is_some_and(|e| e.phase == RecallPhase::Failed)
    })
    .await;
    assert!(!svc.calls().iter().any(|c| c.starts_with("recall_filled")));
    assert_eq!(marker(&p).unwrap().resident_bytes, 0);
    // An FSAL connects (Ganesha back): a later attempt completes the fill.
    let _fsal = Fsal::connect(&socket).await;
    let filled = format!("recall_filled 777001 9030 {}", data.len());
    let deadline = Instant::now() + Duration::from_secs(30);
    while !svc.calls().contains(&filled) {
        assert!(
            Instant::now() < deadline,
            "the interrupted fill was never resumed"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert_eq!(std::fs::read(&p).unwrap(), data);
    assert!(
        !svc.calls().iter().any(|c| c.starts_with("recall_failed")),
        "only interrupted: not failed"
    );
    d.shutdown().await;
}

/// #52 review, finding 3: a failure report delayed while the service was
/// away is dropped once a newer fill of the same recall runs (or has
/// brought the bytes), rather than failing the recall that fill completes.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_delayed_failure_report_does_not_fail_a_newer_fill() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let data = pattern(2 << 20);
    let (d, svc, p, socket) =
        archived_world(dir.path(), 904, 9040, "late.segy", &data, false, "").await;
    let mut fsal = Fsal::connect(&socket).await;
    let rid = svc.add_recall(904, 9040, "downloading");
    // A slow fill whose bytes fail their CRC, and the service going away
    // while it runs: its failure report waits and retries (1 s, 2 s, ...).
    svc.set_range_delay_ms(400);
    svc.push_event(events::recall(
        rid,
        904,
        9040,
        "downloading",
        Some("mem://904/9040"),
        5,
    ));
    fsal.expect(|m| matches!(m, Message::RecallState(s) if s.obj.file_id == 904 && s.state == RecallPhase::Fetching)).await;
    svc.set_unavailable(true);
    fsal.expect(|m| matches!(m, Message::RecallState(s) if s.obj.file_id == 904 && s.state == RecallPhase::Failed)).await;
    // The right bytes now, and a newer fill of the same recall, slow enough
    // that the old report's next try comes while it runs.
    svc.put_blob(904, 9040, data.clone());
    svc.set_range_delay_ms(1500);
    let node = d
        .core
        .replica
        .lock()
        .await
        .node_by_file_id(904)
        .unwrap()
        .unwrap();
    let fill = tokio::spawn(d.core.clone().run_fill(
        (904, 9040, 0),
        node,
        omnifile_edge::ports::ReadUrl::azure("mem://904/9040"),
        Some(rid),
    ));
    fsal.expect(|m| matches!(m, Message::RecallState(s) if s.obj.file_id == 904 && s.state == RecallPhase::Fetching)).await;
    svc.set_unavailable(false);
    fill.await.unwrap();
    let filled = format!("recall_filled {rid} 9040 {}", data.len());
    eventually("the newer fill reported", || svc.calls().contains(&filled)).await;
    assert_eq!(std::fs::read(&p).unwrap(), data);
    // Past every retry the old report would have made by now.
    tokio::time::sleep(Duration::from_secs(9)).await;
    assert!(
        !svc.calls().iter().any(|c| c.starts_with("recall_failed")),
        "a stale failure report failed the recall: {:?}",
        svc.calls()
    );
    assert_eq!(svc.recall_state(rid).as_deref(), Some("available"));
    d.shutdown().await;
}

/// #52 review, finding 1: an interrupted fill of a rewind placeholder is
/// not resumed on a timer (its entry is forgotten 5 s after FAILED, so the
/// resume found no placeholder and reported the recall failed). The recall
/// is the live file's too, so that report also stopped the live file's
/// fill. Now the rewind read is left to the reader's next open; the live
/// file's fill resumes and completes, and nothing is reported failed.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_interrupted_rewind_fill_is_left_to_the_next_open() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let exe = std::env::current_exe().unwrap();
    let cfg = config_fsal(
        dir.path(),
        &exe.display().to_string(),
        "reconnect_wait_ms = 1500",
    );
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.set_archived(true);
    svc.push_event(events::share_with_rewind(1, 100, "proj", 2, 1));
    svc.push_event(events::dir(100, None, "", 1));
    let data = pattern(2 << 20);
    let crc = omnifile_integrity::crc64_nvme(&data);
    let live_ev = events::file(
        905,
        100,
        "both.segy",
        data.len() as u64,
        9050,
        Some(crc),
        true,
        None,
        1,
    );
    let asof = live_ev["file"].clone();
    svc.push_event(live_ev);
    svc.put_blob(905, 9050, data.clone());
    let live = data_root.join("proj").join("both.segy");
    eventually("live placeholder", || marker(&live).is_some()).await;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    let today = omnifile_edge::rewind::label_of(now - now.rem_euclid(86_400));
    let label = data_root.join(".omni/snap/proj").join(&today);
    eventually("label directories", || label.is_dir()).await;
    let t = omnifile_edge::rewind::label_time(&today).unwrap();
    svc.set_asof(100, &t, vec![asof]);
    let mut fsal = Fsal::connect(&socket).await;
    fsal.send(&Message::PopulateDir(
        omnifile_fsal_proto::wire::PopulateDir {
            obj: obj_of(&label, 100),
            identity: Identity::default(),
            principal: vec![],
            path_hint: vec![],
        },
    ))
    .await;
    fsal.expect(|m| matches!(m, Message::PopulateResult(_)))
        .await;
    let a = label.join("both.segy");
    let am = marker(&a).unwrap();
    assert_eq!((am.file_id, am.version_id), (905, 9050));
    // A read of the label's copy, the live file's current version: one
    // recall for both.
    let mut ri = intent(&a, 905, 9050, Policy::Wait);
    ri.marker_flags = am.flags.bits();
    fsal.send(&Message::ReadIntent(ri)).await;
    let Message::RecallState(rs) = fsal
        .expect(
            |m| matches!(m, Message::RecallState(s) if s.obj.file_id == 905 && s.recall_id != 0),
        )
        .await
    else {
        panic!()
    };
    let rid = rs.recall_id;
    // Ganesha goes away; the recall reaches `downloading`: both fills (the
    // live file's and the label's) fail their fence, interrupted.
    fsal.close();
    svc.set_recall_state(rid, "downloading");
    svc.push_event(events::recall(
        rid,
        905,
        9050,
        "downloading",
        Some("mem://905/9050"),
        5,
    ));
    use std::os::unix::fs::MetadataExt;
    let live_key = (905, 9050, 0);
    let label_key = (905, 9050, std::fs::metadata(&a).unwrap().ino());
    let failed = |k| {
        d.core
            .recalls
            .lock()
            .unwrap()
            .get(&k)
            .is_some_and(|e| e.phase == RecallPhase::Failed)
    };
    eventually("both fills failed for now", || {
        failed(live_key) && failed(label_key)
    })
    .await;
    // Ganesha stays away past the first timer resume (5 s, which fails its
    // fence again): from then on the label's entry, forgotten 5 s after its
    // FAILED, is gone before any later resume of it.
    tokio::time::sleep(Duration::from_secs(9)).await;
    // Ganesha back: the live file's fill resumes and completes.
    let _fsal = Fsal::connect(&socket).await;
    let deadline = Instant::now() + Duration::from_secs(30);
    while marker(&live).is_none_or(|m| m.resident_bytes != data.len() as u64) {
        assert!(
            Instant::now() < deadline,
            "the live fill never completed: {:?}",
            svc.calls()
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert_eq!(std::fs::read(&live).unwrap(), data);
    let filled = format!("recall_filled {rid} 9050 {}", data.len());
    eventually("reported filled", || svc.calls().contains(&filled)).await;
    // Past any resume of the label's fill made with the live file's.
    tokio::time::sleep(Duration::from_secs(3)).await;
    assert!(
        !svc.calls().iter().any(|c| c.starts_with("recall_failed")),
        "the rewind fill's resume reported the shared recall failed: {:?}",
        svc.calls()
    );
    d.shutdown().await;
}

/// A daemon on an empty share `proj`, committing with a `quiet_s` quiet
/// period and a `reclaim_s` reclaim window after a Ganesha instance
/// connects; `no_ganesha` for an edge without one. Returns it, the service,
/// the share's root and the socket.
async fn commit_world(
    dir: &Path,
    quiet_s: u64,
    reclaim_s: u64,
    no_ganesha: bool,
) -> (
    Daemon<FakeService, FakeService>,
    Arc<FakeService>,
    PathBuf,
    PathBuf,
) {
    let mut cfg = config(dir);
    cfg.commit.quiet_s = quiet_s;
    cfg.fsal.reclaim_window_s = reclaim_s;
    cfg.fsal.no_ganesha = no_ganesha;
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let root = cfg.data_root.join("proj");
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.push_event(events::share(1, 100, "proj", "fail", 1));
    svc.push_event(events::dir(100, None, "", 1));
    eventually("the share", || root.is_dir()).await;
    (d, svc, root, socket)
}

fn seen(p: &Path, closed: bool) -> Option<Seen> {
    let m = std::fs::metadata(p).unwrap();
    Some(Seen {
        size: m.len(),
        mtime_ns: m.mtime() * 1_000_000_000 + m.mtime_nsec(),
        closed,
        at: Instant::now(),
    })
}

fn uploads(svc: &FakeService) -> usize {
    svc.calls()
        .iter()
        .filter(|c| c.starts_with("upload_start"))
        .count()
}

/// Decision 67: a daemon with a plain share `proj` (root 100, quiet 1 s) and a
/// WORM share `legal` (root 200, its default of `worm_s`).
async fn quiet_world(
    dir: &Path,
    worm_s: u64,
    detect: bool,
) -> (
    Daemon<FakeService, FakeService>,
    Arc<FakeService>,
    PathBuf,
    PathBuf,
) {
    let mut cfg = config(dir);
    cfg.commit.quiet_s = 1;
    cfg.commit.worm_quiet_s = worm_s;
    cfg.commit.detect = detect;
    cfg.fsal.reclaim_window_s = 0;
    cfg.fsal.no_ganesha = true;
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let (proj, legal) = (cfg.data_root.join("proj"), cfg.data_root.join("legal"));
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.push_event(events::share(1, 100, "proj", "fail", 1));
    svc.push_event(events::dir(100, None, "", 1));
    svc.push_event(events::share_quiet(2, 200, "legal", None, true, 1));
    svc.push_event(events::dir(200, None, "", 1));
    eventually("the shares", || proj.is_dir() && legal.is_dir()).await;
    // The table is rebuilt from the replica every 5 s. Before that every path
    // has the longer default, so wait for the plain share's own 1 s.
    let deadline = Instant::now() + Duration::from_secs(15);
    while d.core.quiet.of_rel(Path::new("proj/f")) != Duration::from_secs(1) {
        assert!(Instant::now() < deadline, "the shares' periods never came");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert_eq!(
        d.core.quiet.of_rel(Path::new("legal/f")),
        Duration::from_secs(worm_s)
    );
    (d, svc, proj, legal)
}

fn resident(p: &Path) -> bool {
    marker(p).is_some_and(|m| m.state == State::Resident)
}

fn write_from_child(p: &Path, data: &str) {
    let st = std::process::Command::new("sh")
        .arg("-c")
        .arg(format!("printf '%s' '{data}' > '{}'", p.display()))
        .status()
        .unwrap();
    assert!(st.success());
}

/// Decision 67, through the real detector: each file waits its own share's
/// period, an existing WORM share with no value of its own takes the WORM
/// default, and a change from the feed reaches a file already waiting, with no
/// restart.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn each_share_waits_its_own_quiet_period_and_a_change_reaches_a_waiting_file() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, proj, legal) = quiet_world(dir.path(), 300, true).await;
    let (fast, slow) = (proj.join("a.bin"), legal.join("b.bin"));
    write_from_child(&fast, "plain");
    write_from_child(&slow, "worm!");
    eventually("the plain share's file committed", || resident(&fast)).await;
    tokio::time::sleep(Duration::from_secs(3)).await;
    assert!(!resident(&slow), "the WORM share waits its own period");
    // And the detector itself waits it: it hasn't handed the file to the commit,
    // whose further wait would otherwise hold it (and hide a detector that didn't).
    let ino = std::fs::metadata(&slow).unwrap().ino();
    assert!(
        !d.core.growth.lock().unwrap().keys().any(|k| k.1 == ino),
        "the detector reported the WORM share's file after the default period"
    );
    // An Owner sets 10 s on it: the file is by now part way through waiting.
    svc.push_event(events::share_quiet(2, 200, "legal", Some(10), true, 2));
    let deadline = Instant::now() + Duration::from_secs(60);
    while !resident(&slow) {
        assert!(
            Instant::now() < deadline,
            "a shorter period never reached the waiting file"
        );
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    d.shutdown().await;
}

/// This process's descriptors that name `name` (the daemon runs here).
fn held_fds(name: &str) -> usize {
    std::fs::read_dir("/proc/self/fd")
        .unwrap()
        .filter_map(|e| std::fs::read_link(e.ok()?.path()).ok())
        .filter(|l| l.to_string_lossy().contains(name))
        .count()
}

fn avail_mib(p: &Path) -> u64 {
    let c = std::ffi::CString::new(p.as_os_str().as_encoded_bytes()).unwrap();
    // SAFETY: statvfs fills the struct it is given; `c` is a valid C string.
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    assert_eq!(unsafe { libc::statvfs(c.as_ptr(), &mut st) }, 0);
    st.f_bavail as u64 * st.f_frsize as u64 / (1 << 20)
}

/// 2026-11-08 regression (the soak's volume stayed full for 600 s after each
/// `fill_disk`): a file deleted while the detector waits out its share's
/// period gives its space back within seconds, not when the period ends. Here
/// a 256 MiB file on a WORM share with a 300 s period.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_file_deleted_while_it_waits_gives_its_space_back() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, _proj, legal) = quiet_world(dir.path(), 300, true).await;
    let p = legal.join("deleted-soon-256m.bin");
    let st = std::process::Command::new("dd")
        .args(["if=/dev/zero", "bs=1M", "count=256", "status=none"])
        .arg(format!("of={}", p.display()))
        .status()
        .unwrap();
    assert!(st.success());
    eventually("the detector waits on it", || {
        held_fds("deleted-soon-256m.bin") > 0
    })
    .await;
    let full = avail_mib(&legal);
    std::fs::remove_file(&p).unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    while held_fds("deleted-soon-256m.bin") != 0 || avail_mib(&legal) < full + 200 {
        assert!(
            Instant::now() < deadline,
            "its space is still taken: {} fds held, {} MiB back of 256",
            held_fds("deleted-soon-256m.bin"),
            avail_mib(&legal).saturating_sub(full)
        );
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    assert_eq!(uploads(&svc), 0, "a deleted file is never committed");
    d.shutdown().await;
}

/// A file directly under the data root is in no share and is never committed:
/// the daemon's detector doesn't wait on it, so it never holds it open (the
/// soak's `.chaos-fill` was held for the 600 s a path in no share waits).
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_file_directly_under_the_data_root_is_not_held_open() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, _svc, proj, _legal) = quiet_world(dir.path(), 300, true).await;
    let stray = proj.parent().unwrap().join("stray-at-the-root.bin");
    write_from_child(&stray, "not in any share");
    write_from_child(&proj.join("in-the-share.bin"), "plain");
    eventually("the share's file committed", || {
        resident(&proj.join("in-the-share.bin"))
    })
    .await;
    assert_eq!(
        held_fds("stray-at-the-root.bin"),
        0,
        "held open, though never committed"
    );
    d.shutdown().await;
}

/// Decision 67: the commit's further wait is the share's period too, for a
/// file the detector saw go quiet.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_further_wait_is_the_share_s_period() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, _svc, proj, legal) = quiet_world(dir.path(), 8, false).await;
    let (fast, slow) = (proj.join("a.bin"), legal.join("b.bin"));
    for p in [&fast, &slow] {
        std::fs::write(p, pattern(1 << 20)).unwrap();
        d.commits
            .send(CloseWrite {
                path: p.to_path_buf(),
                ino: std::fs::metadata(p).unwrap().ino(),
                swept_gen: None,
                token: None,
                seen: seen(p, true),
            })
            .await
            .unwrap();
    }
    eventually("the plain share's file committed", || resident(&fast)).await;
    // Past the default (1 s) and its first looks, inside the WORM share's 8 s.
    tokio::time::sleep(Duration::from_secs(3)).await;
    assert!(!resident(&slow), "held for the WORM share's period");
    let deadline = Instant::now() + Duration::from_secs(40);
    while !resident(&slow) {
        assert!(
            Instant::now() < deadline,
            "the WORM share's file never committed"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    d.shutdown().await;
}

/// Decision 67: a file no close was seen for (a sweep) waits for its share's
/// period too.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_swept_file_waits_for_its_share_s_period() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, _svc, _proj, legal) = quiet_world(dir.path(), 10, false).await;
    let slow = legal.join("b.bin");
    std::fs::write(&slow, pattern(1 << 20)).unwrap();
    let fd = sys::open_ro(&slow).unwrap();
    let key = sys::inode_key(fd.as_fd()).unwrap();
    let sweep = CloseWrite {
        path: slow.clone(),
        ino: key.ino,
        swept_gen: Some(key.ino_gen),
        token: None,
        seen: None,
    };
    // Past the default of 1 s, inside the WORM share's 10 s.
    tokio::time::sleep(Duration::from_secs(2)).await;
    d.commits.send(sweep).await.unwrap();
    tokio::time::sleep(Duration::from_secs(3)).await;
    assert!(!resident(&slow), "written within the WORM share's period");
    let deadline = Instant::now() + Duration::from_secs(40);
    while !resident(&slow) {
        assert!(Instant::now() < deadline, "the swept file never committed");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    d.shutdown().await;
}

/// Partial-version fix (WP3's soak: a version committed mid-write on a
/// large NFSv4.1 copy). A file the FSAL says an NFSv4 client still holds
/// open for write is not committed, however quiet it went; once the OPEN
/// is closed it is, once, with all its bytes.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_file_an_nfsv4_client_holds_open_for_write_is_not_committed() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root, socket) = commit_world(dir.path(), 1, 0, false).await;
    let fsal = Fsal::connect_with(&socket, feature::PARK | feature::OPEN_STATE).await;
    fsal.open_writes.store(1, Ordering::SeqCst);
    let p = root.join("copying.segy");
    let half = pattern(2 << 20);
    std::fs::write(&p, &half).unwrap();
    // Quiet (here a close of Ganesha's fd was even seen), but the OPEN stays.
    d.commits
        .send(CloseWrite {
            path: p.clone(),
            ino: std::fs::metadata(&p).unwrap().ino(),
            swept_gen: None,
            token: None,
            seen: seen(&p, true),
        })
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_secs(3)).await;
    assert!(marker(&p).is_none(), "committed while open for write");
    assert_eq!(uploads(&svc), 0);
    // The copy finishes and the client closes.
    let mut all = half.clone();
    all.extend(pattern(1 << 20));
    std::fs::write(&p, &all).unwrap();
    fsal.open_writes.store(0, Ordering::SeqCst);
    d.commits
        .send(CloseWrite {
            path: p.clone(),
            ino: std::fs::metadata(&p).unwrap().ino(),
            swept_gen: None,
            token: None,
            seen: seen(&p, true),
        })
        .await
        .unwrap();
    eventually("committed once closed", || {
        marker(&p).is_some_and(|m| m.state == State::Resident)
    })
    .await;
    let m = marker(&p).unwrap();
    assert_eq!(svc.blob(m.file_id, m.version_id).unwrap(), all);
    tokio::time::sleep(Duration::from_secs(3)).await;
    assert_eq!(uploads(&svc), 1, "one version, never a partial one");
    d.shutdown().await;
}

/// #80 review, item 4: a write handed to the commit with no detector view and
/// no sweep generation, as the outbox's re-commit and `commit_modified` (after
/// a repair, a remote change or a conflict) send it, waits out the quiet
/// period too. Before, it was committed at once: a writer that was still going
/// got a version of its file part-way, on any share, WORM included.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_write_handed_over_with_no_view_isnt_committed_part_way() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root, _socket) = commit_world(dir.path(), 3, 0, true).await;
    let p = root.join("growing.segy");
    let chunk = pattern(256 << 10);
    let mut want = chunk.clone();
    std::fs::write(&p, &chunk).unwrap();
    // A writer that goes on, with pauses shorter than the period.
    let writer = {
        let (p, chunk) = (p.clone(), chunk.clone());
        std::thread::spawn(move || {
            use std::io::Write;
            for _ in 0..8 {
                std::thread::sleep(Duration::from_millis(500));
                let mut f = std::fs::OpenOptions::new().append(true).open(&p).unwrap();
                f.write_all(&chunk).unwrap();
            }
        })
    };
    for _ in 0..8 {
        want.extend_from_slice(&chunk);
    }
    // Handed over mid-write, as `commit_modified` does: no view, inode 0.
    tokio::time::sleep(Duration::from_millis(700)).await;
    d.commits.send(CloseWrite::new(p.clone(), 0)).await.unwrap();
    writer.join().unwrap();
    let deadline = Instant::now() + Duration::from_secs(40);
    while !resident(&p) {
        assert!(Instant::now() < deadline, "never committed");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let m = marker(&p).unwrap();
    assert_eq!(
        svc.blob(m.file_id, m.version_id).unwrap().len(),
        want.len(),
        "a version of the file part-way"
    );
    assert_eq!(uploads(&svc), 1, "one version, never a partial one");
    d.shutdown().await;
}

/// #82 review: a writer that puts back an old mtime after each write (an SMB
/// SetInfo, `touch -d`) and pauses for less than the period is not committed
/// part-way: the wait is timed on the daemon's own clock, not from the mtime.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_writer_that_sets_an_old_mtime_isnt_committed_part_way() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root, _socket) = commit_world(dir.path(), 3, 0, true).await;
    let p = root.join("old-mtime.segy");
    let chunk = pattern(256 << 10);
    let old = std::time::SystemTime::now() - Duration::from_secs(3600);
    let put_back = move |p: &Path| {
        std::fs::File::options()
            .write(true)
            .open(p)
            .unwrap()
            .set_modified(old)
            .unwrap();
    };
    std::fs::write(&p, &chunk).unwrap();
    put_back(&p);
    let writer = {
        let (p, chunk) = (p.clone(), chunk.clone());
        std::thread::spawn(move || {
            use std::io::Write;
            for _ in 0..4 {
                std::thread::sleep(Duration::from_millis(1200));
                let mut f = std::fs::OpenOptions::new().append(true).open(&p).unwrap();
                f.write_all(&chunk).unwrap();
                drop(f);
                put_back(&p);
            }
        })
    };
    tokio::time::sleep(Duration::from_millis(300)).await;
    d.commits.send(CloseWrite::new(p.clone(), 0)).await.unwrap();
    writer.join().unwrap();
    let deadline = Instant::now() + Duration::from_secs(60);
    while !resident(&p) {
        assert!(Instant::now() < deadline, "never committed");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let m = marker(&p).unwrap();
    assert_eq!(
        svc.blob(m.file_id, m.version_id).unwrap().len(),
        5 * chunk.len(),
        "a version of the file part-way"
    );
    assert_eq!(uploads(&svc), 1, "one version, never a partial one");
    d.shutdown().await;
}

/// #82 review: a write no one watched isn't committed while another process
/// holds it open for write, quiet or not, as for a sweep.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_write_no_one_watched_waits_while_it_is_open_for_write() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root, _socket) = commit_world(dir.path(), 2, 0, true).await;
    let p = root.join("held.segy");
    std::fs::write(&p, pattern(1 << 20)).unwrap();
    // A writer holds it open, quiet, well past the period, then finishes.
    let mut holder = std::process::Command::new("sh")
        .arg("-c")
        .arg(format!(
            "exec 3>>'{}'; sleep 7; printf 'the end' >&3",
            p.display()
        ))
        .spawn()
        .unwrap();
    tokio::time::sleep(Duration::from_millis(300)).await;
    d.commits.send(CloseWrite::new(p.clone(), 0)).await.unwrap();
    tokio::time::sleep(Duration::from_secs(5)).await;
    assert!(!resident(&p), "committed while open for write");
    assert_eq!(uploads(&svc), 0);
    holder.wait().unwrap();
    let deadline = Instant::now() + Duration::from_secs(60);
    while !resident(&p) {
        assert!(Instant::now() < deadline, "never committed");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let m = marker(&p).unwrap();
    assert!(
        svc.blob(m.file_id, m.version_id)
            .unwrap()
            .ends_with(b"the end")
    );
    d.shutdown().await;
}

/// #80 review, item 4: a file written moments ago and handed over with no view
/// isn't committed before its period is over.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_write_handed_over_with_no_view_waits_out_the_period() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root, _socket) = commit_world(dir.path(), 6, 0, true).await;
    let p = root.join("fresh.segy");
    std::fs::write(&p, pattern(1 << 20)).unwrap();
    d.commits.send(CloseWrite::new(p.clone(), 0)).await.unwrap();
    tokio::time::sleep(Duration::from_secs(4)).await;
    assert!(!resident(&p), "committed within the period");
    assert_eq!(uploads(&svc), 0);
    let deadline = Instant::now() + Duration::from_secs(40);
    while !resident(&p) {
        assert!(Instant::now() < deadline, "never committed");
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    d.shutdown().await;
}

/// Partial-version fix, NFSv3 (no open state): a file the detector saw go
/// quiet is committed only once its size and mtime have held for a further
/// quiet period, even with a close seen (Ganesha closes an NFSv3 writer's fd
/// after each request: #54 review, S2), and a later event without the
/// detector's view (a sweep) does not skip the check under way (M1). Here
/// the writer resumes after a pause longer than the quiet period, and goes
/// on after the sweep's event: no half-written file is ever a version.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_file_quiet_with_no_close_waits_for_its_size_and_mtime_to_hold() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root, _socket) = commit_world(dir.path(), 2, 0, true).await;
    let p = root.join("v3copy.segy");
    let ino = || std::fs::metadata(&p).unwrap().ino();
    let mut all = pattern(2 << 20);
    std::fs::write(&p, &all).unwrap();
    // Quiet, and a close seen: Ganesha's, after one NFSv3 WRITE.
    d.commits
        .send(CloseWrite {
            path: p.clone(),
            ino: ino(),
            swept_gen: None,
            token: None,
            seen: seen(&p, true),
        })
        .await
        .unwrap();
    // The copy goes on after the pause; an event without the detector's view.
    tokio::time::sleep(Duration::from_millis(1500)).await;
    let append = |all: &mut Vec<u8>, n: usize| {
        let more = pattern(n);
        use std::io::Write;
        std::fs::OpenOptions::new()
            .append(true)
            .open(&p)
            .unwrap()
            .write_all(&more)
            .unwrap();
        all.extend(more);
    };
    append(&mut all, 1 << 20);
    d.commits
        .send(CloseWrite {
            path: p.clone(),
            ino: ino(),
            swept_gen: None,
            token: None,
            seen: None,
        })
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(500)).await;
    append(&mut all, 1 << 20);
    tokio::time::sleep(Duration::from_secs(1)).await;
    assert!(marker(&p).is_none(), "a version committed mid-write");
    assert_eq!(uploads(&svc), 0);
    // Now it holds still.
    eventually("committed once it held still", || {
        marker(&p).is_some_and(|m| m.state == State::Resident)
    })
    .await;
    let m = marker(&p).unwrap();
    assert_eq!(svc.blob(m.file_id, m.version_id).unwrap(), all);
    assert_eq!(uploads(&svc), 1, "one version, never a partial one");
    d.shutdown().await;
}

/// #54 review, B2: a Ganesha restart during an NFSv4 write. Its exit closes
/// the client's fd (so the detector sees a close), the client keeps its
/// OPEN and reclaims it once the new Ganesha is up. With no FSAL connected,
/// and while the new instance's reclaim window lasts, nothing is committed;
/// after it, an answer of 0 is believed.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_ganesha_restart_during_an_nfsv4_write_commits_nothing_partial() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let (d, svc, root, socket) = commit_world(dir.path(), 1, 4, false).await;
    let feats = feature::PARK | feature::OPEN_STATE;
    let old = Fsal::connect_instance(&socket, feats, 41).await;
    // The first instance's own window passes; the client holds its OPEN.
    tokio::time::sleep(Duration::from_secs(4)).await;
    old.open_writes.store(1, Ordering::SeqCst);
    let p = root.join("restart.segy");
    let mut all = pattern(2 << 20);
    std::fs::write(&p, &all).unwrap();
    let close = |p: &Path| CloseWrite {
        path: p.to_path_buf(),
        ino: std::fs::metadata(p).unwrap().ino(),
        swept_gen: None,
        token: None,
        seen: None,
    };
    d.commits.send(close(&p)).await.unwrap();
    // Ganesha stops: its exit closes the fd.
    old.close();
    tokio::time::sleep(Duration::from_millis(300)).await;
    d.commits.send(close(&p)).await.unwrap();
    tokio::time::sleep(Duration::from_secs(2)).await;
    assert!(marker(&p).is_none(), "committed with no FSAL to ask");
    // The new Ganesha: the client has not reclaimed its OPEN yet (0).
    let new = Fsal::connect_instance(&socket, feats, 42).await;
    d.commits.send(close(&p)).await.unwrap();
    tokio::time::sleep(Duration::from_secs(1)).await;
    assert!(marker(&p).is_none(), "committed within the reclaim window");
    // The client reclaims and finishes its copy, then closes.
    new.open_writes.store(1, Ordering::SeqCst);
    all.extend(pattern(1 << 20));
    std::fs::write(&p, &all).unwrap();
    tokio::time::sleep(Duration::from_secs(4)).await;
    d.commits.send(close(&p)).await.unwrap();
    tokio::time::sleep(Duration::from_secs(1)).await;
    assert!(
        marker(&p).is_none(),
        "committed while reclaimed open for write"
    );
    new.open_writes.store(0, Ordering::SeqCst);
    d.commits.send(close(&p)).await.unwrap();
    eventually("committed once closed", || {
        marker(&p).is_some_and(|m| m.state == State::Resident)
    })
    .await;
    let m = marker(&p).unwrap();
    assert_eq!(svc.blob(m.file_id, m.version_id).unwrap(), all);
    assert_eq!(uploads(&svc), 1, "one version, never a partial one");
    d.shutdown().await;
}

/// A small xfs volume of its own, loop-mounted under `dir` (unmounted on
/// drop): a test can fill it without starving the other tests' volume.
struct SmallXfs {
    mnt: PathBuf,
}

impl SmallXfs {
    fn new(dir: &Path, mib: u64) -> Option<SmallXfs> {
        use std::process::Command;
        let img = dir.join("small.img");
        let mnt = dir.join("small");
        std::fs::create_dir_all(&mnt).ok()?;
        std::fs::File::create(&img).ok()?.set_len(mib << 20).ok()?;
        let ok = |c: &mut Command| c.status().is_ok_and(|s| s.success());
        if !ok(Command::new("mkfs.xfs").arg("-q").arg(&img))
            || !ok(Command::new("mount")
                .arg("-o")
                .arg("loop")
                .arg(&img)
                .arg(&mnt))
        {
            eprintln!("skipping: no loop-mounted xfs here");
            return None;
        }
        Some(SmallXfs { mnt })
    }

    /// Fill the volume with one preallocated file, leaving `leave` bytes.
    fn fill(&self, leave: u64) -> PathBuf {
        let ballast = self.mnt.join("ballast");
        let f = std::fs::File::create(&ballast).unwrap();
        // SAFETY: plain syscalls on valid fds and a zeroed statvfs.
        let free = unsafe {
            let mut st: libc::statvfs = std::mem::zeroed();
            let c = std::ffi::CString::new(self.mnt.as_os_str().as_encoded_bytes()).unwrap();
            assert_eq!(libc::statvfs(c.as_ptr(), &mut st), 0);
            st.f_bavail * st.f_frsize
        };
        let len = free.saturating_sub(leave) as libc::off_t;
        use std::os::fd::AsRawFd;
        // SAFETY: a valid fd.
        let r = unsafe { libc::fallocate(f.as_raw_fd(), 0, 0, len) };
        assert_eq!(r, 0, "fallocate: {}", std::io::Error::last_os_error());
        ballast
    }
}

impl Drop for SmallXfs {
    fn drop(&mut self) {
        let _ = std::process::Command::new("umount")
            .arg("-l")
            .arg(&self.mnt)
            .status();
    }
}

/// fill-enospc-resume (WP3's soak: recalls 21, 271, 437 and 702 failed for
/// good with "No space left on device" during a short fill_disk fault). A
/// recall's fill that finds the data volume full is only interrupted: it is
/// resumed with the usual backoff, and once space is freed (the cache
/// evicts, a user deletes files) the recall completes. Nothing is reported
/// failed.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_recall_fill_that_runs_out_of_space_resumes_once_space_is_freed() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let Some(small) = SmallXfs::new(dir.path(), 512) else {
        return;
    };
    let exe = std::env::current_exe().unwrap();
    let mut cfg = config_fsal(dir.path(), &exe.display().to_string(), "no_ganesha = true");
    cfg.data_root = small.mnt.join("data");
    std::fs::create_dir_all(&cfg.data_root).unwrap();
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.set_archived(true);
    svc.push_event(events::share(1, 100, "proj", "wait", 1));
    svc.push_event(events::dir(100, None, "", 1));
    let data = pattern(16 << 20);
    svc.push_event(events::file(
        951,
        100,
        "full.segy",
        data.len() as u64,
        9510,
        Some(omnifile_integrity::crc64_nvme(&data)),
        true,
        None,
        1,
    ));
    svc.put_blob(951, 9510, data.clone());
    let p = data_root.join("proj").join("full.segy");
    eventually("placeholder", || marker(&p).is_some()).await;
    // The volume full, a little short of the file.
    let ballast = small.fill(4 << 20);
    let rid = svc.add_recall(951, 9510, "downloading");
    svc.push_event(events::recall(
        rid,
        951,
        9510,
        "downloading",
        Some("mem://951/9510"),
        5,
    ));
    let key = (951, 9510, 0);
    eventually("the fill ran out of space", || {
        d.core
            .recalls
            .lock()
            .unwrap()
            .get(&key)
            .is_some_and(|e| e.phase == RecallPhase::Failed)
    })
    .await;
    assert!(
        !svc.calls().iter().any(|c| c.starts_with("recall_failed")),
        "a full volume is not a permanent failure: {:?}",
        svc.calls()
    );
    // Space comes back: the resumed fill completes.
    std::fs::remove_file(&ballast).unwrap();
    let filled = format!("recall_filled {rid} 9510 {}", data.len());
    let deadline = Instant::now() + Duration::from_secs(30);
    while !svc.calls().contains(&filled) {
        assert!(
            Instant::now() < deadline,
            "the fill never resumed: {:?}",
            svc.calls()
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert_eq!(std::fs::read(&p).unwrap(), data);
    assert!(!svc.calls().iter().any(|c| c.starts_with("recall_failed")));
    d.shutdown().await;
    drop(small);
}

/// #62 review: while a resume of an interrupted fill is scheduled, a reader
/// who opens the file again joins the recall but starts no fill of its own:
/// readers retrying every few seconds would skip the backoff, and each fill
/// would re-read the whole on-disk prefix for its CRC. The scheduled resume
/// completes the recall once space is back. (Readers within 5 s of a FAILED
/// get its answer; this is the read after that, during a longer backoff.)
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_reader_does_not_restart_a_fill_whose_resume_is_scheduled() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let Some(small) = SmallXfs::new(dir.path(), 512) else {
        return;
    };
    let exe = std::env::current_exe().unwrap();
    let mut cfg = config_fsal(dir.path(), &exe.display().to_string(), "");
    cfg.data_root = small.mnt.join("data");
    std::fs::create_dir_all(&cfg.data_root).unwrap();
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let data_root = cfg.data_root.clone();
    let socket = cfg.socket_path.clone();
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    let mut fsal = Fsal::connect(&socket).await;
    svc.set_archived(true);
    svc.push_event(events::share(1, 100, "proj", "wait", 1));
    svc.push_event(events::dir(100, None, "", 1));
    let data = pattern(16 << 20);
    svc.push_event(events::file(
        952,
        100,
        "again.segy",
        data.len() as u64,
        9520,
        Some(omnifile_integrity::crc64_nvme(&data)),
        true,
        None,
        1,
    ));
    svc.put_blob(952, 9520, data.clone());
    let p = data_root.join("proj").join("again.segy");
    eventually("placeholder", || marker(&p).is_some()).await;
    let ballast = small.fill(4 << 20);
    let rid = svc.add_recall(952, 9520, "downloading");
    svc.push_event(events::recall(
        rid,
        952,
        9520,
        "downloading",
        Some("mem://952/9520"),
        5,
    ));
    // The fill runs out of space: FAILED, a resume in 5 s; that one too, and
    // the next resume is 10 s away.
    for _ in 0..2 {
        fsal.expect(|m| {
            matches!(m, Message::RecallState(s) if s.obj.file_id == 952 && s.state == RecallPhase::Failed)
        })
        .await;
    }
    // The FAILED entry is forgotten 5 s later; the resume is still pending.
    eventually("the failed entry forgotten", || {
        !d.core.recalls.lock().unwrap().contains_key(&(952, 9520, 0))
    })
    .await;
    // A reader opens the file again: it joins the recall, and no fill starts.
    fsal.send(&Message::ReadIntent(intent(&p, 952, 9520, Policy::Wait)))
        .await;
    let fetching = tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            let Some(f) = fsal.rx.recv().await else {
                return false;
            };
            if matches!(f.msg, Message::RecallState(s) if s.obj.file_id == 952 && s.state == RecallPhase::Fetching)
            {
                return true;
            }
        }
    })
    .await;
    assert!(
        fetching.is_err(),
        "a reader started a fill while its resume was scheduled"
    );
    assert!(
        svc.calls()
            .iter()
            .any(|c| c.starts_with("recall_start 952"))
    );
    // Space comes back: the scheduled resume completes the recall.
    std::fs::remove_file(&ballast).unwrap();
    let filled = format!("recall_filled {rid} 9520 {}", data.len());
    let deadline = Instant::now() + Duration::from_secs(30);
    while !svc.calls().contains(&filled) {
        assert!(
            Instant::now() < deadline,
            "the resume never completed: {:?}",
            svc.calls()
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert_eq!(std::fs::read(&p).unwrap(), data);
    d.shutdown().await;
    drop(small);
}

// --- a recall whose file was replaced or deleted (control API §4.4, migration 0059) ---

/// The recall of `archived_world`'s file, started by a read and already
/// `downloading` in the catalog; its event is not sent yet.
async fn started_recall(svc: &FakeService, fsal: &mut Fsal, p: &Path, fid: u64, vid: u64) -> u64 {
    fsal.send(&Message::ReadIntent(intent(p, fid, vid, Policy::Wait)))
        .await;
    let Message::RecallState(rs) = fsal
        .expect(
            |m| matches!(m, Message::RecallState(s) if s.obj.file_id == fid && s.recall_id != 0),
        )
        .await
    else {
        panic!()
    };
    svc.set_recall_state(rs.recall_id, "downloading");
    rs.recall_id
}

/// Nothing is reported for `rid` while a failure report would have had time
/// to go (the settle wait, and the first try).
async fn never_reported_failed(svc: &FakeService, rid: u64) {
    tokio::time::sleep(Duration::from_secs(4)).await;
    let calls = svc.calls();
    assert!(
        !calls.iter().any(|c| c.starts_with("recall_failed")),
        "a replaced file's recall was reported failed: {calls:?}"
    );
    assert!(!calls.iter().any(|c| c.starts_with("recall_filled")));
    assert_eq!(svc.recall_state(rid).as_deref(), Some("downloading"));
}

/// The soak's recall 209: a writer replaced the file in place while it was
/// being recalled. The FSAL removes the marker when a stub is truncated
/// (`fsal-socket.md` §6.1), so the fill finds a plain file: the user's new
/// bytes, not committed yet. That is no failure of the recall, which the
/// service ends when the new version is committed.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_fill_that_meets_a_file_replaced_in_place_reports_nothing() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let data = pattern(2 << 20);
    let (d, svc, p, socket) =
        archived_world(dir.path(), 911, 9110, "cut.segy", &data, true, "").await;
    let mut fsal = Fsal::connect(&socket).await;
    let rid = started_recall(&svc, &mut fsal, &p, 911, 9110).await;
    // The live read is of whatever the file holds: not pinned.
    assert!(
        svc.calls()
            .iter()
            .any(|c| c.starts_with("recall_start 911 9110"))
    );
    assert!(!svc.calls().iter().any(|c| c.ends_with(" pinned")));
    // As the FSAL's truncate: the marker first, then the bytes.
    {
        let ro = sys::open_ro(&p).unwrap();
        xattr::remove(ro.as_fd()).unwrap();
    }
    std::fs::write(&p, b"shot again").unwrap();
    svc.push_event(events::recall(
        rid,
        911,
        9110,
        "downloading",
        Some("mem://911/9110"),
        5,
    ));
    never_reported_failed(&svc, rid).await;
    assert_eq!(
        std::fs::read(&p).unwrap(),
        b"shot again",
        "the user's bytes"
    );
    d.shutdown().await;
}

/// The same, once the new version is committed and the fill still has the
/// old node (a resume after an interruption, as in the soak): the marker
/// names another version of the file.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_fill_that_meets_a_newer_marker_reports_nothing() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let data = pattern(2 << 20);
    let (d, svc, p, socket) =
        archived_world(dir.path(), 912, 9120, "newer.segy", &data, true, "").await;
    let mut fsal = Fsal::connect(&socket).await;
    let rid = started_recall(&svc, &mut fsal, &p, 912, 9120).await;
    let was = marker(&p).unwrap();
    {
        let ro = sys::open_ro(&p).unwrap();
        let mut m = was;
        m.version_id = 9121;
        xattr::write_replace(ro.as_fd(), &m).unwrap();
    }
    svc.push_event(events::recall(
        rid,
        912,
        9120,
        "downloading",
        Some("mem://912/9120"),
        5,
    ));
    never_reported_failed(&svc, rid).await;
    assert_eq!(marker(&p).unwrap().resident_bytes, was.resident_bytes);
    d.shutdown().await;
}

/// And when another file was renamed over it (an editor's save).
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_fill_that_meets_another_file_under_the_name_reports_nothing() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let data = pattern(2 << 20);
    let (d, svc, p, socket) =
        archived_world(dir.path(), 913, 9130, "saved.segy", &data, true, "").await;
    let mut fsal = Fsal::connect(&socket).await;
    let rid = started_recall(&svc, &mut fsal, &p, 913, 9130).await;
    let tmp = p.with_file_name(".saved.segy.tmp");
    std::fs::write(&tmp, b"saved over it").unwrap();
    std::fs::rename(&tmp, &p).unwrap();
    svc.push_event(events::recall(
        rid,
        913,
        9130,
        "downloading",
        Some("mem://913/9130"),
        5,
    ));
    never_reported_failed(&svc, rid).await;
    assert_eq!(std::fs::read(&p).unwrap(), b"saved over it");
    d.shutdown().await;
}

/// And when the file was deleted: the fill finds nothing under the name, and
/// the delete reaches the replica while the edge waits to be sure. Nothing
/// is reported; the tombstone ends the recall.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_fill_whose_file_was_deleted_reports_nothing() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let data = pattern(2 << 20);
    let (d, svc, p, socket) =
        archived_world(dir.path(), 916, 9160, "deleted.segy", &data, true, "").await;
    let mut fsal = Fsal::connect(&socket).await;
    let rid = started_recall(&svc, &mut fsal, &p, 916, 9160).await;
    std::fs::remove_file(&p).unwrap();
    svc.push_event(events::recall(
        rid,
        916,
        9160,
        "downloading",
        Some("mem://916/9160"),
        5,
    ));
    // The catalog's tombstone follows, within the edge's settle wait.
    let mut gone = events::file(
        916,
        100,
        "deleted.segy",
        data.len() as u64,
        9160,
        Some(omnifile_integrity::crc64_nvme(&data)),
        true,
        None,
        7,
    );
    gone["type"] = serde_json::Value::from("file.tombstone");
    svc.push_event(gone);
    never_reported_failed(&svc, rid).await;
    assert!(!p.exists());
    d.shutdown().await;
}

/// The service ended the recall (`superseded`) after the file moved on: the
/// row has no live placeholder to be applied to, and before this its entry
/// stayed QUEUED and its note stayed next to the new file. The entry ends
/// as a cancelled one, and the note goes.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_superseded_recall_of_a_file_that_moved_on_is_tidied_away() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let data = pattern(2 << 20);
    let (d, svc, p, socket) =
        archived_world(dir.path(), 914, 9140, "moved.segy", &data, true, "").await;
    let mut fsal = Fsal::connect(&socket).await;
    fsal.send(&Message::ReadIntent(intent(&p, 914, 9140, Policy::Wait)))
        .await;
    let Message::RecallState(rs) = fsal
        .expect(
            |m| matches!(m, Message::RecallState(s) if s.obj.file_id == 914 && s.recall_id != 0),
        )
        .await
    else {
        panic!()
    };
    let note = p.with_file_name("moved.segy.RECALLING.txt");
    eventually("note", || note.exists()).await;
    // The catalog commits a newer version of the file (the feed says so).
    let new = b"a newer version".to_vec();
    svc.put_blob(914, 9141, new.clone());
    svc.push_event(events::file(
        914,
        100,
        "moved.segy",
        new.len() as u64,
        9141,
        Some(omnifile_integrity::crc64_nvme(&new)),
        false,
        None,
        7,
    ));
    eventually("the newer version", || {
        marker(&p).is_some_and(|m| m.version_id == 9141)
    })
    .await;
    svc.push_event(events::recall(
        rs.recall_id,
        914,
        9140,
        "superseded",
        None,
        8,
    ));
    fsal.expect(|m| {
        matches!(m, Message::RecallState(s)
            if s.obj.file_id == 914 && s.recall_id == rs.recall_id
                && s.state == RecallPhase::Cancelled)
    })
    .await;
    eventually("the note is gone", || !note.exists()).await;
    assert!(!svc.calls().iter().any(|c| c.starts_with("recall_failed")));
    // The reader that was waiting reads the file as it is now: its next read
    // is of the new version, which is online, and it gets those bytes.
    svc.set_archived(false);
    fsal.send(&Message::ReadIntent(intent(&p, 914, 9141, Policy::Wait)))
        .await;
    fsal.expect(|m| {
        matches!(m, Message::RecallState(s)
            if s.obj.file_id == 914 && s.version_id == 9141 && s.state == RecallPhase::Done)
    })
    .await;
    assert_eq!(std::fs::read(&p).unwrap(), new, "the new version's bytes");
    d.shutdown().await;
}

/// The file is deleted while a reader waits on its recall, and the service
/// ends the recall (`superseded`). A deleted file has no placeholder for the
/// row to be applied to; before this its entry stayed as it was, and a
/// reader of a `wait` share that still held the file open was told to wait
/// with nothing left to wait for. The entry ends with an error for that
/// reader, and the note next to where the file was goes too.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_reader_waiting_on_a_file_that_is_deleted_is_released() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let data = pattern(2 << 20);
    let (d, svc, p, socket) =
        archived_world(dir.path(), 915, 9150, "gone.segy", &data, true, "").await;
    let mut fsal = Fsal::connect(&socket).await;
    fsal.send(&Message::ReadIntent(intent(&p, 915, 9150, Policy::Wait)))
        .await;
    let Message::RecallState(rs) = fsal
        .expect(
            |m| matches!(m, Message::RecallState(s) if s.obj.file_id == 915 && s.recall_id != 0),
        )
        .await
    else {
        panic!()
    };
    assert!(!rs.state.is_terminal(), "the reader waits: {rs:?}");
    let note = p.with_file_name("gone.segy.RECALLING.txt");
    eventually("note", || note.exists()).await;
    // The catalog's tombstone of the file (its delete, through the feed).
    let mut gone = events::file(
        915,
        100,
        "gone.segy",
        data.len() as u64,
        9150,
        Some(omnifile_integrity::crc64_nvme(&data)),
        true,
        None,
        7,
    );
    gone["type"] = serde_json::Value::from("file.tombstone");
    svc.push_event(gone);
    eventually("the placeholder is removed", || !p.exists()).await;
    svc.push_event(events::recall(
        rs.recall_id,
        915,
        9150,
        "superseded",
        None,
        8,
    ));
    // FAILED, not CANCELLED: the inode the reader holds is no longer a file,
    // and a cancelled entry would tell a `wait` reader to wait again.
    let Message::RecallState(end) = fsal
        .expect(|m| {
            matches!(m, Message::RecallState(s)
                if s.recall_id == rs.recall_id && s.state.is_terminal())
        })
        .await
    else {
        panic!()
    };
    assert_eq!(
        (end.state, end.answer),
        (RecallPhase::Failed, Decision::Eio),
        "{end:?}"
    );
    eventually("the note is gone", || !note.exists()).await;
    assert!(!svc.calls().iter().any(|c| c.starts_with("recall_failed")));
    d.shutdown().await;
}

/// A job in the service (a version restore, an annex placement) kept the
/// recall going after the file was deleted, and closed it `available` when it
/// had its bytes (#96, second review). The rows that reach the edge are
/// `downloading` and then `available`, for a file with no placeholder. The
/// edge did nothing with either, so a reader of a `wait` share that still
/// held the deleted file open waited for good. It ends with an error, as a
/// deleted file's reader does when its recall is `superseded`.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_reader_of_a_deleted_file_is_released_when_its_recall_ends_available() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let data = pattern(2 << 20);
    let (d, svc, p, socket) =
        archived_world(dir.path(), 917, 9170, "held.segy", &data, true, "").await;
    let mut fsal = Fsal::connect(&socket).await;
    fsal.send(&Message::ReadIntent(intent(&p, 917, 9170, Policy::Wait)))
        .await;
    let Message::RecallState(rs) = fsal
        .expect(
            |m| matches!(m, Message::RecallState(s) if s.obj.file_id == 917 && s.recall_id != 0),
        )
        .await
    else {
        panic!()
    };
    assert!(!rs.state.is_terminal(), "the reader waits: {rs:?}");
    let note = p.with_file_name("held.segy.RECALLING.txt");
    eventually("note", || note.exists()).await;
    let mut gone = events::file(
        917,
        100,
        "held.segy",
        data.len() as u64,
        9170,
        Some(omnifile_integrity::crc64_nvme(&data)),
        true,
        None,
        7,
    );
    gone["type"] = serde_json::Value::from("file.tombstone");
    svc.push_event(gone);
    eventually("the placeholder is removed", || !p.exists()).await;
    // The recall goes on for the job: its bytes are back, then the job is done.
    svc.push_event(events::recall(
        rs.recall_id,
        917,
        9170,
        "downloading",
        None,
        8,
    ));
    svc.push_event(events::recall(
        rs.recall_id,
        917,
        9170,
        "available",
        None,
        9,
    ));
    let Message::RecallState(end) = fsal
        .expect(|m| {
            matches!(m, Message::RecallState(s)
                if s.recall_id == rs.recall_id && s.state.is_terminal())
        })
        .await
    else {
        panic!()
    };
    assert_eq!(
        (end.state, end.answer),
        (RecallPhase::Failed, Decision::Eio),
        "{end:?}"
    );
    eventually("the note is gone", || !note.exists()).await;
    let calls = svc.calls();
    assert!(
        !calls
            .iter()
            .any(|c| c.starts_with("recall_failed") || c.starts_with("recall_filled")),
        "{calls:?}"
    );
    d.shutdown().await;
}

/// The same, for a file that was replaced in place while a job kept its
/// recall: the `available` row is for a version the file no longer holds.
/// The entry ends as a cancelled one (nothing was filled into this file),
/// the note goes, and the reader's next read is of the new version.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_available_recall_of_a_version_the_file_left_is_tidied_away() {
    let Some(base) = base() else { return };
    let dir = tempfile::tempdir_in(&base).unwrap();
    let data = pattern(2 << 20);
    let (d, svc, p, socket) =
        archived_world(dir.path(), 918, 9180, "kept.segy", &data, true, "").await;
    let mut fsal = Fsal::connect(&socket).await;
    fsal.send(&Message::ReadIntent(intent(&p, 918, 9180, Policy::Wait)))
        .await;
    let Message::RecallState(rs) = fsal
        .expect(
            |m| matches!(m, Message::RecallState(s) if s.obj.file_id == 918 && s.recall_id != 0),
        )
        .await
    else {
        panic!()
    };
    let note = p.with_file_name("kept.segy.RECALLING.txt");
    eventually("note", || note.exists()).await;
    let new = b"a newer version".to_vec();
    svc.put_blob(918, 9181, new.clone());
    svc.push_event(events::file(
        918,
        100,
        "kept.segy",
        new.len() as u64,
        9181,
        Some(omnifile_integrity::crc64_nvme(&new)),
        false,
        None,
        7,
    ));
    eventually("the newer version", || {
        marker(&p).is_some_and(|m| m.version_id == 9181)
    })
    .await;
    svc.push_event(events::recall(
        rs.recall_id,
        918,
        9180,
        "downloading",
        None,
        8,
    ));
    svc.push_event(events::recall(
        rs.recall_id,
        918,
        9180,
        "available",
        None,
        9,
    ));
    fsal.expect(|m| {
        matches!(m, Message::RecallState(s)
            if s.obj.file_id == 918 && s.recall_id == rs.recall_id
                && s.state == RecallPhase::Cancelled)
    })
    .await;
    eventually("the note is gone", || !note.exists()).await;
    let calls = svc.calls();
    assert!(
        !calls
            .iter()
            .any(|c| c.starts_with("recall_failed") || c.starts_with("recall_filled")),
        "{calls:?}"
    );
    svc.set_archived(false);
    fsal.send(&Message::ReadIntent(intent(&p, 918, 9181, Policy::Wait)))
        .await;
    fsal.expect(|m| {
        matches!(m, Message::RecallState(s)
            if s.obj.file_id == 918 && s.version_id == 9181 && s.state == RecallPhase::Done)
    })
    .await;
    assert_eq!(std::fs::read(&p).unwrap(), new, "the new version's bytes");
    d.shutdown().await;
}
