//! Placeholders, the stub handshake and recall fill (R1,
//! `docs/design/fsal-namespace.md` §5 and §5.1).
//!
//! The safety rules, all fail-closed:
//! - A placeholder never appears without its marker: `O_TMPFILE` in a
//!   staging directory on the same xfs filesystem, marker and attributes set
//!   on the fd, then `linkat` into place.
//! - Removing bytes (stub, eviction, content-version change) narrows the
//!   marker first, then waits for **every** connected FSAL to acknowledge an
//!   `INVALIDATE`, and only then punches. No ack, no punch.
//! - A fill widens the marker only after the new bytes are `fsync`ed, and
//!   writes only into a file whose marker names the content version being
//!   fetched.
//! - Writes go through the private fill path (M2: a bind mount carrying the
//!   smb-guard's ignore mark), never through the share's path.
//!
//! Every filesystem effect is idempotent, so a redo after a crash is safe.

use std::future::Future;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};

#[cfg(target_os = "linux")]
pub mod chunks;
#[cfg(target_os = "linux")]
pub mod fill;
#[cfg(target_os = "linux")]
pub mod handshake;
#[cfg(target_os = "linux")]
pub mod materialize;
#[cfg(target_os = "linux")]
pub mod placeholder;
#[cfg(target_os = "linux")]
pub mod sys;

/// Sleep on the tokio timer, or block the thread outside a runtime.
#[cfg(target_os = "linux")]
pub(crate) async fn sleep(d: std::time::Duration) {
    if tokio::runtime::Handle::try_current().is_ok() {
        tokio::time::sleep(d).await
    } else {
        std::thread::sleep(d)
    }
}

/// Run blocking filesystem work (big writes, `fsync`, punches, checksums)
/// off the async workers: a stalled worker delays the FSAL socket's `PONG`s
/// past the FSAL's timeout and disconnects every FSAL.
#[cfg(target_os = "linux")]
pub(crate) async fn blocking<T: Send + 'static>(
    f: impl FnOnce() -> io::Result<T> + Send + 'static,
) -> io::Result<T> {
    // Outside a tokio runtime (a test harness driving the future with its
    // own executor), the caller's thread is not an async worker: run inline.
    match tokio::runtime::Handle::try_current() {
        Ok(h) => h.spawn_blocking(f).await.map_err(io::Error::other)?,
        Err(_) => f(),
    }
}

/// The FSAL's key for an inode (`obj_ref` minus `export_id`).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub struct InodeKey {
    pub fsid: u64,
    pub ino: u64,
    pub ino_gen: u32,
    pub file_id: u64,
}

/// `INVALIDATE.what` bits (`docs/contracts/fsal-socket.md` §5.5).
pub mod what {
    pub const MARKER: u32 = 1 << 0;
    pub const ATTRS: u32 = 1 << 1;
    pub const CONTENT: u32 = 1 << 2;
    pub const DIR: u32 = 1 << 3;
}

/// How the stubber and filler talk to the FSALs (implemented by the daemon
/// over `omnifile-fsal-proto`).
pub trait FsalNotifier: Send + Sync {
    /// Send `INVALIDATE` to every connected FSAL. With `ack`, resolve to
    /// true only when at least one FSAL is connected and every connected one
    /// acknowledged. No FSAL connected is *not* an acknowledgement, unless
    /// the edge is configured to run no Ganesha at all.
    fn invalidate(&self, key: InodeKey, what: u32, ack: bool) -> impl Future<Output = bool> + Send;
    /// The FSAL connection epoch now: it changes whenever a connection
    /// becomes ready or stops being ready. A narrowing records it before
    /// writing the narrowed marker and punches only while
    /// [`FsalNotifier::fence_holds`] still holds for it.
    ///
    /// The default (0) suits notifiers whose `fence_holds` ignores epochs.
    fn connection_epoch(&self) -> u64 {
        0
    }
    /// A fill made `[0, resident_bytes)` durable and widened the marker.
    fn fill_progress(&self, key: InodeKey, version_id: u64, resident_bytes: u64);
    /// Fence a fill before its first write: every connected FSAL must hold
    /// the inode's `FETCHING` state (and therefore refuse to truncate it,
    /// `fsal-socket.md` §6.1), confirmed by an acknowledged `INVALIDATE`
    /// sent after the state. Returns the FSAL connection epoch the fence
    /// covers, or `None` if not every FSAL confirmed: then nothing is written.
    fn begin_fill(
        &self,
        key: InodeKey,
        version_id: u64,
    ) -> impl Future<Output = Option<u64>> + Send;
    /// True while no FSAL connection was made or lost since `epoch`. A fill
    /// checks it before every write and widen, and stops when it breaks.
    fn fence_holds(&self, epoch: u64) -> bool;
    /// True once the daemon wants the fill of `(key, version_id)` stopped
    /// (the recall was cancelled or failed, or the daemon is shutting down).
    /// Checked with the fence before every write and widen.
    fn fill_cancelled(&self, _key: InodeKey, _version_id: u64) -> bool {
        false
    }
    /// Fence a content-version replacement of a file whose marker names
    /// `old_version`, as [`FsalNotifier::begin_fill`] fences a fill: every
    /// connected FSAL holds the inode's `FETCHING` state for `old_version`
    /// (so it refuses to truncate the narrowed placeholder), confirmed by an
    /// acknowledged `INVALIDATE`. Returns the connection epoch the fence
    /// covers, or `None` (then nothing is changed). The default refuses.
    fn begin_replace(
        &self,
        _key: InodeKey,
        _old_version: u64,
    ) -> impl Future<Output = Option<u64>> + Send {
        std::future::ready(None)
    }
    /// End the fence [`FsalNotifier::begin_replace`] set (push a terminal
    /// state for `old_version`). Called on every path out.
    fn end_replace(&self, _key: InodeKey, _old_version: u64) {}
    /// The smb-guard's step 2 (`fsal-namespace.md` §5.1): remove the inode's
    /// fanotify ignore mark, and keep the guard from re-adding it, before
    /// any narrowing of its marker (stub, content change, fill
    /// narrow-back). Without it a later SMB read of the punched range skips
    /// the guard and reads zeros. Default: no guard.
    fn before_narrow(&self, _key: InodeKey) -> io::Result<()> {
        Ok(())
    }
}

