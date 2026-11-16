//! OmniFile Gateway edge: the write detector.
//!
//! A version of a file is "complete" when the writer closed it and nothing
//! touched it for a quiet period (`AGENTS.md` scenario step 2;
//! `edge-metadata.md` §5.4). The detector watches the landing filesystem
//! with fanotify and emits one [`CloseWriteEvent`] per quiet file:
//!
//! * `FAN_CLOSE_WRITE` arms (or re-arms) the file's quiet timer;
//! * `FAN_MODIFY` (optional, on by default) re-arms it too, so a writer
//!   that keeps the file open (NFSv3 through Ganesha's fd cache) still
//!   produces a candidate once it goes quiet, with `closed = false`;
//! * when the timer expires, the file is re-checked through the fd fanotify
//!   handed over (so a rename in between is followed, and a delete drops
//!   it), and the event carries the path, inode, size and mtime at that
//!   moment.
//!
//! The daemon still verifies a candidate (commit clone, hashing, SEG-Y
//! structure check); the detector only says "look at this now".
//!
//! Events caused by this process (the daemon writing commit clones) and
//! paths under `ignore_prefixes` (for example `.omni/`) are skipped. The
//! group uses `FAN_UNLIMITED_QUEUE`, so events are not dropped; marking a
//! whole filesystem needs `CAP_SYS_ADMIN`. The daemon keeps the group in the
//! systemd fd store ([`FDNAME`], [`run_with`]), so a close that lands while
//! it restarts is still queued for the next instance. A close the detector
//! had already read, and was waiting out the quiet period for, is recorded in
//! the daemon's [`PendingLog`] first, and replayed at the next start
//! ([`Watch`]; `docs/design/namespace-sync.md`, "Files no close was seen for").
//!
//! Linux only: elsewhere [`run`] returns [`DetectError::Unsupported`]. The
//! quiet-period logic ([`QuietTracker`]) is portable and tested everywhere.

use std::collections::HashMap;
use std::hash::Hash;
use std::path::PathBuf;
use std::time::{Duration, Instant};

#[cfg(target_os = "linux")]
mod linux;
/// Namespace events: which directories changed (namespace sync).
#[cfg(target_os = "linux")]
pub mod ns;

/// Which objects the fanotify mark covers.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum MarkScope {
    /// The whole filesystem holding `root` (`FAN_MARK_FILESYSTEM`): sees
    /// writes through every mount and bind mount of it.
    #[default]
    Filesystem,
    /// Only the mount at `root` (`FAN_MARK_MOUNT`).
    Mount,
}

#[derive(Clone, Debug)]
pub struct DetectConfig {
    /// The landing tree. Events outside it are ignored.
    pub root: PathBuf,
    pub scope: MarkScope,
    /// How long a file must stay untouched after its last write or close.
    pub quiet: Duration,
    /// Also watch `FAN_MODIFY` (writes), so files held open still go quiet.
    pub watch_modify: bool,
    /// Paths never reported (absolute; for example `<root>/.omni`).
    pub ignore_prefixes: Vec<PathBuf>,
    /// How often the quiet timers are checked. Default: `quiet / 4`,
    /// between 50 ms and 1 s.
    pub tick: Option<Duration>,
    /// A file must be at least this many path components below `root` to be
    /// waited on. The daemon sets 2: a file directly under the data root is
    /// in no share and is never committed, so it is not tracked at all.
    pub min_depth: usize,
    /// How often every waiting file is checked for deletion. A file deleted
    /// while it waits is let go then, not when its period ends: the detector
    /// holds it open, and an open file's blocks stay allocated after its
    /// unlink (2026-11-08 regression: a deleted 62 GiB file kept the volume
    /// full for the 600 s it was waited on).
    pub gone_every: Duration,
    /// At most this many waiting files are checked per tick, so a check of a
    /// million files never stalls the reading of events.
    pub gone_batch: usize,
}

impl DetectConfig {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        let root = root.into();
        Self {
            ignore_prefixes: vec![root.join(".omni")],
            root,
            scope: MarkScope::default(),
            quiet: Duration::from_secs(10),
            watch_modify: true,
            tick: None,
            min_depth: 1,
            gone_every: Duration::from_secs(2),
            gone_batch: 20_000,
        }
    }

    pub fn tick(&self) -> Duration {
        self.tick.unwrap_or_else(|| {
            (self.quiet / 4).clamp(Duration::from_millis(50), Duration::from_secs(1))
        })
    }

    /// Is `path` inside `root`, at least `min_depth` components below it, and
    /// outside every ignored prefix?
    pub fn wants(&self, path: &std::path::Path) -> bool {
        path.strip_prefix(&self.root)
            .is_ok_and(|rel| rel.components().count() >= self.min_depth)
            && !self.ignore_prefixes.iter().any(|p| path.starts_with(p))
    }
}

