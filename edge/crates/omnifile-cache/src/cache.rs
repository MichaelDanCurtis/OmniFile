//! The cache itself: fetch a range of a remote object into its placeholder,
//! read ahead on sequential access, keep within the SSD budget, prefetch
//! headers at a rate cap, and count it all for the heartbeat.

use std::collections::{HashMap, VecDeque};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use omnifile_blob::{BlobError, RangeReader, ReadTarget};
use omnifile_marker::{DEFAULT_CHUNK_SHIFT, Flags, State};
use omnifile_stub::chunks::{
    self, ChunkFill, ChunkNarrow, ChunkSource, ChunkTarget, chunk_range, chunks_for,
};
use omnifile_stub::fill::{FillDone, FillError, FillRequest};
use omnifile_stub::{FileLocks, FsalNotifier, Layout, MarkerSeq};
use tracing::{debug, warn};

use crate::index::{FileMeta, Index, Victim};
use crate::rate::RateCap;
use crate::readahead::Planner;

#[derive(Clone, Debug)]
pub struct CacheConfig {
    /// The SSD budget for cached remote bytes (chunks and headers).
    pub capacity_bytes: u64,
    /// Evict down to this share of the budget once it is exceeded (percent).
    pub low_watermark_pct: u8,
    /// Chunk size for new maps (log2; 22 = 4 MiB).
    pub chunk_shift: u8,
    /// Most chunks read ahead of a sequential reader (0: no read-ahead).
    pub readahead_max_chunks: u32,
    /// Background header prefetches started per second (0: none).
    pub header_prefetch_per_sec: f64,
}

impl Default for CacheConfig {
    fn default() -> Self {
        Self {
            capacity_bytes: 100 << 30,
            low_watermark_pct: 90,
            chunk_shift: DEFAULT_CHUNK_SHIFT,
            readahead_max_chunks: 16,
            header_prefetch_per_sec: 20.0,
        }
    }
}

/// One read the FSAL or smb-guard needs local.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FetchRequest {
    /// Path relative to the data root.
    pub rel: PathBuf,
    /// The inode the replica knows, if any: `(ino, ino_gen)`.
    pub expect_ino: Option<(u64, u32)>,
    pub file_id: u64,
    /// The content version (the marker's).
    pub version_id: u64,
    pub size: u64,
    /// The catalog's whole-file CRC-64/NVME.
    pub crc64nvme: Option<u64>,
    /// For egress accounting (`remote_reads`).
    pub backend_id: u64,
    pub target: ReadTarget,
    pub offset: u64,
    pub len: u64,
    /// The dataset's `header_bytes`: fetched as a resident prefix on first
    /// access (0: no header cache).
    pub header_bytes: u64,
    /// The resident prefix the catalog counts ([`FileMeta::floor`]); the
    /// cache never evicts below it (line A: stubs keep their headers).
    pub floor: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FetchOutcome {
    /// Every byte asked for was already local.
    Local,
    /// The range is now local (`bytes` fetched for it).
    Filled { bytes: u64 },
    /// Every byte of the file is local and verified: the marker is
    /// `RESIDENT`. Report `POST …/files/{id}/filled`.
    Complete(FillDone),
    /// Another fill, stub or eviction holds the file: ask again shortly.
    Busy,
}

