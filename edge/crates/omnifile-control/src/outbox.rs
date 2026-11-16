//! The uploads/commit outbox (control API §4.2; `edge-metadata.md` §5.4).
//!
//! Each outbox `commit` op is one record file, rewritten atomically at every
//! state change, so a restart resumes exactly where it stopped:
//!
//! ```text
//! Queued --uploads--> Uploading --blocks + block list--> Uploaded --complete--> Committed
//!        \--dedupe---> Deduped
//!        \--409 version_conflict--> Conflict
//! any non-retryable error ------------------------------------------------> Failed
//! ```
//!
//! * The intent's idempotency key derives from `op_id`, `complete`'s from
//!   `upload_id`, so a resend after a crash is a replay, never a second
//!   version.
//! * Block uploads resume from the per-op upload journal
//!   ([`omnifile_blob::UploadJournal`]).
//! * The write SAS is renewed (`credentials {access: write, upload_id}`) when
//!   its `cache_until` has passed or Azure refuses it.
//! * The whole-file CRC combined from the uploaded blocks must equal the
//!   CRC the hasher computed, or the op fails instead of committing.
//! * **WORM rule (decision 31):** the outbox never deletes the commit clone.
//!   The daemon deletes it after it sees `Committed` or `Deduped`, then
//!   calls [`UploadOutbox::remove`].
//!
//! Draining stops at the first "service unavailable" or transient Azure
//! error, so ops go out in order once the service is back (control API §11,
//! reconnect order); the caller retries later with backoff.

use std::collections::BTreeMap;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use omnifile_blob::{BlobClient, BlobError, CommitProps, PosixMeta, UploadOutcome, UploadRequest};
use omnifile_integrity::BlockPlan;
use serde::{Deserialize, Serialize};

use crate::client::ControlClient;
use crate::error::{ControlError, Result};
use crate::types::{
    CompleteBlock, Credential, CredentialRequest, FileRef, UploadComplete, UploadIntent,
    UploadStart, crc_hex, id, nanos,
};
use crate::util::{parse_time, sha256_hex, write_atomic};

/// What the daemon enqueues when a version is complete.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct NewUpload {
    /// The outbox op id (edge id + monotonic counter), unique per edge.
    pub op_id: String,
    pub file: FileRef,
    /// The commit clone under `.omni/commit/`.
    pub source: PathBuf,
    pub size: u64,
    #[serde(with = "nanos")]
    pub mtime_ns: i64,
    pub blake3: String,
    #[serde(with = "crc_hex")]
    pub crc64nvme: u64,
    pub block_size: u64,
    /// Per-block CRCs from the hasher, for `block_size`.
    pub block_crcs: Vec<u64>,
    #[serde(default, with = "id::opt")]
    pub base_version_id: Option<u64>,
    pub uid: u32,
    pub gid: u32,
    pub mode: u32,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum UploadState {
    Queued,
    Uploading {
        #[serde(with = "id")]
        upload_id: u64,
        #[serde(with = "id")]
        version_id: u64,
        block_size: u64,
        set_tier: bool,
        target: Credential,
    },
    Uploaded {
        #[serde(with = "id")]
        upload_id: u64,
        #[serde(with = "id")]
        version_id: u64,
        etag: Option<String>,
        backend_version_id: Option<String>,
        /// The committed block list `complete` declares (control API §4.2).
        #[serde(default)]
        blocks: Vec<CompleteBlock>,
    },
    Committed {
        #[serde(with = "id")]
        version_id: u64,
        committed_at: String,
        retention_until: Option<String>,
    },
    Deduped {
        #[serde(with = "id")]
        version_id: u64,
    },
    /// `409 version_conflict`: the edge resolves it from the feed.
    Conflict {
        code: String,
        detail: Option<String>,
    },
    Failed {
        detail: String,
    },
}

