//! Recall fill: write fetched bytes into a placeholder, widening its marker
//! only after the bytes are durable.
//!
//! - The marker must name the content version being fetched; otherwise
//!   nothing is written (a rewind placeholder never receives the current
//!   version's bytes, and vice versa).
//! - Writes go through the private fill path, opened `O_WRONLY` and checked
//!   to be the same inode.
//! - Bytes are fetched in ranges of at most [`MAX_VERIFIED_RANGE`], each
//!   verified by the source against the service's range CRC-64, so every
//!   byte an intermediate widen exposes was checked on arrival. A source
//!   whose ranges aren't verified (S3, [`crate::SourceKind`]) gets no
//!   intermediate widen: its bytes are exposed only after the whole-file
//!   CRC matched.
//!   Every `widen_every` bytes it records how far it got instead
//!   ([`FETCHED_XATTR`], after an `fsync`), so an attempt that fails (a
//!   presigned URL expiring during a 184 GB file) resumes there: the next
//!   one reads the bytes on disk into the CRC and fetches the rest. The
//!   whole-file CRC still covers every byte.
//! - Every `widen_every` bytes: `fdatasync`, re-read the marker (it must be
//!   unchanged), widen `resident_bytes`, tell the FSAL (no ack needed).
//! - A fill needs the whole-file CRC-64/NVME (a target without one is
//!   refused); the last widen (to `RESIDENT`) happens only after it
//!   matches. On a mismatch the marker is narrowed back to where the fill
//!   started, by the stub handshake, retried until every FSAL acknowledged.
//! - Disk work (the prefix CRC, writes, `fdatasync`) runs off the async
//!   workers.
//!
//! What closes the window between "the marker names this version" and "the
//! bytes land":
//! - **Within the daemon**, the file's lock is held for the whole fill;
//!   stubs and content-version replacements take the same lock, so nothing
//!   in the daemon can narrow or re-version the marker mid-fill.
//! - **Against the FSAL** (an NFS truncate to 0 removes the marker), the fill
//!   is fenced before its first write: every connected FSAL acknowledges an
//!   `INVALIDATE` sent after the inode's `FETCHING` state, and then refuses
//!   to truncate it (`fsal-socket.md` §6.1). Any FSAL connecting or
//!   disconnecting breaks the fence and the fill stops before its next write.
//! - **Every write and widen** re-checks the marker (same `marker_seq`,
//!   version and size, and the inode's size), and a widen replaces the
//!   marker with `XATTR_REPLACE`, so a removed marker is never recreated.
//!   A range is marked resident only if every byte in it was written while
//!   the marker named this version. SMB and local writers are fenced by the
//!   smb-guard in M2.

use std::fs::File;
use std::io::{self, Read};
use std::os::fd::{AsFd, BorrowedFd};
use std::os::unix::fs::FileExt;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use omnifile_integrity::Crc64;
use omnifile_marker::xattr::{self, ReadOutcome};
use omnifile_marker::{Marker, State};
use tracing::{error, info, warn};

