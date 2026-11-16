//! The smb-guard and AD identity, wired into the daemon (M2 integration).
//!
//! Samba reads the xfs volume directly, so SMB (and local) reads of a stub
//! are caught by the fanotify guard in `omnifile-smbguard`, not by
//! `FSAL_OMNI`. The guard decides from the marker whether bytes are local;
//! everything else (start or join the recall, wait or fail per the share's
//! `on_open`) is decided here, from the same recall table the FSAL path uses
//! ([`Core::recalls`]), so an NFS reader and an SMB reader of one file share
//! one recall and one answer.
//!
//! Start-up order (`main`): [`start`] runs first, before the Tokio runtime
//! or any other thread exists, because the guard takes the systemd-passed
//! descriptors and reserves the descriptor numbers of its predecessor's
//! unanswered events. Until the daemon's [`Core`] exists, the
//! [`LateBroker`] holds every event (`Wait`): a read that arrives during
//! start-up waits, it never reads holes.

use std::collections::HashMap;
use std::net::IpAddr;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use omnifile_fsal_proto::wire::{Decision, ObjRef, ReadDecision, RecallPhase};
use omnifile_identity::{
    CachedIdMap, IdMap, IdentityResolver, Proof, ResolvedIdentity, Resolver, ResolverConfig,
    SmbRequest, Via, Wbinfo,
};
use omnifile_marker::{Flags, Marker};
use omnifile_smbguard::linux::marks::{DirMarks, IgnoreMarks};
use omnifile_smbguard::{
    AccessRequest, Answer, DirRequest, EventIdentity, EventOrigin, FillMount, GuardConfig,
    HugePolicy, RecallBroker, TruncWatch, Waker,
};
use omnifile_stub::{InodeKey, SmbGuardHook};
use tracing::{debug, info, warn};

use crate::config::{Config, SmbGuardConfig};
use crate::core::{Core, RecallEntry};
use crate::ports::{BlobIo, ControlPlane, RecallRequest};

/// Winbind-backed identity, shared by the guard (SMB sessions), the recall
/// path (NFS uid → SID) and the ACL code (catalog SIDs → ids).
#[derive(Clone)]
pub struct EdgeIdentity {
    pub resolver: Arc<dyn IdentityResolver>,
    pub idmap: Arc<dyn IdMap>,
}

impl EdgeIdentity {
    /// `wbinfo` and `smbstatus` as separate processes (GPL never linked).
    pub fn system() -> EdgeIdentity {
        let rc = ResolverConfig::default();
        EdgeIdentity {
            idmap: Arc::new(CachedIdMap::new(
                Wbinfo::default(),
                rc.idmap_ttl,
                rc.idmap_negative_ttl,
            )),
            resolver: Arc::new(Resolver::system(rc)),
        }
    }
}

/// The guard's [`EventIdentity`]: the SMB session behind the event's pid
/// (or the local process), resolved while the event is pending.
///
/// All SMB attribution, including the blocked smbd thread's fsuid matched to
/// the connection's sessions, lives in `omnifile_identity::Resolver` (M2
/// review S5): it records how each identity was decided
/// (`ResolvedIdentity::method`) and marks it `claimed` unless a session read
/// during the lookup, or the thread and a session together, decided it. The
/// thread scan needs `CAP_SYS_PTRACE` (wchan of another process); without it
/// the resolver logs an error once and degrades to `claimed`/unknown.
pub struct GuardIdentity(pub EdgeIdentity);

impl EventIdentity for GuardIdentity {
    type Identity = ResolvedIdentity;
    fn resolve(&self, o: &EventOrigin<'_>) -> ResolvedIdentity {
        self.0.resolver.resolve_smb(&SmbRequest {
            pid: o.pid,
            pidfd: o.pidfd,
        })
    }
}

