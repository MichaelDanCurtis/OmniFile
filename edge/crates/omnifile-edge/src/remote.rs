//! M5: chunk-range reads of `ONLINE` placeholders through the edge's SSD
//! cache (`omnifile-cache`; `fsal-socket.md` §6.3 "edge-local fetches",
//! `control-api.md` §4.4 "remote objects"). Port of line B's `remote.rs`
//! onto line A's recall table and ports.
//!
//! A `READ_INTENT` for an `ONLINE` range, from an FSAL that negotiated
//! `CHUNK_MAP` (v0.2), or an smb-guard read of one, is not a service recall:
//! the daemon answers `PARK` (or `DELAY`) and hands the range to the cache.
//! The cache fills the chunks the range touches into the placeholder
//! (bytes, `fdatasync`, map, then marker) under the fill fence, and the
//! daemon pushes `RECALL_STATE` `FETCHING` (`recall_id = 0`) after every
//! widening, which resumes every parked read so its worker re-checks the
//! marker and map; `DONE` follows once nothing is in flight for the file.
//! Nothing is reported to the service until every byte is local and the
//! whole file matches the catalog's CRC-64/NVME (`POST …/files/{id}/filled`,
//! the M1 online fetch's report).
//!
//! Where the bytes come from is the service's read credential for the
//! content version (`POST …/credentials`, cached by the control client):
//! an Azure SAS (each ≤ 4 MiB range CRC-checked), or, for S3, the
//! `s3_roles_anywhere` pointer the edge signs itself (presigned URLs only as
//! the operator's fallback). Every S3 read is pinned by version id, else by
//! ETag (`If-Match`); a pointer with neither is never read. A `412` means
//! the source object changed: the cache narrows the file back to the
//! catalog's prefix (acknowledged `INVALIDATE`), the read fails closed, and
//! the edge sends the audit record `remote.source_changed`.
//!
//! Archived objects, an unreachable service, rewind placeholders and
//! operations that need the whole file (writes, truncates) keep line A's
//! recall and whole-file fill path. So does an FSAL that negotiated v0.1:
//! it reads the prefix only, and the whole-file fill serves it.

use std::collections::{HashMap, HashSet, VecDeque};
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::{Arc, Mutex, Weak};

use bytes::{Bytes, BytesMut};
use omnifile_blob::{BlobError, RangeReader, ReadTarget, S3Object};
use omnifile_cache::{Cache, CacheConfig, CacheError, FetchOutcome, FetchRequest};
use omnifile_fsal_proto::ConnInfo;
use omnifile_fsal_proto::wire::{
    Decision, ObjRef, ReadDecision, ReadIntent, RecallPhase, feature, reason,
};
use omnifile_stub::{FsalNotifier, InodeKey, SourceKind};
use tracing::{debug, info, warn};

use crate::core::{Core, RecallEntry, RecallKey, Terminal};
use crate::ports::{BlobIo, ControlPlane, ReadUrl, RecallRequest};

/// Largest Azure range whose CRC-64 Azure returns (`MAX_RANGE_CRC`).
const AZURE_PIECE: u64 = omnifile_blob::client::MAX_RANGE_CRC;

/// The cache as the daemon calls it (a trait object, so [`Core`] stays
/// generic only over the control plane and blob ports).
type Planned = Result<(FetchOutcome, Option<FetchRequest>), CacheError>;

pub trait RemoteFetch: Send + Sync + 'static {
    /// A demanded range, and the read-ahead request the caller fetches next
    /// under its own fill fence ([`RemoteFetch::fetch_ahead`]).
    fn fetch<'a>(&'a self, req: FetchRequest)
    -> Pin<Box<dyn Future<Output = Planned> + Send + 'a>>;
    fn fetch_ahead<'a>(
        &'a self,
        req: FetchRequest,
    ) -> Pin<Box<dyn Future<Output = Result<FetchOutcome, CacheError>> + Send + 'a>>;
    /// Index a placeholder found on disk (the startup scan).
    fn adopt(&self, rel: &std::path::Path, file_id: u64, floor: u64) -> std::io::Result<bool>;
    fn hold_admission(&self);
    fn open_admission(&self);
    fn enforce_budget<'a>(&'a self) -> Pin<Box<dyn Future<Output = ()> + Send + 'a>>;
    /// Drop what the cache knows of a file (its content changed).
    fn forget(&self, file_id: u64);
    /// For the heartbeat.
    fn stats(&self) -> omnifile_cache::CacheStats;
}

