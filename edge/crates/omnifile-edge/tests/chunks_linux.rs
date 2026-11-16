//! M5: chunk-range reads end to end on the dev VM (real SEQPACKET socket,
//! real xfs, the daemon's cache; fake service and synthetic remote
//! objects). Port of line B's `chunks_linux.rs` onto line A. Run as root on
//! an unguarded xfs:
//!
//! ```text
//! sudo -E cargo test -p omnifile-edge --test chunks_linux -- --nocapture
//! ```
//!
//! `g1_random_reads_of_a_100_gib_object` prints the G1 numbers (1,000
//! random 64 KiB reads of a 100 GiB remote object through the daemon, the
//! cache evicting under a small budget).
#![cfg(target_os = "linux")]

use std::os::fd::AsFd;
use std::os::unix::fs::{FileExt, MetadataExt};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use omnifile_edge::checks::startup_checks;
use omnifile_edge::config::Config;
use omnifile_edge::daemon::Daemon;
use omnifile_edge::fakes::{FakeService, events, synthetic_byte};
use omnifile_fsal_proto::PacketIo;
use omnifile_fsal_proto::socket::SeqPacket;
use omnifile_fsal_proto::wire::{self, *};
use omnifile_marker::{ChunkMap, Flags, Marker, State, xattr};
use omnifile_stub::{SourceKind, sys};

const MIB: u64 = 1 << 20;
const GIB: u64 = 1 << 30;

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
            "skipping: {} is guarded; set OMNIFILE_TEST_DIR",
            b.display()
        );
        return None;
    }
    Some(b)
}

fn config(dir: &Path, extra: &str) -> Config {
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
        [feed]
        wait_s = 1
        retry_s = 1
        reconcile_s = 0
        reconcile_quiet_s = 0
        [fill]
        chunk_mib = 1
        widen_every_mib = 2
        [commit]
        detect = false
        [namespace]
        enabled = false
        [log]
        format = "text"
        filter = "info"
        {extra}
        "#,
        d = dir.display(),
        exe = exe.display(),
    );
    std::fs::create_dir_all(dir.join("data")).unwrap();
    Config::from_toml(&toml, Path::new("test.toml")).unwrap()
}

/// A scripted FSAL that acks every INVALIDATE.
struct Fsal {
    io: Arc<SeqPacket>,
    rx: tokio::sync::mpsc::UnboundedReceiver<Frame>,
    next: u64,
    ack: HelloAck,
}

impl Fsal {
    async fn connect(path: &Path, minor: u16, features: u32) -> Fsal {
        let io = Arc::new(SeqPacket::connect(path).await.unwrap());
        let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
        let rio = io.clone();
        tokio::spawn(async move {
            while let Ok(Some(rec)) = rio.recv().await {
                let f = wire::decode(&rec).expect("malformed");
                if let Message::Invalidate(inv) = &f.msg
                    && inv.flags & INVALIDATE_ACK_REQUIRED != 0
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
            next: 1,
            ack: HelloAck {
                proto_major: 0,
                proto_minor: 0,
                features: 0,
                instance: 0,
                max_inflight: 0,
                park_ms_max: 0,
                answer_ttl_ms: 0,
            },
        };
        f.send(&Message::Hello(Hello {
            proto_major: 0,
            proto_minor: minor,
            features,
            pid: std::process::id(),
            instance: 5,
            ganesha_version: b"V15.6".to_vec(),
            fsal_build: b"test".to_vec(),
        }))
        .await;
        let Message::HelloAck(a) = f.expect(|m| matches!(m, Message::HelloAck(_))).await else {
            unreachable!()
        };
        f.ack = a;
        f.expect(|m| matches!(m, Message::ResyncEnd)).await;
        f
    }

    /// A v0.2 FSAL: PARK and CHUNK_MAP.
    async fn v02(path: &Path) -> Fsal {
        Fsal::connect(path, 2, feature::PARK | feature::CHUNK_MAP).await
    }

    async fn send(&mut self, m: &Message) -> u64 {
        let id = self.next;
        self.next += 1;
        self.io.send(&encode(m, id)).await.unwrap();
        id
    }

    async fn expect(&mut self, pred: impl Fn(&Message) -> bool) -> Message {
        let deadline = Instant::now() + Duration::from_secs(30);
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            match tokio::time::timeout(left, self.rx.recv()).await {
                Ok(Some(f)) if pred(&f.msg) => return f.msg,
                Ok(Some(_)) => continue,
                _ => panic!("timed out waiting for a message"),
            }
        }
    }

    /// Ask for `[off, off+len)` and return the decision.
    async fn read(
        &mut self,
        p: &Path,
        file_id: u64,
        version: u64,
        off: u64,
        len: u64,
    ) -> ReadDecision {
        let mut ri = intent(p, file_id, version);
        ri.offset = off;
        ri.length = len;
        let id = self.send(&Message::ReadIntent(ri)).await;
        let deadline = Instant::now() + Duration::from_secs(30);
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            match tokio::time::timeout(left, self.rx.recv()).await {
                Ok(Some(f)) if f.header.req_id == id => {
                    if let Message::ReadDecision(d) = f.msg {
                        return d;
                    }
                }
                Ok(Some(_)) => continue,
                _ => panic!("no READ_DECISION"),
            }
        }
    }
}

