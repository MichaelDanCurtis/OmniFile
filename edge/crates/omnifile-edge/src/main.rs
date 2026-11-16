//! `omnifile-edge [--config PATH] [--check]`
//! `omnifile-edge [--config PATH] enrol BUNDLE-FILE`
//! `omnifile-edge [--config PATH] roles-anywhere-cert`
//!
//! `roles-anywhere-cert` gets the edge's IAM Roles Anywhere certificate from
//! the edge CA now (decision 50), for the setup wizard's `aws` step.
//!
//! `enrol` reads a one-time enrolment bundle (`ofe1.…`, from the portal;
//! `-` reads stdin), enrols with the service (control API §2) and stores the
//! identity in `[service] identity_dir`. The daemon then takes its `edge_id`
//! from that identity when `edge_id` is not set in the config.
//!
//! Exit codes: 0 clean shutdown; 1 runtime error; 2 bad config or failed
//! startup check (the daemon refuses to run where it could fail open).

use std::path::PathBuf;
use std::process::ExitCode;

use omnifile_edge::config::Config;

const DEFAULT_CONFIG: &str = "/etc/omnifile/edge.toml";

fn usage() -> ExitCode {
    eprintln!(
        "usage: omnifile-edge [--config PATH] [--check | enrol BUNDLE-FILE | roles-anywhere-cert]"
    );
    ExitCode::from(2)
}

fn main() -> ExitCode {
    let mut config = PathBuf::from(DEFAULT_CONFIG);
    let mut check_only = false;
    let mut enrol_bundle: Option<String> = None;
    let mut roles_anywhere = false;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--config" => match args.next() {
                Some(p) => config = p.into(),
                None => return usage(),
            },
            "--check" => check_only = true,
            "enrol" => match args.next() {
                Some(p) => enrol_bundle = Some(p),
                None => return usage(),
            },
            "roles-anywhere-cert" => roles_anywhere = true,
            "--version" => {
                println!("omnifile-edge {}", env!("CARGO_PKG_VERSION"));
                return ExitCode::SUCCESS;
            }
            _ => return usage(),
        }
    }
    let cfg = match Config::load(&config) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("omnifile-edge: {e}");
            return ExitCode::from(2);
        }
    };
    omnifile_edge::init_logging(&cfg.log.format, &cfg.log.filter);
    // Before any client exists, so enrolment and every later connection obey it.
    omnifile_blob::net::set_private_only(cfg.network.private_only);
    // The DOS "offline" attribute lives in an xattr only Linux's Samba reads.
    #[cfg(target_os = "linux")]
    omnifile_marker::dosattr::set_enabled(cfg.shares.smb_offline);
    // The local sim listens on loopback; an appliance never reaches it.
    omnifile_blob::net::set_loopback_ok(
        std::env::var("OMNIFILE_BLOB_ALLOW_HTTP").is_ok_and(|v| v == "1"),
    );
    // Likewise for the keys: sealed with systemd credentials unless `file`.
    // Config::load has checked the value.
    if let Ok(p) = cfg.key_protection() {
        omnifile_control::seal::set_key_protection(p);
    }
    if let Some(bundle) = enrol_bundle {
        return enrol(&cfg, &bundle);
    }
    if roles_anywhere {
        return roles_anywhere_cert(&cfg);
    }
    run(cfg, check_only)
}

/// `omnifile-edge roles-anywhere-cert`: get the IAM Roles Anywhere
/// certificate from the edge CA now (decision 50), for the request the setup
/// wizard left in `[s3] roles_anywhere_dir`, over the enrolled mTLS
/// identity. The setup wizard runs it; the daemon renews by itself after.
fn roles_anywhere_cert(cfg: &Config) -> ExitCode {
    let dir = std::path::PathBuf::from(
        cfg.service
            .get("identity_dir")
            .and_then(|v| v.as_str())
            .unwrap_or("/var/lib/omnifile/identity"),
    );
    let ctl = omnifile_control::ControlConfig {
        api_url_override: cfg
            .service
            .get("api_url")
            .and_then(|v| v.as_str())
            .map(str::to_owned),
        ..omnifile_control::ControlConfig::default()
    };
    let files = omnifile_control::RolesAnywhereFiles::new(&cfg.s3.roles_anywhere_dir);
    if !files.enabled() {
        eprintln!(
            "omnifile-edge: no certificate request in {} (the setup wizard's aws step writes it)",
            files.dir.display()
        );
        return ExitCode::from(2);
    }
    let client = match omnifile_control::ControlClient::open(dir, ctl) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("omnifile-edge: not enrolled? {e}");
            return ExitCode::FAILURE;
        }
    };
    let rt = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("omnifile-edge: runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    match rt.block_on(client.renew_roles_anywhere_if_due(&files, true)) {
        Ok(_) => {
            let until = files.installed().map(|i| i.not_after).unwrap_or_default();
            println!(
                "Roles Anywhere certificate: {} (until {until})",
                files
                    .path(omnifile_control::roles_anywhere::CERT_FILE)
                    .display()
            );
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("omnifile-edge: Roles Anywhere certificate: {e}");
            ExitCode::FAILURE
        }
    }
}

