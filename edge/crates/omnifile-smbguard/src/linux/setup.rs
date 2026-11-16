//! Startup: prove the volume, set up the private fill mount, take the group
//! back from the fd store or create it, and arm the marks.

use std::os::fd::{AsFd, AsRawFd, OwnedFd, RawFd};
use std::path::Path;

use tracing::{info, warn};

use super::sys;
use crate::api::{FillMount, GuardConfig, format_uuid};
use crate::fdinfo;
use crate::sdnotify;
use crate::uapi::*;

#[derive(Debug, thiserror::Error)]
pub enum GuardError {
    #[error("refusing to arm: {0}")]
    Volume(String),
    #[error("refusing to start: fill mount {path}: {why}")]
    FillMount { path: String, why: String },
    #[error(
        "refusing to create a fanotify group that would die with this process: {0} \
         (run under the omnifile unit, or set require_fdstore = false for tests)"
    )]
    NoFdStore(String),
    #[error(
        "the group taken from the fd store is not usable ({0}); it is kept (releasing it \
         would let pending reads through). Stop smbd and Ganesha, then: omnifile-guardctl clean --force"
    )]
    StoredGroup(String),
    #[error("{what}: {source}")]
    Sys {
        what: &'static str,
        source: std::io::Error,
    },
}

fn sys_err(what: &'static str) -> impl FnOnce(std::io::Error) -> GuardError {
    move |source| GuardError::Sys { what, source }
}

/// The verified data volume.
pub struct Volume {
    pub data_fd: OwnedFd,
    pub dev: u64,
    /// `dev` in the kernel encoding fdinfo prints.
    pub kdev: u32,
    pub root_ino: u64,
    pub mnt_id: u32,
}

/// The data root must be the root of an xfs filesystem whose UUID is the
/// recorded one, and not the root filesystem. An unmounted mount point
/// would otherwise make `FAN_MARK_FILESYSTEM` mark `/` (spike).
pub fn check_volume(cfg: &GuardConfig) -> Result<Volume, GuardError> {
    let p = &cfg.data_root;
    if p == Path::new("/") {
        return Err(GuardError::Volume("the data root is /".into()));
    }
    let fd = sys::open_dir(p).map_err(sys_err("open data root"))?;
    let st = sys::fstat(fd.as_fd()).map_err(sys_err("stat data root"))?;
    let ft = sys::fstatfs_type(fd.as_fd()).map_err(sys_err("statfs data root"))?;
    if ft != sys::XFS_SUPER_MAGIC {
        return Err(GuardError::Volume(format!(
            "{} is not xfs (f_type {ft:#x})",
            p.display()
        )));
    }
    let root = sys::open_dir(Path::new("/")).map_err(sys_err("open /"))?;
    if sys::fstat(root.as_fd()).map_err(sys_err("stat /"))?.st_dev == st.st_dev {
        return Err(GuardError::Volume(format!(
            "{} is on the root filesystem (not mounted?)",
            p.display()
        )));
    }
    let parent = sys::open_dir(&p.join("..")).map_err(sys_err("open data root parent"))?;
    let pst = sys::fstat(parent.as_fd()).map_err(sys_err("stat data root parent"))?;
    let mnt_id = fdinfo::mnt_id(&sys::fdinfo(fd.as_raw_fd()).map_err(sys_err("fdinfo"))?)
        .ok_or_else(|| GuardError::Volume("no mnt_id in fdinfo".into()))?;
    let parent_mnt = fdinfo::mnt_id(&sys::fdinfo(parent.as_raw_fd()).map_err(sys_err("fdinfo"))?);
    if pst.st_dev == st.st_dev && parent_mnt == Some(mnt_id) {
        return Err(GuardError::Volume(format!(
            "{} is not a mount point",
            p.display()
        )));
    }
    let uuid = sys::fs_uuid(fd.as_fd()).map_err(sys_err("FS_IOC_GETFSUUID"))?;
    if uuid != cfg.fs_uuid {
        return Err(GuardError::Volume(format!(
            "{} has filesystem UUID {}, expected {}",
            p.display(),
            format_uuid(&uuid),
            format_uuid(&cfg.fs_uuid)
        )));
    }
    let (maj, min) = sys::dev_major_minor(st.st_dev);
    info!(
        data_root = %p.display(),
        uuid = %format_uuid(&uuid),
        dev = format!("{maj}:{min}"),
        "data volume verified (xfs, mount point, UUID)"
    );
    Ok(Volume {
        data_fd: fd,
        dev: st.st_dev,
        kdev: fdinfo::kernel_dev(maj, min),
        root_ino: st.st_ino,
        mnt_id,
    })
}

