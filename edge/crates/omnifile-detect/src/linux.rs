//! fanotify backend (Linux).

use std::collections::VecDeque;
use std::fs::File;
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::fs::MetadataExt;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use nix::errno::Errno;
use nix::fcntl::{AT_FDCWD, OFlag};
use nix::sys::fanotify::{EventFFlags, Fanotify, InitFlags, MarkFlags, MaskFlags};
use tokio::io::Interest;
use tokio::io::unix::AsyncFd;
use tokio::sync::mpsc::Sender;

/// Files written and not yet closed whose writers are remembered.
const WRITERS_MAX: usize = 100_000;

use crate::{Activity, CloseWriteEvent, DetectConfig, DetectError, MarkScope, QuietTracker, Watch};

fn err(what: &'static str, e: Errno) -> DetectError {
    DetectError::Fanotify {
        what,
        source: std::io::Error::from(e),
    }
}

/// The current path of an open file, or `None` if it was deleted.
fn path_of(file: &File) -> Option<PathBuf> {
    let p = std::fs::read_link(format!("/proc/self/fd/{}", file.as_raw_fd())).ok()?;
    let s = p.to_str()?;
    if s.ends_with(" (deleted)") {
        return None;
    }
    Some(p)
}

fn mtime_ns(m: &std::fs::Metadata) -> i64 {
    m.mtime()
        .saturating_mul(1_000_000_000)
        .saturating_add(m.mtime_nsec())
}

struct Held {
    file: File,
    /// mtime when last observed: a change at expiry means unseen writes.
    mtime_ns: i64,
    /// From the pending log, when there is one.
    token: Option<u64>,
    /// The period it is waiting out.
    quiet: Duration,
}

/// A new notification group for the detector: `FAN_CLASS_NOTIF` with an
/// unlimited queue, so nothing is dropped while no one reads it (a daemon
/// restart, with the group kept in the systemd fd store).
pub fn create_group() -> Result<OwnedFd, DetectError> {
    let fan = Fanotify::init(
        InitFlags::FAN_CLASS_NOTIF
            | InitFlags::FAN_CLOEXEC
            | InitFlags::FAN_NONBLOCK
            | InitFlags::FAN_UNLIMITED_QUEUE,
        EventFFlags::from_bits_truncate(
            (OFlag::O_RDONLY | OFlag::O_CLOEXEC | OFlag::O_LARGEFILE).bits() as u32,
        ),
    )
    .map_err(|e| err("init (needs CAP_SYS_ADMIN)", e))?;
    Ok(OwnedFd::from(fan))
}

/// Whether `fd` is a group like the ones [`create_group`] makes: a fanotify
/// notification group with an unlimited queue. Anything else systemd hands
/// back under the detector's name is not used.
pub fn usable_group(fd: &OwnedFd) -> bool {
    let n = fd.as_raw_fd();
    let link = std::fs::read_link(format!("/proc/self/fd/{n}")).unwrap_or_default();
    let info = std::fs::read_to_string(format!("/proc/self/fdinfo/{n}")).unwrap_or_default();
    link.as_os_str() == "anon_inode:[fanotify]"
        && crate::fdinfo_flags(&info).is_some_and(crate::flags_usable)
}

