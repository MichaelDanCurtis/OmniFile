//! Shared daemon state and the FSAL-facing logic: answering `READ_INTENT`,
//! starting recalls and fetches, running fills, and pushing `RECALL_STATE`.
//!
//! The FSAL handler never waits on the network or the replica: it answers
//! from the in-memory recall table (or from the intent itself) and starts
//! the slow work in a background task. What it answers can only make the
//! FSAL stricter; bytes are served only once the marker covers them.

use std::collections::{HashMap, HashSet};
use std::net::IpAddr;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use omnifile_fsal_proto::wire::{
    Decision, ObjRef, Policy, ReadDecision, ReadIntent, RecallPhase, RecallState, feature, reason,
};
use omnifile_fsal_proto::{ConnInfo, Handler, Notification, Server};
use omnifile_replica::{Node, RecallRow, Replica};
use omnifile_stub::fill::{FillError, FillOptions, FillRequest, fill_tracked};
use omnifile_stub::{
    FetchTarget, FileLocks, FsalNotifier, InodeKey, Layout, MarkerSeq, RangeSource,
};
use tokio::sync::{Notify, watch};
use tracing::{debug, info, warn};

use crate::config::Config;
use crate::ports::{BlobIo, ControlPlane, Health, ReadUrl, RecallRequest, RecallResponse};

const MARKER_ONLINE: u16 = 1;
/// The marker of a rewind placeholder (`.omni/snap`, decision 30).
const MARKER_READONLY: u16 = 1 << 2;

/// The daemon's view of one recall or fetch, keyed by `(file_id, content version)`.
#[derive(Clone, Debug)]
pub struct RecallEntry {
    pub obj: ObjRef,
    pub version_id: u64,
    pub recall_id: u64,
    pub phase: RecallPhase,
    /// What the FSAL answers while this lasts.
    pub answer: Decision,
    pub eta_s: u32,
    pub resident: u64,
    pub online: bool,
    pub requesters: HashSet<(u32, Option<IpAddr>)>,
}

/// `(file_id, content version, placeholder)`: `placeholder` is 0 for the
/// live file and a rewind placeholder's inode otherwise (`fsal-namespace.md`
/// §7), so a read of an old version is never served by, or into, the live
/// file, and one version shown in two labels fills each placeholder.
pub type RecallKey = (u64, u64, u64);

/// Resumes of a recall fill that was only interrupted, within
/// [`FILL_RESUME_WINDOW`], before it is reported failed (5 s doubling to
/// 5 min: about 15 minutes in all). Counted per resume the timer makes, not
/// per failure, so readers whose opens restart the fill every few seconds
/// don't use the budget up in a minute.
const FILL_RESUMES: u32 = 8;
const FILL_RESUME_BASE: Duration = Duration::from_secs(5);
const FILL_RESUME_MAX: Duration = Duration::from_secs(300);
const FILL_RESUME_WINDOW: Duration = Duration::from_secs(30 * 60);

/// A recall fill's timer resumes (see [`FILL_RESUMES`]).
#[derive(Debug, Default)]
pub struct FillResumes {
    /// When each resume in the window was scheduled.
    times: std::collections::VecDeque<std::time::Instant>,
    /// A resume is scheduled and has not run yet.
    pending: bool,
}

impl FillResumes {
    /// Resumes scheduled within the window.
    pub fn in_window(&self) -> usize {
        self.times.len()
    }

    /// A resume is scheduled and has not run yet.
    pub fn pending(&self) -> bool {
        self.pending
    }
}
/// Tries of a fill's report while the service is away (1 s doubling to
/// 60 s: about 8 minutes).
const REPORT_TRIES: u32 = 12;
/// A fill found its file gone from the disk while the replica still has it: a
/// delete (or a rename) is on its way through the namespace sync. This long
/// before the replica is asked again and the fill is called failed.
const MOVED_ON_SETTLE: Duration = Duration::from_secs(2);

/// Whether a fill was only interrupted (worth resuming where it got to),
/// not failed for good. A full data volume (`ENOSPC`, or `EDQUOT` under a
/// quota) is often transient, and is resumed like any interruption, then
/// reported failed only once the resume window is used up (WP3's soak:
/// recalls failed for good during a short fill_disk fault). Nothing on the
/// edge frees space for it: the edge never stubs files on its own, and the
/// chunk cache evicts only within its own budget. Users deleting files, an
/// admin growing the volume, or the action the low-disk alert asks for is
/// what frees it.
pub(crate) fn fill_interrupted(e: &FillError) -> bool {
    match e {
        FillError::FenceBroken
        | FillError::Busy
        | FillError::Source(_)
        | FillError::ShortRead { .. } => true,
        FillError::Io(io) => out_of_space(io),
        _ => false,
    }
}

/// The volume (or the writer's quota) is full.
fn out_of_space(e: &std::io::Error) -> bool {
    matches!(e.raw_os_error(), Some(libc::ENOSPC | libc::EDQUOT))
}

/// The key a read of the inode `ino` with these marker flags is tracked by.
pub fn recall_key(file_id: u64, version_id: u64, marker_flags: u16, ino: u64) -> RecallKey {
    let placeholder = if marker_flags & MARKER_READONLY != 0 {
        ino
    } else {
        0
    };
    (file_id, version_id, placeholder)
}

/// Everything the daemon's tasks share.
pub struct Core<C, B> {
    pub cfg: Config,
    pub layout: Layout,
    pub fsid: u64,
    pub replica: tokio::sync::Mutex<Replica>,
    pub control: Arc<C>,
    pub blob: Arc<B>,
    pub server: OnceLock<Server<FsalBridge<C, B>>>,
    pub seq: Arc<MarkerSeq>,
    pub locks: Arc<FileLocks>,
    pub recalls: Mutex<HashMap<RecallKey, RecallEntry>>,
    pub shutdown: watch::Sender<bool>,
    pub wake_stubber: Notify,
    /// Fills running now, by `(file_id, content version)` (S3, S10).
    pub fills: FillTracker,
    /// Files the commit waits on to hold still, by `(fsid, ino, gen)`: the
    /// size and mtime last seen, and since when (`commit.rs`, growth check).
    pub growth: Mutex<HashMap<(u64, u64, u32), crate::commit::Growth>>,
    /// Each share's quiet period (decision 67), rebuilt from the replica by
    /// `daemon::quiet_loop`.
    pub quiet: Arc<crate::quiet::QuietPeriods>,
    /// Interrupted recall fills, by key: their timer resumes.
    pub fill_resumes: Mutex<HashMap<RecallKey, FillResumes>>,
    /// The commit pipeline's queue: files whose local bytes are no longer a
    /// committed version (written into while partial) are sent here once
    /// they are whole again.
    pub commit_tx: OnceLock<tokio::sync::mpsc::Sender<crate::ports::CloseWrite>>,
    /// The daemon's runtime, for work started from guard threads.
    pub rt: OnceLock<tokio::runtime::Handle>,
    /// The smb-guard's hooks, once it is armed (M2 integration).
    pub guard: OnceLock<crate::guard::GuardHooks>,
    /// AD identity through winbind: requester SIDs, catalog ACLs (M2).
    pub identity: OnceLock<crate::guard::EdgeIdentity>,
    /// Files written since the last on-change ACL scan.
    pub acl_dirty: Mutex<HashSet<std::path::PathBuf>>,
    /// Rewind directories being populated now, by inode.
    pub rewind_busy: Mutex<HashSet<u64>>,
    /// The Unix minute rewind labels were last refreshed.
    pub rewind_minute: std::sync::atomic::AtomicI64,
    /// Rewind placeholders being read, by `(file_id, version)`: where they
    /// are and the version they hold (they are not in the replica).
    pub rewind_targets: Mutex<HashMap<RecallKey, crate::rewind::Target>>,
    /// Where this daemon wrote each file's `.RECALLING.txt` (the file's path
    /// then, by file id): a deleted file has no path in the replica any
    /// more, and its note must still go when its recall ends.
    pub note_paths: Mutex<HashMap<u64, std::path::PathBuf>>,
    /// Populated rewind directories and when they were last used.
    pub rewind_used: Mutex<HashMap<std::path::PathBuf, std::time::Instant>>,
    /// An ACL changed since the rewind trees were brought up to date: their
    /// populated directories are emptied (they fill again with the ACLs of
    /// now) and the labels get the live share root's ACL again.
    pub rewind_acls_stale: std::sync::atomic::AtomicBool,
    /// The share config changed and the file servers haven't reloaded it
    /// yet (a reload failed): try again (`shares`).
    pub shares_reload_pending: std::sync::atomic::AtomicBool,
    /// The replica differs from the catalog (`feed_loop::reconcile_once`):
    /// the feed loop re-snapshots before its next page, never alongside
    /// one.
    pub resnapshot_wanted: std::sync::atomic::AtomicBool,
    /// Audit records not yet in the replica (`audit_loop` moves them).
    pub audit_pending: Mutex<Vec<serde_json::Value>>,
    /// When each `(file, requester, reason)` refusal was last recorded:
    /// clients retry a refused read again and again; one record per window.
    pub audit_seen: Mutex<HashMap<(u64, String, &'static str), std::time::Instant>>,
    /// Directories (by lid) whose entries changed on disk: the namespace
    /// sync reconciles them (`nsync`).
    pub ns_dirty: Mutex<std::collections::BTreeSet<i64>>,
    pub ns_wake: Notify,
    /// Events were lost: reconcile every directory.
    pub ns_sweep: std::sync::atomic::AtomicBool,
    /// Deleted folders whose delete waits for something moved out of them:
    /// how many passes it has waited (it backs off, then gives up on the part
    /// still alive).
    pub ns_defers: Mutex<std::collections::HashMap<i64, u32>>,
    /// Who made recent namespace changes (`control-api.md` §4.1 `actor`):
    /// hints from the FSAL's `NS_HINT` and the namespace watcher (`actor`).
    pub ns_hints: Mutex<crate::actor::Hints>,
    /// The watcher is resolving who made a batch of changes: passes wait.
    pub ns_hints_busy: std::sync::atomic::AtomicBool,
    /// New folders with no hint yet, waiting one pass for it (`nsync`).
    pub ns_fresh_waits: Mutex<HashSet<std::path::PathBuf>>,
    /// Connected FSALs by peer pid (`SO_PEERCRED`; its binary was checked at
    /// connect): Ganesha is known by its pid and binary, never by its name.
    pub fsal_pids: Mutex<HashMap<i32, usize>>,
    /// New hard links whose first name isn't in the catalog yet, by inode:
    /// how many passes they have waited before being split (decision 42).
    pub ns_link_waits: Mutex<HashMap<(u64, u32), u32>>,
    /// Files the namespace sweep handed to the commit pipeline as a close no
    /// one saw (WP9), by inode, and when: not handed again while there.
    pub sweep_handed: Mutex<HashMap<(u64, u32), std::time::Instant>>,
    /// Files the sweep found with no close seen but still being written (or
    /// open for write), by inode: how many passes they have waited.
    pub sweep_waits: Mutex<HashMap<(u64, u32), u32>>,
    /// The durable log of pending closes, while the detector runs (WP9).
    pub pending: OnceLock<Arc<crate::pending::PendingCloses>>,
    /// Outbox ops whose finish failed, by op id: how many times, and when
    /// to try again (WP9).
    pub outbox_failures: Mutex<HashMap<String, (u32, std::time::Instant)>>,
    /// One creator of edge folders at a time (commits and the namespace
    /// sync both register them).
    pub dir_create: tokio::sync::Mutex<()>,
    /// The signing helper's pending restart onto a new Roles Anywhere
    /// certificate: its retry backoff (decision 50, review on #32).
    pub helper_restart: Mutex<crate::helper_restart::HelperRestart>,
    /// M5: the SSD cache of `ONLINE` placeholders (chunk-range reads), when
    /// `[cache] enabled` (`remote`).
    pub remote: OnceLock<Arc<dyn crate::remote::RemoteFetch>>,
    /// Ranges waiting for the cache, per key (`remote`).
    pub fetches: crate::remote::FetchQueue,
    /// Why a key's edge-local fetch failed (the `READ_DECISION` reason).
    pub fetch_reasons: Mutex<HashMap<RecallKey, u8>>,
    /// Which Ganesha processes run now, so a fill's fence knows which FSALs
    /// to wait for (tests set it; unset, [`ganesha_pids`] scans `/proc`).
    pub fsal_probe: OnceLock<FsalProbe>,
    /// The last `/proc` scan and when it was made.
    ganesha_scan: Mutex<Option<(std::time::Instant, Option<HashSet<i32>>)>>,
    /// Fills whose fence is waiting for the FSALs, and since when.
    fence_waits: Mutex<HashMap<RecallKey, FenceWait>>,
}

/// The pids of the Ganesha processes running now, or `None` when that
/// can't be told.
pub type FsalProbe = Arc<dyn Fn() -> Option<HashSet<i32>> + Send + Sync>;

/// How long one `/proc` scan for Ganesha is reused.
const GANESHA_SCAN_TTL: Duration = Duration::from_millis(500);
/// How often a waiting fence looks again.
const FENCE_POLL: Duration = Duration::from_millis(50);
/// A fill's fence attempt this soon after its last one continues the same
/// wait (`run_fill`'s retries back off at most 1 s).
const FENCE_EPISODE_GAP: Duration = Duration::from_secs(5);

/// The pids of the processes whose `/proc/<pid>/exe` is `exe`: the Ganesha
/// processes whose FSALs this daemon expects. `None` (unknown) when `exe`
/// is not an absolute path, `/proc` can't be read, or any process's binary
/// can't be told for a reason other than its having exited: an unknown set
/// never counts as "no Ganesha".
pub fn ganesha_pids(exe: &str) -> Option<HashSet<i32>> {
    let want = std::path::Path::new(exe);
    if !want.is_absolute() {
        return None;
    }
    let mut out = HashSet::new();
    for e in std::fs::read_dir("/proc").ok()? {
        let e = e.ok()?;
        let Some(pid) = e.file_name().to_str().and_then(|n| n.parse::<i32>().ok()) else {
            continue;
        };
        match std::fs::read_link(e.path().join("exe")) {
            Ok(p) if p == want => {
                out.insert(pid);
            }
            Ok(_) => {}
            // Exited since the listing, or a kernel thread (no binary).
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return None,
        }
    }
    Some(out)
}

/// One audit record per refused file, requester and reason in this window.
pub const AUDIT_COALESCE: Duration = Duration::from_secs(600);
const AUDIT_SEEN_MAX: usize = 50_000;
/// Records held in memory between flushes; beyond it, refusals are not
/// recorded until the next flush (a flood of distinct refusals).
const AUDIT_PENDING_MAX: usize = 50_000;
const AUDIT_BATCH: usize = 5000;

/// What a fill that is asked to stop ends with.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Terminal {
    pub phase: RecallPhase,
    pub answer: Decision,
}

