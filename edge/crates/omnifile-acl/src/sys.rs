//! Reading and writing ACLs on the edge's filesystem (Linux xattrs).
//!
//! Symlinks are never followed, in any component: [`SysFs`] resolves every
//! path with `openat2(RESOLVE_NO_SYMLINKS)` to an `O_PATH` fd on the object
//! itself and works on that inode (xattrs through its `/proc/self/fd`
//! link, owner with `AT_EMPTY_PATH`, directory listings through a real fd).
//! A directory swapped for a symlink mid-walk therefore fails instead of
//! letting root rewrite ACLs outside the share. The data volume's own path
//! must not contain symlinks either. A symlink has no ACL of its own.

use std::io;
use std::path::Path;

use crate::posix::PosixAcl;

/// What Samba's `vfs_acl_xattr` would store. Its presence means someone loaded
/// that module, and the file now has a Windows ACL that NFS never enforces.
pub const XATTR_NTACL: &str = "security.NTACL";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    File,
    Dir,
    Symlink,
    Other,
}

/// One inode's ownership, mode and ACLs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FileAcl {
    pub dev: u64,
    pub ino: u64,
    pub kind: Kind,
    pub uid: u32,
    pub gid: u32,
    /// `st_mode & 0o7777`.
    pub mode: u32,
    /// `None` when the file has only its mode (a minimal ACL).
    pub access: Option<PosixAcl>,
    /// Directories only.
    pub default: Option<PosixAcl>,
    pub has_ntacl: bool,
}

impl FileAcl {
    /// The access ACL, synthesised from the mode when the file has none.
    pub fn effective_access(&self) -> PosixAcl {
        self.access
            .clone()
            .unwrap_or_else(|| PosixAcl::from_mode(self.mode))
    }
}

/// The filesystem operations the scanner and the share-root code use.
/// [`SysFs`] is Linux; tests may substitute a fake.
pub trait AclFs: Send + Sync {
    fn read(&self, path: &Path) -> io::Result<FileAcl>;
    fn write_access(&self, path: &Path, acl: &PosixAcl) -> io::Result<()>;
    /// `None` removes the default ACL.
    fn write_default(&self, path: &Path, acl: Option<&PosixAcl>) -> io::Result<()>;
    fn chown(&self, path: &Path, uid: Option<u32>, gid: Option<u32>) -> io::Result<()>;
    fn read_dir(&self, path: &Path) -> io::Result<Vec<std::path::PathBuf>>;
    /// [`AclFs::write_access`], only if `path` is still the inode `pin`
    /// (`(st_dev, st_ino)` from the [`FileAcl`] the new ACL was computed
    /// from): a rename between the read and the write must not apply the
    /// ACL to another file (`ESTALE` then).
    fn write_access_at(&self, path: &Path, pin: (u64, u64), acl: &PosixAcl) -> io::Result<()> {
        let _ = pin;
        self.write_access(path, acl)
    }
    /// [`AclFs::write_default`], pinned as [`AclFs::write_access_at`].
    fn write_default_at(
        &self,
        path: &Path,
        pin: (u64, u64),
        acl: Option<&PosixAcl>,
    ) -> io::Result<()> {
        let _ = pin;
        self.write_default(path, acl)
    }
    /// [`AclFs::chown`], pinned as [`AclFs::write_access_at`].
    fn chown_at(
        &self,
        path: &Path,
        pin: (u64, u64),
        uid: Option<u32>,
        gid: Option<u32>,
    ) -> io::Result<()> {
        let _ = pin;
        self.chown(path, uid, gid)
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct SysFs;

#[cfg(target_os = "linux")]
mod imp {
    use super::*;
    use crate::posix::{XATTR_ACCESS, XATTR_DEFAULT};
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;

    fn cpath(p: &Path) -> io::Result<CString> {
        CString::new(p.as_os_str().as_bytes())
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "NUL in path"))
    }

