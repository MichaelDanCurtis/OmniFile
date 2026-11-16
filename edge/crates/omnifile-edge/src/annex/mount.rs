//! The NAS mount (`annex.md` §5): read-only in Copy mode, always.
//!
//! - **SMB**: `mount -t cifs //host/share … -o ro,sec=krb5,cruid=0,serverino`.
//!   Kerberos with the edge's AD computer account: `cifs.upcall` gets root's
//!   ticket from the host keytab (`/etc/krb5.keytab`, from the setup
//!   wizard's AD join), so there is no password. `serverino` keeps the SMB
//!   file id as the inode, which tells a rename from a delete.
//! - **NFS**: `mount -t nfs host:/export … -o ro,sec=krb5` (rpc.gssd uses the
//!   same keytab), or `sec=sys` when the filer's credential mode says so:
//!   the NAS then trusts the edge's address, and the portal labels the
//!   annex host-trusted.
//!
//! The mount is checked before anything is read: it must be read-only and
//! readable. The result is the `mount` event, the filer's health.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use omnifile_control::annex::AnnexFiler;
use serde_json::{Value, json};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MountPlan {
    pub fstype: &'static str,
    pub source: String,
    pub target: PathBuf,
    pub options: String,
    /// `krb5` or `sys`.
    pub sec: &'static str,
}

/// The SMB mount's fixed options (`port=` and the like are added after).
pub const SMB_OPTIONS: &str =
    "ro,nosuid,nodev,noexec,sec=krb5,cruid=0,serverino,nobrl,actimeo=1,nolease,nohandlecache";

/// The NFS mount's, before `vers=` and `sec=`.
pub const NFS_OPTIONS: &str = "ro,nosuid,nodev,noexec";

/// The flags every annex mount has, set again by a bind remount after the
/// mount (`nosymfollow` is a VFS flag the cifs and nfs helpers don't all
/// parse): nothing on the NAS is executed, a device or set-id file means
/// nothing, and no symlink is followed, whatever reads it (`nasfs` doesn't
/// follow them either).
pub const VFS_FLAGS: &str = "ro,nosuid,nodev,noexec,nosymfollow";

/// `ST_NOSYMFOLLOW` (Linux 5.10; not in the libc crate).
pub const ST_NOSYMFOLLOW: u64 = 0x2000;
// statvfs's f_flag bits as u64 (c_ulong is u64 on the 64-bit targets we build).
#[allow(clippy::unnecessary_cast)]
const RDONLY: u64 = libc::ST_RDONLY as u64;
#[cfg(target_os = "linux")]
#[allow(clippy::unnecessary_cast)]
const NOSUID: u64 = libc::ST_NOSUID as u64;
#[cfg(target_os = "linux")]
#[allow(clippy::unnecessary_cast)]
const NODEV: u64 = libc::ST_NODEV as u64;
#[cfg(target_os = "linux")]
#[allow(clippy::unnecessary_cast)]
const NOEXEC: u64 = libc::ST_NOEXEC as u64;

/// What a mount's `statvfs` flags lack, if anything: read-only (when
/// required), `nosuid`, `nodev`, `noexec` and `nosymfollow`.
pub fn flags_problem(f_flag: u64, require_ro: bool) -> Option<String> {
    let mut missing = Vec::new();
    if require_ro && f_flag & RDONLY == 0 {
        missing.push("ro");
    }
    #[cfg(target_os = "linux")]
    for (bit, name) in [
        (NOSUID, "nosuid"),
        (NODEV, "nodev"),
        (NOEXEC, "noexec"),
        (ST_NOSYMFOLLOW, "nosymfollow"),
    ] {
        if f_flag & bit == 0 {
            missing.push(name);
        }
    }
    (!missing.is_empty()).then(|| format!("the mount lacks {}", missing.join(",")))
}

