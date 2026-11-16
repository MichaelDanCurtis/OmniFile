//! The cache on a real xfs volume (Linux, as root), over an in-memory
//! remote object: fetch by chunk, the header cache, read-ahead, the budget
//! and LRU eviction (headers last), statistics and egress, the
//! unsupported/archived errors, the whole-file check, and rebuilding the
//! index from disk.
//!
//! ```text
//! sudo -E cargo test -p omnifile-cache --test linux_cache
//! ```
#![cfg(target_os = "linux")]

use std::fs::File;
use std::os::fd::AsFd;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bytes::Bytes;
use omnifile_blob::{BlobError, RangeReader, ReadTarget};
use omnifile_cache::{Cache, CacheConfig, FetchOutcome, FetchRequest};
use omnifile_integrity::crc64_nvme;
use omnifile_marker::{Flags, State};
use omnifile_stub::chunks::read_state;
use omnifile_stub::placeholder::{PlaceholderSpec, create_placeholder};
use omnifile_stub::{FileLocks, FsalNotifier, InodeKey, Layout, MarkerSeq, sys};

const MIB: u64 = 1 << 20;

fn layout() -> Option<(tempfile::TempDir, Layout)> {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        eprintln!("skipping: needs root for trusted.* xattrs");
        return None;
    }
    let base =
        std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/omnifile/data/test".into());
    std::fs::create_dir_all(&base).ok()?;
    let d = File::open(&base).ok()?;
    if !sys::is_xfs(d.as_fd()).unwrap_or(false)
        || sys::fanotify_guarded(Path::new(&base)).unwrap_or(false)
    {
        eprintln!("skipping: {base} is not an unguarded xfs");
        return None;
    }
    let dir = tempfile::tempdir_in(&base).ok()?;
    let l = Layout::new(dir.path());
    Some((dir, l))
}

#[derive(Default)]
struct Fsal {
    epoch: AtomicU64,
}

impl FsalNotifier for Fsal {
    async fn invalidate(&self, _k: InodeKey, _w: u32, _ack: bool) -> bool {
        true
    }
    fn fill_progress(&self, _k: InodeKey, _v: u64, _r: u64) {}
    async fn begin_fill(&self, _k: InodeKey, _v: u64) -> Option<u64> {
        Some(self.epoch.load(Ordering::SeqCst))
    }
    fn fence_holds(&self, e: u64) -> bool {
        self.epoch.load(Ordering::SeqCst) == e
    }
}

fn byte_at(i: u64) -> u8 {
    (i.wrapping_mul(13) ^ (i >> 12)) as u8
}

fn content(off: u64, len: u64) -> Vec<u8> {
    (off..off + len).map(byte_at).collect()
}

/// An in-memory remote object behind every S3 target; records each read.
#[derive(Default)]
struct Remote {
    reads: Mutex<Vec<(u64, u64)>>,
    archived: bool,
    /// After this many reads the source "changes": reads answer 412.
    changes_after: Option<usize>,
}

impl RangeReader for Remote {
    async fn read_range(&self, t: &ReadTarget, offset: u64, len: u64) -> Result<Bytes, BlobError> {
        if let ReadTarget::Unsupported { mode } = t {
            return Err(BlobError::Unsupported(mode.clone()));
        }
        if self.archived {
            return Err(BlobError::Http {
                op: "S3 GetObject",
                status: 403,
                code: Some("InvalidObjectState".into()),
                message: "archived".into(),
                request_id: None,
                retry_after: None,
            });
        }
        if self
            .changes_after
            .is_some_and(|n| self.reads.lock().unwrap().len() >= n)
        {
            return Err(BlobError::Http {
                op: "S3 GetObject",
                status: 412,
                code: Some("PreconditionFailed".into()),
                message: "At least one of the pre-conditions you specified did not hold".into(),
                request_id: None,
                retry_after: None,
            });
        }
        self.reads.lock().unwrap().push((offset, len));
        Ok(Bytes::from(content(offset, len)))
    }
}

fn s3() -> ReadTarget {
    ReadTarget::S3(omnifile_blob::S3Object {
        bucket: "b".into(),
        key: "k".into(),
        version_id: Some("v".into()),
        region: "eu-west-1".into(),
        endpoint: "vpce-1.s3.eu-west-1.vpce.amazonaws.com".into(),
        addressing: omnifile_blob::Addressing::VirtualHost,
        tls12_only: true,
        requester_pays: false,
        etag: None,
        private_network: true,
    })
}

