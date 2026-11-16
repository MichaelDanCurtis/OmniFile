//! fanotify uapi (`include/uapi/linux/fanotify.h`, Linux 7.0) and parsing of
//! the event buffer. Pure code, so it is unit-tested on every platform.

/// `fanotify_init` flags.
pub const FAN_CLOEXEC: u32 = 0x0000_0001;
pub const FAN_NONBLOCK: u32 = 0x0000_0002;
pub const FAN_CLASS_NOTIF: u32 = 0x0000_0000;
pub const FAN_CLASS_PRE_CONTENT: u32 = 0x0000_0008;
pub const FAN_UNLIMITED_QUEUE: u32 = 0x0000_0010;
pub const FAN_REPORT_PIDFD: u32 = 0x0000_0080;

/// `fanotify_mark` flags.
pub const FAN_MARK_ADD: u32 = 0x0000_0001;
pub const FAN_MARK_REMOVE: u32 = 0x0000_0002;
pub const FAN_MARK_MOUNT: u32 = 0x0000_0010;
pub const FAN_MARK_IGNORED_SURV_MODIFY: u32 = 0x0000_0040;
pub const FAN_MARK_FILESYSTEM: u32 = 0x0000_0100;
pub const FAN_MARK_EVICTABLE: u32 = 0x0000_0200;
pub const FAN_MARK_IGNORE: u32 = 0x0000_0400;

/// Event masks.
pub const FAN_MODIFY: u64 = 0x0000_0002;
pub const FAN_CLOSE_WRITE: u64 = 0x0000_0008;
pub const FAN_Q_OVERFLOW: u64 = 0x0000_4000;
pub const FAN_OPEN_PERM: u64 = 0x0001_0000;
pub const FAN_PRE_ACCESS: u64 = 0x0010_0000;
/// Also report events on directories (with `FAN_OPEN_PERM`: opendir).
pub const FAN_ONDIR: u64 = 0x4000_0000;

/// Responses.
pub const FAN_ALLOW: u32 = 0x01;
pub const FAN_DENY: u32 = 0x02;

pub const FAN_NOFD: i32 = -1;
/// `pidfd` values that are not descriptors.
pub const FAN_NOPIDFD: i32 = -1;
pub const FAN_EPIDFD: i32 = -2;

pub const FAN_EVENT_INFO_TYPE_PIDFD: u8 = 4;
pub const FAN_EVENT_INFO_TYPE_RANGE: u8 = 6;
pub const FANOTIFY_METADATA_VERSION: u8 = 3;
/// `sizeof(struct fanotify_event_metadata)`.
pub const META_LEN: usize = 24;

/// `FAN_DENY` with a custom errno. The kernel accepts only `EIO, EPERM,
/// EBUSY, ETXTBSY, EAGAIN, ENOSPC, EDQUOT` (`process_access_response()`).
pub const fn deny_errno(errno: i32) -> u32 {
    FAN_DENY | (((errno as u32) & 0xff) << 24)
}

/// What a pre-content event asks for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Access {
    /// `[off, off + len)` with `len > 0`: a read, write, `mmap()` or
    /// `copy_file_range`/`sendfile`/`splice` source range (page aligned).
    Range { off: u64, len: u64 },
    /// `(0, 0)`: a truncate to 0 **or** a whole-file reflink clone
    /// (`remap_verify_area()` passes `len = 0`). Indistinguishable, so it is
    /// always treated as a whole-file access (spike A10).
    WholeFile,
    /// `(L > 0, 0)`: `truncate(2)`/`ftruncate(2)` to length `L` (rounded
    /// down to a page).
    Truncate { to: u64 },
    /// No range record (not expected for `FAN_PRE_ACCESS`): whole file.
    Unknown,
    /// `FAN_OPEN_PERM` on a directory: smbd opening a rewind directory to
    /// list it (fsal-namespace.md section 7). Only unfilled rewind
    /// directories carry this mark.
    DirOpen,
}

/// How [`Access::DirOpen`] is journalled: no range event has this shape
/// (`len == u64::MAX` overflows any offset).
const DIR_OPEN_RAW: (u64, u64) = (u64::MAX, u64::MAX);

