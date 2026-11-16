//! Thin libc wrappers for the filesystem calls the stubber needs (Linux).

use std::ffi::{CString, OsStr};
use std::fs::File;
use std::io;
use std::mem::zeroed;
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, FromRawFd, OwnedFd};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::Path;

use crate::InodeKey;

/// `XFS_SUPER_MAGIC` ("XFSB").
pub const XFS_SUPER_MAGIC: i64 = 0x5846_5342;

fn cvt(r: libc::c_int) -> io::Result<libc::c_int> {
    if r < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(r)
    }
}

pub fn cstr(p: &Path) -> io::Result<CString> {
    CString::new(p.as_os_str().as_bytes())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "NUL in path"))
}

fn cname(n: &OsStr) -> io::Result<CString> {
    CString::new(n.as_bytes())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "NUL in name"))
}

/// `openat` relative to a directory fd (or an absolute path with `dir = None`).
pub fn open(path: &Path, flags: libc::c_int, mode: libc::mode_t) -> io::Result<OwnedFd> {
    let c = cstr(path)?;
    // SAFETY: c is NUL-terminated.
    let fd = cvt(unsafe { libc::open(c.as_ptr(), flags | libc::O_CLOEXEC, mode as libc::c_uint) })?;
    // SAFETY: fd is new and owned here.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

/// Open for reading metadata and xattrs: `O_RDONLY | O_NOFOLLOW`, never
/// `O_RDWR` (an immutable inode would refuse it), never blocking on a FIFO.
pub fn open_ro(path: &Path) -> io::Result<OwnedFd> {
    open(
        path,
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK,
        0,
    )
}

pub fn open_path(path: &Path) -> io::Result<OwnedFd> {
    open(path, libc::O_PATH | libc::O_NOFOLLOW, 0)
}

pub fn open_dir(path: &Path) -> io::Result<OwnedFd> {
    open(
        path,
        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW,
        0,
    )
}

pub fn fstat(fd: BorrowedFd<'_>) -> io::Result<libc::stat> {
    // SAFETY: st is a valid out-parameter.
    let mut st: libc::stat = unsafe { zeroed() };
    cvt(unsafe { libc::fstat(fd.as_raw_fd(), &mut st) })?;
    Ok(st)
}

pub fn fstatfs(fd: BorrowedFd<'_>) -> io::Result<libc::statfs> {
    // SAFETY: sf is a valid out-parameter.
    let mut sf: libc::statfs = unsafe { zeroed() };
    cvt(unsafe { libc::fstatfs(fd.as_raw_fd(), &mut sf) })?;
    Ok(sf)
}

/// `statfs.f_fsid` as the FSAL computes it: the two 32-bit words, low first.
pub fn fsid_of(sf: &libc::statfs) -> u64 {
    // SAFETY: fsid_t is two c_int words; reading it as bytes is sound.
    let words: [u32; 2] = unsafe { std::mem::transmute_copy(&sf.f_fsid) };
    words[0] as u64 | (words[1] as u64) << 32
}

/// True when the filesystem holding `fd` is xfs.
pub fn is_xfs(fd: BorrowedFd<'_>) -> io::Result<bool> {
    Ok(fstatfs(fd)?.f_type as i64 == XFS_SUPER_MAGIC)
}

const FS_IOC_GETVERSION: libc::c_ulong = 0x8008_7601; // _IOR('v', 1, long)

/// Inode generation (`i_generation`), as the FSAL decodes it from the handle.
pub fn inode_generation(fd: BorrowedFd<'_>) -> io::Result<u32> {
    let mut gen_: libc::c_long = 0;
    // SAFETY: gen_ is a valid out-parameter for FS_IOC_GETVERSION.
    cvt(unsafe { libc::ioctl(fd.as_raw_fd(), FS_IOC_GETVERSION as _, &mut gen_) })?;
    Ok(gen_ as u32)
}

/// `(fsid, ino, ino_gen)` of an open file; `file_id` left 0.
pub fn inode_key(fd: BorrowedFd<'_>) -> io::Result<InodeKey> {
    let st = fstat(fd)?;
    let sf = fstatfs(fd)?;
    Ok(InodeKey {
        fsid: fsid_of(&sf),
        ino: st.st_ino,
        ino_gen: inode_generation(fd)?,
        file_id: 0,
    })
}

/// An unnamed file in `dir` (same filesystem as its final name).
pub fn open_tmpfile(dir: &Path, mode: libc::mode_t) -> io::Result<File> {
    let fd = open(dir, libc::O_TMPFILE | libc::O_RDWR, mode)?;
    Ok(File::from(fd))
}

/// `openat2` resolution flags for every path below a share: never leave the
/// starting directory or its mount, never follow a symlink (or a `/proc`
/// magic link) in any component.
pub const RESOLVE_SAFE: u64 = libc::RESOLVE_BENEATH
    | libc::RESOLVE_NO_SYMLINKS
    | libc::RESOLVE_NO_MAGICLINKS
    | libc::RESOLVE_NO_XDEV;

/// `openat2(dir, rel, flags, mode, resolve)`.
pub fn openat2(
    dir: BorrowedFd<'_>,
    rel: &Path,
    flags: libc::c_int,
    mode: libc::mode_t,
    resolve: u64,
) -> io::Result<OwnedFd> {
    let c = cstr(rel)?;
    // SAFETY: open_how is plain data; zero is a valid value.
    let mut how: libc::open_how = unsafe { zeroed() };
    how.flags = (flags | libc::O_CLOEXEC) as u64;
    if flags & (libc::O_CREAT | libc::O_TMPFILE) != 0 {
        how.mode = mode as u64;
    }
    how.resolve = resolve;
    // A rename elsewhere on the filesystem during the walk makes the kernel
    // answer EAGAIN rather than risk an escape; try again a few times.
    for _ in 0..8 {
        // SAFETY: c is NUL-terminated; how is a valid open_how of that size.
        let r = unsafe {
            libc::syscall(
                libc::SYS_openat2,
                dir.as_raw_fd(),
                c.as_ptr(),
                &how as *const libc::open_how,
                std::mem::size_of::<libc::open_how>(),
            )
        };
        if r >= 0 {
            // SAFETY: r is a new fd owned here.
            return Ok(unsafe { OwnedFd::from_raw_fd(r as libc::c_int) });
        }
        let e = io::Error::last_os_error();
        if e.raw_os_error() != Some(libc::EAGAIN) {
            return Err(e);
        }
    }
    Err(io::Error::from_raw_os_error(libc::EAGAIN))
}

/// One name in a directory that was resolved below a root without
/// following symlinks. Every operation on it is `*at(dir, name)` with the
/// final component never followed, so nothing a share's users create (a
/// symlink in any component, `..`) can redirect it outside the share.
#[derive(Debug)]
pub struct At {
    /// The parent directory (`O_PATH`).
    pub dir: OwnedFd,
    /// A single path component.
    pub name: CString,
}

/// Resolve `rel` (relative to `root`, the trusted data volume or fill path)
/// to its parent directory and final name: `openat2` with
/// [`RESOLVE_SAFE`] from a `root` dirfd. `rel` must be a plain relative
/// path ([`crate::split_rel`]), so the walk also stays inside `rel`'s share.
pub fn resolve_beneath(root: &Path, rel: &Path) -> io::Result<At> {
    let (parent, name) = crate::split_rel(rel)?;
    let root_fd = open(root, libc::O_PATH | libc::O_DIRECTORY, 0)?;
    let dir = if parent.as_os_str().is_empty() {
        root_fd
    } else {
        openat2(
            root_fd.as_fd(),
            parent,
            libc::O_PATH | libc::O_DIRECTORY,
            0,
            RESOLVE_SAFE,
        )?
    };
    Ok(At {
        dir,
        name: cname(name)?,
    })
}

/// Open `rel` below `root` without following a symlink anywhere.
pub fn open_beneath(
    root: &Path,
    rel: &Path,
    flags: libc::c_int,
    mode: libc::mode_t,
) -> io::Result<OwnedFd> {
    resolve_beneath(root, rel)?.open(flags, mode)
}

/// [`open_ro`] for `rel` below `root`, never following a symlink.
pub fn open_ro_beneath(root: &Path, rel: &Path) -> io::Result<OwnedFd> {
    resolve_beneath(root, rel)?.open_ro()
}

/// [`open_dir`] for `rel` below `root`, never following a symlink.
pub fn open_dir_beneath(root: &Path, rel: &Path) -> io::Result<OwnedFd> {
    resolve_beneath(root, rel)?.open_dir()
}

/// Reopen an `O_PATH` fd as a real one on the same inode (no path lookup,
/// so no race with a rename or a symlink swap).
pub fn reopen(fd: BorrowedFd<'_>, flags: libc::c_int) -> io::Result<OwnedFd> {
    let p = CString::new(format!("/proc/self/fd/{}", fd.as_raw_fd())).expect("no NUL");
    // SAFETY: p is NUL-terminated.
    let r = cvt(unsafe { libc::open(p.as_ptr(), flags | libc::O_CLOEXEC) })?;
    // SAFETY: r is new and owned here.
    Ok(unsafe { OwnedFd::from_raw_fd(r) })
}

/// An unnamed file in the directory `dir` (any fd, `O_PATH` included).
pub fn tmpfile_in(dir: BorrowedFd<'_>, mode: libc::mode_t) -> io::Result<File> {
    // SAFETY: "." is NUL-terminated.
    let fd = cvt(unsafe {
        libc::openat(
            dir.as_raw_fd(),
            c".".as_ptr(),
            libc::O_TMPFILE | libc::O_RDWR | libc::O_CLOEXEC,
            mode as libc::c_uint,
        )
    })?;
    // SAFETY: fd is new and owned here.
    Ok(File::from(unsafe { OwnedFd::from_raw_fd(fd) }))
}

impl At {
    fn d(&self) -> libc::c_int {
        self.dir.as_raw_fd()
    }

    /// `openat(dir, name, flags | O_NOFOLLOW)`.
    pub fn open(&self, flags: libc::c_int, mode: libc::mode_t) -> io::Result<OwnedFd> {
        // SAFETY: name is NUL-terminated.
        let fd = cvt(unsafe {
            libc::openat(
                self.d(),
                self.name.as_ptr(),
                flags | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                mode as libc::c_uint,
            )
        })?;
        // SAFETY: fd is new and owned here.
        Ok(unsafe { OwnedFd::from_raw_fd(fd) })
    }

    /// Like [`open_ro`]: metadata and xattrs, never blocking on a FIFO.
    pub fn open_ro(&self) -> io::Result<OwnedFd> {
        self.open(libc::O_RDONLY | libc::O_NONBLOCK, 0)
    }

    pub fn open_dir(&self) -> io::Result<OwnedFd> {
        self.open(libc::O_RDONLY | libc::O_DIRECTORY, 0)
    }

    /// An `O_PATH` fd on the object itself (a symlink is not followed).
    pub fn open_path(&self) -> io::Result<OwnedFd> {
        self.open(libc::O_PATH, 0)
    }

    pub fn lstat(&self) -> io::Result<libc::stat> {
        // SAFETY: st is a valid out-parameter; name is NUL-terminated.
        let mut st: libc::stat = unsafe { zeroed() };
        cvt(unsafe {
            libc::fstatat(
                self.d(),
                self.name.as_ptr(),
                &mut st,
                libc::AT_SYMLINK_NOFOLLOW,
            )
        })?;
        Ok(st)
    }

    pub fn mkdir(&self, mode: libc::mode_t) -> io::Result<()> {
        // SAFETY: name is NUL-terminated.
        cvt(unsafe { libc::mkdirat(self.d(), self.name.as_ptr(), mode) })?;
        Ok(())
    }

    pub fn symlink(&self, target: &str) -> io::Result<()> {
        let t = cname(OsStr::new(target))?;
        // SAFETY: both are NUL-terminated.
        cvt(unsafe { libc::symlinkat(t.as_ptr(), self.d(), self.name.as_ptr()) })?;
        Ok(())
    }

    pub fn readlink(&self) -> io::Result<std::ffi::OsString> {
        let mut buf = vec![0u8; libc::PATH_MAX as usize];
        // SAFETY: buf is valid for buf.len() bytes.
        let n = unsafe {
            libc::readlinkat(
                self.d(),
                self.name.as_ptr(),
                buf.as_mut_ptr().cast(),
                buf.len(),
            )
        };
        if n < 0 {
            return Err(io::Error::last_os_error());
        }
        buf.truncate(n as usize);
        Ok(std::ffi::OsString::from_vec(buf))
    }

    pub fn unlink(&self) -> io::Result<()> {
        // SAFETY: name is NUL-terminated.
        cvt(unsafe { libc::unlinkat(self.d(), self.name.as_ptr(), 0) })?;
        Ok(())
    }

    pub fn rmdir(&self) -> io::Result<()> {
        // SAFETY: name is NUL-terminated.
        cvt(unsafe { libc::unlinkat(self.d(), self.name.as_ptr(), libc::AT_REMOVEDIR) })?;
        Ok(())
    }

    /// Give an `O_TMPFILE` this name. Uses the `/proc/self/fd` form, which
    /// needs no `CAP_DAC_READ_SEARCH`. Fails with `EEXIST` if the name is
    /// taken (never replaces, never follows a symlink at the name).
    pub fn link_tmpfile(&self, f: &File) -> io::Result<()> {
        let src = CString::new(format!("/proc/self/fd/{}", f.as_raw_fd())).expect("no NUL");
        // SAFETY: both names are NUL-terminated.
        cvt(unsafe {
            libc::linkat(
                libc::AT_FDCWD,
                src.as_ptr(),
                self.d(),
                self.name.as_ptr(),
                libc::AT_SYMLINK_FOLLOW,
            )
        })?;
        Ok(())
    }

    /// Owner, mode and mtime of the object at this name, never following a
    /// symlink. Every change goes through an fd on the object itself, so a
    /// name swapped meanwhile cannot redirect it. A symlink takes owner and
    /// mtime only (`mode` must be `None`); a regular file or directory
    /// also takes `mode`; anything else is refused.
    pub fn set_attrs(
        &self,
        uid: u32,
        gid: u32,
        mode: Option<u32>,
        mtime_ns: i64,
    ) -> io::Result<()> {
        let p = self.open_path()?;
        set_attrs_opath(p.as_fd(), uid, gid, mode, mtime_ns)
    }
}

/// [`At::set_attrs`] on an `O_PATH` fd.
pub fn set_attrs_opath(
    p: BorrowedFd<'_>,
    uid: u32,
    gid: u32,
    mode: Option<u32>,
    mtime_ns: i64,
) -> io::Result<()> {
    let st = fstat(p)?;
    let kind = st.st_mode & libc::S_IFMT;
    let refuse = |why: &str| io::Error::new(io::ErrorKind::InvalidInput, why.to_string());
    match kind {
        libc::S_IFLNK => {
            if mode.is_some() {
                return Err(refuse("a symlink has no mode of its own"));
            }
            let flags = libc::AT_EMPTY_PATH | libc::AT_SYMLINK_NOFOLLOW;
            // SAFETY: "" is NUL-terminated; p is an O_PATH fd on the link.
            cvt(unsafe { libc::fchownat(p.as_raw_fd(), c"".as_ptr(), uid, gid, flags) })?;
            let times = [
                libc::timespec {
                    tv_sec: 0,
                    tv_nsec: libc::UTIME_OMIT,
                },
                timespec(mtime_ns),
            ];
            // SAFETY: times has two entries.
            cvt(unsafe { libc::utimensat(p.as_raw_fd(), c"".as_ptr(), times.as_ptr(), flags) })?;
            Ok(())
        }
        libc::S_IFREG | libc::S_IFDIR => {
            let flags = if kind == libc::S_IFDIR {
                libc::O_RDONLY | libc::O_DIRECTORY
            } else {
                libc::O_RDONLY | libc::O_NONBLOCK
            };
            let fd = reopen(p, flags)?;
            match mode {
                Some(mode) => set_attrs_fd(fd.as_fd(), uid, gid, mode, mtime_ns),
                None => {
                    let m = fstat(fd.as_fd())?.st_mode;
                    set_attrs_fd(fd.as_fd(), uid, gid, m, mtime_ns)
                }
            }
        }
        _ => Err(refuse("not a regular file, directory or symlink")),
    }
}

/// `renameat2(…, RENAME_NOREPLACE)`: never overwrite something at the
/// target. Both names come from [`resolve_beneath`]; the caller keeps them
/// in one share.
pub fn rename_noreplace(from: &At, to: &At) -> io::Result<()> {
    // SAFETY: both names are NUL-terminated.
    cvt(unsafe {
        libc::renameat2(
            from.d(),
            from.name.as_ptr(),
            to.d(),
            to.name.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    })?;
    Ok(())
}

/// Owner, mode and mtime on an open fd.
pub fn set_attrs_fd(
    fd: BorrowedFd<'_>,
    uid: u32,
    gid: u32,
    mode: u32,
    mtime_ns: i64,
) -> io::Result<()> {
    // SAFETY: plain syscalls on a valid fd.
    cvt(unsafe { libc::fchown(fd.as_raw_fd(), uid, gid) })?;
    cvt(unsafe { libc::fchmod(fd.as_raw_fd(), (mode & 0o7777) as libc::mode_t) })?;
    let times = [
        libc::timespec {
            tv_sec: 0,
            tv_nsec: libc::UTIME_OMIT,
        },
        timespec(mtime_ns),
    ];
    // SAFETY: times has two entries.
    cvt(unsafe { libc::futimens(fd.as_raw_fd(), times.as_ptr()) })?;
    Ok(())
}

fn timespec(ns: i64) -> libc::timespec {
    libc::timespec {
        tv_sec: ns.div_euclid(1_000_000_000) as _,
        tv_nsec: ns.rem_euclid(1_000_000_000) as _,
    }
}

/// Free `[offset, offset+len)` without changing the size.
pub fn punch_hole(f: &File, offset: u64, len: u64) -> io::Result<()> {
    if len == 0 {
        return Ok(());
    }
    // SAFETY: plain syscall on a valid fd.
    cvt(unsafe {
        libc::fallocate(
            f.as_raw_fd(),
            libc::FALLOC_FL_PUNCH_HOLE | libc::FALLOC_FL_KEEP_SIZE,
            offset as libc::off_t,
            len as libc::off_t,
        )
    })?;
    Ok(())
}

/// True if any process has the file open for writing. A read lease can only
/// be taken when nobody does (`EAGAIN` otherwise). The lease is dropped at
/// once. Root has `CAP_LEASE`.
pub fn open_for_write_elsewhere(fd: BorrowedFd<'_>) -> io::Result<bool> {
    Ok(ReadLease::take(fd)?.is_none())
}

/// A held `F_RDLCK` lease on an open (read-only) file.
///
/// While it is held, any `open` for write and any `truncate` of the inode
/// by another process waits (up to `/proc/sys/fs/lease-break-time`) for the
/// holder to release it, and the lease shows as being broken
/// ([`ReadLease::broken`]). It can only be taken while nobody has the file
/// open for write, including this process: never open the file for write
/// while holding one (the open would wait for our own release).
///
/// The kernel signals a lease break to the holder with `SIGIO`, whose
/// default action terminates the process. Taking a lease therefore sets
/// `SIGIO` to ignored (once, process-wide; nothing in the daemon uses it)
/// and clears the fd's owner so no signal is sent at all.
pub struct ReadLease<'a> {
    fd: BorrowedFd<'a>,
}

fn ignore_sigio() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    ONCE.call_once(|| {
        // SAFETY: setting a signal's disposition to SIG_IGN is always sound.
        unsafe { libc::signal(libc::SIGIO, libc::SIG_IGN) };
    });
}

