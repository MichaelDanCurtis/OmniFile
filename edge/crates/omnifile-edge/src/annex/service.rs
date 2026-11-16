//! What the annex unit needs from the service and from Azure, as one trait:
//! the control API's annex calls (§4.8), its upload calls (§4.2), and a
//! block-blob upload. [`Live`] is the real thing (`omnifile-control` and
//! `omnifile-blob`); [`Fake`] is an in-memory service for the unit tests.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Mutex;

use omnifile_blob::{BlobClient, UploadOutcome, UploadRequest};
use omnifile_control::ControlClient;
use omnifile_control::annex::{
    AnnexDryRun, AnnexEventResult, AnnexRecord, AnnexRecords, EdgeAnnex,
};
use omnifile_control::types as ct;

/// A failed call. `unavailable`: try again later (network, 5xx, 429).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SvcError {
    pub unavailable: bool,
    pub code: Option<String>,
    pub message: String,
}

/// `code` of a read the blob store refused: a credential, perhaps run out.
pub const READ_REFUSED: &str = "read_refused";
/// `code` of a read the blob says no to, and will again (gone, archived again).
pub const READ_DEFINITE: &str = "blob_unreadable";
/// `code` of a range that wasn't what was asked: fetched again.
pub const READ_INTEGRITY: &str = "integrity";

/// A ranged read's failure, by what it says. Only a network failure and a 5xx
/// or a throttle are an outage. Live and the Fake both go through this.
pub fn read_error(e: &omnifile_blob::BlobError) -> SvcError {
    use omnifile_blob::BlobError as B;
    let (unavailable, code) = match e {
        B::Http { status, .. } => match status {
            401 | 403 => (false, Some(READ_REFUSED)),
            408 | 429 => (true, None),
            s if *s >= 500 => (true, None),
            _ => (false, Some(READ_DEFINITE)),
        },
        B::Integrity(_) | B::BadResponse { .. } => (false, Some(READ_INTEGRITY)),
        _ => (true, None),
    };
    SvcError {
        unavailable,
        code: code.map(str::to_owned),
        message: omnifile_control::util::redact_sas(&e.to_string()),
    }
}

impl SvcError {
    pub fn other(message: impl Into<String>) -> SvcError {
        SvcError {
            unavailable: false,
            code: None,
            message: message.into(),
        }
    }
}

impl std::fmt::Display for SvcError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match &self.code {
            Some(c) => write!(f, "{c}: {}", self.message),
            None => f.write_str(&self.message),
        }
    }
}

fn ctl(e: omnifile_control::ControlError) -> SvcError {
    SvcError {
        unavailable: e.is_unavailable(),
        code: e.code().map(str::to_owned),
        message: omnifile_control::util::redact_sas(&e.to_string()),
    }
}

/// `uploads`' answer, reduced to what the copier acts on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Start {
    Upload {
        upload_id: u64,
        version_id: u64,
        sas_url: String,
    },
    /// Already committed under this op id, or the same bytes are in the
    /// dataset: nothing to send.
    Dedupe { version_id: u64 },
}

#[allow(async_fn_in_trait)]
pub trait AnnexService {
    async fn annexes(&self) -> Result<Vec<EdgeAnnex>, SvcError>;
    async fn events(
        &self,
        annex_id: u64,
        events: &[serde_json::Value],
        batch_key: &str,
    ) -> Result<Vec<AnnexEventResult>, SvcError>;
    async fn records(
        &self,
        annex_id: u64,
        after: &str,
        limit: u32,
    ) -> Result<AnnexRecords, SvcError>;
    async fn upload_start(&self, intent: &ct::UploadIntent) -> Result<Start, SvcError>;
    async fn upload_complete(
        &self,
        upload_id: u64,
        body: &ct::UploadComplete,
    ) -> Result<u64, SvcError>;
    async fn upload_abort(&self, upload_id: u64) -> Result<(), SvcError>;
    /// Put Block for every block not yet in the journal, then Put Block List.
    /// The dry runs waiting for one annex.
    async fn dry_runs(&self, annex_id: u64) -> Result<Vec<AnnexDryRun>, SvcError>;

    /// A dry run's report (`{state: done, report}` or `{state: failed, error}`).
    async fn dry_run_report(&self, run_id: u64, body: &serde_json::Value) -> Result<(), SvcError>;

