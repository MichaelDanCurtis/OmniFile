//! Users' renames, moves, deletes and owner changes reach the catalog
//! (`docs/design/namespace-sync.md`).
//!
//! - [`watch_loop`] turns namespace events (`omnifile_detect::ns`) into dirty
//!   directories.
//! - [`ns_loop`] reconciles each dirty directory against the replica by inode
//!   and generation ([`reconcile_dir`]), sweeps every directory now and then,
//!   and sends the queued ops ([`send_pending`]).
//!
//! Nothing is taken from an event but which directory to look at, so a lost
//! or reordered event costs only a later pass, and the daemon's own changes
//! find the replica already agreeing.
//!
//! Who made each change (`control-api.md` §4.1 `actor`) comes from hints
//! ([`crate::actor`]): the FSAL's `NS_HINT` for NFS, the watcher's names and
//! pids for SMB and local processes. An op no hint names goes `unknown`.
//!
//! Hard links are refused on shares (decision 42): the FSAL refuses NFS
//! `LINK`; a second name made over SMB or locally is split here into its
//! own copy (a reflink), which the commit pipeline commits as a separate
//! file; a second name for a placeholder whose bytes are not all local is
//! removed (after checking by handle that it is that inode's other name),
//! and audited. Two names never share one catalog file or one inode.

use std::collections::{HashMap, HashSet};
use std::ffi::CString;
use std::io;
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, FromRawFd, OwnedFd};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Duration;

use omnifile_replica::{Kind, Node};
use omnifile_stub::sys;
use serde_json::Value;
use tracing::{debug, info, warn};

use crate::actor::{self, HintOp, Key};
use crate::core::Core;
use crate::ports::{
    Actor, ActorVia, BlobIo, CloseWrite, ControlPlane, CreateOp, NsChange, NsStatus,
};

type R<T> = Result<T, Box<dyn std::error::Error + Send + Sync>>;

/// Ops sent per batch.
const BATCH: usize = 200;
/// Directories reconciled per sweep step.
const SWEEP_STEP: usize = 200;
/// A directory the materialiser is busy in is looked at again after this.
const BUSY_RETRY: Duration = Duration::from_secs(5);
/// A delete waits this many passes for something moved out of it.
const MAX_DELETE_WAITS: u32 = 6;
/// A new hard link whose first name isn't in the catalog yet waits this
/// many passes for that name's commit, then is split anyway.
const MAX_LINK_WAITS: u32 = 6;
/// A pass waits at most this long for hints being resolved (smbstatus has a
/// 3 s timeout).
const HINT_WAIT: Duration = Duration::from_secs(5);
/// A folder with no hint whose inode changed less than this ago waits one
/// pass, [`FRESH_RETRY`] later, for its hint.
const FRESH_AGE: Duration = Duration::from_secs(3);
const FRESH_RETRY: Duration = Duration::from_secs(1);
/// A file no close was seen for is handed to the commit pipeline once it
/// has been quiet this much longer than the commit quiet period: by then
/// the detector has reported any close it saw (WP9).
const UNSEEN_MARGIN: Duration = Duration::from_secs(2);
/// A file still being written (or open for write) is looked at again at
/// most this many times, backing off; after that its close (the detector)
/// or the next sweep commits it.
const MAX_UNSEEN_WAITS: u32 = 8;
/// A file handed once is not handed again for this long, unless its commit
/// is done (it is then known to the replica).
const HANDED_TTL: Duration = Duration::from_secs(30 * 60);

/// Whether `path`'s inode changed less than [`FRESH_AGE`] ago.
fn just_made(path: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    let Ok(m) = std::fs::symlink_metadata(path) else {
        return false;
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let ctime = Duration::new(
        m.ctime().max(0) as u64,
        m.ctime_nsec().clamp(0, 999_999_999) as u32,
    );
    now.saturating_sub(ctime) < FRESH_AGE
}
/// Audit actions (`control-api.md` §4.7) for hard links (decision 42).
pub const AUDIT_LINK_SPLIT: &str = "namespace.hardlink_split";
pub const AUDIT_LINK_REMOVED: &str = "namespace.hardlink_removed";

/// One entry of a directory on disk.
#[derive(Clone, Debug)]
struct Entry {
    name: String,
    ino: u64,
    /// The generation, by handle (never by opening the entry): None for what
    /// isn't a regular file or directory, or when the handle can't be read.
    generation: Option<u32>,
    fmt: u32,
    uid: u32,
    gid: u32,
    mode: u32,
    nlink: u64,
    /// The inode's change time: a hint noted before it is an earlier
    /// change's (`actor::Hints::take`).
    ctime: std::time::SystemTime,
}

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// A directory's entries changed: reconcile it soon.
    pub fn ns_mark(&self, lid: i64) {
        self.ns_dirty.lock().unwrap().insert(lid);
        self.ns_wake.notify_one();
    }
}

fn cname(name: &str) -> io::Result<CString> {
    CString::new(name).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))
}