async fn eventually(what: &str, mut f: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(30);
    while !f() {
        assert!(Instant::now() < deadline, "timed out: {what}");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

fn state(p: &Path) -> Option<(Marker, Option<ChunkMap>)> {
    let fd = sys::open_ro(p).ok()?;
    let m = xattr::read_required(fd.as_fd()).ok()?;
    let map = if m.flags.contains(Flags::CHUNK_MAP) {
        xattr::read_map(fd.as_fd(), &m).ok().flatten()
    } else {
        None
    };
    Some((m, map))
}

fn covers(p: &Path, off: u64, len: u64) -> bool {
    state(p).is_some_and(|(m, map)| m.covers_with_map(map.as_ref(), off, len))
}

fn intent(p: &Path, file_id: u64, version_id: u64) -> ReadIntent {
    let (m, _) = state(p).unwrap();
    let fd = sys::open_ro(p).unwrap();
    let k = sys::inode_key(fd.as_fd()).unwrap();
    ReadIntent {
        obj: ObjRef {
            fsid: k.fsid,
            ino: k.ino,
            ino_gen: k.ino_gen,
            export_id: 1,
            file_id,
        },
        version_id,
        offset: 0,
        length: 65536,
        size: m.size,
        resident_bytes: m.resident_bytes,
        op: ReadOp::Read,
        policy: Policy::FailFast,
        marker_flags: m.flags.bits(),
        quick_ms: 0,
        parked: true,
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

fn read_at(p: &Path, off: u64, len: u64) -> Vec<u8> {
    let f = std::fs::File::open(p).unwrap();
    let mut b = vec![0u8; len as usize];
    f.read_exact_at(&mut b, off).unwrap();
    b
}

fn want(seed: u64, off: u64, len: u64) -> Vec<u8> {
    (off..off + len).map(|i| synthetic_byte(seed, i)).collect()
}

/// A daemon with a share and one remote (`ONLINE`, nothing local) object.
struct Rig {
    _dir: tempfile::TempDir,
    extra: String,
    d: Option<Daemon<FakeService, FakeService>>,
    svc: Arc<FakeService>,
    socket: PathBuf,
    data_root: PathBuf,
}

async fn rig(extra: &str) -> Option<Rig> {
    let base = base()?;
    let dir = tempfile::tempdir_in(&base).unwrap();
    let cfg = config(dir.path(), extra);
    omnifile_edge::init_logging("text", "info");
    let vol = startup_checks(&cfg).unwrap();
    let svc = Arc::new(FakeService::new());
    let (socket, data_root) = (cfg.socket_path.clone(), cfg.data_root.clone());
    let d = Daemon::start(cfg, vol, svc.clone(), svc.clone())
        .await
        .unwrap();
    svc.push_event(events::share(1, 100, "proj", "fail", 1));
    svc.push_event(events::dir(100, None, "", 1));
    Some(Rig {
        _dir: dir,
        extra: extra.to_owned(),
        d: Some(d),
        svc,
        socket,
        data_root,
    })
}

impl Rig {
    /// Stop the daemon and start a new one on the same volume and replica.
    async fn restart(&mut self) {
        self.stop().await;
        let cfg = config(self._dir.path(), &self.extra);
        let vol = startup_checks(&cfg).unwrap();
        self.d = Some(
            Daemon::start(cfg, vol, self.svc.clone(), self.svc.clone())
                .await
                .unwrap(),
        );
    }

    /// A remote object of `size` synthetic bytes; with `crc`, the catalog
    /// has its whole-file CRC.
    async fn object(&self, file_id: u64, name: &str, size: u64, crc: bool, ev: u64) -> PathBuf {
        let version = file_id * 10;
        let seed = file_id;
        self.svc.put_synthetic(file_id, version, size, seed);
        let crc = crc.then(|| {
            let mut c = omnifile_integrity::Crc64::new();
            let mut off = 0;
            while off < size {
                let n = (8 * MIB).min(size - off);
                c.update(&want(seed, off, n));
                off += n;
            }
            c.finalize()
        });
        self.svc.push_event(events::file(
            file_id, 100, name, size, version, crc, false, None, ev,
        ));
        let p = self.data_root.join("proj").join(name);
        eventually("placeholder", || state(&p).is_some()).await;
        p
    }

    async fn stop(&mut self) {
        if let Some(d) = self.d.take() {
            d.shutdown().await;
        }
    }
}

/// The chunk path: a v0.2 FSAL's ONLINE reads PARK, only the chunks they
/// touch are fetched into the map (the golden vector's layout), FETCHING
/// follows each widening, DONE when nothing is in flight, and nothing goes
/// to the service's recall flow or is reported filled.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn online_reads_fetch_only_their_chunks() {
    let Some(mut r) = rig("[cache]\nreadahead_max_chunks = 0\nheader_mib = 0").await else {
        return;
    };
    let p = r.object(501, "seis.segy", 10 * MIB, true, 2).await;
    let mut fsal = Fsal::v02(&r.socket).await;
    assert_eq!(fsal.ack.proto_minor, 2);
    assert_ne!(fsal.ack.features & feature::CHUNK_MAP, 0);
    let d = fsal.read(&p, 501, 5010, 9 * MIB, 64 * 1024).await;
    assert_eq!(d.decision, Decision::Park, "an ONLINE range parks");
    assert_eq!(d.reason, reason::FETCH_IN_PROGRESS);
    fsal.expect(|m| matches!(m, Message::RecallState(s) if s.recall_id == 0 && s.state == RecallPhase::Fetching)).await;
    fsal.expect(|m| matches!(m, Message::RecallState(s) if s.recall_id == 0 && s.state == RecallPhase::Done)).await;
    let (m, map) = state(&p).unwrap();
    assert_eq!((m.state, m.resident_bytes), (State::Partial, 0));
    assert!(m.flags.contains(Flags::CHUNK_MAP | Flags::ONLINE));
    assert_eq!(m.chunk_shift, 22);
    assert_eq!(map.unwrap().encode(), &[0b100], "chunk 2 only");
    assert_eq!(
        read_at(&p, 9 * MIB, 64 * 1024),
        want(501, 9 * MIB, 64 * 1024)
    );
    // Chunk 0 too: the map is the contract's golden vector's.
    fsal.read(&p, 501, 5010, 0, 4096).await;
    eventually("chunk 0", || covers(&p, 0, 4096)).await;
    assert_eq!(state(&p).unwrap().1.unwrap().encode(), &[0x05]);
    assert!(!covers(&p, 4 * MIB, 1), "chunk 1 was never asked for");
    let ranges = r.svc.ranges();
    assert_eq!(
        ranges.iter().map(|(_, _, l)| l).sum::<u64>(),
        2 * MIB + 4 * MIB,
        "the short last chunk (2 MiB) and chunk 0 (4 MiB), nothing more: {ranges:?}"
    );
    let calls = r.svc.calls();
    assert!(
        !calls.iter().any(|c| c.starts_with("recall_start")),
        "{calls:?}"
    );
    assert!(
        !calls.iter().any(|c| c.starts_with("file_filled")),
        "{calls:?}"
    );
    assert!(calls.iter().any(|c| c == "read_url 501 5010"), "{calls:?}");
    r.stop().await;
}

/// Reading every byte completes the file: the whole-file CRC matches, the
/// marker becomes RESIDENT (map gone), and the fill is reported like an
/// online fetch. Without a catalog CRC the file stays PARTIAL and is still
/// served by its map.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn the_last_chunk_completes_and_reports() {
    let Some(mut r) = rig("[cache]\nreadahead_max_chunks = 0\nheader_mib = 0").await else {
        return;
    };
    let size = 9 * MIB + 7;
    let p = r.object(502, "whole.segy", size, true, 2).await;
    let q = r.object(503, "nocrc.segy", size, false, 3).await;
    let mut fsal = Fsal::v02(&r.socket).await;
    for (path, id) in [(&p, 502), (&q, 503)] {
        for c in 0..3u64 {
            fsal.read(path, id, id * 10, c * 4 * MIB, 4096).await;
            eventually("chunk", || covers(path, c * 4 * MIB, 4096)).await;
        }
    }
    eventually("resident", || {
        state(&p).is_some_and(|(m, _)| m.state == State::Resident)
    })
    .await;
    let (m, map) = state(&p).unwrap();
    assert!(!m.flags.contains(Flags::CHUNK_MAP) && map.is_none());
    assert_eq!(std::fs::read(&p).unwrap(), want(502, 0, size));
    eventually("reported filled", || {
        r.svc
            .calls()
            .iter()
            .any(|c| c == &format!("file_filled 502 5020 {size}"))
    })
    .await;
    let (m, _) = state(&q).unwrap();
    assert_eq!(
        m.state,
        State::Partial,
        "no catalog CRC: never RESIDENT by chunks"
    );
    assert!(covers(&q, 0, size), "but every chunk is served");
    assert!(
        !r.svc
            .calls()
            .iter()
            .any(|c| c.starts_with("file_filled 503"))
    );
    r.stop().await;
}

/// A v0.1 FSAL never gets chunk maps: its ONLINE reads take line A's
/// recall and whole-file fill, unchanged.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_v01_fsal_keeps_whole_file_fills() {
    let Some(mut r) = rig("").await else { return };
    let p = r.object(504, "old.segy", 6 * MIB, true, 2).await;
    let mut fsal = Fsal::connect(&r.socket, 1, feature::PARK | feature::CHUNK_MAP).await;
    assert_eq!(fsal.ack.features & feature::CHUNK_MAP, 0);
    let d = fsal.read(&p, 504, 5040, 0, 4096).await;
    assert_eq!(d.decision, Decision::Park);
    fsal.expect(|m| matches!(m, Message::RecallState(s) if s.obj.file_id == 504 && s.state == RecallPhase::Done)).await;
    let (m, _) = state(&p).unwrap();
    assert_eq!(m.state, State::Resident, "the whole file, as in M1");
    assert!(
        r.svc
            .calls()
            .iter()
            .any(|c| c.starts_with("recall_start 504 5040"))
    );
    r.stop().await;
}

/// S3 through the pointer the edge signs (`s3_roles_anywhere`): ranges are
/// read as S3 (`S3Signed`). A `412` (the source changed under an ETag or
/// version pin) fails the read closed, narrows the file to nothing cached
/// (acknowledged) and is reported as `remote.source_changed`.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn s3_reads_are_signed_and_a_changed_source_fails_closed() {
    let Some(mut r) = rig("[cache]\nreadahead_max_chunks = 0\nheader_mib = 0").await else {
        return;
    };
    r.svc.set_read_s3(true);
    let p = r.object(505, "s3.segy", 16 * MIB, true, 2).await;
    let mut fsal = Fsal::v02(&r.socket).await;
    fsal.read(&p, 505, 5050, 5 * MIB, 4096).await;
    eventually("chunk 1", || covers(&p, 5 * MIB, 4096)).await;
    assert!(
        r.svc
            .range_sources()
            .iter()
            .all(|k| *k == SourceKind::S3Signed)
    );
    assert_eq!(read_at(&p, 5 * MIB, 4096), want(505, 5 * MIB, 4096));
    // The object changes in the bucket.
    r.svc.set_source_changed(505, true);
    fsal.read(&p, 505, 5050, 12 * MIB, 4096).await;
    fsal.expect(|m| matches!(m, Message::RecallState(s) if s.obj.file_id == 505 && s.state == RecallPhase::Failed && s.answer == Decision::Eio)).await;
    eventually("narrowed", || !covers(&p, 5 * MIB, 4096)).await;
    let (m, map) = state(&p).unwrap();
    assert_eq!((m.state, m.resident_bytes), (State::Partial, 0));
    assert!(map.is_none());
    let f = std::fs::File::open(&p).unwrap();
    assert!(
        sys::first_data_at(f.as_fd(), 0).unwrap().is_none(),
        "every cached byte of the old object was punched"
    );
    // While it lasts, reads are refused (EIO), never served.
    let d = fsal.read(&p, 505, 5050, 5 * MIB, 4096).await;
    assert_eq!(d.decision, Decision::Eio);
    // The audit record reaches the service.
    eventually("audited", || {
        r.svc
            .audits()
            .iter()
            .any(|a| a["action"] == "remote.source_changed" && a["file_id"] == "505")
    })
    .await;
    r.stop().await;
}

