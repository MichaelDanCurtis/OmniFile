//! Integration tests on a real xfs volume (Linux, as root: `trusted.*`
//! xattrs need CAP_SYS_ADMIN). Run inside the dev VM:
//!
//! ```text
//! limactl shell omnifile-edge -- sudo -E cargo test -p omnifile-stub
//! ```
//!
//! `OMNIFILE_TEST_DIR` (default `/srv/omnifile/data/test`) must be on xfs.
//! Without root or xfs the tests print a note and pass vacuously.
#![cfg(target_os = "linux")]

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::fd::AsFd;
use std::os::unix::fs::{FileExt, MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};

use omnifile_marker::xattr::{self, ReadOutcome};
use omnifile_marker::{Flags, Marker, State};
use omnifile_replica::{Kind, Replica};
use omnifile_stub::fill::{FillError, FillOptions, FillRequest, fill};
use omnifile_stub::handshake::{StubRequest, stub};
use omnifile_stub::materialize::{Materializer, run_redo};
use omnifile_stub::placeholder::{Created, PlaceholderSpec, create_placeholder};
use omnifile_stub::{
    FetchTarget, FileLocks, FsalNotifier, InodeKey, Layout, MarkerSeq, NoSmbGuard, RangeSource,
    SkipReason, StubOutcome, sys,
};
use serde_json::json;

struct Env {
    _dir: tempfile::TempDir,
    layout: Layout,
}

fn env() -> Option<Env> {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("skipping: needs root for trusted.* xattrs");
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
    let layout = Layout::new(dir.path());
    Some(Env { _dir: dir, layout })
}

type Hook = Box<dyn FnMut() + Send>;

#[derive(Default)]
struct FakeFsal {
    nack: AtomicBool,
    /// Refuse the next this-many acknowledged invalidations.
    nack_next: AtomicUsize,
    /// Run once, inside the next acknowledged invalidation (after the
    /// narrowed marker is written, before the ack is returned).
    on_ack: Mutex<Option<Hook>>,
    /// Refuse the fill fence (an FSAL did not confirm).
    refuse_fence: AtomicBool,
    /// The connection epoch; tests bump it to simulate an FSAL (re)connecting.
    epoch: AtomicU64,
    cancel: AtomicBool,
    /// Run (every time) when a narrowing asks to remove the ignore mark.
    on_before_narrow: Mutex<Option<Hook>>,
    /// Delay every acknowledged invalidation by this long.
    ack_delay_ms: AtomicU64,
    fences: AtomicUsize,
    replace_fences: AtomicUsize,
    replace_ends: AtomicUsize,
    invalidations: AtomicUsize,
    acked_invalidations: AtomicUsize,
    progress: Mutex<Vec<u64>>,
}

impl FsalNotifier for FakeFsal {
    async fn invalidate(&self, _key: InodeKey, _what: u32, ack: bool) -> bool {
        self.invalidations.fetch_add(1, Ordering::SeqCst);
        if ack {
            let d = self.ack_delay_ms.load(Ordering::SeqCst);
            if d > 0 {
                tokio::time::sleep(std::time::Duration::from_millis(d)).await;
            }
            self.acked_invalidations.fetch_add(1, Ordering::SeqCst);
            let hook = self.on_ack.lock().unwrap().take();
            if let Some(mut h) = hook {
                h();
            }
            let n = self.nack_next.load(Ordering::SeqCst);
            if n > 0 {
                self.nack_next.store(n - 1, Ordering::SeqCst);
                return false;
            }
        }
        !(ack && self.nack.load(Ordering::SeqCst))
    }
    fn connection_epoch(&self) -> u64 {
        self.epoch.load(Ordering::SeqCst)
    }
    fn fill_progress(&self, _key: InodeKey, _version_id: u64, resident_bytes: u64) {
        self.progress.lock().unwrap().push(resident_bytes);
    }
    async fn begin_fill(&self, _key: InodeKey, _version_id: u64) -> Option<u64> {
        self.fences.fetch_add(1, Ordering::SeqCst);
        if self.refuse_fence.load(Ordering::SeqCst) {
            None
        } else {
            Some(self.epoch.load(Ordering::SeqCst))
        }
    }
    fn fence_holds(&self, epoch: u64) -> bool {
        self.epoch.load(Ordering::SeqCst) == epoch
    }
    fn fill_cancelled(&self, _key: InodeKey, _version_id: u64) -> bool {
        self.cancel.load(Ordering::SeqCst)
    }
    async fn begin_replace(&self, _key: InodeKey, _old_version: u64) -> Option<u64> {
        self.replace_fences.fetch_add(1, Ordering::SeqCst);
        if self.refuse_fence.load(Ordering::SeqCst) {
            None
        } else {
            Some(self.epoch.load(Ordering::SeqCst))
        }
    }
    fn end_replace(&self, _key: InodeKey, _old_version: u64) {
        self.replace_ends.fetch_add(1, Ordering::SeqCst);
    }
    fn before_narrow(&self, _key: InodeKey) -> io::Result<()> {
        if let Some(h) = self.on_before_narrow.lock().unwrap().as_mut() {
            h();
        }
        Ok(())
    }
}

struct MemSource {
    data: Vec<u8>,
    corrupt_at: Option<usize>,
}

impl RangeSource for MemSource {
    async fn get_range(&self, _t: &FetchTarget, offset: u64, len: u64) -> io::Result<Vec<u8>> {
        let mut v = self.data[offset as usize..(offset + len) as usize].to_vec();
        if let Some(c) = self.corrupt_at
            && (offset as usize..(offset + len) as usize).contains(&c)
        {
            v[c - offset as usize] ^= 0xFF;
        }
        Ok(v)
    }
}

fn pattern(n: usize) -> Vec<u8> {
    (0..n).map(|i| (i * 7 + i / 4096) as u8).collect()
}

fn marker_at(p: &Path) -> Marker {
    let f = File::open(p).unwrap();
    xattr::read_required(f.as_fd()).unwrap()
}

/// A resident, uploaded file: real bytes plus a RESIDENT marker.
fn resident_file(
    layout: &Layout,
    rel: &str,
    data: &[u8],
    file_id: u64,
    version_id: u64,
) -> PathBuf {
    let p = layout.path(Path::new(rel));
    std::fs::write(&p, data).unwrap();
    let f = File::open(&p).unwrap();
    let mut m = Marker::resident(file_id, version_id, data.len() as u64, 1);
    m.flags = Flags::ONLINE;
    xattr::write(f.as_fd(), &m).unwrap();
    p
}

fn data_extent_end(p: &Path, from: u64) -> Option<u64> {
    let mut f = File::open(p).unwrap();
    let fd = std::os::fd::AsRawFd::as_raw_fd(&f);
    // SAFETY: plain lseek.
    let r = unsafe { libc::lseek(fd, from as i64, libc::SEEK_DATA) };
    let _ = f.seek(SeekFrom::Start(0));
    if r < 0 { None } else { Some(r as u64) }
}

#[tokio::test]
async fn placeholder_is_never_visible_without_its_marker() {
    let Some(e) = env() else { return };
    let seq = MarkerSeq::new();
    let spec = PlaceholderSpec {
        file_id: 11,
        version_id: 110,
        size: 184 << 30,
        share_id: 1,
        flags: Flags::ARCHIVED,
        uid: 1234,
        gid: 5678,
        mode: 0o640,
        mtime_ns: 1_700_000_000_123_456_789,
    };
    let rel = Path::new("line_1042.segy");
    let Created::Created(key) = create_placeholder(&e.layout, rel, &spec, &seq).unwrap() else {
        panic!()
    };
    let p = e.layout.path(rel);
    let md = std::fs::metadata(&p).unwrap();
    assert_eq!(md.len(), 184 << 30);
    assert_eq!(md.blocks(), 0, "no data blocks");
    assert_eq!(
        (md.uid(), md.gid(), md.mode() & 0o7777),
        (1234, 5678, 0o640)
    );
    assert_eq!(md.mtime_nsec(), 123_456_789);
    assert_eq!(md.ino(), key.ino);
    let m = marker_at(&p);
    assert_eq!(
        (m.state, m.resident_bytes, m.file_id, m.version_id),
        (State::Partial, 0, 11, 110)
    );
    assert!(m.flags.contains(Flags::ARCHIVED));
    // The FSAL's generation matches FS_IOC_GETVERSION.
    let f = File::open(&p).unwrap();
    assert_eq!(sys::inode_generation(f.as_fd()).unwrap(), key.ino_gen);
    // Idempotent: a redo adopts it.
    let again = create_placeholder(&e.layout, rel, &spec, &seq).unwrap();
    assert!(
        matches!(again, Created::Adopted { key: k, version_id: Some(110) } if k.ino == key.ino)
    );
    // A local unmanaged file at the name is never overwritten.
    std::fs::write(e.layout.path(Path::new("mine.txt")), b"user data").unwrap();
    let other = PlaceholderSpec {
        file_id: 12,
        ..spec.clone()
    };
    assert!(matches!(
        create_placeholder(&e.layout, Path::new("mine.txt"), &other, &seq).unwrap(),
        Created::Conflict(_)
    ));
    assert_eq!(
        std::fs::read(e.layout.path(Path::new("mine.txt"))).unwrap(),
        b"user data"
    );
    // The staging directory holds nothing afterwards.
    assert_eq!(std::fs::read_dir(&e.layout.staging).unwrap().count(), 0);
}

