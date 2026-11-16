//! The stub handshake (R1 §5.1) and the narrowing primitive it shares with
//! content-version replacement and eviction.
//!
//! 1. Check: not open for write; the marker's content version is the
//!    instruction's. A read lease taken here is **held** through step 5:
//!    while it is held, any open for write or truncate by another process
//!    waits and shows as a lease break.
//! 2. smb-guard hook (remove the ignore mark).
//! 3. Narrow the marker (new `marker_seq`, `XATTR_REPLACE`, `fsync`ed), after
//!    recording the FSAL connection epoch.
//! 4. `INVALIDATE` with `ACK_REQUIRED` to every connected FSAL. The FSAL acks
//!    only once every I/O that passed its check under the old marker has
//!    finished (`fsal-socket.md` §5.5), so an NFS WRITE can still land
//!    between steps 1 and 4.
//! 5. Any missing ack: stop here. Otherwise re-check that the lease is
//!    unbroken and that `mtime` and size are what they were at step 1 (the
//!    ctime moves with our own xattr write, so it can't be used). On any
//!    change a writer got in: re-widen the marker and skip. Then release the
//!    lease. From now on the FSAL treats every write as needing a full fill.
//! 6. Punch through the private fill path, only if the connection epoch is
//!    still the one recorded at step 3 and the marker is still ours.
//! 7. The caller reports `stubbed`.
//!
//! A crash anywhere leaves either the old marker over all bytes, or a
//! narrowed marker over bytes that are still present: both are safe, and
//! a redo of the same instruction completes the punch.

use std::fs::File;
use std::io;
use std::os::fd::{AsFd, BorrowedFd};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use omnifile_marker::xattr::{self, ReadOutcome};
use omnifile_marker::{Flags, MAP_XATTR_NAME, Marker, State};
use tracing::{info, warn};

use crate::{
    FileLocks, FsalNotifier, InodeKey, Layout, MarkerSeq, SkipReason, SmbGuardHook, StubOutcome,
    blocking, sys, what,
};

/// A stub instruction (after `stub/prepare` granted a lease).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StubRequest {
    /// Path relative to the data root.
    pub rel: PathBuf,
    pub file_id: u64,
    /// Content version the instruction names.
    pub version_id: u64,
    pub resident_bytes: u64,
    /// The inode the replica knows, if any: `(ino, ino_gen)`.
    pub expect_ino: Option<(u64, u32)>,
    /// `st_ctime` (ns) recorded after the daemon's last write. If the file's
    /// ctime differs, someone wrote to it since the version was committed,
    /// so the local bytes may not be that version: skip.
    pub expect_ctime_ns: Option<i64>,
}

/// `st_ctime` in nanoseconds.
pub fn ctime_ns(st: &libc::stat) -> i64 {
    st.st_ctime * 1_000_000_000 + st.st_ctime_nsec
}

/// `st_mtime` in nanoseconds.
pub fn mtime_ns(st: &libc::stat) -> i64 {
    st.st_mtime * 1_000_000_000 + st.st_mtime_nsec
}

/// What a writer changes: size and mtime (not ctime, which our own marker
/// writes move too).
fn content_stamp(st: &libc::stat) -> (i64, i64) {
    (st.st_size, mtime_ns(st))
}

fn skip(reason: SkipReason, detail: impl Into<String>) -> StubOutcome {
    StubOutcome::Skipped {
        reason,
        detail: detail.into(),
    }
}

fn marker_io(e: xattr::Error) -> io::Error {
    match e {
        xattr::Error::Io(e) => e,
        xattr::Error::Marker(e) => io::Error::new(io::ErrorKind::InvalidData, e.to_string()),
    }
}

/// Open the fill-path name for writing and check it is the same inode as `ro`.
pub fn open_fill_writer(layout: &Layout, rel: &Path, ro: BorrowedFd<'_>) -> io::Result<File> {
    let w = File::from(sys::open_beneath(
        &layout.fill_root,
        rel,
        libc::O_WRONLY | libc::O_NOFOLLOW,
        0,
    )?);
    let a = sys::fstat(ro)?;
    let b = sys::fstat(w.as_fd())?;
    if a.st_ino != b.st_ino || a.st_dev != b.st_dev {
        return Err(io::Error::other("fill path names a different inode"));
    }
    Ok(w)
}

/// True when the marker on disk is exactly `m` (same `marker_seq` too).
pub fn marker_is(ro: &File, m: &Marker) -> io::Result<bool> {
    Ok(matches!(xattr::read(ro.as_fd())?, ReadOutcome::Marker(now) if now == *m))
}