struct T {
    _dir: tempfile::TempDir,
    layout: Layout,
    remote: Arc<Remote>,
    cache: Cache<Remote, Fsal>,
    seq: Arc<MarkerSeq>,
}

fn setup(cfg: CacheConfig, remote: Remote) -> Option<T> {
    let (dir, layout) = layout()?;
    let remote = Arc::new(remote);
    let seq = Arc::new(MarkerSeq::new());
    let cache = Cache::new(
        cfg,
        layout.clone(),
        remote.clone(),
        Arc::new(Fsal::default()),
        seq.clone(),
        Arc::new(FileLocks::new()),
    );
    Some(T {
        _dir: dir,
        layout,
        remote,
        cache,
        seq,
    })
}

fn placeholder(t: &T, file_id: u64, size: u64) -> PathBuf {
    let rel = PathBuf::from(format!("f{file_id}.segy"));
    create_placeholder(
        &t.layout,
        &rel,
        &PlaceholderSpec {
            file_id,
            version_id: 7,
            size,
            share_id: 1,
            flags: Flags::ONLINE,
            uid: 0,
            gid: 0,
            mode: 0o644,
            mtime_ns: 0,
        },
        &t.seq,
    )
    .unwrap();
    rel
}

fn req(rel: &Path, file_id: u64, size: u64, offset: u64, len: u64) -> FetchRequest {
    FetchRequest {
        rel: rel.to_owned(),
        expect_ino: None,
        file_id,
        version_id: 7,
        size,
        // Computed only where a test completes a file (`with_crc`).
        crc64nvme: None,
        backend_id: 9,
        target: s3(),
        offset,
        len,
        header_bytes: 0,
        floor: 0,
    }
}

fn with_crc(mut r: FetchRequest) -> FetchRequest {
    r.crc64nvme = Some(crc64_nvme(&content(0, r.size)));
    r
}

fn read_at(t: &T, rel: &Path, off: u64, len: u64) -> Vec<u8> {
    use std::os::unix::fs::FileExt;
    let f = File::open(t.layout.path(rel)).unwrap();
    let mut b = vec![0u8; len as usize];
    f.read_exact_at(&mut b, off).unwrap();
    b
}

fn no_readahead() -> CacheConfig {
    CacheConfig {
        readahead_max_chunks: 0,
        ..CacheConfig::default()
    }
}

/// Random reads of a large object: only the chunks touched are fetched,
/// never the whole object; a second read of them is local.
#[tokio::test(flavor = "multi_thread")]
async fn random_reads_fetch_only_their_chunks() {
    let Some(t) = setup(no_readahead(), Remote::default()) else {
        return;
    };
    let size = 1 << 30;
    let rel = placeholder(&t, 1, size);
    let mut rng = 0x1234_5678u64;
    let mut offs = Vec::new();
    for _ in 0..40 {
        rng = rng
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        let off = rng % (size - 70_000);
        let r = t
            .cache
            .fetch(&req(&rel, 1, size, off, 65_536))
            .await
            .unwrap();
        assert!(
            matches!(r, FetchOutcome::Filled { .. } | FetchOutcome::Local),
            "{r:?}"
        );
        assert_eq!(read_at(&t, &rel, off, 65_536), content(off, 65_536));
        offs.push(off);
    }
    let reads = t.remote.reads.lock().unwrap().clone();
    assert!(
        reads.iter().all(|(_, l)| *l <= 4 * MIB),
        "chunk-sized reads only"
    );
    let fetched: u64 = reads.iter().map(|(_, l)| l).sum();
    assert!(fetched <= 80 * 4 * MIB && fetched < size / 2, "{fetched}");
    let n = reads.len();
    for off in offs {
        let r = t
            .cache
            .fetch(&req(&rel, 1, size, off, 65_536))
            .await
            .unwrap();
        assert_eq!(r, FetchOutcome::Local);
    }
    assert_eq!(
        t.remote.reads.lock().unwrap().len(),
        n,
        "no new remote reads"
    );
    let s = t.cache.stats();
    assert_eq!(s.used_bytes, fetched);
    assert!(s.hits >= 40 && s.misses as usize == n, "{s:?}");
    assert_eq!(t.cache.take_remote_reads(), vec![(9, fetched)]);
    assert!(t.cache.take_remote_reads().is_empty(), "drained");
}