/// The smb-guard's part of the handshake (step 2). M2 removes the inode's
/// fanotify ignore mark here; M1 has no guard, so the default does nothing.
pub trait SmbGuardHook: Send + Sync {
    fn before_narrow(&self, _key: InodeKey) -> io::Result<()> {
        Ok(())
    }
}

/// No smb-guard (M1).
pub struct NoSmbGuard;
impl SmbGuardHook for NoSmbGuard {}

/// What a fill reads from (the Edge-I/O Blob client, through an adapter in
/// the daemon).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FetchTarget {
    pub file_id: u64,
    /// The content version being fetched.
    pub version_id: u64,
    /// Read URL (from the recall's `read_credential` or `/credentials`).
    pub url: String,
    /// What kind of URL it is, and so whether its ranges are verified.
    pub source: SourceKind,
    pub size: u64,
    /// Whole-file CRC-64/NVME, when the catalog knows it.
    pub crc64nvme: Option<u64>,
}

/// Where a fill's bytes come from.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub enum SourceKind {
    /// An Azure blob URL with a read SAS: every range of at most
    /// [`MAX_VERIFIED_RANGE`] comes with Azure's CRC-64 of it.
    #[default]
    AzureSas,
    /// An S3 presigned GetObject URL: ranges carry no checksum, so nothing
    /// a fill fetched is exposed before the whole-file CRC matches
    /// (`docs/design/fsal-namespace.md` §5.3).
    S3Presigned,
    /// An S3 object version the edge signs itself (SigV4, temporary
    /// credentials from its IAM Roles Anywhere helper). The "URL" is the
    /// service's pointer, encoded by the daemon (no secret). Ranges carry no
    /// checksum, as for [`SourceKind::S3Presigned`].
    S3Signed,
}

impl SourceKind {
    /// Every range the source returns was verified against the service's
    /// checksum of that range.
    pub fn ranges_verified(self) -> bool {
        matches!(self, SourceKind::AzureSas)
    }
}

/// Largest range a fill asks for at once: every range must be verified by
/// its source against the service's own checksum before it is written,
/// and Azure returns a range CRC-64 only for ranges up to 4 MiB
/// (`omnifile_blob::MAX_RANGE_CRC`).
pub const MAX_VERIFIED_RANGE: u64 = 4 << 20;

pub trait RangeSource: Send + Sync {
    /// Exactly `len` bytes at `offset` (`len` ≤ [`MAX_VERIFIED_RANGE`]),
    /// read from the content version the target's URL names; or an error.
    /// For a target whose [`SourceKind::ranges_verified`], the bytes must be
    /// verified against the service's checksum of that range (Azure's
    /// `x-ms-content-crc64`): a fill widens the marker over them before the
    /// whole-file CRC is known, so an unverified range must never be
    /// returned. For other targets the fill widens only after the
    /// whole-file CRC matched.
    fn get_range(
        &self,
        t: &FetchTarget,
        offset: u64,
        len: u64,
    ) -> impl Future<Output = io::Result<Vec<u8>>> + Send;
}

