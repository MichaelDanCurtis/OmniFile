//! The mutual-TLS client for the `api` app.
//!
//! Each method is **one attempt**: it never sleeps or retries, so a caller on
//! a latency-sensitive path (a recall for a waiting read) decides itself what
//! to do when the service is down ([`ControlError::is_unavailable`]). Durable,
//! retried work goes through [`crate::outbox`], [`crate::feed::FeedPoller`]
//! and [`crate::reads::ReadsBatcher`].

use std::path::PathBuf;
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, SystemTime};

use reqwest::{Method, Response, Url};
use serde::Serialize;
use serde::de::DeserializeOwned;

use omnifile_blob::tls::{self, TlsVersions};

use crate::creds::CredentialCache;
use crate::error::{ControlError, Result};
use crate::identity::{Identity, JsonBody, PENDING_RENEWAL, PendingCsr};
use crate::types::*;
use crate::util::{derived_idempotency_key, new_idempotency_key, parse_time};

/// Client-side time limits per call (control API §10 gives the server's).
#[derive(Clone, Debug)]
pub struct Timeouts {
    pub connect: Duration,
    /// Feed: `wait` plus this.
    pub feed_slack: Duration,
    /// `recalls`: the server budget is 2 s; a read is waiting.
    pub recall: Duration,
    /// `credentials`: the server budget is 1 s.
    pub credentials: Duration,
    pub default: Duration,
}

impl Default for Timeouts {
    fn default() -> Self {
        Self {
            connect: Duration::from_secs(5),
            feed_slack: Duration::from_secs(10),
            recall: Duration::from_secs(3),
            credentials: Duration::from_secs(3),
            default: Duration::from_secs(15),
        }
    }
}

#[derive(Clone, Debug)]
pub struct ControlConfig {
    /// Sent as `OmniFile-Edge-Version`.
    pub edge_version: String,
    pub tls_versions: TlsVersions,
    pub timeouts: Timeouts,
    /// Override the `api_url` stored at enrolment (tests, DNS changes).
    pub api_url_override: Option<String>,
    /// SAS cache size (entries).
    pub credential_cache_entries: usize,
}

impl Default for ControlConfig {
    fn default() -> Self {
        Self {
            edge_version: env!("CARGO_PKG_VERSION").to_owned(),
            tls_versions: TlsVersions::default(),
            timeouts: Timeouts::default(),
            api_url_override: None,
            credential_cache_entries: 10_000,
        }
    }
}

struct Inner {
    cfg: ControlConfig,
    base: Url,
    edge_id: u64,
    identity: RwLock<Identity>,
    http: RwLock<reqwest::Client>,
    creds: Mutex<CredentialCache>,
}

/// The edge's control-API client. Cheap to clone.
#[derive(Clone)]
pub struct ControlClient {
    inner: Arc<Inner>,
}

impl std::fmt::Debug for ControlClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ControlClient")
            .field("edge_id", &self.inner.edge_id)
            .field("base", &self.inner.base.as_str())
            .finish()
    }
}

/// Result of `stub/prepare`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StubPrepare {
    Lease(StubLease),
    /// `409 stub_not_allowed` or `version_conflict`: report `skip`.
    Refused {
        code: String,
        detail: Option<String>,
    },
}

impl ControlClient {
    /// A client for the identity enrolled in `identity_dir`.
    pub fn open(identity_dir: impl Into<PathBuf>, cfg: ControlConfig) -> Result<Self> {
        let identity = Identity::load(&identity_dir.into())?;
        Self::from_identity(identity, cfg)
    }

    pub fn from_identity(identity: Identity, cfg: ControlConfig) -> Result<Self> {
        let base_str = cfg
            .api_url_override
            .clone()
            .unwrap_or_else(|| identity.info.api_url.clone());
        let base = Url::parse(base_str.trim_end_matches('/'))
            .map_err(|e| ControlError::Invalid(format!("api url {base_str:?}: {e}")))?;
        let http = build_http(&identity, &cfg)?;
        let cache = CredentialCache::new(cfg.credential_cache_entries);
        Ok(Self {
            inner: Arc::new(Inner {
                edge_id: identity.info.edge_id,
                base,
                cfg,
                identity: RwLock::new(identity),
                http: RwLock::new(http),
                creds: Mutex::new(cache),
            }),
        })
    }

    pub fn edge_id(&self) -> u64 {
        self.inner.edge_id
    }

    pub fn identity(&self) -> Identity {
        self.inner.identity.read().expect("identity lock").clone()
    }

    fn http(&self) -> reqwest::Client {
        self.inner.http.read().expect("http lock").clone()
    }

    fn url(&self, path: &str) -> Url {
        let mut u = self.inner.base.clone();
        let base_path = u.path().trim_end_matches('/').to_owned();
        u.set_path(&format!(
            "{base_path}/v0/edges/{}{path}",
            self.inner.edge_id
        ));
        u
    }

