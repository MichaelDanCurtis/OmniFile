//! Chunk fills and chunk eviction on a real xfs volume (Linux, as root).
//! Run inside the dev VM:
//!
//! ```text
//! sudo -E cargo test -p omnifile-stub --test linux_chunks
//! ```
//!
//! `OMNIFILE_TEST_DIR` (default `/srv/omnifile/data/test`) must be on xfs.
//! Without root or xfs the tests print a note and pass vacuously.
#![cfg(target_os = "linux")]

use std::fs::File;
use std::io;
use std::os::fd::AsFd;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};

use omnifile_integrity::crc64_nvme;
use omnifile_marker::xattr::{self, ReadOutcome};
use omnifile_marker::{ChunkMap, Flags, MAP_XATTR_NAME, Marker, State};
use omnifile_stub::chunks::{
    ChunkFill, ChunkNarrow, ChunkSource, ChunkTarget, evict_file, fill_chunks, fill_prefix,
    narrow_chunks, read_state,
};
use omnifile_stub::fill::{FillError, FillRequest};
use omnifile_stub::placeholder::{Created, PlaceholderSpec, create_placeholder};
use omnifile_stub::{FileLocks, FsalNotifier, InodeKey, Layout, MarkerSeq, sys};

const MIB: u64 = 1 << 20;

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
        eprintln!("skipping: {base} is watched by a running smb-guard");
        return None;
    }
    let dir = tempfile::tempdir_in(&base).ok()?;
    let layout = Layout::new(dir.path());
    Some(Env { _dir: dir, layout })
}

#[derive(Default)]
struct FakeFsal {
    nack: AtomicBool,
    refuse_fence: AtomicBool,
    epoch: AtomicU64,
    fences: AtomicUsize,
    invalidations: AtomicUsize,
    acked: AtomicUsize,
    before_narrow: AtomicUsize,
    progress: Mutex<Vec<u64>>,
}

