//! The edge's shares, served (M6, docs/design/share-provisioning.md).
//!
//! From the `share.upsert` rows in the replica, the daemon:
//! - renders each share's Samba stanza and Ganesha `EXPORT` block
//!   ([`ShareConfig`], access-control.md §3–4) into `[shares] smb_conf` and
//!   `ganesha_conf`, which `/etc/samba/omnifile-shares.conf` and
//!   `/etc/ganesha/omnifile-exports.conf` include;
//! - reloads smbd and Ganesha when either file changed (`[shares] reload`);
//! - gives each share root its Owners/Writers/Readers ACL, once the root is
//!   on disk and winbind maps the groups, and again when the groups change.
//!
//! A share is rendered only once its root is on disk, and only if it passes
//! [`ShareConfig::validate`]; a share that doesn't is left out and logged,
//! and every other share is still served. The files are replaced atomically
//! and only when their content changes, so a restart reloads nothing.
//!
//! The servers take a share's root by path, so a share is served only while
//! that path is still the directory the edge made (the replica's inode, with
//! no symlink on the way), checked at every pass ([`root_state`]). A folder
//! share's root sits in its parent share, whose Writers can rename it and put
//! their own directory, or a symlink, in its place; the share then stops being
//! served until the folder is back, and its groups' ACL is only ever written to
//! the edge's own directory ([`omnifile_acl::apply_root_at`]).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tracing::{error, info, warn};

use omnifile_acl::{FsalPolicy, Protocol, ShareConfig, ShareGroups};
use omnifile_identity::{NfsSec, Sid, TrustLevel};

use crate::core::Core;
use crate::ports::{BlobIo, ControlPlane};

const SMB_HEADER: &str = "# Rendered by omnifile-edge from the catalog's shares. Do not edit:\n\
                          # change shares in the portal (docs/design/share-provisioning.md).\n";

/// How often the replica's shares are compared with what was rendered.
const EVERY: Duration = Duration::from_secs(5);
/// After a failed step (a reload, an ACL, a group winbind can't map yet).
const RETRY: Duration = Duration::from_secs(30);

/// Where a share root stands on disk, against the replica.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum RootState {
    /// Not made yet: the replica has no inode for it.
    Missing,
    /// The directory the edge made: `(st_dev, st_ino)`.
    Ours(u64, u64),
    /// Something else is at its path, or nothing: it was renamed away and
    /// perhaps replaced by another directory or a symlink.
    Moved,
}

/// Is the share root at `rel` (below `data_root`) still the inode the replica
/// made, `made` = `(ino, ino_gen)`, reached with no symlink on the way?
pub fn root_state(data_root: &Path, rel: &Path, made: Option<(u64, u32)>) -> RootState {
    let Some((ino, generation)) = made else {
        return RootState::Missing;
    };
    let Ok(fd) = omnifile_stub::sys::open_dir_beneath(data_root, rel) else {
        // ENOENT (renamed away), ELOOP (a symlink), ENOTDIR, EXDEV ...
        return RootState::Moved;
    };
    use std::os::fd::AsFd;
    match omnifile_stub::sys::inode_key(fd.as_fd()) {
        Ok(k) if k.ino == ino && k.ino_gen == generation => {
            match omnifile_stub::sys::fstat(fd.as_fd()) {
                Ok(st) => RootState::Ours(st.st_dev, st.st_ino),
                Err(_) => RootState::Moved,
            }
        }
        _ => RootState::Moved,
    }
}

/// A share root's path below the data root, and the inode the edge made it
/// as, `(ino, ino_gen)`.
type RootAt = (PathBuf, Option<(u64, u32)>);

/// [`RootAt`] for a share's root folder; `None` until it is on disk.
fn root_of(rep: &omnifile_replica::Replica, root_dir_id: u64) -> Option<RootAt> {
    let rel = rep.dir_path(root_dir_id).ok().flatten()?;
    let made = rep
        .node_by_file_id(root_dir_id)
        .ok()
        .flatten()
        .and_then(|n| Some((n.ino?, n.ino_gen?)));
    Some((rel, made))
}

/// A share as the edge serves it: its rendered config and its root's groups.
#[derive(Debug)]
pub struct Rendered {
    pub share_id: u64,
    pub config: ShareConfig,
    /// The root the edge checked it made, `(st_dev, st_ino)`: the only inode
    /// its ACL is written to.
    pub root_pin: Option<(u64, u64)>,
    /// The groups' SIDs, mapped to gids for the root ACL.
    pub owners: Sid,
    pub writers: Option<Sid>,
    pub readers: Option<Sid>,
}