/// Replace the marker (`XATTR_REPLACE`) and make it durable. `Ok(false)`
/// when there is no marker to replace (the FSAL removed it after a
/// truncate): it is never recreated.
async fn replace_marker(ro: &File, m: &Marker) -> io::Result<bool> {
    match xattr::write_replace(ro.as_fd(), m) {
        Ok(()) => {}
        Err(xattr::Error::Io(e)) if e.raw_os_error() == Some(libc::ENODATA) => return Ok(false),
        Err(e) => return Err(marker_io(e)),
    }
    let f = ro.try_clone()?;
    blocking(move || sys::fsync(f.as_fd())).await?;
    Ok(true)
}

/// The resident prefix a stub keeps: `resident_bytes` rounded **up** to the
/// larger of the page size and the filesystem's block size (never past
/// `size`). fanotify pre-content events are page-granular: a read that
/// ends inside the last, partly resident page raises an event for the
/// whole page, so an unaligned prefix would recall on every header read
/// (and the partial page could never be punched anyway).
pub fn resident_granule(resident_bytes: u64, size: u64, st_blksize: u64) -> u64 {
    // SAFETY: sysconf has no preconditions.
    let page = unsafe { libc::sysconf(libc::_SC_PAGESIZE) }.max(4096) as u64;
    let g = page.max(st_blksize).max(1);
    resident_bytes.div_ceil(g).saturating_mul(g).min(size)
}

/// Length of the punch past EOF that frees speculative preallocation. Any
/// length reaching past the last block works; this one keeps
/// `offset + len` far inside `off_t` for any file size.
const PAST_EOF_PUNCH: u64 = 1 << 60;

/// Result of [`narrow_and_punch`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Narrowed {
    /// Marker narrowed and bytes past `resident_bytes` punched.
    Punched(Marker),
    /// Marker narrowed; not every FSAL acknowledged, or an FSAL connection
    /// came or went since the narrowing: nothing punched.
    NotAcked(Marker),
    /// Marker narrowed and acknowledged, but the caller's check before the
    /// punch failed (a writer got in): nothing punched.
    Aborted(Marker),
    /// The marker on disk is not the one the caller read, or not the one
    /// we narrowed to (rewritten, or removed after a truncate): stopped.
    Changed,
}

/// Steps 3–6: narrow `current` to `resident_bytes`, get acknowledged
/// invalidation, run `before_punch` (false: stop without punching), punch.
///
/// The narrowed marker replaces `current` only if `current` is still what
/// is on disk (`XATTR_REPLACE`, same `marker_seq`), and the punch happens
/// only while the FSAL connection epoch is the one recorded before the
/// narrowing and the narrowed marker is still on disk.
#[allow(clippy::too_many_arguments)]
pub async fn narrow_and_punch<N: FsalNotifier>(
    layout: &Layout,
    rel: &Path,
    ro: &File,
    key: InodeKey,
    current: Marker,
    resident_bytes: u64,
    notifier: &N,
    seq: &MarkerSeq,
    before_punch: impl FnOnce() -> io::Result<bool>,
) -> io::Result<Narrowed> {
    narrow_impl(
        layout,
        rel,
        ro,
        key,
        current,
        resident_bytes,
        false,
        notifier,
        seq,
        before_punch,
    )
    .await
}

