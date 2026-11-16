//! Chunk fills and chunk eviction for remote objects (M5; R1 §5.2,
//! `docs/contracts/fsal-socket.md` §8 "Chunk map rules", control API §4.4).
//!
//! A remote object is a `PARTIAL` placeholder. Its online ranges are fetched
//! into the placeholder chunk by chunk (default 4 MiB), recorded in the
//! `trusted.omnifile.map` bitmap, and evicted again by the stub handshake
//! without a service lease (the catalog never counted those bytes as local).
//!
//! The rules, all fail-closed:
//! - **Widen after durability, map before marker.** Bytes are written
//!   through the private fill path, `fdatasync`ed, then the map gets the new
//!   bits, then the marker is rewritten (`CHUNK_MAP`, `chunk_shift`, a new
//!   `marker_seq`, `XATTR_REPLACE`). A reader that sees a map without the
//!   marker's flag ignores it.
//! - **Fenced like a recall fill.** The fill takes the file's lock, fences
//!   every FSAL with `begin_fill` before its first write, and re-checks the
//!   marker (same `marker_seq`, version, size) around every write and widen.
//! - **Whole-file check when fully local.** S3 range reads carry no
//!   checksum, so when the last missing byte arrives the file's CRC-64/NVME
//!   is computed from disk and compared with the catalog's. A match makes the
//!   marker `RESIDENT` (map removed); the caller then reports `filled`. A
//!   mismatch narrows the file back to nothing local, by the handshake, and
//!   fails: unverifiable bytes never become a resident file. Without a
//!   catalog CRC the file stays `PARTIAL` (every chunk still served).
//! - **Narrow before punch.** Eviction writes the narrowed map and a new
//!   marker, gets an acknowledged `INVALIDATE` from every FSAL, and only then
//!   punches; it never punches a chunk whose bit is still set on disk.

use std::fs::File;
use std::future::Future;
use std::io::{self, Read};
use std::os::fd::AsFd;
use std::os::unix::fs::FileExt;
use std::path::Path;
use std::sync::Arc;

use omnifile_integrity::Crc64;
use omnifile_marker::xattr::{self, ReadOutcome};
use omnifile_marker::{
    ChunkMap, DEFAULT_CHUNK_SHIFT, Flags, MAP_MAX, MAP_XATTR_NAME, MAX_CHUNK_SHIFT,
    MIN_CHUNK_SHIFT, Marker, State,
};
use tracing::{info, warn};

use crate::fill::{
    FillDone, FillError, FillRequest, after_own_change, marker_io, may_write, narrow_back,
    read_marker, still_ours, track,
};
use crate::handshake::{Narrowed, ctime_ns, marker_is, narrow_and_punch, open_fill_writer};
use crate::{FileLocks, FsalNotifier, InodeKey, Layout, MarkerSeq, blocking, sys, what};

/// Where a chunk fill reads from: exactly `len` bytes at `offset` of the
/// content version, or an error (an adapter over `omnifile_blob::RangeReader`
/// in the cache). A short buffer is refused here too.
pub trait ChunkSource: Send + Sync {
    fn read(&self, offset: u64, len: u64) -> impl Future<Output = io::Result<Vec<u8>>> + Send;
}

/// The content version a chunk fill is for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ChunkTarget {
    pub file_id: u64,
    pub version_id: u64,
    pub size: u64,
    /// The catalog's whole-file CRC-64/NVME. Without it the file never
    /// becomes `RESIDENT` by chunk fills.
    pub crc64nvme: Option<u64>,
    /// log2 of the preferred chunk size for a new map (22 = 4 MiB). It is
    /// raised for a file whose map would not fit an xattr value
    /// ([`chunk_shift_for`]); a file that already has a map keeps its own.
    pub chunk_shift: u8,
}

impl ChunkTarget {
    pub fn new(file_id: u64, version_id: u64, size: u64, crc64nvme: Option<u64>) -> Self {
        Self {
            file_id,
            version_id,
            size,
            crc64nvme,
            chunk_shift: DEFAULT_CHUNK_SHIFT,
        }
    }
}

