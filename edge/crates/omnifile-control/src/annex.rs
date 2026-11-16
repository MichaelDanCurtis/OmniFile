//! Annexes (`control-api.md` §4.8; `docs/design/annex.md`, phase A1): the
//! wire types of `GET annexes`, `POST annex-events` and
//! `GET annexes/{annex_id}/records`. The calls are on
//! [`crate::ControlClient`].
//!
//! Events go out as JSON objects ([`serde_json::Value`]) built by the annex
//! unit: their kinds and fields are open (§4.8), and the service checks each.

use serde::{Deserialize, Serialize};

use crate::types::id;

/// One annex this edge runs (`GET annexes`).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct EdgeAnnex {
    #[serde(with = "id")]
    pub annex_id: u64,
    pub name: String,
    /// Open: `copy` in A1.
    pub mode: String,
    /// Open: `registered`, `baseline`, `active`, `paused`.
    pub state: String,
    /// Under the filer's export; `""` = all of it.
    pub path: String,
    #[serde(with = "id")]
    pub dataset_id: u64,
    pub filer: AnnexFiler,
    pub settings: AnnexSettings,
    #[serde(default)]
    pub ruleset: Option<serde_json::Value>,
    #[serde(default)]
    pub baseline_at: Option<String>,
    #[serde(with = "id")]
    pub entity_version: u64,
    /// The service's clock when it answered: the edge's bound on its own
    /// before an age-based removal.
    #[serde(default)]
    pub served_at: Option<String>,
    /// Whether removals are live (phase A2.2), and their limits.
    #[serde(default)]
    pub removals: Option<AnnexRemovals>,
    /// A reconciliation the service asked for now (a copy Azure's read-back refused):
    /// the unit runs one when it sees a value it hasn't answered.
    #[serde(default)]
    pub reconcile_asked_at: Option<String>,
}

/// `GET annexes`' `removals`: the unit mounts the NAS read-write only while
/// `live`; otherwise a removing rule set's choices are `would_remove` events.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnnexRemovals {
    pub live: bool,
    #[serde(default)]
    pub why_not: Option<String>,
    #[serde(default)]
    pub dry_run: bool,
    #[serde(default)]
    pub grace_minutes: u32,
    #[serde(default)]
    pub pointer_files: bool,
    #[serde(default)]
    pub rate_per_hour: u32,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnnexFiler {
    #[serde(with = "id")]
    pub filer_id: u64,
    pub vendor: String,
    pub host: String,
    /// Open: `smb`, `nfs3`, `nfs4`.
    pub protocol: String,
    /// The SMB share name, or the NFS export path.
    pub export: String,
    /// Open: `scan`, `smb_notify`.
    pub event_source: String,
    /// Open: `machine_account`, `service_keytab`, `sec_sys`.
    pub credential_mode: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnnexSettings {
    pub quiet_minutes: u32,
    pub quick_scan_minutes: u32,
    pub full_scan_minutes: u32,
    pub reconcile_days: u32,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnnexList {
    pub annexes: Vec<EdgeAnnex>,
}

/// One event's result (`POST annex-events`), in the order sent.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnnexEventResult {
    pub index: usize,
    /// Open: `applied`, `rejected`.
    pub status: String,
    #[serde(default)]
    pub code: Option<String>,
    #[serde(default)]
    pub detail: Option<String>,
    #[serde(default, with = "id::opt")]
    pub file_id: Option<u64>,
    #[serde(default, with = "id::opt")]
    pub content_version_id: Option<u64>,
    #[serde(default, with = "id::opt")]
    pub version_id: Option<u64>,
    /// `arrived` / `changed`: upload this file (through `uploads`).
    #[serde(default)]
    pub copy: Option<bool>,
    #[serde(default)]
    pub verified: Option<bool>,
}

impl AnnexEventResult {
    pub fn applied(&self) -> bool {
        self.status == "applied"
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnnexEventResults {
    pub results: Vec<AnnexEventResult>,
}

/// The catalog's record of one live annexed file (`GET …/records`).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnnexRecord {
    pub path: String,
    #[serde(with = "id")]
    pub file_id: u64,
    pub nas_file_id: String,
    pub size: u64,
    #[serde(default)]
    pub mtime_ns: Option<String>,
    #[serde(default)]
    pub ctime_ns: Option<String>,
    #[serde(default, with = "id::opt")]
    pub copied_version_id: Option<u64>,
    #[serde(default)]
    pub copied_size: Option<u64>,
    #[serde(default)]
    pub copied_mtime_ns: Option<String>,
    #[serde(default)]
    pub copied_ctime_ns: Option<String>,
    pub verified: bool,
    pub copy_ok: bool,
    /// The read-back refused this copy and the service wants another: what a
    /// reconciliation it asked for copies again (`copy_ok` is false while a read-back
    /// is only pending, too).
    #[serde(default)]
    pub refused: bool,
    /// An ArchiveAdmin stopped copies of these bytes again (the service's migration
    /// 0058): no reconciliation copies them, the edge's own included, until a version
    /// verifies or the admin resumes. A file changed on the NAS is still copied.
    #[serde(default)]
    pub copy_stopped: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnnexRecords {
    pub records: Vec<AnnexRecord>,
    #[serde(default)]
    pub next: Option<String>,
}

/// Nanoseconds from a record's decimal string.
pub fn nanos(v: &Option<String>) -> Option<i64> {
    v.as_deref().and_then(|s| s.parse().ok())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn results_and_records_read_the_wire_shapes() {
        let r: AnnexEventResults = serde_json::from_str(
            r#"{"results":[{"index":0,"status":"applied","file_id":"7",
                "content_version_id":null,"copy":true},
               {"index":1,"status":"rejected","code":"path_conflict","detail":"x"}]}"#,
        )
        .unwrap();
        assert!(r.results[0].applied() && r.results[0].copy == Some(true));
        assert_eq!(r.results[0].file_id, Some(7));
        assert_eq!(r.results[1].code.as_deref(), Some("path_conflict"));
        let p: AnnexRecords = serde_json::from_str(
            r#"{"records":[{"path":"a/b","file_id":"3","nas_file_id":"9","size":5,
                "mtime_ns":"10","ctime_ns":null,"copied_version_id":"4","copied_size":5,
                "copied_mtime_ns":"10","copied_ctime_ns":"11","verified":true,"copy_ok":true}],
                "next":null}"#,
        )
        .unwrap();
        assert_eq!(p.records[0].copied_version_id, Some(4));
        assert_eq!(nanos(&p.records[0].copied_ctime_ns), Some(11));
    }
}

/// A dry run the service asked for (`GET annex-dry-runs`): evaluate `body`
/// (a rule set, approved or proposed) over the annex's listing, read-only.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnnexDryRun {
    #[serde(with = "id")]
    pub run_id: u64,
    #[serde(with = "id")]
    pub annex_id: u64,
    pub body: serde_json::Value,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AnnexDryRuns {
    pub dry_runs: Vec<AnnexDryRun>,
}
