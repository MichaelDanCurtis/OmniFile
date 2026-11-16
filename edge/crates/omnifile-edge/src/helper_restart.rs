//! Restarting the AWS signing helper onto a new Roles Anywhere certificate
//! (decision 50; review on #32).
//!
//! A new certificate is installed with a mark on disk
//! (`omnifile_control::roles_anywhere::RESTART_FILE`). The helper reads its
//! certificate only at start, so until it restarts it keeps presenting the
//! old one, which expires. The mark is cleared only when a restart succeeds;
//! a failed one is retried on later heartbeats, backing off from one minute
//! to an hour, and logged as an error from the third failure on.

use std::future::Future;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use omnifile_control::RolesAnywhereFiles;
use tracing::{error, info, warn};

/// Failures in a row after which each one is logged as an error.
pub const ERROR_AFTER: u32 = 3;

#[derive(Debug)]
pub struct HelperRestart {
    failures: u32,
    next_at: Option<Instant>,
    base: Duration,
    max: Duration,
}

impl Default for HelperRestart {
    fn default() -> Self {
        Self::new(Duration::from_secs(60), Duration::from_secs(3600))
    }
}

impl HelperRestart {
    pub fn new(base: Duration, max: Duration) -> Self {
        Self {
            failures: 0,
            next_at: None,
            base,
            max,
        }
    }

    pub fn failures(&self) -> u32 {
        self.failures
    }

    fn due(&self, now: Instant) -> bool {
        self.next_at.is_none_or(|t| now >= t)
    }

    fn record(&mut self, ok: bool, now: Instant) {
        if ok {
            self.failures = 0;
            self.next_at = None;
        } else {
            self.failures += 1;
            let wait = self
                .base
                .saturating_mul(1 << (self.failures - 1).min(16))
                .min(self.max);
            self.next_at = Some(now + wait);
        }
    }
}

/// Restarts the helper with `run` when a new certificate waits for it and no
/// backoff holds it. `None`: nothing to do now; `Some(ok)`: it ran.
pub async fn restart_if_pending<F, Fut>(
    files: &RolesAnywhereFiles,
    state: &Mutex<HelperRestart>,
    now: Instant,
    run: F,
) -> Option<bool>
where
    F: FnOnce() -> Fut,
    Fut: Future<Output = bool>,
{
    if !files.restart_pending() || !state.lock().unwrap().due(now) {
        return None;
    }
    let mut ok = run().await;
    if ok && let Err(e) = files.restart_done() {
        warn!(err = %e, "clearing the signing helper's restart mark");
        ok = false;
    }
    let mut st = state.lock().unwrap();
    st.record(ok, now);
    match (ok, st.failures) {
        (true, _) => {
            info!("the AWS signing helper restarted onto the new Roles Anywhere certificate")
        }
        (false, n) if n >= ERROR_AFTER => error!(
            failures = n,
            "the AWS signing helper still presents the old Roles Anywhere certificate, which \
             expires: restart omnifile-aws-signing-helper.service; retried with backoff"
        ),
        (false, n) => warn!(
            failures = n,
            "restarting the AWS signing helper failed; retried"
        ),
    }
    Some(ok)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};

    #[tokio::test]
    async fn a_failed_restart_is_retried_with_backoff_until_it_works() {
        let dir = tempfile::tempdir().unwrap();
        let files = RolesAnywhereFiles::new(dir.path().join("ra"));
        let st = Mutex::new(HelperRestart::new(
            Duration::from_secs(60),
            Duration::from_secs(3600),
        ));
        let t0 = Instant::now();
        let runs = AtomicU32::new(0);
        // A runner that fails once, then succeeds.
        let runner = || async { runs.fetch_add(1, Ordering::SeqCst) > 0 };
        assert_eq!(
            restart_if_pending(&files, &st, t0, runner).await,
            None,
            "nothing installed"
        );
        files.mark_restart_pending().unwrap();
        assert_eq!(
            restart_if_pending(&files, &st, t0, runner).await,
            Some(false)
        );
        assert!(files.restart_pending(), "kept for the next heartbeat");
        assert_eq!(st.lock().unwrap().failures(), 1);
        // Backing off: not before a minute has passed.
        let soon = t0 + Duration::from_secs(30);
        assert_eq!(restart_if_pending(&files, &st, soon, runner).await, None);
        assert_eq!(runs.load(Ordering::SeqCst), 1);
        let later = t0 + Duration::from_secs(61);
        assert_eq!(
            restart_if_pending(&files, &st, later, runner).await,
            Some(true)
        );
        assert!(!files.restart_pending());
        assert_eq!(st.lock().unwrap().failures(), 0);
        assert_eq!(restart_if_pending(&files, &st, later, runner).await, None);
        assert_eq!(runs.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn the_backoff_doubles_up_to_an_hour() {
        let mut st = HelperRestart::default();
        let t0 = Instant::now();
        let mut waits = Vec::new();
        for _ in 0..9 {
            st.record(false, t0);
            waits.push(st.next_at.unwrap() - t0);
        }
        let mins: Vec<u64> = waits.iter().map(|d| d.as_secs() / 60).collect();
        assert_eq!(mins, [1, 2, 4, 8, 16, 32, 60, 60, 60]);
        st.record(true, t0);
        assert!(st.due(t0));
    }
}
