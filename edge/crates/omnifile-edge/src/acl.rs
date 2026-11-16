//! POSIX ACLs on the edge tree (decision 28, `access-control.md` §1.1 and
//! "Stubs and recalls never touch permissions").
//!
//! - **Placeholders.** A placeholder is created in the staging directory and
//!   linked into place, so it does not inherit the share's default ACL. After
//!   each feed page the daemon puts the right ACL on every placeholder it has
//!   not handled yet: the catalog's ACL when the file has one
//!   ([`omnifile_acl::to_local`], ids through winbind), otherwise what the
//!   kernel would have given a file created in its directory (the parent's
//!   default ACL, masked by the file's mode). Directories are created in
//!   place and inherit by themselves; a catalog ACL is put on them too.
//!   Each inode is handled once (per inode and `acl_id`), so a later Explorer
//!   edit is never undone by a feed update.
//! - **Fail closed.** If a catalog SID has no uid/gid here, guessing would
//!   drop a grant or a restriction: the placeholder keeps only its owner's
//!   bits and is tried again after the next page.
//! - **Normalisation.** Every inode the daemon touches here, and every file
//!   the detector reports, goes through [`omnifile_acl::normalize_inode`].
//!
//! Which inodes were handled is kept in a small side table in the replica
//! database (`edge_acl_applied`), so a restart does not redo them.

use std::os::fd::AsFd;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use std::time::Duration;

use omnifile_acl::{
    AclFs, AclSnapshot, DriftTracker, InodeKey, Perms, PosixAcl, SysFs, Tag, normalize_inode,
    to_local,
};
use omnifile_identity::IdMap;
use omnifile_marker::xattr::{self, ReadOutcome};
use omnifile_replica::{Kind, Node};
use omnifile_stub::materialize::PermsSource;
use omnifile_stub::placeholder::NewPerms;
use omnifile_stub::sys;
use tracing::{debug, info, warn};

use crate::core::Core;
use crate::ports::{BlobIo, ControlPlane};

const DDL: &str = "CREATE TABLE IF NOT EXISTS edge_acl_applied (\
    lid INTEGER PRIMARY KEY, ino INTEGER NOT NULL, acl_id INTEGER)";

/// Placeholders handled per pass.
const BATCH: i64 = 512;

/// What happened to one inode.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Applied {
    /// The catalog's ACL was put on it.
    Catalog,
    /// It inherited its directory's default ACL.
    Inherited,
    /// Nothing to do (resident data, no default ACL, already extended).
    Unchanged,
    /// Not done: fail closed (owner-only) and retry later.
    Deferred(String),
}

/// The access ACL a file created in a directory with default ACL `def`
/// and create mode `mode` gets (`posix_acl_create`).
pub fn inherit_access(def: &PosixAcl, mode: u32) -> PosixAcl {
    let mut a = def.clone();
    let and = |a: &mut PosixAcl, t: Tag, bits: u32| {
        if let Some(p) = a.get(t) {
            a.set(t, Perms(p.bits() & (bits & 7) as u8));
        }
    };
    and(&mut a, Tag::UserObj, mode >> 6);
    if a.get(Tag::Mask).is_some() {
        and(&mut a, Tag::Mask, mode >> 3);
    } else {
        and(&mut a, Tag::GroupObj, mode >> 3);
    }
    and(&mut a, Tag::Other, mode);
    a
}