    async fn send<B: Serialize>(
        &self,
        op: &'static str,
        method: Method,
        url: Url,
        body: Option<&B>,
        idempotency_key: Option<&str>,
        timeout: Duration,
    ) -> Result<Response> {
        let mut req = self
            .http()
            .request(method, url)
            .header("OmniFile-Edge-Version", &self.inner.cfg.edge_version)
            .header(reqwest::header::ACCEPT, "application/json")
            .timeout(timeout);
        if let Some(k) = idempotency_key {
            req = req.header("Idempotency-Key", k);
        }
        if let Some(b) = body {
            req = req.json_body(b)?;
        }
        req.send().await.map_err(|e| transport_error(op, e))
    }

    async fn post_json<B: Serialize, R: DeserializeOwned>(
        &self,
        op: &'static str,
        path: &str,
        body: &B,
        key: &str,
        timeout: Duration,
    ) -> Result<R> {
        let resp = self
            .send(
                op,
                Method::POST,
                self.url(path),
                Some(body),
                Some(key),
                timeout,
            )
            .await?;
        decode(op, resp).await
    }

    async fn post_empty<B: Serialize>(
        &self,
        op: &'static str,
        path: &str,
        body: Option<&B>,
        key: &str,
    ) -> Result<()> {
        let resp = self
            .send(
                op,
                Method::POST,
                self.url(path),
                body,
                Some(key),
                self.inner.cfg.timeouts.default,
            )
            .await?;
        check(op, resp).await.map(|_| ())
    }

    // ---- ops ---------------------------------------------------------------

    pub async fn heartbeat(&self, req: &HeartbeatRequest) -> Result<HeartbeatResponse> {
        self.post_json(
            "heartbeat",
            "/heartbeat",
            req,
            &new_idempotency_key(),
            self.inner.cfg.timeouts.default,
        )
        .await
    }

    // ---- feed --------------------------------------------------------------

    /// One page of the feed. With `wait_s > 0` the service holds the request
    /// up to that long when there is nothing new.
    pub async fn feed(&self, after: &str, limit: u32, wait_s: u32) -> Result<FeedPage> {
        const OP: &str = "feed";
        let mut url = self.url("/feed");
        url.query_pairs_mut()
            .append_pair("after", after)
            .append_pair("limit", &limit.clamp(1, 1000).to_string())
            .append_pair("wait", &wait_s.min(30).to_string());
        let timeout =
            Duration::from_secs(u64::from(wait_s.min(30))) + self.inner.cfg.timeouts.feed_slack;
        let resp = self
            .send::<()>(OP, Method::GET, url, None, None, timeout)
            .await?;
        decode(OP, resp).await
    }

    /// A directory's entries as they were at `t` (rewind trees, section 4.5).
    /// Entries are raw `FileRow` JSON; page with the last name as `after`.
    pub async fn asof(
        &self,
        dir_id: u64,
        t: &str,
        after: Option<&str>,
        limit: u32,
    ) -> Result<AsofPage> {
        const OP: &str = "asof";
        let mut url = self.url("/asof");
        url.query_pairs_mut()
            .append_pair("dir_id", &dir_id.to_string())
            .append_pair("t", t)
            .append_pair("limit", &limit.clamp(1, 10_000).to_string());
        if let Some(a) = after {
            url.query_pairs_mut().append_pair("after", a);
        }
        let resp = self
            .send::<()>(
                OP,
                Method::GET,
                url,
                None,
                None,
                self.inner.cfg.timeouts.default,
            )
            .await?;
        decode(OP, resp).await
    }

    /// One page of a dataset's snapshot, files after `after_file_id`
    /// (`control-api.md` §6).
    pub async fn snapshot(
        &self,
        dataset_id: u64,
        after_file_id: Option<u64>,
        limit: u32,
    ) -> Result<SnapshotPage> {
        const OP: &str = "snapshot";
        let mut url = self.url("/snapshot");
        url.query_pairs_mut()
            .append_pair("dataset_id", &dataset_id.to_string())
            .append_pair("limit", &limit.clamp(1, 10_000).to_string());
        if let Some(a) = after_file_id {
            url.query_pairs_mut()
                .append_pair("after_file_id", &a.to_string());
        }
        let resp = self
            .send::<()>(
                OP,
                Method::GET,
                url,
                None,
                None,
                self.inner.cfg.timeouts.default,
            )
            .await?;
        decode(OP, resp).await
    }

    /// `POST …/snapshot-exports`: start a bulk export of a dataset
    /// (`control-api.md` §6). `nonce` makes one attempt's retries
    /// idempotent; a new attempt passes a new one.
    pub async fn snapshot_export_start(
        &self,
        dataset_id: u64,
        nonce: &str,
    ) -> Result<SnapshotExport> {
        let body = serde_json::json!({"dataset_id": dataset_id.to_string()});
        let key = derived_idempotency_key("snapshot-exports", &format!("{dataset_id}:{nonce}"));
        self.post_json(
            "snapshot-exports",
            "/snapshot-exports",
            &body,
            &key,
            self.inner.cfg.timeouts.default,
        )
        .await
    }

