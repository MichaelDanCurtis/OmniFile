//! omnifile-smbguard-harness: the production guard library, standalone, with
//! a file-backed recall broker. It exists for the A-suite
//! (`tests/a-suite/`): the appliance runs the guard inside `omnifile-edge`.
//!
//! The broker fills stubs through the production fill path
//! (`omnifile_stub::fill::fill`) from a synthetic backend: the bytes of file
//! `file_id` are the test pattern with seed `file_id`. Behaviour is steered
//! by files in `--state DIR`, re-read on every access:
//! - `policy`: `wait` (default), `failfast` or `waitquick:<ms>`;
//! - `delay_ms`: latency before a fill's first byte;
//! - `rate_mbps`: fill rate cap (0 = none);
//! - `fail`: if present, fills fail.
//!
//! Every recall is logged to `DIR/recalls.log` with the identity.

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("omnifile-smbguard-harness runs on Linux only");
    std::process::exit(2);
}

#[cfg(target_os = "linux")]
fn main() {
    linux::main()
}

#[cfg(target_os = "linux")]
mod linux {
    use std::collections::{HashMap, HashSet};
    use std::future::Future;
    use std::io::Write;
    use std::path::{Path, PathBuf};
    use std::pin::pin;
    use std::sync::{Arc, Mutex, OnceLock};
    use std::task::{Context, Poll, Wake, Waker as TaskWaker};
    use std::time::{Duration, Instant};

    use omnifile_marker::Marker;
    use omnifile_smbguard::api::parse_uuid;
    use omnifile_smbguard::{
        AccessRequest, Answer, DirRequest, FillMount, GuardConfig, HugePolicy, PidIdentity,
        PidOnly, RecallBroker, TruncWatch, Waker,
    };
    use omnifile_stub::fill::{FillOptions, FillRequest, fill};
    use omnifile_stub::{
        FetchTarget, FileLocks, FsalNotifier, InodeKey, Layout, MarkerSeq, RangeSource,
    };
    use tracing::{info, warn};

    // ---- a minimal executor: the fill's futures only ever wait on threads.
    struct ThreadWake(std::thread::Thread);
    impl Wake for ThreadWake {
        fn wake(self: Arc<Self>) {
            self.0.unpark();
        }
    }
    fn block_on<F: Future>(f: F) -> F::Output {
        let mut f = pin!(f);
        let w = TaskWaker::from(Arc::new(ThreadWake(std::thread::current())));
        let mut cx = Context::from_waker(&w);
        loop {
            if let Poll::Ready(v) = f.as_mut().poll(&mut cx) {
                return v;
            }
            std::thread::park_timeout(Duration::from_millis(50));
        }
    }

    const PAT: u64 = 0x4F4D_4E49_4649_4C45; // "OMNIFILE"

    /// The pattern: the LE u64 at 8-aligned offset o is `o ^ PAT ^ seed`.
    pub fn pattern(off: u64, len: usize, seed: u64) -> Vec<u8> {
        let base = off & !7;
        let skip = (off - base) as usize;
        let mut v = Vec::with_capacity(len + 16);
        let mut o = base;
        while v.len() < skip + len {
            v.extend_from_slice(&(o ^ PAT ^ seed).to_le_bytes());
            o += 8;
        }
        v[skip..skip + len].to_vec()
    }

    /// CRC-64/NVME of the first `size` bytes of the pattern with `seed`.
    fn pattern_crc(size: u64, seed: u64) -> u64 {
        let mut c = omnifile_integrity::Crc64::new();
        let mut off = 0;
        while off < size {
            let n = (size - off).min(8 << 20);
            c.update(&pattern(off, n as usize, seed));
            off += n;
        }
        c.finalize()
    }

    fn state(dir: &Path, name: &str, default: &str) -> String {
        std::fs::read_to_string(dir.join(name))
            .map(|s| s.trim().to_string())
            .unwrap_or_else(|_| default.to_string())
    }

    struct Source {
        state: PathBuf,
        delayed: Mutex<bool>,
    }

    impl RangeSource for Source {
        async fn get_range(
            &self,
            t: &FetchTarget,
            offset: u64,
            len: u64,
        ) -> std::io::Result<Vec<u8>> {
            if self.state.join("fail").exists() {
                return Err(std::io::Error::other("backend failure (state/fail)"));
            }
            let first = std::mem::replace(&mut *self.delayed.lock().unwrap(), true);
            if !first {
                let d: u64 = state(&self.state, "delay_ms", "0").parse().unwrap_or(0);
                std::thread::sleep(Duration::from_millis(d));
            }
            let rate: u64 = state(&self.state, "rate_mbps", "0").parse().unwrap_or(0);
            if rate > 0 {
                std::thread::sleep(Duration::from_micros(len * 1_000_000 / (rate << 20)));
            }
            Ok(pattern(offset, len as usize, t.file_id))
        }
    }

