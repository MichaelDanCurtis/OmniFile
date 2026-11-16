//! A NAS that stops answering.
//!
//! NFS mounts are `hard`: a call into a NAS that has gone quiet blocks in the
//! kernel until it answers, and the run loop is in it, so nothing is asked,
//! reported or bounded. One thread per mount only stats it. When a stat hasn't
//! returned within the limit, the unit tells the service (`say_hung`, bounded so it
//! can't hang too), and then exits.
//!
//! **Telling the service comes first, because the exit doesn't always get out.**
//! The kernel can hold a dying process until the NAS answers, depending on what it
//! has open (`annex.md` section 3.2 has the table, from runs on kernel 7.0):
//! - With nothing being written (a scan, a read), it went at once in those runs. The unit restarts,
//!   its mount namespace taking the mounts with it, and with no mount it asks,
//!   reports and bounds its placements (`Runner::while_unmounted`).
//! - In a fill, over NFSv3 or NFSv4, closing the file waits for its dirty pages: it
//!   stays in D state, SIGKILL does nothing, and systemd still shows the unit active
//!   and running. Nothing more is reported, bounded or heard until the NAS answers,
//!   when it goes and the unit restarts by itself; or until someone restarts the
//!   unit, when systemd gives up on the old process after its stop timeouts and
//!   starts a new one.
//!
//! Everything it does is journalled to survive this; a kill is tested at every step.
//!
//! `soft` looks simpler and isn't: every stat and read would wait out its own
//! timeout, one after another, for as many files as a tick touches.
//!
//! A slow pass isn't a hang, so the loop's own progress says nothing (a scan of
//! 10^7 files or a 200 GB copy can take hours). Only a stat still out counts.

use std::future::Future;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use super::service::AnnexService;

/// What a hang leaves in the state directory for the next start to report, if
/// the service couldn't be told.
pub const HANG_NOTE: &str = "nas_hang";

/// How long telling the service may take: the unit has to get on with exiting.
pub const SAY_BOUND: Duration = Duration::from_secs(20);

/// How often each mount is asked.
const PROBE_EVERY: Duration = Duration::from_secs(15);

/// Whether a stat is still out, and has been for too long. `asked_ms` is when the
/// one in flight was asked, plus one, or 0 with none in flight: time between
/// stats isn't time without an answer.
fn hung(asked_ms: u64, now_ms: u64, limit_ms: u64) -> bool {
    asked_ms != 0 && now_ms.saturating_sub(asked_ms - 1) >= limit_ms
}

/// Calls `on_hang` once, with the mount and how long it has been quiet, when a
/// `probe` of one of `paths` hasn't returned within `limit`. A probe that returns
/// an error has answered: that is for the caller's own error paths.
pub fn watch(
    paths: Vec<PathBuf>,
    limit: Duration,
    every: Duration,
    probe: impl Fn(&Path) + Send + Sync + 'static,
    on_hang: impl FnOnce(&Path, u64) + Send + 'static,
) {
    let start = Instant::now();
    let probe = Arc::new(probe);
    let asked: Vec<(PathBuf, Arc<AtomicU64>)> = paths
        .into_iter()
        .map(|p| (p, Arc::new(AtomicU64::new(0))))
        .collect();
    for (p, a) in &asked {
        let (p, a, probe) = (p.clone(), a.clone(), probe.clone());
        std::thread::spawn(move || {
            loop {
                a.store(start.elapsed().as_millis() as u64 + 1, Ordering::SeqCst);
                probe(&p);
                a.store(0, Ordering::SeqCst);
                std::thread::sleep(every);
            }
        });
    }
    let limit_ms = limit.as_millis() as u64;
    let tick = (limit / 4).clamp(Duration::from_millis(1), Duration::from_secs(5));
    std::thread::spawn(move || {
        loop {
            std::thread::sleep(tick);
            let now = start.elapsed().as_millis() as u64;
            let quiet = asked.iter().find_map(|(p, a)| {
                let at = a.load(Ordering::SeqCst);
                hung(at, now, limit_ms).then(|| (p, now - (at - 1)))
            });
            if let Some((p, ms)) = quiet {
                on_hang(p, ms / 1000);
                return;
            }
        }
    });
}

/// The live probe: its attributes last a second (`actimeo=1`), so this reaches the NAS.
pub fn stat(p: &Path) {
    let _ = std::fs::symlink_metadata(p);
}

/// `watch` with the live probe.
pub fn watch_mounts(
    paths: Vec<PathBuf>,
    limit: Duration,
    on_hang: impl FnOnce(&Path, u64) + Send + 'static,
) {
    watch(paths, limit, PROBE_EVERY, stat, on_hang);
}

/// The report of a mount that stopped answering: the filer's health, and a problem.
pub fn hung_report(what: &str, sec: &str, at: &str) -> Value {
    json!({"kind": "mount", "observed_at": at,
           "details": {"ok": false, "read_only": null, "readable": false, "sec": sec,
                       "hung": true, "error": what}})
}

