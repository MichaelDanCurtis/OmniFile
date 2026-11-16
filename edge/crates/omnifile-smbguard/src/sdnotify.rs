//! The two halves of systemd's fd store, without libsystemd:
//! - `sd_notify(3)` with `SCM_RIGHTS` (`FDSTORE=1`, `READY=1`, `STATUS=`);
//! - `sd_listen_fds_with_names(3)`: `LISTEN_PID`, `LISTEN_FDS`, `LISTEN_FDNAMES`.

/// Descriptors systemd passed (fd number, name), when `LISTEN_PID` is us.
/// Passed descriptors start at 3 (`SD_LISTEN_FDS_START`).
pub fn parse_listen_fds(
    listen_pid: Option<&str>,
    listen_fds: Option<&str>,
    listen_fdnames: Option<&str>,
    my_pid: u32,
) -> Vec<(i32, String)> {
    let Some(pid) = listen_pid.and_then(|s| s.trim().parse::<u32>().ok()) else {
        return Vec::new();
    };
    if pid != my_pid {
        return Vec::new();
    }
    let Some(n) = listen_fds.and_then(|s| s.trim().parse::<i32>().ok()) else {
        return Vec::new();
    };
    let names: Vec<&str> = listen_fdnames.unwrap_or("").split(':').collect();
    (0..n.max(0))
        .map(|i| {
            let name = names.get(i as usize).copied().unwrap_or("unknown");
            (3 + i, name.to_string())
        })
        .collect()
}

/// `$FDSTORE`: how many descriptors this unit's store may hold (systemd ≥
/// 254 sets it only when `FileDescriptorStoreMax=` is above zero).
pub fn parse_fdstore_capacity(v: Option<&str>) -> u32 {
    v.and_then(|s| s.trim().parse().ok()).unwrap_or(0)
}

/// An `FDNAME=` value is at most 255 printable ASCII characters, no `:`.
pub fn fdname_is_valid(s: &str) -> bool {
    !s.is_empty() && s.len() <= 255 && s.bytes().all(|b| (0x21..0x7f).contains(&b) && b != b':')
}

#[cfg(target_os = "linux")]
pub use linux::{consume_listen_fds, notify};

#[cfg(target_os = "linux")]
mod linux {
    use std::io;
    use std::os::fd::RawFd;
    use std::os::unix::ffi::OsStrExt;

    /// Send `msg` (newline-separated assignments) to `$NOTIFY_SOCKET`, with
    /// `fds` attached. `Ok(false)` when not running under systemd.
    pub fn notify(msg: &str, fds: &[RawFd]) -> io::Result<bool> {
        let Some(path) = std::env::var_os("NOTIFY_SOCKET") else {
            return Ok(false);
        };
        let pb = path.as_bytes();
        // SAFETY: plain syscalls on stack-owned buffers; every length is
        // checked against the buffer it describes.
        unsafe {
            let mut addr: libc::sockaddr_un = std::mem::zeroed();
            if pb.is_empty() || pb.len() >= addr.sun_path.len() {
                return Err(io::Error::other("NOTIFY_SOCKET path too long"));
            }
            addr.sun_family = libc::AF_UNIX as libc::sa_family_t;
            for (i, b) in pb.iter().enumerate() {
                // A leading '@' names an abstract socket.
                addr.sun_path[i] = if i == 0 && *b == b'@' {
                    0
                } else {
                    *b as libc::c_char
                };
            }
            let alen = (std::mem::size_of::<libc::sa_family_t>() + pb.len()) as libc::socklen_t;
            let s = libc::socket(libc::AF_UNIX, libc::SOCK_DGRAM | libc::SOCK_CLOEXEC, 0);
            if s < 0 {
                return Err(io::Error::last_os_error());
            }
            let mut iov = libc::iovec {
                iov_base: msg.as_ptr() as *mut _,
                iov_len: msg.len(),
            };
            let mut mh: libc::msghdr = std::mem::zeroed();
            mh.msg_name = &mut addr as *mut _ as *mut _;
            mh.msg_namelen = alen;
            mh.msg_iov = &mut iov;
            mh.msg_iovlen = 1;
            let payload = std::mem::size_of_val(fds) as u32;
            let mut cbuf = vec![0u8; libc::CMSG_SPACE(payload) as usize];
            if !fds.is_empty() {
                mh.msg_control = cbuf.as_mut_ptr() as *mut _;
                mh.msg_controllen = cbuf.len() as _;
                let cm = libc::CMSG_FIRSTHDR(&mh);
                (*cm).cmsg_level = libc::SOL_SOCKET;
                (*cm).cmsg_type = libc::SCM_RIGHTS;
                (*cm).cmsg_len = libc::CMSG_LEN(payload) as _;
                std::ptr::copy_nonoverlapping(
                    fds.as_ptr() as *const u8,
                    libc::CMSG_DATA(cm),
                    payload as usize,
                );
            }
            let r = libc::sendmsg(s, &mh, libc::MSG_NOSIGNAL);
            let e = io::Error::last_os_error();
            libc::close(s);
            if r < 0 {
                return Err(e);
            }
        }
        Ok(true)
    }

    /// Take the descriptors systemd passed and clear `LISTEN_*` so children
    /// don't inherit them. Marks each descriptor close-on-exec.
    ///
    /// Call it before any other thread exists: it changes the environment.
    pub fn consume_listen_fds() -> Vec<(RawFd, String)> {
        let v = |k| std::env::var(k).ok();
        let fds = super::parse_listen_fds(
            v("LISTEN_PID").as_deref(),
            v("LISTEN_FDS").as_deref(),
            v("LISTEN_FDNAMES").as_deref(),
            std::process::id(),
        );
        for k in ["LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"] {
            // SAFETY: documented precondition: no other thread runs yet.
            unsafe { std::env::remove_var(k) };
        }
        for (fd, _) in &fds {
            // SAFETY: fcntl on a descriptor number we were given.
            unsafe { libc::fcntl(*fd, libc::F_SETFD, libc::FD_CLOEXEC) };
        }
        fds
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn listen_fds_for_us() {
        let v = parse_listen_fds(Some("42"), Some("2"), Some("omnifile-guard-ab:other"), 42);
        assert_eq!(
            v,
            vec![(3, "omnifile-guard-ab".into()), (4, "other".into())]
        );
    }

    #[test]
    fn listen_fds_for_someone_else_are_ignored() {
        assert!(parse_listen_fds(Some("41"), Some("1"), Some("x"), 42).is_empty());
        assert!(parse_listen_fds(None, Some("1"), None, 42).is_empty());
        assert!(parse_listen_fds(Some("42"), Some("bad"), None, 42).is_empty());
    }

    #[test]
    fn missing_names_are_unknown() {
        let v = parse_listen_fds(Some("1"), Some("2"), None, 1);
        assert_eq!(v[1], (4, "unknown".into()));
    }

    #[test]
    fn fdstore_capacity() {
        assert_eq!(parse_fdstore_capacity(Some("8")), 8);
        assert_eq!(parse_fdstore_capacity(None), 0);
    }

    #[test]
    fn fdnames() {
        assert!(fdname_is_valid("omnifile-guard-0123abcd"));
        assert!(!fdname_is_valid("a:b"));
        assert!(!fdname_is_valid(""));
        assert!(!fdname_is_valid("sp ace"));
    }
}
