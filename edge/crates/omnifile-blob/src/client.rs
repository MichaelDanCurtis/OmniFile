//! The Blob data-plane client: Put Block, Put Block List, Get Blob (ranged),
//! Get Blob Properties, all authorised by a user-delegation SAS in the URL;
//! and ranged S3 GetObject from a presigned URL (reads only).
//!
//! Behaviour per Microsoft's REST reference (Put Block, Put Block List,
//! Get Blob, Get Blob Properties) and `service/.../sim_http/README.md`:
//!
//! * every Put Block carries `x-ms-content-crc64` (Base64 of the
//!   little-endian CRC-64/NVME), so Azure verifies each block on receipt;
//! * Put Block List sends **no `x-ms-access-tier` unless asked**: on a Smart
//!   account a blob with an explicit tier never enters Smart;
//! * POSIX attributes go in metadata named `file_owner`, `file_group`,
//!   `file_permissions` (Azure metadata names must be C# identifiers);
//! * ranged reads of up to 4 MiB ask for `x-ms-range-get-content-crc64` and
//!   check it.

use std::collections::BTreeMap;
use std::future::Future;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use bytes::Bytes;
use omnifile_integrity::{
    BlockPlan, Crc64Combiner, azure_content_crc64, block_id, crc64_nvme, parse_azure_content_crc64,
};
use reqwest::{Method, RequestBuilder, Response, Url};
use tokio::task::JoinSet;

use crate::error::{BlobError, Result};
use crate::journal::UploadJournal;
use crate::tls::{TlsOptions, http_client};

/// Default `x-ms-version`. The sim facade's default; override for accounts
/// that need another one.
pub const DEFAULT_X_MS_VERSION: &str = "2026-04-06";

/// Largest range for which Azure returns a range CRC64.
pub const MAX_RANGE_CRC: u64 = 4 << 20;

#[derive(Clone, Debug)]
pub struct RetryPolicy {
    /// Attempts in total, including the first.
    pub max_attempts: u32,
    pub base_delay: Duration,
    pub max_delay: Duration,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self {
            max_attempts: 6,
            base_delay: Duration::from_millis(250),
            max_delay: Duration::from_secs(20),
        }
    }
}

impl RetryPolicy {
    /// Full-jitter exponential backoff for attempt `n` (1-based), at least
    /// `floor` (a server `Retry-After`).
    pub fn delay(&self, n: u32, floor: Option<Duration>) -> Duration {
        let exp = self
            .base_delay
            .saturating_mul(1u32 << n.saturating_sub(1).min(16))
            .min(self.max_delay);
        let jittered = exp.mul_f64(0.5 + fastrand::f64() * 0.5);
        jittered.max(floor.unwrap_or_default().min(self.max_delay * 4))
    }
}

#[derive(Clone, Debug)]
pub struct BlobOptions {
    pub x_ms_version: String,
    /// Blocks in flight at once per upload. Memory use is about
    /// `concurrency × block_size`.
    pub concurrency: usize,
    pub retry: RetryPolicy,
    pub connect_timeout: Duration,
    /// Per-request timeout for small requests (properties, block list).
    pub request_timeout: Duration,
    /// Floor on the time a block transfer is allowed: `block_len / min_rate`.
    pub min_transfer_bytes_per_sec: u64,
    /// Cap on upload throughput per upload (bytes/s), for example to leave
    /// ExpressRoute gateway headroom. `None`: unlimited.
    pub max_upload_bytes_per_sec: Option<u64>,
    pub tls: TlsOptions,
    /// Accept `http://` blob URLs. Only for the local sim and tests: a SAS
    /// sent in clear text can be replayed by anyone on the path. Off by
    /// default, so every URL must be `https://`.
    pub allow_http: bool,
}

impl Default for BlobOptions {
    fn default() -> Self {
        Self {
            x_ms_version: DEFAULT_X_MS_VERSION.to_owned(),
            concurrency: 4,
            retry: RetryPolicy::default(),
            connect_timeout: Duration::from_secs(10),
            request_timeout: Duration::from_secs(60),
            min_transfer_bytes_per_sec: 1 << 20,
            max_upload_bytes_per_sec: None,
            tls: TlsOptions::public(),
            allow_http: false,
        }
    }
}

/// POSIX attributes stored as blob metadata.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PosixMeta {
    pub uid: u32,
    pub gid: u32,
    /// Permission bits (`mode & 0o7777`).
    pub mode: u32,
}