impl Access {
    pub fn from_range(r: Option<(u64, u64)>) -> Access {
        match r {
            None => Access::Unknown,
            Some(DIR_OPEN_RAW) => Access::DirOpen,
            Some((0, 0)) => Access::WholeFile,
            Some((off, 0)) => Access::Truncate { to: off },
            Some((off, len)) => Access::Range { off, len },
        }
    }

    /// `(off, len)` as journalled (`len == 0` for the truncate forms).
    pub fn raw(self) -> (u64, u64) {
        match self {
            Access::Range { off, len } => (off, len),
            Access::WholeFile | Access::Unknown => (0, 0),
            Access::Truncate { to } => (to, 0),
            Access::DirOpen => DIR_OPEN_RAW,
        }
    }
}

/// One event from the group, before anything is done with it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RawEvent {
    pub mask: u64,
    /// The event's file descriptor (the response key), or `FAN_NOFD`.
    pub fd: i32,
    /// tgid of the process that caused the event.
    pub pid: i32,
    /// pidfd from a `FAN_REPORT_PIDFD` record, if it is a descriptor.
    pub pidfd: Option<i32>,
    /// `(offset, count)` from a `FAN_EVENT_INFO_TYPE_RANGE` record.
    pub range: Option<(u64, u64)>,
}

