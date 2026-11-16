//! The daemon's view of the service and of blob storage.
//!
//! These traits are the seams to the Edge-I/O crates (`omnifile-control`,
//! `omnifile-blob`, `omnifile-detect`). The daemon only ever talks to these
//! traits; [`crate::fakes`] implements them in memory for tests and for
//! running the daemon without a service, and thin adapters implement them
//! over the real clients.

use std::future::Future;
use std::io;
use std::path::PathBuf;

pub use omnifile_control::types::{Actor, ActorVia};
pub use omnifile_stub::SourceKind;
use serde_json::Value;

/// A control-API failure.
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub struct CtlError {
    /// The service can't be reached: answer from local state, never block
    /// the FSAL, retry later.
    pub unavailable: bool,
    /// A stable error code from the problem document, if any.
    pub code: Option<String>,
    pub message: String,
}

impl CtlError {
    pub fn unavailable(msg: impl Into<String>) -> CtlError {
        CtlError {
            unavailable: true,
            code: None,
            message: msg.into(),
        }
    }
    pub fn rejected(code: &str, msg: impl Into<String>) -> CtlError {
        CtlError {
            unavailable: false,
            code: Some(code.into()),
            message: msg.into(),
        }
    }
}

pub type CtlResult<T> = Result<T, CtlError>;

/// One page of the feed (raw events, parsed by `omnifile-replica`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct FeedPage {
    pub events: Vec<Value>,
    pub next: String,
    pub reset_required: bool,
}

/// One page of a directory as it was at a time (raw `FileRow` JSON).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct AsofPage {
    pub entries: Vec<Value>,
    pub more: bool,
}

/// One page of a dataset's snapshot (`control-api.md` §6): raw rows in the
/// feed's shapes; `as_of`, the dataset, shares and ACLs on the first page.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct SnapshotPage {
    pub as_of: Option<String>,
    pub dataset: Option<Value>,
    pub shares: Vec<Value>,
    pub acls: Vec<Value>,
    pub files: Vec<Value>,
    pub more: bool,
}

/// What the edge reports about itself every heartbeat (`control-api.md`
/// §4.7). Every field is a gauge read at send time.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Health {
    pub version: String,
    pub kernel: Option<String>,
    /// `armed`, `degraded` or `down`; `None` without an smb-guard.
    pub guard_state: Option<String>,
    pub outbox_depth: u64,
    pub feed_cursor: Option<String>,
    pub fills_running: u64,
    pub recalls_waiting: u64,
    pub data_free_bytes: Option<u64>,
    pub data_capacity_bytes: Option<u64>,
    pub fsal_connections: u64,
    /// 1 while the break-glass account has been used since it was last
    /// sealed; `None` on a box without one (setup-wizard.md).
    pub recovery_used: Option<u64>,
    /// This edge's quiet periods for a share with none of its own (decision 67):
    /// `[commit] quiet_s` and `[commit] worm_quiet_s`. Sent at all, it also says
    /// the edge applies each share's own period.
    pub quiet_default_s: Option<u64>,
    pub quiet_worm_default_s: Option<u64>,
    /// How long the shares' quiet periods have failed to be read, in seconds; 0
    /// while reads work (#80 re-check).
    pub quiet_stale_s: Option<u64>,
    /// 1: this edge knows the recall state `superseded`. Until the service has
    /// heard it, the feed says `cancelled` in its place (control API §4.4).
    pub recall_superseded: Option<u64>,
}

/// The service's answer to a heartbeat.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct HeartbeatReply {
    pub min_edge_version: String,
    pub feed_lag_events: Option<i64>,
    pub certificate_renew_after: Option<String>,
}

/// The service's reconciliation digest (`control-api.md` §6).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Digest {
    pub as_of: String,
    pub live_rows: u64,
    pub digest: u64,
}

/// A bulk snapshot export (`control-api.md` §6): `parts` once `state` is
/// `ready`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SnapshotExport {
    pub export_id: u64,
    /// `running`, `ready`, `failed` or `expired`.
    pub state: String,
    pub as_of: Option<String>,
    pub parts: Vec<ExportPart>,
}

/// One zstd-compressed NDJSON part of an export, read by `url` and checked
/// against `bytes` and `sha256` before any of it is used.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ExportPart {
    pub url: String,
    pub rows: u64,
    pub bytes: u64,
    pub sha256: String,
}

