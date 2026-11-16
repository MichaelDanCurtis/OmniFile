//! Commit pipeline: a finished write becomes a catalog version in Blob.
//!
//! 1. Find (or register) the node; a new object gets a `file_id` through a
//!    `create` change op, journalled in the outbox first (echo suppression).
//! 2. Reflink the file into `.omni/commit/` (the commit clone, R1 §9) and
//!    check the live file did not change while cloning.
//! 3. Hash the clone (BLAKE3 + per-block CRC-64/NVME), `uploads` intent,
//!    upload from the clone (or dedupe), `complete`.
//! 4. If the live file is still exactly what was cloned, write a `RESIDENT`
//!    marker for the new content version and record its ctime: from then on
//!    a ctime change means a newer local write, and the stubber skips.
//! 5. Delete the clone only after the service confirmed the version.
//!
//! Partial files (a placeholder or stub) are never uploaded: their bytes
//! are not all local.
//!
//! With the real service, step 3 goes through the durable uploads outbox
//! ([`ControlPlane::outbox_commit`]): the op and its clone survive a
//! restart, and [`commit_loop`] finishes them later
//! ([`Core::resume_outbox`]), replaying with the same `op_id`.

use std::collections::HashMap;
use std::fs::File;
use std::os::fd::{AsFd, AsRawFd};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use omnifile_marker::Marker;
use omnifile_marker::xattr::{self, ReadOutcome};
use omnifile_replica::{Kind, LocalAttrs, Node};
use omnifile_stub::handshake::ctime_ns;
use omnifile_stub::sys;
use serde_json::json;
use tokio::sync::mpsc;
use tracing::{debug, info, warn};

use crate::core::Core;
use crate::ports::{
    Actor, BlobIo, BlobUpload, CloseWrite, ControlPlane, CreateOp, OutboxOutcome, OutboxUpload,
    UploadDone, UploadIntent, UploadStart,
};

