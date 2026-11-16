//! `omnifile-annex [--config PATH] run --annex ID [--root DIR]`
//! `omnifile-annex [--config PATH] supervise`
//! `omnifile-annex [--config PATH] status --annex ID`
//!
//! `run` is one annex (`omnifile-annex@<id>.service`): it mounts the NAS
//! read-only in its own mount namespace, scans, and copies (`annex::runner`).
//! `--root DIR` reads DIR as the annex's directory instead of mounting (a
//! lab or test mount made by hand; the mount check still runs on it).
//! Signals: `SIGUSR1` a quick scan now, `SIGHUP` a full scan, `SIGUSR2` a
//! reconciliation; `SIGTERM` stops after sending what is queued.
//!
//! `supervise` (`omnifile-annex.service`) starts a unit per live annex of
//! this edge and stops the units of annexes that are gone or retired.
//!
//! `status` prints the annex's `annex.db` counts as JSON (operators, tests).
//!
//! Exit codes: 0 clean (or the annex is no longer this edge's); 1 runtime
//! error; 2 bad usage or config.

use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::atomic::Ordering;
use std::time::Duration;

use omnifile_edge::annex::db::AnnexDb;
use omnifile_edge::annex::runner::Runner;
use omnifile_edge::annex::service::{AnnexService, Live};
use omnifile_edge::annex::{AnnexConfig, mount, notify, watchdog};
use omnifile_edge::config::Config;
use tracing::{error, info, warn};

const DEFAULT_CONFIG: &str = "/etc/omnifile/edge.toml";

fn usage() -> ExitCode {
    eprintln!(
        "usage: omnifile-annex [--config PATH] (run --annex ID [--root DIR] | supervise | status --annex ID)"
    );
    ExitCode::from(2)
}

fn main() -> ExitCode {
    let mut config = PathBuf::from(DEFAULT_CONFIG);
    let mut cmd: Option<String> = None;
    let mut annex: Option<u64> = None;
    let mut root: Option<PathBuf> = None;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--config" => match args.next() {
                Some(p) => config = p.into(),
                None => return usage(),
            },
            "--annex" => match args.next().and_then(|v| v.parse().ok()) {
                Some(id) => annex = Some(id),
                None => return usage(),
            },
            "--root" => match args.next() {
                Some(p) => root = Some(p.into()),
                None => return usage(),
            },
            "--version" => {
                println!("omnifile-annex {}", env!("CARGO_PKG_VERSION"));
                return ExitCode::SUCCESS;
            }
            "run" | "supervise" | "status" if cmd.is_none() => cmd = Some(a),
            _ => return usage(),
        }
    }
    let cfg = match Config::load(&config) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("omnifile-annex: {e}");
            return ExitCode::from(2);
        }
    };
    omnifile_edge::init_logging(&cfg.log.format, &cfg.log.filter);
    omnifile_blob::net::set_private_only(cfg.network.private_only);
    omnifile_blob::net::set_loopback_ok(
        std::env::var("OMNIFILE_BLOB_ALLOW_HTTP").is_ok_and(|v| v == "1"),
    );
    if let Ok(p) = cfg.key_protection() {
        omnifile_control::seal::set_key_protection(p);
    }
    match (cmd.as_deref(), annex) {
        (Some("run"), Some(id)) => run(&cfg, id, root),
        (Some("status"), Some(id)) => status(&cfg.annex, id),
        (Some("supervise"), None) => supervise(&cfg),
        _ => usage(),
    }
}

fn live(cfg: &Config) -> Result<Live, String> {
    let dir = cfg
        .service
        .get("identity_dir")
        .and_then(|v| v.as_str())
        .unwrap_or("/var/lib/omnifile/identity");
    let ctl = omnifile_control::ControlConfig {
        api_url_override: cfg
            .service
            .get("api_url")
            .and_then(|v| v.as_str())
            .map(str::to_owned),
        ..omnifile_control::ControlConfig::default()
    };
    let control = omnifile_control::ControlClient::open(PathBuf::from(dir), ctl)
        .map_err(|e| format!("not enrolled? {e}"))?;
    let blob = omnifile_blob::BlobClient::new(omnifile_edge::io::blob_options())
        .map_err(|e| e.to_string())?;
    Ok(Live { control, blob })
}

