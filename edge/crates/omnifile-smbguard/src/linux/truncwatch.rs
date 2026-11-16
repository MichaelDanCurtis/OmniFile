//! The notification watcher: `O_TRUNC` overwrites and writes into a stub's
//! local prefix (recommended design item 6, last bullet; M2 review B2).
//!
//! An `open(O_TRUNC)` raises no pre-content event (the truncate hooks sit
//! only in `truncate(2)`/`ftruncate(2)`), so a stub overwritten that way
//! keeps a `PARTIAL` marker naming the old content: a later fill would put
//! old bytes into the user's new file. Two things catch it:
//! - the **pre-content path**: the first access after the truncation (any
//!   read or write, which all raise events) sees a `PARTIAL` marker larger
//!   than the inode, and clears it before letting the access through
//!   (`Engine::clear_stale`);
//! - this **watcher**, a separate notification group, for a file that is
//!   truncated and then not accessed again (`: > file`): it clears the stale
//!   marker so NFS reads of the now empty file don't fail on `BAD_MARKER`.
//!
//! The same group always listens for `FAN_MODIFY`. A write into the local
//! prefix of a `PARTIAL` file is allowed by the pre-content guard (the
//! bytes are local), but the file is now a new version whose tail is still
//! the old one's, remote. [`RecallBroker::partial_modified`] tells the daemon,
//! which marks the file dirty, fills it whole and commits it.
//!
//! The group is kept in the systemd fd store (`FDNAME=omnifile-guardnotif-
//! <id>`) and taken back on restart, like the pre-content group (M3 review):
//! a write that lands while the daemon is down (one answered by the dead
//! instance, or by the new one before it re-armed a fresh group) still
//! queues `FS_MODIFY` here and is reported after the restart. It is armed
//! before any predecessor event is answered. No startup rescan is used: the
//! stub handshake's punch also moves mtime, so an mtime/ctime comparison
//! would report every stub at every restart. The private fill mount carries
//! an ignore mark in this group too, so the daemon's own fills and punches
//! raise nothing.

use std::os::fd::{AsFd, AsRawFd, FromRawFd, OwnedFd};
use std::sync::Arc;
use std::time::Duration;

use omnifile_marker::xattr;
use omnifile_stub::InodeKey;
use tracing::{error, info};

use super::engine::Engine;
use super::sys;
use crate::api::{EventIdentity, RecallBroker, TruncWatch};
use crate::classify::MarkerView;
use crate::uapi::*;

/// `FDNAME` prefix of the stored notification group.
pub const FDNAME_PREFIX: &str = "omnifile-guardnotif-";

/// Is `fd` a notification-class fanotify group with an unlimited queue?
fn usable_stored(fd: &OwnedFd) -> bool {
    let link = sys::fd_path(fd.as_raw_fd()).unwrap_or_default();
    let info = crate::fdinfo::parse(&sys::fdinfo(fd.as_raw_fd()).unwrap_or_default());
    let flags = info.flags.unwrap_or(u32::MAX);
    link.to_string_lossy() == "anon_inode:[fanotify]"
        && flags & 0x0c == FAN_CLASS_NOTIF
        && flags & FAN_UNLIMITED_QUEUE != 0
}

/// Arm the watcher. `stored`: the group systemd passed back, if any.
/// `group_id`: names a newly stored group.
pub fn start<B, I>(
    engine: Arc<Engine<B, I>>,
    data_fd: std::os::fd::RawFd,
    fill_fd: std::os::fd::RawFd,
    stored: Option<OwnedFd>,
    group_id: &str,
) -> std::io::Result<()>
where
    B: RecallBroker<I::Identity>,
    I: EventIdentity,
{
    // FAN_MODIFY always (B2: writes into a PARTIAL file's prefix);
    // FAN_CLOSE_WRITE too in `close` mode.
    let mask = match engine.cfg.trunc_watch {
        TruncWatch::Off => return Ok(()),
        TruncWatch::CloseWrite => FAN_CLOSE_WRITE | FAN_MODIFY,
        TruncWatch::Modify => FAN_MODIFY,
    };
    let g = match stored {
        Some(fd) if usable_stored(&fd) => {
            info!("REUSING the notification group from the systemd fd store");
            fd
        }
        other => {
            if let Some(fd) = other {
                // Not ours to use; keep it open (never release a group
                // blindly) and create a fresh one.
                error!(
                    fd = fd.as_raw_fd(),
                    "stored notification group unusable; creating a new one"
                );
                std::mem::forget(fd);
            }
            let g = sys::fanotify_init(
                FAN_CLASS_NOTIF | FAN_CLOEXEC | FAN_UNLIMITED_QUEUE,
                (libc::O_RDONLY | libc::O_LARGEFILE | libc::O_CLOEXEC) as u32,
            )?;
            let name = format!("{FDNAME_PREFIX}{group_id}");
            match crate::sdnotify::notify(
                &format!("FDSTORE=1\nFDNAME={name}\nFDPOLL=0"),
                &[g.as_raw_fd()],
            ) {
                Ok(true) => info!(fdname = %name, "notification group pushed to the fd store"),
                Ok(false) => {
                    info!("not under systemd: the notification group dies with this process")
                }
                Err(e) => error!(err = %e, "could not store the notification group"),
            }
            g
        }
    };
    // (Re-)arm: FAN_MARK_ADD is idempotent, and the fill mount is a new
    // mount object at every start.
    sys::fanotify_mark(
        g.as_raw_fd(),
        FAN_MARK_ADD | FAN_MARK_FILESYSTEM,
        mask,
        data_fd,
        None,
    )?;
    sys::fanotify_mark(
        g.as_raw_fd(),
        FAN_MARK_ADD | FAN_MARK_MOUNT | FAN_MARK_IGNORE | FAN_MARK_IGNORED_SURV_MODIFY,
        mask,
        fill_fd,
        None,
    )?;
    info!(mask = format!("{mask:#x}"), "O_TRUNC watcher armed");
    std::thread::Builder::new()
        .name("smbguard-trunc".into())
        .spawn(move || run(engine, g))?;
    Ok(())
}