/// What the daemon hands the guard once its core exists.
pub trait GuardBackend: Send + Sync + 'static {
    fn on_access(&self, req: &AccessRequest<'_, ResolvedIdentity>) -> Answer;
    fn on_dir_open(&self, req: &DirRequest<'_, ResolvedIdentity>) -> Answer;
    fn quiesce_fill(&self, key: InodeKey, old: &Marker);
    fn stub_overwritten(&self, key: InodeKey, old: &Marker, discarded: u64);
    fn partial_modified(&self, key: InodeKey, m: &Marker);
    fn may_act_on_size_mismatch(&self, key: InodeKey, m: &Marker) -> bool;
    fn stub_extended(&self, key: InodeKey, m: &Marker, st_size: u64) -> Answer;
}

/// The guard's [`RecallBroker`], bound to the daemon after start-up.
#[derive(Clone, Default)]
pub struct LateBroker(Arc<OnceLock<Arc<dyn GuardBackend>>>);

impl LateBroker {
    pub fn bind(&self, b: Arc<dyn GuardBackend>) {
        let _ = self.0.set(b);
    }
}

impl RecallBroker<ResolvedIdentity> for LateBroker {
    fn on_access(&self, req: &AccessRequest<'_, ResolvedIdentity>) -> Answer {
        match self.0.get() {
            Some(b) => b.on_access(req),
            // Not started yet: hold the event; the guard asks again.
            None => Answer::Wait,
        }
    }

    fn on_dir_open(&self, req: &DirRequest<'_, ResolvedIdentity>) -> Answer {
        match self.0.get() {
            Some(b) => b.on_dir_open(req),
            // Not started yet: hold it (never list an unfilled folder).
            None => Answer::Wait,
        }
    }

    fn quiesce_fill(&self, key: InodeKey, old: &Marker) {
        if let Some(b) = self.0.get() {
            b.quiesce_fill(key, old)
        }
    }

    fn stub_overwritten(&self, key: InodeKey, old: &Marker, discarded: u64) {
        match self.0.get() {
            Some(b) => b.stub_overwritten(key, old, discarded),
            None => warn!(
                ino = key.ino,
                file_id = old.file_id,
                discarded,
                "stub overwritten before the daemon was ready"
            ),
        }
    }

    fn partial_modified(&self, key: InodeKey, m: &Marker) {
        match self.0.get() {
            Some(b) => b.partial_modified(key, m),
            None => warn!(
                ino = key.ino,
                file_id = m.file_id,
                "partial file written before the daemon was ready"
            ),
        }
    }

    fn may_act_on_size_mismatch(&self, key: InodeKey, m: &Marker) -> bool {
        // Not started: a redo may still have to finish a content change.
        self.0
            .get()
            .is_some_and(|b| b.may_act_on_size_mismatch(key, m))
    }

    fn stub_extended(&self, key: InodeKey, m: &Marker, st_size: u64) -> Answer {
        match self.0.get() {
            Some(b) => b.stub_extended(key, m, st_size),
            None => Answer::Wait,
        }
    }
}

/// A share's open policy, as the guard needs it (from memory).
#[derive(Clone, Debug, PartialEq, Eq)]
struct ShareRule {
    on_open: String,
}

/// What [`Core`] keeps of the guard.
pub struct GuardHooks {
    pub marks: Arc<IgnoreMarks>,
    pub waker: Waker,
    /// Open-permission marks on unfilled rewind directories.
    pub dirs: Arc<DirMarks>,
    rt: tokio::runtime::Handle,
    shares: Mutex<HashMap<u64, (ShareRule, Instant)>>,
    /// Rewind directories an SMB open is waiting on, by inode.
    dir_fills: Mutex<HashMap<u64, DirFill>>,
}

/// A rewind directory being filled for an open the guard holds.
#[derive(Clone, Copy, Debug)]
enum DirFill {
    Running,
    /// The fill failed: the held opens are refused once, then a later
    /// open tries again.
    Failed,
}

impl GuardHooks {
    pub fn new(
        marks: Arc<IgnoreMarks>,
        waker: Waker,
        dirs: Arc<DirMarks>,
        rt: tokio::runtime::Handle,
    ) -> GuardHooks {
        GuardHooks {
            marks,
            waker,
            dirs,
            rt,
            shares: Mutex::new(HashMap::new()),
            dir_fills: Mutex::new(HashMap::new()),
        }
    }
}

/// How long a share's `on_open` is reused before the replica is asked again.
const SHARE_TTL: Duration = Duration::from_secs(30);

/// The stub handshake's ignore-mark hook: the guard's marks, or none.
pub enum StubGuard<'a> {
    None,
    Marks(&'a IgnoreMarks),
}

impl SmbGuardHook for StubGuard<'_> {
    fn before_narrow(&self, key: InodeKey) -> std::io::Result<()> {
        match self {
            StubGuard::None => Ok(()),
            StubGuard::Marks(m) => m.before_narrow(key),
        }
    }
}