fn runtime() -> Result<tokio::runtime::Runtime, String> {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .map_err(|e| e.to_string())
}

fn status(cfg: &AnnexConfig, id: u64) -> ExitCode {
    let path = cfg.state_dir.join(id.to_string()).join("annex.db");
    let db = match AnnexDb::open(&path) {
        Ok(d) => d,
        Err(e) => {
            eprintln!("omnifile-annex: {}: {e}", path.display());
            return ExitCode::FAILURE;
        }
    };
    let mut states = serde_json::Map::new();
    for s in ["new", "reported", "ready", "copied", "skipped"] {
        states.insert(s.into(), db.count(Some(s)).unwrap_or(-1).into());
    }
    let meta = |k: &str| db.meta(k).ok().flatten();
    let out = serde_json::json!({
        "annex_id": id,
        "entries": db.count(None).unwrap_or(-1),
        "states": states,
        "outbox": db.outbox_len().unwrap_or(-1),
        "copy_jobs": db.copy_jobs().unwrap_or_default(),
        "baseline_done": meta("baseline_done"),
        "last_quick": meta("last_quick"),
        "last_full": meta("last_full"),
        "last_reconcile": meta("last_reconcile"),
    });
    println!("{out}");
    ExitCode::SUCCESS
}

fn run(cfg: &Config, id: u64, root_override: Option<PathBuf>) -> ExitCode {
    let svc = match live(cfg) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("omnifile-annex: {e}");
            return ExitCode::FAILURE;
        }
    };
    let rt = match runtime() {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("omnifile-annex: runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    let creds = std::env::var_os("CREDENTIALS_DIRECTORY").map(PathBuf::from);
    let annex_cfg = cfg.annex.clone().with_credentials(creds.as_deref());
    match rt.block_on(run_annex(annex_cfg, svc, id, root_override)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            error!(annex = id, error = %e, "annex unit failed");
            ExitCode::FAILURE
        }
    }
}

async fn find(svc: &Live, id: u64) -> Result<Option<omnifile_control::annex::EdgeAnnex>, String> {
    let all = svc.annexes().await.map_err(|e| e.to_string())?;
    Ok(all.into_iter().find(|a| a.annex_id == id))
}

/// The exit of a unit whose NAS stopped answering (`EX_TEMPFAIL`).
const HANG_EXIT: i32 = 75;