pub(crate) async fn run(
    cfg: DetectConfig,
    group: Option<OwnedFd>,
    watch: Watch,
    tx: Sender<CloseWriteEvent>,
) -> Result<(), DetectError> {
    let Watch {
        initial,
        log,
        inject,
        periods,
    } = watch;
    let group = match group {
        Some(g) => g,
        None => create_group()?,
    };
    // A group from the fd store keeps the flags it was made with; reads must
    // never block the runtime whatever they were.
    // SAFETY: fcntl on a descriptor we own.
    unsafe {
        let fl = libc::fcntl(group.as_raw_fd(), libc::F_GETFL);
        if fl >= 0 && fl & libc::O_NONBLOCK == 0 {
            libc::fcntl(group.as_raw_fd(), libc::F_SETFL, fl | libc::O_NONBLOCK);
        }
    }
    // SAFETY: a fanotify group, made here or checked with `usable_group`.
    let fan = unsafe { Fanotify::from_owned_fd(group) };
    let mut mask = MaskFlags::FAN_CLOSE_WRITE;
    if cfg.watch_modify {
        mask |= MaskFlags::FAN_MODIFY;
    }
    let scope = match cfg.scope {
        MarkScope::Filesystem => MarkFlags::FAN_MARK_FILESYSTEM,
        MarkScope::Mount => MarkFlags::FAN_MARK_MOUNT,
    };
    // FAN_MARK_ADD is idempotent: a group from the fd store keeps its mark.
    fan.mark(
        MarkFlags::FAN_MARK_ADD | scope,
        mask,
        AT_FDCWD,
        Some(cfg.root.as_path()),
    )
    .map_err(|e| err("mark", e))?;
    // SAFETY: `fan` owns its descriptor for as long as the AsyncFd lives, and
    // `as_raw_fd` always returns that one.
    let afd = unsafe { AsyncFd::register_with_interest(fan, Interest::READABLE) }.map_err(|e| {
        DetectError::Fanotify {
            what: "register with tokio",
            source: e.into_parts().1,
        }
    })?;
    tracing::info!(root = %cfg.root.display(), quiet_ms = cfg.quiet.as_millis() as u64, "write detector armed");

    let mut st = State {
        cfg: &cfg,
        me: std::process::id() as i32,
        writers: Default::default(),
        tracker: QuietTracker::new(cfg.quiet),
        log,
        generation: periods.as_ref().map_or(0, |p| p.generation()),
        periods,
        gone_queue: Vec::new(),
        gone_at: Instant::now(),
    };
    let mut ticker = tokio::time::interval(cfg.tick());
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    // What a previous run was waiting on: a quiet period again, then reported
    // as if its close had just been seen.
    for (file, token) in initial {
        st.adopt(file, token);
    }
    let mut inject = inject;
    // Reported files waiting for room in `tx`. Never awaited in the loop: a
    // commit backlog must not stop the reading (WP9 review), or closes would
    // sit only in the kernel queue, which a reboot loses. Each is in the
    // pending log already, so this queue need not be durable.
    let mut out = Out::default();

    loop {
        tokio::select! {
            guard = afd.readable() => {
                let mut guard = guard.map_err(|e| DetectError::Fanotify { what: "poll", source: e })?;
                match guard.get_inner().read_events() {
                    Ok(events) => st.take(events),
                    Err(Errno::EAGAIN) => guard.clear_ready(),
                    Err(e) => return Err(err("read", e)),
                }
            }
            _ = ticker.tick() => st.expire(&mut out),
            Some((file, token)) = async {
                match inject.as_mut() {
                    Some(r) => r.recv().await,
                    None => std::future::pending().await,
                }
            } => st.adopt(file, token),
            permit = tx.reserve(), if !out.is_empty() => match permit {
                Ok(p) => {
                    if let Some(ev) = out.pop() {
                        p.send(ev);
                    }
                }
                Err(_) => return drain(&afd, &mut st),
            },
            _ = tx.closed() => return drain(&afd, &mut st),
        }
    }
}

/// Stopping: read what the kernel still queues, to the end, and record it in
/// the pending log, so a reboot before the next start loses none of it.
fn drain(afd: &AsyncFd<Fanotify>, st: &mut State<'_>) -> Result<(), DetectError> {
    loop {
        match afd.get_ref().read_events() {
            Ok(events) if !events.is_empty() => st.take(events),
            Ok(_) | Err(Errno::EAGAIN) => break,
            Err(e) => return Err(err("read", e)),
        }
    }
    if let Some(l) = &st.log {
        l.sync();
    }
    Ok(())
}

/// Reported files not yet handed over, oldest first, at most one per file:
/// a file reported again before its last report went out replaces it (the
/// newest path, size and token), so the queue is bounded by the number of
/// files written, not by how often (WP9 review). Every one is in the
/// pending log already.
#[derive(Default)]
struct Out {
    order: VecDeque<(u64, u64)>,
    by: std::collections::HashMap<(u64, u64), CloseWriteEvent>,
}

impl Out {
    fn push(&mut self, ev: CloseWriteEvent) {
        let key = (ev.dev, ev.ino);
        if self.by.insert(key, ev).is_none() {
            self.order.push_back(key);
        }
    }

