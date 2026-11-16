//! The guard on Linux.

pub mod engine;
pub mod marks;
pub mod setup;
pub mod sys;
pub mod truncwatch;

use std::collections::BTreeMap;
use std::os::fd::{AsFd, AsRawFd, FromRawFd, OwnedFd};
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Duration;

use tracing::{error, info, warn};

use crate::api::{EventIdentity, GuardConfig, RecallBroker};
use crate::journal::{self, Journal, Record};
use crate::sdnotify;
use crate::uapi::Access;
use engine::{DENY_EIO, Engine, Pending, Responder, Stats, Wake, Waker};
use marks::{DirMarks, IgnoreMarks};
pub use setup::{GuardError, Origin};

/// What the daemon keeps after [`start`].
pub struct GuardHandle {
    /// Whether the group came from the fd store or was created now.
    pub origin: Origin,
    pub group_id: String,
    /// Remove an inode's ignore mark before narrowing: pass `&*handle.marks`
    /// as the stub handshake's `SmbGuardHook`.
    pub marks: Arc<IgnoreMarks>,
    /// Re-check held events (call on fill progress and recall state changes).
    pub waker: Waker,
    /// Mark unfilled rewind directories, so smbd's opens of them are held
    /// while they fill.
    pub dirs: Arc<DirMarks>,
    pub stats: Arc<Stats>,
}

