//! Raw syscalls the guard needs (through `libc::syscall` where glibc and
//! musl differ, so both build).

use std::ffi::{CStr, CString};
use std::io;
use std::os::fd::{AsRawFd, BorrowedFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

use crate::journal::Handle;

fn cvt(r: libc::c_long) -> io::Result<libc::c_long> {
    if r < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(r)
    }
}

pub fn cpath(p: &Path) -> io::Result<CString> {
    CString::new(p.as_os_str().as_bytes())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "path contains NUL"))
}

pub fn errno_of(e: &io::Error) -> i32 {
    e.raw_os_error().unwrap_or(0)
}

pub fn fanotify_init(flags: u32, event_f_flags: u32) -> io::Result<OwnedFd> {
    // SAFETY: plain syscall; a non-negative result is a new descriptor we own.
    let fd = cvt(unsafe {
        libc::syscall(
            libc::SYS_fanotify_init,
            flags as libc::c_uint,
            event_f_flags as libc::c_uint,
        )
    })?;
    // SAFETY: fd was just returned by the kernel.
    Ok(unsafe { OwnedFd::from_raw_fd(fd as RawFd) })
}

/// `fanotify_mark(group, flags, mask, dirfd, path)`. With `path = None` the
/// object `dirfd` refers to is marked (no path lookup, no TOCTOU).
pub fn fanotify_mark(
    group: RawFd,
    flags: u32,
    mask: u64,
    dirfd: RawFd,
    path: Option<&CStr>,
) -> io::Result<()> {
    let p = path.map(|p| p.as_ptr()).unwrap_or(std::ptr::null());
    // SAFETY: p is NULL or a NUL-terminated string that outlives the call.
    cvt(unsafe {
        libc::syscall(
            libc::SYS_fanotify_mark,
            group,
            flags as libc::c_uint,
            mask,
            dirfd,
            p,
        )
    })?;
    Ok(())
}

/// Write one `struct fanotify_response`.
pub fn respond(group: RawFd, fd: i32, response: u32) -> io::Result<()> {
    let buf: [u32; 2] = [fd as u32, response];
    // SAFETY: buf is 8 valid bytes.
    let r = unsafe { libc::write(group, buf.as_ptr() as *const _, 8) };
    if r < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

const MAX_HANDLE_SZ: usize = 128;

#[repr(C)]
struct FileHandle {
    handle_bytes: u32,
    handle_type: i32,
    f_handle: [u8; MAX_HANDLE_SZ],
}

/// `name_to_handle_at(fd, "", AT_EMPTY_PATH)`. Pre-content groups cannot
/// report handles themselves, so the journal takes one per event.
pub fn handle_of(fd: BorrowedFd<'_>) -> io::Result<Handle> {
    let mut fh = FileHandle {
        handle_bytes: MAX_HANDLE_SZ as u32,
        handle_type: 0,
        f_handle: [0; MAX_HANDLE_SZ],
    };
    let mut mnt: libc::c_int = 0;
    // SAFETY: fh and mnt are valid out-parameters; "" is NUL-terminated.
    cvt(unsafe {
        libc::syscall(
            libc::SYS_name_to_handle_at,
            fd.as_raw_fd(),
            c"".as_ptr(),
            &mut fh as *mut FileHandle,
            &mut mnt as *mut libc::c_int,
            libc::AT_EMPTY_PATH,
        )
    })?;
    let n = (fh.handle_bytes as usize).min(MAX_HANDLE_SZ);
    Ok(Handle {
        handle_type: fh.handle_type,
        bytes: fh.f_handle[..n].to_vec(),
    })
}

pub fn open_by_handle(mount_fd: RawFd, h: &Handle, flags: libc::c_int) -> io::Result<OwnedFd> {
    if h.bytes.len() > MAX_HANDLE_SZ {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "handle too long",
        ));
    }
    let mut fh = FileHandle {
        handle_bytes: h.bytes.len() as u32,
        handle_type: h.handle_type,
        f_handle: [0; MAX_HANDLE_SZ],
    };
    fh.f_handle[..h.bytes.len()].copy_from_slice(&h.bytes);
    // SAFETY: fh is a valid file_handle of handle_bytes bytes.
    let fd = cvt(unsafe {
        libc::syscall(
            libc::SYS_open_by_handle_at,
            mount_fd,
            &mut fh as *mut FileHandle,
            flags | libc::O_CLOEXEC,
        )
    })?;
    // SAFETY: a new descriptor from the kernel.
    Ok(unsafe { OwnedFd::from_raw_fd(fd as RawFd) })
}