/// A file that went quiet after being written.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CloseWriteEvent {
    /// Its path when it went quiet.
    pub path: PathBuf,
    pub dev: u64,
    pub ino: u64,
    pub size: u64,
    pub mtime_ns: i64,
    /// A `FAN_CLOSE_WRITE` was seen after the last write. False: the writer
    /// may still hold it open (it went quiet on `FAN_MODIFY` alone).
    pub closed: bool,
    /// What the [`PendingLog`] returned when the file was first seen: hand it
    /// back to the log (`done`) once the write is committed.
    pub token: Option<u64>,
}

#[derive(Debug, thiserror::Error)]
pub enum DetectError {
    #[error("fanotify write detection needs Linux")]
    Unsupported,
    #[error("fanotify {what}: {source}")]
    Fanotify {
        what: &'static str,
        #[source]
        source: std::io::Error,
    },
}

/// What happened to a file.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Activity {
    Modify,
    CloseWrite,
}

struct Pending<T> {
    last: Instant,
    /// How long this file must stay untouched: its share's period.
    quiet: Duration,
    closed: bool,
    payload: T,
}

/// Quiet-period bookkeeping, keyed by file identity (`(dev, ino)` on Linux).
/// Every activity re-arms the timer; [`QuietTracker::due`] returns the files
/// quiet for at least their period, once each. A file's period is
/// `quiet` unless [`QuietTracker::observe_for`] gave it its own.
pub struct QuietTracker<K, T> {
    quiet: Duration,
    pending: HashMap<K, Pending<T>>,
}

impl<K: Eq + Hash + Clone, T> QuietTracker<K, T> {
    pub fn new(quiet: Duration) -> Self {
        Self {
            quiet,
            pending: HashMap::new(),
        }
    }

    pub fn len(&self) -> usize {
        self.pending.len()
    }

    pub fn is_empty(&self) -> bool {
        self.pending.is_empty()
    }

    /// Record activity on `key` at `now`. `payload` replaces the stored one
    /// (the newest handle is the best one). A write after a close clears
    /// `closed`: the writer is back.
    pub fn observe(&mut self, key: K, activity: Activity, payload: T, now: Instant) {
        self.observe_for(key, activity, payload, now, self.quiet);
    }

    /// [`QuietTracker::observe`], for a file whose period is `quiet`.
    pub fn observe_for(
        &mut self,
        key: K,
        activity: Activity,
        payload: T,
        now: Instant,
        quiet: Duration,
    ) {
        let closed = activity == Activity::CloseWrite;
        match self.pending.get_mut(&key) {
            Some(p) => {
                p.last = p.last.max(now);
                p.quiet = quiet;
                p.closed = closed;
                p.payload = payload;
            }
            None => {
                self.pending.insert(
                    key,
                    Pending {
                        last: now,
                        quiet,
                        closed,
                        payload,
                    },
                );
            }
        }
    }

    /// The payload held for `key`, if it is pending.
    pub fn get(&self, key: &K) -> Option<&T> {
        self.pending.get(key).map(|p| &p.payload)
    }

    /// Re-arm `key` (it changed after all) without new payload.
    pub fn rearm(&mut self, key: &K, now: Instant) {
        if let Some(p) = self.pending.get_mut(key) {
            p.last = now;
        }
    }

    /// Drop `key` (deleted, or no longer wanted).
    pub fn forget(&mut self, key: &K) {
        self.pending.remove(key);
    }

    /// Give every waiting file the period `period_of` says it has now. Each
    /// is still measured from its last write: a shorter period can make it
    /// due at once, and a longer one makes it wait on.
    pub fn requiet(&mut self, mut period_of: impl FnMut(&mut T) -> Duration) {
        for p in self.pending.values_mut() {
            p.quiet = period_of(&mut p.payload);
        }
    }

    /// Every waiting file's key, for a pass over them in slices
    /// ([`QuietTracker::forget_if`]).
    pub fn keys(&self) -> Vec<K> {
        self.pending.keys().cloned().collect()
    }

