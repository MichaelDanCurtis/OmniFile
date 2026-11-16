//! In-memory service and blob store, for tests and for `backend = "fake"`.
//!
//! It is a minimal catalog: it numbers files and versions, keeps uploaded
//! bytes, serves a feed the test pushes events into, grants stub leases and
//! answers recalls as `online` (or as an archive recall, when told to).

use std::collections::HashMap;
use std::io;
use std::sync::Mutex;
use std::time::Duration;

use serde_json::{Value, json};
use tokio::sync::Notify;

use crate::ports::*;

#[derive(Default)]
struct State {
    next_id: u64,
    events: Vec<Value>,
    blobs: HashMap<String, Vec<u8>>,
    uploads: HashMap<u64, (u64, u64)>,
    calls: Vec<String>,
    unavailable: bool,
    archived: bool,
    refuse_stub: bool,
    /// Delay before each range read (tests that act mid-fill).
    range_delay_ms: u64,
    /// Delay before each upload (a slow commit pipeline).
    upload_delay_ms: u64,
    /// Fail this many `stub_commit` calls (a lost response).
    fail_stub_commits: u32,
    /// The kind of every range read, in order.
    range_sources: Vec<SourceKind>,
    /// `asof` answers: `(dir_id, t)` -> entries, sorted by name.
    asof: HashMap<(u64, String), Vec<Value>>,
    /// Read URLs that are refused (403) from this offset on: expired.
    expire_at: HashMap<String, u64>,
    /// Snapshots by dataset, and whether the feed answers `reset_required`.
    snapshots: HashMap<u64, FakeSnapshot>,
    reset_required: bool,
    /// Bulk exports: lines per part, or None to answer `layout_conflict`.
    export_rows: Option<usize>,
    /// Store this part with one byte flipped (its SHA-256 no longer matches).
    export_corrupt: Option<usize>,
    /// Exports by id, with how many times each was read (the first read
    /// answers `running`).
    exports: HashMap<u64, (SnapshotExport, u32)>,
    /// Audit records received, in order.
    audits: Vec<Value>,
    /// Heartbeats received, in order.
    heartbeats: Vec<Health>,
    /// `certificate_renew_after` to answer heartbeats with.
    renew_after: Option<String>,
    /// `renew_if_due` calls, with the `renew_after` each was given.
    renewals: Vec<Option<String>>,
    /// `renew_roles_anywhere_if_due` calls, with the directory each was given.
    ra_renewals: Vec<std::path::PathBuf>,
    /// What `renew_roles_anywhere_if_due` answers (a new certificate installed).
    ra_installs: bool,
    /// A busy dataset: every digest answer differs from the last.
    digest_drift: bool,
    digest_calls: u64,
    /// Renames to these names are answered `conflict` (`name_taken`).
    ns_conflict_names: Vec<String>,
    /// Files `outbox_holds` says the upload outbox holds (WP9 review).
    outbox_holding: std::collections::HashSet<u64>,
    /// Archived recalls by id: their state, as the catalog would hold it.
    /// A read of a version with an active recall joins it (start-or-join).
    recall_states: HashMap<u64, (u64, u64, String)>,
    /// A scripted upload outbox: finished ops `outbox_resume` hands back
    /// until they are forgotten (None: no outbox, uploads are direct).
    outbox_finished: Option<Vec<(OutboxUpload, OutboxOutcome)>>,
    /// Each namespace op and create sent (as in `calls`), with its actor.
    actors: Vec<(String, Option<crate::ports::Actor>)>,
    /// Creates of these names are refused (`conflict`).
    create_conflict_names: Vec<String>,
    /// The next create is made, but its answer is lost (an error).
    lose_next_create: bool,
    /// Creates made, by op id: a create sent again gets the same answer.
    created_by_op: HashMap<String, u64>,
    /// M5: objects whose byte `i` is `synthetic_byte(seed, i)`, by URL
    /// (huge remote objects that are never held in memory): size and seed.
    synthetic: HashMap<String, (u64, u64)>,
    /// M5: `read_url` answers the S3 pointer the edge signs itself
    /// (`s3_roles_anywhere`) instead of an Azure SAS.
    read_s3: bool,
    /// Review on #23: `read_url` answers a presigned S3 URL (the operator's
    /// fallback), with this signed `If-Match` (`Some(None)`: unpinned).
    read_presigned: Option<Option<String>>,
    /// M5: S3 reads of these files answer `412` (If-Match refused: the
    /// source changed under the catalog).
    source_changed: std::collections::HashSet<u64>,
    /// M5: every range read `(url, offset, len)`, in order.
    ranges: Vec<(String, u64, u64)>,
}

/// A dataset's state as the (restored) catalog has it, for `snapshot`.
#[derive(Clone, Debug, Default)]
pub struct FakeSnapshot {
    pub as_of: String,
    pub dataset: Option<Value>,
    pub shares: Vec<Value>,
    pub acls: Vec<Value>,
    /// `file` rows (the `"file"` object of a `file.upsert`), any order.
    pub files: Vec<Value>,
}

/// The fake service and blob store (one object implements both traits so
/// uploads and reads see the same bytes).
#[derive(Default)]
pub struct FakeService {
    st: Mutex<State>,
    pushed: Notify,
}

fn blob_url(file_id: u64, version_id: u64) -> String {
    format!("mem://{file_id}/{version_id}")
}

