//! Every read of the NAS goes through here (`annex.md` §2.1): paths are
//! relative to the annex's root, which is held open as a directory fd, and
//! each is resolved with `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
//! RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV)`. A symlink in any component
//! (the NAS's users can make them), a `..`, or a mount below the root never
//! takes a scan, a copy or an ACL read outside the annex. Entries of a
//! directory are stat'ed with `statx(dirfd, name, AT_SYMLINK_NOFOLLOW)`, one
//! component at a time, and anything on another device is not crossed.
//!
//! Off Linux (the unit tests on a developer's Mac) the same checks are made
//! by walking each component with `symlink_metadata`: slower, and racy, but
//! the same answers for a tree that holds still.

use std::ffi::OsString;
use std::fs::File;
use std::io;
use std::path::{Path, PathBuf};

use super::db::Seen;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    File,
    Dir,
    Symlink,
    /// A device, FIFO or socket, or a mount below the root: never read.
    Other,
}

/// One `statx` of a NAS path, never following a symlink.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Stat {
    pub kind: Kind,
    pub ino: u64,
    pub dev: u64,
    pub size: u64,
    pub mtime_ns: i64,
    pub ctime_ns: i64,
    pub btime_ns: Option<i64>,
    pub atime_ns: i64,
    pub nlink: u64,
    pub uid: u32,
    pub gid: u32,
    pub mode: u32,
}

impl Stat {
    pub fn seen(&self) -> Seen {
        Seen {
            ino: self.ino.to_string(),
            size: self.size,
            mtime_ns: self.mtime_ns,
            ctime_ns: self.ctime_ns,
            btime_ns: self.btime_ns,
            nlink: self.nlink,
        }
    }

    pub fn is_file(&self) -> bool {
        self.kind == Kind::File
    }

    pub fn is_dir(&self) -> bool {
        self.kind == Kind::Dir
    }
}

fn ns(secs: i64, nsec: i64) -> i64 {
    secs.saturating_mul(1_000_000_000).saturating_add(nsec)
}

/// `rel` is the root (`""`) or a plain relative path: `/`-separated names,
/// none empty, `.` or `..`.
pub fn check_rel(rel: &str) -> io::Result<()> {
    if rel.is_empty() {
        return Ok(());
    }
    if rel.contains('\0') {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "a NUL in a NAS path",
        ));
    }
    omnifile_stub::split_rel(Path::new(rel)).map(|_| ())
}

// mode_t is u32 on Linux and u16 on macOS: the casts are needed on one.
#[allow(clippy::unnecessary_cast)]
fn kind_of_mode(mode: u32) -> Kind {
    match mode & (libc::S_IFMT as u32) {
        m if m == libc::S_IFREG as u32 => Kind::File,
        m if m == libc::S_IFDIR as u32 => Kind::Dir,
        m if m == libc::S_IFLNK as u32 => Kind::Symlink,
        _ => Kind::Other,
    }
}

/// The annex's root on the mounted NAS.
pub struct Root {
    path: PathBuf,
    dev: u64,
    #[cfg(target_os = "linux")]
    fd: std::os::fd::OwnedFd,
}

/// One directory read: the directory's own stat, then each entry with its
/// stat (or why it couldn't be stat'ed).
pub type Listing = (Stat, Vec<(OsString, io::Result<Stat>)>);

#[cfg(target_os = "linux")]
mod imp {
    use super::*;
    use std::ffi::CString;
    use std::os::fd::{AsFd, AsRawFd, BorrowedFd, OwnedFd};
    use std::os::unix::ffi::OsStrExt;

    use omnifile_stub::sys::{RESOLVE_SAFE, openat2};