    struct Notifier(Arc<OnceLock<Waker>>);
    impl FsalNotifier for Notifier {
        async fn invalidate(&self, _k: InodeKey, _w: u32, _ack: bool) -> bool {
            true
        }
        fn fill_progress(&self, key: InodeKey, _v: u64, _r: u64) {
            if let Some(w) = self.0.get() {
                w.wake(key.ino);
            }
        }
        async fn begin_fill(&self, _k: InodeKey, _v: u64) -> Option<u64> {
            Some(0)
        }
        fn fence_holds(&self, _e: u64) -> bool {
            true
        }
    }

    #[derive(Default)]
    struct FillState {
        running: bool,
        failed_at: Option<Instant>,
    }

    struct Broker {
        data_root: PathBuf,
        fill_root: PathBuf,
        state: PathBuf,
        waker: Arc<OnceLock<Waker>>,
        fills: Arc<Mutex<HashMap<u64, FillState>>>,
        locks: Arc<FileLocks>,
        seq: Arc<MarkerSeq>,
        /// Directories being filled now, by inode.
        dir_fills: Arc<Mutex<HashSet<u64>>>,
    }

    /// The daemon's `POPULATE_DIR`, as the harness plays it: after
    /// `delay_ms`, write `filled.txt` into the directory and clear its
    /// `DIR_UNPOPULATED` flag (as `rewind::populate` does, flag last).
    fn fill_dir(dir: &Path, seq: &MarkerSeq) -> Result<(), String> {
        use omnifile_marker::Flags;
        use omnifile_marker::xattr::{self, ReadOutcome};
        use std::os::fd::AsFd;
        std::fs::write(dir.join("filled.txt"), "filled\n").map_err(|e| e.to_string())?;
        let f = std::fs::File::open(dir).map_err(|e| e.to_string())?;
        let Ok(ReadOutcome::Marker(mut m)) = xattr::read(f.as_fd()) else {
            return Err("no marker".into());
        };
        m.flags = m.flags.without(Flags::DIR_UNPOPULATED);
        m.marker_seq = seq.next();
        xattr::write_replace(f.as_fd(), &m).map_err(|e| e.to_string())
    }

    impl Broker {
        fn rel(&self, p: &Path) -> Option<PathBuf> {
            p.strip_prefix(&self.data_root)
                .or_else(|_| p.strip_prefix(&self.fill_root))
                .ok()
                .map(Path::to_path_buf)
        }

        fn start_fill(&self, req: &AccessRequest<'_, PidIdentity>) -> bool {
            let fid = req.marker.file_id;
            {
                let mut f = self.fills.lock().unwrap();
                let e = f.entry(fid).or_default();
                if e.running {
                    return false;
                }
                e.running = true;
                e.failed_at = None;
            }
            let Some(rel) = req.path.and_then(|p| self.rel(p)) else {
                warn!(?req.path, "cannot place the file under the data root");
                self.fills.lock().unwrap().entry(fid).or_default().running = false;
                return false;
            };
            let mut layout = Layout::new(&self.data_root);
            layout.fill_root = self.fill_root.clone();
            let target = FetchTarget {
                file_id: fid,
                version_id: req.marker.version_id,
                url: format!("synthetic:{fid}"),
                source: omnifile_stub::SourceKind::AzureSas,
                size: req.marker.size,
                // A fill verifies the whole file against the catalog's
                // CRC-64/NVME and refuses without one; the "catalog" here is
                // the synthetic pattern's own CRC (computed in the fill thread).
                crc64nvme: None,
            };
            let freq = FillRequest {
                rel,
                expect_ino: Some((req.key.ino, req.key.ino_gen)),
            };
            let (fills, locks, seq, waker, st) = (
                self.fills.clone(),
                self.locks.clone(),
                self.seq.clone(),
                self.waker.clone(),
                self.state.clone(),
            );
            let ino = req.key.ino;
            std::thread::spawn(move || {
                let t0 = Instant::now();
                let mut target = target;
                target.crc64nvme = Some(pattern_crc(target.size, target.file_id));
                let src = Source {
                    state: st,
                    delayed: Mutex::new(false),
                };
                let n = Notifier(waker.clone());
                let opts = FillOptions {
                    chunk: 8 << 20,
                    widen_every: 8 << 20,
                };
                let r = block_on(fill(&layout, &freq, &target, &src, &n, &seq, &locks, opts));
                {
                    let mut f = fills.lock().unwrap();
                    let e = f.entry(fid).or_default();
                    e.running = false;
                    if r.is_err() {
                        e.failed_at = Some(Instant::now());
                    }
                }
                match &r {
                    Ok(n) => info!(
                        file_id = fid,
                        bytes = n,
                        secs = t0.elapsed().as_secs_f64(),
                        "fill done"
                    ),
                    Err(e) => warn!(file_id = fid, err = %e, "fill failed"),
                }
                if let Some(w) = waker.get() {
                    w.wake(ino);
                }
            });
            true
        }
    }