/// [`narrow_and_punch`]; with `force_partial` the narrowed marker is
/// `PARTIAL` even when nothing is removed (an empty file): a content-version
/// change resizes the file next, and a `RESIDENT` marker over a resized file
/// reads as all local (contract §8).
#[allow(clippy::too_many_arguments)]
async fn narrow_impl<N: FsalNotifier>(
    layout: &Layout,
    rel: &Path,
    ro: &File,
    key: InodeKey,
    current: Marker,
    resident_bytes: u64,
    force_partial: bool,
    notifier: &N,
    seq: &MarkerSeq,
    before_punch: impl FnOnce() -> io::Result<bool>,
) -> io::Result<Narrowed> {
    // Before the narrowed marker exists: an FSAL connecting after this
    // point may have loaded either marker, so the punch waits for a redo.
    let epoch = notifier.connection_epoch();
    let target = resident_bytes.min(current.size).min(match current.state {
        State::Resident => current.size,
        State::Partial => current.resident_bytes,
    });
    let mut narrowed = current;
    if target < current.size || force_partial {
        narrowed.state = State::Partial;
    }
    narrowed.resident_bytes = target;
    // Prefix-only after narrowing: a chunk map would claim punched chunks.
    narrowed.flags = narrowed.flags.without(Flags::CHUNK_MAP);
    narrowed.chunk_shift = 0;
    if !marker_is(ro, &current)? {
        return Ok(Narrowed::Changed);
    }
    if narrowed != current || current.flags.contains(Flags::CHUNK_MAP) {
        // Every narrowing, whoever asks for it: the guard's ignore mark goes
        // first (and stays suppressed through the punch).
        notifier.before_narrow(key)?;
        narrowed.marker_seq = seq.next();
        // Durable before any FSAL is told (and before any punch).
        if !replace_marker(ro, &narrowed).await? {
            return Ok(Narrowed::Changed);
        }
    } else {
        narrowed.marker_seq = current.marker_seq;
    }
    if !notifier
        .invalidate(key, what::MARKER | what::CONTENT, true)
        .await
    {
        return Ok(Narrowed::NotAcked(narrowed));
    }
    if !before_punch()? {
        return Ok(Narrowed::Aborted(narrowed));
    }
    if !notifier.fence_holds(epoch) {
        return Ok(Narrowed::NotAcked(narrowed));
    }
    if !marker_is(ro, &narrowed)? {
        return Ok(Narrowed::Changed);
    }
    if target < current.size {
        let w = open_fill_writer(layout, rel, ro.as_fd())?;
        let size = current.size;
        blocking(move || {
            sys::punch_hole(&w, target, size - target)?;
            // Also free what xfs preallocated past EOF while the file was
            // written. A writer that opens, writes and closes per operation
            // (Ganesha with Close_Fast, NFSv3) defeats xfs's trim on close,
            // so without this the space comes back only with xfs's
            // background eofblocks scan, minutes later (found by the M1 e2e
            // test). KEEP_SIZE: the size, and so every byte the marker
            // covers, stays.
            sys::punch_hole(&w, size, PAST_EOF_PUNCH)?;
            w.sync_all()
        })
        .await?;
    }
    if current.flags.contains(Flags::CHUNK_MAP) {
        xattr::remove_raw(ro.as_fd(), MAP_XATTR_NAME)?;
    }
    Ok(Narrowed::Punched(narrowed))
}

/// Carry out one stub instruction.
///
/// Holds the file's lock for the whole handshake: no fill or content
/// replacement can interleave with it ([`StubOutcome::Busy`] if one runs).
pub async fn stub<N: FsalNotifier, G: SmbGuardHook>(
    layout: &Layout,
    req: &StubRequest,
    notifier: &N,
    guard: &G,
    seq: &MarkerSeq,
    locks: &FileLocks,
) -> StubOutcome {
    let Some(_lock) = locks.try_lock(req.file_id) else {
        return StubOutcome::Busy;
    };
    match stub_inner(layout, req, notifier, guard, seq).await {
        Ok(o) => o,
        Err(e) => skip(SkipReason::Io, e.to_string()),
    }
}