/// Put the right ACL on one placeholder (or catalog-ACL'd directory).
pub fn apply_one(
    fs: &dyn AclFs,
    idmap: Option<&dyn IdMap>,
    root: &Path,
    rel: &Path,
    node: &Node,
    catalog: Option<&AclSnapshot>,
) -> std::io::Result<Applied> {
    let path = &root.join(rel);
    let is_dir = node.kind == Kind::Dir;
    if node.kind == Kind::File {
        // Only placeholders: a file whose bytes are local was created (or
        // last written) in place and has the ACL its writer left.
        // Beneath the data root, never through a symlink (as root).
        let fd = sys::open_ro_beneath(root, rel)?;
        match xattr::read(fd.as_fd())? {
            ReadOutcome::Marker(m) if !m.is_fully_resident() => {}
            _ => return Ok(Applied::Unchanged),
        }
    } else if !is_dir || catalog.is_none() {
        return Ok(Applied::Unchanged);
    }
    // Every write is pinned to the inode read here (and that inode must be
    // the replica's): a rename in between fails (ESTALE) instead of putting
    // the ACL on another file.
    let f = fs.read(path)?;
    if node.ino.is_some_and(|ino| ino != f.ino) {
        return Ok(Applied::Deferred(format!(
            "{} is not the replica's inode",
            path.display()
        )));
    }
    let pin = (f.dev, f.ino);
    let out = match catalog {
        Some(snap) => {
            let Some(idmap) = idmap else {
                return Ok(Applied::Deferred("no identity source".into()));
            };
            match to_local(snap, idmap) {
                Ok(local) => {
                    fs.write_access_at(path, pin, &local.access)?;
                    if is_dir {
                        fs.write_default_at(path, pin, local.default.as_ref())?;
                    }
                    Applied::Catalog
                }
                Err(e) => {
                    // Fail closed: owner-only until the SIDs map.
                    if f.mode & 0o077 != 0 || f.access.is_some() {
                        fs.write_access_at(path, pin, &PosixAcl::from_mode(f.mode & 0o700))?;
                    }
                    return Ok(Applied::Deferred(e.to_string()));
                }
            }
        }
        None => {
            let parent = path.parent().unwrap_or(Path::new("/"));
            if f.access.is_some() {
                Applied::Unchanged
            } else {
                match fs.read(parent)?.default {
                    Some(def) => {
                        fs.write_access_at(path, pin, &inherit_access(&def, node.mode))?;
                        Applied::Inherited
                    }
                    None => Applied::Unchanged,
                }
            }
        }
    };
    normalize_inode(fs, path)?;
    Ok(out)
}

/// The permissions each new placeholder or directory gets before its name
/// exists (M2 review S4), so no user can open it under looser permissions
/// than [`apply_one`] would give it a moment later. Same outcome as
/// [`apply_one`]: the catalog's ACL ([`to_local`]); else what the kernel
/// would inherit from the directory's default ACL; else the mode. When the
/// catalog ACL cannot be mapped (or has not arrived), owner-only until
/// [`sync`] can apply it.
pub struct CreatePerms {
    idmap: Option<Arc<dyn IdMap>>,
    catalog: std::sync::Mutex<std::collections::HashMap<u64, AclSnapshot>>,
}

impl CreatePerms {
    pub fn new(idmap: Option<Arc<dyn IdMap>>) -> CreatePerms {
        CreatePerms {
            idmap,
            catalog: Default::default(),
        }
    }

    /// Cache the catalog ACLs `ops` name, from the replica (call under the
    /// replica lock taken to read the ops).
    pub fn load(&self, rep: &omnifile_replica::Replica, ops: &[omnifile_replica::PendingOp]) {
        let mut cache = self.catalog.lock().unwrap();
        for id in ops.iter().filter_map(|o| o.node.acl_id) {
            if cache.contains_key(&id) {
                continue;
            }
            let body: Option<String> = rep
                .conn()
                .query_row("SELECT body FROM acl WHERE acl_id = ?1", [id as i64], |r| {
                    r.get(0)
                })
                .ok();
            if let Some(snap) = body.and_then(|b| serde_json::from_str::<AclSnapshot>(&b).ok()) {
                cache.insert(id, snap);
            }
        }
    }

    /// [`NewPerms`] for the catalog ACL `acl_id`, read from the replica
    /// (rewind trees): owner-only when it is unknown here or can't be mapped.
    /// `with_default` also gives a directory the ACL's default entries.
    /// Every entry loses `w` (the mask too): setting the ACL sets the mode
    /// from it, and rewind entries are never writable.
    pub fn for_acl(
        &self,
        rep: &omnifile_replica::Replica,
        acl_id: u64,
        with_default: bool,
    ) -> NewPerms {
        let body: Option<String> = rep
            .conn()
            .query_row(
                "SELECT body FROM acl WHERE acl_id = ?1",
                [acl_id as i64],
                |r| r.get(0),
            )
            .ok();
        let snap = body.and_then(|b| serde_json::from_str::<AclSnapshot>(&b).ok());
        let (Some(snap), Some(idmap)) = (snap, self.idmap.as_deref()) else {
            return NewPerms::OwnerOnly;
        };
        match to_local(&snap, idmap) {
            Ok(local) => NewPerms::Acl {
                access: local.access.without_write().to_xattr(),
                default: if with_default {
                    local.default.as_ref().map(|d| d.without_write().to_xattr())
                } else {
                    None
                },
            },
            Err(_) => NewPerms::OwnerOnly,
        }
    }