use crate::handshake::{Narrowed, ctime_ns, narrow_and_punch, open_fill_writer};
use crate::{
    FetchTarget, FileLocks, FsalNotifier, InodeKey, Layout, MAX_VERIFIED_RANGE, MarkerSeq,
    RangeSource, blocking, sys,
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FillRequest {
    pub rel: PathBuf,
    /// The inode the replica knows, if any: `(ino, ino_gen)`.
    pub expect_ino: Option<(u64, u32)>,
}

#[derive(Clone, Copy, Debug)]
pub struct FillOptions {
    /// Bytes per range GET; capped at [`MAX_VERIFIED_RANGE`].
    pub chunk: u64,
    /// Widen the marker at least this often.
    pub widen_every: u64,
}

impl Default for FillOptions {
    fn default() -> Self {
        FillOptions {
            chunk: MAX_VERIFIED_RANGE,
            widen_every: 64 << 20,
        }
    }
}

/// How often a fill whose whole-file CRC failed retries narrowing back
/// before giving up (the narrowed marker stays; a later redo punches).
const NARROW_BACK_ATTEMPTS: u32 = 20;

#[derive(Debug, thiserror::Error)]
pub enum FillError {
    #[error("no valid marker: {0}")]
    NotManaged(String),
    #[error(
        "marker names file {have_file} version {have_version}; fetch is for {want_file}/{want_version}"
    )]
    VersionMismatch {
        have_file: u64,
        have_version: u64,
        want_file: u64,
        want_version: u64,
    },
    #[error("inode changed under the fill")]
    InodeChanged,
    #[error("marker changed during the fill")]
    MarkerChanged,
    #[error("another stub, fill or content replacement holds the file")]
    Busy,
    #[error("not every FSAL confirmed the fill fence, or a connection changed")]
    FenceBroken,
    #[error("the fill was cancelled")]
    Cancelled,
    #[error("the catalog has no whole-file CRC-64/NVME for this version: refusing to fill")]
    NoExpectedCrc,
    #[error("source: {0}")]
    Source(io::Error),
    #[error("source returned {got} bytes for a {want}-byte range")]
    ShortRead { got: usize, want: u64 },
    #[error("whole-file CRC-64/NVME {actual:016x} != expected {expected:016x}")]
    Integrity { expected: u64, actual: u64 },
    #[error("io: {0}")]
    Io(#[from] io::Error),
}

pub(crate) fn marker_io(e: xattr::Error) -> io::Error {
    match e {
        xattr::Error::Io(e) => e,
        xattr::Error::Marker(e) => io::Error::new(io::ErrorKind::InvalidData, e.to_string()),
    }
}

pub(crate) fn read_marker(ro: &File) -> Result<Marker, FillError> {
    match xattr::read(ro.as_fd())? {
        ReadOutcome::Marker(m) => Ok(m),
        ReadOutcome::Unmanaged => Err(FillError::NotManaged("no marker".into())),
        ReadOutcome::Bad(e) => Err(FillError::NotManaged(e.to_string())),
    }
}

/// The fence and the cancellation, checked before every write and widen.
pub(crate) fn may_write<N: FsalNotifier>(
    notifier: &N,
    key: InodeKey,
    version_id: u64,
    epoch: u64,
) -> Result<(), FillError> {
    if notifier.fill_cancelled(key, version_id) {
        return Err(FillError::Cancelled);
    }
    if !notifier.fence_holds(epoch) {
        return Err(FillError::FenceBroken);
    }
    Ok(())
}

/// What a finished fill leaves.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FillDone {
    /// The final `resident_bytes` (the size).
    pub size: u64,
    /// The file's `st_ctime` (ns) after the fill's last own change, if
    /// nothing but the fill changed the inode from its start to its end.
    /// `None`: someone else wrote (or chmod-ed) the file meanwhile (an SMB
    /// write the guard let through to a resident range, say), so its bytes
    /// may not be the content version: never record it as committed.
    pub ctime_ns: Option<i64>,
}

/// Fill a placeholder completely. Returns the final `resident_bytes` (the size).
#[allow(clippy::too_many_arguments)]
pub async fn fill<S: RangeSource, N: FsalNotifier>(
    layout: &Layout,
    req: &FillRequest,
    target: &FetchTarget,
    source: &S,
    notifier: &N,
    seq: &MarkerSeq,
    locks: &FileLocks,
    opts: FillOptions,
) -> Result<u64, FillError> {
    fill_tracked(layout, req, target, source, notifier, seq, locks, opts)
        .await
        .map(|d| d.size)
}

/// `ctime` unchanged since our last own change, or `None` from then on.
pub(crate) fn track(ro: &File, ours: Option<i64>) -> io::Result<Option<i64>> {
    let now = ctime_ns(&sys::fstat(ro.as_fd())?);
    Ok(ours.filter(|c| *c == now))
}