impl<R, N> RemoteFetch for Cache<R, N>
where
    R: RangeReader + 'static,
    N: FsalNotifier + 'static,
{
    fn fetch<'a>(
        &'a self,
        req: FetchRequest,
    ) -> Pin<Box<dyn Future<Output = Planned> + Send + 'a>> {
        Box::pin(async move { Cache::fetch_planned(self, &req).await })
    }

    fn fetch_ahead<'a>(
        &'a self,
        req: FetchRequest,
    ) -> Pin<Box<dyn Future<Output = Result<FetchOutcome, CacheError>> + Send + 'a>> {
        Box::pin(async move { Cache::fetch_ahead(self, &req).await })
    }

    fn adopt(&self, rel: &std::path::Path, file_id: u64, floor: u64) -> std::io::Result<bool> {
        Cache::adopt_from_disk(self, rel, file_id, floor)
    }

    fn hold_admission(&self) {
        Cache::hold_admission(self)
    }

    fn open_admission(&self) {
        Cache::open_admission(self)
    }

    fn enforce_budget<'a>(&'a self) -> Pin<Box<dyn Future<Output = ()> + Send + 'a>> {
        Box::pin(async move { Cache::enforce_budget(self).await })
    }

    fn forget(&self, file_id: u64) {
        Cache::forget(self, file_id)
    }

    fn stats(&self) -> omnifile_cache::CacheStats {
        Cache::stats(self)
    }
}

/// The daemon's [`FsalNotifier`] for the cache, holding the core weakly
/// (the core owns the cache).
pub struct CoreNotifier<C: ControlPlane, B: BlobIo>(pub Weak<Core<C, B>>);

impl<C: ControlPlane, B: BlobIo> FsalNotifier for CoreNotifier<C, B> {
    async fn invalidate(&self, key: InodeKey, what: u32, ack: bool) -> bool {
        match self.0.upgrade() {
            Some(c) => FsalNotifier::invalidate(&*c, key, what, ack).await,
            None => false,
        }
    }
    fn connection_epoch(&self) -> u64 {
        self.0
            .upgrade()
            .map_or(0, |c| FsalNotifier::connection_epoch(&*c))
    }
    fn fill_progress(&self, key: InodeKey, version_id: u64, resident_bytes: u64) {
        if let Some(c) = self.0.upgrade() {
            c.fill_progress(key, version_id, resident_bytes)
        }
    }
    async fn begin_fill(&self, key: InodeKey, version_id: u64) -> Option<u64> {
        let c = self.0.upgrade()?;
        c.begin_fill(key, version_id).await
    }
    fn fence_holds(&self, epoch: u64) -> bool {
        self.0.upgrade().is_some_and(|c| c.fence_holds(epoch))
    }
    fn fill_cancelled(&self, key: InodeKey, version_id: u64) -> bool {
        self.0
            .upgrade()
            .is_none_or(|c| c.fill_cancelled(key, version_id))
    }
    fn before_narrow(&self, key: InodeKey) -> std::io::Result<()> {
        match self.0.upgrade() {
            Some(c) => c.before_narrow(key),
            None => Err(std::io::Error::other("daemon gone")),
        }
    }
}

/// The cache's reader over the daemon's [`BlobIo`] port, so the fakes (and
/// the production adapter, `io::ServiceBlob`) serve the cache as they serve
/// whole-file fills.
pub struct PortReader<B>(pub Arc<B>);

/// The [`BlobError`] an I/O error carries, or one that wraps it: the cache
/// tells archived, refused, changed-source and unsupported reads apart by it.
fn blob_error(e: std::io::Error) -> BlobError {
    if e.get_ref().is_some_and(|i| i.is::<BlobError>()) {
        match e.into_inner().map(|i| i.downcast::<BlobError>()) {
            Some(Ok(b)) => return *b,
            Some(Err(other)) => return BlobError::Io(std::io::Error::other(other)),
            None => unreachable!("checked above"),
        }
    }
    BlobError::Io(e)
}