/// A local namespace change, by catalog ids (`docs/design/namespace-sync.md`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NsChange {
    /// Renamed or moved on the edge.
    Rename {
        op_id: String,
        file_id: u64,
        new_parent_file_id: u64,
        new_name: String,
        actor: Option<Actor>,
    },
    /// Deleted on the edge (a folder only once its children are).
    Unlink {
        op_id: String,
        file_id: u64,
        actor: Option<Actor>,
    },
    /// `chown` on the edge: the owner and group only (the ACL scan reports
    /// mode and ACL).
    Owner {
        op_id: String,
        file_id: u64,
        uid: u32,
        gid: u32,
        actor: Option<Actor>,
    },
}

impl NsChange {
    pub fn op_id(&self) -> &str {
        match self {
            NsChange::Rename { op_id, .. }
            | NsChange::Unlink { op_id, .. }
            | NsChange::Owner { op_id, .. } => op_id,
        }
    }

    /// Who made it (`control-api.md` §4.1 `actor`).
    pub fn actor(&self) -> Option<&Actor> {
        match self {
            NsChange::Rename { actor, .. }
            | NsChange::Unlink { actor, .. }
            | NsChange::Owner { actor, .. } => actor.as_ref(),
        }
    }
}

/// The service's answer to one [`NsChange`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NsResult {
    pub op_id: String,
    pub status: NsStatus,
    pub entity_version: Option<u64>,
    /// `version_conflict`, `name_taken`, `directory_not_empty`, …
    pub code: Option<String>,
    pub detail: Option<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NsStatus {
    /// Applied now, or already (a replay).
    Applied,
    /// The catalog kept its own state; the edge keeps the user's.
    Conflict,
    Rejected,
}

/// A `create` change op for an edge-created object.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CreateOp {
    pub op_id: String,
    /// The parent's catalog id.
    pub parent_file_id: u64,
    pub name: String,
    /// `file`, `dir` or `symlink`.
    pub kind: String,
    pub fsid: u64,
    pub ino: u64,
    pub ino_gen: u32,
    pub size: u64,
    pub mtime_ns: i64,
    pub uid: u32,
    pub gid: u32,
    pub mode: u32,
    /// Who made it (a new folder: `control-api.md` §4.1 `actor`); None for
    /// a file's create, which its first commit sends.
    pub actor: Option<Actor>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Created {
    pub file_id: u64,
    pub entity_version: u64,
}

/// `POST …/uploads`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UploadIntent {
    pub op_id: String,
    pub file_id: u64,
    pub size: u64,
    pub mtime_ns: i64,
    pub blake3_hex: String,
    pub crc64nvme: u64,
    pub block_size: u64,
    pub base_version_id: Option<u64>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum UploadStart {
    Upload {
        upload_id: u64,
        version_id: u64,
        sas_url: String,
        block_size: u64,
    },
    /// Same bytes already committed in the dataset: no upload.
    Dedupe { version_id: u64 },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UploadDone {
    pub crc64nvme: u64,
    pub blake3_hex: String,
    pub size: u64,
    pub etag: String,
    pub backend_version_id: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StubPrepare {
    Lease {
        lease_id: String,
        resident_bytes: u64,
    },
    Refused {
        code: String,
    },
}

/// `POST …/recalls`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RecallRequest {
    pub file_id: u64,
    pub version_id: u64,
    pub offset: u64,
    pub length: u64,
    pub uid: Option<u32>,
    /// `sys`, `krb5`, … (absent when unknown).
    pub sec: Option<String>,
    /// `claimed` or `proven`.
    pub proof: String,
    pub client_addr: Option<String>,
    /// The share's `on_open`: `wait`, `fail` or `auto`.
    pub on_open: String,
    /// `nfs` (FSAL_OMNI), `smb` or `local` (the smb-guard).
    pub via: String,
    /// The requester's AD SID, when winbind knows it.
    pub sid: Option<String>,
    /// The read is of a rewind placeholder: the reader asked for this version
    /// itself, so its recall outlives the file being replaced or deleted.
    pub pinned: bool,
}

/// A URL a fill reads from, and what kind it is.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReadUrl {
    pub url: String,
    pub source: SourceKind,
    /// The object's CRC-64/NVME the credential vouches for (S3), used
    /// when the replica has none for the file yet.
    pub crc64nvme: Option<u64>,
}

