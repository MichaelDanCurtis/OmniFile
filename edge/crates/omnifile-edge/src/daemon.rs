//! Wiring and lifecycle: replica, FSAL socket server, feed, stubber,
//! commit pipeline, recall refresh; and a shutdown that leaves every FSAL
//! disconnected (failing closed) rather than half-served.

use std::sync::Arc;
use std::time::Duration;

use omnifile_fsal_proto::socket::Listener;
use omnifile_fsal_proto::{Server, ServerConfig, wire};
use omnifile_replica::Replica;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tracing::{debug, info, warn};

use crate::checks::Volume;
use crate::config::Config;
use crate::core::{Core, FsalBridge, refresh_loop};
use crate::ports::{BlobIo, CloseWrite, ControlPlane};
use crate::{commit, feed_loop, stubber};

#[derive(Debug, thiserror::Error)]
pub enum StartError {
    #[error("replica {path}: {err}")]
    Replica {
        path: String,
        err: omnifile_replica::Error,
    },
    #[error("fsal socket {path}: {err}")]
    Socket { path: String, err: std::io::Error },
}

pub struct Daemon<C, B> {
    pub core: Arc<Core<C, B>>,
    pub server: Server<FsalBridge<C, B>>,
    /// Finished writes for the commit pipeline (detector, tests).
    pub commits: mpsc::Sender<CloseWrite>,
    tasks: Vec<JoinHandle<()>>,
    socket_task: JoinHandle<std::io::Result<()>>,
}

pub fn server_config(cfg: &Config) -> ServerConfig {
    let mut features = wire::feature::PARK
        | wire::feature::WRITE_CLOSED
        | wire::feature::NS_HINT
        | wire::feature::OPEN_STATE;
    if cfg.cache.enabled {
        // M5 (socket v0.2): FSALs read chunk maps; ONLINE reads are fetched
        // chunk by chunk (`remote`). Negotiated only with a minor-2 FSAL.
        features |= wire::feature::CHUNK_MAP;
    }
    ServerConfig {
        features,
        park_ms_max: cfg.fsal.park_ms_max,
        answer_ttl_ms: cfg.fsal.answer_ttl_ms,
        handler_timeout: Duration::from_millis(cfg.fsal.handler_timeout_ms),
        invalidate_timeout: Duration::from_millis(cfg.fsal.invalidate_timeout_ms),
        allowed_uids: cfg.fsal.allowed_uids.clone(),
        required_exe: (!cfg.fsal.required_exe.is_empty()).then(|| cfg.fsal.required_exe.clone()),
        ..ServerConfig::default()
    }
}

