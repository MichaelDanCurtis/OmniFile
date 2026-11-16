//! Ignore marks on wholly local inodes (recommended design item 8, A13).
//!
//! Without them every `read()` of a resident file costs an event round
//! trip (4 KiB reads fell from 3,600 to 76 MB/s). The mark is a plain
//! `FAN_MARK_IGNORE | FAN_MARK_EVICTABLE` for `FAN_PRE_ACCESS`, added
//! through a **read-only fd opened by handle on the private fill mount**,
//! after the event's own fd is closed:
//! - through an fd that is open for write (the spike's `O_RDWR` event fd),
//!   `do_fanotify_mark()` returns 0 and adds nothing (`fanotify_user.c`);
//! - never `FAN_MARK_IGNORE_SURV`: a plain ignore mark is cleared by the
//!   kernel on the next modification, so a punch re-arms the guard even if
//!   the explicit removal below were missed. A surviving one read zeros
//!   after a re-stub (A13 (b)).
//!
//! **Removal before narrowing** (`fsal-namespace.md` §5.1 step 2) is
//! [`IgnoreMarks::remove`], which the stub handshake calls through
//! [`omnifile_stub::SmbGuardHook`]. It first puts the inode on a
//! suppression list, under the same lock as [`IgnoreMarks::add`], so an
//! event that classified the file as resident just before the narrowing
//! cannot put the mark back afterwards.
//!
//! The kernel adds nothing, and still returns 0, while the inode is open for
//! write anywhere (a writer still has it open, or smbd holds a writable
//! handle). The guard cannot see that, so it never trusts an add: it only
//! rate-limits retries per inode ([`RETRY_AFTER`]). Once a mark is really in
//! place, events for that inode stop arriving, and so do the retries.
//!
//! Marks belong to the group, which outlives the process; a restarted guard
//! does not know which inodes have one. Removal therefore never relies on
//! the in-memory set: it reaches the inode by an xfs handle built from
//! `(ino, generation)` and removes whatever mark is there.

use std::collections::HashMap;
use std::io;
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, RawFd};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use omnifile_stub::{InodeKey, SmbGuardHook};
use tracing::{debug, warn};

use super::sys;
use crate::journal::Handle;
use crate::uapi::{
    FAN_MARK_ADD, FAN_MARK_EVICTABLE, FAN_MARK_IGNORE, FAN_MARK_REMOVE, FAN_ONDIR, FAN_OPEN_PERM,
    FAN_PRE_ACCESS,
};

/// Open-permission marks on unfilled rewind directories
/// (`fsal-namespace.md` section 7). smbd opens a previous version's folder
/// only to list it; with this mark the open is held while the daemon fills
/// it, so it is never listed empty.
///
/// The daemon marks each rewind directory as it creates it unfilled, before
/// it lets anyone in, and marks every unfilled one it finds at startup.
/// Marks are inode marks in the guard's group, so they last as long as the
/// group (kept in the fd store) and the inode: an evicted directory, which
/// is unfilled again, is still marked. They are not evictable: losing one
/// would let an unfilled directory be listed empty. A filled directory's
/// open is allowed at once.
pub struct DirMarks {
    group: RawFd,
    pub added: AtomicU64,
}

impl DirMarks {
    pub(crate) fn new(group: RawFd) -> DirMarks {
        DirMarks {
            group,
            added: AtomicU64::new(0),
        }
    }

    /// Mark the directory `dir` is open on. Idempotent.
    pub fn watch(&self, dir: BorrowedFd<'_>) -> io::Result<()> {
        sys::fanotify_mark(
            self.group,
            FAN_MARK_ADD,
            FAN_OPEN_PERM | FAN_ONDIR,
            dir.as_raw_fd(),
            None,
        )?;
        self.added.fetch_add(1, Ordering::Relaxed);
        Ok(())
    }
}

/// How long a removal keeps the guard from re-adding a mark. The handshake
/// narrows and punches within a second or two of the removal.
const SUPPRESS_FOR: Duration = Duration::from_secs(300);
/// Minimum time between two attempts to mark the same inode.
pub const RETRY_AFTER: Duration = Duration::from_secs(1);
/// Bound on the in-memory bookkeeping.
const MAX_TRACKED: usize = 1 << 20;

struct State {
    /// Inode -> when a mark was last attempted.
    marked: HashMap<u64, Instant>,
    suppressed: HashMap<u64, Instant>,
}