impl RawEvent {
    pub fn access(&self) -> Access {
        if self.mask & FAN_OPEN_PERM != 0 {
            return Access::DirOpen;
        }
        Access::from_range(self.range)
    }
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ParseError {
    #[error("event metadata version {0}, expected {FANOTIFY_METADATA_VERSION}")]
    Version(u8),
    #[error("malformed event at byte {0}")]
    Malformed(usize),
}

fn rd32(b: &[u8], o: usize) -> u32 {
    u32::from_ne_bytes(b[o..o + 4].try_into().unwrap())
}

fn rd64(b: &[u8], o: usize) -> u64 {
    u64::from_ne_bytes(b[o..o + 8].try_into().unwrap())
}

/// Parse a buffer returned by `read(2)` on a fanotify group.
///
/// Events parsed before a malformed one are returned alongside the error,
/// because their descriptors are already open and must be answered: the
/// caller never drops an event it has read.
pub fn parse(buf: &[u8]) -> (Vec<RawEvent>, Option<ParseError>) {
    let mut out = Vec::new();
    let mut p = 0usize;
    while p + META_LEN <= buf.len() {
        let event_len = rd32(buf, p) as usize;
        let vers = buf[p + 4];
        let meta_len = u16::from_ne_bytes([buf[p + 6], buf[p + 7]]) as usize;
        if vers != FANOTIFY_METADATA_VERSION {
            return (out, Some(ParseError::Version(vers)));
        }
        if event_len < META_LEN || meta_len < META_LEN || p + event_len > buf.len() {
            return (out, Some(ParseError::Malformed(p)));
        }
        let mut ev = RawEvent {
            mask: rd64(buf, p + 8),
            fd: rd32(buf, p + 16) as i32,
            pid: rd32(buf, p + 20) as i32,
            pidfd: None,
            range: None,
        };
        let mut q = p + meta_len;
        while q + 4 <= p + event_len {
            let info_type = buf[q];
            let info_len = u16::from_ne_bytes([buf[q + 2], buf[q + 3]]) as usize;
            if info_len < 4 || q + info_len > p + event_len {
                out.push(ev);
                return (out, Some(ParseError::Malformed(q)));
            }
            match info_type {
                FAN_EVENT_INFO_TYPE_RANGE if info_len >= 24 => {
                    ev.range = Some((rd64(buf, q + 8), rd64(buf, q + 16)));
                }
                FAN_EVENT_INFO_TYPE_PIDFD if info_len >= 8 => {
                    let fd = rd32(buf, q + 4) as i32;
                    if fd >= 0 {
                        ev.pidfd = Some(fd);
                    }
                }
                _ => {}
            }
            q += info_len;
        }
        out.push(ev);
        p += event_len;
    }
    (out, None)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    #[test]
    fn a_directory_open_survives_the_journal() {
        let ev = RawEvent {
            mask: FAN_OPEN_PERM | FAN_ONDIR,
            fd: 5,
            pid: 1,
            pidfd: None,
            range: None,
        };
        assert_eq!(ev.access(), Access::DirOpen);
        assert_eq!(
            Access::from_range(Some(Access::DirOpen.raw())),
            Access::DirOpen
        );
        // Range events never decode as one.
        assert_eq!(
            Access::from_range(Some((0, 4096))),
            Access::Range { off: 0, len: 4096 }
        );
    }

    /// Build one event the way the kernel lays it out.
    pub(crate) fn event(
        mask: u64,
        fd: i32,
        pid: i32,
        range: Option<(u64, u64)>,
        pidfd: Option<i32>,
    ) -> Vec<u8> {
        let mut info = Vec::new();
        if let Some(p) = pidfd {
            info.push(FAN_EVENT_INFO_TYPE_PIDFD);
            info.push(0);
            info.extend_from_slice(&8u16.to_ne_bytes());
            info.extend_from_slice(&p.to_ne_bytes());
        }
        if let Some((o, c)) = range {
            info.push(FAN_EVENT_INFO_TYPE_RANGE);
            info.push(0);
            info.extend_from_slice(&24u16.to_ne_bytes());
            info.extend_from_slice(&0u32.to_ne_bytes());
            info.extend_from_slice(&o.to_ne_bytes());
            info.extend_from_slice(&c.to_ne_bytes());
        }
        let len = (META_LEN + info.len()) as u32;
        let mut b = Vec::new();
        b.extend_from_slice(&len.to_ne_bytes());
        b.push(FANOTIFY_METADATA_VERSION);
        b.push(0);
        b.extend_from_slice(&(META_LEN as u16).to_ne_bytes());
        b.extend_from_slice(&mask.to_ne_bytes());
        b.extend_from_slice(&fd.to_ne_bytes());
        b.extend_from_slice(&pid.to_ne_bytes());
        b.extend_from_slice(&info);
        b
    }

    #[test]
    fn parses_a_batch_with_ranges_and_pidfds() {
        let mut buf = event(FAN_PRE_ACCESS, 7, 100, Some((4096, 8192)), Some(9));
        buf.extend(event(
            FAN_PRE_ACCESS,
            8,
            101,
            Some((0, 0)),
            Some(FAN_EPIDFD),
        ));
        buf.extend(event(FAN_PRE_ACCESS, 10, 102, Some((1 << 20, 0)), None));
        let (evs, err) = parse(&buf);
        assert!(err.is_none());
        assert_eq!(evs.len(), 3);
        assert_eq!(evs[0].fd, 7);
        assert_eq!(evs[0].pidfd, Some(9));
        assert_eq!(
            evs[0].access(),
            Access::Range {
                off: 4096,
                len: 8192
            }
        );
        assert_eq!(evs[1].pidfd, None, "FAN_EPIDFD is not a descriptor");
        assert_eq!(evs[1].access(), Access::WholeFile);
        assert_eq!(evs[2].access(), Access::Truncate { to: 1 << 20 });
    }

    #[test]
    fn overflow_event_has_no_fd() {
        let buf = event(FAN_Q_OVERFLOW, FAN_NOFD, 0, None, None);
        let (evs, err) = parse(&buf);
        assert!(err.is_none());
        assert_eq!(evs[0].fd, FAN_NOFD);
        assert_eq!(evs[0].access(), Access::Unknown);
    }

    #[test]
    fn malformed_keeps_earlier_events() {
        let mut buf = event(FAN_PRE_ACCESS, 7, 1, Some((0, 4096)), None);
        let mut bad = event(FAN_PRE_ACCESS, 8, 1, None, None);
        bad[0..4].copy_from_slice(&1000u32.to_ne_bytes());
        buf.extend(bad);
        let (evs, err) = parse(&buf);
        assert_eq!(evs.len(), 1);
        assert_eq!(evs[0].fd, 7);
        assert!(matches!(err, Some(ParseError::Malformed(_))));
    }

    #[test]
    fn wrong_version_is_reported() {
        let mut buf = event(FAN_PRE_ACCESS, 7, 1, Some((0, 4096)), None);
        buf[4] = 2;
        let (evs, err) = parse(&buf);
        assert!(evs.is_empty());
        assert_eq!(err, Some(ParseError::Version(2)));
    }

    #[test]
    fn deny_errno_encoding() {
        assert_eq!(deny_errno(5), 0x0500_0002);
    }
}