/// The mount for a filer; `extra` adds options (lab ports, an address).
pub fn plan(filer: &AnnexFiler, target: &Path, extra: &str) -> Result<MountPlan, String> {
    let host = &filer.host;
    if host.is_empty() || host.contains(['/', ' ', ',']) {
        return Err(format!("bad filer host {host:?}"));
    }
    let add = |base: String| {
        if extra.trim().is_empty() {
            base
        } else {
            format!("{base},{}", extra.trim())
        }
    };
    match filer.protocol.as_str() {
        "smb" => {
            if filer.credential_mode == "sec_sys" {
                return Err("sec=sys is for NFS; SMB uses Kerberos".into());
            }
            Ok(MountPlan {
                fstype: "cifs",
                source: format!("//{host}/{}", filer.export),
                target: target.to_owned(),
                // nolease and nohandlecache: no directory leases, so no
                // cached directory handles or their cached attributes. With
                // one, a folder's mtime can stay stale while its lease isn't
                // broken (the exit test caught a Samba server not breaking it
                // for a delete), and quick scans would miss the change.
                options: add(SMB_OPTIONS.into()),
                sec: "krb5",
            })
        }
        p @ ("nfs3" | "nfs4") => {
            let sec = if filer.credential_mode == "sec_sys" {
                "sys"
            } else {
                "krb5"
            };
            let vers = if p == "nfs3" { "3" } else { "4.2" };
            let base = format!("{NFS_OPTIONS},vers={vers},sec={sec},hard,actimeo=1");
            let options = if p == "nfs4" {
                // One edge's NFS options cover its NFSv3 filers too, and
                // mount.nfs refuses the mount protocol's options on NFSv4
                // ("Protocol not supported"), which has no such protocol.
                let kept: Vec<&str> = extra
                    .split(',')
                    .map(str::trim)
                    .filter(|o| !o.is_empty())
                    .filter(|o| !o.starts_with("mountport=") && !o.starts_with("mountproto="))
                    .collect();
                if kept.is_empty() {
                    base
                } else {
                    format!("{base},{}", kept.join(","))
                }
            } else {
                add(base)
            };
            Ok(MountPlan {
                fstype: "nfs",
                source: format!("{host}:{}", filer.export),
                target: target.to_owned(),
                options,
                sec,
            })
        }
        other => Err(format!("unknown protocol {other:?}")),
    }
}

/// The VFS flags of the remover's read-write mount: writable, and nothing
/// else allowed (no exec, set-id, device or symlink).
pub const RW_VFS_FLAGS: &str = "rw,nosuid,nodev,noexec,nosymfollow";

/// The remover's read-write mount (phase A2.2): the same export, its own
/// superblock and connection (`nosharesock`, `nosharecache`), so the
/// read-only mount stays read-only whatever happens to this one; over SMB
/// with the remove-rights account's ticket (`cruid`).
pub fn rw_plan(
    filer: &AnnexFiler,
    target: &Path,
    extra: &str,
    remove_uid: u32,
) -> Result<MountPlan, String> {
    let mut p = plan(filer, target, extra)?;
    let opts = p
        .options
        .strip_prefix("ro,")
        .ok_or("the read-only plan doesn't start with ro")?;
    p.options = match p.fstype {
        "cifs" => {
            let o = format!("rw,{opts},nosharesock");
            if remove_uid == 0 {
                o
            } else {
                o.replace("cruid=0", &format!("cruid={remove_uid}"))
            }
        }
        _ => format!("rw,{opts},nosharecache"),
    };
    Ok(p)
}

/// Mount the remover's read-write mount, with its VFS flags.
pub fn mount_rw(p: &MountPlan) -> Result<(), String> {
    std::fs::create_dir_all(&p.target).map_err(|e| format!("{}: {e}", p.target.display()))?;
    if !is_mounted(&p.target) {
        run(Command::new("mount")
            .arg("-t")
            .arg(p.fstype)
            .arg(&p.source)
            .arg(&p.target)
            .arg("-o")
            .arg(&p.options))?;
    }
    run(Command::new("mount")
        .arg("-o")
        .arg(format!("remount,bind,{RW_VFS_FLAGS}"))
        .arg(&p.target))
}

