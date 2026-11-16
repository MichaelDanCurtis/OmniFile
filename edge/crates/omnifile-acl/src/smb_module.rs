//! Does smbd actually load `vfs_omnifile_prevver`? (`samba-vfs/`, decision 36)
//!
//! A share whose `vfs objects` names a module smbd can't load fails to
//! connect at all. smbd refuses a module built for another VFS interface
//! version (51 in Samba 4.23), and an interface bump is exactly what a Samba
//! upgrade can bring. [`prevver_installed`](crate::prevver_installed) only
//! sees that the file exists, so this module proves more, in two steps:
//!
//! 1. **Build info** ([`check_build`]): `build.sh` bakes the module's VFS
//!    interface version and Samba release into the `.so` as a plain string
//!    (`OMNIFILE-PREVVER-BUILD vfs_interface=51 samba=4.23.6`, from the
//!    macros the module was compiled with). It is compared with the running
//!    smbd's, without loading anything.
//! 2. **A real load** ([`check_load`]): a throwaway `smbd` on a loopback port,
//!    with a scratch share that lists the module, and an `smbclient` round
//!    trip to it. smbd loads a share's VFS modules when a client connects to
//!    it, so only this proves it. (`testparm` does not load them.)
//!
//! Every failure names the module and the Samba versions involved.

use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use crate::config::PREVVER_MODULE;

/// What `build.sh` bakes into the module, ahead of `vfs_interface=` and `samba=`.
pub const BUILD_INFO_MARKER: &str = "OMNIFILE-PREVVER-BUILD";

/// What the module says it was built against.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ModuleBuild {
    /// `SMB_VFS_INTERFACE_VERSION` of the tree it was compiled in.
    pub vfs_interface: Option<u32>,
    /// That tree's release, such as `4.23.6`.
    pub samba: Option<String>,
}

/// Reads the build info out of a module file's bytes. `None`: the module
/// predates the marker (or isn't ours).
pub fn parse_module_build(bytes: &[u8]) -> Option<ModuleBuild> {
    let marker = BUILD_INFO_MARKER.as_bytes();
    let at = bytes.windows(marker.len()).position(|w| w == marker)?;
    let rest = &bytes[at + marker.len()..];
    let end = rest.iter().position(|b| *b == 0).unwrap_or(rest.len());
    let text = String::from_utf8_lossy(&rest[..end.min(200)]);
    let mut b = ModuleBuild {
        vfs_interface: None,
        samba: None,
    };
    for w in text.split_whitespace() {
        if let Some(v) = w.strip_prefix("vfs_interface=") {
            b.vfs_interface = v.parse().ok();
        } else if let Some(v) = w.strip_prefix("samba=") {
            b.samba = Some(v.to_string());
        }
    }
    Some(b)
}

/// The installed smbd, as `smbd --version` and `smbd -b` describe it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SambaHost {
    /// Full text of `smbd --version`'s version, such as `4.23.6-Ubuntu`.
    pub version_full: String,
    /// `X.Y.Z` of it.
    pub version: String,
    /// `MODULESDIR`: VFS modules are in its `vfs/`.
    pub modules_dir: PathBuf,
    /// smbd's `SMB_VFS_INTERFACE_VERSION`, when `smbd -b` prints it.
    pub vfs_interface: Option<u32>,
}

/// `Version 4.23.6-Ubuntu` (as `smbd --version` prints it) to `(4.23.6-Ubuntu, 4.23.6)`.
pub fn parse_smbd_version(text: &str) -> Option<(String, String)> {
    let full = text
        .lines()
        .find_map(|l| l.trim().strip_prefix("Version "))?
        .trim()
        .to_string();
    let num: String = full
        .chars()
        .take_while(|c| c.is_ascii_digit() || *c == '.')
        .collect();
    (num.split('.').count() >= 2 && !num.ends_with('.')).then_some((full, num))
}

/// `MODULESDIR` and the VFS interface version (when printed) from `smbd -b`.
pub fn parse_smbd_build(text: &str) -> (Option<PathBuf>, Option<u32>) {
    let mut dir = None;
    let mut vfs = None;
    for l in text.lines() {
        let l = l.trim();
        if let Some(d) = l.strip_prefix("MODULESDIR:") {
            dir = Some(PathBuf::from(d.trim()));
        } else if let Some((k, v)) = l.split_once(':')
            && k.trim()
                .to_ascii_uppercase()
                .contains("VFS_INTERFACE_VERSION")
        {
            vfs = v.trim().parse().ok();
        }
    }
    (dir, vfs)
}

