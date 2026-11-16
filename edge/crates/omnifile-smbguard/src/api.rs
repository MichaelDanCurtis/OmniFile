//! What the guard needs from the rest of the daemon, and its configuration.
//!
//! The guard decides **whether bytes are local** from the marker alone. It
//! never decides **how to get them**: that is the [`RecallBroker`]'s job
//! (the daemon's recall table, as for `READ_INTENT` from `FSAL_OMNI`). The
//! broker can make the guard wait or deny; it can never make the guard
//! allow a range the marker does not cover (`fsal-socket.md` §1, principle 2).

use std::fmt::Debug;
use std::os::fd::BorrowedFd;
use std::path::{Path, PathBuf};
use std::time::Duration;

use omnifile_marker::Marker;
use omnifile_stub::InodeKey;

use crate::classify::Limits;

/// The process behind an event, while the event is still pending (so the
/// smbd session it belongs to still exists, spike A12).
pub struct EventOrigin<'a> {
    /// The tgid that caused the event: for SMB, the smbd child serving the
    /// session (its pthreadpool workers share the tgid).
    pub pid: i32,
    /// From `FAN_REPORT_PIDFD`, when the process still existed: it pins the
    /// process, so a reused pid is never attributed.
    pub pidfd: Option<BorrowedFd<'a>>,
}

/// Who caused an event. Implemented by M2 integration over the Identity
/// track's `omnifile_identity::Resolver` (`resolve_smb`).
///
/// Called on a guard worker thread, once per event that needs bytes, before
/// the event is answered. It must not fail and must return promptly (a
/// cached lookup); on error it returns an identity that says so.
pub trait EventIdentity: Send + Sync + 'static {
    type Identity: Send + Sync + Clone + Debug + 'static;
    fn resolve(&self, origin: &EventOrigin<'_>) -> Self::Identity;
}

/// The pid and command name only (tests, and before identity is wired).
#[derive(Clone, Copy, Debug, Default)]
pub struct PidOnly;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PidIdentity {
    pub pid: i32,
    pub comm: String,
}

impl EventIdentity for PidOnly {
    type Identity = PidIdentity;
    fn resolve(&self, o: &EventOrigin<'_>) -> PidIdentity {
        let comm = std::fs::read_to_string(format!("/proc/{}/comm", o.pid))
            .map(|s| s.trim().to_string())
            .unwrap_or_else(|_| "?".into());
        PidIdentity { pid: o.pid, comm }
    }
}

/// One access that needs bytes the marker does not cover.
#[derive(Debug)]
pub struct AccessRequest<'a, Id> {
    /// `(fsid, ino, ino_gen)` as the FSAL computes them, `file_id` from the marker.
    pub key: InodeKey,
    pub marker: Marker,
    /// `[start, end)`, clipped to the marker's size.
    pub start: u64,
    pub end: u64,
    /// A whole-file access: `(0,0)` (truncate to 0 or clone), a truncate,
    /// or a write past EOF. Like the FSAL's `needs_full_fill()`.
    pub whole_file: bool,
    /// More than [`GuardConfig::limits`]`.max_wait_bytes` still to fetch.
    pub huge: bool,
    /// The file's path when the event arrived (a hint: it goes stale on rename).
    pub path: Option<&'a Path>,
    pub identity: &'a Id,
    /// False when the guard asks again for an event it is holding (every
    /// [`GuardConfig::poll_interval`], and on [`crate::Waker::wake`]).
    pub first: bool,
}

/// An unfilled rewind directory being opened (smbd listing a previous
/// version's folder, `fsal-namespace.md` section 7).
#[derive(Debug)]
pub struct DirRequest<'a, Id> {
    /// The directory's inode; `file_id` from its marker.
    pub key: InodeKey,
    pub marker: Marker,
    pub path: Option<&'a Path>,
    pub identity: &'a Id,
    /// False when the guard asks again for an event it is holding.
    pub first: bool,
}

/// What the broker says about an access.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Answer {
    /// Hold the event; a fill is coming. The guard re-reads the marker on
    /// every wake and poll, and allows once it covers the range.
    Wait,
    /// Answer `EIO` now (failfast, waitquick over its threshold, a failed
    /// recall, or the service refused). The broker has queued the recall.
    Deny,
    /// The fill finished: re-read the marker now.
    Recheck,
}