impl<B: BlobIo> RangeReader for PortReader<B> {
    async fn read_range(
        &self,
        target: &ReadTarget,
        offset: u64,
        len: u64,
    ) -> omnifile_blob::Result<Bytes> {
        let (url, source) = match target {
            ReadTarget::AzureSas { url } => (url.clone(), SourceKind::AzureSas),
            ReadTarget::S3Presigned { url } => (url.clone(), SourceKind::S3Presigned),
            ReadTarget::S3(o) => (o.to_pointer(), SourceKind::S3Signed),
            ReadTarget::Unsupported { mode } => {
                return Err(BlobError::Unsupported(format!("backend mode {mode:?}")));
            }
        };
        // Azure verifies a range's CRC only up to 4 MiB: split, so every byte
        // read from Azure is checked on arrival whatever the chunk size.
        let piece = if source == SourceKind::AzureSas {
            AZURE_PIECE
        } else {
            len.max(1)
        };
        let end = offset
            .checked_add(len)
            .ok_or_else(|| BlobError::Invalid("range overflows".into()))?;
        let mut buf = BytesMut::with_capacity(len as usize);
        let mut at = offset;
        while at < end {
            let n = piece.min(end - at);
            let b = self
                .0
                .get_range(&url, source, at, n)
                .await
                .map_err(blob_error)?;
            if b.len() as u64 != n {
                return Err(BlobError::BadResponse {
                    op: "read_range",
                    detail: format!("asked {n} bytes at {at}, got {}", b.len()),
                });
            }
            buf.extend_from_slice(&b);
            at += n;
        }
        Ok(buf.freeze())
    }
}

/// The cache's read target for a read credential.
pub fn read_target(u: &ReadUrl) -> ReadTarget {
    match u.source {
        SourceKind::AzureSas => ReadTarget::AzureSas { url: u.url.clone() },
        SourceKind::S3Presigned => ReadTarget::S3Presigned { url: u.url.clone() },
        SourceKind::S3Signed => match S3Object::from_pointer(&u.url) {
            Ok(o) => ReadTarget::S3(o),
            Err(e) => ReadTarget::Unsupported {
                mode: format!("bad S3 pointer: {e}"),
            },
        },
    }
}

/// Ranges `(offset, len, read_ahead)` waiting, per key; one worker per key
/// drains them (the cache serialises fills of a file anyway). Read-ahead
/// runs in the same worker, so it holds the same fill fence (review on #23).
/// `(offset, len, read_ahead)` ranges per key.
type Queued = HashMap<RecallKey, VecDeque<(u64, u64, bool)>>;

#[derive(Default)]
pub struct FetchQueue {
    q: Mutex<Queued>,
}

impl FetchQueue {
    /// Queue a demanded range; `true` if no worker runs for the key (start
    /// one). Demand goes before any read-ahead already queued.
    pub fn push(&self, key: RecallKey, off: u64, len: u64) -> bool {
        self.push_kind(key, off, len, false)
    }

    /// Queue read-ahead for a running worker (never starts one).
    fn push_ahead(&self, key: RecallKey, off: u64, len: u64) {
        if let Some(v) = self.q.lock().unwrap().get_mut(&key)
            && !v.iter().any(|r| (r.0, r.1) == (off, len))
        {
            v.push_back((off, len, true));
        }
    }

    fn push_kind(&self, key: RecallKey, off: u64, len: u64, ahead: bool) -> bool {
        let mut q = self.q.lock().unwrap();
        match q.get_mut(&key) {
            Some(v) => {
                if !v.iter().any(|r| (r.0, r.1) == (off, len)) {
                    match v.iter().position(|r| r.2) {
                        Some(i) if !ahead => v.insert(i, (off, len, ahead)),
                        _ => v.push_back((off, len, ahead)),
                    }
                }
                false
            }
            None => {
                q.insert(key, VecDeque::from([(off, len, ahead)]));
                true
            }
        }
    }

    /// The next range, or `None` (and the key is retired) when empty.
    fn next(&self, key: RecallKey) -> Option<(u64, u64, bool)> {
        let mut q = self.q.lock().unwrap();
        let v = q.get_mut(&key)?;
        match v.pop_front() {
            Some(r) => Some(r),
            None => {
                q.remove(&key);
                None
            }
        }
    }

    /// Retire the key at once, dropping what waits.
    fn clear(&self, key: RecallKey) {
        self.q.lock().unwrap().remove(&key);
    }

    /// A worker runs for the key.
    pub fn has(&self, key: RecallKey) -> bool {
        self.q.lock().unwrap().contains_key(&key)
    }