    impl RecallBroker<PidIdentity> for Broker {
        fn on_access(&self, req: &AccessRequest<'_, PidIdentity>) -> Answer {
            let fid = req.marker.file_id;
            if let Some(t) = self
                .fills
                .lock()
                .unwrap()
                .get(&fid)
                .and_then(|f| f.failed_at)
                && t.elapsed() < Duration::from_secs(5)
            {
                return Answer::Deny;
            }
            let started = self.start_fill(req);
            if started || req.first {
                let line = format!(
                    "RECALL file_id={fid} range=[{},{}) whole={} huge={} pid={} comm={} path={}\n",
                    req.start,
                    req.end,
                    req.whole_file,
                    req.huge,
                    req.identity.pid,
                    req.identity.comm,
                    req.path
                        .map(|p| p.display().to_string())
                        .unwrap_or_default()
                );
                let _ = std::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(self.state.join("recalls.log"))
                    .and_then(|mut f| f.write_all(line.as_bytes()));
            }
            let pol = state(&self.state, "policy", "wait");
            if pol == "failfast" {
                return Answer::Deny;
            }
            if let Some(ms) = pol.strip_prefix("waitquick:") {
                let limit: u64 = ms.parse().unwrap_or(0);
                // The ETA is the configured latency until the fill has begun.
                let eta: u64 = if started || req.first {
                    state(&self.state, "delay_ms", "0").parse().unwrap_or(0)
                } else {
                    0
                };
                if req.first && eta > limit {
                    return Answer::Deny;
                }
            }
            Answer::Wait
        }