/// `call`, or false once `bound` is up: nothing here may hang too.
pub async fn within<T, E>(bound: Duration, call: impl Future<Output = Result<T, E>>) -> bool {
    matches!(tokio::time::timeout(bound, call).await, Ok(Ok(_)))
}

/// Tell the service a mount stopped answering, before trying to exit. Whether it heard.
pub async fn say_hung<S: AnnexService>(
    svc: &S,
    annex_id: u64,
    report: &Value,
    key: &str,
    bound: Duration,
) -> bool {
    within(
        bound,
        svc.events(annex_id, std::slice::from_ref(report), key),
    )
    .await
}

/// Leave a note of a hang, for the next start.
pub fn leave_note(state_dir: &Path, what: &str) {
    let _ = std::fs::write(state_dir.join(HANG_NOTE), what);
}

/// The note a hang left, once.
pub fn take_note(state_dir: &Path) -> Option<String> {
    let p = state_dir.join(HANG_NOTE);
    let what = std::fs::read_to_string(&p).ok()?;
    let _ = std::fs::remove_file(&p);
    Some(what)
}

#[cfg(test)]
mod tests {
    use super::super::service::Fake;
    use super::*;
    use std::sync::mpsc;

    #[test]
    fn only_a_stat_still_out_counts() {
        assert!(!hung(0, 999_999, 1000), "none in flight");
        assert!(!hung(1001, 1999, 1000));
        assert!(hung(1001, 2000, 1000));
        assert!(!hung(5001, 1000, 1000), "no underflow");
    }

    #[test]
    fn a_mount_that_stops_answering_is_named() {
        let (tx, rx) = mpsc::channel();
        let quiet = PathBuf::from("/quiet");
        let q = quiet.clone();
        watch(
            vec![PathBuf::from("/fine"), quiet.clone()],
            Duration::from_millis(200),
            Duration::from_millis(10),
            move |p| {
                if p == q {
                    std::thread::park();
                }
            },
            move |p, secs| tx.send((p.to_owned(), secs)).unwrap(),
        );
        let (p, _) = rx.recv_timeout(Duration::from_secs(10)).unwrap();
        assert_eq!(p, quiet);
    }

    #[test]
    fn mounts_that_answer_never_hang_even_with_errors() {
        let (tx, rx) = mpsc::channel();
        watch(
            vec![PathBuf::from("/no/such/path")],
            Duration::from_millis(200),
            Duration::from_millis(10),
            stat,
            move |p, _| tx.send(p.to_owned()).unwrap(),
        );
        assert!(rx.recv_timeout(Duration::from_millis(800)).is_err());
    }

    #[test]
    fn a_limit_shorter_than_the_wait_between_stats_doesnt_fire_on_a_healthy_mount() {
        // hang_s of 15 or less always fired: the wait counted as no answer.
        let (tx, rx) = mpsc::channel();
        watch(
            vec![PathBuf::from("/no/such/path")],
            Duration::from_millis(100),
            Duration::from_millis(400),
            stat,
            move |p, _| tx.send(p.to_owned()).unwrap(),
        );
        assert!(rx.recv_timeout(Duration::from_millis(1500)).is_err());
    }

    #[test]
    fn each_stat_is_timed_from_when_it_was_asked() {
        // Answers come, each too late: it isn't saved by the last one's answer.
        let (tx, rx) = mpsc::channel();
        watch(
            vec![PathBuf::from("/slow")],
            Duration::from_millis(100),
            Duration::from_millis(1),
            |_| std::thread::sleep(Duration::from_millis(400)),
            move |p, _| tx.send(p.to_owned()).unwrap(),
        );
        assert!(rx.recv_timeout(Duration::from_secs(5)).is_ok());
    }

    #[tokio::test]
    async fn the_service_is_told_as_a_hung_mount() {
        let f = Fake::default();
        let r = hung_report(
            "/mnt has not answered for 300 s",
            "sys",
            "2026-11-06T00:00:00.000Z",
        );
        assert!(say_hung(&f, 7, &r, "hang-1", SAY_BOUND).await);
        assert_eq!(f.state.lock().unwrap().batches["hang-1"].len(), 1);
        assert_eq!(r["details"]["hung"], true);
        assert_eq!(r["details"]["ok"], false);
        // Down: not told, and it says so.
        f.state.lock().unwrap().down = true;
        assert!(!say_hung(&f, 7, &r, "hang-2", SAY_BOUND).await);
    }

    #[tokio::test]
    async fn telling_the_service_cant_hang_too() {
        let started = Instant::now();
        let never = std::future::pending::<Result<(), ()>>();
        assert!(!within(Duration::from_millis(50), never).await);
        assert!(started.elapsed() < Duration::from_secs(5));
    }

    #[test]
    fn a_hang_leaves_a_note_for_the_next_start_once() {
        let d = tempfile::tempdir().unwrap();
        assert_eq!(take_note(d.path()), None);
        leave_note(d.path(), "quiet");
        assert_eq!(take_note(d.path()).as_deref(), Some("quiet"));
        assert_eq!(take_note(d.path()), None);
    }
}