    /// `lgetxattr`; `Ok(None)` when the attribute does not exist.
    pub fn lgetxattr(path: &Path, name: &str) -> io::Result<Option<Vec<u8>>> {
        let p = cpath(path)?;
        let n = CString::new(name).unwrap();
        let mut buf = vec![0u8; 256];
        loop {
            // SAFETY: valid C strings and a buffer of the given length.
            let r = unsafe {
                libc::lgetxattr(p.as_ptr(), n.as_ptr(), buf.as_mut_ptr().cast(), buf.len())
            };
            if r >= 0 {
                buf.truncate(r as usize);
                return Ok(Some(buf));
            }
            let e = io::Error::last_os_error();
            match e.raw_os_error() {
                Some(libc::ENODATA) => return Ok(None),
                Some(libc::EOPNOTSUPP) if name == XATTR_NTACL => return Ok(None),
                Some(libc::ERANGE) => {
                    // SAFETY: size query with a null buffer.
                    let need =
                        unsafe { libc::lgetxattr(p.as_ptr(), n.as_ptr(), std::ptr::null_mut(), 0) };
                    if need < 0 {
                        return Err(io::Error::last_os_error());
                    }
                    buf.resize(need as usize + 64, 0);
                }
                _ => return Err(e),
            }
        }
    }