pub(crate) fn after_own_change(ro: &File, ours: Option<i64>) -> io::Result<Option<i64>> {
    Ok(match ours {
        Some(_) => Some(ctime_ns(&sys::fstat(ro.as_fd())?)),
        None => None,
    })
}

/// [`fill`], also saying whether anyone else changed the file meanwhile.
#[allow(clippy::too_many_arguments)]
pub async fn fill_tracked<S: RangeSource, N: FsalNotifier>(
    layout: &Layout,
    req: &FillRequest,
    target: &FetchTarget,
    source: &S,
    notifier: &N,
    seq: &MarkerSeq,
    locks: &FileLocks,
    opts: FillOptions,
) -> Result<FillDone, FillError> {
    let Some(expected) = target.crc64nvme else {
        return Err(FillError::NoExpectedCrc);
    };
    let Some(_lock) = locks.try_lock(target.file_id) else {
        return Err(FillError::Busy);
    };
    // Through the private fill mount: the smb-guard ignores it. The prefix
    // CRC below reads bytes of a PARTIAL file, and through the data root that
    // read would raise a pre-content event (page-granular, so its last page
    // reaches past `resident_bytes`) that waits for this very fill.
    let ro = File::from(sys::open_ro_beneath(&layout.fill_root, &req.rel)?);
    let mut key: InodeKey = sys::inode_key(ro.as_fd())?;
    key.file_id = target.file_id;
    if let Some((ino, gen_)) = req.expect_ino
        && (ino != key.ino || gen_ != key.ino_gen)
    {
        return Err(FillError::InodeChanged);
    }
    let start_marker = read_marker(&ro)?;
    if start_marker.file_id != target.file_id || start_marker.version_id != target.version_id {
        return Err(FillError::VersionMismatch {
            have_file: start_marker.file_id,
            have_version: start_marker.version_id,
            want_file: target.file_id,
            want_version: target.version_id,
        });
    }
    let size = start_marker.size;
    if sys::fstat(ro.as_fd())?.st_size as u64 != size || target.size != size {
        return Err(FillError::NotManaged(format!(
            "size mismatch: marker {size}, target {}",
            target.size
        )));
    }
    // Taken before anything is read: a change by anyone else from here on
    // shows as a ctime the fill did not cause.
    let mut ours = Some(ctime_ns(&sys::fstat(ro.as_fd())?));
    if start_marker.is_fully_resident() {
        return Ok(FillDone {
            size,
            ctime_ns: None,
        });
    }
    let start = start_marker.resident_bytes;
    let w = Arc::new(open_fill_writer(layout, &req.rel, ro.as_fd())?);
    // A source whose ranges aren't verified on arrival (S3) resumes after
    // the bytes an earlier attempt wrote (never readable: the marker isn't
    // widened before the whole-file CRC matches, and that CRC covers them).
    let verified = target.source.ranges_verified();
    let resume = if verified {
        start
    } else {
        read_fetched(ro.as_fd(), target.version_id, start, size)
    };

    // CRC over the prefix on disk (off the async workers).
    let prefix = File::from(sys::reopen(ro.as_fd(), libc::O_RDONLY)?);
    let mut crc = blocking(move || {
        let mut c = Crc64::new();
        let mut r = prefix;
        let mut left = resume;
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
        Ok(c)
    })
    .await?;

    // Fence before the first byte, then confirm nothing changed meanwhile.
    let epoch = notifier
        .begin_fill(key, target.version_id)
        .await
        .ok_or(FillError::FenceBroken)?;
    let mut current = start_marker;
    still_ours(&ro, &current)?;
    let chunk = opts.chunk.clamp(1, MAX_VERIFIED_RANGE);
    let mut off = resume;
    let mut recorded = resume;
    while off < size {
        let len = chunk.min(size - off);
        let data = source
            .get_range(target, off, len)
            .await
            .map_err(FillError::Source)?;
        if data.len() as u64 != len {
            return Err(FillError::ShortRead {
                got: data.len(),
                want: len,
            });
        }
        // The user may have replaced the file meanwhile (an NFS truncate to 0
        // makes the FSAL remove the marker): never write backend bytes into it.
        may_write(notifier, key, target.version_id, epoch)?;
        still_ours(&ro, &current)?;
        // Check again on the blocking thread immediately before the
        // `pwrite`: the hop to that thread must not widen the window in
        // which a local truncate (not fenced by the FSAL) can slip between
        // the check and the write (`fsal-socket.md` §6.1).
        let (wc, roc, cur) = (w.clone(), ro.try_clone()?, current);
        let r = blocking(move || {
            if let Err(e) = still_ours(&roc, &cur) {
                return Ok(Err(e));
            }
            let before = track(&roc, ours)?;
            wc.write_all_at(&data, off)?;
            let after = after_own_change(&roc, before)?;
            crc.update(&data);
            Ok(Ok((crc, after)))
        })
        .await?;
        (crc, ours) = r?;
        still_ours(&ro, &current)?;
        off += len;
        let last = off == size;
        // The final widen waits for the CRC check below, and so does every
        // widen when the source's ranges are not verified on arrival (S3).
        if !last
            && target.source.ranges_verified()
            && off - current.resident_bytes >= opts.widen_every
        {
            may_write(notifier, key, target.version_id, epoch)?;
            ours = track(&ro, ours)?;
            current = widen(&ro, &w, current, off, seq).await?;
            ours = after_own_change(&ro, ours)?;
            notifier.fill_progress(key, target.version_id, off);
        }
        // Unverified bytes: record how far the fill got, once they are on
        // disk, so a failed attempt (an expired URL) resumes here.
        if !last && !verified && off - recorded >= opts.widen_every {
            let (wc, roc, version, to) = (w.clone(), ro.try_clone()?, target.version_id, off);
            blocking(move || {
                sys::fsync(wc.as_fd())?;
                write_fetched(roc.as_fd(), version, to)
            })
            .await?;
            recorded = off;
        }
    }

    let actual = crc.finalize();
    // Matched or not, nothing resumes from these bytes again (nor from an
    // earlier unverified attempt's, when this one came from a verified
    // source).
    clear_fetched(ro.as_fd())?;
    if actual != expected {
        warn!(
            file_id = target.file_id,
            version_id = target.version_id,
            "fill CRC mismatch; narrowing back"
        );
        narrow_back(layout, req, &ro, key, start, notifier, seq).await?;
        return Err(FillError::Integrity { expected, actual });
    }
    may_write(notifier, key, target.version_id, epoch)?;
    ours = track(&ro, ours)?;
    widen(&ro, &w, current, size, seq).await?;
    ours = after_own_change(&ro, ours)?;
    // The RESIDENT marker durable before the fill reports DONE.
    let f = ro.try_clone()?;
    blocking(move || sys::fsync(f.as_fd())).await?;
    if ours.is_none() {
        warn!(
            file_id = target.file_id,
            version_id = target.version_id,
            "the file was changed by someone else during the fill"
        );
    }
    notifier.fill_progress(key, target.version_id, size);
    info!(
        file_id = target.file_id,
        version_id = target.version_id,
        size,
        "fill complete"
    );
    Ok(FillDone {
        size,
        ctime_ns: ours,
    })
}