/// The guard configuration from `[smbguard]`.
pub fn guard_config(cfg: &Config) -> Result<GuardConfig, String> {
    let g: &SmbGuardConfig = &cfg.smbguard;
    let uuid = omnifile_smbguard::api::parse_uuid(&g.fs_uuid).ok_or_else(|| {
        format!(
            "smbguard.fs_uuid {:?} is not a UUID (record the data volume's: blkid -s UUID -o value <device>)",
            g.fs_uuid
        )
    })?;
    if cfg.fill_root() != g.fill_mount {
        return Err(format!(
            "fill_root ({}) must be the guard's fill mount ({}), or fills raise guard events",
            cfg.fill_root().display(),
            g.fill_mount.display()
        ));
    }
    let mut gc = GuardConfig::new(
        &cfg.data_root,
        uuid,
        FillMount::Existing(g.fill_mount.clone()),
    );
    gc.journal_path = g.journal_path.clone();
    gc.workers = g.workers.max(1);
    gc.poll_interval = Duration::from_millis(g.poll_ms.max(50));
    gc.limits.max_wait_bytes = g.max_wait_mib << 20;
    gc.huge_policy = HugePolicy::Deny;
    gc.report_pidfd = g.report_pidfd;
    gc.trunc_watch = match g.trunc_watch.as_str() {
        "off" => TruncWatch::Off,
        "modify" => TruncWatch::Modify,
        _ => TruncWatch::CloseWrite,
    };
    // READY=1 once the guard is armed: smbd and Ganesha start after it
    // (their drop-ins), and both are guarded from their first open.
    gc.notify_ready = true;
    Ok(gc)
}

/// What [`start`] returns: the guard's handle and the broker to bind.
pub struct Started {
    pub handle: omnifile_smbguard::GuardHandle,
    pub broker: LateBroker,
}

/// Start the guard. Call first in `main`, before any thread exists.
pub fn start(cfg: &Config, identity: &EdgeIdentity) -> Result<Started, String> {
    let gc = guard_config(cfg)?;
    let broker = LateBroker::default();
    let handle = omnifile_smbguard::start(gc, broker.clone(), GuardIdentity(identity.clone()))
        .map_err(|e| e.to_string())?;
    info!(origin = ?handle.origin, group = %handle.group_id, "smb-guard armed");
    Ok(Started { handle, broker })
}

/// The daemon side of the guard, over [`Core`].
pub struct CoreBackend<C, B>(pub Arc<Core<C, B>>);

fn answer_of(d: Decision) -> Answer {
    match d {
        Decision::Delay | Decision::Park => Answer::Wait,
        Decision::Recheck => Answer::Recheck,
        _ => Answer::Deny,
    }
}

