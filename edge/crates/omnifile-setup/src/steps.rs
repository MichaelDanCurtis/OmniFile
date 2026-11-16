//! The setup steps, in order. Each one is idempotent: rerunning the wizard
//! with the same answers changes nothing, and with new answers changes only
//! what they touch. A step that fails stops the run, because later steps
//! depend on it (the data volume's UUID, the domain join, enrolment).

use std::time::Duration;

use crate::CRED_RECOVERY_HASH;
use crate::answers::Answers;
use crate::aws;
use crate::host::{Host, Output};
use crate::render::{self, *};

/// The steps, in the order they run.
pub const STEPS: [&str; 12] = [
    "hostname", "network", "time", "data", "ad", "admins", "recovery", "edge", "enrol", "aws",
    "updates", "services",
];

pub const IDENTITY_JSON: &str = "/var/lib/omnifile/identity/identity.json";
/// systemd's host key for credentials. The daemon seals its keys with it
/// (and the TPM), but can't create it: /var/lib/systemd is read-only to it.
pub const CRED_HOST_KEY: &str = "/var/lib/systemd/credential.secret";

/// What a run works with.
pub struct Ctx {
    pub answers: Answers,
    /// The domain join password, asked on the console or read from the
    /// credential. Used only if the box isn't joined yet.
    pub join_password: Option<String>,
    /// The data volume's filesystem UUID, once the data step knows it.
    pub fs_uuid: Option<String>,
    /// How long to wait for winbind to see the domain.
    pub wait: Duration,
    /// A crypt(3) hash for the recovery account, from the credential
    /// `omnifile.recovery-hash` (unattended setup). Never a password.
    pub recovery_hash: Option<String>,
    /// Whether a new recovery password may be made and shown (someone is at
    /// the console to write it down).
    pub can_reveal: bool,
    /// A recovery password made by this run, for the caller to show once.
    pub revealed: Option<String>,
}

impl Ctx {
    pub fn new(answers: Answers) -> Ctx {
        Ctx {
            answers,
            join_password: None,
            fs_uuid: None,
            wait: Duration::from_secs(60),
            recovery_hash: None,
            can_reveal: false,
            revealed: None,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// Done now; what changed.
    Changed(String),
    /// Already as asked.
    Unchanged,
    /// Not done, and why (not an error).
    Skipped(String),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Failure {
    pub step: &'static str,
    pub message: String,
}

impl std::fmt::Display for Failure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.step, self.message)
    }
}

type Step = Result<Outcome, String>;

/// Runs the steps (all, or those in `only`), stopping at the first failure.
pub fn run(
    h: &mut dyn Host,
    c: &mut Ctx,
    only: Option<&[String]>,
    mut report: impl FnMut(&str, &Outcome),
) -> Result<Vec<(&'static str, Outcome)>, Failure> {
    let mut done = Vec::new();
    for step in STEPS {
        if let Some(o) = only
            && !o.iter().any(|s| s == step)
        {
            continue;
        }
        let r = match step {
            "hostname" => hostname(h, c),
            "network" => network(h, c),
            "time" => time(h, c),
            "data" => data(h, c),
            "ad" => ad(h, c),
            "admins" => admins(h, c),
            "recovery" => recovery(h, c),
            "edge" => edge(h, c),
            "enrol" => enrol(h, c),
            "aws" => aws(h, c),
            "updates" => updates(h, c),
            "services" => services(h, c),
            _ => unreachable!(),
        };
        match r {
            Ok(o) => {
                report(step, &o);
                done.push((step, o));
            }
            Err(message) => return Err(Failure { step, message }),
        }
    }
    Ok(done)
}

/// Runs a command that must succeed; its error carries the command's own words.
fn must(
    h: &mut dyn Host,
    argv: &[&str],
    env: &[(&str, &str)],
    stdin: Option<&str>,
) -> Result<Output, String> {
    let o = h
        .run(argv, env, stdin)
        .map_err(|e| format!("{}: {e}", argv[0]))?;
    if o.ok() {
        Ok(o)
    } else {
        let said = last_lines(&format!("{}{}", o.stderr, o.stdout), 4);
        Err(format!("`{}` failed ({}): {said}", argv.join(" "), o.code))
    }
}

fn last_lines(s: &str, n: usize) -> String {
    let lines: Vec<&str> = s.lines().filter(|l| !l.trim().is_empty()).collect();
    lines[lines.len().saturating_sub(n)..].join(" | ")
}

fn write(h: &mut dyn Host, path: &str, content: &str, mode: u32) -> Result<bool, String> {
    h.write(path, content, mode)
        .map_err(|e| format!("writing {path}: {e}"))
}

fn changed(what: Vec<&str>) -> Outcome {
    if what.is_empty() {
        Outcome::Unchanged
    } else {
        Outcome::Changed(what.join(", "))
    }
}

// ------------------------------------------------------------------ steps