impl UploadState {
    pub fn is_terminal(&self) -> bool {
        matches!(
            self,
            UploadState::Committed { .. }
                | UploadState::Deduped { .. }
                | UploadState::Conflict { .. }
                | UploadState::Failed { .. }
        )
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct UploadOp {
    pub seq: u64,
    pub upload: NewUpload,
    pub state: UploadState,
    pub attempts: u32,
    pub last_error: Option<String>,
}

/// One block-blob upload for an [`UploadSink`].
#[derive(Clone, Debug)]
pub struct UploadJob {
    pub sas_url: String,
    pub source: PathBuf,
    pub plan: BlockPlan,
    pub block_crcs: Option<Vec<u64>>,
    pub journal: PathBuf,
    pub fingerprint: String,
    pub posix: PosixMeta,
    /// The service's `set_tier`. False: send no `x-ms-access-tier`.
    pub set_tier: bool,
}

/// Where the bytes go. Implemented for [`BlobClient`]; tests use fakes.
pub trait UploadSink: Send + Sync {
    fn upload(
        &self,
        job: &UploadJob,
    ) -> impl Future<Output = std::result::Result<UploadOutcome, BlobError>> + Send;
}

/// The tier sent when the service says `set_tier: true`. The contract does
/// not name one; Hot is the only tier valid on every account type.
pub const EXPLICIT_TIER: &str = "Hot";

/// A re-minted write SAS must have carried the upload at least this long
/// before an auth failure re-mints again (the SAS lasts 15 minutes).
pub const SAS_REMINT_MIN_RUN: std::time::Duration = std::time::Duration::from_secs(60);
/// Re-mints per drive: 64 x 15 minutes is far beyond any single upload.
pub const MAX_SAS_RENEWALS: u32 = 64;

impl UploadSink for BlobClient {
    async fn upload(&self, job: &UploadJob) -> std::result::Result<UploadOutcome, BlobError> {
        if job.set_tier {
            tracing::warn!(
                tier = EXPLICIT_TIER,
                "service asked for an explicit tier; the contract names none, sending Hot"
            );
        }
        let req = UploadRequest {
            sas_url: job.sas_url.clone(),
            source: job.source.clone(),
            plan: job.plan,
            expected_block_crcs: job.block_crcs.clone(),
            journal: job.journal.clone(),
            fingerprint: job.fingerprint.clone(),
            props: CommitProps {
                metadata: job.posix.to_metadata(),
                access_tier: job.set_tier.then(|| EXPLICIT_TIER.to_owned()),
                content_type: None,
            },
        };
        self.upload_file(&req).await
    }
}

/// Outcome of a drain.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct DrainReport {
    /// Ops that reached a terminal state in this drain.
    pub finished: Vec<String>,
    /// Ops still waiting (service or Azure unavailable).
    pub waiting: usize,
}

pub struct UploadOutbox {
    dir: PathBuf,
    ops: BTreeMap<String, UploadOp>,
    next_seq: u64,
}

impl UploadOutbox {
    /// Open (or create) the outbox in `dir`, loading every record.
    pub fn open(dir: impl Into<PathBuf>) -> Result<Self> {
        let dir = dir.into();
        std::fs::create_dir_all(dir.join("ops"))?;
        std::fs::create_dir_all(dir.join("journals"))?;
        let mut ops = BTreeMap::new();
        let mut next_seq = 1;
        for entry in std::fs::read_dir(dir.join("ops"))? {
            let path = entry?.path();
            if path.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }
            let op: UploadOp = serde_json::from_slice(&std::fs::read(&path)?).map_err(|e| {
                ControlError::Invalid(format!("outbox record {}: {e}", path.display()))
            })?;
            next_seq = next_seq.max(op.seq + 1);
            ops.insert(op.upload.op_id.clone(), op);
        }
        Ok(Self { dir, ops, next_seq })
    }

    fn stem(op_id: &str) -> String {
        sha256_hex(op_id.as_bytes())[..32].to_owned()
    }

    fn record_path(&self, op_id: &str) -> PathBuf {
        self.dir
            .join("ops")
            .join(format!("{}.json", Self::stem(op_id)))
    }

    /// The upload journal for an op.
    pub fn journal_path(&self, op_id: &str) -> PathBuf {
        self.dir
            .join("journals")
            .join(format!("{}.journal", Self::stem(op_id)))
    }

    fn persist(&self, op: &UploadOp) -> Result<()> {
        let json = serde_json::to_vec(op).map_err(|e| ControlError::Invalid(e.to_string()))?;
        write_atomic(&self.record_path(&op.upload.op_id), &json, 0o600)
    }