    /// `POST …/audit` (`control-api.md` §4.7): a batch of audit records.
    /// The key derives from the batch, so a resend is idempotent.
    pub async fn audit(&self, records: &[serde_json::Value]) -> Result<()> {
        let body = serde_json::json!({ "records": records });
        let key = derived_idempotency_key(
            "audit",
            &crate::util::sha256_hex(body.to_string().as_bytes()),
        );
        self.post_empty("audit", "/audit", Some(&body), &key).await
    }

    // ---- annexes (§4.8) --------------------------------------------------------

    /// `GET …/annexes`: the annexes this edge's annex units run.
    pub async fn annexes(&self) -> Result<crate::annex::AnnexList> {
        const OP: &str = "annexes";
        let resp = self
            .send::<()>(
                OP,
                Method::GET,
                self.url("/annexes"),
                None,
                None,
                self.inner.cfg.timeouts.default,
            )
            .await?;
        decode(OP, resp).await
    }

    /// `POST …/annex-events`: one annex's events, in order. `batch_key` names
    /// the batch (its first and last outbox sequence numbers), so a resend
    /// after a restart is a replay answered from the service's store.
    pub async fn annex_events(
        &self,
        annex_id: u64,
        events: &[serde_json::Value],
        batch_key: &str,
    ) -> Result<crate::annex::AnnexEventResults> {
        let body = serde_json::json!({ "annex_id": annex_id.to_string(), "events": events });
        let key = derived_idempotency_key("annex-events", &format!("{annex_id}:{batch_key}"));
        self.post_json(
            "annex-events",
            "/annex-events",
            &body,
            &key,
            self.inner.cfg.timeouts.default,
        )
        .await
    }

    /// `GET …/annex-dry-runs`: the dry runs waiting for this edge's annexes.
    pub async fn annex_dry_runs(&self, annex_id: u64) -> Result<crate::annex::AnnexDryRuns> {
        const OP: &str = "annex-dry-runs";
        let mut url = self.url("/annex-dry-runs");
        url.query_pairs_mut()
            .append_pair("annex_id", &annex_id.to_string());
        let resp = self
            .send::<()>(
                OP,
                Method::GET,
                url,
                None,
                None,
                self.inner.cfg.timeouts.default,
            )
            .await?;
        decode(OP, resp).await
    }

    /// `POST …/annex-dry-runs/{run_id}`: a dry run's report, or why it
    /// couldn't run. Idempotent: a run is answered once.
    pub async fn annex_dry_run_report(
        &self,
        run_id: u64,
        body: &serde_json::Value,
    ) -> Result<serde_json::Value> {
        let key = dry_run_key(run_id, body);
        self.post_json(
            "annex-dry-run",
            &format!("/annex-dry-runs/{run_id}"),
            body,
            &key,
            self.inner.cfg.timeouts.default,
        )
        .await
    }

    /// `POST …/annex-removal-tickets`: a ticket per candidate, or why not
    /// (phase A2.2). `ask` is unique per question: a "not yet" must be asked
    /// again afresh, never answered from the first reply (the service gives
    /// a file one ticket however often it is asked).
    pub async fn annex_removal_tickets(
        &self,
        body: &serde_json::Value,
        ask: &str,
    ) -> Result<serde_json::Value> {
        let key = removal_tickets_key(body, ask);
        self.post_json(
            "annex-removal-tickets",
            "/annex-removal-tickets",
            body,
            &key,
            self.inner.cfg.timeouts.default,
        )
        .await
    }

    /// `POST …/annex-placement-tickets` (phase A3.3): tickets for the ready
    /// placements on this annex's filer. Each ask is its own request.
    pub async fn annex_placement_tickets(
        &self,
        body: &serde_json::Value,
    ) -> Result<serde_json::Value> {
        self.post_json(
            "annex-placement-tickets",
            "/annex-placement-tickets",
            body,
            &new_idempotency_key(),
            self.inner.cfg.timeouts.default,
        )
        .await
    }

    /// `POST …/annex-placements/{placement_id}`: ask `go` before P4, or report
    /// a step. `ask` is unique per question; a report's key is the step's own.
    pub async fn annex_placement_step(
        &self,
        placement_id: u64,
        body: &serde_json::Value,
        ask: &str,
    ) -> Result<serde_json::Value> {
        let key = derived_idempotency_key("annex-placement", &format!("{placement_id}:{ask}"));
        self.post_json(
            "annex-placement",
            &format!("/annex-placements/{placement_id}"),
            body,
            &key,
            self.inner.cfg.timeouts.default,
        )
        .await
    }