fn hostname(h: &mut dyn Host, c: &mut Ctx) -> Step {
    let a = &c.answers;
    let mut what = Vec::new();
    if write(h, "/etc/hostname", &render::hostname(a), 0o644)? {
        what.push("/etc/hostname");
    }
    let hosts = render::hosts(&h.read("/etc/hosts").unwrap_or_default(), a);
    if write(h, "/etc/hosts", &hosts, 0o644)? {
        what.push("/etc/hosts");
    }
    if !what.is_empty() {
        // hostnamed may be absent this early; the kernel name is what counts.
        let r = h.run(&["hostnamectl", "set-hostname", &a.hostname], &[], None);
        if !r.map(|o| o.ok()).unwrap_or(false) {
            must(h, &["hostname", &a.hostname], &[], None)?;
        }
    }
    Ok(changed(what))
}

/// The wired interfaces of the box (physical: they have a device).
pub fn wired_interfaces(h: &dyn Host) -> Vec<String> {
    h.list("/sys/class/net")
        .into_iter()
        .filter(|i| i != "lo" && h.exists(&format!("/sys/class/net/{i}/device")))
        .collect()
}

fn network(h: &mut dyn Host, c: &mut Ctx) -> Step {
    let n = &c.answers.network;
    let iface = if !n.interface.is_empty() {
        if h.live() && !h.exists(&format!("/sys/class/net/{}", n.interface)) {
            return Err(format!(
                "there is no interface {}; this box has: {}",
                n.interface,
                wired_interfaces(h).join(", ")
            ));
        }
        Some(n.interface.clone())
    } else if n.mode == "static" {
        let ifs = wired_interfaces(h);
        match ifs.as_slice() {
            [one] => Some(one.clone()),
            _ => {
                return Err(format!(
                    "a fixed address needs the interface it goes on; this box has: {}. \
                     Set network.interface",
                    if ifs.is_empty() {
                        "none found".into()
                    } else {
                        ifs.join(", ")
                    }
                ));
            }
        }
    } else {
        None
    };
    let text = render::network(&c.answers, iface.as_deref());
    if !write(h, NETWORK_FILE, &text, 0o644)? {
        return Ok(Outcome::Unchanged);
    }
    must(h, &["networkctl", "reload"], &[], None)?;
    Ok(Outcome::Changed(format!(
        "{NETWORK_FILE} ({}{})",
        n.mode,
        iface.map(|i| format!(" on {i}")).unwrap_or_default()
    )))
}

fn time(h: &mut dyn Host, c: &mut Ctx) -> Step {
    if !write(h, TIMESYNCD_FILE, &render::timesyncd(&c.answers), 0o644)? {
        return Ok(Outcome::Unchanged);
    }
    must(
        h,
        &["systemctl", "restart", "systemd-timesyncd.service"],
        &[],
        None,
    )?;
    Ok(Outcome::Changed(TIMESYNCD_FILE.into()))
}

/// The UUID of the xfs mounted on the data root, if one is.
pub fn mounted_data_uuid(h: &mut dyn Host) -> Option<String> {
    let o = h
        .run(
            &["findmnt", "-n", "-o", "FSTYPE,UUID", DATA_ROOT],
            &[],
            None,
        )
        .ok()?;
    let mut w = o.stdout.split_whitespace();
    match (o.ok(), w.next(), w.next()) {
        (true, Some("xfs"), Some(u)) => Some(u.to_string()),
        _ => None,
    }
}

/// Where a device is mounted: the device and everything on it (its
/// partitions, and what is built on them), as lsblk lists them.
pub fn mounts_of(h: &mut dyn Host, path: &str) -> Vec<String> {
    let listed = h
        .run(&["lsblk", "-nro", "MOUNTPOINTS", path], &[], None)
        .map(|o| o.stdout)
        .unwrap_or_default();
    // One line per device; a volume mounted twice may come as one line with
    // its mount points joined by an escaped newline.
    listed
        .lines()
        .flat_map(|l| l.split("\\x0a"))
        .map(|m| m.trim().replace("\\x20", " "))
        .filter(|m| !m.is_empty())
        .collect()
}

/// The UUID of the data volume an earlier run made: the one setup's own mount
/// unit names.
pub fn made_data_uuid(h: &dyn Host) -> Option<String> {
    let unit = h.read(DATA_MOUNT_UNIT)?;
    if !unit.starts_with(OWNED) {
        return None;
    }
    unit.lines()
        .find_map(|l| l.trim().strip_prefix("What=/dev/disk/by-uuid/"))
        .map(str::to_string)
        .filter(|u| !u.is_empty())
}

