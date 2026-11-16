//! M1 carry-overs (`docs/milestones/M1.md`, "Open items"):
//! - after an online fetch the edge tells the service, so the catalog says
//!   `resident` rather than `stub` ([`Core::report_fetch_filled`]);
//! - a commit that failed (a `create` the service refused or never got, an
//!   upload error) is retried with backoff ([`Retries`]), instead of waiting
//!   for a next close-write that may never come.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use tracing::{debug, info, warn};

use crate::core::Core;
use crate::ports::{BlobIo, CloseWrite, ControlPlane};

/// Attempts to report a fill before giving up (the next fill reports again).
const FILLED_ATTEMPTS: u32 = 8;

impl<C: ControlPlane, B: BlobIo> Core<C, B> {
    /// Report an online fetch's fill, retrying while the service is away.
    pub fn report_fetch_filled(self: Arc<Self>, file_id: u64, version_id: u64, resident: u64) {
        tokio::spawn(async move {
            let mut backoff = Duration::from_secs(1);
            for attempt in 1..=FILLED_ATTEMPTS {
                match self
                    .control
                    .file_filled(file_id, version_id, resident)
                    .await
                {
                    Ok(()) => {
                        debug!(file_id, version_id, resident, "fill reported");
                        return;
                    }
                    Err(e) if e.unavailable && attempt < FILLED_ATTEMPTS => {
                        tokio::time::sleep(backoff).await;
                        backoff = (backoff * 2).min(Duration::from_secs(60));
                    }
                    Err(e) => {
                        warn!(file_id, version_id, err = %e, "could not report the fill");
                        return;
                    }
                }
                if self.is_shutting_down() {
                    return;
                }
            }
        });
    }
}

/// Failed commits waiting for another try, by path.
#[derive(Default)]
pub struct Retries {
    due: HashMap<PathBuf, (CloseWrite, u32, Instant)>,
}

/// Retries per path before the failure is left to the next close-write.
pub const MAX_ATTEMPTS: u32 = 10;

impl Retries {
    /// Backoff before attempt `n` (1-based): 2 s, 4 s, … capped at 5 min.
    pub fn backoff(n: u32) -> Duration {
        Duration::from_secs((2u64 << n.saturating_sub(1).min(8)).min(300))
    }

    /// A commit of `cw` failed: schedule another try (or give up).
    pub fn failed(&mut self, cw: &CloseWrite) {
        self.again(cw, false);
    }

    /// A swept file was not ready to commit (WP9: written within the quiet
    /// period or open for write, or held in the upload outbox): look again
    /// later, on the same backoff. After [`MAX_ATTEMPTS`] its next close or the
    /// next sweep commits it. Other writes that aren't ready are never given up
    /// (`commit::not_yet`, #82 review).
    pub fn deferred(&mut self, cw: &CloseWrite) {
        self.again(cw, true);
    }

    /// A write still open for write over NFSv4, or one the daemon cannot
    /// ask about (no FSAL, or one restarted within its reclaim window):
    /// looked at again on the same backoff, at most every 5 minutes, and
    /// never given up (#54 review). A long wait is logged, so a count that
    /// never falls shows.
    pub fn waiting(&mut self, cw: &CloseWrite, why: &str) {
        let attempts = self.due.get(&cw.path).map(|e| e.1).unwrap_or(0) + 1;
        if attempts == MAX_ATTEMPTS || (attempts > MAX_ATTEMPTS && attempts.is_multiple_of(12)) {
            warn!(path = %cw.path.display(), attempts, why, "still not committed: the writer may not be done");
        } else {
            debug!(path = %cw.path.display(), attempts, why, "waiting for the writer");
        }
        let after = Self::backoff(attempts);
        self.due.insert(
            cw.path.clone(),
            (cw.clone(), attempts, Instant::now() + after),
        );
    }

    fn again(&mut self, cw: &CloseWrite, deferred: bool) {
        let attempts = self.due.get(&cw.path).map(|e| e.1).unwrap_or(0) + 1;
        if attempts > MAX_ATTEMPTS {
            if deferred {
                warn!(path = %cw.path.display(), attempts,
                      "a file the namespace sweep found is still not ready to commit: left to its next close or the next sweep");
            } else {
                warn!(path = %cw.path.display(), attempts, "commit still failing; giving up until the next write");
            }
            self.due.remove(&cw.path);
            return;
        }
        let after = Self::backoff(attempts);
        if deferred {
            debug!(path = %cw.path.display(), attempts, ?after, "swept file not ready; looking again");
        } else {
            info!(path = %cw.path.display(), attempts, ?after, "commit failed; retrying");
        }
        self.due.insert(
            cw.path.clone(),
            (cw.clone(), attempts, Instant::now() + after),
        );
    }

    /// A commit of this path succeeded (or a new write supersedes the retry).
    pub fn clear(&mut self, cw: &CloseWrite) {
        self.due.remove(&cw.path);
    }

    /// Writes whose next try is due now (they stay scheduled until cleared
    /// or failed again).
    pub fn take_due(&mut self) -> Vec<CloseWrite> {
        let now = Instant::now();
        let mut out = Vec::new();
        for (cw, _, at) in self.due.values_mut() {
            if *at <= now {
                out.push(cw.clone());
                // Not due again until it fails again (failed() reschedules).
                *at = now + Duration::from_secs(3600);
            }
        }
        out
    }

    pub fn len(&self) -> usize {
        self.due.len()
    }

    pub fn is_empty(&self) -> bool {
        self.due.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_grows_and_gives_up() {
        assert_eq!(Retries::backoff(1), Duration::from_secs(2));
        assert_eq!(Retries::backoff(2), Duration::from_secs(4));
        assert_eq!(Retries::backoff(20), Duration::from_secs(300));
        let mut r = Retries::default();
        let cw = CloseWrite::new("/d/s/f".into(), 1);
        r.failed(&cw);
        assert_eq!(r.len(), 1);
        assert!(r.take_due().is_empty(), "not due yet");
        for _ in 0..MAX_ATTEMPTS {
            r.failed(&cw);
        }
        assert!(r.is_empty(), "gave up after MAX_ATTEMPTS");
        r.failed(&cw);
        r.clear(&cw);
        assert!(r.is_empty());
    }

    /// Decision 67: a commit's further wait is one share period, looked at
    /// with [`Retries::deferred`]. Those looks must outlast the longest period
    /// a share can have, or such a file would be left to its next write.
    #[test]
    fn deferred_looks_outlast_the_longest_quiet_period() {
        let looked: u64 = (1..=MAX_ATTEMPTS)
            .map(|n| Retries::backoff(n).as_secs())
            .sum();
        assert!(
            looked > crate::quiet::MAX_SHARE_QUIET_S,
            "{looked} s of looks"
        );
    }

    /// #54 review, S1: a file still open for write (or one the daemon cannot
    /// ask about) is looked at again, at most every 5 minutes, and never
    /// given up, however long the copy takes.
    #[test]
    fn a_wait_for_the_writer_is_never_given_up() {
        let mut r = Retries::default();
        let cw = CloseWrite::new("/d/s/big".into(), 1);
        for _ in 0..(5 * MAX_ATTEMPTS) {
            r.waiting(&cw, "an NFSv4 client holds it open for write");
        }
        assert_eq!(
            r.len(),
            1,
            "still scheduled after {} tries",
            5 * MAX_ATTEMPTS
        );
        let (_, n, at) = r.due.get(&cw.path).unwrap();
        assert_eq!(*n, 5 * MAX_ATTEMPTS);
        assert!(
            *at <= Instant::now() + Duration::from_secs(300),
            "at the 5 min cap"
        );
    }
}