fn fstatat(dir: BorrowedFd<'_>, name: &str) -> io::Result<libc::stat> {
    let c = cname(name)?;
    // SAFETY: st is a valid out-parameter; c is NUL-terminated.
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    let r = unsafe {
        libc::fstatat(
            dir.as_raw_fd(),
            c.as_ptr(),
            &mut st,
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if r < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(st)
}

/// `(ino, generation)` of `name` in `dir`, from its xfs file handle
/// (`name_to_handle_at`, the final component not followed): the entry is
/// never opened, so a FIFO or device swapped in is never opened as root.
fn handle_at(dir: BorrowedFd<'_>, name: &str) -> io::Result<(u64, u32)> {
    let c = cname(name)?;
    let mut h = Handle {
        bytes: 12,
        ty: 0,
        f: [0; 12],
    };
    let mut mount_id: libc::c_int = 0;
    // SAFETY: h has room for 12 handle bytes, as `bytes` says; c is
    // NUL-terminated; the result is checked.
    let r = unsafe {
        libc::syscall(
            libc::SYS_name_to_handle_at,
            dir.as_raw_fd(),
            c.as_ptr(),
            (&mut h as *mut Handle).cast::<libc::c_void>(),
            &mut mount_id as *mut libc::c_int,
            0,
        )
    };
    if r < 0 {
        return Err(io::Error::last_os_error());
    }
    match (h.ty, h.bytes) {
        (0x81, 12) => Ok((
            u64::from_ne_bytes(h.f[..8].try_into().unwrap()),
            u32::from_ne_bytes(h.f[8..12].try_into().unwrap()),
        )),
        (1, 8) => Ok((
            u32::from_ne_bytes(h.f[..4].try_into().unwrap()) as u64,
            u32::from_ne_bytes(h.f[4..8].try_into().unwrap()),
        )),
        _ => Err(io::Error::from(io::ErrorKind::Unsupported)),
    }
}

/// Whether `rel` (below the data root, reached with no symlink) is still the
/// inode `(ino, generation)`.
fn still_at(data_root: &Path, rel: &Path, ino: u64, generation: u32) -> bool {
    let (Some(parent), Some(name)) = (rel.parent(), rel.file_name().and_then(|n| n.to_str()))
    else {
        return false;
    };
    let Ok(dir) = sys::open_dir_beneath(data_root, parent) else {
        return false;
    };
    handle_at(dir.as_fd(), name).is_ok_and(|k| k == (ino, generation))
}

/// A directory's entries, if `rel` is still the directory `(ino, gen)`
/// (reached with no symlink). `None`: it moved or went; another pass finds it.
fn scan_dir(
    data_root: &Path,
    rel: &Path,
    want: (u64, u32),
) -> io::Result<Option<(OwnedFd, Vec<Entry>)>> {
    let fd = match sys::open_dir_beneath(data_root, rel) {
        Ok(fd) => fd,
        Err(e)
            if matches!(
                e.raw_os_error(),
                Some(libc::ENOENT | libc::ELOOP | libc::ENOTDIR | libc::EXDEV)
            ) =>
        {
            return Ok(None);
        }
        Err(e) => return Err(e),
    };
    let k = sys::inode_key(fd.as_fd())?;
    if (k.ino, k.ino_gen) != want {
        return Ok(None);
    }
    let mut out = Vec::new();
    for e in std::fs::read_dir(format!("/proc/self/fd/{}", fd.as_raw_fd()))? {
        let e = e?;
        // A name the catalog can't hold isn't the catalog's.
        let Some(name) = e.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        let st = match fstatat(fd.as_fd(), &name) {
            Ok(st) => st,
            Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
            Err(e) => return Err(e),
        };
        let fmt = st.st_mode & libc::S_IFMT;
        let generation = if matches!(fmt, libc::S_IFREG | libc::S_IFDIR) {
            handle_at(fd.as_fd(), &name)
                .ok()
                .filter(|(ino, _)| *ino == st.st_ino)
                .map(|(_, g)| g)
        } else {
            None
        };
        out.push(Entry {
            name,
            ino: st.st_ino,
            generation,
            fmt,
            uid: st.st_uid,
            gid: st.st_gid,
            mode: st.st_mode & 0o7777,
            #[allow(clippy::unnecessary_cast)] // u32 on aarch64, u64 on x86_64
            nlink: st.st_nlink as u64,
            ctime: std::time::UNIX_EPOCH
                + Duration::new(
                    st.st_ctime.max(0) as u64,
                    st.st_ctime_nsec.clamp(0, 999_999_999) as u32,
                ),
        });
    }
    Ok(Some((fd, out)))
}

/// Where an inode is now.
#[derive(Debug, PartialEq, Eq)]
enum Whereabouts {
    /// Deleted (no such inode, or no links left).
    Gone,
    /// Alive, at this path below the data root.
    At(PathBuf),
    /// Can't tell (an error, or a path outside the data root): do nothing.
    Unknown,
}

#[repr(C)]
struct Handle {
    bytes: libc::c_uint,
    ty: libc::c_int,
    f: [u8; 12],
}

/// Open an xfs inode by `(ino, generation)`: xfs's 64-bit handle (type 0x81:
/// the inode then the generation, host order), or its 32-bit one.
pub(crate) fn open_inode(
    mount: BorrowedFd<'_>,
    ino: u64,
    generation: u32,
    flags: libc::c_int,
) -> io::Result<OwnedFd> {
    let mut tries: Vec<Handle> = Vec::new();
    let mut h64 = Handle {
        bytes: 12,
        ty: 0x81,
        f: [0; 12],
    };
    h64.f[..8].copy_from_slice(&ino.to_ne_bytes());
    h64.f[8..].copy_from_slice(&generation.to_ne_bytes());
    tries.push(h64);
    if ino >> 32 == 0 {
        let mut h32 = Handle {
            bytes: 8,
            ty: 1,
            f: [0; 12],
        };
        h32.f[..4].copy_from_slice(&(ino as u32).to_ne_bytes());
        h32.f[4..8].copy_from_slice(&generation.to_ne_bytes());
        tries.push(h32);
    }
    let mut last = io::Error::from_raw_os_error(libc::ESTALE);
    for mut h in tries {
        // SAFETY: h is a valid struct file_handle with `bytes` bytes after it.
        let fd = unsafe {
            libc::open_by_handle_at(
                mount.as_raw_fd(),
                (&mut h as *mut Handle).cast(),
                flags | libc::O_CLOEXEC,
            )
        };
        if fd >= 0 {
            // SAFETY: fd is new and owned here.
            return Ok(unsafe { OwnedFd::from_raw_fd(fd) });
        }
        last = io::Error::last_os_error();
        if last.raw_os_error() == Some(libc::ESTALE) {
            return Err(last);
        }
    }
    Err(last)
}

fn whereabouts(mount: BorrowedFd<'_>, data_root: &Path, ino: u64, generation: u32) -> Whereabouts {
    let fd = match open_inode(mount, ino, generation, libc::O_PATH) {
        Ok(fd) => fd,
        Err(e) if e.raw_os_error() == Some(libc::ESTALE) => return Whereabouts::Gone,
        Err(e) => {
            debug!(ino, err = %e, "inode lookup by handle failed");
            return Whereabouts::Unknown;
        }
    };
    match sys::fstat(fd.as_fd()) {
        Ok(st) if st.st_nlink == 0 => return Whereabouts::Gone,
        Ok(_) => {}
        Err(_) => return Whereabouts::Unknown,
    }
    let Ok(p) = std::fs::read_link(format!("/proc/self/fd/{}", fd.as_raw_fd())) else {
        return Whereabouts::Unknown;
    };
    match p.strip_prefix(data_root) {
        Ok(rel) if !rel.as_os_str().is_empty() => Whereabouts::At(rel.to_path_buf()),
        _ => Whereabouts::Unknown,
    }
}

/// What a pass decided, applied to the replica in one go.
#[derive(Default)]
struct Plan {
    /// (lid, new name): renamed or moved into this directory.
    moves: Vec<(i64, String)>,
    /// Deleted nodes, each with the ops to send for it and what was below it
    /// (deepest first), and whether it waits for this pass's moves (something
    /// below it moved here: its rename must reach the service first).
    deletes: Vec<Delete>,
    /// (lid, uid, gid)
    owners: Vec<(i64, u32, u32)>,
    /// (lid, mode): the ACL scan reports it.
    modes: Vec<(i64, u32, PathBuf)>,
    /// New folders (relative paths), created in the catalog.
    new_dirs: Vec<PathBuf>,
    /// Directories to look at next (where a missing child now lives).
    also: Vec<PathBuf>,
    /// lids found alive in another directory the replica knows (that
    /// directory's pass renames them).
    moved_away: HashSet<i64>,
    /// Look at this directory again after this (a delete waits for a move).
    again: Option<Duration>,
    /// Second names of files the catalog has under another name (hard
    /// links, decision 42): split into their own copies.
    splits: Vec<Split>,
    /// Who made each change (`actor`), as JSON for the outbox.
    actors: Actors,
    /// Each entry's inode change time, by name (this directory's entries).
    ctimes: HashMap<String, std::time::SystemTime>,
}

/// A second name to split (decision 42).
struct Split {
    name: String,
    key: Key,
    file_id: Option<u64>,
    /// The catalogued name of the inode (below the data root), never
    /// touched: only `name` is split, or removed.
    catalog_rel: Option<PathBuf>,
}

/// The `actor` of each op a pass queues (`control-api.md` §4.1), as the
/// outbox stores it.
#[derive(Default)]
struct Actors {
    moves: HashMap<i64, Value>,
    deletes: HashMap<i64, Value>,
    owners: HashMap<i64, Value>,
    new_dirs: HashMap<PathBuf, Actor>,
}

struct Delete {
    lid: i64,
    /// The node and what was below it in the catalog that is gone too,
    /// deepest first.
    lids: Vec<i64>,
    after_moves: bool,
}

/// Reconcile one directory against the replica.
pub async fn reconcile_dir<C: ControlPlane, B: BlobIo>(core: &Arc<Core<C, B>>, lid: i64) -> R<()> {
    let (dir, rel, children, share_roots) = {
        let rep = core.replica.lock().await;
        let Some(dir) = rep.node(lid)? else {
            return Ok(());
        };
        if dir.deleted || dir.kind != Kind::Dir || dir.ino.is_none() || dir.ino_gen.is_none() {
            return Ok(());
        }
        if rep.dir_busy(lid)? {
            // The materialiser's half-done work isn't the user's.
            let core = core.clone();
            tokio::spawn(async move {
                tokio::time::sleep(BUSY_RETRY).await;
                core.ns_mark(lid);
            });
            return Ok(());
        }
        let Some(rel) = rep.fs_path(lid)? else {
            return Ok(());
        };
        let children = rep.fs_children(lid)?;
        let mut share_roots = HashSet::new();
        for c in children.iter().chain(std::iter::once(&dir)) {
            if let Some(f) = c.file_id
                && rep.is_share_root(f)?
            {
                share_roots.insert(c.lid);
            }
        }
        (dir, rel, children, share_roots)
    };
    let want = (dir.ino.unwrap_or(0), dir.ino_gen.unwrap_or(0));
    let data_root = core.layout.data_root.clone();
    let rel2 = rel.clone();
    let scanned =
        tokio::task::spawn_blocking(move || -> io::Result<_> { scan_dir(&data_root, &rel2, want) })
            .await??;
    let Some((dirfd, entries)) = scanned else {
        return Ok(());
    };
    // The daemon's own temporary names (a hard-link split or removal cut
    // short): never the user's, never committed, renamed or deleted in the
    // catalog. Left over from an earlier run, they are cleaned up.
    let (temps, entries): (Vec<Entry>, Vec<Entry>) = entries.into_iter().partition(|e| {
        e.name.starts_with(".omnifile-split-") || e.name.starts_with(".omnifile-unlink-")
    });
    if !temps.is_empty() {
        clean_leftover_temps(core, &dirfd, temps).await;
    }

    let by_name: HashMap<&str, &Node> = children
        .iter()
        .filter_map(|n| n.fs_name.as_deref().map(|name| (name, n)))
        .collect();
    let mut plan = Plan {
        ctimes: entries.iter().map(|e| (e.name.clone(), e.ctime)).collect(),
        ..Plan::default()
    };
    let mut claimed: HashSet<i64> = HashSet::new();
    let mut unmatched: Vec<&Entry> = Vec::new();
    // Regular files with one name and no commit: new to the replica, or
    // registered by a commit that was cut short. Their close may never be
    // seen (WP9): the detector's event died with a restart.
    let mut unseen: Vec<&Entry> = Vec::new();
    for e in &entries {
        match by_name.get(e.name.as_str()) {
            Some(n)
                if n.ino == Some(e.ino) && e.generation.is_some() && n.ino_gen == e.generation =>
            {
                claimed.insert(n.lid);
                if e.fmt == libc::S_IFREG
                    && e.nlink == 1
                    && !n.fs_pending
                    && crate::commit::never_committed(n)
                {
                    unseen.push(e);
                }
                // chown: owner and group (a share root's group is the edge's).
                let gid = if share_roots.contains(&n.lid) {
                    n.gid
                } else {
                    e.gid
                };
                if (e.uid, gid) != (n.uid, n.gid) {
                    plan.owners.push((n.lid, e.uid, gid));
                }
                // chmod: the ACL scan reports mode and ACL together.
                if e.mode != n.mode & 0o7777 && e.fmt != libc::S_IFLNK {
                    plan.modes.push((n.lid, e.mode, rel.join(&e.name)));
                }
            }
            _ if matches!(e.fmt, libc::S_IFREG | libc::S_IFDIR) => unmatched.push(e),
            _ => {}
        }
    }

    // Entries the replica doesn't hold by name: moved here, a second link,
    // or new.
    let mut cands: Vec<(&Entry, Node, Option<PathBuf>)> = Vec::new();
    // New files with more than one name, none of them in the catalog yet.
    let mut new_links: Vec<&Entry> = Vec::new();
    {
        let rep = core.replica.lock().await;
        for e in unmatched {
            let Some(g) = e.generation else {
                debug!(name = %e.name, "no generation: left for the next pass");
                continue;
            };
            match rep.node_by_ino(e.ino, g)? {
                Some(n) if n.lid != lid && !n.fs_pending => {
                    claimed.insert(n.lid);
                    if n.fs_parent_lid != Some(lid) || n.fs_name.as_deref() != Some(e.name.as_str())
                    {
                        let at = rep.fs_path(n.lid)?;
                        cands.push((e, n, at));
                    }
                }
                Some(_) => {}
                None if e.fmt == libc::S_IFDIR => plan.new_dirs.push(rel.join(&e.name)),
                // A new file with another name too (decision 42) waits for
                // that name's commit, then this one is split. Once it has
                // one name, it is a new file like any other.
                None if e.fmt == libc::S_IFREG && e.nlink > 1 => new_links.push(e),
                // A new file: the write detector commits it when it sees the
                // close. A close it never saw (it was waiting out the quiet
                // period when the daemon stopped, or the daemon was down) is
                // found here and committed once quiet (WP9).
                None if e.fmt == libc::S_IFREG => unseen.push(e),
                None => {}
            }
        }
    }
    if !cands.is_empty() {
        // A node whose recorded place still holds the same inode has another
        // link here (`cp -al`, `rsync --link-dest`), not a new name (M7
        // review S1): the catalog keeps one name for it.
        let checks: Vec<(Option<PathBuf>, u64, u32)> = cands
            .iter()
            .map(|(_, n, at)| (at.clone(), n.ino.unwrap_or(0), n.ino_gen.unwrap_or(0)))
            .collect();
        let data_root = core.layout.data_root.clone();
        let linked: Vec<bool> = tokio::task::spawn_blocking(move || {
            checks
                .into_iter()
                .map(|(at, ino, g)| at.is_some_and(|p| still_at(&data_root, &p, ino, g)))
                .collect()
        })
        .await?;
        for ((e, n, at), is_link) in cands.into_iter().zip(linked) {
            if is_link {
                // Not a rename, and never a second name of the same catalog
                // file (decision 42): this name gets its own copy.
                debug!(name = %e.name, lid = n.lid, "another link to a known file: split");
                if let (Some(ino), Some(g)) = (n.ino, n.ino_gen) {
                    plan.splits.push(Split {
                        name: e.name.clone(),
                        key: (ino, g),
                        file_id: n.file_id,
                        catalog_rel: at,
                    });
                }
            } else {
                plan.moves.push((n.lid, e.name.clone()));
            }
        }
    }
    for e in new_links {
        let Some(g) = e.generation else { continue };
        let tries = {
            let mut w = core.ns_link_waits.lock().unwrap();
            let t = w.entry((e.ino, g)).or_insert(0);
            *t = t.saturating_add(1);
            *t
        };
        if tries <= MAX_LINK_WAITS {
            let after = BUSY_RETRY * (1 << (tries - 1));
            debug!(name = %e.name, tries, ?after, "a new file with another name: waits for the first name's commit");
            plan.again = Some(plan.again.map_or(after, |a| a.min(after)));
        } else {
            plan.splits.push(Split {
                name: e.name.clone(),
                key: (e.ino, g),
                file_id: None,
                catalog_rel: None,
            });
        }
    }
    if !unseen.is_empty()
        && let Some(after) = hand_unseen(core, &rel, &dirfd, &unseen).await?
    {
        plan.again = Some(plan.again.map_or(after, |a| a.min(after)));
    }

    // Children the replica has here whose name is gone and which nothing
    // here claims: deleted, or moved elsewhere.
    let missing: Vec<&Node> = children
        .iter()
        .filter(|n| {
            !claimed.contains(&n.lid) && !n.fs_pending && n.ino.is_some() && n.ino_gen.is_some()
        })
        .collect();
    if !missing.is_empty() {
        let probes: Vec<(i64, u64, u32)> = missing
            .iter()
            .map(|n| (n.lid, n.ino.unwrap_or(0), n.ino_gen.unwrap_or(0)))
            .collect();
        let data_root = core.layout.data_root.clone();
        let found = tokio::task::spawn_blocking(move || -> io::Result<Vec<(i64, Whereabouts)>> {
            let mount = sys::open(&data_root, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
            Ok(probes
                .into_iter()
                .map(|(lid, ino, g)| (lid, whereabouts(mount.as_fd(), &data_root, ino, g)))
                .collect())
        })
        .await??;
        let mut gone: Vec<i64> = Vec::new();
        let mut away: Vec<(i64, PathBuf)> = Vec::new();
        for (child, w) in found {
            match w {
                Whereabouts::Gone => gone.push(child),
                Whereabouts::At(p) => {
                    if let Some(parent) = p.parent() {
                        plan.also.push(parent.to_path_buf());
                        away.push((child, parent.to_path_buf()));
                    }
                }
                Whereabouts::Unknown => {}
            }
        }
        // Moved into a directory the replica knows with a catalog id: that
        // directory's pass sends its rename, so it may be moved aside from a
        // name another node takes here (M7 review S5).
        for (child, parent) in away {
            if dir_node(core, &parent)
                .await?
                .is_some_and(|n| n.file_id.is_some())
            {
                plan.moved_away.insert(child);
            }
        }
        plan.deletes =
            plan_deletes(core, &gone, &plan.moves, &mut plan.again, &mut plan.also).await?;
    }

    plan.actors = plan_actors(core, &dir, &plan).await?;
    debug!(
        dir = lid,
        moves = plan.moves.len(),
        deletes = plan.deletes.len(),
        owners = plan.owners.len(),
        new_dirs = plan.new_dirs.len(),
        splits = plan.splits.len(),
        "reconciled"
    );
    apply_plan(core, lid, plan).await
}

/// The temporary name a hard-link split or removal of an earlier run left
/// (`.omnifile-split-<pid>-<ino>`, `.omnifile-unlink-<pid>-<ino>`), and
/// which: `Some(true)` for a split. `None`: not one, or this process's own.
fn leftover_temp(name: &str) -> Option<bool> {
    let (split, rest) = if let Some(r) = name.strip_prefix(".omnifile-split-") {
        (true, r)
    } else {
        (false, name.strip_prefix(".omnifile-unlink-")?)
    };
    let (pid, ino) = rest.split_once('-')?;
    let pid: u32 = pid.parse().ok()?;
    ino.parse::<u64>().ok()?;
    (pid != std::process::id()).then_some(split)
}

/// Remove what an earlier run's hard-link split or removal left (WP9
/// review), only where no bytes can go with it:
/// - a name whose inode has another link (`nlink >= 2`): the second name a
///   removal moved aside, or the link a split swapped out;
/// - a split's copy (one link, an inode the replica doesn't know): the
///   name it was copied from still has the bytes, and the next pass splits
///   it again.
///
/// Anything else (the last link of a catalogued file) is kept, with a
/// warning: it may be the only copy.
async fn clean_leftover_temps<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    dir: &OwnedFd,
    temps: Vec<Entry>,
) {
    let mut doomed: Vec<String> = Vec::new();
    for e in temps {
        let Some(split) = leftover_temp(&e.name) else {
            continue;
        };
        if e.fmt != libc::S_IFREG {
            continue;
        }
        let known = match e.generation {
            Some(g) => core
                .replica
                .lock()
                .await
                .node_by_ino(e.ino, g)
                .ok()
                .flatten()
                .is_some(),
            None => true,
        };
        if e.nlink >= 2 || (split && !known) {
            doomed.push(e.name);
        } else {
            warn!(name = %e.name, "a temporary name left by an earlier run holds the last link of a catalogued file: kept");
        }
    }
    if doomed.is_empty() {
        return;
    }
    let Ok(d) = dir.try_clone() else { return };
    let removed = tokio::task::spawn_blocking(move || {
        doomed
            .into_iter()
            .filter(|n| sys::unlinkat(d.as_fd(), std::ffi::OsStr::new(n)).is_ok())
            .count()
    })
    .await
    .unwrap_or(0);
    if removed > 0 {
        info!(removed, "temporary names left by an earlier run removed");
    }
}

/// What the sweep found when it looked at a file no close was seen for.
#[derive(Debug, PartialEq, Eq)]
enum Unseen {
    /// Open for write nowhere: commit it.
    Ready,
    /// Someone has it open for write: look again later.
    Busy,
    /// Not a user's new file (the edge's own: it carries a marker, or it is
    /// a recall note), or no longer the inode the pass saw.
    Skip,
}

/// Look at `name` in `dir` without following a symlink and without opening
/// anything but the regular file `ino` (an `O_PATH` handle first, checked,
/// then reopened read-only: a FIFO or device swapped in is never opened).
fn look_at_unseen(dir: BorrowedFd<'_>, name: &str, ino: u64) -> io::Result<Unseen> {
    let os = std::ffi::OsStr::new(name);
    let p = match sys::openat_nofollow(dir, os, libc::O_PATH, 0) {
        Ok(p) => p,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Unseen::Skip),
        Err(e) => return Err(e),
    };
    let st = sys::fstat(p.as_fd())?;
    if st.st_mode & libc::S_IFMT != libc::S_IFREG || st.st_ino != ino {
        return Ok(Unseen::Skip);
    }
    let ro = sys::reopen(p.as_fd(), libc::O_RDONLY | libc::O_NONBLOCK)?;
    if omnifile_stub::placeholder::is_note(ro.as_fd(), os) {
        return Ok(Unseen::Skip);
    }
    match omnifile_marker::xattr::read(ro.as_fd()) {
        Ok(omnifile_marker::xattr::ReadOutcome::Unmanaged) => {}
        _ => return Ok(Unseen::Skip),
    }
    Ok(if sys::open_for_write_elsewhere(ro.as_fd())? {
        Unseen::Busy
    } else {
        Unseen::Ready
    })
}

/// Files in this directory that no close was seen for (WP9): a user's new
/// file whose close the detector was still waiting out the quiet period for
/// when the daemon stopped, one written while it was down, or one whose
/// commit was cut short after its `create`. Those quiet for longer than the
/// commit quiet period and open for write nowhere go to the commit pipeline
/// as a close no one saw ([`CloseWrite::swept_gen`]), which checks it all
/// again before committing. Returns when to look at the directory again, for
/// those still being written.
async fn hand_unseen<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    rel: &Path,
    dir: &OwnedFd,
    unseen: &[&Entry],
) -> R<Option<Duration>> {
    if !core.cfg.commit.enabled {
        return Ok(None);
    }
    let Some(tx) = core.commit_tx.get().cloned() else {
        return Ok(None);
    };
    let quiet = core.quiet.of_rel(rel) + UNSEEN_MARGIN;
    let now = std::time::SystemTime::now();
    // (inode, at least this long before the next look)
    let mut not_yet: Vec<(Key, Duration)> = Vec::new();
    let mut look: Vec<(String, u64, u32)> = Vec::new();
    {
        let handed = core.sweep_handed.lock().unwrap();
        for e in unseen {
            let Some(g) = e.generation else { continue };
            if e.name.starts_with(".omnifile-") {
                continue; // the daemon's own temporary name
            }
            if handed
                .get(&(e.ino, g))
                .is_some_and(|t| t.elapsed() < HANDED_TTL)
            {
                continue;
            }
            // ctime: it moves with every write, and with a `touch -d` or a
            // `cp -p` that puts back an old mtime.
            let age = now.duration_since(e.ctime).unwrap_or_default();
            if age < quiet {
                not_yet.push(((e.ino, g), quiet - age));
            } else {
                look.push((e.name.clone(), e.ino, g));
            }
        }
    }
    let ready: Vec<(String, u64, u32)> = if look.is_empty() {
        Vec::new()
    } else {
        let d = dir.try_clone()?;
        let looked = tokio::task::spawn_blocking(move || {
            look.into_iter()
                .map(|(name, ino, g)| {
                    let r = look_at_unseen(d.as_fd(), &name, ino);
                    (name, ino, g, r)
                })
                .collect::<Vec<_>>()
        })
        .await?;
        let mut ready = Vec::new();
        for (name, ino, g, r) in looked {
            match r {
                Ok(Unseen::Ready) => ready.push((name, ino, g)),
                Ok(Unseen::Busy) => not_yet.push(((ino, g), Duration::ZERO)),
                Ok(Unseen::Skip) => {}
                Err(e) => debug!(name, err = %e, "a file with no close seen: not readable now"),
            }
        }
        ready
    };
    let mut again: Option<Duration> = None;
    {
        let mut waits = core.sweep_waits.lock().unwrap();
        if waits.len() > 100_000 {
            waits.clear();
        }
        for (key, at_least) in not_yet {
            let t = waits.entry(key).or_insert(0);
            *t = t.saturating_add(1);
            if *t > MAX_UNSEEN_WAITS {
                continue;
            }
            let backoff = (BUSY_RETRY * (1 << (*t - 1).min(6))).min(Duration::from_secs(300));
            // The first look comes as soon as the quiet period is over: a
            // file closed just before a restart is committed then.
            let after = if *t == 1 {
                at_least.max(FRESH_RETRY)
            } else {
                at_least.max(backoff)
            };
            again = Some(again.map_or(after, |a: Duration| a.min(after)));
        }
        for (_, ino, g) in &ready {
            waits.remove(&(*ino, *g));
        }
    }
    if ready.is_empty() {
        return Ok(again);
    }
    {
        let mut handed = core.sweep_handed.lock().unwrap();
        if handed.len() > 100_000 {
            handed.retain(|_, t| t.elapsed() < HANDED_TTL);
        }
        for (_, ino, g) in &ready {
            handed.insert((*ino, *g), std::time::Instant::now());
        }
    }
    info!(
        dir = %rel.display(),
        files = ready.len(),
        "files no close was seen for: handing them to the commit pipeline"
    );
    let paths: Vec<(PathBuf, u64, u32)> = ready
        .into_iter()
        .map(|(name, ino, g)| (core.layout.path(&rel.join(name)), ino, g))
        .collect();
    let core = core.clone();
    // Not from this pass: a full queue (a backlog of big commits) must not
    // hold up the namespace sync.
    tokio::spawn(async move {
        for (path, ino, g) in paths {
            // As the detector's closes are: one permission model for NFS and
            // SMB, and the ACL reported in the next on-change scan.
            let p = path.clone();
            let _ = tokio::task::spawn_blocking(move || crate::acl::normalize_path(&p)).await;
            core.acl_dirty.lock().unwrap().insert(path.clone());
            let cw = CloseWrite {
                path,
                ino,
                swept_gen: Some(g),
                token: None,
                seen: None,
            };
            if tx.send(cw).await.is_err() {
                return;
            }
        }
    });
    Ok(again)
}

