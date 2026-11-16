//! `omnifile-setup`: the edge filer's setup wizard.
//!
//! ```text
//! omnifile-setup                    ask on the console, then set up the filer
//! omnifile-setup apply [--answers FILE] [--only STEP,...] [--root DIR]
//!                                   set up from an answers file (default: the saved
//!                                   one). --root writes the files below DIR and runs
//!                                   nothing: a review of what would be written
//! omnifile-setup unattended         set up from systemd credentials (first boot):
//!                                   omnifile.setup (the answers), and optionally
//!                                   omnifile.join-password, omnifile.enrol-bundle and
//!                                   omnifile.recovery-hash
//! omnifile-setup enrol [CODE|-]     enrol with a code from the portal, then start
//!                                   the file servers
//! omnifile-setup aws-cert FILE      (`[aws] issuer = "client"` only) install the
//!                                   certificate the trust anchor's CA
//!                                   issued for /etc/omnifile/roles-anywhere.csr (IAM
//!                                   Roles Anywhere), then start the credential helper
//! omnifile-setup check [FILE]       check an answers file
//! omnifile-setup show               the saved answers, as the admin reads them
//! omnifile-setup check-smb-module   whether smbd can load vfs_omnifile_prevver (build info
//!                                   against the running Samba, then a real load); exit 1 if not
//! omnifile-setup recovery seal      give the break-glass account a new password,
//!                                   shown once (after every use of it)
//! omnifile-setup recovery status    whether it is sealed, and used since
//! omnifile-setup recovery-used      PAM's hook (pam_exec) on its sessions
//! ```
//!
//! The domain join password is never stored. `apply` reads it from
//! `OMNIFILE_JOIN_PASSWORD` or the credential, or asks on a terminal.
//! Exit codes: 0 done, 1 a step failed, 2 bad usage or answers.

use std::process::ExitCode;

use omnifile_setup::answers::Answers;
use omnifile_setup::host::{Host, System};
use omnifile_setup::prompt::{self, Console, Terminal};
use omnifile_setup::render::{RECOVERY_USED, RECOVERY_USER};
use omnifile_setup::steps::{self, Ctx, Outcome, STEPS};
use omnifile_setup::{
    ANSWERS_FILE, CRED_ANSWERS, CRED_BUNDLE, CRED_PASSWORD, CRED_RECOVERY_HASH, DONE_FILE,
};

fn usage() -> ExitCode {
    eprintln!(
        "usage: omnifile-setup [apply [--answers FILE] [--only STEP,...] [--root DIR] | unattended | \
         enrol [CODE|-] | aws-cert FILE | check [FILE] | show]\nsteps: {}",
        STEPS.join(", ")
    );
    ExitCode::from(2)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let cmd = args.first().map(String::as_str).unwrap_or("");
    let r = match cmd {
        "" => interactive(),
        "apply" => apply(&args[1..]),
        "unattended" => unattended(),
        "enrol" => enrol(args.get(1).map(String::as_str)),
        "aws-cert" => aws_cert(args.get(1).map(String::as_str)),
        "check" => check(args.get(1).map(String::as_str).unwrap_or(ANSWERS_FILE)),
        "show" => show(),
        "check-smb-module" => check_smb_module(),
        "recovery" => recovery(args.get(1).map(String::as_str)),
        // PAM's hook: it must never fail a login.
        "recovery-used" => {
            recovery_used();
            return ExitCode::SUCCESS;
        }
        "-h" | "--help" | "help" => return usage(),
        _ => return usage(),
    };
    match r {
        Ok(()) => ExitCode::SUCCESS,
        Err((code, msg)) => {
            eprintln!("omnifile-setup: {msg}");
            ExitCode::from(code)
        }
    }
}

type Res = Result<(), (u8, String)>;

fn check_smb_module() -> Res {
    match steps::check_smb_module() {
        Ok(Some(msg)) => {
            println!("ok: {msg}");
            Ok(())
        }
        Ok(None) => {
            println!("ok: no smbd, or vfs_omnifile_prevver is not installed: nothing to check");
            Ok(())
        }
        Err(e) => Err((1, e)),
    }
}