/// Review on #23 (P1): with the operator's presigned fallback on an
/// unversioned bucket, the URL carries the catalog's ETag as a signed
/// `If-Match`. An overwrite between two chunk reads makes the second fail
/// closed (`412`): the file is narrowed, nothing of the new object is mixed
/// with the cached chunks, and `remote.source_changed` is audited.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn presigned_reads_are_pinned_by_a_signed_if_match() {
    let Some(mut r) = rig("[cache]\nreadahead_max_chunks = 0\nheader_mib = 0").await else {
        return;
    };
    r.svc.set_read_presigned(Some("\"fake\""));
    let p = r.object(508, "ps.segy", 16 * MIB, true, 2).await;
    let mut fsal = Fsal::v02(&r.socket).await;
    fsal.read(&p, 508, 5080, 5 * MIB, 4096).await;
    eventually("chunk 1", || covers(&p, 5 * MIB, 4096)).await;
    assert!(
        r.svc
            .range_sources()
            .iter()
            .all(|k| *k == SourceKind::S3Presigned)
    );
    assert_eq!(read_at(&p, 5 * MIB, 4096), want(508, 5 * MIB, 4096));
    // The object is overwritten in the bucket between two chunk reads.
    r.svc.set_source_changed(508, true);
    fsal.read(&p, 508, 5080, 12 * MIB, 4096).await;
    fsal.expect(|m| matches!(m, Message::RecallState(s) if s.obj.file_id == 508 && s.state == RecallPhase::Failed && s.answer == Decision::Eio)).await;
    eventually("narrowed", || !covers(&p, 5 * MIB, 4096)).await;
    let (m, map) = state(&p).unwrap();
    assert_eq!((m.state, m.resident_bytes), (State::Partial, 0));
    assert!(map.is_none(), "no chunk of either object is served");
    let f = std::fs::File::open(&p).unwrap();
    assert!(sys::first_data_at(f.as_fd(), 0).unwrap().is_none());
    eventually("audited", || {
        r.svc
            .audits()
            .iter()
            .any(|a| a["action"] == "remote.source_changed" && a["file_id"] == "508")
    })
    .await;
    r.stop().await;
}