/// The chunk shift a new map of a file of `size` gets: `preferred`
/// (clamped to the valid range), raised until the map fits an xattr value
/// (`MAP_MAX` bytes; 4 MiB chunks cover up to 2 TiB, 8 MiB up to 4 TiB…).
pub fn chunk_shift_for(size: u64, preferred: u8) -> u8 {
    let mut s = preferred.clamp(MIN_CHUNK_SHIFT, MAX_CHUNK_SHIFT);
    while s < MAX_CHUNK_SHIFT && ChunkMap::chunk_count(size, s).div_ceil(8) > MAP_MAX as u64 {
        s += 1;
    }
    s
}

/// The chunk shift a file uses now: its map's, if the marker on disk names
/// `version_id` and has one; otherwise what a new map would get.
pub fn file_chunk_shift(m: Option<&Marker>, version_id: u64, size: u64, preferred: u8) -> u8 {
    match m {
        Some(m) if m.version_id == version_id && m.flags.contains(Flags::CHUNK_MAP) => {
            m.chunk_shift
        }
        _ => chunk_shift_for(size, preferred),
    }
}

/// Write and widen at least this often within one call.
const WIDEN_EVERY: u64 = 64 << 20;

/// What a chunk or prefix fill left.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ChunkFill {
    /// The ranges asked for are local and durable (some may have been
    /// already). `written`: chunk indexes this call wrote (prefix fills: none);
    /// `bytes`: bytes written.
    Filled {
        written: Vec<u64>,
        bytes: u64,
        marker: Marker,
    },
    /// Every byte is now local and the whole-file CRC matched: the marker is
    /// `RESIDENT`. Report `POST …/files/{id}/filled` with `resident_bytes =
    /// size`.
    Complete(FillDone),
    /// The file was already `RESIDENT`.
    Resident,
}

/// Chunk index range of a file.
pub fn chunk_range(size: u64, shift: u8, chunk: u64) -> Option<(u64, u64)> {
    let start = chunk.checked_shl(shift as u32)?;
    if start >= size {
        return None;
    }
    let len = (1u64 << shift).min(size - start);
    Some((start, len))
}

/// The chunks overlapping `[offset, offset+len)` of a file of `size`.
pub fn chunks_for(size: u64, shift: u8, offset: u64, len: u64) -> std::ops::Range<u64> {
    let end = offset.saturating_add(len).min(size);
    if offset >= end {
        return 0..0;
    }
    (offset >> shift)..(((end - 1) >> shift) + 1)
}

/// The marker and map on disk (`None`: no marker).
pub fn read_state(layout: &Layout, rel: &Path) -> io::Result<Option<(Marker, Option<ChunkMap>)>> {
    let ro = File::from(sys::open_ro_beneath(&layout.fill_root, rel)?);
    let m = match xattr::read(ro.as_fd())? {
        ReadOutcome::Marker(m) => m,
        ReadOutcome::Unmanaged => return Ok(None),
        ReadOutcome::Bad(e) => {
            return Err(io::Error::new(io::ErrorKind::InvalidData, e.to_string()));
        }
    };
    let map = if m.flags.contains(Flags::CHUNK_MAP) {
        xattr::read_map(ro.as_fd(), &m).map_err(marker_io)?
    } else {
        None
    };
    Ok(Some((m, map)))
}

/// True when the map (with the prefix) covers every byte.
fn all_local(m: &Marker, map: &ChunkMap) -> bool {
    m.is_fully_resident() || map.covers(m.resident_bytes, m.size, m.size)
}

/// An open placeholder, checked against the target.
struct Opened {
    ro: File,
    w: Arc<File>,
    key: InodeKey,
    marker: Marker,
}

