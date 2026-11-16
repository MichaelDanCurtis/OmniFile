//! Rewind trees: read-only point-in-time views of a share (decision 30;
//! `docs/design/fsal-namespace.md` §7).
//!
//! - **Where:** `.omni/snap/<share>/<label>/<share-relative path>`, outside
//!   the shares, so crawlers and the upload detector never walk history. The
//!   FSAL presents `<share>` as `.snapshots`; Samba's `vfs_shadow_copy2`
//!   reads the same area.
//! - **Labels** name one instant each, `YYYY-MM-DDTHHMMZ` (UTC). The set
//!   comes from the share's `rewind` settings ([`labels`]): daily at a time
//!   of day for `depth_days`, plus hourly for the last `hourly_for_hours`.
//!   [`ensure_labels`] creates the missing ones (unpopulated) and removes the
//!   expired ones.
//! - **Lazy:** a directory is filled on first access ([`populate`], for the
//!   FSAL's `POPULATE_DIR`): its entries as of the label come from the
//!   service (`GET …/asof`), files become `READONLY` placeholders of the
//!   content version valid then (reading one recalls that version), and
//!   subdirectories are created `DIR_UNPOPULATED`. The directory's marker
//!   loses `DIR_UNPOPULATED` last, so a half-filled directory is never shown
//!   as complete.
//! - **Permissions err towards fewer rights:** a file gets its ACL as of the
//!   label; a directory gets its as-of ACL only when the live folder still
//!   has the same one, else owner-only (POSIX ACLs can't express the exact
//!   intersection). A label directory gets the live share root's ACL. Every
//!   write bit is cleared.

use std::collections::BTreeSet;
use std::io;
use std::os::fd::AsFd;
use std::path::{Component, Path, PathBuf};

use omnifile_marker::xattr::{self, ReadOutcome};
use omnifile_marker::{Flags, Marker};
use omnifile_replica::{Kind, Replica};
use omnifile_stub::placeholder::{
    Created, NewPerms, PlaceholderSpec, create_dir_with_perms, create_placeholder_with_perms,
    create_symlink, reset_dir_perms,
};
use omnifile_stub::{Layout, MarkerSeq, sys};
use serde_json::Value;
use tracing::{info, warn};

use crate::acl::CreatePerms;
use crate::ports::{ControlPlane, CtlError};

/// The rewind area, relative to the data root.
pub const SNAP: &str = ".omni/snap";
/// Entries asked for per `asof` page.
const PAGE: u32 = 1000;
/// Write bits: rewind trees are read-only.
const NO_WRITE: u32 = !0o222;

#[derive(Debug, thiserror::Error)]
pub enum RewindError {
    #[error("not a rewind directory: {0}")]
    NotRewind(String),
    #[error("service: {0}")]
    Control(String),
    #[error("replica: {0}")]
    Replica(String),
    #[error("io: {0}")]
    Io(#[from] io::Error),
}

impl From<CtlError> for RewindError {
    fn from(e: CtlError) -> Self {
        RewindError::Control(e.message)
    }
}

/// When labels are taken, from the share's `rewind` (`ShareRow`).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct LabelPolicy {
    pub depth_days: u32,
    /// Minutes after midnight UTC of the daily label; `None`: no daily labels.
    pub daily_at_min: Option<u32>,
    pub hourly_for_hours: u32,
}

impl LabelPolicy {
    /// From the share's raw `rewind` object; nothing when depth is 0.
    pub fn from_share(rewind: &Value) -> LabelPolicy {
        let depth = rewind["depth_days"].as_u64().unwrap_or(0).min(36_500) as u32;
        let labels = &rewind["labels"];
        let daily = labels["daily_at_utc"].as_str().and_then(parse_hhmm).or(
            if depth > 0 && labels.get("daily_at_utc").is_none() {
                Some(0)
            } else {
                None
            },
        );
        LabelPolicy {
            depth_days: depth,
            daily_at_min: daily,
            hourly_for_hours: labels["hourly_for_hours"]
                .as_u64()
                .unwrap_or(0)
                .min(24 * 31) as u32,
        }
    }
}

