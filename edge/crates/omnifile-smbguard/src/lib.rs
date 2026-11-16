//! The smb-guard: fail-closed fanotify pre-content guard for SMB and local
//! reads of stubs (`docs/pilots/filer-spike-results-A.md`, "Recommended
//! design"; `docs/milestones/M2.md`, SMB guard track).
//!
//! Samba reads the xfs volume directly, so a stub's holes would read as
//! zeros. One `FAN_CLASS_PRE_CONTENT` group with a `FAN_MARK_FILESYSTEM`
//! `FAN_PRE_ACCESS` mark on the data volume makes every read, write,
//! `mmap()`, truncate and clone of a file on it wait for the guard's answer.
//! fanotify **fails open** (a released group, an overflowed queue, a file
//! opened before any group existed all read zeros), so every measure below
//! exists to keep that from happening:
//!
//! | Measure | Where |
//! |---|---|
//! | Group held in the systemd fd store, created once, reused on restart; `READY=1` only once the mark is armed and fdinfo proves it is on the volume whose xfs UUID was recorded | [`linux::setup`], `edge/systemd/omnifile-edge.service.d/10-smbguard.conf` |
//! | `FAN_UNLIMITED_QUEUE` (checked on reuse too) | [`linux::setup`] |
//! | Journal written before acting; orphan replay with reserved fd numbers; deny-sweep | [`journal`], [`linux::start`] |
//! | Private fill mount carrying a mount ignore mark, verified to be a separate mount in a private namespace | [`linux::setup`] |
//! | Ignore marks on wholly local inodes through a read-only fd from the fill mount; explicit removal before narrowing ([`omnifile_stub::SmbGuardHook`]) | [`linux::marks`] |
//! | `(0,0)` is whole-file; a size cap for huge ranges | [`classify`] |
//! | Never exit on `EMFILE`/`ENFILE`; in-flight events capped below the fd limit; an event dropped unanswered is denied | [`linux::engine`] |
//! | Decisions from the marker only, with the FSAL's rules | [`classify`] |
//! | Fill fence for SMB/local writers: truncates and writes of a `PARTIAL` file wait for the bytes they touch; an `O_TRUNC` overwrite (no event) is caught and its stub state cleared, discarding any racing fill bytes; an `O_APPEND` that landed early fails closed | [`classify`], [`linux::truncwatch`] |
//! | Identity at event time (pid, pidfd) | [`api::EventIdentity`] |
//! | smbd and Ganesha `Requires=`/`BindsTo=`/`After=` the guard; ops interlock | `edge/systemd/`, [`interlock`] |
//!
//! The appliance runs the guard inside `omnifile-edge` (one unit holds the
//! group, the journal and the recall table): the daemon calls
//! [`linux::start`] first thing in `main`, with its recall table as the
//! [`api::RecallBroker`] and the Identity track's resolver as the
//! [`api::EventIdentity`]. `omnifile-smbguard-harness` runs the same
//! library standalone with a file-backed broker for the A-suite.

pub mod api;
pub mod classify;
pub mod fdinfo;
pub mod interlock;
pub mod journal;
pub mod sdnotify;
pub mod uapi;

#[cfg(target_os = "linux")]
pub mod linux;

pub use api::{
    AccessRequest, Answer, DirRequest, EventIdentity, EventOrigin, FillMount, GuardConfig,
    HugePolicy, PidIdentity, PidOnly, RecallBroker, TruncWatch,
};
pub use classify::Limits;
#[cfg(target_os = "linux")]
pub use linux::engine::Waker;
#[cfg(target_os = "linux")]
pub use linux::{GuardError, GuardHandle, Origin, start};