impl SambaHost {
    pub fn probe() -> Result<SambaHost, PrevverError> {
        let run = |arg: &str| -> Result<String, PrevverError> {
            let o = Command::new("smbd")
                .arg(arg)
                .output()
                .map_err(|e| PrevverError::NoSmbd(format!("`smbd {arg}`: {e}")))?;
            Ok(String::from_utf8_lossy(&o.stdout).into_owned())
        };
        let ver = run("--version")?;
        let (version_full, version) = parse_smbd_version(&ver).ok_or_else(|| {
            PrevverError::NoSmbd(format!("cannot read smbd's version from {ver:?}"))
        })?;
        let (dir, vfs_interface) = parse_smbd_build(&run("-b")?);
        let modules_dir =
            dir.ok_or_else(|| PrevverError::NoSmbd("`smbd -b` prints no MODULESDIR".into()))?;
        Ok(SambaHost {
            version_full,
            version,
            modules_dir,
            vfs_interface,
        })
    }

    /// Where smbd looks for the module.
    pub fn module_path(&self) -> PathBuf {
        self.modules_dir
            .join("vfs")
            .join(format!("{PREVVER_MODULE}.so"))
    }
}

/// Why the module can't be relied on. Each message names the module and the
/// versions involved, and says what to do.
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum PrevverError {
    #[error("{0}")]
    NoSmbd(String),
    #[error("{} is not installed in smbd's modules directory: run samba-vfs/install.sh", .0.display())]
    NotInstalled(PathBuf),
    #[error("cannot read {}: {}", .path.display(), .why)]
    Unreadable { path: PathBuf, why: String },
    #[error("{0}")]
    Mismatch(String),
    #[error("{0}")]
    LoadFailed(String),
}

/// The fix, for every message.
const REBUILD: &str = "Rebuild it for this Samba (samba-vfs/build.sh) and reinstall it (samba-vfs/install.sh), \
     or leave it out (shares then use shadow_copy2 alone)";

fn show(v: Option<u32>) -> String {
    v.map_or_else(|| "unknown".into(), |n| n.to_string())
}

/// `4.23.6` to `(4, 23)`: Samba changes the VFS interface between minor
/// releases, not in stable patch releases.
fn major_minor(v: &str) -> Option<(u32, u32)> {
    let mut p = v.split('.');
    Some((p.next()?.parse().ok()?, p.next()?.parse().ok()?))
}

/// Compares a module's build with the running smbd. `Ok` carries notes worth
/// showing (a build with no marker, a different patch release); the error
/// is a mismatch, worded for an admin.
pub fn compare_build(
    module: &Path,
    built: Option<&ModuleBuild>,
    host: &SambaHost,
) -> Result<Vec<String>, PrevverError> {
    let m = module.display();
    let Some(b) = built else {
        return Ok(vec![format!(
            "{m} carries no build info (built before it was recorded): only a load test can \
             tell whether Samba {} accepts it",
            host.version_full
        )]);
    };
    let mismatch = |what: String| -> PrevverError {
        PrevverError::Mismatch(format!(
            "{PREVVER_MODULE} ({m}) was built for Samba {} (VFS interface {}), but this host \
             runs Samba {} (VFS interface {}): {what}. {REBUILD}",
            b.samba.as_deref().unwrap_or("unknown"),
            show(b.vfs_interface),
            host.version_full,
            show(host.vfs_interface),
        ))
    };
    if let (Some(a), Some(h)) = (b.vfs_interface, host.vfs_interface)
        && a != h
    {
        return Err(mismatch(
            "smbd refuses a module built for another VFS interface, and every share \
             that names it stops connecting"
                .into(),
        ));
    }
    let mut notes = Vec::new();
    if let Some(bs) = &b.samba {
        match (major_minor(bs), major_minor(&host.version)) {
            (Some(a), Some(h)) if a != h => {
                return Err(mismatch(format!(
                    "Samba {}.{} and {}.{} are different releases, which can differ in VFS \
                     interface",
                    a.0, a.1, h.0, h.1
                )));
            }
            _ if *bs != host.version => notes.push(format!(
                "{PREVVER_MODULE} was built for Samba {bs}, this host runs {}: the same \
                 release series, whose VFS interface does not change",
                host.version_full
            )),
            _ => {}
        }
    }
    Ok(notes)
}