fn parse_hhmm(s: &str) -> Option<u32> {
    let (h, m) = s.split_once(':')?;
    let (h, m): (u32, u32) = (h.parse().ok()?, m.parse().ok()?);
    (h < 24 && m < 60).then_some(h * 60 + m)
}

/// The labels a share has at `now` (Unix seconds), within its depth.
pub fn labels(p: &LabelPolicy, now: i64) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    if p.depth_days == 0 {
        return out;
    }
    let oldest = now - i64::from(p.depth_days) * 86_400;
    if let Some(at) = p.daily_at_min {
        let off = i64::from(at) * 60;
        let today = (now - off).div_euclid(86_400);
        for d in 0..=i64::from(p.depth_days) {
            let t = (today - d) * 86_400 + off;
            if t <= now && t >= oldest {
                out.insert(label_of(t));
            }
        }
    }
    let hour = now.div_euclid(3600) * 3600;
    for h in 0..i64::from(p.hourly_for_hours) {
        let t = hour - h * 3600;
        if t >= oldest {
            out.insert(label_of(t));
        }
    }
    out
}

/// `YYYY-MM-DDTHHMMZ` for Unix seconds.
pub fn label_of(t: i64) -> String {
    let days = t.div_euclid(86_400);
    let secs = t.rem_euclid(86_400);
    let (y, m, d) = civil_from_days(days);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}{:02}Z",
        secs / 3600,
        (secs % 3600) / 60
    )
}

/// The RFC 3339 instant a label names, if it is one.
pub fn label_time(label: &str) -> Option<String> {
    let b = label.as_bytes();
    let digits = |r: std::ops::Range<usize>| b[r].iter().all(u8::is_ascii_digit);
    if b.len() != 16
        || b[4] != b'-'
        || b[7] != b'-'
        || b[10] != b'T'
        || b[15] != b'Z'
        || !digits(0..4)
        || !digits(5..7)
        || !digits(8..10)
        || !digits(11..15)
    {
        return None;
    }
    let (h, mi): (u32, u32) = (label[11..13].parse().ok()?, label[13..15].parse().ok()?);
    (h < 24 && mi < 60).then(|| format!("{}:{}:00Z", &label[..13], &label[13..15]))
}

/// Howard Hinnant's `civil_from_days`: days since 1970-01-01 to (y, m, d).
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let y = yoe + era * 400 + i64::from(m <= 2);
    (y, m, d)
}

/// A single path component that is safe to create (a catalog name).
fn safe_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 255
        && name != "."
        && name != ".."
        && !name.contains('/')
        && !name.contains('\0')
}

/// A rewind entry's mode: read-only, and never set-id (an ACL'd entry
/// keeps the mode's set-id bits, `set_new_attrs_fd`).
fn history_mode(mode: u32) -> u32 {
    mode & NO_WRITE & 0o1777
}

/// An entry this edge can't make (its other fields): skipped, with a
/// warning, rather than failing its whole directory.
fn unmakeable(e: &omnifile_replica::FileRow) -> Option<&'static str> {
    match e.kind {
        Kind::Symlink if e.symlink_target.as_deref().is_none_or(str::is_empty) => {
            Some("a symlink with no target")
        }
        Kind::File if e.size > i64::MAX as u64 => Some("a size past what the filesystem holds"),
        _ => None,
    }
}

/// `(share, label, rest)` of a path below [`SNAP`].
fn split(rel: &Path) -> Option<(String, String, PathBuf)> {
    let rest = rel.strip_prefix(SNAP).ok()?;
    let mut parts = rest.components();
    let share = match parts.next()? {
        Component::Normal(s) => s.to_str()?.to_owned(),
        _ => return None,
    };
    let label = match parts.next()? {
        Component::Normal(s) => s.to_str()?.to_owned(),
        _ => return None,
    };
    let tail: PathBuf = parts.collect();
    if tail
        .components()
        .any(|c| !matches!(c, Component::Normal(_)))
    {
        return None;
    }
    Some((share, label, tail))
}