/// Whether `target` is a mount point (in this mount namespace).
pub fn mounted(target: &Path) -> bool {
    is_mounted(target)
}

/// The device of what is mounted at `target` (the last mount there, as the kernel shows
/// it), as `st_dev` reads.
pub fn mount_dev(target: &Path) -> Option<u64> {
    let t = std::fs::canonicalize(target).ok()?;
    let info = std::fs::read_to_string("/proc/self/mountinfo").ok()?;
    dev_in(&info, &t)
}

fn dev_in(mountinfo: &str, target: &Path) -> Option<u64> {
    let line = mountinfo
        .lines()
        .rfind(|l| l.split(' ').nth(4).is_some_and(|m| Path::new(m) == target))?;
    let (major, minor) = line.split(' ').nth(2)?.split_once(':')?;
    let (major, minor) = (major.parse().ok()?, minor.parse().ok()?);
    Some(libc::makedev(major, minor) as u64)
}

/// The ticket cache a mount's `cruid` reads: `/tmp/krb5cc_<uid>`.
pub fn ccache_path(uid: u32) -> PathBuf {
    PathBuf::from(format!("/tmp/krb5cc_{uid}"))
}

/// A ticket for `k` into `uid`'s cache (the remove- or restore-rights
/// account's), owned by `uid`: `cifs.upcall` reads a `cruid` cache as that
/// uid, so a root-owned one would be refused and the mount would fail.
pub fn kinit_into(k: &Kerberos, uid: u32) -> Result<(), String> {
    if k.principal.is_empty() || k.principal == "$" {
        return Err("no Kerberos principal to mount with".into());
    }
    let path = ccache_path(uid);
    run(Command::new("kinit")
        .arg("-k")
        .arg("-t")
        .arg(&k.keytab)
        .arg("-c")
        .arg(format!("FILE:{}", path.display()))
        .arg(&k.principal))?;
    give_cache(&path, uid)
}

/// A cache's ticket is gone with its mount: the account's TGT doesn't outlive
/// the only use it has.
pub fn drop_ccache(uid: u32) {
    if uid != 0 {
        let _ = std::fs::remove_file(ccache_path(uid));
    }
}

/// Root's cache holds the read account's ticket, which its mount reconnects
/// with. Another account's ticket written there would break it, and turn the
/// read-only mount into that account's.
pub fn cache_problem(principal: &str, uid: u32, read: Option<&str>) -> Option<String> {
    (!principal.is_empty() && uid == 0 && Some(principal) != read).then(|| {
        format!(
            "{principal} needs a uid of its own for its ticket cache: \
             root's holds the read account's"
        )
    })
}

/// The account a placement mount uses: (keytab, principal, cache's uid).
///
/// - `restore_*`, by default none: then the read account itself, for a lab or
///   a machine account that holds the restore group too;
/// - `remove_*` if that's all there is, on an annex that removes. A Copy annex
///   never gets the remove-rights account (`annex.md` section 5), so it gets
///   no placement mount until it has a restore account.
pub fn placement_account(
    cfg: &super::AnnexConfig,
    filer: &AnnexFiler,
    mode: &str,
) -> Result<(std::path::PathBuf, String, u32), String> {
    let read = read_name(cfg, filer);
    let (k, p, u): (&std::path::PathBuf, &String, u32) = if !cfg.restore_principal.is_empty() {
        (&cfg.restore_keytab, &cfg.restore_principal, cfg.restore_uid)
    } else if !cfg.remove_principal.is_empty() {
        if mode == "copy" {
            return Err(
                "a Copy annex never places files with the remove-rights account: \
                        set restore_principal"
                    .into(),
            );
        }
        (&cfg.remove_keytab, &cfg.remove_principal, cfg.remove_uid)
    } else {
        return Ok((std::path::PathBuf::new(), String::new(), 0));
    };
    if let Some(why) = cache_problem(p, u, read.as_deref()) {
        return Err(why);
    }
    if !cfg.remove_principal.is_empty() && *p != cfg.remove_principal && u == cfg.remove_uid {
        return Err(format!(
            "{p} and {} need a ticket cache each: give them different uids",
            cfg.remove_principal
        ));
    }
    Ok((k.clone(), p.clone(), u))
}