fn bad(m: impl Into<String>) -> (u8, String) {
    (2, m.into())
}

fn need_root() -> Res {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        return Err(bad("run it as root (sudo omnifile-setup)"));
    }
    Ok(())
}

fn load(h: &dyn Host, path: &str) -> Result<Answers, (u8, String)> {
    let text = h
        .read(path)
        .or_else(|| std::fs::read_to_string(path).ok())
        .ok_or_else(|| {
            bad(format!(
                "no answers at {path}: run omnifile-setup on the console first"
            ))
        })?;
    let a = Answers::parse(&text).map_err(|e| bad(format!("{path}: {e}")))?;
    let problems = a.problems();
    if !problems.is_empty() {
        let list: Vec<String> = problems.iter().map(|p| format!("  {p}")).collect();
        return Err(bad(format!("{path} needs fixing:\n{}", list.join("\n"))));
    }
    Ok(a)
}

fn save(h: &mut dyn Host, a: &Answers) -> Res {
    // 0600: until enrolment the file holds the one-time code.
    h.write(
        ANSWERS_FILE,
        &format!("{}\n{}", omnifile_setup::render::OWNED, a.to_toml()),
        0o600,
    )
    .map(|_| ())
    .map_err(|e| (1, format!("saving {ANSWERS_FILE}: {e}")))
}

fn credential(name: &str) -> Option<String> {
    let dir = std::env::var_os("CREDENTIALS_DIRECTORY")?;
    let v = std::fs::read_to_string(std::path::Path::new(&dir).join(name)).ok()?;
    let v = v.trim_end_matches(['\n', '\r']).to_string();
    (!v.is_empty()).then_some(v)
}

/// Runs the steps, printing each outcome, then saves the answers (the code
/// gone once used, the erase flag cleared once formatted).
fn execute(h: &mut dyn Host, c: &mut Ctx, only: Option<&[String]>) -> Res {
    let r = steps::run(h, c, only, |step, o| match o {
        Outcome::Changed(w) => println!("  {step:<9} done: {w}"),
        Outcome::Unchanged => println!("  {step:<9} already as asked"),
        Outcome::Skipped(w) => println!("  {step:<9} skipped: {w}"),
    });
    save(h, &c.answers)?;
    if let Some(pw) = c.revealed.take() {
        reveal(&pw);
    }
    match r {
        Ok(done) => {
            if only.is_none() {
                h.write(DONE_FILE, "", 0o644)
                    .map_err(|e| (1, format!("{DONE_FILE}: {e}")))?;
            }
            let waiting = done.iter().any(|(_, o)| matches!(o, Outcome::Skipped(_)));
            println!(
                "{}",
                if waiting {
                    "Setup is done, apart from the steps skipped above."
                } else {
                    "Setup is done."
                }
            );
            Ok(())
        }
        Err(f) => Err((
            1,
            format!(
                "stopped at {f}\nFix it and run omnifile-setup again: the steps already done are kept."
            ),
        )),
    }
}

/// Shows a new recovery password, once.
fn reveal(pw: &str) {
    println!(
        "\nThe recovery account {RECOVERY_USER} is sealed. Its password, shown only now:\n\n    {pw}\n\n\
         Keep it where your break-glass credentials live (a vault, or a sealed envelope).\n\
         It logs in on this console only, never over ssh, for when the domain can't be reached.\n\
         Every use is logged and shown in the portal until it is sealed again:\n\
         sudo omnifile-setup recovery seal\n"
    );
}

fn interactive() -> Res {
    need_root()?;
    let mut h = System::new("/");
    let before = h.read(ANSWERS_FILE).and_then(|t| Answers::parse(&t).ok());
    let mut term = Terminal;
    let asked = prompt::questions(&mut term, &mut h, before.as_ref()).map_err(|e| (1, e))?;
    let mut c = Ctx::new(asked.answers);
    c.join_password = asked.join_password;
    c.can_reveal = true;
    save(&mut h, &c.answers)?;
    term.say("\nSetting up:");
    execute(&mut h, &mut c, None)
}