impl ReadUrl {
    pub fn azure(url: impl Into<String>) -> ReadUrl {
        ReadUrl {
            url: url.into(),
            source: SourceKind::AzureSas,
            crc64nvme: None,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RecallResponse {
    /// Readable now: range-GET it with this read URL.
    Online { url: ReadUrl },
    Recall {
        recall_id: u64,
        /// Catalog state word.
        state: String,
        /// `wait` or `fail`.
        decision: String,
        eta_s: Option<u32>,
        note: Option<String>,
    },
}

/// A commit clone handed to the durable uploads outbox (control API §4.2,
/// `edge-metadata.md` §5.4). The outbox owns the op from here: it survives a
/// restart, replays with the same `op_id` (so the service never makes a
/// second version), and never deletes the clone (the daemon does, after it
/// has acted on the outcome).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OutboxUpload {
    pub op_id: String,
    pub file_id: u64,
    /// The commit clone under `.omni/commit/`.
    pub clone: PathBuf,
    pub size: u64,
    pub mtime_ns: i64,
    pub blake3_hex: String,
    pub crc64nvme: u64,
    pub block_size: u64,
    pub block_crcs: Vec<u64>,
    pub base_version_id: Option<u64>,
    pub uid: u32,
    pub gid: u32,
    pub mode: u32,
}

/// A terminal outbox state.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OutboxOutcome {
    /// Committed by `complete`, or deduplicated by the service.
    Committed { version_id: u64 },
    /// `409 version_conflict`: the catalog moved on; the feed resolves it.
    Conflict { detail: String },
    /// A permanent failure (the upload was aborted).
    Failed { detail: String },
}

/// The service, as the daemon needs it (control-api §4–§5).
pub trait ControlPlane: Send + Sync + 'static {
    fn feed(
        &self,
        after: &str,
        limit: u32,
        wait_s: u32,
    ) -> impl Future<Output = CtlResult<FeedPage>> + Send;
    fn feed_ack(&self, cursor: &str) -> impl Future<Output = CtlResult<()>> + Send;
    /// A directory's entries as they were at `t` (rewind trees,
    /// `control-api.md` §4.5), names after `after`.
    fn asof(
        &self,
        dir_id: u64,
        t: &str,
        after: Option<&str>,
        limit: u32,
    ) -> impl Future<Output = CtlResult<AsofPage>> + Send;
    /// One page of a dataset's snapshot, files after `after_file_id`.
    fn snapshot(
        &self,
        dataset_id: u64,
        after_file_id: Option<u64>,
        limit: u32,
    ) -> impl Future<Output = CtlResult<SnapshotPage>> + Send;
    /// Start a bulk export of a dataset's snapshot (`control-api.md` §6).
    /// `nonce` makes one attempt's retries idempotent.
    fn snapshot_export_start(
        &self,
        dataset_id: u64,
        nonce: &str,
    ) -> impl Future<Output = CtlResult<SnapshotExport>> + Send;
    fn snapshot_export(
        &self,
        export_id: u64,
    ) -> impl Future<Output = CtlResult<SnapshotExport>> + Send;
    /// The digest of a dataset's live rows, computed now.
    fn digest(&self, dataset_id: u64) -> impl Future<Output = CtlResult<Digest>> + Send;
    /// Report health (`control-api.md` §4.7).
    fn heartbeat(&self, h: &Health) -> impl Future<Output = CtlResult<HeartbeatReply>> + Send;
    /// Renew the client certificate if it is due (day 20 of 30, or the
    /// service's `certificate_renew_after`). `true` if it was renewed.
    fn renew_if_due(
        &self,
        renew_after: Option<&str>,
    ) -> impl Future<Output = CtlResult<bool>> + Send;
    /// Request or renew the IAM Roles Anywhere certificate from the edge CA
    /// when the wizard's request is in `dir` and it is due (decision 50).
    /// `true` if a new one was installed.
    fn renew_roles_anywhere_if_due(
        &self,
        dir: &std::path::Path,
    ) -> impl Future<Output = CtlResult<bool>> + Send;
    /// Send audit records (`control-api.md` §4.7), at most 5,000.
    fn audit(&self, records: &[Value]) -> impl Future<Output = CtlResult<()>> + Send;
    fn create(&self, op: &CreateOp) -> impl Future<Output = CtlResult<Created>> + Send;
    /// Local renames, deletes and owner changes, in order, one batch
    /// (`control-api.md` §4.1). A result for every op.
    fn ns_changes(&self, ops: &[NsChange])
    -> impl Future<Output = CtlResult<Vec<NsResult>>> + Send;
    fn upload_start(
        &self,
        intent: &UploadIntent,
    ) -> impl Future<Output = CtlResult<UploadStart>> + Send;
    fn upload_complete(
        &self,
        upload_id: u64,
        done: &UploadDone,
    ) -> impl Future<Output = CtlResult<u64>> + Send;
    fn stub_prepare(
        &self,
        file_id: u64,
        version_id: u64,
    ) -> impl Future<Output = CtlResult<StubPrepare>> + Send;
    fn stub_commit(
        &self,
        file_id: u64,
        lease_id: &str,
        version_id: u64,
        resident_bytes: u64,
    ) -> impl Future<Output = CtlResult<()>> + Send;
    fn stub_skip(
        &self,
        file_id: u64,
        version_id: u64,
        reason: &str,
        detail: Option<&str>,
    ) -> impl Future<Output = CtlResult<()>> + Send;
    fn recall_start(
        &self,
        req: &RecallRequest,
    ) -> impl Future<Output = CtlResult<RecallResponse>> + Send;
    fn recall_filled(
        &self,
        recall_id: u64,
        version_id: u64,
        resident_bytes: u64,
    ) -> impl Future<Output = CtlResult<()>> + Send;
    /// A `downloading` recall this edge could not fill: it becomes `failed`
    /// with `reason`, so it never stays `downloading` (control-api.md §4.4).
    fn recall_failed(
        &self,
        recall_id: u64,
        version_id: u64,
        reason: &str,
    ) -> impl Future<Output = CtlResult<()>> + Send;
    /// An online fetch (no recall) filled a stub: the catalog says
    /// `resident` again once every byte is local (control API §4.4).
    fn file_filled(
        &self,
        _file_id: u64,
        _version_id: u64,
        _resident_bytes: u64,
    ) -> impl Future<Output = CtlResult<()>> + Send {
        async { Ok(()) }
    }
    /// `POST …/acls`: ACL bodies (`AclSnapshot` JSON) for a dataset (§4.6).
    fn acl_put(
        &self,
        _dataset_id: u64,
        _acls: Vec<Value>,
    ) -> impl Future<Output = CtlResult<()>> + Send {
        async { Ok(()) }
    }
    /// `POST …/acl-scans` → `scan_id` (`kind`: `full` or `on_change`).
    fn acl_scan_start(
        &self,
        _dataset_id: u64,
        _kind: &str,
        _nonce: &str,
    ) -> impl Future<Output = CtlResult<u64>> + Send {
        async { Ok(0) }
    }
    /// `POST …/acl-scans/{scan_id}/complete`.
    fn acl_scan_complete(
        &self,
        _scan_id: u64,
        _seen: u64,
        _changed: u64,
    ) -> impl Future<Output = CtlResult<()>> + Send {
        async { Ok(()) }
    }
    /// `setattr` change ops `{mode, acl_sha256}`: point each file at its ACL
    /// and record the mode that goes with it (the catalog must not later chmod
    /// the file back to a mode the ACL no longer has). `(file_id, sha, mode)`.
    /// `batch_id` makes the batch's idempotency key. Returns how many applied.
    fn acl_set(
        &self,
        _batch_id: &str,
        _ops: Vec<(u64, String, u32)>,
    ) -> impl Future<Output = CtlResult<usize>> + Send {
        async { Ok(0) }
    }
    /// A read URL for `(file_id, content version)`.
    fn read_url(
        &self,
        file_id: u64,
        version_id: u64,
    ) -> impl Future<Output = CtlResult<ReadUrl>> + Send;

