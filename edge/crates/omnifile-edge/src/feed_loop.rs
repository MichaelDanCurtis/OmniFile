//! The feed loop (`edge-metadata.md` §5.1): pull a page, apply it in one
//! replica transaction with its cursor, materialise, react.

use std::sync::Arc;
use std::time::Duration;

use omnifile_replica::Outcome;
use omnifile_stub::materialize::{Materializer, RedoStats};
use tracing::{debug, error, info, warn};

use crate::core::Core;
use crate::ports::{BlobIo, ControlPlane};

/// Redo every pending filesystem op (startup, and after every page).
///
/// The replica lock is held only to read a batch of ops and to record each
/// outcome, never across the filesystem work: a content-version change
/// waits up to the invalidate timeout for FSAL acks, and holding the lock
/// meanwhile would stall the FSAL handler, the stubber and commits (S7).
pub async fn redo<C: ControlPlane, B: BlobIo>(core: &Arc<Core<C, B>>) {
    // New objects get their ACL before their name exists (S4).
    let perms = crate::acl::CreatePerms::new(core.identity.get().map(|i| i.idmap.clone()));
    let m = Materializer {
        layout: &core.layout,
        notifier: core.as_ref(),
        seq: &core.seq,
        locks: &core.locks,
        perms: &perms,
    };
    let mut st = RedoStats::default();
    for _ in 0..64 {
        let ops = {
            let rep = core.replica.lock().await;
            match rep.pending(512) {
                Ok(ops) => {
                    perms.load(&rep, &ops);
                    ops
                }
                Err(e) => {
                    error!(err = %e, "replica error during redo");
                    return;
                }
            }
        };
        if ops.is_empty() {
            break;
        }
        st.passes += 1;
        let mut progress = false;
        for op in &ops {
            let out = m.apply(op).await;
            match &out {
                Outcome::Done { .. } | Outcome::Removed => {
                    st.done += 1;
                    progress = true;
                }
                Outcome::Retry(_) => st.retried += 1,
                Outcome::Conflict(_) => {
                    st.conflicts += 1;
                    progress = true;
                }
            }
            if let Err(e) = core.replica.lock().await.record(op, out) {
                error!(err = %e, "replica error during redo");
                return;
            }
        }
        if !progress {
            break;
        }
    }
    if st.done + st.conflicts + st.retried > 0 {
        debug!(?st, "materialised");
    }
}

/// Snapshot rows per page.
const SNAPSHOT_PAGE: u32 = 5000;

