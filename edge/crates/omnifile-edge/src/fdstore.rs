//! The write detector's fanotify group in the systemd fd store (WP9).
//!
//! A close the detector hasn't read yet is queued in its group. The group is
//! pushed to the unit's fd store when it is made (`FDSTORE=1`,
//! `FDNAME=omnifile-detect`, `FDPOLL=0`) and handed back on every restart,
//! so a close that lands while the daemon restarts (or is down) is read by
//! the next instance, as the smb-guard's groups are. The unit keeps the
//! store across restarts (`FileDescriptorStoreMax=`,
//! `FileDescriptorStorePreserve=yes`, `NotifyAccess=main`:
//! `systemd/omnifile-edge.service`).
//!
//! The smb-guard takes the systemd-passed descriptors first thing in `main`
//! (it must reserve its predecessor's event fd numbers before anything
//! opens one). So [`peek`] only reads the environment for the detector's
//! descriptor number, before the guard starts, and [`adopt`] takes it once
//! the guard is up. The guard leaves this one open and alone, by its name
//! (`omnifile_smbguard::linux::setup::OTHERS_FDNAMES`).

/// The detector's descriptor number among those systemd passed
/// (`LISTEN_PID`, `LISTEN_FDS`, `LISTEN_FDNAMES`), by its name.
pub fn find(
    listen_pid: Option<&str>,
    listen_fds: Option<&str>,
    listen_fdnames: Option<&str>,
    my_pid: u32,
) -> Vec<i32> {
    omnifile_smbguard::sdnotify::parse_listen_fds(listen_pid, listen_fds, listen_fdnames, my_pid)
        .into_iter()
        .filter(|(_, name)| name == omnifile_detect::FDNAME)
        .map(|(fd, _)| fd)
        .collect()
}

#[cfg(target_os = "linux")]
mod linux {
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    use std::sync::Mutex;

    use tracing::{debug, error, info, warn};

    /// Groups handed back by systemd under the detector's name, adopted.
    static STORED: Mutex<Vec<OwnedFd>> = Mutex::new(Vec::new());

    /// The detector's descriptors from the environment. Reads only the
    /// environment and marks them close-on-exec: call it before the
    /// smb-guard starts, which then takes (and clears) `LISTEN_*`.
    pub fn peek() -> Vec<i32> {
        let v = |k| std::env::var(k).ok();
        let fds = super::find(
            v("LISTEN_PID").as_deref(),
            v("LISTEN_FDS").as_deref(),
            v("LISTEN_FDNAMES").as_deref(),
            std::process::id(),
        );
        for fd in &fds {
            // SAFETY: fcntl on a descriptor number systemd passed us.
            unsafe { libc::fcntl(*fd, libc::F_SETFD, libc::FD_CLOEXEC) };
        }
        fds
    }

    /// Take ownership of what [`peek`] found (after the guard started).
    pub fn adopt(fds: Vec<i32>) {
        let mut st = STORED.lock().unwrap();
        for fd in fds {
            // SAFETY: systemd passed this descriptor to this process, and
            // nothing else in it owns it (the guard leaves names it doesn't
            // know alone).
            st.push(unsafe { OwnedFd::from_raw_fd(fd) });
        }
    }

    /// The group for the detector: the one from the fd store if it is
    /// usable, else a new one, pushed to the store before it has a mark. A
    /// stored descriptor that isn't usable is kept open, never released
    /// blindly. `None`: no group could be made (the detector reports why).
    pub fn detect_group() -> Option<OwnedFd> {
        let stored: Vec<OwnedFd> = std::mem::take(&mut *STORED.lock().unwrap());
        let mut found = None;
        for fd in stored {
            if found.is_none() && omnifile_detect::usable_group(&fd) {
                found = Some(fd);
            } else {
                error!(
                    fd = fd.as_raw_fd(),
                    "a stored descriptor named {} is not the detector's group; kept open, unused",
                    omnifile_detect::FDNAME
                );
                std::mem::forget(fd);
            }
        }
        if let Some(fd) = found {
            info!("REUSING the write detector's group from the systemd fd store");
            return Some(fd);
        }
        let g = match omnifile_detect::create_group() {
            Ok(g) => g,
            Err(e) => {
                warn!(err = %e, "could not make the write detector's group");
                return None;
            }
        };
        let msg = format!("FDSTORE=1\nFDNAME={}\nFDPOLL=0", omnifile_detect::FDNAME);
        match omnifile_smbguard::sdnotify::notify(&msg, &[g.as_raw_fd()]) {
            Ok(true) => info!("write detector's group pushed to the fd store"),
            Ok(false) => {
                debug!("not under systemd: the write detector's group dies with this process")
            }
            Err(e) => error!(err = %e, "could not store the write detector's group"),
        }
        Some(g)
    }
}

#[cfg(target_os = "linux")]
pub use linux::{adopt, detect_group, peek};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_detector_group_is_found_by_name_among_the_guards() {
        let names = "omnifile-guard-ab12:omnifile-guardnotif-ab12:omnifile-detect";
        assert_eq!(find(Some("42"), Some("3"), Some(names), 42), vec![5]);
        assert!(
            find(Some("41"), Some("3"), Some(names), 42).is_empty(),
            "not ours"
        );
        assert!(find(Some("42"), Some("2"), Some("a:b"), 42).is_empty());
        assert!(find(None, None, None, 42).is_empty());
    }
}