/// Review on #23 (P1): a presigned URL that pins neither a version nor an
/// ETag is never read chunk by chunk. The read goes to the whole-file fill,
/// which exposes nothing before the whole-file CRC matched.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn unpinned_presigned_urls_get_no_chunk_reads() {
    let Some(mut r) = rig("[cache]\nreadahead_max_chunks = 0\nheader_mib = 0").await else {
        return;
    };
    r.svc.set_read_presigned(None);
    let size = 6 * MIB;
    let p = r.object(509, "unpinned.segy", size, true, 2).await;
    let mut fsal = Fsal::v02(&r.socket).await;
    fsal.read(&p, 509, 5090, 5 * MIB, 4096).await;
    eventually("resident", || {
        state(&p).is_some_and(|(m, _)| m.state == State::Resident)
    })
    .await;
    assert_eq!(std::fs::read(&p).unwrap(), want(509, 0, size));
    assert!(
        !r.svc.range_sources().contains(&SourceKind::S3Presigned),
        "the unpinned URL was never read by range: {:?}",
        r.svc.range_sources()
    );
    assert!(
        r.svc
            .calls()
            .iter()
            .any(|c| c.starts_with("recall_start 509 5090")),
        "the whole-file path"
    );
    r.stop().await;
}

/// Review on #23 (P2): read-ahead runs inside the fetch worker, under its
/// fill fence. Before, it was a detached task that began after the worker
/// had ended its fill, so the fence refused it and nothing was read ahead
/// (on a single-thread runtime, every time).
#[tokio::test(flavor = "current_thread")]
async fn read_ahead_fills_the_next_chunks_under_the_fence() {
    let Some(mut r) = rig("[cache]\nreadahead_max_chunks = 4\nheader_mib = 0").await else {
        return;
    };
    let p = r.object(510, "seq.segy", 64 * MIB, false, 2).await;
    let mut fsal = Fsal::v02(&r.socket).await;
    // A sequential reader: chunks 0, 1, 2, each fetched on demand.
    for c in 0..3u64 {
        fsal.read(&p, 510, 5100, c * 4 * MIB, 4 * MIB).await;
        eventually("demanded chunk", || covers(&p, c * 4 * MIB, 4 * MIB)).await;
    }
    // The chunks after them arrive without being asked for.
    eventually("read ahead", || covers(&p, 3 * 4 * MIB, 4 * MIB)).await;
    let asked: Vec<u64> = r
        .svc
        .ranges()
        .iter()
        .map(|(_, o, _)| o / (4 * MIB))
        .collect();
    assert!(
        asked.iter().any(|c| *c >= 3),
        "chunks past the reader were fetched: {asked:?}"
    );
    assert_eq!(read_at(&p, 3 * 4 * MIB, 4096), want(510, 3 * 4 * MIB, 4096));
    r.stop().await;
}