    pub fn statx(dir: BorrowedFd<'_>, name: &CString, flags: libc::c_int) -> io::Result<Stat> {
        // SAFETY: plain data; zero is a valid value.
        let mut st: libc::statx = unsafe { std::mem::zeroed() };
        // SAFETY: a valid fd, a NUL-terminated name and a statx to fill.
        let r = unsafe {
            libc::statx(
                dir.as_raw_fd(),
                name.as_ptr(),
                flags | libc::AT_SYMLINK_NOFOLLOW,
                libc::STATX_BASIC_STATS | libc::STATX_BTIME,
                &mut st,
            )
        };
        if r != 0 {
            return Err(io::Error::last_os_error());
        }
        let btime = (st.stx_mask & libc::STATX_BTIME != 0)
            .then(|| ns(st.stx_btime.tv_sec, st.stx_btime.tv_nsec as i64));
        Ok(Stat {
            kind: kind_of_mode(st.stx_mode as u32),
            ino: st.stx_ino,
            dev: libc::makedev(st.stx_dev_major, st.stx_dev_minor),
            size: st.stx_size,
            mtime_ns: ns(st.stx_mtime.tv_sec, st.stx_mtime.tv_nsec as i64),
            ctime_ns: ns(st.stx_ctime.tv_sec, st.stx_ctime.tv_nsec as i64),
            btime_ns: btime,
            atime_ns: ns(st.stx_atime.tv_sec, st.stx_atime.tv_nsec as i64),
            nlink: st.stx_nlink as u64,
            uid: st.stx_uid,
            gid: st.stx_gid,
            mode: st.stx_mode as u32,
        })
    }

    pub fn empty() -> CString {
        CString::new("").unwrap()
    }

    impl Root {
        /// A root from a directory already opened, so that what was checked on
        /// that fd is what the creates use.
        pub fn from_dir(f: File, path: PathBuf, dev: u64) -> io::Result<Root> {
            Ok(Root {
                path,
                dev,
                fd: std::os::fd::OwnedFd::from(f),
            })
        }

        pub fn open(path: &Path) -> io::Result<Root> {
            let fd = omnifile_stub::sys::open(path, libc::O_PATH | libc::O_DIRECTORY, 0)?;
            let st = statx(fd.as_fd(), &empty(), libc::AT_EMPTY_PATH)?;
            Ok(Root {
                path: path.to_owned(),
                dev: st.dev,
                fd,
            })
        }

        /// The same root, on its own fd (for a blocking task).
        pub fn try_clone(&self) -> io::Result<Root> {
            Ok(Root {
                path: self.path.clone(),
                dev: self.dev,
                fd: self.fd.try_clone()?,
            })
        }

        /// The directory `rel` beneath this root, as a root of its own.
        pub fn sub(&self, rel: &str) -> io::Result<Root> {
            if rel.is_empty() {
                return Root::open(&self.path);
            }
            let fd = self.at(rel, libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW)?;
            let st = statx(fd.as_fd(), &empty(), libc::AT_EMPTY_PATH)?;
            Ok(Root {
                path: self.path.join(rel),
                dev: st.dev,
                fd,
            })
        }

        fn at(&self, rel: &str, flags: libc::c_int) -> io::Result<OwnedFd> {
            check_rel(rel)?;
            let p = if rel.is_empty() { "." } else { rel };
            openat2(self.fd.as_fd(), Path::new(p), flags, 0, RESOLVE_SAFE)
        }

        pub fn stat(&self, rel: &str, sync: bool) -> io::Result<Stat> {
            let fd = self.at(rel, libc::O_PATH | libc::O_NOFOLLOW)?;
            let f = libc::AT_EMPTY_PATH | if sync { libc::AT_STATX_FORCE_SYNC } else { 0 };
            statx(fd.as_fd(), &empty(), f)
        }