    /// [`NewPerms`] for `node` (also usable without the trait).
    pub fn decide(&self, node: &Node, parent_default: Option<&[u8]>) -> NewPerms {
        let is_dir = node.kind == Kind::Dir;
        if let Some(id) = node.acl_id {
            let snap = self.catalog.lock().unwrap().get(&id).cloned();
            let (Some(snap), Some(idmap)) = (snap, self.idmap.as_deref()) else {
                return NewPerms::OwnerOnly;
            };
            return match to_local(&snap, idmap) {
                Ok(local) => NewPerms::Acl {
                    access: local.access.to_xattr(),
                    default: if is_dir {
                        local.default.as_ref().map(PosixAcl::to_xattr)
                    } else {
                        None
                    },
                },
                Err(_) => NewPerms::OwnerOnly,
            };
        }
        match parent_default.map(PosixAcl::from_xattr) {
            None => NewPerms::Mode,
            Some(Ok(def)) => NewPerms::Acl {
                access: inherit_access(&def, node.mode).to_xattr(),
                default: is_dir.then(|| def.to_xattr()),
            },
            Some(Err(_)) => NewPerms::OwnerOnly,
        }
    }
}

impl PermsSource for CreatePerms {
    fn new_perms(&self, node: &Node, parent_default_acl: Option<&[u8]>) -> NewPerms {
        self.decide(node, parent_default_acl)
    }
}

struct Todo {
    lid: i64,
    ino: u64,
    acl_id: Option<u64>,
    node: Node,
    rel: PathBuf,
    path: PathBuf,
    catalog: Option<AclSnapshot>,
}

