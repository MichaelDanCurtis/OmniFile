//! The event engine: reader, workers, held events, `O_TRUNC` clean-up.
//!
//! - **Reader** (one thread): reads batches from the group, journals each
//!   batch in one write, answers the fast path inline (unmanaged files,
//!   wholly local files, ranges inside the resident prefix, bad markers),
//!   and hands the rest to the workers. It never exits: `EMFILE`/`ENFILE`
//!   back off (the kernel has already denied the event it could not open an
//!   fd for), and in-flight events are capped below the fd limit.
//! - **Workers**: resolve identity while the event is pending, ask the
//!   broker, answer or hold.
//! - **Held events** (one thread): re-checked on [`Waker::wake`] and every
//!   poll interval, by re-reading the marker; allowed only once it covers
//!   the range.
//! - Every event is a [`Pending`]; dropping one unanswered denies it
//!   (`EIO`), so a bug or panic fails closed and never leaves a reader hung
//!   or an fd number ambiguous.

use std::collections::{HashMap, HashSet};
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, OwnedFd, RawFd};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use omnifile_marker::xattr;
use omnifile_marker::{MAP_XATTR_NAME, Marker};
use omnifile_stub::InodeKey;
use tracing::{debug, error, info, warn};

use super::marks::IgnoreMarks;
use super::sys;
use crate::api::{
    AccessRequest, Answer, DirRequest, EventIdentity, EventOrigin, GuardConfig, HugePolicy,
    RecallBroker,
};
use crate::classify::{MarkerView, Verdict, classify_mapped};
use crate::journal::{Handle, Journal, Record};
use crate::uapi::{self, Access, FAN_ALLOW, FAN_Q_OVERFLOW, deny_errno};

pub const DENY_EIO: u32 = deny_errno(libc::EIO);
/// Upper bound on one pre-content event: metadata (24), range record (24),
/// pidfd record (8), rounded up.
const MAX_EVENT_LEN: usize = 64;

/// Counters, logged every minute and readable through the handle.
#[derive(Debug, Default)]
pub struct Stats {
    pub events: AtomicU64,
    pub fast_allowed: AtomicU64,
    pub slow: AtomicU64,
    pub held: AtomicU64,
    pub allowed_after_wait: AtomicU64,
    pub denied: AtomicU64,
    pub bad_markers: AtomicU64,
    pub stale_cleared: AtomicU64,
    pub fd_exhaustion: AtomicU64,
    pub overflows: AtomicU64,
    pub orphans: AtomicU64,
    pub swept: AtomicU64,
    pub inflight: AtomicU64,
    pub held_cap_denied: AtomicU64,
    pub partial_modified: AtomicU64,
}

impl Stats {
    pub fn line(&self) -> String {
        let l = |a: &AtomicU64| a.load(Ordering::Relaxed);
        format!(
            "events={} fast_allow={} slow={} held_now={} allowed_after_wait={} denied={} \
             bad_marker={} stale_cleared={} emfile={} overflow={} orphans={} swept={} inflight={} \
             held_cap_denied={} partial_modified={}",
            l(&self.events),
            l(&self.fast_allowed),
            l(&self.slow),
            l(&self.held),
            l(&self.allowed_after_wait),
            l(&self.denied),
            l(&self.bad_markers),
            l(&self.stale_cleared),
            l(&self.fd_exhaustion),
            l(&self.overflows),
            l(&self.orphans),
            l(&self.swept),
            l(&self.inflight),
            l(&self.held_cap_denied),
            l(&self.partial_modified),
        )
    }
}

/// Writes answers; shared by every [`Pending`].
pub struct Responder {
    pub group: RawFd,
    pub journal: Journal,
    pub stats: Arc<Stats>,
}