    pub fn len(&self) -> usize {
        self.q.lock().unwrap().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// Why a fetch can't be done by the cache.
#[derive(Debug)]
enum Refused {
    /// The service's recall flow handles it (archived, unreachable, a
    /// credential it refused).
    Recall(String),
    /// Fail closed (EIO for a while), with this `READ_DECISION` reason.
    Fail(String, u8),
}

/// Start the cache of remote objects (`[cache]`), reading through the
/// daemon's blob port.
pub fn install_cache<C: ControlPlane, B: BlobIo>(core: &Arc<Core<C, B>>) {
    if !core.cfg.cache.enabled {
        info!("remote-object cache off: ONLINE reads fill whole files");
        return;
    }
    let c = &core.cfg.cache;
    let cfg = CacheConfig {
        capacity_bytes: if c.capacity_mib > 0 {
            c.capacity_mib << 20
        } else {
            c.capacity_gib << 30
        },
        low_watermark_pct: c.low_watermark_pct,
        chunk_shift: c.chunk_shift,
        readahead_max_chunks: c.readahead_max_chunks,
        // Line A has no `prefetch_headers` in the feed yet: headers come in
        // on first access only.
        header_prefetch_per_sec: 0.0,
    };
    let notifier = Arc::new(CoreNotifier(Arc::downgrade(core)));
    let cache = Cache::new(
        cfg,
        core.layout.clone(),
        Arc::new(PortReader(core.blob.clone())),
        notifier,
        core.seq.clone(),
        core.locks.clone(),
    );
    let cache: Arc<dyn RemoteFetch> = Arc::new(cache);
    // Index what is cached on disk before admitting new bytes, so a restart
    // never admits another budget on top of an uncounted one (review on
    // #23). Reads wait (park) meanwhile.
    cache.hold_admission();
    let _ = core.remote.set(cache.clone());
    let this = core.clone();
    tokio::spawn(async move { this.adopt_cached(cache).await });
    info!(
        capacity_gib = c.capacity_gib,
        chunk_mib = (1u64 << c.chunk_shift) >> 20,
        "remote-object cache ready"
    );
}

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// The startup index: every `ONLINE` placeholder on disk with cached
    /// bytes (chunks, or a prefix above the catalog's) is counted against
    /// the budget, then the cache evicts down to it and admits new bytes.
    /// A page at a time, the xattr reads off the async workers.
    async fn adopt_cached(self: Arc<Self>, cache: Arc<dyn RemoteFetch>) {
        const PAGE: usize = 1000;
        let (mut after, mut seen, mut adopted) = (0i64, 0u64, 0u64);
        loop {
            if self.is_shutting_down() {
                break;
            }
            let page: Vec<(PathBuf, u64, u64, i64)> = {
                let rep = self.replica.lock().await;
                match rep.online_files_on_disk_after(after, PAGE) {
                    Ok(nodes) => nodes
                        .into_iter()
                        .filter_map(|n| {
                            let rel = rep.fs_path(n.lid).ok().flatten()?;
                            Some((rel, n.file_id?, n.stub_resident_bytes.unwrap_or(0), n.lid))
                        })
                        .collect(),
                    Err(e) => {
                        warn!(err = %e, "cache startup index: replica unreadable");
                        break;
                    }
                }
            };
            let Some(last) = page.last().map(|p| p.3) else {
                break;
            };
            after = last;
            seen += page.len() as u64;
            let c = cache.clone();
            adopted += tokio::task::spawn_blocking(move || {
                page.iter()
                    .filter(|(rel, f, floor, _)| c.adopt(rel, *f, *floor).unwrap_or(false))
                    .count() as u64
            })
            .await
            .unwrap_or(0);
        }
        cache.enforce_budget().await;
        cache.open_admission();
        info!(seen, adopted, "cache startup index done");
    }

    /// The chunk path applies: an `ONLINE` read of the live file (not a
    /// rewind placeholder) that needs no full fill, from an FSAL that reads
    /// chunk maps, with the cache on.
    pub(crate) fn chunk_read(&self, conn: &ConnInfo, ri: &ReadIntent, key: RecallKey) -> bool {
        key.2 == 0
            && ri.marker_flags & 1 != 0
            && !ri.op.needs_full_fill()
            && conn.features & feature::CHUNK_MAP != 0
            && self.remote.get().is_some()
    }

    /// The chunk path of [`Core::on_read_intent`].
    pub(crate) fn on_remote_read(
        self: &Arc<Self>,
        conn: &ConnInfo,
        ri: &ReadIntent,
        key: RecallKey,
    ) -> ReadDecision {
        let requester = (ri.identity.uid, ri.identity.client_addr());
        match self.join_remote(key, ri.obj, ri.version_id, ri.resident_bytes, requester) {
            Joined::Failed(why) => ReadDecision::simple(Decision::Eio, why),
            Joined::Recall(e) => ReadDecision::simple(e.answer, reason::RECALL_IN_PROGRESS),
            Joined::Queued => {
                self.queue_remote_fetch(key, ri.obj, ri.offset, ri.length);
                ReadDecision {
                    decision: if conn.features & feature::PARK != 0 {
                        Decision::Park
                    } else {
                        Decision::Delay
                    },
                    reason: reason::FETCH_IN_PROGRESS,
                    eta_s: ReadDecision::ETA_UNKNOWN,
                    ttl_ms: 0,
                    recall_id: 0,
                    resident_bytes: ri.resident_bytes,
                }
            }
        }
    }

    /// The smb-guard's chunk path: queue `[start, end)`. `Ok`: the reader
    /// waits for the map to cover it. `Err(None)`: the fetch failed (deny).
    /// `Err(Some(answer))`: a service recall of this version runs, and its
    /// answer stands.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn guard_remote_read(
        self: &Arc<Self>,
        key: RecallKey,
        obj: ObjRef,
        version_id: u64,
        resident: u64,
        requester: (u32, Option<std::net::IpAddr>),
        start: u64,
        end: u64,
    ) -> Result<(), Option<Decision>> {
        match self.join_remote(key, obj, version_id, resident, requester) {
            Joined::Failed(_) => Err(None),
            Joined::Recall(e) => Err(Some(e.answer)),
            Joined::Queued => {
                self.queue_remote_fetch(key, obj, start, end.saturating_sub(start));
                Ok(())
            }
        }
    }

