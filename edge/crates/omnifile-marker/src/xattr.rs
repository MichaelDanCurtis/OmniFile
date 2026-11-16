//! Reading and writing the marker on an open file descriptor (Linux).
//!
//! Always on an `O_RDONLY` or `O_PATH` fd: never `O_RDWR`, which fails with
//! `EPERM` on an immutable inode (the spike's `chattr +i` bug). xattr calls
//! need no write access to the file. `O_PATH` fds don't support
//! `fgetxattr`/`fsetxattr` (`EBADF`), so for those the call goes through the
//! fd's `/proc/self/fd/<n>` magic link, which still names the same inode.
//!
//! `fsetxattr` replaces the whole value in one call, so a marker update is
//! atomic: a reader sees either the old marker or the new one.

use std::ffi::CString;
use std::io;
use std::os::fd::{AsRawFd, BorrowedFd};

use crate::{ChunkMap, MAP_XATTR_NAME, MARKER_LEN, Marker, MarkerError, XATTR_NAME};

/// What reading a marker found.
#[derive(Debug)]
pub enum ReadOutcome {
    /// No marker: an ordinary, unmanaged local file.
    Unmanaged,
    Marker(Marker),
    /// A marker exists but is invalid: fail closed, report `BAD_MARKER`.
    Bad(MarkerError),
}

#[derive(Debug)]
pub enum Error {
    Io(io::Error),
    Marker(MarkerError),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Io(e) => write!(f, "marker xattr: {e}"),
            Error::Marker(e) => write!(f, "marker: {e}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self {
        Error::Io(e)
    }
}

impl From<MarkerError> for Error {
    fn from(e: MarkerError) -> Self {
        Error::Marker(e)
    }
}

fn cname(name: &str) -> CString {
    CString::new(name).expect("xattr names have no NUL")
}

fn proc_path(fd: BorrowedFd<'_>) -> CString {
    CString::new(format!("/proc/self/fd/{}", fd.as_raw_fd())).expect("no NUL")
}

/// Raw `fgetxattr` with an `O_PATH` fallback. `Ok(None)` when absent.
pub fn get_raw(fd: BorrowedFd<'_>, name: &str, cap: usize) -> io::Result<Option<Vec<u8>>> {
    let name = cname(name);
    let mut buf = vec![0u8; cap];
    // SAFETY: buf is valid for cap bytes; name is NUL-terminated.
    let mut n = unsafe {
        libc::fgetxattr(
            fd.as_raw_fd(),
            name.as_ptr(),
            buf.as_mut_ptr().cast(),
            buf.len(),
        )
    };
    if n < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::EBADF) {
        let p = proc_path(fd);
        // SAFETY: as above.
        n = unsafe {
            libc::getxattr(
                p.as_ptr(),
                name.as_ptr(),
                buf.as_mut_ptr().cast(),
                buf.len(),
            )
        };
    }
    if n < 0 {
        let e = io::Error::last_os_error();
        return match e.raw_os_error() {
            Some(libc::ENODATA) => Ok(None),
            // A value larger than `cap` is not a valid marker; report its
            // existence as a too-long value so the caller fails closed.
            Some(libc::ERANGE) => Ok(Some(vec![0u8; cap + 1])),
            _ => Err(e),
        };
    }
    buf.truncate(n as usize);
    Ok(Some(buf))
}

/// Raw `fsetxattr` with an `O_PATH` fallback. `flags` is 0, `XATTR_CREATE`
/// or `XATTR_REPLACE`.
pub fn set_raw(fd: BorrowedFd<'_>, name: &str, value: &[u8], flags: i32) -> io::Result<()> {
    let name = cname(name);
    // SAFETY: value is valid for its length; name is NUL-terminated.
    let mut r = unsafe {
        libc::fsetxattr(
            fd.as_raw_fd(),
            name.as_ptr(),
            value.as_ptr().cast(),
            value.len(),
            flags,
        )
    };
    if r < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::EBADF) {
        let p = proc_path(fd);
        // SAFETY: as above.
        r = unsafe {
            libc::setxattr(
                p.as_ptr(),
                name.as_ptr(),
                value.as_ptr().cast(),
                value.len(),
                flags,
            )
        };
    }
    if r < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