impl Responder {
    fn send(&self, resp_fd: i32, resp: u32) {
        if let Err(e) = sys::respond(self.group, resp_fd, resp) {
            // ENOENT: no such pending event (an orphan its process already
            // gave up on with SIGKILL, which cancels the event).
            debug!(fd = resp_fd, err = %e, "response not accepted");
        }
        if let Err(e) = self.journal.end(&[resp_fd]) {
            warn!(err = %e, "journal write failed");
        }
        if resp != FAN_ALLOW {
            self.stats.denied.fetch_add(1, Ordering::Relaxed);
        }
    }
}

/// One event the guard owes an answer.
pub struct Pending {
    /// The number the kernel matches the answer by.
    pub resp_fd: i32,
    /// The file, opened by the kernel for the event (or, for an orphan,
    /// reopened by handle through the fill mount).
    pub file: Option<OwnedFd>,
    /// For orphans: `/dev/null` holding `resp_fd`'s number until answered.
    pub reserved: Option<OwnedFd>,
    pub pidfd: Option<OwnedFd>,
    pub pid: i32,
    pub access: Access,
    pub ino: u64,
    pub ino_gen: u32,
    pub handle: Option<Handle>,
    pub since: Instant,
    responder: Arc<Responder>,
    answered: bool,
}

impl Pending {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        responder: Arc<Responder>,
        resp_fd: i32,
        file: OwnedFd,
        reserved: Option<OwnedFd>,
        pidfd: Option<OwnedFd>,
        pid: i32,
        access: Access,
        ino: u64,
        ino_gen: u32,
        handle: Option<Handle>,
    ) -> Pending {
        responder.stats.inflight.fetch_add(1, Ordering::Relaxed);
        Pending {
            resp_fd,
            file: Some(file),
            reserved,
            pidfd,
            pid,
            access,
            ino,
            ino_gen,
            handle,
            since: Instant::now(),
            responder,
            answered: false,
        }
    }

    pub fn fd(&self) -> BorrowedFd<'_> {
        self.file
            .as_ref()
            .expect("file present until answered")
            .as_fd()
    }

    /// Answer, journal the end, then close the descriptors (in that order:
    /// the number must not be reused before the answer is journalled).
    pub fn answer(mut self, resp: u32) {
        self.finish(resp);
    }

    fn finish(&mut self, resp: u32) {
        if self.answered {
            return;
        }
        self.answered = true;
        self.responder.send(self.resp_fd, resp);
        self.file.take();
        self.reserved.take();
        self.pidfd.take();
        self.responder
            .stats
            .inflight
            .fetch_sub(1, Ordering::Relaxed);
    }
}

impl Drop for Pending {
    fn drop(&mut self) {
        if !self.answered {
            error!(
                fd = self.resp_fd,
                "event dropped unanswered: denying it (fail closed)"
            );
            self.finish(DENY_EIO);
        }
    }
}

/// Lets the daemon re-check held events at once (fill progress, recall
/// state change) instead of at the next poll.
#[derive(Clone)]
pub struct Waker(pub(crate) Sender<Wake>);

pub(crate) enum Wake {
    Ino(u64),
    All,
}

impl Waker {
    /// Re-check events held on this inode.
    pub fn wake(&self, ino: u64) {
        let _ = self.0.send(Wake::Ino(ino));
    }

    pub fn wake_all(&self) {
        let _ = self.0.send(Wake::All);
    }
}

struct Held<Id> {
    p: Pending,
    identity: Id,
    path: Option<PathBuf>,
}

pub struct Engine<B, I: EventIdentity> {
    pub cfg: GuardConfig,
    pub group: RawFd,
    pub fill_fd: RawFd,
    pub fsid: u64,
    pub responder: Arc<Responder>,
    pub broker: B,
    pub identity: I,
    pub marks: Arc<IgnoreMarks>,
    pub stats: Arc<Stats>,
    pub max_inflight: u64,
    pub max_held: u64,
    clearing: Mutex<HashSet<u64>>,
    clearing_cv: Condvar,
    held: Mutex<HashMap<u64, Vec<Held<I::Identity>>>>,
    jobs: Mutex<Option<Sender<Pending>>>,
    pub shutting_down: AtomicBool,
}