/// One `share.upsert` row, mapped to a [`ShareConfig`]. `path` is the share
/// root on disk; `snapdir` its rewind area when it keeps rewind depth.
pub fn share_config(
    row: &Value,
    path: PathBuf,
    snapdir: Option<PathBuf>,
    smb_prevver: bool,
) -> Result<Rendered, String> {
    let s = |k: &str| row.get(k).and_then(Value::as_str);
    let list = |k: &str| -> Vec<String> {
        row.get(k)
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default()
    };
    let name = s("name").ok_or("no name")?.to_owned();
    let protocols = list("protocols")
        .iter()
        .map(|p| match p.as_str() {
            "nfs3" => Ok(Protocol::Nfs3),
            "nfs4" => Ok(Protocol::Nfs4),
            "smb" => Ok(Protocol::Smb),
            o => Err(format!("unknown protocol {o:?}")),
        })
        .collect::<Result<Vec<_>, _>>()?;
    let nfs_sec = list("nfs_sec")
        .iter()
        .map(|v| {
            serde_json::from_value::<NfsSec>(Value::String(v.clone()))
                .map_err(|_| format!("unknown NFS security flavour {v:?}"))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let nfs = protocols.iter().any(|p| *p != Protocol::Smb);
    let trust_level = match s("trust_level") {
        Some(t) => serde_json::from_value::<TrustLevel>(Value::String(t.into()))
            .map_err(|_| format!("unknown trust level {t:?}"))?,
        None => TrustLevel::of(nfs, &nfs_sec),
    };
    let sid = |k: &str| -> Result<Option<Sid>, String> {
        s(k).map(|v| Sid::parse(v).map_err(|e| format!("{k}: {e}")))
            .transpose()
    };
    let owners = sid("owners_group_sid")?.ok_or("no owners group")?;
    let (writers, readers) = (sid("writers_group_sid")?, sid("readers_group_sid")?);
    let export_id = row
        .get("nfs_export_id")
        .and_then(Value::as_u64)
        .and_then(|v| u16::try_from(v).ok())
        .ok_or("no nfs_export_id: the service predates M6 share rendering")?;
    let fsal_policy = match s("on_open").unwrap_or("fail") {
        "wait" => FsalPolicy::Wait,
        "fail" => FsalPolicy::FailFast,
        "auto" => {
            let min = row
                .get("wait_threshold_minutes")
                .and_then(Value::as_u64)
                .ok_or("on_open auto without wait_threshold_minutes")?;
            FsalPolicy::WaitQuick {
                ms: min.saturating_mul(60_000),
            }
        }
        o => return Err(format!("unknown on_open {o:?}")),
    };
    let flag = |k: &str| row.get(k).and_then(Value::as_bool).unwrap_or(false);
    let config = ShareConfig {
        name,
        path,
        protocols,
        nfs_sec,
        nfs_clients: list("nfs_clients"),
        root_squash: row
            .get("root_squash")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        trust_level,
        owners_sid: owners.clone(),
        writers_sid: writers.clone(),
        readers_sid: readers.clone(),
        // The service checks restricted folders (and records the override)
        // when the share is made; the edge has no catalog-wide view of them.
        restricted: false,
        host_trusted_override: flag("host_trust_override"),
        export_id,
        fsal_policy,
        smb_encrypt: flag("smb_encrypt"),
        smb_prevver: smb_prevver && snapdir.is_some(),
        snapdir,
    };
    config.validate().map_err(|e| {
        e.iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>()
            .join("; ")
    })?;
    Ok(Rendered {
        share_id: row
            .get("share_id")
            .and_then(Value::as_str)
            .and_then(|v| v.parse().ok())
            .unwrap_or(0),
        config,
        root_pin: None,
        owners,
        writers,
        readers,
    })
}

/// Both files' content, shares in name order so the output is stable.
/// Samba and Windows match share names regardless of case, so of two names
/// that differ only in case the older share is served and the other left
/// out (the service refuses such a name; this is the backstop).
pub fn render_all(shares: &[Rendered]) -> (String, String) {
    let mut by_name: Vec<&Rendered> = shares.iter().collect();
    by_name.sort_by_key(|r| (r.config.name.to_lowercase(), r.share_id));
    by_name.dedup_by(|b, a| {
        let dup = a.config.name.eq_ignore_ascii_case(&b.config.name);
        if dup {
            error!(share_id = b.share_id, share = %b.config.name, kept = %a.config.name,
                   "share name differs from another only in case: not served");
        }
        dup
    });
    let mut smb = String::from(SMB_HEADER);
    let mut ganesha = String::from(SMB_HEADER);
    for r in by_name {
        if let Some(st) = r.config.render_smb() {
            smb.push('\n');
            smb.push_str(&st);
        }
        if let Some(ex) = r.config.render_ganesha() {
            ganesha.push('\n');
            ganesha.push_str(&ex);
        }
    }
    (smb, ganesha)
}

/// Replaces `path` with `content` (atomically) unless it already holds it.
/// Returns whether it changed.
pub fn write_if_changed(path: &Path, content: &str) -> std::io::Result<bool> {
    if std::fs::read(path).ok().as_deref() == Some(content.as_bytes()) {
        return Ok(false);
    }
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, content)?;
    std::fs::set_permissions(&tmp, std::os::unix::fs::PermissionsExt::from_mode(0o644))?;
    std::fs::File::open(&tmp)?.sync_all()?;
    std::fs::rename(&tmp, path)?;
    Ok(true)
}

/// The gids each share root was last given, by share id: the entries to
/// replace when the groups change.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
struct Applied {
    owners: u32,
    writers: Option<u32>,
    readers: Option<u32>,
}

impl From<Applied> for ShareGroups {
    fn from(a: Applied) -> ShareGroups {
        ShareGroups {
            owners: a.owners,
            writers: a.writers,
            readers: a.readers,
        }
    }
}

fn applied_path(cfg: &crate::config::Config) -> PathBuf {
    cfg.replica_path.with_file_name("share-acls.json")
}

fn load_applied(p: &Path) -> BTreeMap<u64, Applied> {
    std::fs::read(p)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or_default()
}

/// One pass. Returns whether everything was done (false: retry soon).
pub async fn sync_once<C: ControlPlane, B: BlobIo>(core: &Arc<Core<C, B>>) -> bool {
    let cfg = &core.cfg.shares;
    let rows = {
        let rep = core.replica.lock().await;
        let mut out = Vec::new();
        for (id, _ev, body) in rep.share_rows().unwrap_or_default() {
            let Ok(row) = serde_json::from_str::<Value>(&body) else {
                continue;
            };
            let root = row
                .get("root_dir_id")
                .and_then(Value::as_str)
                .and_then(|v| v.parse::<u64>().ok())
                .and_then(|r| root_of(&rep, r));
            out.push((id, row, root));
        }
        out
    };
    // Is each root still the directory the edge made? (Off the async workers.)
    let data_root = core.layout.data_root.clone();
    let rows = tokio::task::spawn_blocking(move || {
        rows.into_iter()
            .map(|(id, row, root)| {
                let root = root.map(|(rel, made)| {
                    let st = root_state(&data_root, &rel, made);
                    (rel, st)
                });
                (id, row, root)
            })
            .collect::<Vec<_>>()
    })
    .await
    .unwrap_or_default();
    let prevver = match cfg.smb_prevver.as_str() {
        "on" => true,
        "off" => false,
        _ => match tokio::task::spawn_blocking(omnifile_acl::prevver_usable)
            .await
            .unwrap_or(Ok(false))
        {
            Ok(usable) => usable,
            // Installed, but not for this smbd: naming it would stop every
            // rewind share connecting. Leave it out (shadow_copy2 alone still
            // serves Previous Versions) and say why, every render.
            Err(e) => {
                error!("vfs_omnifile_prevver is left out of the SMB shares: {e}");
                false
            }
        },
    };
    let mut done = true;
    let mut shares = Vec::new();
    for (id, row, root) in rows {
        let name = row.get("name").and_then(Value::as_str).unwrap_or_default();
        let (rel, pin) = match root {
            Some((rel, RootState::Ours(dev, ino))) => (Some(rel), Some((dev, ino))),
            Some((rel, RootState::Moved)) => {
                error!(
                    share_id = id,
                    share = name,
                    path = %core.layout.path(&rel).display(),
                    "the share's folder is not the one the edge made (renamed, replaced, or a \
                     symlink): not served until it is back"
                );
                done = false;
                continue;
            }
            _ => (None, None),
        };
        let Some(rel) = rel else {
            if row.get("root_dir_id").is_none_or(Value::is_null) {
                // A share made outside the portal with no root folder: the
                // edge can't place it (Replica::root_name), and retrying won't.
                warn!(share_id = id, "share has no root folder: not served");
                continue;
            }
            // The root isn't on disk yet: rendered once it is.
            done = false;
            continue;
        };
        let path = core.layout.path(&rel);
        let depth = row
            .pointer("/rewind/depth_days")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let snapdir =
            (depth > 0).then(|| core.layout.path(&Path::new(crate::rewind::SNAP).join(name)));
        match share_config(&row, path, snapdir, prevver) {
            Ok(mut r) => {
                r.root_pin = pin;
                shares.push(r)
            }
            Err(e) => error!(share_id = id, share = name, "share not served: {e}"),
        }
    }

    if cfg.root_acls && !root_acls(core, &shares).await {
        done = false;
    }

    let (smb, ganesha) = render_all(&shares);
    let (smb_p, gan_p) = (cfg.smb_conf.clone(), cfg.ganesha_conf.clone());
    let owed = restart_owed_path(&gan_p);
    let wrote = tokio::task::spawn_blocking(move || -> std::io::Result<bool> {
        // Ganesha takes an export's new Snap_Dir only at a restart
        // (FSAL_OMNI's update_export keeps the one the export was made
        // with). The restart owed is recorded before the file changes, so a
        // crash between the two still restarts it.
        let old = std::fs::read_to_string(&gan_p).unwrap_or_default();
        if snap_dirs_changed(&old, &ganesha) {
            std::fs::write(&owed, b"")?;
        }
        let a = write_if_changed(&smb_p, &smb)?;
        let b = write_if_changed(&gan_p, &ganesha)?;
        Ok(a || b)
    })
    .await;
    let changed = match wrote {
        Ok(Ok(c)) => c,
        Ok(Err(e)) => {
            error!(err = %e, "writing the share config failed");
            return false;
        }
        Err(_) => return false,
    };
    if changed
        || core
            .shares_reload_pending
            .load(std::sync::atomic::Ordering::SeqCst)
    {
        info!(
            shares = shares.len(),
            "share config changed: reloading the file servers"
        );
        if reload(&cfg.reload).await {
            core.shares_reload_pending
                .store(false, std::sync::atomic::Ordering::SeqCst);
        } else {
            core.shares_reload_pending
                .store(true, std::sync::atomic::Ordering::SeqCst);
            done = false;
        }
    }
    if restart_owed_path(&cfg.ganesha_conf).exists() {
        info!(
            "an NFS export's rewind area changed: restarting Ganesha (clients ride its grace period)"
        );
        if reload(&cfg.restart).await {
            let _ = std::fs::remove_file(restart_owed_path(&cfg.ganesha_conf));
        } else {
            done = false;
        }
    }
    done
}

fn restart_owed_path(ganesha_conf: &Path) -> PathBuf {
    ganesha_conf.with_extension("restart-owed")
}

/// Each export's `Snap_Dir` in a rendered Ganesha file, by `Export_Id`.
fn snap_dirs(text: &str) -> BTreeMap<u16, Option<String>> {
    let mut out = BTreeMap::new();
    let mut cur: Option<u16> = None;
    for line in text.lines().map(str::trim) {
        if let Some(v) = line.strip_prefix("Export_Id = ") {
            cur = v.trim_end_matches(';').trim().parse().ok();
            if let Some(id) = cur {
                out.insert(id, None);
            }
        } else if let (Some(id), Some(i)) = (cur, line.find("Snap_Dir = \"")) {
            let rest = &line[i + "Snap_Dir = \"".len()..];
            if let Some(end) = rest.find('"') {
                out.insert(id, Some(rest[..end].to_owned()));
            }
        }
    }
    out
}

/// Does an export in both files have another `Snap_Dir` (or gain or lose
/// one)? Added and removed exports reload without a restart.
fn snap_dirs_changed(old: &str, new: &str) -> bool {
    let (a, b) = (snap_dirs(old), snap_dirs(new));
    a.iter().any(|(id, s)| b.get(id).is_some_and(|t| t != s))
}

async fn reload(argv: &[String]) -> bool {
    run_command(argv, "reloading the file servers").await
}

/// Runs `argv` (nothing for an empty one), logging a failure as `what` failed.
pub(crate) async fn run_command(argv: &[String], what: &str) -> bool {
    let Some((cmd, args)) = argv.split_first() else {
        return true;
    };
    let (cmd, args) = (cmd.clone(), args.to_vec());
    let out = tokio::task::spawn_blocking(move || {
        std::process::Command::new(&cmd)
            .args(&args)
            .output()
            .map_err(|e| (cmd, e))
    })
    .await;
    let Ok(out) = out else { return false };
    match out {
        Ok(o) if o.status.success() => true,
        Ok(o) => {
            warn!(
                status = %o.status,
                stderr = %String::from_utf8_lossy(&o.stderr).trim(),
                "{what} failed"
            );
            false
        }
        Err((cmd, e)) => {
            warn!(err = %e, cmd = %cmd, "{what} failed");
            false
        }
    }
}

/// A share root and its groups: (share id, root, its pinned inode, Owners,
/// Writers, Readers).
type RootWanted = (u64, PathBuf, (u64, u64), Sid, Option<Sid>, Option<Sid>);

/// Gives each share root its groups' ACL when the groups it last had differ.
async fn root_acls<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    shares: &[Rendered],
) -> bool {
    let Some(identity) = core.identity.get() else {
        return false;
    };
    let idmap = identity.idmap.clone();
    let wanted: Vec<RootWanted> = shares
        .iter()
        .filter_map(|r| {
            Some((
                r.share_id,
                r.config.path.clone(),
                r.root_pin?,
                r.owners.clone(),
                r.writers.clone(),
                r.readers.clone(),
            ))
        })
        .collect();
    let state = applied_path(&core.cfg);
    let res = tokio::task::spawn_blocking(move || {
        let mut applied = load_applied(&state);
        let mut done = true;
        let mut touched = Vec::new();
        for (id, path, pin, owners, writers, readers) in wanted {
            let gid = |s: &Sid| idmap.sid_to_gid(s).ok().flatten();
            let Some(o) = gid(&owners) else {
                error!(share_id = id, sid = %owners, "the Owners group has no gid: root ACL waits");
                done = false;
                continue;
            };
            // A Writers or Readers group winbind can't map yet is left out
            // and retried, but never holds up the rest: a group it replaces
            // loses its entries now (fewer rights, never more).
            let mut opt = |s: &Option<Sid>| {
                let sid = s.as_ref()?;
                let g = gid(sid);
                if g.is_none() {
                    warn!(share_id = id, sid = %sid, "a share group has no gid yet: left out, retried");
                    done = false;
                }
                g
            };
            let want = Applied {
                owners: o,
                writers: opt(&writers),
                readers: opt(&readers),
            };
            let before = applied.get(&id).copied();
            // Trust the record only while the root still has what it says:
            // a root made again, or one whose group was changed back, is
            // given its ACL again.
            let on_disk = match omnifile_acl::AclFs::read(&omnifile_acl::SysFs, &path) {
                Ok(a) if (a.dev, a.ino) != pin => {
                    warn!(share_id = id, path = %path.display(), "share root moved: ACL waits");
                    done = false;
                    continue;
                }
                Ok(a) => {
                    let d = omnifile_acl::check_root(&a, &want.into());
                    a.gid == o && d.missing.is_empty() && d.default_missing.is_empty() && !d.no_default
                }
                Err(e) => {
                    warn!(share_id = id, path = %path.display(), err = %e, "share root unreadable");
                    done = false;
                    continue;
                }
            };
            if before == Some(want) && on_disk {
                continue;
            }
            match omnifile_acl::apply_root_at(
                &omnifile_acl::SysFs,
                &path,
                pin,
                &want.into(),
                before.map(ShareGroups::from).as_ref(),
            ) {
                Ok(_) => {
                    info!(share_id = id, path = %path.display(), "share root ACL applied");
                    applied.insert(id, want);
                    touched.push(path);
                }
                Err(e) => {
                    error!(share_id = id, path = %path.display(), err = %e, "share root ACL failed");
                    done = false;
                }
            }
        }
        if !touched.is_empty()
            && let Ok(json) = serde_json::to_vec(&applied)
            && let Err(e) = write_if_changed(&state, &String::from_utf8_lossy(&json))
        {
            error!(err = %e, "recording the share root ACLs failed");
        }
        (done, touched)
    })
    .await;
    let Ok((done, touched)) = res else {
        return false;
    };
    if !touched.is_empty() {
        // Report the new ACLs in the next on-change scan, and give the
        // rewind trees the live roots' ACL.
        core.acl_dirty.lock().unwrap().extend(touched);
        core.rewind_acls_stale
            .store(true, std::sync::atomic::Ordering::SeqCst);
    }
    done
}

