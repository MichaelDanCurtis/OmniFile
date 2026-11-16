//! Namespace events (Linux): which directories changed, as file handles
//! (`docs/design/namespace-sync.md`).
//!
//! A fanotify notification group in directory-FID mode (`FAN_REPORT_DIR_FID`,
//! Linux 5.9), marked on the whole filesystem: creates, deletes, moves in and
//! out, and attribute changes, on files and directories. Each event carries
//! the handle of the directory it happened in (for an attribute change on a
//! directory, the directory itself). The daemon reconciles the whole
//! directory, so a lost or reordered event costs only a later pass.
//!
//! Where the kernel has them (Linux 5.17: `FAN_REPORT_NAME` with
//! `FAN_REPORT_TARGET_FID`), each event also names the entry and carries the
//! entry's own handle ([`EntryChanged`]). The daemon uses them only to say
//! *who* made a change (`control-api.md` §4.1 `actor`), never to decide what
//! changed. `nix` doesn't decode FID-mode events, so this reads them itself.

use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::path::Path;

use tokio::io::Interest;
use tokio::io::unix::AsyncFd;
use tokio::sync::mpsc::Sender;

use crate::DetectError;

const FAN_CLOEXEC: libc::c_uint = 0x1;
const FAN_NONBLOCK: libc::c_uint = 0x2;
const FAN_CLASS_NOTIF: libc::c_uint = 0x0;
const FAN_REPORT_DIR_FID: libc::c_uint = 0x400;
/// Also the object's own handle: without it, the kernel reports no
/// attribute change on a file (a `chown`), only directory entry changes.
const FAN_REPORT_FID: libc::c_uint = 0x200;
/// The entry's name with its directory (Linux 5.9).
const FAN_REPORT_NAME: libc::c_uint = 0x800;
/// The child's own handle for creates, deletes and moves (Linux 5.17).
const FAN_REPORT_TARGET_FID: libc::c_uint = 0x1000;

const FAN_MARK_ADD: libc::c_uint = 0x1;
const FAN_MARK_FILESYSTEM: libc::c_uint = 0x100;

const FAN_ATTRIB: u64 = 0x4;
const FAN_MOVED_FROM: u64 = 0x40;
const FAN_MOVED_TO: u64 = 0x80;
const FAN_CREATE: u64 = 0x100;
const FAN_DELETE: u64 = 0x200;
const FAN_Q_OVERFLOW: u64 = 0x4000;
const FAN_ONDIR: u64 = 0x4000_0000;

const FAN_EVENT_INFO_TYPE_FID: u8 = 1;
const FAN_EVENT_INFO_TYPE_DFID_NAME: u8 = 2;
const FAN_EVENT_INFO_TYPE_DFID: u8 = 3;

/// The events that change what a directory holds or who owns its entries.
pub const NS_MASK: u64 =
    FAN_CREATE | FAN_DELETE | FAN_MOVED_FROM | FAN_MOVED_TO | FAN_ATTRIB | FAN_ONDIR;

/// A directory that changed, by handle (open it with `open_by_handle_at`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DirChanged {
    pub handle_type: i32,
    pub handle: Vec<u8>,
    /// Who changed it (the daemon skips its own).
    pub pid: i32,
    /// The change was to the directory itself (its attributes), not to an
    /// entry in it: its parent holds what changed.
    pub on_dir_itself: bool,
}

/// What happened to one entry.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EntryOp {
    Create,
    Delete,
    MovedFrom,
    MovedTo,
    Attrib,
}

/// One entry's change, with its name and handles (names mode only): who
/// made it, never what the daemon does about it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EntryChanged {
    pub op: EntryOp,
    /// `FAN_ONDIR`.
    pub is_dir: bool,
    pub pid: i32,
    /// The directory the entry is in (its new one after a move in), as a
    /// handle `(handle_type, bytes)`.
    pub dir: Option<(i32, Vec<u8>)>,
    /// The entry's name in it (`.` or empty for the directory itself).
    pub name: Vec<u8>,
    /// The entry's own handle (`FAN_REPORT_TARGET_FID`, or the object of an
    /// attribute change).
    pub child: Option<(i32, Vec<u8>)>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NsEvent {
    Dir(DirChanged),
    Entry(EntryChanged),
    /// Events were lost: reconcile everything.
    Overflow,
}

fn err(what: &'static str) -> DetectError {
    DetectError::Fanotify {
        what,
        source: std::io::Error::last_os_error(),
    }
}