/// What the marker and inode say right now.
struct Facts {
    regular: bool,
    st_size: u64,
    marker: MarkerView,
    /// M5: the marker's chunk map, when it has one.
    map: Option<omnifile_marker::ChunkMap>,
}

fn inspect(fd: BorrowedFd<'_>) -> Facts {
    match sys::fstat(fd) {
        Ok(st) => {
            let mut marker = MarkerView::from_read(xattr::read(fd));
            let mut map = None;
            // M5: a PARTIAL marker with CHUNK_MAP is read with its map. The
            // marker was read first: the daemon widens the map before the
            // marker and narrows both before punching, so a newer map only
            // names more durable chunks. Missing or invalid: fail closed.
            if let MarkerView::Valid(m) = &marker
                && !m.is_fully_resident()
                && m.flags.contains(omnifile_marker::Flags::CHUNK_MAP)
            {
                match xattr::read_map(fd, m) {
                    Ok(Some(cm)) => map = Some(cm),
                    Ok(None) | Err(_) => marker = MarkerView::Bad(3),
                }
            }
            Facts {
                regular: st.st_mode & libc::S_IFMT == libc::S_IFREG,
                st_size: st.st_size as u64,
                marker,
                map,
            }
        }
        Err(_) => Facts {
            regular: true,
            st_size: 0,
            marker: MarkerView::Bad(5),
            map: None,
        },
    }
}

enum Step<Id> {
    Done,
    Hold(Held<Id>),
}