/// Start the guard. **Call it first thing in `main`**, before any other
/// thread exists and before anything else opens descriptors: it takes the
/// systemd-passed descriptors, may `unshare` the mount namespace, and must
/// reserve the descriptor numbers of its predecessor's unanswered events
/// before anything else can be given those numbers.
///
/// On success the guard is armed and serving, `READY=1` has been sent, and
/// it never stops: its threads run for the life of the process.
pub fn start<B, I>(cfg: GuardConfig, broker: B, identity: I) -> Result<GuardHandle, GuardError>
where
    B: RecallBroker<I::Identity>,
    I: EventIdentity,
{
    let mut passed = sdnotify::consume_listen_fds();
    // The stored notification group (O_TRUNC / PARTIAL-write watcher).
    let stored_notif = passed
        .iter()
        .position(|(_, n)| n.starts_with(truncwatch::FDNAME_PREFIX))
        // SAFETY: systemd passed this descriptor to us; we own it now.
        .map(|i| unsafe { OwnedFd::from_raw_fd(passed.remove(i).0) });

    // The predecessor's unanswered events, FIRST (review N1): their fd
    // numbers are what the kernel matches answers by, so they are reserved
    // before this process opens any descriptor of its own (the volume, the
    // fill mount, the journal...), which could otherwise take one of them.
    // Only the journal is read here, and closed again.
    let stored_id = passed
        .iter()
        .find_map(|(_, name)| name.strip_prefix(setup::FDNAME_PREFIX))
        .map(String::from);
    let replay = match &stored_id {
        Some(id) => journal::replay(&cfg.journal_path, id).unwrap_or_else(|e| {
            error!(err = %e, "cannot read the journal: every predecessor event will be denied by the sweep");
            journal::Replay::default()
        }),
        None => journal::Replay::default(),
    };
    let mut orphans: BTreeMap<i32, (Record, OwnedFd)> = BTreeMap::new();
    let mut deny_now: Vec<i32> = Vec::new();
    for (n, rec) in &replay.pending {
        if *n < 0 {
            continue;
        }
        if sys::fd_is_open(*n) {
            // A systemd-passed descriptor has this number: we can't hold it
            // reserved, so answer the orphan now.
            deny_now.push(*n);
            continue;
        }
        match sys::reserve_fd_number(*n) {
            // SAFETY: we just made `n` a /dev/null descriptor we own.
            Ok(()) => {
                orphans.insert(*n, (rec.clone(), unsafe { OwnedFd::from_raw_fd(*n) }));
            }
            Err(e) => {
                warn!(fd = n, err = %e, "cannot reserve orphan fd number");
                deny_now.push(*n);
            }
        }
    }

    let vol = setup::check_volume(&cfg)?;
    let fill = setup::setup_fill_mount(&cfg, &vol)?;
    let group = setup::acquire_group(&cfg, passed)?;
    // A created group has no predecessor events; a journal read for another
    // id (cannot happen: both come from the same name) is discarded.
    if group.origin == Origin::Created || stored_id.as_deref() != Some(group.id.as_str()) {
        orphans.clear();
        deny_now.clear();
    }
    // Store a new group before it has a mark: a crash between arming and
    // storing would otherwise release a group whose queue may already hold
    // events (released = allowed). An unarmed stored group is harmless; the
    // next start arms it (FAN_MARK_ADD is idempotent).
    if group.origin == Origin::Created {
        setup::store(&group, cfg.require_fdstore)?;
    }
    setup::arm(&group, &vol)?;
    setup::arm_fill_ignore(&group, &fill, &vol)?;

    // Every in-flight event holds its fd (and maybe a pidfd); if the kernel
    // can't open one it denies the event, which is fail-closed but avoidable.
    let nofile = sys::raise_nofile();
    let max_inflight = (nofile.saturating_sub(cfg.fd_reserve) / 2).max(16);
    info!(nofile, max_inflight, "descriptor budget");
    let group_fd = group.fd.as_raw_fd();
    if let Some(dir) = cfg.journal_path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let carry: Vec<Record> = orphans.values().map(|(r, _)| r.clone()).collect();
    let high_water = if group.origin == Origin::Reused {
        replay.max_fd
    } else {
        0
    };
    let jr = Journal::create(
        &cfg.journal_path,
        &group.id,
        &carry,
        high_water,
        cfg.journal_max_bytes,
    )
    .map_err(|e| GuardError::Sys {
        what: "create journal",
        source: e,
    })?;
    let stats = Arc::new(Stats::default());
    let responder = Arc::new(Responder {
        group: group_fd,
        journal: jr,
        stats: stats.clone(),
    });
    for n in deny_now {
        warn!(fd = n, "orphan event denied (fd number busy)");
        let _ = sys::respond(group_fd, n, DENY_EIO);
        let _ = responder.journal.end(&[n]);
    }
    // Deny-sweep: an event the predecessor read but never journalled can't
    // be answered correctly; deny it rather than leave it hanging.
    if group.origin == Origin::Reused {
        let bound = replay.max_fd.max(0).saturating_add(cfg.sweep_extra);
        let mut swept = 0u64;
        for n in 0..=bound {
            if orphans.contains_key(&n) {
                continue;
            }
            if sys::respond(group_fd, n, DENY_EIO).is_ok() {
                swept += 1;
                warn!(fd = n, "sweep: unjournalled event denied with EIO");
            }
        }
        stats.swept.fetch_add(swept, Ordering::Relaxed);
        info!(bound, swept, orphans = orphans.len(), "restart sweep done");
    }

    let fsid = omnifile_stub::sys::fstatfs(vol.data_fd.as_fd())
        .map(|s| omnifile_stub::sys::fsid_of(&s))
        .unwrap_or(0);
    let marks = Arc::new(IgnoreMarks::new(
        group_fd,
        fill.fd.as_raw_fd(),
        cfg.ignore_marks,
    ));
    let eng = Arc::new(Engine::new(
        cfg.clone(),
        group_fd,
        fill.fd.as_raw_fd(),
        fsid,
        responder.clone(),
        broker,
        identity,
        marks.clone(),
        max_inflight,
    ));
    // The watcher before any event is answered: a write let through from
    // here on is seen by it (M3 review).
    if let Err(e) = truncwatch::start(
        eng.clone(),
        vol.data_fd.as_raw_fd(),
        fill.fd.as_raw_fd(),
        stored_notif,
        &group.id,
    ) {
        error!(err = %e, "O_TRUNC watcher not started; relying on the pre-content path");
    }
    eng.start_workers();

    // Orphans: reopen through the fill mount and decide them like new events.
    for (n, (rec, placeholder)) in orphans {
        stats.orphans.fetch_add(1, Ordering::Relaxed);
        let reopened = rec
            .handle
            .as_ref()
            .ok_or_else(|| std::io::Error::other("no handle journalled"))
            .and_then(|h| sys::open_by_handle(fill.fd.as_raw_fd(), h, libc::O_RDONLY));
        let file = match reopened {
            Ok(f) => match sys::fstat(f.as_fd()) {
                Ok(st) if st.st_ino == rec.ino => f,
                _ => {
                    warn!(
                        fd = n,
                        ino = rec.ino,
                        "orphan's handle names another inode: EIO"
                    );
                    let _ = sys::respond(group_fd, n, DENY_EIO);
                    let _ = responder.journal.end(&[n]);
                    continue;
                }
            },
            Err(e) => {
                warn!(fd = n, err = %e, "orphan cannot be reopened: EIO (fail closed)");
                let _ = sys::respond(group_fd, n, DENY_EIO);
                let _ = responder.journal.end(&[n]);
                continue;
            }
        };
        info!(
            fd = n,
            ino = rec.ino,
            off = rec.off,
            len = rec.len,
            pid = rec.pid,
            "ORPHAN event from the previous instance: deciding it"
        );
        let access = Access::from_range(Some((rec.off, rec.len)));
        let p = Pending::new(
            responder.clone(),
            n,
            file,
            Some(placeholder),
            None,
            rec.pid,
            access,
            rec.ino,
            rec.ino_gen,
            rec.handle.clone(),
        );
        eng.dispatch_batch(vec![p]);
    }

    let (wtx, wrx) = std::sync::mpsc::channel::<Wake>();
    {
        let e = eng.clone();
        std::thread::Builder::new()
            .name("smbguard-held".into())
            .spawn(move || e.held_loop(wrx))
            .map_err(|e| GuardError::Sys {
                what: "spawn",
                source: e,
            })?;
    }
    {
        let e = eng.clone();
        std::thread::Builder::new()
            .name("smbguard-read".into())
            .spawn(move || e.read_loop())
            .map_err(|e| GuardError::Sys {
                what: "spawn",
                source: e,
            })?;
    }
    {
        let s = stats.clone();
        let m = marks.clone();
        let _ = std::thread::Builder::new()
            .name("smbguard-stats".into())
            .spawn(move || {
                loop {
                    std::thread::sleep(Duration::from_secs(60));
                    let line = format!(
                        "{} ignore_marks_added={} removed={}",
                        s.line(),
                        m.added.load(Ordering::Relaxed),
                        m.removed.load(Ordering::Relaxed)
                    );
                    info!("stats: {line}");
                    let _ = sdnotify::notify(&format!("STATUS=guard armed; {line}"), &[]);
                }
            });
    }

    let status = format!(
        "READY=1\nSTATUS=guard armed on {} (group {}, {:?})",
        cfg.data_root.display(),
        group.id,
        group.origin
    );
    let status = if cfg.notify_ready {
        status
    } else {
        status.replacen("READY=1\n", "", 1)
    };
    match sdnotify::notify(&status, &[]) {
        Ok(true) => info!("READY=1 sent"),
        Ok(false) => info!("not under systemd: no READY notification"),
        Err(e) => error!(err = %e, "READY notification failed"),
    }
    info!(data_root = %cfg.data_root.display(), group_id = %group.id, origin = ?group.origin,
        "guard armed and serving events");

    if group.origin == Origin::Created {
        let servers = sys::servers_with_open_files(&cfg.server_comms, vol.dev);
        if !servers.is_empty() {
            error!(
                ?servers,
                "a NEW fanotify group was created while file servers hold files \
                on the volume open: those files are unguarded for life (A9); restarting them"
            );
            if let Some(cmd) = &cfg.restart_servers
                && let Some((prog, args)) = cmd.split_first()
            {
                let _ = std::process::Command::new(prog).args(args).spawn();
            }
        }
    }

    // The group and the fill mount live as long as the process.
    std::mem::forget(group.fd);
    std::mem::forget(fill.fd);
    std::mem::forget(vol.data_fd);
    Ok(GuardHandle {
        origin: group.origin,
        group_id: group.id,
        marks,
        waker: Waker(wtx),
        dirs: Arc::new(DirMarks::new(group_fd)),
        stats,
    })
}