/// Where things are on disk.
#[derive(Clone, Debug)]
pub struct Layout {
    /// The xfs data volume (shares live below it).
    pub data_root: PathBuf,
    /// The private fill path: the same tree, reached through a mount the
    /// smb-guard ignores (M2). In M1 it may equal `data_root`.
    pub fill_root: PathBuf,
    /// `O_TMPFILE` staging directory, on the same filesystem.
    pub staging: PathBuf,
}

impl Layout {
    pub fn new(data_root: impl Into<PathBuf>) -> Layout {
        let data_root = data_root.into();
        Layout {
            fill_root: data_root.clone(),
            staging: data_root.join(".omni/staging"),
            data_root,
        }
    }

    pub fn path(&self, rel: &Path) -> PathBuf {
        self.data_root.join(rel)
    }

    pub fn fill_path(&self, rel: &Path) -> PathBuf {
        self.fill_root.join(rel)
    }
}

/// Split a data-volume-relative path into its parent and final name, after
/// checking it can only name something inside its own share: every
/// component must be a plain name (no `/`, `.`, `..` or empty path). The
/// first component is the share. The daemon resolves the parent from a
/// directory fd without following symlinks (`sys::resolve_beneath`), so
/// together nothing a share's users create can redirect a root-owned write
/// outside the share.
pub fn split_rel(rel: &Path) -> io::Result<(&Path, &std::ffi::OsStr)> {
    use std::path::Component;
    let bad = || {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("{} is not a plain relative path", rel.display()),
        )
    };
    let mut n = 0;
    for c in rel.components() {
        match c {
            Component::Normal(_) => n += 1,
            _ => return Err(bad()),
        }
    }
    // `components()` drops interior `.`; reject it (and `//`) textually too.
    let bytes = rel.as_os_str().as_encoded_bytes();
    if n == 0
        || bytes.ends_with(b"/")
        || bytes
            .split(|b| *b == b'/')
            .any(|s| s.is_empty() || s == b".")
    {
        return Err(bad());
    }
    let name = rel.file_name().ok_or_else(bad)?;
    Ok((rel.parent().unwrap_or(Path::new("")), name))
}

/// The share (first component) of a data-volume-relative path.
pub fn share_of(rel: &Path) -> Option<&std::ffi::OsStr> {
    match rel.components().next()? {
        std::path::Component::Normal(s) => Some(s),
        _ => None,
    }
}

/// Suffix of the recall note the daemon writes next to a stub.
pub const NOTE_SUFFIX: &str = ".RECALLING.txt";

/// The note's name for a file named `name`.
pub fn note_name(name: &std::ffi::OsStr) -> std::ffi::OsString {
    let mut s = name.to_os_string();
    s.push(NOTE_SUFFIX);
    s
}

/// Longest note the daemon writes, in characters.
pub const NOTE_MAX_CHARS: usize = 4096;

/// Make the service's note text safe to put in a file users read: the file
/// name, which a user chose (NFS names may hold `\n` or terminal escapes),
/// is escaped where the text quotes it at the start; every other control
/// character except `\n` is dropped; the length is capped.
pub fn sanitize_note(text: &str, name: &str) -> String {
    let escaped: String = name
        .chars()
        .map(|c| {
            if c.is_control() {
                c.escape_debug().collect::<String>()
            } else {
                c.to_string()
            }
        })
        .collect();
    let text = match text.strip_prefix(name) {
        Some(rest) if !name.is_empty() => format!("{escaped}{rest}"),
        _ => text.to_string(),
    };
    let mut out: String = text
        .chars()
        .filter(|c| *c == '\n' || !c.is_control())
        .take(NOTE_MAX_CHARS)
        .collect();
    if !out.ends_with('\n') {
        out.push('\n');
    }
    out
}

/// Daemon-wide, increasing `marker_seq`: microseconds since the epoch, or
/// the previous value plus one, whichever is larger. It keeps increasing
/// across restarts as long as the clock doesn't step back by more than the
/// daemon's downtime.
#[derive(Debug, Default)]
pub struct MarkerSeq(AtomicU64);

impl MarkerSeq {
    pub fn new() -> MarkerSeq {
        MarkerSeq(AtomicU64::new(0))
    }

    pub fn next(&self) -> u64 {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_micros() as u64)
            .unwrap_or(0);
        let mut cur = self.0.load(Ordering::Relaxed);
        loop {
            let next = now.max(cur + 1);
            match self
                .0
                .compare_exchange_weak(cur, next, Ordering::SeqCst, Ordering::Relaxed)
            {
                Ok(_) => return next,
                Err(v) => cur = v,
            }
        }
    }
}

/// One stub, fill or content replacement per file at a time.
#[derive(Debug, Default)]
pub struct FileLocks(Mutex<std::collections::HashSet<u64>>);

pub struct FileLockGuard<'a> {
    locks: &'a FileLocks,
    file_id: u64,
}

