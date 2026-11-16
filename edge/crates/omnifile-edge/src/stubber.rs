//! The stubber: carries out the service's desired stub state (control-api
//! §4.3): `stub/prepare` for a lease, the R1 §5.1 handshake, then
//! `stub/commit`, or `stub/skip` with the reason.

use std::collections::HashMap;
use std::os::fd::AsFd;
use std::sync::Arc;
use std::time::Duration;

use omnifile_replica::stub_status;
use omnifile_stub::handshake::{StubRequest, ctime_ns, stub};
use omnifile_stub::{StubOutcome, sys};
use tracing::{info, warn};

use crate::core::Core;
use crate::ports::{BlobIo, ControlPlane, StubPrepare};

/// One pass over the wanted stubs. Returns how many were stubbed.
pub async fn stub_pass<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    deferrals: &mut HashMap<u64, u32>,
) -> usize {
    // No FSAL connected yet (Ganesha starting, restarting or down): nothing
    // can acknowledge a narrowing, so don't start one (B2). An edge that
    // runs no Ganesha says so in `fsal.no_ganesha`.
    if !core.fsal_ready() {
        return 0;
    }
    let wants = match core.replica.lock().await.stub_wanted(core.cfg.stub.batch) {
        Ok(w) => w,
        Err(e) => {
            warn!(err = %e, "replica: stub_wanted");
            return 0;
        }
    };
    let mut stubbed = 0;
    for w in wants {
        if core.is_shutting_down() {
            break;
        }
        let Some(file_id) = w.node.file_id else {
            continue;
        };
        let lid = w.node.lid;
        // Never stub a file that is being filled or recalled.
        if core
            .recalls
            .lock()
            .unwrap()
            .get(&(file_id, w.version_id, 0))
            .is_some_and(|e| !e.phase.is_terminal())
        {
            continue;
        }
        // `stub` takes the lock itself; skip early so no lease is wasted.
        if core.locks.is_locked(file_id) {
            continue;
        }
        let lease = match core.control.stub_prepare(file_id, w.version_id).await {
            Ok(StubPrepare::Lease {
                lease_id,
                resident_bytes,
            }) => (lease_id, resident_bytes),
            Ok(StubPrepare::Refused { code }) => {
                info!(file_id, code, "stub refused by the service");
                let _ = core
                    .replica
                    .lock()
                    .await
                    .set_stub_status(lid, stub_status::SKIPPED);
                continue;
            }
            Err(e) if e.unavailable => break,
            Err(e) => {
                warn!(file_id, err = %e, "stub/prepare failed");
                continue;
            }
        };
        // The lease's resident size is authoritative; never keep less than
        // the feed asked for.
        let resident = lease.1.max(w.resident_bytes);
        let req = StubRequest {
            rel: w.path.clone(),
            file_id,
            version_id: w.version_id,
            resident_bytes: resident,
            expect_ino: w.node.ino.zip(w.node.ino_gen),
            expect_ctime_ns: w.node.fs_ctime_ns,
        };
        match stub(
            &core.layout,
            &req,
            core.as_ref(),
            &core.stub_guard(),
            &core.seq,
            &core.locks,
        )
        .await
        {
            StubOutcome::Stubbed { resident_bytes } => {
                // Record the ctime our narrowing and punch left, before the
                // commit call: if the commit fails, the retry must see the
                // file as unchanged (and redo the idempotent handshake), not
                // as written since and report `stub_skip` for a file whose
                // bytes are already punched (S5).
                if let Ok(fd) = sys::open_ro_beneath(&core.layout.data_root, &w.path)
                    && let Ok(st) = sys::fstat(fd.as_fd())
                {
                    let _ = core.replica.lock().await.set_fs_ctime(lid, ctime_ns(&st));
                }
                match core
                    .control
                    // The size the lease was minted for: the handshake may
                    // keep a little more (whole pages), which is harmless.
                    .stub_commit(
                        file_id,
                        &lease.0,
                        w.version_id,
                        resident.min(resident_bytes),
                    )
                    .await
                {
                    Ok(()) => {
                        let _ = core
                            .replica
                            .lock()
                            .await
                            .set_stub_status(lid, stub_status::DONE);
                        deferrals.remove(&file_id);
                        stubbed += 1;
                    }
                    // The punch is done and safe; the commit is retried on the
                    // next pass (the handshake is idempotent).
                    Err(e) => warn!(file_id, err = %e, "stub/commit failed; will retry"),
                }
            }
            StubOutcome::Skipped { reason, detail } => {
                info!(file_id, reason = reason.as_str(), detail, "stub skipped");
                let _ = core
                    .control
                    .stub_skip(file_id, w.version_id, reason.as_str(), Some(&detail))
                    .await;
                let _ = core
                    .replica
                    .lock()
                    .await
                    .set_stub_status(lid, stub_status::SKIPPED);
            }
            StubOutcome::Busy => continue,
            StubOutcome::Deferred { detail } => {
                // The narrowing was our own write: keep tracking the file.
                if let Ok(fd) = sys::open_ro_beneath(&core.layout.data_root, &w.path)
                    && let Ok(st) = sys::fstat(fd.as_fd())
                {
                    let _ = core.replica.lock().await.set_fs_ctime(lid, ctime_ns(&st));
                }
                let n = deferrals.entry(file_id).or_insert(0);
                *n += 1;
                if *n >= core.cfg.stub.max_deferrals {
                    warn!(file_id, detail, "stub deferred too often; skipping");
                    let _ = core
                        .control
                        .stub_skip(
                            file_id,
                            w.version_id,
                            "invalidate_not_acknowledged",
                            Some(&detail),
                        )
                        .await;
                    let _ = core
                        .replica
                        .lock()
                        .await
                        .set_stub_status(lid, stub_status::SKIPPED);
                    deferrals.remove(&file_id);
                }
            }
        }
    }
    stubbed
}

pub async fn stub_loop<C: ControlPlane, B: BlobIo>(core: Arc<Core<C, B>>) {
    let mut stop = core.shutdown.subscribe();
    let mut deferrals = HashMap::new();
    let every = Duration::from_secs(core.cfg.stub.interval_s.max(1));
    loop {
        stub_pass(&core, &mut deferrals).await;
        tokio::select! {
            _ = tokio::time::sleep(every) => {}
            _ = core.wake_stubber.notified() => {}
            _ = stop.changed() => return,
        }
    }
}
