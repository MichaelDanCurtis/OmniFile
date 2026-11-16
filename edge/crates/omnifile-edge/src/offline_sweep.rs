//! One pass over the data volume that brings every file's SMB offline bit in
//! line with its marker (`omnifile_marker::dosattr`).
//!
//! Marker writes keep the bit current, but a stub made before the edge set
//! it, or a bit left behind when `[shares] smb_offline` is turned off, would
//! otherwise stay as it is until that file's marker next changes. The pass
//! runs in the background at start whenever the setting differs from the
//! one it last ran with (the replica's `smb_offline_sweep`), so once per edge
//! and once per change of the setting. Files are opened `O_PATH`, so nothing
//! is read, filled or held by the guard, and symlinks are never followed.

use std::os::fd::AsFd;
use std::path::Path;

use omnifile_marker::{dosattr, xattr};
use omnifile_stub::sys;
use tracing::{info, warn};

use crate::core::Core;
use crate::ports::{BlobIo, ControlPlane};

const META_KEY: &str = "smb_offline_sweep";

/// What one pass saw: files with a marker, and values it changed.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Swept {
    pub markers: u64,
    pub changed: u64,
}

/// Walk `root` and sync every regular file's bit to its marker; a file
/// without one is local, so a bit left on it is cleared.
pub fn sweep(root: &Path) -> Swept {
    let mut out = Swept::default();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for e in entries.flatten() {
            // DirEntry::file_type never follows a symlink.
            let Ok(t) = e.file_type() else { continue };
            if t.is_dir() {
                stack.push(e.path());
                continue;
            }
            if !t.is_file() {
                continue;
            }
            let flags = libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC;
            let Ok(fd) = sys::open(&e.path(), flags, 0) else {
                continue;
            };
            let offline = match xattr::read(fd.as_fd()) {
                Ok(xattr::ReadOutcome::Marker(m)) => {
                    out.markers += 1;
                    dosattr::offline(&m)
                }
                Ok(_) => false,
                Err(_) => continue,
            };
            if matches!(dosattr::sync(fd.as_fd(), offline), Ok(true)) {
                out.changed += 1;
            }
        }
    }
    out
}

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// Run `sweep` once for the current setting (see the module docs).
    pub async fn sweep_smb_offline_once(&self) {
        let want = if dosattr::enabled() { "on" } else { "off" };
        let done = self.replica.lock().await.meta_get(META_KEY);
        if matches!(done, Ok(Some(ref v)) if v == want) {
            return;
        }
        let root = self.layout.data_root.clone();
        match tokio::task::spawn_blocking(move || sweep(&root)).await {
            Ok(s) => {
                info!(
                    markers = s.markers,
                    changed = s.changed,
                    setting = want,
                    "SMB offline bits brought in line with the markers"
                );
                if let Err(e) = self.replica.lock().await.meta_set(META_KEY, want) {
                    warn!(err = %e, "recording the SMB offline sweep failed; it runs again at the next start");
                }
            }
            Err(e) => warn!(err = %e, "SMB offline sweep failed; it runs again at the next start"),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::fs::OpenOptions;
    use std::os::fd::AsFd;

    use omnifile_marker::{Flags, Marker, dosattr, xattr};

    use super::*;

    /// As root on xfs (`OMNIFILE_TEST_DIR`), like the marker crate's tests.
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

    fn attrib(p: &Path) -> Option<u32> {
        let f = OpenOptions::new().read(true).open(p).unwrap();
        xattr::get_raw(f.as_fd(), dosattr::NAME, 256)
            .unwrap()
            .map(|v| dosattr::attrib(&v).unwrap())
    }

    #[test]
    fn a_stub_from_before_the_bit_is_marked_and_a_stale_bit_is_cleared() {
        let Some(dir) = test_dir() else { return };
        let sub = dir.path().join("a/b");
        std::fs::create_dir_all(&sub).unwrap();
        let stub = sub.join("old-stub.segy");
        let local = dir.path().join("local.txt");
        let resident = dir.path().join("resident.segy");
        for p in [&stub, &local, &resident] {
            std::fs::write(p, b"x").unwrap();
        }
        // A marker written the old way (no DOS attributes), as an upgraded
        // edge finds it.
        let raw = |p: &Path, m: &Marker| {
            let f = OpenOptions::new().read(true).open(p).unwrap();
            xattr::set_raw(
                f.as_fd(),
                omnifile_marker::XATTR_NAME,
                &m.encode().unwrap(),
                0,
            )
            .unwrap();
        };
        raw(&stub, &Marker::partial(1, 2, 4096, 0, 1, Flags::ONLINE));
        raw(&resident, &Marker::resident(1, 3, 1, 1));
        // A bit left on a file that is local now.
        let f = OpenOptions::new().read(true).open(&local).unwrap();
        xattr::set_raw(f.as_fd(), dosattr::NAME, &dosattr::fresh(0x1020), 0).unwrap();
        // A symlink to the stub's folder is not followed (the stub counts once).
        std::os::unix::fs::symlink(&sub, dir.path().join("link")).unwrap();

        let s = sweep(dir.path());
        assert_eq!(
            s,
            Swept {
                markers: 2,
                changed: 2
            }
        );
        assert_eq!(attrib(&stub), Some(0x1020));
        assert_eq!(attrib(&local), Some(0x20));
        assert_eq!(attrib(&resident), None);
        // A second pass finds nothing to change.
        assert_eq!(
            sweep(dir.path()),
            Swept {
                markers: 2,
                changed: 0
            }
        );
    }
}
