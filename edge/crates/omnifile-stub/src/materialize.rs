//! Drive the xfs tree from the replica: carry out each pending op and record
//! the result (`edge-metadata.md` §5.1 steps 3–4).
//!
//! Every op is idempotent and checks the inode it is about to touch:
//! - creates adopt an object that already is this one, and report a
//!   conflict for anything else at the name;
//! - renames use `RENAME_NOREPLACE` and accept "already renamed";
//! - updates and removals only touch the inode the replica recorded; a
//!   different inode at the path is someone else's file and is left alone.
//! - a content-version change goes through the narrowing handshake.

use std::io;
use std::os::fd::AsFd;
use std::path::Path;

use omnifile_marker::Flags;
use omnifile_marker::xattr::{self, ReadOutcome};
use omnifile_replica::{Action, Kind, Node, Outcome, PendingOp, Replica};
use tracing::{debug, warn};

use crate::placeholder::{
    Created, NewPerms, PlaceholderSpec, create_dir_with_perms, create_placeholder_with_perms,
    create_symlink, parent_default_acl,
};
use crate::{FileLocks, FsalNotifier, InodeKey, Layout, MarkerSeq, handshake, share_of, sys, what};

pub struct Materializer<'a, N> {
    pub layout: &'a Layout,
    pub notifier: &'a N,
    pub seq: &'a MarkerSeq,
    /// The same locks the stubber and filler take: a content-version change
    /// never interleaves with a stub or a fill.
    pub locks: &'a FileLocks,
    /// What a new placeholder or directory gets before its name exists (S4).
    pub perms: &'a dyn PermsSource,
}

/// Decides a new object's permissions ([`NewPerms`]) from its replica node
/// and its directory's default ACL (raw xattr). The daemon's implementation
/// maps the catalog ACL to local ids and falls back to owner-only.
pub trait PermsSource: Send + Sync {
    fn new_perms(&self, node: &Node, parent_default_acl: Option<&[u8]>) -> NewPerms;
}

/// The node's mode alone (tests, and edges without ACL handling).
pub struct ModeOnly;

impl PermsSource for ModeOnly {
    fn new_perms(&self, _: &Node, _: Option<&[u8]>) -> NewPerms {
        NewPerms::Mode
    }
}

/// The placeholder for a replica node.
pub fn spec_for(node: &Node, share_id: u64) -> Option<PlaceholderSpec> {
    let mut flags = Flags::empty();
    if node.archived {
        flags = flags | Flags::ARCHIVED;
    }
    if node.online {
        flags = flags | Flags::ONLINE;
    }
    Some(PlaceholderSpec {
        file_id: node.file_id?,
        version_id: node.content_version_id?,
        size: node.size,
        share_id,
        flags,
        uid: node.uid,
        gid: node.gid,
        mode: node.mode,
        mtime_ns: node.mtime_ns,
    })
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct RedoStats {
    pub done: usize,
    pub retried: usize,
    pub conflicts: usize,
    pub passes: usize,
}

fn same_inode(at: &sys::At, node: &Node) -> io::Result<Option<bool>> {
    match at.lstat() {
        Ok(st) => Ok(Some(Some(st.st_ino) == node.ino)),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e),
    }
}

fn key_at(at: &sys::At, file_id: u64) -> io::Result<InodeKey> {
    let fd = at.open_ro().or_else(|_| at.open_dir())?;
    let mut k = sys::inode_key(fd.as_fd())?;
    k.file_id = file_id;
    Ok(k)
}