impl<B, I> Engine<B, I>
where
    B: RecallBroker<I::Identity>,
    I: EventIdentity,
{
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        cfg: GuardConfig,
        group: RawFd,
        fill_fd: RawFd,
        fsid: u64,
        responder: Arc<Responder>,
        broker: B,
        identity: I,
        marks: Arc<IgnoreMarks>,
        max_inflight: u64,
    ) -> Engine<B, I> {
        let stats = responder.stats.clone();
        let max_held = cfg
            .max_held
            .unwrap_or(max_inflight / 2)
            .clamp(1, max_inflight.saturating_sub(1).max(1));
        Engine {
            cfg,
            group,
            fill_fd,
            fsid,
            responder,
            broker,
            identity,
            marks,
            stats,
            max_inflight,
            max_held,
            clearing: Mutex::new(HashSet::new()),
            clearing_cv: Condvar::new(),
            held: Mutex::new(HashMap::new()),
            jobs: Mutex::new(None),
            shutting_down: AtomicBool::new(false),
        }
    }

    fn classify_now(&self, fd: BorrowedFd<'_>, access: Access) -> Verdict {
        let f = inspect(fd);
        classify_mapped(
            f.regular,
            f.st_size,
            &f.marker,
            f.map.as_ref(),
            access,
            self.cfg.limits,
        )
    }

    fn key(&self, p: &Pending, m: &Marker) -> InodeKey {
        InodeKey {
            fsid: self.fsid,
            ino: p.ino,
            ino_gen: p.ino_gen,
            file_id: m.file_id,
        }
    }

    fn is_clearing(&self, ino: u64) -> bool {
        self.clearing.lock().unwrap().contains(&ino)
    }

    /// Answer `ALLOW`, then add an ignore mark if the file is wholly local.
    fn allow(&self, p: Pending, ignorable: bool) {
        let mark = if ignorable && self.cfg.ignore_marks {
            p.handle.clone().map(|h| (p.ino, h))
        } else {
            None
        };
        p.answer(FAN_ALLOW);
        if let Some((ino, h)) = mark {
            self.marks.add(ino, &h);
        }
    }

    // ---------------------------------------------------------------- reader

    /// Turn a batch of raw events into journalled [`Pending`]s.
    fn admit(&self, evs: Vec<uapi::RawEvent>) -> Vec<Pending> {
        let mut out = Vec::with_capacity(evs.len());
        let mut recs = Vec::with_capacity(evs.len());
        for ev in evs {
            if ev.mask & FAN_Q_OVERFLOW != 0 {
                self.stats.overflows.fetch_add(1, Ordering::Relaxed);
                error!("FAN_Q_OVERFLOW: the kernel dropped (allowed) permission events");
            }
            // SAFETY: the kernel opened these descriptors for us.
            let pidfd = ev
                .pidfd
                .map(|fd| unsafe { <OwnedFd as std::os::fd::FromRawFd>::from_raw_fd(fd) });
            if ev.fd < 0 {
                continue;
            }
            // SAFETY: as above.
            let file = unsafe { <OwnedFd as std::os::fd::FromRawFd>::from_raw_fd(ev.fd) };
            let (ino, ino_gen) = match sys::fstat(file.as_fd()) {
                Ok(st) => (
                    st.st_ino,
                    omnifile_stub::sys::inode_generation(file.as_fd()).unwrap_or(0),
                ),
                Err(_) => (0, 0),
            };
            let handle = sys::handle_of(file.as_fd()).ok();
            let access = ev.access();
            let (off, len) = access.raw();
            recs.push(Record {
                fd: ev.fd,
                ino,
                ino_gen,
                off,
                len,
                pid: ev.pid,
                handle: handle.clone(),
            });
            out.push(Pending::new(
                self.responder.clone(),
                ev.fd,
                file,
                None,
                pidfd,
                ev.pid,
                access,
                ino,
                ino_gen,
                handle,
            ));
        }
        self.stats
            .events
            .fetch_add(out.len() as u64, Ordering::Relaxed);
        // Before acting on any of it.
        if self.cfg.test_skip_journal {
            recs.clear();
        }
        if let Err(e) = self.responder.journal.begin(&recs) {
            error!(err = %e, "journal write failed: a crash now would deny these events (sweep)");
        }
        out
    }

    /// The reader loop. Never returns.
    pub fn read_loop(self: &Arc<Self>) {
        let mut buf = vec![0u64; 8192];
        let mut last_emfile_log = Instant::now() - Duration::from_secs(60);
        loop {
            while self.stats.inflight.load(Ordering::Relaxed) >= self.max_inflight {
                std::thread::sleep(Duration::from_millis(2));
            }
            match sys::poll_in(self.group, 1000) {
                Ok(true) => {}
                Ok(false) => continue,
                Err(e) if sys::errno_of(&e) == libc::EINTR => continue,
                Err(e) => {
                    error!(err = %e, "poll on the group failed; retrying");
                    std::thread::sleep(Duration::from_millis(100));
                    continue;
                }
            }
            // The kernel opens a descriptor for every event it copies out, so
            // read no more events than the in-flight budget has room for (an
            // event it cannot open an fd for is denied).
            let room = self
                .max_inflight
                .saturating_sub(self.stats.inflight.load(Ordering::Relaxed))
                .max(1);
            let want = (room as usize)
                .saturating_mul(MAX_EVENT_LEN)
                .min(buf.len() * 8);
            // SAFETY: a u64 buffer viewed as bytes (8-byte aligned for parsing).
            let bytes =
                unsafe { std::slice::from_raw_parts_mut(buf.as_mut_ptr() as *mut u8, want) };
            let n = match sys::read_into(self.group, bytes) {
                Ok(n) => n,
                Err(e) => {
                    match sys::errno_of(&e) {
                        libc::EINTR | libc::EAGAIN => {}
                        libc::EMFILE | libc::ENFILE => {
                            // The kernel denied (EPERM) the event it could not
                            // open an fd for. Never exit: that would release
                            // nothing (the store holds the group) but it would
                            // stall every reader until the restart.
                            self.stats.fd_exhaustion.fetch_add(1, Ordering::Relaxed);
                            if last_emfile_log.elapsed() > Duration::from_secs(5) {
                                error!(err = %e, inflight = self.stats.inflight.load(Ordering::Relaxed),
                                    "out of descriptors reading events; backing off");
                                last_emfile_log = Instant::now();
                            }
                            std::thread::sleep(Duration::from_millis(20));
                        }
                        _ => {
                            error!(err = %e, "read on the group failed; retrying");
                            std::thread::sleep(Duration::from_millis(100));
                        }
                    }
                    continue;
                }
            };
            let (evs, perr) = uapi::parse(&bytes[..n]);
            if let Some(e) = perr {
                error!(err = %e, "unparseable event data");
            }
            let pending = self.admit(evs);
            self.dispatch_batch(pending);
        }
    }

    /// Fast path inline; the rest to the workers.
    pub fn dispatch_batch(self: &Arc<Self>, batch: Vec<Pending>) {
        let own = std::process::id() as i32;
        for p in batch {
            // A rewind directory is held and filled only for the file
            // servers (smbd, Ganesha), the users' way in. Anything else is
            // let through at once (it sees the directory unfilled):
            // - the daemon itself, filling or walking it: holding its own
            //   opens while a user's open of the same directory waits
            //   would deadlock (file bytes are always written through the
            //   fill mount, which raises nothing);
            // - root's tools (find, updatedb, a backup agent) crawling the
            //   private rewind area, which would otherwise fill the whole
            //   history tree.
            if p.access == Access::DirOpen && (p.pid == own || !self.is_server(p.pid)) {
                self.stats.fast_allowed.fetch_add(1, Ordering::Relaxed);
                self.allow(p, false);
                continue;
            }
            let v = self.classify_now(p.fd(), p.access);
            // An O_TRUNC clean-up of this inode is running: never allow on a
            // verdict taken during it (the marker is already gone, so the
            // file looks unmanaged). The order matters: classify first, then
            // check; a clean-up registers before it removes the marker and
            // deregisters only after its truncate.
            if self.is_clearing(p.ino) {
                self.to_worker(p);
                continue;
            }
            match v {
                Verdict::Allow { ignorable } => {
                    self.stats.fast_allowed.fetch_add(1, Ordering::Relaxed);
                    self.allow(p, ignorable);
                }
                Verdict::BadMarker(reason) => {
                    self.stats.bad_markers.fetch_add(1, Ordering::Relaxed);
                    warn!(ino = p.ino, reason, path = ?sys::fd_path(p.fd().as_raw_fd()),
                        "invalid marker: EIO (fail closed)");
                    p.answer(DENY_EIO);
                }
                _ => self.to_worker(p),
            }
        }
    }

    /// The process is one of [`GuardConfig::server_comms`]. smbd names a
    /// session's process after its client (`smbd[10.0.0.7]`).
    fn is_server(&self, pid: i32) -> bool {
        let Ok(comm) = std::fs::read_to_string(format!("/proc/{pid}/comm")) else {
            return false;
        };
        let comm = comm.trim_end();
        self.cfg.server_comms.iter().any(|n| {
            comm == n
                || comm
                    .strip_prefix(n.as_str())
                    .is_some_and(|rest| rest.starts_with('['))
        })
    }

    fn to_worker(&self, p: Pending) {
        self.stats.slow.fetch_add(1, Ordering::Relaxed);
        let tx = self.jobs.lock().unwrap().clone();
        match tx {
            Some(tx) => {
                if let Err(e) = tx.send(p) {
                    // No worker: the Pending is dropped and denied.
                    drop(e);
                }
            }
            None => drop(p),
        }
    }

    // --------------------------------------------------------------- workers

    pub fn start_workers(self: &Arc<Self>) {
        let (tx, rx) = std::sync::mpsc::channel::<Pending>();
        *self.jobs.lock().unwrap() = Some(tx);
        let rx = Arc::new(Mutex::new(rx));
        for i in 0..self.cfg.workers.max(1) {
            let rx = rx.clone();
            let me = self.clone();
            std::thread::Builder::new()
                .name(format!("smbguard-w{i}"))
                .spawn(move || {
                    loop {
                        let job = { rx.lock().unwrap().recv() };
                        let Ok(p) = job else { return };
                        // A panic drops `p`, which denies it.
                        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                            me.handle_new(p)
                        }));
                    }
                })
                .expect("spawn worker");
        }
    }

    fn handle_new(&self, mut p: Pending) {
        let identity = {
            let origin = EventOrigin {
                pid: p.pid,
                pidfd: p.pidfd.as_ref().map(|f| f.as_fd()),
            };
            self.identity.resolve(&origin)
        };
        p.pidfd.take();
        let path = sys::fd_path(p.fd().as_raw_fd());
        let h = Held { p, identity, path };
        if let Step::Hold(h) = self.evaluate(h, true) {
            self.hold(h);
        }
    }

    /// Decide a slow-path event. `first`: not yet held.
    fn evaluate(&self, h: Held<I::Identity>, first: bool) -> Step<I::Identity> {
        let mut rechecked = false;
        let mut tries = 0;
        loop {
            tries += 1;
            if tries > 4 {
                break;
            }
            let v = self.classify_now(h.p.fd(), h.p.access);
            // An O_TRUNC clean-up of this inode is running (it removed the
            // marker, and will truncate away what is there): no verdict
            // taken now may stand, an Allow least of all, because the
            // clean-up's truncate would then wipe this writer's bytes.
            // Classify after the check-in, never before.
            if self.is_clearing(h.p.ino) {
                self.wait_clearing(h.p.ino);
                tries -= 1;
                continue;
            }
            match v {
                Verdict::Allow { ignorable } => {
                    if !first {
                        self.stats
                            .allowed_after_wait
                            .fetch_add(1, Ordering::Relaxed);
                    }
                    self.allow(h.p, ignorable);
                    return Step::Done;
                }
                Verdict::BadMarker(reason) => {
                    self.stats.bad_markers.fetch_add(1, Ordering::Relaxed);
                    warn!(ino = h.p.ino, reason, path = ?h.path, "invalid marker: EIO (fail closed)");
                    h.p.answer(DENY_EIO);
                    return Step::Done;
                }
                Verdict::Truncated(m) => {
                    let key = self.key(&h.p, &m);
                    if !self.broker.may_act_on_size_mismatch(key, &m) {
                        return Step::Hold(h);
                    }
                    self.clear_stale(h.p.fd(), key, h.p.handle.as_ref(), h.path.as_deref());
                    continue;
                }
                Verdict::Extended(m) => {
                    let key = self.key(&h.p, &m);
                    if !self.broker.may_act_on_size_mismatch(key, &m) {
                        return Step::Hold(h);
                    }
                    let st = sys::fstat(h.p.fd()).map(|s| s.st_size as u64).unwrap_or(0);
                    if first {
                        error!(ino = h.p.ino, file_id = m.file_id, old_size = m.size, st_size = st,
                            path = ?h.path, "stub grew past its marker (O_APPEND before a fill): fail closed");
                    }
                    match self.broker.stub_extended(key, &m, st) {
                        Answer::Deny => {
                            h.p.answer(DENY_EIO);
                            return Step::Done;
                        }
                        Answer::Recheck if !rechecked => {
                            rechecked = true;
                            continue;
                        }
                        Answer::Recheck | Answer::Wait => return Step::Hold(h),
                    }
                }
                Verdict::Populate(marker) => {
                    let req = DirRequest {
                        key: self.key(&h.p, &marker),
                        marker,
                        path: h.path.as_deref(),
                        identity: &h.identity,
                        first,
                    };
                    match self.broker.on_dir_open(&req) {
                        Answer::Deny => {
                            if first {
                                info!(ino = h.p.ino, pid = h.p.pid, path = ?h.path,
                                    "unfilled rewind directory can't be filled: EIO");
                            }
                            h.p.answer(DENY_EIO);
                            return Step::Done;
                        }
                        Answer::Recheck if !rechecked => {
                            rechecked = true;
                            continue;
                        }
                        Answer::Recheck | Answer::Wait => {
                            if first {
                                if self.stats.held.load(Ordering::Relaxed) >= self.max_held {
                                    self.stats.held_cap_denied.fetch_add(1, Ordering::Relaxed);
                                    warn!(ino = h.p.ino, max_held = self.max_held, path = ?h.path,
                                        "too many events already held: EIO (fill requested)");
                                    h.p.answer(DENY_EIO);
                                    return Step::Done;
                                }
                                info!(ino = h.p.ino, pid = h.p.pid, path = ?h.path,
                                    "unfilled rewind directory: holding the open while it fills");
                            }
                            return Step::Hold(h);
                        }
                    }
                }
                Verdict::Need {
                    marker,
                    start,
                    end,
                    whole_file,
                    huge,
                } => {
                    let req = AccessRequest {
                        key: self.key(&h.p, &marker),
                        marker,
                        start,
                        end,
                        whole_file,
                        huge,
                        path: h.path.as_deref(),
                        identity: &h.identity,
                        first,
                    };
                    let mut a = self.broker.on_access(&req);
                    if huge && self.cfg.huge_policy == HugePolicy::Deny && a != Answer::Recheck {
                        if first {
                            info!(ino = h.p.ino, start, end, path = ?h.path,
                                "range above the size cap: EIO (recall requested)");
                        }
                        a = Answer::Deny;
                    }
                    match a {
                        Answer::Deny => {
                            if first {
                                info!(ino = h.p.ino, pid = h.p.pid, start, end, path = ?h.path,
                                    identity = ?h.identity, "needs bytes: EIO now (failfast)");
                            }
                            h.p.answer(DENY_EIO);
                            return Step::Done;
                        }
                        Answer::Recheck if !rechecked => {
                            rechecked = true;
                            continue;
                        }
                        Answer::Recheck | Answer::Wait => {
                            if first {
                                if self.stats.held.load(Ordering::Relaxed) >= self.max_held {
                                    self.stats.held_cap_denied.fetch_add(1, Ordering::Relaxed);
                                    warn!(ino = h.p.ino, pid = h.p.pid, max_held = self.max_held,
                                        path = ?h.path, "too many reads already held: EIO (recall requested)");
                                    h.p.answer(DENY_EIO);
                                    return Step::Done;
                                }
                                info!(ino = h.p.ino, pid = h.p.pid, start, end, path = ?h.path,
                                    identity = ?h.identity, "needs bytes: holding the read");
                            }
                            return Step::Hold(h);
                        }
                    }
                }
            }
        }
        // Repeated stale clean-ups or rechecks without progress.
        warn!(ino = h.p.ino, "no stable verdict; EIO (fail closed)");
        h.p.answer(DENY_EIO);
        Step::Done
    }

    fn hold(&self, h: Held<I::Identity>) {
        self.stats.held.fetch_add(1, Ordering::Relaxed);
        self.held
            .lock()
            .unwrap()
            .entry(h.p.ino)
            .or_default()
            .push(h);
    }

    /// The held-events loop. Never returns.
    pub(crate) fn held_loop(self: &Arc<Self>, rx: Receiver<Wake>) {
        let mut next_poll = Instant::now() + self.cfg.poll_interval;
        loop {
            let timeout = next_poll.saturating_duration_since(Instant::now());
            let due: Vec<Held<I::Identity>> = match rx.recv_timeout(timeout) {
                Ok(Wake::Ino(ino)) => self.take_held(Some(ino)),
                Ok(Wake::All) => self.take_held(None),
                Err(RecvTimeoutError::Timeout) => {
                    next_poll = Instant::now() + self.cfg.poll_interval;
                    self.take_held(None)
                }
                Err(RecvTimeoutError::Disconnected) => {
                    std::thread::sleep(self.cfg.poll_interval);
                    self.take_held(None)
                }
            };
            for h in due {
                self.stats.held.fetch_sub(1, Ordering::Relaxed);
                let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    self.evaluate(h, false)
                }));
                if let Ok(Step::Hold(h)) = r {
                    self.hold(h);
                }
            }
        }
    }

    fn take_held(&self, ino: Option<u64>) -> Vec<Held<I::Identity>> {
        let mut m = self.held.lock().unwrap();
        match ino {
            Some(i) => m.remove(&i).unwrap_or_default(),
            None => m.drain().flat_map(|(_, v)| v).collect(),
        }
    }

    // ------------------------------------------------------ O_TRUNC clean-up

    fn wait_clearing(&self, ino: u64) {
        let mut s = self.clearing.lock().unwrap();
        while s.contains(&ino) {
            s = self.clearing_cv.wait(s).unwrap();
        }
    }

    /// Clear the stub state of a `PARTIAL` file now smaller than its marker:
    /// an `O_TRUNC` open (no pre-content event) replaced it.
    ///
    /// Every user write after the truncation raises an event first, and that
    /// event lands here (the file is smaller than its marker), so under this
    /// inode's clean-up lock no user byte has landed since the truncation.
    /// Any bytes present were written by a fill that raced the truncation,
    /// and are discarded. (Growth past the marker, an `O_APPEND`, is
    /// [`Verdict::Extended`] and never comes here.)
    /// Order: remove the marker (the fill's next check fails, and a widen
    /// can't recreate it), wait the fill out, truncate its bytes away
    /// through the fill mount (no event), report.
    pub fn clear_stale(
        &self,
        fd: BorrowedFd<'_>,
        key: InodeKey,
        handle: Option<&Handle>,
        path: Option<&std::path::Path>,
    ) {
        {
            let mut s = self.clearing.lock().unwrap();
            if s.contains(&key.ino) {
                while s.contains(&key.ino) {
                    s = self.clearing_cv.wait(s).unwrap();
                }
                return;
            }
            s.insert(key.ino);
        }
        let r = self.clear_stale_locked(fd, key, handle, path);
        if let Err(e) = r {
            error!(ino = key.ino, err = %e, path = ?path, "could not clear the stale stub state");
        }
        self.clearing.lock().unwrap().remove(&key.ino);
        self.clearing_cv.notify_all();
    }

    fn clear_stale_locked(
        &self,
        fd: BorrowedFd<'_>,
        key: InodeKey,
        handle: Option<&Handle>,
        path: Option<&std::path::Path>,
    ) -> std::io::Result<()> {
        let f = inspect(fd);
        let MarkerView::Valid(m) = f.marker else {
            return Ok(());
        };
        // Only a file smaller than its marker was truncated; a larger one was
        // appended to, and its bytes are the user's plus holes to fill.
        if m.is_fully_resident() || f.st_size >= m.size {
            return Ok(());
        }
        xattr::remove(fd)?;
        match xattr::remove_raw(fd, MAP_XATTR_NAME) {
            Ok(()) => {}
            Err(e) if sys::errno_of(&e) == libc::ENODATA => {}
            Err(e) => return Err(e),
        }
        self.broker.quiesce_fill(key, &m);
        let size = sys::fstat(fd)?.st_size as u64;
        let mut discarded = 0;
        if size > 0 {
            let h = handle.ok_or_else(|| std::io::Error::other("no file handle"))?;
            let w = sys::open_by_handle(self.fill_fd, h, libc::O_WRONLY)?;
            if sys::fstat(w.as_fd())?.st_ino != key.ino {
                return Err(std::io::Error::other("handle resolved to another inode"));
            }
            sys::ftruncate(w.as_fd(), 0)?;
            discarded = size;
        }
        self.stats.stale_cleared.fetch_add(1, Ordering::Relaxed);
        warn!(
            ino = key.ino,
            file_id = m.file_id,
            version_id = m.version_id,
            old_size = m.size,
            discarded,
            path = ?path,
            "stub overwritten with O_TRUNC: stub state cleared"
        );
        self.broker.stub_overwritten(key, &m, discarded);
        Ok(())
    }
}
