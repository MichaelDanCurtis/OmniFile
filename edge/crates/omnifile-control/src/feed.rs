//! The feed poller (control API §5).
//!
//! The cursor is opaque: the poller stores what the service sent as `next`
//! and sends it back, **including after an empty page** (`next` can advance
//! past events this edge doesn't receive). The caller owns durability: it
//! applies a page, stores `page.next` in its replica in the same transaction,
//! and only then calls [`FeedPoller::advance`]. Until then, `poll` re-reads
//! the same page (at-least-once delivery, made harmless by `entity_version`).

use std::time::Duration;

use crate::client::ControlClient;
use crate::error::{ControlError, Result};
use crate::types::{FEED_START, FeedPage};
use crate::util::Backoff;

pub struct FeedPoller {
    client: ControlClient,
    cursor: String,
    pub limit: u32,
    /// Long-poll seconds (0–30).
    pub wait_s: u32,
    backoff: Backoff,
}

impl FeedPoller {
    /// Resume from the replica's durable cursor, or `None` for a new edge.
    pub fn new(client: ControlClient, cursor: Option<String>) -> Self {
        Self {
            client,
            cursor: cursor.unwrap_or_else(|| FEED_START.to_owned()),
            limit: 500,
            wait_s: 25,
            backoff: Backoff::default(),
        }
    }

    /// Replace the retry backoff (tests use a short one).
    pub fn with_backoff(mut self, backoff: Backoff) -> Self {
        self.backoff = backoff;
        self
    }

    pub fn cursor(&self) -> &str {
        &self.cursor
    }

    /// One page from the current cursor. Errors are returned, not retried;
    /// see [`FeedPoller::poll_until_ok`].
    pub async fn poll(&mut self) -> Result<FeedPage> {
        let page = self
            .client
            .feed(&self.cursor, self.limit, self.wait_s)
            .await?;
        self.backoff.reset();
        Ok(page)
    }

    /// Poll, sleeping with jittered backoff (1 s .. 5 min) while the service
    /// is unavailable. Non-retryable errors (for example `cursor_expired`)
    /// are returned. `on_unavailable` is told each failure, for status.
    pub async fn poll_until_ok(
        &mut self,
        mut on_unavailable: impl FnMut(&ControlError, Duration),
    ) -> Result<FeedPage> {
        loop {
            match self.poll().await {
                Ok(p) => return Ok(p),
                Err(e) if e.is_retryable() => {
                    let d = self.backoff.next_delay(e.retry_after());
                    on_unavailable(&e, d);
                    tokio::time::sleep(d).await;
                }
                Err(e) => return Err(e),
            }
        }
    }

    /// The page is durably applied: continue from its `next`.
    pub fn advance(&mut self, next: &str) {
        self.cursor = next.to_owned();
    }

    /// After a re-bootstrap, continue from the snapshot's `as_of`.
    pub fn reset_to(&mut self, cursor: &str) {
        self.cursor = cursor.to_owned();
    }

    /// Record progress with the service (monitoring only; the edge's own
    /// cursor is authoritative).
    pub async fn ack(&self) -> Result<()> {
        self.client.feed_ack(&self.cursor).await
    }
}