/// The daemon's side of a guarded access. Implemented by M2 integration
/// over the daemon's recall table (`omnifile-edge` `Core`).
///
/// Every method runs on a guard thread and must not block for long:
/// [`RecallBroker::on_access`] answers from memory and starts slow work in
/// the background, like `on_read_intent`.
pub trait RecallBroker<Id>: Send + Sync + 'static {
    /// Start (or join) the recall or fetch for `req`, and say what to do now.
    /// Idempotent: called again for every re-check of a held event.
    fn on_access(&self, req: &AccessRequest<'_, Id>) -> Answer;

    /// Fill the unfilled rewind directory `req` names (the daemon's
    /// `POPULATE_DIR`), and say what to do now: `Wait` while it fills (the
    /// guard is woken, or polls, and allows once the marker has lost
    /// `DIR_UNPOPULATED`), `Deny` if it can't be filled. Idempotent. The
    /// default denies: an unfilled directory is never listed empty.
    fn on_dir_open(&self, _req: &DirRequest<'_, Id>) -> Answer {
        Answer::Deny
    }

    /// A `PARTIAL` file was found overwritten by an `O_TRUNC` open (which
    /// raises no pre-content event), and its marker has just been removed.
    /// Return only when no fill write for this file can land any more:
    /// stop or wait out a running fill. Its next marker check fails, and a
    /// widen uses `XATTR_REPLACE`, so it can't recreate the marker.
    fn quiesce_fill(&self, key: InodeKey, old: &Marker);

    /// The stub state of an overwritten file was cleared: tell the service
    /// (the new bytes are the user's, and become a new version on commit).
    /// `discarded` counts bytes that a racing fill had written into it and
    /// that the guard truncated away.
    fn stub_overwritten(&self, _key: InodeKey, _old: &Marker, _discarded: u64) {}

    /// A `PARTIAL` file grew past its marker's size: an `O_APPEND` write
    /// landed while `[resident_bytes, old.size)` was not local (the kernel
    /// reports an append's event at the file position, not at the end of
    /// file, so it cannot be held for a fill first). The bytes past
    /// `resident_bytes` up to `old.size` are holes that must be the old
    /// content. The guard denies (`EIO`) unless this says otherwise: the
    /// daemon may repair the file (fill the old range through the fill
    /// mount, then rewrite or drop the marker) and answer `Wait`.
    fn stub_extended(&self, _key: InodeKey, _old: &Marker, _st_size: u64) -> Answer {
        Answer::Deny
    }

    /// `FAN_MODIFY` (or a writable close) on a file whose marker is
    /// `PARTIAL` and whose size still equals the marker's: someone wrote
    /// into its local prefix outside the fill mount. Called on the watcher
    /// thread; must not block. The daemon marks the file dirty, fills it
    /// whole, then commits it.
    fn partial_modified(&self, _key: InodeKey, _marker: &Marker) {}

    /// Asked before a size mismatch is acted on (truncated: clear the stub
    /// state; extended: [`RecallBroker::stub_extended`]). `false` means the
    /// daemon is replacing the file's content itself: the guard holds the
    /// event and re-classifies it on the next wake or poll, and the watcher
    /// leaves the file alone.
    fn may_act_on_size_mismatch(&self, _key: InodeKey, _marker: &Marker) -> bool {
        true
    }
}

/// What a `huge` need gets.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HugePolicy {
    /// Deny with `EIO` at once (the recall is still started, so a later,
    /// smaller access finds the bytes). The default.
    Deny,
    /// Treat it like any other need (wait if the broker says wait).
    AsRequested,
}

/// How the `O_TRUNC` watcher listens. `O_TRUNC` raises no pre-content
/// event, only a notification (`notify_change()` → `FS_MODIFY`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TruncWatch {
    Off,
    /// `FAN_CLOSE_WRITE`: one event per writable close (the default).
    CloseWrite,
    /// `FAN_MODIFY`: every write. Catches a truncated file still held open,
    /// at the cost of an event per write on the whole volume.
    Modify,
}

/// Where the guard's private fill mount comes from (recommended design 6).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FillMount {
    /// A bind mount of the data volume that already exists in this
    /// process's **private** mount namespace (the appliance unit uses
    /// `PrivateMounts=yes` and `BindPaths=`).
    Existing(PathBuf),
    /// `unshare(CLONE_NEWNS)` and bind-mount the volume here ourselves
    /// (standalone runs; needs a single-threaded process).
    PrivateBind(PathBuf),
}

impl FillMount {
    pub fn path(&self) -> &Path {
        match self {
            FillMount::Existing(p) | FillMount::PrivateBind(p) => p,
        }
    }
}