        pub fn open_file(&self, rel: &str) -> io::Result<File> {
            // O_NONBLOCK: a FIFO swapped in after the stat never blocks the
            // open (and is refused below); it means nothing for a file.
            let fd = self.at(
                rel,
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_NOCTTY,
            )?;
            let st = statx(fd.as_fd(), &empty(), libc::AT_EMPTY_PATH)?;
            if st.kind != Kind::File {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("{rel} is not a regular file"),
                ));
            }
            Ok(File::from(fd))
        }

        /// A directory, opened for reading its ACL and free space (never
        /// followed, never on another device).
        pub fn open_dir(&self, rel: &str) -> io::Result<(File, Stat)> {
            let fd = self.at(rel, libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW)?;
            let st = statx(fd.as_fd(), &empty(), libc::AT_EMPTY_PATH)?;
            if st.kind != Kind::Dir || st.dev != self.dev {
                return Err(io::Error::from_raw_os_error(libc::ENOTDIR));
            }
            Ok((File::from(fd), st))
        }

        pub fn read_dir(&self, rel: &str) -> io::Result<Listing> {
            let dfd = self.at(rel, libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW)?;
            let me = statx(dfd.as_fd(), &empty(), libc::AT_EMPTY_PATH)?;
            let names = names(&dfd)?;
            let mut out = Vec::with_capacity(names.len());
            for name in names {
                let c = CString::new(name.as_bytes())?;
                let st = statx(dfd.as_fd(), &c, 0).map(|mut s| {
                    if s.dev != self.dev {
                        s.kind = Kind::Other; // another mount: not crossed
                    }
                    s
                });
                out.push((name, st));
            }
            Ok((me, out))
        }
    }

    /// The names in a directory (not `.` or `..`), from a dup of its fd.
    fn names(dfd: &OwnedFd) -> io::Result<Vec<OsString>> {
        use std::os::unix::ffi::OsStringExt;
        let dup = dfd.try_clone()?;
        // SAFETY: fdopendir takes ownership of a valid directory fd.
        let dir = unsafe { libc::fdopendir(dup.as_raw_fd()) };
        if dir.is_null() {
            return Err(io::Error::last_os_error());
        }
        std::mem::forget(dup); // closedir closes it
        // fdopendir shares the offset with dfd's file: start at the top.
        // SAFETY: dir is a valid DIR*.
        unsafe { libc::rewinddir(dir) };
        let mut out = Vec::new();
        let err = loop {
            // SAFETY: errno is thread-local; reset it to tell end from error.
            unsafe { *libc::__errno_location() = 0 };
            // SAFETY: dir is a valid DIR*.
            let ent = unsafe { libc::readdir(dir) };
            if ent.is_null() {
                let e = io::Error::last_os_error();
                break (e.raw_os_error() != Some(0)).then_some(e);
            }
            // SAFETY: d_name is NUL-terminated within the entry.
            let name = unsafe { std::ffi::CStr::from_ptr((*ent).d_name.as_ptr()) };
            let b = name.to_bytes();
            if b != b"." && b != b".." {
                out.push(OsString::from_vec(b.to_vec()));
            }
        };
        // SAFETY: dir is a valid DIR*, closed once.
        unsafe { libc::closedir(dir) };
        match err {
            Some(e) => Err(e),
            None => Ok(out),
        }
    }

    /// `fgetxattr`, or `None` if the file has no such attribute or the
    /// filesystem none at all.
    pub fn fgetxattr(f: &File, name: &str) -> io::Result<Option<Vec<u8>>> {
        let n = CString::new(name)?;
        let mut buf = vec![0u8; 4096];
        loop {
            // SAFETY: a valid fd, a NUL-terminated name, a buffer of buf.len().
            let r = unsafe {
                libc::fgetxattr(
                    f.as_raw_fd(),
                    n.as_ptr(),
                    buf.as_mut_ptr().cast(),
                    buf.len(),
                )
            };
            if r >= 0 {
                buf.truncate(r as usize);
                return Ok(Some(buf));
            }
            let e = io::Error::last_os_error();
            match e.raw_os_error() {
                Some(libc::ERANGE) if buf.len() < (1 << 20) => buf.resize(buf.len() * 4, 0),
                Some(libc::ENODATA) | Some(libc::EOPNOTSUPP) => return Ok(None),
                _ => return Err(e),
            }
        }
    }

    /// Whether the filesystem has this attribute for this file at all: it has a
    /// value, or says this file has none (`ENODATA`). Anything else isn't "yes".
    pub fn xattr_supported(f: &File, name: &str) -> bool {
        let Ok(n) = CString::new(name) else {
            return false;
        };
        // SAFETY: a valid fd, a NUL-terminated name, and no buffer: only its size.
        let r = unsafe { libc::fgetxattr(f.as_raw_fd(), n.as_ptr(), std::ptr::null_mut(), 0) };
        r >= 0 || io::Error::last_os_error().raw_os_error() == Some(libc::ENODATA)
    }

    /// Drop the file's cached pages, so the next read comes from the NAS.
    pub fn drop_cache(f: &File) {
        // SAFETY: a valid fd; advice only.
        unsafe { libc::posix_fadvise(f.as_raw_fd(), 0, 0, libc::POSIX_FADV_DONTNEED) };
    }

    /// A path naming this open file (its `/proc/self/fd` link), for a
    /// reader that opens by path: the open file, not a path resolved again.
    pub fn fd_path(f: &File) -> PathBuf {
        PathBuf::from(format!("/proc/self/fd/{}", f.as_raw_fd()))
    }
}