/// Review on #23 (P2): after a restart the cache indexes every placeholder
/// it holds bytes of before it admits new ones, so filling, restarting and
/// filling again never puts more than the budget on disk.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_restart_never_admits_a_second_budget() {
    let Some(mut r) = rig(
        "[cache]\nreadahead_max_chunks = 0\nheader_mib = 0\ncapacity_mib = 64\nlow_watermark_pct = 75",
    )
    .await
    else {
        return;
    };
    let a = r.object(511, "a.segy", 256 * MIB, false, 2).await;
    let b = r.object(512, "b.segy", 256 * MIB, false, 3).await;
    let on_disk = || {
        [&a, &b]
            .iter()
            .map(|p| std::fs::metadata(p).unwrap().blocks() * 512)
            .sum::<u64>()
    };
    let budget = 64 * MIB;
    let mut fsal = Fsal::v02(&r.socket).await;
    for c in 0..14u64 {
        fsal.read(&a, 511, 5110, c * 4 * MIB, 4096).await;
        eventually("chunk of a", || covers(&a, c * 4 * MIB, 4096)).await;
        assert!(on_disk() <= budget, "over budget: {}", on_disk());
    }
    let before = on_disk();
    assert!(before >= 40 * MIB, "a holds most of the budget: {before}");
    r.restart().await;
    let mut fsal = Fsal::v02(&r.socket).await;
    for c in 0..14u64 {
        fsal.read(&b, 512, 5120, c * 4 * MIB, 4096).await;
        eventually("chunk of b", || covers(&b, c * 4 * MIB, 4096)).await;
        let now = on_disk();
        assert!(
            now <= budget,
            "a second budget was admitted: {now} bytes on disk"
        );
    }
    r.stop().await;
}