#[derive(Debug, Default)]
struct RunningFill {
    /// Set: the fill stops before its next write or widen.
    stop: Option<Terminal>,
}

/// The fills this daemon runs. While a fill runs, its key's FSAL state is
/// `FETCHING` (the truncate fence, `fsal-socket.md` §6.1) and only the fill
/// itself may end it: anything else that wants a terminal state asks the
/// fill to stop, and the fill pushes that state once it has stopped writing.
#[derive(Debug, Default)]
pub struct FillTracker {
    running: Mutex<HashMap<RecallKey, RunningFill>>,
    changed: Notify,
}

impl FillTracker {
    /// Register a fill; `false` if one already runs for `key`.
    pub(crate) fn start(&self, key: RecallKey) -> bool {
        let mut m = self.running.lock().unwrap();
        if m.contains_key(&key) {
            return false;
        }
        m.insert(key, RunningFill::default());
        true
    }

    /// Unregister and end with the terminal state a stop asked for, else
    /// `default`, calling `end` while the tracker is locked: a stop that
    /// arrives meanwhile either is seen here, or finds no fill and pushes
    /// its own state after ours (so a late CANCELLED is never lost).
    pub(crate) fn finish_with(
        &self,
        key: RecallKey,
        default: Terminal,
        end: impl FnOnce(Terminal),
    ) {
        {
            let mut m = self.running.lock().unwrap();
            let t = m.remove(&key).and_then(|f| f.stop).unwrap_or(default);
            end(t);
        }
        self.changed.notify_waiters();
    }

    /// Run `f` with the set of running keys locked (lock order: tracker,
    /// then the recall table).
    fn with_running<T>(&self, f: impl FnOnce(&HashMap<RecallKey, RunningFill>) -> T) -> T {
        f(&self.running.lock().unwrap())
    }

    /// Unregister; returns the terminal state asked for, if any.
    fn finish(&self, key: RecallKey) -> Option<Terminal> {
        let t = self
            .running
            .lock()
            .unwrap()
            .remove(&key)
            .and_then(|f| f.stop);
        self.changed.notify_waiters();
        t
    }

    pub fn is_running(&self, key: RecallKey) -> bool {
        self.running.lock().unwrap().contains_key(&key)
    }

    /// Ask a running fill to stop and end with `t`. `false`: none runs.
    fn stop(&self, key: RecallKey, t: Terminal) -> bool {
        match self.running.lock().unwrap().get_mut(&key) {
            Some(f) => {
                f.stop.get_or_insert(t);
                true
            }
            None => false,
        }
    }

    pub(crate) fn stopping(&self, key: RecallKey) -> bool {
        self.running
            .lock()
            .unwrap()
            .get(&key)
            .is_some_and(|f| f.stop.is_some())
    }

    pub fn count(&self) -> usize {
        self.running.lock().unwrap().len()
    }

    /// Ask every fill to stop, then wait up to `limit` for them to end.
    pub async fn stop_all(&self, t: Terminal, limit: Duration) -> bool {
        for f in self.running.lock().unwrap().values_mut() {
            f.stop.get_or_insert(t);
        }
        let deadline = tokio::time::Instant::now() + limit;
        loop {
            let wait = self.changed.notified();
            if self.count() == 0 {
                return true;
            }
            if tokio::time::timeout_at(deadline, wait).await.is_err() {
                return self.count() == 0;
            }
        }
    }
}

/// Ends a running fill on every path out of [`Core::run_fill`], a panic
/// included: unregisters it and, unless the fill reported its own end,
/// pushes `FAILED` so the FSAL's fence never outlives the fill.
struct FillRun<C: ControlPlane, B: BlobIo> {
    core: Arc<Core<C, B>>,
    key: RecallKey,
    ended: bool,
}

impl<C: ControlPlane, B: BlobIo> Drop for FillRun<C, B> {
    fn drop(&mut self) {
        if self.ended {
            self.core.fills.finish(self.key);
            return;
        }
        warn!(
            file_id = self.key.0,
            version_id = self.key.1,
            "fill ended abnormally (panicked or aborted)"
        );
        let (core, key) = (&self.core, self.key);
        core.fills.finish_with(
            key,
            Terminal {
                phase: RecallPhase::Failed,
                answer: Decision::Eio,
            },
            |t| core.end_with(key, t),
        );
    }
}

/// The FSAL handler: a thin shim over [`Core`].
pub struct FsalBridge<C, B> {
    pub core: Arc<Core<C, B>>,
}

/// Fill source over the Blob client.
pub struct BlobSource<B>(pub Arc<B>);

impl<B: BlobIo> RangeSource for BlobSource<B> {
    async fn get_range(&self, t: &FetchTarget, offset: u64, len: u64) -> std::io::Result<Vec<u8>> {
        self.0.get_range(&t.url, t.source, offset, len).await
    }
}