        fn on_dir_open(&self, req: &DirRequest<'_, PidIdentity>) -> Answer {
            let pol = state(&self.state, "dirpolicy", "fill");
            let ino = req.key.ino;
            if req.first {
                let line = format!(
                    "POPULATE ino={ino} file_id={} pid={} comm={} path={}\n",
                    req.marker.file_id,
                    req.identity.pid,
                    req.identity.comm,
                    req.path
                        .map(|p| p.display().to_string())
                        .unwrap_or_default()
                );
                let _ = std::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(self.state.join("populates.log"))
                    .and_then(|mut f| f.write_all(line.as_bytes()));
            }
            if pol == "fail" {
                return Answer::Deny;
            }
            if pol == "fill" && self.dir_fills.lock().unwrap().insert(ino) {
                let Some(rel) = req.path.and_then(|p| self.rel(p)) else {
                    self.dir_fills.lock().unwrap().remove(&ino);
                    return Answer::Deny;
                };
                // Through the data root, as the daemon does: its own
                // directory opens are never held.
                let dir = self.data_root.join(rel);
                let delay: u64 = state(&self.state, "delay_ms", "0").parse().unwrap_or(0);
                let (fills, seq, waker) =
                    (self.dir_fills.clone(), self.seq.clone(), self.waker.clone());
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_millis(delay));
                    let r = fill_dir(&dir, &seq);
                    info!(dir = %dir.display(), ?r, "directory filled");
                    fills.lock().unwrap().remove(&ino);
                    if let Some(w) = waker.get() {
                        w.wake(ino);
                    }
                });
            }
            // "hang": never filled; the open waits.
            Answer::Wait
        }

        fn quiesce_fill(&self, key: InodeKey, old: &Marker) {
            let t0 = Instant::now();
            while self.locks.is_locked(old.file_id) && t0.elapsed() < Duration::from_secs(120) {
                std::thread::sleep(Duration::from_millis(10));
            }
            // `quiesce_delay_ms`: stretch the O_TRUNC clean-up window, so the
            // A-suite can land a parallel write inside it (review B1).
            let d: u64 = state(&self.state, "quiesce_delay_ms", "0")
                .parse()
                .unwrap_or(0);
            std::thread::sleep(Duration::from_millis(d));
            info!(ino = key.ino, file_id = old.file_id, "fill quiesced");
        }

        fn partial_modified(&self, key: InodeKey, marker: &Marker) {
            let line = format!(
                "MODIFIED ino={} file_id={} size={} resident={}\n",
                key.ino, marker.file_id, marker.size, marker.resident_bytes
            );
            let _ = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(self.state.join("modified.log"))
                .and_then(|mut f| f.write_all(line.as_bytes()));
        }

        fn stub_overwritten(&self, key: InodeKey, old: &Marker, discarded: u64) {
            let line = format!(
                "OVERWRITTEN ino={} file_id={} old_size={} discarded={discarded}\n",
                key.ino, old.file_id, old.size
            );
            let _ = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(self.state.join("overwritten.log"))
                .and_then(|mut f| f.write_all(line.as_bytes()));
        }
    }

    fn num(s: &str) -> u64 {
        let s = s.trim();
        let (n, m) = match s.chars().last() {
            Some('K' | 'k') => (&s[..s.len() - 1], 1u64 << 10),
            Some('M' | 'm') => (&s[..s.len() - 1], 1 << 20),
            Some('G' | 'g') => (&s[..s.len() - 1], 1 << 30),
            _ => (s, 1),
        };
        n.parse::<u64>().unwrap_or(0) * m
    }

    pub fn main() {
        omnifile_smbguard_harness_logging();
        let a: Vec<String> = std::env::args().collect();
        let arg = |n: &str| {
            a.iter()
                .position(|x| x == n)
                .and_then(|i| a.get(i + 1))
                .cloned()
        };
        let flag = |n: &str| a.iter().any(|x| x == n);
        let (Some(data), Some(uuid), Some(fillp), Some(statep)) = (
            arg("--data"),
            arg("--uuid"),
            arg("--fill-mount"),
            arg("--state"),
        ) else {
            eprintln!(
                "usage: omnifile-smbguard-harness --data DIR --uuid UUID --fill-mount DIR --state DIR \
                 [--private-bind] [--journal PATH] [--no-fdstore] [--no-pidfd] [--no-ignore] \
                 [--trunc off|close|modify] [--max-wait N] [--huge deny|wait] [--workers N] [--poll-ms N] [--max-held N] [--server-comm NAME]"
            );
            std::process::exit(2);
        };
        let Some(uuid) = parse_uuid(&uuid) else {
            eprintln!("bad --uuid");
            std::process::exit(2);
        };
        let fill_mount = if flag("--private-bind") {
            FillMount::PrivateBind(fillp.clone().into())
        } else {
            FillMount::Existing(fillp.clone().into())
        };
        let mut cfg = GuardConfig::new(&data, uuid, fill_mount);
        let statep = PathBuf::from(statep);
        cfg.journal_path = arg("--journal")
            .map(PathBuf::from)
            .unwrap_or_else(|| statep.join("journal"));
        cfg.require_fdstore = !flag("--no-fdstore");
        cfg.report_pidfd = !flag("--no-pidfd");
        cfg.ignore_marks = !flag("--no-ignore");
        cfg.trunc_watch = match arg("--trunc").as_deref() {
            Some("off") => TruncWatch::Off,
            Some("modify") => TruncWatch::Modify,
            _ => TruncWatch::CloseWrite,
        };
        if let Some(n) = arg("--max-wait") {
            cfg.limits.max_wait_bytes = num(&n);
        }
        if arg("--huge").as_deref() == Some("wait") {
            cfg.huge_policy = HugePolicy::AsRequested;
        }
        if let Some(n) = arg("--workers") {
            cfg.workers = num(&n) as usize;
        }
        if let Some(n) = arg("--poll-ms") {
            cfg.poll_interval = Duration::from_millis(num(&n));
        }
        cfg.test_skip_journal = flag("--test-no-journal");
        if let Some(n) = arg("--max-held") {
            cfg.max_held = Some(num(&n));
        }
        // Another process name that counts as a file server (x02 lists
        // rewind directories with `ls` as a stand-in for smbd).
        if let Some(c) = arg("--server-comm") {
            cfg.server_comms.push(c);
        }
        // Tests run without restarting real servers.
        cfg.restart_servers = None;
        let waker = Arc::new(OnceLock::new());
        let locks = Arc::new(FileLocks::new());
        let seq = Arc::new(MarkerSeq::new());
        let broker = Broker {
            data_root: PathBuf::from(&data),
            fill_root: PathBuf::from(&fillp),
            state: statep.clone(),
            waker: waker.clone(),
            fills: Arc::new(Mutex::new(HashMap::new())),
            locks: locks.clone(),
            seq: seq.clone(),
            dir_fills: Arc::new(Mutex::new(HashSet::new())),
        };
        match omnifile_smbguard::start(cfg, broker, PidOnly) {
            Ok(h) => {
                let _ = waker.set(h.waker.clone());
                info!(origin = ?h.origin, group = %h.group_id, "harness running");
                let mut layout = Layout::new(&data);
                layout.fill_root = PathBuf::from(&fillp);
                let marks = h.marks.clone();
                let dirs = h.dirs.clone();
                std::thread::spawn(move || {
                    ctl_loop(statep.join("ctl"), layout, marks, dirs, seq, locks)
                });
                loop {
                    std::thread::park();
                }
            }
            Err(e) => {
                eprintln!("omnifile-smbguard-harness: {e}");
                std::process::exit(1);
            }
        }
    }

    /// `DIR/ctl/restub.<n>` containing `<path relative to the data root>
    /// <resident_bytes>`: run the production stub handshake
    /// (`omnifile_stub::handshake::stub`, with the guard's ignore-mark hook)
    /// and write the outcome to `restub.<n>.done`.
    ///
    /// `DIR/ctl/watchdir.<n>` containing `<path relative to the data root>`:
    /// put the guard's open-permission mark on that directory (as the daemon
    /// does for each unfilled rewind directory); outcome in `.done`.
    fn ctl_loop(
        dir: PathBuf,
        layout: Layout,
        marks: Arc<omnifile_smbguard::linux::marks::IgnoreMarks>,
        dirs: Arc<omnifile_smbguard::linux::marks::DirMarks>,
        seq: Arc<MarkerSeq>,
        locks: Arc<FileLocks>,
    ) {
        use omnifile_marker::xattr::{self, ReadOutcome};
        use omnifile_stub::handshake::{StubRequest, stub};
        use std::os::fd::AsFd;
        let _ = std::fs::create_dir_all(&dir);
        let n = Notifier(Arc::new(OnceLock::new()));
        loop {
            std::thread::sleep(Duration::from_millis(100));
            let Ok(rd) = std::fs::read_dir(&dir) else {
                continue;
            };
            for e in rd.flatten() {
                let p = e.path();
                let name = p
                    .file_name()
                    .and_then(|s| s.to_str())
                    .unwrap_or("")
                    .to_string();
                if name.ends_with(".done") {
                    continue;
                }
                if name.starts_with("watchdir.") {
                    let body = std::fs::read_to_string(&p).unwrap_or_default();
                    let _ = std::fs::remove_file(&p);
                    let outcome = std::fs::File::open(layout.path(Path::new(body.trim())))
                        .and_then(|f| dirs.watch(f.as_fd()))
                        .map_or_else(|e| format!("error: {e}"), |()| "ok".to_string());
                    info!(dir = body.trim(), %outcome, "watchdir");
                    let _ = std::fs::write(dir.join(format!("{name}.done")), outcome + "\n");
                    continue;
                }
                if !name.starts_with("restub.") {
                    continue;
                }
                let body = std::fs::read_to_string(&p).unwrap_or_default();
                let _ = std::fs::remove_file(&p);
                let mut it = body.split_whitespace();
                let (Some(rel), Some(res)) = (it.next(), it.next()) else {
                    continue;
                };
                let rel = PathBuf::from(rel);
                let outcome = match std::fs::File::open(layout.path(&rel))
                    .map_err(|e| e.to_string())
                    .and_then(|f| xattr::read(f.as_fd()).map_err(|e| e.to_string()))
                {
                    Ok(ReadOutcome::Marker(m)) => {
                        let req = StubRequest {
                            rel: rel.clone(),
                            file_id: m.file_id,
                            version_id: m.version_id,
                            resident_bytes: num(res),
                            expect_ino: None,
                            expect_ctime_ns: None,
                        };
                        format!(
                            "{:?}",
                            block_on(stub(&layout, &req, &n, &*marks, &seq, &locks))
                        )
                    }
                    Ok(o) => format!("no marker: {o:?}"),
                    Err(e) => format!("error: {e}"),
                };
                info!(path = %rel.display(), %outcome, "restub");
                let _ = std::fs::write(dir.join(format!("{name}.done")), outcome + "\n");
            }
        }
    }

    fn omnifile_smbguard_harness_logging() {
        let filter = tracing_subscriber::EnvFilter::try_from_default_env()
            .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));
        let _ = tracing_subscriber::fmt()
            .with_env_filter(filter)
            .with_target(false)
            .with_ansi(false)
            .with_writer(std::io::stderr)
            .try_init();
    }
}