impl<'a> ReadLease<'a> {
    /// `Ok(None)` when someone has the file open for write.
    pub fn take(fd: BorrowedFd<'a>) -> io::Result<Option<ReadLease<'a>>> {
        ignore_sigio();
        // SAFETY: plain fcntl on a valid fd.
        let r = unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_SETLEASE, libc::F_RDLCK) };
        if r < 0 {
            let e = io::Error::last_os_error();
            return match e.raw_os_error() {
                Some(libc::EAGAIN) | Some(libc::EBUSY) => Ok(None),
                _ => Err(e),
            };
        }
        // No break signal: the holder polls `broken()` instead.
        // SAFETY: as above; owner 0 clears it.
        unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_SETOWN, 0) };
        Ok(Some(ReadLease { fd }))
    }

    /// True once another process tried to open the file for write or
    /// truncate it (`F_GETLEASE` reports the type the lease is being broken
    /// to), or if the lease is gone for any other reason.
    pub fn broken(&self) -> bool {
        // SAFETY: plain fcntl on a valid fd.
        let r = unsafe { libc::fcntl(self.fd.as_raw_fd(), libc::F_GETLEASE) };
        r != libc::F_RDLCK
    }
}

impl Drop for ReadLease<'_> {
    fn drop(&mut self) {
        // SAFETY: plain fcntl on a valid fd.
        unsafe { libc::fcntl(self.fd.as_raw_fd(), libc::F_SETLEASE, libc::F_UNLCK) };
    }
}