/// Fail closed under chunks: the daemon stops (shutdown, then a new one)
/// while chunk fills run. No chunk whose bit is set ever holds wrong bytes
/// or zeros, and a chunk whose bit is clear is never served.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn stopping_mid_fetch_never_exposes_zeros() {
    let Some(mut r) = rig("[cache]\nreadahead_max_chunks = 0\nheader_mib = 0").await else {
        return;
    };
    let size = 256 * MIB;
    let p = r.object(506, "big.segy", size, false, 2).await;
    r.svc.set_range_delay_ms(30);
    let mut fsal = Fsal::v02(&r.socket).await;
    for c in (0..64u64).step_by(3) {
        fsal.read(&p, 506, 5060, c * 4 * MIB + 17, 100).await;
    }
    // Stop while those fetches run.
    tokio::time::sleep(Duration::from_millis(400)).await;
    r.stop().await;
    let (m, map) = state(&p).unwrap();
    assert_eq!(m.state, State::Partial);
    let map = map.expect("some chunks landed before the stop");
    let mut set = 0;
    for c in 0..64u64 {
        if map.is_set(c) {
            set += 1;
            assert_eq!(
                read_at(&p, c * 4 * MIB, 4 * MIB),
                want(506, c * 4 * MIB, 4 * MIB),
                "chunk {c}"
            );
        }
    }
    assert!(set > 0 && set < 22, "the stop came mid-way ({set} of 22)");
    eprintln!("stopped with {set} of 22 asked chunks local; every set chunk verified");
}

