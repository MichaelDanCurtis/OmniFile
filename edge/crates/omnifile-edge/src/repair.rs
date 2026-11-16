//! Files whose local bytes stopped being a committed version while part of
//! them was remote (M2 review B2, S1, S3), as the smb-guard reports them:
//!
//! - **Written while `PARTIAL`** (`partial_modified`): fanotify cannot tell
//!   a read from a write, so an SMB write to a resident range of a stub is
//!   let through. The file is marked dirty (its recorded ctime cleared: the
//!   stubber leaves it alone, and the feed holds updates for it), filled
//!   whole, then committed as a new version.
//! - **Grown past its marker** (`stub_extended`): an `O_APPEND` write landed
//!   while the end was remote. The guard holds every access; the daemon
//!   repairs the file (`omnifile_stub::fill::repair_extended`) and commits
//!   it as a new version.
//! - **Size mismatch while the daemon replaces content**
//!   (`may_act_on_size_mismatch`): a content-version change resizes the
//!   placeholder before it writes the new marker. Until it is done (or, after
//!   a crash, redone), the guard must not take the mismatch for a user's
//!   truncate or append.

use std::path::Path;
use std::sync::Arc;

use omnifile_fsal_proto::wire::{Decision, ObjRef, ReadDecision, RecallPhase};
use omnifile_marker::Marker;
use omnifile_stub::InodeKey;
use tracing::{info, warn};

use crate::core::{Core, RecallEntry};
use crate::ports::{BlobIo, CloseWrite, ControlPlane, RecallRequest};

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// False while this daemon is changing the file's size itself: it holds
    /// the file's lock (a content change or repair runs), or the replica
    /// still has a content-version change to apply to it (a crash in the
    /// middle of one: the redo finishes it).
    pub fn may_act_on_size_mismatch(&self, _key: InodeKey, m: &Marker) -> bool {
        if m.file_id == 0 {
            return true;
        }
        if self.locks.is_locked(m.file_id) {
            return false;
        }
        // A guard thread, outside the runtime: a short blocking wait.
        let rep = match tokio::runtime::Handle::try_current() {
            Ok(_) => return !self.content_change_pending_try(m.file_id),
            Err(_) => self.replica.blocking_lock(),
        };
        !rep.node_by_file_id(m.file_id)
            .ok()
            .flatten()
            .is_some_and(|n| content_change_pending(&n))
    }

    /// From inside the runtime (tests): never block a worker.
    fn content_change_pending_try(&self, file_id: u64) -> bool {
        match self.replica.try_lock() {
            Ok(rep) => rep
                .node_by_file_id(file_id)
                .ok()
                .flatten()
                .is_some_and(|n| content_change_pending(&n)),
            // Busy: the conservative answer.
            Err(_) => true,
        }
    }

    /// The guard saw a write to a `PARTIAL` file. Must not block.
    pub fn on_partial_modified(self: &Arc<Self>, key: InodeKey, m: &Marker) {
        warn!(
            ino = key.ino,
            file_id = m.file_id,
            version_id = m.version_id,
            resident = m.resident_bytes,
            "written while partially remote: filling it whole, then committing"
        );
        self.spawn_whole_fill(key, *m);
    }

    /// The guard found a `PARTIAL` file grown past its marker. Starts the
    /// repair and holds the access (`Wait`) until the marker is `RESIDENT`.
    pub fn on_stub_extended(self: &Arc<Self>, key: InodeKey, m: &Marker, st_size: u64) -> bool {
        info!(
            ino = key.ino,
            file_id = m.file_id,
            old_size = m.size,
            st_size,
            "stub grew past its marker: repairing (fill the remote range, then commit)"
        );
        if m.file_id == 0 {
            return false;
        }
        self.spawn_whole_fill(key, *m);
        true
    }

    /// Mark the file dirty and get its missing bytes (a fetch, or a recall
    /// for archived data); `run_fill` commits it once whole.
    fn spawn_whole_fill(self: &Arc<Self>, key: InodeKey, m: Marker) {
        let Some(rt) = tokio::runtime::Handle::try_current()
            .ok()
            .or_else(|| self.rt.get().cloned())
        else {
            warn!(
                file_id = m.file_id,
                "no runtime yet: the change is caught at the next scan"
            );
            return;
        };
        // One fetch or recall per content version: the guard asks again on
        // every re-check of a held event, and only the first one starts it.
        let rkey = (m.file_id, m.version_id, 0); // the live file only
        let start = {
            let mut map = self.recalls.lock().unwrap();
            match map.get(&rkey) {
                Some(e) if !e.phase.is_terminal() => false,
                _ => {
                    map.insert(
                        rkey,
                        RecallEntry {
                            obj: ObjRef {
                                fsid: key.fsid,
                                ino: key.ino,
                                ino_gen: key.ino_gen,
                                export_id: 0,
                                file_id: m.file_id,
                            },
                            version_id: m.version_id,
                            recall_id: 0,
                            phase: RecallPhase::Queued,
                            answer: Decision::Delay,
                            eta_s: ReadDecision::ETA_UNKNOWN,
                            resident: m.resident_bytes,
                            online: m.flags.contains(omnifile_marker::Flags::ONLINE),
                            requesters: Default::default(),
                        },
                    );
                    true
                }
            }
        };
        let core = self.clone();
        rt.spawn(async move {
            {
                let mut rep = core.replica.lock().await;
                match rep.node_by_file_id(m.file_id) {
                    Ok(Some(n)) if n.ino == Some(key.ino) => {
                        let _ = rep.set_local_dirty(n.lid, true);
                        let _ = rep.clear_fs_ctime(n.lid);
                    }
                    _ => return,
                }
            }
            if m.is_fully_resident() {
                if let Ok(Some(rel)) = core.rel_of(m.file_id).await {
                    core.commit_modified(&rel).await;
                }
                return;
            }
            if !start || core.fills.is_running(rkey) {
                // The running fetch or fill sees the dirty flag when it ends.
                return;
            }
            let req = RecallRequest {
                file_id: m.file_id,
                version_id: m.version_id,
                offset: 0,
                length: m.size,
                uid: Some(0),
                sec: None,
                proof: "claimed".into(),
                client_addr: None,
                on_open: String::new(),
                via: "local".into(),
                sid: None,
                pinned: rkey.2 != 0,
            };
            core.start_recall_with(rkey, req, Decision::Delay).await;
        });
    }

    async fn rel_of(&self, file_id: u64) -> omnifile_replica::Result<Option<std::path::PathBuf>> {
        let rep = self.replica.lock().await;
        match rep.node_by_file_id(file_id)? {
            Some(n) => rep.fs_path(n.lid),
            None => Ok(None),
        }
    }

    /// Hand a file the user changed to the commit pipeline.
    pub(crate) async fn commit_modified(&self, rel: &Path) {
        let Some(tx) = self.commit_tx.get() else {
            warn!(path = %rel.display(), "no commit pipeline: the change waits for the next close-write");
            return;
        };
        let _ = tx.send(CloseWrite::new(self.layout.path(rel), 0)).await;
    }
}

/// The replica still has to change this file's content version on disk.
fn content_change_pending(n: &omnifile_replica::Node) -> bool {
    n.fs_pending && n.fs_conflict.is_none() && n.content_version_id != n.fs_version_id
}