    /// `POST …/annex-removals/{removal_id}`: ask before a write (`hide`,
    /// `delete`), or report a step taken. `ask` is unique per question, so
    /// a delete asked again after a pause is answered afresh, never replayed;
    /// a report's key is the step's own.
    pub async fn annex_removal_step(
        &self,
        removal_id: u64,
        body: &serde_json::Value,
        ask: &str,
    ) -> Result<serde_json::Value> {
        let step = body.get("step").and_then(|s| s.as_str()).unwrap_or("");
        let key = derived_idempotency_key("annex-removal", &format!("{removal_id}:{step}:{ask}"));
        self.post_json(
            "annex-removal",
            &format!("/annex-removals/{removal_id}"),
            body,
            &key,
            self.inner.cfg.timeouts.default,
        )
        .await
    }

    /// `GET …/annexes/{annex_id}/records`: one page of the catalog's record
    /// of the annex's live files, in path order, after `after`.
    pub async fn annex_records(
        &self,
        annex_id: u64,
        after: &str,
        limit: u32,
    ) -> Result<crate::annex::AnnexRecords> {
        const OP: &str = "annex-records";
        let mut url = self.url(&format!("/annexes/{annex_id}/records"));
        url.query_pairs_mut()
            .append_pair("after", after)
            .append_pair("limit", &limit.clamp(1, 5000).to_string());
        let resp = self
            .send::<()>(
                OP,
                Method::GET,
                url,
                None,
                None,
                self.inner.cfg.timeouts.default,
            )
            .await?;
        decode(OP, resp).await
    }

    /// `GET …/digest`: the digest of a dataset's live rows, or of one
    /// directory's children (`control-api.md` §6).
    pub async fn digest(&self, dataset_id: u64, dir_id: Option<u64>) -> Result<Digest> {
        const OP: &str = "digest";
        let mut url = self.url("/digest");
        url.query_pairs_mut()
            .append_pair("dataset_id", &dataset_id.to_string());
        if let Some(d) = dir_id {
            url.query_pairs_mut().append_pair("dir_id", &d.to_string());
        }
        let resp = self
            .send::<()>(
                OP,
                Method::GET,
                url,
                None,
                None,
                self.inner.cfg.timeouts.default,
            )
            .await?;
        decode(OP, resp).await
    }

    /// `GET …/snapshot-exports/{export_id}`.
    pub async fn snapshot_export(&self, export_id: u64) -> Result<SnapshotExport> {
        const OP: &str = "snapshot-exports/get";
        let resp = self
            .send::<()>(
                OP,
                Method::GET,
                self.url(&format!("/snapshot-exports/{export_id}")),
                None,
                None,
                self.inner.cfg.timeouts.default,
            )
            .await?;
        decode(OP, resp).await
    }

    pub async fn feed_ack(&self, cursor: &str) -> Result<()> {
        let body = FeedAck {
            cursor: cursor.to_owned(),
        };
        self.post_empty("feed/ack", "/feed/ack", Some(&body), &new_idempotency_key())
            .await
    }

    // ---- namespace ---------------------------------------------------------

    /// Send an ordered batch of namespace ops. `batch_key` is the batch's
    /// idempotency key: reuse it when resending the same batch.
    pub async fn changes(&self, ops: &[ChangeOp], batch_key: &str) -> Result<Vec<ChangeResult>> {
        let body = ChangesRequest { ops: ops.to_vec() };
        let r: ChangesResponse = self
            .post_json(
                "changes",
                "/changes",
                &body,
                batch_key,
                self.inner.cfg.timeouts.default,
            )
            .await?;
        Ok(r.results)
    }

    // ---- uploads -------------------------------------------------------------

    /// Upload intent for an outbox `commit` op. The idempotency key derives
    /// from `op_id`, so a resend after a restart is a replay.
    pub async fn upload_start(&self, intent: &UploadIntent) -> Result<UploadStart> {
        let key = derived_idempotency_key("uploads", &intent.op_id);
        self.post_json(
            "uploads",
            "/uploads",
            intent,
            &key,
            self.inner.cfg.timeouts.default,
        )
        .await
    }

    pub async fn upload_complete(
        &self,
        upload_id: u64,
        body: &UploadComplete,
    ) -> Result<UploadCommitted> {
        let key = derived_idempotency_key("uploads/complete", &upload_id.to_string());
        self.post_json(
            "uploads/complete",
            &format!("/uploads/{upload_id}/complete"),
            body,
            &key,
            self.inner.cfg.timeouts.default,
        )
        .await
    }

    pub async fn upload_abort(&self, upload_id: u64) -> Result<()> {
        let key = derived_idempotency_key("uploads/abort", &upload_id.to_string());
        self.post_empty::<()>(
            "uploads/abort",
            &format!("/uploads/{upload_id}/abort"),
            None,
            &key,
        )
        .await
    }

    // ---- credentials ---------------------------------------------------------