/// The verified fill mount: a bind of the volume's root, as a separate
/// mount object, in a mount namespace no other process shares.
pub struct Fill {
    pub fd: OwnedFd,
    pub mnt_id: u32,
}

pub fn setup_fill_mount(cfg: &GuardConfig, vol: &Volume) -> Result<Fill, GuardError> {
    let path = cfg.fill_mount.path();
    let fail = |why: String| GuardError::FillMount {
        path: path.display().to_string(),
        why,
    };
    if let FillMount::PrivateBind(p) = &cfg.fill_mount {
        sys::unshare_and_bind(&cfg.data_root, p)
            .map_err(|e| fail(format!("unshare + bind: {e}")))?;
    }
    let fd = sys::open_dir(path).map_err(|e| fail(e.to_string()))?;
    let st = sys::fstat(fd.as_fd()).map_err(|e| fail(e.to_string()))?;
    if st.st_dev != vol.dev || st.st_ino != vol.root_ino {
        return Err(fail("not a bind mount of the data volume's root".into()));
    }
    let mnt_id = fdinfo::mnt_id(&sys::fdinfo(fd.as_raw_fd()).map_err(|e| fail(e.to_string()))?)
        .ok_or_else(|| fail("no mnt_id".into()))?;
    // The ignore mark goes on this mount object. If it were the share's own
    // mount, every SMB read would skip the guard.
    if mnt_id == vol.mnt_id {
        return Err(fail(
            "it is the data root's own mount, not a separate bind".into(),
        ));
    }
    let mine = sys::mount_ns("self").map_err(|e| fail(e.to_string()))?;
    let host = "this process shares the host mount namespace, so other processes could read \
                through the fill mount unguarded (use PrivateMounts=yes, or FillMount::PrivateBind)";
    match sys::mount_ns("1") {
        Ok(init) if init == mine => return Err(fail(host.into())),
        Ok(_) => {}
        // A confined unit (no CAP_SYS_PTRACE: the daemon's bounding set)
        // cannot inspect PID 1. Then the namespace must show it is private:
        // systemd's PrivateMounts= makes `/` a slave ("master:") in the
        // unit's namespace, while the host's `/` is "shared:" only.
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
            let mi = std::fs::read_to_string("/proc/self/mountinfo")
                .map_err(|e| fail(format!("mountinfo: {e}")))?;
            if !root_is_private(&mi) {
                return Err(fail(host.into()));
            }
        }
        Err(e) => return Err(fail(e.to_string())),
    }
    info!(fill = %path.display(), mnt_id, "private fill mount verified");
    Ok(Fill { fd, mnt_id })
}

/// Whether `/` in this mountinfo is not a plain shared mount, i.e. the
/// namespace is a private copy (a slave of the host, or private): the host
/// namespace under systemd has `/` "shared:N" with no "master:N".
pub fn root_is_private(mountinfo: &str) -> bool {
    for line in mountinfo.lines() {
        let f: Vec<&str> = line.split_whitespace().collect();
        if f.len() < 7 || f[4] != "/" {
            continue;
        }
        let opt: Vec<&str> = f[6..].iter().take_while(|x| **x != "-").copied().collect();
        let shared = opt.iter().any(|o| o.starts_with("shared:"));
        let slave = opt.iter().any(|o| o.starts_with("master:"));
        return slave || !shared;
    }
    false
}

#[cfg(test)]
mod tests {
    use super::root_is_private;

    #[test]
    fn host_root_is_shared_unit_root_is_slave() {
        let host = "25 1 252:1 / / rw,relatime shared:1 - ext4 /dev/vda1 rw\n";
        let unit = "500 400 252:1 / / rw,relatime master:1 - ext4 /dev/vda1 rw\n";
        let private = "500 400 252:1 / / rw,relatime - ext4 /dev/vda1 rw\n";
        assert!(!root_is_private(host));
        assert!(root_is_private(unit));
        assert!(root_is_private(private));
        assert!(!root_is_private(""));
    }