    /// Removal tickets (`{annex_id, candidates}` -> `{results}`). `ask`
    /// makes each question its own request: a "not yet" is asked again
    /// afresh, never replayed (the service keeps one ticket per file).
    async fn removal_tickets(
        &self,
        body: &serde_json::Value,
        ask: &str,
    ) -> Result<serde_json::Value, SvcError>;

    /// A removal's step: asked (`hide`, `delete` -> `{proceed, reason}`) or
    /// reported. `ask` makes each question its own request.
    async fn removal_step(
        &self,
        removal_id: u64,
        body: &serde_json::Value,
        ask: &str,
    ) -> Result<serde_json::Value, SvcError>;

    /// Placement tickets (`{annex_id}` -> `{tickets, failed}`, phase A3.3).
    async fn placement_tickets(
        &self,
        body: &serde_json::Value,
    ) -> Result<serde_json::Value, SvcError>;

    /// A placement's step: `go` (-> `{proceed, reason}`) or a report.
    async fn placement_step(
        &self,
        placement_id: u64,
        body: &serde_json::Value,
        ask: &str,
    ) -> Result<serde_json::Value, SvcError>;

    /// A range of a version's bytes, through a ticket's read credential.
    async fn read_range(
        &self,
        cred: &serde_json::Value,
        offset: u64,
        len: u64,
    ) -> Result<Vec<u8>, SvcError>;

    /// Stage every block, ask `gate`, then commit the block list: a `gate`
    /// error is an `integrity` error with nothing committed.
    async fn upload_blob(
        &self,
        req: &UploadRequest,
        gate: &omnifile_blob::client::Gate<'_>,
    ) -> Result<UploadOutcome, SvcError>;
}

/// The service and Azure.
#[derive(Clone)]
pub struct Live {
    pub control: ControlClient,
    pub blob: BlobClient,
}

impl AnnexService for Live {
    async fn annexes(&self) -> Result<Vec<EdgeAnnex>, SvcError> {
        Ok(self.control.annexes().await.map_err(ctl)?.annexes)
    }

    async fn events(
        &self,
        annex_id: u64,
        events: &[serde_json::Value],
        batch_key: &str,
    ) -> Result<Vec<AnnexEventResult>, SvcError> {
        Ok(self
            .control
            .annex_events(annex_id, events, batch_key)
            .await
            .map_err(ctl)?
            .results)
    }

    async fn records(
        &self,
        annex_id: u64,
        after: &str,
        limit: u32,
    ) -> Result<AnnexRecords, SvcError> {
        self.control
            .annex_records(annex_id, after, limit)
            .await
            .map_err(ctl)
    }

    async fn upload_start(&self, intent: &ct::UploadIntent) -> Result<Start, SvcError> {
        match self.control.upload_start(intent).await.map_err(ctl)? {
            ct::UploadStart::Dedupe { version_id } => Ok(Start::Dedupe { version_id }),
            ct::UploadStart::Upload {
                upload_id,
                version_id,
                target,
                ..
            } => {
                let sas_url = target.sas_url().map(str::to_owned).ok_or_else(|| {
                    SvcError::other("the upload target is not an Azure SAS: upgrade the edge")
                })?;
                Ok(Start::Upload {
                    upload_id,
                    version_id,
                    sas_url,
                })
            }
        }
    }

    async fn upload_complete(
        &self,
        upload_id: u64,
        body: &ct::UploadComplete,
    ) -> Result<u64, SvcError> {
        Ok(self
            .control
            .upload_complete(upload_id, body)
            .await
            .map_err(ctl)?
            .version_id)
    }

    async fn upload_abort(&self, upload_id: u64) -> Result<(), SvcError> {
        self.control.upload_abort(upload_id).await.map_err(ctl)
    }

    async fn dry_runs(&self, annex_id: u64) -> Result<Vec<AnnexDryRun>, SvcError> {
        Ok(self
            .control
            .annex_dry_runs(annex_id)
            .await
            .map_err(ctl)?
            .dry_runs)
    }

    async fn dry_run_report(&self, run_id: u64, body: &serde_json::Value) -> Result<(), SvcError> {
        self.control
            .annex_dry_run_report(run_id, body)
            .await
            .map_err(ctl)
            .map(|_| ())
    }

    async fn removal_tickets(
        &self,
        body: &serde_json::Value,
        ask: &str,
    ) -> Result<serde_json::Value, SvcError> {
        self.control
            .annex_removal_tickets(body, ask)
            .await
            .map_err(ctl)
    }