fn open_checked(layout: &Layout, req: &FillRequest, t: &ChunkTarget) -> Result<Opened, FillError> {
    let ro = File::from(sys::open_ro_beneath(&layout.fill_root, &req.rel)?);
    let mut key = sys::inode_key(ro.as_fd())?;
    key.file_id = t.file_id;
    if let Some((ino, gen_)) = req.expect_ino
        && (ino != key.ino || gen_ != key.ino_gen)
    {
        return Err(FillError::InodeChanged);
    }
    let marker = read_marker(&ro)?;
    if marker.file_id != t.file_id || marker.version_id != t.version_id {
        return Err(FillError::VersionMismatch {
            have_file: marker.file_id,
            have_version: marker.version_id,
            want_file: t.file_id,
            want_version: t.version_id,
        });
    }
    if marker.state == State::Partial
        && (sys::fstat(ro.as_fd())?.st_size as u64 != marker.size || t.size != marker.size)
    {
        return Err(FillError::NotManaged(format!(
            "size mismatch: marker {}, target {}",
            marker.size, t.size
        )));
    }
    let w = Arc::new(open_fill_writer(layout, &req.rel, ro.as_fd())?);
    Ok(Opened { ro, w, key, marker })
}

/// The map to widen: the file's own (if valid and its shift is sane), or a
/// new empty one. A bad map on disk is replaced, never trusted.
fn current_map(ro: &File, m: &Marker, want_shift: u8) -> io::Result<ChunkMap> {
    if m.flags.contains(Flags::CHUNK_MAP) {
        match xattr::read_map(ro.as_fd(), m) {
            Ok(Some(map)) => return Ok(map),
            Ok(None) => {}
            Err(e) => warn!(file_id = m.file_id, error = %e, "bad chunk map; starting a new one"),
        }
        return Ok(ChunkMap::empty(m.size, m.chunk_shift));
    }
    Ok(ChunkMap::empty(m.size, chunk_shift_for(m.size, want_shift)))
}

/// Fetch `[off, off+len)` and write it, fenced and re-checked. Returns the
/// updated ctime tracker.
#[allow(clippy::too_many_arguments)]
async fn fetch_and_write<S: ChunkSource, N: FsalNotifier>(
    o: &Opened,
    current: &Marker,
    t: &ChunkTarget,
    source: &S,
    notifier: &N,
    epoch: u64,
    off: u64,
    len: u64,
    ours: Option<i64>,
) -> Result<Option<i64>, FillError> {
    let data = source.read(off, len).await.map_err(FillError::Source)?;
    if data.len() as u64 != len {
        return Err(FillError::ShortRead {
            got: data.len(),
            want: len,
        });
    }
    may_write(notifier, o.key, t.version_id, epoch)?;
    still_ours(&o.ro, current)?;
    let (wc, roc, cur) = (o.w.clone(), o.ro.try_clone()?, *current);
    let r = blocking(move || {
        if let Err(e) = still_ours(&roc, &cur) {
            return Ok(Err(e));
        }
        let before = track(&roc, ours)?;
        wc.write_all_at(&data, off)?;
        Ok(Ok(after_own_change(&roc, before)?))
    })
    .await?;
    let ours = r?;
    still_ours(&o.ro, current)?;
    Ok(ours)
}

/// `fdatasync`, then the map, then the marker. Returns the new marker.
async fn widen_map(
    o: &Opened,
    current: Marker,
    map: &ChunkMap,
    seq: &MarkerSeq,
) -> Result<Marker, FillError> {
    let wc = o.w.clone();
    blocking(move || wc.sync_data()).await?;
    still_ours(&o.ro, &current)?;
    let mut next = current;
    // `resident_bytes` is not raised over set chunks (the contract allows
    // it, not requires it): chunks stay chunks, evictable one by one, and
    // the prefix stays the header cache.
    next.flags = next.flags.with(Flags::CHUNK_MAP);
    next.chunk_shift = map.shift;
    next.marker_seq = seq.next();
    xattr::write_map(o.ro.as_fd(), map)?;
    write_marker_replace(&o.ro, &next)?;
    Ok(next)
}