impl<C: ControlPlane, B: BlobIo> Daemon<C, B> {
    /// Open the replica, bind the FSAL socket and start every task.
    /// The startup checks must already have passed.
    pub async fn start(
        cfg: Config,
        vol: Volume,
        control: Arc<C>,
        blob: Arc<B>,
    ) -> Result<Self, StartError> {
        let mut replica = Replica::open(&cfg.replica_path).map_err(|err| StartError::Replica {
            path: cfg.replica_path.display().to_string(),
            err,
        })?;
        if cfg.edge_id != 0 && replica.edge_id() != cfg.edge_id {
            replica
                .set_edge_id(cfg.edge_id)
                .map_err(|err| StartError::Replica {
                    path: cfg.replica_path.display().to_string(),
                    err,
                })?;
        }
        let socket_path = cfg.socket_path.clone();
        let commit_enabled = cfg.commit.enabled;
        let scfg = server_config(&cfg);
        let core = Core::new(cfg, vol.fsid, replica, control, blob);
        // M5: chunk-range reads of ONLINE placeholders (`[cache]`).
        crate::remote::install_cache(&core);
        // Recalls in flight before a restart: rebuild the FSAL-facing table,
        // and restart fills for those already downloading.
        let active = core
            .replica
            .lock()
            .await
            .active_recalls()
            .unwrap_or_default();
        core.on_recall_rows(active).await;
        let server = Server::new(scfg, FsalBridge { core: core.clone() });
        let _ = core.server.set(server.clone());

        let listener = Listener::bind(&socket_path).map_err(|err| StartError::Socket {
            path: socket_path.display().to_string(),
            err,
        })?;
        info!(socket = %socket_path.display(), instance = server.config().instance, "fsal socket listening");
        let socket_task = tokio::spawn(omnifile_fsal_proto::socket::run(listener, server.clone()));

        // Before anything looks at a file: a sweep or a replayed close at
        // start must see each share's own period, not the default.
        refresh_quiet(&core).await;
        let (tx, rx) = mpsc::channel(4096);
        let _ = core.commit_tx.set(tx.clone());
        let _ = core.rt.set(tokio::runtime::Handle::current());
        let mut tasks = vec![
            tokio::spawn(feed_loop::feed_loop(core.clone())),
            tokio::spawn(feed_loop::reconcile_loop(core.clone())),
            tokio::spawn(stubber::stub_loop(core.clone())),
            tokio::spawn(refresh_loop(core.clone())),
            tokio::spawn(crate::core::audit_loop(core.clone())),
            tokio::spawn(crate::core::heartbeat_loop(core.clone())),
            tokio::spawn(quiet_loop(core.clone())),
        ];
        if core.cfg.acl.enabled {
            tasks.push(tokio::spawn(crate::acl::scan_loop(core.clone())));
        }
        if core.cfg.shares.render {
            tasks.push(tokio::spawn(crate::shares::shares_loop(core.clone())));
        }
        if core.cfg.namespace.enabled {
            tasks.push(tokio::spawn(crate::nsync::ns_loop(core.clone())));
            tasks.push(tokio::spawn(crate::nsync::watch_loop(core.clone())));
        }
        if commit_enabled {
            tasks.push(tokio::spawn(commit::commit_loop(core.clone(), rx)));
            if core.cfg.commit.detect {
                tasks.push(tokio::spawn(detect_loop(core.clone(), tx.clone())));
            }
        }
        Ok(Daemon {
            core,
            server,
            commits: tx,
            tasks,
            socket_task,
        })
    }

    /// Stop the loops, let in-flight handshakes finish, then tell every FSAL
    /// we are going (`ERROR` 5) and close. The FSAL then drops its marker
    /// cache and pending table and fails closed until a daemon is back.
    pub async fn shutdown(self) {
        info!("shutting down");
        let _ = self.core.shutdown.send(true);
        for t in self.tasks {
            if tokio::time::timeout(Duration::from_secs(10), t)
                .await
                .is_err()
            {
                warn!("a task did not stop within 10 s");
            }
        }
        // Running fills stop before their next write or widen and end their
        // FETCHING with FAILED while the FSALs are still connected (S10);
        // the recall resumes from the replica at the next start.
        if !self
            .core
            .fills
            .stop_all(
                crate::core::Terminal {
                    phase: wire::RecallPhase::Failed,
                    answer: wire::Decision::Eio,
                },
                Duration::from_secs(15),
            )
            .await
        {
            warn!(
                running = self.core.fills.count(),
                "fills did not stop within 15 s"
            );
        }
        // A stub or fill holding a file lock finishes its current step first;
        // every intermediate state is safe, so this wait is bounded.
        for _ in 0..100 {
            if self.core.locks_held() == 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        self.server.shutdown().await;
        let _ = tokio::time::timeout(Duration::from_secs(2), self.socket_task).await;
        info!("stopped");
    }
}

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// Stubs or fills in progress.
    pub fn locks_held(&self) -> usize {
        self.locks.held()
    }
}

/// How often each share's quiet period is read from the replica.
const QUIET_EVERY: Duration = Duration::from_secs(5);