/// The S3 object the fake names for a file version (versioned, private).
pub fn s3_object(file_id: u64, version_id: u64) -> omnifile_blob::S3Object {
    omnifile_blob::S3Object {
        bucket: "fake-source".into(),
        key: format!("{file_id}/{version_id}"),
        version_id: Some(format!("v{version_id}")),
        etag: Some("\"fake\"".into()),
        region: "eu-west-1".into(),
        endpoint: "vpce-0fake.s3.eu-west-1.vpce.amazonaws.com".into(),
        addressing: omnifile_blob::Addressing::VirtualHost,
        tls12_only: true,
        requester_pays: false,
        private_network: true,
    }
}

/// Byte `i` of a synthetic object.
pub fn synthetic_byte(seed: u64, i: u64) -> u8 {
    (i.wrapping_mul(31).wrapping_add(i / 997).wrapping_add(seed)) as u8
}

/// The fake's blob key for a read: a pointer names its file version.
fn key_of(url: &str, source: SourceKind) -> String {
    if source == SourceKind::S3Presigned
        && let Ok(r) = omnifile_blob::PresignedRead::decode(url)
        && let Some(path) = r
            .url
            .strip_prefix("https://fake.vpce-1.s3.eu-west-1.vpce.amazonaws.com/")
        && let Some((fv, _)) = path.split_once('?')
    {
        return format!("mem://{fv}");
    }
    if source == SourceKind::S3Signed
        && let Ok(o) = omnifile_blob::S3Object::from_pointer(url)
        && let Some((f, v)) = o.key.split_once('/')
    {
        return format!("mem://{f}/{v}");
    }
    url.to_owned()
}

impl FakeService {
    pub fn new() -> FakeService {
        let f = FakeService::default();
        f.st.lock().unwrap().next_id = 1000;
        f
    }

    pub fn next_id(&self) -> u64 {
        let mut s = self.st.lock().unwrap();
        s.next_id += 1;
        s.next_id
    }

    /// Append a raw feed event.
    /// What `asof(dir_id, t)` answers (sorted by name here).
    /// The feed position now (events pushed so far), for a snapshot's `as_of`.
    pub fn feed_position(&self) -> usize {
        self.st.lock().unwrap().events.len()
    }

    /// What `snapshot` serves for `dataset_id` (files sorted by id).
    pub fn set_snapshot(&self, dataset_id: u64, mut snap: FakeSnapshot) {
        let id = |v: &Value| v["file_id"].as_str().and_then(|s| s.parse::<u64>().ok());
        snap.files.sort_by_key(id);
        self.st.lock().unwrap().snapshots.insert(dataset_id, snap);
    }

    /// Make every digest answer differ (writes racing the check).
    pub fn set_digest_drift(&self, v: bool) {
        self.st.lock().unwrap().digest_drift = v;
    }

    /// Every audit record the edge has sent.
    pub fn audits(&self) -> Vec<Value> {
        self.st.lock().unwrap().audits.clone()
    }

    pub fn heartbeats(&self) -> Vec<Health> {
        self.st.lock().unwrap().heartbeats.clone()
    }

    /// Answer heartbeats with this `certificate_renew_after`.
    pub fn set_renew_after(&self, t: Option<&str>) {
        self.st.lock().unwrap().renew_after = t.map(String::from);
    }

    /// Directories `renew_roles_anywhere_if_due` was asked about, in order.
    pub fn ra_renewals(&self) -> Vec<std::path::PathBuf> {
        self.st.lock().unwrap().ra_renewals.clone()
    }

    /// Answer `renew_roles_anywhere_if_due` with `installed`.
    pub fn set_ra_installs(&self, installed: bool) {
        self.st.lock().unwrap().ra_installs = installed;
    }

    pub fn renewals(&self) -> Vec<Option<String>> {
        self.st.lock().unwrap().renewals.clone()
    }

    /// Serve bulk snapshot exports (`snapshot_export_start`) from the
    /// datasets' snapshots, `rows` NDJSON lines per part. Without this the
    /// fake answers `layout_conflict`, as for an S3 dataset.
    pub fn set_exports(&self, rows: usize) {
        self.st.lock().unwrap().export_rows = Some(rows);
    }

    /// Flip one byte of part `n` of every later export, after its SHA-256 is
    /// taken: a corrupt download.
    pub fn set_export_corrupt(&self, n: Option<usize>) {
        self.st.lock().unwrap().export_corrupt = n;
    }

    /// The catalog was restored: the feed answers `reset_required` to every
    /// cursor until the edge reads from a cursor the snapshot gave it.
    pub fn set_reset_required(&self, v: bool) {
        self.st.lock().unwrap().reset_required = v;
    }

    pub fn set_asof(&self, dir_id: u64, t: &str, mut entries: Vec<Value>) {
        entries.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
        self.st
            .lock()
            .unwrap()
            .asof
            .insert((dir_id, t.to_owned()), entries);
    }

    pub fn push_event(&self, ev: Value) {
        self.st.lock().unwrap().events.push(ev);
        self.pushed.notify_waiters();
    }

    /// Every service call so far, as short strings.
    /// Answer renames to `name` with a conflict (the name is taken).
    /// Refuse creates of this name, as the service does when the name is
    /// taken in the catalog.
    pub fn conflict_on_create_named(&self, name: &str) {
        self.st
            .lock()
            .unwrap()
            .create_conflict_names
            .push(name.to_string());
    }

