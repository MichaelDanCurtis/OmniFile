//! Read reporting for the `last_read` policy clock (control API §4.7).
//!
//! The edge coalesces to **one entry per file per hour**: the latest read
//! time in that hour. An hour's entries are sent once the hour is over (so
//! the entry really is that hour's latest), at least hourly, in batches of up
//! to 10,000. While the service is unreachable they are held. Since the
//! service keeps the later of the stored and reported times, an older pending
//! entry for a file is simply replaced by a newer one, so at most one entry
//! per file is ever pending.
//!
//! Pending entries live in memory: a restart loses at most the last hour or
//! so of read times, which only delays a policy clock.

use std::collections::HashMap;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::client::ControlClient;
use crate::error::Result;
use crate::types::ReadEntry;
use crate::util::{format_time, new_idempotency_key};

pub const MAX_BATCH: usize = 10_000;
const HOUR: u64 = 3600;

fn hour_of(t: SystemTime) -> u64 {
    t.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() / HOUR
}

#[derive(Default)]
pub struct ReadsBatcher {
    /// file_id -> latest read time not yet reported.
    pending: HashMap<u64, SystemTime>,
}

impl ReadsBatcher {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn len(&self) -> usize {
        self.pending.len()
    }

    pub fn is_empty(&self) -> bool {
        self.pending.is_empty()
    }

    /// A read of `file_id` at `at`.
    pub fn record(&mut self, file_id: u64, at: SystemTime) {
        let e = self.pending.entry(file_id).or_insert(at);
        if at > *e {
            *e = at;
        }
    }

    /// Entries ready to send at `now`: those whose hour is over, or all of
    /// them with `force` (shutdown). Removed from the batcher; give them back
    /// with [`ReadsBatcher::restore`] if sending fails.
    pub fn take_ready(&mut self, now: SystemTime, force: bool) -> Vec<(u64, SystemTime)> {
        let current = hour_of(now);
        let mut out = Vec::new();
        self.pending.retain(|&id, &mut t| {
            if force || hour_of(t) < current {
                out.push((id, t));
                false
            } else {
                true
            }
        });
        out.sort_unstable();
        out
    }

    pub fn restore(&mut self, entries: impl IntoIterator<Item = (u64, SystemTime)>) {
        for (id, t) in entries {
            self.record(id, t);
        }
    }

    /// Send what is ready, in batches of up to 10,000. On failure the unsent
    /// entries are kept and the error is returned. Returns the number sent.
    pub async fn flush(
        &mut self,
        client: &ControlClient,
        now: SystemTime,
        force: bool,
    ) -> Result<usize> {
        let ready = self.take_ready(now, force);
        let mut sent = 0;
        let mut chunks = ready.chunks(MAX_BATCH);
        while let Some(chunk) = chunks.next() {
            let entries: Vec<ReadEntry> = chunk
                .iter()
                .map(|&(file_id, t)| ReadEntry {
                    file_id,
                    last_read_at: format_time(t),
                })
                .collect();
            match client.report_reads(&entries, &new_idempotency_key()).await {
                Ok(r) => {
                    if !r.unknown_file_ids.is_empty() {
                        tracing::debug!(
                            n = r.unknown_file_ids.len(),
                            "service doesn't know some read files"
                        );
                    }
                    sent += chunk.len();
                }
                Err(e) => {
                    self.restore(chunk.iter().copied());
                    for rest in chunks {
                        self.restore(rest.iter().copied());
                    }
                    return Err(e);
                }
            }
        }
        Ok(sent)
    }

    /// Time until the next hour boundary: when to flush next.
    pub fn next_flush_in(now: SystemTime) -> Duration {
        let secs = now.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
        Duration::from_secs(HOUR - secs % HOUR + 1)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(h: u64, m: u64) -> SystemTime {
        UNIX_EPOCH + Duration::from_secs(1_790_000_000 / HOUR * HOUR + h * HOUR + m * 60)
    }

    #[test]
    fn one_entry_per_file_per_hour_latest_wins() {
        let mut b = ReadsBatcher::new();
        b.record(1, t(0, 5));
        b.record(1, t(0, 40));
        b.record(1, t(0, 20));
        b.record(2, t(0, 1));
        // Still in hour 0: nothing ready.
        assert!(b.take_ready(t(0, 59), false).is_empty());
        let ready = b.take_ready(t(1, 0), false);
        assert_eq!(ready, vec![(1, t(0, 40)), (2, t(0, 1))]);
        assert!(b.is_empty());
    }

    #[test]
    fn current_hour_waits_and_force_sends() {
        let mut b = ReadsBatcher::new();
        b.record(1, t(0, 5));
        b.record(2, t(1, 5));
        assert_eq!(b.take_ready(t(1, 10), false), vec![(1, t(0, 5))]);
        assert_eq!(b.take_ready(t(1, 10), true), vec![(2, t(1, 5))]);
    }

    #[test]
    fn restore_keeps_latest() {
        let mut b = ReadsBatcher::new();
        b.record(1, t(0, 50));
        let taken = b.take_ready(t(1, 0), false);
        b.record(1, t(1, 2));
        b.restore(taken);
        assert_eq!(b.len(), 1);
        assert_eq!(b.take_ready(t(2, 0), false), vec![(1, t(1, 2))]);
    }

    #[test]
    fn next_flush_is_after_the_hour() {
        let d = ReadsBatcher::next_flush_in(t(3, 59));
        assert!(d <= Duration::from_secs(61) && d >= Duration::from_secs(60));
    }
}