/// Rebuild each share's quiet period from the replica's share rows. A failed
/// read keeps the periods as they were (`QuietPeriods::refresh`).
async fn refresh_quiet<C: ControlPlane, B: BlobIo>(core: &Arc<Core<C, B>>) {
    let rep = core.replica.lock().await;
    core.quiet
        .refresh(rep.share_rows(), &core.cfg.commit, |id| rep.dir_path(id));
}

/// Keep each share's quiet period current (decision 67): a change in the
/// portal reaches the replica through the feed, and takes effect here within
/// [`QUIET_EVERY`], with no restart.
async fn quiet_loop<C: ControlPlane, B: BlobIo>(core: Arc<Core<C, B>>) {
    let mut stop = core.shutdown.subscribe();
    loop {
        tokio::select! {
            _ = tokio::time::sleep(QUIET_EVERY) => refresh_quiet(&core).await,
            _ = stop.changed() => return,
        }
    }
}

/// Run the fanotify detector (`omnifile-detect`) and hand each quiet file to
/// the commit pipeline. The detector skips this process's own writes (fills,
/// markers, clones) and everything under `.omni/`. Its group lives in the
/// systemd fd store ([`crate::fdstore`]): closes queued while the daemon
/// restarts are read now.
async fn detect_loop<C: ControlPlane, B: BlobIo>(
    core: Arc<Core<C, B>>,
    commits: mpsc::Sender<CloseWrite>,
) {
    let mut dcfg = omnifile_detect::DetectConfig::new(core.cfg.data_root.clone());
    dcfg.quiet = Duration::from_secs(core.cfg.commit.quiet_s);
    // A file directly under the data root is in no share: the commit ignores
    // it, so the detector need not wait on it (and hold it open) at all.
    dcfg.min_depth = 2;
    let (tx, mut rx) = mpsc::channel(4096);
    let mut stop = core.shutdown.subscribe();
    let group = tokio::task::spawn_blocking(crate::fdstore::detect_group)
        .await
        .unwrap_or_default();
    // The closes a previous run had read but not committed (WP9): waited on
    // again, so an overwrite in that window is still committed.
    let (inject_tx, inject_rx) = mpsc::unbounded_channel();
    let (mut watch, not_yet) = {
        let core = core.clone();
        tokio::task::spawn_blocking(move || pending_watch(&core))
            .await
            .unwrap_or_default()
    };
    watch.inject = Some(inject_rx);
    watch.periods = Some(core.quiet.clone());
    if !not_yet.is_empty() {
        tokio::spawn(retry_pending(core.clone(), not_yet, inject_tx));
    }
    let detector = tokio::spawn(omnifile_detect::run_full(dcfg, group, watch, tx));
    loop {
        let ev = tokio::select! {
            ev = rx.recv() => match ev {
                Some(ev) => ev,
                None => break,
            },
            _ = stop.changed() => break,
        };
        // One permission model for NFS and SMB: undo what a chmod or a 0644
        // create did to `other` and the mask (access-control.md §1.1),
        // before the commit snapshots it.
        let p = ev.path.clone();
        let _ = tokio::task::spawn_blocking(move || crate::acl::normalize_path(&p)).await;
        // And report its ACL in the next on-change scan.
        core.acl_dirty.lock().unwrap().insert(ev.path.clone());
        let mut cw = CloseWrite::new(ev.path, ev.ino);
        cw.token = ev.token;
        cw.seen = Some(crate::ports::Seen {
            size: ev.size,
            mtime_ns: ev.mtime_ns,
            closed: ev.closed,
            at: std::time::Instant::now(),
        });
        // Waiting for room holds up only this hand-over (the detector keeps
        // reading and logging), and never the shutdown: a stop while the
        // commit queue is full leaves this close pending in the log, and the
        // detector drains its group at once (WP9 re-review).
        tokio::select! {
            r = commits.send(cw) => if r.is_err() { break },
            _ = stop.changed() => break,
        }
    }
    // The detector drains its group into the pending log when this goes.
    drop(rx);
    match tokio::time::timeout(Duration::from_secs(5), detector).await {
        Ok(Ok(Err(e))) => warn!(err = %e, "detector stopped with an error"),
        Err(_) => warn!("detector did not stop within 5 s"),
        _ => {}
    }
}