    /// Make the next create but lose its answer.
    pub fn lose_next_create(&self) {
        self.st.lock().unwrap().lose_next_create = true;
    }

    pub fn conflict_on_rename_to(&self, name: &str) {
        self.st
            .lock()
            .unwrap()
            .ns_conflict_names
            .push(name.to_string());
    }

    pub fn calls(&self) -> Vec<String> {
        self.st.lock().unwrap().calls.clone()
    }

    /// The actor sent with the first namespace op or create recorded as
    /// `call` (`control-api.md` §4.1): `None` if no such op was sent,
    /// `Some(None)` if it was sent without one.
    pub fn actor_of(&self, call: &str) -> Option<Option<crate::ports::Actor>> {
        self.st
            .lock()
            .unwrap()
            .actors
            .iter()
            .find(|(c, _)| c == call)
            .map(|(_, a)| a.clone())
    }

    /// The source kind of every range read so far.
    pub fn range_sources(&self) -> Vec<SourceKind> {
        self.st.lock().unwrap().range_sources.clone()
    }

    /// Wait this long before each upload.
    pub fn set_upload_delay_ms(&self, ms: u64) {
        self.st.lock().unwrap().upload_delay_ms = ms;
    }

    /// Have an outbox, whose `outbox_resume` hands back these finished ops
    /// until each is forgotten. Uploads then go through `outbox_commit`,
    /// which this fake refuses: set it once the files are committed.
    pub fn set_outbox_finished(&self, ops: Vec<(OutboxUpload, OutboxOutcome)>) {
        self.st.lock().unwrap().outbox_finished = Some(ops);
    }

    /// Move recall `recall_id` to `state`, as the service would (without a
    /// feed event: push one with `events::recall` when the test wants it).
    pub fn set_recall_state(&self, recall_id: u64, state: &str) {
        if let Some(r) = self.st.lock().unwrap().recall_states.get_mut(&recall_id) {
            r.2 = state.to_string();
        }
    }

    /// An active recall of `(file_id, version_id)` the catalog already holds
    /// in `state` (one this edge started before it last stopped, say): the
    /// next read joins it. Returns its id.
    pub fn add_recall(&self, file_id: u64, version_id: u64, state: &str) -> u64 {
        let id = self.next_id();
        self.st
            .lock()
            .unwrap()
            .recall_states
            .insert(id, (file_id, version_id, state.to_string()));
        id
    }

    /// Recall `recall_id`'s state as the fake catalog holds it.
    pub fn recall_state(&self, recall_id: u64) -> Option<String> {
        self.st
            .lock()
            .unwrap()
            .recall_states
            .get(&recall_id)
            .map(|r| r.2.clone())
    }

    /// Answer `outbox_holds(file_id)` with `v`, as if an upload of it were
    /// held in an outbox.
    pub fn set_outbox_holds(&self, file_id: u64, v: bool) {
        let mut st = self.st.lock().unwrap();
        if v {
            st.outbox_holding.insert(file_id);
        } else {
            st.outbox_holding.remove(&file_id);
        }
    }

    pub fn set_unavailable(&self, v: bool) {
        self.st.lock().unwrap().unavailable = v;
    }

    /// Answer recalls as archive recalls instead of `online`.
    pub fn set_archived(&self, v: bool) {
        self.st.lock().unwrap().archived = v;
    }

    /// Serve each range read after this delay.
    pub fn set_range_delay_ms(&self, ms: u64) {
        self.st.lock().unwrap().range_delay_ms = ms;
    }

    /// Refuse range reads of `url` from `offset` on, as an expired presigned
    /// URL is (403, `PermissionDenied`).
    pub fn expire_url_at(&self, url: &str, offset: u64) {
        self.st.lock().unwrap().expire_at.insert(url.into(), offset);
    }

    /// Fail the next `n` `stub_commit` calls with a non-retryable error.
    pub fn fail_stub_commits(&self, n: u32) {
        self.st.lock().unwrap().fail_stub_commits = n;
    }

    pub fn set_refuse_stub(&self, v: bool) {
        self.st.lock().unwrap().refuse_stub = v;
    }

    /// Store an object as if it were already in the backend.
    pub fn put_blob(&self, file_id: u64, version_id: u64, data: Vec<u8>) {
        self.st
            .lock()
            .unwrap()
            .blobs
            .insert(blob_url(file_id, version_id), data);
    }

    /// Store a synthetic object of `size` bytes (never held in memory).
    pub fn put_synthetic(&self, file_id: u64, version_id: u64, size: u64, seed: u64) {
        self.st
            .lock()
            .unwrap()
            .synthetic
            .insert(blob_url(file_id, version_id), (size, seed));
    }

    /// `read_url` answers S3 pointers (`s3_roles_anywhere`).
    pub fn set_read_s3(&self, v: bool) {
        self.st.lock().unwrap().read_s3 = v;
    }

    /// `read_url` answers presigned S3 URLs (no `versionId`), signed with
    /// `if_match` when given (`None`: an unpinned URL).
    pub fn set_read_presigned(&self, if_match: Option<&str>) {
        self.st.lock().unwrap().read_presigned = Some(if_match.map(str::to_owned));
    }