/// Open (creating it 0700 if missing) a daemon-private directory `path`,
/// which must be `<parent>/.omni/<name>`. `<parent>` is opened as given;
/// below it, `.omni` and `<name>` are each opened with
/// `O_DIRECTORY | O_NOFOLLOW` relative to the one before, so a symlink
/// planted at either never redirects the daemon's writes.
pub fn private_dir(path: &Path) -> io::Result<OwnedFd> {
    let name = path
        .file_name()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "no directory name"))?;
    let omni = path
        .parent()
        .filter(|p| p.file_name() == Some(OsStr::new(".omni")))
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "not below .omni"))?;
    let root = omni
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "no data root"))?;
    let root = open(root, libc::O_PATH | libc::O_DIRECTORY, 0)?;
    let omni = subdir(root.as_fd(), OsStr::new(".omni"))?;
    subdir(omni.as_fd(), name)
}

/// `mkdirat` (0700, an existing one is fine) then open it `O_DIRECTORY |
/// O_NOFOLLOW`: a symlink at `name` fails with `ENOTDIR`/`ELOOP`.
fn subdir(dir: BorrowedFd<'_>, name: &OsStr) -> io::Result<OwnedFd> {
    let c = cname(name)?;
    // SAFETY: c is NUL-terminated; dir is a valid fd.
    let r = unsafe { libc::mkdirat(dir.as_raw_fd(), c.as_ptr(), 0o700) };
    if r < 0 {
        let e = io::Error::last_os_error();
        if e.raw_os_error() != Some(libc::EEXIST) {
            return Err(e);
        }
    }
    // SAFETY: as above.
    let fd = cvt(unsafe {
        libc::openat(
            dir.as_raw_fd(),
            c.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    })?;
    // SAFETY: fd is new and owned here.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

/// `openat(dir, name, flags | O_NOFOLLOW | O_CLOEXEC, mode)`.
pub fn openat_nofollow(
    dir: BorrowedFd<'_>,
    name: &OsStr,
    flags: libc::c_int,
    mode: libc::mode_t,
) -> io::Result<OwnedFd> {
    let c = cname(name)?;
    // SAFETY: c is NUL-terminated; dir is a valid fd.
    let fd = cvt(unsafe {
        libc::openat(
            dir.as_raw_fd(),
            c.as_ptr(),
            flags | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            mode as libc::c_uint,
        )
    })?;
    // SAFETY: fd is new and owned here.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

/// `unlinkat(dir, name, 0)`; absent is not an error.
pub fn unlinkat(dir: BorrowedFd<'_>, name: &OsStr) -> io::Result<()> {
    let c = cname(name)?;
    // SAFETY: c is NUL-terminated; dir is a valid fd.
    if unsafe { libc::unlinkat(dir.as_raw_fd(), c.as_ptr(), 0) } < 0 {
        let e = io::Error::last_os_error();
        if e.kind() != io::ErrorKind::NotFound {
            return Err(e);
        }
    }
    Ok(())
}

/// The first offset at or after `from` that holds data (`SEEK_DATA`), or
/// `None` if only holes follow.
pub fn first_data_at(fd: BorrowedFd<'_>, from: u64) -> io::Result<Option<u64>> {
    // SAFETY: plain lseek on a valid fd (the offset is not shared: callers
    // pass a dedicated fd).
    let r = unsafe { libc::lseek(fd.as_raw_fd(), from as libc::off_t, libc::SEEK_DATA) };
    if r < 0 {
        let e = io::Error::last_os_error();
        return if e.raw_os_error() == Some(libc::ENXIO) {
            Ok(None)
        } else {
            Err(e)
        };
    }
    Ok(Some(r as u64))
}

/// `fsync` an fd (also valid on an `O_RDONLY` fd on Linux): makes a marker
/// written through it durable.
pub fn fsync(fd: BorrowedFd<'_>) -> io::Result<()> {
    // SAFETY: plain syscall on a valid fd.
    cvt(unsafe { libc::fsync(fd.as_raw_fd()) })?;
    Ok(())
}

/// True when some process holds a fanotify **filesystem** mark with a
/// permission or pre-content mask on the filesystem holding `path` (a
/// running smb-guard). Read from `/proc/*/fdinfo`, whose `sdev:` is the
/// superblock's `dev_t` in the kernel's encoding (`major << 20 | minor`).
/// Tests use it to refuse a volume a live guard would fail closed on.
pub fn fanotify_guarded(path: &Path) -> io::Result<bool> {
    let st = std::fs::metadata(path)?;
    use std::os::unix::fs::MetadataExt;
    let dev = st.dev();
    let kdev = (libc::major(dev) as u64) << 20 | libc::minor(dev) as u64;
    let want = format!("sdev:{kdev:x} ");
    for p in std::fs::read_dir("/proc")?.flatten() {
        let Ok(fds) = std::fs::read_dir(p.path().join("fdinfo")) else {
            continue;
        };
        for fd in fds.flatten() {
            let Ok(info) = std::fs::read_to_string(fd.path()) else {
                continue;
            };
            if fdinfo_guards(&info, &want) {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

/// Whether one fanotify fd's `fdinfo` text is a guard on the filesystem
/// whose mark line contains `want` (`sdev:<hex> `): a group of a permission
/// class (`FAN_CLASS_CONTENT` or `FAN_CLASS_PRE_CONTENT`, class bits 0x0c of
/// `fanotify flags:`) with a filesystem mark of a non-empty mask. A
/// notification group (the daemon's write detector or namespace watcher)
/// never blocks a read, so it is not one: counting it made Linux tests
/// started beside a running daemon skip (WP9).
pub fn fdinfo_guards(info: &str, want: &str) -> bool {
    let class = info.lines().find_map(|l| {
        let hex = l
            .trim()
            .strip_prefix("fanotify flags:")?
            .split_whitespace()
            .next()?;
        u32::from_str_radix(hex, 16).ok().map(|f| f & 0x0c)
    });
    if !class.is_some_and(|c| c != 0) {
        return false;
    }
    info.lines().any(|line| {
        // A filesystem mark has `sdev:` but no `ino:`/`mnt_id:`.
        line.strip_prefix("fanotify sdev:").is_some_and(|rest| {
            line.contains(want)
                && rest
                    .split_whitespace()
                    .find_map(|w| w.strip_prefix("mask:"))
                    .is_some_and(|m| u64::from_str_radix(m, 16).is_ok_and(|m| m != 0))
        })
    })
}

/// `CapEff` bit `cap` of this process, from `/proc/self/status`.
pub fn has_capability(cap: u32) -> io::Result<bool> {
    let s = std::fs::read_to_string("/proc/self/status")?;
    for line in s.lines() {
        if let Some(hex) = line.strip_prefix("CapEff:") {
            let v = u64::from_str_radix(hex.trim(), 16).map_err(io::Error::other)?;
            return Ok(v & (1 << cap) != 0);
        }
    }
    Err(io::Error::other("no CapEff in /proc/self/status"))
}

pub const CAP_SYS_ADMIN: u32 = 21;

/// Borrow an `OwnedFd` as a `File`-like fd for `AsFd` APIs.
pub fn as_fd(f: &impl AsFd) -> BorrowedFd<'_> {
    f.as_fd()
}

#[cfg(test)]
mod guard_tests {
    use super::fdinfo_guards;

    const MARK: &str = "fanotify sdev:fd00001 mflags:0 mask:40000 ignored_mask:0\n";

    #[test]
    fn only_a_permission_class_group_with_a_filesystem_mark_guards() {
        let pre = format!("pos:\t0\nfanotify flags:18 event-flags:88002\n{MARK}");
        let content = format!("pos:\t0\nfanotify flags:14 event-flags:88002\n{MARK}");
        let notif = format!("pos:\t0\nfanotify flags:13 event-flags:88002\n{MARK}");
        assert!(fdinfo_guards(&pre, "sdev:fd00001 "));
        assert!(fdinfo_guards(&content, "sdev:fd00001 "));
        assert!(
            !fdinfo_guards(&notif, "sdev:fd00001 "),
            "a detector's group"
        );
        assert!(!fdinfo_guards(&pre, "sdev:fd00002 "), "another filesystem");
        let empty = "fanotify flags:18 event-flags:0\nfanotify sdev:fd00001 mflags:0 mask:0 ignored_mask:0\n";
        assert!(!fdinfo_guards(empty, "sdev:fd00001 "), "no events marked");
        assert!(!fdinfo_guards("pos:\t0\nflags:\t02\n", "sdev:fd00001 "));
    }
}
