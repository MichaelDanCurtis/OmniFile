//! Creating namespace objects: placeholders (atomic, with their marker),
//! directories and symlinks. Each create is idempotent: an object that is
//! already there and is this object is adopted; anything else at the name is
//! a conflict and is never overwritten (the edge never renames or deletes
//! user data on its own, `edge-metadata.md` §7).

use std::io;
use std::os::fd::AsFd;
use std::path::Path;

use omnifile_marker::xattr::{self, ReadOutcome};
use omnifile_marker::{Flags, Marker};

use crate::{InodeKey, Layout, MarkerSeq, sys};

/// What a placeholder stands for.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlaceholderSpec {
    pub file_id: u64,
    /// Content version.
    pub version_id: u64,
    pub size: u64,
    pub share_id: u64,
    /// `ONLINE`, `ARCHIVED`, `READONLY`, `WORM`.
    pub flags: Flags,
    pub uid: u32,
    pub gid: u32,
    pub mode: u32,
    pub mtime_ns: i64,
}

impl PlaceholderSpec {
    /// The marker of a placeholder with nothing resident. An empty object
    /// is trivially resident.
    pub fn marker(&self, marker_seq: u64) -> Marker {
        let mut m = if self.size == 0 {
            let mut m = Marker::resident(self.file_id, self.version_id, 0, self.share_id);
            m.flags = self.flags;
            m
        } else {
            Marker::partial(
                self.file_id,
                self.version_id,
                self.size,
                0,
                self.share_id,
                self.flags,
            )
        };
        m.marker_seq = marker_seq;
        m
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Created {
    Created(InodeKey),
    /// Already there; `version_id` is the content version its marker names
    /// (`None` for directories and symlinks).
    Adopted {
        key: InodeKey,
        version_id: Option<u64>,
    },
    Conflict(String),
}

/// The staging directory, opened without following a symlink at any step
/// below the data root (`.omni`, then `staging`), created 0700 if missing.
fn staging_dir(layout: &Layout) -> io::Result<std::os::fd::OwnedFd> {
    sys::private_dir(&layout.staging)
}

/// The object at `at`, if it is the placeholder (or file) for `file_id`.
fn adopt_file(at: &sys::At, path: &Path, file_id: u64) -> io::Result<Created> {
    let fd = match at.open_ro() {
        Ok(fd) => fd,
        // A symlink at the name (O_NOFOLLOW).
        Err(e) if e.raw_os_error() == Some(libc::ELOOP) => {
            return Ok(Created::Conflict(format!(
                "{} exists and is not a regular file",
                path.display()
            )));
        }
        Err(e) => return Err(e),
    };
    let st = sys::fstat(fd.as_fd())?;
    if st.st_mode & libc::S_IFMT != libc::S_IFREG {
        return Ok(Created::Conflict(format!(
            "{} exists and is not a regular file",
            path.display()
        )));
    }
    match xattr::read(fd.as_fd())? {
        ReadOutcome::Marker(m) if m.file_id == file_id => {
            let mut key = sys::inode_key(fd.as_fd())?;
            key.file_id = file_id;
            Ok(Created::Adopted {
                key,
                version_id: Some(m.version_id),
            })
        }
        ReadOutcome::Marker(m) => Ok(Created::Conflict(format!(
            "{} holds file {} not {}",
            path.display(),
            m.file_id,
            file_id
        ))),
        ReadOutcome::Unmanaged => Ok(Created::Conflict(format!(
            "{} is a local unmanaged file",
            path.display()
        ))),
        ReadOutcome::Bad(e) => Ok(Created::Conflict(format!(
            "{} has a bad marker: {e}",
            path.display()
        ))),
    }
}

/// Create a placeholder at `rel` with its marker already set (R1 §5):
/// `O_TMPFILE` in the staging directory, size, marker, owner, mode, times,
/// `fsync`, then `linkat`. The name never exists without the marker. The
/// parent is resolved below the data volume without following symlinks.
pub fn create_placeholder(
    layout: &Layout,
    rel: &Path,
    spec: &PlaceholderSpec,
    seq: &MarkerSeq,
) -> io::Result<Created> {
    create_placeholder_with_seq(layout, rel, spec, seq.next())
}

/// [`create_placeholder`] with the new marker's `marker_seq` taken by the
/// caller (so the blocking work can run on another thread).
pub fn create_placeholder_with_seq(
    layout: &Layout,
    rel: &Path,
    spec: &PlaceholderSpec,
    marker_seq: u64,
) -> io::Result<Created> {
    create_placeholder_with_perms(layout, rel, spec, marker_seq, &NewPerms::Mode)
}

/// `system.posix_acl_access` / `system.posix_acl_default`.
pub const XATTR_ACL_ACCESS: &str = "system.posix_acl_access";
pub const XATTR_ACL_DEFAULT: &str = "system.posix_acl_default";

/// The permissions a new placeholder or directory has from the moment its
/// name exists (M2 review S4). A placeholder is linked in from the staging
/// directory and inherits nothing, and an fd opened on it survives a later
/// ACL change, so the right ACL must be on the inode before `linkat` (and
/// on a new directory before its mode is opened up).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NewPerms {
    /// The spec's mode alone: no catalog ACL, and the directory has no
    /// default ACL to inherit.
    Mode,
    /// Owner bits only, until the ACL sync can put the right ACL on it
    /// (the catalog ACL names a SID with no id here, or is not known yet).
    OwnerOnly,
    /// These ACLs (Linux xattr encoding). `default` is for directories; a
    /// directory gets no default ACL when it is `None`.
    Acl {
        access: Vec<u8>,
        default: Option<Vec<u8>>,
    },
}

/// The default ACL of `rel`'s directory (raw xattr), for [`NewPerms`]
/// decisions; `None` when it has none.
pub fn parent_default_acl(layout: &Layout, rel: &Path) -> io::Result<Option<Vec<u8>>> {
    let at = sys::resolve_beneath(&layout.data_root, rel)?;
    match xattr::get_raw(at.dir.as_fd(), XATTR_ACL_DEFAULT, 64 * 1024) {
        Ok(v) => Ok(v),
        Err(e) if e.raw_os_error() == Some(libc::EOPNOTSUPP) => Ok(None),
        Err(e) => Err(e),
    }
}

/// Owner, permissions and mtime of a new object, through its fd. The mode
/// never grants more than `perms` allows at any point: with an ACL, the
/// group and other bits stay closed until the ACL (which sets them) is on.
fn set_new_attrs_fd(
    fd: std::os::fd::BorrowedFd<'_>,
    uid: u32,
    gid: u32,
    mode: u32,
    mtime_ns: i64,
    perms: &NewPerms,
    is_dir: bool,
) -> io::Result<()> {
    use std::os::fd::AsRawFd;
    let raw = fd.as_raw_fd();
    let cvt = |r: libc::c_int| {
        if r < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    };
    let closed = (mode & 0o7000) | (mode & 0o700);
    // SAFETY: plain syscalls on a valid fd. fchown first: it clears the
    // set-id bits the chmod then sets.
    cvt(unsafe { libc::fchown(raw, uid, gid) })?;
    match perms {
        NewPerms::Mode => cvt(unsafe { libc::fchmod(raw, (mode & 0o7777) as libc::mode_t) })?,
        NewPerms::OwnerOnly => cvt(unsafe { libc::fchmod(raw, (mode & 0o700) as libc::mode_t) })?,
        NewPerms::Acl { access, default } => {
            cvt(unsafe { libc::fchmod(raw, closed as libc::mode_t) })?;
            if is_dir {
                match default {
                    Some(d) => xattr::set_raw(fd, XATTR_ACL_DEFAULT, d, 0)?,
                    None => match xattr::remove_raw(fd, XATTR_ACL_DEFAULT) {
                        Err(e) if e.raw_os_error() != Some(libc::ENODATA) => return Err(e),
                        _ => {}
                    },
                }
            }
            // Sets the mode's rwx bits from the ACL too.
            xattr::set_raw(fd, XATTR_ACL_ACCESS, access, 0)?;
        }
    }
    let times = [
        libc::timespec {
            tv_sec: 0,
            tv_nsec: libc::UTIME_OMIT,
        },
        libc::timespec {
            tv_sec: mtime_ns.div_euclid(1_000_000_000) as _,
            tv_nsec: mtime_ns.rem_euclid(1_000_000_000) as _,
        },
    ];
    // SAFETY: times has two entries.
    cvt(unsafe { libc::futimens(raw, times.as_ptr()) })
}

/// [`create_placeholder_with_seq`] with the new inode's permissions
/// ([`NewPerms`]) set on the `O_TMPFILE`, before its name exists.
pub fn create_placeholder_with_perms(
    layout: &Layout,
    rel: &Path,
    spec: &PlaceholderSpec,
    marker_seq: u64,
    perms: &NewPerms,
) -> io::Result<Created> {
    let path = layout.path(rel);
    let at = sys::resolve_beneath(&layout.data_root, rel)?;
    match at.lstat() {
        Ok(_) => return adopt_file(&at, &path, spec.file_id),
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    let staging = staging_dir(layout)?;
    let f = sys::tmpfile_in(staging.as_fd(), 0o600)?;
    f.set_len(spec.size)?;
    xattr::write(f.as_fd(), &spec.marker(marker_seq)).map_err(|e| match e {
        xattr::Error::Io(e) => e,
        xattr::Error::Marker(e) => io::Error::new(io::ErrorKind::InvalidData, e.to_string()),
    })?;
    // Permissions before the name exists: an fd opened through the name
    // could otherwise outlive a later, stricter ACL (S4).
    set_new_attrs_fd(
        f.as_fd(),
        spec.uid,
        spec.gid,
        spec.mode,
        spec.mtime_ns,
        perms,
        false,
    )?;
    f.sync_all()?;
    match at.link_tmpfile(&f) {
        Ok(()) => {}
        Err(e) if e.raw_os_error() == Some(libc::EEXIST) => {
            return adopt_file(&at, &path, spec.file_id);
        }
        Err(e) => return Err(e),
    }
    let mut key = sys::inode_key(f.as_fd())?;
    key.file_id = spec.file_id;
    Ok(Created::Created(key))
}

/// Create (or adopt) a directory. An existing name is adopted only if it is
/// a directory itself (never a symlink to one); owner, mode and times are
/// then set through an fd on it.
pub fn create_dir(
    layout: &Layout,
    rel: &Path,
    file_id: u64,
    uid: u32,
    gid: u32,
    mode: u32,
    mtime_ns: i64,
) -> io::Result<Created> {
    create_dir_with_perms(
        layout,
        rel,
        file_id,
        uid,
        gid,
        mode,
        mtime_ns,
        &NewPerms::Mode,
    )
}

/// [`create_dir`] with a new directory's permissions ([`NewPerms`]) set
/// before its mode is opened up (it is created 0700). An adopted directory
/// only gets owner, mode and times, as before.
#[allow(clippy::too_many_arguments)]
pub fn create_dir_with_perms(
    layout: &Layout,
    rel: &Path,
    file_id: u64,
    uid: u32,
    gid: u32,
    mode: u32,
    mtime_ns: i64,
    perms: &NewPerms,
) -> io::Result<Created> {
    let path = layout.path(rel);
    let at = sys::resolve_beneath(&layout.data_root, rel)?;
    let created = match at.mkdir(0o700) {
        Ok(()) => true,
        Err(e) if e.raw_os_error() == Some(libc::EEXIST) => false,
        Err(e) => return Err(e),
    };
    // O_DIRECTORY | O_NOFOLLOW: a symlink or a file at the name fails here.
    let fd = match at.open_dir() {
        Ok(fd) => fd,
        Err(e) if !created && matches!(e.raw_os_error(), Some(libc::ELOOP | libc::ENOTDIR)) => {
            return Ok(Created::Conflict(format!(
                "{} exists and is not a directory",
                path.display()
            )));
        }
        Err(e) => return Err(e),
    };
    if created {
        set_new_attrs_fd(fd.as_fd(), uid, gid, mode, mtime_ns, perms, true)?;
    } else {
        sys::set_attrs_fd(fd.as_fd(), uid, gid, mode, mtime_ns)?;
    }
    let mut key = sys::inode_key(fd.as_fd())?;
    key.file_id = file_id;
    Ok(if created {
        Created::Created(key)
    } else {
        Created::Adopted {
            key,
            version_id: None,
        }
    })
}

/// Set an existing directory's owner, mode and permissions again, as
/// `create_dir_with_perms` sets a new one's (an ACL replaces the old one).
pub fn reset_dir_perms(
    layout: &Layout,
    rel: &Path,
    uid: u32,
    gid: u32,
    mode: u32,
    perms: &NewPerms,
) -> io::Result<()> {
    let fd = sys::open_dir_beneath(&layout.data_root, rel)?;
    set_new_attrs_fd(fd.as_fd(), uid, gid, mode, 0, perms, true)
}

/// Create (or adopt) a symlink.
pub fn create_symlink(
    layout: &Layout,
    rel: &Path,
    file_id: u64,
    target: &str,
    uid: u32,
    gid: u32,
    mtime_ns: i64,
) -> io::Result<Created> {
    let path = layout.path(rel);
    let at = sys::resolve_beneath(&layout.data_root, rel)?;
    let created = match at.symlink(target) {
        Ok(()) => true,
        Err(e) if e.raw_os_error() == Some(libc::EEXIST) => match at.readlink() {
            Ok(t) if t.as_os_str() == target => false,
            _ => {
                return Ok(Created::Conflict(format!(
                    "{} exists and is not this symlink",
                    path.display()
                )));
            }
        },
        Err(e) => return Err(e),
    };
    let p = at.open_path()?;
    let st = sys::fstat(p.as_fd())?;
    if st.st_mode & libc::S_IFMT != libc::S_IFLNK {
        return Ok(Created::Conflict(format!(
            "{} was replaced while being created",
            path.display()
        )));
    }
    sys::set_attrs_opath(p.as_fd(), uid, gid, None, mtime_ns)?;
    let sf = sys::fstatfs(p.as_fd())?;
    let key = InodeKey {
        fsid: sys::fsid_of(&sf),
        ino: st.st_ino,
        ino_gen: 0,
        file_id,
    };
    Ok(if created {
        Created::Created(key)
    } else {
        Created::Adopted {
            key,
            version_id: None,
        }
    })
}

/// `trusted.*` xattr on every recall note the daemon creates. Users cannot
/// set `trusted.*` (it needs `CAP_SYS_ADMIN`), so it tells the daemon's
/// own notes from user files that merely share the name.
pub const NOTE_XATTR: &str = "trusted.omnifile.note";

/// What [`write_note`] did.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NoteOutcome {
    Created,
    /// The daemon's earlier note at the name was rewritten.
    Rewritten,
    /// Something that is not the daemon's note holds the name; left alone.
    Foreign,
}

enum Existing {
    None,
    Ours(std::fs::File),
    Foreign,
}

/// The object at `at`, opened for writing only if it is a regular file
/// carrying [`NOTE_XATTR`]. The check and the open are on one inode: an
/// `O_PATH` fd taken without following, then reopened through `/proc`.
fn existing_note(at: &sys::At) -> io::Result<Existing> {
    let p = match at.open_path() {
        Ok(p) => p,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Existing::None),
        Err(e) => return Err(e),
    };
    if sys::fstat(p.as_fd())?.st_mode & libc::S_IFMT != libc::S_IFREG {
        return Ok(Existing::Foreign);
    }
    let fd = sys::reopen(p.as_fd(), libc::O_RDWR | libc::O_NONBLOCK)?;
    if !is_note_fd(fd.as_fd())? {
        return Ok(Existing::Foreign);
    }
    Ok(Existing::Ours(std::fs::File::from(fd)))
}