/// Checks a device the files may be put on, for the questions and the data
/// step alike, and returns its real path (an alias such as
/// `/dev/disk/by-path/...` resolved). It must be:
/// - a whole disk, never a partition: a free partition of the system disk
///   (the inactive `/usr` of the A/B updates) is what the next update writes;
/// - unused: nothing of it, or of anything on it, mounted;
/// - once an earlier run made the data volume (its mount unit names the
///   UUID), that volume. Device names (`sdX`) can change between boots, and
///   the files are on that volume, so no other disk is formatted in its place.
pub fn data_device(h: &mut dyn Host, dev: &str) -> Result<String, String> {
    let o = h
        .run(&["realpath", "-e", "--", dev], &[], None)
        .map_err(|e| format!("realpath: {e}"))?;
    if !o.ok() {
        return Err(format!("there is no disk {dev}"));
    }
    let real = match o.stdout.trim() {
        "" => dev.to_string(),
        p => p.to_string(),
    };
    let shown = if real == dev {
        dev.to_string()
    } else {
        format!("{dev} ({real})")
    };
    if h.live() {
        let kind = h
            .run(&["lsblk", "-dno", "TYPE", &real], &[], None)
            .map(|o| o.stdout.trim().to_string())
            .unwrap_or_default();
        if kind != "disk" {
            return Err(match kind.as_str() {
                "part" => format!(
                    "{shown} is a partition, not a whole disk: the files need a disk of their \
                     own (a free partition of the system disk is overwritten by updates)"
                ),
                "" => format!("{shown} is not a disk lsblk knows"),
                k => format!("{shown} is a {k} device, not a whole disk"),
            });
        }
    }
    let used = mounts_of(h, &real);
    if !used.is_empty() {
        return Err(format!(
            "{shown} is in use (mounted at {}): choose an empty disk",
            used.join(", ")
        ));
    }
    if h.live()
        && let Some(made) = made_data_uuid(h)
    {
        let uuid = h
            .run(
                &["blkid", "-p", "-o", "value", "-s", "UUID", &real],
                &[],
                None,
            )
            .ok()
            .filter(|o| o.ok())
            .map(|o| o.stdout.trim().to_string())
            .unwrap_or_default();
        if uuid != made {
            return Err(format!(
                "{shown} is not the data volume an earlier run made (xfs {made}, which \
                 {DATA_MOUNT_UNIT} mounts){}, so it is never formatted. That volume holds the \
                 files: attach its disk to this filer again and run setup again. To start over \
                 on another disk, deploy the appliance again",
                if uuid.is_empty() {
                    String::new()
                } else {
                    format!("; it holds {uuid}")
                }
            ));
        }
    }
    Ok(real)
}