/// One op whose actor is looked up.
struct Ask {
    op: HintOp,
    lid: i64,
    /// Its inode.
    target: Option<Key>,
    /// Its directory's inode and its name.
    place: Option<(Key, String)>,
    /// When its inode changed, if it is still there.
    changed: Option<std::time::SystemTime>,
    /// Look, but leave the hint for the op it belongs to.
    peek: bool,
}

/// Who made each change the pass found (`control-api.md` §4.1 `actor`):
/// the latest hint that names it ([`crate::actor`]), else `unknown`. NFS
/// and local actors get their SID here, through winbind.
async fn plan_actors<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    dir: &Node,
    plan: &Plan,
) -> R<Actors> {
    let dkey: Option<Key> = dir.ino.zip(dir.ino_gen);
    let at = |name: &str| dkey.map(|k| (k, name.to_owned()));
    let mut asks: Vec<Ask> = Vec::new();
    {
        let rep = core.replica.lock().await;
        let key_of =
            |lid: i64| -> R<Option<Key>> { Ok(rep.node(lid)?.and_then(|n| n.ino.zip(n.ino_gen))) };
        let changed = |name: &str| plan.ctimes.get(name).copied();
        for (lid, name) in &plan.moves {
            asks.push(Ask {
                op: HintOp::Rename,
                lid: *lid,
                target: key_of(*lid)?,
                place: at(name),
                changed: changed(name),
                peek: false,
            });
        }
        for l in &plan.moved_away {
            // Moved aside first (names swapped): its own rename's hint, left
            // for the pass that sends that rename.
            asks.push(Ask {
                op: HintOp::Rename,
                lid: *l,
                target: key_of(*l)?,
                place: None,
                changed: None,
                peek: true,
            });
        }
        for d in &plan.deletes {
            for l in &d.lids {
                let Some(n) = rep.node(*l)? else { continue };
                let parent = match n.fs_parent_lid {
                    Some(p) => rep.node(p)?.and_then(|p| p.ino.zip(p.ino_gen)),
                    None => None,
                };
                let place = parent.zip(n.fs_name.clone());
                asks.push(Ask {
                    op: HintOp::Delete,
                    lid: *l,
                    target: n.ino.zip(n.ino_gen),
                    place,
                    changed: None,
                    peek: false,
                });
            }
        }
        for (lid, _, _) in &plan.owners {
            let n = rep.node(*lid)?;
            let name = n.as_ref().and_then(|n| n.fs_name.clone());
            asks.push(Ask {
                op: HintOp::Attrib,
                lid: *lid,
                target: n.and_then(|n| n.ino.zip(n.ino_gen)),
                place: name.as_deref().and_then(at),
                changed: name.as_deref().and_then(changed),
                peek: false,
            });
        }
    }
    // Each new folder's own (ino, generation), by handle (nothing opened).
    let new_keys: Vec<Option<Key>> = {
        let data_root = core.layout.data_root.clone();
        let rels = plan.new_dirs.clone();
        tokio::task::spawn_blocking(move || {
            rels.iter()
                .map(|rel| {
                    let (parent, name) = (rel.parent()?, rel.file_name()?.to_str()?);
                    let fd = sys::open_dir_beneath(&data_root, parent).ok()?;
                    handle_at(fd.as_fd(), name).ok()
                })
                .collect()
        })
        .await?
    };
    let mut out = Actors::default();
    let mut found: Vec<(HintOp, i64, Actor)> = Vec::new();
    let mut dirs: Vec<(PathBuf, Actor)> = Vec::new();
    {
        // Each hint is used for one op only, and never for a change made
        // after it was noted (Codex review of #25): an op with no hint of its
        // own is `unknown`, never an earlier actor's.
        let mut hints = core.ns_hints.lock().unwrap();
        for k in asks {
            let place = k.place.as_ref().map(|(d, n)| (*d, n.as_str()));
            let a = if k.peek {
                hints.peek(k.op, k.target, place, k.changed)
            } else {
                hints.take(k.op, k.target, place, k.changed)
            };
            found.push((k.op, k.lid, a.unwrap_or_else(Actor::unknown)));
        }
        for (rel, key) in plan.new_dirs.iter().zip(&new_keys) {
            let name = rel.file_name().and_then(|n| n.to_str()).unwrap_or_default();
            let place = dkey.map(|k| (k, name));
            let changed = plan.ctimes.get(name).copied();
            // By the folder's own inode first. Samba makes a folder under a
            // temporary name (`.::TMPNAME:D:…`) and renames it into place:
            // whoever moved it in made it. The rename's ctime is later than
            // the create's hint, so the create is looked up with no floor.
            let a = hints
                .take(HintOp::Create, *key, place, None)
                .or_else(|| hints.take(HintOp::Rename, *key, place, changed))
                .unwrap_or_else(Actor::unknown);
            dirs.push((rel.clone(), a));
        }
    }
    let mut sids: HashMap<u32, Option<String>> = HashMap::new();
    for (op, lid, mut a) in found {
        with_sid(core, &mut a, &mut sids).await;
        let v = serde_json::to_value(&a)?;
        match op {
            HintOp::Rename => out.moves.insert(lid, v),
            HintOp::Delete => out.deletes.insert(lid, v),
            HintOp::Attrib => out.owners.insert(lid, v),
            HintOp::Create => None,
        };
    }
    for (rel, mut a) in dirs {
        with_sid(core, &mut a, &mut sids).await;
        out.new_dirs.insert(rel, a);
    }
    Ok(out)
}