async fn run_annex(
    cfg: AnnexConfig,
    svc: Live,
    id: u64,
    root_override: Option<PathBuf>,
) -> Result<(), String> {
    let annex = loop {
        match find(&svc, id).await {
            Ok(Some(a)) => break a,
            Ok(None) => {
                info!(
                    annex = id,
                    "this edge doesn't run that annex (retired?): stopping"
                );
                return Ok(());
            }
            Err(e) => {
                warn!(annex = id, error = %e, "service unreachable; retrying");
                tokio::time::sleep(Duration::from_secs(10)).await;
            }
        }
    };
    let state_dir = cfg.state_dir.join(id.to_string());
    std::fs::create_dir_all(&state_dir).map_err(|e| format!("{}: {e}", state_dir.display()))?;
    let db = AnnexDb::open(&state_dir.join("annex.db")).map_err(|e| e.to_string())?;
    // The NAS: mounted read-only by this unit, or a directory given.
    let (mountpoint, sec, mounted) = match &root_override {
        Some(r) => (r.clone(), "given", false),
        None => {
            let target = cfg.mount_root.join(id.to_string()).join("mnt");
            let extra = if annex.filer.protocol == "smb" {
                &cfg.smb_options
            } else {
                &cfg.nfs_options
            };
            let plan = mount::plan(&annex.filer, &target, extra)?;
            (target, plan.sec, true)
        }
    };
    // The annex's path beneath the mount: plain and relative, resolved
    // without following a symlink (`nasfs::open_root`).
    let root_rel = if root_override.is_some() {
        String::new()
    } else {
        annex.path.clone()
    };
    omnifile_edge::annex::nasfs::check_rel(&root_rel)
        .map_err(|e| format!("annex {id}: its path {root_rel:?}: {e}"))?;
    let root = if root_rel.is_empty() {
        mountpoint.clone()
    } else {
        mountpoint.join(&root_rel)
    };
    let ids: Box<dyn omnifile_identity::IdMap> = Box::new(omnifile_identity::CachedIdMap::new(
        omnifile_identity::Wbinfo::default(),
        Duration::from_secs(600),
        Duration::from_secs(60),
    ));
    let mut runner = Runner::new(svc, db, annex.clone(), &mountpoint, &state_dir, &cfg, ids);
    runner.root_rel = root_rel.clone();
    let krb = if mounted {
        mount::kerberos(&annex.filer, &cfg)
    } else {
        None
    };
    let mut last_kinit = tokio::time::Instant::now();
    if mounted {
        // A hard mount that stops answering holds the loop for as long as it
        // likes, with nothing said: say so, and try to die (`annex/watchdog.rs`).
        let base = cfg.mount_root.join(id.to_string());
        let note_dir = state_dir.clone();
        let handle = tokio::runtime::Handle::current();
        let teller = runner.svc.clone();
        let sec_word = sec.to_owned();
        watchdog::watch_mounts(
            vec![mountpoint.clone(), base.join("rw"), base.join("pl")],
            Duration::from_secs(cfg.hang_s.max(1)),
            move |p, secs| {
                let what = format!("{} has not answered for {secs} s", p.display());
                error!(
                    annex = id,
                    what, "ALERT annex: the NAS isn't answering: exiting, once the kernel lets it"
                );
                watchdog::leave_note(&note_dir, &what);
                let now = omnifile_edge::annex::now_ms();
                let at = omnifile_edge::annex::scan::rfc3339_ms(now);
                let report = watchdog::hung_report(&what, &sec_word, &at);
                let key = format!("hang-{id}-{now}");
                let told = handle.block_on(watchdog::say_hung(
                    &teller,
                    id,
                    &report,
                    &key,
                    watchdog::SAY_BOUND,
                ));
                if told {
                    // Said: the next start needn't say it again.
                    let _ = watchdog::take_note(&note_dir);
                } else {
                    warn!(
                        annex = id,
                        "the service wasn't told of the hang: the next start says it"
                    );
                }
                std::process::exit(HANG_EXIT);
            },
        );
    }
    if let Some(what) = watchdog::take_note(&state_dir) {
        runner.report_mount(serde_json::json!({"ok": false, "read_only": null,
            "readable": false, "sec": sec, "hung": true, "error": what}));
        // Before the mount below, which may itself take minutes to fail.
        runner.send().await;
    }
    // Mount and check, until it is safe to read from.
    loop {
        if mounted {
            let extra = if annex.filer.protocol == "smb" {
                &cfg.smb_options
            } else {
                &cfg.nfs_options
            };
            let plan = mount::plan(&annex.filer, &mountpoint, extra)?;
            let ticket = match &krb {
                Some(k) => mount::kinit(k).map(|_| last_kinit = tokio::time::Instant::now()),
                None => Ok(()),
            };
            if let Err(e) = ticket.and_then(|_| mount::mount(&plan)) {
                warn!(annex = id, error = %e, "mount failed; retrying");
                runner.report_mount(serde_json::json!({"ok": false, "read_only": null,
                    "readable": false, "sec": sec, "error": e}));
                runner
                    .while_unmounted(&format!("the NAS's mount: {e}"))
                    .await;
                tokio::time::sleep(Duration::from_secs(60)).await;
                continue;
            }
        }
        let (ok, details) = mount::check(&mountpoint, &root_rel, sec, true, mounted);
        runner.report_mount(details.clone());
        runner.send().await;
        if ok {
            info!(annex = id, root = %root.display(), ?details, "NAS mounted read-only");
            runner.mount_is_back();
            break;
        }
        warn!(
            annex = id,
            ?details,
            "the mount isn't safe to read from; retrying"
        );
        if mounted {
            mount::unmount(&mountpoint);
        }
        runner
            .while_unmounted("the mount isn't safe to read from")
            .await;
        tokio::time::sleep(Duration::from_secs(60)).await;
    }
    if annex.filer.protocol == "smb" && annex.filer.event_source == "smb_notify" {
        notify::watch(root.clone(), runner.wants.quick.clone());
    }
    use tokio::signal::unix::{SignalKind, signal};
    let sig = |k| signal(k).map_err(|e| e.to_string());
    let mut usr1 = sig(SignalKind::user_defined1())?;
    let mut usr2 = sig(SignalKind::user_defined2())?;
    let mut hup = sig(SignalKind::hangup())?;
    let mut term = sig(SignalKind::terminate())?;
    let mut int = sig(SignalKind::interrupt())?;
    let tick = Duration::from_secs(cfg.tick_s.max(1));
    let poll = Duration::from_secs(cfg.poll_s.max(5));
    let mut last_poll = tokio::time::Instant::now();
    // Dry runs are asked for often (the rule builder's live counts), so the
    // loop wakes every DRY_RUN_POLL; the full tick runs every `tick`.
    const DRY_RUN_POLL: Duration = Duration::from_secs(2);
    let mut next_tick = tokio::time::Instant::now();
    let rw_target = cfg.mount_root.join(id.to_string()).join("rw");
    let pl_target = cfg.mount_root.join(id.to_string()).join("pl");
    loop {
        if mounted {
            sync_rw(&mut runner, &rw_target, &cfg);
            let now = runner_now();
            runner.ask_placements(now).await;
            sync_pl(&mut runner, &pl_target, &cfg);
        }
        if tokio::time::Instant::now() >= next_tick {
            next_tick = tokio::time::Instant::now() + tick;
            let t = runner.tick().await;
            if t.scanned.is_some() || t.copied > 0 || t.drift > 0 || t.would_remove > 0 {
                info!(annex = id, ?t, "annex tick");
            }
        }
        let n = runner.dry_runs().await;
        if n > 0 {
            info!(annex = id, runs = n, "annex dry runs answered");
        }
        // The ticket lasts hours; SMB needs one again to reconnect.
        if let Some(k) = &krb
            && last_kinit.elapsed() >= Duration::from_secs(4 * 3600)
        {
            match mount::kinit(k) {
                Ok(()) => last_kinit = tokio::time::Instant::now(),
                Err(e) => warn!(annex = id, error = %e, "Kerberos ticket renewal failed"),
            }
        }
        // While removals are live, the settings (a pause) are read often;
        // the service is asked before every write in any case.
        let poll = if runner.removals_live() {
            poll.min(Duration::from_secs(10))
        } else {
            poll
        };
        if last_poll.elapsed() >= poll {
            last_poll = tokio::time::Instant::now();
            match find(&runner.svc, id).await {
                Ok(Some(a)) if a.path != annex.path && root_override.is_none() => {
                    // Mounted for the old path: a restart mounts the new one.
                    info!(annex = id, "the annex's path changed: restarting");
                    break;
                }
                Ok(Some(a)) => runner.update(a, &cfg),
                Ok(None) => {
                    info!(annex = id, "the annex is retired or moved: stopping");
                    break;
                }
                Err(e) => warn!(annex = id, error = %e, "settings poll failed"),
            }
        }
        let wants = runner.wants.clone();
        let wait =
            DRY_RUN_POLL.min(next_tick.saturating_duration_since(tokio::time::Instant::now()));
        let mut now_tick = || next_tick = tokio::time::Instant::now();
        tokio::select! {
            _ = tokio::time::sleep(wait) => {}
            _ = usr1.recv() => { wants.quick.store(true, Ordering::SeqCst); now_tick() }
            _ = usr2.recv() => { wants.reconcile.store(true, Ordering::SeqCst); now_tick() }
            _ = hup.recv() => { wants.full.store(true, Ordering::SeqCst); now_tick() }
            _ = term.recv() => break,
            _ = int.recv() => break,
        }
    }
    runner.send().await;
    if mounted {
        if runner.rw_mountpoint.take().is_some() {
            mount::unmount(&rw_target);
        }
        if runner.pl_mountpoint.take().is_some() {
            mount::unmount(&pl_target);
        }
        mount::unmount(&mountpoint);
    }
    Ok(())
}