fn data(h: &mut dyn Host, c: &mut Ctx) -> Step {
    let dev = c.answers.data.device.clone();
    let mut what = Vec::new();
    let uuid = if let Some(u) = mounted_data_uuid(h) {
        u
    } else {
        if h.live() && !h.exists(&dev) {
            let disks = h
                .run(&["lsblk", "-dnpo", "NAME,SIZE,TYPE"], &[], None)
                .map(|o| o.stdout)
                .unwrap_or_default();
            return Err(format!(
                "there is no disk {dev}. The disks are:\n{}",
                disks.trim_end()
            ));
        }
        // Everything below acts on the real device, never on an alias.
        let dev = data_device(h, &dev)?;
        let label = h
            .run(
                &["blkid", "-p", "-o", "value", "-s", "LABEL", &dev],
                &[],
                None,
            )
            .map(|o| o.stdout.trim().to_string())
            .unwrap_or_default();
        let types = h
            .run(
                &[
                    "wipefs",
                    "--no-act",
                    "--noheadings",
                    "--output",
                    "TYPE",
                    &dev,
                ],
                &[],
                None,
            )
            .map(|o| {
                o.stdout
                    .split_whitespace()
                    .map(str::to_string)
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let ours = label == DATA_LABEL && types == ["xfs"];
        if !ours {
            if !types.is_empty() && !c.answers.data.erase {
                return Err(format!(
                    "{dev} is not empty: it holds {}. To format it, and lose everything on it, \
                     answer ERASE on the console or set data.erase = true",
                    types.join(", ")
                ));
            }
            if !types.is_empty() {
                must(h, &["wipefs", "--all", &dev], &[], None)?;
            }
            must(h, &["mkfs.xfs", "-f", "-L", DATA_LABEL, &dev], &[], None)?;
            what.push("formatted");
        }
        let o = must(
            h,
            &["blkid", "-p", "-o", "value", "-s", "UUID", &dev],
            &[],
            None,
        )?;
        match o.stdout.trim() {
            "" if h.live() => return Err(format!("{dev} has no filesystem UUID after formatting")),
            "" => "STAGED".to_string(),
            u => u.to_string(),
        }
    };
    // Never erase again: the volume now holds the shares. This holds for a
    // volume found mounted too, so an erase asked for the first boot (an
    // unattended credential is read again at every boot) never reaches the
    // saved answers, where a later `apply` would act on it.
    c.answers.data.erase = false;
    let unit_changed = write(h, DATA_MOUNT_UNIT, &render::data_mount(&uuid), 0o644)?;
    let dropin_changed = write(h, EDGE_DATA_DROPIN, &render::edge_data_dropin(), 0o644)?;
    if unit_changed || dropin_changed {
        must(h, &["systemctl", "daemon-reload"], &[], None)?;
        what.push("mount unit");
    }
    must(
        h,
        &["systemctl", "enable", "--now", "srv-omnifile-data.mount"],
        &[],
        None,
    )?;
    if h.live() && mounted_data_uuid(h).as_deref() != Some(uuid.as_str()) {
        return Err(format!("{DATA_ROOT} did not mount the xfs {uuid}"));
    }
    c.fs_uuid = Some(uuid.clone());
    Ok(if what.is_empty() {
        Outcome::Unchanged
    } else {
        Outcome::Changed(format!(
            "{} ({dev}, xfs {uuid}, at {DATA_ROOT})",
            what.join(", ")
        ))
    })
}

/// `vfs_omnifile_prevver` (`samba-vfs/`) against the smbd on this machine:
/// built for its VFS interface, and loaded by a throwaway smbd. `Ok(None)`:
/// there is no smbd or no module to check. The error names the module and both
/// Samba versions. (`omnifile-setup check-smb-module`, and the `ad` step.)
pub fn check_smb_module() -> Result<Option<String>, String> {
    use omnifile_acl::smb_module::{PrevverError, check_prevver};
    match check_prevver() {
        Ok(ok) => Ok(Some(ok.to_string())),
        Err(PrevverError::NoSmbd(_) | PrevverError::NotInstalled(_)) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

fn ad(h: &mut dyn Host, c: &mut Ctx) -> Step {
    let a = c.answers.clone();
    let mut what = Vec::new();
    for (path, text) in [
        (KRB5_FILE, render::krb5(&a)),
        (SMB_FILE, render::smb_conf(&a)?),
        (IDMAPD_FILE, render::idmapd(&a)),
        (
            "/etc/nsswitch.conf",
            render::nsswitch(&h.read("/etc/nsswitch.conf").unwrap_or_default()),
        ),
    ] {
        if write(h, path, &text, 0o644)? {
            what.push(path);
        }
    }
    if write(h, SMB_SHARES_FILE, &render::smb_shares_include(), 0o644)? {
        what.push(SMB_SHARES_FILE);
    }
    rendered_files(h)?;
    must(
        h,
        &["testparm", "-s", "--suppress-prompt", SMB_FILE],
        &[],
        None,
    )?;
    // testparm doesn't load VFS modules. If the image carries
    // vfs_omnifile_prevver, smbd must really be able to load it, or every
    // rewind share stops connecting.
    if h.live() {
        check_smb_module()?;
    }

    let joined = h
        .run(&["net", "ads", "testjoin"], &[], None)
        .map(|o| o.ok())
        .unwrap_or(false);
    if !joined {
        let Some(pw) = c.join_password.clone() else {
            return Err(format!(
                "joining {} needs the password of {}: answer it on the console, or give \
                 the systemd credential omnifile.join-password",
                a.realm(),
                a.ad.join_user
            ));
        };
        let user = a.ad.join_user.clone();
        let ou = format!("createcomputer={}", a.ad.join_ou);
        let mut argv = vec!["net", "ads", "join", "-U", user.as_str()];
        if !a.ad.join_ou.is_empty() {
            argv.push(ou.as_str());
        }
        // PASSWD: Samba reads the password from the environment, so it is
        // never on a command line.
        must(h, &argv, &[("PASSWD", pw.as_str())], None)
            .map_err(|e| format!("the domain join failed: {e}"))?;
        what.push("joined the domain");
        // Clients find the filer by name; a DC that refuses dynamic DNS is
        // not fatal (the admin adds the record).
        let _ = h.run(&["net", "ads", "dns", "register", "-P"], &[], None);
    }
    must(
        h,
        &["pam-auth-update", "--enable", "winbind", "mkhomedir"],
        &[],
        None,
    )?;
    must(h, &["systemctl", "enable", "winbind.service"], &[], None)?;
    must(h, &["systemctl", "restart", "winbind.service"], &[], None)?;

    // winbind needs a moment to find a DC.
    let started = std::time::Instant::now();
    loop {
        if h.run(&["wbinfo", "-t"], &[], None)
            .map(|o| o.ok())
            .unwrap_or(false)
        {
            break;
        }
        if started.elapsed() >= c.wait {
            return Err(format!(
                "winbind can't reach {}: check DNS (it must find the domain controllers) and the time",
                a.realm()
            ));
        }
        std::thread::sleep(Duration::from_secs(2).min(c.wait));
    }
    let g = render::admin_group(&a);
    if !h
        .run(&["getent", "group", &g], &[], None)
        .map(|o| o.ok())
        .unwrap_or(false)
    {
        return Err(format!(
            "winbind does not show the group {g}: check its name (the pre-Windows 2000 name) \
             and that it has a gidNumber in AD, which idmap_ad needs"
        ));
    }
    Ok(changed(what))
}

fn admins(h: &mut dyn Host, c: &mut Ctx) -> Step {
    let a = &c.answers;
    let mut what = Vec::new();
    if write(h, SUDOERS_FILE, &render::sudoers(a), 0o440)? {
        // A broken sudoers file would break sudo for everyone: check it, and
        // remove it if it doesn't pass.
        if let Err(e) = must(h, &["visudo", "-c", "-q", "-f", SUDOERS_FILE], &[], None) {
            h.remove(SUDOERS_FILE).ok();
            return Err(e);
        }
        what.push(SUDOERS_FILE);
    }
    if write(h, SSHD_FILE, &render::sshd(a), 0o644)? {
        // sshd -t wants its privilege-separation directory, which a
        // socket-activated sshd (Ubuntu's default) creates only on demand.
        must(h, &["mkdir", "-p", "-m", "0755", "/run/sshd"], &[], None)?;
        if let Err(e) = must(h, &["sshd", "-t"], &[], None) {
            h.remove(SSHD_FILE).ok();
            return Err(e);
        }
        // Socket-activated, each connection starts a fresh sshd that reads
        // the new file; a running listener is reloaded.
        must(
            h,
            &["systemctl", "try-reload-or-restart", "ssh.service"],
            &[],
            None,
        )?;
        what.push(SSHD_FILE);
    }
    Ok(changed(what))
}

/// A recovery password: 30 characters from an alphabet without look-alikes,
/// in groups of five (150 bits).
pub fn new_recovery_password() -> String {
    const ALPHABET: &[u8; 32] = b"ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    let mut raw = [0u8; 30];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| std::io::Read::read_exact(&mut f, &mut raw))
        .expect("/dev/urandom");
    let chars: Vec<char> = raw
        .iter()
        .map(|b| ALPHABET[(*b % 32) as usize] as char)
        .collect();
    chars
        .chunks(5)
        .map(|c| c.iter().collect::<String>())
        .collect::<Vec<_>>()
        .join("-")
}

/// Whether the recovery account has a usable password (`passwd -S`: P).
fn recovery_sealed(h: &mut dyn Host) -> bool {
    h.run(&["passwd", "-S", RECOVERY_USER], &[], None)
        .map(|o| o.ok() && o.stdout.split_whitespace().nth(1) == Some("P"))
        .unwrap_or(false)
}

/// Seals the recovery account with a new password and returns it, to be
/// shown once. The password goes to chpasswd on stdin, never a command line,
/// and only its hash is kept (/etc/shadow).
pub fn seal_recovery(h: &mut dyn Host) -> Result<String, String> {
    let pw = new_recovery_password();
    must(
        h,
        &["chpasswd"],
        &[],
        Some(&format!("{RECOVERY_USER}:{pw}\n")),
    )?;
    h.remove(RECOVERY_USED)
        .map_err(|e| format!("{RECOVERY_USED}: {e}"))?;
    Ok(pw)
}

fn recovery(h: &mut dyn Host, c: &mut Ctx) -> Step {
    let mut what = Vec::new();
    if !h
        .run(&["getent", "passwd", RECOVERY_USER], &[], None)
        .map(|o| o.ok())
        .unwrap_or(false)
    {
        // The appliance image creates it (sysusers.d); elsewhere, here.
        must(
            h,
            &[
                "useradd",
                "--system",
                "--create-home",
                "--home-dir",
                "/var/lib/omnifile-recovery",
                "--shell",
                "/bin/bash",
                "--comment",
                "OmniFile recovery (break-glass)",
                RECOVERY_USER,
            ],
            &[],
            None,
        )?;
        must(h, &["passwd", "-l", RECOVERY_USER], &[], None)?;
        what.push("account created");
    }
    if write(h, RECOVERY_SUDOERS_FILE, &render::sudoers_recovery(), 0o440)? {
        if let Err(e) = must(
            h,
            &["visudo", "-c", "-q", "-f", RECOVERY_SUDOERS_FILE],
            &[],
            None,
        ) {
            h.remove(RECOVERY_SUDOERS_FILE).ok();
            return Err(e);
        }
        what.push(RECOVERY_SUDOERS_FILE);
    }
    // The appliance ships the profile in /usr; a writable /usr gets it here.
    if !h.exists(PAM_PROFILE_FILE) {
        write(h, PAM_PROFILE_FILE, render::pam_profile(), 0o644)?;
        what.push(PAM_PROFILE_FILE);
    }
    must(
        h,
        &["pam-auth-update", "--enable", "omnifile-recovery"],
        &[],
        None,
    )?;
    must(
        h,
        &["mkdir", "-p", "-m", "0700", "/var/lib/omnifile/recovery"],
        &[],
        None,
    )?;
    if recovery_sealed(h) {
        return Ok(changed(what));
    }
    if let Some(hash) = c.recovery_hash.clone() {
        // chpasswd -e: the line holds the hash, on stdin. One crypt(3) hash
        // only: a second line would set another account's password (root's).
        if !is_crypt_hash(&hash) {
            return Err(format!(
                "{CRED_RECOVERY_HASH} is not one crypt(3) hash (e.g. from `mkpasswd -m yescrypt`): \
                 {RECOVERY_USER} stays locked"
            ));
        }
        must(
            h,
            &["chpasswd", "-e"],
            &[],
            Some(&format!("{RECOVERY_USER}:{hash}\n")),
        )?;
        h.remove(RECOVERY_USED)
            .map_err(|e| format!("{RECOVERY_USED}: {e}"))?;
        what.push("sealed with the given hash");
    } else if c.can_reveal {
        c.revealed = Some(seal_recovery(h)?);
        what.push("sealed with a new password (shown below)");
    } else {
        return Ok(Outcome::Skipped(format!(
            "{RECOVERY_USER} stays locked: seal it on the console with `omnifile-setup recovery seal`"
        )));
    }
    Ok(changed(what))
}

/// One crypt(3) hash: `$id$...`, with only the characters hashes use (no
/// `:`, whitespace or newline, which `chpasswd` would read as another field
/// or another line).
pub fn is_crypt_hash(h: &str) -> bool {
    h.len() <= 512
        && ["$y$", "$gy$", "$7$", "$6$", "$5$", "$2b$", "$2y$"]
            .iter()
            .any(|p| h.starts_with(p))
        && h.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"./$=,".contains(&b))
}

fn edge(h: &mut dyn Host, c: &mut Ctx) -> Step {
    let uuid = match c.fs_uuid.clone().or_else(|| mounted_data_uuid(h)) {
        Some(u) => u,
        None if !h.live() => "STAGED".into(),
        None => return Err("the data volume isn't mounted: run the data step first".into()),
    };
    let mut what = Vec::new();
    if write(h, EDGE_FILE, &render::edge_toml(&c.answers, &uuid), 0o644)? {
        what.push(EDGE_FILE);
    }
    if write(h, GANESHA_BASE_FILE, &render::ganesha_base(), 0o644)? {
        what.push(GANESHA_BASE_FILE);
    }
    if h.live() && !h.exists(CRED_HOST_KEY) {
        must(h, &["systemd-creds", "setup"], &[], None)?;
        what.push("the host key for sealed credentials");
    }
    for (path, text) in [
        (GANESHA_FILE, render::ganesha_main()),
        (GANESHA_EXPORTS_FILE, render::ganesha_exports_include()),
    ] {
        if write(h, path, &text, 0o644)? {
            what.push(path);
        }
    }
    rendered_files(h)?;
    Ok(changed(what))
}

/// The files the daemon renders shares into, made empty until it does: the
/// servers' includes need them to exist. Never overwritten.
fn rendered_files(h: &mut dyn Host) -> Result<(), String> {
    for path in [RENDERED_SMB, RENDERED_GANESHA] {
        if !h.exists(path) {
            write(h, path, &render::rendered_empty(), 0o644)?;
        }
    }
    Ok(())
}

pub fn enrolled(h: &dyn Host) -> bool {
    h.exists(IDENTITY_JSON)
}

fn enrol(h: &mut dyn Host, c: &mut Ctx) -> Step {
    if enrolled(h) {
        // A code pasted again is spent already: drop it.
        c.answers.service.bundle.clear();
        return Ok(Outcome::Unchanged);
    }
    let bundle = c.answers.service.bundle.trim().to_string();
    if bundle.is_empty() {
        return Ok(Outcome::Skipped(
            "no enrolment code: get one in the portal (Edges, Add edge), then run \
             `omnifile-setup enrol`"
                .into(),
        ));
    }
    let o = must(h, &["omnifile-edge", "--config", EDGE_FILE, "enrol", "-"], &[], Some(&bundle))
        .map_err(|e| {
            format!("enrolment failed: {e}. A code works once and for 24 hours; get a new one in the portal if it was used or expired")
        })?;
    c.answers.service.bundle.clear();
    Ok(Outcome::Changed(last_lines(&o.stdout, 1)))
}

/// The subject this filer's Roles Anywhere request names: the edge, once it
/// is enrolled (as its mTLS certificate does, so the profile's certificate
/// conditions can name it: threat-model.md T4), else the host.
fn aws_subject(h: &dyn Host, a: &Answers) -> (String, Option<String>) {
    let id = h.read(IDENTITY_JSON).and_then(|j| {
        let i = j.find("\"edge_id\"")?;
        let rest = j[i + "\"edge_id\"".len()..]
            .trim_start()
            .strip_prefix(':')?;
        let n: String = rest
            .trim_start()
            .chars()
            .take_while(char::is_ascii_digit)
            .collect();
        (!n.is_empty()).then_some(n)
    });
    match id {
        Some(id) => (
            format!("omnifile-edge-{id}"),
            Some(format!("urn:omnifile:edge:{id}")),
        ),
        None => (format!("omnifile-edge-{}", a.hostname), None),
    }
}

/// Installs a certificate for the Roles Anywhere key, after checking it
/// against the key's public half. Returns what it installed.
pub fn install_aws_certificate(h: &mut dyn Host, path: &str) -> Result<String, String> {
    let Some(public) = h.read(aws::PUB_FILE) else {
        return Err(format!(
            "there is no Roles Anywhere key yet ({}): set [aws] and run the aws step first",
            aws::PUB_FILE
        ));
    };
    let text = h
        .read(path)
        .or_else(|| std::fs::read_to_string(path).ok())
        .ok_or_else(|| format!("can't read the certificate {path}"))?;
    let cert = aws::check_certificate(&text, &public, std::time::SystemTime::now())
        .map_err(|e| format!("the certificate {path} is refused: {e}"))?;
    write(h, aws::CERT_FILE, &cert.leaf_pem, 0o644)?;
    if cert.chain_pem.is_empty() {
        h.remove(aws::CHAIN_FILE)
            .map_err(|e| format!("{}: {e}", aws::CHAIN_FILE))?;
    } else {
        write(h, aws::CHAIN_FILE, &cert.chain_pem, 0o644)?;
    }
    Ok(format!(
        "the certificate for {} (until {})",
        cert.subject, cert.not_after
    ))
}

fn aws(h: &mut dyn Host, c: &mut Ctx) -> Step {
    let w = c.answers.aws.clone();
    if w.is_empty() {
        if h.exists(aws::ENV_FILE) {
            // Turned off: the helper stops (its condition fails); the key is kept.
            h.remove(aws::ENV_FILE)
                .map_err(|e| format!("removing {}: {e}", aws::ENV_FILE))?;
            must(h, &["systemctl", "stop", aws::HELPER_UNIT], &[], None)?;
            return Ok(Outcome::Changed(format!(
                "Roles Anywhere off: removed {} (the sealed key is kept)",
                aws::ENV_FILE
            )));
        }
        return Ok(Outcome::Skipped(
            "no Roles Anywhere: S3 reads need presigned URLs from the service until [aws] is set"
                .into(),
        ));
    }
    let mut what = Vec::new();
    if !h.exists(aws::KEY_CRED) {
        let (cn, uri) = aws_subject(h, &c.answers);
        let key = aws::new_key(&cn, uri.as_deref())?;
        // The key goes to systemd-creds on stdin, and only sealed to disk.
        must(h, &aws::seal_argv(), &[], Some(key.key_pem.as_str()))
            .map_err(|e| format!("sealing the Roles Anywhere key: {e}"))?;
        drop(key.key_pem);
        if h.live() && !h.exists(aws::KEY_CRED) {
            return Err(format!("systemd-creds wrote no {}", aws::KEY_CRED));
        }
        must(h, &["chmod", "0600", aws::KEY_CRED], &[], None)?;
        write(h, aws::PUB_FILE, &key.public_pem, 0o644)?;
        write(h, aws::CSR_FILE, &key.csr_pem, 0o644)?;
        what.push(format!(
            "a new key ({cn}), sealed to the TPM, and its certificate request {}",
            aws::CSR_FILE
        ));
    } else if !h.exists(aws::PUB_FILE) {
        return Err(format!(
            "{} is there but {} is not: remove the key to make a new one",
            aws::KEY_CRED,
            aws::PUB_FILE
        ));
    }
    // The other issuer's certificate goes, and the helper stops presenting it
    // until this issuer's is installed (review on #32): the edge CA's request,
    // certificate and state when switching to the client's CA, the client
    // CA's certificate when switching back.
    let stale: &[&str] = if w.client_ca() {
        &[
            aws::RA_CSR,
            aws::RA_CERT,
            aws::RA_CHAIN,
            aws::RA_STATE,
            aws::RA_RESTART,
        ]
    } else {
        &[aws::CERT_FILE, aws::CHAIN_FILE]
    };
    let stale: Vec<&str> = stale.iter().copied().filter(|p| h.exists(p)).collect();
    if !stale.is_empty() {
        for p in &stale {
            h.remove(p).map_err(|e| format!("removing {p}: {e}"))?;
        }
        must(h, &["systemctl", "stop", aws::HELPER_UNIT], &[], None)?;
        what.push(format!(
            "now {} issues the certificate: removed {}, and the helper is stopped until its \
             certificate is installed",
            if w.client_ca() {
                "your own CA"
            } else {
                "OmniFile's edge CA"
            },
            stale.join(", ")
        ));
    }
    if w.client_ca() {
        // The client's own CA: the daemon leaves the certificate alone.
        if !w.certificate.is_empty() {
            what.push(install_aws_certificate(h, &w.certificate)?);
            // Installed once; a renewal is installed the same way.
            c.answers.aws.certificate.clear();
        }
    } else {
        // Decision 50, the default: OmniFile's edge CA. The request goes where
        // the daemon reads it; the certificate comes from the service over
        // mTLS, now if the edge is enrolled, and the daemon renews it.
        let csr = h.read(aws::CSR_FILE).ok_or_else(|| {
            format!(
                "{} is missing: remove the key to make a new one",
                aws::CSR_FILE
            )
        })?;
        let new_request = write(h, aws::RA_CSR, &csr, 0o644)?;
        if new_request {
            what.push(format!(
                "the certificate request for OmniFile's edge CA, {}",
                aws::RA_CSR
            ));
        }
        if enrolled(h) && (new_request || !h.exists(aws::RA_CERT)) {
            let o = must(
                h,
                &[
                    "omnifile-edge",
                    "--config",
                    EDGE_FILE,
                    "roles-anywhere-cert",
                ],
                &[],
                None,
            )
            .map_err(|e| format!("the Roles Anywhere certificate from OmniFile's edge CA: {e}"))?;
            what.push(last_lines(&o.stdout, 1));
        }
    }
    let env = aws::env_file(&w, h.exists(aws::CHAIN_FILE));
    if write(h, aws::ENV_FILE, &env, 0o644)? {
        what.push(aws::ENV_FILE.to_string());
    }
    let has_cert = h.exists(if w.client_ca() {
        aws::CERT_FILE
    } else {
        aws::RA_CERT
    });
    if !what.is_empty() && has_cert {
        // Starts it once everything is there (its Condition*= checks).
        must(h, &["systemctl", "restart", aws::HELPER_UNIT], &[], None)?;
    }
    if !has_cert {
        let todo = if w.client_ca() {
            format!(
                "waiting for its certificate: have the trust anchor's CA sign {}, then run \
                 `omnifile-setup aws-cert FILE`",
                aws::CSR_FILE
            )
        } else {
            "waiting for its certificate from OmniFile's edge CA: the edge requests it once \
             it is enrolled (`omnifile-setup enrol`, then the daemon's first heartbeat)"
                .to_string()
        };
        return Ok(if what.is_empty() {
            Outcome::Skipped(todo)
        } else {
            Outcome::Changed(format!("{}; {todo}", what.join(", ")))
        });
    }
    Ok(if what.is_empty() {
        Outcome::Unchanged
    } else {
        Outcome::Changed(what.join(", "))
    })
}

fn updates(h: &mut dyn Host, c: &mut Ctx) -> Step {
    let u = c.answers.updates.clone();
    let mut what = Vec::new();
    if u.feed.is_empty() {
        for t in TRANSFERS {
            let p = render::transfer_dropin_path(t);
            if h.exists(&p) {
                h.remove(&p).map_err(|e| format!("removing {p}: {e}"))?;
                what.push("removed the update feed");
            }
        }
        if what.is_empty() {
            return Ok(Outcome::Skipped(
                "no update feed: this filer won't update until one is set".into(),
            ));
        }
    } else {
        must(
            h,
            &["systemd-analyze", "calendar", u.reboot_window.trim()],
            &[],
            None,
        )
        .map_err(|_| {
            format!(
                "the restart window {:?} isn't a time systemd understands (e.g. Sat *-*-* 02:00)",
                u.reboot_window
            )
        })?;
        for t in TRANSFERS {
            if write(
                h,
                &render::transfer_dropin_path(t),
                &render::transfer_source(&u.feed),
                0o644,
            )? {
                what.push("feed");
            }
        }
        if write(
            h,
            REBOOT_TIMER_DROPIN,
            &render::reboot_timer(&c.answers),
            0o644,
        )? {
            what.push("restart window");
        }
    }
    what.dedup();
    if !what.is_empty() {
        must(h, &["systemctl", "daemon-reload"], &[], None)?;
    }
    if !u.feed.is_empty() {
        must(
            h,
            &[
                "systemctl",
                "enable",
                "--now",
                "systemd-sysupdate.timer",
                "systemd-sysupdate-reboot.timer",
            ],
            &[],
            None,
        )?;
    }
    Ok(changed(what))
}

fn services(h: &mut dyn Host, _c: &mut Ctx) -> Step {
    if !enrolled(h) {
        return Ok(Outcome::Skipped(
            "the file servers start once the edge is enrolled".into(),
        ));
    }
    let mut what = Vec::new();
    // The annex supervisor too: on the appliance its conditions held it at boot, before
    // there was a config and an enrolment (omnifile-annex.service.d/05-appliance.conf).
    for unit in [
        "omnifile-edge.service",
        "omnifile-annex.service",
        "smbd.service",
        "nfs-ganesha-omni.service",
    ] {
        let on = h
            .run(&["systemctl", "is-enabled", "--quiet", unit], &[], None)
            .map(|o| o.ok())
            .unwrap_or(false);
        must(h, &["systemctl", "enable", "--now", unit], &[], None)?;
        if !on {
            what.push(unit);
        }
    }
    Ok(changed(what))
}