#[derive(Debug, thiserror::Error)]
pub enum CommitError {
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("replica: {0}")]
    Replica(#[from] omnifile_replica::Error),
    #[error("service: {0}")]
    Control(#[from] crate::ports::CtlError),
    #[error("{0}")]
    Other(String),
    /// The upload is held in the outbox (service or Azure unavailable); its
    /// clone stays, and the outbox loop finishes it.
    #[error("queued in the upload outbox: {0}")]
    Queued(String),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Committed {
    /// A new version, now marked resident.
    Version { file_id: u64, version_id: u64 },
    /// Uploaded, but the file changed meanwhile; a later event commits it.
    ChangedDuringCommit,
    /// Nothing to do (our own write, a partial file, an ignored path).
    Ignored(&'static str),
    /// Not ready yet: a swept file written within the quiet period or open
    /// for write, a close for a file whose upload the outbox holds, or one
    /// that has not held still for a further quiet period. Tried again
    /// later, a bounded number of times; its close stays pending in the log.
    Deferred(&'static str),
    /// The writer may not be done, and only time tells: an NFSv4 client
    /// holds it open for write, or the daemon cannot ask (no FSAL, one that
    /// does not answer, or one restarted within its reclaim window). Tried
    /// again at most every 5 minutes, never given up.
    Waiting(&'static str),
}

/// A file node the edge registered but never committed a version of: its
/// commit was cut short (a crash or restart after the `create`, a change
/// while cloning) before the upload was journalled.
pub(crate) fn never_committed(n: &Node) -> bool {
    n.kind == Kind::File && !n.deleted && n.local_dirty && n.content_version_id.is_none()
}

/// Whether the file was written (its mtime) within `quiet`. Not its ctime:
/// the ACL normalisation just before the commit moves that. A writer that
/// sets an old mtime (`cp -p`) does so before its close, and a file still
/// open for write is caught by the lease check.
pub(crate) fn written_within(st: &libc::stat, quiet: std::time::Duration) -> bool {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let mtime = std::time::Duration::new(
        st.st_mtime.max(0) as u64,
        st.st_mtime_nsec.clamp(0, 999_999_999) as u32,
    );
    now.saturating_sub(mtime) < quiet
}

/// The size-and-mtime check of [`Core::still_being_written`]: `None` once the
/// file has held `cur` for `quiet`. `quiet` is the share's period now, so a
/// period changed during the wait applies at the next look, measured from when
/// the file last changed.
///
/// A write handed over with no detector view and no check under way (the
/// outbox committing a file again, `commit_modified` after a repair, a remote
/// change or a conflict) used to be committed at once, on every share, so a
/// file being written then was committed part-way (#80 review). Its first look
/// here now starts the check, on the daemon's own clock: it is committed once
/// its size and mtime have held for the period since, like any other write.
/// Never from its mtime alone, which a writer can set to any time (an SMB
/// SetInfo, `touch -d`, `cp -p`) and then pause (#82 review). A sweep
/// (`swept`) has checked the period itself, from the inode's ctime, which no
/// writer can set, so it is committed now.
fn held_still(
    growth: &mut HashMap<(u64, u64, u32), Growth>,
    k: (u64, u64, u32),
    cur: (u64, i64),
    seen: Option<crate::ports::Seen>,
    swept: bool,
    now: std::time::Instant,
    quiet: std::time::Duration,
) -> Option<Committed> {
    let since = match growth.get(&k) {
        Some(g) if (g.size, g.mtime_ns) == cur => g.since,
        Some(_) => {
            growth.insert(k, Growth::new(cur, now));
            return Some(Committed::Deferred("written again since it went quiet"));
        }
        None => match seen {
            Some(seen) if (seen.size, seen.mtime_ns) == cur => seen.at,
            Some(_) => now,
            // Neither the detector's view nor a check under way.
            None if swept => return None,
            None => now,
        },
    };
    if now.duration_since(since) >= quiet {
        growth.remove(&k);
        return None;
    }
    if growth.len() >= 100_000 {
        growth.retain(|_, g| now.duration_since(g.since) < std::time::Duration::from_secs(3600));
    }
    growth.insert(k, Growth::new(cur, since));
    Some(Committed::Deferred(
        "its size and mtime must hold for a further quiet period",
    ))
}

/// Remove a commit clone and its upload journal, through `.omni/commit`
/// opened without following symlinks. Anything not in that directory is
/// left alone.
fn remove_clone(data_root: &Path, clone: &Path) {
    let dir = data_root.join(".omni/commit");
    if clone.parent() != Some(dir.as_path()) {
        warn!(path = %clone.display(), "not a commit clone; not removed");
        return;
    }
    let (Some(name), Ok(fd)) = (clone.file_name(), sys::private_dir(&dir)) else {
        return;
    };
    let _ = sys::unlinkat(fd.as_fd(), name);
    let mut j = name.to_owned();
    j.push(".journal");
    let _ = sys::unlinkat(fd.as_fd(), &j);
}

/// A file the commit waits on to hold still: its size and mtime, and since
/// when it has had them.
#[derive(Clone, Copy, Debug)]
pub struct Growth {
    pub size: u64,
    pub mtime_ns: i64,
    pub since: std::time::Instant,
}

impl Growth {
    fn new((size, mtime_ns): (u64, i64), since: std::time::Instant) -> Self {
        Growth {
            size,
            mtime_ns,
            since,
        }
    }
}

fn snapshot(st: &libc::stat) -> (i64, i64, i64) {
    (
        st.st_size,
        st.st_mtime * 1_000_000_000 + st.st_mtime_nsec,
        ctime_ns(st),
    )
}

fn ficlone(src: &File, dst: &File) -> std::io::Result<()> {
    const FICLONE: libc::c_ulong = 0x4004_9409; // _IOW(0x94, 9, int)
    // SAFETY: both fds are valid; FICLONE takes the source fd as argument.
    let r = unsafe { libc::ioctl(dst.as_raw_fd(), FICLONE as _, src.as_raw_fd()) };
    if r < 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// The daemon's own tree. Recall notes are recognised by inode in
    /// `commit` (N4): a user's file that merely ends in `.RECALLING.txt` is
    /// committed like any other.
    fn ignored(&self, rel: &Path) -> bool {
        rel.components()
            .next()
            .is_some_and(|c| c.as_os_str() == ".omni")
    }

    async fn new_op_id(&self) -> Result<String, CommitError> {
        let n = self.replica.lock().await.next_counter("op")?;
        Ok(format!("{}-{n}", self.cfg.edge_id))
    }

    /// The node for a directory on disk, registering (and numbering) it if
    /// the edge created it.
    pub(crate) async fn ensure_dir(&self, rel: &Path) -> Result<Node, CommitError> {
        self.ensure_dir_as(rel, None).await
    }

    /// [`Core::ensure_dir`] for a folder a user made: its create says who
    /// (`control-api.md` §4.1 `actor`). Folders above it that are new too
    /// are created with no actor.
    pub(crate) async fn ensure_dir_as(
        &self,
        rel: &Path,
        actor: Option<Actor>,
    ) -> Result<Node, CommitError> {
        let _one = self.dir_create.lock().await;
        self.ensure_dir_locked(rel, actor).await
    }

    async fn ensure_dir_locked(
        &self,
        rel: &Path,
        actor: Option<Actor>,
    ) -> Result<Node, CommitError> {
        let abs = self.layout.path(rel);
        let fd = sys::open_dir_beneath(&self.layout.data_root, rel)?;
        let key = sys::inode_key(fd.as_fd())?;
        if let Some(n) = self
            .replica
            .lock()
            .await
            .node_by_ino(key.ino, key.ino_gen)?
        {
            return Ok(n);
        }
        let parent_rel = rel
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .ok_or_else(|| {
                CommitError::Other(format!("{} is not inside a known share", abs.display()))
            })?;
        let parent = Box::pin(self.ensure_dir_locked(parent_rel, None)).await?;
        let st = sys::fstat(fd.as_fd())?;
        let name = rel
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        self.register_and_create(&parent, &name, Kind::Dir, key, &st, actor)
            .await
    }

    async fn register_and_create(
        &self,
        parent: &Node,
        name: &str,
        kind: Kind,
        key: omnifile_stub::InodeKey,
        st: &libc::stat,
        actor: Option<Actor>,
    ) -> Result<Node, CommitError> {
        let parent_file_id = parent
            .file_id
            .ok_or_else(|| CommitError::Other("parent has no file_id yet".into()))?;
        let attrs = LocalAttrs {
            size: st.st_size as u64,
            mtime_ns: st.st_mtime * 1_000_000_000 + st.st_mtime_nsec,
            uid: st.st_uid,
            gid: st.st_gid,
            mode: st.st_mode & 0o7777,
        };
        let lid = self.replica.lock().await.register_local(
            Some(parent.lid),
            name,
            kind,
            key.ino,
            key.ino_gen,
            attrs,
        )?;
        let op = CreateOp {
            op_id: self.new_op_id().await?,
            parent_file_id,
            name: name.to_string(),
            kind: kind.as_str().into(),
            fsid: key.fsid,
            ino: key.ino,
            ino_gen: key.ino_gen,
            size: attrs.size,
            mtime_ns: attrs.mtime_ns,
            uid: attrs.uid,
            gid: attrs.gid,
            mode: attrs.mode,
            actor,
        };
        {
            let mut rep = self.replica.lock().await;
            // ino and gen, so a create whose answer was lost can be sent
            // again even once its node is gone (M7 re-review R2), and who
            // made it, for that resend.
            let mut payload = json!({"parent": parent_file_id, "name": name,
                                     "ino": key.ino, "ino_gen": key.ino_gen});
            if let Some(a) = &op.actor {
                payload["actor"] = serde_json::to_value(a).unwrap_or_default();
            }
            rep.outbox_enqueue(&op.op_id, Some(lid), "create", &payload)?;
            rep.outbox_set_state(&op.op_id, "sent", None)?;
        }
        let created = self.control.create(&op).await?;
        let mut rep = self.replica.lock().await;
        rep.assign_file_id(lid, created.file_id, created.entity_version)?;
        rep.outbox_set_state(
            &op.op_id,
            "acked",
            Some(&json!({"file_id": created.file_id})),
        )?;
        if kind == Kind::Dir {
            // A folder has no bytes to commit: once the service has it, the
            // catalog's changes to it apply again.
            rep.set_local_dirty(lid, false)?;
        }
        rep.node(lid)?
            .ok_or_else(|| CommitError::Other("node vanished".into()))
    }

    /// Commit one finished write.
    pub async fn commit(&self, ev: &CloseWrite) -> Result<Committed, CommitError> {
        let Ok(rel) = ev
            .path
            .strip_prefix(&self.layout.data_root)
            .map(Path::to_path_buf)
        else {
            return Ok(Committed::Ignored("outside the data volume"));
        };
        if self.ignored(&rel) || rel.components().count() < 2 {
            return Ok(Committed::Ignored("daemon path or share root"));
        }
        let abs = self.layout.path(&rel);
        // Never through a symlink: a user could swap a directory for one
        // after the write and have root upload a file outside the share.
        let ro = match sys::open_ro_beneath(&self.layout.data_root, &rel) {
            Ok(fd) => File::from(fd),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Committed::Ignored("gone"));
            }
            Err(e) if matches!(e.raw_os_error(), Some(libc::ELOOP | libc::EXDEV)) => {
                return Ok(Committed::Ignored("reached through a symlink"));
            }
            Err(e) => return Err(e.into()),
        };
        let st = sys::fstat(ro.as_fd())?;
        if st.st_mode & libc::S_IFMT != libc::S_IFREG {
            return Ok(Committed::Ignored("not a regular file"));
        }
        if rel
            .file_name()
            .is_some_and(|n| omnifile_stub::placeholder::is_note(ro.as_fd(), n))
        {
            return Ok(Committed::Ignored("the daemon's recall note"));
        }
        let key = sys::inode_key(ro.as_fd())?;
        let marker = xattr::read(ro.as_fd())?;
        let known = self
            .replica
            .lock()
            .await
            .node_by_ino(key.ino, key.ino_gen)?;
        let quiet = self.quiet.of_rel(&rel);
        if let Some(g) = ev.swept_gen
            && let Some(not_now) = self
                .swept_unready(ev, g, &ro, &st, key, &marker, known.as_ref(), quiet)
                .await?
        {
            return Ok(not_now);
        }
        match (&marker, &known) {
            (ReadOutcome::Marker(m), _) if !m.is_fully_resident() => {
                return Ok(Committed::Ignored("partial file: bytes are not all local"));
            }
            (ReadOutcome::Marker(m), Some(n))
                if n.fs_version_id == Some(m.version_id)
                    && n.fs_ctime_ns == Some(ctime_ns(&st)) =>
            {
                return Ok(Committed::Ignored(
                    "unchanged since the daemon's last write",
                ));
            }
            (ReadOutcome::Bad(e), _) => {
                return Err(CommitError::Other(format!(
                    "bad marker on {}: {e}",
                    abs.display()
                )));
            }
            _ => {}
        }
        if let Some(n) = &known
            && n.file_id.is_some_and(|f| self.locks.is_locked(f))
        {
            return Ok(Committed::Ignored("a stub or fill is running"));
        }
        // A detector close (or one replayed from the pending log) for a file
        // whose upload the outbox still holds: committing it now would make
        // a second version of the same write (WP9 review). It waits instead,
        // still pending in the log, and is looked at again once the outbox
        // let go: then it is either unchanged (nothing to do) or one commit.
        // The outbox's own re-check (`finish_outbox_op`) may commit it first;
        // a crash in between loses nothing, since the close is still pending
        // (WP9 re-review).
        if ev.token.is_some()
            && let Some(f) = known.as_ref().and_then(|n| n.file_id)
            && self.control.outbox_holds(f).await
        {
            return Ok(Committed::Deferred("an upload of it is held in the outbox"));
        }
        // A write no one watched, as a sweep would have: nobody may still have
        // it open for write (a read lease can't be taken then). An SMB or local
        // writer holding it open is not seen by the FSALs (#82 review).
        if ev.seen.is_none() && ev.swept_gen.is_none() && sys::open_for_write_elsewhere(ro.as_fd())?
        {
            return Ok(Committed::Deferred("open for write"));
        }
        if let Some(not_yet) = self.still_being_written(ev, &st, key, quiet).await {
            return Ok(not_yet);
        }
        let node = match known {
            Some(n) => n,
            None => {
                let parent_rel = rel.parent().unwrap_or(Path::new(""));
                let parent = self.ensure_dir(parent_rel).await?;
                let name = rel
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default();
                self.register_and_create(&parent, &name, Kind::File, key, &st, None)
                    .await?
            }
        };
        let file_id = node
            .file_id
            .ok_or_else(|| CommitError::Other("no file_id".into()))?;
        self.replica.lock().await.set_local_dirty(node.lid, true)?;

        // 2. Commit clone.
        let n = self.replica.lock().await.next_counter("clone")?;
        let clone_path: PathBuf = self
            .layout
            .data_root
            .join(format!(".omni/commit/{}.{n}", node.lid));
        let before = snapshot(&st);
        // Created relative to `.omni/commit` opened without following a
        // symlink at `.omni` or `commit`, `O_EXCL | O_NOFOLLOW`.
        let commit_dir = sys::private_dir(&self.layout.data_root.join(".omni/commit"))?;
        let clone = File::from(sys::openat_nofollow(
            commit_dir.as_fd(),
            clone_path.file_name().expect("clone name"),
            libc::O_CREAT | libc::O_EXCL | libc::O_RDWR,
            0o600,
        )?);
        // The clone and its upload journal (`<clone>.journal`, kept by the
        // blob client until here so a retried upload resumes): removed
        // together, only once the service confirmed the version or the
        // commit is given up (N5).
        let cleanup = |p: &Path| remove_clone(&self.layout.data_root, p);
        if let Err(e) = ficlone(&ro, &clone) {
            cleanup(&clone_path);
            return Err(e.into());
        }
        drop(clone);
        if snapshot(&sys::fstat(ro.as_fd())?) != before {
            cleanup(&clone_path);
            return Ok(Committed::ChangedDuringCommit);
        }

        // 3. Hash, intent, upload, complete.
        let cp = clone_path.clone();
        let digest = tokio::task::spawn_blocking(move || {
            omnifile_integrity::hash_file(&cp, omnifile_integrity::Options::default())
        })
        .await
        .map_err(|e| CommitError::Other(e.to_string()))?
        .map_err(|e| CommitError::Other(format!("hash: {e:?}")));
        let digest = match digest {
            Ok(d) => d,
            Err(e) => {
                cleanup(&clone_path);
                return Err(e);
            }
        };
        let result = self.upload(&node, file_id, &clone_path, &st, &digest).await;
        let (version_id, outbox_op) = match result {
            Ok(v) => v,
            Err(e @ CommitError::Queued(_)) => return Err(e),
            Err(e) => {
                cleanup(&clone_path);
                return Err(e);
            }
        };

        // 4. Mark the live file, only if it is still what was uploaded. A
        // write landing between this check and the marker write is caught
        // by `mark_resident`'s re-check after the marker write.
        let now = sys::fstat(ro.as_fd())?;
        let outcome = if snapshot(&now) == before {
            self.mark_resident(
                &ro,
                node.lid,
                file_id,
                version_id,
                digest.size,
                digest.crc64,
                before,
            )
            .await?;
            Committed::Version {
                file_id,
                version_id,
            }
        } else {
            // The catalog has the version all the same: the next commit of
            // this file must name it as its base.
            self.replica.lock().await.set_committed_version(
                node.lid,
                version_id,
                digest.size,
                Some(digest.crc64),
            )?;
            debug!(
                file_id,
                "file changed after upload; the next close-write commits it"
            );
            Committed::ChangedDuringCommit
        };
        // 5. The service confirmed the version: the clone can go.
        cleanup(&clone_path);
        if let Some(op_id) = outbox_op
            && let Err(e) = self.control.outbox_forget(&op_id).await
        {
            warn!(op_id, err = %e, "could not forget a finished outbox op");
        }
        Ok(outcome)
    }

    /// A file the namespace sweep found with no close seen (WP9): `None` if
    /// it is to be committed now. Checked here, in the one commit task, so
    /// no other commit of it can run between the check and this one:
    ///
    /// * the name still holds the inode the sweep saw;
    /// * it carries no marker: whatever the edge makes (placeholders, stubs,
    ///   rewind trees, recall notes) is marked, and a user's new file isn't;
    /// * the replica doesn't know it, or knows it only as a file whose
    ///   commit was cut short ([`never_committed`]) and which the upload
    ///   outbox doesn't hold: never a second commit of the same write;
    /// * nobody wrote it within the quiet period, and nobody has it open
    ///   for write (a read lease can't be taken then). Else it is deferred.
    #[allow(clippy::too_many_arguments)]
    async fn swept_unready(
        &self,
        ev: &CloseWrite,
        generation: u32,
        ro: &File,
        st: &libc::stat,
        key: omnifile_stub::InodeKey,
        marker: &ReadOutcome,
        known: Option<&Node>,
        quiet: std::time::Duration,
    ) -> Result<Option<Committed>, CommitError> {
        if (key.ino, key.ino_gen) != (ev.ino, generation) {
            return Ok(Some(Committed::Ignored(
                "another file has the name since the sweep",
            )));
        }
        if !matches!(marker, ReadOutcome::Unmanaged) {
            return Ok(Some(Committed::Ignored("the edge's own file (marked)")));
        }
        if let Some(n) = known {
            if !never_committed(n) {
                return Ok(Some(Committed::Ignored("already committed")));
            }
            if let Some(f) = n.file_id
                && self.control.outbox_holds(f).await
            {
                return Ok(Some(Committed::Ignored("held in the upload outbox")));
            }
        }
        if written_within(st, quiet) {
            return Ok(Some(Committed::Deferred("written within the quiet period")));
        }
        if sys::open_for_write_elsewhere(ro.as_fd())? {
            return Ok(Some(Committed::Deferred("open for write")));
        }
        Ok(None)
    }

    /// Whether the writer may not be done, though the file went quiet: a
    /// large NFS copy can pause for longer than the quiet period (a
    /// writeback stall), and a version committed then is a partial one
    /// (WP3's soak; `edge-metadata.md`, "A quiet file is not always a
    /// finished one").
    ///
    /// * NFSv4 ([`Committed::Waiting`]): every ready FSAL is asked whether
    ///   a client still holds the file open for write (`OPEN_STATE_QUERY`,
    ///   `fsal-socket.md` §5.5). An OPEN for write, an FSAL that does not
    ///   answer or is in its handshake, no FSAL at all (unless the edge runs
    ///   no Ganesha), or a Ganesha instance that connected within the
    ///   reclaim window (its clients may still be reclaiming their opens)
    ///   holds the commit.
    /// * No open state ([`Committed::Deferred`]): a file the detector saw go
    ///   quiet, however its last close came (an NFSv3 writer's fd is closed
    ///   by Ganesha after each request), must keep the size and mtime it had
    ///   for a further quiet period. The check is kept per inode, so a later
    ///   event without the detector's view (a sweep, a replay) does not
    ///   skip one that is under way.
    async fn still_being_written(
        &self,
        ev: &CloseWrite,
        st: &libc::stat,
        key: omnifile_stub::InodeKey,
        quiet: std::time::Duration,
    ) -> Option<Committed> {
        match self.server.get() {
            None if !self.cfg.fsal.no_ganesha => {
                return Some(Committed::Waiting(
                    "no FSAL to ask whether it is open for write",
                ));
            }
            None => {}
            Some(server) => {
                let ow = server
                    .open_writes(omnifile_fsal_proto::wire::ObjRef {
                        fsid: key.fsid,
                        ino: key.ino,
                        ino_gen: key.ino_gen,
                        export_id: 0,
                        file_id: 0,
                    })
                    .await;
                if ow.write_opens > 0 {
                    return Some(Committed::Waiting(
                        "an NFSv4 client holds it open for write",
                    ));
                }
                if !ow.closed() {
                    return Some(Committed::Waiting(
                        "not every FSAL said whether it is open for write",
                    ));
                }
                if ow.asked + ow.unsupported == 0 && !self.cfg.fsal.no_ganesha {
                    // Ganesha restarting: its NFSv4 clients keep their opens
                    // and reclaim them once it is back.
                    return Some(Committed::Waiting("no FSAL connected to ask"));
                }
                if ow.newest_instance_age.is_some_and(|a| {
                    a < std::time::Duration::from_secs(self.cfg.fsal.reclaim_window_s)
                }) {
                    return Some(Committed::Waiting(
                        "a Ganesha instance connected within its reclaim window",
                    ));
                }
            }
        }
        let k = (key.fsid, key.ino, key.ino_gen);
        let (size, mtime_ns, _) = snapshot(st);
        let cur = (size as u64, mtime_ns);
        let now = std::time::Instant::now();
        let mut growth = self.growth.lock().unwrap();
        held_still(
            &mut growth,
            k,
            cur,
            ev.seen,
            ev.swept_gen.is_some(),
            now,
            quiet,
        )
    }

    /// Write a `RESIDENT` marker for a version the service has committed,
    /// and record it in the replica. The caller has checked that the open
    /// file `ro` holds exactly that version's bytes, with `before` the
    /// snapshot it checked against.
    ///
    /// After the marker write the size and mtime are compared with
    /// `before` again (the ctime moves with the marker write itself). If a
    /// write landed in between, the ctime is not recorded: the stubber then
    /// leaves the file alone (it can't prove the bytes are this version),
    /// and the writer's close-write commits the new content.
    #[allow(clippy::too_many_arguments)]
    async fn mark_resident(
        &self,
        ro: &File,
        lid: i64,
        file_id: u64,
        version_id: u64,
        size: u64,
        crc64: u64,
        before: (i64, i64, i64),
    ) -> Result<(), CommitError> {
        let share_id = self
            .replica
            .lock()
            .await
            .share_of(lid)?
            .map(|s| s.share_id)
            .unwrap_or(0);
        // The file's lock across the marker write: no stub, fill or content
        // change can interleave (a narrowed marker overwritten by RESIDENT
        // would make punched bytes read as zeros). Busy, or changed since
        // the caller's check: record the version without a ctime, so the
        // stubber leaves the file alone and the next close-write recommits.
        // Short holders (a flag sync, a redo) are waited for; a stub or fill
        // that keeps it is not.
        let mut lock = self.locks.try_lock(file_id);
        for _ in 0..40 {
            if lock.is_some() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            lock = self.locks.try_lock(file_id);
        }
        if lock.is_none() || snapshot(&sys::fstat(ro.as_fd())?) != before {
            let mut rep = self.replica.lock().await;
            rep.set_committed_version(lid, version_id, size, Some(crc64))?;
            rep.clear_fs_ctime(lid)?;
            debug!(
                file_id,
                version_id, "busy or changed before the marker write"
            );
            return Ok(());
        }
        let mut m = Marker::resident(file_id, version_id, size, share_id);
        // Just uploaded to the live account: readable by range GET.
        m.flags = omnifile_marker::Flags::ONLINE;
        m.marker_seq = self.seq.next();
        xattr::write(ro.as_fd(), &m).map_err(|e| CommitError::Other(e.to_string()))?;
        sys::fsync(ro.as_fd())?;
        drop(lock);
        #[cfg(test)]
        if let Some(h) = tests::AFTER_MARKER_WRITE.lock().unwrap().take() {
            h(ro);
        }
        let after = snapshot(&sys::fstat(ro.as_fd())?);
        let mut rep = self.replica.lock().await;
        rep.set_committed_version(lid, version_id, size, Some(crc64))?;
        if (after.0, after.1) != (before.0, before.1) {
            rep.clear_fs_ctime(lid)?;
            debug!(
                file_id,
                version_id, "written during the marker write; the next close-write commits it"
            );
            return Ok(());
        }
        rep.set_fs_ctime(lid, after.2)?;
        rep.set_local_dirty(lid, false)?;
        info!(
            file_id,
            version_id,
            size,
            crc64 = format!("{crc64:016x}"),
            "committed"
        );
        Ok(())
    }

    /// Finish outbox ops left by an earlier run (or held while the service
    /// was away): drive them, then act on each terminal one.
    ///
    /// * Committed: the replica learns the version. If the live file still
    ///   holds exactly the uploaded bytes (BLAKE3 re-checked, since the
    ///   in-memory snapshot is gone), it is marked resident; otherwise it is
    ///   handed back to the commit pipeline as a new write.
    /// * Conflict or failed: the live file is committed again from scratch.
    ///
    /// Then the clone is deleted and the op forgotten. Returns the writes to
    /// commit again.
    pub async fn resume_outbox(&self) -> Vec<CloseWrite> {
        let done = match self.control.outbox_resume().await {
            Ok(d) => d,
            Err(e) => {
                debug!(err = %e, "upload outbox: resume");
                return Vec::new();
            }
        };
        let mut again = Vec::new();
        let now = std::time::Instant::now();
        for (up, outcome) in done {
            if self
                .outbox_failures
                .lock()
                .unwrap()
                .get(&up.op_id)
                .is_some_and(|(_, next)| *next > now)
            {
                continue; // backing off
            }
            match self.finish_outbox_op(&up, &outcome).await {
                Ok(Some(cw)) => again.push(cw),
                Ok(None) => {}
                Err(e) => {
                    // Kept, with its clone, and tried again with a backoff
                    // (a persistent error must not re-hash a big file every
                    // tick). After the last attempt it is forgotten and its
                    // file committed again from the live copy: safe, since
                    // its close is still pending in the log (WP9 re-review).
                    let attempts = {
                        let mut f = self.outbox_failures.lock().unwrap();
                        let base = self.cfg.commit.outbox_finish_backoff_s.max(1);
                        let e = f.entry(up.op_id.clone()).or_insert((0, now));
                        e.0 += 1;
                        let wait = base.saturating_mul(1 << (e.0 - 1).min(10)).min(3600);
                        e.1 = now + std::time::Duration::from_secs(wait);
                        e.0
                    };
                    if attempts < self.cfg.commit.outbox_finish_attempts.max(1) {
                        warn!(op_id = up.op_id, attempts, err = %e, "upload outbox: finishing an op failed; tried again later");
                        continue;
                    }
                    warn!(op_id = up.op_id, attempts, err = %e,
                          "upload outbox: finishing an op keeps failing; forgotten, its file committed again");
                    if let Some(cw) = self.outbox_file_again(up.file_id).await {
                        again.push(cw);
                    }
                }
            }
            self.outbox_failures.lock().unwrap().remove(&up.op_id);
            remove_clone(&self.layout.data_root, &up.clone);
            if let Err(e) = self.control.outbox_forget(&up.op_id).await {
                warn!(op_id = up.op_id, err = %e, "could not forget a finished outbox op");
            }
        }
        again
    }

    /// The live file of `file_id`, to commit again (an outbox op given up).
    async fn outbox_file_again(&self, file_id: u64) -> Option<CloseWrite> {
        let rep = self.replica.lock().await;
        let node = rep.node_by_file_id(file_id).ok()??;
        let rel = rep.fs_path(node.lid).ok()??;
        Some(CloseWrite::new(
            self.layout.path(&rel),
            node.ino.unwrap_or(0),
        ))
    }

    async fn finish_outbox_op(
        &self,
        up: &OutboxUpload,
        outcome: &OutboxOutcome,
    ) -> Result<Option<CloseWrite>, CommitError> {
        let node = self.replica.lock().await.node_by_file_id(up.file_id)?;
        let Some(node) = node.filter(|n| !n.deleted) else {
            return Ok(None);
        };
        let Some(rel) = self.replica.lock().await.fs_path(node.lid)? else {
            return Ok(None);
        };
        let abs = self.layout.path(&rel);
        let ro = match sys::open_ro_beneath(&self.layout.data_root, &rel) {
            Ok(fd) => File::from(fd),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e.into()),
        };
        let st = sys::fstat(ro.as_fd())?;
        let again = Some(CloseWrite::new(abs.clone(), st.st_ino));
        let OutboxOutcome::Committed { version_id } = outcome else {
            warn!(
                op_id = up.op_id,
                ?outcome,
                "outbox op did not commit; committing the file again"
            );
            return Ok(again);
        };
        info!(
            op_id = up.op_id,
            file_id = up.file_id,
            version_id,
            "upload outbox: op finished"
        );
        let before = snapshot(&st);
        let same = st.st_size as u64 == up.size && {
            // Hash the inode opened above, never the path again: a directory
            // swapped for a symlink since must not redirect the read.
            let f = ro.try_clone()?;
            let digest = tokio::task::spawn_blocking(move || {
                let p = PathBuf::from(format!("/proc/self/fd/{}", f.as_raw_fd()));
                omnifile_integrity::hash_file(&p, omnifile_integrity::Options::default())
            })
            .await
            .map_err(|e| CommitError::Other(e.to_string()))?
            .map_err(|e| CommitError::Other(format!("hash: {e:?}")))?;
            digest.blake3.to_hex().as_str() == up.blake3_hex
        };
        if same && snapshot(&sys::fstat(ro.as_fd())?) == before {
            self.mark_resident(
                &ro,
                node.lid,
                up.file_id,
                *version_id,
                up.size,
                up.crc64nvme,
                before,
            )
            .await?;
            Ok(None)
        } else {
            self.replica.lock().await.set_committed_version(
                node.lid,
                *version_id,
                up.size,
                Some(up.crc64nvme),
            )?;
            Ok(again)
        }
    }

    /// Upload the clone and commit the version. Returns the version and,
    /// for an outbox op, its `op_id` (to forget once the clone is gone).
    async fn upload(
        &self,
        node: &Node,
        file_id: u64,
        clone: &Path,
        st: &libc::stat,
        digest: &omnifile_integrity::FileDigest,
    ) -> Result<(u64, Option<String>), CommitError> {
        let op_id = self.new_op_id().await?;
        let blake3_hex = digest.blake3.to_hex().to_string();
        if self.control.has_outbox() {
            let up = OutboxUpload {
                op_id: op_id.clone(),
                file_id,
                clone: clone.to_path_buf(),
                size: digest.size,
                mtime_ns: st.st_mtime * 1_000_000_000 + st.st_mtime_nsec,
                blake3_hex,
                crc64nvme: digest.crc64,
                block_size: digest.plan.block_size(),
                block_crcs: digest.blocks.iter().map(|b| b.crc64).collect(),
                base_version_id: node.content_version_id,
                uid: st.st_uid,
                gid: st.st_gid,
                mode: st.st_mode & 0o7777,
            };
            return match self.control.outbox_commit(&up).await {
                Ok(OutboxOutcome::Committed { version_id }) => Ok((version_id, Some(op_id))),
                Ok(other) => {
                    let _ = self.control.outbox_forget(&op_id).await;
                    Err(CommitError::Other(format!("upload {op_id}: {other:?}")))
                }
                // Held in the outbox, with the clone: finished by the loop.
                Err(e) => Err(CommitError::Queued(format!("{op_id}: {e}"))),
            };
        }
        let intent = UploadIntent {
            op_id,
            file_id,
            size: digest.size,
            mtime_ns: st.st_mtime * 1_000_000_000 + st.st_mtime_nsec,
            blake3_hex: blake3_hex.clone(),
            crc64nvme: digest.crc64,
            block_size: digest.plan.block_size(),
            base_version_id: node.content_version_id,
        };
        match self.control.upload_start(&intent).await? {
            UploadStart::Dedupe { version_id } => Ok((version_id, None)),
            UploadStart::Upload {
                upload_id,
                version_id: _,
                sas_url,
                block_size,
            } => {
                if block_size != digest.plan.block_size() {
                    return Err(CommitError::Other(format!(
                        "service block size {block_size} != planned {}",
                        digest.plan.block_size()
                    )));
                }
                let up = self
                    .blob
                    .upload(&BlobUpload {
                        sas_url,
                        path: clone.to_path_buf(),
                        size: digest.size,
                        block_size,
                        block_crcs: digest.blocks.iter().map(|b| b.crc64).collect(),
                        uid: st.st_uid,
                        gid: st.st_gid,
                        mode: st.st_mode & 0o7777,
                    })
                    .await?;
                if up.crc64nvme != digest.crc64 || up.size != digest.size {
                    return Err(CommitError::Other(
                        "uploaded CRC or size differs from the hash".into(),
                    ));
                }
                let done = UploadDone {
                    crc64nvme: digest.crc64,
                    blake3_hex,
                    size: digest.size,
                    etag: up.etag,
                    backend_version_id: up.version_id,
                };
                Ok((self.control.upload_complete(upload_id, &done).await?, None))
            }
        }
    }
}

/// Entries of `.omni/commit` that no outbox upload names (WP9): a clone, and
/// its blob upload journal (`<clone>.journal`), left by a commit that
/// stopped before its upload was journalled. The live file is still there
/// (or was replaced by the user), and its commit is redone from it.
pub(crate) fn orphan_clones(names: &[String], referenced: &[String]) -> Vec<String> {
    names
        .iter()
        .filter(|n| {
            let clone = n.strip_suffix(".journal").unwrap_or(n);
            !referenced.iter().any(|r| r == clone)
        })
        .cloned()
        .collect()
}

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// Remove orphaned commit clones. Run once at start, before any commit
    /// of this run makes a clone of its own.
    pub(crate) async fn remove_orphan_clones(&self) {
        let referenced: Vec<String> = self
            .control
            .outbox_clones()
            .await
            .iter()
            .filter_map(|p| p.file_name()?.to_str().map(str::to_owned))
            .collect();
        let dir = self.layout.data_root.join(".omni/commit");
        let removed = tokio::task::spawn_blocking(move || -> std::io::Result<usize> {
            let fd = sys::private_dir(&dir)?;
            let mut names = Vec::new();
            for e in std::fs::read_dir(format!("/proc/self/fd/{}", fd.as_raw_fd()))? {
                if let Some(n) = e?.file_name().to_str() {
                    names.push(n.to_owned());
                }
            }
            let orphans = orphan_clones(&names, &referenced);
            for n in &orphans {
                sys::unlinkat(fd.as_fd(), std::ffi::OsStr::new(n))?;
            }
            Ok(orphans.len())
        })
        .await;
        match removed {
            Ok(Ok(0)) => {}
            Ok(Ok(n)) => info!(
                removed = n,
                "orphaned commit clones removed (no upload names them)"
            ),
            Ok(Err(e)) => warn!(err = %e, "removing orphaned commit clones"),
            Err(e) => warn!(err = %e, "removing orphaned commit clones"),
        }
    }
}

/// How often the outbox is driven when nothing else happens.
const OUTBOX_EVERY: std::time::Duration = std::time::Duration::from_secs(10);

/// Consume finished writes until shutdown, and finish outbox ops: at
/// startup (ops left by an earlier run), then every [`OUTBOX_EVERY`].
/// Both run in this one task, so an outbox op never races a commit.
pub async fn commit_loop<C: ControlPlane, B: BlobIo>(
    core: Arc<Core<C, B>>,
    mut rx: mpsc::Receiver<CloseWrite>,
) {
    let mut stop = core.shutdown.subscribe();
    core.remove_orphan_clones().await;
    let mut tick = tokio::time::interval(OUTBOX_EVERY);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    // Failed commits (a refused or unanswered `create`, an upload error) are
    // tried again with backoff (M1 carry-over: crate::carry).
    let mut retries = crate::carry::Retries::default();
    let mut retry_tick = tokio::time::interval(std::time::Duration::from_secs(1));
    retry_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        let evs = tokio::select! {
            ev = rx.recv() => match ev {
                // A new close or quiet period is new news: its backoff
                // starts again (a file deferred while still being written,
                // or open for write, is looked at soon after it settles).
                Some(ev) => {
                    retries.clear(&ev);
                    vec![ev]
                }
                None => return,
            },
            _ = tick.tick(), if core.control.has_outbox() => core.resume_outbox().await,
            _ = retry_tick.tick(), if !retries.is_empty() => retries.take_due(),
            _ = stop.changed() => return,
        };
        for ev in evs {
            match commit_one(&core, &ev).await {
                done @ (Done::Yes | Done::Held) => {
                    retries.clear(&ev);
                    if let Some(g) = ev.swept_gen {
                        core.sweep_handed.lock().unwrap().remove(&(ev.ino, g));
                    }
                    // Committed, held in the upload outbox (durable), or
                    // nothing to do: no longer a pending close. A failure
                    // stays pending, so the next start tries it again.
                    if let (Some(t), Some(log)) = (ev.token, core.pending.get()) {
                        if matches!(done, Done::Held) {
                            log.done_durable(t);
                        } else {
                            log.done(t);
                        }
                    }
                }
                Done::Failed => retries.failed(&ev),
                Done::NotYet => not_yet(&mut retries, &ev),
                Done::Waiting(why) => retries.waiting(&ev, why),
            }
        }
    }
}