/// The remover's read-write mount exists only while removals are live
/// (gate 1): mounted when they become live, unmounted when they stop, so a
/// Copy annex, one in its dry run or a paused one has no writable path.
fn sync_rw(runner: &mut Runner<Live>, target: &std::path::Path, cfg: &AnnexConfig) {
    // Live, or hidden files to put back after removals stopped.
    let live = runner.needs_rw();
    let id = runner.annex.annex_id;
    match (live, runner.rw_mountpoint.is_some()) {
        (true, false) => {
            let filer = &runner.annex.filer;
            let extra = if filer.protocol == "smb" {
                &cfg.smb_options
            } else {
                &cfg.nfs_options
            };
            let res = (|| {
                let read = mount::read_name(cfg, filer);
                if let Some(why) =
                    mount::cache_problem(&cfg.remove_principal, cfg.remove_uid, read.as_deref())
                {
                    return Err(why);
                }
                if !cfg.remove_principal.is_empty() {
                    let k = mount::Kerberos {
                        keytab: cfg.remove_keytab.clone(),
                        principal: cfg.remove_principal.clone(),
                    };
                    mount::kinit_into(&k, cfg.remove_uid)?;
                }
                let p = mount::rw_plan(filer, target, extra, cfg.remove_uid)?;
                mount::mount_rw(&p)?;
                let f = mount::vfs_flags(target).map_err(|e| e.to_string())?;
                if let Some(why) = mount::flags_problem(f, false) {
                    return Err(why);
                }
                Ok(())
            })();
            match res {
                Ok(()) => {
                    info!(annex = id, "removals are live: the read-write mount is up");
                    // A pause's hidden files wait their grace again (gate 7).
                    let _ = runner.db.restart_grace(runner_now());
                    runner.rw_mountpoint = Some(target.to_owned());
                }
                Err(e) => {
                    warn!(annex = id, error = %e, "the read-write mount failed; no removals");
                    mount::unmount(target);
                }
            }
        }
        (false, true) => {
            info!(annex = id, "removals stopped: the read-write mount is gone");
            mount::unmount(target);
            runner.rw_mountpoint = None;
        }
        _ => {}
    }
}