async fn stub_inner<N: FsalNotifier, G: SmbGuardHook>(
    layout: &Layout,
    req: &StubRequest,
    notifier: &N,
    guard: &G,
    seq: &MarkerSeq,
) -> io::Result<StubOutcome> {
    let ro = match sys::open_ro_beneath(&layout.data_root, &req.rel) {
        Ok(fd) => File::from(fd),
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            return Ok(skip(SkipReason::NotManaged, "file is gone"));
        }
        Err(e) => return Err(e),
    };
    let st = sys::fstat(ro.as_fd())?;
    if st.st_mode & libc::S_IFMT != libc::S_IFREG {
        return Ok(skip(SkipReason::NotManaged, "not a regular file"));
    }
    let mut key = sys::inode_key(ro.as_fd())?;
    key.file_id = req.file_id;
    if let Some((ino, gen_)) = req.expect_ino
        && (ino != key.ino || gen_ != key.ino_gen)
    {
        return Ok(skip(
            SkipReason::InodeChanged,
            format!("inode {} is not {ino}", key.ino),
        ));
    }
    if let Some(c) = req.expect_ctime_ns
        && ctime_ns(&st) != c
    {
        return Ok(skip(
            SkipReason::VersionMismatch,
            "file changed since its version was committed",
        ));
    }
    // 1. Check, under a read lease held until the punch decision.
    let Some(lease) = sys::ReadLease::take(ro.as_fd())? else {
        return Ok(skip(SkipReason::OpenForWrite, "file is open for write"));
    };
    // A writer that opened, wrote and closed before the lease was taken.
    let st = {
        let now = sys::fstat(ro.as_fd())?;
        if ctime_ns(&now) != ctime_ns(&st) {
            return Ok(skip(
                SkipReason::VersionMismatch,
                "file changed while the stub started",
            ));
        }
        now
    };
    let before = content_stamp(&st);
    let m = match xattr::read(ro.as_fd())? {
        ReadOutcome::Unmanaged => return Ok(skip(SkipReason::NotManaged, "no marker")),
        ReadOutcome::Bad(e) => return Ok(skip(SkipReason::BadMarker, e.to_string())),
        ReadOutcome::Marker(m) => m,
    };
    if m.file_id != req.file_id || m.version_id != req.version_id {
        return Ok(skip(
            SkipReason::VersionMismatch,
            format!(
                "local content is file {} version {}",
                m.file_id, m.version_id
            ),
        ));
    }
    if m.size != st.st_size as u64 {
        return Ok(skip(
            SkipReason::BadMarker,
            format!("marker size {} != inode size {}", m.size, st.st_size),
        ));
    }
    // 2. smb-guard.
    if let Err(e) = guard.before_narrow(key) {
        return Ok(StubOutcome::Deferred {
            detail: format!("smb-guard: {e}"),
        });
    }
    // Has a writer got in since step 1? `Some(reason)` if so.
    // (A `Mutex`, not a `RefCell`: the handshake future must stay `Send`.)
    let lease = Mutex::new(Some(lease));
    type Intruder = Option<(SkipReason, &'static str)>;
    let intruder = |lease: &Mutex<Option<sys::ReadLease<'_>>>| -> io::Result<Intruder> {
        if lease.lock().unwrap().as_ref().is_some_and(|l| l.broken()) {
            return Ok(Some((
                SkipReason::OpenForWrite,
                "opened for write during the stub handshake",
            )));
        }
        if content_stamp(&sys::fstat(ro.as_fd())?) != before {
            return Ok(Some((
                SkipReason::VersionMismatch,
                "written during the stub handshake",
            )));
        }
        Ok(None)
    };
    // 3–6, keeping whole pages resident.
    let keep = resident_granule(req.resident_bytes, m.size, st.st_blksize as u64);
    let res = narrow_and_punch(layout, &req.rel, &ro, key, m, keep, notifier, seq, || {
        if intruder(&lease)?.is_some() {
            return Ok(false);
        }
        // Release before the punch opens the file for write (our own
        // open would otherwise wait for our own lease).
        lease.lock().unwrap().take();
        Ok(true)
    })
    .await?;
    match res {
        Narrowed::Punched(n) => {
            info!(
                file_id = req.file_id,
                version_id = req.version_id,
                resident = n.resident_bytes,
                "stubbed"
            );
            Ok(StubOutcome::Stubbed {
                resident_bytes: n.resident_bytes,
            })
        }
        Narrowed::Changed => Ok(skip(
            SkipReason::VersionMismatch,
            "marker changed during the stub handshake",
        )),
        Narrowed::Aborted(n) | Narrowed::NotAcked(n) => {
            // Still holding the lease (unless the epoch check failed after
            // it was released): a writer waiting on it writes only after
            // the marker covers every byte again, when we drop it on return.
            if let Some((reason, detail)) = intruder(&lease)? {
                warn!(
                    file_id = req.file_id,
                    detail, "writer during the stub handshake; re-widening"
                );
                rewiden(&ro, key, &n, m, notifier, seq).await?;
                return Ok(skip(reason, detail));
            }
            warn!(
                file_id = req.file_id,
                resident = n.resident_bytes,
                "marker narrowed but not every FSAL acked; not punching"
            );
            Ok(StubOutcome::Deferred {
                detail: "invalidate not acknowledged".into(),
            })
        }
    }
}

/// Undo a narrowing whose bytes were not punched: put back `original`'s
/// coverage (new `marker_seq`), only over the marker we narrowed to.
async fn rewiden<N: FsalNotifier>(
    ro: &File,
    key: InodeKey,
    narrowed: &Marker,
    original: Marker,
    notifier: &N,
    seq: &MarkerSeq,
) -> io::Result<()> {
    if !marker_is(ro, narrowed)? {
        return Ok(());
    }
    // Nothing was punched and the chunk map (if any) is still there.
    let mut wide = original;
    wide.marker_seq = seq.next();
    if replace_marker(ro, &wide).await? {
        // Widening needs no ack.
        notifier.invalidate(key, what::MARKER, false).await;
    }
    Ok(())
}