fn write_marker_replace(ro: &File, m: &Marker) -> Result<(), FillError> {
    xattr::write_replace(ro.as_fd(), m).map_err(|e| match e {
        xattr::Error::Io(io) if io.raw_os_error() == Some(libc::ENODATA) => {
            FillError::MarkerChanged
        }
        e => FillError::Io(marker_io(e)),
    })
}

/// Fill the given chunks (indexes) of a remote placeholder. Chunks already
/// local are skipped. Returns once those chunks are durable and marked; if
/// that made every byte local, the whole-file check runs (see the module
/// docs).
#[allow(clippy::too_many_arguments)]
pub async fn fill_chunks<S: ChunkSource, N: FsalNotifier>(
    layout: &Layout,
    req: &FillRequest,
    t: &ChunkTarget,
    chunks: &[u64],
    source: &S,
    notifier: &N,
    seq: &MarkerSeq,
    locks: &FileLocks,
) -> Result<ChunkFill, FillError> {
    if !(MIN_CHUNK_SHIFT..=MAX_CHUNK_SHIFT).contains(&t.chunk_shift) {
        return Err(FillError::NotManaged(format!(
            "chunk_shift {} out of range",
            t.chunk_shift
        )));
    }
    let Some(_lock) = locks.try_lock(t.file_id) else {
        return Err(FillError::Busy);
    };
    let o = open_checked(layout, req, t)?;
    if o.marker.state == State::Resident {
        return Ok(ChunkFill::Resident);
    }
    let mut ours = Some(ctime_ns(&sys::fstat(o.ro.as_fd())?));
    let mut map = current_map(&o.ro, &o.marker, t.chunk_shift)?;
    let shift = map.shift;
    let mut todo: Vec<u64> = chunks
        .iter()
        .copied()
        .filter(|c| {
            chunk_range(o.marker.size, shift, *c)
                .is_some_and(|(s, l)| !map.is_set(*c) && s + l > o.marker.resident_bytes)
        })
        .collect();
    todo.sort_unstable();
    todo.dedup();
    let mut current = o.marker;
    let mut written = Vec::new();
    let mut bytes = 0u64;
    if !todo.is_empty() {
        let epoch = notifier
            .begin_fill(o.key, t.version_id)
            .await
            .ok_or(FillError::FenceBroken)?;
        still_ours(&o.ro, &current)?;
        let mut pending = 0u64;
        for (i, c) in todo.iter().enumerate() {
            let (start, len) = chunk_range(current.size, shift, *c).expect("filtered");
            ours =
                fetch_and_write(&o, &current, t, source, notifier, epoch, start, len, ours).await?;
            map.set(*c);
            written.push(*c);
            bytes += len;
            pending += len;
            if pending >= WIDEN_EVERY || i + 1 == todo.len() {
                may_write(notifier, o.key, t.version_id, epoch)?;
                ours = track(&o.ro, ours)?;
                current = widen_map(&o, current, &map, seq).await?;
                ours = after_own_change(&o.ro, ours)?;
                pending = 0;
                notifier.invalidate(o.key, what::MARKER, false).await;
                notifier.fill_progress(o.key, t.version_id, current.resident_bytes);
            }
        }
    }
    if all_local(&current, &map) {
        return complete(layout, req, &o, current, t, notifier, seq, ours).await;
    }
    Ok(ChunkFill::Filled {
        written,
        bytes,
        marker: current,
    })
}