/// Re-snapshot a live edge after `reset_required` (`edge-metadata.md` §5.2):
/// a cursor beyond retention, or a restored catalog (`control-api.md` §5).
///
/// Every dataset the edge serves is read again from the service and applied
/// over the replica whatever its `entity_version`, so inodes stay and only
/// differences reach the tree. Then:
/// - entries the snapshot lacks (numbered after a restore point) leave the
///   replica, never the disk: resident files among them are committed again
///   as new files, and stubs stay placeholders that answer EIO (their blobs
///   show in the reconcile report);
/// - a file whose local version is newer than the catalog's (committed
///   after the restore point) keeps its local bytes and is committed again;
///   if it is a stub, the catalog's version is applied;
/// - the feed resumes from the first page's `as_of`.
pub async fn resnapshot<C: ControlPlane, B: BlobIo>(core: &Arc<Core<C, B>>) -> Result<(), String> {
    let datasets: Vec<u64> = {
        let rep = core.replica.lock().await;
        let mut ids: Vec<u64> = rep
            .shares()
            .map_err(|e| e.to_string())?
            .iter()
            .map(|s| s.dataset_id)
            .collect();
        ids.sort_unstable();
        ids.dedup();
        ids
    };
    core.replica
        .lock()
        .await
        .resnapshot_begin()
        .map_err(|e| e.to_string())?;
    let mut as_of: Option<String> = None;
    let mut lost = Vec::new();
    for ds in datasets {
        // A bulk export first; paging when the service has none for this
        // dataset, or it fails. Rows a failed export applied are applied
        // again by the pages, whatever their entity_version.
        match bulk_snapshot(core, ds).await {
            Ok(Some(ds_as_of)) => {
                as_of.get_or_insert(ds_as_of);
                lost.extend(
                    core.replica
                        .lock()
                        .await
                        .resnapshot_lost(ds)
                        .map_err(|e| e.to_string())?,
                );
                continue;
            }
            Ok(None) => {}
            Err(err) => warn!(dataset = ds, %err, "bulk export not used; paging"),
        }
        let mut after: Option<u64> = None;
        loop {
            let page = core
                .control
                .snapshot(ds, after, SNAPSHOT_PAGE)
                .await
                .map_err(|e| e.message)?;
            let mut rep = core.replica.lock().await;
            if after.is_none() {
                // The earliest `as_of`: replaying from it is harmless.
                if as_of.is_none() {
                    as_of = page.as_of.clone();
                }
                rep.resnapshot_meta(page.dataset.as_ref(), &page.shares, &page.acls)
                    .map_err(|e| e.to_string())?;
            }
            rep.resnapshot_files(&page.files)
                .map_err(|e| e.to_string())?;
            let last = page
                .files
                .last()
                .and_then(|f| f["file_id"].as_str())
                .and_then(|s| s.parse::<u64>().ok());
            if !page.more || last.is_none() {
                break;
            }
            after = last;
        }
        lost.extend(
            core.replica
                .lock()
                .await
                .resnapshot_lost(ds)
                .map_err(|e| e.to_string())?,
        );
    }
    let Some(as_of) = as_of else {
        return Err("the snapshot named no cursor".into());
    };
    let mut recommit: Vec<std::path::PathBuf> = Vec::new();
    let newer = core
        .replica
        .lock()
        .await
        .resnapshot_newer()
        .map_err(|e| e.to_string())?;
    for (node, path, row) in newer {
        let resident = path.as_deref().is_some_and(|rel| fully_resident(core, rel));
        let mut rep = core.replica.lock().await;
        match path {
            Some(rel) if resident => {
                let _ = rep.set_local_dirty(node.lid, true);
                let _ = rep.clear_fs_ctime(node.lid);
                recommit.push(rel);
            }
            _ => {
                rep.resnapshot_apply_held(&row).map_err(|e| e.to_string())?;
            }
        }
    }
    core.replica
        .lock()
        .await
        .resnapshot_finish(&as_of)
        .map_err(|e| e.to_string())?;
    redo(core).await;
    let n_lost = lost.len();
    for (node, path) in lost {
        if let Some(rel) = path
            && node.kind == omnifile_replica::Kind::File
            && fully_resident(core, &rel)
        {
            recommit.push(rel);
        }
    }
    info!(
        as_of,
        lost = n_lost,
        recommit = recommit.len(),
        "re-snapshot done"
    );
    for rel in recommit {
        core.commit_modified(&rel).await;
    }
    Ok(())
}

/// Bytes per ranged read of an export part: each comes with Azure's CRC-64.
const EXPORT_RANGE: u64 = omnifile_stub::MAX_VERIFIED_RANGE;
/// Rows applied to the replica at a time while a part is loaded.
const EXPORT_BATCH: usize = 5000;

/// One step of loading a part: metadata (first part only), or file rows.
enum ExportBatch {
    Meta {
        dataset: Option<serde_json::Value>,
        shares: Vec<serde_json::Value>,
        acls: Vec<serde_json::Value>,
    },
    Files(Vec<serde_json::Value>),
}