    /// Add a `commit` op. Enqueuing an `op_id` that is already present is a
    /// no-op, so the caller may retry after a crash.
    pub fn enqueue(&mut self, upload: NewUpload) -> Result<()> {
        if self.ops.contains_key(&upload.op_id) {
            return Ok(());
        }
        if upload.block_crcs.len() as u64 != upload.size.div_ceil(upload.block_size.max(1)) {
            return Err(ControlError::Invalid(format!(
                "op {}: {} block CRCs for {} bytes in {}-byte blocks",
                upload.op_id,
                upload.block_crcs.len(),
                upload.size,
                upload.block_size
            )));
        }
        let op = UploadOp {
            seq: self.next_seq,
            upload,
            state: UploadState::Queued,
            attempts: 0,
            last_error: None,
        };
        self.persist(&op)?;
        self.next_seq += 1;
        self.ops.insert(op.upload.op_id.clone(), op);
        Ok(())
    }

    pub fn get(&self, op_id: &str) -> Option<&UploadOp> {
        self.ops.get(op_id)
    }

    /// Ops not yet terminal, in enqueue order.
    pub fn pending(&self) -> Vec<&UploadOp> {
        let mut v: Vec<_> = self
            .ops
            .values()
            .filter(|o| !o.state.is_terminal())
            .collect();
        v.sort_by_key(|o| o.seq);
        v
    }

    /// Terminal ops, waiting for the daemon to act and [`remove`](Self::remove) them.
    pub fn finished(&self) -> Vec<&UploadOp> {
        let mut v: Vec<_> = self
            .ops
            .values()
            .filter(|o| o.state.is_terminal())
            .collect();
        v.sort_by_key(|o| o.seq);
        v
    }

    pub fn len(&self) -> usize {
        self.ops.len()
    }

    pub fn is_empty(&self) -> bool {
        self.ops.is_empty()
    }

    /// Forget a terminal op and its journal (after the daemon has dealt with
    /// the commit clone).
    pub fn remove(&mut self, op_id: &str) -> Result<()> {
        if let Some(op) = self.ops.get(op_id)
            && !op.state.is_terminal()
        {
            return Err(ControlError::Invalid(format!(
                "op {op_id} is still in flight"
            )));
        }
        omnifile_blob::UploadJournal::remove(&self.journal_path(op_id))
            .map_err(|e| ControlError::Invalid(e.to_string()))?;
        match std::fs::remove_file(self.record_path(op_id)) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
        self.ops.remove(op_id);
        Ok(())
    }

    fn set_state(&mut self, op_id: &str, state: UploadState, error: Option<String>) -> Result<()> {
        // Errors may quote a URL: no SAS signature is ever stored.
        let state = match state {
            UploadState::Failed { detail } => UploadState::Failed {
                detail: crate::util::redact_sas(&detail),
            },
            UploadState::Conflict { code, detail } => UploadState::Conflict {
                code,
                detail: detail.map(|d| crate::util::redact_sas(&d)),
            },
            other => other,
        };
        let op = self.ops.get_mut(op_id).expect("op exists");
        op.state = state;
        op.last_error = error.map(|e| crate::util::redact_sas(&e));
        let snapshot = op.clone();
        self.persist(&snapshot)
    }

    fn note_error(&mut self, op_id: &str, e: &dyn std::fmt::Display) -> Result<()> {
        let op = self.ops.get_mut(op_id).expect("op exists");
        op.attempts += 1;
        op.last_error = Some(crate::util::redact_sas(&e.to_string()));
        let snapshot = op.clone();
        self.persist(&snapshot)
    }

    /// Drive every pending op as far as it can go, in order. Returns `Err`
    /// when the service (or Azure) is unavailable; everything done so far is
    /// durable, and the next call resumes.
    pub async fn drain<S: UploadSink>(
        &mut self,
        client: &ControlClient,
        sink: &S,
    ) -> Result<DrainReport> {
        let mut report = DrainReport::default();
        let ids: Vec<String> = self
            .pending()
            .iter()
            .map(|o| o.upload.op_id.clone())
            .collect();
        for (i, op_id) in ids.iter().enumerate() {
            match self.drive(op_id, client, sink).await {
                Ok(()) => report.finished.push(op_id.clone()),
                Err(e) => {
                    report.waiting = ids.len() - i;
                    tracing::info!(op_id, error = %crate::util::redact_sas(&e.to_string()), "upload outbox paused");
                    return Err(e);
                }
            }
        }
        Ok(report)
    }