fn dir_marker(file_id: u64, share_id: u64, flags: Flags, seq: &MarkerSeq) -> Marker {
    let mut m = Marker::resident(file_id, 0, 0, share_id);
    m.flags = flags;
    m.marker_seq = seq.next();
    m
}

fn read_marker(fd: std::os::fd::BorrowedFd<'_>) -> io::Result<Option<Marker>> {
    Ok(match xattr::read(fd)? {
        ReadOutcome::Marker(m) => Some(m),
        _ => None,
    })
}

fn write_marker(fd: std::os::fd::BorrowedFd<'_>, m: &Marker, replace: bool) -> io::Result<()> {
    let r = if replace {
        xattr::write_replace(fd, m)
    } else {
        xattr::write(fd, m)
    };
    r.map_err(|e| match e {
        xattr::Error::Io(e) => e,
        xattr::Error::Marker(e) => io::Error::new(io::ErrorKind::InvalidData, e.to_string()),
    })
}

/// Permissions for an entry: its as-of ACL for files; for directories, the
/// as-of ACL only while the live folder has the same one.
fn perms_for(
    rep: &Replica,
    perms: &CreatePerms,
    acl_id: Option<u64>,
    live_acl: Option<Option<u64>>,
    is_dir: bool,
) -> NewPerms {
    if is_dir && live_acl != Some(acl_id) {
        return NewPerms::OwnerOnly;
    }
    match acl_id {
        Some(id) => perms.for_acl(rep, id, false),
        None => NewPerms::OwnerOnly,
    }
}