    /// Join (or start) the edge-local fetch of `key`.
    fn join_remote(
        self: &Arc<Self>,
        key: RecallKey,
        obj: ObjRef,
        version_id: u64,
        resident: u64,
        requester: (u32, Option<std::net::IpAddr>),
    ) -> Joined {
        let existing = self.recalls.lock().unwrap().get(&key).cloned();
        match existing {
            Some(e) if e.phase == RecallPhase::Failed => Joined::Failed(
                self.fetch_reasons
                    .lock()
                    .unwrap()
                    .get(&key)
                    .copied()
                    .unwrap_or(reason::RESTORE_FAILED),
            ),
            // A service recall (or a whole-file fill) of this version runs:
            // its answers stand, and its fill brings the bytes.
            Some(e) if !self.fetches.has(key) => Joined::Recall(e),
            Some(_) => {
                if let Some(e) = self.recalls.lock().unwrap().get_mut(&key) {
                    e.requesters.insert(requester);
                }
                Joined::Queued
            }
            None => {
                // Register the fill before the entry exists, so the orphan
                // sweep never sees FETCHING without a fill.
                if !self.fills.start(key) {
                    return Joined::Recall(RecallEntry {
                        obj,
                        version_id,
                        recall_id: 0,
                        phase: RecallPhase::Fetching,
                        answer: Decision::Delay,
                        eta_s: ReadDecision::ETA_UNKNOWN,
                        resident,
                        online: true,
                        requesters: HashSet::new(),
                    });
                }
                self.fetch_reasons.lock().unwrap().remove(&key);
                self.recalls.lock().unwrap().insert(
                    key,
                    RecallEntry {
                        obj,
                        version_id,
                        recall_id: 0,
                        phase: RecallPhase::Fetching,
                        answer: Decision::Delay,
                        eta_s: ReadDecision::ETA_UNKNOWN,
                        resident,
                        online: true,
                        requesters: HashSet::from([requester]),
                    },
                );
                Joined::Queued
            }
        }
    }

    /// Queue `[off, off+len)` of `key`'s object and make sure a worker
    /// drains the queue. The caller registered the fill for a new key.
    pub(crate) fn queue_remote_fetch(
        self: &Arc<Self>,
        key: RecallKey,
        obj: ObjRef,
        off: u64,
        len: u64,
    ) {
        if self.fetches.push(key, off, len) {
            let this = self.clone();
            let task = async move { this.fetch_worker(key, obj).await };
            match tokio::runtime::Handle::try_current() {
                Ok(h) => drop(h.spawn(task)),
                Err(_) => match self.rt.get() {
                    // From an smb-guard thread.
                    Some(h) => drop(h.spawn(task)),
                    None => {
                        warn!(file_id = key.0, "no runtime for an edge-local fetch");
                        self.fetches.clear(key);
                    }
                },
            }
        }
    }