/// The xfs handle of an inode from `(ino, generation)`: type
/// `FILEID_INO32_GEN | XFS_FILEID_TYPE_64FLAG` (0x81), a 64-bit inode
/// number then the 32-bit generation, native endian (`xfs_fid64`).
/// `xfs_fs_fh_to_dentry()` accepts it on every xfs, so the guard can reach an
/// inode the stub handshake names by key alone.
pub fn xfs_handle(ino: u64, ino_gen: u32) -> Handle {
    let mut bytes = ino.to_ne_bytes().to_vec();
    bytes.extend_from_slice(&ino_gen.to_ne_bytes());
    Handle {
        handle_type: 0x81,
        bytes,
    }
}

pub fn fstat(fd: BorrowedFd<'_>) -> io::Result<libc::stat> {
    omnifile_stub::sys::fstat(fd)
}

pub fn fstatfs_type(fd: BorrowedFd<'_>) -> io::Result<i64> {
    Ok(omnifile_stub::sys::fstatfs(fd)?.f_type as i64)
}

pub const XFS_SUPER_MAGIC: i64 = 0x5846_5342;

/// `FS_IOC_GETFSUUID` (`_IOR(0x15, 0, struct fsuuid2)`, Linux ≥ 6.5).
pub fn fs_uuid(fd: BorrowedFd<'_>) -> io::Result<[u8; 16]> {
    #[repr(C)]
    struct Fsuuid2 {
        len: u8,
        uuid: [u8; 16],
    }
    const FS_IOC_GETFSUUID: libc::c_ulong = 0x8011_1500;
    let mut u = Fsuuid2 {
        len: 0,
        uuid: [0; 16],
    };
    // SAFETY: u is a valid out-parameter of the ioctl's size (17 bytes).
    let r = unsafe {
        libc::ioctl(
            fd.as_raw_fd(),
            FS_IOC_GETFSUUID as _,
            &mut u as *mut Fsuuid2,
        )
    };
    if r < 0 {
        return Err(io::Error::last_os_error());
    }
    if u.len != 16 {
        return Err(io::Error::other(format!("fs uuid length {}", u.len)));
    }
    Ok(u.uuid)
}

/// glibc's `major()`/`minor()` of a `dev_t`.
pub fn dev_major_minor(dev: u64) -> (u32, u32) {
    let major = ((dev >> 8) & 0xfff) | ((dev >> 32) & !0xfff);
    let minor = (dev & 0xff) | ((dev >> 12) & !0xff);
    (major as u32, minor as u32)
}

pub fn fdinfo(fd: RawFd) -> io::Result<String> {
    std::fs::read_to_string(format!("/proc/self/fdinfo/{fd}"))
}

pub fn fd_path(fd: RawFd) -> Option<PathBuf> {
    std::fs::read_link(format!("/proc/self/fd/{fd}")).ok()
}

pub fn fd_is_open(fd: RawFd) -> bool {
    // SAFETY: F_GETFD on any number is harmless.
    unsafe { libc::fcntl(fd, libc::F_GETFD) >= 0 }
}

/// Occupy descriptor number `n` with `/dev/null` (so a new event cannot get
/// the number of an orphan we still have to answer).
pub fn reserve_fd_number(n: RawFd) -> io::Result<()> {
    // SAFETY: open/dup2/close on descriptors we own.
    unsafe {
        let t = libc::open(c"/dev/null".as_ptr(), libc::O_RDONLY | libc::O_CLOEXEC);
        if t < 0 {
            return Err(io::Error::last_os_error());
        }
        if t == n {
            return Ok(());
        }
        let r = libc::dup3(t, n, libc::O_CLOEXEC);
        let e = io::Error::last_os_error();
        libc::close(t);
        if r < 0 {
            return Err(e);
        }
    }
    Ok(())
}

pub fn close_raw(fd: RawFd) {
    // SAFETY: the caller owns fd and never uses it again.
    unsafe { libc::close(fd) };
}

/// Raise `RLIMIT_NOFILE` to its hard limit; returns the new soft limit.
#[allow(clippy::unnecessary_cast)] // rlim_t differs between libcs
pub fn raise_nofile() -> u64 {
    let mut rl = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    // SAFETY: rl is a valid out/in parameter.
    unsafe {
        libc::getrlimit(libc::RLIMIT_NOFILE, &mut rl);
        rl.rlim_cur = rl.rlim_max;
        libc::setrlimit(libc::RLIMIT_NOFILE, &rl);
        libc::getrlimit(libc::RLIMIT_NOFILE, &mut rl);
    }
    rl.rlim_cur as u64
}