    /// True when uploads go through the durable outbox ([`Self::outbox_commit`]).
    /// False: the commit pipeline calls `upload_start`, [`BlobIo::upload`] and
    /// `upload_complete` itself (the in-memory fake).
    fn has_outbox(&self) -> bool {
        false
    }

    /// Enqueue (idempotently, by `op_id`) and drive one upload to a terminal
    /// state. `Err` with `unavailable`: the op stays queued, with its clone,
    /// and [`Self::outbox_resume`] finishes it later.
    fn outbox_commit(
        &self,
        _up: &OutboxUpload,
    ) -> impl Future<Output = CtlResult<OutboxOutcome>> + Send {
        async {
            Err(CtlError::rejected(
                "no_outbox",
                "this control plane has no outbox",
            ))
        }
    }

    /// Drive every queued op as far as it goes, and return every op in a
    /// terminal state that has not been forgotten yet (oldest first).
    fn outbox_resume(
        &self,
    ) -> impl Future<Output = CtlResult<Vec<(OutboxUpload, OutboxOutcome)>>> + Send {
        async { Ok(Vec::new()) }
    }

    /// Forget a terminal op (after the daemon removed its clone).
    fn outbox_forget(&self, _op_id: &str) -> impl Future<Output = CtlResult<()>> + Send {
        async { Ok(()) }
    }