fn answer_for(
    state: &str,
    on_open: &str,
    eta_s: Option<u32>,
    wait_threshold_minutes: Option<u64>,
) -> Decision {
    match state {
        "failed" => Decision::Eio,
        _ => match on_open {
            "wait" => Decision::Delay,
            "auto" => match (eta_s, wait_threshold_minutes) {
                (Some(eta), Some(min)) if (eta as u64) < min * 60 => Decision::Delay,
                _ => Decision::Eio,
            },
            _ => Decision::Eio,
        },
    }
}

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    pub fn new(
        cfg: Config,
        fsid: u64,
        replica: Replica,
        control: Arc<C>,
        blob: Arc<B>,
    ) -> Arc<Self> {
        let mut layout = Layout::new(cfg.data_root.clone());
        layout.fill_root = cfg.fill_root().to_path_buf();
        let quiet = Arc::new(crate::quiet::QuietPeriods::new(&cfg.data_root, &cfg.commit));
        Arc::new(Core {
            layout,
            fsid,
            replica: tokio::sync::Mutex::new(replica),
            control,
            blob,
            server: OnceLock::new(),
            seq: Arc::new(MarkerSeq::new()),
            locks: Arc::new(FileLocks::new()),
            recalls: Mutex::new(HashMap::new()),
            shutdown: watch::channel(false).0,
            wake_stubber: Notify::new(),
            fills: FillTracker::default(),
            fill_resumes: Mutex::new(HashMap::new()),
            growth: Mutex::new(HashMap::new()),
            quiet,
            commit_tx: OnceLock::new(),
            rt: OnceLock::new(),
            guard: OnceLock::new(),
            identity: OnceLock::new(),
            acl_dirty: Mutex::new(HashSet::new()),
            rewind_busy: Mutex::new(HashSet::new()),
            rewind_minute: std::sync::atomic::AtomicI64::new(0),
            rewind_targets: Mutex::new(HashMap::new()),
            note_paths: Mutex::new(HashMap::new()),
            rewind_used: Mutex::new(HashMap::new()),
            rewind_acls_stale: std::sync::atomic::AtomicBool::new(false),
            shares_reload_pending: std::sync::atomic::AtomicBool::new(false),
            ns_dirty: Mutex::new(Default::default()),
            ns_wake: Notify::new(),
            ns_sweep: std::sync::atomic::AtomicBool::new(false),
            ns_defers: Mutex::new(Default::default()),
            ns_hints: Mutex::new(Default::default()),
            ns_hints_busy: std::sync::atomic::AtomicBool::new(false),
            ns_fresh_waits: Mutex::new(HashSet::new()),
            fsal_pids: Mutex::new(HashMap::new()),
            ns_link_waits: Mutex::new(HashMap::new()),
            sweep_handed: Mutex::new(HashMap::new()),
            sweep_waits: Mutex::new(HashMap::new()),
            pending: OnceLock::new(),
            outbox_failures: Mutex::new(HashMap::new()),
            dir_create: tokio::sync::Mutex::new(()),
            remote: OnceLock::new(),
            fetches: Default::default(),
            fetch_reasons: Mutex::new(HashMap::new()),
            resnapshot_wanted: std::sync::atomic::AtomicBool::new(false),
            audit_pending: Mutex::new(Vec::new()),
            audit_seen: Mutex::new(HashMap::new()),
            helper_restart: Mutex::new(Default::default()),
            fsal_probe: OnceLock::new(),
            ganesha_scan: Mutex::new(None),
            fence_waits: Mutex::new(HashMap::new()),
            cfg,
        })
    }

    pub fn is_shutting_down(&self) -> bool {
        *self.shutdown.borrow()
    }

    pub fn obj_for(&self, node: &Node) -> Option<ObjRef> {
        Some(ObjRef {
            fsid: self.fsid,
            ino: node.ino?,
            ino_gen: node.ino_gen.unwrap_or(0),
            export_id: 0,
            file_id: node.file_id.unwrap_or(0),
        })
    }

    fn recall_state_of(&self, e: &RecallEntry) -> RecallState {
        RecallState {
            obj: e.obj,
            version_id: e.version_id,
            recall_id: e.recall_id,
            state: e.phase,
            answer: e.answer,
            eta_s: e.eta_s,
            resident_bytes: e.resident,
            valid_ms: self.cfg.fsal.recall_valid_ms,
        }
    }

    /// Push the entry's state to every connected FSAL.
    pub fn push(&self, e: &RecallEntry) {
        if let Some(s) = self.server.get() {
            s.push_recall_state(self.recall_state_of(e));
        }
        // SMB and local readers held by the smb-guard re-check now.
        if let Some(g) = self.guard.get() {
            g.waker.wake(e.obj.ino);
        }
    }

    /// Update an entry (if present) and push it.
    pub(crate) fn update<F: FnOnce(&mut RecallEntry)>(
        &self,
        key: RecallKey,
        f: F,
    ) -> Option<RecallEntry> {
        let e = {
            let mut map = self.recalls.lock().unwrap();
            let e = map.get_mut(&key)?;
            f(e);
            e.clone()
        };
        self.push(&e);
        Some(e)
    }

    /// Mark failed (the FSAL answers `EIO` while it lasts), then forget it
    /// after a few seconds so a later read asks again. If a fill of `key`
    /// is running, it is asked to stop and ends with `FAILED` itself.
    fn fail(self: &Arc<Self>, key: RecallKey, why: &str) {
        warn!(
            file_id = key.0,
            version_id = key.1,
            why,
            "recall or fetch failed"
        );
        let t = Terminal {
            phase: RecallPhase::Failed,
            answer: Decision::Eio,
        };
        if self.fills.stop(key, t) {
            return;
        }
        self.end_with(key, t);
    }

    /// Push a terminal state for `key` (never while its fill runs) and
    /// forget it a little later.
    pub(crate) fn end_with(self: &Arc<Self>, key: RecallKey, t: Terminal) {
        self.update(key, |e| {
            e.phase = t.phase;
            e.answer = t.answer;
        });
        let after = if t.phase == RecallPhase::Failed {
            Duration::from_secs(5)
        } else {
            Duration::from_millis(self.cfg.fsal.recall_valid_ms as u64)
        };
        // Also reached from a `Drop` during a panic: only with a runtime.
        if tokio::runtime::Handle::try_current().is_ok() {
            self.forget_later(key, after);
        }
    }

    fn forget_later(self: &Arc<Self>, key: RecallKey, after: Duration) {
        let this = self.clone();
        tokio::spawn(async move {
            tokio::time::sleep(after).await;
            let mut map = this.recalls.lock().unwrap();
            if map.get(&key).is_some_and(|e| e.phase.is_terminal()) {
                map.remove(&key);
                this.rewind_targets.lock().unwrap().remove(&key);
            }
        });
    }

    /// Every non-terminal entry, for `RESYNC` and periodic refresh.
    pub fn active_states(&self) -> Vec<RecallState> {
        self.recalls
            .lock()
            .unwrap()
            .values()
            .filter(|e| !e.phase.is_terminal())
            .map(|e| self.recall_state_of(e))
            .collect()
    }

    /// Answer a `READ_INTENT` at once, starting background work if needed.
    pub fn on_read_intent(self: &Arc<Self>, conn: ConnInfo, ri: ReadIntent) -> ReadDecision {
        let fail = ri.policy.fail_closed();
        let file_id = ri.obj.file_id;
        if file_id == 0 {
            if fail == Decision::Eio {
                self.audit_refused(&self.recall_request(&ri, ""), "unknown_file");
            }
            return ReadDecision::simple(fail, reason::UNKNOWN_FILE);
        }
        if self.is_shutting_down() {
            if fail == Decision::Eio {
                self.audit_refused(&self.recall_request(&ri, ""), "shutting_down");
            }
            return ReadDecision::simple(fail, reason::NONE);
        }
        let key = recall_key(file_id, ri.version_id, ri.marker_flags, ri.obj.ino);
        // M5: an ONLINE range of the live file, for an FSAL that reads chunk
        // maps, is fetched by the edge chunk by chunk (fsal-socket.md §6.3):
        // no service recall, PARK, FETCHING after each widening (`remote`).
        if self.chunk_read(&conn, &ri, key) {
            return self.on_remote_read(&conn, &ri, key);
        }
        let online = ri.marker_flags & MARKER_ONLINE != 0;
        let requester = (ri.identity.uid, ri.identity.client_addr());
        let (entry, new_entry, new_requester) = {
            let mut map = self.recalls.lock().unwrap();
            match map.get_mut(&key) {
                Some(e) => {
                    let added = e.requesters.insert(requester);
                    (e.clone(), false, added)
                }
                None => {
                    let e = RecallEntry {
                        obj: ri.obj,
                        version_id: ri.version_id,
                        recall_id: 0,
                        phase: RecallPhase::Queued,
                        // ONLINE data is a fetch of seconds: wait for it, even
                        // on failfast shares. Archived data uses the policy.
                        answer: if online { Decision::Delay } else { fail },
                        eta_s: ReadDecision::ETA_UNKNOWN,
                        resident: ri.resident_bytes,
                        online,
                        requesters: HashSet::from([requester]),
                    };
                    map.insert(key, e.clone());
                    (e, true, true)
                }
            }
        };
        if new_entry {
            let this = self.clone();
            let r = ri.clone();
            tokio::spawn(async move { this.start_recall(r).await });
        } else if new_requester && entry.recall_id != 0 {
            // Every person waiting gets a card: report the new requester
            // (the service joins the existing recall).
            let this = self.clone();
            let r = ri.clone();
            tokio::spawn(async move { this.report_requester(r).await });
        }
        let reason_code = if new_entry {
            if online {
                reason::FETCH_IN_PROGRESS
            } else {
                reason::RECALL_STARTED
            }
        } else if online {
            reason::FETCH_IN_PROGRESS
        } else {
            reason::RECALL_IN_PROGRESS
        };
        let decision = match entry.phase {
            RecallPhase::Done => Decision::Recheck,
            RecallPhase::Failed => Decision::Eio,
            RecallPhase::Cancelled => fail,
            _ if online && conn.features & feature::PARK != 0 && !ri.op.needs_full_fill() => {
                Decision::Park
            }
            _ => entry.answer,
        };
        if decision == Decision::Eio {
            let why = match entry.phase {
                RecallPhase::Failed => "recall_failed",
                RecallPhase::Cancelled => "recall_cancelled",
                _ => "failfast",
            };
            self.audit_refused(&self.recall_request(&ri, ""), why);
        }
        ReadDecision {
            decision,
            reason: if entry.phase == RecallPhase::Failed {
                reason::RESTORE_FAILED
            } else {
                reason_code
            },
            eta_s: entry.eta_s,
            ttl_ms: 0,
            recall_id: entry.recall_id,
            resident_bytes: entry.resident,
        }
    }

    /// Record a read the edge refused (`control-api.md` §4.7): NFS through
    /// the FSAL or SMB through the guard. The service records the recalls
    /// it is asked for; only the edge knows the reads it answered with an
    /// error itself. One record per file, requester and reason per
    /// `AUDIT_COALESCE`, however often the client retries.
    pub fn audit_refused(&self, rr: &RecallRequest, reason: &'static str) {
        let who = rr
            .sid
            .clone()
            .or_else(|| rr.uid.map(|u| format!("uid:{u}")))
            .unwrap_or_default();
        let now = std::time::Instant::now();
        {
            let mut seen = self.audit_seen.lock().unwrap();
            if seen.len() > AUDIT_SEEN_MAX {
                seen.retain(|_, at| now.duration_since(*at) < AUDIT_COALESCE);
            }
            match seen.get(&(rr.file_id, who.clone(), reason)) {
                Some(at) if now.duration_since(*at) < AUDIT_COALESCE => return,
                _ => {
                    seen.insert((rr.file_id, who, reason), now);
                }
            }
        }
        let mut rec = serde_json::json!({
            "at": omnifile_control::util::format_time(std::time::SystemTime::now()),
            "action": "read.refused",
            "decision": "deny",
            "proof": rr.proof,
            "details": {
                "reason": reason,
                "via": rr.via,
                "version_id": rr.version_id.to_string(),
                "on_open": rr.on_open,
            },
        });
        if rr.file_id != 0 {
            rec["file_id"] = rr.file_id.to_string().into();
        }
        if let Some(sid) = &rr.sid {
            rec["sid"] = sid.clone().into();
        }
        if let Some(uid) = rr.uid.filter(|u| *u != u32::MAX) {
            rec["uid"] = uid.into();
        }
        if let Some(a) = &rr.client_addr {
            rec["client_addr"] = a.clone().into();
        }
        if let Some(sec) = &rr.sec {
            rec["details"]["sec"] = sec.clone().into();
        }
        let mut pending = self.audit_pending.lock().unwrap();
        if pending.len() < AUDIT_PENDING_MAX {
            pending.push(rec);
        }
    }

    /// Record what the namespace sync did about a user's change
    /// (`control-api.md` §4.7): a hard link split or left out (decision 42).
    pub fn audit_ns(
        &self,
        action: &str,
        decision: &str,
        file_id: Option<u64>,
        path: &std::path::Path,
    ) {
        let mut rec = serde_json::json!({
            "at": omnifile_control::util::format_time(std::time::SystemTime::now()),
            "action": action,
            "decision": decision,
            "details": {"path": path.to_string_lossy()},
        });
        if let Some(f) = file_id {
            rec["file_id"] = f.to_string().into();
        }
        let mut pending = self.audit_pending.lock().unwrap();
        if pending.len() < AUDIT_PENDING_MAX {
            pending.push(rec);
        }
    }

    /// Move pending audit records into the replica, then send what it
    /// holds, 5,000 at a time, while the service takes them.
    pub async fn flush_audit(&self) {
        let pending = std::mem::take(&mut *self.audit_pending.lock().unwrap());
        if !pending.is_empty() {
            let now = omnifile_control::util::format_time(std::time::SystemTime::now());
            if let Err(err) = self.replica.lock().await.audit_push(&pending, &now) {
                warn!(%err, n = pending.len(), "audit records not kept");
            }
        }
        loop {
            let batch = match self.replica.lock().await.audit_peek(AUDIT_BATCH) {
                Ok(b) if !b.is_empty() => b,
                Ok(_) => return,
                Err(err) => return warn!(%err, "audit records unreadable"),
            };
            let last = batch.last().map(|(s, _)| *s).unwrap_or(0);
            let records: Vec<serde_json::Value> = batch.into_iter().map(|(_, r)| r).collect();
            if let Err(e) = self.control.audit(&records).await {
                if !e.unavailable {
                    warn!(code = ?e.code, message = %e.message, "audit records refused; kept");
                }
                return;
            }
            if let Err(err) = self.replica.lock().await.audit_ack(last) {
                return warn!(%err, "audit records sent, not cleared");
            }
        }
    }

    fn recall_request(&self, ri: &ReadIntent, on_open: &str) -> RecallRequest {
        let id = &ri.identity;
        RecallRequest {
            file_id: ri.obj.file_id,
            version_id: ri.version_id,
            offset: ri.offset,
            length: ri.length,
            uid: (id.uid != u32::MAX).then_some(id.uid),
            sec: (id.sec != 0).then(|| id.sec_name().to_string()),
            proof: if id.proven() {
                "proven".into()
            } else {
                "claimed".into()
            },
            client_addr: id.client_addr().map(|a| a.to_string()),
            on_open: on_open.to_string(),
            via: "nfs".into(),
            sid: None,
            pinned: ri.marker_flags & MARKER_READONLY != 0,
        }
    }

    async fn report_requester(self: Arc<Self>, ri: ReadIntent) {
        let req = self.recall_request(&ri, "");
        self.report_requester_with(req).await
    }

    /// Report one more requester of an active recall (NFS or smb-guard).
    pub(crate) async fn report_requester_with(self: Arc<Self>, mut req: RecallRequest) {
        req.on_open = self.on_open_of(req.file_id).await.0;
        let req = self.with_sid(req).await;
        if let Err(e) = self.control.recall_start(&req).await {
            debug!(err = %e, "could not report an additional requester");
        }
    }

    /// `(on_open, wait_threshold_minutes)` of the file's share.
    async fn on_open_of(&self, file_id: u64) -> (String, Option<u64>) {
        let rep = self.replica.lock().await;
        let share = rep
            .node_by_file_id(file_id)
            .ok()
            .flatten()
            .and_then(|n| rep.share_of(n.lid).ok().flatten());
        match share {
            Some(s) => (s.on_open, s.wait_threshold_minutes),
            None => ("fail".into(), None),
        }
    }

    async fn start_recall(self: Arc<Self>, ri: ReadIntent) {
        let key = recall_key(ri.obj.file_id, ri.version_id, ri.marker_flags, ri.obj.ino);
        let blocked = ri.policy.fail_closed();
        let req = self.recall_request(&ri, "");
        self.start_recall_with(key, req, blocked).await
    }

    /// Start a recall or fetch for `key` on behalf of `req`'s requester
    /// (`on_open` is filled in here). `blocked`: what archived reads get
    /// while the service is unreachable.
    pub(crate) async fn start_recall_with(
        self: Arc<Self>,
        key: RecallKey,
        req: RecallRequest,
        blocked: Decision,
    ) {
        let node = if key.2 == 0 {
            match self.replica.lock().await.node_by_file_id(key.0) {
                Ok(Some(n)) if !n.deleted && n.content_version_id == Some(key.1) => n,
                _ => return self.fail(key, "not the current content version"),
            }
        } else {
            // A rewind placeholder of the version the file held then.
            match self.rewind_node(key).await {
                Some(n) => n,
                None => return self.fail(key, "rewind placeholder not found"),
            }
        };
        let (on_open, _) = self.on_open_of(key.0).await;
        let req = self.with_sid(RecallRequest { on_open, ..req }).await;
        let mut backoff = Duration::from_secs(1);
        loop {
            if self.is_shutting_down() {
                return;
            }
            match self.control.recall_start(&req).await {
                Ok(RecallResponse::Online { url }) => {
                    return self.run_fill(key, node, url, None).await;
                }
                Ok(RecallResponse::Recall {
                    recall_id,
                    state,
                    decision,
                    eta_s,
                    note,
                }) => {
                    let answer = if decision == "wait" {
                        Decision::Delay
                    } else {
                        Decision::Eio
                    };
                    let answer = if state == "failed" {
                        Decision::Eio
                    } else {
                        answer
                    };
                    // FETCHING fences truncates in the FSAL, so only a fill
                    // this daemon runs shows it (`fsal-socket.md` §6.1): a
                    // `downloading` recall no fill works on yet is QUEUED.
                    let filling = self.fills.is_running(key);
                    self.update(key, |e| {
                        e.recall_id = recall_id;
                        let phase = RecallPhase::from_catalog(&state);
                        if !(phase == RecallPhase::Fetching && !filling) {
                            e.phase = phase;
                        } else if e.phase != RecallPhase::Fetching {
                            e.phase = RecallPhase::Queued;
                        }
                        e.answer = answer;
                        e.eta_s = eta_s.unwrap_or(ReadDecision::ETA_UNKNOWN);
                    });
                    let row = RecallRow {
                        recall_id,
                        file_id: key.0,
                        version_id: key.1,
                        state: state.clone(),
                        eta: None,
                        error: None,
                        read_credential: None,
                        entity_version: 0,
                    };
                    if let Err(e) = self.replica.lock().await.upsert_recall(&row) {
                        warn!(err = %e, "could not record recall");
                    }
                    if let Some(note) = note {
                        self.write_note(&node, &note).await;
                    }
                    info!(
                        file_id = key.0,
                        version_id = key.1,
                        recall_id,
                        state,
                        "recall started"
                    );
                    // Already `downloading`, and no fill here works on it
                    // (its fill failed, or this daemon restarted since): the
                    // feed won't say `downloading` again, so the fill is
                    // started or resumed now, never left to an event that
                    // already came (recall-resume). Not while a resume of an
                    // interrupted fill is scheduled: readers retrying every few
                    // seconds would skip its backoff, and each fill would
                    // re-read the whole on-disk prefix for its CRC (#62 review).
                    let resume_pending = self
                        .fill_resumes
                        .lock()
                        .unwrap()
                        .get(&key)
                        .is_some_and(FillResumes::pending);
                    if state == "downloading" && !self.fills.is_running(key) && !resume_pending {
                        let row = self
                            .replica
                            .lock()
                            .await
                            .recall(recall_id)
                            .ok()
                            .flatten()
                            .unwrap_or(row);
                        self.apply_recall_row(&row, key, node).await;
                    }
                    return;
                }
                Err(e) if e.unavailable => {
                    // Queued on the edge; the FSAL answers the policy default.
                    self.update(key, |en| {
                        en.phase = RecallPhase::Blocked;
                        en.answer = if en.online { Decision::Delay } else { blocked };
                    });
                    debug!(err = %e, ?backoff, "service unreachable; recall queued on the edge");
                    tokio::time::sleep(backoff).await;
                    backoff = (backoff * 2).min(Duration::from_secs(30));
                    if !self.recalls.lock().unwrap().contains_key(&key) {
                        return;
                    }
                }
                Err(e) => return self.fail(key, &e.message),
            }
        }
    }

    /// Write `<file>.RECALLING.txt` (B1): resolved below the data volume
    /// without following symlinks, created as a new marked inode owned by
    /// the file's owner, and never written through a name the daemon did
    /// not create (`omnifile_stub::placeholder::write_note`).
    async fn write_note(&self, node: &Node, text: &str) {
        let Some(rel) = self.replica.lock().await.fs_path(node.lid).ok().flatten() else {
            return;
        };
        let name = rel
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        let text = omnifile_stub::sanitize_note(text, &name);
        if let Some(file_id) = node.file_id {
            self.note_paths.lock().unwrap().insert(file_id, rel.clone());
        }
        let layout = self.layout.clone();
        let (uid, gid) = (node.uid, node.gid);
        let r = tokio::task::spawn_blocking(move || {
            let r = omnifile_stub::placeholder::write_note(&layout, &rel, &text, uid, gid);
            (rel, r)
        })
        .await;
        match r {
            Ok((_, Ok(omnifile_stub::placeholder::NoteOutcome::Foreign))) => {
                debug!(file = %name, "a file that is not the daemon's note holds the note's name; left alone");
            }
            Ok((_, Ok(_))) => {}
            Ok((rel, Err(e))) => {
                warn!(path = %rel.display(), err = %e, "could not write recall note");
            }
            Err(e) => warn!(err = %e, "recall note task failed"),
        }
    }

    async fn remove_note(&self, node: &Node) {
        if let Some(file_id) = node.file_id {
            self.note_paths.lock().unwrap().remove(&file_id);
        }
        let Some(rel) = self.replica.lock().await.fs_path(node.lid).ok().flatten() else {
            return;
        };
        let layout = self.layout.clone();
        let _ = tokio::task::spawn_blocking(move || {
            omnifile_stub::placeholder::remove_note(&layout, &rel)
        })
        .await;
    }

    /// Remove the note this daemon wrote for `file_id`, where it wrote it:
    /// for a file that has since been deleted, which the replica can no
    /// longer place. Only the daemon's own note is ever removed
    /// (`placeholder::remove_note`).
    async fn remove_note_where_written(&self, file_id: u64) {
        let Some(rel) = self.note_paths.lock().unwrap().remove(&file_id) else {
            return;
        };
        let layout = self.layout.clone();
        let _ = tokio::task::spawn_blocking(move || {
            omnifile_stub::placeholder::remove_note(&layout, &rel)
        })
        .await;
    }

    /// Fill `key`'s placeholder `node` from `url`. `recall_id` is set for
    /// archive recalls (the service is told when the fill is done). A node
    /// that doesn't hold `key`'s version is refused: its bytes would be
    /// another version's.
    pub async fn run_fill(
        self: Arc<Self>,
        key: RecallKey,
        node: Node,
        url: ReadUrl,
        recall_id: Option<u64>,
    ) {
        self.run_fill_from(key, node, url, recall_id, false).await
    }

    /// [`Self::run_fill`]; `resumed` when the resume timer of an interrupted
    /// fill started it.
    async fn run_fill_from(
        self: Arc<Self>,
        key: RecallKey,
        node: Node,
        url: ReadUrl,
        recall_id: Option<u64>,
        resumed: bool,
    ) {
        let (file_id, version_id) = (key.0, key.1);
        if node.file_id != Some(file_id) || node.content_version_id != Some(version_id) {
            warn!(
                file_id,
                version_id,
                node_version = ?node.content_version_id,
                "fill refused: the placeholder holds another version"
            );
            return;
        }
        // One fill per key: a second request (a restart's resumed recall
        // and the feed's `downloading`) joins the running one, and must not
        // end its FETCHING state with a FAILED of its own.
        if self.is_shutting_down() || !self.fills.start(key) {
            return;
        }
        let mut run = FillRun {
            core: self.clone(),
            key,
            ended: false,
        };
        // `fill` takes the file's lock itself; this only avoids disturbing
        // the state of a fill (or stub) that is already running.
        // Wait for a stub, content change or earlier fill of this file to
        // finish (a finished fill makes this one a no-op). `fill` takes the
        // lock itself, and retries briefly if it loses the race.
        for _ in 0..100 {
            if !self.locks.is_locked(file_id) || self.fills.stopping(key) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        let obj = self.obj_for(&node);
        {
            let mut map = self.recalls.lock().unwrap();
            let e = map.entry(key).or_insert_with(|| RecallEntry {
                obj: obj.unwrap_or_default(),
                version_id,
                recall_id: recall_id.unwrap_or(0),
                phase: RecallPhase::Fetching,
                answer: Decision::Delay,
                eta_s: ReadDecision::ETA_UNKNOWN,
                resident: 0,
                online: true,
                requesters: HashSet::new(),
            });
            e.phase = RecallPhase::Fetching;
            e.answer = Decision::Delay;
            if let Some(r) = recall_id {
                e.recall_id = r;
            }
        }
        if let Some(e) = self.recalls.lock().unwrap().get(&key).cloned() {
            self.push(&e);
        }
        let rel = match self.fill_rel(&node, key).await {
            Some(r) => r,
            None => {
                run.ended = true;
                drop(run);
                // Only the live file's own placeholder missing is the
                // recall's failure. A rewind placeholder this daemon no
                // longer tracks shares its recall with the live file, and a
                // resume finding nothing is not a new failure either: the
                // reader's next open starts the fill again (#52 review).
                if let Some(rid) = recall_id
                    && key.2 == 0
                    && !resumed
                {
                    self.clone().after_failed_fill(
                        key,
                        rid,
                        node.clone(),
                        "placeholder not on disk".into(),
                        false,
                    );
                }
                return self.fail(key, "placeholder not on disk");
            }
        };
        // Already filled at this version (a fill whose report was lost, or
        // a resumed one that finished before a restart): nothing to fill,
        // and nothing the fill changed to account for; only the report.
        let already = self.resident_at(&rel, file_id, version_id).await;
        let mut target = FetchTarget {
            file_id,
            version_id,
            url: url.url,
            source: url.source,
            size: node.size,
            // The catalog's CRC, through the replica; for a file the
            // replica has none for yet, the one the S3 credential vouches
            // for (the service learned it from S3 for this read).
            crc64nvme: node.crc64nvme.or(url.crc64nvme),
        };
        let req = FillRequest {
            rel: rel.clone(),
            expect_ino: node.ino.zip(node.ino_gen),
        };
        let opts = FillOptions {
            chunk: self.cfg.fill.chunk_mib << 20,
            widen_every: self.cfg.fill.widen_every_mib << 20,
        };
        let src = BlobSource(self.blob.clone());
        let (mut attempt, mut refreshed) = (0, 0);
        let res = loop {
            // A placeholder an O_APPEND write grew past its marker (M2
            // review S3) is repaired, not filled.
            let extended = omnifile_stub::fill::is_extended(&self.layout, &req).unwrap_or(false);
            let r = if extended {
                omnifile_stub::fill::repair_extended(
                    &self.layout,
                    &req,
                    &target,
                    &src,
                    self.as_ref(),
                    &self.seq,
                    &self.locks,
                )
                .await
            } else {
                fill_tracked(
                    &self.layout,
                    &req,
                    &target,
                    &src,
                    self.as_ref(),
                    &self.seq,
                    &self.locks,
                    opts,
                )
                .await
            };
            match r {
                // An FSAL (re)connected or didn't confirm the fence: nothing
                // unsafe happened; try again shortly.
                // Busy: a stub or content change holds the file for a moment.
                // FETCHING stays asserted meanwhile (truncates wait); every
                // path out of this loop pushes DONE or FAILED, which ends it.
                Err(FillError::FenceBroken | FillError::Busy)
                    if attempt < 5 && !self.fills.stopping(key) =>
                {
                    attempt += 1;
                    tokio::time::sleep(Duration::from_millis(200 * attempt)).await;
                }
                // The read credential expired mid-fill: a new one, and the
                // fill resumes where it got to.
                Err(FillError::Source(e))
                    if e.kind() == std::io::ErrorKind::PermissionDenied
                        && refreshed < 3
                        && !self.fills.stopping(key) =>
                {
                    refreshed += 1;
                    match self.control.read_url(file_id, version_id).await {
                        Ok(u) => {
                            target.url = u.url;
                            target.source = u.source;
                            target.crc64nvme = target.crc64nvme.or(u.crc64nvme);
                        }
                        Err(err) => {
                            break Err(FillError::Source(std::io::Error::other(err.message)));
                        }
                    }
                }
                other => break other,
            }
        };
        // The fill no longer writes: only now may its FETCHING end. It is
        // unregistered (dropping `run`) after the terminal state is pushed,
        // so the orphan sweep never sees FETCHING without a fill.
        run.ended = true;
        match res {
            Ok(done) => {
                let n = done.size;
                self.update(key, |e| {
                    e.phase = RecallPhase::Done;
                    e.resident = n;
                });
                // Record the ctime only if nothing but the fill changed the
                // file from its start to its end, and nobody wrote into it
                // while it was partial (M2 review B2): the ctime the fill
                // left, never one stat-ed now (after a later user write).
                // Otherwise the bytes are the user's: commit them.
                // A rewind placeholder is history, not the live file: none of
                // the live file's bookkeeping applies to it.
                let rewind = node.lid < 0;
                let dirty = !rewind && !already && {
                    let mut rep = self.replica.lock().await;
                    let was_dirty = rep
                        .node_by_file_id(file_id)
                        .ok()
                        .flatten()
                        .is_some_and(|n| n.local_dirty);
                    match done.ctime_ns {
                        Some(c) if !was_dirty => {
                            let _ = rep.set_fs_ctime(node.lid, c);
                            false
                        }
                        _ => {
                            let _ = rep.clear_fs_ctime(node.lid);
                            let _ = rep.set_local_dirty(node.lid, true);
                            true
                        }
                    }
                };
                if dirty {
                    self.commit_modified(&rel).await;
                }
                if let Some(rid) = recall_id {
                    let row = RecallRow {
                        recall_id: rid,
                        file_id,
                        version_id,
                        state: "available".into(),
                        eta: None,
                        error: None,
                        read_credential: None,
                        entity_version: self
                            .replica
                            .lock()
                            .await
                            .recall(rid)
                            .ok()
                            .flatten()
                            .map(|r| r.entity_version)
                            .unwrap_or(0),
                    };
                    self.fill_resumes.lock().unwrap().remove(&key);
                    self.clone().report_recall_filled(row, n);
                } else if !rewind {
                    // An online fetch: the catalog says resident again (M1 carry-over).
                    self.clone().report_fetch_filled(file_id, version_id, n);
                }
                self.remove_note(&node).await;
                self.forget_later(
                    key,
                    Duration::from_millis(self.cfg.fsal.recall_valid_ms as u64),
                );
            }
            // What the stop asked for (a cancelled or failed recall, or
            // shutdown), else FAILED: never left at FETCHING. The FSAL's
            // fill fence ends only on DONE, FAILED or CANCELLED
            // (`fsal-socket.md` §6.1).
            Err(e) => {
                // Stopped on purpose (the daemon stopping, the recall
                // cancelled or failed by the service): not this fill's to
                // report. A restart resumes a recall still `downloading`.
                let stopped = self.is_shutting_down() || self.fills.stopping(key);
                self.fills.finish_with(
                    key,
                    Terminal {
                        phase: RecallPhase::Failed,
                        answer: Decision::Eio,
                    },
                    |t| {
                        warn!(file_id, version_id, err = %e, phase = ?t.phase, "fill ended without the data");
                        self.end_with(key, t);
                    },
                );
                if let Some(rid) = recall_id
                    && !stopped
                {
                    self.clone().after_failed_fill(
                        key,
                        rid,
                        node.clone(),
                        e.to_string(),
                        fill_interrupted(&e),
                    );
                }
            }
        }
        drop(run);
    }

    /// Report a recall's fill to the service, retrying while it is away. The
    /// replica's row says `available` once the service has it; until then a
    /// restart or the next read resumes the recall, finds the bytes already
    /// there and reports again.
    fn report_recall_filled(self: Arc<Self>, mut row: RecallRow, resident: u64) {
        tokio::spawn(async move {
            let mut backoff = Duration::from_secs(1);
            for attempt in 1..=REPORT_TRIES {
                match self
                    .control
                    .recall_filled(row.recall_id, row.version_id, resident)
                    .await
                {
                    Err(e)
                        if e.unavailable && attempt < REPORT_TRIES && !self.is_shutting_down() =>
                    {
                        debug!(recall_id = row.recall_id, err = %e, ?backoff, "fill report: service away; retried");
                        tokio::time::sleep(backoff).await;
                        backoff = (backoff * 2).min(Duration::from_secs(60));
                    }
                    Err(e) if e.unavailable => {
                        warn!(recall_id = row.recall_id, err = %e,
                              "could not report the fill; the next read or start reports it again");
                        return;
                    }
                    other => {
                        if let Err(e) = other {
                            // Refused (the recall failed or was cancelled
                            // meanwhile): the feed brings its state. The bytes
                            // are local either way.
                            warn!(recall_id = row.recall_id, err = %e, "the fill report was refused");
                        }
                        row.state = "available".into();
                        let _ = self.replica.lock().await.upsert_recall(&row);
                        return;
                    }
                }
            }
        });
    }

    /// A recall's fill ended without the data, not by request. Interrupted
    /// (an FSAL not yet reconnected, the source or network away, the data
    /// volume full): resumed after a backoff, up to [`FILL_RESUMES`] times
    /// within [`FILL_RESUME_WINDOW`], while the recall is still
    /// `downloading`. Anything else (the placeholder gone or replaced, the
    /// bytes failing their CRC), or too many interruptions: reported, and
    /// the recall becomes `failed` with the
    /// reason, so it never stays `downloading` (WP3's soak; control-api.md
    /// §4.4). A reader who opens the file again starts a new recall.
    fn after_failed_fill(
        self: Arc<Self>,
        key: RecallKey,
        recall_id: u64,
        node: Node,
        reason: String,
        interrupted: bool,
    ) {
        tokio::spawn(async move {
            if interrupted {
                // A rewind placeholder's fill is left to the reader's next
                // open: its entry is forgotten soon after FAILED, so a timer
                // resume would find no placeholder (#52 review).
                if key.2 != 0 {
                    info!(recall_id, %reason, "rewind fill interrupted; the next open resumes it");
                    return;
                }
                let n = {
                    let mut m = self.fill_resumes.lock().unwrap();
                    let r = m.entry(key).or_default();
                    let now = std::time::Instant::now();
                    while r
                        .times
                        .front()
                        .is_some_and(|t| now.duration_since(*t) > FILL_RESUME_WINDOW)
                    {
                        r.times.pop_front();
                    }
                    if r.pending {
                        // Already scheduled: this failure (a reader's fill)
                        // costs nothing more.
                        return;
                    }
                    if r.times.len() < FILL_RESUMES as usize {
                        r.times.push_back(now);
                        r.pending = true;
                        Some(r.times.len() as u32)
                    } else {
                        None
                    }
                };
                if let Some(n) = n {
                    let wait = (FILL_RESUME_BASE * (1 << (n - 1).min(8))).min(FILL_RESUME_MAX);
                    info!(recall_id, attempt = n, ?wait, %reason, "recall fill interrupted; resumed later");
                    let mut stop = self.shutdown.subscribe();
                    tokio::select! {
                        _ = tokio::time::sleep(wait) => {}
                        _ = stop.changed() => return,
                    }
                    if let Some(r) = self.fill_resumes.lock().unwrap().get_mut(&key) {
                        r.pending = false;
                    }
                    let row = self.replica.lock().await.recall(recall_id).ok().flatten();
                    if let Some(row) = row.filter(|r| r.state == "downloading")
                        && !self.fills.is_running(key)
                    {
                        self.apply_recall_row_from(&row, key, node, true).await;
                    }
                    return;
                }
            }
            self.fill_resumes.lock().unwrap().remove(&key);
            // A rewind placeholder of the live file's current version shares
            // the live file's recall: failing it would stop that fill too.
            if key.2 != 0
                && self
                    .replica
                    .lock()
                    .await
                    .node_by_file_id(key.0)
                    .ok()
                    .flatten()
                    .is_some_and(|n| n.content_version_id == Some(key.1))
            {
                warn!(recall_id, %reason, "rewind fill failed; not reported (the live file shares its recall)");
                return;
            }
            let mut backoff = Duration::from_secs(1);
            for attempt in 1..=REPORT_TRIES {
                // The live file is no longer this version: someone replaced or
                // deleted it while it was being recalled. That is no failure
                // of the recall. The service ends it `superseded` in the
                // transaction that commits the new version or the delete
                // (control API §4.4), so nothing is reported, before or after
                // that commit.
                if let Some(why) = self.moved_on(key, &node).await {
                    info!(
                        recall_id,
                        %reason,
                        why,
                        "the fill met another file; not reported: the recall ends with that change"
                    );
                    return;
                }
                if attempt == 1 {
                    warn!(recall_id, %reason, "recall fill failed: reporting it");
                }
                // A newer fill of this recall is running, or the recall left
                // `downloading` (the bytes arrived, it was cancelled): this
                // report is stale and must not fail it (#52 review).
                if self.fills.is_running(key) {
                    debug!(recall_id, "failure report dropped: a newer fill is running");
                    return;
                }
                // A newer fill finished, its own report still to come.
                if let Some(rel) = self.fill_rel(&node, key).await
                    && self.resident_at(&rel, key.0, key.1).await
                {
                    debug!(recall_id, "failure report dropped: the bytes are local");
                    return;
                }
                if self
                    .replica
                    .lock()
                    .await
                    .recall(recall_id)
                    .ok()
                    .flatten()
                    .is_some_and(|r| r.state != "downloading")
                {
                    debug!(
                        recall_id,
                        "failure report dropped: the recall is no longer downloading"
                    );
                    return;
                }
                match self.control.recall_failed(recall_id, key.1, &reason).await {
                    Err(e)
                        if e.unavailable && attempt < REPORT_TRIES && !self.is_shutting_down() =>
                    {
                        tokio::time::sleep(backoff).await;
                        backoff = (backoff * 2).min(Duration::from_secs(60));
                    }
                    Err(e) if e.unavailable => {
                        // Still `downloading` here too: a restart or the next
                        // read tries again.
                        warn!(recall_id, err = %e, "could not report the failed fill");
                        return;
                    }
                    Err(e) => {
                        // Refused (the recall is available, cancelled or for
                        // another version by now): the feed brings its state.
                        warn!(recall_id, err = %e, "the failed fill's report was refused");
                        return;
                    }
                    Ok(()) => {
                        let mut rep = self.replica.lock().await;
                        if let Ok(Some(mut row)) = rep.recall(recall_id)
                            && row.state == "downloading"
                        {
                            row.state = "failed".into();
                            row.error = Some(reason.clone());
                            let _ = rep.upsert_recall(&row);
                        }
                        return;
                    }
                }
            }
        });
    }

    /// Why a fill of the live file that ended without the data is no failure
    /// of its recall: the file is no longer the version the recall is for.
    /// `None` for a rewind placeholder, and when nothing says so.
    ///
    /// - The replica says it: the file is deleted, or its content version is
    ///   another (the change is committed).
    /// - The disk says it, before the change is committed: another inode
    ///   holds the name (a rename over it), the file has no marker (the FSAL
    ///   removes it when a writer truncates a stub, `fsal-socket.md` §6.1),
    ///   or its marker names another version of the file.
    /// - The file is gone from the disk but not yet from the replica: a
    ///   delete on its way (or a rename). The replica is asked again after
    ///   [`MOVED_ON_SETTLE`], and only a delete counts.
    ///
    /// A marker that can't be read, or one that still names this version, is
    /// neither: that fill really failed.
    async fn moved_on(&self, key: RecallKey, node: &Node) -> Option<&'static str> {
        if key.2 != 0 {
            return None;
        }
        for settled in [false, true] {
            let now = match self.replica.lock().await.node_by_file_id(key.0) {
                Ok(Some(n)) => n,
                Ok(None) => return Some("deleted"),
                Err(_) => return None,
            };
            if now.deleted {
                return Some("deleted");
            }
            if now.content_version_id != Some(key.1) {
                return Some("replaced: a newer version is committed");
            }
            if settled {
                return None;
            }
            let rel = self.replica.lock().await.fs_path(now.lid).ok().flatten()?;
            let root = self.layout.data_root.clone();
            let (file_id, version_id) = (key.0, key.1);
            let expect = node.ino.zip(node.ino_gen);
            let on_disk = tokio::task::spawn_blocking(move || {
                use omnifile_marker::xattr::ReadOutcome;
                use std::os::fd::AsFd;
                let fd = match omnifile_stub::sys::open_ro_beneath(&root, &rel) {
                    Ok(fd) => fd,
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Err(()),
                    Err(_) => return Ok(None),
                };
                if let Some((ino, ino_gen)) = expect
                    && omnifile_stub::sys::inode_key(fd.as_fd())
                        .is_ok_and(|k| k.ino != ino || k.ino_gen != ino_gen)
                {
                    return Ok(Some("replaced: another file holds its name"));
                }
                Ok(match omnifile_marker::xattr::read(fd.as_fd()) {
                    Ok(ReadOutcome::Unmanaged) => {
                        Some("replaced: its new bytes are local, not committed yet")
                    }
                    Ok(ReadOutcome::Marker(m))
                        if m.file_id == file_id && m.version_id != version_id =>
                    {
                        Some("replaced: its marker names another version")
                    }
                    _ => None,
                })
            })
            .await
            .unwrap_or(Ok(None));
            match on_disk {
                Ok(why) => return why,
                // Gone from the disk: wait for the delete to reach the replica.
                Err(()) => tokio::time::sleep(MOVED_ON_SETTLE).await,
            }
        }
        None
    }

    /// React to recall rows the feed changed.
    pub async fn on_recall_rows(self: &Arc<Self>, rows: Vec<RecallRow>) {
        for r in rows {
            let live = (r.file_id, r.version_id, 0);
            // A deleted file has no placeholder (and no inode in the replica)
            // for the row to be applied to: it is left behind, as a file that
            // holds another version is.
            let targets: Vec<(RecallKey, Node)> = self
                .recall_row_targets(&r)
                .await
                .into_iter()
                .filter(|(k, n)| *k != live || (!n.deleted && self.obj_for(n).is_some()))
                .collect();
            let phase = RecallPhase::from_catalog(&r.state);
            if phase.is_terminal() && !targets.iter().any(|(k, _)| *k == live) {
                // `available` too. A job in the service (a version restore,
                // an annex placement) can keep a recall going after its file
                // was deleted or replaced, and close it `available` when it
                // has its bytes. Nothing was filled here, and a reader that
                // was waiting on this recall would wait for good. Only a
                // reader still waiting is told: an entry this daemon's own
                // fill ended is left as the fill ended it.
                let waiting = self
                    .recalls
                    .lock()
                    .unwrap()
                    .get(&live)
                    .is_some_and(|e| !e.phase.is_terminal());
                if phase != RecallPhase::Done || waiting {
                    self.end_left_behind(&r, live, phase).await;
                }
            }
            for (key, node) in targets {
                self.apply_recall_row(&r, key, node).await;
            }
        }
    }

    /// A recall ended (`superseded`, `failed`, `cancelled`, or `available`
    /// with a reader still waiting) for a version the live file no longer
    /// holds, or for a file that is deleted, so the row has no live
    /// placeholder to be applied to. What this daemon still keeps
    /// for it goes: a fill still running is asked to stop, a resume is not
    /// scheduled again, and its entry ends, so nothing waits on it any more.
    ///
    /// What a reader that was waiting gets depends on the inode it holds:
    /// - **the file's own inode, replaced in place:** the entry ends as the
    ///   row says (`CANCELLED` for `superseded`), the FSAL drops it, and the
    ///   reader's next read finds the new marker (or none) and the new bytes.
    ///   `available` ends it `CANCELLED` too: the bytes that came back were
    ///   another version's, and nothing was filled into this file;
    /// - **an inode that is no longer the file** (the file is deleted, or
    ///   another was renamed over it, and the reader still holds the old one
    ///   open): its bytes can never come, so the entry ends `FAILED` and the
    ///   reader gets `EIO` on its next read. Ended as cancelled, a reader of
    ///   a `wait` share would have been told to wait again each time.
    ///
    /// Its `.RECALLING.txt` note is removed unless a recall of the file's
    /// present version has one of its own.
    async fn end_left_behind(self: &Arc<Self>, r: &RecallRow, key: RecallKey, phase: RecallPhase) {
        self.fill_resumes.lock().unwrap().remove(&key);
        let held_ino = self.recalls.lock().unwrap().get(&key).map(|e| e.obj.ino);
        let now = self
            .replica
            .lock()
            .await
            .node_by_file_id(r.file_id)
            .ok()
            .flatten();
        let still_the_file = now
            .as_ref()
            .is_some_and(|n| !n.deleted && n.ino.is_some() && n.ino == held_ino);
        let t = if still_the_file {
            let (on_open, threshold) = self.on_open_of(r.file_id).await;
            let (phase, state) = if phase == RecallPhase::Done {
                (RecallPhase::Cancelled, "cancelled")
            } else {
                (phase, r.state.as_str())
            };
            Terminal {
                phase,
                answer: answer_for(state, &on_open, None, threshold),
            }
        } else {
            Terminal {
                phase: RecallPhase::Failed,
                answer: Decision::Eio,
            }
        };
        let known = held_ino.is_some();
        if !self.fills.stop(key, t) && known {
            self.end_with(key, t);
        }
        let other_active = self
            .recalls
            .lock()
            .unwrap()
            .iter()
            .any(|(k, e)| k.0 == r.file_id && *k != key && k.2 == 0 && !e.phase.is_terminal());
        if !other_active {
            // Where it was written (a deleted or renamed file), then where
            // the file is now.
            self.remove_note_where_written(r.file_id).await;
            if let Some(n) = now.filter(|n| !n.deleted) {
                self.remove_note(&n).await;
            }
        }
    }

    /// The placeholders a recall row is for: the live file when the version
    /// is its current content, and each rewind placeholder of that version
    /// being read. A rewind read this daemon no longer tracks (it restarted)
    /// is picked up again by the reader's next READ_INTENT.
    async fn recall_row_targets(&self, r: &RecallRow) -> Vec<(RecallKey, Node)> {
        let mut out = Vec::new();
        if let Ok(Some(n)) = self.replica.lock().await.node_by_file_id(r.file_id)
            && n.content_version_id == Some(r.version_id)
        {
            out.push(((r.file_id, r.version_id, 0), n));
        }
        let mut keys: Vec<RecallKey> = self
            .recalls
            .lock()
            .unwrap()
            .keys()
            .chain(self.rewind_targets.lock().unwrap().keys())
            .filter(|k| k.0 == r.file_id && k.1 == r.version_id && k.2 != 0)
            .copied()
            .collect();
        keys.sort_unstable();
        keys.dedup();
        for key in keys {
            if let Some(n) = self.rewind_node(key).await {
                out.push((key, n));
            }
        }
        out
    }

    async fn apply_recall_row(self: &Arc<Self>, r: &RecallRow, key: RecallKey, node: Node) {
        self.apply_recall_row_from(r, key, node, false).await
    }

    /// [`Self::apply_recall_row`]; `resumed` when an interrupted fill's
    /// resume timer applies it.
    async fn apply_recall_row_from(
        self: &Arc<Self>,
        r: &RecallRow,
        key: RecallKey,
        node: Node,
        resumed: bool,
    ) {
        {
            let Some(obj) = self.obj_for(&node) else {
                return;
            };
            let (on_open, threshold) = self.on_open_of(r.file_id).await;
            let phase = RecallPhase::from_catalog(&r.state);
            let answer = answer_for(&r.state, &on_open, None, threshold);
            // A fill of this key still writes: its FETCHING (the truncate
            // fence) must stay until it has stopped. Ask it to stop; it
            // pushes this terminal state itself.
            if phase.is_terminal()
                && phase != RecallPhase::Done
                && self.fills.stop(key, Terminal { phase, answer })
            {
                if let Some(e) = self.recalls.lock().unwrap().get_mut(&key) {
                    e.recall_id = r.recall_id;
                }
                if r.state != "available" {
                    self.remove_note(&node).await;
                }
                return;
            }
            let e = {
                let mut map = self.recalls.lock().unwrap();
                let e = map.entry(key).or_insert_with(|| RecallEntry {
                    obj,
                    version_id: r.version_id,
                    recall_id: r.recall_id,
                    phase,
                    answer,
                    eta_s: ReadDecision::ETA_UNKNOWN,
                    resident: 0,
                    online: false,
                    requesters: HashSet::new(),
                });
                // A fill in progress owns the phase until it finishes. FETCHING
                // fences truncates in the FSAL (`fsal-socket.md` §6.1), so it is
                // only ever pushed by a fill this daemon runs, which always ends
                // it with DONE or FAILED: the feed's `downloading` is shown as
                // QUEUED until the fill starts.
                let filling = self.fills.is_running(key);
                if !filling && (e.phase != RecallPhase::Fetching || phase.is_terminal()) {
                    e.phase = if phase == RecallPhase::Fetching {
                        RecallPhase::Queued
                    } else {
                        phase
                    };
                }
                e.recall_id = r.recall_id;
                if !filling {
                    e.answer = answer;
                }
                e.clone()
            };
            self.push(&e);
            let recall_id = r.recall_id;
            match r.state.as_str() {
                "downloading" => {
                    // The recall's own credential, if this edge can read with
                    // it; otherwise ask for one (an unknown mode fails there).
                    let url = r
                        .read_credential
                        .as_ref()
                        .and_then(|c| {
                            serde_json::from_value::<omnifile_control::types::Credential>(c.clone())
                                .ok()
                        })
                        .as_ref()
                        .and_then(crate::io::read_url_of);
                    match url {
                        Some(url) => {
                            let this = self.clone();
                            tokio::spawn(async move {
                                this.run_fill_from(key, node, url, Some(recall_id), resumed)
                                    .await
                            });
                        }
                        None => match self.control.read_url(r.file_id, r.version_id).await {
                            Ok(url) => {
                                let this = self.clone();
                                tokio::spawn(async move {
                                    this.run_fill_from(key, node, url, Some(recall_id), resumed)
                                        .await
                                });
                            }
                            Err(err) => {
                                warn!(%err, recall_id = r.recall_id, "no read credential for the fill");
                                if !err.unavailable {
                                    self.fail(key, &err.message);
                                }
                            }
                        },
                    }
                }
                "available" | "failed" | "cancelled" | "superseded" => {
                    if r.state != "available" {
                        self.remove_note(&node).await;
                    }
                    self.forget_later(
                        key,
                        Duration::from_millis(self.cfg.fsal.recall_valid_ms as u64),
                    );
                }
                _ => {}
            }
        }
    }
}

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// The recall key of a fill of `key`'s inode: a rewind placeholder's
    /// own key when one is running for that inode, else the live file's.
    fn fill_key(&self, key: InodeKey, version_id: u64) -> RecallKey {
        let rewind = (key.file_id, version_id, key.ino);
        if self.fills.is_running(rewind) {
            rewind
        } else {
            (key.file_id, version_id, 0)
        }
    }
}