/// Raw `fremovexattr` with an `O_PATH` fallback; absent is not an error.
pub fn remove_raw(fd: BorrowedFd<'_>, name: &str) -> io::Result<()> {
    let name = cname(name);
    // SAFETY: name is NUL-terminated.
    let mut r = unsafe { libc::fremovexattr(fd.as_raw_fd(), name.as_ptr()) };
    if r < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::EBADF) {
        let p = proc_path(fd);
        // SAFETY: as above.
        r = unsafe { libc::removexattr(p.as_ptr(), name.as_ptr()) };
    }
    if r < 0 {
        let e = io::Error::last_os_error();
        if e.raw_os_error() == Some(libc::ENODATA) {
            return Ok(());
        }
        return Err(e);
    }
    Ok(())
}

/// Read and validate the marker. I/O errors other than "no such attribute"
/// are returned as errors; the caller fails closed on them too.
pub fn read(fd: BorrowedFd<'_>) -> io::Result<ReadOutcome> {
    match get_raw(fd, XATTR_NAME, MARKER_LEN)? {
        None => Ok(ReadOutcome::Unmanaged),
        Some(b) => Ok(match Marker::decode(&b) {
            Ok(m) => ReadOutcome::Marker(m),
            Err(e) => ReadOutcome::Bad(e),
        }),
    }
}

/// Read the marker, requiring it to exist and be valid.
pub fn read_required(fd: BorrowedFd<'_>) -> Result<Marker, Error> {
    match read(fd)? {
        ReadOutcome::Marker(m) => Ok(m),
        ReadOutcome::Unmanaged => Err(Error::Io(io::Error::from_raw_os_error(libc::ENODATA))),
        ReadOutcome::Bad(e) => Err(Error::Marker(e)),
    }
}

/// Write the marker (create or replace) in one `fsetxattr`.
pub fn write(fd: BorrowedFd<'_>, m: &Marker) -> Result<(), Error> {
    let b = m.encode()?;
    set_raw(fd, XATTR_NAME, &b, 0)?;
    sync_offline(fd, crate::dosattr::offline(m));
    Ok(())
}

/// The SMB offline bit follows the marker (`dosattr`). Advisory: a failure
/// never fails the marker write.
fn sync_offline(fd: BorrowedFd<'_>, offline: bool) {
    let _ = crate::dosattr::sync(fd, offline);
}

/// Replace an existing marker; fails with `ENODATA` if there is none. Used
/// to widen: a marker someone removed (an NFS truncate to 0) is never
/// recreated over the user's new content.
pub fn write_replace(fd: BorrowedFd<'_>, m: &Marker) -> Result<(), Error> {
    let b = m.encode()?;
    set_raw(fd, XATTR_NAME, &b, libc::XATTR_REPLACE)?;
    sync_offline(fd, crate::dosattr::offline(m));
    Ok(())
}

/// Remove the marker. Only after the file was truncated to 0 (R1 §5).
pub fn remove(fd: BorrowedFd<'_>) -> io::Result<()> {
    remove_raw(fd, XATTR_NAME)?;
    sync_offline(fd, false);
    Ok(())
}

/// Read the chunk map for a marker that has `CHUNK_MAP`.
pub fn read_map(fd: BorrowedFd<'_>, m: &Marker) -> Result<Option<ChunkMap>, Error> {
    let want = ChunkMap::chunk_count(m.size, m.chunk_shift).div_ceil(8) as usize;
    match get_raw(fd, MAP_XATTR_NAME, want.max(1))? {
        None => Ok(None),
        Some(b) => Ok(Some(ChunkMap::decode(&b, m.size, m.chunk_shift)?)),
    }
}