#[derive(Debug, thiserror::Error)]
pub enum CacheError {
    #[error("{0}")]
    Fill(#[from] FillError),
    #[error("io: {0}")]
    Io(#[from] io::Error),
}

impl CacheError {
    fn blob(&self) -> Option<&BlobError> {
        match self {
            CacheError::Fill(FillError::Source(e)) => e.get_ref()?.downcast_ref::<BlobError>(),
            _ => None,
        }
    }

    /// The backend or credential mode is unknown to this edge (reason 10,
    /// "upgrade the edge").
    pub fn is_unsupported(&self) -> bool {
        self.blob().is_some_and(BlobError::is_unsupported)
    }

    /// The object is archived (not restored): this needs a recall.
    pub fn is_archived(&self) -> bool {
        self.blob().is_some_and(BlobError::is_archived)
    }

    /// The credential was refused: get a new one and ask again.
    pub fn is_auth(&self) -> bool {
        self.blob().is_some_and(BlobError::is_auth)
    }

    /// The source object changed under an ETag-pinned read (`412`): the
    /// file's cached bytes were discarded. Report it to the service
    /// (`omnifile_control::types::AuditRecord::source_changed`).
    pub fn is_source_changed(&self) -> bool {
        self.blob().is_some_and(BlobError::is_source_changed)
    }

    /// The object has neither a version id nor an ETag: never read.
    pub fn is_unpinned(&self) -> bool {
        matches!(self.blob(), Some(BlobError::Unpinned(_)))
    }

    /// The whole-file check failed: the file was narrowed back to nothing.
    pub fn is_integrity(&self) -> bool {
        matches!(self, CacheError::Fill(FillError::Integrity { .. }))
    }
}

/// Counters for the heartbeat's `cache` object.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct CacheStats {
    pub used_bytes: u64,
    pub capacity_bytes: u64,
    pub header_bytes: u64,
    pub hits: u64,
    pub misses: u64,
    pub evictions: u64,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct EvictReport {
    pub freed: u64,
    /// Victims that couldn't be evicted now (busy, or not acknowledged).
    pub deferred: u64,
}

struct Inner<R, N> {
    cfg: CacheConfig,
    layout: Layout,
    reader: Arc<R>,
    notifier: Arc<N>,
    seq: Arc<MarkerSeq>,
    locks: Arc<FileLocks>,
    index: Mutex<Index>,
    planner: Mutex<Planner>,
    /// One fetch per file at a time, inside the cache.
    gates: Mutex<HashMap<u64, Arc<tokio::sync::Mutex<()>>>>,
    hits: AtomicU64,
    misses: AtomicU64,
    evictions: AtomicU64,
    egress: Mutex<HashMap<u64, u64>>,
    evicting: AtomicBool,
    /// One eviction pass at a time: two passes computing their need from
    /// the same `used` would evict twice as much (headers included).
    evict_lock: tokio::sync::Mutex<()>,
    prefetch: Mutex<VecDeque<FetchRequest>>,
    prefetch_wake: tokio::sync::Notify,
    /// Fetches wait while this is false: the daemon indexes what is on disk
    /// after a restart before the cache admits new bytes (review on #23).
    admitting: AtomicBool,
    admit_wake: tokio::sync::Notify,
}

/// The edge's cache of remote objects. Cheap to clone.
pub struct Cache<R, N> {
    inner: Arc<Inner<R, N>>,
}

impl<R, N> Clone for Cache<R, N> {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
        }
    }
}

/// [`ChunkSource`] over a [`RangeReader`], counting egress per backend.
struct Source<'a, R, N> {
    inner: &'a Inner<R, N>,
    target: &'a ReadTarget,
    backend_id: u64,
}

impl<R: RangeReader, N: FsalNotifier> ChunkSource for Source<'_, R, N> {
    async fn read(&self, offset: u64, len: u64) -> io::Result<Vec<u8>> {
        let b = self
            .inner
            .reader
            .read_range(self.target, offset, len)
            .await
            .map_err(io::Error::other)?;
        if b.len() as u64 != len {
            return Err(io::Error::other(format!(
                "source returned {} bytes for a {len}-byte range",
                b.len()
            )));
        }
        *self
            .inner
            .egress
            .lock()
            .unwrap()
            .entry(self.backend_id)
            .or_default() += len;
        Ok(b.to_vec())
    }
}