impl<C: ControlPlane, B: BlobIo> CoreBackend<C, B> {
    fn share_rule(&self, hooks: &GuardHooks, share_id: u64) -> ShareRule {
        if let Some((r, at)) = hooks.shares.lock().unwrap().get(&share_id)
            && at.elapsed() < SHARE_TTL
        {
            return r.clone();
        }
        // A guard worker thread, outside the runtime: a short blocking wait
        // for the replica is fine (once per share per SHARE_TTL).
        let rule = match self.0.replica.blocking_lock().share(share_id) {
            Ok(Some(s)) => ShareRule { on_open: s.on_open },
            // Unknown share: the strict answer.
            _ => ShareRule {
                on_open: "fail".into(),
            },
        };
        hooks
            .shares
            .lock()
            .unwrap()
            .insert(share_id, (rule.clone(), Instant::now()));
        rule
    }
}

fn via_word(v: Via) -> &'static str {
    match v {
        Via::Nfs => "nfs",
        Via::Smb => "smb",
        Via::Local => "local",
    }
}

fn sec_word(id: &ResolvedIdentity) -> Option<String> {
    id.sec.and_then(|s| {
        serde_json::to_value(s)
            .ok()
            .and_then(|v| v.as_str().map(str::to_owned))
    })
}

impl<C: ControlPlane, B: BlobIo> GuardBackend for CoreBackend<C, B> {
    fn on_access(&self, req: &AccessRequest<'_, ResolvedIdentity>) -> Answer {
        let core = &self.0;
        let Some(hooks) = core.guard.get() else {
            return Answer::Wait;
        };
        let m = &req.marker;
        if m.file_id == 0 {
            return Answer::Deny;
        }
        if core.is_shutting_down() {
            // The group outlives us: the next instance answers it.
            return Answer::Wait;
        }
        let key = crate::core::recall_key(m.file_id, m.version_id, m.flags.bits(), req.key.ino);
        let online = m.flags.contains(Flags::ONLINE);
        let rule = self.share_rule(hooks, m.share_id);
        // Archived data follows the share; `auto` waits until the service
        // has decided against the ETA (the wake re-checks at once).
        let policy_answer = match rule.on_open.as_str() {
            "wait" | "auto" => Decision::Delay,
            _ => Decision::Eio,
        };
        let id = req.identity;
        let addr: Option<IpAddr> = id.client_addr.as_deref().and_then(|a| a.parse().ok());
        let requester = (id.uid.unwrap_or(u32::MAX), addr);
        // M5: an ONLINE range of the live file is fetched chunk by chunk
        // into the placeholder (`remote`); the guard re-reads the marker and
        // its map on every wake and allows once they cover the range.
        if online && !req.whole_file && key.2 == 0 && core.remote.get().is_some() {
            let obj = ObjRef {
                fsid: req.key.fsid,
                ino: req.key.ino,
                ino_gen: req.key.ino_gen,
                export_id: 0,
                file_id: m.file_id,
            };
            if !core.recalls.lock().unwrap().contains_key(&key) {
                // As for a recall below: who first read the non-local bytes.
                info!(
                    file_id = m.file_id,
                    version_id = m.version_id,
                    via = via_word(id.via),
                    uid = ?id.uid,
                    sid = ?id.sid,
                    user = ?id.username,
                    process = ?id.process,
                    identity_error = ?id.error,
                    start = req.start,
                    end = req.end,
                    online,
                    "smb-guard: read of non-local bytes"
                );
            }
            return match core.guard_remote_read(
                key,
                obj,
                m.version_id,
                m.resident_bytes,
                requester,
                req.start,
                req.end,
            ) {
                Ok(()) => Answer::Wait,
                Err(None) => Answer::Deny,
                Err(Some(d)) => answer_of(d),
            };
        }
        let (entry, new_entry, new_requester) = {
            let mut map = core.recalls.lock().unwrap();
            match map.get_mut(&key) {
                Some(e) => {
                    let added = e.requesters.insert(requester);
                    (e.clone(), false, added)
                }
                None => {
                    let e = RecallEntry {
                        obj: ObjRef {
                            fsid: req.key.fsid,
                            ino: req.key.ino,
                            ino_gen: req.key.ino_gen,
                            export_id: 0,
                            file_id: m.file_id,
                        },
                        version_id: m.version_id,
                        recall_id: 0,
                        phase: RecallPhase::Queued,
                        answer: if online {
                            Decision::Delay
                        } else {
                            policy_answer
                        },
                        eta_s: ReadDecision::ETA_UNKNOWN,
                        resident: m.resident_bytes,
                        online,
                        requesters: std::collections::HashSet::from([requester]),
                    };
                    map.insert(key, e.clone());
                    (e, true, true)
                }
            }
        };
        let mut rr = RecallRequest {
            file_id: m.file_id,
            version_id: m.version_id,
            offset: req.start,
            length: req.end.saturating_sub(req.start),
            uid: id.uid,
            sec: sec_word(id),
            proof: match id.proof {
                Proof::Proven => "proven".into(),
                Proof::Claimed => "claimed".into(),
            },
            client_addr: id.client_addr.clone(),
            on_open: rule.on_open.clone(),
            via: via_word(id.via).into(),
            sid: id.sid.as_ref().map(|s| s.as_str().to_owned()),
            pinned: key.2 != 0,
        };
        if rr.uid.is_none() && rr.sid.is_none() {
            // Nobody could be named (the process exited before the lookup,
            // say). The read still needs its bytes: report the requester as
            // unknown (uid 0xFFFFFFFF, as in the FSAL identity record; never
            // 0, which names root), claimed, rather than fail the read.
            rr.uid = Some(id.wire_uid());
            rr.proof = "claimed".into();
        }
        let audit_rr = rr.clone();
        if new_entry {
            info!(
                file_id = m.file_id,
                version_id = m.version_id,
                via = via_word(id.via),
                uid = ?id.uid,
                sid = ?id.sid,
                user = ?id.username,
                process = ?id.process,
                identity_error = ?id.error,
                start = req.start,
                end = req.end,
                online,
                "smb-guard: read of non-local bytes"
            );
            let c = core.clone();
            let blocked = policy_answer;
            hooks
                .rt
                .spawn(async move { c.start_recall_with(key, rr, blocked).await });
        } else if new_requester && entry.recall_id != 0 {
            let c = core.clone();
            hooks
                .rt
                .spawn(async move { c.report_requester_with(rr).await });
        }
        let answer = match entry.phase {
            RecallPhase::Done => Answer::Recheck,
            RecallPhase::Failed => Answer::Deny,
            RecallPhase::Cancelled => answer_of(policy_answer),
            _ => answer_of(entry.answer),
        };
        if answer == Answer::Deny {
            let why = match entry.phase {
                RecallPhase::Failed => "recall_failed",
                RecallPhase::Cancelled => "recall_cancelled",
                _ => "failfast",
            };
            core.audit_refused(&audit_rr, why);
        }
        answer
    }

