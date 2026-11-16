//! SMB change notify (`annex.md` §2.2): a cheap hint on any SMB server. A
//! thread asks the cifs client to watch the annex's tree
//! (`CIFS_IOC_NOTIFY`, SMB2 CHANGE_NOTIFY with `SMB2_WATCH_TREE`) and, each
//! time the server answers, asks for a quick scan. It is never the record:
//! an overflow ("rescan the directory") or a dropped watch only means the
//! next scheduled scan finds the change, and reconciliation backs both.

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;

/// Names, attributes, size, last write and security (MS-SMB2 2.2.35).
pub const FILTER: u32 = 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x100;

/// Start watching `root`; `wanted` is set on every change. Returns at once;
/// the watch ends with the process.
#[cfg(target_os = "linux")]
pub fn watch(root: PathBuf, wanted: Arc<AtomicBool>) {
    std::thread::Builder::new()
        .name("annex-notify".into())
        .spawn(move || {
            use std::os::fd::AsRawFd;
            use std::sync::atomic::Ordering;
            #[repr(C, packed)]
            struct Smb3Notify {
                completion_filter: u32,
                watch_tree: u8,
            }
            // _IOW(0xCF, 9, struct smb3_notify): the struct is packed (5
            // bytes); older headers padded it to 8.
            const REQS: [u64; 2] = [0x4005_CF09, 0x4008_CF09];
            let Ok(dir) = std::fs::File::open(&root) else {
                tracing::warn!(root = %root.display(), "change notify: can't open the root");
                return;
            };
            let mut req = None;
            let mut failures = 0u32;
            loop {
                let arg = Smb3Notify {
                    completion_filter: FILTER,
                    watch_tree: 1,
                };
                let mut done = false;
                for r in req.map(|r| vec![r]).unwrap_or_else(|| REQS.to_vec()) {
                    // SAFETY: a valid fd and a pointer to a live struct.
                    let rc =
                        unsafe { libc::ioctl(dir.as_raw_fd(), r as _, &arg as *const Smb3Notify) };
                    if rc == 0 {
                        req = Some(r);
                        done = true;
                        break;
                    }
                    let e = std::io::Error::last_os_error();
                    if e.raw_os_error() != Some(libc::ENOTTY) && req.is_none() {
                        req = Some(r);
                        break;
                    }
                }
                if done {
                    failures = 0;
                    wanted.store(true, Ordering::SeqCst);
                } else {
                    failures += 1;
                    if req.is_none() || failures > 20 {
                        tracing::info!("change notify isn't available here; scans only");
                        return;
                    }
                    // The server dropped the watch or overflowed: scan, and
                    // watch again after a pause.
                    wanted.store(true, Ordering::SeqCst);
                    std::thread::sleep(std::time::Duration::from_secs(5));
                }
            }
        })
        .ok();
}

#[cfg(not(target_os = "linux"))]
pub fn watch(_root: PathBuf, _wanted: Arc<AtomicBool>) {}