    async fn removal_step(
        &self,
        removal_id: u64,
        body: &serde_json::Value,
        ask: &str,
    ) -> Result<serde_json::Value, SvcError> {
        self.control
            .annex_removal_step(removal_id, body, ask)
            .await
            .map_err(ctl)
    }

    async fn placement_tickets(
        &self,
        body: &serde_json::Value,
    ) -> Result<serde_json::Value, SvcError> {
        self.control
            .annex_placement_tickets(body)
            .await
            .map_err(ctl)
    }

    async fn placement_step(
        &self,
        placement_id: u64,
        body: &serde_json::Value,
        ask: &str,
    ) -> Result<serde_json::Value, SvcError> {
        self.control
            .annex_placement_step(placement_id, body, ask)
            .await
            .map_err(ctl)
    }

    async fn read_range(
        &self,
        cred: &serde_json::Value,
        offset: u64,
        len: u64,
    ) -> Result<Vec<u8>, SvcError> {
        // Annex copies are in Azure (v1): a blob URL with its SAS.
        let url = match cred.get("mode").and_then(|m| m.as_str()) {
            Some("azure_user_delegation_sas") => cred["url"].as_str().unwrap_or(""),
            other => {
                return Err(SvcError::other(format!(
                    "a read credential of mode {other:?}"
                )));
            }
        };
        self.blob
            .get_range(url, offset, len)
            .await
            .map(|b| b.to_vec())
            .map_err(|e| read_error(&e))
    }

    async fn upload_blob(
        &self,
        req: &UploadRequest,
        gate: &omnifile_blob::client::Gate<'_>,
    ) -> Result<UploadOutcome, SvcError> {
        self.blob
            .upload_file_gated(req, gate)
            .await
            .map_err(|e| SvcError {
                unavailable: !matches!(e, omnifile_blob::BlobError::Integrity(_)),
                code: matches!(e, omnifile_blob::BlobError::Integrity(_))
                    .then(|| "integrity".to_owned()),
                message: omnifile_control::util::redact_sas(&e.to_string()),
            })
    }
}

/// One file as the fake service holds it.
#[derive(Clone, Debug, Default)]
pub struct FakeFile {
    pub file_id: u64,
    pub path: String,
    pub nas_file_id: String,
    pub size: u64,
    pub mtime_ns: i64,
    pub ctime_ns: Option<i64>,
    pub versions: Vec<u64>,
    pub copied: Option<(u64, u64, i64, Option<i64>)>,
    pub gone: bool,
    pub acl: Option<serde_json::Value>,
}

/// An in-memory service: the annex events and uploads, with every call
/// recorded, and switches to make calls fail.
#[derive(Default)]
pub struct Fake {
    pub state: Mutex<FakeState>,
}