fn apply(args: &[String]) -> Res {
    let mut answers = ANSWERS_FILE.to_string();
    let mut only: Option<Vec<String>> = None;
    let mut root = "/".to_string();
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--answers" => {
                answers = it
                    .next()
                    .ok_or_else(|| bad("--answers needs a file"))?
                    .clone()
            }
            "--only" => {
                let v: Vec<String> = it
                    .next()
                    .ok_or_else(|| bad("--only needs steps"))?
                    .split(',')
                    .map(|s| s.trim().to_string())
                    .collect();
                if let Some(u) = v.iter().find(|s| !STEPS.contains(&s.as_str())) {
                    return Err(bad(format!(
                        "no step {u}; the steps are {}",
                        STEPS.join(", ")
                    )));
                }
                only = Some(v);
            }
            "--root" => {
                root = it
                    .next()
                    .ok_or_else(|| bad("--root needs a directory"))?
                    .clone()
            }
            other => return Err(bad(format!("unknown option {other}"))),
        }
    }
    let mut h = System::new(&root);
    if h.live() {
        need_root()?;
    }
    let a = load(&h, &answers)?;
    let mut c = Ctx::new(a);
    c.join_password = std::env::var("OMNIFILE_JOIN_PASSWORD")
        .ok()
        .filter(|p| !p.is_empty())
        .or_else(|| credential(CRED_PASSWORD));
    c.recovery_hash = credential(CRED_RECOVERY_HASH);
    // SAFETY: isatty has no preconditions.
    c.can_reveal = h.live() && unsafe { libc::isatty(0) == 1 && libc::isatty(1) == 1 };
    let joins = only.as_ref().is_none_or(|o| o.iter().any(|s| s == "ad"));
    if c.join_password.is_none()
        && joins
        && h.live()
        // SAFETY: isatty has no preconditions.
        && unsafe { libc::isatty(0) } == 1
        && !h.run(&["net", "ads", "testjoin"], &[], None).map(|o| o.ok()).unwrap_or(false)
    {
        c.join_password = Terminal.secret(&format!(
            "Password of {} (not shown, not kept) > ",
            c.answers.ad.join_user
        ));
    }
    let r = execute(&mut h, &mut c, only.as_deref());
    if !h.live() {
        println!("Wrote the files below {root}. Commands a live run would add:");
        for s in &h.skipped {
            println!("  {s}");
        }
    }
    r
}

fn unattended() -> Res {
    need_root()?;
    let text = credential(CRED_ANSWERS).ok_or_else(|| {
        bad(format!(
            "no credential {CRED_ANSWERS} (ImportCredential= in the unit)"
        ))
    })?;
    let mut a =
        Answers::parse(&text).map_err(|e| bad(format!("credential {CRED_ANSWERS}: {e}")))?;
    if let Some(b) = credential(CRED_BUNDLE) {
        a.service.bundle = b;
    }
    let problems = a.problems();
    if !problems.is_empty() {
        let list: Vec<String> = problems.iter().map(|p| format!("  {p}")).collect();
        return Err(bad(format!(
            "credential {CRED_ANSWERS} needs fixing:\n{}",
            list.join("\n")
        )));
    }
    let mut h = System::new("/");
    let mut c = Ctx::new(a);
    c.join_password = credential(CRED_PASSWORD);
    c.recovery_hash = credential(CRED_RECOVERY_HASH);
    println!("Unattended setup of {}:", c.answers.hostname);
    execute(&mut h, &mut c, None)
}

fn enrol(code: Option<&str>) -> Res {
    need_root()?;
    let mut h = System::new("/");
    let mut a = load(&h, ANSWERS_FILE)?;
    let code = match code {
        Some("-") => {
            let mut s = String::new();
            std::io::Read::read_to_string(&mut std::io::stdin(), &mut s)
                .map_err(|e| bad(e.to_string()))?;
            s
        }
        Some(c) => c.to_string(),
        None => Terminal
            .line("The enrolment code from the portal (Edges, Add edge)\n> ")
            .unwrap_or_default(),
    };
    let code = code.trim().to_string();
    if !code.starts_with("ofe1.") {
        return Err(bad("that is not an enrolment code; it starts with ofe1."));
    }
    a.service.bundle = code;
    let mut c = Ctx::new(a);
    execute(
        &mut h,
        &mut c,
        Some(&["enrol".to_string(), "services".to_string()]),
    )
}