    /// Drive one op to a terminal state, or return the transient error that
    /// stopped it.
    pub async fn drive<S: UploadSink>(
        &mut self,
        op_id: &str,
        client: &ControlClient,
        sink: &S,
    ) -> Result<()> {
        let mut renewed_sas = false;
        let mut sas_renewals = 0u32;
        let mut sas_since = std::time::Instant::now();
        let mut minted = false;
        loop {
            let op = self
                .ops
                .get(op_id)
                .ok_or_else(|| ControlError::Invalid(format!("no op {op_id}")))?
                .clone();
            let u = &op.upload;
            match op.state {
                ref s if s.is_terminal() => return Ok(()),
                UploadState::Queued => {
                    let intent = UploadIntent {
                        op_id: u.op_id.clone(),
                        file: u.file.clone(),
                        size: u.size,
                        mtime_ns: u.mtime_ns,
                        blake3: u.blake3.clone(),
                        crc64nvme: u.crc64nvme,
                        block_size: u.block_size,
                        base_version_id: u.base_version_id,
                    };
                    match client.upload_start(&intent).await {
                        Ok(UploadStart::Upload {
                            upload_id,
                            version_id,
                            target,
                            block_size,
                            set_tier,
                        }) => self.set_state(
                            op_id,
                            UploadState::Uploading {
                                upload_id,
                                version_id,
                                block_size,
                                set_tier,
                                target,
                            },
                            None,
                        )?,
                        Ok(UploadStart::Dedupe { version_id }) => {
                            self.set_state(op_id, UploadState::Deduped { version_id }, None)?
                        }
                        Err(e) if e.code() == Some("version_conflict") => self.set_state(
                            op_id,
                            UploadState::Conflict {
                                code: "version_conflict".into(),
                                detail: e.problem().and_then(|p| p.detail.clone()),
                            },
                            None,
                        )?,
                        Err(e) => return self.fail_or_wait(op_id, e, None, client).await,
                    }
                }
                UploadState::Uploading {
                    upload_id,
                    version_id,
                    block_size,
                    set_tier,
                    ref target,
                } => {
                    let stale = target
                        .cache_until()
                        .and_then(parse_time)
                        .is_none_or(|t| SystemTime::now() >= t);
                    // Renew a stale SAS once per drive: a credential the service
                    // has just minted is used even if its cache window is short.
                    if target.is_unsupported() {
                        // A backend this edge cannot write to: minting again
                        // would return the same thing. Fail this op only.
                        tracing::error!(
                            op_id,
                            "write credential has an unknown mode: unsupported backend, upgrade the edge"
                        );
                        let e = ControlError::Invalid(UNSUPPORTED_CREDENTIAL.into());
                        return self.fail_or_wait(op_id, e, Some(upload_id), client).await;
                    }
                    let unusable = target.sas_url().is_none_or(str::is_empty);
                    if unusable && minted {
                        let e = ControlError::Invalid(
                            "the service returned a write credential with no SAS URL".into(),
                        );
                        return self.fail_or_wait(op_id, e, Some(upload_id), client).await;
                    }
                    if unusable || (stale && !minted) {
                        minted = true;
                        match client
                            .mint_credential(&CredentialRequest::Write { upload_id })
                            .await
                        {
                            Ok(fresh) => {
                                self.set_state(
                                    op_id,
                                    UploadState::Uploading {
                                        upload_id,
                                        version_id,
                                        block_size,
                                        set_tier,
                                        target: fresh,
                                    },
                                    None,
                                )?;
                                continue;
                            }
                            Err(e) => {
                                return self.fail_or_wait(op_id, e, Some(upload_id), client).await;
                            }
                        }
                    }
                    let sas_url = target.sas_url().expect("checked above").to_owned();
                    let plan = BlockPlan::with_block_size(u.size, block_size)
                        .map_err(|e| ControlError::Invalid(format!("op {op_id}: {e}")));
                    let plan = match plan {
                        Ok(p) => p,
                        Err(e) => {
                            return self.fail_or_wait(op_id, e, Some(upload_id), client).await;
                        }
                    };
                    let job = UploadJob {
                        sas_url,
                        source: u.source.clone(),
                        plan,
                        // The hasher's CRCs only describe this layout if the
                        // service kept the proposed block size.
                        block_crcs: (block_size == u.block_size).then(|| u.block_crcs.clone()),
                        journal: self.journal_path(op_id),
                        fingerprint: format!("{}:{}", u.blake3, upload_id),
                        posix: PosixMeta {
                            uid: u.uid,
                            gid: u.gid,
                            mode: u.mode,
                        },
                        set_tier,
                    };
                    match sink.upload(&job).await {
                        Ok(out) if out.crc64nvme != u.crc64nvme || out.size != u.size => {
                            let detail = format!(
                                "uploaded CRC {:016x}/{} bytes != hashed {:016x}/{} bytes",
                                out.crc64nvme, out.size, u.crc64nvme, u.size
                            );
                            self.abort_quietly(client, upload_id).await;
                            self.set_state(op_id, UploadState::Failed { detail }, None)?;
                        }
                        Ok(out) => self.set_state(
                            op_id,
                            UploadState::Uploaded {
                                upload_id,
                                version_id,
                                etag: out.etag,
                                backend_version_id: out.version_id,
                                blocks: out
                                    .blocks
                                    .iter()
                                    .map(|b| CompleteBlock {
                                        id: b.id.clone(),
                                        length: b.len,
                                        crc64nvme: b.crc64nvme,
                                    })
                                    .collect(),
                            },
                            None,
                        )?,
                        // The write SAS lasts 15 minutes: a long upload outlives
                        // it. Re-mint and resume (the journal keeps the staged
                        // blocks) whenever the last SAS carried the upload a
                        // while; an auth failure right after a mint is final.
                        Err(e)
                            if e.is_auth()
                                && (!renewed_sas || sas_since.elapsed() >= SAS_REMINT_MIN_RUN)
                                && sas_renewals < MAX_SAS_RENEWALS =>
                        {
                            renewed_sas = true;
                            sas_renewals += 1;
                            sas_since = std::time::Instant::now();
                            minted = false;
                            self.note_error(op_id, &e)?;
                            let op = self.ops.get_mut(op_id).expect("op exists");
                            if let UploadState::Uploading { target, .. } = &mut op.state {
                                *target = expired_placeholder();
                            }
                        }
                        Err(e) if e.is_retryable() => {
                            self.note_error(op_id, &e)?;
                            return Err(ControlError::Unavailable {
                                op: "blob upload",
                                detail: e.to_string(),
                                retry_after: e.retry_after(),
                            });
                        }
                        Err(e) => {
                            self.abort_quietly(client, upload_id).await;
                            self.set_state(
                                op_id,
                                UploadState::Failed {
                                    detail: e.to_string(),
                                },
                                None,
                            )?;
                        }
                    }
                }
                UploadState::Uploaded {
                    upload_id,
                    ref etag,
                    ref backend_version_id,
                    ref blocks,
                    ..
                } => {
                    let body = UploadComplete {
                        crc64nvme: u.crc64nvme,
                        blake3: u.blake3.clone(),
                        size: u.size,
                        etag: etag.clone(),
                        backend_version_id: backend_version_id.clone(),
                        blocks: blocks.clone(),
                    };
                    match client.upload_complete(upload_id, &body).await {
                        Ok(c) => self.set_state(
                            op_id,
                            UploadState::Committed {
                                version_id: c.version_id,
                                committed_at: c.committed_at,
                                retention_until: c.retention_until,
                            },
                            None,
                        )?,
                        Err(e) => return self.fail_or_wait(op_id, e, None, client).await,
                    }
                }
                _ => unreachable!("terminal states handled above"),
            }
        }
    }