/// A header scan: one header fetch per file the first time, none the second.
#[tokio::test(flavor = "multi_thread")]
async fn header_scan_costs_one_fetch_per_file_once() {
    let Some(t) = setup(no_readahead(), Remote::default()) else {
        return;
    };
    let size = 64 * MIB;
    let mut rels = Vec::new();
    for id in 1..=20 {
        rels.push(placeholder(&t, id, size));
    }
    for round in 0..2 {
        for (i, rel) in rels.iter().enumerate() {
            let mut r = req(rel, i as u64 + 1, size, 0, 3600);
            r.header_bytes = 4 * MIB;
            let out = t.cache.fetch(&r).await.unwrap();
            if round == 1 {
                assert_eq!(out, FetchOutcome::Local);
            }
            assert_eq!(read_at(&t, rel, 0, 3600), content(0, 3600));
        }
    }
    let reads = t.remote.reads.lock().unwrap().clone();
    assert_eq!(reads.len(), 20, "one header fetch per file: {reads:?}");
    let s = t.cache.stats();
    assert_eq!(s.header_bytes, 20 * 4 * MIB);
    let (m, _) = read_state(&t.layout, &rels[0]).unwrap().unwrap();
    assert_eq!(m.resident_bytes, 4 * MIB);
}

#[tokio::test(flavor = "multi_thread")]
async fn sequential_reads_read_ahead_in_the_background() {
    let cfg = CacheConfig {
        readahead_max_chunks: 8,
        ..CacheConfig::default()
    };
    let Some(t) = setup(cfg, Remote::default()) else {
        return;
    };
    let size = 256 * MIB;
    let rel = placeholder(&t, 1, size);
    for i in 0..4 {
        t.cache
            .fetch(&req(&rel, 1, size, i * MIB, MIB))
            .await
            .unwrap();
    }
    // Read-ahead lands in the background.
    let mut ok = false;
    for _ in 0..100 {
        let (m, map) = read_state(&t.layout, &rel).unwrap().unwrap();
        // Windows of 1, 2, 4 and 8 chunks: chunks 1..9 (to 36 MiB).
        if m.covers_with_map(map.as_ref(), 4 * MIB, 32 * MIB) {
            ok = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(ok, "chunks ahead of the reader were fetched");
    let (m, map) = read_state(&t.layout, &rel).unwrap().unwrap();
    assert!(
        !m.covers_with_map(map.as_ref(), 36 * MIB, 1),
        "no further than the window"
    );
    // A random reader gets no read-ahead.
    let n = t.remote.reads.lock().unwrap().len();
    t.cache
        .fetch(&req(&rel, 1, size, 200 * MIB + 5, 100))
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(t.remote.reads.lock().unwrap().len(), n + 1);
}

/// Over budget: LRU chunks go first, then headers; the placeholders stay.
#[tokio::test(flavor = "multi_thread")]
async fn budget_evicts_lru_chunks_then_headers() {
    let cfg = CacheConfig {
        capacity_bytes: 40 * MIB,
        low_watermark_pct: 50,
        readahead_max_chunks: 0,
        ..CacheConfig::default()
    };
    let Some(t) = setup(cfg, Remote::default()) else {
        return;
    };
    let size = 128 * MIB;
    let a = placeholder(&t, 1, size);
    let b = placeholder(&t, 2, size);
    let mut ra = req(&a, 1, size, 0, 100);
    ra.header_bytes = 4 * MIB;
    t.cache.fetch(&ra).await.unwrap();
    // Chunks of b, oldest first: 10, 11, ... 19 (40 MiB): over budget.
    for c in 10..20u64 {
        t.cache
            .fetch(&req(&b, 2, size, c * 4 * MIB, 1))
            .await
            .unwrap();
    }
    // Explicit eviction to 20 MiB (the background one may run too).
    let r = t.cache.evict(20 * MIB).await;
    let s = t.cache.stats();
    assert!(s.used_bytes <= 20 * MIB, "{s:?} {r:?}");
    assert_eq!(s.header_bytes, 4 * MIB, "the header is evicted last");
    assert!(s.evictions >= 5, "{s:?}");
    let (m, map) = read_state(&t.layout, &b).unwrap().unwrap();
    let map = map.unwrap();
    assert!(!map.is_set(10) && !map.is_set(11), "oldest chunks evicted");
    assert!(map.is_set(19), "newest kept");
    assert_eq!(m.state, State::Partial);
    let f = File::open(t.layout.path(&b)).unwrap();
    assert!(
        sys::first_data_at(f.as_fd(), 40 * MIB).unwrap().unwrap() >= 44 * MIB,
        "evicted chunks are holes"
    );
    // Down to nothing: the header goes too.
    t.cache.evict(0).await;
    let s = t.cache.stats();
    assert_eq!((s.used_bytes, s.header_bytes), (0, 0), "{s:?}");
    let (m, _) = read_state(&t.layout, &a).unwrap().unwrap();
    assert_eq!(m.resident_bytes, 0);
    assert_eq!(std::fs::metadata(t.layout.path(&a)).unwrap().len(), size);
}

#[tokio::test(flavor = "multi_thread")]
async fn whole_object_completes_and_leaves_the_cache() {
    let Some(t) = setup(no_readahead(), Remote::default()) else {
        return;
    };
    let size = 9 * MIB + 7;
    let rel = placeholder(&t, 1, size);
    let r = t
        .cache
        .fetch(&with_crc(req(&rel, 1, size, 0, size)))
        .await
        .unwrap();
    let FetchOutcome::Complete(d) = r else {
        panic!("{r:?}")
    };
    assert_eq!(d.size, size);
    let (m, map) = read_state(&t.layout, &rel).unwrap().unwrap();
    assert_eq!(m.state, State::Resident);
    assert!(map.is_none());
    assert_eq!(
        t.cache.stats().used_bytes,
        0,
        "resident files are the catalog's"
    );
    // A wrong catalog CRC: never resident, narrowed back.
    let rel2 = placeholder(&t, 2, size);
    let mut r2 = req(&rel2, 2, size, 0, size);
    r2.crc64nvme = Some(1);
    let e = t.cache.fetch(&r2).await.unwrap_err();
    assert!(e.is_integrity(), "{e}");
    let (m, _) = read_state(&t.layout, &rel2).unwrap().unwrap();
    assert_eq!((m.state, m.resident_bytes), (State::Partial, 0));
}

#[tokio::test(flavor = "multi_thread")]
async fn unsupported_and_archived_fail_closed() {
    let Some(t) = setup(
        no_readahead(),
        Remote {
            archived: true,
            ..Remote::default()
        },
    ) else {
        return;
    };
    let size = 8 * MIB;
    let rel = placeholder(&t, 1, size);
    let e = t.cache.fetch(&req(&rel, 1, size, 0, 10)).await.unwrap_err();
    assert!(e.is_archived() && !e.is_unsupported(), "{e}");
    let mut r = req(&rel, 1, size, 0, 10);
    r.target = ReadTarget::Unsupported {
        mode: "ontap".into(),
    };
    let e = t.cache.fetch(&r).await.unwrap_err();
    assert!(e.is_unsupported(), "{e}");
    assert!(e.to_string().contains("upgrade the edge"), "{e}");
    // Nothing was marked local.
    let (m, map) = read_state(&t.layout, &rel).unwrap().unwrap();
    assert_eq!(m.resident_bytes, 0);
    assert!(map.is_none());
}

#[tokio::test(flavor = "multi_thread")]
async fn index_is_rebuilt_from_disk() {
    let Some(t) = setup(no_readahead(), Remote::default()) else {
        return;
    };
    let size = 32 * MIB;
    let rel = placeholder(&t, 1, size);
    let mut r = req(&rel, 1, size, 0, 1);
    r.header_bytes = 4 * MIB;
    t.cache.fetch(&r).await.unwrap();
    t.cache
        .fetch(&req(&rel, 1, size, 20 * MIB, 1))
        .await
        .unwrap();
    let before = t.cache.stats();
    // A fresh cache (daemon restart) adopts what is on disk.
    let fresh: Cache<Remote, Fsal> = Cache::new(
        no_readahead(),
        t.layout.clone(),
        t.remote.clone(),
        Arc::new(Fsal::default()),
        t.seq.clone(),
        Arc::new(FileLocks::new()),
    );
    assert!(fresh.adopt_from_disk(&rel, 1, 0).unwrap());
    assert!(
        !fresh.adopt_from_disk(&rel, 2, 0).unwrap(),
        "another file id"
    );
    let s = fresh.stats();
    assert_eq!(
        (s.used_bytes, s.header_bytes),
        (before.used_bytes, before.header_bytes)
    );
    let out = fresh
        .fetch(&req(&rel, 1, size, 20 * MIB + 5, 10))
        .await
        .unwrap();
    assert_eq!(out, FetchOutcome::Local);
    fresh.evict(0).await;
    assert_eq!(fresh.stats().used_bytes, 0);
}

#[tokio::test(flavor = "multi_thread")]
async fn header_prefetch_respects_its_rate_cap() {
    let cfg = CacheConfig {
        header_prefetch_per_sec: 10.0,
        readahead_max_chunks: 0,
        ..CacheConfig::default()
    };
    let Some(t) = setup(cfg, Remote::default()) else {
        return;
    };
    let size = 16 * MIB;
    for id in 1..=6 {
        let rel = placeholder(&t, id, size);
        let mut r = req(&rel, id, size, 0, 0);
        r.header_bytes = MIB;
        t.cache.queue_header_prefetch(r);
    }
    let c = t.cache.clone();
    let task = tokio::spawn(async move { c.run_header_prefetcher().await });
    let start = std::time::Instant::now();
    while t.cache.stats().header_bytes < 6 * MIB {
        assert!(
            start.elapsed() < Duration::from_secs(10),
            "prefetch stalled"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    // Six starts at 10/s: at least ~0.5 s.
    assert!(
        start.elapsed() >= Duration::from_millis(450),
        "{:?}",
        start.elapsed()
    );
    task.abort();
}

/// The source changed (412 on an ETag-pinned read): the read fails, and
/// every cached byte of the file is discarded before it does.
#[tokio::test(flavor = "multi_thread")]
async fn changed_source_discards_the_file() {
    let Some(t) = setup(
        no_readahead(),
        Remote {
            changes_after: Some(3),
            ..Remote::default()
        },
    ) else {
        return;
    };
    let size = 64 * MIB;
    let rel = placeholder(&t, 1, size);
    let mut r = req(&rel, 1, size, 0, 10);
    r.header_bytes = 4 * MIB;
    t.cache.fetch(&r).await.unwrap();
    t.cache
        .fetch(&req(&rel, 1, size, 20 * MIB, 10))
        .await
        .unwrap();
    t.cache
        .fetch(&req(&rel, 1, size, 40 * MIB, 10))
        .await
        .unwrap();
    assert!(t.cache.stats().used_bytes >= 12 * MIB);
    let e = t
        .cache
        .fetch(&req(&rel, 1, size, 50 * MIB, 10))
        .await
        .unwrap_err();
    assert!(e.is_source_changed() && !e.is_archived(), "{e}");
    let (m, map) = read_state(&t.layout, &rel).unwrap().unwrap();
    assert_eq!((m.state, m.resident_bytes), (State::Partial, 0));
    assert!(map.is_none());
    let f = File::open(t.layout.path(&rel)).unwrap();
    assert!(
        sys::first_data_at(f.as_fd(), 0).unwrap().is_none(),
        "all punched"
    );
    assert_eq!(t.cache.stats().used_bytes, 0, "forgotten");
}

/// No version id and no ETag: never read, nothing written.
#[tokio::test(flavor = "multi_thread")]
async fn unpinned_objects_are_never_read() {
    let Some(t) = setup(no_readahead(), Remote::default()) else {
        return;
    };
    let size = 8 * MIB;
    let rel = placeholder(&t, 1, size);
    let mut r = req(&rel, 1, size, 0, 10);
    let ReadTarget::S3(o) = &mut r.target else {
        panic!()
    };
    o.version_id = None;
    o.etag = None;
    let e = t.cache.fetch(&r).await.unwrap_err();
    assert!(e.is_unpinned(), "{e}");
    assert!(t.remote.reads.lock().unwrap().is_empty());
    let (m, map) = read_state(&t.layout, &rel).unwrap().unwrap();
    assert_eq!(m.resident_bytes, 0);
    assert!(map.is_none());
    // With an ETag it reads.
    let ReadTarget::S3(o) = &mut r.target else {
        panic!()
    };
    o.etag = Some("\"e\"".into());
    t.cache.fetch(&r).await.unwrap();
}

/// Review S6: a remote object over 2 TiB is chunk-fetched with a larger
/// chunk size instead of failing every read.
#[tokio::test(flavor = "multi_thread")]
async fn objects_over_2_tib_are_fetched() {
    let Some(t) = setup(no_readahead(), Remote::default()) else {
        return;
    };
    let size = 3u64 << 40;
    let rel = placeholder(&t, 1, size);
    let off = (5u64 << 40) / 2 + 777;
    let r = t
        .cache
        .fetch(&req(&rel, 1, size, off, 65_536))
        .await
        .unwrap();
    assert!(matches!(r, FetchOutcome::Filled { .. }), "{r:?}");
    assert_eq!(read_at(&t, &rel, off, 65_536), content(off, 65_536));
    let (m, map) = read_state(&t.layout, &rel).unwrap().unwrap();
    assert_eq!((m.chunk_shift, map.unwrap().shift), (23, 23));
    assert_eq!(t.cache.stats().used_bytes, 8 * MIB, "one 8 MiB chunk");
    assert_eq!(
        t.cache
            .fetch(&req(&rel, 1, size, off, 65_536))
            .await
            .unwrap(),
        FetchOutcome::Local
    );
}

/// Review N4: the chunk size changed in the config and the daemon
/// restarted. A file keeps its on-disk map's chunk size: reads of what is
/// cached stay local, new chunks land in the same map.
#[tokio::test(flavor = "multi_thread")]
async fn a_new_chunk_size_never_misreads_old_maps() {
    let Some(t) = setup(no_readahead(), Remote::default()) else {
        return;
    };
    let size = 256 * MIB;
    let rel = placeholder(&t, 1, size);
    t.cache
        .fetch(&req(&rel, 1, size, 100 * MIB, 10))
        .await
        .unwrap();
    let n = t.remote.reads.lock().unwrap().len();
    for adopt in [false, true] {
        let cfg = CacheConfig {
            chunk_shift: 24,
            ..no_readahead()
        };
        let fresh: Cache<Remote, Fsal> = Cache::new(
            cfg,
            t.layout.clone(),
            t.remote.clone(),
            Arc::new(Fsal::default()),
            t.seq.clone(),
            Arc::new(FileLocks::new()),
        );
        if adopt {
            assert!(fresh.adopt_from_disk(&rel, 1, 0).unwrap());
            assert_eq!(
                fresh.stats().used_bytes,
                4 * MIB,
                "indexed at the file's shift"
            );
        }
        // 100 MiB lies in 4 MiB chunk 25; at 16 MiB it would be chunk 6.
        let out = fresh
            .fetch(&req(&rel, 1, size, 100 * MIB + 5, 100))
            .await
            .unwrap();
        assert_eq!(out, FetchOutcome::Local, "adopt={adopt}");
        assert_eq!(t.remote.reads.lock().unwrap().len(), n, "no remote read");
    }
    // A new chunk still lands in the file's 4 MiB map.
    let fresh: Cache<Remote, Fsal> = Cache::new(
        CacheConfig {
            chunk_shift: 24,
            ..no_readahead()
        },
        t.layout.clone(),
        t.remote.clone(),
        Arc::new(Fsal::default()),
        t.seq.clone(),
        Arc::new(FileLocks::new()),
    );
    fresh
        .fetch(&req(&rel, 1, size, 200 * MIB, 10))
        .await
        .unwrap();
    let (m, map) = read_state(&t.layout, &rel).unwrap().unwrap();
    assert_eq!(m.chunk_shift, 22);
    let map = map.unwrap();
    assert!(map.is_set(25) && map.is_set(50));
    // Line A: the first fetch of a file the index doesn't know adopts it
    // from disk, so chunk 25 (cached before the "restart") counts too.
    assert_eq!(fresh.stats().used_bytes, 8 * MIB);
    let (_, l) = t.remote.reads.lock().unwrap()[n];
    assert_eq!(l, 4 * MIB);
}