/// Load one dataset from a bulk export (`control-api.md` §6). `Ok(None)`:
/// the service has none for it (an S3 dataset: `layout_conflict`; an older
/// service: 404), or exports are off (`export_wait_s = 0`); page instead.
/// Returns the export's `as_of`. A part is used only after its size and
/// SHA-256 match, and only if it holds the rows it says it does.
async fn bulk_snapshot<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    ds: u64,
) -> Result<Option<String>, String> {
    let wait = core.cfg.feed.export_wait_s;
    if wait == 0 {
        return Ok(None);
    }
    let nonce = format!(
        "{:x}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    );
    let started = match core.control.snapshot_export_start(ds, &nonce).await {
        Ok(e) => e,
        Err(e) if !e.unavailable => {
            info!(dataset = ds, code = ?e.code, "no bulk export; paging");
            return Ok(None);
        }
        Err(e) => return Err(e.message),
    };
    let deadline = std::time::Instant::now() + Duration::from_secs(wait);
    let mut delay = Duration::from_millis(200);
    let ex = loop {
        let e = core
            .control
            .snapshot_export(started.export_id)
            .await
            .map_err(|e| e.message)?;
        match e.state.as_str() {
            "ready" => break e,
            "running" => {}
            other => return Err(format!("export {} is {other}", started.export_id)),
        }
        if std::time::Instant::now() >= deadline {
            return Err(format!(
                "export {} not ready in {wait} s",
                started.export_id
            ));
        }
        tokio::time::sleep(delay).await;
        delay = (delay * 2).min(Duration::from_secs(30));
    };
    let as_of = ex.as_of.clone().ok_or("the export named no cursor")?;
    for (n, part) in ex.parts.iter().enumerate() {
        let blob = fetch_part(core, part)
            .await
            .map_err(|e| format!("part {n}: {e}"))?;
        load_part(core, blob, part.rows, n == 0)
            .await
            .map_err(|e| format!("part {n}: {e}"))?;
    }
    info!(
        dataset = ds,
        export = ex.export_id,
        parts = ex.parts.len(),
        as_of,
        "bulk export loaded"
    );
    Ok(Some(as_of))
}

/// A whole part, in CRC-checked ranges, checked against its size and SHA-256.
async fn fetch_part<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    part: &crate::ports::ExportPart,
) -> Result<Vec<u8>, String> {
    let mut blob = Vec::with_capacity(part.bytes as usize);
    let mut off = 0;
    while off < part.bytes {
        let len = EXPORT_RANGE.min(part.bytes - off);
        let chunk = core
            .blob
            .get_range(&part.url, omnifile_stub::SourceKind::AzureSas, off, len)
            .await
            .map_err(|e| e.to_string())?;
        blob.extend_from_slice(&chunk);
        off += len;
    }
    let sha = ring::digest::digest(&ring::digest::SHA256, &blob);
    let hex: String = sha.as_ref().iter().map(|b| format!("{b:02x}")).collect();
    if !hex.eq_ignore_ascii_case(&part.sha256) {
        return Err(format!("SHA-256 {hex}, the export says {}", part.sha256));
    }
    Ok(blob)
}

/// Decode a verified part on a blocking thread and apply it in batches.
async fn load_part<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    blob: Vec<u8>,
    rows: u64,
    first: bool,
) -> Result<(), String> {
    let (tx, mut rx) = tokio::sync::mpsc::channel::<Result<ExportBatch, String>>(2);
    let decoder = tokio::task::spawn_blocking(move || {
        let checked = decode_part(&blob, first, &tx).and_then(|n| {
            if n == rows {
                Ok(())
            } else {
                Err(format!("{n} lines, the export says {rows}"))
            }
        });
        if let Err(e) = checked {
            let _ = tx.blocking_send(Err(e));
        }
    });
    while let Some(batch) = rx.recv().await {
        let mut rep = core.replica.lock().await;
        match batch? {
            ExportBatch::Meta {
                dataset,
                shares,
                acls,
            } => rep.resnapshot_meta(dataset.as_ref(), &shares, &acls),
            ExportBatch::Files(files) => rep.resnapshot_files(&files),
        }
        .map_err(|e| e.to_string())?;
    }
    decoder.await.map_err(|e| e.to_string())
}

/// The part's lines, sent as batches: the metadata first (first part only),
/// then file rows. Returns the number of lines.
fn decode_part(
    blob: &[u8],
    first: bool,
    tx: &tokio::sync::mpsc::Sender<Result<ExportBatch, String>>,
) -> Result<u64, String> {
    use std::io::BufRead;
    let dec = ruzstd::decoding::StreamingDecoder::new(blob).map_err(|e| e.to_string())?;
    let (mut dataset, mut shares, mut acls) = (None, Vec::new(), Vec::new());
    let mut meta_sent = !first;
    let mut files = Vec::with_capacity(EXPORT_BATCH);
    let mut n = 0u64;
    let send = |b: ExportBatch| {
        tx.blocking_send(Ok(b))
            .map_err(|_| "the loader stopped".to_string())
    };
    for line in std::io::BufReader::new(dec).lines() {
        let line = line.map_err(|e| e.to_string())?;
        n += 1;
        let v: serde_json::Value =
            serde_json::from_str(&line).map_err(|e| format!("line {n}: {e}"))?;
        let Some((kind, row)) = v
            .as_object()
            .filter(|o| o.len() == 1)
            .and_then(|o| o.iter().next())
        else {
            return Err(format!("line {n}: not a one-key object"));
        };
        match kind.as_str() {
            "dataset" if !meta_sent => dataset = Some(row.clone()),
            "share" if !meta_sent => shares.push(row.clone()),
            "acl" if !meta_sent => acls.push(row.clone()),
            "file" => {
                if !meta_sent {
                    send(ExportBatch::Meta {
                        dataset: dataset.take(),
                        shares: std::mem::take(&mut shares),
                        acls: std::mem::take(&mut acls),
                    })?;
                    meta_sent = true;
                }
                files.push(row.clone());
                if files.len() == EXPORT_BATCH {
                    send(ExportBatch::Files(std::mem::take(&mut files)))?;
                }
            }
            other => return Err(format!("line {n}: unexpected {other:?}")),
        }
    }
    if !meta_sent {
        send(ExportBatch::Meta {
            dataset,
            shares,
            acls,
        })?;
    }
    if !files.is_empty() {
        send(ExportBatch::Files(files))?;
    }
    Ok(n)
}