/// How far an earlier fill of an unverified source wrote:
/// `version_id` then `fetched_to`, little-endian. `trusted.*`, like the
/// marker: users can't set it.
pub const FETCHED_XATTR: &str = "trusted.omnifile.fetched";

/// Where a fill of `version_id` resumes: the recorded offset when it is
/// for this version and past `start`, else `start`.
fn read_fetched(fd: BorrowedFd<'_>, version_id: u64, start: u64, size: u64) -> u64 {
    let Ok(Some(v)) = xattr::get_raw(fd, FETCHED_XATTR, 16) else {
        return start;
    };
    let Ok(v) = <[u8; 16]>::try_from(v.as_slice()) else {
        return start;
    };
    let (ver, to) = (
        u64::from_le_bytes(v[..8].try_into().unwrap_or_default()),
        u64::from_le_bytes(v[8..].try_into().unwrap_or_default()),
    );
    if ver == version_id && to > start && to < size {
        to
    } else {
        start
    }
}

fn write_fetched(fd: BorrowedFd<'_>, version_id: u64, to: u64) -> io::Result<()> {
    let mut v = [0u8; 16];
    v[..8].copy_from_slice(&version_id.to_le_bytes());
    v[8..].copy_from_slice(&to.to_le_bytes());
    xattr::set_raw(fd, FETCHED_XATTR, &v, 0)
}

