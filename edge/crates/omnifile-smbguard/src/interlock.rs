//! The guard ops interlock (recommended design item 2, open risk 1).
//!
//! The fanotify group must never be released while anything reads the
//! volume: releasing it answers every pending read "allow" (A7), and a
//! released fd store means the next guard creates a new group, under which
//! every file opened before it is unguarded for life (A9). Three operator
//! actions release or endanger the stored group:
//!
//! - `systemctl disable` (with `--now`, or followed by a stop): a stopped
//!   unit that nothing references is garbage-collected and its fd store
//!   closed (A6c-1);
//! - `systemctl clean --what=fdstore`;
//! - removing or masking the unit (an uninstall or upgrade).
//!
//! Defences, in layers:
//! 1. **systemd itself:** `clean` is refused while the unit is active
//!    ("Unit is not inactive or has pending job", A7), and the package ships
//!    the unit's `multi-user.target.wants/` link under `/usr/lib`, which
//!    `systemctl disable` cannot remove, so the unit stays referenced and
//!    its store is kept across `stop` (`edge/systemd/install.sh`).
//! 2. **`omnifile-guardctl`** (this module's rules): the supported way to
//!    stop, disable, clean or uninstall. It refuses while smbd or Ganesha
//!    run, or while the store holds a group and the action would drop it,
//!    unless forced with the servers already stopped.
//!
//! No polkit rule is used: root can always call `systemctl` directly, so the
//! wrapper is a guard rail, and layer 1 is what holds without it.

use std::path::{Path, PathBuf};

/// The drop-ins that bind file servers to the guard (`edge/systemd/`). If
/// any is installed, smbd or Ganesha are meant to run only under an armed
/// guard.
pub const SERVER_DROPINS: &[&str] = &[
    "smbd.service.d/10-omnifile-guard.conf",
    "nfs-ganesha-omni.service.d/10-omnifile-guard.conf",
];

/// Where systemd looks for unit drop-ins, relative to the root.
pub const UNIT_DIRS: &[&str] = &[
    "etc/systemd/system",
    "run/systemd/system",
    "usr/local/lib/systemd/system",
    "usr/lib/systemd/system",
    "lib/systemd/system",
];

/// Server drop-ins installed under `root` (`/` on a live system).
pub fn installed_server_dropins(root: &Path) -> Vec<PathBuf> {
    UNIT_DIRS
        .iter()
        .flat_map(|d| SERVER_DROPINS.iter().map(move |f| root.join(d).join(f)))
        .filter(|p| p.exists())
        .collect()
}

/// The daemon was configured with the guard disabled (`smbguard.enabled =
/// false`). It must not then tell systemd it is ready while smbd or Ganesha
/// are bound to it: they would start and serve stubs with no guard, and
/// SMB reads of holes would return zeros (M2 review S8).
///
/// `Err(why)`: refuse to start (the caller logs it as critical and exits;
/// the servers stay down). `Ok(Some(warning))`: allowed by the explicit
/// override `allow`, still critical. `Ok(None)`: no server drop-in installed.
pub fn disabled_guard_refusal(root: &Path, allow: bool) -> Result<Option<String>, String> {
    let found = installed_server_dropins(root);
    if found.is_empty() {
        return Ok(None);
    }
    let list = found
        .iter()
        .map(|p| p.display().to_string())
        .collect::<Vec<_>>()
        .join(", ");
    if allow {
        Ok(Some(format!(
            "smb-guard DISABLED while file servers are bound to this unit ({list}); \
             override smbguard.allow_disabled_with_servers is set: SMB and local \
             reads of stubs are NOT guarded and can return zeros"
        )))
    } else {
        Err(format!(
            "smb-guard is disabled (smbguard.enabled = false) but file servers are bound to \
             this unit ({list}): refusing to start, so they stay down. Enable the guard, \
             remove those drop-ins, or set smbguard.allow_disabled_with_servers = true"
        ))
    }
}

/// What the operator asks for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Op {
    Status,
    Stop,
    Restart,
    Disable,
    /// `systemctl clean --what=fdstore`: releases the group.
    CleanFdstore,
    Mask,
    Uninstall,
}

impl Op {
    pub fn parse(s: &str) -> Option<Op> {
        Some(match s {
            "status" => Op::Status,
            "stop" => Op::Stop,
            "restart" => Op::Restart,
            "disable" => Op::Disable,
            "clean" | "clean-fdstore" => Op::CleanFdstore,
            "mask" => Op::Mask,
            "uninstall" => Op::Uninstall,
            _ => return None,
        })
    }

    /// Would this drop the stored group (now, or at the next stop)?
    pub fn releases_group(self) -> bool {
        matches!(
            self,
            Op::Disable | Op::CleanFdstore | Op::Mask | Op::Uninstall
        )
    }
}