impl<N: FsalNotifier> Materializer<'_, N> {
    /// `rel`'s parent, resolved below the data volume without symlinks.
    fn at(&self, rel: &Path) -> io::Result<sys::At> {
        sys::resolve_beneath(&self.layout.data_root, rel)
    }

    /// Carry out one op.
    pub async fn apply(&self, op: &PendingOp) -> Outcome {
        match self.apply_inner(op).await {
            Ok(o) => o,
            Err(e) => Outcome::Retry(e.to_string()),
        }
    }

    async fn apply_inner(&self, op: &PendingOp) -> io::Result<Outcome> {
        let n = &op.node;
        match &op.action {
            Action::Create { path } => self.create(op, path).await,
            Action::Update {
                path,
                rename_from,
                content_changed,
            } => {
                if let Some(from) = rename_from {
                    // A rename never moves an object between shares.
                    if share_of(from) != share_of(path) {
                        return Ok(Outcome::Conflict(format!(
                            "{} -> {} crosses shares",
                            from.display(),
                            path.display()
                        )));
                    }
                    let at_from = self.at(from)?;
                    let at_to = self.at(path)?;
                    match same_inode(&at_from, n)? {
                        Some(true) => match sys::rename_noreplace(&at_from, &at_to) {
                            Ok(()) => {}
                            Err(e) if e.raw_os_error() == Some(libc::EEXIST) => {
                                return Ok(Outcome::Retry(format!("{} is taken", path.display())));
                            }
                            Err(e) => return Err(e),
                        },
                        // Already moved (a redo), or replaced by someone else.
                        _ => {
                            if same_inode(&at_to, n)? != Some(true) {
                                return Ok(Outcome::Conflict(format!(
                                    "{} is no longer the replica's inode",
                                    from.display()
                                )));
                            }
                        }
                    }
                }
                let abs = self.layout.path(path);
                let at = self.at(path)?;
                if same_inode(&at, n)? != Some(true) {
                    return Ok(Outcome::Conflict(format!(
                        "{} is not the replica's inode",
                        path.display()
                    )));
                }
                // The daemon's own changes below move ctime; carry the stubber's
                // "unchanged since commit" check across them, but only if no
                // one else changed the file before we got here.
                let ctime_before = at.lstat().map(|st| ctime_of(&st))?;
                let tracked = n.fs_ctime_ns == Some(ctime_before);
                let mut fs_version = None;
                if *content_changed {
                    let Some(spec) = spec_for(n, op.share_id.unwrap_or(0)) else {
                        return Ok(Outcome::Retry("content change without ids".into()));
                    };
                    match handshake::replace_content(
                        self.layout,
                        path,
                        &spec,
                        self.notifier,
                        self.seq,
                        self.locks,
                    )
                    .await?
                    {
                        Some(_) => fs_version = Some(spec.version_id),
                        None => {
                            return Ok(Outcome::Retry(
                                "invalidate not acknowledged, or the file is busy".into(),
                            ));
                        }
                    }
                }
                let flags_changed = if *content_changed {
                    false
                } else {
                    match self.sync_flags(n, &at)? {
                        Some(c) => c,
                        None => {
                            return Ok(Outcome::Retry("a stub or fill holds the file".into()));
                        }
                    }
                };
                let changed = self.set_attrs(n, op.share_root, &at, &abs)?
                    || fs_version.is_some()
                    || flags_changed;
                let key = key_at(&at, n.file_id.unwrap_or(0))?;
                if changed {
                    let w = if flags_changed {
                        what::ATTRS | what::MARKER
                    } else if n.kind == Kind::Dir {
                        what::ATTRS | what::DIR
                    } else {
                        what::ATTRS
                    };
                    self.notifier.invalidate(key, w, false).await;
                }
                let ctime_ns = if tracked {
                    Some(ctime_of(&at.lstat()?))
                } else {
                    None
                };
                Ok(Outcome::Done {
                    ino: key.ino,
                    ino_gen: key.ino_gen,
                    fs_version_id: fs_version,
                    ctime_ns,
                })
            }
            Action::Remove { path } => {
                let at = self.at(path)?;
                match same_inode(&at, n)? {
                    None | Some(false) => return Ok(Outcome::Removed),
                    Some(true) => {}
                }
                let r = if n.kind == Kind::Dir {
                    at.rmdir()
                } else {
                    at.unlink()
                };
                match r {
                    Ok(()) => Ok(Outcome::Removed),
                    Err(e) if e.raw_os_error() == Some(libc::ENOTEMPTY) => Ok(Outcome::Retry(
                        "ENOTEMPTY: waiting for children's tombstones".into(),
                    )),
                    Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(Outcome::Removed),
                    Err(e) => Err(e),
                }
            }
        }
    }

    async fn create(&self, op: &PendingOp, path: &Path) -> io::Result<Outcome> {
        let n = &op.node;
        let file_id = n.file_id.unwrap_or(0);
        let perms = || -> io::Result<NewPerms> {
            let def = parent_default_acl(self.layout, path)?;
            Ok(self.perms.new_perms(n, def.as_deref()))
        };
        let created = match n.kind {
            Kind::Dir => create_dir_with_perms(
                self.layout,
                path,
                file_id,
                n.uid,
                n.gid,
                n.mode,
                n.mtime_ns,
                &perms()?,
            )?,
            Kind::Symlink => {
                let target = n.symlink_target.clone().unwrap_or_default();
                create_symlink(
                    self.layout,
                    path,
                    file_id,
                    &target,
                    n.uid,
                    n.gid,
                    n.mtime_ns,
                )?
            }
            Kind::File => {
                let Some(spec) = spec_for(n, op.share_id.unwrap_or(0)) else {
                    return Ok(Outcome::Retry(
                        "file without file_id or content version".into(),
                    ));
                };
                // set_len, fsync and linkat: off the async workers.
                let (layout, rel) = (self.layout.clone(), path.to_path_buf());
                let marker_seq = self.seq.next();
                let perms = perms()?;
                crate::blocking(move || {
                    create_placeholder_with_perms(&layout, &rel, &spec, marker_seq, &perms)
                })
                .await?
            }
        };
        Ok(match created {
            Created::Created(k) => {
                debug!(path = %path.display(), ino = k.ino, "materialised");
                Outcome::Done {
                    ino: k.ino,
                    ino_gen: k.ino_gen,
                    fs_version_id: n.content_version_id,
                    ctime_ns: None,
                }
            }
            Created::Adopted { key, version_id } => {
                if n.kind == Kind::File {
                    self.set_attrs(n, false, &self.at(path)?, &self.layout.path(path))?;
                }
                Outcome::Done {
                    ino: key.ino,
                    ino_gen: key.ino_gen,
                    fs_version_id: version_id,
                    ctime_ns: None,
                }
            }
            Created::Conflict(why) => Outcome::Conflict(why),
        })
    }

    /// Keep the marker's `ONLINE`/`ARCHIVED` flags in step with the catalog
    /// location (a tier change or a restore). Only for the marker's own
    /// content version; coverage is never touched here.
    ///
    /// Under the file's lock (`None`: a stub or fill holds it, retry), so a
    /// stub can't narrow and punch between our read and our write (writing
    /// back a stale `RESIDENT` marker over punched bytes). The write is
    /// `XATTR_REPLACE` over exactly the marker read (same `marker_seq`), so
    /// a marker the FSAL removed after a truncate is never recreated.
    fn sync_flags(&self, n: &Node, at: &sys::At) -> io::Result<Option<bool>> {
        if n.kind != Kind::File {
            return Ok(Some(false));
        }
        let Some(file_id) = n.file_id else {
            return Ok(Some(false));
        };
        let Some(_lock) = self.locks.try_lock(file_id) else {
            return Ok(None);
        };
        self.sync_flags_locked(n, at).map(Some)
    }

    fn sync_flags_locked(&self, n: &Node, at: &sys::At) -> io::Result<bool> {
        let fd = at.open_ro()?;
        let ReadOutcome::Marker(mut m) = xattr::read(fd.as_fd())? else {
            return Ok(false);
        };
        if Some(m.file_id) != n.file_id || Some(m.version_id) != n.content_version_id {
            return Ok(false);
        }
        let mut want = m.flags.without(Flags::ONLINE).without(Flags::ARCHIVED);
        if n.online {
            want = want | Flags::ONLINE;
        }
        if n.archived {
            want = want | Flags::ARCHIVED;
        }
        if want == m.flags {
            return Ok(false);
        }
        let read = m;
        m.flags = want;
        m.marker_seq = self.seq.next();
        // Exactly the marker we read, still: nothing else rewrote it.
        if !matches!(xattr::read(fd.as_fd())?, ReadOutcome::Marker(now) if now == read) {
            return Ok(false);
        }
        match xattr::write_replace(fd.as_fd(), &m) {
            Ok(()) => Ok(true),
            Err(xattr::Error::Io(e)) if e.raw_os_error() == Some(libc::ENODATA) => Ok(false),
            Err(e) => Err(io::Error::other(e.to_string())),
        }
    }

    /// Bring owner, mode and mtime to the catalog's values. Returns whether
    /// anything changed (no-op updates would move ctime for nothing).
    fn set_attrs(&self, n: &Node, share_root: bool, at: &sys::At, abs: &Path) -> io::Result<bool> {
        // One fd on the object for the check and every change: a name swapped
        // meanwhile (a hard link to another file put in its place, M7 review)
        // can't get this node's owner and mode.
        let obj = at.open_path()?;
        let st = sys::fstat(obj.as_fd())?;
        if Some(st.st_ino) != n.ino {
            return Err(io::Error::from_raw_os_error(libc::ESTALE));
        }
        let mtime = st.st_mtime * 1_000_000_000 + st.st_mtime_nsec;
        // A share's root (a dataset's root, or a shared folder): its owning
        // group is the share's Owners group, which the edge sets itself
        // (omnifile_edge::shares, apply_root) and the catalog doesn't carry.
        // Keep it.
        let gid = if (share_root || n.parent_file_id.is_none()) && n.kind == Kind::Dir {
            st.st_gid
        } else {
            n.gid
        };
        let same = st.st_uid == n.uid
            && st.st_gid == gid
            && (n.kind == Kind::Symlink || st.st_mode & 0o7777 == n.mode & 0o7777)
            && mtime == n.mtime_ns;
        if !same {
            let mode = if n.kind == Kind::Symlink {
                None
            } else {
                Some(n.mode)
            };
            sys::set_attrs_opath(obj.as_fd(), n.uid, gid, mode, n.mtime_ns)?;
        }
        // A placeholder's size follows the marker; a mismatch is fixed only by
        // a content-version change, never by truncating bytes here.
        if n.kind == Kind::File {
            let fd = at.open_ro()?;
            if let ReadOutcome::Marker(m) = xattr::read(fd.as_fd())?
                && m.size != n.size
                && Some(m.version_id) == n.content_version_id
            {
                warn!(path = %abs.display(), marker = m.size, catalog = n.size, "size differs for the same content version");
            }
        }
        Ok(!same)
    }
}

fn ctime_of(st: &libc::stat) -> i64 {
    st.st_ctime * 1_000_000_000 + st.st_ctime_nsec
}

/// Run pending ops until none is ready or `max_passes` is reached.
pub async fn run_redo<N: FsalNotifier>(
    replica: &mut Replica,
    m: &Materializer<'_, N>,
    batch: usize,
    max_passes: usize,
) -> omnifile_replica::Result<RedoStats> {
    let mut stats = RedoStats::default();
    for _ in 0..max_passes {
        let ops = replica.pending(batch)?;
        if ops.is_empty() {
            break;
        }
        stats.passes += 1;
        let mut progress = false;
        for op in &ops {
            let out = m.apply(op).await;
            match &out {
                Outcome::Done { .. } | Outcome::Removed => {
                    stats.done += 1;
                    progress = true;
                }
                Outcome::Retry(_) => stats.retried += 1,
                Outcome::Conflict(_) => {
                    stats.conflicts += 1;
                    progress = true;
                }
            }
            replica.record(op, out)?;
        }
        if !progress {
            break;
        }
    }
    Ok(stats)
}