/// Wait up to `ms` for `fd` to be readable.
pub fn poll_in(fd: RawFd, ms: i32) -> io::Result<bool> {
    let mut p = libc::pollfd {
        fd,
        events: libc::POLLIN,
        revents: 0,
    };
    // SAFETY: one valid pollfd.
    let r = unsafe { libc::poll(&mut p, 1, ms) };
    if r < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(r > 0)
}

pub fn read_into(fd: RawFd, buf: &mut [u8]) -> io::Result<usize> {
    // SAFETY: buf is valid for its length.
    let n = unsafe { libc::read(fd, buf.as_mut_ptr() as *mut _, buf.len()) };
    if n < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(n as usize)
    }
}

pub fn ftruncate(fd: BorrowedFd<'_>, len: u64) -> io::Result<()> {
    // SAFETY: plain syscall on a descriptor we hold.
    if unsafe { libc::ftruncate(fd.as_raw_fd(), len as libc::off_t) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

pub fn open_dir(p: &Path) -> io::Result<OwnedFd> {
    let c = cpath(p)?;
    // SAFETY: c is NUL-terminated.
    let fd = unsafe {
        libc::open(
            c.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: new descriptor.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

/// Inode number of a process's mount namespace.
pub fn mount_ns(pid: &str) -> io::Result<u64> {
    use std::os::unix::fs::MetadataExt;
    Ok(std::fs::metadata(format!("/proc/{pid}/ns/mnt"))?.ino())
}

/// `unshare(CLONE_NEWNS)`, make everything a slave of the host (so later
/// host mounts still propagate in), and bind `src` at `dst`, which no other
/// process can see. Must run while the process is single-threaded.
pub fn unshare_and_bind(src: &Path, dst: &Path) -> io::Result<()> {
    std::fs::create_dir_all(dst)?;
    let (s, d) = (cpath(src)?, cpath(dst)?);
    // SAFETY: plain syscalls with NUL-terminated paths.
    unsafe {
        if libc::unshare(libc::CLONE_NEWNS) < 0 {
            return Err(io::Error::last_os_error());
        }
        if libc::mount(
            std::ptr::null(),
            c"/".as_ptr(),
            std::ptr::null(),
            libc::MS_REC | libc::MS_SLAVE,
            std::ptr::null(),
        ) < 0
        {
            return Err(io::Error::last_os_error());
        }
        if libc::mount(
            s.as_ptr(),
            d.as_ptr(),
            std::ptr::null(),
            libc::MS_BIND,
            std::ptr::null(),
        ) < 0
        {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

/// Random hex id for a new group (names its fd-store entry and journal).
pub fn random_id() -> String {
    let mut b = [0u8; 8];
    // SAFETY: b is valid for 8 bytes.
    let n = unsafe { libc::getrandom(b.as_mut_ptr() as *mut _, 8, 0) };
    if n != 8 {
        let t = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0);
        b = (t ^ ((std::process::id() as u64) << 32)).to_ne_bytes();
    }
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// Processes whose `comm` is one of `names` and that hold a file on the
/// device `dev` open: those whose open files a new group cannot guard (A9).
pub fn servers_with_open_files(names: &[String], dev: u64) -> Vec<(i32, String)> {
    use std::os::unix::fs::MetadataExt;
    let mut out = Vec::new();
    let Ok(rd) = std::fs::read_dir("/proc") else {
        return out;
    };
    for e in rd.flatten() {
        let Some(pid) = e.file_name().to_str().and_then(|s| s.parse::<i32>().ok()) else {
            continue;
        };
        let Ok(c) = std::fs::read_to_string(format!("/proc/{pid}/comm")) else {
            continue;
        };
        let c = c.trim();
        if !names.iter().any(|n| n == c) {
            continue;
        }
        let Ok(fds) = std::fs::read_dir(format!("/proc/{pid}/fd")) else {
            continue;
        };
        if fds.flatten().any(|f| {
            std::fs::metadata(f.path())
                .map(|m| m.dev() == dev)
                .unwrap_or(false)
        }) {
            out.push((pid, c.to_string()));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn xfs_handle_layout() {
        let h = xfs_handle(0x0102_0304_0506_0708, 0x0a0b_0c0d);
        assert_eq!(h.handle_type, 0x81);
        assert_eq!(h.bytes.len(), 12);
        assert_eq!(&h.bytes[..8], &0x0102_0304_0506_0708u64.to_ne_bytes());
    }

    #[test]
    fn dev_split() {
        // makedev(7, 3) in glibc's encoding.
        assert_eq!(dev_major_minor(0x703), (7, 3));
        assert_eq!(dev_major_minor((259 << 8) | 1), (259, 1));
    }
}