fn obj_of(key: InodeKey) -> ObjRef {
    ObjRef {
        fsid: key.fsid,
        ino: key.ino,
        ino_gen: key.ino_gen,
        export_id: 0,
        file_id: key.file_id,
    }
}

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// True when bytes may be punched on the strength of this outcome:
    /// every connected FSAL acknowledged, or this edge runs no Ganesha and
    /// none is connected. Zero connections is never an ack otherwise (B2).
    fn acked(&self, o: &omnifile_fsal_proto::InvalidateOutcome) -> bool {
        o.all_acked() || (self.cfg.fsal.no_ganesha && o.no_fsal())
    }

    /// At least one FSAL is connected and past `HELLO_ACK`, or this edge is
    /// configured to run no Ganesha. Until then nothing is narrowed.
    pub fn fsal_ready(&self) -> bool {
        self.cfg.fsal.no_ganesha || self.server.get().is_some_and(|s| s.ready_connections() > 0)
    }

    /// Push the inode's FETCHING state (`push`) to every FSAL, then get an
    /// acknowledged `INVALIDATE` on the same connections: the fence of a
    /// fill or a content change. Returns the connection epoch it covers.
    /// One attempt: zero connections is not an ack (B2).
    async fn fence(&self, key: InodeKey, push: impl Fn()) -> Option<u64> {
        match self.fence_attempt(key, &push).await {
            Fence::Held(e) => Some(e),
            _ => None,
        }
    }

    async fn fence_attempt(&self, key: InodeKey, push: &impl Fn()) -> Fence {
        let Some(server) = self.server.get() else {
            return if self.cfg.fsal.no_ganesha {
                Fence::Held(0)
            } else {
                Fence::Moving
            };
        };
        let before = server.connection_epoch();
        push();
        let out = server
            .invalidate(obj_of(key), omnifile_stub::what::CONTENT, true)
            .await;
        let after = server.connection_epoch();
        if before != after || out.not_ready > 0 {
            // An FSAL connected, went or is half-way through its handshake.
            return Fence::Moving;
        }
        if self.acked(&out) {
            Fence::Held(after)
        } else if out.no_fsal() {
            Fence::NoFsal(after)
        } else {
            Fence::Refused
        }
    }

    /// The Ganesha processes running now ([`Core::fsal_probe`], or a
    /// `/proc` scan for `fsal.required_exe`, reused for half a second and
    /// run off the async workers).
    async fn ganesha_now(&self) -> Option<HashSet<i32>> {
        if let Some(p) = self.fsal_probe.get() {
            return p();
        }
        let cached = {
            let last = self.ganesha_scan.lock().unwrap();
            last.as_ref()
                .filter(|(at, _)| at.elapsed() < GANESHA_SCAN_TTL)
                .map(|(_, set)| set.clone())
        };
        if let Some(set) = cached {
            return set;
        }
        let exe = self.cfg.fsal.required_exe.clone();
        let set = tokio::task::spawn_blocking(move || ganesha_pids(&exe))
            .await
            .unwrap_or(None);
        *self.ganesha_scan.lock().unwrap() = Some((std::time::Instant::now(), set.clone()));
        set
    }

    /// The Ganesha set as last seen, if that covers `ready`: a fence with
    /// every FSAL it knows of connected needs no new `/proc` scan (the
    /// common case: every M5 chunk fetch fences).
    fn known_ganesha_covered_by(&self, ready: &HashSet<i32>) -> bool {
        if self.fsal_probe.get().is_some() {
            return false;
        }
        match &*self.ganesha_scan.lock().unwrap() {
            Some((_, Some(set))) => set.is_subset(ready),
            Some((_, None)) => true,
            None => false,
        }
    }

    /// Whether the FSALs this edge expects are connected: every running
    /// Ganesha has a ready connection and none is half-way through its
    /// handshake (`Ready`), or no Ganesha runs and none is connected
    /// (`NoGanesha`). Anything else (the socket server not up yet, a
    /// Ganesha whose FSAL has not reconnected, an unknown set and no
    /// connection) is `Waiting`.
    async fn fsal_set(&self) -> FsalSet {
        if self.cfg.fsal.no_ganesha {
            return FsalSet::Ready;
        }
        let Some(server) = self.server.get() else {
            return FsalSet::Waiting;
        };
        if server.handshaking_connections() > 0 {
            return FsalSet::Waiting;
        }
        let ready: HashSet<i32> = server.ready_pids().into_iter().collect();
        if !ready.is_empty() && self.known_ganesha_covered_by(&ready) {
            return FsalSet::Ready;
        }
        match self.ganesha_now().await {
            Some(expected) if expected.is_empty() && ready.is_empty() => FsalSet::NoGanesha,
            Some(expected) if !ready.is_empty() && expected.is_subset(&ready) => FsalSet::Ready,
            None if !ready.is_empty() => FsalSet::Ready,
            _ => FsalSet::Waiting,
        }
    }

    /// When the fence of `rkey`'s fill started waiting: one bound for the
    /// fill, however often it retries a broken fence (`run_fill` does, 5
    /// times). An attempt that follows the last one within
    /// [`FENCE_EPISODE_GAP`] continues its wait; a later one starts anew.
    fn fence_wait_start(&self, rkey: RecallKey) -> std::time::Instant {
        let now = std::time::Instant::now();
        let mut waits = self.fence_waits.lock().unwrap();
        waits.retain(|_, w| now.duration_since(w.last) < FENCE_EPISODE_GAP);
        let w = waits.entry(rkey).or_insert(FenceWait {
            started: now,
            last: now,
        });
        w.last = now;
        w.started
    }

    fn fence_wait_end(&self, rkey: RecallKey, held: bool) {
        let mut waits = self.fence_waits.lock().unwrap();
        if held {
            waits.remove(&rkey);
        } else if let Some(w) = waits.get_mut(&rkey) {
            w.last = std::time::Instant::now();
        }
    }

    /// The fence of a fill (`fsal-socket.md` §6.1), waiting up to
    /// `fsal.reconnect_wait_ms` for the FSALs this edge expects: after a
    /// daemon restart the held reads are taken over at once, before
    /// Ganesha's FSAL has reconnected, and a fill fenced on nobody's word
    /// would fail them (M2 step 10). The bound is the fill's, across its
    /// retries ([`Core::fence_wait_start`]). Zero connections is accepted
    /// only when no Ganesha runs at all: a Ganesha starting later connects,
    /// which changes the epoch and stops the fill before its next write,
    /// and its FSAL refuses to truncate a `PARTIAL` file until its resync
    /// (which carries this fill's `FETCHING`) has ended. An FSAL that is
    /// connected and does not acknowledge fails the fence at once; the
    /// bound expiring fails it too. `abort` stops the wait (the fill was
    /// stopped).
    async fn fill_fence(
        &self,
        key: InodeKey,
        rkey: RecallKey,
        push: impl Fn(),
        abort: impl Fn() -> bool,
    ) -> Option<u64> {
        let r = self.fill_fence_wait(key, rkey, push, abort).await;
        self.fence_wait_end(rkey, r.is_some());
        r
    }

    async fn fill_fence_wait(
        &self,
        key: InodeKey,
        rkey: RecallKey,
        push: impl Fn(),
        abort: impl Fn() -> bool,
    ) -> Option<u64> {
        let wait = Duration::from_millis(self.cfg.fsal.reconnect_wait_ms);
        let t0 = self.fence_wait_start(rkey);
        let mut waiting = false;
        loop {
            if abort() || self.is_shutting_down() {
                return None;
            }
            let set = self.fsal_set().await;
            if set != FsalSet::Waiting {
                match self.fence_attempt(key, &push).await {
                    Fence::Held(e) => {
                        if waiting {
                            info!(
                                file_id = key.file_id,
                                waited_ms = t0.elapsed().as_millis() as u64,
                                "fill fence held once the FSALs reconnected"
                            );
                        }
                        return Some(e);
                    }
                    Fence::NoFsal(e) if set == FsalSet::NoGanesha => {
                        debug!(
                            file_id = key.file_id,
                            "no Ganesha runs and no FSAL is connected: the fill is fenced \
                             against NFS by the FSAL's own refusal to truncate while it \
                             reconnects"
                        );
                        return Some(e);
                    }
                    Fence::Refused => {
                        warn!(
                            file_id = key.file_id,
                            "a connected FSAL did not acknowledge the fill fence"
                        );
                        return None;
                    }
                    // Connections moved meanwhile: look again.
                    Fence::NoFsal(_) | Fence::Moving => {}
                }
            }
            if t0.elapsed() >= wait {
                let server = self.server.get();
                warn!(
                    file_id = key.file_id,
                    waited_ms = t0.elapsed().as_millis() as u64,
                    ready = server.map_or(0, |s| s.ready_connections()),
                    handshaking = server.map_or(0, |s| s.handshaking_connections()),
                    "the FSALs this edge expects did not reconnect in time: the fill fails closed"
                );
                return None;
            }
            if !waiting {
                waiting = true;
                let ganesha = self.ganesha_now().await;
                info!(
                    file_id = key.file_id,
                    ?ganesha,
                    ready = self.server.get().map_or(0, |s| s.ready_connections()),
                    "fill fence waits for the FSALs to reconnect"
                );
            }
            tokio::time::sleep(FENCE_POLL).await;
        }
    }
}