impl FsalNotifier for FakeFsal {
    async fn invalidate(&self, _key: InodeKey, _what: u32, ack: bool) -> bool {
        self.invalidations.fetch_add(1, Ordering::SeqCst);
        if ack {
            self.acked.fetch_add(1, Ordering::SeqCst);
            return !self.nack.load(Ordering::SeqCst);
        }
        true
    }
    fn connection_epoch(&self) -> u64 {
        self.epoch.load(Ordering::SeqCst)
    }
    fn fill_progress(&self, _key: InodeKey, _version_id: u64, resident_bytes: u64) {
        self.progress.lock().unwrap().push(resident_bytes);
    }
    async fn begin_fill(&self, _key: InodeKey, _version_id: u64) -> Option<u64> {
        self.fences.fetch_add(1, Ordering::SeqCst);
        (!self.refuse_fence.load(Ordering::SeqCst)).then(|| self.epoch.load(Ordering::SeqCst))
    }
    fn fence_holds(&self, epoch: u64) -> bool {
        self.epoch.load(Ordering::SeqCst) == epoch
    }
    fn before_narrow(&self, _key: InodeKey) -> io::Result<()> {
        self.before_narrow.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}

/// The remote object's bytes.
fn byte_at(i: u64) -> u8 {
    (i.wrapping_mul(7) + i / 4096) as u8
}

fn content(off: u64, len: u64) -> Vec<u8> {
    (off..off + len).map(byte_at).collect()
}

struct Src {
    corrupt_at: Option<u64>,
    short_at: Option<u64>,
    reads: AtomicUsize,
    /// Changes the remote object's bytes at this offset after this many reads.
    cut_after: Option<usize>,
}

impl Src {
    fn good() -> Self {
        Src {
            corrupt_at: None,
            short_at: None,
            reads: AtomicUsize::new(0),
            cut_after: None,
        }
    }
}

impl ChunkSource for Src {
    async fn read(&self, offset: u64, len: u64) -> io::Result<Vec<u8>> {
        let n = self.reads.fetch_add(1, Ordering::SeqCst);
        if self.cut_after.is_some_and(|c| n >= c) {
            return Err(io::Error::other("connection reset"));
        }
        let mut v = content(offset, len);
        if let Some(c) = self.corrupt_at
            && (offset..offset + len).contains(&c)
        {
            v[(c - offset) as usize] ^= 0xFF;
        }
        if let Some(s) = self.short_at
            && (offset..offset + len).contains(&s)
        {
            v.truncate(v.len() / 2);
        }
        Ok(v)
    }
}

struct Ctx {
    e: Env,
    fsal: FakeFsal,
    seq: MarkerSeq,
    locks: FileLocks,
}

fn ctx() -> Option<Ctx> {
    Some(Ctx {
        e: env()?,
        fsal: FakeFsal::default(),
        seq: MarkerSeq::new(),
        locks: FileLocks::new(),
    })
}

const FILE: u64 = 0x0102_0304_0506_0708;
const VERSION: u64 = 42;

fn remote(c: &Ctx, name: &str, size: u64) -> (PathBuf, FillRequest) {
    let spec = PlaceholderSpec {
        file_id: FILE,
        version_id: VERSION,
        size,
        share_id: 3,
        flags: Flags::ONLINE,
        uid: 0,
        gid: 0,
        mode: 0o644,
        mtime_ns: 1_700_000_000_000_000_000,
    };
    let rel = PathBuf::from(name);
    assert!(matches!(
        create_placeholder(&c.e.layout, &rel, &spec, &c.seq).unwrap(),
        Created::Created(_)
    ));
    (
        rel.clone(),
        FillRequest {
            rel,
            expect_ino: None,
        },
    )
}

fn target(size: u64, crc: bool) -> ChunkTarget {
    ChunkTarget::new(
        FILE,
        VERSION,
        size,
        crc.then(|| crc64_nvme(&content(0, size))),
    )
}

fn state(c: &Ctx, rel: &Path) -> (Marker, Option<ChunkMap>) {
    read_state(&c.e.layout, rel).unwrap().unwrap()
}

fn read_at(c: &Ctx, rel: &Path, off: u64, len: u64) -> Vec<u8> {
    use std::os::unix::fs::FileExt;
    let f = File::open(c.e.layout.path(rel)).unwrap();
    let mut b = vec![0u8; len as usize];
    f.read_exact_at(&mut b, off).unwrap();
    b
}

/// The offsets in `[from, to)` that hold data (`SEEK_DATA`), as a crude
/// "is anything allocated here".
fn has_data(c: &Ctx, rel: &Path, from: u64, to: u64) -> bool {
    let f = File::open(c.e.layout.path(rel)).unwrap();
    sys::first_data_at(f.as_fd(), from)
        .unwrap()
        .is_some_and(|d| d < to)
}

#[tokio::test]
async fn chunks_fill_into_the_map_like_the_golden_vector() {
    let Some(c) = ctx() else { return };
    let size = 10 * MIB;
    let (rel, req) = remote(&c, "seis.segy", size);
    let before = state(&c, &rel).0;
    let r = fill_chunks(
        &c.e.layout,
        &req,
        &target(size, true),
        &[2, 0],
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    let ChunkFill::Filled {
        written,
        bytes,
        marker,
    } = r
    else {
        panic!("{r:?}")
    };
    assert_eq!(written, vec![0, 2]);
    assert_eq!(bytes, 6 * MIB);
    let (m, map) = state(&c, &rel);
    assert_eq!(m, marker);
    assert!(m.marker_seq > before.marker_seq);
    assert_eq!((m.state, m.resident_bytes), (State::Partial, 0));
    assert!(m.flags.contains(Flags::CHUNK_MAP | Flags::ONLINE));
    assert_eq!(m.chunk_shift, 22);
    // The contract's map golden vector: chunks 0 and 2 of a 10 MiB file.
    let map = map.unwrap();
    assert_eq!(map.encode(), &[0x05]);
    // And the marker the fill wrote is the golden one (but for marker_seq).
    let mut g = m;
    g.marker_seq = 8;
    let hex: String = g
        .encode()
        .unwrap()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    assert_eq!(
        hex,
        concat!(
            "4f4d463101021100",
            "0807060504030201",
            "2a00000000000000",
            "0000a00000000000",
            "0000000000000000",
            "0800000000000000",
            "0300000000000000",
            "16000000",
            "a2fb3ce0"
        )
    );
    assert!(m.covers_with_map(Some(&map), 0, 4 * MIB));
    assert!(m.covers_with_map(Some(&map), 8 * MIB, 2 * MIB));
    assert!(!m.covers_with_map(Some(&map), 4 * MIB - 1, 2));
    assert_eq!(read_at(&c, &rel, 0, 4 * MIB), content(0, 4 * MIB));
    assert_eq!(
        read_at(&c, &rel, 8 * MIB, 2 * MIB),
        content(8 * MIB, 2 * MIB)
    );
    assert!(
        !has_data(&c, &rel, 4 * MIB, 8 * MIB),
        "chunk 1 stays a hole"
    );
    assert_eq!(c.fsal.fences.load(Ordering::SeqCst), 1);
    // A second fill of the same chunks writes nothing and needs no fence.
    let r = fill_chunks(
        &c.e.layout,
        &req,
        &target(size, true),
        &[0],
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert!(matches!(r, ChunkFill::Filled { ref written, bytes: 0, .. } if written.is_empty()));
    assert_eq!(c.fsal.fences.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn last_chunk_completes_after_the_whole_file_check() {
    let Some(c) = ctx() else { return };
    let size = 10 * MIB + 123;
    let (rel, req) = remote(&c, "whole.segy", size);
    let t = target(size, true);
    for chunks in [&[0u64, 2][..], &[1][..]] {
        let r = fill_chunks(
            &c.e.layout,
            &req,
            &t,
            chunks,
            &Src::good(),
            &c.fsal,
            &c.seq,
            &c.locks,
        )
        .await
        .unwrap();
        if chunks == [1] {
            let ChunkFill::Complete(d) = r else {
                panic!("{r:?}")
            };
            assert_eq!(d.size, size);
            assert!(d.ctime_ns.is_some(), "nobody else touched it");
        }
    }
    let (m, map) = state(&c, &rel);
    assert_eq!((m.state, m.resident_bytes), (State::Resident, size));
    assert!(!m.flags.contains(Flags::CHUNK_MAP));
    assert_eq!(m.chunk_shift, 0);
    assert!(map.is_none());
    let f = File::open(c.e.layout.path(&rel)).unwrap();
    assert!(
        xattr::get_raw(f.as_fd(), MAP_XATTR_NAME, 16)
            .unwrap()
            .is_none(),
        "map removed"
    );
    assert_eq!(read_at(&c, &rel, 0, size), content(0, size));
    assert_eq!(c.fsal.progress.lock().unwrap().last(), Some(&size));
    // Already resident: nothing to do.
    let r = fill_chunks(
        &c.e.layout,
        &req,
        &t,
        &[0],
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert_eq!(r, ChunkFill::Resident);
}

/// S3 ranges carry no checksum: a corrupted chunk is caught by the
/// whole-file CRC, and the file is narrowed back to nothing local.
#[tokio::test]
async fn corrupt_chunk_never_becomes_resident() {
    let Some(c) = ctx() else { return };
    let size = 12 * MIB;
    let (rel, req) = remote(&c, "bad.segy", size);
    let src = Src {
        corrupt_at: Some(5 * MIB + 17),
        ..Src::good()
    };
    let e = fill_chunks(
        &c.e.layout,
        &req,
        &target(size, true),
        &[0, 1, 2],
        &src,
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap_err();
    assert!(matches!(e, FillError::Integrity { .. }), "{e}");
    let (m, map) = state(&c, &rel);
    assert_eq!((m.state, m.resident_bytes), (State::Partial, 0));
    assert!(!m.flags.contains(Flags::CHUNK_MAP));
    assert!(map.is_none());
    assert!(!has_data(&c, &rel, 0, size), "every chunk punched");
    assert!(
        c.fsal.acked.load(Ordering::SeqCst) >= 1,
        "narrowing was acknowledged"
    );
}

#[tokio::test]
async fn without_a_catalog_crc_the_file_stays_partial() {
    let Some(c) = ctx() else { return };
    let size = 5 * MIB;
    let (rel, req) = remote(&c, "nocrc.segy", size);
    let r = fill_chunks(
        &c.e.layout,
        &req,
        &target(size, false),
        &[0, 1],
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert!(matches!(r, ChunkFill::Filled { .. }), "{r:?}");
    let (m, map) = state(&c, &rel);
    assert_eq!(m.state, State::Partial);
    assert!(
        m.covers_with_map(map.as_ref(), 0, size),
        "every chunk served"
    );
}

/// A short or failed read marks nothing: no bit is set for bytes that
/// didn't fully arrive.
#[tokio::test]
async fn short_or_failed_reads_mark_nothing() {
    let Some(c) = ctx() else { return };
    let size = 12 * MIB;
    let (rel, req) = remote(&c, "short.segy", size);
    let before = state(&c, &rel).0;
    let src = Src {
        short_at: Some(4 * MIB),
        ..Src::good()
    };
    let e = fill_chunks(
        &c.e.layout,
        &req,
        &target(size, true),
        &[0, 1],
        &src,
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap_err();
    assert!(matches!(e, FillError::ShortRead { .. }), "{e}");
    assert_eq!(state(&c, &rel), (before, None), "marker untouched, no map");
    let src = Src {
        cut_after: Some(1),
        ..Src::good()
    };
    let e = fill_chunks(
        &c.e.layout,
        &req,
        &target(size, true),
        &[0, 1],
        &src,
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap_err();
    assert!(matches!(e, FillError::Source(_)), "{e}");
    assert_eq!(state(&c, &rel), (before, None));
}

#[tokio::test]
async fn refused_fence_or_wrong_version_writes_nothing() {
    let Some(c) = ctx() else { return };
    let size = 8 * MIB;
    let (rel, req) = remote(&c, "fence.segy", size);
    c.fsal.refuse_fence.store(true, Ordering::SeqCst);
    let e = fill_chunks(
        &c.e.layout,
        &req,
        &target(size, true),
        &[1],
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap_err();
    assert!(matches!(e, FillError::FenceBroken), "{e}");
    assert!(!has_data(&c, &rel, 0, size));
    c.fsal.refuse_fence.store(false, Ordering::SeqCst);
    let mut t = target(size, true);
    t.version_id = 43;
    let e = fill_chunks(
        &c.e.layout,
        &req,
        &t,
        &[1],
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap_err();
    assert!(matches!(e, FillError::VersionMismatch { .. }), "{e}");
    let _busy = c.locks.try_lock(FILE).unwrap();
    let e = fill_chunks(
        &c.e.layout,
        &req,
        &target(size, true),
        &[1],
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap_err();
    assert!(matches!(e, FillError::Busy), "{e}");
    assert!(!has_data(&c, &rel, 0, size));
}

#[tokio::test]
async fn eviction_narrows_then_punches_only_when_acknowledged() {
    let Some(c) = ctx() else { return };
    let size = 10 * MIB;
    let (rel, req) = remote(&c, "evict.segy", size);
    fill_chunks(
        &c.e.layout,
        &req,
        &target(size, true),
        &[0, 2],
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    // An FSAL that doesn't acknowledge: the bit is cleared, the bytes stay.
    c.fsal.nack.store(true, Ordering::SeqCst);
    let r = narrow_chunks(
        &c.e.layout,
        &rel,
        FILE,
        VERSION,
        &[2],
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert_eq!(r, ChunkNarrow::NotAcked);
    let (m, map) = state(&c, &rel);
    let map = map.unwrap();
    assert_eq!(map.encode(), &[0x01]);
    assert!(!m.covers_with_map(Some(&map), 8 * MIB, 1));
    assert!(
        has_data(&c, &rel, 8 * MIB, size),
        "not punched without an ack"
    );
    assert!(c.fsal.before_narrow.load(Ordering::SeqCst) >= 1);
    // Acknowledged: a retry of the same chunks punches.
    c.fsal.nack.store(false, Ordering::SeqCst);
    let r = narrow_chunks(
        &c.e.layout,
        &rel,
        FILE,
        VERSION,
        &[2],
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert_eq!(r, ChunkNarrow::Punched { freed: 2 * MIB });
    assert!(!has_data(&c, &rel, 4 * MIB, size));
    assert_eq!(
        read_at(&c, &rel, 0, 4 * MIB),
        content(0, 4 * MIB),
        "chunk 0 kept"
    );
    // An FSAL connecting between narrowing and punch: no punch.
    let ep = c.fsal.epoch.load(Ordering::SeqCst);
    struct Bump<'a>(&'a FakeFsal);
    impl FsalNotifier for Bump<'_> {
        async fn invalidate(&self, k: InodeKey, w: u32, ack: bool) -> bool {
            self.0.epoch.fetch_add(1, Ordering::SeqCst);
            self.0.invalidate(k, w, ack).await
        }
        fn connection_epoch(&self) -> u64 {
            self.0.connection_epoch()
        }
        fn fill_progress(&self, k: InodeKey, v: u64, r: u64) {
            self.0.fill_progress(k, v, r)
        }
        async fn begin_fill(&self, k: InodeKey, v: u64) -> Option<u64> {
            self.0.begin_fill(k, v).await
        }
        fn fence_holds(&self, e: u64) -> bool {
            self.0.fence_holds(e)
        }
    }
    let r = narrow_chunks(
        &c.e.layout,
        &rel,
        FILE,
        VERSION,
        &[0],
        &Bump(&c.fsal),
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert_eq!(r, ChunkNarrow::NotAcked);
    assert!(c.fsal.epoch.load(Ordering::SeqCst) > ep);
    assert!(has_data(&c, &rel, 0, 4 * MIB));
    // Busy while another operation holds the file.
    let g = c.locks.try_lock(FILE).unwrap();
    let r = narrow_chunks(
        &c.e.layout,
        &rel,
        FILE,
        VERSION,
        &[0],
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert_eq!(r, ChunkNarrow::Busy);
    drop(g);
    // Another version: nothing done.
    let r = narrow_chunks(
        &c.e.layout,
        &rel,
        FILE,
        VERSION + 1,
        &[0],
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert_eq!(r, ChunkNarrow::Changed);
}

#[tokio::test]
async fn header_prefix_then_evict_everything() {
    let Some(c) = ctx() else { return };
    let size = 64 * MIB;
    let (rel, req) = remote(&c, "header.segy", size);
    let t = target(size, true);
    let r = fill_prefix(
        &c.e.layout,
        &req,
        &t,
        3600,
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    let ChunkFill::Filled { marker, .. } = r else {
        panic!("{r:?}")
    };
    // Rounded up to whole pages / fs blocks, never a partial page.
    assert!(marker.resident_bytes >= 4096 && marker.resident_bytes % 4096 == 0);
    assert!(!marker.flags.contains(Flags::CHUNK_MAP));
    assert_eq!(read_at(&c, &rel, 0, 3600), content(0, 3600));
    // A larger prefix (a v0.1 FSAL reading further) extends it.
    fill_prefix(
        &c.e.layout,
        &req,
        &t,
        6 * MIB + 1,
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    // Chunks past the prefix.
    fill_chunks(
        &c.e.layout,
        &req,
        &t,
        &[1, 10],
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    let (m, map) = state(&c, &rel);
    assert!(m.resident_bytes > 6 * MIB);
    assert!(m.covers_with_map(map.as_ref(), 40 * MIB, 4 * MIB));
    assert_eq!(read_at(&c, &rel, 40 * MIB, MIB), content(40 * MIB, MIB));
    // Evict the whole file, header included.
    let r = evict_file(
        &c.e.layout,
        &rel,
        FILE,
        VERSION,
        0,
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert!(
        matches!(r, ChunkNarrow::Punched { freed } if freed >= m.resident_bytes + 4 * MIB),
        "{r:?}"
    );
    let (m, map) = state(&c, &rel);
    assert_eq!((m.state, m.resident_bytes), (State::Partial, 0));
    assert!(map.is_none());
    assert!(!has_data(&c, &rel, 0, size));
    // The placeholder itself stays: never ENOENT.
    let md = std::fs::metadata(c.e.layout.path(&rel)).unwrap();
    assert_eq!(md.len(), size);
    let f = File::open(c.e.layout.path(&rel)).unwrap();
    assert!(matches!(
        xattr::read(f.as_fd()).unwrap(),
        ReadOutcome::Marker(_)
    ));
}

/// The source changed under the cache (an ETag-pinned read got 412): the
/// fill fails without marking the chunk it was reading, and evicting the
/// file discards every chunk cached from the old object, header included.
#[tokio::test]
async fn changed_source_discards_every_cached_chunk() {
    let Some(c) = ctx() else { return };
    let size = 16 * MIB;
    let (rel, req) = remote(&c, "changed.segy", size);
    let t = target(size, true);
    fill_prefix(
        &c.e.layout,
        &req,
        &t,
        MIB,
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    fill_chunks(
        &c.e.layout,
        &req,
        &t,
        &[1, 3],
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    let before = state(&c, &rel);
    // Every read now fails as "precondition failed".
    let changed = Src {
        cut_after: Some(0),
        ..Src::good()
    };
    let e = fill_chunks(
        &c.e.layout,
        &req,
        &t,
        &[2],
        &changed,
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap_err();
    assert!(matches!(e, FillError::Source(_)), "{e}");
    assert_eq!(
        state(&c, &rel),
        before,
        "nothing marked for the failed chunk"
    );
    assert!(!has_data(&c, &rel, 8 * MIB, 12 * MIB));
    let acked = c.fsal.acked.load(Ordering::SeqCst);
    let r = evict_file(
        &c.e.layout,
        &rel,
        FILE,
        VERSION,
        0,
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert!(
        matches!(r, ChunkNarrow::Punched { freed } if freed >= MIB + 8 * MIB),
        "{r:?}"
    );
    assert!(
        c.fsal.acked.load(Ordering::SeqCst) > acked,
        "acknowledged before the punch"
    );
    let (m, map) = state(&c, &rel);
    assert_eq!((m.state, m.resident_bytes), (State::Partial, 0));
    assert!(!m.flags.contains(Flags::CHUNK_MAP) && map.is_none());
    assert!(
        !has_data(&c, &rel, 0, size),
        "no byte of the old object is left"
    );
}

/// Review S6: a file over 2 TiB gets a larger chunk size, so its map still
/// fits an xattr value, and chunk fills work at any offset.
#[tokio::test]
async fn files_over_2_tib_get_bigger_chunks() {
    use omnifile_stub::chunks::{chunk_shift_for, chunks_for};
    let Some(c) = ctx() else { return };
    let size = 3u64 << 40;
    let (rel, req) = remote(&c, "huge.segy", size);
    let t = target(size, false);
    assert_eq!(t.chunk_shift, 22);
    let shift = chunk_shift_for(size, t.chunk_shift);
    assert_eq!(shift, 23);
    let off = (5u64 << 40) / 2 + 12_345;
    let want: Vec<u64> = chunks_for(size, shift, off, 4096).collect();
    let r = fill_chunks(
        &c.e.layout,
        &req,
        &t,
        &want,
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert!(
        matches!(r, ChunkFill::Filled { ref written, .. } if written == &want),
        "{r:?}"
    );
    let (m, map) = state(&c, &rel);
    let map = map.unwrap();
    assert_eq!((m.chunk_shift, map.shift), (23, 23));
    assert_eq!(map.encode().len(), 49_152, "3 TiB / 8 MiB chunks / 8 bits");
    assert!(m.covers_with_map(Some(&map), off, 4096));
    assert_eq!(read_at(&c, &rel, off, 4096), content(off, 4096));
    // A second fill keeps the file's own shift whatever the caller prefers.
    let mut t2 = t;
    t2.chunk_shift = 22;
    let more: Vec<u64> = chunks_for(size, 23, 1 << 40, 1).collect();
    fill_chunks(
        &c.e.layout,
        &req,
        &t2,
        &more,
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert_eq!(state(&c, &rel).0.chunk_shift, 23);
    assert_eq!(read_at(&c, &rel, 1 << 40, 100), content(1 << 40, 100));
}

/// Line A: a stub's resident prefix is the catalog's (the stubber kept the
/// headers under a lease). Evicting the file's cached bytes narrows to that
/// floor, never below it, and punches only what the cache added.
#[tokio::test]
async fn evicting_a_stub_keeps_the_catalogs_prefix() {
    let Some(c) = ctx() else { return };
    let size = 64 * MIB;
    let (rel, req) = remote(&c, "stubbed.segy", size);
    let t = target(size, false);
    // The stubber's prefix, then chunks the cache fetched above it.
    fill_prefix(
        &c.e.layout,
        &req,
        &t,
        2 * MIB,
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    let floor = state(&c, &rel).0.resident_bytes;
    assert!(floor >= 2 * MIB);
    fill_chunks(
        &c.e.layout,
        &req,
        &t,
        &[3, 9],
        &Src::good(),
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    let r = evict_file(
        &c.e.layout,
        &rel,
        FILE,
        VERSION,
        floor,
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert_eq!(r, ChunkNarrow::Punched { freed: 8 * MIB }, "the two chunks");
    let (m, map) = state(&c, &rel);
    assert_eq!((m.state, m.resident_bytes), (State::Partial, floor));
    assert!(map.is_none() && !m.flags.contains(Flags::CHUNK_MAP));
    assert_eq!(read_at(&c, &rel, 0, floor), content(0, floor));
    assert!(!has_data(&c, &rel, floor, size));
    // Nothing above the floor any more: a second eviction frees nothing.
    let r = evict_file(
        &c.e.layout,
        &rel,
        FILE,
        VERSION,
        floor,
        &c.fsal,
        &c.seq,
        &c.locks,
    )
    .await
    .unwrap();
    assert_eq!(r, ChunkNarrow::Punched { freed: 0 });
    assert_eq!(state(&c, &rel).0.resident_bytes, floor);
}