    /// Transient: record and return the error (the op waits). Permanent: the
    /// op fails (and its upload is aborted, if one was started).
    async fn fail_or_wait(
        &mut self,
        op_id: &str,
        e: ControlError,
        upload_id: Option<u64>,
        client: &ControlClient,
    ) -> Result<()> {
        if e.is_retryable() {
            self.note_error(op_id, &e)?;
            return Err(e);
        }
        if let Some(id) = upload_id {
            self.abort_quietly(client, id).await;
        }
        self.set_state(
            op_id,
            UploadState::Failed {
                detail: e.to_string(),
            },
            None,
        )?;
        Ok(())
    }

    async fn abort_quietly(&self, client: &ControlClient, upload_id: u64) {
        if let Err(e) = client.upload_abort(upload_id).await {
            tracing::warn!(upload_id, error = %e, "abort failed; the service aborts it after 7 days");
        }
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }
}

/// What an older edge says about a credential `mode` it does not know.
pub const UNSUPPORTED_CREDENTIAL: &str = "unsupported backend: the service sent a credential mode this edge does not know; upgrade the edge";

/// A credential that is already stale, forcing a renewal on the next step.
fn expired_placeholder() -> Credential {
    Credential::AzureUserDelegationSas {
        url: String::new(),
        expires_at: "1970-01-01T00:00:00Z".into(),
        cache_until: "1970-01-01T00:00:00Z".into(),
    }
}