    /// Whether the outbox holds an upload of `file_id`, finished or not (it
    /// is done once forgotten). A file it holds is not committed again by
    /// the namespace sweep.
    fn outbox_holds(&self, _file_id: u64) -> impl Future<Output = bool> + Send {
        async { false }
    }

    /// The commit clones the outbox's uploads name, finished or not. A clone
    /// in `.omni/commit` that none names is an orphan (WP9): its commit
    /// stopped before the upload was journalled.
    fn outbox_clones(&self) -> impl Future<Output = Vec<PathBuf>> + Send {
        async { Vec::new() }
    }
}

/// `PUT` of a whole file as a plain block blob.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BlobUpload {
    pub sas_url: String,
    pub path: PathBuf,
    pub size: u64,
    pub block_size: u64,
    /// Expected per-block CRC-64/NVME, from the hasher.
    pub block_crcs: Vec<u64>,
    pub uid: u32,
    pub gid: u32,
    pub mode: u32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BlobUploaded {
    pub etag: String,
    pub version_id: Option<String>,
    pub crc64nvme: u64,
    pub size: u64,
}

/// Blob data plane (the Edge-I/O Blob client).
pub trait BlobIo: Send + Sync + 'static {
    /// Exactly `len` bytes at `offset`: CRC-checked per range for Azure,
    /// unverified for S3 (the fill checks the whole file).
    fn get_range(
        &self,
        url: &str,
        source: SourceKind,
        offset: u64,
        len: u64,
    ) -> impl Future<Output = io::Result<Vec<u8>>> + Send;
    fn upload(&self, req: &BlobUpload) -> impl Future<Output = io::Result<BlobUploaded>> + Send;
}

/// A finished write, from the detector (close-write plus quiet period) or
/// the FSAL's `WRITE_CLOSED`, or a file the namespace sweep found that no
/// close was seen for.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CloseWrite {
    pub path: PathBuf,
    pub ino: u64,
    /// Set by the namespace sweep (WP9): the inode's generation when the
    /// sweep found it. No close was seen, so the commit checks again that
    /// it is the same inode, quiet, open for write nowhere, and not yet
    /// committed or held in the upload outbox ([`crate::commit`]).
    pub swept_gen: Option<u32>,
    /// The detector's pending-close token (WP9, [`crate::pending`]): the
    /// write stays in the durable log of pending closes until this commit
    /// is done, so a restart before then replays it.
    pub token: Option<u64>,
    /// What the detector saw when the file went quiet. Without a close
    /// (an NFSv3 writer, whose Ganesha keeps its fd), the commit waits a
    /// further quiet period for the size and mtime to hold.
    pub seen: Option<Seen>,
}

/// The file as the detector saw it when it went quiet.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Seen {
    pub size: u64,
    pub mtime_ns: i64,
    /// A close was seen after the last write.
    pub closed: bool,
    pub at: std::time::Instant,
}

impl CloseWrite {
    /// A close the detector (or anything that saw the write) reports.
    pub fn new(path: PathBuf, ino: u64) -> Self {
        CloseWrite {
            path,
            ino,
            swept_gen: None,
            token: None,
            seen: None,
        }
    }
}