    /// Mint a credential (no cache).
    pub async fn mint_credential(&self, req: &CredentialRequest) -> Result<Credential> {
        self.post_json(
            "credentials",
            "/credentials",
            req,
            &new_idempotency_key(),
            self.inner.cfg.timeouts.credentials,
        )
        .await
    }

    /// A read credential for one content version, reused until its
    /// `cache_until`. While the service is unreachable, a cached credential
    /// keeps reads of cloud-resident files going (control API §11).
    pub async fn read_credential(&self, file_id: u64, version_id: u64) -> Result<Credential> {
        let now = SystemTime::now();
        if let Some(c) = self.cached_credential(file_id, version_id, now) {
            return Ok(c);
        }
        let c = self
            .mint_credential(&CredentialRequest::Read {
                file_id,
                version_id,
            })
            .await?;
        self.remember_credential(file_id, version_id, c.clone());
        Ok(c)
    }

    pub fn cached_credential(
        &self,
        file_id: u64,
        version_id: u64,
        now: SystemTime,
    ) -> Option<Credential> {
        self.inner
            .creds
            .lock()
            .expect("credential cache lock")
            .get(file_id, version_id, now)
    }

    /// Cache a credential that arrived another way (a recall's `online`
    /// answer, a `recall.state` event).
    pub fn remember_credential(&self, file_id: u64, version_id: u64, c: Credential) {
        self.inner
            .creds
            .lock()
            .expect("credential cache lock")
            .insert(file_id, version_id, c, SystemTime::now());
    }

    /// Drop a cached credential (Azure refused it).
    pub fn forget_credential(&self, file_id: u64, version_id: u64) {
        self.inner
            .creds
            .lock()
            .expect("credential cache lock")
            .remove(file_id, version_id);
    }

    // ---- stubs ---------------------------------------------------------------

    pub async fn stub_prepare(&self, file_id: u64, version_id: u64) -> Result<StubPrepare> {
        const OP: &str = "stub/prepare";
        let key = new_idempotency_key();
        match self
            .post_json::<_, StubLease>(
                OP,
                &format!("/files/{file_id}/stub/prepare"),
                &StubPrepareRequest { version_id },
                &key,
                self.inner.cfg.timeouts.default,
            )
            .await
        {
            Ok(lease) => Ok(StubPrepare::Lease(lease)),
            Err(ControlError::Problem { problem, .. })
                if problem.status == 409
                    && matches!(
                        problem.code.as_str(),
                        "stub_not_allowed" | "version_conflict"
                    ) =>
            {
                Ok(StubPrepare::Refused {
                    code: problem.code,
                    detail: problem.detail,
                })
            }
            Err(e) => Err(e),
        }
    }

    /// Idempotent by `lease_id`.
    pub async fn stub_commit(
        &self,
        file_id: u64,
        lease_id: &str,
        version_id: u64,
        resident_bytes: u64,
    ) -> Result<()> {
        let key = derived_idempotency_key("stub/commit", lease_id);
        let body = StubCommitRequest {
            lease_id: lease_id.to_owned(),
            version_id,
            resident_bytes,
        };
        self.post_empty(
            "stub/commit",
            &format!("/files/{file_id}/stub/commit"),
            Some(&body),
            &key,
        )
        .await
    }

    pub async fn stub_skip(
        &self,
        file_id: u64,
        version_id: u64,
        reason: StubSkipReason,
        detail: Option<&str>,
    ) -> Result<()> {
        let body = StubSkipRequest {
            version_id,
            reason,
            detail: detail.map(str::to_owned),
        };
        self.post_empty(
            "stub/skip",
            &format!("/files/{file_id}/stub/skip"),
            Some(&body),
            &new_idempotency_key(),
        )
        .await
    }

    // ---- recalls -------------------------------------------------------------

    /// A read reached non-local bytes. One attempt with a short timeout: on
    /// [`ControlError::is_unavailable`] the daemon answers from the share's
    /// policy (DELAY or EIO) and journals the request.
    pub async fn recall_start(&self, req: &RecallRequest) -> Result<RecallResponse> {
        let resp: RecallResponse = self
            .post_json(
                "recalls",
                "/recalls",
                req,
                &new_idempotency_key(),
                self.inner.cfg.timeouts.recall,
            )
            .await?;
        if let RecallResponse::Online { credential } = &resp {
            self.remember_credential(req.file_id, req.version_id, credential.clone());
        }
        Ok(resp)
    }

    /// Idempotent by `recall_id`.
    pub async fn recall_filled(
        &self,
        recall_id: u64,
        version_id: u64,
        resident_bytes: u64,
    ) -> Result<()> {
        let key = derived_idempotency_key("recalls/filled", &recall_id.to_string());
        let body = RecallFilledRequest {
            version_id,
            resident_bytes,
        };
        self.post_empty(
            "recalls/filled",
            &format!("/recalls/{recall_id}/filled"),
            Some(&body),
            &key,
        )
        .await
    }

