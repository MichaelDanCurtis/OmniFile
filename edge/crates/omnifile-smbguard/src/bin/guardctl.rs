//! omnifile-guardctl: the supported way to stop, disable, clean or uninstall
//! the guard unit. Refuses anything that would release the fanotify group
//! while smbd or Ganesha run (`omnifile_smbguard::interlock`).
//!
//! ```text
//! omnifile-guardctl [--unit U] [--servers a.service,b.service] [--volume DIR]
//!                   [--force] [--dry-run] status|stop|restart|disable|clean|mask|uninstall
//! ```
//!
//! Exit status: 0 done (or allowed with `--dry-run`), 3 refused, 2 usage, 1 a
//! systemctl call failed.
//!
//! **Advisory.** Root can always call `systemctl` directly; this tool only
//! stops an operator from releasing the group by mistake. Failing closed
//! rests on the units themselves: smbd and Ganesha `BindsTo=` the guard unit,
//! and the group lives in its fd store (`edge/systemd/`). `systemctl` is run
//! by absolute path ([`SYSTEMCTL`]), never looked up in `PATH`.

use std::process::{Command, ExitCode};

use omnifile_smbguard::interlock::{self, Observed, Op, Verdict};

/// systemd's control tool, by absolute path (Ubuntu 26.04: `/usr/bin`).
const SYSTEMCTL: &str = "/usr/bin/systemctl";

struct Args {
    unit: String,
    servers: Vec<String>,
    volume: Option<String>,
    force: bool,
    dry_run: bool,
    op: Op,
}

fn usage() -> ExitCode {
    eprintln!(
        "usage: omnifile-guardctl [--unit U] [--servers a,b] [--volume DIR] [--force] [--dry-run] \
         status|stop|restart|disable|clean|mask|uninstall"
    );
    ExitCode::from(2)
}

fn parse() -> Option<Args> {
    let a: Vec<String> = std::env::args().skip(1).collect();
    let mut r = Args {
        unit: "omnifile-edge.service".into(),
        servers: vec!["smbd.service".into(), "nfs-ganesha-omni.service".into()],
        volume: Some("/srv/omnifile/data".into()),
        force: false,
        dry_run: false,
        op: Op::Status,
    };
    let mut op = None;
    let mut i = 0;
    while i < a.len() {
        match a[i].as_str() {
            "--unit" => {
                r.unit = a.get(i + 1)?.clone();
                i += 1;
            }
            "--servers" => {
                r.servers = a
                    .get(i + 1)?
                    .split(',')
                    .filter(|s| !s.is_empty())
                    .map(String::from)
                    .collect();
                i += 1;
            }
            "--volume" => {
                r.volume = Some(a.get(i + 1)?.clone()).filter(|s| !s.is_empty());
                i += 1;
            }
            "--force" => r.force = true,
            "--dry-run" => r.dry_run = true,
            x => op = Some(Op::parse(x)?),
        }
        i += 1;
    }
    r.op = op?;
    Some(r)
}

fn systemctl(args: &[&str]) -> Option<String> {
    let out = Command::new(SYSTEMCTL).args(args).output().ok()?;
    Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn show(unit: &str, prop: &str) -> String {
    systemctl(&["show", "-p", prop, "--value", unit]).unwrap_or_default()
}

fn running(unit: &str) -> bool {
    matches!(
        systemctl(&["is-active", unit]).as_deref(),
        Some("active" | "activating" | "reloading" | "deactivating" | "refreshing")
    )
}

/// Processes (other than the guard itself and us) holding files under `vol`.
fn openers(vol: &str, exclude: &[u32]) -> Vec<String> {
    let mut out = Vec::new();
    let Ok(rd) = std::fs::read_dir("/proc") else {
        return out;
    };
    let prefix = format!("{}/", vol.trim_end_matches('/'));
    for e in rd.flatten() {
        let Some(pid) = e.file_name().to_str().and_then(|s| s.parse::<u32>().ok()) else {
            continue;
        };
        if exclude.contains(&pid) {
            continue;
        }
        let Ok(fds) = std::fs::read_dir(format!("/proc/{pid}/fd")) else {
            continue;
        };
        let holds = fds.flatten().any(|f| {
            std::fs::read_link(f.path())
                .map(|l| l.to_string_lossy().starts_with(&prefix))
                .unwrap_or(false)
        });
        if holds {
            let comm = std::fs::read_to_string(format!("/proc/{pid}/comm")).unwrap_or_default();
            out.push(format!("{}({pid})", comm.trim()));
        }
    }
    out
}

fn observe(a: &Args) -> Observed {
    let main_pid: u32 = show(&a.unit, "MainPID").parse().unwrap_or(0);
    let wanted_by = show(&a.unit, "WantedBy");
    let state = show(&a.unit, "UnitFileState");
    Observed {
        servers_active: a.servers.iter().filter(|s| running(s)).cloned().collect(),
        local_openers: a
            .volume
            .as_deref()
            .map(|v| openers(v, &[std::process::id(), main_pid]))
            .unwrap_or_default(),
        guard_active: running(&a.unit),
        fdstore_fds: show(&a.unit, "NFileDescriptorStore").parse().unwrap_or(0),
        referenced_when_stopped: state == "enabled"
            || wanted_by
                .split_whitespace()
                .any(|w| w == "multi-user.target"),
    }
}

fn run(dry: bool, cmds: &[&[&str]]) -> ExitCode {
    for c in cmds {
        println!("+ systemctl {}", c.join(" "));
        if dry {
            continue;
        }
        match Command::new(SYSTEMCTL).args(*c).status() {
            Ok(s) if s.success() => {}
            _ => return ExitCode::from(1),
        }
    }
    ExitCode::SUCCESS
}

fn main() -> ExitCode {
    let Some(a) = parse() else {
        return usage();
    };
    let o = observe(&a);
    println!(
        "guard {} ({}); fd store holds {} descriptor(s); referenced when stopped: {}; \
         servers running: [{}]; local openers: [{}]",
        a.unit,
        if o.guard_active { "active" } else { "inactive" },
        o.fdstore_fds,
        o.referenced_when_stopped,
        o.servers_active.join(", "),
        o.local_openers.join(", ")
    );
    match interlock::check(a.op, &o, a.force) {
        Verdict::Refused(why) => {
            eprintln!("REFUSED: {why}");
            return ExitCode::from(3);
        }
        Verdict::Warn(w) => eprintln!("warning: {w}"),
        Verdict::Allowed => {}
    }
    let u = a.unit.as_str();
    match a.op {
        Op::Status => ExitCode::SUCCESS,
        Op::Stop => run(a.dry_run, &[&["stop", u]]),
        Op::Restart => run(a.dry_run, &[&["restart", u]]),
        Op::Disable => run(a.dry_run, &[&["disable", u]]),
        Op::CleanFdstore => run(a.dry_run, &[&["stop", u], &["clean", "--what=fdstore", u]]),
        Op::Mask => run(a.dry_run, &[&["stop", u], &["mask", u]]),
        Op::Uninstall => run(
            a.dry_run,
            &[
                &["stop", u],
                &["clean", "--what=fdstore", u],
                &["disable", u],
            ],
        ),
    }
}