#[derive(Default)]
pub struct FakeState {
    pub annexes: Vec<EdgeAnnex>,
    pub files: BTreeMap<u64, FakeFile>,
    /// Files whose copy's read-back is still pending (`copy_ok` false).
    pub unverified: HashSet<u64>,
    /// Files whose copy the read-back refused (`refused`, and `copy_ok` false).
    pub refused: HashSet<u64>,
    /// Files whose copying again an ArchiveAdmin stopped (`copy_stopped`).
    pub copy_stopped: HashSet<u64>,
    pub next_id: u64,
    pub events: Vec<serde_json::Value>,
    pub batches: HashMap<String, Vec<AnnexEventResult>>,
    /// op_id -> (upload_id, version_id, committed)
    pub uploads: HashMap<String, (u64, u64, bool)>,
    pub commits: u64,
    pub blobs: HashMap<u64, Vec<u8>>,
    pub down: bool,
    /// Fail the next blob upload (after this many blocks were sent).
    pub fail_blob: bool,
    /// Run once after the next blob upload has read the file and staged its
    /// blocks, before the gate (a change on the NAS the per-block CRCs can't
    /// see).
    pub after_blob: Option<Box<dyn FnOnce() + Send>>,
    /// Uploads aborted (`uploads/abort`), by upload id.
    pub aborted: Vec<u64>,
    /// Dry runs to hand out, and the reports sent back (run id, body).
    pub dry_runs: Vec<AnnexDryRun>,
    pub dry_run_reports: Vec<(u64, serde_json::Value)>,
    /// Runs taken (`{"state": "running"}`), and runs the service refuses to
    /// hand over (replaced by a newer draft, or expired).
    pub dry_runs_taken: Vec<u64>,
    pub dry_runs_superseded: Vec<u64>,
    /// Placement tickets to hand out (A3.3), the steps the placer sent, the
    /// answer to `go` (None: proceed), and blob bytes by credential URL.
    pub placement_tickets: Vec<serde_json::Value>,
    pub placement_steps: Vec<(u64, serde_json::Value)>,
    pub refuse_go: Option<String>,
    /// A3.4: answers to the replace questions, by step (and `:phase`), used once;
    /// without one, proceed.
    pub replace_answers: HashMap<String, serde_json::Value>,
    /// Asks for tickets, as they came.
    pub placement_asks: Vec<serde_json::Value>,
    /// The tickets given, as the service keeps them: `go` is answered from
    /// them, and a `renew` hands out a new credential.
    pub tickets_given: HashMap<u64, serde_json::Value>,
    /// Read credentials (by URL) that have run out.
    pub expired_urls: Vec<String>,
    /// The credentials used, in order.
    pub read_urls: Vec<String>,
    /// Steps refused, by a service that answers.
    pub refuse_steps: bool,
    /// Blob storage answers every read with this status, the service up.
    pub read_status: Option<u16>,
    /// A range fails its CRC, this many more times.
    pub read_integrity: u32,
    /// One credential answers with this status.
    pub url_status: HashMap<String, u16>,
    /// What a `renew` hands out: (URL, expires_at).
    pub renewed_read: Option<(String, String)>,
    /// A placement's end, as the service would answer it (`cancelled`).
    pub ended: HashMap<u64, String>,
    /// `arrived` and `changed` refused with this code.
    pub reject_arrivals: Option<String>,
    /// How many events it had when each `done` came.
    pub events_at_done: Vec<usize>,
    pub read_blobs: HashMap<String, Vec<u8>>,
    /// Removals: tickets issued (removal id -> the candidate), every step
    /// posted (removal id, body), and answers to force: a candidate's path
    /// -> (status, reason) instead of a ticket, and a step -> a refusal
    /// reason (`hide` or `delete`; a hold reason keeps the state).
    pub removal_tickets: BTreeMap<u64, serde_json::Value>,
    pub removal_steps: Vec<(u64, serde_json::Value)>,
    pub ticket_answer: HashMap<String, (String, String)>,
    pub refuse_step: HashMap<String, String>,
}

/// The service's `go`, as far as a POSIX ACL goes: the staged mode must not let
/// anyone read who the ticket's mode doesn't, and the folder must let others
/// add a file (the requester isn't its owner). `None`: go on.
fn fake_go(ticket: Option<&serde_json::Value>, body: &serde_json::Value) -> Option<String> {
    let ticket = ticket?;
    if body["crc64"] != ticket["crc64"] {
        return Some("crc_mismatch".into());
    }
    let want = ticket["acl"]["mode"].as_u64()?;
    let staged = body["acl"]["mode"].as_u64().unwrap_or(0o7777);
    if staged & 0o444 & !want != 0 {
        return Some("acl_mismatch".into());
    }
    let folder = body["folder_acl"]["mode"].as_u64().unwrap_or(0);
    if folder & 0o022 == 0 {
        return Some("access_denied".into());
    }
    None
}

impl FakeState {
    /// Committed blobs (Put Block List) whose upload was never completed:
    /// billed, referenced by nothing.
    pub fn orphans(&self) -> Vec<u64> {
        let mut out: Vec<u64> = self
            .blobs
            .keys()
            .filter(|v| !self.uploads.values().any(|u| u.1 == **v && u.2))
            .copied()
            .collect();
        out.sort();
        out
    }
}

impl Fake {
    pub fn with(f: impl FnOnce(&mut FakeState)) -> Fake {
        let fake = Fake::default();
        f(&mut fake.state.lock().unwrap());
        fake
    }

    fn down(&self) -> Result<(), SvcError> {
        if self.state.lock().unwrap().down {
            return Err(SvcError {
                unavailable: true,
                code: None,
                message: "down".into(),
            });
        }
        Ok(())
    }

    pub fn live(&self) -> Vec<FakeFile> {
        self.state
            .lock()
            .unwrap()
            .files
            .values()
            .filter(|f| !f.gone)
            .cloned()
            .collect()
    }

    pub fn kinds(&self) -> Vec<String> {
        self.state
            .lock()
            .unwrap()
            .events
            .iter()
            .map(|e| e["kind"].as_str().unwrap_or("").to_owned())
            .collect()
    }
}

fn nanos(v: &serde_json::Value) -> Option<i64> {
    v.as_str().and_then(|s| s.parse().ok())
}