/// Where a fill's fence wait stands (one bound across its retries).
#[derive(Clone, Copy, Debug)]
pub(crate) struct FenceWait {
    started: std::time::Instant,
    last: std::time::Instant,
}

/// One fence attempt's outcome.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Fence {
    /// Every FSAL acknowledged on an unchanged epoch.
    Held(u64),
    /// No FSAL connection of any kind, on an unchanged epoch.
    NoFsal(u64),
    /// Connections changed during the attempt (or the server isn't up).
    Moving,
    /// A stable, connected FSAL did not acknowledge.
    Refused,
}

/// The FSALs a fence would be asked of, against those this edge expects.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FsalSet {
    Ready,
    NoGanesha,
    Waiting,
}

impl<C: ControlPlane, B: BlobIo> FsalNotifier for Core<C, B> {
    async fn invalidate(&self, key: InodeKey, what: u32, ack: bool) -> bool {
        let Some(server) = self.server.get() else {
            // No socket server at all: nothing is told, so nothing is acked.
            return self.cfg.fsal.no_ganesha;
        };
        let out = server.invalidate(obj_of(key), what, ack).await;
        !ack || self.acked(&out)
    }

    fn connection_epoch(&self) -> u64 {
        self.server.get().map_or(0, |s| s.connection_epoch())
    }