/// A `struct file_handle` at `h` within `buf[..end]`: `(type, bytes, where
/// the bytes end)`.
fn handle_at(buf: &[u8], h: usize, end: usize) -> Option<(i32, Vec<u8>, usize)> {
    if h + 8 > end {
        return None;
    }
    let n = u32::from_ne_bytes(buf[h..h + 4].try_into().ok()?) as usize;
    let ty = i32::from_ne_bytes(buf[h + 4..h + 8].try_into().ok()?);
    if h + 8 + n > end {
        return None;
    }
    Some((ty, buf[h + 8..h + 8 + n].to_vec(), h + 8 + n))
}

/// Every op in an event's mask, oldest first as far as it can be told: the
/// kernel merges unread events with the same records (the same directory,
/// name and child), so `mv f g; chown g; rm g` can come as one event.
fn entry_ops(mask: u64) -> Vec<EntryOp> {
    [
        (FAN_CREATE, EntryOp::Create),
        (FAN_MOVED_TO, EntryOp::MovedTo),
        (FAN_ATTRIB, EntryOp::Attrib),
        (FAN_MOVED_FROM, EntryOp::MovedFrom),
        (FAN_DELETE, EntryOp::Delete),
    ]
    .into_iter()
    .filter(|(bit, _)| mask & bit != 0)
    .map(|(_, op)| op)
    .collect()
}

/// Parse a buffer of FID-mode events.
pub fn parse_events(buf: &[u8], out: &mut Vec<NsEvent>) {
    let mut at = 0usize;
    // struct fanotify_event_metadata: event_len u32, vers u8, reserved u8,
    // metadata_len u16, mask u64, fd i32, pid i32 (24 bytes).
    while at + 24 <= buf.len() {
        let event_len = u32::from_ne_bytes(buf[at..at + 4].try_into().unwrap()) as usize;
        let meta_len = u16::from_ne_bytes(buf[at + 6..at + 8].try_into().unwrap()) as usize;
        if event_len < 24 || at + event_len > buf.len() || meta_len < 24 {
            break;
        }
        let mask = u64::from_ne_bytes(buf[at + 8..at + 16].try_into().unwrap());
        let pid = i32::from_ne_bytes(buf[at + 20..at + 24].try_into().unwrap());
        if mask & FAN_Q_OVERFLOW != 0 {
            out.push(NsEvent::Overflow);
            at += event_len;
            continue;
        }
        // Info records: header (type u8, pad u8, len u16), then fsid (8
        // bytes) and struct file_handle (handle_bytes u32, handle_type i32,
        // f_handle[]); a DFID_NAME record then has the NUL-terminated name.
        let mut i = at + meta_len;
        let end = at + event_len;
        let ops = entry_ops(mask);
        let mut entry: Option<EntryChanged> = None;
        while i + 4 <= end {
            let ty = buf[i];
            let len = u16::from_ne_bytes(buf[i + 2..i + 4].try_into().unwrap()) as usize;
            if len < 4 || i + len > end {
                break;
            }
            let rec_end = i + len;
            let dir_rec = matches!(ty, FAN_EVENT_INFO_TYPE_DFID | FAN_EVENT_INFO_TYPE_DFID_NAME);
            if (dir_rec || ty == FAN_EVENT_INFO_TYPE_FID)
                && len >= 4 + 8 + 8
                && let Some((handle_type, handle, after)) = handle_at(buf, i + 4 + 8, rec_end)
            {
                if dir_rec {
                    out.push(NsEvent::Dir(DirChanged {
                        handle_type,
                        handle: handle.clone(),
                        pid,
                        on_dir_itself: mask & FAN_ONDIR != 0 && mask & FAN_ATTRIB != 0,
                    }));
                }
                if let Some(&op) = ops.first()
                    && ty != FAN_EVENT_INFO_TYPE_DFID
                {
                    let e = entry.get_or_insert_with(|| EntryChanged {
                        op,
                        is_dir: mask & FAN_ONDIR != 0,
                        pid,
                        dir: None,
                        name: Vec::new(),
                        child: None,
                    });
                    if ty == FAN_EVENT_INFO_TYPE_DFID_NAME {
                        let name = &buf[after..rec_end];
                        let n = name.iter().position(|b| *b == 0).unwrap_or(name.len());
                        e.dir = Some((handle_type, handle));
                        e.name = name[..n].to_vec();
                    } else {
                        e.child = Some((handle_type, handle));
                    }
                }
            }
            i = rec_end;
        }
        // Names mode only (a DFID_NAME record): who made it.
        // One entry per op merged into the event.
        if let Some(e) = entry.filter(|e| e.dir.is_some()) {
            for op in ops {
                out.push(NsEvent::Entry(EntryChanged { op, ..e.clone() }));
            }
        }
        at += event_len;
    }
}