/// An NFS caller's or a local process's SID, from its uid (winbind, cached).
async fn with_sid<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    a: &mut Actor,
    cache: &mut HashMap<u32, Option<String>>,
) {
    let (Some(uid), None) = (a.uid, a.sid.as_ref()) else {
        return;
    };
    if !matches!(a.via, ActorVia::Nfs | ActorVia::Local) {
        return;
    }
    let Some(idmap) = core.identity.get().map(|i| i.idmap.clone()) else {
        return;
    };
    if let Some(s) = cache.get(&uid) {
        a.sid = s.clone();
        return;
    }
    let sid = tokio::task::spawn_blocking(move || {
        idmap
            .uid_to_sid(uid)
            .ok()
            .flatten()
            .filter(|s| !s.is_unix_fallback())
            .map(|s| s.as_str().to_owned())
    })
    .await
    .ok()
    .flatten();
    cache.insert(uid, sid.clone());
    a.sid = sid;
}

/// The replica node of the directory at `rel` (no symlink on the way).
async fn dir_node<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    rel: &Path,
) -> R<Option<Node>> {
    let data_root = core.layout.data_root.clone();
    let rel = rel.to_path_buf();
    let key = tokio::task::spawn_blocking(move || {
        sys::open_dir_beneath(&data_root, &rel).and_then(|fd| sys::inode_key(fd.as_fd()))
    })
    .await?;
    match key {
        Ok(k) => Ok(core.replica.lock().await.node_by_ino(k.ino, k.ino_gen)?),
        Err(_) => Ok(None),
    }
}

/// What each deleted node takes with it (M7 review B1). Its catalog subtree,
/// less what this pass moves here (and what is below that). Everything else
/// that was on disk is looked up by handle: if any of it is alive somewhere
/// else, its move hasn't been seen yet, and unlinking it would tombstone a
/// live file, so the delete waits for a later pass (`again`).
async fn plan_deletes<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    gone: &[i64],
    moves: &[(i64, String)],
    again: &mut Option<Duration>,
    also: &mut Vec<PathBuf>,
) -> R<Vec<Delete>> {
    let moving: HashSet<i64> = moves.iter().map(|(l, _)| *l).collect();
    let mut out = Vec::new();
    for &g in gone {
        let subtree = core.replica.lock().await.subtree_postorder(g)?;
        let mut excluded: HashSet<i64> = HashSet::new();
        for n in subtree.iter().rev() {
            // Shallowest first, so a moved folder takes what is below it.
            if moving.contains(&n.lid) || n.parent_lid.is_some_and(|p| excluded.contains(&p)) {
                excluded.insert(n.lid);
            }
        }
        let keep: Vec<&Node> = subtree
            .iter()
            .filter(|n| !excluded.contains(&n.lid))
            .collect();
        let probes: Vec<(i64, u64, u32)> = keep
            .iter()
            .filter(|n| n.lid != g)
            .filter_map(|n| Some((n.lid, n.ino?, n.ino_gen?)))
            .collect();
        let data_root = core.layout.data_root.clone();
        let found = tokio::task::spawn_blocking(move || -> io::Result<Vec<(i64, Whereabouts)>> {
            let mount = sys::open(&data_root, libc::O_RDONLY | libc::O_DIRECTORY, 0)?;
            Ok(probes
                .into_iter()
                .map(|(l, ino, gn)| (l, whereabouts(mount.as_fd(), &data_root, ino, gn)))
                .collect())
        })
        .await??;
        let mut alive: HashSet<i64> = HashSet::new();
        for (l, w) in found {
            match w {
                Whereabouts::Gone => {}
                Whereabouts::At(p) => {
                    alive.insert(l);
                    if let Some(parent) = p.parent() {
                        also.push(parent.to_path_buf());
                    }
                }
                Whereabouts::Unknown => {
                    alive.insert(l);
                }
            }
        }
        let mut lids: Vec<i64> = keep.iter().map(|n| n.lid).collect();
        if !alive.is_empty() {
            let tries = {
                let mut d = core.ns_defers.lock().unwrap();
                let t = d.entry(g).or_insert(0);
                *t += 1;
                *t
            };
            if tries <= MAX_DELETE_WAITS {
                // Backing off (5 s, 10 s, ... about 5 min in all); each wait
                // looks the subtree up again (M7 re-review R3).
                let after = BUSY_RETRY * (1 << (tries - 1));
                info!(
                    lid = g,
                    tries,
                    ?after,
                    "a deleted folder had something moved out of it: its delete waits for that move"
                );
                *again = Some(again.map_or(after, |a| a.min(after)));
                continue;
            }
            // Still alive after the waits (moved somewhere no pass adopts, or
            // not found): what is gone goes; what is alive stays, with every
            // folder above it (the catalog removes only empty folders).
            let parent_of: HashMap<i64, Option<i64>> =
                subtree.iter().map(|n| (n.lid, n.parent_lid)).collect();
            let mut stays: HashSet<i64> = HashSet::new();
            for l in &alive {
                let mut cur = Some(*l);
                while let Some(c) = cur {
                    if !stays.insert(c) {
                        break;
                    }
                    cur = parent_of.get(&c).copied().flatten();
                }
            }
            lids.retain(|l| !stays.contains(l));
            warn!(
                lid = g,
                alive = alive.len(),
                "a deleted folder still has something alive below it: only what is gone is deleted"
            );
            core.replica
                .lock()
                .await
                .note_conflict(g, "delete_waits_on_alive_descendant")?;
        }
        core.ns_defers.lock().unwrap().remove(&g);
        if lids.is_empty() {
            continue;
        }
        out.push(Delete {
            lid: g,
            lids,
            after_moves: !excluded.is_empty(),
        });
    }
    Ok(out)
}