/// Replace a placeholder's content version (the source object was
/// rewritten): narrow to nothing resident under the old version, punch, then
/// set the new size and record the new version with nothing resident.
///
/// Fenced like a fill: every FSAL holds the inode's `FETCHING` state for the
/// old version first, so none truncates the narrowed placeholder (which
/// would remove its marker and make the file the user's) between our marker
/// check and `set_len`. Every marker write is `XATTR_REPLACE` over the
/// marker we last saw.
///
/// Returns `Ok(None)` when it could not proceed safely (an FSAL did not
/// acknowledge, the fence broke, the marker changed, or a stub or fill
/// holds the file); retry later.
pub async fn replace_content<N: FsalNotifier>(
    layout: &Layout,
    rel: &Path,
    new: &crate::placeholder::PlaceholderSpec,
    notifier: &N,
    seq: &MarkerSeq,
    locks: &FileLocks,
) -> io::Result<Option<InodeKey>> {
    let Some(_lock) = locks.try_lock(new.file_id) else {
        return Ok(None);
    };
    let ro = File::from(sys::open_ro_beneath(&layout.data_root, rel)?);
    let mut key = sys::inode_key(ro.as_fd())?;
    key.file_id = new.file_id;
    let current = match xattr::read(ro.as_fd())? {
        ReadOutcome::Marker(m) => m,
        // Unmanaged or bad: never touch bytes we can't account for.
        _ => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "no valid marker on placeholder",
            ));
        }
    };
    if current.file_id != new.file_id {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "marker names another file",
        ));
    }
    if current.version_id == new.version_id {
        return Ok(Some(key));
    }
    let Some(epoch) = notifier.begin_replace(key, current.version_id).await else {
        return Ok(None);
    };
    // Ends the old version's FETCHING fence on every path out, including a
    // panic or the future being dropped mid-await (an aborted task): the
    // FSAL never sweeps FETCHING by age, so a fence left up would block
    // truncates of the file until the next reconnect.
    let _end = EndReplace {
        notifier,
        key,
        old_version: current.version_id,
    };
    replace_fenced(layout, rel, &ro, key, current, new, notifier, seq, epoch).await
}

struct EndReplace<'a, N: FsalNotifier> {
    notifier: &'a N,
    key: InodeKey,
    old_version: u64,
}

impl<N: FsalNotifier> Drop for EndReplace<'_, N> {
    fn drop(&mut self) {
        self.notifier.end_replace(self.key, self.old_version);
    }
}

#[allow(clippy::too_many_arguments)]
async fn replace_fenced<N: FsalNotifier>(
    layout: &Layout,
    rel: &Path,
    ro: &File,
    key: InodeKey,
    current: Marker,
    new: &crate::placeholder::PlaceholderSpec,
    notifier: &N,
    seq: &MarkerSeq,
    epoch: u64,
) -> io::Result<Option<InodeKey>> {
    // Narrow to PARTIAL with nothing resident, acknowledged by every FSAL,
    // before the size changes: no reader (cached marker or not) can see
    // the new size under a marker that calls old or absent bytes local.
    let narrowed = match narrow_impl(
        layout,
        rel,
        ro,
        key,
        current,
        0,
        true,
        notifier,
        seq,
        || Ok(true),
    )
    .await?
    {
        Narrowed::Punched(n) => n,
        Narrowed::NotAcked(_) | Narrowed::Aborted(_) | Narrowed::Changed => return Ok(None),
    };
    // Nothing of the old version is readable now. Resize, then name the new
    // version (still nothing resident) — only over our own marker, inside
    // the fence.
    if !notifier.fence_holds(epoch) || !marker_is(ro, &narrowed)? {
        return Ok(None);
    }
    let w = open_fill_writer(layout, rel, ro.as_fd())?;
    let size = new.size;
    blocking(move || {
        w.set_len(size)?;
        w.sync_all()
    })
    .await?;
    if !marker_is(ro, &narrowed)? || !replace_marker(ro, &new.marker(seq.next())).await? {
        return Ok(None);
    }
    notifier
        .invalidate(key, what::MARKER | what::ATTRS | what::CONTENT, false)
        .await;
    Ok(Some(key))
}