    /// S3 reads of `file_id` answer `412 PreconditionFailed`.
    pub fn set_source_changed(&self, file_id: u64, v: bool) {
        let mut st = self.st.lock().unwrap();
        if v {
            st.source_changed.insert(file_id);
        } else {
            st.source_changed.remove(&file_id);
        }
    }

    /// Every range read so far: `(blob url, offset, len)`.
    pub fn ranges(&self) -> Vec<(String, u64, u64)> {
        self.st.lock().unwrap().ranges.clone()
    }

    pub fn blob(&self, file_id: u64, version_id: u64) -> Option<Vec<u8>> {
        self.st
            .lock()
            .unwrap()
            .blobs
            .get(&blob_url(file_id, version_id))
            .cloned()
    }

    fn call(&self, s: String) -> CtlResult<()> {
        let mut st = self.st.lock().unwrap();
        if st.unavailable {
            return Err(CtlError::unavailable("fake service is down"));
        }
        st.calls.push(s);
        Ok(())
    }
}

impl ControlPlane for FakeService {
    async fn outbox_holds(&self, file_id: u64) -> bool {
        let _ = self.call(format!("outbox_holds {file_id}"));
        self.st.lock().unwrap().outbox_holding.contains(&file_id)
    }

    fn has_outbox(&self) -> bool {
        self.st.lock().unwrap().outbox_finished.is_some()
    }

    async fn outbox_resume(&self) -> CtlResult<Vec<(OutboxUpload, OutboxOutcome)>> {
        Ok(self
            .st
            .lock()
            .unwrap()
            .outbox_finished
            .clone()
            .unwrap_or_default())
    }

    async fn outbox_forget(&self, op_id: &str) -> CtlResult<()> {
        self.call(format!("outbox_forget {op_id}"))?;
        if let Some(ops) = self.st.lock().unwrap().outbox_finished.as_mut() {
            ops.retain(|(u, _)| u.op_id != op_id);
        }
        Ok(())
    }

    async fn feed(&self, after: &str, limit: u32, wait_s: u32) -> CtlResult<FeedPage> {
        let pos: usize = after
            .split('.')
            .next()
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        // After a restore (`set_reset_required`), cursors carry the new
        // incarnation as a third part; one without it is refused.
        let restored = after.split('.').nth(2) == Some("1");
        let suffix = if restored { ".1" } else { "" };
        for _ in 0..2 {
            let notified = self.pushed.notified();
            {
                let st = self.st.lock().unwrap();
                if st.unavailable {
                    return Err(CtlError::unavailable("fake service is down"));
                }
                if st.reset_required && !restored {
                    return Ok(FeedPage {
                        events: vec![],
                        next: after.to_string(),
                        reset_required: true,
                    });
                }
                if st.events.len() > pos || wait_s == 0 {
                    let end = st.events.len().min(pos + limit as usize);
                    return Ok(FeedPage {
                        events: st.events[pos.min(end)..end].to_vec(),
                        next: format!("{end}.0{suffix}"),
                        reset_required: false,
                    });
                }
            }
            let _ = tokio::time::timeout(Duration::from_secs(wait_s as u64), notified).await;
        }
        Ok(FeedPage {
            events: vec![],
            next: after.to_string(),
            reset_required: false,
        })
    }

    async fn feed_ack(&self, _cursor: &str) -> CtlResult<()> {
        Ok(())
    }

    async fn asof(
        &self,
        dir_id: u64,
        t: &str,
        after: Option<&str>,
        limit: u32,
    ) -> CtlResult<AsofPage> {
        self.call(format!("asof {dir_id} {t} {}", after.unwrap_or("")))?;
        let st = self.st.lock().unwrap();
        let all = st
            .asof
            .get(&(dir_id, t.to_owned()))
            .cloned()
            .unwrap_or_default();
        let rest: Vec<Value> = all
            .into_iter()
            .filter(|e| after.is_none_or(|a| e["name"].as_str().unwrap_or("") > a))
            .collect();
        let more = rest.len() > limit as usize;
        Ok(AsofPage {
            entries: rest.into_iter().take(limit as usize).collect(),
            more,
        })
    }

    async fn snapshot(
        &self,
        dataset_id: u64,
        after_file_id: Option<u64>,
        limit: u32,
    ) -> CtlResult<SnapshotPage> {
        self.call(format!(
            "snapshot {dataset_id} {}",
            after_file_id.unwrap_or(0)
        ))?;
        let st = self.st.lock().unwrap();
        let Some(snap) = st.snapshots.get(&dataset_id).cloned() else {
            return Ok(SnapshotPage::default());
        };
        let id = |v: &Value| v["file_id"].as_str().and_then(|s| s.parse::<u64>().ok());
        let rest: Vec<Value> = snap
            .files
            .iter()
            .filter(|f| after_file_id.is_none_or(|a| id(f).is_some_and(|i| i > a)))
            .cloned()
            .collect();
        let first = after_file_id.is_none();
        Ok(SnapshotPage {
            as_of: first.then(|| snap.as_of.clone()),
            dataset: if first { snap.dataset.clone() } else { None },
            shares: if first { snap.shares.clone() } else { vec![] },
            acls: if first { snap.acls.clone() } else { vec![] },
            more: rest.len() > limit as usize,
            files: rest.into_iter().take(limit as usize).collect(),
        })
    }