/// G1 (port of B's gate): 1,000 random 64 KiB reads of a 100 GiB remote
/// object through the daemon's chunk path, every one checked against the
/// source, with the cache evicting down a 1 GiB budget meanwhile.
#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn g1_random_reads_of_a_100_gib_object() {
    let Some(mut r) = rig("[cache]\nreadahead_max_chunks = 0\nheader_mib = 0\ncapacity_gib = 1\nlow_watermark_pct = 75").await else {
        return;
    };
    let size = 100 * GIB;
    let p = r.object(507, "g1.segy", size, false, 2).await;
    assert_eq!(std::fs::metadata(&p).unwrap().blocks(), 0, "sparse");
    let mut fsal = Fsal::v02(&r.socket).await;
    assert_ne!(fsal.ack.features & feature::CHUNK_MAP, 0);
    let mut rng = fastrand::Rng::with_seed(1042);
    let (mut lat, mut wrong, mut intents, mut touched) = (Vec::new(), 0u32, 0u32, 0u64);
    for _ in 0..1000 {
        let off = rng.u64(0..size - 65536) & !4095;
        let t0 = Instant::now();
        loop {
            if covers(&p, off, 65536) {
                // A reader holds the bytes it saw: read now. An eviction may
                // punch afterwards, never before the map is narrowed.
                let b = read_at(&p, off, 65536);
                if covers(&p, off, 65536) {
                    if b != want(507, off, 65536) {
                        wrong += 1;
                    }
                    break;
                }
                continue;
            }
            intents += 1;
            touched += ((off + 65535) >> 22) - (off >> 22) + 1;
            let d = fsal.read(&p, 507, 5070, off, 65536).await;
            assert_ne!(d.decision, Decision::Eio, "a read failed at {off}");
            let deadline = Instant::now() + Duration::from_secs(10);
            while !covers(&p, off, 65536) && Instant::now() < deadline {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        }
        lat.push(t0.elapsed());
    }
    lat.sort();
    let p50 = lat[lat.len() / 2];
    let p99 = lat[lat.len() * 99 / 100];
    let max = *lat.last().unwrap();
    let fetched: u64 = r.svc.ranges().iter().map(|(_, _, l)| l).sum();
    eprintln!(
        "G1 (daemon): 1000 random 64 KiB reads of 100 GiB: wrong {wrong}, READ_INTENTs {intents} \
         ({:.2}/read), p50 {:?}, p99 {:?}, max {:?}, fetched {} MiB",
        intents as f64 / 1000.0,
        p50,
        p99,
        max,
        fetched >> 20
    );
    assert_eq!(wrong, 0, "every byte matched the source");
    assert!(max < Duration::from_secs(5), "no read over 5 s");
    assert!(
        fetched <= touched * 4 * MIB,
        "only the chunks the reads touched: {fetched} > {touched} chunks"
    );
    let used: u64 = std::fs::metadata(&p).unwrap().blocks() * 512;
    assert!(
        used <= GIB + 64 * MIB,
        "the budget held: {used} bytes on disk"
    );
    r.stop().await;
}