    pub fn lsetxattr(path: &Path, name: &str, val: &[u8]) -> io::Result<()> {
        let p = cpath(path)?;
        let n = CString::new(name).unwrap();
        // SAFETY: valid C strings and a value buffer of the given length.
        let r =
            unsafe { libc::lsetxattr(p.as_ptr(), n.as_ptr(), val.as_ptr().cast(), val.len(), 0) };
        if r == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    pub fn lremovexattr(path: &Path, name: &str) -> io::Result<()> {
        let p = cpath(path)?;
        let n = CString::new(name).unwrap();
        // SAFETY: valid C strings.
        let r = unsafe { libc::lremovexattr(p.as_ptr(), n.as_ptr()) };
        if r == 0 {
            return Ok(());
        }
        let e = io::Error::last_os_error();
        if e.raw_os_error() == Some(libc::ENODATA) {
            Ok(())
        } else {
            Err(e)
        }
    }

    /// `O_PATH` fd on `path`'s object: no symlink is followed in any
    /// component; a trailing symlink yields an fd on the link itself.
    pub fn open_nofollow(path: &Path) -> io::Result<std::os::fd::OwnedFd> {
        use std::os::fd::FromRawFd;
        let p = cpath(path)?;
        // SAFETY: open_how is plain data; zero is valid.
        let mut how: libc::open_how = unsafe { std::mem::zeroed() };
        how.flags = (libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC) as u64;
        how.resolve = libc::RESOLVE_NO_SYMLINKS | libc::RESOLVE_NO_MAGICLINKS;
        for _ in 0..8 {
            // SAFETY: p is NUL-terminated; how is a valid open_how.
            let r = unsafe {
                libc::syscall(
                    libc::SYS_openat2,
                    libc::AT_FDCWD,
                    p.as_ptr(),
                    &how as *const libc::open_how,
                    std::mem::size_of::<libc::open_how>(),
                )
            };
            if r >= 0 {
                // SAFETY: r is a new fd owned here.
                return Ok(unsafe { std::os::fd::OwnedFd::from_raw_fd(r as libc::c_int) });
            }
            let e = io::Error::last_os_error();
            if e.raw_os_error() != Some(libc::EAGAIN) {
                return Err(e);
            }
        }
        Err(io::Error::from_raw_os_error(libc::EAGAIN))
    }

    fn fstat(fd: &std::os::fd::OwnedFd) -> io::Result<libc::stat> {
        use std::os::fd::AsRawFd;
        // SAFETY: st is a valid out-parameter.
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        if unsafe { libc::fstat(fd.as_raw_fd(), &mut st) } < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(st)
    }

    /// The pinned inode's `/proc/self/fd` path. The xattr calls follow this
    /// magic link to exactly that inode, with no further lookup.
    fn proc_path(fd: &std::os::fd::OwnedFd) -> std::path::PathBuf {
        use std::os::fd::AsRawFd;
        std::path::PathBuf::from(format!("/proc/self/fd/{}", fd.as_raw_fd()))
    }

    /// `getxattr` on the inode itself; `Ok(None)` when absent.
    fn fd_getxattr(fd: &std::os::fd::OwnedFd, name: &str) -> io::Result<Option<Vec<u8>>> {
        let p = cpath(&proc_path(fd))?;
        let n = CString::new(name).unwrap();
        let mut buf = vec![0u8; 256];
        loop {
            // SAFETY: valid C strings and a buffer of the given length.
            let r = unsafe {
                libc::getxattr(p.as_ptr(), n.as_ptr(), buf.as_mut_ptr().cast(), buf.len())
            };
            if r >= 0 {
                buf.truncate(r as usize);
                return Ok(Some(buf));
            }
            let e = io::Error::last_os_error();
            match e.raw_os_error() {
                Some(libc::ENODATA) => return Ok(None),
                Some(libc::EOPNOTSUPP) if name == XATTR_NTACL => return Ok(None),
                Some(libc::ERANGE) => {
                    // SAFETY: size query with a null buffer.
                    let need =
                        unsafe { libc::getxattr(p.as_ptr(), n.as_ptr(), std::ptr::null_mut(), 0) };
                    if need < 0 {
                        return Err(io::Error::last_os_error());
                    }
                    buf.resize(need as usize + 64, 0);
                }
                _ => return Err(e),
            }
        }
    }

    /// An `O_PATH` fd on a regular file or directory at `path`, for writing
    /// its xattrs; a symlink (or anything else) is refused.
    fn open_for_acl(path: &Path) -> io::Result<std::os::fd::OwnedFd> {
        let fd = open_nofollow(path)?;
        let k = fstat(&fd)?.st_mode & libc::S_IFMT;
        if k != libc::S_IFREG && k != libc::S_IFDIR {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("{} is not a regular file or directory", path.display()),
            ));
        }
        Ok(fd)
    }

    /// [`open_for_acl`], refusing (`ESTALE`) an inode other than `pin`.
    fn open_pinned(path: &Path, pin: (u64, u64)) -> io::Result<std::os::fd::OwnedFd> {
        let fd = open_for_acl(path)?;
        let st = fstat(&fd)?;
        if (st.st_dev, st.st_ino) != pin {
            return Err(io::Error::from_raw_os_error(libc::ESTALE));
        }
        Ok(fd)
    }

    fn fd_setxattr(fd: &std::os::fd::OwnedFd, name: &str, val: &[u8]) -> io::Result<()> {
        let p = cpath(&proc_path(fd))?;
        let n = CString::new(name).unwrap();
        // SAFETY: valid C strings and a value buffer of the given length.
        let r =
            unsafe { libc::setxattr(p.as_ptr(), n.as_ptr(), val.as_ptr().cast(), val.len(), 0) };
        if r == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    fn fd_removexattr(fd: &std::os::fd::OwnedFd, name: &str) -> io::Result<()> {
        let p = cpath(&proc_path(fd))?;
        let n = CString::new(name).unwrap();
        // SAFETY: valid C strings.
        if unsafe { libc::removexattr(p.as_ptr(), n.as_ptr()) } == 0 {
            return Ok(());
        }
        let e = io::Error::last_os_error();
        if e.raw_os_error() == Some(libc::ENODATA) {
            Ok(())
        } else {
            Err(e)
        }
    }

    fn acl(fd: &std::os::fd::OwnedFd, name: &str) -> io::Result<Option<PosixAcl>> {
        match fd_getxattr(fd, name)? {
            None => Ok(None),
            Some(b) => PosixAcl::from_xattr(&b)
                .map(Some)
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e)),
        }
    }

    /// `fchownat` on the fd's own inode (the link itself for a symlink, like
    /// `lchown`).
    fn fchown_fd(fd: &std::os::fd::OwnedFd, uid: Option<u32>, gid: Option<u32>) -> io::Result<()> {
        use std::os::fd::AsRawFd;
        // SAFETY: "" is NUL-terminated; AT_EMPTY_PATH acts on fd's inode
        // (the link itself for a symlink, like lchown).
        let r = unsafe {
            libc::fchownat(
                fd.as_raw_fd(),
                c"".as_ptr(),
                uid.unwrap_or(u32::MAX),
                gid.unwrap_or(u32::MAX),
                libc::AT_EMPTY_PATH | libc::AT_SYMLINK_NOFOLLOW,
            )
        };
        if r == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    impl AclFs for SysFs {
        fn read(&self, path: &Path) -> io::Result<FileAcl> {
            let fd = open_nofollow(path)?;
            let st = fstat(&fd)?;
            let kind = match st.st_mode & libc::S_IFMT {
                libc::S_IFLNK => Kind::Symlink,
                libc::S_IFDIR => Kind::Dir,
                libc::S_IFREG => Kind::File,
                _ => Kind::Other,
            };
            let (access, default, has_ntacl) = if kind == Kind::Symlink {
                (None, None, false)
            } else {
                (
                    acl(&fd, XATTR_ACCESS)?,
                    if kind == Kind::Dir {
                        acl(&fd, XATTR_DEFAULT)?
                    } else {
                        None
                    },
                    fd_getxattr(&fd, XATTR_NTACL)?.is_some(),
                )
            };
            Ok(FileAcl {
                dev: st.st_dev,
                ino: st.st_ino,
                kind,
                uid: st.st_uid,
                gid: st.st_gid,
                mode: st.st_mode & 0o7777,
                access,
                default,
                has_ntacl,
            })
        }

        fn write_access(&self, path: &Path, acl: &PosixAcl) -> io::Result<()> {
            acl.validate()
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;
            // The kernel also updates the mode's group class from the mask.
            fd_setxattr(&open_for_acl(path)?, XATTR_ACCESS, &acl.to_xattr())
        }

        fn write_default(&self, path: &Path, acl: Option<&PosixAcl>) -> io::Result<()> {
            let fd = open_for_acl(path)?;
            match acl {
                Some(a) => {
                    a.validate()
                        .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;
                    fd_setxattr(&fd, XATTR_DEFAULT, &a.to_xattr())
                }
                None => fd_removexattr(&fd, XATTR_DEFAULT),
            }
        }

        fn chown(&self, path: &Path, uid: Option<u32>, gid: Option<u32>) -> io::Result<()> {
            fchown_fd(&open_nofollow(path)?, uid, gid)
        }

        fn chown_at(
            &self,
            path: &Path,
            pin: (u64, u64),
            uid: Option<u32>,
            gid: Option<u32>,
        ) -> io::Result<()> {
            fchown_fd(&open_pinned(path, pin)?, uid, gid)
        }

        fn write_access_at(&self, path: &Path, pin: (u64, u64), acl: &PosixAcl) -> io::Result<()> {
            acl.validate()
                .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;
            fd_setxattr(&open_pinned(path, pin)?, XATTR_ACCESS, &acl.to_xattr())
        }

        fn write_default_at(
            &self,
            path: &Path,
            pin: (u64, u64),
            acl: Option<&PosixAcl>,
        ) -> io::Result<()> {
            let fd = open_pinned(path, pin)?;
            match acl {
                Some(a) => {
                    a.validate()
                        .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))?;
                    fd_setxattr(&fd, XATTR_DEFAULT, &a.to_xattr())
                }
                None => fd_removexattr(&fd, XATTR_DEFAULT),
            }
        }

        fn read_dir(&self, path: &Path) -> io::Result<Vec<std::path::PathBuf>> {
            let fd = open_nofollow(path)?;
            if fstat(&fd)?.st_mode & libc::S_IFMT != libc::S_IFDIR {
                return Err(io::Error::from_raw_os_error(libc::ENOTDIR));
            }
            // Listing through the pinned inode's /proc link; the names are
            // joined to `path` for the caller, which resolves each again
            // without symlinks.
            let mut v = Vec::new();
            for e in std::fs::read_dir(proc_path(&fd))? {
                v.push(path.join(e?.file_name()));
            }
            v.sort();
            Ok(v)
        }
    }
}