impl FakeState {
    fn apply(&mut self, i: usize, e: &serde_json::Value) -> AnnexEventResult {
        let kind = e["kind"].as_str().unwrap_or("");
        let path = e["path"].as_str().unwrap_or("").to_owned();
        let mut r = AnnexEventResult {
            index: i,
            status: "applied".into(),
            ..Default::default()
        };
        if matches!(kind, "arrived" | "changed")
            && let Some(code) = self.reject_arrivals.clone()
        {
            r.status = "rejected".into();
            r.code = Some(code);
            return r;
        }
        let live = |s: &FakeState, p: &str| {
            s.files
                .values()
                .find(|f| !f.gone && f.path == p)
                .map(|f| f.file_id)
        };
        match kind {
            "arrived" | "changed" => {
                let nas = e["nas_file_id"].as_str().unwrap_or("").to_owned();
                let size = e["size"].as_u64().unwrap_or(0);
                let (m, c) = (nanos(&e["mtime_ns"]).unwrap_or(0), nanos(&e["ctime_ns"]));
                let id = match live(self, &path) {
                    Some(id) if self.files[&id].nas_file_id == nas => id,
                    other => {
                        if let Some(old) = other {
                            self.files.get_mut(&old).unwrap().gone = true;
                        }
                        self.next_id += 1;
                        let id = self.next_id;
                        self.files.insert(
                            id,
                            FakeFile {
                                file_id: id,
                                path: path.clone(),
                                nas_file_id: nas,
                                ..Default::default()
                            },
                        );
                        id
                    }
                };
                let f = self.files.get_mut(&id).unwrap();
                f.size = size;
                f.mtime_ns = m;
                f.ctime_ns = c;
                f.acl = Some(e["acl"].clone());
                let same = f
                    .copied
                    .is_some_and(|(_, s, mm, cc)| (s, mm, cc) == (size, m, c));
                r.file_id = Some(id);
                r.content_version_id = f.versions.last().copied();
                r.copy = Some(!same);
            }
            "copied" => {
                let id = e["file_id"]
                    .as_str()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(0);
                let v = e["version_id"]
                    .as_str()
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(0);
                if let Some(f) = self.files.get_mut(&id) {
                    f.copied = Some((
                        v,
                        e["size"].as_u64().unwrap_or(0),
                        nanos(&e["mtime_ns"]).unwrap_or(0),
                        nanos(&e["ctime_ns"]),
                    ));
                    r.file_id = Some(id);
                    r.version_id = Some(v);
                }
            }
            "left" => {
                if let Some(id) = live(self, &path) {
                    self.files.get_mut(&id).unwrap().gone = true;
                    r.file_id = Some(id);
                }
            }
            "renamed" => {
                let old = e["old_path"].as_str().unwrap_or("");
                match live(self, old) {
                    Some(id) => {
                        if let Some(v) = live(self, &path) {
                            self.files.get_mut(&v).unwrap().gone = true;
                        }
                        self.files.get_mut(&id).unwrap().path = path.clone();
                        r.file_id = Some(id);
                    }
                    None => {
                        r.status = "rejected".into();
                        r.code = Some("not_found".into());
                    }
                }
            }
            _ => {}
        }
        self.events.push(e.clone());
        r
    }
}

impl AnnexService for Fake {
    async fn annexes(&self) -> Result<Vec<EdgeAnnex>, SvcError> {
        self.down()?;
        Ok(self.state.lock().unwrap().annexes.clone())
    }

    async fn events(
        &self,
        _annex_id: u64,
        events: &[serde_json::Value],
        batch_key: &str,
    ) -> Result<Vec<AnnexEventResult>, SvcError> {
        self.down()?;
        let mut s = self.state.lock().unwrap();
        if let Some(r) = s.batches.get(batch_key) {
            return Ok(r.clone());
        }
        let out: Vec<_> = events
            .iter()
            .enumerate()
            .map(|(i, e)| s.apply(i, e))
            .collect();
        s.batches.insert(batch_key.to_owned(), out.clone());
        Ok(out)
    }