fn new_op_id<C: ControlPlane, B: BlobIo>(
    core: &Core<C, B>,
    rep: &mut omnifile_replica::Replica,
) -> R<String> {
    Ok(format!("{}-{}", core.cfg.edge_id, rep.next_counter("op")?))
}

async fn apply_plan<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    dir_lid: i64,
    plan: Plan,
) -> R<()> {
    let mut queued = 0usize;
    {
        let mut rep = core.replica.lock().await;
        // Deletes first, so a rename onto a deleted file's name finds the
        // name free in the catalog; but not a folder something below which
        // moved here: its delete follows those renames.
        for d in plan.deletes.iter().filter(|d| !d.after_moves) {
            queued += queue_delete(core, &mut rep, dir_lid, d, &plan.actors)?;
        }
        // Names swapped (a <-> b, or a chain): a node that moved here may
        // take a name the catalog still gives another node that moved too.
        // That one goes to a temporary name first, so the service never
        // finds the name taken; its own rename follows. A holder that didn't
        // move (a catalog file the edge couldn't place) keeps its name, and
        // the service answers `conflict`.
        let moving: HashSet<i64> = plan.moves.iter().map(|(l, _)| *l).collect();
        for (lid, name) in &plan.moves {
            if let Some(holder) = rep.live_child_named(dir_lid, name)?
                && holder != *lid
                && (moving.contains(&holder) || plan.moved_away.contains(&holder))
            {
                let op = new_op_id(core, &mut rep)?;
                // A name no node here has (the op counter makes it unique).
                let mut tmp = format!(".omnifile-mv-{holder}-{}", rep.next_counter("op")?);
                while rep.live_child_named(dir_lid, &tmp)?.is_some() {
                    tmp = format!(".omnifile-mv-{holder}-{}", rep.next_counter("op")?);
                }
                info!(dir = dir_lid, lid = holder, name = %name, "a name another node moved onto: moving the old holder aside first");
                rep.queue_rename(holder, dir_lid, &tmp, &op, plan.actors.moves.get(&holder))?;
                queued += 1;
            }
        }
        for (lid, name) in &plan.moves {
            let op = new_op_id(core, &mut rep)?;
            info!(dir = dir_lid, lid, name = %name, "renamed or moved on the edge: telling the service");
            rep.local_moved(*lid, dir_lid, name, &op, plan.actors.moves.get(lid))?;
            queued += 1;
        }
        for d in plan.deletes.iter().filter(|d| d.after_moves) {
            queued += queue_delete(core, &mut rep, dir_lid, d, &plan.actors)?;
        }
        for (lid, uid, gid) in &plan.owners {
            let op = new_op_id(core, &mut rep)?;
            rep.local_owner(*lid, *uid, *gid, &op, plan.actors.owners.get(lid))?;
            queued += 1;
        }
        for (lid, mode, _) in &plan.modes {
            rep.local_mode(*lid, *mode)?;
        }
    }
    if !plan.modes.is_empty() {
        let mut dirty = core.acl_dirty.lock().unwrap();
        for (_, _, rel) in &plan.modes {
            dirty.insert(core.layout.path(rel));
        }
    }
    if !plan.splits.is_empty() {
        let rel = match core.replica.lock().await.fs_path(dir_lid)? {
            Some(r) => r,
            None => PathBuf::new(),
        };
        split_links(core, dir_lid, &rel, &plan.splits).await;
    }
    if queued > 0 && !plan.new_dirs.is_empty() {
        // Renames and deletes before creates (line B, 81fa187): a new folder
        // may take a name one of them frees (`mv X Y; mkdir X`, made while
        // the daemon was down), and the catalog still gives it to the old
        // folder until the rename is applied.
        if let Err(e) = send_pending(core).await {
            debug!(err = %e, "sending this pass's ops before its creates failed: they follow");
        }
    }
    let mut fresh_wait = false;
    for rel in &plan.new_dirs {
        let who = plan.actors.new_dirs.get(rel).cloned();
        // A folder made a moment ago with no hint yet (its event may still be
        // on its way, or its session being resolved): looked at once more a
        // second later, so it isn't put on nobody for a race.
        if who.as_ref().is_none_or(Actor::is_unknown) && just_made(&core.layout.path(rel)) {
            let first = core.ns_fresh_waits.lock().unwrap().insert(rel.clone());
            if first {
                fresh_wait = true;
                continue;
            }
        }
        core.ns_fresh_waits.lock().unwrap().remove(rel);
        match core.ensure_dir_as(rel, who).await {
            // What is already in it (`mkdir -p a/b/c`, a folder moved in from
            // outside the share) is looked at now, not at the next sweep.
            Ok(n) => core.ns_mark(n.lid),
            Err(e) => {
                warn!(path = %rel.display(), err = %e, "a new folder couldn't be created in the catalog: next pass")
            }
        }
    }
    let again = match (plan.again, fresh_wait) {
        (Some(a), true) => Some(a.min(FRESH_RETRY)),
        (None, true) => Some(FRESH_RETRY),
        (a, false) => a,
    };
    if let Some(after) = again {
        let core = core.clone();
        tokio::spawn(async move {
            tokio::time::sleep(after).await;
            core.ns_mark(dir_lid);
        });
    }
    for rel in &plan.also {
        let data_root = core.layout.data_root.clone();
        let rel = rel.clone();
        let key = tokio::task::spawn_blocking(move || {
            sys::open_dir_beneath(&data_root, &rel).and_then(|fd| sys::inode_key(fd.as_fd()))
        })
        .await?;
        if let Ok(k) = key
            && let Some(n) = core.replica.lock().await.node_by_ino(k.ino, k.ino_gen)?
        {
            core.ns_mark(n.lid);
        }
    }
    if queued > 0 {
        core.ns_wake.notify_one();
    }
    Ok(())
}

fn queue_delete<C: ControlPlane, B: BlobIo>(
    core: &Core<C, B>,
    rep: &mut omnifile_replica::Replica,
    dir_lid: i64,
    d: &Delete,
    actors: &Actors,
) -> R<usize> {
    let mut ops = Vec::with_capacity(d.lids.len());
    for l in &d.lids {
        ops.push((*l, new_op_id(core, rep)?, actors.deletes.get(l).cloned()));
    }
    info!(
        dir = dir_lid,
        lid = d.lid,
        nodes = ops.len(),
        "deleted on the edge: telling the service"
    );
    rep.local_deleted(&ops)?;
    Ok(ops.len())
}

/// Send the queued ops, oldest first, until none is left or one waits on a
/// catalog id it doesn't have yet. Returns whether all were sent.
pub async fn send_pending<C: ControlPlane, B: BlobIo>(core: &Arc<Core<C, B>>) -> R<bool> {
    // Creates put off in this call (a name another op is freeing): skipped
    // until the next call, so the ops behind them still go.
    let mut deferred: HashSet<String> = HashSet::new();
    let mut deferred_lids: HashSet<i64> = HashSet::new();
    let mut refetch: Vec<i64> = Vec::new();
    let all = loop {
        let rows = core
            .replica
            .lock()
            .await
            .ns_unacked(BATCH, core.cfg.namespace.create_resend_s as i64)?;
        if rows.iter().all(|r| deferred.contains(&r.op_id)) {
            break deferred.is_empty();
        }
        let mut batch: Vec<(NsChange, i64)> = Vec::new();
        let mut blocked = false;
        for row in &rows {
            if deferred.contains(&row.op_id) {
                continue;
            }
            let Some(lid) = row.lid else {
                core.replica
                    .lock()
                    .await
                    .ns_refused(&row.op_id, 0, "no_node")?;
                continue;
            };
            if row.op == "create" {
                if !batch.is_empty() {
                    break; // a create goes on its own, after what came before
                }
                match resend_create(core, &row.op_id, lid, &row.payload).await? {
                    Resent::Done => {}
                    Resent::Wait => {
                        blocked = true; // its parent first, or the service is away
                        break;
                    }
                    Resent::Defer => {
                        deferred.insert(row.op_id.clone());
                        deferred_lids.insert(lid);
                    }
                }
                continue;
            }
            let rep = core.replica.lock().await;
            let Some(node) = rep.node(lid)? else {
                drop(rep);
                core.replica
                    .lock()
                    .await
                    .ns_refused(&row.op_id, lid, "no_node")?;
                continue;
            };
            let Some(file_id) = node.file_id else {
                if rep.create_pending(lid)? {
                    if deferred_lids.contains(&lid) {
                        // Its create was put off: it waits with it, and the
                        // ops behind it go (M7 re-review R1).
                        deferred.insert(row.op_id.clone());
                        continue;
                    }
                    blocked = true; // its create comes first
                    break;
                }
                drop(rep);
                // Never in the catalog: a delete has nothing to do, anything
                // else can't be said.
                let mut rep = core.replica.lock().await;
                if row.op == "unlink" {
                    rep.ns_applied(&row.op_id, lid, None)?;
                } else {
                    rep.ns_refused(&row.op_id, lid, "not_in_catalog")?;
                }
                continue;
            };
            // Who made it, as the pass recorded it (`control-api.md` §4.1).
            let actor: Option<Actor> = row
                .payload
                .get("actor")
                .and_then(|a| serde_json::from_value(a.clone()).ok());
            let change = match row.op.as_str() {
                "rename" => {
                    let parent_lid = row.payload["new_parent_lid"].as_i64().unwrap_or(-1);
                    let new_name = row.payload["new_name"]
                        .as_str()
                        .unwrap_or_default()
                        .to_string();
                    let Some(parent) = rep.node(parent_lid)? else {
                        drop(rep);
                        core.replica
                            .lock()
                            .await
                            .ns_refused(&row.op_id, lid, "no_parent")?;
                        continue;
                    };
                    let Some(new_parent_file_id) = parent.file_id else {
                        if rep.create_pending(parent_lid)? {
                            if deferred_lids.contains(&parent_lid) {
                                deferred.insert(row.op_id.clone());
                                continue;
                            }
                            blocked = true;
                            break;
                        }
                        drop(rep);
                        core.replica.lock().await.ns_refused(
                            &row.op_id,
                            lid,
                            "parent_not_in_catalog",
                        )?;
                        continue;
                    };
                    NsChange::Rename {
                        op_id: row.op_id.clone(),
                        file_id,
                        new_parent_file_id,
                        new_name,
                        actor,
                    }
                }
                "unlink" => NsChange::Unlink {
                    op_id: row.op_id.clone(),
                    file_id,
                    actor,
                },
                _ => NsChange::Owner {
                    op_id: row.op_id.clone(),
                    file_id,
                    uid: row.payload["uid"].as_u64().unwrap_or(0) as u32,
                    gid: row.payload["gid"].as_u64().unwrap_or(0) as u32,
                    actor,
                },
            };
            batch.push((change, lid));
        }
        if batch.is_empty() {
            if blocked {
                break false;
            }
            continue;
        }
        {
            let mut rep = core.replica.lock().await;
            for (c, _) in &batch {
                rep.outbox_set_state(c.op_id(), "sent", None)?;
            }
        }
        let ops: Vec<NsChange> = batch.iter().map(|(c, _)| c.clone()).collect();
        let results = match core.control.ns_changes(&ops).await {
            Ok(r) => r,
            Err(e) if e.unavailable => {
                debug!(err = %e, "service unavailable: namespace ops wait");
                break false;
            }
            Err(e) => {
                // A refusal of the whole batch: send them one by one next
                // time, so one bad op can't hold the rest.
                warn!(err = %e, n = ops.len(), "the service refused a batch of namespace ops");
                if ops.len() == 1 {
                    let (c, lid) = &batch[0];
                    core.replica.lock().await.ns_refused(
                        c.op_id(),
                        *lid,
                        e.code.as_deref().unwrap_or("rejected"),
                    )?;
                    continue;
                }
                for (c, lid) in &batch {
                    let one = core.control.ns_changes(std::slice::from_ref(c)).await;
                    let mut rep = core.replica.lock().await;
                    match one {
                        Ok(rs) => {
                            if record(&mut rep, c.op_id(), *lid, rs.first())? {
                                refetch.push(*lid);
                            }
                        }
                        Err(e) if e.unavailable => return Ok(false),
                        Err(e) => rep.ns_refused(
                            c.op_id(),
                            *lid,
                            e.code.as_deref().unwrap_or("rejected"),
                        )?,
                    }
                }
                continue;
            }
        };
        let by_op: HashMap<&str, &crate::ports::NsResult> =
            results.iter().map(|r| (r.op_id.as_str(), r)).collect();
        let mut rep = core.replica.lock().await;
        for (c, lid) in &batch {
            if record(&mut rep, c.op_id(), *lid, by_op.get(c.op_id()).copied())? {
                refetch.push(*lid);
            }
        }
    };
    for lid in refetch {
        if let Err(e) = refetch_node(core, lid).await {
            debug!(lid, err = %e, "fetching a node again failed: the next event or the digest has it");
        }
    }
    Ok(all)
}