    /// `POST …/recalls/{recall_id}/failed`: a `downloading` recall the edge
    /// could not fill becomes `failed`, with `reason` (at most 500 chars).
    pub async fn recall_failed(&self, recall_id: u64, version_id: u64, reason: &str) -> Result<()> {
        let key = derived_idempotency_key("recalls/failed", &recall_id.to_string());
        let reason: String = reason.chars().take(500).collect();
        let body = crate::types::RecallFailedRequest { version_id, reason };
        self.post_empty(
            "recalls/failed",
            &format!("/recalls/{recall_id}/failed"),
            Some(&body),
            &key,
        )
        .await
    }

    /// An online fetch (no recall) filled a stub (control API §4.4).
    /// Idempotent by `(file_id, version_id, resident_bytes)`.
    pub async fn file_filled(
        &self,
        file_id: u64,
        version_id: u64,
        resident_bytes: u64,
    ) -> Result<()> {
        let key = derived_idempotency_key(
            "files/filled",
            &format!("{file_id}.{version_id}.{resident_bytes}"),
        );
        let body = RecallFilledRequest {
            version_id,
            resident_bytes,
        };
        self.post_empty(
            "files/filled",
            &format!("/files/{file_id}/filled"),
            Some(&body),
            &key,
        )
        .await
    }

    // ---- ACLs (§4.6) -----------------------------------------------------------

    /// `POST …/acls`: store ACL bodies (the edge's canonical snapshots,
    /// `omnifile_acl::AclSnapshot` as JSON) for `dataset_id`. Returns
    /// `sha256 → acl_id`. Sending a known ACL again is harmless.
    pub async fn put_acls(
        &self,
        dataset_id: u64,
        acls: &[serde_json::Value],
    ) -> Result<std::collections::BTreeMap<String, String>> {
        #[derive(serde::Deserialize)]
        struct Resp {
            acl_ids: std::collections::BTreeMap<String, String>,
        }
        let body = serde_json::json!({"dataset_id": dataset_id.to_string(), "acls": acls});
        let mut shas: Vec<&str> = acls.iter().filter_map(|a| a["sha256"].as_str()).collect();
        shas.sort_unstable();
        let key = derived_idempotency_key("acls", &format!("{dataset_id}:{}", shas.join(",")));
        let r: Resp = self
            .post_json(
                "acls",
                "/acls",
                &body,
                &key,
                self.inner.cfg.timeouts.default,
            )
            .await?;
        Ok(r.acl_ids)
    }

    /// `POST …/acl-scans` `{dataset_id, kind}` → `scan_id`. `kind`: `full`
    /// or `on_change`. `nonce` makes each scan its own request.
    pub async fn acl_scan_start(&self, dataset_id: u64, kind: &str, nonce: &str) -> Result<u64> {
        #[derive(serde::Deserialize)]
        struct Resp {
            #[serde(with = "crate::types::id")]
            scan_id: u64,
        }
        let body = serde_json::json!({"dataset_id": dataset_id.to_string(), "kind": kind});
        let key = derived_idempotency_key("acl-scans", &format!("{dataset_id}:{kind}:{nonce}"));
        let r: Resp = self
            .post_json(
                "acl-scans",
                "/acl-scans",
                &body,
                &key,
                self.inner.cfg.timeouts.default,
            )
            .await?;
        Ok(r.scan_id)
    }

    /// `POST …/acl-scans/{scan_id}/complete`. Idempotent by `scan_id`.
    pub async fn acl_scan_complete(&self, scan_id: u64, seen: u64, changed: u64) -> Result<()> {
        let body = serde_json::json!({"entries_seen": seen, "entries_changed": changed});
        let key = derived_idempotency_key("acl-scans/complete", &scan_id.to_string());
        self.post_empty(
            "acl-scans/complete",
            &format!("/acl-scans/{scan_id}/complete"),
            Some(&body),
            &key,
        )
        .await
    }

    // ---- reads ---------------------------------------------------------------

    pub async fn report_reads(&self, reads: &[ReadEntry], key: &str) -> Result<ReadsResponse> {
        let body = ReadsRequest {
            reads: reads.to_vec(),
        };
        self.post_json(
            "reads",
            "/reads",
            &body,
            key,
            self.inner.cfg.timeouts.default,
        )
        .await
    }

    // ---- certificate renewal -------------------------------------------------