#[cfg(not(target_os = "linux"))]
mod imp {
    use super::*;
    use std::os::unix::fs::MetadataExt;

    fn stat_of(m: &std::fs::Metadata) -> Stat {
        let btime = m
            .created()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_nanos() as i64);
        Stat {
            kind: kind_of_mode(m.mode()),
            ino: m.ino(),
            dev: m.dev(),
            size: m.len(),
            mtime_ns: ns(m.mtime(), m.mtime_nsec()),
            ctime_ns: ns(m.ctime(), m.ctime_nsec()),
            btime_ns: btime,
            atime_ns: ns(m.atime(), m.atime_nsec()),
            nlink: m.nlink(),
            uid: m.uid(),
            gid: m.gid(),
            mode: m.mode(),
        }
    }

    impl Root {
        pub fn from_dir(_f: File, path: PathBuf, dev: u64) -> io::Result<Root> {
            Ok(Root { path, dev })
        }

        pub fn open(path: &Path) -> io::Result<Root> {
            let m = std::fs::metadata(path)?;
            if !m.is_dir() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "not a directory",
                ));
            }
            Ok(Root {
                path: path.to_owned(),
                dev: m.dev(),
            })
        }

        /// Every component of `rel` is a directory on the root's device,
        /// never a symlink; then the final one, not followed.
        fn walk(&self, rel: &str) -> io::Result<(PathBuf, Stat)> {
            check_rel(rel)?;
            let mut p = self.path.clone();
            let parts: Vec<&str> = if rel.is_empty() {
                vec![]
            } else {
                rel.split('/').collect()
            };
            for (i, part) in parts.iter().enumerate() {
                p.push(part);
                let s = stat_of(&std::fs::symlink_metadata(&p)?);
                let last = i + 1 == parts.len();
                if s.kind == Kind::Symlink && !last {
                    return Err(io::Error::from_raw_os_error(libc::ELOOP));
                }
                if s.dev != self.dev {
                    return Err(io::Error::from_raw_os_error(libc::EXDEV));
                }
                if !last && s.kind != Kind::Dir {
                    return Err(io::Error::from_raw_os_error(libc::ENOTDIR));
                }
            }
            let s = stat_of(&std::fs::symlink_metadata(&p)?);
            Ok((p, s))
        }

        pub fn try_clone(&self) -> io::Result<Root> {
            Ok(Root {
                path: self.path.clone(),
                dev: self.dev,
            })
        }

        pub fn sub(&self, rel: &str) -> io::Result<Root> {
            let (p, s) = self.walk(rel)?;
            if s.kind != Kind::Dir {
                return Err(io::Error::from_raw_os_error(libc::ENOTDIR));
            }
            Ok(Root {
                path: p,
                dev: s.dev,
            })
        }

        pub fn stat(&self, rel: &str, _sync: bool) -> io::Result<Stat> {
            self.walk(rel).map(|(_, s)| s)
        }

        pub fn open_file(&self, rel: &str) -> io::Result<File> {
            let (p, s) = self.walk(rel)?;
            if s.kind != Kind::File {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    format!("{rel} is not a regular file"),
                ));
            }
            File::open(p)
        }

        pub fn open_dir(&self, rel: &str) -> io::Result<(File, Stat)> {
            let (p, s) = self.walk(rel)?;
            if s.kind != Kind::Dir || s.dev != self.dev {
                return Err(io::Error::from_raw_os_error(libc::ENOTDIR));
            }
            Ok((File::open(p)?, s))
        }

        pub fn read_dir(&self, rel: &str) -> io::Result<Listing> {
            let (p, me) = self.walk(rel)?;
            if me.kind != Kind::Dir {
                return Err(io::Error::from_raw_os_error(libc::ENOTDIR));
            }
            let mut out = Vec::new();
            for item in std::fs::read_dir(&p)? {
                let item = item?;
                let st = std::fs::symlink_metadata(item.path()).map(|m| {
                    let mut s = stat_of(&m);
                    if s.dev != self.dev {
                        s.kind = Kind::Other;
                    }
                    s
                });
                out.push((item.file_name(), st));
            }
            Ok((me, out))
        }
    }

    pub fn fgetxattr(_f: &File, _name: &str) -> io::Result<Option<Vec<u8>>> {
        Ok(None)
    }

    pub fn xattr_supported(_f: &File, _name: &str) -> bool {
        false
    }

    pub fn drop_cache(_f: &File) {}

    pub fn fd_path(_f: &File) -> PathBuf {
        PathBuf::new()
    }
}