    /// smbd opening an unfilled rewind directory (listing a previous
    /// version's folder): fill it as `POPULATE_DIR` would, and hold the open
    /// until it is filled (`fsal-namespace.md` section 7).
    fn on_dir_open(&self, req: &DirRequest<'_, ResolvedIdentity>) -> Answer {
        let core = &self.0;
        let Some(hooks) = core.guard.get() else {
            return Answer::Wait;
        };
        let ino = req.key.ino;
        {
            let mut fills = hooks.dir_fills.lock().unwrap();
            match fills.get(&ino) {
                Some(DirFill::Running) => return Answer::Wait,
                Some(DirFill::Failed) => {
                    fills.remove(&ino);
                    return Answer::Deny;
                }
                None => {}
            }
            fills.insert(ino, DirFill::Running);
        }
        let id = req.identity;
        info!(
            ino,
            dir_id = req.marker.file_id,
            via = via_word(id.via),
            uid = ?id.uid,
            user = ?id.username,
            process = ?id.process,
            path = ?req.path,
            "smb-guard: unfilled rewind directory opened; filling it"
        );
        let obj = ObjRef {
            fsid: core.fsid,
            ino,
            ino_gen: req.key.ino_gen,
            export_id: 0,
            file_id: req.marker.file_id,
        };
        let c = core.clone();
        hooks.rt.spawn(async move {
            // 1 done, 2 another fill of it runs (the guard re-checks the
            // marker on its poll), 3 failed.
            let r = c.populate_rewind(obj).await;
            if let Some(h) = c.guard.get() {
                let mut fills = h.dir_fills.lock().unwrap();
                if r == 3 {
                    fills.insert(ino, DirFill::Failed);
                } else {
                    fills.remove(&ino);
                }
                drop(fills);
                h.waker.wake(ino);
            }
        });
        Answer::Wait
    }