    async fn snapshot_export_start(
        &self,
        dataset_id: u64,
        _nonce: &str,
    ) -> CtlResult<SnapshotExport> {
        self.call(format!("snapshot_export_start {dataset_id}"))?;
        let export_id = self.next_id();
        let mut st = self.st.lock().unwrap();
        let Some(rows) = st.export_rows else {
            return Err(CtlError {
                unavailable: false,
                code: Some("layout_conflict".into()),
                message: "no export area".into(),
            });
        };
        let snap = st.snapshots.get(&dataset_id).cloned().unwrap_or_default();
        let mut lines: Vec<String> = Vec::new();
        if let Some(d) = &snap.dataset {
            lines.push(serde_json::json!({ "dataset": d }).to_string());
        }
        lines.extend(
            snap.shares
                .iter()
                .map(|s| serde_json::json!({ "share": s }).to_string()),
        );
        lines.extend(
            snap.acls
                .iter()
                .map(|a| serde_json::json!({ "acl": a }).to_string()),
        );
        lines.extend(
            snap.files
                .iter()
                .map(|f| serde_json::json!({ "file": f }).to_string()),
        );
        let mut parts = Vec::new();
        for (n, chunk) in lines.chunks(rows.max(1)).enumerate() {
            let raw: String = chunk.iter().map(|l| format!("{l}\n")).collect();
            let mut blob = ruzstd::encoding::compress_to_vec(
                raw.as_bytes(),
                ruzstd::encoding::CompressionLevel::Fastest,
            );
            let sha = ring::digest::digest(&ring::digest::SHA256, &blob);
            let sha256: String = sha.as_ref().iter().map(|b| format!("{b:02x}")).collect();
            if st.export_corrupt == Some(n) {
                let last = blob.len() - 1;
                blob[last] ^= 0xFF;
            }
            let url = format!("mem://export/{export_id}/{n}");
            parts.push(ExportPart {
                url: url.clone(),
                rows: chunk.len() as u64,
                bytes: blob.len() as u64,
                sha256,
            });
            st.blobs.insert(url, blob);
        }
        let e = SnapshotExport {
            export_id,
            state: "ready".into(),
            as_of: Some(snap.as_of.clone()),
            parts,
        };
        st.exports.insert(export_id, (e.clone(), 0));
        Ok(SnapshotExport {
            state: "running".into(),
            parts: vec![],
            ..e
        })
    }

    async fn digest(&self, dataset_id: u64) -> CtlResult<Digest> {
        self.call(format!("digest {dataset_id}"))?;
        let mut st = self.st.lock().unwrap();
        st.digest_calls += 1;
        let drift = if st.digest_drift { st.digest_calls } else { 0 };
        let snap = st.snapshots.get(&dataset_id).cloned().unwrap_or_default();
        let id = |v: &Value, k: &str| {
            v[k].as_str()
                .and_then(|s| s.parse::<u64>().ok())
                .unwrap_or(0)
        };
        let digest = snap
            .files
            .iter()
            .map(|f| omnifile_replica::digest_row(id(f, "file_id"), id(f, "entity_version")))
            .fold(0, |a, b| a ^ b)
            ^ drift;
        let suffix = if st.reset_required { ".1" } else { "" };
        Ok(Digest {
            as_of: format!("{}.0{suffix}", st.events.len()),
            live_rows: snap.files.len() as u64,
            digest,
        })
    }

    async fn audit(&self, records: &[Value]) -> CtlResult<()> {
        self.call(format!("audit {}", records.len()))?;
        self.st.lock().unwrap().audits.extend_from_slice(records);
        Ok(())
    }

    async fn heartbeat(&self, h: &Health) -> CtlResult<HeartbeatReply> {
        self.call("heartbeat".into())?;
        let mut st = self.st.lock().unwrap();
        st.heartbeats.push(h.clone());
        Ok(HeartbeatReply {
            min_edge_version: "0.0.0".into(),
            feed_lag_events: Some(0),
            certificate_renew_after: st.renew_after.clone(),
        })
    }

    async fn renew_if_due(&self, renew_after: Option<&str>) -> CtlResult<bool> {
        self.call("renew_if_due".into())?;
        let mut st = self.st.lock().unwrap();
        st.renewals.push(renew_after.map(String::from));
        Ok(false)
    }

    async fn renew_roles_anywhere_if_due(&self, dir: &std::path::Path) -> CtlResult<bool> {
        self.call("renew_roles_anywhere_if_due".into())?;
        let mut st = self.st.lock().unwrap();
        st.ra_renewals.push(dir.to_owned());
        Ok(st.ra_installs)
    }

    async fn snapshot_export(&self, export_id: u64) -> CtlResult<SnapshotExport> {
        self.call(format!("snapshot_export {export_id}"))?;
        let mut st = self.st.lock().unwrap();
        let Some((e, reads)) = st.exports.get_mut(&export_id) else {
            return Err(CtlError {
                unavailable: false,
                code: Some("not_found".into()),
                message: "no such export".into(),
            });
        };
        *reads += 1;
        if *reads == 1 {
            return Ok(SnapshotExport {
                state: "running".into(),
                parts: vec![],
                ..e.clone()
            });
        }
        Ok(e.clone())
    }