/// Whether every byte of the file at `rel` is on the edge (no marker, or a
/// resident one).
fn fully_resident<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
    rel: &std::path::Path,
) -> bool {
    use std::os::fd::AsFd;
    let Ok(fd) = omnifile_stub::sys::open_ro_beneath(&core.layout.data_root, rel) else {
        return false;
    };
    match omnifile_marker::xattr::read(fd.as_fd()) {
        Ok(omnifile_marker::xattr::ReadOutcome::Marker(m)) => m.is_fully_resident(),
        Ok(omnifile_marker::xattr::ReadOutcome::Unmanaged) => true,
        // A bad marker fails closed: its bytes are never trusted.
        Ok(omnifile_marker::xattr::ReadOutcome::Bad(_)) | Err(_) => false,
    }
}

/// Pull and apply one page. Returns false when the service was unreachable.
pub async fn feed_once<C: ControlPlane, B: BlobIo>(core: &Arc<Core<C, B>>, wait_s: u32) -> bool {
    use std::sync::atomic::Ordering::SeqCst;
    if core.resnapshot_wanted.swap(false, SeqCst) {
        warn!("the replica differs from the catalog's digest: re-snapshot");
        if let Err(e) = resnapshot(core).await {
            error!(err = %e, "re-snapshot failed; tried again before the next page");
            core.resnapshot_wanted.store(true, SeqCst);
            return false;
        }
    }
    let cursor = match core.replica.lock().await.cursor() {
        Ok(c) => c,
        Err(e) => {
            error!(err = %e, "replica: cursor");
            return false;
        }
    };
    let page = match core
        .control
        .feed(&cursor, core.cfg.feed.limit, wait_s)
        .await
    {
        Ok(p) => p,
        Err(e) => {
            warn!(err = %e, unavailable = e.unavailable, "feed");
            return false;
        }
    };
    if page.reset_required {
        warn!(
            cursor,
            "feed cursor refused (beyond retention, or the catalog was restored): re-snapshot"
        );
        return match resnapshot(core).await {
            Ok(()) => true,
            Err(e) => {
                error!(err = %e, "re-snapshot failed; the feed waits");
                false
            }
        };
    }
    let report = match core
        .replica
        .lock()
        .await
        .apply_page(&page.events, &page.next)
    {
        Ok(r) => r,
        Err(e) => {
            error!(err = %e, "replica: apply_page");
            return false;
        }
    };
    if !page.events.is_empty() {
        debug!(
            events = page.events.len(),
            ?report,
            next = page.next,
            "feed page applied"
        );
    }
    redo(core).await;
    // Placeholders get the catalog's (or their directory's) ACL (M2).
    crate::acl::sync(core).await;
    // Rewind label directories (decision 30), at most once a minute, and
    // at once when an ACL changed.
    if report.acls_changed {
        core.rewind_acls_stale
            .store(true, std::sync::atomic::Ordering::SeqCst);
    }
    core.refresh_rewind_labels().await;
    if !report.recalls.is_empty() {
        core.on_recall_rows(report.recalls).await;
    }
    if report.stub_wanted > 0 {
        core.wake_stubber.notify_one();
    }
    if page.next != cursor {
        let _ = core.control.feed_ack(&page.next).await;
    }
    true
}