#[cfg(target_os = "linux")]
pub(crate) use imp::statx;
pub use imp::{drop_cache, fgetxattr, xattr_supported};

#[cfg(target_os = "linux")]
impl Root {
    /// The root's directory fd, for the remover's `*at` calls (W1-W4), the
    /// only code that writes through one.
    pub(crate) fn dir_fd(&self) -> std::os::fd::BorrowedFd<'_> {
        use std::os::fd::AsFd;
        self.fd.as_fd()
    }
}

/// The annex's root: `rel` (its `path`, checked plain and relative)
/// resolved beneath `base` (the mountpoint) without following a symlink.
pub fn open_root(base: &Path, rel: &str) -> io::Result<Root> {
    check_rel(rel)?;
    let b = Root::open(base)?;
    if rel.is_empty() { Ok(b) } else { b.sub(rel) }
}

impl Root {
    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn dev(&self) -> u64 {
        self.dev
    }

    /// A path a by-path reader (the uploader) can open `f` with: on Linux
    /// the open file itself; elsewhere `rel` under the root (tests only).
    pub fn source_path(&self, rel: &str, f: &File) -> PathBuf {
        let p = imp::fd_path(f);
        if p.as_os_str().is_empty() {
            self.path.join(rel)
        } else {
            p
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_must_be_plain_and_relative() {
        for bad in ["/etc", "..", "a/../b", "a/./b", "a//b", "a/", "./a", "a\0b"] {
            assert!(check_rel(bad).is_err(), "{bad:?}");
        }
        for good in ["", "a", "a/b.segy", "a b/c"] {
            assert!(check_rel(good).is_ok(), "{good:?}");
        }
    }

    #[test]
    fn a_symlink_in_any_component_is_never_followed() {
        let outside = tempfile::tempdir().unwrap();
        std::fs::write(outside.path().join("secret"), b"not the NAS's").unwrap();
        let t = tempfile::tempdir().unwrap();
        std::fs::create_dir(t.path().join("d")).unwrap();
        std::fs::write(t.path().join("d/f"), b"ok").unwrap();
        std::os::unix::fs::symlink(outside.path(), t.path().join("away")).unwrap();
        std::os::unix::fs::symlink(outside.path().join("secret"), t.path().join("d/link")).unwrap();
        let r = Root::open(t.path()).unwrap();
        assert!(r.open_file("d/f").is_ok());
        // Through a symlinked directory: refused.
        assert!(r.stat("away/secret", false).is_err());
        assert!(r.open_file("away/secret").is_err());
        assert!(r.read_dir("away").is_err());
        // A symlink as the last component: seen as a symlink, not opened.
        assert_eq!(r.stat("d/link", false).unwrap().kind, Kind::Symlink);
        assert!(r.open_file("d/link").is_err());
        assert!(r.open_file("../x").is_err());
        // The annex's own path is resolved the same way.
        assert!(open_root(t.path(), "d").is_ok());
        assert!(open_root(t.path(), "away").is_err());
        assert!(open_root(t.path(), "../x").is_err());
        let (me, items) = r.read_dir("d").unwrap();
        assert!(me.is_dir());
        let mut kinds: Vec<_> = items
            .iter()
            .map(|(n, s)| (n.to_string_lossy().into_owned(), s.as_ref().unwrap().kind))
            .collect();
        kinds.sort_by(|a, b| a.0.cmp(&b.0));
        assert_eq!(
            kinds,
            vec![("f".into(), Kind::File), ("link".into(), Kind::Symlink)]
        );
    }
}