    async fn records(
        &self,
        _annex_id: u64,
        after: &str,
        limit: u32,
    ) -> Result<AnnexRecords, SvcError> {
        self.down()?;
        let s = self.state.lock().unwrap();
        let mut all: Vec<_> = s
            .files
            .values()
            .filter(|f| !f.gone && f.path.as_str() > after)
            .collect();
        all.sort_by(|a, b| a.path.cmp(&b.path));
        let more = all.len() > limit as usize;
        let page: Vec<AnnexRecord> = all
            .into_iter()
            .take(limit as usize)
            .map(|f| AnnexRecord {
                path: f.path.clone(),
                file_id: f.file_id,
                nas_file_id: f.nas_file_id.clone(),
                size: f.size,
                mtime_ns: Some(f.mtime_ns.to_string()),
                ctime_ns: f.ctime_ns.map(|c| c.to_string()),
                copied_version_id: f.copied.map(|c| c.0),
                copied_size: f.copied.map(|c| c.1),
                copied_mtime_ns: f.copied.map(|c| c.2.to_string()),
                copied_ctime_ns: f.copied.and_then(|c| c.3).map(|c| c.to_string()),
                verified: f.copied.is_some(),
                copy_ok: f.copied.is_some()
                    && !s.unverified.contains(&f.file_id)
                    && !s.refused.contains(&f.file_id),
                refused: s.refused.contains(&f.file_id) && !s.copy_stopped.contains(&f.file_id),
                copy_stopped: s.copy_stopped.contains(&f.file_id),
            })
            .collect();
        let next = if more {
            page.last().map(|r| r.path.clone())
        } else {
            None
        };
        Ok(AnnexRecords {
            records: page,
            next,
        })
    }

    async fn upload_start(&self, i: &ct::UploadIntent) -> Result<Start, SvcError> {
        self.down()?;
        let mut s = self.state.lock().unwrap();
        if let Some(&(u, v, committed)) = s.uploads.get(&i.op_id) {
            return Ok(if committed {
                Start::Dedupe { version_id: v }
            } else {
                Start::Upload {
                    upload_id: u,
                    version_id: v,
                    sas_url: format!("fake://{v}"),
                }
            });
        }
        s.next_id += 1;
        let v = s.next_id;
        s.uploads.insert(i.op_id.clone(), (v, v, false));
        Ok(Start::Upload {
            upload_id: v,
            version_id: v,
            sas_url: format!("fake://{v}"),
        })
    }

    async fn upload_complete(
        &self,
        upload_id: u64,
        _body: &ct::UploadComplete,
    ) -> Result<u64, SvcError> {
        self.down()?;
        let mut s = self.state.lock().unwrap();
        let mut found = None;
        for u in s.uploads.values_mut() {
            if u.0 == upload_id {
                found = Some((u.1, !u.2));
                u.2 = true;
            }
        }
        match found {
            Some((v, first)) => {
                if first {
                    s.commits += 1;
                }
                Ok(v)
            }
            None => Err(SvcError::other("no such upload")),
        }
    }

    async fn dry_runs(&self, annex_id: u64) -> Result<Vec<AnnexDryRun>, SvcError> {
        self.down()?;
        let mut s = self.state.lock().unwrap();
        let (mine, rest): (Vec<_>, Vec<_>) = std::mem::take(&mut s.dry_runs)
            .into_iter()
            .partition(|r| r.annex_id == annex_id);
        s.dry_runs = rest;
        Ok(mine)
    }

    async fn removal_tickets(
        &self,
        body: &serde_json::Value,
        _ask: &str,
    ) -> Result<serde_json::Value, SvcError> {
        self.down()?;
        let mut s = self.state.lock().unwrap();
        let mut results = Vec::new();
        for c in body["candidates"].as_array().cloned().unwrap_or_default() {
            let path = c["path"].as_str().unwrap_or("").to_owned();
            let file_id = c["file_id"].clone();
            if let Some((status, reason)) = s.ticket_answer.get(&path).cloned() {
                results.push(serde_json::json!({"file_id": file_id, "status": status,
                                                "reason": reason}));
                continue;
            }
            // One removal in flight per file: asked again, the same ticket.
            let open = s
                .removal_tickets
                .iter()
                .find(|(_, t)| t["file_id"] == file_id)
                .map(|(id, _)| *id);
            let id = match open {
                Some(id) => id,
                None => {
                    s.next_id += 1;
                    let id = s.next_id;
                    s.removal_tickets.insert(id, c.clone());
                    id
                }
            };
            results.push(serde_json::json!({"file_id": file_id, "status": "ticket",
                "removal": {"removal_id": id.to_string(), "file_id": file_id,
                    "version_id": c["version_id"], "path": path, "state": "ticketed",
                    "expires_at": "2099-01-01T00:00:00.000Z",
                    "expect": {"nas_file_id": c["nas_file_id"], "size": c["size"],
                               "mtime_ns": c["mtime_ns"], "ctime_ns": c["ctime_ns"]}}}));
        }
        Ok(serde_json::json!({"results": results}))
    }