    async fn create(&self, op: &CreateOp) -> CtlResult<Created> {
        let line = format!("create {} {}", op.parent_file_id, op.name);
        self.call(line.clone())?;
        self.st
            .lock()
            .unwrap()
            .actors
            .push((line, op.actor.clone()));
        if let Some(id) = self.st.lock().unwrap().created_by_op.get(&op.op_id) {
            return Ok(Created {
                file_id: *id,
                entity_version: 1,
            });
        }
        if self
            .st
            .lock()
            .unwrap()
            .create_conflict_names
            .contains(&op.name)
        {
            return Err(CtlError::rejected("conflict", "the name is taken"));
        }
        let file_id = self.next_id();
        let mut st = self.st.lock().unwrap();
        st.created_by_op.insert(op.op_id.clone(), file_id);
        if std::mem::take(&mut st.lose_next_create) {
            return Err(CtlError::unavailable("the answer was lost"));
        }
        Ok(Created {
            file_id,
            entity_version: 1,
        })
    }

    async fn ns_changes(&self, ops: &[NsChange]) -> CtlResult<Vec<NsResult>> {
        let mut out = Vec::new();
        for op in ops {
            let line = match op {
                NsChange::Rename {
                    file_id,
                    new_parent_file_id,
                    new_name,
                    ..
                } => format!("rename {file_id} {new_parent_file_id} {new_name}"),
                NsChange::Unlink { file_id, .. } => format!("unlink {file_id}"),
                NsChange::Owner {
                    file_id, uid, gid, ..
                } => format!("owner {file_id} {uid} {gid}"),
            };
            self.call(line.clone())?;
            self.st
                .lock()
                .unwrap()
                .actors
                .push((line, op.actor().cloned()));
            let taken = matches!(op, NsChange::Rename { new_name, .. }
                if self.st.lock().unwrap().ns_conflict_names.contains(new_name));
            out.push(NsResult {
                op_id: op.op_id().to_string(),
                status: if taken {
                    NsStatus::Conflict
                } else {
                    NsStatus::Applied
                },
                entity_version: (!taken).then(|| self.next_id()),
                code: taken.then(|| "name_taken".to_string()),
                detail: None,
            });
        }
        Ok(out)
    }

    async fn upload_start(&self, intent: &UploadIntent) -> CtlResult<UploadStart> {
        self.call(format!("upload_start {}", intent.file_id))?;
        let version_id = self.next_id();
        let upload_id = self.next_id();
        self.st
            .lock()
            .unwrap()
            .uploads
            .insert(upload_id, (intent.file_id, version_id));
        Ok(UploadStart::Upload {
            upload_id,
            version_id,
            sas_url: format!("mem://upload/{upload_id}"),
            block_size: intent.block_size,
        })
    }

    async fn upload_complete(&self, upload_id: u64, done: &UploadDone) -> CtlResult<u64> {
        self.call(format!("upload_complete {upload_id}"))?;
        let mut st = self.st.lock().unwrap();
        let (file_id, version_id) = st
            .uploads
            .remove(&upload_id)
            .ok_or_else(|| CtlError::rejected("not_found", "no such upload"))?;
        let data = st
            .blobs
            .remove(&format!("mem://upload/{upload_id}"))
            .ok_or_else(|| CtlError::rejected("blob_missing", "upload not found in blob store"))?;
        if omnifile_integrity::crc64_nvme(&data) != done.crc64nvme || data.len() as u64 != done.size
        {
            return Err(CtlError::rejected("integrity", "crc or size mismatch"));
        }
        st.blobs.insert(blob_url(file_id, version_id), data);
        Ok(version_id)
    }

    async fn stub_prepare(&self, file_id: u64, version_id: u64) -> CtlResult<StubPrepare> {
        self.call(format!("stub_prepare {file_id} {version_id}"))?;
        if self.st.lock().unwrap().refuse_stub {
            return Ok(StubPrepare::Refused {
                code: "stub_not_allowed".into(),
            });
        }
        Ok(StubPrepare::Lease {
            lease_id: format!("lease-{file_id}-{version_id}"),
            resident_bytes: 0,
        })
    }

    async fn stub_commit(
        &self,
        file_id: u64,
        lease_id: &str,
        version_id: u64,
        resident_bytes: u64,
    ) -> CtlResult<()> {
        self.call(format!(
            "stub_commit {file_id} {lease_id} {version_id} {resident_bytes}"
        ))?;
        let mut st = self.st.lock().unwrap();
        if st.fail_stub_commits > 0 {
            st.fail_stub_commits -= 1;
            return Err(CtlError {
                unavailable: false,
                code: Some("internal".into()),
                message: "fake stub_commit failure".into(),
            });
        }
        Ok(())
    }

    async fn stub_skip(
        &self,
        file_id: u64,
        version_id: u64,
        reason: &str,
        _detail: Option<&str>,
    ) -> CtlResult<()> {
        self.call(format!("stub_skip {file_id} {version_id} {reason}"))
    }

