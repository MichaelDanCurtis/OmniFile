//! The edge's cache of remote objects (M5, edge data path track;
//! `docs/milestones/M5.md`, `docs/contracts/control-api.md` §4.4).
//!
//! Edge-local: the SSD budget, LRU per chunk, headers first in and last out,
//! a read-ahead planner for sequential access, `prefetch_headers` at a rate
//! cap, statistics for the heartbeat and egress per backend. The cache is not
//! reported to the service and needs no stub lease. The interface the
//! namespace track calls is `Cache::{fetch(range), evict(budget)}`.
//!
//! The bytes live in the placeholders themselves (R1 §5.2): a fetched chunk
//! is written into the file and recorded in its chunk map by
//! `omnifile_stub::chunks`, and eviction punches it again by the stub
//! handshake. This crate decides what to fetch and what to evict.
//!
//! * [`index`]: per-chunk LRU and header accounting (portable).
//! * [`readahead`]: sequential-read detection (portable).
//! * [`rate`]: the header prefetch rate cap (portable).
//! * `Cache` (Linux): fetch, read-ahead, eviction, prefetch, statistics.

pub mod index;
pub mod rate;
pub mod readahead;

#[cfg(target_os = "linux")]
mod cache;
#[cfg(target_os = "linux")]
pub use cache::{
    Cache, CacheConfig, CacheError, CacheStats, EvictReport, FetchOutcome, FetchRequest,
};