/// Which cache goes with the placement mount: none if the remove-rights account
/// is doing the placing, whose mount may still need it to reconnect.
pub fn placement_cache_to_drop(cfg: &super::AnnexConfig) -> Option<u32> {
    if cfg.restore_principal.is_empty() {
        None
    } else {
        Some(cfg.restore_uid)
    }
}

/// The placement account's name, for looking up its SID.
pub fn placement_name(cfg: &super::AnnexConfig, filer: &AnnexFiler) -> Option<String> {
    if !cfg.restore_principal.is_empty() {
        Some(cfg.restore_principal.clone())
    } else if !cfg.remove_principal.is_empty() {
        Some(cfg.remove_principal.clone())
    } else {
        read_name(cfg, filer)
    }
}

/// The read account's name: the mount's own credential.
pub fn read_name(cfg: &super::AnnexConfig, filer: &AnnexFiler) -> Option<String> {
    match filer.credential_mode.as_str() {
        "sec_sys" => None,
        "service_keytab" => Some(cfg.service_principal.clone()).filter(|s| !s.is_empty()),
        _ => Some(if cfg.machine_principal.is_empty() {
            machine_principal()
        } else {
            cfg.machine_principal.clone()
        }),
    }
}

/// The cache handed to its uid (nothing to do for root's).
pub fn give_cache(path: &Path, uid: u32) -> Result<(), String> {
    if uid == 0 {
        return Ok(());
    }
    std::os::unix::fs::chown(path, Some(uid), None)
        .map_err(|e| format!("{}: chown to {uid}: {e}", path.display()))
}

/// How long a mount helper may take. `mount.nfs` gives up on its own over NFSv3
/// (two minutes). Over NFSv4 to a NAS that has gone quiet it blocks for good, and
/// the unit's loop with it, so nothing is reported or bounded (the exit test's stage
/// `hang` found this).
const RUN_BOUND: Duration = Duration::from_secs(180);

fn run(cmd: &mut Command) -> Result<(), String> {
    run_within(cmd, RUN_BOUND)
}

fn run_within(cmd: &mut Command, bound: Duration) -> Result<(), String> {
    use std::io::{Read, Seek};
    use std::os::unix::process::CommandExt;
    // Stderr goes to a file, not a pipe: a helper can leave something running that holds it
    // (`mount.nfs` starts `rpc.statd`), and reading a pipe to its end would then outlast
    // the bound, with nothing reported.
    let mut errors = stderr_file();
    let stderr = match errors.as_ref().map(std::fs::File::try_clone) {
        Some(Ok(f)) => Stdio::from(f),
        _ => Stdio::null(),
    };
    // Its own process group: `mount` waits on `mount.nfs`, and that is the one stuck.
    cmd.process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(stderr);
    let mut child = cmd.spawn().map_err(|e| format!("{cmd:?}: {e}"))?;
    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if started.elapsed() < bound => {
                std::thread::sleep(Duration::from_millis(50));
            }
            Ok(None) => {
                let group = child.id() as libc::pid_t;
                // SAFETY: a signal to the group made for this child.
                unsafe { libc::kill(-group, libc::SIGKILL) };
                // Its wait is killable; if it doesn't go, this still returns.
                for _ in 0..100 {
                    if matches!(child.try_wait(), Ok(Some(_))) {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
                return Err(format!(
                    "no answer in {} s: {:?}",
                    bound.as_secs(),
                    cmd.get_program()
                ));
            }
            Err(e) => return Err(format!("{cmd:?}: {e}")),
        }
    };
    if status.success() {
        return Ok(());
    }
    let mut said = String::new();
    if let Some(f) = errors.as_mut()
        && f.rewind().is_ok()
    {
        let _ = f.take(8192).read_to_string(&mut said);
    }
    Err(format!("{status}: {}", said.trim()))
}