/// One pass: every materialised node not handled yet. Returns how many
/// inodes got an ACL.
pub async fn sync<C: ControlPlane, B: BlobIo>(core: &Arc<Core<C, B>>) -> usize {
    let todo = {
        let rep = core.replica.lock().await;
        let conn = rep.conn();
        if let Err(e) = conn.execute(DDL, []) {
            warn!(err = %e, "replica: acl side table");
            return 0;
        }
        let lids: Vec<i64> = match conn
            .prepare(
                "SELECT n.lid FROM node n LEFT JOIN edge_acl_applied a ON a.lid = n.lid \
                 WHERE n.kind IN ('file', 'dir') AND n.deleted = 0 AND n.ino IS NOT NULL \
                   AND n.fs_pending = 0 AND n.file_id IS NOT NULL \
                   AND (a.lid IS NULL OR a.ino != n.ino OR a.acl_id IS NOT n.acl_id) \
                 ORDER BY n.lid LIMIT ?1",
            )
            .and_then(|mut st| {
                st.query_map([BATCH], |r| r.get::<_, i64>(0))?
                    .collect::<Result<Vec<_>, _>>()
            }) {
            Ok(v) => v,
            Err(e) => {
                warn!(err = %e, "replica: acl candidates");
                return 0;
            }
        };
        let mut todo = Vec::new();
        for lid in lids {
            let (Ok(Some(node)), Ok(Some(rel))) = (rep.node(lid), rep.fs_path(lid)) else {
                continue;
            };
            if rel
                .components()
                .next()
                .is_some_and(|c| c.as_os_str() == ".omni")
            {
                continue;
            }
            let catalog = node.acl_id.and_then(|id| {
                let body: Option<String> = conn
                    .query_row("SELECT body FROM acl WHERE acl_id = ?1", [id as i64], |r| {
                        r.get(0)
                    })
                    .ok();
                body.and_then(|b| serde_json::from_str::<AclSnapshot>(&b).ok())
            });
            todo.push(Todo {
                lid,
                ino: node.ino.unwrap_or(0),
                acl_id: node.acl_id,
                path: core.layout.path(&rel),
                rel,
                node,
                catalog,
            });
        }
        todo
    };
    if todo.is_empty() {
        return 0;
    }
    let idmap = core.identity.get().map(|i| i.idmap.clone());
    let root = core.layout.data_root.clone();
    let done = tokio::task::spawn_blocking(move || {
        let mut done = Vec::new();
        for t in todo {
            if t.acl_id.is_some() && t.catalog.is_none() {
                // The acl.upsert has not arrived yet (it comes first in a
                // page, so this is a malformed row): try again later.
                debug!(lid = t.lid, acl_id = ?t.acl_id, "catalog ACL not in the replica");
                continue;
            }
            let r = apply_one(
                &SysFs,
                idmap.as_deref(),
                &root,
                &t.rel,
                &t.node,
                t.catalog.as_ref(),
            );
            match r {
                Ok(Applied::Deferred(why)) => {
                    warn!(path = %t.path.display(), why, "placeholder ACL deferred (owner-only meanwhile)")
                }
                Ok(a) => {
                    if a != Applied::Unchanged {
                        info!(path = %t.path.display(), how = ?a, "placeholder ACL set");
                    }
                    done.push((t.lid, t.ino, t.acl_id, a != Applied::Unchanged));
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => warn!(path = %t.path.display(), err = %e, "placeholder ACL"),
            }
        }
        done
    })
    .await
    .unwrap_or_default();
    let rep = core.replica.lock().await;
    let mut n = 0;
    for (lid, ino, acl_id, changed) in done {
        n += changed as usize;
        let _ = rep.conn().execute(
            "INSERT INTO edge_acl_applied (lid, ino, acl_id) VALUES (?1, ?2, ?3) \
             ON CONFLICT(lid) DO UPDATE SET ino = excluded.ino, acl_id = excluded.acl_id",
            (lid, ino as i64, acl_id.map(|a| a as i64)),
        );
    }
    n
}

/// Normalise a file the detector reported (a local or SMB/NFS write).
pub fn normalize_path(path: &Path) {
    if let Err(e) = normalize_inode(&SysFs, path)
        && e.kind() != std::io::ErrorKind::NotFound
    {
        debug!(path = %path.display(), err = %e, "normalise ACL");
    }
}

/// One share tree to scan, as the replica places it.
struct ShareRoot {
    dataset_id: u64,
    path: PathBuf,
}

/// The ACL scans (control API §4.6): a full scan of every share every
/// `[acl] full_scan_s` (the nightly scan; this is how Explorer edits, which
/// raise no write event, are found), and on-change batches of the files the
/// detector saw written. Each reports only ACLs that changed since the last
/// report ([`DriftTracker`], seeded from the catalog's view in the replica):
/// the bodies through `POST …/acls`, then one `acl` change op per file,
/// bracketed by `acl-scans` and `…/complete`. Never writes to the tree.
pub async fn scan_loop<C: ControlPlane, B: BlobIo>(core: Arc<Core<C, B>>) {
    let cfg = core.cfg.acl.clone();
    let mut stop = core.shutdown.subscribe();
    let mut tracker = DriftTracker::new();
    let mut seeded = false;
    let mut next_full = tokio::time::Instant::now() + Duration::from_secs(cfg.first_full_s);
    let every = Duration::from_secs(cfg.on_change_s.max(1));
    loop {
        tokio::select! {
            _ = tokio::time::sleep(every) => {}
            _ = stop.changed() => return,
        }
        if core.is_shutting_down() {
            return;
        }
        if !seeded {
            seed(&core, &mut tracker).await;
            seeded = true;
        }
        if tokio::time::Instant::now() >= next_full {
            next_full = tokio::time::Instant::now() + Duration::from_secs(cfg.full_scan_s.max(10));
            for root in share_roots(&core).await {
                report(
                    &core,
                    &mut tracker,
                    root.dataset_id,
                    "full",
                    vec![root.path],
                )
                .await;
            }
            // A full scan covers the pending files too.
            core.acl_dirty.lock().unwrap().clear();
            continue;
        }
        let dirty: Vec<PathBuf> = core.acl_dirty.lock().unwrap().drain().collect();
        if dirty.is_empty() {
            continue;
        }
        for root in share_roots(&core).await {
            let mine: Vec<PathBuf> = dirty
                .iter()
                .filter(|p| p.starts_with(&root.path))
                .cloned()
                .collect();
            if !mine.is_empty() {
                report(&core, &mut tracker, root.dataset_id, "on_change", mine).await;
            }
        }
    }
}

/// What the catalog already has, so a restart doesn't report everything.
async fn seed<C: ControlPlane, B: BlobIo>(core: &Arc<Core<C, B>>, tracker: &mut DriftTracker) {
    use std::os::unix::fs::MetadataExt;
    let Ok(dev) = std::fs::metadata(&core.layout.data_root).map(|m| m.dev()) else {
        return;
    };
    let rep = core.replica.lock().await;
    let rows: Vec<(i64, String)> = rep
        .conn()
        .prepare(
            "SELECT n.ino, a.sha256 FROM node n JOIN acl a ON a.acl_id = n.acl_id \
             WHERE n.ino IS NOT NULL AND n.deleted = 0",
        )
        .and_then(|mut st| {
            st.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
                .collect::<Result<Vec<_>, _>>()
        })
        .unwrap_or_default();
    for (ino, sha) in rows {
        tracker.seed(
            InodeKey {
                dev,
                ino: ino as u64,
            },
            sha,
        );
    }
    debug!(
        seeded = tracker.len(),
        "ACL drift tracker seeded from the replica"
    );
}

async fn share_roots<C: ControlPlane, B: BlobIo>(core: &Arc<Core<C, B>>) -> Vec<ShareRoot> {
    let rep = core.replica.lock().await;
    let mut out: Vec<ShareRoot> = Vec::new();
    for s in rep.shares().unwrap_or_default() {
        let Some(root) = s.root_dir_id else { continue };
        let Ok(Some(n)) = rep.node_by_file_id(root) else {
            continue;
        };
        if let Ok(Some(rel)) = rep.fs_path(n.lid) {
            let path = core.layout.path(&rel);
            if !out.iter().any(|r| r.path == path) {
                out.push(ShareRoot {
                    dataset_id: s.dataset_id,
                    path,
                });
            }
        }
    }
    out
}

/// Scan `paths` (share roots, or single files) and report what changed.
async fn report<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    tracker: &mut DriftTracker,
    dataset_id: u64,
    kind: &'static str,
    paths: Vec<PathBuf>,
) {
    let Some(idmap) = core.identity.get().map(|i| i.idmap.clone()) else {
        return;
    };
    let scanned = tokio::task::spawn_blocking(move || {
        let mut all = omnifile_acl::ScanResult::default();
        for p in paths {
            match omnifile_acl::scan(&SysFs, idmap.as_ref(), &p, &[".omni"]) {
                Ok(r) => {
                    all.entries_seen += r.entries_seen;
                    all.files.extend(r.files);
                    all.acls.extend(r.acls);
                    all.findings.extend(r.findings);
                }
                Err(e) => warn!(path = %p.display(), err = %e, "ACL scan"),
            }
        }
        all
    })
    .await;
    let Ok(scan) = scanned else { return };
    for f in &scan.findings {
        if let omnifile_acl::Finding::NtAcl(p) = f {
            warn!(path = %p.display(), "security.NTACL present: a Windows ACL NFS never enforces");
        }
    }
    let changed: Vec<omnifile_acl::scan::ScannedFile> =
        tracker.changed(&scan).into_iter().cloned().collect();
    // Only catalog objects can point at an ACL.
    let mut ops: Vec<(u64, String, u32)> = Vec::new();
    let mut keys = Vec::new();
    {
        let rep = core.replica.lock().await;
        for f in &changed {
            let file_id: Option<i64> = rep
                .conn()
                .query_row(
                    "SELECT file_id FROM node WHERE ino = ?1 AND deleted = 0 AND file_id IS NOT NULL",
                    [f.key.ino as i64],
                    |r| r.get(0),
                )
                .ok();
            match file_id {
                Some(id) => {
                    let mode = scan.acls.get(&f.acl_sha256).map(|a| a.mode).unwrap_or(0);
                    ops.push((id as u64, f.acl_sha256.clone(), mode));
                    keys.push(f.key);
                }
                // Not in the catalog yet: report it once it is.
                None => tracker.forget(&f.key),
            }
        }
    }
    let nonce = match core.replica.lock().await.next_counter("acl-scan") {
        Ok(n) => format!("{}-{n}", core.cfg.edge_id),
        Err(_) => return,
    };
    let scan_id = match core.control.acl_scan_start(dataset_id, kind, &nonce).await {
        Ok(id) => id,
        Err(e) => {
            debug!(err = %e, "acl-scans: start");
            for k in &keys {
                tracker.forget(k);
            }
            return;
        }
    };
    let mut ok = true;
    if !ops.is_empty() {
        let wanted: std::collections::BTreeSet<&str> =
            ops.iter().map(|(_, s, _)| s.as_str()).collect();
        let bodies: Vec<serde_json::Value> = scan
            .acls
            .iter()
            .filter(|(sha, _)| wanted.contains(sha.as_str()))
            .filter_map(|(_, a)| serde_json::to_value(a).ok())
            .collect();
        let sent = match core.control.acl_put(dataset_id, bodies).await {
            Ok(()) => {
                core.control
                    .acl_set(&format!("acl-{nonce}"), ops.clone())
                    .await
            }
            Err(e) => Err(e),
        };
        match sent {
            Ok(n) => info!(
                dataset_id,
                kind,
                changed = n,
                seen = scan.entries_seen,
                "ACL changes reported"
            ),
            Err(e) => {
                warn!(dataset_id, kind, err = %e, "ACL report failed; retried by the next scan");
                for k in &keys {
                    tracker.forget(k);
                }
                ok = false;
            }
        }
    }
    let changed_n = if ok { ops.len() as u64 } else { 0 };
    if let Err(e) = core
        .control
        .acl_scan_complete(scan_id, scan.entries_seen, changed_n.min(scan.entries_seen))
        .await
    {
        debug!(err = %e, scan_id, "acl-scans: complete");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rewind_entries_get_no_write_bit_from_their_acl() {
        // Review S1: setting an ACL sets the mode from it, so a `w` left in
        // any entry (the mask included) made rewind entries writable again.
        let d = omnifile_identity::fake::FakeIdMap::DOMAIN_SID;
        let rep = omnifile_replica::Replica::open_in_memory().unwrap();
        let body = serde_json::json!({
            "sha256": "x", "owner_sid": format!("{d}-1112"), "group_sid": format!("{d}-1105"),
            "mode": 0o770,
            "entries": [
                {"scope": "access", "tag": "owner", "perms": "rwx"},
                {"scope": "access", "tag": "owning_group", "perms": "rwx"},
                {"scope": "access", "tag": "group", "sid": format!("{d}-1104"), "perms": "rw-"},
                {"scope": "access", "tag": "mask", "perms": "rwx"},
                {"scope": "access", "tag": "other", "perms": "r--"},
                {"scope": "default", "tag": "owner", "perms": "rwx"},
                {"scope": "default", "tag": "owning_group", "perms": "rwx"},
                {"scope": "default", "tag": "other", "perms": "---"},
            ],
        });
        rep.conn()
            .execute(
                "INSERT INTO acl (acl_id, dataset_id, sha256, entity_version, body)
                 VALUES (7, 1, 'x', 1, ?1)",
                [body.to_string()],
            )
            .unwrap();
        let idmap: Arc<dyn IdMap> = Arc::new(omnifile_identity::fake::FakeIdMap::dev_ad());
        let perms = CreatePerms::new(Some(idmap));
        let NewPerms::Acl { access, default } = perms.for_acl(&rep, 7, true) else {
            panic!("the ACL maps");
        };
        let access = PosixAcl::from_xattr(&access).unwrap();
        assert!(access.entries().len() >= 5);
        assert!(
            access.entries().iter().all(|e| !e.perms.write()),
            "{access:?}"
        );
        assert!(access.entries().iter().any(|e| e.perms.read()));
        let default = PosixAcl::from_xattr(&default.unwrap()).unwrap();
        assert!(default.entries().iter().all(|e| !e.perms.write()));
    }

    #[test]
    fn inherit_masks_by_mode() {
        let def = PosixAcl::from_text(
            "user::rwx,group::r-x,group:30001:r-x,group:30002:rwx,mask::rwx,other::---",
        )
        .unwrap();
        let a = inherit_access(&def, 0o640);
        assert_eq!(a.get(Tag::UserObj), Some(Perms::RW));
        assert_eq!(a.get(Tag::Mask), Some(Perms::R));
        // Named entries are kept; the mask limits them.
        assert_eq!(a.get(Tag::Group(30002)), Some(Perms::RWX));
        assert_eq!(a.get(Tag::Other), Some(Perms::NONE));
        let minimal = PosixAcl::from_mode(0o755);
        let b = inherit_access(&minimal, 0o600);
        assert_eq!(b.get(Tag::GroupObj), Some(Perms::NONE));
    }

    fn node(kind: Kind, mode: u32, acl_id: Option<u64>) -> Node {
        Node {
            lid: 1,
            file_id: Some(9),
            dataset_id: 1,
            parent_file_id: None,
            parent_lid: None,
            waiting_parent: false,
            name: "f".into(),
            kind,
            symlink_target: None,
            size: 10,
            mtime_ns: 0,
            uid: 1000,
            gid: 2000,
            mode,
            acl_id,
            content_version_id: Some(1),
            crc64nvme: None,
            location: None,
            archived: false,
            online: true,
            stub_version_id: None,
            stub_resident_bytes: None,
            stub_status: String::new(),
            entity_version: 1,
            deleted: false,
            local_dirty: false,
            ino: None,
            ino_gen: None,
            fs_parent_lid: None,
            fs_name: None,
            fs_version_id: None,
            fs_pending: true,
            fs_conflict: None,
            fs_error: None,
            fs_ctime_ns: None,
        }
    }

    /// S4: the permissions a new placeholder gets before its name exists
    /// are what `apply_one` would put on it, or owner-only.
    #[test]
    fn create_perms_match_the_sync_or_fail_closed() {
        use omnifile_identity::Sid;
        let p = CreatePerms::new(Some(Arc::new(omnifile_identity::fake::FakeIdMap::new(
            "OMNI",
        ))));
        // No catalog ACL, no default ACL: the mode.
        assert_eq!(
            p.decide(&node(Kind::File, 0o640, None), None),
            NewPerms::Mode
        );
        // No catalog ACL: what the kernel would inherit (masked by mode).
        let def = PosixAcl::from_text("user::rwx,group::r-x,group:30001:r-x,mask::rwx,other::---")
            .unwrap();
        let NewPerms::Acl { access, default } =
            p.decide(&node(Kind::File, 0o640, None), Some(&def.to_xattr()))
        else {
            panic!("expected an ACL")
        };
        assert_eq!(
            PosixAcl::from_xattr(&access).unwrap(),
            inherit_access(&def, 0o640)
        );
        assert_eq!(default, None);
        let NewPerms::Acl { default, .. } =
            p.decide(&node(Kind::Dir, 0o750, None), Some(&def.to_xattr()))
        else {
            panic!("expected an ACL")
        };
        assert_eq!(default, Some(def.to_xattr()));
        // A catalog ACL not in the cache yet: owner-only.
        assert_eq!(
            p.decide(&node(Kind::File, 0o644, Some(5)), None),
            NewPerms::OwnerOnly
        );
        // A catalog ACL that maps (unix-id SIDs): exactly `to_local`'s.
        let acl = PosixAcl::from_text("user::rw-,group::r--,group:30001:r--,mask::r--,other::---")
            .unwrap();
        let mk = |sid: &str| {
            omnifile_acl::snapshot::snapshot_with(
                Some(Sid::parse("S-1-22-1-1000").unwrap()),
                Some(Sid::parse("S-1-22-2-2000").unwrap()),
                0o640,
                &acl,
                None,
                |_| Some(Sid::parse(sid).unwrap()),
            )
        };
        let good = mk("S-1-22-2-30001");
        p.catalog.lock().unwrap().insert(5, good.clone());
        let NewPerms::Acl { access, .. } = p.decide(&node(Kind::File, 0o644, Some(5)), None) else {
            panic!("expected the catalog ACL")
        };
        let want = to_local(&good, &omnifile_identity::fake::FakeIdMap::new("OMNI")).unwrap();
        assert_eq!(PosixAcl::from_xattr(&access).unwrap(), want.access);
        // A SID with no id here: owner-only, never a guess.
        p.catalog
            .lock()
            .unwrap()
            .insert(6, mk("S-1-5-21-1-2-3-4242"));
        assert_eq!(
            p.decide(&node(Kind::File, 0o644, Some(6)), None),
            NewPerms::OwnerOnly
        );
        // No identity source at all: owner-only.
        let none = CreatePerms::new(None);
        none.catalog.lock().unwrap().insert(5, good);
        assert_eq!(
            none.decide(&node(Kind::File, 0o644, Some(5)), None),
            NewPerms::OwnerOnly
        );
    }
}
