//! Who asked: the AD identity behind an NFS or SMB request (decision 28,
//! `docs/design/access-control.md`, `AGENTS.md` "Identity chain").
//!
//! - **NFS.** `FSAL_OMNI` sends the caller's uid, gid, sec flavour and client
//!   address (`fsal-socket.md` §4.2). [`Resolver::resolve_nfs`] maps the uid to
//!   an AD SID through winbind. With `sec=sys` the uid is only *claimed* by the
//!   client host; with `krb5*` it is *proven*.
//! - **SMB.** Samba reads the local filesystem, so the smb-guard only sees a
//!   fanotify event whose pid is the smbd child serving the session (spike A12).
//!   [`Resolver::resolve_smb`] maps that pid to the session user with
//!   `smbstatus --json` **while the request is pending**, and checks the
//!   event's pidfd (`FAN_REPORT_PIDFD`) afterwards so a reused pid is never
//!   attributed to the wrong person. SMB users authenticated against AD, so
//!   they are *proven*.
//!
//! Winbind and Samba are GPL-3.0. This crate never links them: it runs the
//! `wbinfo` and `smbstatus` programs as separate processes (`LICENSES.md`).
//! `libwbclient` is LGPL-3.0+ and would be allowed, but a subprocess keeps the
//! daemon free of any Samba ABI and is fast enough behind the caches here.
//!
//! Resolution never fails and never blocks unboundedly: every subprocess has a
//! timeout, and a failed lookup yields an identity with `sid = None`,
//! `proof = claimed` and the reason in `error`. A stub read is never held up
//! because AD is slow; it is attributed less precisely instead.

mod cache;
mod cmd;
pub mod fake;
mod idmap;
mod resolver;
mod sid;
mod smb;
mod trust;

pub use cache::TtlCache;
pub use cmd::{CmdError, run_bounded};
pub use idmap::{CachedIdMap, IdMap, IdMapError, SidKind, Wbinfo};
pub use resolver::{
    Attribution, IdentityResolver, NfsRequest, ResolvedIdentity, Resolver, ResolverConfig,
    SmbRequest,
};
pub use sid::{Sid, SidError};
pub use smb::{ProcInfo, SessionSource, SmbSession, SmbstatusCmd, SysProc, parse_smbstatus_json};
pub use trust::{NfsSec, Proof, Requester, Sec, TrustLevel, Via};