/// A feed cursor as `(incarnation, txid, event)`: ordered within one
/// incarnation (`control-api.md` §5).
fn cursor_key(c: &str) -> Option<(u64, u64, u64)> {
    let mut it = c.split('.');
    let txid = it.next()?.parse().ok()?;
    let event = it.next()?.parse().ok()?;
    let inc = it.next().map_or(Some(0), |s| s.parse().ok())?;
    it.next().is_none().then_some((inc, txid, event))
}

/// Whether the edge has applied the feed at least up to `as_of`.
fn cursor_reached(cursor: &str, as_of: &str) -> bool {
    match (cursor_key(cursor), cursor_key(as_of)) {
        (Some((i, t, e)), Some((ai, at, ae))) => i == ai && (t, e) >= (at, ae),
        _ => false,
    }
}

/// Check every dataset against the service's reconciliation digest
/// (`control-api.md` §6), and ask for a re-snapshot when one differs.
/// Writes racing the check change the service's digest, so a difference
/// counts only when three service digests, a quiet interval apart, agree and
/// the edge has applied the feed past them: then nothing was written while
/// the edge caught up, and a difference is divergence. A busy dataset is
/// skipped until the next round. Returns the datasets that differ.
pub async fn reconcile_once<C: ControlPlane, B: BlobIo>(
    core: &Arc<Core<C, B>>,
) -> Result<Vec<u64>, String> {
    let quiet = Duration::from_secs(core.cfg.feed.reconcile_quiet_s);
    let datasets: Vec<u64> = {
        let rep = core.replica.lock().await;
        let mut ids: Vec<u64> = rep
            .shares()
            .map_err(|e| e.to_string())?
            .iter()
            .map(|s| s.dataset_id)
            .collect();
        ids.sort_unstable();
        ids.dedup();
        ids
    };
    let mut differ = Vec::new();
    for ds in datasets {
        let digest = || async { core.control.digest(ds).await.map_err(|e| e.message) };
        let a = digest().await?;
        tokio::time::sleep(quiet).await;
        let b = digest().await?;
        if (a.live_rows, a.digest) != (b.live_rows, b.digest) {
            info!(
                dataset = ds,
                "reconcile: the dataset is changing; next round"
            );
            continue;
        }
        // The feed loop catches up on its own; wait for it, a while.
        let deadline = std::time::Instant::now() + quiet.max(Duration::from_secs(5)) * 2;
        let mut reached = false;
        while std::time::Instant::now() < deadline {
            let cur = core
                .replica
                .lock()
                .await
                .cursor()
                .map_err(|e| e.to_string())?;
            if cursor_reached(&cur, &b.as_of) {
                reached = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
        if !reached {
            info!(
                dataset = ds,
                as_of = b.as_of,
                "reconcile: the feed is behind; next round"
            );
            continue;
        }
        let (rows, x) = core
            .replica
            .lock()
            .await
            .digest(ds)
            .map_err(|e| e.to_string())?;
        if (rows, x) == (b.live_rows, b.digest) {
            debug!(
                dataset = ds,
                rows, "reconcile: the replica matches the catalog"
            );
            continue;
        }
        let c = digest().await?;
        if (c.live_rows, c.digest) != (b.live_rows, b.digest) {
            info!(
                dataset = ds,
                "reconcile: the dataset changed during the check; next round"
            );
            continue;
        }
        warn!(
            dataset = ds,
            catalog_rows = b.live_rows,
            replica_rows = rows,
            "reconcile: the replica differs from the catalog"
        );
        differ.push(ds);
    }
    if !differ.is_empty() {
        core.resnapshot_wanted
            .store(true, std::sync::atomic::Ordering::SeqCst);
    }
    Ok(differ)
}

/// [`reconcile_once`] every `[feed] reconcile_s` (0: never).
pub async fn reconcile_loop<C: ControlPlane, B: BlobIo>(core: Arc<Core<C, B>>) {
    let every = core.cfg.feed.reconcile_s;
    if every == 0 {
        return;
    }
    let mut stop = core.shutdown.subscribe();
    loop {
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(every)) => {}
            _ = stop.changed() => return,
        }
        let r = tokio::select! {
            r = reconcile_once(&core) => r,
            _ = stop.changed() => return,
        };
        if let Err(err) = r {
            warn!(%err, "reconcile: not finished; next round");
        }
    }
}