/// An unnamed file for a helper's stderr. Without one, a failure just says less.
fn stderr_file() -> Option<std::fs::File> {
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let path = std::env::temp_dir().join(format!(".omnifile-helper-{}-{n}", std::process::id()));
    let f = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(&path)
        .ok()?;
    let _ = std::fs::remove_file(&path);
    Some(f)
}

/// Mount, then check. Mounted already (a restarted unit) is fine.
pub fn mount(p: &MountPlan) -> Result<(), String> {
    std::fs::create_dir_all(&p.target).map_err(|e| format!("{}: {e}", p.target.display()))?;
    if !is_mounted(&p.target) {
        run(Command::new("mount")
            .arg("-t")
            .arg(p.fstype)
            .arg(&p.source)
            .arg(&p.target)
            .arg("-o")
            .arg(&p.options))?;
    }
    // Every time (a restarted unit finds it mounted): the VFS flags.
    run(Command::new("mount")
        .arg("-o")
        .arg(format!("remount,bind,{VFS_FLAGS}"))
        .arg(&p.target))
}

/// Where the unit keeps its Kerberos ticket: root's default cache, which
/// `cifs.upcall` finds after it switches into the mounting process's mount
/// namespace (the unit's own `/tmp`, `PrivateTmp=yes`). `rpc.gssd` uses the
/// host keytab for root by itself.
pub const CCACHE: &str = "FILE:/tmp/krb5cc_0";

/// The Kerberos identity a mount uses (`annex.md` §5).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Kerberos {
    pub keytab: PathBuf,
    pub principal: String,
}

/// The identity for a filer's credential mode: the edge's AD computer
/// account (`HOST$` from the host keytab the AD join wrote), or a dedicated
/// service account's keytab. `None` for `sec_sys`.
pub fn kerberos(filer: &AnnexFiler, cfg: &super::AnnexConfig) -> Option<Kerberos> {
    match filer.credential_mode.as_str() {
        "sec_sys" => None,
        "service_keytab" => Some(Kerberos {
            keytab: cfg.service_keytab.clone(),
            principal: cfg.service_principal.clone(),
        }),
        _ => Some(Kerberos {
            keytab: cfg.machine_keytab.clone(),
            principal: if cfg.machine_principal.is_empty() {
                machine_principal()
            } else {
                cfg.machine_principal.clone()
            },
        }),
    }
}

/// `ACL1$` for host `acl1`: the computer account's name.
pub fn machine_principal() -> String {
    let host = std::fs::read_to_string("/proc/sys/kernel/hostname").unwrap_or_default();
    let short = host
        .trim()
        .split('.')
        .next()
        .unwrap_or("")
        .to_ascii_uppercase();
    format!("{short}$")
}

/// A ticket for `k` into [`CCACHE`] (no password: the keytab).
pub fn kinit(k: &Kerberos) -> Result<(), String> {
    if k.principal.is_empty() || k.principal == "$" {
        return Err("no Kerberos principal to mount with".into());
    }
    run(Command::new("kinit")
        .arg("-k")
        .arg("-t")
        .arg(&k.keytab)
        .arg("-c")
        .arg(CCACHE)
        .arg(&k.principal))
}

pub fn unmount(target: &Path) {
    let _ = run(Command::new("umount").arg("-l").arg(target));
}

fn is_mounted(target: &Path) -> bool {
    let Ok(t) = std::fs::canonicalize(target) else {
        return false;
    };
    std::fs::read_to_string("/proc/self/mountinfo")
        .map(|s| {
            s.lines()
                .any(|l| l.split(' ').nth(4).is_some_and(|m| Path::new(m) == t))
        })
        .unwrap_or(false)
}

/// The mount's `statvfs` flags.
#[cfg(unix)]
pub fn vfs_flags(path: &Path) -> std::io::Result<u64> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let c = CString::new(path.as_os_str().as_bytes())?;
    // SAFETY: a valid path and a zeroed statvfs to fill.
    let mut st: libc::statvfs = unsafe { std::mem::zeroed() };
    let r = unsafe { libc::statvfs(c.as_ptr(), &mut st) };
    if r != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(st.f_flag as u64)
}