#[derive(Clone, Debug)]
pub struct GuardConfig {
    /// The xfs data volume: the only filesystem ever marked.
    pub data_root: PathBuf,
    /// Its filesystem UUID, recorded at setup. The guard refuses to mark a
    /// filesystem with any other UUID (an unmounted path would mark `/`).
    pub fs_uuid: [u8; 16],
    pub fill_mount: FillMount,
    /// The event journal; on tmpfs (`/run`).
    pub journal_path: PathBuf,
    pub journal_max_bytes: u64,
    pub limits: Limits,
    pub huge_policy: HugePolicy,
    /// Threads that resolve identity and ask the broker.
    pub workers: usize,
    /// How often a held event is re-checked without a wake.
    pub poll_interval: Duration,
    /// Initialise new groups with `FAN_REPORT_PIDFD`.
    pub report_pidfd: bool,
    /// Refuse to create a group unless it can be pushed into the systemd fd
    /// store (`$NOTIFY_SOCKET` and `$FDSTORE` set). Only tests turn it off.
    pub require_fdstore: bool,
    /// Descriptors kept free below `RLIMIT_NOFILE`.
    pub fd_reserve: u64,
    /// The restart sweep denies fd numbers up to the highest journalled one
    /// plus this.
    pub sweep_extra: i32,
    /// Add ignore marks on wholly local inodes (A13).
    pub ignore_marks: bool,
    pub trunc_watch: TruncWatch,
    /// Run when the guard had to create a new group while smbd or Ganesha
    /// run: their open files are unguarded for life (A9). `None`: log only.
    pub restart_servers: Option<Vec<String>>,
    /// Process names that count as file servers.
    pub server_comms: Vec<String>,
    /// Send `READY=1` once armed. The daemon may turn it off and send its
    /// own after the rest of its startup; the unit is `Type=notify` either way.
    pub notify_ready: bool,
    /// Test only (A6, the deny-sweep): read events without journalling
    /// them, as if the guard crashed between `read()` and the journal write.
    pub test_skip_journal: bool,
    /// Most events held at once waiting for bytes. A new event that would
    /// be held beyond it is denied (`EIO`) instead, so held events can never
    /// use up the in-flight budget and stall resident reads.
    /// `None`: half the in-flight budget.
    pub max_held: Option<u64>,
}

impl GuardConfig {
    pub fn new(data_root: impl Into<PathBuf>, fs_uuid: [u8; 16], fill_mount: FillMount) -> Self {
        GuardConfig {
            data_root: data_root.into(),
            fs_uuid,
            fill_mount,
            journal_path: PathBuf::from("/run/omnifile/smbguard/journal"),
            journal_max_bytes: 16 << 20,
            limits: Limits::default(),
            huge_policy: HugePolicy::Deny,
            workers: 32,
            poll_interval: Duration::from_secs(2),
            report_pidfd: true,
            require_fdstore: true,
            fd_reserve: 1024,
            sweep_extra: 4096,
            ignore_marks: true,
            trunc_watch: TruncWatch::CloseWrite,
            restart_servers: Some(
                [
                    "/usr/bin/systemctl",
                    "try-restart",
                    "--no-block",
                    "smbd.service",
                    "nfs-ganesha-omni.service",
                ]
                .iter()
                .map(|s| s.to_string())
                .collect(),
            ),
            server_comms: vec!["smbd".into(), "ganesha.nfsd".into()],
            notify_ready: true,
            test_skip_journal: false,
            max_held: None,
        }
    }
}

/// Parse a UUID in its usual text form (`blkid`, `xfs_admin -u`).
pub fn parse_uuid(s: &str) -> Option<[u8; 16]> {
    let hex: String = s.trim().chars().filter(|c| *c != '-').collect();
    if hex.len() != 32 {
        return None;
    }
    let mut out = [0u8; 16];
    for (i, b) in out.iter_mut().enumerate() {
        *b = u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).ok()?;
    }
    Some(out)
}

pub fn format_uuid(u: &[u8; 16]) -> String {
    let h: String = u.iter().map(|b| format!("{b:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uuid_round_trip() {
        let s = "0b6c5f0e-3c2a-4f1e-9d6b-2a7f00c1d2e3";
        let u = parse_uuid(s).unwrap();
        assert_eq!(format_uuid(&u), s);
        assert_eq!(parse_uuid("0b6c"), None);
        assert_eq!(parse_uuid("zz6c5f0e-3c2a-4f1e-9d6b-2a7f00c1d2e3"), None);
    }

    #[test]
    fn defaults_are_fail_closed() {
        let c = GuardConfig::new(
            "/srv/omnifile/data",
            [1; 16],
            FillMount::Existing("/x".into()),
        );
        assert!(c.require_fdstore);
        assert_eq!(c.huge_policy, HugePolicy::Deny);
        assert!(c.journal_path.starts_with("/run"));
        assert_eq!(c.fill_mount.path(), Path::new("/x"));
    }
}