impl FileLocks {
    pub fn new() -> FileLocks {
        FileLocks::default()
    }

    /// `None` if another operation holds the file.
    pub fn try_lock(&self, file_id: u64) -> Option<FileLockGuard<'_>> {
        if self.0.lock().unwrap().insert(file_id) {
            Some(FileLockGuard {
                locks: self,
                file_id,
            })
        } else {
            None
        }
    }

    /// Number of files currently locked.
    pub fn held(&self) -> usize {
        self.0.lock().unwrap().len()
    }

    pub fn is_locked(&self, file_id: u64) -> bool {
        self.0.lock().unwrap().contains(&file_id)
    }
}

impl Drop for FileLockGuard<'_> {
    fn drop(&mut self) {
        self.locks.0.lock().unwrap().remove(&self.file_id);
    }
}

/// Why a stub instruction was not carried out (reported with `stub/skip`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SkipReason {
    /// The local content is another version.
    VersionMismatch,
    OpenForWrite,
    /// No marker: the file isn't managed (or was overwritten locally).
    NotManaged,
    BadMarker,
    /// The inode at the path is not the one the replica knows.
    InodeChanged,
    Io,
}

impl SkipReason {
    pub fn as_str(self) -> &'static str {
        match self {
            SkipReason::VersionMismatch => "version_mismatch",
            SkipReason::OpenForWrite => "open_for_write",
            SkipReason::NotManaged => "not_managed",
            SkipReason::BadMarker => "bad_marker",
            SkipReason::InodeChanged => "inode_changed",
            SkipReason::Io => "io_error",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StubOutcome {
    /// Bytes past `resident_bytes` are punched; the marker says so.
    Stubbed { resident_bytes: u64 },
    /// Not done; report `stub/skip` with the reason.
    Skipped { reason: SkipReason, detail: String },
    /// The marker is narrowed but some FSAL didn't acknowledge the
    /// invalidation, so nothing was punched. Safe; retry later.
    Deferred { detail: String },
    /// Another stub, fill or content replacement holds the file; nothing done.
    Busy,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn marker_seq_increases() {
        let s = MarkerSeq::new();
        let a = s.next();
        let b = s.next();
        assert!(b > a);
    }

    #[test]
    fn file_locks_are_exclusive() {
        let l = FileLocks::new();
        let g = l.try_lock(5).unwrap();
        assert!(l.try_lock(5).is_none());
        assert!(l.try_lock(6).is_some());
        drop(g);
        assert!(l.try_lock(5).is_some());
    }

    #[test]
    fn split_rel_accepts_plain_paths() {
        let (p, n) = split_rel(Path::new("proj/a/f.segy")).unwrap();
        assert_eq!(p, Path::new("proj/a"));
        assert_eq!(n, "f.segy");
        let (p, n) = split_rel(Path::new("proj")).unwrap();
        assert_eq!(p, Path::new(""));
        assert_eq!(n, "proj");
        assert_eq!(
            share_of(Path::new("proj/a/f")),
            Some(std::ffi::OsStr::new("proj"))
        );
    }

    #[test]
    fn split_rel_rejects_escapes() {
        for bad in [
            "",
            "/etc/passwd",
            "../etc/passwd",
            "proj/../other/f",
            "proj/./f",
            "./proj/f",
            "proj//f",
            "proj/f/",
            "proj/..",
            ".",
        ] {
            assert!(split_rel(Path::new(bad)).is_err(), "{bad:?} accepted");
        }
    }

    #[test]
    fn sanitize_note_escapes_the_name_and_controls() {
        let name = "a\nFAKE: all files deleted\x1b[2J.segy";
        let text = format!("{name} is archived.\nYou will get a Teams message.\n");
        let s = sanitize_note(&text, name);
        assert_eq!(s.lines().count(), 2, "{s:?}");
        assert!(s.starts_with("a\\nFAKE"), "{s:?}");
        assert!(!s.contains('\x1b'));
        assert_eq!(sanitize_note("x\ty", "z"), "xy\n");
        assert_eq!(
            sanitize_note(&"x".repeat(10_000), "z").len(),
            NOTE_MAX_CHARS + 1
        );
    }

    #[test]
    fn note_names() {
        assert_eq!(
            note_name(std::ffi::OsStr::new("x.segy")),
            std::ffi::OsString::from("x.segy.RECALLING.txt")
        );
    }

    #[test]
    fn layout_paths() {
        let l = Layout::new("/srv/omnifile/data");
        assert_eq!(l.staging, PathBuf::from("/srv/omnifile/data/.omni/staging"));
        assert_eq!(
            l.path(Path::new("p/f")),
            PathBuf::from("/srv/omnifile/data/p/f")
        );
    }
}