    async fn removal_step(
        &self,
        removal_id: u64,
        body: &serde_json::Value,
        _ask: &str,
    ) -> Result<serde_json::Value, SvcError> {
        self.down()?;
        let mut s = self.state.lock().unwrap();
        s.removal_steps.push((removal_id, body.clone()));
        let step = body["step"].as_str().unwrap_or("").to_owned();
        if let Some(reason) = s.refuse_step.get(&step).cloned() {
            return Ok(serde_json::json!({"removal_id": removal_id.to_string(),
                "proceed": false, "reason": reason}));
        }
        if matches!(step.as_str(), "done" | "cancelled") {
            s.removal_tickets.remove(&removal_id);
        }
        Ok(
            serde_json::json!({"removal_id": removal_id.to_string(), "proceed": true,
                              "reason": null}),
        )
    }

    async fn placement_tickets(
        &self,
        body: &serde_json::Value,
    ) -> Result<serde_json::Value, SvcError> {
        self.down()?;
        let mut s = self.state.lock().unwrap();
        s.placement_asks.push(body.clone());
        let mut t = std::mem::take(&mut s.placement_tickets);
        for v in &t {
            if let Some(pid) = v["placement_id"]
                .as_str()
                .and_then(|p| p.parse::<u64>().ok())
            {
                s.tickets_given.insert(pid, v.clone());
            }
        }
        // Asked to renew: the ticket again, with the new credential.
        for r in body["renew"].as_array().into_iter().flatten() {
            let Some(pid) = r.as_str().and_then(|p| p.parse::<u64>().ok()) else {
                continue;
            };
            if let (Some(given), Some((url, until))) =
                (s.tickets_given.get(&pid).cloned(), s.renewed_read.clone())
            {
                let mut v = given;
                v["read"]["url"] = serde_json::json!(url);
                v["expires_at"] = serde_json::json!(until);
                t.push(v);
            }
        }
        Ok(serde_json::json!({"tickets": t, "failed": []}))
    }

    async fn placement_step(
        &self,
        placement_id: u64,
        body: &serde_json::Value,
        _ask: &str,
    ) -> Result<serde_json::Value, SvcError> {
        self.down()?;
        let mut s = self.state.lock().unwrap();
        if s.refuse_steps {
            return Err(SvcError::other("400: refused"));
        }
        s.placement_steps.push((placement_id, body.clone()));
        if body["step"] == "done" {
            let n = s.events.len();
            s.events_at_done.push(n);
        }
        if let Some(end) = s.ended.get(&placement_id).cloned() {
            return Ok(serde_json::json!({"placement_id": placement_id.to_string(),
                                         "state": end, "proceed": false, "reason": end}));
        }
        // A3.4's questions: an answer set by the test (`replace_go`,
        // `replace_delete_go:place`, `replace_delete_go:delete`), used once.
        if let Some(step) = body["step"].as_str().filter(|s| s.starts_with("replace_")) {
            let key = match body["phase"].as_str() {
                Some(ph) => format!("{step}:{ph}"),
                None => step.to_owned(),
            };
            if let Some(a) = s.replace_answers.remove(&key) {
                let mut a = a;
                a["placement_id"] = serde_json::json!(placement_id.to_string());
                return Ok(a);
            }
            // As the service: the hide is heard before a question about the hidden file.
            let heard = s
                .placement_steps
                .iter()
                .any(|(id, b)| *id == placement_id && b["step"] == "replace_hidden");
            if step == "replace_delete_go" && !heard {
                return Ok(serde_json::json!({"placement_id": placement_id.to_string(),
                                             "proceed": false, "reason": "not_in_order"}));
            }
        }
        if body["step"] == "go" {
            if let Some(r) = s.refuse_go.clone() {
                return Ok(serde_json::json!({"placement_id": placement_id.to_string(),
                                             "proceed": false, "reason": r}));
            }
            if let Some(r) = fake_go(s.tickets_given.get(&placement_id), body) {
                return Ok(serde_json::json!({"placement_id": placement_id.to_string(),
                                             "proceed": false, "reason": r}));
            }
        }
        Ok(
            serde_json::json!({"placement_id": placement_id.to_string(), "proceed": true,
                              "reason": null}),
        )
    }