/// Watch the filesystem holding `root` until the receiver goes away.
pub async fn watch(root: &Path, tx: Sender<NsEvent>) -> Result<(), DetectError> {
    // The default (bounded) queue: under a flood (`tar x`, `chown -R`) the
    // kernel drops events and says so, and the daemon sweeps, instead of
    // queueing without limit (M7 review S4).
    let base = FAN_CLASS_NOTIF | FAN_CLOEXEC | FAN_NONBLOCK | FAN_REPORT_DIR_FID | FAN_REPORT_FID;
    let init = |flags: libc::c_uint| {
        // SAFETY: plain syscall; the result is checked.
        unsafe { libc::fanotify_init(flags, (libc::O_RDONLY | libc::O_CLOEXEC) as libc::c_uint) }
    };
    // Names and the child's handle, for who made a change. Without them (a
    // kernel before 5.17) only directories: every change is then `unknown`.
    let mut fd = init(base | FAN_REPORT_NAME | FAN_REPORT_TARGET_FID);
    let names = fd >= 0;
    if !names {
        fd = init(base);
    }
    if fd < 0 {
        return Err(err("init FID mode (needs CAP_SYS_ADMIN, Linux 5.9)"));
    }
    // SAFETY: fd is a new descriptor owned here.
    let fan = unsafe { OwnedFd::from_raw_fd(fd) };
    let p = std::ffi::CString::new(root.as_os_str().as_encoded_bytes()).map_err(|_| {
        DetectError::Fanotify {
            what: "root path",
            source: std::io::Error::from(std::io::ErrorKind::InvalidInput),
        }
    })?;
    // SAFETY: valid fd and NUL-terminated path.
    let r = unsafe {
        libc::fanotify_mark(
            fan.as_raw_fd(),
            FAN_MARK_ADD | FAN_MARK_FILESYSTEM,
            NS_MASK,
            libc::AT_FDCWD,
            p.as_ptr(),
        )
    };
    if r < 0 {
        return Err(err("mark namespace events"));
    }
    // SAFETY: `fan` owns its descriptor for as long as the AsyncFd lives, and
    // `as_raw_fd` always returns that one.
    let afd = unsafe { AsyncFd::register_with_interest(fan, Interest::READABLE) }.map_err(|e| {
        DetectError::Fanotify {
            what: "register with tokio",
            source: e.into_parts().1,
        }
    })?;
    tracing::info!(root = %root.display(), names, "namespace watcher armed");
    let mut buf = vec![0u8; 64 * 1024];
    let mut events = Vec::new();
    loop {
        let mut guard = afd.readable().await.map_err(|e| DetectError::Fanotify {
            what: "poll",
            source: e,
        })?;
        loop {
            // SAFETY: buf is a valid writable buffer of its length.
            let n = unsafe {
                libc::read(
                    guard.get_inner().as_raw_fd(),
                    buf.as_mut_ptr().cast(),
                    buf.len(),
                )
            };
            if n < 0 {
                let e = std::io::Error::last_os_error();
                if e.kind() == std::io::ErrorKind::WouldBlock {
                    guard.clear_ready();
                    break;
                }
                if e.kind() == std::io::ErrorKind::Interrupted {
                    continue;
                }
                return Err(DetectError::Fanotify {
                    what: "read",
                    source: e,
                });
            }
            events.clear();
            parse_events(&buf[..n as usize], &mut events);
            for ev in events.drain(..) {
                if tx.send(ev).await.is_err() {
                    return Ok(());
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn event(mask: u64, pid: i32, handle: &[u8]) -> Vec<u8> {
        let info_len = 4 + 8 + 8 + handle.len();
        let total = 24 + info_len;
        let mut b = Vec::new();
        b.extend_from_slice(&(total as u32).to_ne_bytes());
        b.extend_from_slice(&[3, 0]);
        b.extend_from_slice(&24u16.to_ne_bytes());
        b.extend_from_slice(&mask.to_ne_bytes());
        b.extend_from_slice(&(-1i32).to_ne_bytes());
        b.extend_from_slice(&pid.to_ne_bytes());
        b.extend_from_slice(&[FAN_EVENT_INFO_TYPE_DFID, 0]);
        b.extend_from_slice(&(info_len as u16).to_ne_bytes());
        b.extend_from_slice(&[0u8; 8]);
        b.extend_from_slice(&(handle.len() as u32).to_ne_bytes());
        b.extend_from_slice(&0x81i32.to_ne_bytes());
        b.extend_from_slice(handle);
        b
    }

    #[test]
    fn events_name_their_directory_by_handle() {
        let mut buf = event(FAN_CREATE, 42, &[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
        buf.extend(event(FAN_ATTRIB | FAN_ONDIR, 7, &[9; 12]));
        let mut q = 0x4000u64.to_ne_bytes().to_vec();
        let mut ov = Vec::new();
        ov.extend_from_slice(&24u32.to_ne_bytes());
        ov.extend_from_slice(&[3, 0]);
        ov.extend_from_slice(&24u16.to_ne_bytes());
        ov.append(&mut q);
        ov.extend_from_slice(&(-1i32).to_ne_bytes());
        ov.extend_from_slice(&0i32.to_ne_bytes());
        buf.extend(ov);
        let mut out = Vec::new();
        parse_events(&buf, &mut out);
        assert_eq!(out.len(), 3);
        assert_eq!(
            out[0],
            NsEvent::Dir(DirChanged {
                handle_type: 0x81,
                handle: (1..=12).collect(),
                pid: 42,
                on_dir_itself: false,
            })
        );
        assert!(matches!(&out[1], NsEvent::Dir(d) if d.on_dir_itself && d.pid == 7));
        assert_eq!(out[2], NsEvent::Overflow);
        // A truncated buffer yields what is whole, and never panics.
        let mut some = Vec::new();
        parse_events(&buf[..buf.len() - 5], &mut some);
        assert_eq!(some.len(), 2);
    }

    /// Names mode: a DFID_NAME record (the directory and the entry's name)
    /// and a FID record (the child, `FAN_REPORT_TARGET_FID`).
    fn named(mask: u64, pid: i32, dir: &[u8], name: &str, child: &[u8]) -> Vec<u8> {
        let mut nm = name.as_bytes().to_vec();
        nm.push(0);
        while !(4 + 8 + 8 + dir.len() + nm.len()).is_multiple_of(4) {
            nm.push(0);
        }
        let dlen = 4 + 8 + 8 + dir.len() + nm.len();
        let clen = 4 + 8 + 8 + child.len();
        let mut b = Vec::new();
        b.extend_from_slice(&((24 + dlen + clen) as u32).to_ne_bytes());
        b.extend_from_slice(&[3, 0]);
        b.extend_from_slice(&24u16.to_ne_bytes());
        b.extend_from_slice(&mask.to_ne_bytes());
        b.extend_from_slice(&(-1i32).to_ne_bytes());
        b.extend_from_slice(&pid.to_ne_bytes());
        for (ty, h, tail, len) in [
            (FAN_EVENT_INFO_TYPE_DFID_NAME, dir, nm.as_slice(), dlen),
            (FAN_EVENT_INFO_TYPE_FID, child, &[][..], clen),
        ] {
            b.extend_from_slice(&[ty, 0]);
            b.extend_from_slice(&(len as u16).to_ne_bytes());
            b.extend_from_slice(&[0u8; 8]);
            b.extend_from_slice(&(h.len() as u32).to_ne_bytes());
            b.extend_from_slice(&0x81i32.to_ne_bytes());
            b.extend_from_slice(h);
            b.extend_from_slice(tail);
        }
        b
    }

    #[test]
    fn names_mode_events_name_the_entry_and_its_handle() {
        let mut buf = named(FAN_DELETE, 42, &[1; 12], "line_1042.segy", &[2; 12]);
        buf.extend(named(FAN_MOVED_TO | FAN_ONDIR, 43, &[3; 12], "b", &[4; 12]));
        buf.extend(event(FAN_CREATE, 44, &[5; 12])); // a DFID-only record
        let mut out = Vec::new();
        parse_events(&buf, &mut out);
        let entries: Vec<&EntryChanged> = out
            .iter()
            .filter_map(|e| match e {
                NsEvent::Entry(x) => Some(x),
                _ => None,
            })
            .collect();
        let dirs = out.iter().filter(|e| matches!(e, NsEvent::Dir(_))).count();
        assert_eq!(dirs, 3, "every directory record still marks its directory");
        assert_eq!(entries.len(), 2, "{out:?}");
        assert_eq!(
            *entries[0],
            EntryChanged {
                op: EntryOp::Delete,
                is_dir: false,
                pid: 42,
                dir: Some((0x81, vec![1; 12])),
                name: b"line_1042.segy".to_vec(),
                child: Some((0x81, vec![2; 12])),
            }
        );
        assert_eq!(
            (entries[1].op, entries[1].is_dir, &entries[1].name[..]),
            (EntryOp::MovedTo, true, &b"b"[..])
        );
        // Unread events with the same records are merged by the kernel
        // (`mv f g; chown g; rm g`): one entry per op.
        let mut out = Vec::new();
        parse_events(
            &named(
                FAN_MOVED_TO | FAN_ATTRIB | FAN_DELETE,
                9,
                &[1; 12],
                "g",
                &[2; 12],
            ),
            &mut out,
        );
        let ops: Vec<EntryOp> = out
            .iter()
            .filter_map(|e| match e {
                NsEvent::Entry(x) => Some(x.op),
                _ => None,
            })
            .collect();
        assert_eq!(ops, [EntryOp::MovedTo, EntryOp::Attrib, EntryOp::Delete]);
    }
}