    fn quiesce_fill(&self, key: InodeKey, old: &Marker) {
        let t0 = Instant::now();
        while self.0.locks.is_locked(old.file_id) && t0.elapsed() < Duration::from_secs(120) {
            std::thread::sleep(Duration::from_millis(10));
        }
        info!(
            ino = key.ino,
            file_id = old.file_id,
            "fill quiesced after an O_TRUNC overwrite"
        );
    }

    fn partial_modified(&self, key: InodeKey, m: &Marker) {
        self.0.on_partial_modified(key, m)
    }

    fn may_act_on_size_mismatch(&self, key: InodeKey, m: &Marker) -> bool {
        self.0.may_act_on_size_mismatch(key, m)
    }

    fn stub_extended(&self, key: InodeKey, m: &Marker, st_size: u64) -> Answer {
        if self.0.on_stub_extended(key, m, st_size) {
            Answer::Wait
        } else {
            Answer::Deny
        }
    }

    fn stub_overwritten(&self, key: InodeKey, old: &Marker, discarded: u64) {
        warn!(
            ino = key.ino,
            file_id = old.file_id,
            discarded,
            "stub overwritten by an O_TRUNC open: its stub state is cleared"
        );
        let Some(hooks) = self.0.guard.get() else {
            return;
        };
        let core = self.0.clone();
        // The file is now the user's: hold feed updates for it until its
        // new version is committed (as for an NFS truncate, `fsal-socket.md` §6.1).
        hooks.rt.spawn(async move {
            let mut rep = core.replica.lock().await;
            if let Ok(Some(n)) = rep.node_by_ino(key.ino, key.ino_gen) {
                let _ = rep.set_local_dirty(n.lid, true);
            }
        });
    }
}

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// The stub handshake's ignore-mark hook.
    pub fn stub_guard(&self) -> StubGuard<'_> {
        match self.guard.get() {
            Some(g) => StubGuard::Marks(&g.marks),
            None => StubGuard::None,
        }
    }

    /// Fill in the requester's SID from its uid (winbind, cached), off the
    /// runtime's worker threads. Never fails: no SID is reported as none.
    pub(crate) async fn with_sid(&self, mut req: RecallRequest) -> RecallRequest {
        if req.sid.is_some() {
            return req;
        }
        let (Some(uid), Some(id)) = (req.uid.filter(|u| *u != u32::MAX), self.identity.get())
        else {
            return req; // unknown (0xFFFFFFFF) has no SID to look up
        };
        let idmap = id.idmap.clone();
        match tokio::task::spawn_blocking(move || idmap.uid_to_sid(uid)).await {
            Ok(Ok(Some(sid))) => req.sid = Some(sid.as_str().to_owned()),
            Ok(Ok(None)) => debug!(uid, "uid has no AD SID"),
            Ok(Err(e)) => debug!(uid, err = %e, "uid -> SID lookup failed"),
            Err(_) => {}
        }
        req
    }
}
