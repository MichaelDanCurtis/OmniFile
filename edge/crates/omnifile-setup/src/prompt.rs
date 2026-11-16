//! The console questions: plain language first, each answer checked as it is
//! given, the previous answers offered as defaults on a rerun.

use std::io::{BufRead, Write};

use crate::answers::{self, Ad, Answers, Data, Network, Service, Storage, Updates};
use crate::host::Host;
use crate::steps;

pub trait Console {
    /// One line, without its newline. `None`: input ended.
    fn line(&mut self, prompt: &str) -> Option<String>;
    /// A line that is not echoed (the join password).
    fn secret(&mut self, prompt: &str) -> Option<String>;
    fn say(&mut self, text: &str);
}

/// The terminal (stdin and stdout).
pub struct Terminal;

impl Console for Terminal {
    fn line(&mut self, prompt: &str) -> Option<String> {
        print!("{prompt}");
        std::io::stdout().flush().ok();
        let mut s = String::new();
        match std::io::stdin().lock().read_line(&mut s) {
            Ok(0) | Err(_) => None,
            Ok(_) => Some(s.trim_end_matches(['\n', '\r']).to_string()),
        }
    }

    fn secret(&mut self, prompt: &str) -> Option<String> {
        let _echo = EchoOff::new();
        let r = self.line(prompt);
        println!();
        r
    }

    fn say(&mut self, text: &str) {
        println!("{text}");
    }
}

/// Echo off on stdin while it lives (if stdin is a terminal).
struct EchoOff(Option<libc::termios>);

impl EchoOff {
    fn new() -> EchoOff {
        // SAFETY: termios on fd 0, restored on drop; a non-terminal is left alone.
        unsafe {
            let mut t: libc::termios = std::mem::zeroed();
            if libc::isatty(0) == 1 && libc::tcgetattr(0, &mut t) == 0 {
                let old = t;
                t.c_lflag &= !libc::ECHO;
                libc::tcsetattr(0, libc::TCSANOW, &t);
                return EchoOff(Some(old));
            }
        }
        EchoOff(None)
    }
}

impl Drop for EchoOff {
    fn drop(&mut self) {
        if let Some(t) = self.0 {
            // SAFETY: restoring the attributes read in new().
            unsafe {
                libc::tcsetattr(0, libc::TCSANOW, &t);
            }
        }
    }
}

/// Lines from a script, for tests. Everything said is kept in `said`.
pub struct Scripted {
    pub lines: std::collections::VecDeque<String>,
    pub said: String,
}

impl Scripted {
    pub fn new(lines: &[&str]) -> Scripted {
        Scripted {
            lines: lines.iter().map(|s| s.to_string()).collect(),
            said: String::new(),
        }
    }
}

impl Console for Scripted {
    fn line(&mut self, prompt: &str) -> Option<String> {
        self.said.push_str(prompt);
        let l = self.lines.pop_front();
        if let Some(l) = &l {
            self.said.push_str(l);
        }
        self.said.push('\n');
        l
    }

    fn secret(&mut self, prompt: &str) -> Option<String> {
        self.said.push_str(prompt);
        self.said.push_str("(hidden)\n");
        self.lines.pop_front()
    }

    fn say(&mut self, text: &str) {
        self.said.push_str(text);
        self.said.push('\n');
    }
}

/// What the questions produce.
pub struct Asked {
    pub answers: Answers,
    pub join_password: Option<String>,
}

const ENDED: &str = "input ended before setup was answered";

/// Asks until `check` accepts the answer. An empty answer takes `default`.
fn ask(
    c: &mut dyn Console,
    question: &str,
    default: &str,
    mut check: impl FnMut(&str) -> Result<(), String>,
) -> Result<String, String> {
    loop {
        let prompt = if default.is_empty() {
            format!("{question}\n> ")
        } else {
            format!("{question}\n[{default}] > ")
        };
        let got = c.line(&prompt).ok_or(ENDED)?;
        let v = if got.trim().is_empty() {
            default.to_string()
        } else {
            got.trim().to_string()
        };
        match check(&v) {
            Ok(()) => return Ok(v),
            Err(m) => c.say(&format!("  {m}")),
        }
    }
}