/// Azure metadata names for the POSIX attributes (S3 uses `file-owner` etc.;
/// Azure names must be C# identifiers). Values: decimal uid and gid, and the
/// permission bits as 4-digit octal (`0644`).
pub const META_FILE_OWNER: &str = "file_owner";
pub const META_FILE_GROUP: &str = "file_group";
pub const META_FILE_PERMISSIONS: &str = "file_permissions";

impl PosixMeta {
    pub fn to_metadata(self) -> Vec<(String, String)> {
        vec![
            (META_FILE_OWNER.into(), self.uid.to_string()),
            (META_FILE_GROUP.into(), self.gid.to_string()),
            (
                META_FILE_PERMISSIONS.into(),
                format!("{:04o}", self.mode & 0o7777),
            ),
        ]
    }

    pub fn from_metadata(meta: &BTreeMap<String, String>) -> Option<Self> {
        Some(Self {
            uid: meta.get(META_FILE_OWNER)?.parse().ok()?,
            gid: meta.get(META_FILE_GROUP)?.parse().ok()?,
            mode: u32::from_str_radix(meta.get(META_FILE_PERMISSIONS)?, 8).ok()?,
        })
    }
}

/// Asked once every block is staged, before Put Block List (an async check,
/// so a stat of a network file runs off the runtime's threads).
pub type Gate<'a> = dyn Fn() -> std::pin::Pin<
        Box<dyn std::future::Future<Output = std::result::Result<(), String>> + Send + 'a>,
    > + Send
    + Sync
    + 'a;