/// Puts the smb-guard's open-permission mark on a directory the daemon has
/// just made unfilled (`DIR_UNPOPULATED`), so smbd's open of it is held
/// while it fills and it is never listed empty over SMB.
pub type WatchDir = std::sync::Arc<dyn Fn(std::os::fd::BorrowedFd<'_>) + Send + Sync>;

/// No smb-guard (tests, or a daemon without one).
pub fn no_watch() -> WatchDir {
    std::sync::Arc::new(|_| {})
}

/// Create a label directory (or its share directory) if missing.
#[allow(clippy::too_many_arguments)]
fn make_dir(
    layout: &Layout,
    rel: &Path,
    file_id: u64,
    share_id: u64,
    flags: Flags,
    perms: &NewPerms,
    seq: &MarkerSeq,
    watch: &WatchDir,
) -> io::Result<bool> {
    let created = create_dir_with_perms(layout, rel, file_id, 0, 0, 0o555, 0, perms)?;
    let new = matches!(created, Created::Created(_));
    if new {
        let fd = sys::open_dir_beneath(&layout.data_root, rel)?;
        write_marker(
            fd.as_fd(),
            &dir_marker(file_id, share_id, flags, seq),
            false,
        )?;
        if flags.contains(Flags::DIR_UNPOPULATED) {
            watch(fd.as_fd());
        }
    }
    Ok(new)
}

/// Mark every unfilled rewind directory (at startup: a new smb-guard group
/// has no marks). Filled directories are walked; unfilled ones are empty.
/// Returns how many were marked.
pub fn watch_unfilled(layout: &Layout, watch: &WatchDir) -> io::Result<usize> {
    let mut n = 0;
    let mut todo = vec![PathBuf::from(SNAP)];
    while let Some(rel) = todo.pop() {
        let fd = match sys::open_dir_beneath(&layout.data_root, &rel) {
            Ok(fd) => fd,
            Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
            Err(e) => return Err(e),
        };
        if read_marker(fd.as_fd())?.is_some_and(|m| m.flags.contains(Flags::DIR_UNPOPULATED)) {
            watch(fd.as_fd());
            n += 1;
            continue;
        }
        for e in std::fs::read_dir(layout.path(&rel))?.flatten() {
            if e.file_type().is_ok_and(|t| t.is_dir()) {
                todo.push(rel.join(e.file_name()));
            }
        }
    }
    Ok(n)
}

/// One share's labels, read from the replica.
pub struct SharePlan {
    pub share_id: u64,
    pub name: String,
    pub root_dir_id: u64,
    pub want: BTreeSet<String>,
    /// The live share root's ACL.
    pub perms: NewPerms,
}

/// The labels every share should have at `now`, read under the replica
/// lock (no filesystem work).
pub fn plan_labels(
    rep: &Replica,
    perms: &CreatePerms,
    now: i64,
) -> Result<Vec<SharePlan>, RewindError> {
    let shares = rep
        .shares()
        .map_err(|e| RewindError::Replica(e.to_string()))?;
    let mut out = Vec::new();
    for s in shares {
        let (Some(root), true) = (s.root_dir_id, safe_name(&s.name)) else {
            continue;
        };
        let body: Option<String> = rep
            .conn()
            .query_row(
                "SELECT body FROM share WHERE share_id = ?1",
                [s.share_id as i64],
                |r| r.get(0),
            )
            .ok();
        let raw: Value = body
            .and_then(|b| serde_json::from_str(&b).ok())
            .unwrap_or_default();
        let live = rep
            .node_by_file_id(root)
            .map_err(|e| RewindError::Replica(e.to_string()))?;
        let Some(live) = live.filter(|n| !n.deleted) else {
            continue;
        };
        out.push(SharePlan {
            share_id: s.share_id,
            name: s.name,
            root_dir_id: root,
            want: labels(&LabelPolicy::from_share(&raw["rewind"]), now),
            perms: match live.acl_id {
                Some(id) => perms.for_acl(rep, id, false),
                None => NewPerms::OwnerOnly,
            },
        });
    }
    Ok(out)
}

/// Create the missing label directories and remove the expired ones
/// (without the replica lock). `reperm`: an ACL changed, so the share and
/// label directories that exist get the live share root's ACL again.
/// Returns `(created, removed)`.
pub fn apply_labels(
    layout: &Layout,
    seq: &MarkerSeq,
    plans: &[SharePlan],
    reperm: bool,
    watch: &WatchDir,
) -> Result<(usize, usize), RewindError> {
    let (mut created, mut removed) = (0, 0);
    let snap = PathBuf::from(SNAP);
    if !plans.is_empty() {
        std::fs::create_dir_all(layout.path(&snap))?;
        std::fs::set_permissions(
            layout.path(&snap),
            std::os::unix::fs::PermissionsExt::from_mode(0o711),
        )?;
    }
    for p in plans {
        let share_rel = snap.join(&p.name);
        if !p.want.is_empty()
            && !make_dir(
                layout,
                &share_rel,
                p.root_dir_id,
                p.share_id,
                Flags::READONLY,
                &p.perms,
                seq,
                watch,
            )?
            && reperm
        {
            reset_dir_perms(layout, &share_rel, 0, 0, 0o555, &p.perms)?;
        }
        for label in &p.want {
            let flags = Flags::READONLY.with(Flags::DIR_UNPOPULATED);
            let rel = share_rel.join(label);
            if make_dir(
                layout,
                &rel,
                p.root_dir_id,
                p.share_id,
                flags,
                &p.perms,
                seq,
                watch,
            )? {
                created += 1;
            } else if reperm {
                reset_dir_perms(layout, &rel, 0, 0, 0o555, &p.perms)?;
            }
        }
        // Expired labels: a rewind tree is a cache of history, never data.
        if let Ok(dir) = std::fs::read_dir(layout.path(&share_rel)) {
            for e in dir.flatten() {
                let name = e.file_name().to_string_lossy().into_owned();
                if label_time(&name).is_some() && !p.want.contains(&name) {
                    std::fs::remove_dir_all(e.path())?;
                    removed += 1;
                }
            }
        }
    }
    // Trees of shares that are gone or renamed: only directories the daemon
    // made (READONLY marker), never anything else under the snap root.
    if let Ok(dir) = std::fs::read_dir(layout.path(&snap)) {
        for e in dir.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            if plans.iter().any(|p| p.name == name) {
                continue;
            }
            let ours = sys::open_dir_beneath(&layout.data_root, &snap.join(&name))
                .ok()
                .and_then(|fd| read_marker(fd.as_fd()).ok().flatten())
                .is_some_and(|m| m.flags.contains(Flags::READONLY));
            if ours {
                std::fs::remove_dir_all(e.path())?;
                removed += 1;
            }
        }
    }
    Ok((created, removed))
}

