//! `AF_UNIX` / `SOCK_SEQPACKET` transport on Linux, driven by tokio's
//! `AsyncFd`. tokio has no SEQPACKET type, so this is a thin libc wrapper.

use std::io;
use std::mem::{size_of, zeroed};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

use tokio::io::Interest;
use tokio::io::unix::AsyncFd;

use crate::server::{Handler, PacketIo, Server};
use crate::wire::MAX_MESSAGE;

fn cvt(r: libc::c_int) -> io::Result<libc::c_int> {
    if r < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(r)
    }
}

fn sockaddr_un(path: &Path) -> io::Result<(libc::sockaddr_un, libc::socklen_t)> {
    // SAFETY: sockaddr_un is plain data; all-zero is valid.
    let mut addr: libc::sockaddr_un = unsafe { zeroed() };
    addr.sun_family = libc::AF_UNIX as libc::sa_family_t;
    let bytes = path.as_os_str().as_bytes();
    if bytes.len() >= addr.sun_path.len() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "socket path too long",
        ));
    }
    for (d, s) in addr.sun_path.iter_mut().zip(bytes) {
        *d = *s as libc::c_char;
    }
    let len = (size_of::<libc::sa_family_t>() + bytes.len() + 1) as libc::socklen_t;
    Ok((addr, len))
}

fn new_socket() -> io::Result<OwnedFd> {
    // SAFETY: plain syscall.
    let fd = cvt(unsafe {
        libc::socket(
            libc::AF_UNIX,
            libc::SOCK_SEQPACKET | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC,
            0,
        )
    })?;
    // SAFETY: fd was just created and is owned here.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

/// A connected SEQPACKET socket.
pub struct SeqPacket {
    fd: AsyncFd<OwnedFd>,
}

impl SeqPacket {
    pub fn from_owned(fd: OwnedFd) -> io::Result<Self> {
        // SAFETY: the OwnedFd is owned by the AsyncFd for its whole life, and
        // `as_raw_fd` always returns that descriptor.
        let fd =
            unsafe { AsyncFd::register_with_interest(fd, Interest::READABLE | Interest::WRITABLE) }
                .map_err(|e| e.into_parts().1)?;
        Ok(SeqPacket { fd })
    }

    /// Connect to a listening socket (used by tests and tools, which play the FSAL).
    pub async fn connect(path: &Path) -> io::Result<Self> {
        let fd = new_socket()?;
        let (addr, len) = sockaddr_un(path)?;
        // SAFETY: addr is a valid sockaddr_un of length len.
        let r = unsafe {
            libc::connect(
                fd.as_raw_fd(),
                (&addr as *const libc::sockaddr_un).cast(),
                len,
            )
        };
        // A unix-socket connect completes at once or fails (EAGAIN: backlog full).
        cvt(r)?;
        let s = SeqPacket::from_owned(fd)?;
        Ok(s)
    }

    /// `SO_PEERCRED`: (pid, uid, gid).
    pub fn peer_cred(&self) -> io::Result<(i32, u32, u32)> {
        peer_cred(self.fd.get_ref().as_raw_fd())
    }

    /// Close both directions now, even while other holders of this socket
    /// (a reader task) still have it: the peer sees the connection end, as
    /// when a Ganesha stops (used by tests and tools that play the FSAL).
    pub fn shutdown(&self) -> io::Result<()> {
        // SAFETY: plain syscall on the descriptor this socket owns.
        cvt(unsafe { libc::shutdown(self.fd.get_ref().as_raw_fd(), libc::SHUT_RDWR) }).map(|_| ())
    }
}

/// `SO_PEERCRED` of a connected unix socket.
pub fn peer_cred(fd: RawFd) -> io::Result<(i32, u32, u32)> {
    // SAFETY: ucred is plain data.
    let mut cred: libc::ucred = unsafe { zeroed() };
    let mut len = size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: cred and len are valid out-parameters.
    cvt(unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut cred as *mut libc::ucred).cast(),
            &mut len,
        )
    })?;
    Ok((cred.pid, cred.uid, cred.gid))
}

impl PacketIo for SeqPacket {
    async fn recv(&self) -> io::Result<Option<Vec<u8>>> {
        let mut buf = vec![0u8; MAX_MESSAGE + 1];
        loop {
            let mut guard = self.fd.readable().await?;
            let r = guard.try_io(|inner| {
                // MSG_TRUNC makes recv return the real record length, so an
                // oversized record is detected rather than silently cut.
                // SAFETY: buf is valid for its length.
                let n = unsafe {
                    libc::recv(
                        inner.as_raw_fd(),
                        buf.as_mut_ptr().cast(),
                        buf.len(),
                        libc::MSG_TRUNC,
                    )
                };
                if n < 0 {
                    Err(io::Error::last_os_error())
                } else {
                    Ok(n as usize)
                }
            });
            match r {
                Ok(Ok(0)) => return Ok(None),
                Ok(Ok(n)) if n > MAX_MESSAGE => {
                    // Hand the decoder an over-long record so it reports it.
                    buf.truncate(MAX_MESSAGE + 1);
                    return Ok(Some(buf));
                }
                Ok(Ok(n)) => {
                    buf.truncate(n);
                    return Ok(Some(buf));
                }
                Ok(Err(e)) if e.raw_os_error() == Some(libc::ECONNRESET) => return Ok(None),
                Ok(Err(e)) => return Err(e),
                Err(_would_block) => continue,
            }
        }
    }