    async fn fetch_worker(self: Arc<Self>, key: RecallKey, obj: ObjRef) {
        let Some(cache) = self.remote.get().cloned() else {
            self.fetches.clear(key);
            return;
        };
        if !self.fills.is_running(key) && !self.fills.start(key) {
            // A whole-file fill of this version runs: it brings the bytes.
            self.fetches.clear(key);
            return;
        }
        let mut escalate: Option<String> = None;
        let mut failed: Option<(String, u8)> = None;
        while let Some((off, len, ahead)) = self.fetches.next(key) {
            if self.is_shutting_down() || self.fills.stopping(key) {
                self.fetches.clear(key);
                break;
            }
            let (req, node) = match self.fetch_request(key, off, len).await {
                Ok(r) => r,
                Err(Refused::Recall(why)) => {
                    escalate = Some(why);
                    self.fetches.clear(key);
                    break;
                }
                Err(Refused::Fail(why, r)) => {
                    failed = Some((why, r));
                    self.fetches.clear(key);
                    break;
                }
            };
            let rel = req.rel.clone();
            let got = if ahead {
                cache.fetch_ahead(req).await.map(|o| (o, None))
            } else {
                cache.fetch(req).await
            };
            let got = match got {
                Ok((o, next)) => {
                    if let Some(n) = next {
                        // Read-ahead, in this worker: under this fill's fence.
                        self.fetches.push_ahead(key, n.offset, n.len);
                    }
                    Ok(o)
                }
                // A read-ahead that fails is not the reader's failure (the
                // reader's own range was fetched), unless the source changed:
                // the cache has discarded the file, and reads fail closed.
                Err(e) if ahead && !e.is_source_changed() => {
                    debug!(file_id = key.0, error = %e, "read-ahead failed");
                    continue;
                }
                Err(e) => Err(e),
            };
            match got {
                Ok(FetchOutcome::Local) | Ok(FetchOutcome::Filled { .. }) => {
                    // A widening: parked reads re-check the marker and map.
                    self.update(key, |_| {});
                }
                Ok(FetchOutcome::Complete(done)) => {
                    self.update(key, |e| e.resident = done.size);
                    info!(file_id = key.0, version_id = key.1, path = %rel.display(),
                          "object wholly local and verified");
                    self.after_complete(key, &node, &rel, done).await;
                }
                Ok(FetchOutcome::Busy) => {
                    // A stub or eviction holds the file: once more, shortly.
                    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                    self.fetches.push_kind(key, off, len, ahead);
                }
                Err(e) if e.is_archived() => {
                    escalate = Some("archived".into());
                    self.fetches.clear(key);
                    break;
                }
                Err(e) if e.is_auth() => {
                    // The cached credential was refused: a new one next time.
                    escalate = Some(format!("credential refused: {e}"));
                    self.fetches.clear(key);
                    break;
                }
                Err(e) => {
                    failed = Some(self.on_fetch_error(key, &node, &e).await);
                    self.fetches.clear(key);
                    break;
                }
            }
        }
        let default = match &failed {
            Some(_) => Terminal {
                phase: RecallPhase::Failed,
                answer: Decision::Eio,
            },
            None => Terminal {
                phase: RecallPhase::Done,
                answer: Decision::Delay,
            },
        };
        if let Some((why, r)) = &failed {
            warn!(
                file_id = key.0,
                version_id = key.1,
                why,
                "edge-local fetch failed"
            );
            self.fetch_reasons.lock().unwrap().insert(key, *r);
        }
        let escalating = escalate.is_some() && failed.is_none() && !self.is_shutting_down();
        self.fills.finish_with(key, default, |t| {
            if escalating && t.phase == RecallPhase::Done {
                // The recall path takes over the entry (below); set while the
                // tracker is locked, so the orphan sweep never sees a
                // FETCHING entry without its fill.
                if let Some(e) = self.recalls.lock().unwrap().get_mut(&key) {
                    e.phase = RecallPhase::Queued;
                    e.online = false;
                }
                return;
            }
            if t.phase == RecallPhase::Done {
                self.update(key, |e| e.phase = RecallPhase::Done);
                self.recalls.lock().unwrap().remove(&key);
            } else {
                self.end_with(key, t);
            }
        });
        if escalating {
            let why = escalate.unwrap_or_default();
            debug!(
                file_id = key.0,
                why, "remote read goes to the service's recall flow"
            );
            let this = self.clone();
            let req = self.edge_request(key);
            let _ = obj;
            tokio::spawn(async move { this.start_recall_with(key, req, Decision::Delay).await });
        }
    }