/// Feed events for the node were held while its op was in flight (M7
/// review S3): its row is fetched again (a snapshot page of one file).
async fn refetch_node<C: ControlPlane, B: BlobIo>(core: &Arc<Core<C, B>>, lid: i64) -> R<()> {
    let Some(n) = core.replica.lock().await.node(lid)? else {
        return Ok(());
    };
    let Some(f) = n.file_id else {
        return Ok(());
    };
    let page = core
        .control
        .snapshot(n.dataset_id, Some(f.saturating_sub(1)), 1)
        .await?;
    let rows: Vec<serde_json::Value> = page
        .files
        .into_iter()
        .filter(|r| r["file_id"].as_str().and_then(|s| s.parse::<u64>().ok()) == Some(f))
        .collect();
    if !rows.is_empty() {
        core.replica.lock().await.apply_snapshot_rows(&rows)?;
        debug!(lid, file_id = f, "fetched again after a hold");
    }
    Ok(())
}

/// Record one op's answer. Whether the node's row is to be fetched again.
fn record(
    rep: &mut omnifile_replica::Replica,
    op_id: &str,
    lid: i64,
    r: Option<&crate::ports::NsResult>,
) -> R<bool> {
    match r {
        Some(r) if r.status == NsStatus::Applied => {
            return Ok(rep.ns_applied(op_id, lid, r.entity_version)?);
        }
        Some(r) => {
            let why = r.code.clone().unwrap_or_else(|| format!("{:?}", r.status));
            warn!(op_id, lid, reason = %why, "the service kept its own state: the edge keeps the user's");
            rep.ns_refused(op_id, lid, &why)?;
        }
        // No answer for this op: it stays sent, and is sent again.
        None => {}
    }
    Ok(false)
}

/// What a create sent again came to.
enum Resent {
    /// Its state moved on (created, refused, or abandoned).
    Done,
    /// Not now: its parent's create comes first, or the service is away.
    Wait,
    /// Refused while renames or unlinks are in flight: the name it wants may
    /// be one of those being freed (`rm a; cp x a`). Tried again later.
    Defer,
}

/// A create whose answer was lost, or that failed: sent again with its op id
/// (the service answers the first result again).
async fn resend_create<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    op_id: &str,
    lid: i64,
    payload: &serde_json::Value,
) -> R<Resent> {
    let (node, parent) = {
        let rep = core.replica.lock().await;
        let Some(node) = rep.node(lid)? else {
            drop(rep);
            core.replica.lock().await.abandon_create(op_id)?;
            return Ok(Resent::Done);
        };
        let parent = node.parent_lid.map(|p| rep.node(p)).transpose()?.flatten();
        (node, parent)
    };
    if let Some(file_id) = node.file_id {
        let mut rep = core.replica.lock().await;
        rep.outbox_set_state(
            op_id,
            "acked",
            Some(&serde_json::json!({"file_id": file_id})),
        )?;
        return Ok(Resent::Done);
    }
    // Gone from the disk before its create was acknowledged (a temporary
    // file, `mkdir` then `rmdir`). Its answer may only have been lost, so it
    // is sent once more with its op id: if the service made it, the same
    // answer comes back, the node gets its catalog id, and the unlink queued
    // for it follows (M7 re-review R2). Otherwise it is dropped, and what was
    // queued for it resolves without a catalog id (M7 review B2).
    let gone = node.deleted || node.ino.is_none();
    let ino = node.ino.or_else(|| payload["ino"].as_u64()).unwrap_or(0);
    let ino_gen = node
        .ino_gen
        .or_else(|| payload["ino_gen"].as_u64().map(|g| g as u32))
        .unwrap_or(0);
    let Some(parent_file_id) = parent.as_ref().and_then(|p| p.file_id) else {
        if gone {
            core.replica.lock().await.abandon_create(op_id)?;
            return Ok(Resent::Done);
        }
        let waiting = match &parent {
            Some(p) => core.replica.lock().await.create_pending(p.lid)?,
            None => false,
        };
        if waiting {
            return Ok(Resent::Wait);
        }
        // Its folder will never be in the catalog: neither will it.
        core.replica
            .lock()
            .await
            .ns_refused(op_id, lid, "parent_not_in_catalog")?;
        return Ok(Resent::Done);
    };
    let op = CreateOp {
        op_id: op_id.to_string(),
        parent_file_id,
        name: node.name.clone(),
        kind: node.kind.as_str().into(),
        fsid: core.fsid,
        ino,
        ino_gen,
        size: node.size,
        mtime_ns: node.mtime_ns,
        uid: node.uid,
        gid: node.gid,
        mode: node.mode,
        actor: payload
            .get("actor")
            .and_then(|a| serde_json::from_value(a.clone()).ok()),
    };
    let freed = match node.parent_lid {
        Some(p) => core
            .replica
            .lock()
            .await
            .name_being_freed(lid, p, &node.name)?,
        None => false,
    };
    match core.control.create(&op).await {
        Ok(created) => {
            let mut rep = core.replica.lock().await;
            rep.assign_file_id(lid, created.file_id, created.entity_version)?;
            rep.outbox_set_state(
                op_id,
                "acked",
                Some(&serde_json::json!({"file_id": created.file_id})),
            )?;
            if node.kind == Kind::Dir {
                rep.set_local_dirty(lid, false)?;
            }
            info!(lid, file_id = created.file_id, "a create was sent again");
            Ok(Resent::Done)
        }
        Err(e) if e.unavailable => Ok(Resent::Wait),
        Err(_) if gone => {
            info!(lid, "a create for something already gone is dropped");
            core.replica.lock().await.abandon_create(op_id)?;
            Ok(Resent::Done)
        }
        // Only a name another node's unacknowledged rename or unlink frees
        // (M7 re-review R1: not any op in flight, which may be its own
        // dependents).
        Err(e) if freed => {
            debug!(lid, err = %e, "a create refused while names are being freed: tried again later");
            Ok(Resent::Defer)
        }
        Err(e) => {
            warn!(lid, err = %e, "the service refused a create sent again");
            core.replica.lock().await.ns_refused(
                op_id,
                lid,
                e.code.as_deref().unwrap_or("rejected"),
            )?;
            Ok(Resent::Done)
        }
    }
}