    async fn send(&self, rec: &[u8]) -> io::Result<()> {
        loop {
            let mut guard = self.fd.writable().await?;
            let r = guard.try_io(|inner| {
                // SAFETY: rec is valid for its length.
                let n = unsafe {
                    libc::send(
                        inner.as_raw_fd(),
                        rec.as_ptr().cast(),
                        rec.len(),
                        libc::MSG_NOSIGNAL,
                    )
                };
                if n < 0 {
                    Err(io::Error::last_os_error())
                } else {
                    Ok(n as usize)
                }
            });
            match r {
                Ok(Ok(n)) if n == rec.len() => return Ok(()),
                Ok(Ok(n)) => {
                    return Err(io::Error::other(format!(
                        "short seqpacket send {n}/{}",
                        rec.len()
                    )));
                }
                Ok(Err(e)) => return Err(e),
                Err(_would_block) => continue,
            }
        }
    }
}

/// A listening SEQPACKET socket at a filesystem path, mode 0600.
pub struct Listener {
    fd: AsyncFd<OwnedFd>,
    path: PathBuf,
}

impl Listener {
    /// Bind, replacing a stale socket file. The parent directory must exist.
    pub fn bind(path: &Path) -> io::Result<Self> {
        match std::fs::symlink_metadata(path) {
            Ok(m) => {
                use std::os::unix::fs::FileTypeExt;
                if !m.file_type().is_socket() {
                    return Err(io::Error::new(
                        io::ErrorKind::AlreadyExists,
                        format!("{} exists and is not a socket", path.display()),
                    ));
                }
                std::fs::remove_file(path)?;
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return Err(e),
        }
        let fd = new_socket()?;
        let (addr, len) = sockaddr_un(path)?;
        // Create the socket file 0600 from the start, without touching the
        // process-wide umask (N2): Linux gives the bound file the socket
        // inode's mode masked by the umask, so set that mode first. The
        // file can only end up stricter than 0600, never looser.
        // SAFETY: plain syscall on a valid fd.
        cvt(unsafe { libc::fchmod(fd.as_raw_fd(), 0o600) })?;
        // SAFETY: addr is a valid sockaddr_un.
        cvt(unsafe {
            libc::bind(
                fd.as_raw_fd(),
                (&addr as *const libc::sockaddr_un).cast(),
                len,
            )
        })?;
        // SAFETY: plain syscall.
        cvt(unsafe { libc::listen(fd.as_raw_fd(), 16) })?;
        // SAFETY: as in `SeqPacket::from_owned`.
        let fd = unsafe { AsyncFd::register_with_interest(fd, Interest::READABLE) }
            .map_err(|e| e.into_parts().1)?;
        Ok(Listener {
            fd,
            path: path.to_path_buf(),
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub async fn accept(&self) -> io::Result<SeqPacket> {
        loop {
            let mut guard = self.fd.readable().await?;
            let r = guard.try_io(|inner| {
                // SAFETY: null address out-params are allowed.
                let fd = unsafe {
                    libc::accept4(
                        inner.as_raw_fd(),
                        std::ptr::null_mut(),
                        std::ptr::null_mut(),
                        libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC,
                    )
                };
                if fd < 0 {
                    Err(io::Error::last_os_error())
                } else {
                    // SAFETY: accept4 returned a new owned fd.
                    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
                }
            });
            match r {
                Ok(Ok(fd)) => return SeqPacket::from_owned(fd),
                Ok(Err(e)) => return Err(e),
                Err(_would_block) => continue,
            }
        }
    }
}

impl Drop for Listener {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

/// The executable of a peer process, for the `ganesha.nfsd` check.
pub fn peer_exe(pid: i32) -> Option<String> {
    std::fs::read_link(format!("/proc/{pid}/exe"))
        .ok()
        .map(|p| p.to_string_lossy().into_owned())
}

/// Accept connections until the server shuts down.
pub async fn run<H: Handler>(listener: Listener, server: Server<H>) -> io::Result<()> {
    loop {
        if server.is_shutting_down() {
            return Ok(());
        }
        let conn = tokio::select! {
            c = listener.accept() => c,
            _ = tokio::time::sleep(std::time::Duration::from_millis(250)) => continue,
        };
        let conn = match conn {
            Ok(c) => c,
            Err(e) => {
                // EMFILE and friends: back off, never exit.
                tracing::warn!(err = %e, "fsal socket accept failed");
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
                continue;
            }
        };
        let (pid, uid) = match conn.peer_cred() {
            Ok((pid, uid, _)) => (pid, uid),
            Err(e) => {
                tracing::warn!(err = %e, "SO_PEERCRED failed; dropping connection");
                continue;
            }
        };
        let exe = peer_exe(pid);
        let server = server.clone();
        tokio::spawn(async move { server.serve(conn, pid, uid, exe).await });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    /// N2: the socket file is 0600 by its own mode, not by a process-wide
    /// umask change, so it is 0600 even under umask 0, and bind leaves the
    /// umask alone.
    #[tokio::test]
    async fn bound_socket_is_0600_without_touching_umask() {
        let dir = std::env::temp_dir().join(format!("omni-sock-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir(&dir).unwrap();
        let path = dir.join("fsal.sock");
        // SAFETY: plain syscalls; this test is the only one in the crate
        // that creates files.
        let before = unsafe { libc::umask(0) };
        let l = Listener::bind(&path);
        // SAFETY: as above.
        let during = unsafe { libc::umask(before) };
        let l = l.unwrap();
        assert_eq!(during, 0, "bind changed the umask");
        let mode = std::fs::symlink_metadata(&path)
            .unwrap()
            .permissions()
            .mode();
        assert_eq!(mode & 0o777, 0o600, "mode {mode:o}");
        drop(l);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