/// Write the chunk map.
pub fn write_map(fd: BorrowedFd<'_>, map: &ChunkMap) -> io::Result<()> {
    set_raw(fd, MAP_XATTR_NAME, map.encode(), 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Flags, State};
    use std::fs::OpenOptions;
    use std::os::fd::AsFd;
    use std::os::unix::fs::OpenOptionsExt;

    /// Tests run as root on the dev VM's xfs (`OMNIFILE_TEST_DIR`, default
    /// `/srv/omnifile/data/test`). Without root (trusted.* needs
    /// CAP_SYS_ADMIN) they are skipped with a note.
    fn test_dir() -> Option<tempfile::TempDir> {
        // SAFETY: geteuid has no preconditions.
        if unsafe { libc::geteuid() } != 0 {
            eprintln!("skipping: trusted.* xattrs need root");
            return None;
        }
        let base =
            std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/omnifile/data/test".into());
        std::fs::create_dir_all(&base).ok()?;
        tempfile::tempdir_in(base).ok()
    }

    #[test]
    fn the_smb_offline_bit_follows_the_marker() {
        use crate::dosattr::{self, NAME};
        let Some(dir) = test_dir() else { return };
        let attrib = |fd: BorrowedFd<'_>| {
            get_raw(fd, NAME, 256)
                .unwrap()
                .map(|v| dosattr::attrib(&v).unwrap())
        };

        // A placeholder (nothing local) is offline; filled, it isn't.
        let p = dir.path().join("stub");
        std::fs::write(&p, b"").unwrap();
        let f = OpenOptions::new().read(true).open(&p).unwrap();
        let mut m = Marker::partial(9, 10, 4096, 0, 1, Flags::ONLINE);
        write(f.as_fd(), &m).unwrap();
        assert_eq!(attrib(f.as_fd()), Some(0x1020));
        m.state = State::Resident;
        m.resident_bytes = 4096;
        write_replace(f.as_fd(), &m).unwrap();
        assert_eq!(attrib(f.as_fd()), Some(0x20));

        // What Samba stored (hidden, a creation time) survives both ways.
        let samba: Vec<u8> = [
            0x00, 0x00, 0x05, 0x00, 0x05, 0x00, 0x00, 0x00, 0x11, 0x00, 0x00, 0x00, 0x02, 0x00,
            0x00, 0x00, 0xf7, 0x7d, 0x67, 0xb2, 0x41, 0x5d, 0xdd, 0x01,
        ]
        .to_vec();
        set_raw(f.as_fd(), NAME, &samba, 0).unwrap();
        m.state = State::Partial;
        m.resident_bytes = 1024;
        write(f.as_fd(), &m).unwrap();
        let v = get_raw(f.as_fd(), NAME, 256).unwrap().unwrap();
        assert_eq!(dosattr::attrib(&v), Some(0x1002));
        assert_eq!(v[16..], samba[16..]);
        // Truncated to 0 by a user: the marker goes, and so does the bit.
        remove(f.as_fd()).unwrap();
        assert_eq!(get_raw(f.as_fd(), NAME, 256).unwrap().unwrap(), samba);

        // A resident file never gains the xattr, and directories are left alone.
        let r = dir.path().join("resident");
        std::fs::write(&r, vec![1u8; 4096]).unwrap();
        let rf = OpenOptions::new().read(true).open(&r).unwrap();
        write(rf.as_fd(), &Marker::resident(9, 11, 4096, 1)).unwrap();
        assert_eq!(attrib(rf.as_fd()), None);
        let d = OpenOptions::new().read(true).open(dir.path()).unwrap();
        write(d.as_fd(), &Marker::partial(9, 12, 0, 0, 1, Flags::ONLINE)).unwrap();
        assert_eq!(attrib(d.as_fd()), None);

        // Turned off, the marker is written and DOS attributes are not touched.
        dosattr::set_enabled(false);
        let o = dir.path().join("off");
        std::fs::write(&o, b"").unwrap();
        let of = OpenOptions::new().read(true).open(&o).unwrap();
        let w = write(
            of.as_fd(),
            &Marker::partial(9, 13, 4096, 0, 1, Flags::ONLINE),
        );
        // ...and a bit set while it was on is cleared, never left behind.
        let o2 = dir.path().join("was-offline");
        std::fs::write(&o2, b"").unwrap();
        let of2 = OpenOptions::new().read(true).open(&o2).unwrap();
        set_raw(of2.as_fd(), NAME, &dosattr::fresh(0x1020), 0).unwrap();
        let w2 = write(
            of2.as_fd(),
            &Marker::partial(9, 14, 4096, 0, 1, Flags::ONLINE),
        );
        dosattr::set_enabled(true);
        w.unwrap();
        w2.unwrap();
        assert_eq!(attrib(of.as_fd()), None);
        assert_eq!(attrib(of2.as_fd()), Some(0x20));
    }

    #[test]
    fn roundtrip_on_rdonly_and_opath() {
        let Some(dir) = test_dir() else { return };
        let p = dir.path().join("f");
        std::fs::write(&p, vec![7u8; 4096]).unwrap();
        let ro = OpenOptions::new().read(true).open(&p).unwrap();
        assert!(matches!(read(ro.as_fd()).unwrap(), ReadOutcome::Unmanaged));
        let m = Marker::partial(9, 10, 4096, 1024, 1, Flags::ONLINE);
        write(ro.as_fd(), &m).unwrap();
        assert_eq!(read_required(ro.as_fd()).unwrap(), m);

        let opath = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_PATH)
            .open(&p)
            .unwrap();
        let mut m2 = m;
        m2.state = State::Resident;
        m2.resident_bytes = 4096;
        m2.marker_seq = 2;
        write(opath.as_fd(), &m2).unwrap();
        assert_eq!(read_required(opath.as_fd()).unwrap(), m2);
        assert_eq!(read_required(ro.as_fd()).unwrap(), m2);

        set_raw(ro.as_fd(), XATTR_NAME, b"short", 0).unwrap();
        assert!(matches!(read(ro.as_fd()).unwrap(), ReadOutcome::Bad(_)));
        set_raw(ro.as_fd(), XATTR_NAME, &[0u8; 200], 0).unwrap();
        match read(ro.as_fd()).unwrap() {
            ReadOutcome::Bad(e) => assert_eq!(e.bad_marker_reason(), 3),
            other => panic!("oversized marker accepted: {other:?}"),
        }
        remove(ro.as_fd()).unwrap();
        remove(ro.as_fd()).unwrap();
        let e = write_replace(ro.as_fd(), &m).unwrap_err();
        assert!(
            matches!(e, Error::Io(ref io) if io.raw_os_error() == Some(libc::ENODATA)),
            "{e}"
        );
        assert!(
            matches!(read(ro.as_fd()).unwrap(), ReadOutcome::Unmanaged),
            "never recreated"
        );
        assert!(matches!(read(ro.as_fd()).unwrap(), ReadOutcome::Unmanaged));
    }

    #[test]
    fn chunk_map_xattr() {
        let Some(dir) = test_dir() else { return };
        let p = dir.path().join("m");
        let f = std::fs::File::create(&p).unwrap();
        let size = 100u64 << 20;
        f.set_len(size).unwrap();
        let mut m = Marker::partial(1, 2, size, 0, 1, Flags::ONLINE | Flags::CHUNK_MAP);
        m.chunk_shift = crate::DEFAULT_CHUNK_SHIFT;
        let mut map = ChunkMap::empty(size, m.chunk_shift);
        map.set(3);
        write(f.as_fd(), &m).unwrap();
        write_map(f.as_fd(), &map).unwrap();
        assert_eq!(read_map(f.as_fd(), &m).unwrap(), Some(map));
    }
}