/// Reconcile dirty directories, sweep, and send, until shutdown.
pub async fn ns_loop<C: ControlPlane, B: BlobIo>(core: Arc<Core<C, B>>) {
    let cfg = core.cfg.namespace.clone();
    let quiet = Duration::from_millis(cfg.quiet_ms.max(50));
    let mut stop = core.shutdown.subscribe();
    let mut sweep: Option<i64> = cfg.sweep_at_start.then_some(0);
    let mut next_sweep = tokio::time::Instant::now()
        + if cfg.sweep_s == 0 {
            Duration::from_secs(u64::MAX / 4)
        } else {
            Duration::from_secs(cfg.sweep_s)
        };
    let mut send_retry = tokio::time::interval(Duration::from_secs(10));
    loop {
        tokio::select! {
            _ = core.ns_wake.notified() => {}
            _ = send_retry.tick() => {}
            _ = stop.changed() => return,
        }
        // Gather a burst of events into one pass.
        tokio::select! {
            _ = tokio::time::sleep(quiet) => {}
            _ = stop.changed() => return,
        }
        if core.ns_sweep.swap(false, Ordering::SeqCst) && sweep.is_none() {
            warn!("namespace events were lost: reconciling every directory");
            sweep = Some(0);
        }
        if tokio::time::Instant::now() >= next_sweep {
            next_sweep = tokio::time::Instant::now() + Duration::from_secs(cfg.sweep_s.max(60));
            if sweep.is_none() {
                sweep = Some(0);
            }
        }
        // Who made the changes: hints still being resolved go first (at
        // most HINT_WAIT; after it, what has no hint goes `unknown`).
        let t0 = tokio::time::Instant::now();
        while core.ns_hints_busy.load(Ordering::SeqCst) && t0.elapsed() < HINT_WAIT {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        let dirty: Vec<i64> = std::mem::take(&mut *core.ns_dirty.lock().unwrap())
            .into_iter()
            .collect();
        for lid in dirty {
            if let Err(e) = reconcile_dir(&core, lid).await {
                warn!(lid, err = %e, "reconciling a directory failed: next pass");
            }
        }
        if let Some(after) = sweep {
            let step = core
                .replica
                .lock()
                .await
                .dirs_on_disk_after(after, SWEEP_STEP)
                .unwrap_or_default();
            sweep = step.last().copied();
            for lid in step {
                if let Err(e) = reconcile_dir(&core, lid).await {
                    debug!(lid, err = %e, "sweep: a directory failed");
                }
            }
            if sweep.is_some() {
                core.ns_wake.notify_one(); // the next step soon
            } else {
                debug!("namespace sweep done");
            }
        }
        if let Err(e) = send_pending(&core).await {
            warn!(err = %e, "sending namespace ops failed: retried");
        }
    }
}

/// Namespace events -> dirty directories.
pub async fn watch_loop<C: ControlPlane, B: BlobIo>(core: Arc<Core<C, B>>) {
    use omnifile_detect::ns::NsEvent;
    let (tx, mut rx) = tokio::sync::mpsc::channel(8192);
    let root = core.layout.data_root.clone();
    let watcher = tokio::spawn(async move {
        if let Err(e) = omnifile_detect::ns::watch(&root, tx).await {
            warn!(err = %e, "namespace watcher stopped: only sweeps find users' renames and deletes");
        }
    });
    let mount = match sys::open(
        &core.layout.data_root,
        libc::O_RDONLY | libc::O_DIRECTORY,
        0,
    ) {
        Ok(m) => m,
        Err(e) => {
            warn!(err = %e, "namespace watcher can't open the data root");
            return;
        }
    };
    let me = std::process::id() as i32;
    let mut stop = core.shutdown.subscribe();
    let ganesha_exe =
        (!core.cfg.fsal.required_exe.is_empty()).then(|| core.cfg.fsal.required_exe.clone());
    loop {
        let first = tokio::select! {
            ev = rx.recv() => ev,
            _ = stop.changed() => break,
        };
        let Some(first) = first else { break };
        // Everything queued so far, each directory once: a burst in one
        // folder is one lookup, not one per event.
        let mut dirs: HashMap<(i32, Vec<u8>), bool> = HashMap::new();
        let mut entries: Vec<omnifile_detect::ns::EntryChanged> = Vec::new();
        let mut overflow = false;
        let mut next = Some(first);
        while let Some(ev) = next.take() {
            match ev {
                NsEvent::Overflow => overflow = true,
                NsEvent::Dir(d) if d.pid != me => {
                    // (Not the daemon's own change: the replica already agrees.)
                    *dirs.entry((d.handle_type, d.handle)).or_default() |= d.on_dir_itself;
                }
                NsEvent::Dir(_) => {}
                // Who made it: never the daemon itself.
                NsEvent::Entry(e) if e.pid != me && entries.len() < 16_384 => entries.push(e),
                NsEvent::Entry(_) => {}
            }
            if dirs.len() < 4096 {
                next = rx.try_recv().ok();
            }
        }
        // Hints before the directories are marked, so the pass they start
        // finds who made the changes.
        if !entries.is_empty() {
            // A pass that another event started waits for these (an smbd
            // session can take a moment to resolve), so it doesn't send a
            // change it can see on disk before its hint is in.
            core.ns_hints_busy.store(true, Ordering::SeqCst);
            note_entry_hints(&core, entries, ganesha_exe.clone()).await;
            core.ns_hints_busy.store(false, Ordering::SeqCst);
        }
        if overflow {
            core.ns_sweep.store(true, Ordering::SeqCst);
            core.ns_wake.notify_one();
        }
        for ((ty, handle), on_dir_itself) in dirs {
            let key =
                open_handle(mount.as_fd(), ty, &handle).and_then(|fd| sys::inode_key(fd.as_fd()));
            let key = match key {
                Ok(k) => k,
                Err(e) => {
                    debug!(err = %e, "namespace event: directory not found by handle");
                    continue;
                }
            };
            let node = core
                .replica
                .lock()
                .await
                .node_by_ino(key.ino, key.ino_gen)
                .ok()
                .flatten();
            debug!(
                ino = key.ino,
                found = node.is_some(),
                "namespace event: directory"
            );
            if let Some(n) = node {
                core.ns_mark(n.lid);
                if on_dir_itself {
                    // Its own owner or mode changed: its parent holds that.
                    if let Some(p) = n.fs_parent_lid {
                        core.ns_mark(p);
                    }
                }
            }
        }
    }
    watcher.abort();
}

/// Who made the watcher's changes (SMB sessions and local processes), as
/// hints. Ganesha's are skipped: its callers come from `NS_HINT`, and it is
/// known by the FSAL peer's pid or its binary, never by its name.
async fn note_entry_hints<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    entries: Vec<omnifile_detect::ns::EntryChanged>,
    ganesha_exe: Option<String>,
) {
    use omnifile_detect::ns::EntryOp;
    let pids: HashSet<i32> = entries.iter().map(|e| e.pid).collect();
    let fsal_pids: HashSet<i32> = core.fsal_pids.lock().unwrap().keys().copied().collect();
    let resolver = core.identity.get().map(|i| i.resolver.clone());
    let who: HashMap<i32, Option<Actor>> = tokio::task::spawn_blocking(move || {
        pids.into_iter()
            .map(|pid| {
                let a = pid_actor(pid, ganesha_exe.as_deref(), &fsal_pids, resolver.as_deref());
                (pid, a)
            })
            .collect()
    })
    .await
    .unwrap_or_default();
    let mut hints = core.ns_hints.lock().unwrap();
    for e in entries {
        let Some(Some(a)) = who.get(&e.pid) else {
            continue; // Ganesha's
        };
        let op = match e.op {
            EntryOp::Create if e.is_dir => HintOp::Create,
            EntryOp::Delete => HintOp::Delete,
            EntryOp::MovedTo => HintOp::Rename,
            EntryOp::Attrib => HintOp::Attrib,
            _ => continue,
        };
        let dir = e
            .dir
            .as_ref()
            .and_then(|(t, h)| actor::key_of_handle(*t, h));
        let child = e
            .child
            .as_ref()
            .and_then(|(t, h)| actor::key_of_handle(*t, h));
        let name = std::str::from_utf8(&e.name)
            .ok()
            .filter(|n| !n.is_empty() && *n != ".");
        let at = dir.zip(name);
        debug!(pid = e.pid, op = ?e.op, is_dir = e.is_dir, ?dir, ?child, name = ?name,
              via = ?a.via, "namespace hint");
        hints.note(
            op,
            child,
            at,
            (op == HintOp::Rename).then_some(name).flatten(),
            a.clone(),
        );
    }
}

/// Who a process is, for a namespace change it made: `None` for Ganesha
/// (its NFS callers come from `NS_HINT`); an SMB session's user or a local
/// process's uid; `unknown` when it can't be told (it exited, an anonymous
/// session). Blocking (`/proc`, winbind, `smbstatus`).
fn pid_actor(
    pid: i32,
    ganesha_exe: Option<&str>,
    fsal_pids: &HashSet<i32>,
    resolver: Option<&dyn omnifile_identity::IdentityResolver>,
) -> Option<Actor> {
    let exe = std::fs::read_link(format!("/proc/{pid}/exe")).ok();
    let is_ganesha = match (ganesha_exe, &exe) {
        // The configured binary, checked now (a reused pid is not Ganesha).
        (Some(want), Some(have)) if Path::new(want).is_absolute() => have == Path::new(want),
        (Some(_), _) => false,
        (None, _) => fsal_pids.contains(&pid),
    };
    if is_ganesha {
        return None;
    }
    if let Some(r) = resolver {
        let id = r.resolve_smb(&omnifile_identity::SmbRequest { pid, pidfd: None });
        let a = actor::resolved_actor(&id);
        debug!(pid, via = ?a.via, uid = ?a.uid, method = ?id.method, err = ?id.error,
               "namespace change: who made it");
        return Some(a);
    }
    // No AD identity configured: a local process's uid, from the kernel.
    let comm = std::fs::read_to_string(format!("/proc/{pid}/comm")).unwrap_or_default();
    if comm.trim().starts_with("smbd") {
        return Some(Actor::unknown());
    }
    let status = std::fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
    let uid = status
        .lines()
        .find_map(|l| l.strip_prefix("Uid:"))
        .and_then(|u| u.split_whitespace().nth(3)) // the fsuid
        .and_then(|u| u.parse::<u32>().ok());
    Some(match uid {
        Some(uid) => Actor {
            via: ActorVia::Local,
            uid: Some(uid),
            sid: None,
            sec: None,
            proof: Some(omnifile_control::types::Proof::Proven),
            client_addr: None,
        },
        None => Actor::unknown(),
    })
}

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// An NFS caller's namespace change (`NS_HINT`, `fsal-socket.md` §5.4):
    /// who made it, for the op the next pass queues. RENAME names its old
    /// directory, so it is found by the object and the new name.
    pub fn ns_hint_nfs(&self, h: &omnifile_fsal_proto::wire::NsHint) {
        let op = match h.op {
            2 | 8 => HintOp::Delete,
            3 => HintOp::Rename,
            7 => HintOp::Create,
            4 if h.what & 2 != 0 => HintOp::Attrib,
            _ => return,
        };
        let who = actor::nfs_actor(&h.identity);
        let target = (h.target.ino != 0).then_some((h.target.ino, h.target.ino_gen));
        let name = std::str::from_utf8(&h.name).ok().filter(|n| !n.is_empty());
        let new_name = std::str::from_utf8(&h.new_name)
            .ok()
            .filter(|n| !n.is_empty());
        let parent = (h.parent.ino != 0).then_some((h.parent.ino, h.parent.ino_gen));
        let (at, new_name) = if op == HintOp::Rename {
            (None, new_name)
        } else {
            (parent.zip(name), None)
        };
        self.ns_hints
            .lock()
            .unwrap()
            .note(op, target, at, new_name, who);
    }
}

/// What splitting a second name came to.
#[derive(Debug, PartialEq, Eq)]
enum Splitted {
    /// The name now has its own inode: committed as a separate file.
    Done { path: PathBuf, ino: u64 },
    /// A second name for a placeholder whose bytes are not all local, which
    /// can't be copied without a recall: the name was removed (hard links are
    /// refused, decision 42), the catalogued name and the bytes untouched.
    Removed,
    /// The name no longer holds that inode, or has one name only now.
    Stale,
}