pub struct IgnoreMarks {
    group: RawFd,
    fill_mount_fd: RawFd,
    enabled: bool,
    state: Mutex<State>,
    pub added: AtomicU64,
    pub removed: AtomicU64,
}

impl IgnoreMarks {
    pub(crate) fn new(group: RawFd, fill_mount_fd: RawFd, enabled: bool) -> IgnoreMarks {
        IgnoreMarks {
            group,
            fill_mount_fd,
            enabled,
            state: Mutex::new(State {
                marked: HashMap::new(),
                suppressed: HashMap::new(),
            }),
            added: AtomicU64::new(0),
            removed: AtomicU64::new(0),
        }
    }

    /// Add an ignore mark on a wholly local inode. Call only after the
    /// event's fd is closed. Best effort: failure only costs speed.
    pub(crate) fn add(&self, ino: u64, handle: &Handle) {
        if !self.enabled {
            return;
        }
        let mut st = self.state.lock().unwrap();
        if st
            .marked
            .get(&ino)
            .is_some_and(|t| t.elapsed() < RETRY_AFTER)
        {
            return;
        }
        if let Some(t) = st.suppressed.get(&ino) {
            if t.elapsed() < SUPPRESS_FOR {
                return;
            }
            st.suppressed.remove(&ino);
        }
        let ro = match sys::open_by_handle(self.fill_mount_fd, handle, libc::O_RDONLY) {
            Ok(fd) => fd,
            Err(e) => {
                debug!(ino, err = %e, "ignore mark: cannot reopen through the fill mount");
                return;
            }
        };
        match sys::fstat(ro.as_fd()) {
            Ok(s) if s.st_ino == ino => {}
            _ => return,
        }
        match sys::fanotify_mark(
            self.group,
            FAN_MARK_ADD | FAN_MARK_IGNORE | FAN_MARK_EVICTABLE,
            FAN_PRE_ACCESS,
            ro.as_raw_fd(),
            None,
        ) {
            Ok(()) => {
                // Possibly a silent no-op (open for write elsewhere): the
                // next event after RETRY_AFTER tries again.
                if st.marked.len() >= MAX_TRACKED {
                    st.marked.retain(|_, t| t.elapsed() < RETRY_AFTER);
                }
                st.marked.insert(ino, Instant::now());
                self.added.fetch_add(1, Ordering::Relaxed);
            }
            Err(e) => debug!(ino, err = %e, "ignore mark not added"),
        }
    }

    /// Remove any ignore mark on `key`'s inode and keep the guard from
    /// re-adding one for a while. Errors only when the inode exists and the
    /// removal could not be confirmed: the handshake then defers.
    pub fn remove(&self, key: InodeKey) -> io::Result<()> {
        let mut st = self.state.lock().unwrap();
        if st.suppressed.len() >= MAX_TRACKED {
            st.suppressed.retain(|_, t| t.elapsed() < SUPPRESS_FOR);
        }
        st.suppressed.insert(key.ino, Instant::now());
        st.marked.remove(&key.ino);
        let fd = match sys::open_by_handle(
            self.fill_mount_fd,
            &sys::xfs_handle(key.ino, key.ino_gen),
            libc::O_RDONLY,
        ) {
            Ok(fd) => fd,
            // The inode is gone (or was reused with another generation):
            // there is nothing to unmark.
            Err(e) if matches!(sys::errno_of(&e), libc::ESTALE | libc::ENOENT) => return Ok(()),
            Err(e) => return Err(e),
        };
        if sys::fstat(fd.as_fd())?.st_ino != key.ino {
            return Err(io::Error::other("handle resolved to another inode"));
        }
        match sys::fanotify_mark(
            self.group,
            FAN_MARK_REMOVE | FAN_MARK_IGNORE,
            FAN_PRE_ACCESS,
            fd.as_raw_fd(),
            None,
        ) {
            Ok(()) => {
                self.removed.fetch_add(1, Ordering::Relaxed);
                Ok(())
            }
            // No mark on this inode.
            Err(e) if sys::errno_of(&e) == libc::ENOENT => Ok(()),
            Err(e) => {
                warn!(ino = key.ino, err = %e, "could not remove ignore mark");
                Err(e)
            }
        }
    }
}

impl SmbGuardHook for IgnoreMarks {
    fn before_narrow(&self, key: InodeKey) -> io::Result<()> {
        self.remove(key)
    }
}