/// The loop: a pass at start, then whenever the shares change, with a retry
/// while something is left to do.
pub async fn shares_loop<C: ControlPlane, B: BlobIo>(core: Arc<Core<C, B>>) {
    let mut stop = core.shutdown.subscribe();
    // A reload owed when the last daemon stopped (it wrote the files, then
    // crashed, or the reload failed) is invisible now that the files match:
    // reload once at start. The servers outlive the daemon, so it's cheap.
    core.shares_reload_pending
        .store(true, std::sync::atomic::Ordering::SeqCst);
    let mut last: Option<Vec<(u64, u64, RootState)>> = None;
    let mut retry_at: Option<tokio::time::Instant> = None;
    loop {
        // Each share's version, and where its root stands: a root that
        // appears is rendered at once, and one renamed away or replaced stops
        // being served within a tick, not at the next retry.
        let roots: Vec<(u64, u64, Option<RootAt>)> = {
            let rep = core.replica.lock().await;
            rep.share_rows()
                .unwrap_or_default()
                .into_iter()
                .map(|(id, ev, body)| {
                    let root = serde_json::from_str::<Value>(&body)
                        .ok()
                        .and_then(|r| r.get("root_dir_id")?.as_str()?.parse::<u64>().ok())
                        .and_then(|r| root_of(&rep, r));
                    (id, ev, root)
                })
                .collect()
        };
        let data_root = core.layout.data_root.clone();
        let now: Vec<(u64, u64, RootState)> = tokio::task::spawn_blocking(move || {
            roots
                .into_iter()
                .map(|(id, ev, root)| {
                    let st = root.map_or(RootState::Missing, |(rel, made)| {
                        root_state(&data_root, &rel, made)
                    });
                    (id, ev, st)
                })
                .collect()
        })
        .await
        .unwrap_or_default();
        let due = last.as_ref() != Some(&now)
            || retry_at.is_some_and(|t| tokio::time::Instant::now() >= t);
        if due {
            let ok = sync_once(&core).await;
            last = Some(now);
            retry_at = (!ok).then(|| tokio::time::Instant::now() + RETRY);
        }
        tokio::select! {
            _ = tokio::time::sleep(EVERY) => {}
            _ = stop.changed() => return,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn row() -> Value {
        json!({
            "share_id": "7", "dataset_id": "2", "name": "seismic", "root_dir_id": "100",
            "protocols": ["nfs4", "smb"], "nfs_sec": ["krb5p"], "nfs_clients": [],
            "root_squash": true, "trust_level": "proven", "host_trust_override": false,
            "nfs_export_id": 3, "smb_encrypt": true,
            "owners_group_sid": "S-1-5-21-1-2-3-1100",
            "readers_group_sid": "S-1-5-21-1-2-3-1102",
            "on_open": "auto", "wait_threshold_minutes": 10,
            "rewind": {"depth_days": 7, "max_days": 30, "labels": {}},
            "worm": {"enabled": false}, "entity_version": "4"
        })
    }

    #[test]
    fn a_catalog_row_becomes_the_share_the_edge_serves() {
        let r = share_config(
            &row(),
            "/srv/omnifile/data/seismic".into(),
            Some("/srv/omnifile/data/.omni/snap/seismic".into()),
            true,
        )
        .unwrap();
        assert_eq!(r.share_id, 7);
        let c = &r.config;
        assert_eq!(c.export_id, 3);
        assert_eq!(c.fsal_policy, FsalPolicy::WaitQuick { ms: 600_000 });
        assert!(c.smb_encrypt && c.smb_prevver);
        let (smb, ganesha) = render_all(std::slice::from_ref(&r));
        assert!(
            smb.contains("[seismic]\n    path = /srv/omnifile/data/seismic"),
            "{smb}"
        );
        assert!(smb.contains("smb encrypt = required"));
        assert!(smb.contains("valid users = S-1-5-21-1-2-3-1100 S-1-5-21-1-2-3-1102"));
        assert!(smb.contains("shadow:snapdir = /srv/omnifile/data/.omni/snap/seismic"));
        assert!(ganesha.contains("Export_Id = 3;"), "{ganesha}");
        assert!(ganesha.contains("Pseudo = \"/seismic\";"));
        assert!(ganesha.contains("SecType = krb5p;"));
        assert!(ganesha.contains("waitquick:600000"), "{ganesha}");
    }

    #[test]
    fn what_the_edge_refuses_to_serve() {
        // No export id: a service from before M6.
        let mut r = row();
        r.as_object_mut().unwrap().remove("nfs_export_id");
        assert!(
            share_config(&r, "/d/s".into(), None, false)
                .unwrap_err()
                .contains("nfs_export_id")
        );
        // sec=sys with a wildcard (the service refuses it too).
        let mut r = row();
        r["nfs_sec"] = json!(["sys"]);
        r["trust_level"] = json!("host_trusted");
        r["nfs_clients"] = json!(["*"]);
        let e = share_config(&r, "/d/s".into(), None, false).unwrap_err();
        assert!(e.contains("not allowed"), "{e}");
        // A trust level that contradicts the settings.
        let mut r = row();
        r["nfs_sec"] = json!(["sys"]);
        r["nfs_clients"] = json!(["10.1.2.0/24"]);
        assert!(
            share_config(&r, "/d/s".into(), None, false)
                .unwrap_err()
                .contains("trust level")
        );
        // A path Samba and Ganesha can't carry.
        assert!(share_config(&row(), "/d/a\"b".into(), None, false).is_err());
    }

    #[test]
    fn no_rewind_area_no_prevver_and_smb_only_shares_have_no_export() {
        let mut r = row();
        r["protocols"] = json!(["smb"]);
        r["nfs_sec"] = json!([]);
        let s = share_config(&r, "/d/s".into(), None, true).unwrap();
        assert!(!s.config.smb_prevver, "prevver only with a rewind area");
        let (smb, ganesha) = render_all(&[s]);
        assert!(smb.contains("[seismic]") && !smb.contains("shadow_copy2"));
        assert!(!ganesha.contains("EXPORT"));
    }

    #[test]
    fn a_new_rewind_area_on_an_existing_export_needs_a_restart() {
        let with = |snap: Option<&str>| {
            let mut r = row();
            r["rewind"]["depth_days"] = json!(if snap.is_some() { 7 } else { 0 });
            let s = share_config(&r, "/d/seismic".into(), snap.map(PathBuf::from), false).unwrap();
            render_all(&[s]).1
        };
        let (none, some) = (with(None), with(Some("/d/.omni/snap/seismic")));
        assert!(snap_dirs_changed(&none, &some), "0 days -> N days");
        assert!(snap_dirs_changed(&some, &none), "N days -> 0 days");
        assert!(!snap_dirs_changed(&some, &some));
        // An export added or removed reloads; it needs no restart.
        assert!(!snap_dirs_changed("", &some) && !snap_dirs_changed(&some, ""));
        assert_eq!(
            snap_dirs(&some).get(&3),
            Some(&Some("/d/.omni/snap/seismic".to_owned()))
        );
    }

    #[test]
    fn names_that_differ_only_in_case_serve_the_older_share() {
        let mk = |id: &str, name: &str| {
            let mut r = row();
            r["share_id"] = json!(id);
            r["name"] = json!(name);
            share_config(&r, format!("/d/{name}").into(), None, false).unwrap()
        };
        let (smb, _) = render_all(&[mk("9", "data"), mk("4", "Data"), mk("5", "other")]);
        assert!(smb.contains("[Data]") && smb.contains("[other]"), "{smb}");
        assert!(!smb.contains("[data]"), "{smb}");
    }

    #[test]
    fn files_are_replaced_only_when_they_change() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("sub/smb.conf");
        assert!(write_if_changed(&p, "a").unwrap());
        assert!(!write_if_changed(&p, "a").unwrap());
        assert!(write_if_changed(&p, "b").unwrap());
        assert_eq!(std::fs::read_to_string(&p).unwrap(), "b");
        assert!(!p.with_extension("tmp").exists());
    }
}