    async fn recall_start(&self, req: &RecallRequest) -> CtlResult<RecallResponse> {
        self.call(format!(
            "recall_start {} {} uid={:?} via={} sid={:?}{}",
            req.file_id,
            req.version_id,
            req.uid,
            req.via,
            req.sid,
            if req.pinned { " pinned" } else { "" }
        ))?;
        if self.st.lock().unwrap().archived {
            let active = {
                let st = self.st.lock().unwrap();
                st.recall_states
                    .iter()
                    .find(|(_, (f, v, s))| {
                        (*f, *v) == (req.file_id, req.version_id)
                            && !matches!(
                                s.as_str(),
                                "available" | "failed" | "cancelled" | "superseded"
                            )
                    })
                    .map(|(id, (_, _, s))| (*id, s.clone()))
            };
            let (recall_id, state) = match active {
                Some(a) => a,
                None => {
                    let id = self.next_id();
                    self.st
                        .lock()
                        .unwrap()
                        .recall_states
                        .insert(id, (req.file_id, req.version_id, "rehydrating".into()));
                    (id, "rehydrating".into())
                }
            };
            return Ok(RecallResponse::Recall {
                recall_id,
                state,
                decision: if req.on_open == "wait" {
                    "wait".into()
                } else {
                    "fail".into()
                },
                eta_s: None,
                note: Some("This file is archived and is being recalled.".into()),
            });
        }
        Ok(RecallResponse::Online {
            url: ReadUrl::azure(blob_url(req.file_id, req.version_id)),
        })
    }

    async fn recall_filled(
        &self,
        recall_id: u64,
        version_id: u64,
        resident_bytes: u64,
    ) -> CtlResult<()> {
        self.call(format!(
            "recall_filled {recall_id} {version_id} {resident_bytes}"
        ))?;
        self.set_recall_state(recall_id, "available");
        Ok(())
    }

    async fn recall_failed(&self, recall_id: u64, version_id: u64, reason: &str) -> CtlResult<()> {
        self.call(format!("recall_failed {recall_id} {version_id} {reason}"))?;
        self.set_recall_state(recall_id, "failed");
        Ok(())
    }

    async fn file_filled(
        &self,
        file_id: u64,
        version_id: u64,
        resident_bytes: u64,
    ) -> CtlResult<()> {
        self.call(format!(
            "file_filled {file_id} {version_id} {resident_bytes}"
        ))
    }

    async fn read_url(&self, file_id: u64, version_id: u64) -> CtlResult<ReadUrl> {
        self.call(format!("read_url {file_id} {version_id}"))?;
        // A new credential: not expired.
        let url = blob_url(file_id, version_id);
        let mut st = self.st.lock().unwrap();
        st.expire_at.remove(&url);
        if let Some(if_match) = st.read_presigned.clone() {
            return Ok(ReadUrl {
                url: omnifile_blob::PresignedRead {
                    url: format!("https://fake.vpce-1.s3.eu-west-1.vpce.amazonaws.com/{file_id}/{version_id}?X-Amz-Signature=fake"),
                    if_match,
                }
                .encode(),
                source: SourceKind::S3Presigned,
                crc64nvme: None,
            });
        }
        if st.read_s3 {
            return Ok(ReadUrl {
                url: s3_object(file_id, version_id).to_pointer(),
                source: SourceKind::S3Signed,
                crc64nvme: None,
            });
        }
        Ok(ReadUrl::azure(url))
    }
}

impl BlobIo for FakeService {
    async fn get_range(
        &self,
        url: &str,
        source: SourceKind,
        offset: u64,
        len: u64,
    ) -> io::Result<Vec<u8>> {
        let raw_url = url;
        let url = &key_of(url, source);
        {
            let mut st = self.st.lock().unwrap();
            st.range_sources.push(source);
            st.ranges.push((url.clone(), offset, len));
        }
        let delay = self.st.lock().unwrap().range_delay_ms;
        if delay > 0 {
            tokio::time::sleep(std::time::Duration::from_millis(delay)).await;
        }
        let st = self.st.lock().unwrap();
        let pinned_s3 = source == SourceKind::S3Signed
            || (source == SourceKind::S3Presigned && raw_url.starts_with("s3ps:"));
        if pinned_s3
            && let Some(f) = url
                .strip_prefix("mem://")
                .and_then(|r| r.split('/').next())
                .and_then(|f| f.parse::<u64>().ok())
            && st.source_changed.contains(&f)
        {
            return Err(io::Error::other(omnifile_blob::BlobError::Http {
                op: "S3 GetObject",
                status: 412,
                code: Some("PreconditionFailed".into()),
                message: "At least one of the pre-conditions you specified did not hold".into(),
                request_id: None,
                retry_after: None,
            }));
        }
        if let Some(&(size, seed)) = st.synthetic.get(url) {
            let end = offset
                .checked_add(len)
                .filter(|e| *e <= size)
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        format!("range {offset}+{len} past {size}"),
                    )
                })?;
            return Ok((offset..end).map(|i| synthetic_byte(seed, i)).collect());
        }
        if st.expire_at.get(url).is_some_and(|at| offset >= *at) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "403 expired",
            ));
        }
        let b = st
            .blobs
            .get(url)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, url.to_string()))?;
        let end = offset
            .checked_add(len)
            .filter(|e| *e <= b.len() as u64)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    format!("range {offset}+{len} past {}", b.len()),
                )
            })?;
        Ok(b[offset as usize..end as usize].to_vec())
    }

    async fn upload(&self, req: &BlobUpload) -> io::Result<BlobUploaded> {
        let delay = self.st.lock().unwrap().upload_delay_ms;
        if delay > 0 {
            tokio::time::sleep(Duration::from_millis(delay)).await;
        }
        let data = tokio::fs::read(&req.path).await?;
        if data.len() as u64 != req.size {
            return Err(io::Error::other("file changed during upload"));
        }
        let crc = omnifile_integrity::crc64_nvme(&data);
        self.st
            .lock()
            .unwrap()
            .blobs
            .insert(req.sas_url.clone(), data);
        Ok(BlobUploaded {
            etag: format!("\"{crc:016x}\""),
            version_id: None,
            crc64nvme: crc,
            size: req.size,
        })
    }
}