    async fn begin_fill(&self, key: InodeKey, version_id: u64) -> Option<u64> {
        let rkey = self.fill_key(key, version_id);
        // The FETCHING state first, then an acknowledged INVALIDATE on the
        // same connections: once acked, every FSAL holds the state and
        // refuses to truncate the inode while it lasts. No entry (it was
        // forgotten, or the fill is not one this daemon tracks): no state
        // to push, so no fence.
        let stopped = || {
            !self.fills.is_running(rkey)
                || self.fills.stopping(rkey)
                || !self.recalls.lock().unwrap().contains_key(&rkey)
        };
        if stopped() {
            return None;
        }
        // The entry as it is at each attempt (a wait may outlast changes).
        let push = || {
            let e = self.recalls.lock().unwrap().get(&rkey).cloned();
            if let Some(mut e) = e {
                e.phase = RecallPhase::Fetching;
                self.push(&e);
            }
        };
        self.fill_fence(key, rkey, push, stopped).await
    }

    fn fence_holds(&self, epoch: u64) -> bool {
        match self.server.get() {
            Some(s) => s.connection_epoch() == epoch,
            None => self.cfg.fsal.no_ganesha,
        }
    }

    fn fill_cancelled(&self, key: InodeKey, version_id: u64) -> bool {
        self.is_shutting_down() || self.fills.stopping(self.fill_key(key, version_id))
    }

    async fn begin_replace(&self, key: InodeKey, old_version: u64) -> Option<u64> {
        // FETCHING for the old version, with no recall behind it: the FSAL
        // refuses to truncate the narrowed placeholder until it ends.
        let st = RecallState {
            obj: obj_of(key),
            version_id: old_version,
            recall_id: 0,
            state: RecallPhase::Fetching,
            answer: Decision::Delay,
            eta_s: ReadDecision::ETA_UNKNOWN,
            resident_bytes: 0,
            valid_ms: self.cfg.fsal.recall_valid_ms,
        };
        self.fence(key, || {
            if let Some(s) = self.server.get() {
                s.push_recall_state(st)
            }
        })
        .await
    }

    fn end_replace(&self, key: InodeKey, old_version: u64) {
        if let Some(s) = self.server.get() {
            s.push_recall_state(RecallState {
                obj: obj_of(key),
                version_id: old_version,
                recall_id: 0,
                state: RecallPhase::Cancelled,
                answer: Decision::Eio,
                eta_s: ReadDecision::ETA_UNKNOWN,
                resident_bytes: 0,
                valid_ms: self.cfg.fsal.recall_valid_ms,
            });
        }
    }

    fn before_narrow(&self, key: InodeKey) -> std::io::Result<()> {
        use omnifile_stub::SmbGuardHook;
        self.stub_guard().before_narrow(key)
    }