/// Read-only? (statvfs `ST_RDONLY`.)
pub fn read_only(path: &Path) -> std::io::Result<bool> {
    vfs_flags(path).map(|f| f & RDONLY != 0)
}

/// The `mount` event's details: whether the mount is safe to read from.
/// `mountpoint` is the mount (or a directory given, when `mounted` is
/// false), `rel` the annex's path beneath it. `require_ro`: Copy mode
/// refuses a writable mount. A mount without its VFS flags is refused.
pub fn check(
    mountpoint: &Path,
    rel: &str,
    sec: &str,
    require_ro: bool,
    mounted: bool,
) -> (bool, Value) {
    let flags = vfs_flags(mountpoint);
    let ro = flags
        .as_ref()
        .map(|f| f & RDONLY != 0)
        .map_err(|e| e.to_string());
    let readable = super::nasfs::open_root(mountpoint, rel)
        .and_then(|r| r.read_dir("").map(|_| ()))
        .map_err(|e| e.to_string());
    let flag_err = match &flags {
        Ok(f) if mounted => flags_problem(*f, require_ro),
        // A directory given (not mounted by the unit): only read-only.
        Ok(f) => (require_ro && f & RDONLY == 0)
            .then(|| "the mount is writable: Copy mode refuses it".to_owned()),
        Err(e) => Some(format!("statvfs: {e}")),
    };
    let ok = readable.is_ok() && flag_err.is_none();
    let err = match (&ro, &readable) {
        (Ok(false), _) if require_ro => Some("the mount is writable: Copy mode refuses it".into()),
        _ if flag_err.is_some() => flag_err.clone(),
        (_, Err(e)) => Some(format!("read: {e}")),
        _ => None,
    };
    (
        ok,
        json!({
            "ok": ok,
            "read_only": ro.as_ref().ok().copied(),
            "readable": readable.is_ok(),
            "sec": sec,
            "error": err,
        }),
    )
}

#[cfg(test)]
mod tests {
    use super::{Command, Duration, run_within};

    #[test]
    fn a_helpers_failure_says_why_and_its_success_is_ok() {
        let long = Duration::from_secs(30);
        assert_eq!(run_within(Command::new("true").arg(""), long), Ok(()));
        let e = run_within(
            Command::new("sh").args(["-c", "echo nope >&2; exit 3"]),
            long,
        );
        assert!(e.unwrap_err().contains("nope"));
    }

    #[test]
    fn a_mounts_device_is_the_last_mount_there() {
        let info = "22 1 0:21 / /proc rw - proc proc rw\n\
                    30 22 8:1 / /mnt/x rw - xfs /dev/sda1 rw\n\
                    31 30 0:52 / /mnt/x rw - nfs nas:/e rw\n";
        let at = |p: &str| super::dev_in(info, std::path::Path::new(p));
        assert_eq!(at("/mnt/x"), Some(libc::makedev(0, 52) as u64));
        assert_eq!(at("/proc"), Some(libc::makedev(0, 21) as u64));
        assert_eq!(at("/mnt"), None);
    }