/// Fill the prefix `[resident_bytes, upto)` (rounded up to whole pages and
/// filesystem blocks, never past the size): the header cache of a remote
/// object, or a prefix fill up to a read's end for an FSAL without chunk
/// maps (v0.1). Raises `resident_bytes`; chunk bits are untouched.
#[allow(clippy::too_many_arguments)]
pub async fn fill_prefix<S: ChunkSource, N: FsalNotifier>(
    layout: &Layout,
    req: &FillRequest,
    t: &ChunkTarget,
    upto: u64,
    source: &S,
    notifier: &N,
    seq: &MarkerSeq,
    locks: &FileLocks,
) -> Result<ChunkFill, FillError> {
    let Some(_lock) = locks.try_lock(t.file_id) else {
        return Err(FillError::Busy);
    };
    let o = open_checked(layout, req, t)?;
    if o.marker.state == State::Resident {
        return Ok(ChunkFill::Resident);
    }
    let st = sys::fstat(o.ro.as_fd())?;
    let want = crate::handshake::resident_granule(upto, o.marker.size, st.st_blksize as u64);
    let mut ours = Some(ctime_ns(&st));
    let mut current = o.marker;
    let mut bytes = 0u64;
    if want > current.resident_bytes {
        let epoch = notifier
            .begin_fill(o.key, t.version_id)
            .await
            .ok_or(FillError::FenceBroken)?;
        still_ours(&o.ro, &current)?;
        let mut off = current.resident_bytes;
        while off < want {
            let len = crate::MAX_VERIFIED_RANGE.min(want - off);
            ours =
                fetch_and_write(&o, &current, t, source, notifier, epoch, off, len, ours).await?;
            off += len;
            bytes += len;
            if off - current.resident_bytes >= WIDEN_EVERY || off == want {
                may_write(notifier, o.key, t.version_id, epoch)?;
                let wc = o.w.clone();
                blocking(move || wc.sync_data()).await?;
                still_ours(&o.ro, &current)?;
                ours = track(&o.ro, ours)?;
                let mut next = current;
                next.resident_bytes = off;
                next.marker_seq = seq.next();
                write_marker_replace(&o.ro, &next)?;
                ours = after_own_change(&o.ro, ours)?;
                current = next;
                notifier.invalidate(o.key, what::MARKER, false).await;
                notifier.fill_progress(o.key, t.version_id, current.resident_bytes);
            }
        }
    }
    let map = if current.flags.contains(Flags::CHUNK_MAP) {
        current_map(&o.ro, &current, current.chunk_shift)?
    } else {
        ChunkMap::empty(current.size, chunk_shift_for(current.size, t.chunk_shift))
    };
    if all_local(&current, &map) {
        return complete(layout, req, &o, current, t, notifier, seq, ours).await;
    }
    Ok(ChunkFill::Filled {
        written: Vec::new(),
        bytes,
        marker: current,
    })
}