#[cfg(test)]
mod redaction_tests {
    use super::*;

    /// No SAS signature reaches an outbox record (security review item 4).
    #[test]
    fn errors_are_stored_without_sas_signatures() {
        let d = tempfile::tempdir().unwrap();
        let mut ob = UploadOutbox::open(d.path().join("outbox")).unwrap();
        ob.enqueue(NewUpload {
            op_id: "e1:1".into(),
            file: crate::types::FileRef::Id { file_id: 7 },
            source: d.path().join("clone"),
            size: 0,
            mtime_ns: 0,
            blake3: "0".repeat(64),
            crc64nvme: 0,
            block_size: 4 << 20,
            block_crcs: vec![],
            base_version_id: None,
            uid: 0,
            gid: 0,
            mode: 0o600,
        })
        .unwrap();
        let leak = "Put Block: transport: error sending request for url \
                    (https://a.blob.core.windows.net/c/b?sv=1&sig=SECRET%2Bsig&se=2)";
        ob.note_error("e1:1", &leak).unwrap();
        ob.set_state(
            "e1:1",
            UploadState::Failed {
                detail: leak.into(),
            },
            Some(leak.into()),
        )
        .unwrap();
        let op = ob.get("e1:1").unwrap();
        let shown = format!("{:?} {:?}", op.state, op.last_error);
        assert!(!shown.contains("SECRET"), "{shown}");

        // Real transport errors against a URL carrying `sig=`: the raw
        // `reqwest::Error` (whose Display includes the URL) and the
        // `BlobError` the client returns, both stored as the outbox does.
        let port = {
            let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            l.local_addr().unwrap().port()
        };
        let url = format!("http://127.0.0.1:{port}/c/b?sv=1&sig=REALSECRET%2Fx&se=2");
        let rt = tokio::runtime::Runtime::new().unwrap();
        let (raw, blob) = rt.block_on(async {
            let http = omnifile_blob::tls::http_client(
                omnifile_blob::TlsOptions::public().client_config().unwrap(),
                std::time::Duration::from_secs(2),
            )
            .unwrap();
            let raw = http.get(&url).send().await.unwrap_err();
            let blob = omnifile_blob::BlobClient::new(omnifile_blob::BlobOptions {
                allow_http: true,
                retry: omnifile_blob::RetryPolicy {
                    max_attempts: 1,
                    ..Default::default()
                },
                ..Default::default()
            })
            .unwrap()
            .get_range(&url, 0, 10)
            .await
            .unwrap_err();
            (raw, blob)
        });
        assert!(
            raw.to_string().contains("sig="),
            "the raw error does carry it: {raw}"
        );
        let rendered = format!("{blob} {blob:?}");
        assert!(!rendered.contains("REALSECRET"), "{rendered}");
        ob.note_error("e1:1", &raw).unwrap();
        ob.set_state(
            "e1:1",
            UploadState::Failed {
                detail: raw.to_string(),
            },
            Some(blob.to_string()),
        )
        .unwrap();
        let op = ob.get("e1:1").unwrap();
        let shown = format!("{:?} {:?}", op.state, op.last_error);
        assert!(!shown.contains("REALSECRET"), "{shown}");
        for e in std::fs::read_dir(d.path().join("outbox")).unwrap() {
            let p = e.unwrap().path();
            if p.is_file() {
                let s = std::fs::read_to_string(&p).unwrap_or_default();
                assert!(!s.contains("SECRET"), "{} holds a SAS", p.display());
                assert!(!s.contains("sig=R"), "{} holds a SAS", p.display());
            }
        }
    }
}