fn yes_no(c: &mut dyn Console, question: &str, default: bool) -> Result<bool, String> {
    let d = if default { "Y/n" } else { "y/N" };
    loop {
        let got = c.line(&format!("{question} [{d}] > ")).ok_or(ENDED)?;
        match got.trim().to_lowercase().as_str() {
            "" => return Ok(default),
            "y" | "yes" => return Ok(true),
            "n" | "no" => return Ok(false),
            _ => c.say("  answer y or n"),
        }
    }
}

fn any(_: &str) -> Result<(), String> {
    Ok(())
}

fn ips(v: &str) -> Result<(), String> {
    for w in v.split_whitespace() {
        if w.parse::<std::net::IpAddr>().is_err() {
            return Err(format!("{w} is not an IP address"));
        }
    }
    Ok(())
}

/// A disk the questions can offer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Disk {
    pub path: String,
    pub size: String,
    pub model: String,
    /// Where it (or a partition of it) is mounted.
    pub mounts: Vec<String>,
    /// Mounted (the system disk, or anything else), so not offered.
    pub in_use: bool,
    /// The data volume an earlier run of setup made on it, mounted at the
    /// data root (its filesystem UUID): offered to keep, never formatted.
    pub prepared: Option<String>,
}

/// The data volume an earlier run of setup made on `path`, mounted where
/// setup mounts it: its filesystem UUID. All of these must hold:
/// - nothing of the disk is mounted outside the data root (so not the system disk);
/// - the xfs mounted at the data root is this disk's (the same UUID);
/// - the disk's filesystem is xfs labelled `omni-data`, as setup formats it;
/// - setup's own mount unit names that UUID.
pub fn prepared_data_volume(h: &mut dyn Host, path: &str, mounts: &[String]) -> Option<String> {
    use crate::render::{DATA_LABEL, DATA_ROOT};
    let below = format!("{DATA_ROOT}/");
    if !mounts.iter().any(|m| m == DATA_ROOT)
        || mounts
            .iter()
            .any(|m| m != DATA_ROOT && !m.starts_with(&below))
    {
        return None;
    }
    let uuid = steps::mounted_data_uuid(h)?;
    let mut probe = |tag: &str| {
        h.run(&["blkid", "-p", "-o", "value", "-s", tag, path], &[], None)
            .ok()
            .filter(|o| o.ok())
            .map(|o| o.stdout.trim().to_string())
            .unwrap_or_default()
    };
    if probe("TYPE") != "xfs" || probe("LABEL") != DATA_LABEL || probe("UUID") != uuid {
        return None;
    }
    (steps::made_data_uuid(h).as_deref() == Some(uuid.as_str())).then_some(uuid)
}

pub fn disks(h: &mut dyn Host) -> Vec<Disk> {
    let o = h
        .run(&["lsblk", "-dnpro", "NAME,SIZE,TYPE,MODEL"], &[], None)
        .map(|o| o.stdout)
        .unwrap_or_default();
    let mut out = Vec::new();
    for l in o.lines() {
        let mut w = l.split_whitespace();
        let (Some(path), Some(size), Some(kind)) = (w.next(), w.next(), w.next()) else {
            continue;
        };
        if kind != "disk" {
            continue;
        }
        // lsblk -r escapes spaces in the model as \x20.
        let model = w.collect::<Vec<_>>().join(" ").replace("\\x20", " ");
        let mounts = steps::mounts_of(h, path);
        let prepared = prepared_data_volume(h, path, &mounts);
        out.push(Disk {
            path: path.to_string(),
            size: size.to_string(),
            model: model.trim().to_string(),
            in_use: !mounts.is_empty(),
            mounts,
            prepared,
        });
    }
    out
}