/// What step 1 found.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BuildCheck {
    pub host: SambaHost,
    /// The module's own build info (`None`: it predates the marker).
    pub built: Option<ModuleBuild>,
    pub notes: Vec<String>,
}

/// Step 1: the module file in smbd's modules directory, against the running
/// smbd. Runs `smbd --version` and `smbd -b`, loads nothing.
pub fn check_build() -> Result<BuildCheck, PrevverError> {
    let host = SambaHost::probe()?;
    let path = host.module_path();
    let bytes = std::fs::read(&path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            PrevverError::NotInstalled(path.clone())
        } else {
            PrevverError::Unreadable {
                path: path.clone(),
                why: e.to_string(),
            }
        }
    })?;
    let built = parse_module_build(&bytes);
    let notes = compare_build(&path, built.as_ref(), &host)?;
    Ok(BuildCheck { host, built, notes })
}

/// A scratch smb.conf for [`check_load`]: one guest share, `vfs objects`
/// naming just the module, on a loopback port, with every state directory in
/// `dir`.
pub fn scratch_smb_conf(dir: &Path, port: u16) -> String {
    let d = dir.display();
    let mut s = String::new();
    let _ = writeln!(s, "[global]");
    let _ = writeln!(s, "    security = user");
    let _ = writeln!(s, "    server role = standalone server");
    let _ = writeln!(s, "    map to guest = Bad User");
    let _ = writeln!(s, "    smb ports = {port}");
    let _ = writeln!(s, "    disable netbios = yes");
    let _ = writeln!(s, "    interfaces = 127.0.0.1");
    let _ = writeln!(s, "    bind interfaces only = yes");
    let _ = writeln!(s, "    server min protocol = SMB2_10");
    let _ = writeln!(s, "    load printers = no");
    let _ = writeln!(s, "    printing = bsd");
    let _ = writeln!(s, "    printcap name = /dev/null");
    let _ = writeln!(s, "    disable spoolss = yes");
    let _ = writeln!(s, "    pid directory = {d}/run");
    let _ = writeln!(s, "    lock directory = {d}/lock");
    let _ = writeln!(s, "    state directory = {d}/state");
    let _ = writeln!(s, "    cache directory = {d}/cache");
    let _ = writeln!(s, "    private dir = {d}/private");
    let _ = writeln!(s, "    ncalrpc dir = {d}/run/ncalrpc");
    let _ = writeln!(s, "    passdb backend = tdbsam:{d}/private/passdb.tdb");
    let _ = writeln!(s, "    log file = {d}/smbd.log");
    let _ = writeln!(s, "    log level = 1 vfs:5");
    let _ = writeln!(s, "[scratch]");
    let _ = writeln!(s, "    path = {d}/share");
    let _ = writeln!(s, "    guest ok = yes");
    let _ = writeln!(s, "    read only = yes");
    let _ = writeln!(s, "    vfs objects = {PREVVER_MODULE}");
    s
}

/// What the load test did.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LoadOutcome {
    /// smbd served the scratch share with the module loaded.
    Loaded,
    /// It could not be tried, and why (no `smbclient`, or not root).
    Skipped(String),
}

/// `[2026/10/21 20:32:55.5,  0] source3/smbd/vfs.c:399(smbd_vfs_init)`: the line
/// smbd puts ahead of each message. The message is the line after it.
fn is_log_header(l: &str) -> bool {
    l.starts_with("[20") && l.ends_with(')')
}

/// The part of smbd's own log that explains a refusal.
pub fn module_log_lines(log: &str) -> String {
    // Backtrace frames (`#18 /usr/lib/...`) name modules but explain nothing.
    let lines: Vec<&str> = log
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#') && !is_log_header(l))
        .collect();
    let hits: Vec<&str> = lines
        .iter()
        .copied()
        .filter(|l| {
            let l = l.to_ascii_lowercase();
            (l.contains("vfs")
                || l.contains("module")
                || l.contains("omnifile")
                || l.contains("panic")
                || l.contains("signal"))
                && !l.contains("vfs_init_custom: loaded")
        })
        .collect();
    // Nothing about modules: smbd's last words, whatever they were.
    let pick = if hits.is_empty() { &lines } else { &hits };
    pick[pick.len().saturating_sub(4)..].join(" | ")
}

