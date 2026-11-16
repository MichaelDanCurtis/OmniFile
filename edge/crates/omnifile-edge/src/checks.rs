//! Startup checks. The daemon refuses to start rather than run in a
//! configuration that could fail open.

use std::os::fd::AsFd;
use std::path::Path;

use omnifile_stub::sys;

use crate::config::Config;

#[derive(Debug, thiserror::Error)]
pub enum CheckError {
    #[error("data volume {path}: {err}")]
    DataRoot { path: String, err: std::io::Error },
    #[error("refusing to start: data volume {path} is not xfs (f_type {f_type:#x})")]
    NotXfs { path: String, f_type: i64 },
    #[error("refusing to start: fill path {fill} is not the same filesystem as {data}")]
    FillRootElsewhere { fill: String, data: String },
    #[error("refusing to start: CAP_SYS_ADMIN is not effective (trusted.* markers need it)")]
    NoCapSysAdmin,
    #[error("refusing to start: cannot write trusted.* xattrs on {path}: {err}")]
    NoTrustedXattr { path: String, err: std::io::Error },
    #[error("{what} {path}: {err}")]
    Io {
        what: &'static str,
        path: String,
        err: std::io::Error,
    },
}

/// Facts about the data volume the rest of the daemon needs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Volume {
    /// `statfs.f_fsid` as the FSAL computes it (`obj_ref.fsid`).
    pub fsid: u64,
    pub dev: u64,
}

fn io<'a>(what: &'static str, path: &'a Path) -> impl FnOnce(std::io::Error) -> CheckError + 'a {
    move |err| CheckError::Io {
        what,
        path: path.display().to_string(),
        err,
    }
}

/// Run every check; returns the data volume's identity.
pub fn startup_checks(cfg: &Config) -> Result<Volume, CheckError> {
    let data = &cfg.data_root;
    let fd = sys::open_dir(data).map_err(|err| CheckError::DataRoot {
        path: data.display().to_string(),
        err,
    })?;
    let sf = sys::fstatfs(fd.as_fd()).map_err(|err| CheckError::DataRoot {
        path: data.display().to_string(),
        err,
    })?;
    if sf.f_type as i64 != sys::XFS_SUPER_MAGIC {
        return Err(CheckError::NotXfs {
            path: data.display().to_string(),
            f_type: sf.f_type as i64,
        });
    }
    let st = sys::fstat(fd.as_fd()).map_err(io("stat", data))?;
    let fsid = sys::fsid_of(&sf);

    let fill = cfg.fill_root();
    let ffd = sys::open_dir(fill).map_err(io("fill path", fill))?;
    let fst = sys::fstat(ffd.as_fd()).map_err(io("stat", fill))?;
    let fsf = sys::fstatfs(ffd.as_fd()).map_err(io("statfs", fill))?;
    if fst.st_dev != st.st_dev || sys::fsid_of(&fsf) != fsid {
        return Err(CheckError::FillRootElsewhere {
            fill: fill.display().to_string(),
            data: data.display().to_string(),
        });
    }

    if !sys::has_capability(sys::CAP_SYS_ADMIN)
        .map_err(io("capabilities", Path::new("/proc/self/status")))?
    {
        return Err(CheckError::NoCapSysAdmin);
    }

    // Prove trusted.* works on this volume (the capability can be effective
    // and still be refused, for example inside a user namespace).
    let staging = data.join(".omni/staging");
    std::fs::create_dir_all(&staging).map_err(io("create", &staging))?;
    let sfd = sys::open_dir(&staging).map_err(io("open", &staging))?;
    omnifile_marker::xattr::set_raw(sfd.as_fd(), "trusted.omnifile.probe", b"1", 0)
        .and_then(|()| omnifile_marker::xattr::remove_raw(sfd.as_fd(), "trusted.omnifile.probe"))
        .map_err(|err| CheckError::NoTrustedXattr {
            path: staging.display().to_string(),
            err,
        })?;
    std::fs::create_dir_all(data.join(".omni/commit")).map_err(io("create", data))?;

    for p in [&cfg.replica_path, &cfg.socket_path] {
        if let Some(parent) = p.parent() {
            std::fs::create_dir_all(parent).map_err(io("create", parent))?;
        }
    }
    Ok(Volume {
        fsid,
        dev: st.st_dev,
    })
}

/// Free and total bytes of the filesystem holding `path`.
pub fn volume_space(path: &std::path::Path) -> Option<(u64, u64)> {
    use std::os::unix::ffi::OsStrExt;
    let c = std::ffi::CString::new(path.as_os_str().as_bytes()).ok()?;
    let mut st = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: c is NUL-terminated; st is written by statvfs on success.
    if unsafe { libc::statvfs(c.as_ptr(), st.as_mut_ptr()) } != 0 {
        return None;
    }
    // SAFETY: statvfs returned 0, so st is initialised.
    let st = unsafe { st.assume_init() };
    // fsblkcnt_t and the block size are u64 on x86-64, narrower elsewhere.
    #[allow(clippy::unnecessary_cast)]
    let frsize = st.f_frsize as u64;
    #[allow(clippy::unnecessary_cast)]
    let (avail, blocks) = (st.f_bavail as u64, st.f_blocks as u64);
    Some((avail * frsize, blocks * frsize))
}