    /// Drop `key` if it still waits and `gone` says so of its payload, and
    /// return the payload. Used to let go of files deleted while they wait.
    pub fn forget_if(&mut self, key: &K, gone: impl FnOnce(&T) -> bool) -> Option<T> {
        if self.pending.get(key).is_some_and(|p| gone(&p.payload)) {
            return self.pending.remove(key).map(|p| p.payload);
        }
        None
    }

    /// Files quiet for their period by `now`, removed from the tracker.
    pub fn due(&mut self, now: Instant) -> Vec<(K, T, bool)> {
        let keys: Vec<K> = self
            .pending
            .iter()
            .filter(|(_, p)| now.saturating_duration_since(p.last) >= p.quiet)
            .map(|(k, _)| k.clone())
            .collect();
        keys.into_iter()
            .filter_map(|k| self.pending.remove(&k).map(|p| (k, p.payload, p.closed)))
            .collect()
    }
}

/// The `FDNAME` the daemon stores the detector's group under in the systemd
/// fd store (`FDSTORE=1`), so events queued while it restarts survive.
pub const FDNAME: &str = "omnifile-detect";

#[cfg(target_os = "linux")]
pub use linux::{create_group, usable_group};

/// The group flags from a fanotify fd's `/proc/self/fdinfo` text
/// (`fanotify flags:<hex> event-flags:<hex>`).
pub fn fdinfo_flags(fdinfo: &str) -> Option<u32> {
    fdinfo.lines().find_map(|l| {
        let rest = l.trim().strip_prefix("fanotify flags:")?;
        let hex = rest.split_whitespace().next()?;
        u32::from_str_radix(hex, 16).ok()
    })
}

/// A notification-class group (`FAN_CLASS_NOTIF`, class bits 0x0c clear)
/// with `FAN_UNLIMITED_QUEUE` (0x10): what [`run`] makes.
pub fn flags_usable(flags: u32) -> bool {
    const CLASS_BITS: u32 = 0x0c;
    const FAN_UNLIMITED_QUEUE: u32 = 0x10;
    flags & CLASS_BITS == 0 && flags & FAN_UNLIMITED_QUEUE != 0
}

/// Watch `cfg.root` and send a [`CloseWriteEvent`] for each file that goes
/// quiet. Returns `Ok(())` when the receiver is dropped.
pub async fn run(
    cfg: DetectConfig,
    tx: tokio::sync::mpsc::Sender<CloseWriteEvent>,
) -> Result<(), DetectError> {
    run_with(cfg, None, tx).await
}

/// [`run`] on a given group (Linux: from [`create_group`], or one from the
/// systemd fd store that [`usable_group`] accepted). Events it queued while
/// no one read it are reported first.
pub async fn run_with(
    cfg: DetectConfig,
    group: Option<std::os::fd::OwnedFd>,
    tx: tokio::sync::mpsc::Sender<CloseWriteEvent>,
) -> Result<(), DetectError> {
    run_full(cfg, group, Watch::default(), tx).await
}

/// A durable record of the files the detector is waiting on (WP9). A close
/// it has read lives only in its memory until the quiet period ends, and in
/// the daemon's commit queue after that: a restart in between would lose the
/// write. The daemon keeps this log and replays it at start ([`Watch`]).
pub trait PendingLog: Send + Sync {
    /// `file` was written and is now waited on: record it, and return a
    /// token for it. Called once each time a file enters the quiet tracker,
    /// before its event is acted on.
    fn observed(&self, file: &std::fs::File) -> u64;
    /// After a batch of events: make what [`PendingLog::observed`] recorded
    /// durable.
    fn sync(&self) {}
    /// A file with this token will not be reported after all (deleted while
    /// quiet, moved out of the landing tree): it is not pending any more.
    fn finished(&self, _token: u64) {}
}

/// Each file's quiet period, for a detector that watches shares with periods
/// of their own (decision 67). The daemon keeps it up to date.
pub trait QuietSource: Send + Sync {
    /// The period for a file at `path` now.
    fn quiet_of(&self, path: &std::path::Path) -> Duration;
    /// Moves when any period may have changed: the files being waited on are
    /// then given the periods they have now.
    fn generation(&self) -> u64;
}