/// The placement mount (A3.3) exists only while a placement is in hand,
/// with the restore-rights account's ticket (`cruid`); over SMB with backup
/// intent (`backupuid`) for the opens this daemon makes, its own uid, so the
/// restore account's privilege can set another owner. No other mount has it. It is the most
/// powerful handle the edge holds (threat model T7): only the placer has it.
fn sync_pl(runner: &mut Runner<Live>, target: &std::path::Path, cfg: &AnnexConfig) {
    let want = runner.needs_pl();
    let id = runner.annex.annex_id;
    match (want, runner.pl_mountpoint.is_some()) {
        (true, false) => {
            let filer = &runner.annex.filer;
            let account = mount::placement_account(cfg, filer, &runner.annex.mode);
            let mut extra = if filer.protocol == "smb" {
                cfg.smb_options.clone()
            } else {
                cfg.nfs_options.clone()
            };
            if filer.protocol == "smb" {
                // SAFETY: geteuid has no preconditions.
                let b = format!("backupuid={}", unsafe { libc::geteuid() });
                extra = if extra.trim().is_empty() {
                    b
                } else {
                    format!("{extra},{b}")
                };
            }
            let mut account_uid = 0;
            let res = (|| {
                let (keytab, principal, uid) = account?;
                account_uid = uid;
                if !principal.is_empty() {
                    let k = mount::Kerberos { keytab, principal };
                    mount::kinit_into(&k, uid)?;
                }
                let p = mount::rw_plan(filer, target, &extra, uid)?;
                mount::mount_rw(&p)?;
                let f = mount::vfs_flags(target).map_err(|e| e.to_string())?;
                if let Some(why) = mount::flags_problem(f, false) {
                    return Err(why);
                }
                Ok(())
            })();
            match res {
                Ok(()) => {
                    info!(annex = id, "a placement in hand: the placement mount is up");
                    runner.pl_mountpoint = Some(target.to_owned());
                }
                Err(e) => {
                    warn!(annex = id, error = %e, "the placement mount failed; no placements now");
                    runner.pl_problem = Some(e.to_string());
                    mount::unmount(target);
                    if mount::placement_cache_to_drop(cfg).is_some() {
                        mount::drop_ccache(account_uid);
                    }
                }
            }
        }
        (false, true) => {
            info!(
                annex = id,
                "no placement in hand: the placement mount is gone"
            );
            mount::unmount(target);
            if let Some(uid) = mount::placement_cache_to_drop(cfg) {
                mount::drop_ccache(uid);
            }
            runner.pl_mountpoint = None;
        }
        _ => {}
    }
}