fn clear_fetched(fd: BorrowedFd<'_>) -> io::Result<()> {
    match xattr::remove_raw(fd, FETCHED_XATTR) {
        Err(e) if e.raw_os_error() == Some(libc::ENODATA) => Ok(()),
        r => r,
    }
}

/// Never leave unverified bytes marked resident: narrow back to `start` and
/// punch, retrying until every FSAL acknowledged (an FSAL that still caches
/// a widened marker could serve the bad bytes; `NotAcked` is not success).
#[allow(clippy::too_many_arguments)]
pub(crate) async fn narrow_back<N: FsalNotifier>(
    layout: &Layout,
    req: &FillRequest,
    ro: &File,
    key: InodeKey,
    start: u64,
    notifier: &N,
    seq: &MarkerSeq,
) -> Result<(), FillError> {
    let mut wait = Duration::from_millis(100);
    for attempt in 1..=NARROW_BACK_ATTEMPTS {
        let current = match read_marker(ro) {
            Ok(m) => m,
            // Removed (a truncate made the file the user's): nothing of ours.
            Err(_) => return Ok(()),
        };
        match narrow_and_punch(
            layout,
            &req.rel,
            ro,
            key,
            current,
            start,
            notifier,
            seq,
            || Ok(true),
        )
        .await?
        {
            Narrowed::Punched(_) => return Ok(()),
            // Re-read and narrow again.
            Narrowed::Changed => {}
            Narrowed::NotAcked(_) | Narrowed::Aborted(_) => {
                warn!(
                    file_id = key.file_id,
                    attempt, "narrow-back not acknowledged; retrying"
                );
            }
        }
        crate::sleep(wait).await;
        wait = (wait * 2).min(Duration::from_secs(2));
    }
    error!(
        file_id = key.file_id,
        "narrow-back never acknowledged; the marker is narrowed, the punch waits for a redo"
    );
    Ok(())
}

/// The marker is exactly the one this fill last wrote (same `marker_seq`,
/// size, version) and the inode still has that size. Checked before and
/// after every write and before every widen (`fsal-socket.md` §6.1).
pub(crate) fn still_ours(ro: &File, current: &Marker) -> Result<(), FillError> {
    let now = read_marker(ro).map_err(|_| FillError::MarkerChanged)?;
    if now != *current || sys::fstat(ro.as_fd())?.st_size as u64 != current.size {
        return Err(FillError::MarkerChanged);
    }
    Ok(())
}