/// `omnifile-edge enrol BUNDLE-FILE`: one-time enrolment.
fn enrol(cfg: &Config, bundle_path: &str) -> ExitCode {
    use std::io::Read as _;

    let mut text = String::new();
    let read = if bundle_path == "-" {
        std::io::stdin().read_to_string(&mut text).map(|_| ())
    } else {
        std::fs::read_to_string(bundle_path).map(|t| text = t)
    };
    if let Err(e) = read {
        eprintln!("omnifile-edge: reading {bundle_path}: {e}");
        return ExitCode::from(2);
    }
    let bundle = match omnifile_control::EnrolmentBundle::parse(text.trim()) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("omnifile-edge: bad enrolment bundle: {e}");
            return ExitCode::from(2);
        }
    };
    let dir = std::path::PathBuf::from(
        cfg.service
            .get("identity_dir")
            .and_then(|v| v.as_str())
            .unwrap_or("/var/lib/omnifile/identity"),
    );
    if let Err(e) = std::fs::create_dir_all(&dir) {
        eprintln!("omnifile-edge: {}: {e}", dir.display());
        return ExitCode::FAILURE;
    }
    let facts = omnifile_control::types::EdgeFacts {
        hostname: std::fs::read_to_string("/proc/sys/kernel/hostname")
            .map(|h| h.trim().to_owned())
            .unwrap_or_default(),
        os: std::fs::read_to_string("/etc/os-release")
            .ok()
            .and_then(|t| {
                t.lines()
                    .find_map(|l| l.strip_prefix("PRETTY_NAME="))
                    .map(|v| v.trim_matches('"').to_owned())
            }),
        kernel: std::fs::read_to_string("/proc/sys/kernel/osrelease")
            .ok()
            .map(|k| k.trim().to_owned()),
        version: env!("CARGO_PKG_VERSION").to_owned(),
    };
    let rt = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("omnifile-edge: runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    match rt.block_on(omnifile_control::enrol(
        &bundle,
        &dir,
        facts,
        omnifile_blob::TlsVersions::default(),
    )) {
        Ok(id) => {
            println!(
                "enrolled: edge_id={} api_url={} identity={}",
                id.info.edge_id,
                id.info.api_url,
                dir.display()
            );
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("omnifile-edge: enrolment failed: {e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(not(target_os = "linux"))]
fn run(_cfg: Config, _check_only: bool) -> ExitCode {
    eprintln!("omnifile-edge runs on Linux only (xfs, fanotify, trusted.* xattrs)");
    ExitCode::from(2)
}

#[cfg(target_os = "linux")]
fn run(mut cfg: Config, check_only: bool) -> ExitCode {
    use std::sync::Arc;

    use omnifile_edge::checks::startup_checks;
    use omnifile_edge::daemon::Daemon;
    use omnifile_edge::fakes::FakeService;
    use omnifile_edge::guard::{self, EdgeIdentity};
    use tracing::{error, info, warn};

    // The smb-guard first: before the runtime or any other thread exists,
    // and before anything else opens descriptors (it takes the systemd-passed
    // group and reserves its predecessor's event fd numbers).
    let identity = EdgeIdentity::system();
    // The write detector's group from the fd store (WP9): only its number,
    // from the environment, before the guard takes `LISTEN_*`.
    let detect_fds = if check_only {
        Vec::new()
    } else {
        omnifile_edge::fdstore::peek()
    };
    let guard = if check_only {
        None
    } else if cfg.smbguard.enabled {
        match guard::start(&cfg, &identity) {
            Ok(g) => Some(g),
            Err(e) => {
                error!(err = %e, "smb-guard did not start");
                eprintln!("omnifile-edge: smb-guard: {e}");
                return ExitCode::from(2);
            }
        }
    } else {
        // Never READY with the guard off while smbd or Ganesha are bound to
        // this unit: they would start and serve stubs unguarded (M2 review S8).
        match omnifile_smbguard::interlock::disabled_guard_refusal(
            std::path::Path::new("/"),
            cfg.smbguard.allow_disabled_with_servers,
        ) {
            Err(why) => {
                error!(severity = "CRIT", "{why}");
                eprintln!("omnifile-edge: CRIT: {why}");
                return ExitCode::from(2);
            }
            Ok(Some(w)) => error!(severity = "CRIT", "{w}"),
            Ok(None) => warn!("smb-guard disabled: SMB and local reads of stubs are NOT guarded"),
        }
        // The unit may be Type=notify (10-smbguard.conf): say ready ourselves.
        let _ = omnifile_smbguard::sdnotify::notify("READY=1", &[]);
        None
    };
    omnifile_edge::fdstore::adopt(detect_fds);

    let vol = match startup_checks(&cfg) {
        Ok(v) => v,
        Err(e) => {
            error!(err = %e, "startup check failed");
            eprintln!("omnifile-edge: {e}");
            return ExitCode::from(2);
        }
    };
    if check_only {
        // The keys can be sealed and opened as configured (M6).
        if let Err(e) = omnifile_control::seal::probe() {
            error!(err = %e, "key protection check failed");
            eprintln!("omnifile-edge: key_protection: {e}");
            return ExitCode::from(2);
        }
    }
    info!(
        data_root = %cfg.data_root.display(),
        fsid = vol.fsid,
        key_protection = omnifile_control::seal::key_protection().as_str(),
        "startup checks passed"
    );
    if check_only {
        return ExitCode::SUCCESS;
    }
    let rt = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("omnifile-edge: runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    rt.block_on(async move {
        match cfg.backend.as_str() {
            "fake" => {
                let svc = Arc::new(FakeService::new());
                let d = Daemon::start(cfg, vol, svc.clone(), svc).await;
                serve(wire(d, guard.as_ref(), &identity)).await
            }
            _ => {
                let control = match omnifile_edge::io::ServiceControl::from_config(&cfg.service) {
                    Ok(c) => Arc::new(c),
                    Err(e) => {
                        error!(err = %e, "control client (not enrolled? run `omnifile-edge enrol`)");
                        return ExitCode::from(2);
                    }
                };
                if cfg.edge_id == 0 {
                    cfg.edge_id = control.edge_id();
                } else if cfg.edge_id != control.edge_id() {
                    error!(config = cfg.edge_id, identity = control.edge_id(), "edge_id differs from the enrolled identity");
                    return ExitCode::from(2);
                }
                let blob = match omnifile_edge::io::ServiceBlob::new(&cfg.s3) {
                    Ok(b) => Arc::new(b),
                    Err(e) => {
                        error!(err = %e, "blob client");
                        return ExitCode::from(2);
                    }
                };
                let d = Daemon::start(cfg, vol, control, blob).await;
                serve(wire(d, guard.as_ref(), &identity)).await
            }
        }
    })
}

/// Hand the daemon's core to the guard (until now it held every event) and
/// give the core the guard's hooks and the AD identity.
#[cfg(target_os = "linux")]
fn wire<C, B>(
    d: Result<omnifile_edge::daemon::Daemon<C, B>, omnifile_edge::daemon::StartError>,
    guard: Option<&omnifile_edge::guard::Started>,
    identity: &omnifile_edge::guard::EdgeIdentity,
) -> Result<omnifile_edge::daemon::Daemon<C, B>, omnifile_edge::daemon::StartError>
where
    C: omnifile_edge::ports::ControlPlane,
    B: omnifile_edge::ports::BlobIo,
{
    use omnifile_edge::guard::{CoreBackend, GuardHooks};
    let d = d?;
    let _ = d.core.identity.set(identity.clone());
    {
        let core = d.core.clone();
        tokio::spawn(async move { core.sweep_smb_offline_once().await });
    }
    if let Some(g) = guard {
        let _ = d.core.guard.set(GuardHooks::new(
            g.handle.marks.clone(),
            g.handle.waker.clone(),
            g.handle.dirs.clone(),
            tokio::runtime::Handle::current(),
        ));
        g.broker
            .bind(std::sync::Arc::new(CoreBackend(d.core.clone())));
        tracing::info!("smb-guard bound to the daemon");
        // Unfilled rewind directories made before this group existed (a new
        // group, after the fd store was lost) carry no mark yet.
        let core = d.core.clone();
        tokio::spawn(async move { core.watch_unfilled_rewind().await });
    }
    Ok(d)
}

#[cfg(target_os = "linux")]
async fn serve<C, B>(
    d: Result<omnifile_edge::daemon::Daemon<C, B>, omnifile_edge::daemon::StartError>,
) -> ExitCode
where
    C: omnifile_edge::ports::ControlPlane,
    B: omnifile_edge::ports::BlobIo,
{
    use tokio::signal::unix::{SignalKind, signal};
    let d = match d {
        Ok(d) => d,
        Err(e) => {
            tracing::error!(err = %e, "start failed");
            return ExitCode::FAILURE;
        }
    };
    let (Ok(mut term), Ok(mut int)) = (
        signal(SignalKind::terminate()),
        signal(SignalKind::interrupt()),
    ) else {
        d.shutdown().await;
        return ExitCode::FAILURE;
    };
    tokio::select! {
        _ = term.recv() => {}
        _ = int.recv() => {}
    }
    d.shutdown().await;
    ExitCode::SUCCESS
}