/// A rerun: the data disk an earlier run prepared is kept as it is.
fn keep_data_disk(c: &mut dyn Console, d: &Disk) -> Result<String, String> {
    use crate::render::{DATA_LABEL, DATA_ROOT};
    c.say(&format!(
        "The data disk is {} ({}{}). An earlier run of setup formatted it (xfs, label {DATA_LABEL}, \
         UUID {}) and mounted it at {DATA_ROOT}, so it holds this filer's files. It is kept as it \
         is: nothing on it is erased.",
        d.path,
        d.size,
        if d.model.is_empty() {
            String::new()
        } else {
            format!(" {}", d.model)
        },
        d.prepared.as_deref().unwrap_or_default(),
    ));
    if yes_no(c, "Keep it as the data disk and go on?", true)? {
        return Ok(d.path.clone());
    }
    Err(format!(
        "stopped: nothing was changed. Setup keeps the files on {}: it never moves them to \
         another disk or formats a disk that holds them",
        d.path
    ))
}

/// A first run: choose an empty disk; one that holds anything is formatted
/// only after ERASE is typed.
fn choose_data_disk(
    c: &mut dyn Console,
    h: &mut dyn Host,
    found: &[Disk],
    before: &Answers,
) -> Result<(String, bool), String> {
    let free: Vec<&Disk> = found.iter().filter(|d| !d.in_use).collect();
    let in_use = |v: &str| found.iter().find(|d| d.path == v && d.in_use);
    let mut menu =
        String::from("Which disk holds the files? It is formatted (xfs) the first time.");
    for (i, d) in free.iter().enumerate() {
        menu += &format!("\n  {}  {}  {}  {}", i + 1, d.path, d.size, d.model);
    }
    if free.is_empty() {
        menu += "\n  (no free disk found: add one to the VM, or type its device)";
    }
    let d_default = if !before.data.device.is_empty() && in_use(&before.data.device).is_none() {
        before.data.device.clone()
    } else if free.len() == 1 {
        "1".into()
    } else {
        String::new()
    };
    // The real device the answer names (an alias resolved), once it passes the
    // data step's own check: a whole disk, unused, and the volume an earlier
    // run made if it made one.
    let mut real = String::new();
    let pick = ask(c, &menu, &d_default, |v| {
        let dev = match v.parse::<usize>() {
            Ok(n) if n >= 1 && n <= free.len() => free[n - 1].path.clone(),
            Err(_) if v.starts_with("/dev/") => v.to_string(),
            _ => return Err("give the number of a disk above, or its /dev path".into()),
        };
        real = steps::data_device(h, &dev)?;
        Ok(())
    })?;
    let device = match pick.parse::<usize>() {
        Ok(n) => free[n - 1].path.clone(),
        Err(_) => pick,
    };
    let types = h
        .run(
            &[
                "wipefs",
                "--no-act",
                "--noheadings",
                "--output",
                "TYPE",
                &real,
            ],
            &[],
            None,
        )
        .map(|o| o.stdout.split_whitespace().collect::<Vec<_>>().join(", "))
        .unwrap_or_default();
    let label = h
        .run(
            &["blkid", "-p", "-o", "value", "-s", "LABEL", &real],
            &[],
            None,
        )
        .map(|o| o.stdout.trim().to_string())
        .unwrap_or_default();
    if !types.is_empty() && !(types == "xfs" && label == crate::render::DATA_LABEL) {
        let got = c
            .line(&format!(
                "{device} is not empty: it holds {types}. Formatting it destroys everything on it.\n\
                 Type ERASE to format it, or anything else to stop\n> "
            ))
            .ok_or(ENDED)?;
        if got.trim() != "ERASE" {
            return Err(format!("stopped: {device} was left as it is"));
        }
        return Ok((device, true));
    }
    Ok((device, false))
}