/// Installs the certificate the trust anchor's CA issued for this filer's
/// Roles Anywhere request, and starts the helper.
fn aws_cert(path: Option<&str>) -> Res {
    need_root()?;
    let Some(path) = path else {
        return Err(bad(
            "usage: omnifile-setup aws-cert FILE (PEM, the certificate first)",
        ));
    };
    let path = std::path::absolute(path)
        .map_err(|e| bad(format!("{path}: {e}")))?
        .to_string_lossy()
        .into_owned();
    let mut h = System::new("/");
    let mut a = load(&h, ANSWERS_FILE)?;
    if a.aws.is_empty() {
        return Err(bad(
            "Roles Anywhere is not set up: add [aws] to the answers (omnifile-setup), then run it again",
        ));
    }
    if !a.aws.client_ca() {
        return Err(bad(
            "OmniFile's edge CA issues this filer's Roles Anywhere certificate, and the daemon \
             renews it: set [aws] issuer = \"client\" (omnifile-setup) to install one from your own CA",
        ));
    }
    a.aws.certificate = path;
    let mut c = Ctx::new(a);
    execute(&mut h, &mut c, Some(&["aws".to_string()]))
}

fn check(path: &str) -> Res {
    let h = System::new("/");
    let a = load(&h, path)?;
    println!("{path} is fine.{}", prompt::summary(&a));
    Ok(())
}

fn show() -> Res {
    let h = System::new("/");
    let a = load(&h, ANSWERS_FILE)?;
    print!("{}", prompt::summary(&a));
    println!(
        "  set up         {}",
        if h.exists(DONE_FILE) {
            "yes"
        } else {
            "not finished"
        }
    );
    Ok(())
}

fn recovery(what: Option<&str>) -> Res {
    match what {
        Some("seal") => {
            need_root()?;
            let mut h = System::new("/");
            let pw = steps::seal_recovery(&mut h).map_err(|e| (1, e))?;
            reveal(&pw);
            Ok(())
        }
        Some("status") => {
            let mut h = System::new("/");
            let o = h
                .run(&["passwd", "-S", RECOVERY_USER], &[], None)
                .map_err(|e| (1, e.to_string()))?;
            let state = match o.stdout.split_whitespace().nth(1) {
                Some("P") => "sealed (it has a password)",
                Some("L") => "locked (no password yet: seal it on the console)",
                _ if !o.ok() => "missing (run omnifile-setup)",
                _ => "without a password",
            };
            println!("{RECOVERY_USER}: {state}");
            if h.exists(RECOVERY_USED) {
                println!("It has been used since it was last sealed: seal it again.");
            }
            Ok(())
        }
        _ => Err(bad("usage: omnifile-setup recovery seal|status")),
    }
}

/// pam_exec on every session: a session of the recovery account is logged
/// at auth.alert (the edge's Azure Monitor rule picks it up), and marked so
/// the heartbeat reports it until the account is sealed again.
fn recovery_used() {
    let user = std::env::var("PAM_USER").unwrap_or_default();
    let kind = std::env::var("PAM_TYPE").unwrap_or_default();
    if user != RECOVERY_USER || kind != "open_session" {
        return;
    }
    let tty = std::env::var("PAM_TTY").unwrap_or_else(|_| "?".into());
    let svc = std::env::var("PAM_SERVICE").unwrap_or_else(|_| "?".into());
    let mut h = System::new("/");
    let _ = h.write(RECOVERY_USED, &format!("{tty} {svc}\n"), 0o600);
    let _ = h.run(
        &[
            "logger",
            "-p",
            "auth.alert",
            "-t",
            "omnifile-recovery",
            &format!(
                "the break-glass account {RECOVERY_USER} opened a session ({svc} on {tty}); \
                 seal it again afterwards: omnifile-setup recovery seal"
            ),
        ],
        &[],
        None,
    );
}