/// Every byte is local: check the whole file against the catalog's CRC.
#[allow(clippy::too_many_arguments)]
async fn complete<N: FsalNotifier>(
    layout: &Layout,
    req: &FillRequest,
    o: &Opened,
    current: Marker,
    t: &ChunkTarget,
    notifier: &N,
    seq: &MarkerSeq,
    mut ours: Option<i64>,
) -> Result<ChunkFill, FillError> {
    let Some(expected) = t.crc64nvme else {
        info!(
            file_id = t.file_id,
            "every chunk is local, but the catalog has no CRC: the file stays PARTIAL"
        );
        return Ok(ChunkFill::Filled {
            written: Vec::new(),
            bytes: 0,
            marker: current,
        });
    };
    let size = current.size;
    let rd = File::from(sys::reopen(o.ro.as_fd(), libc::O_RDONLY)?);
    let actual = blocking(move || {
        let mut c = Crc64::new();
        let mut r = rd;
        let mut left = size;
        let mut buf = vec![0u8; 1 << 20];
        while left > 0 {
            let want = left.min(buf.len() as u64) as usize;
            let n = r.read(&mut buf[..want])?;
            if n == 0 {
                return Err(io::Error::from(io::ErrorKind::UnexpectedEof));
            }
            c.update(&buf[..n]);
            left -= n as u64;
        }
        Ok(c.finalize())
    })
    .await?;
    if actual != expected {
        warn!(
            file_id = t.file_id,
            version_id = t.version_id,
            "whole-file CRC mismatch after chunk fills; narrowing to nothing local"
        );
        narrow_back(layout, req, &o.ro, o.key, 0, notifier, seq).await?;
        return Err(FillError::Integrity { expected, actual });
    }
    still_ours(&o.ro, &current)?;
    ours = track(&o.ro, ours)?;
    let mut next = current;
    next.state = State::Resident;
    next.resident_bytes = size;
    next.flags = next.flags.without(Flags::CHUNK_MAP);
    next.chunk_shift = 0;
    next.marker_seq = seq.next();
    write_marker_replace(&o.ro, &next)?;
    // The marker no longer names the map: remove it (a reader ignores it).
    match xattr::remove_raw(o.ro.as_fd(), MAP_XATTR_NAME) {
        Ok(()) => {}
        Err(e) if e.raw_os_error() == Some(libc::ENODATA) => {}
        Err(e) => return Err(e.into()),
    }
    let f = o.ro.try_clone()?;
    blocking(move || sys::fsync(f.as_fd())).await?;
    ours = after_own_change(&o.ro, ours)?;
    notifier.invalidate(o.key, what::MARKER, false).await;
    notifier.fill_progress(o.key, t.version_id, size);
    info!(
        file_id = t.file_id,
        version_id = t.version_id,
        size,
        "remote object fully local and verified"
    );
    Ok(ChunkFill::Complete(FillDone {
        size,
        ctime_ns: ours,
    }))
}

/// Result of an eviction.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ChunkNarrow {
    /// Narrowed, acknowledged and punched; `freed` bytes.
    Punched { freed: u64 },
    /// Narrowed (map and marker on disk) but not every FSAL acknowledged,
    /// or a connection came or went: nothing punched. Retry later.
    NotAcked,
    /// The marker isn't what eviction expects (another version, no map,
    /// `RESIDENT`, removed): nothing done.
    Changed,
    /// A fill, stub or replacement holds the file.
    Busy,
}