fn free_port() -> std::io::Result<u16> {
    Ok(std::net::TcpListener::bind("127.0.0.1:0")?
        .local_addr()?
        .port())
}

/// Step 2: a real load. Starts a throwaway `smbd --interactive` with
/// [`scratch_smb_conf`] and connects to the scratch share with `smbclient`
/// (`ls`). smbd loads the share's VFS modules at that connect, so it succeeds
/// only if the installed module loads. Runs as the current user; the guest
/// share needs root to switch to `nobody`.
pub fn check_load(
    host: &SambaHost,
    built: Option<&ModuleBuild>,
) -> Result<LoadOutcome, PrevverError> {
    let module = host.module_path();
    let fail = |detail: String| PrevverError::LoadFailed(detail);
    if Command::new("smbclient")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_err()
    {
        return Ok(LoadOutcome::Skipped(
            "smbclient is not installed, so smbd was not asked to load the module".into(),
        ));
    }
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        return Ok(LoadOutcome::Skipped(
            "not root, so a throwaway smbd could not serve a guest share".into(),
        ));
    }
    let dir = std::env::temp_dir().join(format!(
        "omnifile-prevver-check.{}.{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.subsec_nanos())
    ));
    let cleanup = Cleanup(dir.clone());
    let io = |e: std::io::Error| fail(format!("preparing the throwaway smbd: {e}"));
    for sub in ["run/ncalrpc", "lock", "state", "cache", "private", "share"] {
        std::fs::create_dir_all(dir.join(sub)).map_err(io)?;
    }
    {
        use std::os::unix::fs::PermissionsExt;
        // smbd insists on a private `private dir`; the guest (nobody) must
        // reach the share through the others.
        for (sub, mode) in [("", 0o755), ("share", 0o755), ("private", 0o700)] {
            std::fs::set_permissions(dir.join(sub), std::fs::Permissions::from_mode(mode))
                .map_err(io)?;
        }
    }
    let port = free_port().map_err(io)?;
    let conf = dir.join("smb.conf");
    std::fs::write(&conf, scratch_smb_conf(&dir, port)).map_err(io)?;
    let console = std::fs::File::create(dir.join("console.log")).map_err(io)?;
    use std::os::unix::process::CommandExt as _;
    // --foreground, not --interactive: an interactive smbd exits when its
    // first client disconnects, which is the readiness probe below.
    let mut smbd = Command::new("smbd")
        .process_group(0)
        .args(["--foreground", "--no-process-group", "-s"])
        .arg(&conf)
        .stdin(Stdio::null())
        .stdout(console.try_clone().map_err(io)?)
        .stderr(console)
        .spawn()
        .map_err(|e| PrevverError::NoSmbd(format!("starting a throwaway smbd: {e}")))?;
    // What smbd said: its log, and anything it printed before it had a log.
    let log_of = |dir: &Path| {
        let mut t = std::fs::read_to_string(dir.join("smbd.log")).unwrap_or_default();
        t.push('\n');
        t.push_str(&std::fs::read_to_string(dir.join("console.log")).unwrap_or_default());
        t
    };

    // Wait for it to listen, then connect.
    let t0 = Instant::now();
    let listening = loop {
        if std::net::TcpStream::connect_timeout(
            &([127, 0, 0, 1], port).into(),
            Duration::from_millis(200),
        )
        .is_ok()
        {
            break true;
        }
        if smbd.try_wait().ok().flatten().is_some() || t0.elapsed() > START_TIMEOUT {
            break false;
        }
        std::thread::sleep(Duration::from_millis(100));
    };
    let result = if !listening {
        Err(fail(format!(
            "a throwaway smbd (Samba {}) exited or did not listen within {}s: {}",
            host.version_full,
            START_TIMEOUT.as_secs(),
            module_log_lines(&log_of(&dir))
        )))
    } else {
        let out = Command::new("smbclient")
            .arg("//127.0.0.1/scratch")
            .args([
                "-p",
                &port.to_string(),
                "-N",
                "-U",
                "%",
                "-m",
                "SMB3",
                "-t",
                "20",
                "-c",
                "ls",
            ])
            .arg("-s")
            .arg(&conf)
            .output();
        match out {
            Ok(o) if o.status.success() => Ok(LoadOutcome::Loaded),
            Ok(o) => {
                let said = format!(
                    "{}{}",
                    String::from_utf8_lossy(&o.stdout),
                    String::from_utf8_lossy(&o.stderr)
                );
                Err(fail(format!(
                    "smbd (Samba {}) would not serve a share that uses {PREVVER_MODULE} ({}, \
                     {}): smbclient said {:?}; smbd's log: {}. {REBUILD}",
                    host.version_full,
                    module.display(),
                    built.map_or_else(
                        || "no build info".into(),
                        |b| format!(
                            "built for Samba {}, VFS interface {}",
                            b.samba.as_deref().unwrap_or("unknown"),
                            show(b.vfs_interface)
                        )
                    ),
                    said.trim(),
                    module_log_lines(&log_of(&dir)),
                )))
            }
            Err(e) => Err(fail(format!("running smbclient: {e}"))),
        }
    };
    stop_smbd(&mut smbd);
    drop(cleanup);
    result
}