    async fn read_range(
        &self,
        cred: &serde_json::Value,
        offset: u64,
        len: u64,
    ) -> Result<Vec<u8>, SvcError> {
        self.down()?;
        let mut s = self.state.lock().unwrap();
        let status = |status: u16| {
            read_error(&omnifile_blob::BlobError::Http {
                op: "get_range",
                status,
                code: None,
                message: "fake".into(),
                request_id: None,
                retry_after: None,
            })
        };
        if let Some(st) = s.read_status {
            return Err(status(st));
        }
        if s.read_integrity > 0 {
            s.read_integrity -= 1;
            return Err(read_error(&omnifile_blob::BlobError::Integrity(
                "fake".into(),
            )));
        }
        let url = cred["url"].as_str().unwrap_or("");
        s.read_urls.push(url.to_owned());
        if let Some(st) = s.url_status.get(url) {
            return Err(status(*st));
        }
        if s.expired_urls.iter().any(|u| u == url) {
            return Err(status(403));
        }
        let b = s
            .read_blobs
            .get(url)
            .ok_or_else(|| SvcError::other("no such blob"))?;
        let end = (offset + len).min(b.len() as u64) as usize;
        Ok(b[(offset as usize).min(end)..end].to_vec())
    }

    async fn dry_run_report(&self, run_id: u64, body: &serde_json::Value) -> Result<(), SvcError> {
        self.down()?;
        let mut s = self.state.lock().unwrap();
        if s.dry_runs_superseded.contains(&run_id) {
            return Err(SvcError {
                unavailable: false,
                code: Some("version_conflict".into()),
                message: "the dry run is no longer waiting".into(),
            });
        }
        if body["state"] == "running" {
            s.dry_runs_taken.push(run_id);
        } else {
            s.dry_run_reports.push((run_id, body.clone()));
        }
        Ok(())
    }

    async fn upload_abort(&self, upload_id: u64) -> Result<(), SvcError> {
        self.down()?;
        let mut s = self.state.lock().unwrap();
        // The pending version goes; a new intent for the op id starts afresh.
        s.uploads.retain(|_, u| u.0 != upload_id || u.2);
        s.aborted.push(upload_id);
        Ok(())
    }

    async fn upload_blob(
        &self,
        req: &UploadRequest,
        gate: &omnifile_blob::client::Gate<'_>,
    ) -> Result<UploadOutcome, SvcError> {
        self.down()?;
        if std::mem::take(&mut self.state.lock().unwrap().fail_blob) {
            return Err(SvcError {
                unavailable: true,
                code: None,
                message: "injected blob failure".into(),
            });
        }
        let data = std::fs::read(&req.source).map_err(|e| SvcError::other(e.to_string()))?;
        if data.len() as u64 != req.plan.size() {
            return Err(SvcError {
                unavailable: false,
                code: Some("integrity".into()),
                message: "size changed".into(),
            });
        }
        let mut blocks = Vec::new();
        for b in req.plan.blocks() {
            let chunk = &data[b.range().start as usize..b.range().end as usize];
            let crc = omnifile_integrity::crc64::crc64_nvme(chunk);
            if let Some(exp) = &req.expected_block_crcs
                && exp[b.index as usize] != crc
            {
                return Err(SvcError {
                    unavailable: false,
                    code: Some("integrity".into()),
                    message: format!("block {} changed", b.index),
                });
            }
            blocks.push(omnifile_blob::client::CommittedBlock {
                id: omnifile_integrity::blocks::block_id(1, b.index),
                len: b.len,
                crc64nvme: crc,
            });
        }
        let v: u64 = req
            .sas_url
            .trim_start_matches("fake://")
            .parse()
            .unwrap_or(0);
        // Every block is staged: the hook, then the gate, then Put Block
        // List (the blob exists from here on).
        let hook = self.state.lock().unwrap().after_blob.take();
        if let Some(h) = hook {
            h();
        }
        if let Err(why) = gate().await {
            return Err(SvcError {
                unavailable: false,
                code: Some("integrity".into()),
                message: format!("before commit: {why}"),
            });
        }
        self.state.lock().unwrap().blobs.insert(v, data.clone());
        Ok(UploadOutcome {
            etag: Some("\"e\"".into()),
            version_id: None,
            crc64nvme: omnifile_integrity::crc64::crc64_nvme(&data),
            size: data.len() as u64,
            blocks_uploaded: blocks.len() as u32,
            blocks_resumed: 0,
            blocks,
        })
    }
}