    #[test]
    fn the_detectors_stored_group_is_not_unexpected() {
        let passed = vec![
            (3, "omnifile-detect".to_string()),
            (4, "omnifile-guard-ab12".to_string()),
            (5, "stray".to_string()),
        ];
        let (guard, unexpected) = super::sort_passed(passed);
        assert_eq!(guard, Some((4, "ab12".to_string())));
        assert_eq!(unexpected, vec![(5, "stray".to_string())]);
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Origin {
    /// Taken back from the systemd fd store.
    Reused,
    /// Created by this process (first start, or the store was lost).
    Created,
}

pub struct Group {
    pub fd: OwnedFd,
    pub id: String,
    pub origin: Origin,
}

pub const FDNAME_PREFIX: &str = "omnifile-guard-";
const CLASS_BITS: u32 = 0x0c;

/// Stored descriptors that belong to the rest of the daemon, not the guard:
/// left open and alone, silently (the write detector's group, WP9; the
/// daemon takes it after the guard starts).
pub const OTHERS_FDNAMES: &[&str] = &["omnifile-detect"];

/// A systemd-passed descriptor and its `FDNAME`.
pub type Passed = (RawFd, String);

/// The guard's group among the stored descriptors (its fd and group id),
/// and the ones nobody claims (logged and left open).
pub fn sort_passed(passed: Vec<Passed>) -> (Option<Passed>, Vec<Passed>) {
    let mut found = None;
    let mut unexpected = Vec::new();
    for (fd, name) in passed {
        if let Some(id) = name.strip_prefix(FDNAME_PREFIX)
            && found.is_none()
        {
            found = Some((fd, id.to_string()));
        } else if !OTHERS_FDNAMES.contains(&name.as_str()) {
            unexpected.push((fd, name));
        }
    }
    (found, unexpected)
}

pub fn acquire_group(cfg: &GuardConfig, passed: Vec<(RawFd, String)>) -> Result<Group, GuardError> {
    let (guard, unexpected) = sort_passed(passed);
    for (fd, name) in unexpected {
        warn!(fd, name, "ignoring an unexpected descriptor from systemd");
    }
    // SAFETY: systemd passed this descriptor to us; we own it now.
    let found = guard.map(|(fd, id)| {
        (
            unsafe { <OwnedFd as std::os::fd::FromRawFd>::from_raw_fd(fd) },
            id,
        )
    });
    if let Some((fd, id)) = found {
        let link = sys::fd_path(fd.as_raw_fd()).unwrap_or_default();
        let info = fdinfo::parse(&sys::fdinfo(fd.as_raw_fd()).unwrap_or_default());
        let flags = info.flags.unwrap_or(0);
        if link.to_string_lossy() != "anon_inode:[fanotify]" {
            std::mem::forget(fd);
            return Err(GuardError::StoredGroup(format!(
                "descriptor is {}, not a fanotify group",
                link.display()
            )));
        }
        if flags & CLASS_BITS != FAN_CLASS_PRE_CONTENT || flags & FAN_UNLIMITED_QUEUE == 0 {
            std::mem::forget(fd);
            return Err(GuardError::StoredGroup(format!(
                "group flags {flags:#x} lack FAN_CLASS_PRE_CONTENT or FAN_UNLIMITED_QUEUE"
            )));
        }
        info!(group_id = %id, flags = format!("{flags:#x}"), "REUSING the fanotify group from the systemd fd store");
        return Ok(Group {
            fd,
            id,
            origin: Origin::Reused,
        });
    }

    if cfg.require_fdstore {
        if std::env::var_os("NOTIFY_SOCKET").is_none() {
            return Err(GuardError::NoFdStore("$NOTIFY_SOCKET is not set".into()));
        }
        let cap = sdnotify::parse_fdstore_capacity(std::env::var("FDSTORE").ok().as_deref());
        if cap == 0 {
            return Err(GuardError::NoFdStore(
                "$FDSTORE is 0 or unset (FileDescriptorStoreMax=)".into(),
            ));
        }
    }
    let base = FAN_CLASS_PRE_CONTENT | FAN_UNLIMITED_QUEUE | FAN_CLOEXEC;
    let ev_flags = (libc::O_RDONLY | libc::O_LARGEFILE | libc::O_CLOEXEC) as u32;
    let fd = if cfg.report_pidfd {
        match sys::fanotify_init(base | FAN_REPORT_PIDFD, ev_flags) {
            Ok(fd) => fd,
            Err(e) if sys::errno_of(&e) == libc::EINVAL => {
                warn!("FAN_REPORT_PIDFD refused by the kernel; events carry pids only");
                sys::fanotify_init(base, ev_flags).map_err(sys_err("fanotify_init"))?
            }
            Err(e) => return Err(sys_err("fanotify_init")(e)),
        }
    } else {
        sys::fanotify_init(base, ev_flags).map_err(sys_err("fanotify_init"))?
    };
    let id = sys::random_id();
    info!(group_id = %id, "created a new fanotify pre-content group");
    Ok(Group {
        fd,
        id,
        origin: Origin::Created,
    })
}

/// Arm (or re-arm: `FAN_MARK_ADD` is idempotent) the filesystem mark, then
/// prove from fdinfo that it sits on the data volume.
pub fn arm(group: &Group, vol: &Volume) -> Result<(), GuardError> {
    sys::fanotify_mark(
        group.fd.as_raw_fd(),
        FAN_MARK_ADD | FAN_MARK_FILESYSTEM,
        FAN_PRE_ACCESS,
        vol.data_fd.as_raw_fd(),
        None,
    )
    .map_err(sys_err("fanotify_mark(FILESYSTEM, PRE_ACCESS)"))?;
    let info = fdinfo::parse(&sys::fdinfo(group.fd.as_raw_fd()).map_err(sys_err("fdinfo"))?);
    let ok = info
        .sb_marks
        .iter()
        .any(|(dev, mask, _)| *dev == vol.kdev && (*mask as u64) & FAN_PRE_ACCESS != 0);
    if !ok {
        return Err(GuardError::Volume(format!(
            "after marking, the group's fdinfo shows no FAN_PRE_ACCESS filesystem mark on \
             device {:#x}: {:?}",
            vol.kdev, info.sb_marks
        )));
    }
    for (dev, _, _) in info.sb_marks.iter().filter(|(d, _, _)| *d != vol.kdev) {
        warn!(
            sdev = format!("{dev:#x}"),
            "the group also marks another filesystem"
        );
    }
    Ok(())
}

/// The mount ignore mark on the fill mount (a new mount object at every
/// start, so it is added every time). It also ignores directory opens: at
/// startup the guard reopens its predecessor's unanswered events through
/// the fill mount, before it reads any event, and the reopen of a held
/// rewind directory would otherwise raise an open-permission event that no
/// one could answer yet.
pub fn arm_fill_ignore(group: &Group, fill: &Fill, vol: &Volume) -> Result<(), GuardError> {
    sys::fanotify_mark(
        group.fd.as_raw_fd(),
        FAN_MARK_ADD | FAN_MARK_MOUNT | FAN_MARK_IGNORE | FAN_MARK_IGNORED_SURV_MODIFY,
        FAN_PRE_ACCESS,
        fill.fd.as_raw_fd(),
        None,
    )
    .map_err(sys_err("fanotify_mark(MOUNT ignore) on the fill mount"))?;
    // A separate call: the kernel refuses FAN_ONDIR together with
    // FAN_PRE_ACCESS (measured on 6.18), and accepts it alone.
    sys::fanotify_mark(
        group.fd.as_raw_fd(),
        FAN_MARK_ADD | FAN_MARK_MOUNT | FAN_MARK_IGNORE | FAN_MARK_IGNORED_SURV_MODIFY,
        FAN_OPEN_PERM | FAN_ONDIR,
        fill.fd.as_raw_fd(),
        None,
    )
    .map_err(sys_err(
        "fanotify_mark(MOUNT ignore, directory opens) on the fill mount",
    ))?;
    let info = fdinfo::parse(&sys::fdinfo(group.fd.as_raw_fd()).map_err(sys_err("fdinfo"))?);
    if info
        .mnt_marks
        .iter()
        .any(|(id, _, ign)| *id == vol.mnt_id && (*ign as u64) & FAN_PRE_ACCESS != 0)
    {
        return Err(GuardError::Volume(
            "the data root's own mount carries an ignore mark: the share would be unguarded".into(),
        ));
    }
    if !info
        .mnt_marks
        .iter()
        .any(|(id, _, ign)| *id == fill.mnt_id && (*ign as u64) & FAN_PRE_ACCESS != 0)
    {
        return Err(GuardError::FillMount {
            path: String::new(),
            why: format!("ignore mark not visible in fdinfo: {:?}", info.mnt_marks),
        });
    }
    Ok(())
}

/// Push a newly created group into the fd store (`FDPOLL=0`: systemd must
/// not drop it on `POLLHUP`).
pub fn store(group: &Group, require: bool) -> Result<(), GuardError> {
    let name = format!("{FDNAME_PREFIX}{}", group.id);
    debug_assert!(sdnotify::fdname_is_valid(&name));
    match sdnotify::notify(
        &format!("FDSTORE=1\nFDNAME={name}\nFDPOLL=0"),
        &[group.fd.as_raw_fd()],
    ) {
        Ok(true) => {
            info!(fdname = %name, "group pushed to the systemd fd store");
            Ok(())
        }
        Ok(false) if !require => {
            warn!("not running under systemd: the group dies with this process (tests only)");
            Ok(())
        }
        Ok(false) => Err(GuardError::NoFdStore("$NOTIFY_SOCKET is not set".into())),
        Err(e) => Err(GuardError::NoFdStore(format!("FDSTORE push failed: {e}"))),
    }
}