/// One entry to create, decided under the replica lock.
struct Planned {
    row: omnifile_replica::FileRow,
    perms: NewPerms,
}

/// Fill one rewind directory from the service. Returns the number of
/// entries created; 0 when it was already populated. The replica lock is
/// taken per page to decide permissions, never across filesystem work.
pub async fn populate<C: ControlPlane>(
    layout: &Layout,
    rep: &tokio::sync::Mutex<Replica>,
    perms: &CreatePerms,
    control: &C,
    seq: &MarkerSeq,
    rel: &Path,
    watch: &WatchDir,
) -> Result<usize, RewindError> {
    let (_share, label, _) =
        split(rel).ok_or_else(|| RewindError::NotRewind(rel.display().to_string()))?;
    let t = label_time(&label).ok_or_else(|| RewindError::NotRewind(label.clone()))?;
    let dir = sys::open_dir_beneath(&layout.data_root, rel)?;
    let Some(m) = read_marker(dir.as_fd())? else {
        return Err(RewindError::NotRewind(format!(
            "{} has no marker",
            rel.display()
        )));
    };
    if !m.flags.contains(Flags::READONLY) {
        return Err(RewindError::NotRewind(rel.display().to_string()));
    }
    if !m.flags.contains(Flags::DIR_UNPOPULATED) {
        return Ok(0);
    }
    let mut made = 0;
    let mut after: Option<String> = None;
    loop {
        let page = control.asof(m.file_id, &t, after.as_deref(), PAGE).await?;
        let mut todo = Vec::with_capacity(page.entries.len());
        {
            let rep = rep.lock().await;
            for raw in &page.entries {
                // Pages are in name order: the next starts after this one,
                // even if the edge can't read it.
                if let Some(n) = raw.get("name").and_then(Value::as_str) {
                    after = Some(n.to_owned());
                }
                let e = match omnifile_replica::feed::parse_file_row(raw) {
                    Ok(e) => e,
                    Err(err) => {
                        warn!(%err, "rewind: an entry the edge can't read; skipped");
                        continue;
                    }
                };
                after = Some(e.name.clone());
                if !safe_name(&e.name) {
                    warn!(name = %e.name, "rewind: an unsafe name; skipped");
                    continue;
                }
                if let Some(why) = unmakeable(&e) {
                    warn!(name = %e.name, why, "rewind: an entry the edge can't make; skipped");
                    continue;
                }
                let is_dir = e.kind == Kind::Dir;
                let live = if is_dir {
                    rep.node_by_file_id(e.file_id)
                        .map_err(|x| RewindError::Replica(x.to_string()))?
                        .filter(|n| !n.deleted)
                        .map(|n| n.acl_id)
                } else {
                    None
                };
                let perms = perms_for(&rep, perms, e.acl_id, live, is_dir);
                todo.push(Planned { row: e, perms });
            }
        }
        for Planned { row: e, perms: p } in todo {
            let child = rel.join(&e.name);
            let mode = history_mode(e.mode);
            let created = match e.kind {
                Kind::Dir => {
                    let c = create_dir_with_perms(
                        layout, &child, e.file_id, e.uid, e.gid, mode, e.mtime_ns, &p,
                    )?;
                    if matches!(c, Created::Created(_)) {
                        let fd = sys::open_dir_beneath(&layout.data_root, &child)?;
                        let flags = Flags::READONLY.with(Flags::DIR_UNPOPULATED);
                        let dm = dir_marker(e.file_id, m.share_id, flags, seq);
                        write_marker(fd.as_fd(), &dm, false)?;
                        watch(fd.as_fd());
                    }
                    c
                }
                Kind::Symlink => create_symlink(
                    layout,
                    &child,
                    e.file_id,
                    e.symlink_target.as_deref().unwrap_or(""),
                    e.uid,
                    e.gid,
                    e.mtime_ns,
                )?,
                Kind::File => {
                    let Some(version_id) = e.version_id else {
                        continue; // no bytes then (not uploaded yet)
                    };
                    let archived = e.location.as_ref().is_some_and(|l| l.archived);
                    let flags = Flags::READONLY.with(if archived {
                        Flags::ARCHIVED
                    } else {
                        Flags::ONLINE
                    });
                    let spec = PlaceholderSpec {
                        file_id: e.file_id,
                        version_id,
                        size: e.size,
                        share_id: m.share_id,
                        flags,
                        uid: e.uid,
                        gid: e.gid,
                        mode,
                        mtime_ns: e.mtime_ns,
                    };
                    create_placeholder_with_perms(layout, &child, &spec, seq.next(), &p)?
                }
            };
            match created {
                Created::Created(_) => made += 1,
                Created::Conflict(why) => warn!(%why, "rewind: name taken; skipped"),
                _ => {}
            }
        }
        if !page.more || after.is_none() {
            break;
        }
    }
    // Last: the directory is complete.
    let mut done = m;
    done.flags = m.flags.without(Flags::DIR_UNPOPULATED);
    done.marker_seq = seq.next();
    write_marker(dir.as_fd(), &done, true)?;
    info!(dir = %rel.display(), entries = made, "rewind directory populated");
    Ok(made)
}