/// Evict chunks of a remote placeholder (clear their bits, then punch).
/// Chunks under the resident prefix are not touched (evict the prefix with
/// [`evict_file`]). Calling it again for chunks already cleared but not yet
/// punched completes the punch.
#[allow(clippy::too_many_arguments)]
pub async fn narrow_chunks<N: FsalNotifier>(
    layout: &Layout,
    rel: &Path,
    file_id: u64,
    version_id: u64,
    chunks: &[u64],
    notifier: &N,
    seq: &MarkerSeq,
    locks: &FileLocks,
) -> io::Result<ChunkNarrow> {
    let Some(_lock) = locks.try_lock(file_id) else {
        return Ok(ChunkNarrow::Busy);
    };
    let ro = match sys::open_ro_beneath(&layout.data_root, rel) {
        Ok(fd) => File::from(fd),
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(ChunkNarrow::Changed),
        Err(e) => return Err(e),
    };
    let mut key = sys::inode_key(ro.as_fd())?;
    key.file_id = file_id;
    let current = match xattr::read(ro.as_fd())? {
        ReadOutcome::Marker(m) => m,
        _ => return Ok(ChunkNarrow::Changed),
    };
    if current.file_id != file_id
        || current.version_id != version_id
        || current.state != State::Partial
        || !current.flags.contains(Flags::CHUNK_MAP)
    {
        return Ok(ChunkNarrow::Changed);
    }
    let Some(map) = xattr::read_map(ro.as_fd(), &current).map_err(marker_io)? else {
        return Ok(ChunkNarrow::Changed);
    };
    let mut narrowed_map = map.clone();
    let mut punch: Vec<(u64, u64)> = Vec::new();
    for c in chunks {
        let Some((start, len)) = chunk_range(current.size, map.shift, *c) else {
            continue;
        };
        if start < current.resident_bytes {
            continue;
        }
        narrowed_map.clear(*c);
        punch.push((start, len));
    }
    punch.sort_unstable();
    punch.dedup();
    if punch.is_empty() {
        return Ok(ChunkNarrow::Punched { freed: 0 });
    }
    // Before the narrowed marker exists (as in the stub handshake).
    let epoch = notifier.connection_epoch();
    let narrowed = if narrowed_map != map {
        notifier.before_narrow(key)?;
        if !marker_is(&ro, &current)? {
            return Ok(ChunkNarrow::Changed);
        }
        let mut n = current;
        n.marker_seq = seq.next();
        // Map first: a reader holding the old marker reads the narrower map.
        xattr::write_map(ro.as_fd(), &narrowed_map)?;
        match xattr::write_replace(ro.as_fd(), &n) {
            Ok(()) => {}
            Err(xattr::Error::Io(e)) if e.raw_os_error() == Some(libc::ENODATA) => {
                return Ok(ChunkNarrow::Changed);
            }
            Err(e) => return Err(marker_io(e)),
        }
        let f = ro.try_clone()?;
        blocking(move || sys::fsync(f.as_fd())).await?;
        n
    } else {
        // Already narrowed by an earlier, unacknowledged attempt.
        notifier.before_narrow(key)?;
        current
    };
    if !notifier
        .invalidate(key, what::MARKER | what::CONTENT, true)
        .await
    {
        return Ok(ChunkNarrow::NotAcked);
    }
    if !notifier.fence_holds(epoch) {
        return Ok(ChunkNarrow::NotAcked);
    }
    // Still our narrowed marker and map: never punch a chunk set on disk.
    if !marker_is(&ro, &narrowed)? {
        return Ok(ChunkNarrow::Changed);
    }
    match xattr::read_map(ro.as_fd(), &narrowed).map_err(marker_io)? {
        Some(m) if punch.iter().all(|(s, _)| !m.is_set(s >> m.shift)) => {}
        _ => return Ok(ChunkNarrow::Changed),
    }
    let w = open_fill_writer(layout, rel, ro.as_fd())?;
    let freed = punch.iter().map(|(_, l)| l).sum();
    blocking(move || {
        for (s, l) in &punch {
            sys::punch_hole(&w, *s, *l)?;
        }
        w.sync_all()
    })
    .await?;
    Ok(ChunkNarrow::Punched { freed })
}

/// Evict everything local of a remote placeholder above `keep`, header
/// prefix included (narrow to `keep` resident bytes by the stub handshake,
/// no lease). `keep` is the prefix the catalog counts as resident (a
/// stub's, line A: the stubber's headers), which only a stub lease may
/// change; 0 for a remote object the edge never held.
#[allow(clippy::too_many_arguments)]
pub async fn evict_file<N: FsalNotifier>(
    layout: &Layout,
    rel: &Path,
    file_id: u64,
    version_id: u64,
    keep: u64,
    notifier: &N,
    seq: &MarkerSeq,
    locks: &FileLocks,
) -> io::Result<ChunkNarrow> {
    let Some(_lock) = locks.try_lock(file_id) else {
        return Ok(ChunkNarrow::Busy);
    };
    let ro = match sys::open_ro_beneath(&layout.data_root, rel) {
        Ok(fd) => File::from(fd),
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(ChunkNarrow::Changed),
        Err(e) => return Err(e),
    };
    let mut key = sys::inode_key(ro.as_fd())?;
    key.file_id = file_id;
    let current = match xattr::read(ro.as_fd())? {
        ReadOutcome::Marker(m) => m,
        _ => return Ok(ChunkNarrow::Changed),
    };
    // A RESIDENT file is the catalog's (it was reported filled): stubbing it
    // needs a lease, so the cache never evicts it.
    if current.file_id != file_id
        || current.version_id != version_id
        || current.state != State::Partial
    {
        return Ok(ChunkNarrow::Changed);
    }
    let local = local_bytes(&ro, &current)?;
    let kept = keep.min(current.resident_bytes);
    if local <= kept && !current.flags.contains(Flags::CHUNK_MAP) {
        // Nothing above the floor.
        return Ok(ChunkNarrow::Punched { freed: 0 });
    }
    Ok(
        match narrow_and_punch(layout, rel, &ro, key, current, kept, notifier, seq, || {
            Ok(true)
        })
        .await?
        {
            Narrowed::Punched(_) => ChunkNarrow::Punched {
                freed: local.saturating_sub(kept),
            },
            Narrowed::NotAcked(_) | Narrowed::Aborted(_) => ChunkNarrow::NotAcked,
            Narrowed::Changed => ChunkNarrow::Changed,
        },
    )
}