/// How long a throwaway smbd gets to listen. A first start converts its
/// passdb and takes several seconds.
const START_TIMEOUT: Duration = Duration::from_secs(40);

/// Stops the throwaway smbd and its helpers: TERM to the group, a few
/// seconds' grace, then KILL.
fn stop_smbd(smbd: &mut std::process::Child) {
    // smbd runs in a process group of its own (see check_load), because it
    // signals its whole group when it shuts down: the caller's must not be in
    // it. Its helpers (`notifyd`, `cleanupd`, the listener) are in it too.
    let group = -(smbd.id() as i32);
    // SAFETY: kill(2) on the group of a child we spawned and have not reaped.
    unsafe { libc::kill(group, libc::SIGTERM) };
    let t0 = Instant::now();
    while smbd.try_wait().ok().flatten().is_none() {
        if t0.elapsed() > Duration::from_secs(5) {
            // SAFETY: as above.
            unsafe { libc::kill(group, libc::SIGKILL) };
            let _ = smbd.wait();
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    // Anything the parent left behind (ESRCH when there is nothing).
    // SAFETY: as above.
    unsafe { libc::kill(group, libc::SIGKILL) };
}

struct Cleanup(PathBuf);
impl Drop for Cleanup {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// What a passing check found.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PrevverOk {
    pub host: SambaHost,
    pub load: LoadOutcome,
    pub notes: Vec<String>,
}

impl std::fmt::Display for PrevverOk {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{PREVVER_MODULE} ({}) is built for Samba {}",
            self.host.module_path().display(),
            self.host.version_full
        )?;
        match &self.load {
            LoadOutcome::Loaded => write!(f, " and smbd loaded it")?,
            LoadOutcome::Skipped(why) => write!(f, "; not load-tested: {why}")?,
        }
        for n in &self.notes {
            write!(f, "\nnote: {n}")?;
        }
        Ok(())
    }
}

/// The whole check: build info, then a real load.
pub fn check_prevver() -> Result<PrevverOk, PrevverError> {
    let BuildCheck { host, built, notes } = check_build()?;
    let load = check_load(&host, built.as_ref())?;
    Ok(PrevverOk { host, load, notes })
}

/// Whether a stanza may name the module, without loading anything: it is
/// installed, and its build info is present, complete and agrees with smbd.
/// This is the automatic path (the edge daemon), which never runs the
/// throwaway-smbd load test, so a module that can't vouch for its own build
/// (made before the marker existed, or built without the Samba release) is
/// unusable here: after a Samba upgrade it could be refused by smbd. The
/// explicit checks (`omniacl check-prevver`, `omnifile-setup check-smb-module`)
/// may still load-test such a module. An error is logged by the caller, and
/// the module is left out so the share still connects.
pub fn prevver_usable() -> Result<bool, PrevverError> {
    usable_from(check_build())
}