/// Empty a populated rewind directory and mark it unpopulated again, so
/// the next access fills it anew. The flag goes on first: from then on
/// readers get `DELAY` (and a `POPULATE_DIR`, which the caller holds off),
/// never a half-emptied listing. Returns false when there was nothing to do.
pub fn evict(layout: &Layout, rel: &Path, seq: &MarkerSeq, watch: &WatchDir) -> io::Result<bool> {
    if split(rel).is_none() {
        return Ok(false);
    }
    let dir = match sys::open_dir_beneath(&layout.data_root, rel) {
        Ok(fd) => fd,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(e) => return Err(e),
    };
    let Some(m) = read_marker(dir.as_fd())? else {
        return Ok(false);
    };
    if !m.flags.contains(Flags::READONLY) || m.flags.contains(Flags::DIR_UNPOPULATED) {
        return Ok(false);
    }
    let mut next = m;
    next.flags = m.flags.with(Flags::DIR_UNPOPULATED);
    next.marker_seq = seq.next();
    write_marker(dir.as_fd(), &next, true)?;
    // Marked already, unless the guard's group is newer than the mark.
    watch(dir.as_fd());
    for e in std::fs::read_dir(layout.path(rel))?.flatten() {
        if e.file_type().is_ok_and(|t| t.is_dir()) {
            std::fs::remove_dir_all(e.path())?;
        } else {
            std::fs::remove_file(e.path())?;
        }
    }
    Ok(true)
}

/// The rewind directory with this inode (the FSAL names objects by inode):
/// a walk of [`SNAP`], directories only, never following symlinks.
pub fn find_dir(layout: &Layout, ino: u64, ino_gen: u32) -> io::Result<Option<PathBuf>> {
    find(layout, ino, ino_gen, false)
}

/// [`find_dir`] for a rewind placeholder (a regular file).
pub fn find_file(layout: &Layout, ino: u64, ino_gen: u32) -> io::Result<Option<PathBuf>> {
    find(layout, ino, ino_gen, true)
}