/// Bytes the marker and map call local.
fn local_bytes(ro: &File, m: &Marker) -> io::Result<u64> {
    let mut n = m.resident_bytes;
    if m.flags.contains(Flags::CHUNK_MAP)
        && let Some(map) = xattr::read_map(ro.as_fd(), m).map_err(marker_io)?
    {
        for c in 0..ChunkMap::chunk_count(m.size, map.shift) {
            if map.is_set(c)
                && let Some((s, l)) = chunk_range(m.size, map.shift, c)
            {
                // Only the part above the prefix.
                let e = s + l;
                if e > m.resident_bytes {
                    n += e - s.max(m.resident_bytes);
                }
            }
        }
    }
    Ok(n)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Review S6: a map must fit an xattr value, so big files get bigger
    /// chunks.
    #[test]
    fn shift_grows_until_the_map_fits() {
        const TIB: u64 = 1 << 40;
        assert_eq!(chunk_shift_for(10 << 20, 22), 22);
        assert_eq!(chunk_shift_for(2 * TIB, 22), 22, "exactly 64 KiB of bits");
        assert_eq!(chunk_shift_for(2 * TIB + 1, 22), 23);
        assert_eq!(chunk_shift_for(5 * TIB, 22), 24);
        assert_eq!(chunk_shift_for(u64::MAX, 22), MAX_CHUNK_SHIFT);
        assert_eq!(chunk_shift_for(1, 2), MIN_CHUNK_SHIFT);
        for size in [1u64, 4 << 20, TIB, 3 * TIB, 100 * TIB, 1 << 59] {
            let s = chunk_shift_for(size, 22);
            let bytes = ChunkMap::chunk_count(size, s).div_ceil(8);
            assert!(bytes <= MAP_MAX as u64, "{size}: {bytes}");
            // The decoder accepts that geometry.
            let m = ChunkMap::empty(size, s);
            ChunkMap::decode(m.encode(), size, s).unwrap();
        }
        let mut m = Marker::partial(1, 2, 3 * TIB, 0, 1, Flags::ONLINE | Flags::CHUNK_MAP);
        m.chunk_shift = 25;
        assert_eq!(
            file_chunk_shift(Some(&m), 2, 3 * TIB, 22),
            25,
            "the file's own"
        );
        assert_eq!(
            file_chunk_shift(Some(&m), 9, 3 * TIB, 22),
            23,
            "another version"
        );
        assert_eq!(file_chunk_shift(None, 2, 3 * TIB, 22), 23);
    }

    #[test]
    fn chunk_ranges() {
        let s = 22;
        let size = 10 << 20;
        assert_eq!(chunk_range(size, s, 0), Some((0, 4 << 20)));
        assert_eq!(chunk_range(size, s, 2), Some((8 << 20, 2 << 20)));
        assert_eq!(chunk_range(size, s, 3), None);
        assert_eq!(chunks_for(size, s, 0, 1), 0..1);
        assert_eq!(chunks_for(size, s, (4 << 20) - 1, 2), 0..2);
        assert_eq!(chunks_for(size, s, 9 << 20, 100 << 20), 2..3);
        assert_eq!(chunks_for(size, s, size, 10), 0..0);
        assert_eq!(chunks_for(0, s, 0, 10), 0..0);
    }
}