    #[test]
    fn what_a_helper_leaves_running_doesnt_hold_up_its_answer() {
        // It held stderr: 8 s with a bound of 1, and nothing reported meanwhile.
        let started = super::Instant::now();
        let bound = Duration::from_secs(1);
        let ok = run_within(Command::new("sh").args(["-c", "sleep 8 & exit 0"]), bound);
        assert_eq!(ok, Ok(()));
        let e = run_within(
            Command::new("sh").args(["-c", "sleep 8 & echo nope >&2; exit 3"]),
            bound,
        );
        assert!(e.unwrap_err().contains("nope"));
        assert!(started.elapsed() < Duration::from_secs(4));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn a_helper_that_never_answers_is_stopped_with_what_it_started() {
        // As `mount` waits on `mount.nfs`, in NFSv4's wait, for good.
        let d = tempfile::tempdir().unwrap();
        let pid_file = d.path().join("pid");
        let script = format!("sleep 300 & echo $! >{}; wait", pid_file.display());
        let started = super::Instant::now();
        let e = run_within(
            Command::new("sh").args(["-c", &script]),
            Duration::from_millis(500),
        );
        assert!(e.unwrap_err().contains("no answer"));
        assert!(started.elapsed() < Duration::from_secs(20));
        let pid = std::fs::read_to_string(&pid_file).unwrap();
        let stat = format!("/proc/{}/stat", pid.trim());
        let gone = || match std::fs::read_to_string(&stat) {
            Err(_) => true,
            Ok(s) => s.rsplit(')').next().unwrap().trim().starts_with('Z'),
        };
        for _ in 0..100 {
            if gone() {
                return;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        panic!("what it started is still running");
    }

    use super::*;

    #[test]
    fn a_placement_account_needs_its_own_cache_and_a_copy_annex_never_gets_remove() {
        // E14.
        let f = filer("smb", "service_keytab");
        let mut c = crate::annex::AnnexConfig {
            service_principal: "reader@OMNI.TEST".into(),
            ..Default::default()
        };
        // Nothing named: the read account itself, in root's cache.
        assert_eq!(placement_account(&c, &f, "copy").unwrap().2, 0);
        // The remove-rights account, never on a Copy annex.
        c.remove_principal = "remover@OMNI.TEST".into();
        c.remove_uid = 2001;
        assert!(placement_account(&c, &f, "copy").is_err());
        assert_eq!(
            placement_account(&c, &f, "archive_after").unwrap().1,
            "remover@OMNI.TEST"
        );
        // A restore account: its own uid, or it would overwrite root's cache,
        // and with it what the read-only mount reconnects as.
        c.restore_principal = "restorer@OMNI.TEST".into();
        assert!(placement_account(&c, &f, "copy").is_err());
        c.restore_uid = 2002;
        let (_, p, u) = placement_account(&c, &f, "copy").unwrap();
        assert_eq!((p.as_str(), u), ("restorer@OMNI.TEST", 2002));
        assert_eq!(
            placement_name(&c, &f).as_deref(),
            Some("restorer@OMNI.TEST")
        );
        assert_eq!(read_name(&c, &f).as_deref(), Some("reader@OMNI.TEST"));
        // The same account as the read one may share root's cache.
        assert_eq!(
            cache_problem("reader@OMNI.TEST", 0, Some("reader@OMNI.TEST")),
            None
        );
    }

    #[test]
    fn the_placement_mount_never_drops_a_cache_another_mount_needs() {
        // F8: with no restore account the remove account places, and dropping
        // its cache left the rw mount unable to re-authenticate.
        let f = filer("smb", "service_keytab");
        let mut c = crate::annex::AnnexConfig {
            service_principal: "reader@OMNI.TEST".into(),
            remove_principal: "remover@OMNI.TEST".into(),
            remove_uid: 2001,
            ..Default::default()
        };
        assert_eq!(placement_cache_to_drop(&c), None);
        c.restore_principal = "restorer@OMNI.TEST".into();
        c.restore_uid = 2002;
        assert_eq!(placement_cache_to_drop(&c), Some(2002));
        // Two principals in one cache would overwrite each other's ticket.
        c.restore_uid = 2001;
        let why = placement_account(&c, &f, "archive_after").unwrap_err();
        assert!(why.contains("different uids"), "{why}");
    }

    fn filer(protocol: &str, mode: &str) -> AnnexFiler {
        AnnexFiler {
            filer_id: 1,
            vendor: "ontap".into(),
            host: "ontap01".into(),
            protocol: protocol.into(),
            export: if protocol == "smb" {
                "geo".into()
            } else {
                "/export/geo".into()
            },
            event_source: "scan".into(),
            credential_mode: mode.into(),
        }
    }

    #[test]
    fn the_removers_mount_is_writable_on_its_own_superblock() {
        let t = Path::new("/run/omnifile/annex/1/rw");
        let s = rw_plan(&filer("smb", "machine_account"), t, "port=4450", 0).unwrap();
        assert!(s.options.starts_with("rw,") && !s.options.contains(",ro,"));
        assert!(s.options.contains("nosharesock") && s.options.contains("cruid=0"));
        let s = rw_plan(&filer("smb", "service_keytab"), t, "", 990).unwrap();
        assert!(s.options.contains("cruid=990") && !s.options.contains("cruid=0"));
        let n = rw_plan(&filer("nfs3", "sec_sys"), t, "", 0).unwrap();
        assert!(n.options.starts_with("rw,") && n.options.contains("nosharecache"));
        // The read-only plan is untouched.
        let r = plan(&filer("nfs3", "sec_sys"), t, "").unwrap();
        assert!(r.options.starts_with("ro,"));
    }

    #[test]
    fn every_mount_is_read_only_with_the_machine_account_by_default() {
        let t = Path::new("/run/omnifile/annex/1/mnt");
        let s = plan(&filer("smb", "machine_account"), t, "port=4450").unwrap();
        assert_eq!(s.source, "//ontap01/geo");
        assert!(
            s.options.starts_with("ro,nosuid,nodev,noexec,sec=krb5,"),
            "{}",
            s.options
        );
        assert!(s.options.ends_with(",port=4450"));
        // No directory leases: their cached attributes hide a folder's new
        // mtime from quick scans while a server doesn't break them.
        assert!(
            s.options.split(',').any(|o| o == "nolease"),
            "{}",
            s.options
        );
        assert!(
            s.options.split(',').any(|o| o == "nohandlecache"),
            "{}",
            s.options
        );
        for o in ["nosuid", "nodev", "noexec"] {
            assert!(s.options.split(',').any(|x| x == o), "{}", s.options);
        }
        let n = plan(&filer("nfs4", "machine_account"), t, "").unwrap();
        assert_eq!(n.source, "ontap01:/export/geo");
        assert!(
            n.options
                .starts_with("ro,nosuid,nodev,noexec,vers=4.2,sec=krb5")
        );
        let sys = plan(&filer("nfs3", "sec_sys"), t, "").unwrap();
        assert!(
            sys.options
                .starts_with("ro,nosuid,nodev,noexec,vers=3,sec=sys")
        );
        // Both get the VFS flags again by a bind remount, nosymfollow too.
        assert_eq!(VFS_FLAGS, "ro,nosuid,nodev,noexec,nosymfollow");
        assert_eq!(sys.sec, "sys");
        assert!(plan(&filer("smb", "sec_sys"), t, "").is_err());
        // The mount protocol's options are NFSv3's: mount.nfs refuses them on v4.
        let lab = "port=2049,mountport=20048,mountproto=tcp,nolock";
        let v4 = plan(&filer("nfs4", "sec_sys"), t, lab).unwrap();
        assert!(v4.options.contains("port=2049"), "{}", v4.options);
        assert!(v4.options.ends_with(",nolock"), "{}", v4.options);
        assert!(!v4.options.contains("mountp"), "{}", v4.options);
        let v3 = plan(&filer("nfs3", "sec_sys"), t, lab).unwrap();
        assert!(v3.options.contains("mountproto=tcp"), "{}", v3.options);
    }

    #[test]
    fn a_writable_mount_fails_the_check() {
        let d = tempfile::tempdir().unwrap();
        let (ok, v) = check(d.path(), "", "sys", true, false);
        assert!(!ok);
        assert_eq!(v["read_only"], false);
        let (ok, _) = check(d.path(), "", "sys", false, false);
        assert!(ok);
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn a_mount_without_its_vfs_flags_is_refused() {
        let all = RDONLY | NOSUID | NODEV | NOEXEC | ST_NOSYMFOLLOW;
        assert_eq!(flags_problem(all, true), None);
        let p = flags_problem(all & !ST_NOSYMFOLLOW, true).unwrap();
        assert!(p.contains("nosymfollow"), "{p}");
        let p = flags_problem(all & !(NOEXEC) & !(RDONLY), true).unwrap();
        assert!(p.contains("ro") && p.contains("noexec"), "{p}");
    }
}