fn run<B, I>(engine: Arc<Engine<B, I>>, g: OwnedFd)
where
    B: RecallBroker<I::Identity>,
    I: EventIdentity,
{
    let mut buf = vec![0u64; 4096];
    loop {
        // SAFETY: a u64 buffer viewed as bytes.
        let bytes =
            unsafe { std::slice::from_raw_parts_mut(buf.as_mut_ptr() as *mut u8, buf.len() * 8) };
        let n = match sys::read_into(g.as_raw_fd(), bytes) {
            Ok(n) => n,
            Err(e) => {
                if !matches!(sys::errno_of(&e), libc::EINTR | libc::EAGAIN) {
                    error!(err = %e, "O_TRUNC watcher read failed; retrying");
                    std::thread::sleep(Duration::from_millis(100));
                }
                continue;
            }
        };
        let (evs, _) = parse(&bytes[..n]);
        for ev in evs {
            if ev.mask & FAN_Q_OVERFLOW != 0 {
                error!("O_TRUNC watcher queue overflowed; relying on the pre-content path");
            }
            if ev.fd < 0 {
                continue;
            }
            // SAFETY: the kernel opened it for us.
            let f = unsafe { OwnedFd::from_raw_fd(ev.fd) };
            check(&engine, &f);
        }
    }
}

fn check<B, I>(engine: &Engine<B, I>, f: &OwnedFd)
where
    B: RecallBroker<I::Identity>,
    I: EventIdentity,
{
    let Ok(st) = sys::fstat(f.as_fd()) else {
        return;
    };
    if st.st_mode & libc::S_IFMT != libc::S_IFREG {
        return;
    }
    let MarkerView::Valid(m) = MarkerView::from_read(xattr::read(f.as_fd())) else {
        return;
    };
    if m.is_fully_resident() {
        return;
    }
    let key = InodeKey {
        fsid: engine.fsid,
        ino: st.st_ino,
        ino_gen: omnifile_stub::sys::inode_generation(f.as_fd()).unwrap_or(0),
        file_id: m.file_id,
    };
    let size = st.st_size as u64;
    if size == m.size {
        // Nothing resident: there is no local prefix to have been written
        // into. Every write raises a pre-content event and waits for a fill
        // first. This is the FS_MODIFY of the daemon creating the
        // placeholder (it sizes the O_TMPFILE before setting the marker),
        // and reporting it would fill the file whole and commit it as a new
        // version (found by the M5 e2e test).
        if m.resident_bytes == 0 {
            return;
        }
        // A write into the local prefix (the guard allowed it: the bytes
        // were local). The file is now a new version over a remote tail.
        engine
            .stats
            .partial_modified
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        info!(
            ino = key.ino,
            file_id = m.file_id,
            "PARTIAL file modified: reported to the daemon"
        );
        engine.broker.partial_modified(key, &m);
        return;
    }
    // Larger: an O_APPEND, handled on the next access (Verdict::Extended).
    // Smaller: truncated by O_TRUNC.
    if size > m.size || !engine.broker.may_act_on_size_mismatch(key, &m) {
        return;
    }
    let handle = sys::handle_of(f.as_fd()).ok();
    let path = sys::fd_path(f.as_raw_fd());
    engine.clear_stale(f.as_fd(), key, handle.as_ref(), path.as_deref());
}