    fn pop(&mut self) -> Option<CloseWriteEvent> {
        while let Some(k) = self.order.pop_front() {
            if let Some(ev) = self.by.remove(&k) {
                return Some(ev);
            }
        }
        None
    }

    fn is_empty(&self) -> bool {
        self.by.is_empty()
    }
}

struct State<'a> {
    cfg: &'a DetectConfig,
    me: i32,
    /// (dev, ino) -> whether anyone but us wrote it since its last close.
    writers: std::collections::HashMap<(u64, u64), bool>,
    tracker: QuietTracker<(u64, u64), Held>,
    log: Option<std::sync::Arc<dyn crate::PendingLog>>,
    /// Each file's period (decision 67), and the generation last applied.
    periods: Option<std::sync::Arc<dyn crate::QuietSource>>,
    generation: u64,
    /// Waiting files still to be checked for deletion in the pass under way,
    /// and when that pass began ([`State::let_go_of_deleted`]).
    gone_queue: Vec<(u64, u64)>,
    gone_at: Instant,
}

impl State<'_> {
    /// The period for a file at `path`.
    fn quiet_for(&self, path: &std::path::Path) -> Duration {
        self.periods
            .as_ref()
            .map_or(self.cfg.quiet, |p| p.quiet_of(path))
    }

    /// A period changed: the files being waited on take the new one, measured
    /// from their last write.
    fn apply_new_periods(&mut self) {
        let Some(periods) = self.periods.clone() else {
            return;
        };
        let generation = periods.generation();
        if generation == self.generation {
            return;
        }
        self.generation = generation;
        self.tracker.requiet(|held| {
            if let Some(path) = path_of(&held.file) {
                held.quiet = periods.quiet_of(&path);
            }
            held.quiet
        });
    }

    /// A file a previous run was waiting on, opened by the daemon.
    fn adopt(&mut self, file: File, token: u64) {
        let Ok(meta) = file.metadata() else {
            self.finished(token, "unreadable");
            return;
        };
        if !meta.file_type().is_file() {
            self.finished(token, "not a regular file");
            return;
        }
        let quiet = path_of(&file).map_or(self.cfg.quiet, |p| self.quiet_for(&p));
        let held = Held {
            mtime_ns: mtime_ns(&meta),
            file,
            token: Some(token),
            quiet,
        };
        self.tracker.observe_for(
            (meta.dev(), meta.ino()),
            Activity::CloseWrite,
            held,
            Instant::now(),
            quiet,
        );
    }

    /// A tokened file the detector won't report: the log hears of it, so it
    /// doesn't stay pending.
    fn finished(&self, token: u64, why: &'static str) {
        tracing::info!(token, why, "a waited-on file is not reported");
        if let Some(l) = &self.log {
            l.finished(token);
        }
    }

    fn take(&mut self, events: Vec<nix::sys::fanotify::FanotifyEvent>) {
        let now = Instant::now();
        let mut logged = false;
        for ev in events {
            let mask = ev.mask();
            if mask.contains(MaskFlags::FAN_Q_OVERFLOW) {
                tracing::error!(
                    "fanotify queue overflow: writes may be missed until reconciliation"
                );
                continue;
            }
            let own = ev.pid() == self.me;
            if own && !self.cfg.watch_modify {
                continue;
            }
            let Some(fd) = ev.fd() else { continue };
            let Ok(owned): Result<OwnedFd, _> = fd.try_clone_to_owned() else {
                continue;
            };
            let file = File::from(owned);
            let Ok(meta) = file.metadata() else { continue };
            if !meta.file_type().is_file() {
                continue;
            }
            // A file the daemon wrote can be closed last by another process:
            // a child it spawned (wbinfo) inherits every open fd between fork
            // and exec, and that CLOSE_WRITE carries the child's pid. With
            // FAN_MODIFY on, a close is the daemon's own when only the daemon
            // wrote since the last one.
            let id = (meta.dev(), meta.ino());
            if mask.contains(MaskFlags::FAN_MODIFY) {
                if self.writers.len() >= WRITERS_MAX && !self.writers.contains_key(&id) {
                    self.writers.clear();
                }
                let foreign = self.writers.entry(id).or_insert(false);
                *foreign |= !own;
            }
            if mask.contains(MaskFlags::FAN_CLOSE_WRITE) && self.writers.remove(&id) == Some(false)
            {
                continue;
            }
            if own {
                continue;
            }
            let Some(path) = path_of(&file) else { continue };
            if !self.cfg.wants(&path) {
                continue;
            }
            let activity = if mask.contains(MaskFlags::FAN_CLOSE_WRITE) {
                Activity::CloseWrite
            } else {
                Activity::Modify
            };
            // Recorded before anything else is done with the event: from
            // here on a restart replays it.
            let token = match self.tracker.get(&id).and_then(|h| h.token) {
                Some(t) => Some(t),
                None => self.log.as_ref().map(|l| {
                    logged = true;
                    l.observed(&file)
                }),
            };
            let quiet = self.quiet_for(&path);
            let held = Held {
                mtime_ns: mtime_ns(&meta),
                file,
                token,
                quiet,
            };
            self.tracker.observe_for(id, activity, held, now, quiet);
        }
        if logged && let Some(l) = &self.log {
            l.sync();
        }
    }

    /// Let go of files deleted while they wait. The detector holds each
    /// waiting file open, and an unlinked file's blocks stay allocated while
    /// anyone has it open: without this a deleted file kept its space until
    /// its quiet period ended (up to 600 s). Every waiting file is looked at
    /// once per `gone_every`, at most `gone_batch` of them per tick.
    fn let_go_of_deleted(&mut self) {
        if self.gone_queue.is_empty() {
            if self.gone_at.elapsed() < self.cfg.gone_every {
                return;
            }
            self.gone_at = Instant::now();
            self.gone_queue = self.tracker.keys();
        }
        let n = self.gone_queue.len().min(self.cfg.gone_batch.max(1));
        let batch: Vec<(u64, u64)> = self.gone_queue.drain(..n).collect();
        for key in batch {
            let gone = self
                .tracker
                .forget_if(&key, |h| h.file.metadata().is_ok_and(|m| m.nlink() == 0));
            if let Some(held) = gone
                && let Some(t) = held.token
            {
                self.finished(t, "deleted while quiet");
            }
        }
    }

    /// Files quiet long enough: reported (queued in `out`). A tokened one
    /// that isn't reported is finished in the log, never left pending.
    fn expire(&mut self, out: &mut Out) {
        self.apply_new_periods();
        self.let_go_of_deleted();
        let now = Instant::now();
        for ((dev, ino), held, closed) in self.tracker.due(now) {
            let why = match held.file.metadata() {
                Err(_) => Some("unreadable"),
                Ok(m) if m.nlink() == 0 => Some("deleted while quiet"),
                Ok(_) => None,
            };
            if let Some(why) = why {
                if let Some(t) = held.token {
                    self.finished(t, why);
                }
                continue;
            }
            let Ok(meta) = held.file.metadata() else {
                continue;
            };
            if mtime_ns(&meta) != held.mtime_ns {
                // Written without an event we saw (FAN_MODIFY off): wait again.
                let m = mtime_ns(&meta);
                let quiet = held.quiet;
                self.tracker.observe_for(
                    (dev, ino),
                    Activity::Modify,
                    Held {
                        mtime_ns: m,
                        ..held
                    },
                    now,
                    quiet,
                );
                continue;
            }
            let path = path_of(&held.file).filter(|p| self.cfg.wants(p));
            let Some(path) = path else {
                // Deleted, moved out of the landing tree, or into .omni.
                if let Some(t) = held.token {
                    self.finished(t, "deleted or moved out of the landing tree");
                }
                continue;
            };
            // Moved while it waited, into a share that waits longer: it waits
            // that share's period, from now.
            let quiet = self.quiet_for(&path);
            if quiet > held.quiet {
                let activity = if closed {
                    Activity::CloseWrite
                } else {
                    Activity::Modify
                };
                self.tracker
                    .observe_for((dev, ino), activity, Held { quiet, ..held }, now, quiet);
                continue;
            }
            out.push(CloseWriteEvent {
                path,
                dev,
                ino,
                size: meta.len(),
                mtime_ns: mtime_ns(&meta),
                closed,
                token: held.token,
            });
        }
    }
}