#[cfg(target_os = "linux")]
pub use imp::{lgetxattr, lremovexattr, lsetxattr};

#[cfg(not(target_os = "linux"))]
impl AclFs for SysFs {
    fn read(&self, _: &Path) -> io::Result<FileAcl> {
        Err(io::ErrorKind::Unsupported.into())
    }
    fn write_access(&self, _: &Path, _: &PosixAcl) -> io::Result<()> {
        Err(io::ErrorKind::Unsupported.into())
    }
    fn write_default(&self, _: &Path, _: Option<&PosixAcl>) -> io::Result<()> {
        Err(io::ErrorKind::Unsupported.into())
    }
    fn chown(&self, _: &Path, _: Option<u32>, _: Option<u32>) -> io::Result<()> {
        Err(io::ErrorKind::Unsupported.into())
    }
    fn read_dir(&self, _: &Path) -> io::Result<Vec<std::path::PathBuf>> {
        Err(io::ErrorKind::Unsupported.into())
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use crate::posix::{Perms, Tag};

    /// Needs a filesystem with POSIX ACLs (xfs, ext4, tmpfs); skipped where
    /// the temp dir does not support them.
    #[test]
    fn round_trip_on_real_fs() {
        let d = tempfile::tempdir().unwrap();
        let mut a = PosixAcl::from_mode(0o750);
        a.set(Tag::Group(30001), Perms::RX);
        a.recompute_mask();
        match SysFs.write_access(d.path(), &a) {
            Err(e) if e.raw_os_error() == Some(libc::EOPNOTSUPP) => return,
            r => r.unwrap(),
        }
        SysFs.write_default(d.path(), Some(&a)).unwrap();
        let f = SysFs.read(d.path()).unwrap();
        assert_eq!(f.kind, Kind::Dir);
        assert_eq!(f.access.as_ref(), Some(&a));
        assert_eq!(f.default.as_ref(), Some(&a));
        assert_eq!(f.mode & 0o777, 0o750);
        assert!(!f.has_ntacl);
        // Inherited by a new file.
        let p = d.path().join("child");
        std::fs::write(&p, b"x").unwrap();
        let c = SysFs.read(&p).unwrap();
        assert_eq!(
            c.access.as_ref().and_then(|x| x.get(Tag::Group(30001))),
            Some(Perms::RX)
        );
        SysFs.write_default(d.path(), None).unwrap();
        assert!(SysFs.read(d.path()).unwrap().default.is_none());
        // Symlinks are not followed.
        let l = d.path().join("link");
        std::os::unix::fs::symlink(&p, &l).unwrap();
        let s = SysFs.read(&l).unwrap();
        assert_eq!(s.kind, Kind::Symlink);
        assert!(s.access.is_none());
    }

    /// Finding 5: a directory swapped for a symlink is never walked or
    /// written through, in the last component or an intermediate one.
    #[test]
    fn symlinked_directories_are_not_followed() {
        let d = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let secret = outside.path().join("secret");
        std::fs::write(&secret, b"x").unwrap();
        let mut a = PosixAcl::from_mode(0o640);
        a.set(Tag::Group(30001), Perms::RX);
        a.recompute_mask();
        match SysFs.write_access(&secret, &a) {
            Err(e) if e.raw_os_error() == Some(libc::EOPNOTSUPP) => return,
            r => r.unwrap(),
        }
        let before = SysFs.read(&secret).unwrap();
        let share = d.path().join("share");
        std::fs::create_dir(&share).unwrap();
        std::os::unix::fs::symlink(outside.path(), share.join("sub")).unwrap();
        let via = share.join("sub/secret");
        let mut b = PosixAcl::from_mode(0o777);
        b.set(Tag::Group(30009), Perms::RWX);
        b.recompute_mask();
        let loop_ = Some(libc::ELOOP);
        assert_eq!(SysFs.read(&via).unwrap_err().raw_os_error(), loop_);
        assert_eq!(
            SysFs.write_access(&via, &b).unwrap_err().raw_os_error(),
            loop_
        );
        assert_eq!(
            SysFs
                .chown(&via, Some(1), Some(1))
                .unwrap_err()
                .raw_os_error(),
            loop_
        );
        assert!(SysFs.write_access(&share.join("sub"), &b).is_err());
        assert!(SysFs.read_dir(&share.join("sub")).is_err());
        assert_eq!(SysFs.read(&secret).unwrap(), before);
        // retarget_group skips the symlink rather than walking into it.
        crate::scan::retarget_group(&SysFs, &share, 30001, Some(30009)).unwrap();
        assert_eq!(SysFs.read(&secret).unwrap(), before);
    }
}