    /// Renew the client certificate with a new key, authenticated by the
    /// current one. The new key, CSR and idempotency key are persisted first,
    /// so a lost response is retried with the same request and the service
    /// returns the same certificate. On success the new identity replaces
    /// the old one on disk and in this client.
    pub async fn renew_certificate(&self) -> Result<Identity> {
        const OP: &str = "certificate";
        let current = self.identity();
        let pending_path = current.dir.join(PENDING_RENEWAL);
        // Sealed files go through systemd-creds, and so the TPM: off the
        // async workers.
        let (p, sha) = (pending_path.clone(), current.cert_sha256()?);
        let mut pending = blocking(move || PendingCsr::load_or_new(&p, &sha)).await?;
        // The proof is made once and persisted with the CSR: a retry resends
        // the identical body under the same Idempotency-Key.
        let proof = match &pending.proof {
            Some(p) => p.clone(),
            None => {
                let p = crate::identity::renewal_proof(&current, &pending.csr_pem)?;
                pending.proof = Some(p.clone());
                let (save, path) = (pending.clone(), pending_path.clone());
                blocking(move || save.save(&path)).await?;
                p
            }
        };
        let sent = self
            .post_json::<_, IssuedCertificate>(
                OP,
                "/certificate",
                &RenewRequest {
                    csr_pem: pending.csr_pem.clone(),
                    proof,
                },
                &pending.idempotency_key,
                self.inner.cfg.timeouts.default,
            )
            .await;
        let issued = match sent {
            Ok(i) => i,
            Err(e) => {
                // A proof the service refused (too old, after a long outage)
                // can't be resent: start over with a new key, CSR and key.
                if e.code() == Some("certificate_invalid") {
                    PendingCsr::remove(&pending_path);
                }
                return Err(e);
            }
        };
        let mut issued = issued;
        if issued.edge_id == 0 {
            issued.edge_id = current.info.edge_id;
        }
        if issued.edge_id != current.info.edge_id {
            return Err(ControlError::Identity(format!(
                "renewed certificate is for edge {}, not {}",
                issued.edge_id, current.info.edge_id
            )));
        }
        let (cur, key) = (current.clone(), pending.key_pem.clone());
        let identity = blocking(move || {
            Identity::store(
                &cur.dir,
                &issued,
                &key,
                &cur.info.api_url,
                &cur.server_ca_pem,
                cur.info.api_server_ca_sha256.as_deref(),
            )
        })
        .await?;
        let http = build_http(&identity, &self.inner.cfg)?;
        *self.inner.http.write().expect("http lock") = http;
        *self.inner.identity.write().expect("identity lock") = identity.clone();
        PendingCsr::remove(&pending_path);
        tracing::info!(not_after = %identity.info.not_after, "client certificate renewed");
        Ok(identity)
    }

    /// Renew if due (day 20 of 30, the service's `certificate_renew_after`,
    /// or at once for a key that was on disk in the clear).
    pub async fn renew_if_due(&self, renew_after: Option<&str>) -> Result<bool> {
        let now = SystemTime::now();
        let id = self.identity();
        let due = id.renewal_due(now) || renew_after.and_then(parse_time).is_some_and(|t| now >= t);
        if !due {
            return Ok(false);
        }
        if id.expired(now) {
            return Err(ControlError::Identity(
                "the client certificate has expired; a new enrolment bundle is needed".into(),
            ));
        }
        self.renew_certificate().await.map(|_| true)
    }

    // ---- IAM Roles Anywhere certificate (decision 50) ------------------------

    /// `POST …/aws-certificate`: a Roles Anywhere certificate from the edge CA
    /// for the AWS key in `csr_pem` (its self-signature proves the key), with
    /// a proof by the current mTLS key. A new idempotency key per call: a
    /// retry signs a new proof, and an extra certificate for the same key is
    /// harmless. The answer is checked: this edge, and the request's key.
    pub async fn roles_anywhere_certificate(
        &self,
        csr_pem: &str,
    ) -> Result<RolesAnywhereCertificate> {
        let current = self.identity();
        let proof = crate::identity::signed_proof(
            &current,
            csr_pem,
            crate::types::ROLES_ANYWHERE_PROOF_CONTEXT,
        )?;
        let mut issued: RolesAnywhereCertificate = self
            .post_json(
                "aws-certificate",
                "/aws-certificate",
                &RolesAnywhereRequest {
                    csr_pem: csr_pem.to_owned(),
                    proof,
                },
                &new_idempotency_key(),
                self.inner.cfg.timeouts.default,
            )
            .await?;
        if issued.edge_id == 0 {
            issued.edge_id = current.info.edge_id;
        }
        if issued.edge_id != current.info.edge_id {
            return Err(ControlError::Identity(format!(
                "Roles Anywhere certificate is for edge {}, not {}",
                issued.edge_id, current.info.edge_id
            )));
        }
        crate::roles_anywhere::check_for_request(csr_pem, &issued.cert_pem)?;
        Ok(issued)
    }

    /// Requests or renews the Roles Anywhere certificate when OmniFile's edge
    /// CA issues it (`files` holds the wizard's request) and it is due: none
    /// yet, past its `renew_after`, or the mTLS certificate was renewed since
    /// (so the two renew together), or `force`. `true` if a new one was
    /// installed; the caller restarts the signing helper.
    pub async fn renew_roles_anywhere_if_due(
        &self,
        files: &crate::roles_anywhere::RolesAnywhereFiles,
        force: bool,
    ) -> Result<bool> {
        if !files.enabled() {
            return Ok(false);
        }
        let mtls_not_after = self.identity().info.not_after;
        if !force && !files.due(SystemTime::now(), &mtls_not_after) {
            return Ok(false);
        }
        let csr = files.csr_pem()?;
        let issued = self.roles_anywhere_certificate(&csr).await?;
        let (f, cur) = (files.clone(), mtls_not_after);
        blocking(move || f.install(&issued, &cur)).await?;
        tracing::info!("Roles Anywhere certificate installed");
        Ok(true)
    }
}