/// The whole conversation. `before`: the previous answers (a rerun).
pub fn questions(
    c: &mut dyn Console,
    h: &mut dyn Host,
    before: Option<&Answers>,
) -> Result<Asked, String> {
    let b = before.cloned().unwrap_or_default();
    c.say("OmniFile edge filer setup\n\nPress Enter to keep the answer in [brackets].\n");

    let hostname = ask(
        c,
        "What is this filer called on the network? (letters, digits and hyphens, at most 15)",
        &b.hostname,
        |v| {
            if answers::is_hostname(v) {
                Ok(())
            } else {
                Err("use 1 to 15 letters, digits or hyphens, starting with a letter".into())
            }
        },
    )?;

    // Network.
    let wired = steps::wired_interfaces(h);
    let mode_default = if b.network.mode == "static" { "2" } else { "1" };
    let mode = ask(
        c,
        "How does it get its network address?\n  1  automatically (DHCP)\n  2  a fixed address",
        mode_default,
        |v| {
            if v == "1" || v == "2" {
                Ok(())
            } else {
                Err("answer 1 or 2".into())
            }
        },
    )?;
    let mut network = Network {
        mode: if mode == "2" {
            "static".into()
        } else {
            "dhcp".into()
        },
        ntp: b.network.ntp.clone(),
        search: b.network.search.clone(),
        ..Network::default()
    };
    if wired.len() > 1 || !b.network.interface.is_empty() {
        let list = wired.join(", ");
        let d = if b.network.interface.is_empty() {
            wired.first().cloned().unwrap_or_default()
        } else {
            b.network.interface.clone()
        };
        network.interface = ask(c, &format!("Which network interface? ({list})"), &d, |v| {
            if wired.is_empty() || wired.iter().any(|w| w == v) {
                Ok(())
            } else {
                Err(format!("choose one of {list}"))
            }
        })?;
    }
    if network.mode == "static" {
        network.address = ask(
            c,
            "Its address, with the prefix length (e.g. 10.1.2.10/24)",
            &b.network.address,
            |v| {
                answers::parse_cidr(v)
                    .map(|_| ())
                    .ok_or_else(|| "give the address and prefix, e.g. 10.1.2.10/24".into())
            },
        )?;
        network.gateway = ask(c, "The gateway", &b.network.gateway, |v| {
            v.parse::<std::net::IpAddr>()
                .map(|_| ())
                .map_err(|_| "give the gateway's IP address".into())
        })?;
        let dns = ask(
            c,
            "DNS servers, separated by spaces (usually your domain controllers)",
            &b.network.dns.join(" "),
            |v| {
                if v.trim().is_empty() {
                    Err("give at least one".into())
                } else {
                    ips(v)
                }
            },
        )?;
        network.dns = dns.split_whitespace().map(str::to_string).collect();
    } else {
        let dns = ask(
            c,
            "DNS servers, separated by spaces. Empty: the ones DHCP gives, which must know the AD domain",
            &b.network.dns.join(" "),
            ips,
        )?;
        network.dns = dns.split_whitespace().map(str::to_string).collect();
    }

    // The data disk. A rerun keeps the one an earlier run prepared (WP7: the
    // first run's mount made it "in use", and a failed join then looped).
    let found = disks(h);
    let (device, erase) = match found.iter().find(|d| d.prepared.is_some()) {
        Some(d) => (keep_data_disk(c, d)?, false),
        None => choose_data_disk(c, h, &found, &b)?,
    };

    // Active Directory.
    let domain = ask(
        c,
        "Your Active Directory domain (its DNS name, e.g. corp.example)",
        &b.ad.domain,
        |v| {
            if answers::is_dns_name(v) && v.contains('.') {
                Ok(())
            } else {
                Err("give the domain's DNS name, e.g. corp.example".into())
            }
        },
    )?;
    let wg_default = if b.ad.workgroup.is_empty() {
        domain.split('.').next().unwrap_or("").to_uppercase()
    } else {
        b.ad.workgroup.clone()
    };
    let workgroup = ask(c, "Its short (pre-Windows 2000) name", &wg_default, |v| {
        if !v.is_empty() && v.len() <= 15 {
            Ok(())
        } else {
            Err("at most 15 characters".into())
        }
    })?;
    let join_user = ask(
        c,
        "An account that may join computers to the domain",
        &b.ad.join_user,
        |v| {
            if v.is_empty() {
                Err("give the account's name".into())
            } else {
                Ok(())
            }
        },
    )?;
    let join_ou = ask(
        c,
        "The OU for this filer's computer account (e.g. OU=Filers,DC=corp,DC=example). Empty: the default",
        &b.ad.join_ou,
        any,
    )?;
    let admin_group = ask(
        c,
        "The AD group whose members administer this filer (they get sudo and ssh)",
        &b.ad.admin_group,
        |v| {
            if v.is_empty() {
                Err("give the group's name".into())
            } else {
                Ok(())
            }
        },
    )?;
    let joined = h
        .run(&["net", "ads", "testjoin"], &[], None)
        .map(|o| o.ok())
        .unwrap_or(false);
    let join_password = if joined {
        None
    } else {
        let p = c
            .secret(&format!("Password of {join_user} (not shown, not kept) > "))
            .ok_or(ENDED)?;
        Some(p)
    };

    let private_endpoints = yes_no(
        c,
        "Reach Azure storage only through private endpoints (never a public address)?",
        before.map(|b| b.storage.private_endpoints).unwrap_or(true),
    )?;

    let bundle = if steps::enrolled(h) {
        String::new()
    } else {
        ask(
            c,
            "The enrolment code from the portal (Edges, Add edge). Empty: enrol later",
            "",
            |v| {
                if v.is_empty() || v.starts_with("ofe1.") {
                    Ok(())
                } else {
                    Err("paste the whole code; it starts with ofe1.".into())
                }
            },
        )?
    };

    // The image's own feed (GitHub Releases, owner decision B) is offered first time round.
    let image_feed = h
        .read(crate::IMAGE_FEED_FILE)
        .map(|s| s.trim().to_string())
        .filter(|s| s.starts_with("https://"))
        .unwrap_or_default();
    let feed_default = if b.updates.feed.is_empty() && before.is_none() {
        image_feed
    } else {
        b.updates.feed.clone()
    };
    let feed = ask(
        c,
        "The update feed's address (https://...). none: no updates for now",
        &feed_default,
        |v| {
            if v.is_empty() || v == "none" || v.starts_with("https://") {
                Ok(())
            } else {
                Err("give an https:// address, or none".into())
            }
        },
    )?;
    let feed = if feed == "none" { String::new() } else { feed };
    let window_default = if b.updates.reboot_window.is_empty() {
        "Sun *-*-* 03:00".to_string()
    } else {
        b.updates.reboot_window.clone()
    };
    let reboot_window = ask(
        c,
        "When may it restart to apply an update? (a systemd time, e.g. Sat *-*-* 02:00)",
        &window_default,
        |v| {
            if v.is_empty() {
                Err("give a time".into())
            } else {
                Ok(())
            }
        },
    )?;

    let aws = if yes_no(
        c,
        "Will this filer serve files that are still in Amazon S3 (a migration), reading them \
         with IAM Roles Anywhere?",
        !b.aws.is_empty(),
    )? {
        let trust_anchor_arn = ask(
            c,
            "The Roles Anywhere trust anchor's ARN (arn:aws:rolesanywhere:<region>:<account>:trust-anchor/<id>)",
            &b.aws.trust_anchor_arn,
            |v| answers::rolesanywhere_arn_problem(v, "trust-anchor").map_or(Ok(()), Err),
        )?;
        let profile_arn = ask(
            c,
            "The Roles Anywhere profile's ARN (…:profile/<id>)",
            &b.aws.profile_arn,
            |v| answers::rolesanywhere_arn_problem(v, "profile").map_or(Ok(()), Err),
        )?;
        let role_arn = ask(
            c,
            "The role it reads S3 as (arn:aws:iam::<account>:role/<name>)",
            &b.aws.role_arn,
            |v| answers::role_arn_problem(v).map_or(Ok(()), Err),
        )?;
        let endpoint = ask(
            c,
            "The Roles Anywhere interface endpoint (https://...). Empty: its regional name, \
             which private DNS resolves to the endpoint",
            &b.aws.endpoint,
            |v| {
                if v.is_empty() || v.starts_with("https://") {
                    Ok(())
                } else {
                    Err("give an https:// address".into())
                }
            },
        )?;
        // Decision 50: OmniFile's edge CA by default; the client's own CA on request.
        let edge_ca = yes_no(
            c,
            "Should OmniFile's edge CA issue its Roles Anywhere certificate (the trust anchor is \
             OmniFile's edge CA certificate; renewed by itself)? Answer no if your own CA issues it",
            !b.aws.client_ca(),
        )?;
        answers::Aws {
            trust_anchor_arn,
            profile_arn,
            role_arn,
            endpoint,
            region: b.aws.region.clone(),
            session_seconds: b.aws.session_seconds,
            certificate: String::new(),
            issuer: if edge_ca {
                String::new()
            } else {
                answers::ISSUER_CLIENT.to_string()
            },
        }
    } else {
        answers::Aws::default()
    };

    let answers = Answers {
        version: 1,
        hostname,
        network,
        data: Data { device, erase },
        ad: Ad {
            domain: domain.to_lowercase(),
            workgroup: workgroup.to_uppercase(),
            join_user,
            join_ou,
            admin_group,
            idmap_range: if b.ad.idmap_range.is_empty() {
                "10000-999999".into()
            } else {
                b.ad.idmap_range.clone()
            },
        },
        storage: Storage { private_endpoints },
        service: Service { bundle },
        updates: Updates {
            feed,
            reboot_window,
        },
        aws,
    };
    let problems = answers.problems();
    if !problems.is_empty() {
        for p in &problems {
            c.say(&format!("  {p}"));
        }
        return Err("some answers need fixing; run omnifile-setup again".into());
    }
    c.say(&summary(&answers));
    if !yes_no(c, "Set up the filer with these answers?", false)? {
        return Err("stopped: nothing was changed".into());
    }
    Ok(Asked {
        answers,
        join_password,
    })
}