impl<R, N> Cache<R, N>
where
    R: RangeReader + 'static,
    N: FsalNotifier + 'static,
{
    pub fn new(
        cfg: CacheConfig,
        layout: Layout,
        reader: Arc<R>,
        notifier: Arc<N>,
        seq: Arc<MarkerSeq>,
        locks: Arc<FileLocks>,
    ) -> Self {
        let planner = Planner::new(cfg.readahead_max_chunks, 4096);
        Self {
            inner: Arc::new(Inner {
                cfg,
                layout,
                reader,
                notifier,
                seq,
                locks,
                index: Mutex::new(Index::new()),
                planner: Mutex::new(planner),
                gates: Mutex::new(HashMap::new()),
                hits: AtomicU64::new(0),
                misses: AtomicU64::new(0),
                evictions: AtomicU64::new(0),
                egress: Mutex::new(HashMap::new()),
                evicting: AtomicBool::new(false),
                evict_lock: tokio::sync::Mutex::new(()),
                prefetch: Mutex::new(VecDeque::new()),
                prefetch_wake: tokio::sync::Notify::new(),
                admitting: AtomicBool::new(true),
                admit_wake: tokio::sync::Notify::new(),
            }),
        }
    }

    pub fn config(&self) -> &CacheConfig {
        &self.inner.cfg
    }

    fn gate(&self, file_id: u64) -> Arc<tokio::sync::Mutex<()>> {
        let mut g = self.inner.gates.lock().unwrap();
        // Drop gates nobody holds, so the map stays small.
        if g.len() > 1024 {
            g.retain(|_, m| Arc::strong_count(m) > 1);
        }
        g.entry(file_id).or_default().clone()
    }

    /// Make `[offset, offset+len)` of a remote object local: its header
    /// prefix first (if the read touches it), then the chunks the read
    /// covers. Returns once those are durable and marked; read-ahead for a
    /// sequential reader runs in the background afterwards.
    pub async fn fetch(&self, req: &FetchRequest) -> Result<FetchOutcome, CacheError> {
        let (out, ahead) = self.fetch_planned(req).await?;
        if let Some(r) = ahead {
            // Standalone use: read ahead in the background. The daemon uses
            // `fetch_planned` and runs read-ahead inside its own fenced fill.
            let this = self.clone();
            tokio::spawn(async move {
                if let Err(e) = this.fetch_ahead(&r).await {
                    debug!(file_id = r.file_id, error = %e, "read-ahead failed");
                }
            });
        }
        Ok(out)
    }

    /// Stop admitting new bytes until [`Cache::open_admission`]: fetches
    /// wait (the daemon's startup index of what is on disk).
    pub fn hold_admission(&self) {
        self.inner.admitting.store(false, Ordering::SeqCst);
    }

    pub fn open_admission(&self) {
        self.inner.admitting.store(true, Ordering::SeqCst);
        self.inner.admit_wake.notify_waiters();
    }

    async fn admitted(&self) {
        loop {
            let wait = self.inner.admit_wake.notified();
            if self.inner.admitting.load(Ordering::SeqCst) {
                return;
            }
            wait.await;
        }
    }

    /// Evict before admitting `req`'s bytes, so the cache never goes over
    /// its budget on disk: at most the read's chunks (and its header) are
    /// added. Victims another fill holds are skipped (and retried later).
    async fn make_room(&self, req: &FetchRequest) {
        let i = &self.inner;
        let chunk = 1u64 << self.disk_shift(req);
        let need = req
            .len
            .div_ceil(chunk)
            .saturating_add(1)
            .saturating_mul(chunk)
            .saturating_add(req.header_bytes);
        let used = i.index.lock().unwrap().used();
        if used.saturating_add(need) > i.cfg.capacity_bytes {
            let low = i.cfg.capacity_bytes / 100 * u64::from(i.cfg.low_watermark_pct.min(100));
            let budget = low.min(i.cfg.capacity_bytes.saturating_sub(need));
            let r = self.evict(budget).await;
            debug!(
                freed = r.freed,
                deferred = r.deferred,
                "cache eviction before a fill"
            );
        }
    }

    /// Bring the cache within its budget now (after the startup index).
    pub async fn enforce_budget(&self) {
        let i = &self.inner;
        if i.index.lock().unwrap().used() > i.cfg.capacity_bytes {
            let low = i.cfg.capacity_bytes / 100 * u64::from(i.cfg.low_watermark_pct.min(100));
            let r = self.evict(low).await;
            debug!(
                freed = r.freed,
                deferred = r.deferred,
                "cache eviction to the budget"
            );
        }
    }

    /// [`Cache::fetch`] without the background read-ahead: returns the
    /// read-ahead request (a sequential reader's next chunks) for the
    /// caller to fetch under its own fill fence, with [`Cache::fetch_ahead`]
    /// (review on #23: a detached read-ahead outlived the daemon's fence and
    /// was refused).
    pub async fn fetch_planned(
        &self,
        req: &FetchRequest,
    ) -> Result<(FetchOutcome, Option<FetchRequest>), CacheError> {
        if let ReadTarget::Unsupported { mode } = &req.target {
            return Err(
                FillError::Source(io::Error::other(BlobError::Unsupported(mode.clone()))).into(),
            );
        }
        // Nothing pins the bytes of this object: never read it (no fence,
        // no write).
        if let ReadTarget::S3(o) = &req.target
            && !o.is_pinned()
        {
            return Err(FillError::Source(io::Error::other(o.unpinned_error())).into());
        }
        // A presigned URL that pins nothing could serve two objects across
        // two chunks: no chunk reads (review on #23).
        if let ReadTarget::S3Presigned { .. } = &req.target
            && !req.target.is_pinned()
        {
            return Err(FillError::Source(io::Error::other(BlobError::Unpinned(
                "presigned URL with neither a version nor a signed If-Match".into(),
            )))
            .into());
        }
        self.admitted().await;
        self.make_room(req).await;
        let out = self.fetch_demanded(req).await?;
        let ahead = if matches!(out, FetchOutcome::Complete(_) | FetchOutcome::Busy) {
            None
        } else {
            self.plan_read_ahead(req)
        };
        self.maybe_evict();
        Ok((out, ahead))
    }

    /// Fetch a read-ahead request from [`Cache::fetch_planned`].
    pub async fn fetch_ahead(&self, req: &FetchRequest) -> Result<FetchOutcome, CacheError> {
        self.admitted().await;
        self.make_room(req).await;
        let out = self.fetch_demanded(req).await;
        self.maybe_evict();
        out
    }

    async fn fetch_demanded(&self, req: &FetchRequest) -> Result<FetchOutcome, CacheError> {
        let gate = self.gate(req.file_id);
        let _g = gate.lock().await;
        let i = &self.inner;
        // A file cached before a restart is indexed from its marker and map
        // on first use, so its chunks count against the budget again.
        if i.index.lock().unwrap().meta(req.file_id).is_none()
            && let Err(e) = self.adopt_from_disk(&req.rel, req.file_id, req.floor)
        {
            debug!(file_id = req.file_id, error = %e, "not adopted from disk");
        }
        let freq = FillRequest {
            rel: req.rel.clone(),
            expect_ino: req.expect_ino,
        };
        let mut target = ChunkTarget::new(req.file_id, req.version_id, req.size, req.crc64nvme);
        target.chunk_shift = i.cfg.chunk_shift;
        let src = Source {
            inner: i,
            target: &req.target,
            backend_id: req.backend_id,
        };
        let mut bytes = 0u64;
        let end = req.offset.saturating_add(req.len).min(req.size);
        // 1. The header prefix, when the read touches it.
        let mut header_end = i.index.lock().unwrap().header(req.file_id);
        if req.header_bytes > 0 && req.offset < req.header_bytes {
            match chunks::fill_prefix(
                &i.layout,
                &freq,
                &target,
                req.header_bytes,
                &src,
                &*i.notifier,
                &i.seq,
                &i.locks,
            )
            .await
            {
                Ok(ChunkFill::Filled {
                    bytes: b, marker, ..
                }) => {
                    bytes += b;
                    header_end = marker.resident_bytes;
                    let mut ix = i.index.lock().unwrap();
                    ix.set_meta(req.file_id, self.meta(req, &marker));
                    ix.set_header(req.file_id, marker.resident_bytes.saturating_sub(req.floor));
                }
                Ok(ChunkFill::Complete(d)) => return Ok(self.completed(req.file_id, d)),
                Ok(ChunkFill::Resident) => return Ok(self.completed_earlier(req.file_id)),
                Err(FillError::Busy) => return Ok(FetchOutcome::Busy),
                Err(e) => return Err(self.failed(req, e).await),
            }
        }
        // 2. The chunks past the prefix, by the file's own chunk size.
        let from = req.offset.max(header_end);
        if from < end {
            let shift = self.disk_shift(req);
            let want: Vec<u64> = chunks_for(req.size, shift, from, end - from).collect();
            let already = {
                let ix = i.index.lock().unwrap();
                want.iter()
                    .filter(|c| ix.has_chunk(req.file_id, **c))
                    .count() as u64
            };
            match chunks::fill_chunks(
                &i.layout,
                &freq,
                &target,
                &want,
                &src,
                &*i.notifier,
                &i.seq,
                &i.locks,
            )
            .await
            {
                Ok(ChunkFill::Filled {
                    written,
                    bytes: b,
                    marker,
                }) => {
                    bytes += b;
                    let mut ix = i.index.lock().unwrap();
                    ix.set_meta(req.file_id, self.meta(req, &marker));
                    for c in &want {
                        if let Some((_, len)) = chunk_range(req.size, shift, *c)
                            && (written.contains(c) || ix.has_chunk(req.file_id, *c))
                        {
                            ix.touch_chunk(req.file_id, *c, len);
                        }
                    }
                    i.hits.fetch_add(already, Ordering::Relaxed);
                    i.misses.fetch_add(written.len() as u64, Ordering::Relaxed);
                }
                Ok(ChunkFill::Complete(d)) => return Ok(self.completed(req.file_id, d)),
                Ok(ChunkFill::Resident) => return Ok(self.completed_earlier(req.file_id)),
                Err(FillError::Busy) => return Ok(FetchOutcome::Busy),
                Err(e) => return Err(self.failed(req, e).await),
            }
        }
        Ok(if bytes == 0 {
            FetchOutcome::Local
        } else {
            FetchOutcome::Filled { bytes }
        })
    }

    /// The chunk shift of this file as it is on disk (review N4): its map's,
    /// or, without one, what a new map of its size gets (review S6). Never
    /// the configured shift alone: a map written under another
    /// configuration, or a file too large for it, keeps its own.
    fn disk_shift(&self, req: &FetchRequest) -> u8 {
        let on_disk = chunks::read_state(&self.inner.layout, &req.rel)
            .ok()
            .flatten()
            .map(|(m, _)| m);
        chunks::file_chunk_shift(
            on_disk.as_ref(),
            req.version_id,
            req.size,
            self.inner.cfg.chunk_shift,
        )
    }

    fn meta(&self, req: &FetchRequest, m: &omnifile_marker::Marker) -> FileMeta {
        FileMeta {
            rel: req.rel.clone(),
            version_id: req.version_id,
            size: req.size,
            shift: chunks::file_chunk_shift(
                Some(m),
                req.version_id,
                req.size,
                self.inner.cfg.chunk_shift,
            ),
            floor: req.floor,
        }
    }

    /// The file became `RESIDENT`: it is the catalog's now, not the cache's.
    fn completed(&self, file_id: u64, d: FillDone) -> FetchOutcome {
        self.forget(file_id);
        self.inner.misses.fetch_add(1, Ordering::Relaxed);
        FetchOutcome::Complete(d)
    }

    fn completed_earlier(&self, file_id: u64) -> FetchOutcome {
        self.forget(file_id);
        FetchOutcome::Local
    }

    async fn failed(&self, req: &FetchRequest, e: FillError) -> CacheError {
        let e = CacheError::from(e);
        if e.is_integrity() {
            // Narrowed back to nothing local.
            self.forget(req.file_id);
        } else if e.is_source_changed() {
            // The source object is not the catalog's any more: nothing
            // cached for this file can be trusted. Narrow to nothing local
            // (acknowledged) before the read fails.
            warn!(
                file_id = req.file_id,
                version_id = req.version_id,
                "the source object changed (If-Match refused); discarding the file's cached bytes"
            );
            self.discard(req).await;
        }
        e
    }

    /// Evict everything local of a file, retrying while an FSAL doesn't
    /// acknowledge or another operation holds it. A narrowed marker that is
    /// not yet punched is safe (nothing reads as local); a later eviction
    /// pass or restart completes the punch.
    async fn discard(&self, req: &FetchRequest) {
        let i = &self.inner;
        let mut wait = std::time::Duration::from_millis(50);
        for _ in 0..10 {
            match chunks::evict_file(
                &i.layout,
                &req.rel,
                req.file_id,
                req.version_id,
                req.floor,
                &*i.notifier,
                &i.seq,
                &i.locks,
            )
            .await
            {
                Ok(ChunkNarrow::Punched { .. } | ChunkNarrow::Changed) => break,
                Ok(ChunkNarrow::NotAcked | ChunkNarrow::Busy) => {}
                Err(e) => {
                    warn!(file_id = req.file_id, error = %e, "discarding a changed source's bytes failed");
                }
            }
            tokio::time::sleep(wait).await;
            wait = (wait * 2).min(std::time::Duration::from_secs(1));
        }
        self.forget(req.file_id);
    }

    /// The read-ahead request for a sequential reader, if any.
    fn plan_read_ahead(&self, req: &FetchRequest) -> Option<FetchRequest> {
        let shift = self.disk_shift(req);
        let ahead = self.inner.planner.lock().unwrap().observe(
            req.file_id,
            req.offset,
            req.len,
            req.size,
            shift,
        );
        if ahead.is_empty() {
            return None;
        }
        let (start, _) = chunk_range(req.size, shift, ahead.start).unwrap_or((req.size, 0));
        let stop = chunk_range(req.size, shift, ahead.end - 1).map_or(req.size, |(s, l)| s + l);
        let mut r = req.clone();
        r.offset = start;
        r.len = stop.saturating_sub(start);
        r.header_bytes = 0;
        Some(r)
    }

    /// Over budget: evict down to the low watermark in the background.
    fn maybe_evict(&self) {
        let i = &self.inner;
        if i.index.lock().unwrap().used() <= i.cfg.capacity_bytes {
            return;
        }
        if i.evicting.swap(true, Ordering::AcqRel) {
            return;
        }
        let this = self.clone();
        tokio::spawn(async move {
            let target = this.inner.cfg.capacity_bytes / 100
                * u64::from(this.inner.cfg.low_watermark_pct.min(100));
            let r = this.evict(target).await;
            debug!(freed = r.freed, deferred = r.deferred, "cache eviction");
            this.inner.evicting.store(false, Ordering::Release);
        });
    }

    /// Evict least recently used chunks, then headers, until the cache
    /// holds at most `budget` bytes (or nothing more can be evicted now).
    pub async fn evict(&self, budget: u64) -> EvictReport {
        let i = &self.inner;
        let _one = i.evict_lock.lock().await;
        let mut report = EvictReport::default();
        // Bounded passes: deferred victims move to the back each time.
        for _ in 0..4 {
            let need = i.index.lock().unwrap().used().saturating_sub(budget);
            if need == 0 {
                break;
            }
            let victims = i.index.lock().unwrap().victims(need);
            if victims.is_empty() {
                break;
            }
            let mut progressed = false;
            for v in victims {
                let (file_id, meta) = match &v {
                    Victim::Chunks { file_id, meta, .. } | Victim::File { file_id, meta, .. } => {
                        (*file_id, meta.clone())
                    }
                };
                let gate = self.gate(file_id);
                let Ok(_g) = gate.try_lock() else {
                    report.deferred += 1;
                    i.index.lock().unwrap().retry_later(&v);
                    continue;
                };
                let r = match &v {
                    Victim::Chunks { chunks, .. } => {
                        chunks::narrow_chunks(
                            &i.layout,
                            &meta.rel,
                            file_id,
                            meta.version_id,
                            chunks,
                            &*i.notifier,
                            &i.seq,
                            &i.locks,
                        )
                        .await
                    }
                    Victim::File { .. } => {
                        chunks::evict_file(
                            &i.layout,
                            &meta.rel,
                            file_id,
                            meta.version_id,
                            meta.floor,
                            &*i.notifier,
                            &i.seq,
                            &i.locks,
                        )
                        .await
                    }
                };
                let mut ix = i.index.lock().unwrap();
                match r {
                    Ok(ChunkNarrow::Punched { freed }) => {
                        progressed = true;
                        report.freed += freed;
                        match &v {
                            Victim::Chunks { chunks, .. } => {
                                i.evictions
                                    .fetch_add(chunks.len() as u64, Ordering::Relaxed);
                                ix.remove_chunks(file_id, chunks);
                            }
                            Victim::File { .. } => {
                                i.evictions.fetch_add(1, Ordering::Relaxed);
                                ix.forget(file_id);
                            }
                        }
                    }
                    Ok(ChunkNarrow::NotAcked | ChunkNarrow::Busy) => {
                        report.deferred += 1;
                        ix.retry_later(&v);
                    }
                    Ok(ChunkNarrow::Changed) => {
                        // Not ours any more (another version, resident, gone).
                        progressed = true;
                        ix.forget(file_id);
                    }
                    Err(e) => {
                        warn!(file_id, error = %e, "eviction failed");
                        report.deferred += 1;
                        ix.retry_later(&v);
                    }
                }
            }
            if !progressed {
                break;
            }
        }
        report
    }

    /// Index a placeholder found on disk at start (its header prefix and
    /// chunk bits). Files that are not `PARTIAL` placeholders of `file_id`
    /// are ignored.
    pub fn adopt_from_disk(&self, rel: &Path, file_id: u64, floor: u64) -> io::Result<bool> {
        let Some((m, map)) = chunks::read_state(&self.inner.layout, rel)? else {
            return Ok(false);
        };
        if m.file_id != file_id || m.state != State::Partial || m.flags.contains(Flags::READONLY) {
            return Ok(false);
        }
        // Nothing cached above the catalog's prefix: nothing to index (a
        // placeholder per catalog file must not cost index memory).
        if m.resident_bytes <= floor && !m.flags.contains(Flags::CHUNK_MAP) {
            return Ok(false);
        }
        let shift = map.as_ref().map_or_else(
            || chunks::chunk_shift_for(m.size, self.inner.cfg.chunk_shift),
            |map| map.shift,
        );
        let mut ix = self.inner.index.lock().unwrap();
        ix.set_meta(
            file_id,
            FileMeta {
                rel: rel.to_owned(),
                version_id: m.version_id,
                size: m.size,
                shift,
                floor,
            },
        );
        ix.set_header(file_id, m.resident_bytes.saturating_sub(floor));
        if let Some(map) = map {
            for c in 0..omnifile_marker::ChunkMap::chunk_count(m.size, map.shift) {
                if map.is_set(c)
                    && let Some((s, l)) = chunk_range(m.size, map.shift, c)
                    && s >= m.resident_bytes
                {
                    ix.touch_chunk(file_id, c, l);
                }
            }
        }
        Ok(true)
    }

    /// Stop tracking a file (it became resident, was deleted or replaced).
    pub fn forget(&self, file_id: u64) {
        self.inner.index.lock().unwrap().forget(file_id);
        self.inner.planner.lock().unwrap().forget(file_id);
    }

    /// Queue a background header fetch (`prefetch_headers` datasets, after
    /// materialisation). Run [`Cache::run_header_prefetcher`] to drain it.
    pub fn queue_header_prefetch(&self, mut req: FetchRequest) {
        if req.header_bytes == 0 {
            return;
        }
        req.offset = 0;
        req.len = req.header_bytes.min(req.size);
        self.inner.prefetch.lock().unwrap().push_back(req);
        self.inner.prefetch_wake.notify_one();
    }

    pub fn queued_header_prefetches(&self) -> usize {
        self.inner.prefetch.lock().unwrap().len()
    }

    /// Drain the header prefetch queue forever, at most
    /// `header_prefetch_per_sec` starts a second. Returns at once if the
    /// rate is 0.
    pub async fn run_header_prefetcher(&self) {
        let mut cap = RateCap::new(self.inner.cfg.header_prefetch_per_sec);
        if !cap.enabled() {
            return;
        }
        loop {
            let next = self.inner.prefetch.lock().unwrap().pop_front();
            let Some(req) = next else {
                self.inner.prefetch_wake.notified().await;
                continue;
            };
            if let Some(d) = cap.reserve(Instant::now())
                && !d.is_zero()
            {
                tokio::time::sleep(d).await;
            }
            match self.fetch_demanded(&req).await {
                Ok(FetchOutcome::Busy) => {}
                Ok(_) => {}
                Err(e) => debug!(file_id = req.file_id, error = %e, "header prefetch failed"),
            }
            self.maybe_evict();
        }
    }

    pub fn stats(&self) -> CacheStats {
        let i = &self.inner;
        let ix = i.index.lock().unwrap();
        CacheStats {
            used_bytes: ix.used(),
            capacity_bytes: i.cfg.capacity_bytes,
            header_bytes: ix.header_used(),
            hits: i.hits.load(Ordering::Relaxed),
            misses: i.misses.load(Ordering::Relaxed),
            evictions: i.evictions.load(Ordering::Relaxed),
        }
    }

    /// Bytes read from each backend since the last call (`remote_reads`).
    pub fn take_remote_reads(&self) -> Vec<(u64, u64)> {
        let mut m = self.inner.egress.lock().unwrap();
        let mut v: Vec<_> = m.drain().filter(|(_, b)| *b > 0).collect();
        v.sort_unstable();
        v
    }
}