fn build_http(identity: &Identity, cfg: &ControlConfig) -> Result<reqwest::Client> {
    let config = identity.client_config(cfg.tls_versions)?;
    tls::http_client(config, cfg.timeouts.connect)
        .map_err(|e| ControlError::Identity(e.to_string()))
}

pub(crate) fn transport_error(op: &'static str, e: reqwest::Error) -> ControlError {
    // Connection refused, DNS, TLS handshake, timeouts: the service is not
    // reachable from here, whatever the reason.
    ControlError::Unavailable {
        op,
        detail: e.to_string(),
        retry_after: None,
    }
}

async fn check(op: &'static str, resp: Response) -> Result<Response> {
    let status = resp.status();
    if status.is_success() {
        return Ok(resp);
    }
    let retry_after = resp
        .headers()
        .get(reqwest::header::RETRY_AFTER)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok());
    let text = resp.text().await.unwrap_or_default();
    match serde_json::from_str::<Problem>(&text) {
        Ok(mut problem) => {
            if problem.retry_after_s.is_none() {
                problem.retry_after_s = retry_after;
            }
            Err(ControlError::Problem {
                op,
                problem: Box::new(problem),
            })
        }
        Err(_) if matches!(status.as_u16(), 502..=504) => Err(ControlError::Unavailable {
            op,
            detail: format!("HTTP {status}"),
            retry_after: retry_after.map(Duration::from_secs),
        }),
        Err(_) => Err(ControlError::Problem {
            op,
            problem: Box::new(Problem {
                type_: String::new(),
                title: status.canonical_reason().unwrap_or("").to_owned(),
                status: status.as_u16(),
                code: "internal".into(),
                detail: Some(text.chars().take(500).collect()),
                retryable: status.is_server_error(),
                retry_after_s: retry_after,
                request_id: None,
            }),
        }),
    }
}

pub(crate) async fn decode<R: DeserializeOwned>(op: &'static str, resp: Response) -> Result<R> {
    let resp = check(op, resp).await?;
    let bytes = resp.bytes().await.map_err(|e| transport_error(op, e))?;
    serde_json::from_slice(&bytes).map_err(|e| ControlError::Decode {
        op,
        detail: e.to_string(),
    })
}

/// Runs `f` on the blocking pool (sealed keys: `systemd-creds`, the TPM).
async fn blocking<T: Send + 'static>(f: impl FnOnce() -> Result<T> + Send + 'static) -> Result<T> {
    tokio::task::spawn_blocking(f)
        .await
        .map_err(|e| ControlError::Identity(format!("key I/O task: {e}")))?
}

/// One idempotency key per run and step: taking a run (`running`) and
/// answering it (`done`, `failed`) are different requests, and one key for
/// both would answer the report with the replay of "running".
pub fn dry_run_key(run_id: u64, body: &serde_json::Value) -> String {
    let state = body.get("state").and_then(|s| s.as_str()).unwrap_or("");
    derived_idempotency_key("annex-dry-run", &format!("{run_id}:{state}"))
}

#[cfg(test)]
mod dry_run_key_tests {
    use super::dry_run_key;
    use serde_json::json;

    #[test]
    fn taking_and_answering_a_dry_run_are_different_requests() {
        let taken = dry_run_key(9, &json!({"state": "running"}));
        let done = dry_run_key(9, &json!({"state": "done", "report": {}}));
        assert_ne!(taken, done);
        assert_eq!(
            done,
            dry_run_key(9, &json!({"state": "done", "report": {"x": 1}}))
        );
        assert_ne!(done, dry_run_key(10, &json!({"state": "done"})));
    }
}

/// A ticket request's key: the candidates and the moment asked, so a "not
/// yet" is asked again rather than replayed.
pub fn removal_tickets_key(body: &serde_json::Value, ask: &str) -> String {
    derived_idempotency_key("annex-removal-tickets", &format!("{body}:{ask}"))
}

#[cfg(test)]
mod removal_key_tests {
    use super::removal_tickets_key;
    use serde_json::json;

    #[test]
    fn a_ticket_asked_again_is_a_new_request() {
        let b = json!({"annex_id": "1", "candidates": [{"file_id": "7"}]});
        assert_ne!(
            removal_tickets_key(&b, "1000"),
            removal_tickets_key(&b, "11000")
        );
        assert_eq!(
            removal_tickets_key(&b, "1000"),
            removal_tickets_key(&b, "1000")
        );
    }
}