/// Where a pending close's file is now.
#[derive(Debug)]
pub(crate) enum Replayed {
    /// Open, reached by a path below the data root (so the detector can name
    /// it when it reports it).
    File(std::fs::File),
    /// No such inode any more (`ESTALE`), or no links left: not pending.
    Gone,
    /// Not reachable by a path yet (a disconnected dentry after a reboot or
    /// eviction, with the replica's path stale), or an error: tried again.
    NotYet(String),
}

/// Find a pending file `(ino, generation)` (WP9 review, finding 1). First by
/// the replica's path for it, checked to hold that inode: an inode opened by
/// handle when its dentry isn't cached is disconnected, and `/proc/self/fd`
/// can't name it. Then by handle, used only when it is connected below the
/// data root. Blocking.
pub(crate) fn resolve_pending<C: ControlPlane, B: BlobIo>(
    core: &Core<C, B>,
    mount: std::os::fd::BorrowedFd<'_>,
    ino: u64,
    generation: u32,
) -> Replayed {
    use omnifile_stub::sys;
    use std::os::fd::AsFd;
    let regular = |f: &std::fs::File| {
        sys::fstat(f.as_fd()).is_ok_and(|st| st.st_mode & libc::S_IFMT == libc::S_IFREG)
    };
    let rel = {
        let rep = core.replica.blocking_lock();
        rep.node_by_ino(ino, generation)
            .ok()
            .flatten()
            .filter(|n| !n.deleted)
            .and_then(|n| rep.fs_path(n.lid).ok().flatten())
    };
    if let Some(rel) = rel
        && let Ok(fd) = sys::open_ro_beneath(&core.cfg.data_root, &rel)
    {
        let f = std::fs::File::from(fd);
        if sys::inode_key(f.as_fd()).is_ok_and(|k| (k.ino, k.ino_gen) == (ino, generation))
            && regular(&f)
        {
            return Replayed::File(f);
        }
    }
    let f =
        match crate::nsync::open_inode(mount, ino, generation, libc::O_RDONLY | libc::O_NONBLOCK) {
            Ok(fd) => std::fs::File::from(fd),
            Err(e) if e.raw_os_error() == Some(libc::ESTALE) => return Replayed::Gone,
            Err(e) => return Replayed::NotYet(e.to_string()),
        };
    match sys::fstat(f.as_fd()) {
        Ok(st) if st.st_nlink == 0 => return Replayed::Gone,
        Ok(st) if st.st_mode & libc::S_IFMT != libc::S_IFREG => return Replayed::Gone,
        Ok(_) => {}
        Err(e) => return Replayed::NotYet(e.to_string()),
    }
    let at = std::fs::read_link(format!(
        "/proc/self/fd/{}",
        std::os::fd::AsRawFd::as_raw_fd(&f)
    ));
    match at {
        Ok(p)
            if p.starts_with(&core.cfg.data_root)
                && !p.starts_with(core.cfg.data_root.join(".omni")) =>
        {
            Replayed::File(f)
        }
        Ok(p) => Replayed::NotYet(format!(
            "not reachable by a path below the data root ({})",
            p.display()
        )),
        Err(e) => Replayed::NotYet(e.to_string()),
    }
}

/// A pending close: its token, and its file's `(ino, generation)`.
type PendingEntry = (u64, (u64, u32));