fn usable_from(check: Result<BuildCheck, PrevverError>) -> Result<bool, PrevverError> {
    match check {
        Ok(b) => {
            let complete = b.built.as_ref().is_some_and(|m| {
                m.vfs_interface.is_some()
                    && m.samba.as_deref().is_some_and(|v| major_minor(v).is_some())
            });
            if complete {
                Ok(true)
            } else {
                Err(PrevverError::Mismatch(format!(
                    "{PREVVER_MODULE} ({}) carries no complete build info, so it can't be \
                     matched to Samba {} without loading it: {REBUILD}",
                    b.host.module_path().display(),
                    b.host.version_full
                )))
            }
        }
        Err(PrevverError::NotInstalled(_) | PrevverError::NoSmbd(_)) => Ok(false),
        Err(e) => Err(e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn host(version_full: &str, vfs: Option<u32>) -> SambaHost {
        let (version_full, version) =
            parse_smbd_version(&format!("Version {version_full}\n")).unwrap();
        SambaHost {
            version_full,
            version,
            modules_dir: "/usr/lib/aarch64-linux-gnu/samba".into(),
            vfs_interface: vfs,
        }
    }
    fn built(vfs: Option<u32>, samba: &str) -> ModuleBuild {
        ModuleBuild {
            vfs_interface: vfs,
            samba: Some(samba.into()),
        }
    }
    const PATH: &str = "/usr/lib/samba/vfs/omnifile_prevver.so";

    #[test]
    fn build_info_is_found_in_the_module_bytes() {
        let mut b = b"\x7fELF....junk\0".to_vec();
        b.extend_from_slice(b"OMNIFILE-PREVVER-BUILD vfs_interface=51 samba=4.23.6\0more\0");
        assert_eq!(
            parse_module_build(&b),
            Some(ModuleBuild {
                vfs_interface: Some(51),
                samba: Some("4.23.6".into())
            })
        );
        assert_eq!(parse_module_build(b"\x7fELF no marker"), None);
        // Cut off by the end of the file, or garbled: no panic, no guess.
        assert_eq!(
            parse_module_build(b"OMNIFILE-PREVVER-BUILD vfs_interface=x"),
            Some(ModuleBuild {
                vfs_interface: None,
                samba: None
            })
        );
    }

    #[test]
    fn smbd_output_is_parsed() {
        assert_eq!(
            parse_smbd_version("Version 4.23.6-Ubuntu\n"),
            Some(("4.23.6-Ubuntu".into(), "4.23.6".into()))
        );
        assert_eq!(parse_smbd_version("Version 4.19.5\n").unwrap().1, "4.19.5");
        assert_eq!(parse_smbd_version("smbd: command not found"), None);
        let b = "Build environment:\n   Built by:    root\nPaths:\n   MODULESDIR: /usr/lib/aarch64-linux-gnu/samba\n   LOCKDIR: /run/samba\n";
        assert_eq!(
            parse_smbd_build(b),
            (Some("/usr/lib/aarch64-linux-gnu/samba".into()), None)
        );
        assert_eq!(
            parse_smbd_build("   SMB_VFS_INTERFACE_VERSION: 51\n   MODULESDIR: /m\n"),
            (Some("/m".into()), Some(51))
        );
    }

    #[test]
    fn a_matching_build_passes() {
        let h = host("4.23.6-Ubuntu", Some(51));
        let r = compare_build(Path::new(PATH), Some(&built(Some(51), "4.23.6")), &h);
        assert_eq!(r, Ok(vec![]));
        // Interface unknown on the smbd side: the release series decides.
        let r = compare_build(
            Path::new(PATH),
            Some(&built(Some(51), "4.23.6")),
            &host("4.23.6", None),
        );
        assert_eq!(r, Ok(vec![]));
    }

    #[test]
    fn a_different_vfs_interface_is_refused_naming_both() {
        let h = host("4.24.0-Ubuntu", Some(52));
        let e = compare_build(Path::new(PATH), Some(&built(Some(51), "4.23.6")), &h).unwrap_err();
        let m = e.to_string();
        assert!(matches!(e, PrevverError::Mismatch(_)));
        for needle in [
            "omnifile_prevver",
            PATH,
            "Samba 4.23.6 (VFS interface 51)",
            "Samba 4.24.0-Ubuntu (VFS interface 52)",
            "samba-vfs/build.sh",
        ] {
            assert!(m.contains(needle), "{needle:?} missing from {m}");
        }
    }

    #[test]
    fn a_different_release_series_is_refused_even_if_smbd_hides_its_interface() {
        let h = host("4.24.1", None);
        let e = compare_build(Path::new(PATH), Some(&built(Some(51), "4.23.6")), &h).unwrap_err();
        let m = e.to_string();
        assert!(
            m.contains("Samba 4.23.6") && m.contains("Samba 4.24.1"),
            "{m}"
        );
        assert!(m.contains("VFS interface unknown"), "{m}");
        // The other way round, too.
        assert!(
            compare_build(
                Path::new(PATH),
                Some(&built(Some(51), "4.23.6")),
                &host("4.22.9", None)
            )
            .is_err()
        );
    }

    #[test]
    fn a_patch_release_difference_is_a_note_not_a_failure() {
        let h = host("4.23.7-Ubuntu", Some(51));
        let r = compare_build(Path::new(PATH), Some(&built(Some(51), "4.23.6")), &h).unwrap();
        assert_eq!(r.len(), 1);
        assert!(r[0].contains("4.23.6") && r[0].contains("4.23.7"), "{r:?}");
    }

    #[test]
    fn a_module_with_no_build_info_is_left_to_the_load_test() {
        let r = compare_build(Path::new(PATH), None, &host("4.23.6", Some(51))).unwrap();
        assert_eq!(r.len(), 1);
        assert!(r[0].contains("no build info"), "{r:?}");
    }

    fn check_of(built: Option<ModuleBuild>) -> Result<BuildCheck, PrevverError> {
        Ok(BuildCheck {
            host: host("4.24.0-Ubuntu", Some(52)),
            built,
            notes: vec![],
        })
    }

    #[test]
    fn the_automatic_check_refuses_a_module_without_build_info() {
        // Built before the marker existed: compare_build lets it through with a
        // note, but the daemon never load-tests, so it must not render it.
        let e = usable_from(check_of(None)).unwrap_err().to_string();
        assert!(
            e.contains("omnifile_prevver") && e.contains("4.24.0"),
            "{e}"
        );
        assert!(e.contains("Rebuild it"), "{e}");
        // Incomplete info is no better.
        for b in [
            ModuleBuild {
                vfs_interface: None,
                samba: Some("4.23.6".into()),
            },
            ModuleBuild {
                vfs_interface: Some(51),
                samba: None,
            },
            ModuleBuild {
                vfs_interface: Some(51),
                samba: Some("unknown".into()),
            },
        ] {
            assert!(usable_from(check_of(Some(b))).is_err());
        }
        assert_eq!(
            usable_from(check_of(Some(built(Some(51), "4.24.0")))),
            Ok(true)
        );
        assert_eq!(
            usable_from(Err(PrevverError::NotInstalled(PATH.into()))),
            Ok(false)
        );
    }

    #[test]
    fn the_scratch_config_loads_only_the_module_on_a_private_tree() {
        let c = scratch_smb_conf(Path::new("/tmp/x"), 4455);
        assert!(c.contains("vfs objects = omnifile_prevver\n"));
        assert!(c.contains("smb ports = 4455"));
        assert!(c.contains("private dir = /tmp/x/private"));
        assert!(c.contains("path = /tmp/x/share"));
        // Nothing of the host's own (smbd is given this file alone).
        assert!(!c.contains("include"));
    }

    #[test]
    fn backtrace_frames_are_dropped_and_a_log_with_no_module_lines_keeps_its_tail() {
        let log = "PANIC (pid 7): internal error\n  #18 /usr/lib/samba/libsamba-modules.so(+0x1)\n  #19 smb_load_module+0xd8\n";
        assert_eq!(module_log_lines(log), "PANIC (pid 7): internal error");
        assert_eq!(module_log_lines("a\nb\n\nc\nd\ne\n"), "b | c | d | e");
        assert_eq!(module_log_lines(""), "");
        let log = "[2026/10/21 20:32:55.5,  0] source3/smbd/vfs.c:399(smbd_vfs_init)\n  smbd_vfs_init: vfs_init_custom failed for omnifile_prevver\n";
        assert_eq!(
            module_log_lines(log),
            "smbd_vfs_init: vfs_init_custom failed for omnifile_prevver"
        );
    }

    #[test]
    fn smbds_log_lines_about_modules_are_kept() {
        let log = "[2026/10/21] smbd started\n\
                   [2026/10/21] vfs_init_custom: loaded something\n\
                   [2026/10/21] smb_register_vfs: version mismatch for module omnifile_prevver\n\
                   [2026/10/21] unrelated\n";
        let m = module_log_lines(log);
        assert!(
            m.contains("version mismatch") && !m.contains("unrelated"),
            "{m}"
        );
    }
}