fn is_note_fd(fd: std::os::fd::BorrowedFd<'_>) -> io::Result<bool> {
    Ok(xattr::get_raw(fd, NOTE_XATTR, 8)?.is_some())
}

/// True when `fd` (named `name`) is a recall note the daemon wrote: the
/// suffix and the daemon's `trusted.*` mark. A user's own file named
/// `*.RECALLING.txt` is not one, and is committed like any other.
pub fn is_note(fd: std::os::fd::BorrowedFd<'_>, name: &std::ffi::OsStr) -> bool {
    name.as_encoded_bytes()
        .ends_with(crate::NOTE_SUFFIX.as_bytes())
        && is_note_fd(fd).unwrap_or(false)
}

fn note_rel(rel: &Path) -> io::Result<std::path::PathBuf> {
    let (parent, name) = crate::split_rel(rel)?;
    Ok(parent.join(crate::note_name(name)))
}

/// Write `<file>.RECALLING.txt` next to the file at `rel` (B1). Never
/// follows a symlink, never writes into an inode it did not create: the
/// parent is resolved below the data volume without symlinks; a new note is
/// an `O_TMPFILE` (marked with [`NOTE_XATTR`], owned by `uid:gid`, 0644)
/// linked in with `linkat`, which never replaces; an existing name is
/// rewritten only if it is the daemon's own note.
pub fn write_note(
    layout: &Layout,
    rel: &Path,
    text: &str,
    uid: u32,
    gid: u32,
) -> io::Result<NoteOutcome> {
    use std::io::Write;
    let at = sys::resolve_beneath(&layout.data_root, &note_rel(rel)?)?;
    let rewrite = |mut f: std::fs::File| -> io::Result<NoteOutcome> {
        f.set_len(0)?;
        f.write_all(text.as_bytes())?;
        Ok(NoteOutcome::Rewritten)
    };
    match existing_note(&at)? {
        Existing::Ours(f) => return rewrite(f),
        Existing::Foreign => return Ok(NoteOutcome::Foreign),
        Existing::None => {}
    }
    let mut f = sys::tmpfile_in(at.dir.as_fd(), 0o600)?;
    f.write_all(text.as_bytes())?;
    xattr::set_raw(f.as_fd(), NOTE_XATTR, b"1", 0)?;
    // SAFETY: plain syscalls on a valid fd.
    let fd = std::os::fd::AsRawFd::as_raw_fd(&f);
    if unsafe { libc::fchown(fd, uid, gid) } < 0 || unsafe { libc::fchmod(fd, 0o644) } < 0 {
        return Err(io::Error::last_os_error());
    }
    match at.link_tmpfile(&f) {
        Ok(()) => Ok(NoteOutcome::Created),
        // Someone took the name meanwhile.
        Err(e) if e.raw_os_error() == Some(libc::EEXIST) => match existing_note(&at)? {
            Existing::Ours(f) => rewrite(f),
            _ => Ok(NoteOutcome::Foreign),
        },
        Err(e) => Err(e),
    }
}

/// Remove the note next to the file at `rel`, if it is the daemon's own.
/// Returns whether one was removed.
pub fn remove_note(layout: &Layout, rel: &Path) -> io::Result<bool> {
    let at = sys::resolve_beneath(&layout.data_root, &note_rel(rel)?)?;
    match existing_note(&at)? {
        Existing::Ours(f) => {
            // The name could be swapped between the check and the unlink,
            // but only by someone who may write this directory anyway, and
            // unlink never follows or writes through the name.
            let want = sys::fstat(f.as_fd())?.st_ino;
            if at.lstat()?.st_ino != want {
                return Ok(false);
            }
            at.unlink()?;
            Ok(true)
        }
        _ => Ok(false),
    }
}