/// Give each second name its own copy (decision 42), commit it, audit it.
async fn split_links<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    dir_lid: i64,
    dir_rel: &Path,
    splits: &[Split],
) {
    let mut rescan = false;
    for sp in splits {
        let data_root = core.layout.data_root.clone();
        let (rel, name, key) = (dir_rel.to_path_buf(), sp.name.clone(), sp.key);
        let catalog = sp.catalog_rel.clone();
        let r = tokio::task::spawn_blocking(move || {
            split_link(&data_root, &rel, &name, key, catalog.as_deref())
        })
        .await;
        let path = core.layout.path(&dir_rel.join(&sp.name));
        match r {
            Ok(Ok(Splitted::Done { path, ino })) => {
                info!(path = %path.display(), file_id = ?sp.file_id,
                      "hard link split: the name now has its own copy (decision 42)");
                core.ns_link_waits.lock().unwrap().remove(&sp.key);
                core.audit_ns(AUDIT_LINK_SPLIT, "info", sp.file_id, &path);
                if let Some(tx) = core.commit_tx.get() {
                    let _ = tx.try_send(crate::ports::CloseWrite::new(path, ino));
                }
            }
            Ok(Ok(Splitted::Removed)) => {
                warn!(path = %path.display(), file_id = ?sp.file_id,
                      "a hard link to a placeholder whose bytes are not all local: the link was removed (decision 42)");
                core.audit_ns(AUDIT_LINK_REMOVED, "deny", sp.file_id, &path);
            }
            // The name changed under us: never act on it; look again.
            Ok(Ok(Splitted::Stale)) => rescan = true,
            Ok(Err(e)) => {
                warn!(path = %path.display(), err = %e, "splitting a hard link failed: next pass")
            }
            Err(e) => warn!(err = %e, "splitting a hard link failed: next pass"),
        }
    }
    if rescan {
        let core = core.clone();
        tokio::spawn(async move {
            tokio::time::sleep(FRESH_RETRY).await;
            core.ns_mark(dir_lid);
        });
    }
}

/// Whether the bytes of the file behind `fd` are all local (no marker, or a
/// fully resident one).
fn bytes_local(fd: BorrowedFd<'_>) -> Option<bool> {
    match omnifile_marker::xattr::read(fd) {
        Ok(omnifile_marker::xattr::ReadOutcome::Unmanaged) => Some(true),
        Ok(omnifile_marker::xattr::ReadOutcome::Marker(m)) => Some(m.is_fully_resident()),
        _ => None,
    }
}

/// Reflink `src` into `dst` (`FICLONE`), else copy the bytes.
fn clone_or_copy(src: &std::fs::File, dst: &std::fs::File) -> io::Result<()> {
    const FICLONE: libc::c_ulong = 0x4004_9409;
    // SAFETY: both are valid fds; FICLONE takes the source fd as its argument.
    if unsafe { libc::ioctl(dst.as_raw_fd(), FICLONE as _, src.as_raw_fd()) } == 0 {
        return Ok(());
    }
    use std::io::Seek;
    let mut r = src.try_clone()?;
    let mut w = dst.try_clone()?;
    r.seek(io::SeekFrom::Start(0))?;
    w.seek(io::SeekFrom::Start(0))?;
    io::copy(&mut r, &mut w)?;
    Ok(())
}

/// Copy one xattr from `src` to `dst`, if it is set.
fn copy_xattr(src: BorrowedFd<'_>, dst: BorrowedFd<'_>, name: &str) -> io::Result<()> {
    let c = cname(name)?;
    let mut buf = vec![0u8; 65536];
    // SAFETY: buf is valid for its length; c is NUL-terminated.
    let n = unsafe {
        libc::fgetxattr(
            src.as_raw_fd(),
            c.as_ptr(),
            buf.as_mut_ptr().cast(),
            buf.len(),
        )
    };
    if n < 0 {
        let e = io::Error::last_os_error();
        return match e.raw_os_error() {
            Some(libc::ENODATA | libc::EOPNOTSUPP) => Ok(()),
            _ => Err(e),
        };
    }
    // SAFETY: buf holds n bytes; c is NUL-terminated.
    let r = unsafe {
        libc::fsetxattr(
            dst.as_raw_fd(),
            c.as_ptr(),
            buf.as_ptr().cast(),
            n as usize,
            0,
        )
    };
    if r < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// Remove the second name `name` of the inode `key`: moved aside to a
/// temporary name first, checked by handle to be that inode there, then
/// unlinked. Anything else found there (a user replaced the name meanwhile)
/// is put back untouched, and the directory looked at again.
fn remove_alias(dir: BorrowedFd<'_>, name: &str, key: Key) -> io::Result<Splitted> {
    let tmp = format!(".omnifile-unlink-{}-{}", std::process::id(), key.0);
    rename_noreplace(dir, name, &tmp)?;
    let still = handle_at(dir, &tmp).is_ok_and(|h| h == key)
        && fstatat(dir, &tmp).is_ok_and(|st| st.st_nlink >= 2);
    if !still {
        if let Err(e) = rename_noreplace(dir, &tmp, name) {
            warn!(name, tmp = %tmp, err = %e, "a name replaced while its hard link was removed could not be put back");
        }
        return Ok(Splitted::Stale);
    }
    sys::unlinkat(dir, std::ffi::OsStr::new(&tmp))?;
    Ok(Splitted::Removed)
}

fn rename_noreplace(dir: BorrowedFd<'_>, a: &str, b: &str) -> io::Result<()> {
    let (ca, cb) = (cname(a)?, cname(b)?);
    // SAFETY: both names are NUL-terminated; dir is a valid fd.
    let r = unsafe {
        libc::syscall(
            libc::SYS_renameat2,
            dir.as_raw_fd(),
            ca.as_ptr(),
            dir.as_raw_fd(),
            cb.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    };
    if r < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn rename_exchange(dir: BorrowedFd<'_>, a: &str, b: &str) -> io::Result<()> {
    let (ca, cb) = (cname(a)?, cname(b)?);
    // SAFETY: both names are NUL-terminated; dir is a valid fd.
    let r = unsafe {
        libc::syscall(
            libc::SYS_renameat2,
            dir.as_raw_fd(),
            ca.as_ptr(),
            dir.as_raw_fd(),
            cb.as_ptr(),
            libc::RENAME_EXCHANGE,
        )
    };
    if r < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// Give the second name `name` (of the inode `key`) in `dir_rel` its own
/// inode: a reflink copy with the same owner, mode, ACL and times, swapped
/// in atomically (`RENAME_EXCHANGE`). A name a user replaced meanwhile is
/// swapped back, untouched. Ported from line B (81fa187).
fn split_link(
    data_root: &Path,
    dir_rel: &Path,
    name: &str,
    key: Key,
    catalog_rel: Option<&Path>,
) -> io::Result<Splitted> {
    // Never the catalogued name: only the second one is split or removed.
    if catalog_rel.is_some_and(|c| c == dir_rel.join(name)) {
        return Ok(Splitted::Stale);
    }
    let dfd = sys::open_dir_beneath(data_root, dir_rel)?;
    let f = match sys::openat_nofollow(dfd.as_fd(), std::ffi::OsStr::new(name), libc::O_RDONLY, 0) {
        Ok(f) => std::fs::File::from(f),
        Err(e) if matches!(e.raw_os_error(), Some(libc::ENOENT | libc::ELOOP)) => {
            return Ok(Splitted::Stale);
        }
        Err(e) => return Err(e),
    };
    let st = sys::fstat(f.as_fd())?;
    let k = sys::inode_key(f.as_fd())?;
    if st.st_mode & libc::S_IFMT != libc::S_IFREG || (k.ino, k.ino_gen) != key || st.st_nlink < 2 {
        return Ok(Splitted::Stale);
    }
    if bytes_local(f.as_fd()) != Some(true) {
        // Its bytes can't be copied without a recall, and a second name for
        // the managed inode can't stay (decision 42): remove this name. The
        // catalogued name still holds the inode (checked by the caller, and
        // the link count is 2 or more), so no bytes go.
        return remove_alias(dfd.as_fd(), name, key);
    }
    let tmp = sys::tmpfile_in(dfd.as_fd(), 0o600)?;
    clone_or_copy(&f, &tmp)?;
    // SAFETY: plain syscalls on a valid fd.
    unsafe {
        libc::fchown(tmp.as_raw_fd(), st.st_uid, st.st_gid);
        libc::fchmod(tmp.as_raw_fd(), st.st_mode & 0o7777);
    }
    copy_xattr(f.as_fd(), tmp.as_fd(), "system.posix_acl_access")?;
    let times = [
        libc::timespec {
            tv_sec: st.st_atime,
            tv_nsec: st.st_atime_nsec,
        },
        libc::timespec {
            tv_sec: st.st_mtime,
            tv_nsec: st.st_mtime_nsec,
        },
    ];
    // SAFETY: times is a valid [timespec; 2].
    unsafe { libc::futimens(tmp.as_raw_fd(), times.as_ptr()) };
    let tmp_name = format!(".omnifile-split-{}-{}", std::process::id(), key.0);
    let proc = cname(&format!("/proc/self/fd/{}", tmp.as_raw_fd()))?;
    let tn = cname(&tmp_name)?;
    // SAFETY: both names are NUL-terminated; dfd is valid.
    if unsafe {
        libc::linkat(
            libc::AT_FDCWD,
            proc.as_ptr(),
            dfd.as_raw_fd(),
            tn.as_ptr(),
            libc::AT_SYMLINK_FOLLOW,
        )
    } < 0
    {
        return Err(io::Error::last_os_error());
    }
    rename_exchange(dfd.as_fd(), &tmp_name, name)?;
    // The name swapped out must be the link: else swap back, untouched.
    if !handle_at(dfd.as_fd(), &tmp_name).is_ok_and(|h| h == key) {
        let _ = rename_exchange(dfd.as_fd(), &tmp_name, name);
        let _ = sys::unlinkat(dfd.as_fd(), std::ffi::OsStr::new(&tmp_name));
        return Ok(Splitted::Stale);
    }
    sys::unlinkat(dfd.as_fd(), std::ffi::OsStr::new(&tmp_name))?;
    let ino = sys::fstat(tmp.as_fd()).map(|s| s.st_ino).unwrap_or(0);
    Ok(Splitted::Done {
        path: data_root.join(dir_rel).join(name),
        ino,
    })
}

fn open_handle(mount: BorrowedFd<'_>, ty: i32, bytes: &[u8]) -> io::Result<OwnedFd> {
    if bytes.len() > 128 {
        return Err(io::Error::from(io::ErrorKind::InvalidInput));
    }
    #[repr(C)]
    struct Big {
        bytes: libc::c_uint,
        ty: libc::c_int,
        f: [u8; 128],
    }
    let mut h = Big {
        bytes: bytes.len() as libc::c_uint,
        ty,
        f: [0; 128],
    };
    h.f[..bytes.len()].copy_from_slice(bytes);
    // SAFETY: h is a valid struct file_handle with `bytes` bytes after it.
    let fd = unsafe {
        libc::open_by_handle_at(
            mount.as_raw_fd(),
            (&mut h as *mut Big).cast(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: fd is new and owned here.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}