/// What the wrapper found before acting.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Observed {
    /// Units serving the volume that are active (smbd, Ganesha).
    pub servers_active: Vec<String>,
    /// Local processes that hold files on the volume open (lsof-style scan).
    pub local_openers: Vec<String>,
    pub guard_active: bool,
    /// `NFileDescriptorStore` of the guard unit.
    pub fdstore_fds: u32,
    /// The unit stays referenced while stopped (vendor `.wants` link or enabled).
    pub referenced_when_stopped: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Verdict {
    Allowed,
    /// Allowed, with a warning to print.
    Warn(String),
    Refused(String),
}

/// Decide whether `op` may run. `force` lets a group-releasing op through
/// once no server runs; it never overrides a running server.
pub fn check(op: Op, o: &Observed, force: bool) -> Verdict {
    if op == Op::Status {
        return Verdict::Allowed;
    }
    if op.releases_group() && !o.servers_active.is_empty() {
        return Verdict::Refused(format!(
            "{} running: releasing the guard's fanotify group answers every pending read \
             \"allow\" (stubs read as zeros). Stop them first: systemctl stop {}",
            o.servers_active.join(", "),
            o.servers_active.join(" ")
        ));
    }
    if op.releases_group() && o.fdstore_fds > 0 {
        if !o.local_openers.is_empty() {
            return Verdict::Refused(format!(
                "processes still have files on the volume open ({}); releasing the group \
                 would let their pending reads through as zeros",
                o.local_openers.join(", ")
            ));
        }
        if !force {
            return Verdict::Refused(
                "this releases the stored fanotify group; the next start creates a new one. \
                 Re-run with --force once you are sure nothing reads the volume"
                    .into(),
            );
        }
        return Verdict::Warn("releasing the stored fanotify group (forced)".into());
    }
    if op == Op::Stop && !o.referenced_when_stopped {
        return Verdict::Refused(
            "the guard unit is not referenced when stopped (not enabled, no vendor .wants \
             link): systemd would garbage-collect its fd store on stop (A6c-1). \
             Run: systemctl enable omnifile-edge.service"
                .into(),
        );
    }
    if matches!(op, Op::Stop | Op::Restart) && !o.servers_active.is_empty() {
        return Verdict::Warn(format!(
            "{} will be stopped first (BindsTo=); the group stays in the fd store",
            o.servers_active.join(", ")
        ));
    }
    Verdict::Allowed
}

#[cfg(test)]
mod tests {
    use super::*;

    fn serving() -> Observed {
        Observed {
            servers_active: vec!["smbd.service".into()],
            guard_active: true,
            fdstore_fds: 1,
            referenced_when_stopped: true,
            ..Default::default()
        }
    }

    #[test]
    fn releasing_ops_refused_while_servers_run_even_forced() {
        for op in [Op::Disable, Op::CleanFdstore, Op::Mask, Op::Uninstall] {
            assert!(
                matches!(check(op, &serving(), true), Verdict::Refused(_)),
                "{op:?}"
            );
        }
    }

    #[test]
    fn releasing_needs_force_when_idle() {
        let mut o = serving();
        o.servers_active.clear();
        o.guard_active = false;
        assert!(matches!(
            check(Op::CleanFdstore, &o, false),
            Verdict::Refused(_)
        ));
        assert!(matches!(
            check(Op::CleanFdstore, &o, true),
            Verdict::Warn(_)
        ));
        o.local_openers = vec!["cp(123)".into()];
        assert!(matches!(
            check(Op::CleanFdstore, &o, true),
            Verdict::Refused(_)
        ));
    }

    #[test]
    fn empty_store_needs_no_force() {
        let o = Observed::default();
        assert_eq!(check(Op::Uninstall, &o, false), Verdict::Allowed);
    }

    #[test]
    fn stop_and_restart_are_allowed_with_a_warning() {
        assert!(matches!(
            check(Op::Stop, &serving(), false),
            Verdict::Warn(_)
        ));
        assert!(matches!(
            check(Op::Restart, &serving(), false),
            Verdict::Warn(_)
        ));
    }

    #[test]
    fn stop_refused_when_unit_would_be_collected() {
        let mut o = serving();
        o.referenced_when_stopped = false;
        assert!(matches!(check(Op::Stop, &o, false), Verdict::Refused(_)));
    }

    #[test]
    fn disabled_guard_refused_while_server_dropins_exist() {
        let d = tempfile::tempdir().unwrap();
        assert_eq!(disabled_guard_refusal(d.path(), false), Ok(None));
        let p = d
            .path()
            .join("usr/lib/systemd/system/smbd.service.d/10-omnifile-guard.conf");
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(&p, "[Unit]\n").unwrap();
        let e = disabled_guard_refusal(d.path(), false).unwrap_err();
        assert!(e.contains("refusing to start"), "{e}");
        assert!(matches!(
            disabled_guard_refusal(d.path(), true),
            Ok(Some(_))
        ));
        assert_eq!(installed_server_dropins(d.path()), vec![p]);
    }

    #[test]
    fn op_names() {
        assert_eq!(Op::parse("clean"), Some(Op::CleanFdstore));
        assert_eq!(Op::parse("bogus"), None);
        assert!(!Op::Stop.releases_group());
    }
}