/// Feed-event builders for tests and the fake backend.
pub mod events {
    use super::*;

    pub fn share(share_id: u64, root_dir_id: u64, name: &str, on_open: &str, ev: u64) -> Value {
        json!({"type": "share.upsert", "entity_id": share_id.to_string(), "entity_version": ev.to_string(),
               "share": {"share_id": share_id.to_string(), "dataset_id": "1", "name": name,
                         "root_dir_id": root_dir_id.to_string(), "on_open": on_open,
                         "worm": {"enabled": false}, "entity_version": ev.to_string()}})
    }

    /// [`share`] with a quiet period of its own (null: its default), WORM or
    /// not (decision 67).
    pub fn share_quiet(
        share_id: u64,
        root_dir_id: u64,
        name: &str,
        quiet_s: Option<u64>,
        worm: bool,
        ev: u64,
    ) -> Value {
        json!({"type": "share.upsert", "entity_id": share_id.to_string(), "entity_version": ev.to_string(),
               "share": {"share_id": share_id.to_string(), "dataset_id": "1", "name": name,
                         "root_dir_id": root_dir_id.to_string(), "on_open": "fail",
                         "quiet_s": quiet_s, "worm": {"enabled": worm},
                         "entity_version": ev.to_string()}})
    }

    /// [`share`] with rewind labels: daily at 00:00 UTC for `depth_days`.
    pub fn share_with_rewind(
        share_id: u64,
        root_dir_id: u64,
        name: &str,
        depth_days: u32,
        ev: u64,
    ) -> Value {
        let mut v = share(share_id, root_dir_id, name, "wait", ev);
        v["share"]["rewind"] = json!({"depth_days": depth_days, "max_days": depth_days,
                                      "labels": {"daily_at_utc": "00:00"}});
        v
    }

    pub fn dir(file_id: u64, parent: Option<u64>, name: &str, ev: u64) -> Value {
        json!({"type": "file.upsert", "entity_id": file_id.to_string(), "entity_version": ev.to_string(),
               "file": {"file_id": file_id.to_string(), "dataset_id": "1",
                        "parent_id": parent.map(|p| p.to_string()), "name": name, "kind": "dir",
                        "mode": 0o755, "uid": 0, "gid": 0, "mtime_ns": "0", "entity_version": ev.to_string()}})
    }

    #[allow(clippy::too_many_arguments)]
    pub fn file(
        file_id: u64,
        parent: u64,
        name: &str,
        size: u64,
        version_id: u64,
        crc: Option<u64>,
        archived: bool,
        stub: Option<(u64, u64)>,
        ev: u64,
    ) -> Value {
        json!({"type": "file.upsert", "entity_id": file_id.to_string(), "entity_version": ev.to_string(),
               "file": {"file_id": file_id.to_string(), "dataset_id": "1", "parent_id": parent.to_string(),
                        "name": name, "kind": "file", "size": size, "mtime_ns": "1700000000000000000",
                        "uid": 0, "gid": 0, "mode": 0o644, "version_id": version_id.to_string(),
                        "crc64nvme": crc.map(|c| format!("{c:016x}")),
                        "location": {"backend_kind": "azure_blob", "container": "a/c", "object_key": "k",
                                     "logical_tier": if archived { "archive" } else { "hot" },
                                     "archived": archived,
                                     "restore_state": if archived { "archived" } else { "not_needed" }},
                        "stub": stub.map(|(v, r)| json!({"version_id": v.to_string(), "resident_bytes": r})),
                        "entity_version": ev.to_string()}})
    }

    pub fn recall(
        recall_id: u64,
        file_id: u64,
        version_id: u64,
        state: &str,
        url: Option<&str>,
        ev: u64,
    ) -> Value {
        recall_with_mode(
            recall_id,
            file_id,
            version_id,
            state,
            url,
            "azure_user_delegation_sas",
            ev,
        )
    }

    /// [`recall`] whose read credential has this `mode` (`s3_presigned_url`).
    pub fn recall_with_mode(
        recall_id: u64,
        file_id: u64,
        version_id: u64,
        state: &str,
        url: Option<&str>,
        mode: &str,
        ev: u64,
    ) -> Value {
        json!({"type": "recall.state", "entity_id": recall_id.to_string(), "entity_version": ev.to_string(),
               "recall": {"recall_id": recall_id.to_string(), "file_id": file_id.to_string(),
                          "version_id": version_id.to_string(), "state": state, "requesters": [],
                          "read_credential": url.map(|u| json!({"mode": mode, "url": u,
                              "expires_at": "2099-01-01T00:00:00Z", "cache_until": "2099-01-01T00:00:00Z"})),
                          "entity_version": ev.to_string()}})
    }
}