fn find(layout: &Layout, ino: u64, ino_gen: u32, file: bool) -> io::Result<Option<PathBuf>> {
    let mut todo = vec![PathBuf::from(SNAP)];
    while let Some(rel) = todo.pop() {
        let fd = match sys::open_dir_beneath(&layout.data_root, &rel) {
            Ok(fd) => fd,
            Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
            Err(e) => return Err(e),
        };
        let key = sys::inode_key(fd.as_fd())?;
        if !file && key.ino == ino && key.ino_gen == ino_gen {
            return Ok(Some(rel));
        }
        for e in std::fs::read_dir(layout.path(&rel))?.flatten() {
            let Ok(t) = e.file_type() else { continue };
            if t.is_dir() {
                todo.push(rel.join(e.file_name()));
            } else if file && t.is_file() {
                let child = rel.join(e.file_name());
                let Ok(fd) = sys::open_ro_beneath(&layout.data_root, &child) else {
                    continue;
                };
                let k = sys::inode_key(fd.as_fd())?;
                if k.ino == ino && k.ino_gen == ino_gen {
                    return Ok(Some(child));
                }
            }
        }
    }
    Ok(None)
}

/// A rewind placeholder being read: where it is, and the version it holds
/// as a replica-shaped node (`lid` -1: not in the replica).
#[derive(Clone, Debug)]
pub struct Target {
    pub rel: PathBuf,
    pub node: omnifile_replica::Node,
}

/// Resolve the rewind placeholder with this inode for `(file_id,
/// version_id)`: its marker must be `READONLY` and name that version, and
/// its whole-file CRC comes from the as-of listing of its directory.
pub async fn resolve<C: ControlPlane>(
    layout: &Layout,
    control: &C,
    ino: u64,
    ino_gen: u32,
    file_id: u64,
    version_id: u64,
) -> Result<Option<Target>, RewindError> {
    let l = layout.clone();
    let found = tokio::task::spawn_blocking(move || find_file(&l, ino, ino_gen))
        .await
        .map_err(io::Error::other)??;
    let Some(rel) = found else { return Ok(None) };
    let fd = sys::open_ro_beneath(&layout.data_root, &rel)?;
    let Some(m) = read_marker(fd.as_fd())? else {
        return Ok(None);
    };
    if !m.flags.contains(Flags::READONLY) || m.file_id != file_id || m.version_id != version_id {
        return Ok(None);
    }
    let (parent, name) = match (rel.parent(), rel.file_name().and_then(|n| n.to_str())) {
        (Some(p), Some(n)) => (p.to_owned(), n.to_owned()),
        _ => return Ok(None),
    };
    let Some((_, label, _)) = split(&parent) else {
        return Ok(None);
    };
    let t = label_time(&label).ok_or_else(|| RewindError::NotRewind(label.clone()))?;
    let pfd = sys::open_dir_beneath(&layout.data_root, &parent)?;
    let Some(pm) = read_marker(pfd.as_fd())? else {
        return Ok(None);
    };
    // The entry just before `name` can't be named, so page from the start.
    let mut after: Option<String> = None;
    loop {
        let page = control.asof(pm.file_id, &t, after.as_deref(), PAGE).await?;
        for raw in &page.entries {
            let Ok(e) = omnifile_replica::feed::parse_file_row(raw) else {
                continue;
            };
            if e.name == name {
                if e.file_id != file_id || e.version_id != Some(version_id) {
                    return Ok(None);
                }
                return Ok(Some(Target {
                    rel,
                    node: node_of(&e, ino, ino_gen),
                }));
            }
            after = Some(e.name.clone());
        }
        if !page.more || after.is_none() {
            return Ok(None);
        }
    }
}