    /// The request a recall started by an edge-local fetch carries: the
    /// edge's own (`uid` 0, `via = local`, claimed), as the M2 repair fill
    /// sends it. The service records it as the edge's, never a person's.
    fn edge_request(&self, key: RecallKey) -> RecallRequest {
        RecallRequest {
            file_id: key.0,
            version_id: key.1,
            offset: 0,
            length: 0,
            uid: Some(0),
            sec: None,
            proof: "claimed".into(),
            client_addr: None,
            on_open: "wait".into(),
            via: "local".into(),
            sid: None,
            pinned: key.2 != 0,
        }
    }

    /// The whole file became local and verified: it is the catalog's now.
    /// The replica's bookkeeping is a whole-file fill's (`run_fill`), and
    /// the service is told as for an online fetch.
    async fn after_complete(
        self: &Arc<Self>,
        key: RecallKey,
        node: &omnifile_replica::Node,
        rel: &std::path::Path,
        done: omnifile_stub::fill::FillDone,
    ) {
        let dirty = {
            let mut rep = self.replica.lock().await;
            let was_dirty = rep
                .node_by_file_id(key.0)
                .ok()
                .flatten()
                .is_some_and(|n| n.local_dirty);
            match done.ctime_ns {
                Some(c) if !was_dirty => {
                    let _ = rep.set_fs_ctime(node.lid, c);
                    false
                }
                _ => {
                    let _ = rep.clear_fs_ctime(node.lid);
                    let _ = rep.set_local_dirty(node.lid, true);
                    true
                }
            }
        };
        if dirty {
            self.commit_modified(rel).await;
        }
        self.clone().report_fetch_filled(key.0, key.1, done.size);
    }

    /// Build the cache request for a range of `key`'s object.
    async fn fetch_request(
        &self,
        key: RecallKey,
        off: u64,
        len: u64,
    ) -> Result<(FetchRequest, omnifile_replica::Node), Refused> {
        let fail = |s: &str| Refused::Fail(s.to_owned(), reason::UNKNOWN_FILE);
        let (node, rel) = {
            let rep = self.replica.lock().await;
            let node = rep
                .node_by_file_id(key.0)
                .map_err(|e| fail(&e.to_string()))?
                .filter(|n| !n.deleted)
                .ok_or_else(|| fail("file unknown to the replica"))?;
            if node.content_version_id != Some(key.1) {
                return Err(fail("the content version changed"));
            }
            let rel: PathBuf = rep
                .fs_path(node.lid)
                .map_err(|e| fail(&e.to_string()))?
                .ok_or_else(|| fail("not materialised"))?;
            (node, rel)
        };
        let url = match self.control.read_url(key.0, key.1).await {
            Ok(u) => u,
            Err(e) if e.unavailable => {
                return Err(Refused::Recall(format!(
                    "service unreachable: {}",
                    e.message
                )));
            }
            Err(e) if e.code.as_deref() == Some("unsupported_backend") => {
                return Err(Refused::Fail(e.message, reason::UNSUPPORTED_BACKEND));
            }
            Err(e) => return Err(Refused::Recall(e.message)),
        };
        let target = read_target(&url);
        if let ReadTarget::Unsupported { mode } = &target {
            return Err(Refused::Fail(mode.clone(), reason::UNSUPPORTED_BACKEND));
        }
        if matches!(target, ReadTarget::S3Presigned { .. }) && !target.is_pinned() {
            // A presigned URL of an unversioned object with no signed
            // If-Match: two range reads could come from two objects. No
            // chunk reads; the whole-file fill (verified against the CRC
            // before any byte is exposed) serves it (review on #23).
            return Err(Refused::Recall(
                "presigned URL pins no version or ETag: whole-file fill".into(),
            ));
        }
        let backend_id = node
            .location
            .as_ref()
            .and_then(|l| l.get("backend_id"))
            .and_then(|v| v.as_str().and_then(|s| s.parse().ok()).or(v.as_u64()))
            .unwrap_or(0);
        // A stub's prefix is the catalog's (kept under a lease): the cache
        // never evicts below it, and needs no header cache of its own.
        let floor = node.stub_resident_bytes.unwrap_or(0);
        let header_bytes = if floor > 0 {
            0
        } else {
            self.cfg.cache.header_mib << 20
        };
        Ok((
            FetchRequest {
                rel,
                expect_ino: node.ino.zip(node.ino_gen),
                file_id: key.0,
                version_id: key.1,
                size: node.size,
                crc64nvme: node.crc64nvme.or(url.crc64nvme),
                backend_id,
                target,
                offset: off,
                len: len.min(node.size.saturating_sub(off)),
                header_bytes,
                floor,
            },
            node,
        ))
    }