/// A write that isn't ready yet. A swept file is looked at a bounded number of
/// times: the next sweep finds it again. Any other write (a close the detector
/// saw, the outbox's re-commit, `commit_modified`) may have nothing else to
/// bring it back, so it is looked at again, at most every 5 minutes, and never
/// given up (#82 review).
fn not_yet(retries: &mut crate::carry::Retries, ev: &CloseWrite) {
    if ev.swept_gen.is_some() {
        retries.deferred(ev);
    } else {
        retries.waiting(ev, "not quiet for its period yet");
    }
}

enum Done {
    Yes,
    /// Held in the upload outbox, which finishes it.
    Held,
    /// Failed: tried again with backoff.
    Failed,
    /// A swept file not ready yet: looked at again with backoff.
    NotYet,
    /// The writer may not be done: looked at again, never given up.
    Waiting(&'static str),
}

/// Commit one write.
async fn commit_one<C: ControlPlane, B: BlobIo>(core: &Arc<Core<C, B>>, ev: &CloseWrite) -> Done {
    match core.commit(ev).await {
        Ok(Committed::Version { .. }) => {
            if ev.swept_gen.is_some() {
                info!(path = %ev.path.display(), "committed a file no close was seen for (namespace sweep)");
            }
            core.wake_stubber.notify_one();
            Done::Yes
        }
        Ok(Committed::Deferred(why)) => {
            debug!(path = %ev.path.display(), why, "not ready to commit; looked at again");
            Done::NotYet
        }
        Ok(Committed::Waiting(why)) => Done::Waiting(why),
        Ok(other) => {
            debug!(path = %ev.path.display(), ?other, "commit");
            Done::Yes
        }
        Err(e @ CommitError::Queued(_)) => {
            info!(path = %ev.path.display(), err = %e, "upload held; the outbox finishes it");
            Done::Held
        }
        Err(e) => {
            warn!(path = %ev.path.display(), err = %e, "commit failed");
            Done::Failed
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;
    use std::time::{Duration, Instant};

    use super::*;
    use crate::fakes::{FakeService, events};

    /// #80 review, and #82's: a write handed over with no detector view and no
    /// check under way waits out the period on the daemon's own clock, from its
    /// first look, whatever its mtime says; a sweep (which checked the period
    /// from the ctime) is committed now.
    #[test]
    fn a_write_no_one_watched_waits_out_the_period_on_the_daemon_s_clock() {
        let t0 = Instant::now();
        let at = |s| t0 + Duration::from_secs(s);
        let k = (1, 2, 3);
        let q = Duration::from_secs(60);
        let wait = |d: Option<Committed>| matches!(d, Some(Committed::Deferred(_)));
        // However old its mtime, the first look starts the wait.
        let mut g = HashMap::new();
        assert!(wait(held_still(&mut g, k, (100, 5), None, false, at(0), q)));
        assert!(wait(held_still(
            &mut g,
            k,
            (100, 5),
            None,
            false,
            at(59),
            q
        )));
        assert!(held_still(&mut g, k, (100, 5), None, false, at(60), q).is_none());
        // Written again meanwhile, even with an old mtime put back: over again.
        let mut g = HashMap::new();
        assert!(wait(held_still(&mut g, k, (100, 5), None, false, at(0), q)));
        assert!(wait(held_still(
            &mut g,
            k,
            (200, 5),
            None,
            false,
            at(30),
            q
        )));
        assert!(wait(held_still(
            &mut g,
            k,
            (200, 5),
            None,
            false,
            at(89),
            q
        )));
        assert!(held_still(&mut g, k, (200, 5), None, false, at(90), q).is_none());
        // A sweep checked the period itself.
        let mut g = HashMap::new();
        assert!(held_still(&mut g, k, (100, 5), None, true, at(0), q).is_none());
    }

    /// #82 review: a write that isn't quiet yet is never dropped from the
    /// retries, unless a sweep will find it again.
    #[test]
    fn a_write_not_quiet_yet_is_only_given_up_when_a_sweep_will_find_it() {
        let mut r = crate::carry::Retries::default();
        let outbox = CloseWrite::new("/d/s/again".into(), 7);
        // Still scheduled after every look: a dropped one would be put back by
        // the next call, so check each time.
        for n in 1..=(5 * crate::carry::MAX_ATTEMPTS) {
            not_yet(&mut r, &outbox);
            assert_eq!(r.len(), 1, "the outbox's re-commit was dropped at look {n}");
        }
        let mut swept = CloseWrite::new("/d/s/swept".into(), 8);
        swept.swept_gen = Some(1);
        let mut r = crate::carry::Retries::default();
        for _ in 0..=crate::carry::MAX_ATTEMPTS {
            not_yet(&mut r, &swept);
        }
        assert!(r.is_empty(), "left to the next sweep");
    }

    /// Decision 67: the further wait is the share's period, and a period
    /// changed during it applies at the next look, from when the file last
    /// changed.
    #[test]
    fn the_further_wait_takes_the_share_s_period_now() {
        let t0 = Instant::now();
        let at = |s| t0 + Duration::from_secs(s);
        let k = (1, 2, 3);
        let cur = (100, 5);
        let seen = Some(crate::ports::Seen {
            size: 100,
            mtime_ns: 5,
            closed: true,
            at: t0,
        });
        let wait = |d: Option<Committed>| matches!(d, Some(Committed::Deferred(_)));
        let mut g = HashMap::new();
        // 60 s: not yet at 30 s, and a 10 s share is done by then.
        assert!(wait(held_still(
            &mut g,
            k,
            cur,
            seen,
            false,
            at(30),
            Duration::from_secs(60)
        )));
        let mut other = HashMap::new();
        assert!(
            held_still(
                &mut other,
                k,
                cur,
                seen,
                false,
                at(30),
                Duration::from_secs(10)
            )
            .is_none()
        );
        // Lengthened: it waits on, from the same start.
        assert!(wait(held_still(
            &mut g,
            k,
            cur,
            None,
            false,
            at(61),
            Duration::from_secs(120)
        )));
        // Shortened: done at the next look, from the same start.
        assert!(held_still(&mut g, k, cur, None, false, at(62), Duration::from_secs(45)).is_none());
        assert!(g.is_empty(), "the check is over");
        // Written again meanwhile: the period starts over.
        assert!(wait(held_still(
            &mut g,
            k,
            cur,
            seen,
            false,
            at(1),
            Duration::from_secs(60)
        )));
        let later = (200, 6);
        assert!(wait(held_still(
            &mut g,
            k,
            later,
            None,
            false,
            at(30),
            Duration::from_secs(60)
        )));
        assert!(wait(held_still(
            &mut g,
            k,
            later,
            None,
            false,
            at(89),
            Duration::from_secs(60)
        )));
        assert!(
            held_still(
                &mut g,
                k,
                later,
                None,
                false,
                at(90),
                Duration::from_secs(60)
            )
            .is_none()
        );
    }

    #[test]
    fn clones_no_upload_names_are_orphans() {
        let names: Vec<String> = ["7.1", "7.1.journal", "8.2", "8.2.journal", "9.3"]
            .map(String::from)
            .to_vec();
        let referenced = vec!["8.2".to_string()];
        assert_eq!(
            orphan_clones(&names, &referenced),
            vec!["7.1", "7.1.journal", "9.3"]
        );
        assert!(orphan_clones(&names, &names).is_empty());
    }

    type Hook = Box<dyn FnOnce(&File) + Send>;
    /// Runs once, right after `mark_resident`'s marker write.
    pub(super) static AFTER_MARKER_WRITE: Mutex<Option<Hook>> = Mutex::new(None);

    /// S6: a write landing between the commit's last check and its marker
    /// write leaves no ctime recorded, so the stubber never punches the new
    /// bytes as if they were the committed version. Root on xfs only.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_write_during_the_marker_write_is_not_recorded_as_committed() {
        // SAFETY: geteuid has no preconditions.
        if unsafe { libc::geteuid() } != 0 {
            eprintln!("skipping: needs root");
            return;
        }
        let base =
            std::env::var("OMNIFILE_TEST_DIR").unwrap_or_else(|_| "/srv/omnifile/data/test".into());
        let Ok(dir) = tempfile::tempdir_in(&base) else {
            return;
        };
        let d = dir.path();
        let cfg = crate::config::Config::from_toml(
            &format!(
                r#"
                data_root = "{d}/data"
                replica_path = "{d}/state/replica.db"
                socket_path = "{d}/run/fsal.sock"
                edge_id = 7
                backend = "fake"
                [fsal]
                required_exe = ""
                no_ganesha = true
                [commit]
                detect = false
                # The write below is handed over at once, with no detector
                # view: it waits out this period first (#80 review).
                quiet_s = 1
                "#,
                d = d.display()
            ),
            Path::new("t.toml"),
        )
        .unwrap();
        std::fs::create_dir_all(d.join("data")).unwrap();
        let Ok(vol) = crate::checks::startup_checks(&cfg) else {
            eprintln!("skipping: not xfs");
            return;
        };
        let svc = Arc::new(FakeService::new());
        let data_root = cfg.data_root.clone();
        let dm = crate::daemon::Daemon::start(cfg, vol, svc.clone(), svc.clone())
            .await
            .unwrap();
        svc.push_event(events::share(1, 100, "proj", "fail", 1));
        svc.push_event(events::dir(100, None, "", 1));
        let p = data_root.join("proj/w.bin");
        let t0 = Instant::now();
        while !data_root.join("proj").is_dir() {
            assert!(t0.elapsed() < Duration::from_secs(10));
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        std::fs::write(&p, vec![7u8; 1 << 20]).unwrap();
        *AFTER_MARKER_WRITE.lock().unwrap() = Some(Box::new(|f: &File| {
            // A writer's append: size and mtime move.
            let w = File::from(sys::reopen(f.as_fd(), libc::O_WRONLY | libc::O_APPEND).unwrap());
            std::io::Write::write_all(&mut &w, b"late").unwrap();
        }));
        dm.commits
            .send(CloseWrite::new(p.clone(), 0))
            .await
            .unwrap();
        let t0 = Instant::now();
        let node = loop {
            let ro = sys::open_ro(&p).unwrap();
            let key = sys::inode_key(ro.as_fd()).unwrap();
            let n = dm
                .core
                .replica
                .lock()
                .await
                .node_by_ino(key.ino, key.ino_gen)
                .unwrap();
            if let Some(n) = n
                && n.content_version_id.is_some()
            {
                break n;
            }
            assert!(t0.elapsed() < Duration::from_secs(10), "no commit");
            tokio::time::sleep(Duration::from_millis(50)).await;
        };
        assert!(AFTER_MARKER_WRITE.lock().unwrap().is_none(), "hook ran");
        assert_eq!(
            node.fs_ctime_ns, None,
            "the late write must not be recorded as the committed version"
        );
        dm.shutdown().await;
    }
}
