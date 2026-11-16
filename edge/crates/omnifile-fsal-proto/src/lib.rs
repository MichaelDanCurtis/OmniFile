//! The daemon side of the `FSAL_OMNI` ⇄ `omnifile-edge` socket protocol
//! (`docs/contracts/fsal-socket.md`, v0.1).
//!
//! * [`wire`]: exact binary codecs for every message.
//! * [`server`]: handshake, dispatch to a [`server::Handler`], `RECALL_STATE`
//!   pushes, acknowledged `INVALIDATE`, fail-closed answers, shutdown.
//! * [`socket`] (Linux): the `AF_UNIX`/`SOCK_SEQPACKET` listener with the
//!   `SO_PEERCRED` and `ganesha.nfsd` peer checks.
//! * [`testing`]: an in-memory transport and a scripted FSAL for tests.

pub mod server;
#[cfg(target_os = "linux")]
pub mod socket;
pub mod testing;
pub mod wire;

pub use server::{
    ConnInfo, DEFAULT_GANESHA_EXE, Handler, InvalidateOutcome, Notification, OpenWrites, PacketIo,
    Server, ServerConfig,
};

/// Default socket path.
pub const DEFAULT_SOCKET_PATH: &str = "/run/omnifile/fsal.sock";

#[cfg(test)]
mod server_tests;