    /// A fetch failed: the read fails closed. A changed source (`412`) is
    /// reported; the cache has already narrowed the file.
    async fn on_fetch_error(
        &self,
        key: RecallKey,
        node: &omnifile_replica::Node,
        e: &CacheError,
    ) -> (String, u8) {
        if e.is_source_changed() {
            let loc = node.location.clone().unwrap_or_default();
            let s = |k: &str| {
                loc.get(k)
                    .and_then(|v| v.as_str())
                    .unwrap_or_default()
                    .to_owned()
            };
            let rec = serde_json::json!({
                "at": omnifile_control::util::format_time(std::time::SystemTime::now()),
                "action": "remote.source_changed",
                "decision": "info",
                "proof": "claimed",
                "file_id": key.0.to_string(),
                "details": {
                    "version_id": key.1.to_string(),
                    "bucket": s("container"),
                    "key": s("object_key"),
                    "etag": s("etag"),
                },
            });
            // Through the replica's audit queue: kept while the service is away.
            let mut pending = self.audit_pending.lock().unwrap();
            pending.push(rec);
            return (
                "the source object changed (If-Match refused); cached bytes discarded".into(),
                reason::RESTORE_FAILED,
            );
        }
        if e.is_unsupported() {
            return (
                "unsupported backend: upgrade the edge".into(),
                reason::UNSUPPORTED_BACKEND,
            );
        }
        if e.is_unpinned() {
            return (
                "S3 object with neither a version id nor an ETag: never read".into(),
                reason::NO_CREDENTIAL,
            );
        }
        (e.to_string(), reason::RESTORE_FAILED)
    }
}

enum Joined {
    Queued,
    Recall(RecallEntry),
    Failed(u8),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_queue_runs_one_worker_per_key_and_dedupes() {
        let q = FetchQueue::default();
        let k = (1, 2, 0);
        assert!(q.push(k, 0, 10));
        assert!(!q.push(k, 0, 10));
        assert!(!q.push(k, 10, 10));
        assert!(q.has(k));
        assert_eq!(q.next(k), Some((0, 10, false)));
        assert_eq!(q.next(k), Some((10, 10, false)));
        assert_eq!(q.next(k), None);
        assert!(q.is_empty() && !q.has(k));
        assert!(q.push(k, 0, 10), "a new worker after the last retired");
        // Read-ahead joins a running worker only, and demand goes first.
        q.push_ahead(k, 40, 10);
        q.push(k, 20, 10);
        assert_eq!(q.next(k), Some((0, 10, false)));
        assert_eq!(q.next(k), Some((20, 10, false)));
        assert_eq!(q.next(k), Some((40, 10, true)));
        assert_eq!(q.next(k), None);
        q.push_ahead(k, 40, 10);
        assert!(!q.has(k), "no worker is started for read-ahead");
    }

    #[test]
    fn read_targets_follow_the_credential() {
        let az = ReadUrl::azure("https://a.blob.core.windows.net/c/k?sig=x");
        assert!(matches!(read_target(&az), ReadTarget::AzureSas { .. }));
        let o = S3Object {
            bucket: "seis".into(),
            key: "l/1.segy".into(),
            version_id: Some("v1".into()),
            etag: None,
            region: "eu-west-1".into(),
            endpoint: "vpce-1.s3.eu-west-1.vpce.amazonaws.com".into(),
            addressing: omnifile_blob::Addressing::VirtualHost,
            tls12_only: true,
            requester_pays: false,
            private_network: true,
        };
        let s3 = ReadUrl {
            url: o.to_pointer(),
            source: SourceKind::S3Signed,
            crc64nvme: None,
        };
        assert_eq!(read_target(&s3), ReadTarget::S3(o));
        let bad = ReadUrl {
            url: "https://x".into(),
            source: SourceKind::S3Signed,
            crc64nvme: None,
        };
        assert!(
            read_target(&bad).is_unsupported(),
            "never read as something else"
        );
    }

    #[test]
    fn io_errors_keep_the_blob_error_they_carry() {
        let e = std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            BlobError::Http {
                op: "S3 GetObject",
                status: 412,
                code: Some("PreconditionFailed".into()),
                message: "x".into(),
                request_id: None,
                retry_after: None,
            },
        );
        assert!(blob_error(e).is_source_changed());
        assert!(matches!(
            blob_error(std::io::Error::other("plain")),
            BlobError::Io(_)
        ));
    }
}