pub async fn feed_loop<C: ControlPlane, B: BlobIo>(core: Arc<Core<C, B>>) {
    let mut stop = core.shutdown.subscribe();
    redo(&core).await;
    loop {
        if core.is_shutting_down() {
            return;
        }
        let ok = tokio::select! {
            ok = feed_once(&core, core.cfg.feed.wait_s) => ok,
            _ = stop.changed() => return,
        };
        if !ok {
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_secs(core.cfg.feed.retry_s.max(1))) => {}
                _ = stop.changed() => return,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn part(lines: &[&str]) -> Vec<u8> {
        let raw: String = lines.iter().map(|l| format!("{l}\n")).collect();
        ruzstd::encoding::compress_to_vec(
            raw.as_bytes(),
            ruzstd::encoding::CompressionLevel::Fastest,
        )
    }

    fn decode(blob: &[u8], first: bool) -> (Result<u64, String>, Vec<ExportBatch>) {
        let (tx, mut rx) = tokio::sync::mpsc::channel(1 << 16);
        let r = decode_part(blob, first, &tx);
        drop(tx);
        let mut out = Vec::new();
        while let Ok(b) = rx.try_recv() {
            out.push(b.unwrap());
        }
        (r, out)
    }

    #[test]
    fn a_first_part_gives_its_metadata_then_its_files_in_batches() {
        let mut lines = vec![
            r#"{"dataset":{"dataset_id":"1"}}"#.to_string(),
            r#"{"share":{"share_id":"2"}}"#.to_string(),
            r#"{"acl":{"acl_id":"3"}}"#.to_string(),
        ];
        let n = EXPORT_BATCH + 7;
        lines.extend((0..n).map(|i| format!(r#"{{"file":{{"file_id":"{}"}}}}"#, 10 + i)));
        let refs: Vec<&str> = lines.iter().map(String::as_str).collect();
        let (r, batches) = decode(&part(&refs), true);
        assert_eq!(r.unwrap(), lines.len() as u64);
        let ExportBatch::Meta {
            dataset,
            shares,
            acls,
        } = &batches[0]
        else {
            panic!("metadata first")
        };
        assert_eq!(dataset.as_ref().unwrap()["dataset_id"], "1");
        assert_eq!((shares.len(), acls.len()), (1, 1));
        let sizes: Vec<usize> = batches[1..]
            .iter()
            .map(|b| match b {
                ExportBatch::Files(f) => f.len(),
                ExportBatch::Meta { .. } => panic!("metadata once"),
            })
            .collect();
        assert_eq!(sizes, vec![EXPORT_BATCH, 7]);
    }

    #[test]
    fn cursors_are_ordered_within_an_incarnation() {
        assert!(cursor_reached("12.3", "12.3"));
        assert!(cursor_reached("12.4", "12.3"));
        assert!(cursor_reached("13.0", "12.9"));
        assert!(!cursor_reached("12.2", "12.3"));
        assert!(!cursor_reached("9.0", "10.0"));
        // Another incarnation is never "caught up".
        assert!(!cursor_reached("99.0.1", "1.0"));
        assert!(!cursor_reached("99.0", "1.0.1"));
        assert!(cursor_reached("5.0.1", "4.0.1"));
        assert!(!cursor_reached("bad", "1.0") && !cursor_reached("1.0.0.0", "1.0"));
    }

    #[test]
    fn a_later_part_holds_files_only() {
        let (r, batches) = decode(&part(&[r#"{"file":{"file_id":"9"}}"#]), false);
        assert_eq!(r.unwrap(), 1);
        assert!(matches!(&batches[..], [ExportBatch::Files(f)] if f.len() == 1));
        // Metadata in a later part is not the snapshot's shape.
        let (r, _) = decode(&part(&[r#"{"share":{"share_id":"2"}}"#]), false);
        assert!(r.unwrap_err().contains("unexpected \"share\""));
    }

    #[test]
    fn a_line_that_is_not_one_row_fails_the_part() {
        for bad in [r#"{"file":{},"acl":{}}"#, r#"["file"]"#, "not json"] {
            let (r, _) = decode(&part(&[bad]), false);
            assert!(r.is_err(), "{bad}");
        }
        let (r, _) = decode(b"not zstd", false);
        assert!(r.is_err());
    }
}