/// `fdatasync`, check the marker is still the one we widened last, then
/// widen it to `resident`.
async fn widen(
    ro: &File,
    w: &Arc<File>,
    current: Marker,
    resident: u64,
    seq: &MarkerSeq,
) -> Result<Marker, FillError> {
    let wc = w.clone();
    blocking(move || wc.sync_data()).await?;
    still_ours(ro, &current)?;
    let mut next = current;
    next.resident_bytes = resident;
    let whole = resident == next.size;
    if whole {
        next.state = State::Resident;
        // Every byte is local: a chunk map (edge-local chunk fills, M5)
        // means nothing any more.
        next.flags = next.flags.without(omnifile_marker::Flags::CHUNK_MAP);
        next.chunk_shift = 0;
    }
    next.marker_seq = seq.next();
    // XATTR_REPLACE: a marker removed since the check is never recreated.
    xattr::write_replace(ro.as_fd(), &next).map_err(|e| match e {
        xattr::Error::Io(io) if io.raw_os_error() == Some(libc::ENODATA) => {
            FillError::MarkerChanged
        }
        e => FillError::Io(marker_io(e)),
    })?;
    if whole && current.flags.contains(omnifile_marker::Flags::CHUNK_MAP) {
        match xattr::remove_raw(ro.as_fd(), omnifile_marker::MAP_XATTR_NAME) {
            Ok(()) => {}
            Err(e) if e.raw_os_error() == Some(libc::ENODATA) => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(next)
}

/// True when the file at `req` is a `PARTIAL` placeholder that grew past
/// its marker's size (an `O_APPEND` write landed while part of it was
/// remote): [`repair_extended`] applies, [`fill`] refuses.
pub fn is_extended(layout: &Layout, req: &FillRequest) -> io::Result<bool> {
    let ro = File::from(sys::open_ro_beneath(&layout.fill_root, &req.rel)?);
    let st = sys::fstat(ro.as_fd())?;
    Ok(match xattr::read(ro.as_fd())? {
        ReadOutcome::Marker(m) => m.state == State::Partial && st.st_size as u64 > m.size,
        _ => false,
    })
}

/// Repair a placeholder that grew past its marker's size (M2 review S3):
/// the user's appended bytes sit past `size`, and `[resident_bytes, size)`
/// is still holes that must be the content version's bytes.
///
/// 1. Fetch `[resident_bytes, size)` into an unnamed staging file (every
///    range verified by the source), and check the prefix on disk plus
///    those bytes against the version's whole-file CRC. Nothing is written
///    to the file before that passes, so a file whose prefix the user
///    rewrote (not an append) is never touched.
/// 2. Under the fill fence, check the marker is unchanged and that
///    `[resident_bytes, size)` is still all holes (a user write there means
///    the range is the user's: stop), then copy the bytes in, `fsync`, and
///    replace the marker with `RESIDENT` (`XATTR_REPLACE`, contract §8: a
///    `RESIDENT` marker over a larger file reads as all local).
///
/// The result is never recorded as the content version (`ctime_ns` is
/// `None`): the file now holds the version plus the user's bytes, and must
/// be committed as a new version. While the repair runs, the smb-guard
/// holds every access to the file and the FSAL fails it closed (a
/// `PARTIAL` marker whose size differs from the inode's).
#[allow(clippy::too_many_arguments)]
pub async fn repair_extended<S: RangeSource, N: FsalNotifier>(
    layout: &Layout,
    req: &FillRequest,
    target: &FetchTarget,
    source: &S,
    notifier: &N,
    seq: &MarkerSeq,
    locks: &FileLocks,
) -> Result<FillDone, FillError> {
    let Some(expected) = target.crc64nvme else {
        return Err(FillError::NoExpectedCrc);
    };
    let Some(_lock) = locks.try_lock(target.file_id) else {
        return Err(FillError::Busy);
    };
    let ro = File::from(sys::open_ro_beneath(&layout.fill_root, &req.rel)?);
    let mut key: InodeKey = sys::inode_key(ro.as_fd())?;
    key.file_id = target.file_id;
    if let Some((ino, gen_)) = req.expect_ino
        && (ino != key.ino || gen_ != key.ino_gen)
    {
        return Err(FillError::InodeChanged);
    }
    let m = read_marker(&ro)?;
    if m.file_id != target.file_id || m.version_id != target.version_id {
        return Err(FillError::VersionMismatch {
            have_file: m.file_id,
            have_version: m.version_id,
            want_file: target.file_id,
            want_version: target.version_id,
        });
    }
    let (start, size) = (m.resident_bytes, m.size);
    if m.state != State::Partial
        || target.size != size
        || (sys::fstat(ro.as_fd())?.st_size as u64) <= size
    {
        return Err(FillError::NotManaged("not an extended placeholder".into()));
    }
    // 1. Prefix CRC from disk, then the missing range into staging.
    let prefix = File::from(sys::reopen(ro.as_fd(), libc::O_RDONLY)?);
    let mut crc = blocking(move || {
        let mut c = Crc64::new();
        let mut r = prefix;
        let mut left = start;
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
        Ok(c)
    })
    .await?;
    let staging = sys::private_dir(&layout.staging)?;
    let tmp = Arc::new(sys::tmpfile_in(staging.as_fd(), 0o600)?);
    let mut off = start;
    while off < size {
        let len = MAX_VERIFIED_RANGE.min(size - off);
        let data = source
            .get_range(target, off, len)
            .await
            .map_err(FillError::Source)?;
        if data.len() as u64 != len {
            return Err(FillError::ShortRead {
                got: data.len(),
                want: len,
            });
        }
        let t = tmp.clone();
        crc = blocking(move || {
            t.write_all_at(&data, off - start)?;
            crc.update(&data);
            Ok(crc)
        })
        .await?;
        off += len;
    }
    let actual = crc.finalize();
    if actual != expected {
        // The prefix is not the version's (or the source lied): touch nothing.
        return Err(FillError::Integrity { expected, actual });
    }
    // 2. Fenced, re-checked, copied in.
    let epoch = notifier
        .begin_fill(key, target.version_id)
        .await
        .ok_or(FillError::FenceBroken)?;
    may_write(notifier, key, target.version_id, epoch)?;
    let w = Arc::new(open_fill_writer(layout, &req.rel, ro.as_fd())?);
    let (roc, wc, t) = (ro.try_clone()?, w.clone(), tmp.clone());
    let copied = blocking(move || {
        if read_marker(&roc).ok() != Some(m) || (sys::fstat(roc.as_fd())?.st_size as u64) <= size {
            return Ok(Err(FillError::MarkerChanged));
        }
        if sys::first_data_at(roc.as_fd(), start)?.is_some_and(|d| d < size) {
            return Ok(Err(FillError::NotManaged(
                "the missing range holds data: someone wrote there".into(),
            )));
        }
        let mut buf = vec![0u8; 1 << 20];
        let mut at = 0u64;
        while start + at < size {
            let n = (size - start - at).min(buf.len() as u64) as usize;
            t.read_exact_at(&mut buf[..n], at)?;
            wc.write_all_at(&buf[..n], start + at)?;
            at += n as u64;
        }
        wc.sync_data()?;
        Ok(Ok(()))
    })
    .await?;
    copied?;
    may_write(notifier, key, target.version_id, epoch)?;
    if read_marker(&ro).ok() != Some(m) {
        return Err(FillError::MarkerChanged);
    }
    let mut next = m;
    next.state = State::Resident;
    next.resident_bytes = size;
    next.marker_seq = seq.next();
    xattr::write_replace(ro.as_fd(), &next).map_err(|e| match e {
        xattr::Error::Io(io) if io.raw_os_error() == Some(libc::ENODATA) => {
            FillError::MarkerChanged
        }
        e => FillError::Io(marker_io(e)),
    })?;
    let f = ro.try_clone()?;
    blocking(move || sys::fsync(f.as_fd())).await?;
    notifier.fill_progress(key, target.version_id, size);
    info!(
        file_id = target.file_id,
        version_id = target.version_id,
        size,
        "extended placeholder repaired; the file becomes a new version"
    );
    Ok(FillDone {
        size,
        ctime_ns: None,
    })
}