fn node_of(e: &omnifile_replica::FileRow, ino: u64, ino_gen: u32) -> omnifile_replica::Node {
    omnifile_replica::Node {
        lid: -1,
        file_id: Some(e.file_id),
        dataset_id: e.dataset_id,
        parent_file_id: e.parent_id,
        parent_lid: None,
        waiting_parent: false,
        name: e.name.clone(),
        kind: e.kind,
        symlink_target: e.symlink_target.clone(),
        size: e.size,
        mtime_ns: e.mtime_ns,
        uid: e.uid,
        gid: e.gid,
        mode: e.mode,
        acl_id: e.acl_id,
        content_version_id: e.version_id,
        crc64nvme: e.crc64nvme,
        location: e.location.as_ref().map(|l| l.raw.clone()),
        archived: e.location.as_ref().is_some_and(|l| l.archived),
        online: e.location.as_ref().is_some_and(|l| !l.archived),
        stub_version_id: None,
        stub_resident_bytes: None,
        stub_status: String::new(),
        entity_version: e.entity_version,
        deleted: false,
        local_dirty: false,
        ino: Some(ino),
        ino_gen: Some(ino_gen),
        fs_parent_lid: None,
        fs_name: None,
        fs_version_id: Some(e.version_id.unwrap_or(0)),
        fs_pending: false,
        fs_conflict: None,
        fs_error: None,
        fs_ctime_ns: None,
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn history_is_read_only_and_never_set_id() {
        assert_eq!(super::history_mode(0o6775), 0o555);
        assert_eq!(super::history_mode(0o1777), 0o1555);
        assert_eq!(super::history_mode(0o640), 0o440);
    }

    use super::*;

    #[test]
    fn labels_are_daily_and_hourly_within_depth() {
        // 2026-10-15T08:30:00Z
        let now = 1_792_053_000;
        assert_eq!(label_of(now), "2026-10-15T0830Z");
        let p = LabelPolicy {
            depth_days: 2,
            daily_at_min: Some(0),
            hourly_for_hours: 3,
        };
        let got: Vec<String> = labels(&p, now).into_iter().collect();
        assert_eq!(
            got,
            [
                // 2026-10-13T0000Z is more than 2 days before now.
                "2026-10-14T0000Z",
                "2026-10-15T0000Z",
                "2026-10-15T0600Z",
                "2026-10-15T0700Z",
                "2026-10-15T0800Z",
            ]
        );
        assert!(labels(&LabelPolicy::default(), now).is_empty());
    }

    #[test]
    fn a_label_names_one_instant() {
        assert_eq!(
            label_time("2026-09-01T0000Z").as_deref(),
            Some("2026-09-01T00:00:00Z")
        );
        for bad in [
            "2026-09-01",
            "2026-09-01T2460Z",
            "x026-09-01T0000Z",
            "../etc/passwd",
        ] {
            assert_eq!(label_time(bad), None, "{bad}");
        }
        assert_eq!(label_of(0), "1970-01-01T0000Z");
        assert_eq!(label_of(951_782_400), "2000-02-29T0000Z");
    }

    #[test]
    fn the_policy_comes_from_the_share() {
        let v = serde_json::json!({"depth_days": 30, "max_days": 90,
            "labels": {"daily_at_utc": "02:30", "hourly_for_hours": 24}});
        assert_eq!(
            LabelPolicy::from_share(&v),
            LabelPolicy {
                depth_days: 30,
                daily_at_min: Some(150),
                hourly_for_hours: 24
            }
        );
        let none = serde_json::json!({"depth_days": 0, "labels": {}});
        assert_eq!(LabelPolicy::from_share(&none).depth_days, 0);
    }

    #[test]
    fn only_paths_below_the_area_split() {
        let (s, l, t) = split(Path::new(".omni/snap/proj/2026-09-01T0000Z/a/b")).unwrap();
        assert_eq!(
            (s.as_str(), l.as_str(), t),
            ("proj", "2026-09-01T0000Z", PathBuf::from("a/b"))
        );
        assert!(split(Path::new("proj/a")).is_none());
        assert!(split(Path::new(".omni/snap/proj")).is_none());
    }
}