/// What the detector starts with besides its group.
#[derive(Default)]
pub struct Watch {
    /// Files a previous run was waiting on (replayed from the log), each with
    /// its token: they wait out a quiet period again and are reported.
    pub initial: Vec<(std::fs::File, u64)>,
    /// Where waited-on files are recorded.
    pub log: Option<std::sync::Arc<dyn PendingLog>>,
    /// More such files, handed over later (the daemon resolves some only
    /// once its namespace sweep has run).
    pub inject: Option<tokio::sync::mpsc::UnboundedReceiver<(std::fs::File, u64)>>,
    /// Each file's period. Without it every file waits `DetectConfig::quiet`.
    pub periods: Option<std::sync::Arc<dyn QuietSource>>,
}

/// [`run_with`], starting from `watch`.
pub async fn run_full(
    cfg: DetectConfig,
    group: Option<std::os::fd::OwnedFd>,
    watch: Watch,
    tx: tokio::sync::mpsc::Sender<CloseWriteEvent>,
) -> Result<(), DetectError> {
    #[cfg(target_os = "linux")]
    {
        linux::run(cfg, group, watch, tx).await
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = (cfg, group, watch, tx);
        Err(DetectError::Unsupported)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 2026-11-08 regression: a file deleted while it waits is let go at the
    /// next check, whatever its period, and the others wait on.
    #[test]
    fn a_file_gone_while_it_waits_is_forgotten_and_the_rest_wait_on() {
        let t0 = Instant::now();
        let mut tr: QuietTracker<u64, &str> = QuietTracker::new(Duration::from_secs(600));
        tr.observe(1, Activity::CloseWrite, "deleted", t0);
        tr.observe(2, Activity::CloseWrite, "kept", t0);
        let mut keys = tr.keys();
        keys.sort();
        assert_eq!(keys, vec![1, 2]);
        let gone: Vec<&str> = keys
            .iter()
            .filter_map(|k| tr.forget_if(k, |p| *p == "deleted"))
            .collect();
        assert_eq!(gone, vec!["deleted"]);
        assert_eq!(tr.len(), 1);
        // A key no longer waiting is not an error.
        assert!(tr.forget_if(&1, |_| true).is_none());
        assert_eq!(
            tr.due(t0 + Duration::from_secs(600)),
            vec![(2, "kept", true)]
        );
    }

    /// The daemon's setting: a file directly under the root is not waited on.
    #[test]
    fn a_file_directly_under_the_root_is_not_wanted_at_depth_two() {
        let mut c = DetectConfig::new("/d");
        assert!(
            c.wants(std::path::Path::new("/d/stray.bin")),
            "depth 1 by default"
        );
        c.min_depth = 2;
        assert!(!c.wants(std::path::Path::new("/d/stray.bin")));
        assert!(!c.wants(std::path::Path::new("/d/.chaos-fill (deleted)")));
        assert!(c.wants(std::path::Path::new("/d/share/a.segy")));
        assert!(!c.wants(std::path::Path::new("/d/.omni/commit/1.2")));
        assert!(!c.wants(std::path::Path::new("/elsewhere/share/a.segy")));
    }

    #[test]
    fn each_file_waits_its_own_period() {
        let t0 = Instant::now();
        let mut tr: QuietTracker<u64, &str> = QuietTracker::new(Duration::from_secs(10));
        tr.observe_for(1, Activity::CloseWrite, "worm", t0, Duration::from_secs(60));
        tr.observe(2, Activity::CloseWrite, "plain", t0);
        let at = |s| t0 + Duration::from_secs(s);
        assert_eq!(tr.due(at(10)), vec![(2, "plain", true)]);
        assert!(tr.due(at(59)).is_empty(), "60 s from its last write");
        // A new write re-arms it, for its own period.
        tr.observe_for(1, Activity::Modify, "worm", at(59), Duration::from_secs(60));
        assert!(tr.due(at(118)).is_empty());
        assert_eq!(tr.due(at(119)), vec![(1, "worm", false)]);
    }

    /// A period changed while a file waits applies to it, measured from its
    /// last write: a shorter one can make it due at once, a longer one keeps
    /// it waiting.
    #[test]
    fn a_new_period_applies_to_files_already_waiting() {
        let t0 = Instant::now();
        let at = |s| t0 + Duration::from_secs(s);
        let mut tr: QuietTracker<u64, u64> = QuietTracker::new(Duration::from_secs(10));
        tr.observe_for(1, Activity::CloseWrite, 1, t0, Duration::from_secs(60));
        tr.observe_for(2, Activity::CloseWrite, 2, t0, Duration::from_secs(10));
        assert!(tr.due(at(5)).is_empty());
        // File 1 shorter, file 2 longer.
        tr.requiet(|who| {
            Duration::from_secs(match *who {
                1 => 20,
                _ => 30,
            })
        });
        assert!(tr.due(at(19)).is_empty(), "file 2 no longer due at 10 s");
        assert_eq!(tr.due(at(20)), vec![(1, 1, true)]);
        assert!(tr.due(at(29)).is_empty());
        assert_eq!(tr.due(at(30)), vec![(2, 2, true)]);
    }

    #[test]
    fn quiet_period_rearms_and_fires_once() {
        let t0 = Instant::now();
        let q = Duration::from_millis(100);
        let mut tr: QuietTracker<u64, &str> = QuietTracker::new(q);
        tr.observe(1, Activity::Modify, "a", t0);
        tr.observe(
            1,
            Activity::CloseWrite,
            "a2",
            t0 + Duration::from_millis(50),
        );
        assert!(
            tr.due(t0 + Duration::from_millis(120)).is_empty(),
            "re-armed at 50 ms"
        );
        let due = tr.due(t0 + Duration::from_millis(150));
        assert_eq!(due, vec![(1, "a2", true)]);
        assert!(tr.due(t0 + Duration::from_secs(10)).is_empty(), "once");
    }

    #[test]
    fn write_after_close_is_not_closed() {
        let t0 = Instant::now();
        let mut tr: QuietTracker<u64, ()> = QuietTracker::new(Duration::from_millis(10));
        tr.observe(1, Activity::CloseWrite, (), t0);
        tr.observe(1, Activity::Modify, (), t0);
        assert_eq!(tr.due(t0 + Duration::from_millis(10)), vec![(1, (), false)]);
    }

    #[test]
    fn independent_files_and_forget() {
        let t0 = Instant::now();
        let mut tr: QuietTracker<u64, ()> = QuietTracker::new(Duration::from_millis(10));
        tr.observe(1, Activity::CloseWrite, (), t0);
        tr.observe(2, Activity::CloseWrite, (), t0 + Duration::from_millis(8));
        tr.observe(3, Activity::CloseWrite, (), t0);
        tr.forget(&3);
        let due: Vec<_> = tr
            .due(t0 + Duration::from_millis(12))
            .into_iter()
            .map(|d| d.0)
            .collect();
        assert_eq!(due, vec![1]);
        tr.rearm(&2, t0 + Duration::from_millis(15));
        assert!(tr.due(t0 + Duration::from_millis(20)).is_empty());
        assert_eq!(tr.len(), 1);
    }

    #[test]
    fn config_filters_paths() {
        let cfg = DetectConfig::new("/srv/landing");
        assert!(cfg.wants(std::path::Path::new("/srv/landing/a/b.segy")));
        assert!(!cfg.wants(std::path::Path::new("/srv/landing/.omni/commit/x")));
        assert!(!cfg.wants(std::path::Path::new("/srv/other/x")));
        assert_eq!(cfg.tick(), Duration::from_secs(1));
    }

    #[test]
    fn stored_group_flags() {
        // FAN_CLASS_NOTIF | FAN_UNLIMITED_QUEUE | FAN_NONBLOCK | FAN_CLOEXEC.
        let info = "pos:\t0\nflags:\t04002\nmnt_id:\t15\nino:\t1057\n\
                    fanotify flags:13 event-flags:88002\n\
                    fanotify sdev:fd00 mflags:0 mask:a ignored_mask:0\n";
        assert_eq!(fdinfo_flags(info), Some(0x13));
        assert!(flags_usable(0x13));
        assert!(!flags_usable(0x03), "a bounded queue");
        assert!(!flags_usable(0x18), "FAN_CLASS_PRE_CONTENT: the guard's");
        assert!(!flags_usable(0x14), "FAN_CLASS_CONTENT");
        assert_eq!(fdinfo_flags("pos:\t0\n"), None);
    }

    #[cfg(not(target_os = "linux"))]
    #[tokio::test]
    async fn unsupported_off_linux() {
        let (tx, _rx) = tokio::sync::mpsc::channel(1);
        assert!(matches!(
            run(DetectConfig::new("/tmp"), tx).await,
            Err(DetectError::Unsupported)
        ));
    }
}