    fn fill_progress(&self, key: InodeKey, version_id: u64, resident_bytes: u64) {
        let e = {
            let mut map = self.recalls.lock().unwrap();
            map.get_mut(&self.fill_key(key, version_id)).map(|e| {
                e.resident = resident_bytes;
                e.clone()
            })
        };
        if let Some(e) = e {
            self.push(&e);
        }
    }
}

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// `POPULATE_DIR` for the rewind directory `obj` names
    /// (`fsal-namespace.md` §7): 1 `DONE`, 2 `IN_PROGRESS`, 3 `EIO`. The FSAL
    /// answers `DELAY` until the marker loses `DIR_UNPOPULATED`, so a failure
    /// here leaves the directory unpopulated (never shown half-filled).
    pub async fn populate_rewind(&self, obj: omnifile_fsal_proto::wire::ObjRef) -> u8 {
        if obj.fsid != self.fsid || !self.rewind_busy.lock().unwrap().insert(obj.ino) {
            return if obj.fsid != self.fsid { 3 } else { 2 };
        }
        let r = self.populate_rewind_inner(obj).await;
        self.rewind_busy.lock().unwrap().remove(&obj.ino);
        match r {
            Ok(()) => 1,
            Err(err) => {
                warn!(%err, ino = obj.ino, "rewind directory not populated");
                3
            }
        }
    }

    /// The replica-shaped node of the rewind placeholder a read of `key` is
    /// for (the read intent's inode), resolved once and kept while the read
    /// is tracked.
    async fn rewind_node(&self, key: RecallKey) -> Option<Node> {
        if let Some(t) = self.rewind_targets.lock().unwrap().get(&key) {
            return Some(t.node.clone());
        }
        let obj = self.recalls.lock().unwrap().get(&key).map(|e| e.obj)?;
        if obj.fsid != self.fsid {
            return None;
        }
        let r = crate::rewind::resolve(
            &self.layout,
            self.control.as_ref(),
            obj.ino,
            obj.ino_gen,
            key.0,
            key.1,
        )
        .await;
        match r {
            Ok(Some(t)) => {
                let node = t.node.clone();
                if let Some(dir) = t.rel.parent() {
                    self.rewind_used
                        .lock()
                        .unwrap()
                        .insert(dir.to_owned(), std::time::Instant::now());
                }
                self.rewind_targets.lock().unwrap().insert(key, t);
                Some(node)
            }
            Ok(None) => None,
            Err(err) => {
                warn!(%err, file_id = key.0, version_id = key.1, "rewind placeholder");
                None
            }
        }
    }

    /// Where `node` is on disk: its replica path, or a rewind placeholder's.
    /// Whether the placeholder at `rel` holds every byte of this version.
    async fn resident_at(&self, rel: &std::path::Path, file_id: u64, version_id: u64) -> bool {
        let (root, rel) = (self.layout.data_root.clone(), rel.to_path_buf());
        tokio::task::spawn_blocking(move || {
            use std::os::fd::AsFd;
            omnifile_stub::sys::open_ro_beneath(&root, &rel)
                .ok()
                .and_then(|fd| omnifile_marker::xattr::read(fd.as_fd()).ok())
                .is_some_and(|m| {
                    matches!(m, omnifile_marker::xattr::ReadOutcome::Marker(m)
                        if m.is_fully_resident()
                            && m.file_id == file_id
                            && m.version_id == version_id)
                })
        })
        .await
        .unwrap_or(false)
    }

    async fn fill_rel(&self, node: &Node, key: RecallKey) -> Option<std::path::PathBuf> {
        if node.lid < 0 {
            return self
                .rewind_targets
                .lock()
                .unwrap()
                .get(&key)
                .map(|t| t.rel.clone());
        }
        self.replica.lock().await.fs_path(node.lid).ok().flatten()
    }

    /// Create due rewind labels and drop expired ones, at most once a
    /// minute (from the feed loop).
    pub async fn refresh_rewind_labels(&self) {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        let minute = now / 60;
        let reperm = self
            .rewind_acls_stale
            .swap(false, std::sync::atomic::Ordering::SeqCst);
        if self
            .rewind_minute
            .swap(minute, std::sync::atomic::Ordering::SeqCst)
            == minute
            && !reperm
        {
            return;
        }
        let perms = crate::acl::CreatePerms::new(self.identity.get().map(|i| i.idmap.clone()));
        let plans = match crate::rewind::plan_labels(&*self.replica.lock().await, &perms, now) {
            Ok(p) => p,
            Err(err) => return warn!(%err, "rewind labels"),
        };
        // Mass creates and removals: off the async workers.
        let (layout, seq, watch) = (self.layout.clone(), self.seq.clone(), self.watch_dir());
        let applied = tokio::task::spawn_blocking(move || {
            crate::rewind::apply_labels(&layout, &seq, &plans, reperm, &watch)
        })
        .await
        .map_err(std::io::Error::other);
        match applied {
            Ok(Ok((0, 0))) => {}
            Ok(Ok((created, removed))) => info!(created, removed, "rewind labels"),
            Ok(Err(err)) => warn!(%err, "rewind labels"),
            Err(err) => warn!(%err, "rewind labels"),
        }
        // After an ACL change every populated directory is emptied: what
        // it holds was made with the ACLs of before.
        let idle = if reperm {
            Duration::ZERO
        } else {
            Duration::from_secs(self.cfg.rewind.idle_evict_s)
        };
        let n = self.evict_idle_rewind(idle).await;
        if n > 0 {
            info!(directories = n, "idle rewind directories evicted");
        }
    }

    /// Empty the rewind directories unused for `idle` (a cache of history),
    /// except those a read is filling. Returns how many were emptied.
    pub async fn evict_idle_rewind(&self, idle: Duration) -> usize {
        let now = std::time::Instant::now();
        let due: Vec<std::path::PathBuf> = {
            let used = self.rewind_used.lock().unwrap();
            let busy: Vec<std::path::PathBuf> = self
                .rewind_targets
                .lock()
                .unwrap()
                .values()
                .map(|t| t.rel.clone())
                .collect();
            used.iter()
                .filter(|(dir, at)| {
                    now.duration_since(**at) >= idle && !busy.iter().any(|b| b.starts_with(dir))
                })
                .map(|(dir, _)| dir.clone())
                .collect()
        };
        let mut n = 0;
        for dir in due {
            // Hold off POPULATE_DIR for this directory while it is emptied.
            let ino = omnifile_stub::sys::open_dir_beneath(&self.layout.data_root, &dir)
                .and_then(|fd| omnifile_stub::sys::inode_key(std::os::fd::AsFd::as_fd(&fd)))
                .map(|k| k.ino);
            let Ok(ino) = ino else {
                // Gone (its label expired): nothing left to evict.
                self.rewind_used.lock().unwrap().remove(&dir);
                continue;
            };
            if !self.rewind_busy.lock().unwrap().insert(ino) {
                continue; // being populated: kept, and looked at next time
            }
            self.rewind_used.lock().unwrap().remove(&dir);
            let (layout, seq, d) = (self.layout.clone(), self.seq.clone(), dir.clone());
            let watch = self.watch_dir();
            let r = tokio::task::spawn_blocking(move || {
                crate::rewind::evict(&layout, &d, &seq, &watch)
            })
            .await
            .map_err(std::io::Error::other);
            match r {
                Ok(Ok(true)) => n += 1,
                Ok(Ok(false)) => {}
                Ok(Err(err)) | Err(err) => warn!(%err, dir = %dir.display(), "rewind eviction"),
            }
            self.rewind_busy.lock().unwrap().remove(&ino);
        }
        n
    }

    /// The smb-guard's directory mark, for rewind directories made unfilled
    /// (a no-op without a guard). A mark that can't be added is logged: the
    /// folder would then list empty over SMB until it is filled.
    pub fn watch_dir(&self) -> crate::rewind::WatchDir {
        let Some(dirs) = self.guard.get().map(|g| g.dirs.clone()) else {
            return crate::rewind::no_watch();
        };
        std::sync::Arc::new(move |fd| {
            if let Err(err) = dirs.watch(fd) {
                warn!(%err, "smb-guard mark on an unfilled rewind directory");
            }
        })
    }

    /// Mark every unfilled rewind directory (at startup, once the guard is
    /// bound: a group the daemon had to create has none of its marks).
    pub async fn watch_unfilled_rewind(&self) {
        let (layout, watch) = (self.layout.clone(), self.watch_dir());
        match tokio::task::spawn_blocking(move || crate::rewind::watch_unfilled(&layout, &watch))
            .await
        {
            Ok(Ok(0)) => {}
            Ok(Ok(n)) => info!(n, "smb-guard marks on unfilled rewind directories"),
            Ok(Err(err)) => warn!(%err, "smb-guard marks on unfilled rewind directories"),
            Err(err) => warn!(%err, "smb-guard marks on unfilled rewind directories"),
        }
    }

    async fn populate_rewind_inner(
        &self,
        obj: omnifile_fsal_proto::wire::ObjRef,
    ) -> Result<(), crate::rewind::RewindError> {
        let layout = self.layout.clone();
        let found = tokio::task::spawn_blocking(move || {
            crate::rewind::find_dir(&layout, obj.ino, obj.ino_gen)
        })
        .await
        .map_err(std::io::Error::other)??;
        let Some(rel) = found else {
            return Err(crate::rewind::RewindError::NotRewind(format!(
                "no rewind directory with inode {}",
                obj.ino
            )));
        };
        let perms = crate::acl::CreatePerms::new(self.identity.get().map(|i| i.idmap.clone()));
        crate::rewind::populate(
            &self.layout,
            &self.replica,
            &perms,
            self.control.as_ref(),
            &self.seq,
            &rel,
            &self.watch_dir(),
        )
        .await?;
        self.rewind_used
            .lock()
            .unwrap()
            .insert(rel, std::time::Instant::now());
        Ok(())
    }
}

impl<C: ControlPlane, B: BlobIo> Handler for FsalBridge<C, B> {
    async fn read_intent(&self, conn: ConnInfo, req: ReadIntent) -> ReadDecision {
        self.core.on_read_intent(conn, req)
    }

    async fn populate_dir(
        &self,
        _conn: ConnInfo,
        req: omnifile_fsal_proto::wire::PopulateDir,
    ) -> omnifile_fsal_proto::wire::PopulateResult {
        let result = self.core.populate_rewind(req.obj).await;
        omnifile_fsal_proto::wire::PopulateResult { result, eta_ms: 0 }
    }

    fn notification(&self, conn: ConnInfo, n: Notification) {
        match n {
            Notification::BadMarker(b) => {
                warn!(
                    conn = conn.conn_id,
                    ino = b.obj.ino,
                    file_id = b.obj.file_id,
                    reason = b.reason,
                    "FSAL reported a bad marker"
                )
            }
            // Who made a namespace change over NFS (`actor`, nsync).
            // SETATTR after an NFS truncate to 0 (`what` SIZE, or 0 from an
            // older FSAL): the FSAL has already removed the marker. The file
            // is now the user's: hold feed updates for it until its new
            // version is committed (`fsal-socket.md` §6.1). An owner change
            // (`what` OWNER) holds nothing.
            Notification::NsHint(h) => {
                self.core.ns_hint_nfs(&h);
                if h.op == 4 && h.target.ino != 0 && (h.what == 0 || h.what & 1 != 0) {
                    let core = self.core.clone();
                    tokio::spawn(async move {
                        let mut rep = core.replica.lock().await;
                        if let Ok(Some(n)) = rep.node_by_ino(h.target.ino, h.target.ino_gen) {
                            let _ = rep.set_local_dirty(n.lid, true);
                            debug!(lid = n.lid, file_id = ?n.file_id, "setattr over NFS: holding feed updates");
                        }
                    });
                }
            }
            other => debug!(conn = conn.conn_id, ?other, "fsal notification"),
        }
    }

    fn resync_states(&self) -> Vec<RecallState> {
        self.core.active_states()
    }

    fn connected(&self, conn: ConnInfo) {
        *self
            .core
            .fsal_pids
            .lock()
            .unwrap()
            .entry(conn.pid)
            .or_default() += 1;
    }

    fn disconnected(&self, conn: ConnInfo) {
        let mut pids = self.core.fsal_pids.lock().unwrap();
        if let Some(n) = pids.get_mut(&conn.pid) {
            *n = n.saturating_sub(1);
            if *n == 0 {
                pids.remove(&conn.pid);
            }
        }
    }
}

/// Keep the FSAL's pending entries alive: re-push active states well
/// inside `valid_ms`.
/// How often audit records go to the replica and on to the service.
const AUDIT_EVERY: Duration = Duration::from_secs(5);

/// Keep and send the audit records (`control-api.md` §4.7); a last flush on
/// shutdown, so a clean stop loses none.
pub async fn audit_loop<C: ControlPlane, B: BlobIo>(core: Arc<Core<C, B>>) {
    let mut stop = core.shutdown.subscribe();
    loop {
        tokio::select! {
            _ = tokio::time::sleep(AUDIT_EVERY) => {}
            _ = stop.changed() => {
                core.flush_audit().await;
                return;
            }
        }
        core.flush_audit().await;
    }
}

/// The heartbeat (`control-api.md` §4.7): health every `[feed] heartbeat_s`,
/// starting at once (the reconnect order begins with it), and a certificate
/// renewal whenever one is due. Without renewals the 30-day client
/// certificate would expire and the edge would need a new enrolment bundle.
pub async fn heartbeat_loop<C: ControlPlane, B: BlobIo>(core: Arc<Core<C, B>>) {
    if core.cfg.feed.heartbeat_s == 0 {
        return;
    }
    let every = Duration::from_secs(core.cfg.feed.heartbeat_s);
    let mut stop = core.shutdown.subscribe();
    loop {
        core.heartbeat_once().await;
        tokio::select! {
            _ = tokio::time::sleep(every) => {}
            _ = stop.changed() => return,
        }
    }
}

pub async fn refresh_loop<C: ControlPlane, B: BlobIo>(core: Arc<Core<C, B>>) {
    let every = Duration::from_millis((core.cfg.fsal.recall_valid_ms / 3).max(100) as u64);
    let mut stop = core.shutdown.subscribe();
    loop {
        tokio::select! {
            _ = tokio::time::sleep(every) => {}
            _ = stop.changed() => return,
        }
        core.end_orphaned_fetches();
        if let Some(s) = core.server.get() {
            for st in core.active_states() {
                s.push_recall_state(st);
            }
        }
    }
}

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// What the heartbeat reports, read now.
    pub async fn health(&self) -> Health {
        let (outbox_depth, feed_cursor) = {
            let rep = self.replica.lock().await;
            (
                rep.outbox_depth().unwrap_or(0),
                rep.cursor().ok().filter(|c| !c.is_empty()),
            )
        };
        let recalls_waiting = self
            .recalls
            .lock()
            .unwrap()
            .values()
            .filter(|e| {
                !matches!(
                    e.phase,
                    RecallPhase::Done | RecallPhase::Failed | RecallPhase::Cancelled
                )
            })
            .count() as u64;
        let guard_state = match (self.cfg.smbguard.enabled, self.guard.get().is_some()) {
            (_, true) => Some("armed".to_owned()),
            (true, false) => Some("down".to_owned()),
            (false, false) => None,
        };
        let (free, cap) = match crate::checks::volume_space(&self.layout.data_root) {
            Some((f, c)) => (Some(f), Some(c)),
            None => (None, None),
        };
        Health {
            version: env!("CARGO_PKG_VERSION").to_owned(),
            kernel: std::fs::read_to_string("/proc/sys/kernel/osrelease")
                .ok()
                .map(|s| s.trim().to_owned()),
            guard_state,
            outbox_depth,
            feed_cursor,
            fills_running: self.fills.count() as u64,
            recalls_waiting,
            data_free_bytes: free,
            data_capacity_bytes: cap,
            fsal_connections: self
                .server
                .get()
                .map_or(0, |s| s.ready_connections() as u64),
            recovery_used: recovery_used(std::path::Path::new(RECOVERY_DIR)),
            quiet_default_s: Some(self.cfg.commit.quiet_s),
            quiet_worm_default_s: Some(self.cfg.commit.worm_quiet_s),
            quiet_stale_s: Some(self.quiet.stale_for().as_secs()),
            recall_superseded: Some(1),
        }
    }

    /// One heartbeat, then a renewal if the certificate is due. Failures are
    /// logged; the next beat tries again.
    pub async fn heartbeat_once(&self) {
        let h = self.health().await;
        let renew_after = match self.control.heartbeat(&h).await {
            Ok(r) => {
                debug!(lag = ?r.feed_lag_events, min_version = %r.min_edge_version, "heartbeat");
                r.certificate_renew_after
            }
            Err(err) => {
                warn!(%err, "heartbeat failed");
                None
            }
        };
        // Due on its own clock even when the heartbeat failed (day 20 of 30).
        match self.control.renew_if_due(renew_after.as_deref()).await {
            Ok(true) => info!("client certificate renewed"),
            Ok(false) => {}
            Err(err) => warn!(%err, "client certificate renewal failed; retried next heartbeat"),
        }
        // Decision 50: the Roles Anywhere certificate from the edge CA, when
        // the wizard chose it, renews with the mTLS one (or is requested when
        // there is none yet); the signing helper then restarts to present it.
        let ra_dir = self.cfg.s3.roles_anywhere_dir.clone();
        let files = omnifile_control::RolesAnywhereFiles::new(&ra_dir);
        match self.control.renew_roles_anywhere_if_due(&ra_dir).await {
            Ok(true) => {
                info!("Roles Anywhere certificate renewed");
                // The install marks it too; again here, whoever installed it.
                if let Err(e) = files.mark_restart_pending() {
                    warn!(err = %e, "marking the signing helper's restart");
                }
            }
            Ok(false) => {}
            Err(err) => {
                warn!(%err, "Roles Anywhere certificate renewal failed; retried next heartbeat")
            }
        }
        // A restart that failed before (or never ran) is retried with backoff
        // until it works: the mark stays on disk until then.
        let argv = self.cfg.s3.helper_restart.clone();
        crate::helper_restart::restart_if_pending(
            &files,
            &self.helper_restart,
            std::time::Instant::now(),
            || async move {
                crate::shares::run_command(&argv, "restarting the AWS signing helper").await
            },
        )
        .await;
    }

    /// FETCHING is only true while a fill runs. An entry left at FETCHING
    /// with no fill (its task panicked or was dropped before its guard ran)
    /// ends with FAILED instead of being re-pushed forever.
    pub fn end_orphaned_fetches(self: &Arc<Self>) {
        // Checked and ended with the tracker locked, so a fill that is just
        // ending (and pushing its own state) is never overtaken.
        self.fills.with_running(|running| {
            let orphans: Vec<RecallKey> = self
                .recalls
                .lock()
                .unwrap()
                .iter()
                .filter(|(k, e)| e.phase == RecallPhase::Fetching && !running.contains_key(*k))
                .map(|(k, _)| *k)
                .collect();
            for key in orphans {
                warn!(
                    file_id = key.0,
                    version_id = key.1,
                    "FETCHING with no fill running; ending it"
                );
                self.end_with(
                    key,
                    Terminal {
                        phase: RecallPhase::Failed,
                        answer: Decision::Eio,
                    },
                );
            }
        });
    }
}