/// Open the log of pending closes, and find the files a previous run was
/// still waiting on (WP9). Returns the detector's start and the ones not
/// reachable yet. A file that is gone is dropped from the log; nothing else
/// is. Without a log the detector still runs, as before.
fn pending_watch<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
) -> (omnifile_detect::Watch, Vec<PendingEntry>) {
    use crate::pending::PendingCloses;
    use std::os::fd::AsFd;
    let path = PendingCloses::path_for(&core.cfg.replica_path);
    let (log, pending) = match PendingCloses::open(&path) {
        Ok(v) => v,
        Err(e) => {
            warn!(path = %path.display(), err = %e,
                  "pending-close log unavailable: a restart within the quiet period can lose an overwrite");
            return Default::default();
        }
    };
    let log = Arc::new(log);
    let _ = core.pending.set(log.clone());
    let mut initial = Vec::new();
    let mut not_yet = Vec::new();
    if !pending.is_empty() {
        match omnifile_stub::sys::open(&core.cfg.data_root, libc::O_RDONLY | libc::O_DIRECTORY, 0) {
            Ok(mount) => {
                for (token, (ino, generation)) in pending {
                    match resolve_pending(core, mount.as_fd(), ino, generation) {
                        Replayed::File(f) => initial.push((f, token)),
                        Replayed::Gone => log.done(token),
                        Replayed::NotYet(why) => {
                            debug!(ino, why, "a pending close's file: looked for again");
                            not_yet.push((token, (ino, generation)));
                        }
                    }
                }
            }
            Err(e) => {
                warn!(err = %e, "pending-close log: can't open the data root; looked for again");
                // Nothing is dropped.
                return (
                    omnifile_detect::Watch {
                        log: Some(log),
                        ..Default::default()
                    },
                    pending,
                );
            }
        }
        info!(
            files = initial.len(),
            later = not_yet.len(),
            "closes read before the last stop and not yet committed: waiting on them again"
        );
    }
    (
        omnifile_detect::Watch {
            initial,
            log: Some(log),
            inject: None,
            periods: None,
        },
        not_yet,
    )
}

/// Pending closes whose file couldn't be reached at start (after a reboot,
/// a file renamed while the daemon was down is found once the namespace
/// sweep has moved its node): looked for again with a backoff, 5 s doubling
/// to 5 min, for about an hour. One still not found is left pending, with a
/// warning, and the next start looks again: it is never dropped silently.
async fn retry_pending<C: ControlPlane, B: BlobIo>(
    core: Arc<Core<C, B>>,
    mut left: Vec<PendingEntry>,
    inject: mpsc::UnboundedSender<(std::fs::File, u64)>,
) {
    let mut stop = core.shutdown.subscribe();
    let mut after = Duration::from_secs(5);
    for _ in 0..16 {
        tokio::select! {
            _ = tokio::time::sleep(after) => {}
            _ = stop.changed() => return,
        }
        after = (after * 2).min(Duration::from_secs(300));
        let c = core.clone();
        let batch = std::mem::take(&mut left);
        let looked = tokio::task::spawn_blocking(move || {
            let mount = match omnifile_stub::sys::open(
                &c.cfg.data_root,
                libc::O_RDONLY | libc::O_DIRECTORY,
                0,
            ) {
                Ok(m) => m,
                Err(e) => {
                    return batch
                        .into_iter()
                        .map(|p| (p, Replayed::NotYet(e.to_string())))
                        .collect::<Vec<_>>();
                }
            };
            batch
                .into_iter()
                .map(|(t, (ino, g))| {
                    let r = resolve_pending(&c, std::os::fd::AsFd::as_fd(&mount), ino, g);
                    ((t, (ino, g)), r)
                })
                .collect()
        })
        .await
        .unwrap_or_default();
        for ((token, key), r) in looked {
            match r {
                Replayed::File(f) => {
                    info!(
                        ino = key.0,
                        "a pending close's file found: waiting on it again"
                    );
                    if inject.send((f, token)).is_err() {
                        return;
                    }
                }
                Replayed::Gone => {
                    if let Some(log) = core.pending.get() {
                        log.done(token);
                    }
                }
                Replayed::NotYet(_) => left.push((token, key)),
            }
        }
        if left.is_empty() {
            return;
        }
    }
    for (token, (ino, g)) in left {
        warn!(
            token,
            ino,
            generation = g,
            "a pending close's file can't be reached by a path: left pending for the next start"
        );
    }
}