fn runner_now() -> i64 {
    omnifile_edge::annex::now_ms()
}

fn systemctl(bin: &str, args: &[&str]) -> Result<String, String> {
    let out = std::process::Command::new(bin)
        .args(args)
        .output()
        .map_err(|e| e.to_string())?;
    if out.status.success() {
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    } else {
        Err(String::from_utf8_lossy(&out.stderr).trim().to_owned())
    }
}

fn supervise(cfg: &Config) -> ExitCode {
    let svc = match live(cfg) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("omnifile-annex: {e}");
            return ExitCode::FAILURE;
        }
    };
    let rt = match runtime() {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("omnifile-annex: runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    let bin = cfg.annex.systemctl.clone();
    let poll = Duration::from_secs(cfg.annex.poll_s.max(5));
    rt.block_on(async move {
        loop {
            match svc.annexes().await {
                Ok(list) => {
                    let want: Vec<String> = list
                        .iter()
                        .map(|a| format!("omnifile-annex@{}.service", a.annex_id))
                        .collect();
                    for u in &want {
                        if systemctl(&bin, &["is-active", "--quiet", u]).is_err() {
                            match systemctl(&bin, &["start", u]) {
                                Ok(_) => info!(unit = u, "annex unit started"),
                                Err(e) => warn!(unit = u, error = e, "annex unit didn't start"),
                            }
                        }
                    }
                    let running = systemctl(
                        &bin,
                        &[
                            "list-units",
                            "--plain",
                            "--no-legend",
                            "omnifile-annex@*.service",
                        ],
                    )
                    .unwrap_or_default();
                    for line in running.lines() {
                        let Some(u) = line.split_whitespace().next() else {
                            continue;
                        };
                        if !want.iter().any(|w| w == u) {
                            match systemctl(&bin, &["stop", u]) {
                                Ok(_) => info!(unit = u, "annex unit stopped (retired)"),
                                Err(e) => warn!(unit = u, error = e, "annex unit didn't stop"),
                            }
                        }
                    }
                }
                Err(e) => warn!(error = %e, "annexes: service unreachable"),
            }
            tokio::time::sleep(poll).await;
        }
    })
}