/// The answers as the admin reads them back.
pub fn summary(a: &Answers) -> String {
    let n = &a.network;
    let net = if n.mode == "static" {
        format!("{} via {}", n.address, n.gateway)
    } else {
        "DHCP".into()
    };
    format!(
        "\nThe filer:\n  name           {} ({})\n  network        {}{}{}\n  data disk      {}{}\n  domain         {} ({}), joined as {}{}\n  administrators {}\n  storage        {}\n  enrolment      {}\n  updates        {}\n  S3 reads       {}\n",
        a.hostname,
        a.fqdn(),
        net,
        if n.interface.is_empty() {
            String::new()
        } else {
            format!(" on {}", n.interface)
        },
        if n.dns.is_empty() {
            String::new()
        } else {
            format!(", DNS {}", n.dns.join(" "))
        },
        a.data.device,
        if a.data.erase {
            "  (WILL BE ERASED)"
        } else {
            ""
        },
        a.ad.domain,
        a.ad.workgroup,
        a.ad.join_user,
        if a.ad.join_ou.is_empty() {
            String::new()
        } else {
            format!(" into {}", a.ad.join_ou)
        },
        a.ad.admin_group,
        if a.storage.private_endpoints {
            "private endpoints only"
        } else {
            "public endpoints allowed"
        },
        if a.service.bundle.is_empty() {
            "later"
        } else {
            "with the code given"
        },
        if a.updates.feed.is_empty() {
            "none for now".to_string()
        } else {
            format!(
                "from {}, restarting at {}",
                a.updates.feed, a.updates.reboot_window
            )
        },
        if a.aws.is_empty() {
            "none (presigned URLs from the service only)".to_string()
        } else {
            format!(
                "IAM Roles Anywhere as {}, key sealed to the TPM, certificate from {}",
                a.aws.role_arn,
                if a.aws.client_ca() {
                    "your own CA"
                } else {
                    "OmniFile's edge CA"
                }
            )
        },
    )
}