#[tokio::test]
async fn stub_handshake_narrows_then_punches() {
    let Some(e) = env() else { return };
    let data = pattern(8 << 20);
    let p = resident_file(&e.layout, "a.segy", &data, 21, 210);
    let fsal = FakeFsal::default();
    let req = StubRequest {
        rel: "a.segy".into(),
        file_id: 21,
        version_id: 210,
        resident_bytes: 1 << 20,
        expect_ino: None,
        expect_ctime_ns: None,
    };
    let out = stub(
        &e.layout,
        &req,
        &fsal,
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    assert_eq!(
        out,
        StubOutcome::Stubbed {
            resident_bytes: 1 << 20
        }
    );
    assert_eq!(fsal.acked_invalidations.load(Ordering::SeqCst), 1);
    let m = marker_at(&p);
    assert_eq!(
        (m.state, m.resident_bytes, m.size),
        (State::Partial, 1 << 20, 8 << 20)
    );
    // Prefix intact, the rest is a hole.
    let mut head = vec![0u8; 1 << 20];
    File::open(&p).unwrap().read_exact(&mut head).unwrap();
    assert_eq!(head, &data[..1 << 20]);
    assert_eq!(
        data_extent_end(&p, 1 << 20),
        None,
        "nothing but holes past the watermark"
    );
    assert!(std::fs::metadata(&p).unwrap().blocks() * 512 <= (1 << 20) + 65536);
    // A redo of the same instruction is harmless.
    let out = stub(
        &e.layout,
        &req,
        &fsal,
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    assert_eq!(
        out,
        StubOutcome::Stubbed {
            resident_bytes: 1 << 20
        }
    );
}

/// xfs keeps speculative preallocation past EOF when a file is written by
/// many open/write/close cycles (how Ganesha writes an NFSv3 file with
/// Close_Fast): the trim on close is skipped. The stub must free it too, so
/// disk usage drops when the stub is made, not at xfs's next eofblocks scan
/// (M1 e2e finding).
#[tokio::test]
async fn stub_frees_preallocation_past_eof() {
    let Some(e) = env() else { return };
    let rel = "nfs-written.segy";
    let p = e.layout.path(Path::new(rel));
    let data = pattern(32 << 20);
    File::create(&p).unwrap();
    for chunk in data.chunks(256 << 10) {
        let mut f = std::fs::OpenOptions::new().append(true).open(&p).unwrap();
        f.write_all(chunk).unwrap();
    }
    let f = File::open(&p).unwrap();
    let mut m = Marker::resident(23, 230, data.len() as u64, 1);
    m.flags = Flags::ONLINE;
    xattr::write(f.as_fd(), &m).unwrap();
    let before = std::fs::metadata(&p).unwrap().blocks() * 512;
    if before <= data.len() as u64 {
        eprintln!("note: xfs made no post-EOF preallocation here ({before} bytes allocated)");
    }
    let req = StubRequest {
        rel: rel.into(),
        file_id: 23,
        version_id: 230,
        resident_bytes: 1 << 20,
        expect_ino: None,
        expect_ctime_ns: None,
    };
    let out = stub(
        &e.layout,
        &req,
        &FakeFsal::default(),
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    assert_eq!(
        out,
        StubOutcome::Stubbed {
            resident_bytes: 1 << 20
        }
    );
    let meta = std::fs::metadata(&p).unwrap();
    assert_eq!(meta.len(), data.len() as u64, "the size is kept");
    assert!(
        meta.blocks() * 512 <= (1 << 20) + 65536,
        "allocated {} bytes after the stub (was {before})",
        meta.blocks() * 512
    );
    let mut head = vec![0u8; 1 << 20];
    File::open(&p).unwrap().read_exact(&mut head).unwrap();
    assert_eq!(head, &data[..1 << 20]);
}

#[tokio::test]
async fn no_ack_means_no_punch() {
    let Some(e) = env() else { return };
    let data = pattern(4 << 20);
    let p = resident_file(&e.layout, "b.segy", &data, 22, 220);
    let fsal = FakeFsal::default();
    fsal.nack.store(true, Ordering::SeqCst);
    let req = StubRequest {
        rel: "b.segy".into(),
        file_id: 22,
        version_id: 220,
        resident_bytes: 0,
        expect_ino: None,
        expect_ctime_ns: None,
    };
    let out = stub(
        &e.layout,
        &req,
        &fsal,
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    assert!(matches!(out, StubOutcome::Deferred { .. }));
    // Narrowed marker over bytes that are all still there: safe.
    assert_eq!(marker_at(&p).resident_bytes, 0);
    assert_eq!(std::fs::read(&p).unwrap(), data);
    // Once acked, the retry punches.
    fsal.nack.store(false, Ordering::SeqCst);
    let out = stub(
        &e.layout,
        &req,
        &fsal,
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    assert_eq!(out, StubOutcome::Stubbed { resident_bytes: 0 });
    assert_eq!(data_extent_end(&p, 0), None);
}

#[tokio::test]
async fn stub_refuses_other_versions_writers_and_unmanaged_files() {
    let Some(e) = env() else { return };
    let fsal = FakeFsal::default();
    let seq = MarkerSeq::new();
    resident_file(&e.layout, "c.segy", &pattern(1 << 20), 23, 230);
    let mut req = StubRequest {
        rel: "c.segy".into(),
        file_id: 23,
        version_id: 999,
        resident_bytes: 0,
        expect_ino: None,
        expect_ctime_ns: None,
    };
    let out = stub(&e.layout, &req, &fsal, &NoSmbGuard, &seq, &FileLocks::new()).await;
    assert!(matches!(
        out,
        StubOutcome::Skipped {
            reason: SkipReason::VersionMismatch,
            ..
        }
    ));
    req.version_id = 230;
    {
        let _writer = OpenOptions::new()
            .write(true)
            .open(e.layout.path(Path::new("c.segy")))
            .unwrap();
        let out = stub(&e.layout, &req, &fsal, &NoSmbGuard, &seq, &FileLocks::new()).await;
        assert!(
            matches!(
                out,
                StubOutcome::Skipped {
                    reason: SkipReason::OpenForWrite,
                    ..
                }
            ),
            "{out:?}"
        );
    }
    req.expect_ctime_ns = Some(1);
    let out = stub(&e.layout, &req, &fsal, &NoSmbGuard, &seq, &FileLocks::new()).await;
    assert!(
        matches!(
            out,
            StubOutcome::Skipped {
                reason: SkipReason::VersionMismatch,
                ..
            }
        ),
        "{out:?}"
    );
    req.expect_ctime_ns = None;
    req.expect_ino = Some((1, 1));
    let out = stub(&e.layout, &req, &fsal, &NoSmbGuard, &seq, &FileLocks::new()).await;
    assert!(matches!(
        out,
        StubOutcome::Skipped {
            reason: SkipReason::InodeChanged,
            ..
        }
    ));
    std::fs::write(e.layout.path(Path::new("plain.txt")), b"x").unwrap();
    let plain = StubRequest {
        rel: "plain.txt".into(),
        file_id: 1,
        version_id: 1,
        resident_bytes: 0,
        expect_ino: None,
        expect_ctime_ns: None,
    };
    let out = stub(
        &e.layout,
        &plain,
        &fsal,
        &NoSmbGuard,
        &seq,
        &FileLocks::new(),
    )
    .await;
    assert!(matches!(
        out,
        StubOutcome::Skipped {
            reason: SkipReason::NotManaged,
            ..
        }
    ));
    assert_eq!(
        fsal.acked_invalidations.load(Ordering::SeqCst),
        0,
        "nothing narrowed"
    );
}

fn crc(data: &[u8]) -> u64 {
    omnifile_integrity::crc64_nvme(data)
}

#[tokio::test]
async fn fill_widens_only_durable_bytes_and_ends_resident() {
    let Some(e) = env() else { return };
    let data = pattern((20 << 20) + 1234);
    let p = resident_file(&e.layout, "d.segy", &data, 24, 240);
    let fsal = FakeFsal::default();
    let seq = MarkerSeq::new();
    let req = StubRequest {
        rel: "d.segy".into(),
        file_id: 24,
        version_id: 240,
        resident_bytes: 4096,
        expect_ino: None,
        expect_ctime_ns: None,
    };
    assert!(matches!(
        stub(&e.layout, &req, &fsal, &NoSmbGuard, &seq, &FileLocks::new()).await,
        StubOutcome::Stubbed { .. }
    ));
    let target = FetchTarget {
        file_id: 24,
        version_id: 240,
        url: "mem://".into(),
        source: omnifile_stub::SourceKind::AzureSas,
        size: data.len() as u64,
        crc64nvme: Some(crc(&data)),
    };
    let src = MemSource {
        data: data.clone(),
        corrupt_at: None,
    };
    let opts = FillOptions {
        chunk: 1 << 20,
        widen_every: 4 << 20,
    };
    let freq = FillRequest {
        rel: "d.segy".into(),
        expect_ino: None,
    };
    let n = fill(
        &e.layout,
        &freq,
        &target,
        &src,
        &fsal,
        &seq,
        &FileLocks::new(),
        opts,
    )
    .await
    .unwrap();
    assert_eq!(n, data.len() as u64);
    assert_eq!(
        std::fs::read(&p).unwrap(),
        data,
        "byte-identical after fill"
    );
    let m = marker_at(&p);
    assert_eq!(
        (m.state, m.resident_bytes),
        (State::Resident, data.len() as u64)
    );
    let prog = fsal.progress.lock().unwrap().clone();
    assert!(
        prog.len() >= 4 && prog.windows(2).all(|w| w[0] < w[1]),
        "{prog:?}"
    );
    assert_eq!(*prog.last().unwrap(), data.len() as u64);
}

/// S3 ranges carry no checksum (`fsal-namespace.md` §5.3): a fill from S3
/// widens only once, after the whole-file CRC matched, so a bad byte is
/// never readable, not even for a moment.
#[tokio::test]
async fn an_s3_fill_exposes_nothing_before_the_whole_file_matches() {
    let Some(e) = env() else { return };
    for (name, corrupt_at) in [("s3ok.segy", None), ("s3bad.segy", Some(5 << 20))] {
        let data = pattern(6 << 20);
        let p = resident_file(&e.layout, name, &data, 26, 260);
        let fsal = FakeFsal::default();
        let seq = MarkerSeq::new();
        let req = StubRequest {
            rel: name.into(),
            file_id: 26,
            version_id: 260,
            resident_bytes: 0,
            expect_ino: None,
            expect_ctime_ns: None,
        };
        stub(&e.layout, &req, &fsal, &NoSmbGuard, &seq, &FileLocks::new()).await;
        let target = FetchTarget {
            file_id: 26,
            version_id: 260,
            url: "mem://".into(),
            source: omnifile_stub::SourceKind::S3Presigned,
            size: 6 << 20,
            crc64nvme: Some(crc(&data)),
        };
        let src = MemSource {
            data: data.clone(),
            corrupt_at,
        };
        let opts = FillOptions {
            chunk: 1 << 20,
            widen_every: 1 << 20,
        };
        let freq = FillRequest {
            rel: name.into(),
            expect_ino: None,
        };
        let r = fill(
            &e.layout,
            &freq,
            &target,
            &src,
            &fsal,
            &seq,
            &FileLocks::new(),
            opts,
        )
        .await;
        let prog = fsal.progress.lock().unwrap().clone();
        let m = marker_at(&p);
        if corrupt_at.is_none() {
            assert_eq!(r.unwrap(), data.len() as u64);
            // One widen, to the whole file: none on the way.
            assert_eq!(prog, vec![data.len() as u64]);
            assert_eq!((m.state, m.resident_bytes), (State::Resident, 6 << 20));
            assert_eq!(std::fs::read(&p).unwrap(), data);
        } else {
            assert!(matches!(r, Err(FillError::Integrity { .. })), "{r:?}");
            assert!(prog.is_empty(), "nothing was exposed: {prog:?}");
            assert_eq!((m.state, m.resident_bytes), (State::Partial, 0));
            assert_eq!(data_extent_end(&p, 0), None);
        }
    }
}

#[tokio::test]
async fn fill_with_bad_crc_is_narrowed_back() {
    let Some(e) = env() else { return };
    let data = pattern(6 << 20);
    let p = resident_file(&e.layout, "e.segy", &data, 25, 250);
    let fsal = FakeFsal::default();
    let seq = MarkerSeq::new();
    let req = StubRequest {
        rel: "e.segy".into(),
        file_id: 25,
        version_id: 250,
        resident_bytes: 0,
        expect_ino: None,
        expect_ctime_ns: None,
    };
    stub(&e.layout, &req, &fsal, &NoSmbGuard, &seq, &FileLocks::new()).await;
    let target = FetchTarget {
        file_id: 25,
        version_id: 250,
        url: "mem://".into(),
        source: omnifile_stub::SourceKind::AzureSas,
        size: 6 << 20,
        crc64nvme: Some(crc(&data)),
    };
    let src = MemSource {
        data: data.clone(),
        corrupt_at: Some(5 << 20),
    };
    let opts = FillOptions {
        chunk: 1 << 20,
        widen_every: 2 << 20,
    };
    let freq = FillRequest {
        rel: "e.segy".into(),
        expect_ino: None,
    };
    let err = fill(
        &e.layout,
        &freq,
        &target,
        &src,
        &fsal,
        &seq,
        &FileLocks::new(),
        opts,
    )
    .await
    .unwrap_err();
    assert!(matches!(err, FillError::Integrity { .. }), "{err}");
    let m = marker_at(&p);
    assert_eq!(
        (m.state, m.resident_bytes),
        (State::Partial, 0),
        "unverified bytes never stay resident"
    );
    assert_eq!(data_extent_end(&p, 0), None);
}

#[tokio::test]
async fn fill_refuses_another_version() {
    let Some(e) = env() else { return };
    let data = pattern(1 << 20);
    resident_file(&e.layout, "f.segy", &data, 26, 260);
    let fsal = FakeFsal::default();
    let seq = MarkerSeq::new();
    let req = StubRequest {
        rel: "f.segy".into(),
        file_id: 26,
        version_id: 260,
        resident_bytes: 0,
        expect_ino: None,
        expect_ctime_ns: None,
    };
    stub(&e.layout, &req, &fsal, &NoSmbGuard, &seq, &FileLocks::new()).await;
    // A rewind read of version 259 must never fill version 260's placeholder.
    let target = FetchTarget {
        file_id: 26,
        version_id: 259,
        url: "mem://".into(),
        source: omnifile_stub::SourceKind::AzureSas,
        size: 1 << 20,
        crc64nvme: Some(crc(&data)),
    };
    let src = MemSource {
        data,
        corrupt_at: None,
    };
    let freq = FillRequest {
        rel: "f.segy".into(),
        expect_ino: None,
    };
    let err = fill(
        &e.layout,
        &freq,
        &target,
        &src,
        &fsal,
        &seq,
        &FileLocks::new(),
        FillOptions::default(),
    )
    .await
    .unwrap_err();
    assert!(matches!(err, FillError::VersionMismatch { .. }));
}

fn ev_share() -> serde_json::Value {
    json!({"type": "share.upsert", "entity_id": "1", "entity_version": "1",
           "share": {"share_id": "1", "dataset_id": "1", "name": "projects", "root_dir_id": "100",
                     "on_open": "wait", "worm": {"enabled": false}, "entity_version": "1"}})
}

fn ev_file(
    ty: &str,
    id: u64,
    parent: Option<u64>,
    name: &str,
    kind: &str,
    ev: u64,
    version: u64,
) -> serde_json::Value {
    json!({"type": ty, "entity_id": id.to_string(), "entity_version": ev.to_string(),
           "file": {"file_id": id.to_string(), "dataset_id": "1", "parent_id": parent.map(|p| p.to_string()),
                    "name": name, "kind": kind, "size": 5_000_000, "mtime_ns": "1700000000000000000",
                    "uid": 0, "gid": 0, "mode": if kind == "dir" { 0o755 } else { 0o644 },
                    "version_id": version.to_string(),
                    "location": {"backend_kind": "s3", "container": "b", "object_key": "k", "logical_tier": "archive",
                                 "archived": true, "restore_state": "archived"},
                    "entity_version": ev.to_string()}})
}

/// A dataset's root is a share root: the edge gives it the share's Owners
/// group (omnifile_edge::shares), which the catalog doesn't carry. A share
/// update re-materialises the root, and must keep that group; a folder
/// below still follows the catalog (M6 review finding 6).
#[tokio::test]
async fn a_share_root_keeps_the_group_the_edge_gave_it() {
    use std::os::unix::fs::{MetadataExt, chown};
    let Some(e) = env() else { return };
    let fsal = FakeFsal::default();
    let seq = MarkerSeq::new();
    let mut r = Replica::open(&e.layout.data_root.join("replica-root.db")).unwrap();
    let locks = FileLocks::new();
    let m = Materializer {
        layout: &e.layout,
        notifier: &fsal,
        seq: &seq,
        locks: &locks,
        perms: &omnifile_stub::materialize::ModeOnly,
    };
    // A share of the folder `seismic` too.
    let folder_share = json!({"type": "share.upsert", "entity_id": "2", "entity_version": "1",
        "share": {"share_id": "2", "dataset_id": "1", "name": "seis", "root_dir_id": "200",
                  "on_open": "wait", "worm": {"enabled": false}, "entity_version": "1"}});
    r.apply_page(
        &[
            ev_share(),
            folder_share,
            ev_file("file.upsert", 100, None, "", "dir", 1, 1),
            ev_file("file.upsert", 200, Some(100), "seismic", "dir", 1, 1),
            ev_file("file.upsert", 300, Some(100), "other", "dir", 1, 1),
        ],
        "1.0",
    )
    .unwrap();
    run_redo(&mut r, &m, 100, 10).await.unwrap();
    let root = e.layout.path(Path::new("projects"));
    let (sub, other) = (root.join("seismic"), root.join("other"));
    for p in [&root, &sub, &other] {
        chown(p, None, Some(4321)).unwrap();
    }
    let mut share = ev_share();
    share["entity_version"] = json!("2");
    share["share"]["entity_version"] = json!("2");
    share["share"]["on_open"] = json!("fail");
    r.apply_page(
        &[
            share,
            ev_file("file.upsert", 200, Some(100), "seismic", "dir", 2, 1),
            ev_file("file.upsert", 300, Some(100), "other", "dir", 2, 1),
        ],
        "2.0",
    )
    .unwrap();
    run_redo(&mut r, &m, 100, 10).await.unwrap();
    let gid = |p: &Path| std::fs::metadata(p).unwrap().gid();
    assert_eq!(gid(&root), 4321, "the share root keeps its group");
    assert_eq!(gid(&sub), 4321, "so does a shared folder");
    assert_eq!(gid(&other), 0, "a folder that isn't follows the catalog");
}

#[tokio::test]
async fn feed_drives_the_tree_with_redo() {
    let Some(e) = env() else { return };
    let fsal = FakeFsal::default();
    let seq = MarkerSeq::new();
    let mut r = Replica::open(&e.layout.data_root.join("replica.db")).unwrap();
    let locks = FileLocks::new();
    let m = Materializer {
        layout: &e.layout,
        notifier: &fsal,
        seq: &seq,
        locks: &locks,
        perms: &omnifile_stub::materialize::ModeOnly,
    };
    // Child first (orphan), then root and directory.
    r.apply_page(
        &[ev_file(
            "file.upsert",
            301,
            Some(200),
            "line.segy",
            "file",
            1,
            3010,
        )],
        "1.0",
    )
    .unwrap();
    run_redo(&mut r, &m, 100, 10).await.unwrap();
    assert!(!e.layout.path(Path::new("projects")).exists());
    r.apply_page(
        &[
            ev_share(),
            ev_file("file.upsert", 100, None, "", "dir", 1, 1),
            ev_file("file.upsert", 200, Some(100), "seismic", "dir", 1, 1),
        ],
        "2.0",
    )
    .unwrap();
    let st = run_redo(&mut r, &m, 100, 10).await.unwrap();
    assert_eq!(st.done, 3);
    let p = e.layout.path(Path::new("projects/seismic/line.segy"));
    let mk = marker_at(&p);
    assert_eq!(
        (mk.file_id, mk.version_id, mk.resident_bytes),
        (301, 3010, 0)
    );
    assert!(mk.flags.contains(Flags::ARCHIVED));
    assert_eq!(mk.share_id, 1);
    let node = r.node_by_file_id(301).unwrap().unwrap();
    assert_eq!(node.ino, Some(std::fs::metadata(&p).unwrap().ino()));

    // A restore makes the same content version ONLINE: flags follow, bytes untouched.
    let mut restored = ev_file("file.upsert", 301, Some(200), "line.segy", "file", 2, 3010);
    restored["file"]["location"]["archived"] = json!(false);
    restored["file"]["location"]["restore_state"] = json!("not_needed");
    r.apply_page(&[restored], "2.5").unwrap();
    run_redo(&mut r, &m, 100, 10).await.unwrap();
    let mk = marker_at(&p);
    assert!(mk.flags.contains(Flags::ONLINE) && !mk.flags.contains(Flags::ARCHIVED));
    assert_eq!((mk.version_id, mk.resident_bytes), (3010, 0));

    // Directory rename keeps inodes (NFS handles survive).
    let ino_before = std::fs::metadata(&p).unwrap().ino();
    r.apply_page(
        &[ev_file(
            "file.rename",
            200,
            Some(100),
            "seismic-2026",
            "dir",
            2,
            1,
        )],
        "3.0",
    )
    .unwrap();
    run_redo(&mut r, &m, 100, 10).await.unwrap();
    let p2 = e.layout.path(Path::new("projects/seismic-2026/line.segy"));
    assert_eq!(std::fs::metadata(&p2).unwrap().ino(), ino_before);

    // The source object was rewritten: new content version, narrowed with an ack.
    r.apply_page(
        &[ev_file(
            "file.upsert",
            301,
            Some(200),
            "line.segy",
            "file",
            3,
            3011,
        )],
        "4.0",
    )
    .unwrap();
    let before = fsal.acked_invalidations.load(Ordering::SeqCst);
    run_redo(&mut r, &m, 100, 10).await.unwrap();
    assert_eq!(marker_at(&p2).version_id, 3011);
    assert_eq!(fsal.acked_invalidations.load(Ordering::SeqCst), before + 1);
    assert_eq!(
        std::fs::metadata(&p2).unwrap().ino(),
        ino_before,
        "updated in place, never recreated"
    );

    // Directory tombstone before its child's: rmdir retried.
    r.apply_page(
        &[ev_file(
            "file.tombstone",
            200,
            Some(100),
            "seismic-2026",
            "dir",
            4,
            1,
        )],
        "5.0",
    )
    .unwrap();
    run_redo(&mut r, &m, 100, 10).await.unwrap();
    assert!(
        e.layout.path(Path::new("projects/seismic-2026")).exists(),
        "ENOTEMPTY keeps it"
    );
    r.apply_page(
        &[ev_file(
            "file.tombstone",
            301,
            Some(200),
            "line.segy",
            "file",
            5,
            3011,
        )],
        "6.0",
    )
    .unwrap();
    run_redo(&mut r, &m, 100, 10).await.unwrap();
    assert!(!e.layout.path(Path::new("projects/seismic-2026")).exists());
    assert_eq!(r.pending_count().unwrap(), 0);

    // A crash after apply but before redo: reopen and redo.
    r.apply_page(
        &[ev_file(
            "file.upsert",
            302,
            Some(100),
            "late.segy",
            "file",
            1,
            3020,
        )],
        "7.0",
    )
    .unwrap();
    drop(r);
    let mut r = Replica::open(&e.layout.data_root.join("replica.db")).unwrap();
    assert_eq!(r.cursor().unwrap(), "7.0");
    run_redo(&mut r, &m, 100, 10).await.unwrap();
    assert!(e.layout.path(Path::new("projects/late.segy")).exists());
    let _ = Kind::File;
}

#[test]
fn xattr_reads_never_need_rdwr_on_an_immutable_file() {
    let Some(e) = env() else { return };
    let p = resident_file(&e.layout, "imm.segy", b"hello", 30, 300);
    // chattr +i
    let f = File::open(&p).unwrap();
    const FS_IOC_GETFLAGS: libc::c_ulong = 0x8008_6601;
    const FS_IOC_SETFLAGS: libc::c_ulong = 0x4008_6602;
    const FS_IMMUTABLE_FL: libc::c_long = 0x10;
    let fd = std::os::fd::AsRawFd::as_raw_fd(&f);
    let mut flags: libc::c_long = 0;
    // SAFETY: valid out-parameter.
    unsafe { libc::ioctl(fd, FS_IOC_GETFLAGS as _, &mut flags) };
    flags |= FS_IMMUTABLE_FL;
    // SAFETY: valid in-parameter.
    assert_eq!(unsafe { libc::ioctl(fd, FS_IOC_SETFLAGS as _, &flags) }, 0);
    let ro = sys::open_ro(&p).unwrap();
    assert!(matches!(xattr::read(ro.as_fd()).unwrap(), ReadOutcome::Marker(m) if m.file_id == 30));
    assert!(
        OpenOptions::new().read(true).write(true).open(&p).is_err(),
        "O_RDWR is refused"
    );
    flags &= !FS_IMMUTABLE_FL;
    // SAFETY: as above.
    unsafe { libc::ioctl(fd, FS_IOC_SETFLAGS as _, &flags) };
    let mut w = OpenOptions::new().write(true).open(&p).unwrap();
    w.write_all(b"x").unwrap();
    let _ = w.write_at(b"y", 0);
}

/// A source that serves slowly and counts the chunks it served, so a test
/// can act while a fill is in the middle.
struct SlowSource {
    data: Vec<u8>,
    served: AtomicUsize,
}

impl RangeSource for SlowSource {
    async fn get_range(&self, _t: &FetchTarget, offset: u64, len: u64) -> io::Result<Vec<u8>> {
        tokio::time::sleep(std::time::Duration::from_millis(40)).await;
        self.served.fetch_add(1, Ordering::SeqCst);
        Ok(self.data[offset as usize..(offset + len) as usize].to_vec())
    }
}

async fn when(mut cond: impl FnMut() -> bool) {
    while !cond() {
        tokio::time::sleep(std::time::Duration::from_millis(5)).await;
    }
}

/// A stubbed file and the target to fill it from.
async fn stubbed(
    e: &Env,
    name: &str,
    file_id: u64,
    version_id: u64,
    len: usize,
) -> (PathBuf, Vec<u8>, FetchTarget) {
    let data = pattern(len);
    let p = resident_file(&e.layout, name, &data, file_id, version_id);
    let req = StubRequest {
        rel: name.into(),
        file_id,
        version_id,
        resident_bytes: 0,
        expect_ino: None,
        expect_ctime_ns: None,
    };
    let out = stub(
        &e.layout,
        &req,
        &FakeFsal::default(),
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    assert_eq!(out, StubOutcome::Stubbed { resident_bytes: 0 });
    let target = FetchTarget {
        file_id,
        version_id,
        url: "mem://".into(),
        source: omnifile_stub::SourceKind::AzureSas,
        size: len as u64,
        crc64nvme: Some(crc(&data)),
    };
    (p, data, target)
}

const SLOW: FillOptions = FillOptions {
    chunk: 1 << 20,
    widen_every: 2 << 20,
};

#[tokio::test]
async fn a_stub_or_version_change_never_interleaves_with_a_fill() {
    let Some(e) = env() else { return };
    let (p, data, target) = stubbed(&e, "race1.segy", 40, 400, 8 << 20).await;
    let (fsal, seq, locks) = (FakeFsal::default(), MarkerSeq::new(), FileLocks::new());
    let src = SlowSource {
        data: data.clone(),
        served: AtomicUsize::new(0),
    };
    let freq = FillRequest {
        rel: "race1.segy".into(),
        expect_ino: None,
    };
    let racer = async {
        when(|| src.served.load(Ordering::SeqCst) >= 2).await;
        // A stub instruction arrives mid-fill: refused, nothing narrowed.
        let req = StubRequest {
            rel: "race1.segy".into(),
            file_id: 40,
            version_id: 400,
            resident_bytes: 0,
            expect_ino: None,
            expect_ctime_ns: None,
        };
        let out = stub(&e.layout, &req, &fsal, &NoSmbGuard, &seq, &locks).await;
        // The source object is rewritten mid-fill: the version change waits.
        let spec = PlaceholderSpec {
            file_id: 40,
            version_id: 401,
            size: 8 << 20,
            share_id: 1,
            flags: Flags::ONLINE,
            uid: 0,
            gid: 0,
            mode: 0o644,
            mtime_ns: 0,
        };
        let replaced = omnifile_stub::handshake::replace_content(
            &e.layout,
            Path::new("race1.segy"),
            &spec,
            &fsal,
            &seq,
            &locks,
        )
        .await
        .unwrap();
        (out, replaced)
    };
    let (filled, (stub_out, replaced)) = tokio::join!(
        fill(&e.layout, &freq, &target, &src, &fsal, &seq, &locks, SLOW),
        racer
    );
    assert_eq!(stub_out, StubOutcome::Busy);
    assert_eq!(
        replaced, None,
        "the version change is retried after the fill"
    );
    assert_eq!(filled.unwrap(), 8 << 20);
    let m = marker_at(&p);
    assert_eq!((m.state, m.version_id), (State::Resident, 400));
    assert_eq!(
        std::fs::read(&p).unwrap(),
        data,
        "only version 400's bytes, all of them"
    );
    // Afterwards the version change goes through and nothing of 400 stays readable.
    let spec = PlaceholderSpec {
        file_id: 40,
        version_id: 401,
        size: 8 << 20,
        share_id: 1,
        flags: Flags::ONLINE,
        uid: 0,
        gid: 0,
        mode: 0o644,
        mtime_ns: 0,
    };
    omnifile_stub::handshake::replace_content(
        &e.layout,
        Path::new("race1.segy"),
        &spec,
        &fsal,
        &seq,
        &locks,
    )
    .await
    .unwrap()
    .unwrap();
    let m = marker_at(&p);
    assert_eq!((m.version_id, m.resident_bytes), (401, 0));
    assert_eq!(data_extent_end(&p, 0), None);
}

#[tokio::test]
async fn a_truncate_mid_fill_stops_it_and_the_marker_is_never_recreated() {
    let Some(e) = env() else { return };
    let (p, data, target) = stubbed(&e, "race2.segy", 41, 410, 8 << 20).await;
    let (fsal, seq, locks) = (FakeFsal::default(), MarkerSeq::new(), FileLocks::new());
    let src = SlowSource {
        data,
        served: AtomicUsize::new(0),
    };
    let freq = FillRequest {
        rel: "race2.segy".into(),
        expect_ino: None,
    };
    let racer = async {
        when(|| src.served.load(Ordering::SeqCst) >= 3).await;
        // What the FSAL does on an NFS truncate to 0: truncate, then remove
        // the marker. The user's new content follows.
        let f = OpenOptions::new().write(true).open(&p).unwrap();
        f.set_len(0).unwrap();
        xattr::remove(f.as_fd()).unwrap();
        f.write_all_at(b"user", 0).unwrap();
    };
    let (filled, ()) = tokio::join!(
        fill(&e.layout, &freq, &target, &src, &fsal, &seq, &locks, SLOW),
        racer
    );
    assert!(
        matches!(filled, Err(FillError::MarkerChanged)),
        "{filled:?}"
    );
    let ro = sys::open_ro(&p).unwrap();
    assert!(
        matches!(xattr::read(ro.as_fd()).unwrap(), ReadOutcome::Unmanaged),
        "never recreated"
    );
    assert_eq!(
        std::fs::read(&p).unwrap(),
        b"user",
        "no backend bytes in the user's file"
    );
}

#[tokio::test]
async fn a_concurrent_marker_rewrite_is_never_widened_over() {
    let Some(e) = env() else { return };
    let (p, data, target) = stubbed(&e, "race3.segy", 42, 420, 8 << 20).await;
    let (fsal, seq, locks) = (FakeFsal::default(), MarkerSeq::new(), FileLocks::new());
    let src = SlowSource {
        data,
        served: AtomicUsize::new(0),
    };
    let freq = FillRequest {
        rel: "race3.segy".into(),
        expect_ino: None,
    };
    let racer = async {
        when(|| src.served.load(Ordering::SeqCst) >= 1).await;
        // A writer that bypasses the daemon's locks re-versions the marker.
        let ro = sys::open_ro(&p).unwrap();
        let mut m = xattr::read_required(ro.as_fd()).unwrap();
        m.version_id = 421;
        m.marker_seq += 1_000_000;
        xattr::write(ro.as_fd(), &m).unwrap();
    };
    let (filled, ()) = tokio::join!(
        fill(&e.layout, &freq, &target, &src, &fsal, &seq, &locks, SLOW),
        racer
    );
    assert!(
        matches!(filled, Err(FillError::MarkerChanged)),
        "{filled:?}"
    );
    let m = marker_at(&p);
    assert_eq!(
        (m.version_id, m.resident_bytes),
        (421, 0),
        "nothing marked resident for the new version"
    );
}

#[tokio::test]
async fn the_fill_fence_gates_every_write() {
    let Some(e) = env() else { return };
    let (p, data, target) = stubbed(&e, "fence.segy", 43, 430, 8 << 20).await;
    let (fsal, seq, locks) = (FakeFsal::default(), MarkerSeq::new(), FileLocks::new());
    let freq = FillRequest {
        rel: "fence.segy".into(),
        expect_ino: None,
    };
    // An FSAL did not confirm: nothing is written.
    fsal.refuse_fence.store(true, Ordering::SeqCst);
    let src = SlowSource {
        data: data.clone(),
        served: AtomicUsize::new(0),
    };
    let r = fill(&e.layout, &freq, &target, &src, &fsal, &seq, &locks, SLOW).await;
    assert!(matches!(r, Err(FillError::FenceBroken)));
    assert_eq!(
        data_extent_end(&p, 0),
        None,
        "no byte written without the fence"
    );
    // An FSAL (re)connects mid-fill: the fill stops before its next write,
    // and only bytes widened before that are marked resident.
    fsal.refuse_fence.store(false, Ordering::SeqCst);
    let racer = async {
        when(|| src.served.load(Ordering::SeqCst) >= 3).await;
        fsal.epoch.fetch_add(1, Ordering::SeqCst);
    };
    let (r, ()) = tokio::join!(
        fill(&e.layout, &freq, &target, &src, &fsal, &seq, &locks, SLOW),
        racer
    );
    assert!(matches!(r, Err(FillError::FenceBroken)), "{r:?}");
    let m = marker_at(&p);
    assert_eq!(m.state, State::Partial);
    assert!(m.resident_bytes <= 2 << 20, "resident {}", m.resident_bytes);
    let mut head = vec![0u8; m.resident_bytes as usize];
    File::open(&p).unwrap().read_exact(&mut head).unwrap();
    assert_eq!(head, &data[..m.resident_bytes as usize]);
}

// ---- M1 review regressions (B1, B2, S4, S5, S6) -----------------------

fn stub_req(rel: &str, file_id: u64, version_id: u64, resident: u64) -> StubRequest {
    StubRequest {
        rel: rel.into(),
        file_id,
        version_id,
        resident_bytes: resident,
        expect_ino: None,
        expect_ctime_ns: None,
    }
}

fn assert_whole(p: &Path, data: &[u8], size: u64) {
    let m = marker_at(p);
    assert_eq!(
        (m.state, m.resident_bytes),
        (State::Resident, size),
        "the marker covers every byte again"
    );
    assert_eq!(std::fs::read(p).unwrap(), data, "nothing punched");
}

/// B1: a writer that opens the file after the check (an NFS WRITE arriving
/// while we wait for the acks) breaks the held read lease: no punch, the
/// marker is widened back, and the writer then proceeds.
#[tokio::test]
async fn b1_a_writer_opening_during_the_handshake_is_never_punched() {
    let Some(e) = env() else { return };
    let data = pattern(8 << 20);
    let p = resident_file(&e.layout, "b1w.segy", &data, 60, 600);
    let fsal = FakeFsal::default();
    let opened = std::sync::Arc::new(AtomicBool::new(false));
    let (hp, ho) = (p.clone(), opened.clone());
    *fsal.on_ack.lock().unwrap() = Some(Box::new(move || {
        // Non-blocking: under the lease this starts a lease break and
        // returns EWOULDBLOCK; without one it opens and writes.
        if let Ok(f) = OpenOptions::new()
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(&hp)
        {
            f.write_all_at(b"USER", 2 << 20).unwrap();
            ho.store(true, Ordering::SeqCst);
        }
    }));
    let out = stub(
        &e.layout,
        &stub_req("b1w.segy", 60, 600, 0),
        &fsal,
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    let mut expect = data.clone();
    if opened.load(Ordering::SeqCst) {
        expect[2 << 20..(2 << 20) + 4].copy_from_slice(b"USER");
    }
    assert!(
        matches!(
            out,
            StubOutcome::Skipped {
                reason: SkipReason::OpenForWrite,
                ..
            }
        ),
        "{out:?}"
    );
    assert_whole(&p, &expect, data.len() as u64);
    // The lease is gone: the writer gets in at once.
    let f = OpenOptions::new().write(true).open(&p).unwrap();
    f.write_all_at(b"USER", 2 << 20).unwrap();
}

/// B1: a write that landed before the ack (its I/O passed the FSAL's check
/// under the old marker) moved mtime: re-stat after the ack, re-widen, skip.
#[tokio::test]
async fn b1_a_write_landing_before_the_ack_is_never_punched() {
    let Some(e) = env() else { return };
    let data = pattern(4 << 20);
    let p = resident_file(&e.layout, "b1m.segy", &data, 61, 610);
    let fsal = FakeFsal::default();
    let hp = p.clone();
    *fsal.on_ack.lock().unwrap() = Some(Box::new(move || {
        // What a landed write leaves behind (without opening for write,
        // which the lease would catch first).
        let c = sys::cstr(&hp).unwrap();
        let times = [
            libc::timespec {
                tv_sec: 0,
                tv_nsec: libc::UTIME_OMIT,
            },
            libc::timespec {
                tv_sec: 2_000_000_000,
                tv_nsec: 0,
            },
        ];
        // SAFETY: c is NUL-terminated; times has two entries.
        let r = unsafe { libc::utimensat(libc::AT_FDCWD, c.as_ptr(), times.as_ptr(), 0) };
        assert_eq!(r, 0);
    }));
    let out = stub(
        &e.layout,
        &stub_req("b1m.segy", 61, 610, 0),
        &fsal,
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    assert!(
        matches!(
            out,
            StubOutcome::Skipped {
                reason: SkipReason::VersionMismatch,
                ..
            }
        ),
        "{out:?}"
    );
    assert_whole(&p, &data, data.len() as u64);
}

/// B2: an FSAL connecting between the narrowing and the punch may have
/// loaded the old marker: no punch until a redo under a stable epoch.
#[tokio::test]
async fn b2_a_connection_change_after_narrowing_blocks_the_punch() {
    let Some(e) = env() else { return };
    let data = pattern(4 << 20);
    let p = resident_file(&e.layout, "b2.segy", &data, 62, 620);
    let fsal = std::sync::Arc::new(FakeFsal::default());
    let f2 = fsal.clone();
    *fsal.on_ack.lock().unwrap() = Some(Box::new(move || {
        f2.epoch.fetch_add(1, Ordering::SeqCst);
    }));
    let req = stub_req("b2.segy", 62, 620, 0);
    let seq = MarkerSeq::new();
    let out = stub(
        &e.layout,
        &req,
        &*fsal,
        &NoSmbGuard,
        &seq,
        &FileLocks::new(),
    )
    .await;
    assert!(matches!(out, StubOutcome::Deferred { .. }), "{out:?}");
    assert_eq!(std::fs::read(&p).unwrap(), data, "nothing punched");
    assert_eq!(marker_at(&p).resident_bytes, 0, "narrowed is safe");
    let out = stub(
        &e.layout,
        &req,
        &*fsal,
        &NoSmbGuard,
        &seq,
        &FileLocks::new(),
    )
    .await;
    assert_eq!(out, StubOutcome::Stubbed { resident_bytes: 0 });
    assert_eq!(data_extent_end(&p, 0), None);
}

fn spec(file_id: u64, version_id: u64, size: u64) -> PlaceholderSpec {
    PlaceholderSpec {
        file_id,
        version_id,
        size,
        share_id: 1,
        flags: Flags::ONLINE,
        uid: 0,
        gid: 0,
        mode: 0o644,
        mtime_ns: 0,
    }
}

/// S4: a content-version change is fenced; without the fence nothing moves.
#[tokio::test]
async fn s4_replace_content_needs_the_fence() {
    let Some(e) = env() else { return };
    let (p, _data, _t) = stubbed(&e, "s4a.segy", 63, 630, 2 << 20).await;
    let fsal = FakeFsal::default();
    fsal.refuse_fence.store(true, Ordering::SeqCst);
    let (seq, locks) = (MarkerSeq::new(), FileLocks::new());
    let r = omnifile_stub::handshake::replace_content(
        &e.layout,
        Path::new("s4a.segy"),
        &spec(63, 631, 3 << 20),
        &fsal,
        &seq,
        &locks,
    )
    .await
    .unwrap();
    assert_eq!(r, None);
    assert_eq!(marker_at(&p).version_id, 630, "nothing changed");
    assert_eq!(fsal.acked_invalidations.load(Ordering::SeqCst), 0);
    fsal.refuse_fence.store(false, Ordering::SeqCst);
    let r = omnifile_stub::handshake::replace_content(
        &e.layout,
        Path::new("s4a.segy"),
        &spec(63, 631, 3 << 20),
        &fsal,
        &seq,
        &locks,
    )
    .await
    .unwrap();
    assert!(r.is_some());
    let m = marker_at(&p);
    assert_eq!((m.version_id, m.resident_bytes, m.size), (631, 0, 3 << 20));
    assert_eq!(std::fs::metadata(&p).unwrap().len(), 3 << 20);
    assert_eq!(
        (
            fsal.replace_fences.load(Ordering::SeqCst),
            fsal.replace_ends.load(Ordering::SeqCst)
        ),
        (2, 1),
        "the fence that was set is ended"
    );
}

/// S4: an NFS truncate to 0 (the FSAL truncates, then removes the marker)
/// during a version change: the marker is never recreated and the user's
/// file is never resized.
#[tokio::test]
async fn s4_replace_content_never_recreates_a_removed_marker() {
    let Some(e) = env() else { return };
    let data = pattern(2 << 20);
    let p = resident_file(&e.layout, "s4b.segy", &data, 64, 640);
    let fsal = FakeFsal::default();
    let hp = p.clone();
    *fsal.on_ack.lock().unwrap() = Some(Box::new(move || {
        let f = OpenOptions::new()
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(&hp)
            .unwrap();
        f.set_len(0).unwrap();
        xattr::remove(f.as_fd()).unwrap();
        f.write_all_at(b"user", 0).unwrap();
    }));
    let r = omnifile_stub::handshake::replace_content(
        &e.layout,
        Path::new("s4b.segy"),
        &spec(64, 641, 3 << 20),
        &fsal,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await
    .unwrap();
    assert_eq!(r, None);
    let ro = sys::open_ro(&p).unwrap();
    assert!(
        matches!(xattr::read(ro.as_fd()).unwrap(), ReadOutcome::Unmanaged),
        "never recreated"
    );
    assert_eq!(std::fs::read(&p).unwrap(), b"user", "never resized");
}

/// S5: no whole-file CRC, no fill.
#[tokio::test]
async fn s5_a_fill_without_an_expected_crc_is_refused() {
    let Some(e) = env() else { return };
    let (p, data, mut target) = stubbed(&e, "s5a.segy", 65, 650, 1 << 20).await;
    target.crc64nvme = None;
    let fsal = FakeFsal::default();
    let src = MemSource {
        data,
        corrupt_at: None,
    };
    let freq = FillRequest {
        rel: "s5a.segy".into(),
        expect_ino: None,
    };
    let r = fill(
        &e.layout,
        &freq,
        &target,
        &src,
        &fsal,
        &MarkerSeq::new(),
        &FileLocks::new(),
        FillOptions::default(),
    )
    .await;
    assert!(matches!(r, Err(FillError::NoExpectedCrc)), "{r:?}");
    assert_eq!(fsal.fences.load(Ordering::SeqCst), 0);
    assert_eq!(data_extent_end(&p, 0), None, "nothing written");
}

/// A source that records the largest range it was asked for.
struct RecordingSource {
    data: Vec<u8>,
    largest: AtomicU64,
}

impl RangeSource for RecordingSource {
    async fn get_range(&self, _t: &FetchTarget, offset: u64, len: u64) -> io::Result<Vec<u8>> {
        self.largest.fetch_max(len, Ordering::SeqCst);
        Ok(self.data[offset as usize..(offset + len) as usize].to_vec())
    }
}

/// S5: every range is small enough for the service's range CRC, whatever
/// the configured chunk.
#[tokio::test]
async fn s5_ranges_never_exceed_the_verified_size() {
    let Some(e) = env() else { return };
    let (p, data, target) = stubbed(&e, "s5b.segy", 66, 660, 20 << 20).await;
    let src = RecordingSource {
        data: data.clone(),
        largest: AtomicU64::new(0),
    };
    let freq = FillRequest {
        rel: "s5b.segy".into(),
        expect_ino: None,
    };
    let opts = FillOptions {
        chunk: 16 << 20,
        widen_every: 8 << 20,
    };
    fill(
        &e.layout,
        &freq,
        &target,
        &src,
        &FakeFsal::default(),
        &MarkerSeq::new(),
        &FileLocks::new(),
        opts,
    )
    .await
    .unwrap();
    assert_eq!(
        src.largest.load(Ordering::SeqCst),
        omnifile_stub::MAX_VERIFIED_RANGE
    );
    assert_eq!(std::fs::read(&p).unwrap(), data);
}

/// S5: a CRC mismatch narrows back even when the first narrowings are not
/// acknowledged (an FSAL may still cache a widened marker).
#[tokio::test]
async fn s5_a_bad_fill_narrows_back_until_acknowledged() {
    let Some(e) = env() else { return };
    let (p, data, target) = stubbed(&e, "s5c.segy", 67, 670, 6 << 20).await;
    let fsal = FakeFsal::default();
    let src = MemSource {
        data,
        corrupt_at: Some(5 << 20),
    };
    let freq = FillRequest {
        rel: "s5c.segy".into(),
        expect_ino: None,
    };
    let opts = FillOptions {
        chunk: 1 << 20,
        widen_every: 2 << 20,
    };
    // The narrow-back is the fill's only acknowledged invalidation: the
    // first two attempts are not acknowledged.
    fsal.nack_next.store(2, Ordering::SeqCst);
    let r = fill(
        &e.layout,
        &freq,
        &target,
        &src,
        &fsal,
        &MarkerSeq::new(),
        &FileLocks::new(),
        opts,
    )
    .await;
    assert!(matches!(r, Err(FillError::Integrity { .. })), "{r:?}");
    let m = marker_at(&p);
    assert_eq!((m.state, m.resident_bytes), (State::Partial, 0));
    assert_eq!(data_extent_end(&p, 0), None, "punched once acknowledged");
    assert_eq!(
        fsal.nack_next.load(Ordering::SeqCst),
        0,
        "the nacks were used"
    );
}

/// S6: the disk work of a fill (the resident-prefix CRC, writes,
/// `fdatasync`) never stalls the async worker it runs on: on a
/// single-threaded runtime a ticker keeps ticking throughout.
#[test]
fn s6_fill_disk_work_runs_off_the_async_worker() {
    let Some(e) = env() else { return };
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    rt.block_on(async {
        // A 1 GiB resident prefix (CRC'd from disk before the fence) and a
        // short tail to fetch.
        let len = (1usize << 30) + (4 << 20);
        let data = pattern(len);
        let whole_crc = crc(&data);
        let p = resident_file(&e.layout, "s6.segy", &data, 68, 680);
        let req = stub_req("s6.segy", 68, 680, 1 << 30);
        let out = stub(
            &e.layout,
            &req,
            &FakeFsal::default(),
            &NoSmbGuard,
            &MarkerSeq::new(),
            &FileLocks::new(),
        )
        .await;
        assert_eq!(
            out,
            StubOutcome::Stubbed {
                resident_bytes: 1 << 30
            }
        );
        let target = FetchTarget {
            file_id: 68,
            version_id: 680,
            url: "mem://".into(),
            source: omnifile_stub::SourceKind::AzureSas,
            size: len as u64,
            crc64nvme: Some(whole_crc),
        };
        // Drop the page cache so the prefix CRC really reads the disk.
        let _ = std::fs::write("/proc/sys/vm/drop_caches", "1");
        let src = MemSource {
            data,
            corrupt_at: None,
        };
        let done = AtomicBool::new(false);
        let worst = AtomicU64::new(0);
        let ticker = async {
            let mut last = std::time::Instant::now();
            while !done.load(Ordering::SeqCst) {
                tokio::time::sleep(std::time::Duration::from_millis(2)).await;
                let gap = last.elapsed().as_millis() as u64;
                worst.fetch_max(gap, Ordering::SeqCst);
                last = std::time::Instant::now();
            }
        };
        let filler = async {
            let r = fill(
                &e.layout,
                &FillRequest {
                    rel: "s6.segy".into(),
                    expect_ino: None,
                },
                &target,
                &src,
                &FakeFsal::default(),
                &MarkerSeq::new(),
                &FileLocks::new(),
                FillOptions::default(),
            )
            .await;
            done.store(true, Ordering::SeqCst);
            r
        };
        // The ticker is polled first, so its clock starts before the fill.
        let ((), r) = tokio::join!(ticker, filler);
        assert_eq!(r.unwrap(), len as u64);
        let worst = worst.load(Ordering::SeqCst);
        assert!(worst < 100, "the async worker stalled for {worst} ms");
        let _ = std::fs::remove_file(&p);
    });
}

/// S12: a content-version change of an empty RESIDENT file: the marker on
/// disk is PARTIAL (acknowledged) before the file is resized, so an
/// uncached reader never sees the new size under an all-local marker.
#[tokio::test]
async fn s12_replace_narrows_to_partial_before_resizing() {
    let Some(e) = env() else { return };
    let p = resident_file(&e.layout, "s12.segy", b"", 69, 690);
    let fsal = FakeFsal::default();
    let seen = std::sync::Arc::new(Mutex::new(None));
    let (hp, hs) = (p.clone(), seen.clone());
    *fsal.on_ack.lock().unwrap() = Some(Box::new(move || {
        let m = marker_at(&hp);
        let len = std::fs::metadata(&hp).unwrap().len();
        *hs.lock().unwrap() = Some((m.state, len));
    }));
    let r = omnifile_stub::handshake::replace_content(
        &e.layout,
        Path::new("s12.segy"),
        &spec(69, 691, 5 << 20),
        &fsal,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await
    .unwrap();
    assert!(r.is_some());
    assert_eq!(
        *seen.lock().unwrap(),
        Some((State::Partial, 0)),
        "acknowledged as PARTIAL while still the old size"
    );
    let m = marker_at(&p);
    assert_eq!(
        (m.state, m.version_id, m.size),
        (State::Partial, 691, 5 << 20)
    );
    assert_eq!(std::fs::metadata(&p).unwrap().len(), 5 << 20);
}

/// Path safety: a directory under the share swapped for a symlink to a
/// place outside the data volume never redirects a stub, a fill or a
/// content-version change there (all resolve below the data root without
/// following symlinks).
#[tokio::test]
async fn a_dir_swapped_for_a_symlink_never_redirects_stub_fill_or_replace() {
    let Some(e) = env() else { return };
    let base =
        std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/omnifile/data/test".into());
    let outside = tempfile::tempdir_in(&base).unwrap();
    let out_layout = Layout::new(outside.path());
    // Outside: a resident file and a placeholder whose markers match the
    // requests exactly, so only path resolution can stop the operation.
    let data = pattern(2 << 20);
    std::fs::create_dir_all(outside.path().join("d")).unwrap();
    let victim = resident_file(&out_layout, "d/v.segy", &data, 80, 800);
    let (vp, _vdata, mut target) = {
        let p = resident_file(&out_layout, "d/p.segy", &data, 81, 810);
        let r = stub(
            &out_layout,
            &stub_req("d/p.segy", 81, 810, 0),
            &FakeFsal::default(),
            &NoSmbGuard,
            &MarkerSeq::new(),
            &FileLocks::new(),
        )
        .await;
        assert_eq!(r, StubOutcome::Stubbed { resident_bytes: 0 });
        let t = FetchTarget {
            file_id: 81,
            version_id: 810,
            url: "mem://".into(),
            source: omnifile_stub::SourceKind::AzureSas,
            size: data.len() as u64,
            crc64nvme: Some(crc(&data)),
        };
        (p, data.clone(), t)
    };
    // Inside the share: `d` is a symlink to the outside directory.
    std::os::unix::fs::symlink(outside.path().join("d"), e.layout.path(Path::new("d"))).unwrap();
    let fsal = FakeFsal::default();
    let out = stub(
        &e.layout,
        &stub_req("d/v.segy", 80, 800, 0),
        &fsal,
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    assert!(
        !matches!(out, StubOutcome::Stubbed { .. }),
        "stubbed through a symlink: {out:?}"
    );
    assert_whole(&victim, &data, data.len() as u64);
    target.size = data.len() as u64;
    let r = fill(
        &e.layout,
        &FillRequest {
            rel: "d/p.segy".into(),
            expect_ino: None,
        },
        &target,
        &MemSource {
            data: data.clone(),
            corrupt_at: None,
        },
        &fsal,
        &MarkerSeq::new(),
        &FileLocks::new(),
        FillOptions::default(),
    )
    .await;
    assert!(r.is_err(), "filled through a symlink");
    assert_eq!(data_extent_end(&vp, 0), None, "nothing written outside");
    let r = omnifile_stub::handshake::replace_content(
        &e.layout,
        Path::new("d/v.segy"),
        &spec(80, 801, 1),
        &fsal,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    assert!(
        r.is_err() || r.unwrap().is_none(),
        "replaced through a symlink"
    );
    assert_whole(&victim, &data, data.len() as u64);
    assert_eq!(fsal.acked_invalidations.load(Ordering::SeqCst), 0);
}

/// A content-version change dropped mid-way (its task aborted while it
/// waits for acks) still ends the old version's FETCHING fence.
#[tokio::test]
async fn an_aborted_replace_still_ends_its_fence() {
    let Some(e) = env() else { return };
    let data = pattern(1 << 20);
    resident_file(&e.layout, "abort.segy", &data, 82, 820);
    let fsal = FakeFsal::default();
    fsal.ack_delay_ms.store(5_000, Ordering::SeqCst);
    let r = tokio::time::timeout(
        std::time::Duration::from_millis(200),
        omnifile_stub::handshake::replace_content(
            &e.layout,
            Path::new("abort.segy"),
            &spec(82, 821, 1 << 20),
            &fsal,
            &MarkerSeq::new(),
            &FileLocks::new(),
        ),
    )
    .await;
    assert!(r.is_err(), "the replace was dropped mid-way");
    assert_eq!(
        (
            fsal.replace_fences.load(Ordering::SeqCst),
            fsal.replace_ends.load(Ordering::SeqCst)
        ),
        (1, 1),
        "the fence was ended"
    );
}

/// HIGH: a flag sync (tier change) never runs while a stub holds the file,
/// and never recreates a marker the FSAL removed.
#[tokio::test]
async fn flag_sync_takes_the_file_lock_and_never_recreates_a_marker() {
    let Some(e) = env() else { return };
    let fsal = FakeFsal::default();
    let seq = MarkerSeq::new();
    let mut r = Replica::open(&e.layout.data_root.join("replica.db")).unwrap();
    let locks = FileLocks::new();
    let m = Materializer {
        layout: &e.layout,
        notifier: &fsal,
        seq: &seq,
        locks: &locks,
        perms: &omnifile_stub::materialize::ModeOnly,
    };
    r.apply_page(
        &[
            ev_share(),
            ev_file("file.upsert", 100, None, "", "dir", 1, 1),
            ev_file("file.upsert", 401, Some(100), "f.segy", "file", 1, 4010),
        ],
        "1.0",
    )
    .unwrap();
    run_redo(&mut r, &m, 100, 10).await.unwrap();
    let p = e.layout.path(Path::new("projects/f.segy"));
    let before = marker_at(&p);
    assert!(before.flags.contains(Flags::ARCHIVED));
    let restored = |ev: u64| {
        let mut f = ev_file("file.upsert", 401, Some(100), "f.segy", "file", ev, 4010);
        f["file"]["location"]["archived"] = json!(false);
        f["file"]["location"]["restore_state"] = json!("not_needed");
        f
    };
    // A stub (or fill) holds the file: the flag change waits.
    {
        let _held = locks.try_lock(401).unwrap();
        r.apply_page(&[restored(2)], "2.0").unwrap();
        let st = run_redo(&mut r, &m, 100, 1).await.unwrap();
        assert_eq!(st.retried, 1, "{st:?}");
        assert_eq!(marker_at(&p), before, "untouched while locked");
    }
    // The FSAL removed the marker (a truncate to 0): never recreated.
    {
        let f = OpenOptions::new().write(true).open(&p).unwrap();
        f.set_len(0).unwrap();
        xattr::remove(f.as_fd()).unwrap();
    }
    run_redo(&mut r, &m, 100, 10).await.unwrap();
    let ro = sys::open_ro(&p).unwrap();
    assert!(
        matches!(xattr::read(ro.as_fd()).unwrap(), ReadOutcome::Unmanaged),
        "never recreated"
    );
}

/// `.omni` (the staging parent) planted as a symlink never redirects
/// placeholder staging: the create fails and nothing appears outside.
#[tokio::test]
async fn a_symlinked_omni_dir_is_never_followed() {
    let Some(e) = env() else { return };
    let base =
        std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/omnifile/data/test".into());
    let outside = tempfile::tempdir_in(&base).unwrap();
    std::os::unix::fs::symlink(outside.path(), e.layout.data_root.join(".omni")).unwrap();
    let r = omnifile_stub::placeholder::create_placeholder(
        &e.layout,
        Path::new("x.segy"),
        &spec(90, 900, 1 << 20),
        &MarkerSeq::new(),
    );
    assert!(r.is_err(), "{r:?}");
    assert_eq!(std::fs::read_dir(outside.path()).unwrap().count(), 0);
    assert!(!e.layout.path(Path::new("x.segy")).exists());
}

/// fanotify pre-content ranges are page-granular: an unaligned resident
/// size is rounded up to a whole page (never past the size), so a read of
/// the last resident page is local. The bytes kept are all intact.
#[tokio::test]
async fn an_unaligned_resident_size_keeps_whole_pages() {
    let Some(e) = env() else { return };
    let data = pattern(4 << 20);
    let p = resident_file(&e.layout, "pg.segy", &data, 91, 910);
    let out = stub(
        &e.layout,
        &stub_req("pg.segy", 91, 910, (1 << 20) + 3600),
        &FakeFsal::default(),
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    let StubOutcome::Stubbed { resident_bytes } = out else {
        panic!("{out:?}")
    };
    assert!(resident_bytes.is_multiple_of(4096), "{resident_bytes}");
    assert!(((1 << 20) + 3600..(1 << 20) + 3600 + 65536).contains(&resident_bytes));
    let m = marker_at(&p);
    assert_eq!(
        (m.state, m.resident_bytes),
        (State::Partial, resident_bytes)
    );
    let mut head = vec![0u8; resident_bytes as usize];
    File::open(&p).unwrap().read_exact(&mut head).unwrap();
    assert_eq!(
        head,
        &data[..resident_bytes as usize],
        "the whole kept page is real data"
    );
    assert_eq!(data_extent_end(&p, resident_bytes), None, "holes after it");
    // Near the end: never past the size.
    let small = pattern(10_000);
    let q = resident_file(&e.layout, "pg2.segy", &small, 92, 920);
    let out = stub(
        &e.layout,
        &stub_req("pg2.segy", 92, 920, 9_000),
        &FakeFsal::default(),
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    assert!(
        matches!(out, StubOutcome::Stubbed { resident_bytes: r } if r <= 10_000),
        "{out:?}"
    );
    assert_eq!(std::fs::read(&q).unwrap(), small);
}

/// M2 S2: every narrowing (a content change and a fill's narrow-back, not
/// only a stub) removes the guard's ignore mark first.
#[tokio::test]
async fn every_narrowing_removes_the_guards_ignore_mark_first() {
    let Some(e) = env() else { return };
    // A content change.
    let data = pattern(1 << 20);
    resident_file(&e.layout, "s2a.segy", &data, 93, 930);
    let fsal = FakeFsal::default();
    let p = e.layout.path(Path::new("s2a.segy"));
    let order = std::sync::Arc::new(Mutex::new(Vec::<&'static str>::new()));
    let (o2, hp) = (order.clone(), p.clone());
    *fsal.on_before_narrow.lock().unwrap() = Some(Box::new(move || {
        // Called before the narrowed marker is written: still the old one.
        assert_eq!(marker_at(&hp).state, State::Resident);
        o2.lock().unwrap().push("before_narrow");
    }));
    omnifile_stub::handshake::replace_content(
        &e.layout,
        Path::new("s2a.segy"),
        &spec(93, 931, 1 << 20),
        &fsal,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(*order.lock().unwrap(), vec!["before_narrow"]);
    // A fill whose CRC fails narrows back.
    let (_p2, data2, target) = stubbed(&e, "s2b.segy", 94, 940, 2 << 20).await;
    let fsal = FakeFsal::default();
    let n = std::sync::Arc::new(AtomicUsize::new(0));
    let n2 = n.clone();
    *fsal.on_before_narrow.lock().unwrap() = Some(Box::new(move || {
        n2.fetch_add(1, Ordering::SeqCst);
    }));
    let r = fill(
        &e.layout,
        &FillRequest {
            rel: "s2b.segy".into(),
            expect_ino: None,
        },
        &target,
        &MemSource {
            data: data2,
            corrupt_at: Some(1 << 20),
        },
        &fsal,
        &MarkerSeq::new(),
        &FileLocks::new(),
        FillOptions {
            chunk: 1 << 20,
            widen_every: 1 << 20,
        },
    )
    .await;
    assert!(matches!(r, Err(FillError::Integrity { .. })), "{r:?}");
    assert_eq!(
        n.load(Ordering::SeqCst),
        1,
        "the narrow-back removed the mark"
    );
}

/// M2 B2: a write by someone else during a fill (an SMB write to the
/// resident prefix, which the guard lets through) means the filled file is
/// not the content version: the fill reports no ctime to record.
#[tokio::test]
async fn a_write_during_the_fill_is_never_recorded_as_the_version() {
    let Some(e) = env() else { return };
    let (p, data, target) = stubbed(&e, "b2w.segy", 95, 950, 8 << 20).await;
    let src = SlowSource {
        data: data.clone(),
        served: AtomicUsize::new(0),
    };
    let freq = FillRequest {
        rel: "b2w.segy".into(),
        expect_ino: None,
    };
    let (fsal, seq, locks) = (FakeFsal::default(), MarkerSeq::new(), FileLocks::new());
    let racer = async {
        when(|| src.served.load(Ordering::SeqCst) >= 3).await;
        // A user write into the already-filled (resident) range.
        let f = OpenOptions::new().write(true).open(&p).unwrap();
        f.write_all_at(b"USER", 4096).unwrap();
    };
    let (r, ()) = tokio::join!(
        omnifile_stub::fill::fill_tracked(
            &e.layout, &freq, &target, &src, &fsal, &seq, &locks, SLOW
        ),
        racer
    );
    let done = r.unwrap();
    assert_eq!(done.size, 8 << 20);
    assert_eq!(
        done.ctime_ns, None,
        "the user's write must not be recorded as the version"
    );
    // Without a foreign write, the fill reports the ctime it left.
    let (q, data2, target2) = stubbed(&e, "b2c.segy", 96, 960, 2 << 20).await;
    let done = omnifile_stub::fill::fill_tracked(
        &e.layout,
        &FillRequest {
            rel: "b2c.segy".into(),
            expect_ino: None,
        },
        &target2,
        &MemSource {
            data: data2,
            corrupt_at: None,
        },
        &FakeFsal::default(),
        &MarkerSeq::new(),
        &FileLocks::new(),
        FillOptions::default(),
    )
    .await
    .unwrap();
    let st = std::fs::metadata(&q).unwrap();
    assert_eq!(
        done.ctime_ns,
        Some(st.ctime() * 1_000_000_000 + st.ctime_nsec())
    );
}

/// M2 S3: a stub an O_APPEND write grew past its marker is repaired: the
/// remote range is fetched, verified, copied in; the marker is RESIDENT;
/// the user's appended bytes stay; the result is never recorded as the
/// content version.
#[tokio::test]
async fn an_extended_stub_is_repaired_without_touching_the_appended_bytes() {
    let Some(e) = env() else { return };
    let data = pattern(4 << 20);
    let p = resident_file(&e.layout, "ext.segy", &data, 97, 970);
    let out = stub(
        &e.layout,
        &stub_req("ext.segy", 97, 970, 1 << 20),
        &FakeFsal::default(),
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    assert_eq!(
        out,
        StubOutcome::Stubbed {
            resident_bytes: 1 << 20
        }
    );
    // The append the guard could not hold.
    {
        let mut f = OpenOptions::new().append(true).open(&p).unwrap();
        f.write_all(b"APPENDED TAIL").unwrap();
    }
    let req = FillRequest {
        rel: "ext.segy".into(),
        expect_ino: None,
    };
    assert!(omnifile_stub::fill::is_extended(&e.layout, &req).unwrap());
    let target = FetchTarget {
        file_id: 97,
        version_id: 970,
        url: "mem://".into(),
        source: omnifile_stub::SourceKind::AzureSas,
        size: data.len() as u64,
        crc64nvme: Some(crc(&data)),
    };
    // A plain fill refuses it.
    let r = fill(
        &e.layout,
        &req,
        &target,
        &MemSource {
            data: data.clone(),
            corrupt_at: None,
        },
        &FakeFsal::default(),
        &MarkerSeq::new(),
        &FileLocks::new(),
        FillOptions::default(),
    )
    .await;
    assert!(r.is_err());
    let done = omnifile_stub::fill::repair_extended(
        &e.layout,
        &req,
        &target,
        &MemSource {
            data: data.clone(),
            corrupt_at: None,
        },
        &FakeFsal::default(),
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await
    .unwrap();
    assert_eq!(done.ctime_ns, None);
    let mut want = data.clone();
    want.extend_from_slice(b"APPENDED TAIL");
    assert_eq!(std::fs::read(&p).unwrap(), want);
    let m = marker_at(&p);
    assert_eq!(
        (m.state, m.resident_bytes, m.size),
        (State::Resident, 4 << 20, 4 << 20)
    );
}

/// M2 S3: if the missing range holds data (not an append: the user wrote
/// there), or the prefix is not the version's, the repair touches nothing.
#[tokio::test]
async fn an_extended_stub_with_user_data_in_the_hole_is_left_alone() {
    let Some(e) = env() else { return };
    let data = pattern(4 << 20);
    let p = resident_file(&e.layout, "ext2.segy", &data, 98, 980);
    stub(
        &e.layout,
        &stub_req("ext2.segy", 98, 980, 1 << 20),
        &FakeFsal::default(),
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    {
        let f = OpenOptions::new().write(true).open(&p).unwrap();
        f.write_all_at(b"USER IN THE HOLE", 2 << 20).unwrap();
        f.write_all_at(b"TAIL", 4 << 20).unwrap();
    }
    let before = std::fs::read(&p).unwrap();
    let req = FillRequest {
        rel: "ext2.segy".into(),
        expect_ino: None,
    };
    let target = FetchTarget {
        file_id: 98,
        version_id: 980,
        url: "mem://".into(),
        source: omnifile_stub::SourceKind::AzureSas,
        size: data.len() as u64,
        crc64nvme: Some(crc(&data)),
    };
    let r = omnifile_stub::fill::repair_extended(
        &e.layout,
        &req,
        &target,
        &MemSource {
            data: data.clone(),
            corrupt_at: None,
        },
        &FakeFsal::default(),
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    assert!(r.is_err(), "{r:?}");
    assert_eq!(std::fs::read(&p).unwrap(), before, "nothing written");
    assert_eq!(marker_at(&p).state, State::Partial);
    // A prefix that is not the version's: the CRC fails before any write.
    let q = resident_file(&e.layout, "ext3.segy", &data, 99, 990);
    stub(
        &e.layout,
        &stub_req("ext3.segy", 99, 990, 1 << 20),
        &FakeFsal::default(),
        &NoSmbGuard,
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    {
        let f = OpenOptions::new().write(true).open(&q).unwrap();
        f.write_all_at(b"REWRITTEN", 0).unwrap();
        f.write_all_at(b"TAIL", 4 << 20).unwrap();
    }
    let before = std::fs::read(&q).unwrap();
    let r = omnifile_stub::fill::repair_extended(
        &e.layout,
        &FillRequest {
            rel: "ext3.segy".into(),
            expect_ino: None,
        },
        &FetchTarget {
            file_id: 99,
            version_id: 990,
            ..target.clone()
        },
        &MemSource {
            data,
            corrupt_at: None,
        },
        &FakeFsal::default(),
        &MarkerSeq::new(),
        &FileLocks::new(),
    )
    .await;
    assert!(matches!(r, Err(FillError::Integrity { .. })), "{r:?}");
    assert_eq!(std::fs::read(&q).unwrap(), before);
}

/// An S3 source that refuses every range from `fail_from` on (an expired
/// URL), and records the offsets it was asked for.
struct ExpiringSource {
    data: Vec<u8>,
    fail_from: Option<u64>,
    asked: Mutex<Vec<u64>>,
}

impl RangeSource for ExpiringSource {
    async fn get_range(&self, _t: &FetchTarget, offset: u64, len: u64) -> io::Result<Vec<u8>> {
        self.asked.lock().unwrap().push(offset);
        if self.fail_from.is_some_and(|f| offset >= f) {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "403"));
        }
        Ok(self.data[offset as usize..(offset + len) as usize].to_vec())
    }
}

/// Review S3: an S3 fill that fails part-way (its URL expired) resumes
/// where it got to instead of from zero, still exposing nothing until the
/// whole-file CRC matches; bad bytes left on disk are caught by that CRC.
#[tokio::test]
async fn an_s3_fill_resumes_where_a_failed_attempt_got_to() {
    let Some(e) = env() else { return };
    for (name, tamper) in [("s3resume.segy", false), ("s3tamper.segy", true)] {
        let data = pattern(6 << 20);
        let p = resident_file(&e.layout, name, &data, 27, 270);
        let fsal = FakeFsal::default();
        let seq = MarkerSeq::new();
        let req = StubRequest {
            rel: name.into(),
            file_id: 27,
            version_id: 270,
            resident_bytes: 0,
            expect_ino: None,
            expect_ctime_ns: None,
        };
        stub(&e.layout, &req, &fsal, &NoSmbGuard, &seq, &FileLocks::new()).await;
        let target = FetchTarget {
            file_id: 27,
            version_id: 270,
            url: "mem://".into(),
            source: omnifile_stub::SourceKind::S3Presigned,
            size: 6 << 20,
            crc64nvme: Some(crc(&data)),
        };
        let opts = FillOptions {
            chunk: 1 << 20,
            widen_every: 1 << 20,
        };
        let freq = FillRequest {
            rel: name.into(),
            expect_ino: None,
        };
        let first = ExpiringSource {
            data: data.clone(),
            fail_from: Some(4 << 20),
            asked: Mutex::new(vec![]),
        };
        let locks = FileLocks::new();
        let r = fill(&e.layout, &freq, &target, &first, &fsal, &seq, &locks, opts).await;
        assert!(matches!(r, Err(FillError::Source(_))), "{r:?}");
        let m = marker_at(&p);
        assert_eq!(
            (m.state, m.resident_bytes),
            (State::Partial, 0),
            "nothing exposed"
        );
        assert!(fsal.progress.lock().unwrap().is_empty());
        if tamper {
            // Bytes an earlier attempt left are not trusted: a bad one fails
            // the whole-file CRC.
            let f = OpenOptions::new().write(true).open(&p).unwrap();
            f.write_all_at(&[0xFF ^ data[100]], 100).unwrap();
        }
        let second = ExpiringSource {
            data: data.clone(),
            fail_from: None,
            asked: Mutex::new(vec![]),
        };
        let r = fill(
            &e.layout, &freq, &target, &second, &fsal, &seq, &locks, opts,
        )
        .await;
        let asked = second.asked.lock().unwrap().clone();
        assert_eq!(asked.first(), Some(&(4 << 20)), "resumed: {asked:?}");
        let m = marker_at(&p);
        let f = File::open(&p).unwrap();
        let left = xattr::get_raw(f.as_fd(), "trusted.omnifile.fetched", 16).unwrap();
        assert_eq!(left, None, "the record is gone either way");
        if tamper {
            assert!(matches!(r, Err(FillError::Integrity { .. })), "{r:?}");
            assert_eq!((m.state, m.resident_bytes), (State::Partial, 0));
            assert_eq!(data_extent_end(&p, 0), None, "punched");
        } else {
            assert_eq!(r.unwrap(), data.len() as u64);
            assert_eq!((m.state, m.resident_bytes), (State::Resident, 6 << 20));
            assert_eq!(std::fs::read(&p).unwrap(), data);
        }
    }
}

/// Bytes of a large synthetic file, made on demand from the offset.
struct GenSource;

fn gen_byte(i: u64) -> u8 {
    (i.wrapping_mul(7) ^ (i >> 12).wrapping_mul(31)) as u8
}

impl RangeSource for GenSource {
    async fn get_range(&self, _t: &FetchTarget, offset: u64, len: u64) -> io::Result<Vec<u8>> {
        Ok((offset..offset + len).map(gen_byte).collect())
    }
}

/// #62 review, item 4: what the `fdatasync` before every widen costs. Every
/// widen (`fill.rs` `widen`, and the chunk fills' prefix and map widens)
/// syncs the bytes it is about to mark resident, so a writeback failure on
/// a full or thin volume can never leave the marker claiming bytes the disk
/// dropped. This times a 4 GiB fill with a widen every 64 MiB (the default)
/// against one with only the final widen.
///
///   cargo test -p omnifile-stub --test linux_xfs -- --ignored fill_widen_sync_cost --nocapture
#[tokio::test]
#[ignore = "a benchmark: writes 8 GiB"]
async fn fill_widen_sync_cost() {
    let Some(e) = env() else { return };
    let size: u64 = 4 << 30;
    for (name, widen_every) in [("final only", size + 1), ("every 64 MiB", 64u64 << 20)] {
        let rel = format!("bench-{}.segy", widen_every);
        let p = e.layout.path(Path::new(&rel));
        let mut digest = omnifile_integrity::Crc64::new();
        {
            let mut f = File::create(&p).unwrap();
            let mut off = 0u64;
            while off < size {
                let n = (8u64 << 20).min(size - off);
                let buf: Vec<u8> = (off..off + n).map(gen_byte).collect();
                digest.update(&buf);
                f.write_all(&buf).unwrap();
                off += n;
            }
            f.sync_all().unwrap();
        }
        let crc = digest.finalize();
        let f = File::open(&p).unwrap();
        let mut m = Marker::resident(91, 910, size, 1);
        m.flags = Flags::ONLINE;
        xattr::write(f.as_fd(), &m).unwrap();
        drop(f);
        let fsal = FakeFsal::default();
        let seq = MarkerSeq::new();
        let req = StubRequest {
            rel: rel.clone().into(),
            file_id: 91,
            version_id: 910,
            resident_bytes: 4096,
            expect_ino: None,
            expect_ctime_ns: None,
        };
        assert!(matches!(
            stub(&e.layout, &req, &fsal, &NoSmbGuard, &seq, &FileLocks::new()).await,
            StubOutcome::Stubbed { .. }
        ));
        let target = FetchTarget {
            file_id: 91,
            version_id: 910,
            url: "mem://".into(),
            source: omnifile_stub::SourceKind::AzureSas,
            size,
            crc64nvme: Some(crc),
        };
        let opts = FillOptions {
            chunk: 4 << 20,
            widen_every,
        };
        let t0 = std::time::Instant::now();
        let n = fill(
            &e.layout,
            &FillRequest {
                rel: rel.into(),
                expect_ino: None,
            },
            &target,
            &GenSource,
            &fsal,
            &seq,
            &FileLocks::new(),
            opts,
        )
        .await
        .unwrap();
        let s = t0.elapsed().as_secs_f64();
        assert_eq!(n, size);
        let widens = fsal.progress.lock().unwrap().len();
        eprintln!(
            "fill 4 GiB, widen {name}: {s:.2} s ({:.0} MiB/s), {widens} widens",
            4096.0 / s
        );
        std::fs::remove_file(&p).unwrap();
    }
}
