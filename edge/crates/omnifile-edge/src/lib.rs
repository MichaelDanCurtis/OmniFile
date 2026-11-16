//! OmniFile Gateway edge daemon.
//!
//! * [`config`]: the configuration file, `/etc/omnifile/edge.toml`.
//! * [`ports`]: the traits the daemon uses to reach the service and blob
//!   storage (implemented by the Edge-I/O crates through adapters, and by
//!   [`fakes`] for tests).
//! * Linux only: [`checks`] (refuse to start off xfs or without
//!   `CAP_SYS_ADMIN`), [`core`] (FSAL handler, recalls, fills), [`feed_loop`],
//!   [`stubber`], [`commit`], [`daemon`] (wiring and shutdown), [`guard`]
//!   (the smb-guard and AD identity) and [`acl`] (catalog ACLs on
//!   placeholders, normalisation).
//! * [`annex`]: annexes (`docs/design/annex.md`, phase A1), run by the
//!   separate `omnifile-annex` binary, one unit per annex.

pub mod actor;
pub mod annex;
pub mod config;
pub mod fakes;
pub mod fdstore;
pub mod helper_restart;
pub mod io;
pub mod ports;
pub mod quiet;

#[cfg(target_os = "linux")]
pub mod acl;
#[cfg(target_os = "linux")]
pub mod carry;
#[cfg(target_os = "linux")]
pub mod checks;
#[cfg(target_os = "linux")]
pub mod commit;
#[cfg(target_os = "linux")]
pub mod core;
#[cfg(target_os = "linux")]
pub mod daemon;
#[cfg(target_os = "linux")]
pub mod feed_loop;
#[cfg(target_os = "linux")]
pub mod guard;
#[cfg(target_os = "linux")]
pub mod nsync;
#[cfg(target_os = "linux")]
pub mod offline_sweep;
pub mod pending;
#[cfg(target_os = "linux")]
pub mod remote;
#[cfg(target_os = "linux")]
pub mod repair;
#[cfg(target_os = "linux")]
pub mod rewind;
#[cfg(target_os = "linux")]
pub mod shares;
#[cfg(target_os = "linux")]
pub mod stubber;

/// Install the tracing subscriber: JSON lines for the journal, or text.
/// `RUST_LOG` overrides the configured filter.
pub fn init_logging(format: &str, filter: &str) {
    use tracing_subscriber::EnvFilter;
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new(filter));
    let builder = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false);
    let _ = if format == "json" {
        builder.json().flatten_event(true).try_init()
    } else {
        builder.try_init()
    };
}