/// The policy byte for a share's `on_open`.
pub fn policy_of(on_open: &str) -> Policy {
    Policy::from_on_open(on_open)
}

/// Where `omnifile-setup` marks a use of the break-glass account
/// (`docs/design/setup-wizard.md`; the PAM hook creates `used`, sealing removes it).
pub const RECOVERY_DIR: &str = "/var/lib/omnifile/recovery";

/// 1 while `dir/used` exists, 0 without it, `None` without the directory (no
/// recovery account on this box).
pub fn recovery_used(dir: &std::path::Path) -> Option<u64> {
    dir.is_dir().then(|| u64::from(dir.join("used").exists()))
}

#[cfg(test)]
mod tests {
    #[test]
    fn a_full_volume_is_an_interruption_not_a_failure() {
        use std::io::Error;
        assert!(super::fill_interrupted(&FillError::Io(
            Error::from_raw_os_error(libc::ENOSPC)
        )));
        assert!(super::fill_interrupted(&FillError::Io(
            Error::from_raw_os_error(libc::EDQUOT)
        )));
        assert!(!super::fill_interrupted(&FillError::Io(
            Error::from_raw_os_error(libc::EIO)
        )));
        assert!(!super::fill_interrupted(&FillError::InodeChanged));
    }

    use super::*;

    /// A stop arriving while a fill is ending is never lost: either the
    /// ending fill sees it, or the stopper finds no fill and pushes after.
    #[test]
    fn a_late_stop_is_never_overtaken_by_the_fills_own_end() {
        let t = Arc::new(FillTracker::default());
        let key = (1, 2, 0);
        assert!(t.start(key));
        let ended = Arc::new(Mutex::new(Vec::new()));
        let (t2, e2) = (t.clone(), ended.clone());
        let ender = std::thread::spawn(move || {
            t2.finish_with(
                key,
                Terminal {
                    phase: RecallPhase::Failed,
                    answer: Decision::Eio,
                },
                |term| {
                    std::thread::sleep(Duration::from_millis(100));
                    e2.lock().unwrap().push(term.phase);
                },
            )
        });
        std::thread::sleep(Duration::from_millis(20));
        let cancel = Terminal {
            phase: RecallPhase::Cancelled,
            answer: Decision::Eio,
        };
        // The fill is ending: the stop is not taken, so the caller pushes
        // CANCELLED itself — and only after the fill's own push.
        assert!(!t.stop(key, cancel));
        ended.lock().unwrap().push(RecallPhase::Cancelled);
        ender.join().unwrap();
        assert_eq!(
            *ended.lock().unwrap(),
            vec![RecallPhase::Failed, RecallPhase::Cancelled],
            "the last state the FSAL sees is CANCELLED"
        );
    }

    mod fence {
        use super::super::*;
        use crate::fakes::FakeService;
        use omnifile_fsal_proto::ServerConfig;
        use omnifile_fsal_proto::testing::{FakeFsal, pair};
        use omnifile_fsal_proto::wire::{INVALIDATE_ACK_REQUIRED, Message};
        use std::time::Instant;

        type TestCore = Arc<Core<FakeService, FakeService>>;
        type TestServer = Server<FsalBridge<FakeService, FakeService>>;

        const GANESHA: i32 = 4242;
        const KEY: RecallKey = (7, 70, 0);
        const INODE: InodeKey = InodeKey {
            fsid: 1,
            ino: 11,
            ino_gen: 1,
            file_id: 7,
        };

        /// A core with a fill of `KEY` running, `probe` saying which
        /// Ganesha processes run, and no socket server yet (a restarted
        /// daemon takes over the guard's held reads before it binds one).
        fn core(dir: &std::path::Path, wait_ms: u64, probe: Option<HashSet<i32>>) -> TestCore {
            let toml = format!(
                r#"
                data_root = "{d}/data"
                replica_path = "{d}/replica.db"
                socket_path = "{d}/fsal.sock"
                edge_id = 7
                backend = "fake"
                [fsal]
                required_exe = ""
                invalidate_timeout_ms = 300
                reconnect_wait_ms = {wait_ms}
                "#,
                d = dir.display()
            );
            std::fs::create_dir_all(dir.join("data")).unwrap();
            let cfg = Config::from_toml(&toml, std::path::Path::new("t.toml")).unwrap();
            let replica = Replica::open(&cfg.replica_path).unwrap();
            let svc = Arc::new(FakeService::new());
            let core = Core::new(cfg, 1, replica, svc.clone(), svc);
            let _ = core
                .fsal_probe
                .set(Arc::new(move || probe.clone()) as FsalProbe);
            assert!(core.fills.start(KEY));
            core.recalls.lock().unwrap().insert(
                KEY,
                RecallEntry {
                    obj: obj_of(INODE),
                    version_id: 70,
                    recall_id: 0,
                    phase: RecallPhase::Fetching,
                    answer: Decision::Delay,
                    eta_s: ReadDecision::ETA_UNKNOWN,
                    resident: 0,
                    online: true,
                    requesters: HashSet::new(),
                },
            );
            core
        }

        fn bind(core: &TestCore) -> TestServer {
            let s = Server::new(
                ServerConfig {
                    required_exe: None,
                    invalidate_timeout: Duration::from_millis(300),
                    idle_timeout: Duration::from_secs(60),
                    ..ServerConfig::default()
                },
                FsalBridge { core: core.clone() },
            );
            let _ = core.server.set(s.clone());
            s
        }

        /// An FSAL of Ganesha `pid` connects, handshakes and then answers
        /// every acknowledged `INVALIDATE` (or none, with `ack` false).
        async fn connect(s: &TestServer, pid: i32, ack: bool) {
            let (a, b) = pair();
            let srv = s.clone();
            tokio::spawn(async move { srv.serve(a, pid, 0, None).await });
            let mut f = FakeFsal::new(b);
            f.handshake().await;
            tokio::spawn(async move {
                while let Some(fr) = f.recv_timeout(Duration::from_secs(30)).await {
                    if let Message::Invalidate(i) = fr.msg
                        && ack
                        && i.flags & INVALIDATE_ACK_REQUIRED != 0
                    {
                        f.send_with_id(
                            &Message::InvalidateAck {
                                inval_seq: i.inval_seq,
                            },
                            fr.header.req_id,
                        )
                        .await;
                    }
                }
            });
        }

        /// M2 step 10: the restarted daemon fences the taken-over fill
        /// before Ganesha's FSAL is back. It waits for it, then fences.
        #[tokio::test]
        async fn a_fill_waits_for_the_fsal_to_reconnect_after_a_restart() {
            let dir = tempfile::tempdir().unwrap();
            let core = core(dir.path(), 10_000, Some(HashSet::from([GANESHA])));
            let t0 = Instant::now();
            let c = core.clone();
            let fill = tokio::spawn(async move { c.begin_fill(INODE, 70).await });
            tokio::time::sleep(Duration::from_millis(200)).await;
            let s = bind(&core);
            tokio::time::sleep(Duration::from_millis(300)).await;
            assert!(!fill.is_finished(), "no fence on nobody's word");
            connect(&s, GANESHA, true).await;
            let epoch = tokio::time::timeout(Duration::from_secs(5), fill)
                .await
                .expect("the fence ends once the FSAL is back")
                .unwrap()
                .expect("fenced by the reconnected FSAL");
            assert!(t0.elapsed() >= Duration::from_millis(500));
            assert!(core.fence_holds(epoch));
            assert_eq!(s.pending_acks(), 0);
            // The fence still breaks on the next connection change.
            connect(&s, GANESHA + 1, true).await;
            assert!(!core.fence_holds(epoch));
        }

        /// No FSAL comes back within the bound: the fill fails closed.
        #[tokio::test]
        async fn a_fill_fails_closed_when_the_fsal_does_not_reconnect_in_time() {
            let dir = tempfile::tempdir().unwrap();
            let core = core(dir.path(), 700, Some(HashSet::from([GANESHA])));
            bind(&core);
            let t0 = Instant::now();
            assert_eq!(core.begin_fill(INODE, 70).await, None);
            let took = t0.elapsed();
            assert!(
                took >= Duration::from_millis(700),
                "waited the bound: {took:?}"
            );
            assert!(took < Duration::from_secs(5), "and no longer: {took:?}");
        }

        /// A fill that retries its broken fence (as `run_fill` does) keeps
        /// one bound: the retries after it fail at once, not after another
        /// full wait each. A wait that ended long ago does not count.
        #[tokio::test]
        async fn retries_of_a_fence_share_one_bound() {
            let dir = tempfile::tempdir().unwrap();
            let core = core(dir.path(), 700, Some(HashSet::from([GANESHA])));
            bind(&core);
            let t0 = Instant::now();
            assert_eq!(core.begin_fill(INODE, 70).await, None);
            assert!(t0.elapsed() >= Duration::from_millis(700));
            for _ in 0..5 {
                tokio::time::sleep(Duration::from_millis(200)).await;
                let t = Instant::now();
                assert_eq!(core.begin_fill(INODE, 70).await, None);
                assert!(
                    t.elapsed() < Duration::from_millis(300),
                    "a retry does not wait the bound again: {:?}",
                    t.elapsed()
                );
            }
            // After FENCE_EPISODE_GAP (forgotten here), a new wait waits
            // the bound again.
            core.fence_waits.lock().unwrap().clear();
            let t = Instant::now();
            assert_eq!(core.begin_fill(INODE, 70).await, None);
            assert!(t.elapsed() >= Duration::from_millis(700));
        }

        /// Which Ganesha runs can't be told, and nothing is connected: never
        /// "no Ganesha". Waits the bound, then fails closed.
        #[tokio::test]
        async fn an_unknown_ganesha_set_with_no_fsal_fails_closed() {
            let dir = tempfile::tempdir().unwrap();
            let core = core(dir.path(), 400, None);
            bind(&core);
            let t0 = Instant::now();
            assert_eq!(core.begin_fill(INODE, 70).await, None);
            assert!(t0.elapsed() >= Duration::from_millis(400));
        }

        /// Ganesha isn't running: zero connections is legitimate, and the
        /// fill is fenced at once. An FSAL connecting later breaks it.
        #[tokio::test]
        async fn a_fill_is_fenced_with_no_fsal_when_no_ganesha_runs() {
            let dir = tempfile::tempdir().unwrap();
            let core = core(dir.path(), 10_000, Some(HashSet::new()));
            let s = bind(&core);
            let t0 = Instant::now();
            let epoch = core
                .begin_fill(INODE, 70)
                .await
                .expect("no Ganesha: nothing to wait for");
            assert!(t0.elapsed() < Duration::from_secs(2));
            assert!(core.fence_holds(epoch));
            connect(&s, GANESHA, true).await;
            assert!(
                !core.fence_holds(epoch),
                "a Ganesha starting later stops the fill"
            );
        }

        /// A reconnected FSAL that does not acknowledge refuses the fence:
        /// the fill fails at once, not after the bound.
        #[tokio::test]
        async fn a_connected_fsal_that_does_not_ack_fails_the_fence_at_once() {
            let dir = tempfile::tempdir().unwrap();
            let core = core(dir.path(), 10_000, Some(HashSet::from([GANESHA])));
            let s = bind(&core);
            connect(&s, GANESHA, false).await;
            let t0 = Instant::now();
            assert_eq!(core.begin_fill(INODE, 70).await, None);
            assert!(t0.elapsed() < Duration::from_secs(3));
        }

        /// A fill stopped while its fence waits stops waiting.
        #[tokio::test]
        async fn a_stopped_fill_stops_waiting_for_the_fsal() {
            let dir = tempfile::tempdir().unwrap();
            let core = core(dir.path(), 10_000, Some(HashSet::from([GANESHA])));
            bind(&core);
            let c = core.clone();
            let fill = tokio::spawn(async move { c.begin_fill(INODE, 70).await });
            tokio::time::sleep(Duration::from_millis(200)).await;
            core.recalls.lock().unwrap().remove(&KEY);
            let r = tokio::time::timeout(Duration::from_secs(2), fill)
                .await
                .expect("stops within a poll")
                .unwrap();
            assert_eq!(r, None);
        }

        #[test]
        fn ganesha_is_found_by_its_binary() {
            assert_eq!(
                ganesha_pids("ganesha.nfsd"),
                None,
                "a relative path matches nothing"
            );
            let me = std::env::current_exe().unwrap();
            // SAFETY: geteuid has no preconditions.
            if unsafe { libc::geteuid() } != 0 {
                // Other users' binaries may not be readable: unknown then,
                // never a set holding a process that isn't the binary.
                let r = ganesha_pids("/nonexistent/ganesha.nfsd");
                assert!(r.as_ref().is_none_or(|s| s.is_empty()), "{r:?}");
                return;
            }
            let pids = ganesha_pids(me.to_str().unwrap()).expect("root reads /proc");
            assert!(pids.contains(&(std::process::id() as i32)));
            let none = ganesha_pids("/nonexistent/ganesha.nfsd").unwrap();
            assert!(none.is_empty(), "no such Ganesha runs");
        }
    }

    #[test]
    fn answers_follow_on_open() {
        assert_eq!(
            answer_for("rehydrating", "wait", None, None),
            Decision::Delay
        );
        assert_eq!(answer_for("rehydrating", "fail", None, None), Decision::Eio);
        assert_eq!(
            answer_for("rehydrating", "auto", None, Some(10)),
            Decision::Eio,
            "no ETA: too slow"
        );
        assert_eq!(
            answer_for("rehydrating", "auto", Some(60), Some(10)),
            Decision::Delay
        );
        assert_eq!(answer_for("failed", "wait", None, None), Decision::Eio);
        assert_eq!(policy_of("auto"), Policy::WaitQuick);
    }
}