/// Properties set when committing the block list.
#[derive(Clone, Debug, Default)]
pub struct CommitProps {
    pub metadata: Vec<(String, String)>,
    /// `x-ms-access-tier`. Leave `None` on Smart accounts (the service's
    /// `set_tier: false`).
    pub access_tier: Option<String>,
    pub content_type: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CommitInfo {
    pub etag: Option<String>,
    pub version_id: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BlobProperties {
    pub size: u64,
    pub etag: Option<String>,
    pub version_id: Option<String>,
    pub access_tier: Option<String>,
    pub archive_status: Option<String>,
    pub content_type: Option<String>,
    /// `x-ms-meta-*`, names lower-cased.
    pub metadata: BTreeMap<String, String>,
}

/// One upload of a local file to one blob.
#[derive(Clone, Debug)]
pub struct UploadRequest {
    /// Blob URL with a write SAS (`sp` containing `w`, or `cw` for a new blob).
    pub sas_url: String,
    /// The bytes: the commit clone, which must not change during the upload.
    pub source: PathBuf,
    pub plan: BlockPlan,
    /// Per-block CRCs computed when the clone was hashed. When given, every
    /// block read for upload is checked against them, so a clone that changed
    /// is caught before Azure gets a byte.
    pub expected_block_crcs: Option<Vec<u64>>,
    pub journal: PathBuf,
    /// Identity of the bytes for the journal (BLAKE3 hex, or the version id).
    pub fingerprint: String,
    pub props: CommitProps,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UploadOutcome {
    pub etag: Option<String>,
    pub version_id: Option<String>,
    /// Whole-object CRC-64/NVME, combined from the block CRCs.
    pub crc64nvme: u64,
    pub size: u64,
    pub blocks_uploaded: u32,
    pub blocks_resumed: u32,
    /// The committed block list, in order: what `uploads/complete` declares
    /// (control API §4.2: `blocks`).
    pub blocks: Vec<CommittedBlock>,
}

/// One committed block, as Put Block sent it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CommittedBlock {
    /// The base64 block id, exactly as sent on the wire.
    pub id: String,
    pub len: u64,
    /// The CRC-64/NVME sent as the block's `x-ms-content-crc64`.
    pub crc64nvme: u64,
}

#[derive(Clone)]
pub struct BlobClient {
    http: reqwest::Client,
    opts: Arc<BlobOptions>,
}

impl BlobClient {
    pub fn new(opts: BlobOptions) -> Result<Self> {
        let cfg = opts
            .tls
            .client_config()
            .map_err(|e| BlobError::Invalid(e.to_string()))?;
        let http = http_client(cfg, opts.connect_timeout)
            .map_err(|e| BlobError::Invalid(e.to_string()))?;
        Ok(Self {
            http,
            opts: Arc::new(opts),
        })
    }

    /// With an existing reqwest client (shared pool, test setups).
    pub fn with_http(http: reqwest::Client, opts: BlobOptions) -> Self {
        Self {
            http,
            opts: Arc::new(opts),
        }
    }

    pub fn options(&self) -> &BlobOptions {
        &self.opts
    }

    fn request(&self, method: Method, url: Url) -> RequestBuilder {
        self.http
            .request(method, url)
            .header("x-ms-version", &self.opts.x_ms_version)
            .header("x-ms-client-request-id", uuid_like())
    }

    fn transfer_timeout(&self, len: u64) -> Duration {
        let secs = len / self.opts.min_transfer_bytes_per_sec.max(1);
        self.opts.request_timeout + Duration::from_secs(secs)
    }

    async fn with_retry<T, F, Fut>(&self, op: &'static str, mut f: F) -> Result<T>
    where
        F: FnMut() -> Fut,
        Fut: Future<Output = Result<T>>,
    {
        let mut attempt = 1;
        loop {
            match f().await {
                Ok(v) => return Ok(v),
                Err(e) if e.is_retryable() && attempt < self.opts.retry.max_attempts => {
                    let d = self.opts.retry.delay(attempt, e.retry_after());
                    tracing::debug!(op, attempt, error = %e, delay_ms = d.as_millis() as u64, "retrying");
                    tokio::time::sleep(d).await;
                    attempt += 1;
                }
                Err(e) => return Err(e),
            }
        }
    }

    // ---- Put Block -------------------------------------------------------

    /// Stage one block. `crc` is the block's CRC-64/NVME; Azure rejects the
    /// block (400) if the body doesn't match it. No retry here.
    pub async fn put_block(
        &self,
        sas_url: &str,
        block_id: &str,
        data: Bytes,
        crc: u64,
    ) -> Result<()> {
        const OP: &str = "Put Block";
        let mut url = self.sas_url(sas_url)?;
        url.query_pairs_mut()
            .append_pair("comp", "block")
            .append_pair("blockid", block_id);
        let len = data.len() as u64;
        let resp = self
            .request(Method::PUT, url)
            .header("x-ms-content-crc64", azure_content_crc64(crc))
            .header(reqwest::header::CONTENT_LENGTH, len)
            .timeout(self.transfer_timeout(len))
            .body(data)
            .send()
            .await
            .map_err(|source| BlobError::transport(OP, source))?;
        let resp = check(OP, resp).await?;
        if let Some(echo) = header(&resp, "x-ms-content-crc64") {
            let got = parse_azure_content_crc64(&echo).map_err(|e| BlobError::BadResponse {
                op: OP,
                detail: e.to_string(),
            })?;
            if got != crc {
                return Err(BlobError::BadResponse {
                    op: OP,
                    detail: format!("service CRC {got:016x} != sent {crc:016x}"),
                });
            }
        }
        Ok(())
    }

    // ---- Put Block List --------------------------------------------------

    /// Commit `block_ids` (in order) as the blob's content.
    pub async fn put_block_list(
        &self,
        sas_url: &str,
        block_ids: &[String],
        props: &CommitProps,
    ) -> Result<CommitInfo> {
        const OP: &str = "Put Block List";
        for (name, _) in &props.metadata {
            if !is_csharp_identifier(name) {
                return Err(BlobError::Invalid(format!(
                    "metadata name {name:?} is not a C# identifier (Azure rejects it)"
                )));
            }
        }
        let mut body = String::with_capacity(64 + block_ids.len() * 32);
        body.push_str("<?xml version=\"1.0\" encoding=\"utf-8\"?><BlockList>");
        for id in block_ids {
            body.push_str("<Latest>");
            body.push_str(id); // Base64: no XML escaping needed.
            body.push_str("</Latest>");
        }
        body.push_str("</BlockList>");
        let body = Bytes::from(body);
        let body_crc = crc64_nvme(&body);
        let mut url = self.sas_url(sas_url)?;
        url.query_pairs_mut().append_pair("comp", "blocklist");
        self.with_retry(OP, || {
            let mut req = self
                .request(Method::PUT, url.clone())
                .header("x-ms-content-crc64", azure_content_crc64(body_crc))
                .header(reqwest::header::CONTENT_TYPE, "application/xml")
                .timeout(self.opts.request_timeout);
            for (k, v) in &props.metadata {
                req = req.header(format!("x-ms-meta-{k}"), v);
            }
            if let Some(tier) = &props.access_tier {
                req = req.header("x-ms-access-tier", tier);
            }
            if let Some(ct) = &props.content_type {
                req = req.header("x-ms-blob-content-type", ct);
            }
            let req = req.body(body.clone());
            async move {
                let resp = req
                    .send()
                    .await
                    .map_err(|source| BlobError::transport(OP, source))?;
                let resp = check(OP, resp).await?;
                Ok(CommitInfo {
                    etag: header(&resp, "etag"),
                    version_id: header(&resp, "x-ms-version-id"),
                })
            }
        })
        .await
    }

    // ---- Get Blob (ranged) ----------------------------------------------

    /// Read exactly `len` bytes at `offset`, with retries. Ranges of at most
    /// 4 MiB are CRC-checked by Azure's range CRC64.
    pub async fn get_range(&self, sas_url: &str, offset: u64, len: u64) -> Result<Bytes> {
        const OP: &str = "Get Blob";
        if len == 0 {
            return Ok(Bytes::new());
        }
        let url = self.sas_url(sas_url)?;
        let end = offset
            .checked_add(len - 1)
            .ok_or_else(|| BlobError::Invalid("range overflows".into()))?;
        let want_crc = len <= MAX_RANGE_CRC;
        let want_version = url
            .query_pairs()
            .find(|(k, _)| k == "versionid")
            .map(|(_, v)| v.into_owned());
        self.with_retry(OP, || {
            let mut req = self
                .request(Method::GET, url.clone())
                .header("x-ms-range", format!("bytes={offset}-{end}"))
                .timeout(self.transfer_timeout(len));
            if want_crc {
                req = req.header("x-ms-range-get-content-crc64", "true");
            }
            let want_version = want_version.clone();
            async move {
                let resp = req
                    .send()
                    .await
                    .map_err(|source| BlobError::transport(OP, source))?;
                let resp = check(OP, resp).await?;
                let status = resp.status().as_u16();
                let crc_hdr = header(&resp, "x-ms-content-crc64");
                let version_hdr = header(&resp, "x-ms-version-id");
                let body = resp
                    .bytes()
                    .await
                    .map_err(|source| BlobError::transport(OP, source))?;
                if status != 206 && !(status == 200 && offset == 0) {
                    return Err(BlobError::BadResponse {
                        op: OP,
                        detail: format!("status {status} for a range read"),
                    });
                }
                if body.len() as u64 != len {
                    return Err(BlobError::BadResponse {
                        op: OP,
                        detail: format!("asked {len} bytes at {offset}, got {}", body.len()),
                    });
                }
                // The URL names a version (`versionid=`): the bytes must be
                // that version's, never whatever the blob holds now.
                if let Some(want) = &want_version
                    && version_hdr.as_deref() != Some(want.as_str())
                {
                    return Err(BlobError::Integrity(format!(
                        "range read returned version {:?}, not {want}",
                        version_hdr
                    )));
                }
                if want_crc {
                    // Asked for and not given: the bytes are unverified.
                    let Some(h) = crc_hdr else {
                        return Err(BlobError::BadResponse {
                            op: OP,
                            detail: "no x-ms-content-crc64 on a CRC-checked range read".into(),
                        });
                    };
                    let want =
                        parse_azure_content_crc64(&h).map_err(|e| BlobError::BadResponse {
                            op: OP,
                            detail: e.to_string(),
                        })?;
                    let got = crc64_nvme(&body);
                    if want != got {
                        return Err(BlobError::BadResponse {
                            op: OP,
                            detail: format!("range CRC {got:016x} != service {want:016x}"),
                        });
                    }
                }
                Ok(body)
            }
        })
        .await
    }

    // ---- S3 GetObject (ranged, presigned) --------------------------------

    /// Read exactly `len` bytes at `offset` from an S3 presigned GetObject
    /// URL, or from a [`PresignedRead`] (its `s3ps:` form) whose signature
    /// covers an `If-Match` header: that header is sent with exactly the
    /// signed value, and a `412` ([`BlobError::is_source_changed`]) means
    /// the object was replaced since the catalog recorded its ETag.
    ///
    /// With retries. S3 returns no checksum of a range, so these bytes
    /// are **unverified**: the caller checks the whole file before exposing
    /// any of them (`docs/design/fsal-namespace.md` §5.3). What is checked
    /// here: the status, the length, and the version when the URL names one.
    pub async fn get_s3_range(&self, presigned_url: &str, offset: u64, len: u64) -> Result<Bytes> {
        const OP: &str = "S3 GetObject";
        if len == 0 {
            return Ok(Bytes::new());
        }
        let read = PresignedRead::decode(presigned_url)?;
        let url = self.sas_url(&read.url)?;
        let if_match = read.if_match;
        let end = offset
            .checked_add(len - 1)
            .ok_or_else(|| BlobError::Invalid("range overflows".into()))?;
        let want_version = url
            .query_pairs()
            .find(|(k, _)| k == "versionId")
            .map(|(_, v)| v.into_owned());
        self.with_retry(OP, || {
            // Only the host is signed: extra headers would not break the
            // signature, but none are needed.
            let mut req = self
                .http
                .request(Method::GET, url.clone())
                .header("range", format!("bytes={offset}-{end}"))
                .timeout(self.transfer_timeout(len));
            if let Some(etag) = &if_match {
                // A signed header: exactly the value the service signed.
                req = req.header("if-match", etag.as_str());
            }
            let want_version = want_version.clone();
            async move {
                let resp = req
                    .send()
                    .await
                    .map_err(|source| BlobError::transport(OP, source))?;
                let resp = check(OP, resp).await?;
                let status = resp.status().as_u16();
                let version_hdr = header(&resp, "x-amz-version-id");
                let body = resp
                    .bytes()
                    .await
                    .map_err(|source| BlobError::transport(OP, source))?;
                if status != 206 && !(status == 200 && offset == 0) {
                    return Err(BlobError::BadResponse {
                        op: OP,
                        detail: format!("status {status} for a range read"),
                    });
                }
                if body.len() as u64 != len {
                    return Err(BlobError::BadResponse {
                        op: OP,
                        detail: format!("asked {len} bytes at {offset}, got {}", body.len()),
                    });
                }
                if let Some(want) = &want_version
                    && version_hdr.as_deref() != Some(want.as_str())
                {
                    return Err(BlobError::Integrity(format!(
                        "range read returned version {:?}, not {want}",
                        version_hdr
                    )));
                }
                Ok(body)
            }
        })
        .await
    }

    // ---- Get Blob Properties --------------------------------------------

    pub async fn get_properties(&self, sas_url: &str) -> Result<BlobProperties> {
        const OP: &str = "Get Blob Properties";
        let url = self.sas_url(sas_url)?;
        self.with_retry(OP, || {
            let req = self
                .request(Method::HEAD, url.clone())
                .timeout(self.opts.request_timeout);
            async move {
                let resp = req
                    .send()
                    .await
                    .map_err(|source| BlobError::transport(OP, source))?;
                let resp = check(OP, resp).await?;
                let size = header(&resp, "content-length")
                    .and_then(|v| v.parse().ok())
                    .ok_or_else(|| BlobError::BadResponse {
                        op: OP,
                        detail: "no Content-Length".into(),
                    })?;
                let metadata = resp
                    .headers()
                    .iter()
                    .filter_map(|(k, v)| {
                        let name = k.as_str().strip_prefix("x-ms-meta-")?;
                        Some((name.to_ascii_lowercase(), v.to_str().ok()?.to_owned()))
                    })
                    .collect();
                Ok(BlobProperties {
                    size,
                    etag: header(&resp, "etag"),
                    version_id: header(&resp, "x-ms-version-id"),
                    access_tier: header(&resp, "x-ms-access-tier"),
                    archive_status: header(&resp, "x-ms-archive-status"),
                    content_type: header(&resp, "content-type"),
                    metadata,
                })
            }
        })
        .await
    }

    // ---- Resumable, parallel upload --------------------------------------

    /// Upload a file as a block blob, resuming from `req.journal`.
    ///
    /// Blocks already recorded in the journal are not re-sent. Up to
    /// `concurrency` blocks are in flight. The whole-object CRC is combined
    /// from the block CRCs. If the journal already records a commit, nothing
    /// is sent and the recorded result is returned. The journal is left in
    /// place: delete it with [`UploadJournal::remove`] once the service has
    /// accepted `complete`.
    pub async fn upload_file(&self, req: &UploadRequest) -> Result<UploadOutcome> {
        self.upload_file_gated(req, &|| Box::pin(async { Ok(()) }))
            .await
    }

    /// [`upload_file`](Self::upload_file), with `gate` asked once every block
    /// is staged and before Put Block List: an `Err` stops the upload with
    /// nothing committed ([`BlobError::Integrity`]), so a source that changed
    /// at the last moment leaves only uncommitted blocks (which Azure drops
    /// after 7 days), never a committed blob nothing refers to.
    pub async fn upload_file_gated(
        &self,
        req: &UploadRequest,
        gate: &Gate<'_>,
    ) -> Result<UploadOutcome> {
        let plan = req.plan;
        let count = plan.count();
        if let Some(exp) = &req.expected_block_crcs
            && exp.len() != count as usize
        {
            return Err(BlobError::Invalid(format!(
                "{} expected block CRCs for a {count}-block plan",
                exp.len()
            )));
        }
        let meta = tokio::fs::metadata(&req.source).await?;
        if meta.len() != plan.size() {
            return Err(BlobError::Integrity(format!(
                "{} is {} bytes; the plan is for {}",
                req.source.display(),
                meta.len(),
                plan.size()
            )));
        }
        let target = strip_query(&req.sas_url)?;
        let journal_path = req.journal.clone();
        let (fp, size, bs) = (req.fingerprint.clone(), plan.size(), plan.block_size());
        let mut journal =
            blocking(move || UploadJournal::open(&journal_path, &target, size, bs, &fp)).await?;

        let mut reset_done = false;
        loop {
            if let Some(c) = journal.commit() {
                let crc = combined_crc(&plan, journal.done_blocks())?;
                return Ok(UploadOutcome {
                    etag: c.etag.clone(),
                    version_id: c.version_id.clone(),
                    crc64nvme: crc,
                    size: plan.size(),
                    blocks_uploaded: 0,
                    blocks_resumed: count,
                    blocks: committed_blocks(&plan, journal.tag(), journal.done_blocks())?,
                });
            }
            let resumed = journal.done_blocks().len() as u32;
            if let Some(exp) = &req.expected_block_crcs {
                for (&i, &crc) in journal.done_blocks() {
                    if exp[i as usize] != crc {
                        return Err(BlobError::Integrity(format!(
                            "journal block {i} CRC {crc:016x} != expected {:016x}",
                            exp[i as usize]
                        )));
                    }
                }
            }
            let uploaded = self.upload_missing_blocks(req, &mut journal).await?;
            gate()
                .await
                .map_err(|why| BlobError::Integrity(format!("before commit: {why}")))?;
            let tag = journal.tag();
            let ids: Vec<String> = (0..count).map(|i| block_id(tag, i)).collect();
            match self.put_block_list(&req.sas_url, &ids, &req.props).await {
                Ok(info) => {
                    let crc = combined_crc(&plan, journal.done_blocks())?;
                    journal.mark_committed(info.etag.clone(), info.version_id.clone())?;
                    return Ok(UploadOutcome {
                        etag: info.etag,
                        version_id: info.version_id,
                        crc64nvme: crc,
                        size: plan.size(),
                        blocks_uploaded: uploaded,
                        blocks_resumed: resumed,
                        blocks: committed_blocks(&plan, tag, journal.done_blocks())?,
                    });
                }
                Err(e) if e.code() == Some("InvalidBlockList") && !reset_done && resumed > 0 => {
                    // Staged blocks were discarded (7-day limit, or the blob
                    // was committed by someone else): start over once.
                    tracing::warn!(journal = %journal.path().display(), "staged blocks are gone; restarting upload");
                    journal.reset()?;
                    reset_done = true;
                }
                Err(e) => return Err(e),
            }
        }
    }

    async fn upload_missing_blocks(
        &self,
        req: &UploadRequest,
        journal: &mut UploadJournal,
    ) -> Result<u32> {
        let plan = req.plan;
        let tag = journal.tag();
        let todo: Vec<_> = plan
            .blocks()
            .filter(|b| !journal.done_blocks().contains_key(&b.index))
            .collect();
        let concurrency = self.opts.concurrency.max(1);
        let started = Instant::now();
        let mut sent_bytes: u64 = 0;
        let mut set: JoinSet<Result<(u32, u64)>> = JoinSet::new();
        let mut it = todo.into_iter();
        let mut uploaded = 0;
        let src = Arc::new(std::fs::File::open(&req.source)?);
        loop {
            while set.len() < concurrency {
                let Some(block) = it.next() else { break };
                if let Some(rate) = self.opts.max_upload_bytes_per_sec {
                    let due = Duration::from_secs_f64(sent_bytes as f64 / rate.max(1) as f64);
                    if let Some(wait) = due.checked_sub(started.elapsed()) {
                        tokio::time::sleep(wait).await;
                    }
                }
                sent_bytes += block.len;
                let this = self.clone();
                let url = req.sas_url.clone();
                let src = src.clone();
                let expected = req
                    .expected_block_crcs
                    .as_ref()
                    .map(|v| v[block.index as usize]);
                set.spawn(async move {
                    let (data, crc) = blocking(move || {
                        let data = read_exact_at(&src, block.offset, block.len)?;
                        let crc = crc64_nvme(&data);
                        Ok((data, crc))
                    })
                    .await?;
                    if let Some(want) = expected
                        && want != crc
                    {
                        return Err(BlobError::Integrity(format!(
                            "block {} CRC {crc:016x} != {want:016x}: the source changed",
                            block.index
                        )));
                    }
                    let id = block_id(tag, block.index);
                    this.with_retry("Put Block", || this.put_block(&url, &id, data.clone(), crc))
                        .await?;
                    Ok((block.index, crc))
                });
            }
            let Some(res) = set.join_next().await else {
                break;
            };
            let (index, crc) = match res {
                Ok(Ok(v)) => v,
                Ok(Err(e)) => {
                    set.abort_all();
                    return Err(e);
                }
                Err(join) => {
                    set.abort_all();
                    return Err(BlobError::Io(std::io::Error::other(join)));
                }
            };
            journal.mark_block(index, crc)?;
            uploaded += 1;
        }
        Ok(uploaded)
    }
}

fn combined_crc(plan: &BlockPlan, done: &BTreeMap<u32, u64>) -> Result<u64> {
    let mut c = Crc64Combiner::new();
    for b in plan.blocks() {
        let crc = done.get(&b.index).ok_or_else(|| {
            BlobError::Integrity(format!("block {} missing from the journal", b.index))
        })?;
        c.push(*crc, b.len);
    }
    Ok(c.crc())
}

/// The committed block list: ids, lengths and the CRCs sent with each block.
fn committed_blocks(
    plan: &BlockPlan,
    tag: u64,
    done: &BTreeMap<u32, u64>,
) -> Result<Vec<CommittedBlock>> {
    plan.blocks()
        .map(|b| {
            let crc = done.get(&b.index).ok_or_else(|| {
                BlobError::Integrity(format!("block {} missing from the journal", b.index))
            })?;
            Ok(CommittedBlock {
                id: block_id(tag, b.index),
                len: b.len,
                crc64nvme: *crc,
            })
        })
        .collect()
}

fn read_exact_at(f: &std::fs::File, offset: u64, len: u64) -> Result<Bytes> {
    use std::os::unix::fs::FileExt;
    let mut buf = vec![0u8; len as usize];
    f.read_exact_at(&mut buf, offset)?;
    Ok(Bytes::from(buf))
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> Result<T> + Send + 'static) -> Result<T> {
    tokio::task::spawn_blocking(f)
        .await
        .map_err(|e| BlobError::Io(std::io::Error::other(e)))?
}

impl BlobClient {
    /// Parse a SAS URL, refusing anything but `https` (unless the options
    /// allow `http` for the sim).
    fn sas_url(&self, s: &str) -> Result<Url> {
        let u = parse_url(s)?;
        crate::net::check_host(u.host_str()).map_err(BlobError::Invalid)?;
        match u.scheme() {
            "https" => Ok(u),
            "http" if self.opts.allow_http => Ok(u),
            other => Err(BlobError::Invalid(format!(
                "refusing a {other}:// blob URL: only https carries a SAS"
            ))),
        }
    }
}

fn parse_url(s: &str) -> Result<Url> {
    Url::parse(s).map_err(|e| BlobError::Invalid(format!("bad blob URL: {e}")))
}

/// The URL without its query: safe to log or store (no SAS).
pub fn strip_query(s: &str) -> Result<String> {
    let mut u = parse_url(s)?;
    u.set_query(None);
    Ok(u.to_string())
}

/// A presigned S3 GET as the daemon's ports carry it: the URL, and the
/// `If-Match` value its signature covers (the catalog's ETag; control API
/// §3 `s3_presigned_url.if_match`). With an `if_match` it travels as
/// [`PRESIGNED_PREFIX`] then JSON; without one it is the plain URL.
#[derive(Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct PresignedRead {
    pub url: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub if_match: Option<String>,
}

/// See [`PresignedRead`].
pub const PRESIGNED_PREFIX: &str = "s3ps:";

impl PresignedRead {
    pub fn encode(&self) -> String {
        match &self.if_match {
            None => self.url.clone(),
            Some(_) => format!(
                "{PRESIGNED_PREFIX}{}",
                serde_json::to_string(self).expect("serializes")
            ),
        }
    }

    pub fn decode(s: &str) -> Result<PresignedRead> {
        match s.strip_prefix(PRESIGNED_PREFIX) {
            Some(json) => serde_json::from_str(json)
                .map_err(|e| BlobError::Invalid(format!("bad presigned read: {e}"))),
            None => Ok(PresignedRead {
                url: s.to_owned(),
                if_match: None,
            }),
        }
    }

    /// True when the bytes are pinned: the URL names a version, or the GET
    /// carries a signed `If-Match`. An unpinned URL of an unversioned bucket
    /// could serve two objects across two range reads, so chunk reads
    /// refuse it (whole-file fills, checked against the CRC, still work).
    pub fn is_pinned(&self) -> bool {
        self.if_match
            .as_deref()
            .is_some_and(|e| !e.trim().is_empty())
            || Url::parse(&self.url).is_ok_and(|u| {
                u.query_pairs()
                    .any(|(k, v)| k == "versionId" && !v.is_empty() && v != "null")
            })
    }
}

impl std::fmt::Debug for PresignedRead {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // The signature is a secret: never in a log line.
        f.debug_struct("PresignedRead")
            .field(
                "url",
                &strip_query(&self.url).unwrap_or_else(|_| "<bad url>".into()),
            )
            .field("if_match", &self.if_match)
            .finish()
    }
}

fn header(resp: &Response, name: &str) -> Option<String> {
    resp.headers().get(name)?.to_str().ok().map(str::to_owned)
}

async fn check(op: &'static str, resp: Response) -> Result<Response> {
    let status = resp.status();
    if status.is_success() {
        return Ok(resp);
    }
    let code = header(&resp, "x-ms-error-code");
    let request_id = header(&resp, "x-ms-request-id");
    let retry_after = header(&resp, "retry-after")
        .and_then(|v| v.parse::<u64>().ok())
        .map(Duration::from_secs);
    let body = resp.text().await.unwrap_or_default();
    let message = xml_tag(&body, "Message")
        .unwrap_or_else(|| status.canonical_reason().unwrap_or("").to_owned());
    let code = code.or_else(|| xml_tag(&body, "Code"));
    Err(BlobError::Http {
        op,
        status: status.as_u16(),
        code,
        message,
        request_id,
        retry_after,
    })
}

fn xml_tag(body: &str, tag: &str) -> Option<String> {
    let open = format!("<{tag}>");
    let start = body.find(&open)? + open.len();
    let end = body[start..].find(&format!("</{tag}>"))? + start;
    Some(body[start..end].trim().to_owned())
}

fn is_csharp_identifier(s: &str) -> bool {
    let mut chars = s.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_alphabetic() || c == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

fn uuid_like() -> String {
    format!("{:016x}{:016x}", fastrand::u64(..), fastrand::u64(..))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn posix_meta_round_trip_and_names() {
        let m = PosixMeta {
            uid: 41023,
            gid: 100,
            mode: 0o100644,
        };
        let kv = m.to_metadata();
        assert!(kv.iter().all(|(k, _)| is_csharp_identifier(k)));
        assert_eq!(kv[2].1, "0644");
        let map: BTreeMap<_, _> = kv.into_iter().collect();
        let back = PosixMeta::from_metadata(&map).unwrap();
        assert_eq!(back.mode, 0o644);
        assert_eq!(back.uid, 41023);
    }

    #[test]
    fn identifiers() {
        assert!(is_csharp_identifier("file_owner"));
        assert!(!is_csharp_identifier("file-owner"));
        assert!(!is_csharp_identifier("1abc"));
        assert!(!is_csharp_identifier(""));
    }

    #[test]
    fn strip_query_drops_sas() {
        assert_eq!(
            strip_query("https://a.blob.core.windows.net/c/k?sv=1&sig=x").unwrap(),
            "https://a.blob.core.windows.net/c/k"
        );
    }

    #[test]
    fn backoff_bounded() {
        let p = RetryPolicy::default();
        for n in 1..40 {
            assert!(p.delay(n, None) <= p.max_delay);
        }
        assert!(p.delay(1, Some(Duration::from_secs(3))) >= Duration::from_secs(3));
    }

    #[test]
    fn xml_error_parse() {
        let b = "<?xml version=\"1.0\"?><Error><Code>Crc64Mismatch</Code><Message>bad</Message></Error>";
        assert_eq!(xml_tag(b, "Code").as_deref(), Some("Crc64Mismatch"));
    }
}
