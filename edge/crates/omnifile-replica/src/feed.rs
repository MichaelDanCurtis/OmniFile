//! Feed events (control-api §5, `service/openapi/control-api.yaml` `Event`),
//! parsed from the raw JSON the control client hands over.
//!
//! Parsing is tolerant: ids may be decimal strings (the wire form) or
//! numbers, unknown fields are ignored, and an unknown event type is kept
//! as [`Event::Unknown`] so the cursor still advances past it.

use serde_json::Value;

use crate::Error;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    File,
    Dir,
    Symlink,
}

impl Kind {
    pub fn parse(s: &str) -> Option<Kind> {
        match s {
            "file" => Some(Kind::File),
            "dir" => Some(Kind::Dir),
            "symlink" => Some(Kind::Symlink),
            _ => None,
        }
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::File => "file",
            Kind::Dir => "dir",
            Kind::Symlink => "symlink",
        }
    }
}

/// `Location` of a file's primary copy.
#[derive(Clone, Debug, PartialEq)]
pub struct Location {
    pub backend_kind: String,
    pub logical_tier: String,
    pub archived: bool,
    pub restore_state: String,
    /// The whole object, as JSON, for the parts the edge does not interpret.
    pub raw: Value,
}

/// `FileRow`.
#[derive(Clone, Debug, PartialEq)]
pub struct FileRow {
    pub file_id: u64,
    pub dataset_id: u64,
    pub parent_id: Option<u64>,
    pub name: String,
    pub kind: Kind,
    pub symlink_target: Option<String>,
    pub size: u64,
    pub mtime_ns: i64,
    pub uid: u32,
    pub gid: u32,
    pub mode: u32,
    pub owner_sid: Option<String>,
    pub acl_id: Option<u64>,
    pub state: Option<String>,
    pub resident_bytes: u64,
    /// The **content** version.
    pub version_id: Option<u64>,
    /// CRC-64/NVME as big-endian hex.
    pub crc64nvme: Option<u64>,
    pub location: Option<Location>,
    /// Desired stub state: `(content version, resident bytes)`.
    pub stub: Option<(u64, u64)>,
    pub deleted: bool,
    pub entity_version: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ShareRow {
    pub share_id: u64,
    pub dataset_id: u64,
    pub name: String,
    pub root_dir_id: Option<u64>,
    /// `wait`, `fail` or `auto`.
    pub on_open: String,
    pub wait_threshold_minutes: Option<u64>,
    pub worm_enabled: bool,
    pub entity_version: u64,
    pub raw: Value,
}

#[derive(Clone, Debug, PartialEq)]
pub struct DatasetRow {
    pub dataset_id: u64,
    pub name: String,
    pub backend_kind: String,
    pub migration_state: String,
    pub entity_version: u64,
    pub raw: Value,
}

#[derive(Clone, Debug, PartialEq)]
pub struct AclRow {
    pub acl_id: u64,
    pub dataset_id: u64,
    pub sha256: String,
    pub entity_version: u64,
    pub raw: Value,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RecallRow {
    pub recall_id: u64,
    pub file_id: u64,
    /// The content version being recalled.
    pub version_id: u64,
    /// Catalog words: requested, rehydrating, copying, downloading,
    /// available, failed, cancelled.
    pub state: String,
    pub eta: Option<String>,
    pub error: Option<String>,
    /// Present while `downloading`.
    pub read_credential: Option<Value>,
    pub entity_version: u64,
}

impl RecallRow {
    pub fn is_active(&self) -> bool {
        is_active_recall_state(&self.state)
    }
}

pub fn is_active_recall_state(s: &str) -> bool {
    !matches!(s, "available" | "failed" | "cancelled")
}

#[derive(Clone, Debug, PartialEq)]
pub struct Origin {
    pub edge_id: u64,
    pub op_id: String,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Body {
    FileUpsert(FileRow),
    FileRename(FileRow),
    FileTombstone(FileRow),
    FileConflict {
        file_id: u64,
        op_id: String,
        reason: String,
        current: Option<FileRow>,
    },
    AclUpsert(AclRow),
    ShareUpsert(ShareRow),
    /// The share was removed (M6): the edge stops serving it. Its dataset
    /// and files stay.
    ShareDelete {
        share_id: u64,
        entity_version: u64,
    },
    DatasetUpsert(DatasetRow),
    RecallState(RecallRow),
    Unknown {
        ty: String,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub struct Event {
    pub entity_id: u64,
    pub entity_version: u64,
    pub origin: Option<Origin>,
    pub body: Body,
}

/// An id: a decimal string (wire form) or a JSON number.
pub fn id(v: &Value) -> Option<u64> {
    match v {
        Value::String(s) => s.parse().ok(),
        Value::Number(n) => n.as_u64(),
        _ => None,
    }
}

fn int(v: &Value) -> Option<i64> {
    match v {
        Value::String(s) => s.parse().ok(),
        Value::Number(n) => n.as_i64(),
        _ => None,
    }
}

fn req_id(o: &Value, k: &str) -> Result<u64, Error> {
    id(&o[k]).ok_or_else(|| Error::Feed(format!("missing or bad id `{k}`")))
}

fn opt_id(o: &Value, k: &str) -> Option<u64> {
    id(&o[k])
}

fn str_of(o: &Value, k: &str) -> Option<String> {
    o[k].as_str().map(str::to_owned)
}

fn parse_location(v: &Value) -> Option<Location> {
    if !v.is_object() {
        return None;
    }
    Some(Location {
        backend_kind: str_of(v, "backend_kind").unwrap_or_default(),
        logical_tier: str_of(v, "logical_tier").unwrap_or_default(),
        archived: v["archived"].as_bool().unwrap_or(false),
        restore_state: str_of(v, "restore_state").unwrap_or_default(),
        raw: v.clone(),
    })
}

pub fn parse_file_row(o: &Value) -> Result<FileRow, Error> {
    let kind_s = o["kind"].as_str().unwrap_or("file");
    let kind = Kind::parse(kind_s).ok_or_else(|| Error::Feed(format!("unknown kind {kind_s}")))?;
    let stub = if o["stub"].is_object() {
        match (
            id(&o["stub"]["version_id"]),
            int(&o["stub"]["resident_bytes"]),
        ) {
            (Some(v), Some(r)) if r >= 0 => Some((v, r as u64)),
            _ => return Err(Error::Feed("bad stub".into())),
        }
    } else {
        None
    };
    let crc = match o["crc64nvme"].as_str() {
        Some(h) => {
            Some(u64::from_str_radix(h, 16).map_err(|_| Error::Feed("bad crc64nvme".into()))?)
        }
        None => None,
    };
    Ok(FileRow {
        file_id: req_id(o, "file_id")?,
        dataset_id: opt_id(o, "dataset_id").unwrap_or(0),
        parent_id: opt_id(o, "parent_id"),
        name: str_of(o, "name").unwrap_or_default(),
        kind,
        symlink_target: str_of(o, "symlink_target"),
        size: int(&o["size"]).unwrap_or(0).max(0) as u64,
        mtime_ns: int(&o["mtime_ns"]).unwrap_or(0),
        uid: int(&o["uid"]).unwrap_or(0).clamp(0, u32::MAX as i64) as u32,
        gid: int(&o["gid"]).unwrap_or(0).clamp(0, u32::MAX as i64) as u32,
        mode: feed_mode(
            kind,
            int(&o["mode"]).unwrap_or(if kind == Kind::Dir { 0o755 } else { 0o644 }),
        ),
        owner_sid: str_of(o, "owner_sid"),
        acl_id: opt_id(o, "acl_id"),
        state: str_of(o, "state"),
        resident_bytes: int(&o["resident_bytes"]).unwrap_or(0).max(0) as u64,
        version_id: opt_id(o, "version_id"),
        crc64nvme: crc,
        location: parse_location(&o["location"]),
        stub,
        deleted: o["deleted"].as_bool().unwrap_or(false),
        entity_version: req_id(o, "entity_version")?,
    })
}

/// A mode from the catalog. A file never gets set-user-id or set-group-id
/// from it (M7 review): a row with uid 0 and 04755 (a compromised service, or
/// a restored catalog) would otherwise put a root executable on every NFS
/// client that mounts the share without `nosuid`. Data shares have no use for
/// them. Directories keep setgid (group inheritance).
pub(crate) fn feed_mode(kind: Kind, raw: i64) -> u32 {
    const SET_UID: u32 = 0o4000;
    const SET_GID: u32 = 0o2000;
    let m = raw as u32 & 0o7777;
    if kind == Kind::Dir {
        m & !SET_UID
    } else {
        m & !(SET_UID | SET_GID)
    }
}

fn parse_share(o: &Value) -> Result<ShareRow, Error> {
    Ok(ShareRow {
        share_id: req_id(o, "share_id")?,
        dataset_id: opt_id(o, "dataset_id").unwrap_or(0),
        name: str_of(o, "name").ok_or_else(|| Error::Feed("share without name".into()))?,
        root_dir_id: opt_id(o, "root_dir_id"),
        on_open: str_of(o, "on_open").unwrap_or_else(|| "fail".into()),
        wait_threshold_minutes: int(&o["wait_threshold_minutes"]).map(|m| m.max(0) as u64),
        worm_enabled: o["worm"]["enabled"].as_bool().unwrap_or(false),
        entity_version: req_id(o, "entity_version")?,
        raw: o.clone(),
    })
}

fn parse_recall(o: &Value) -> Result<RecallRow, Error> {
    Ok(RecallRow {
        recall_id: req_id(o, "recall_id")?,
        file_id: req_id(o, "file_id")?,
        version_id: req_id(o, "version_id")?,
        state: str_of(o, "state").ok_or_else(|| Error::Feed("recall without state".into()))?,
        eta: str_of(o, "eta"),
        error: str_of(o, "error"),
        read_credential: if o["read_credential"].is_object() {
            Some(o["read_credential"].clone())
        } else {
            None
        },
        entity_version: req_id(o, "entity_version")?,
    })
}

/// Parse one event. Malformed events are an error; the caller skips them
/// (and logs) rather than stalling the feed.
pub fn parse_event(v: &Value) -> Result<Event, Error> {
    let ty = v["type"]
        .as_str()
        .ok_or_else(|| Error::Feed("event without type".into()))?;
    let entity_id = id(&v["entity_id"]).unwrap_or(0);
    let entity_version = id(&v["entity_version"]).unwrap_or(0);
    let origin = if v["origin"].is_object() {
        Some(Origin {
            edge_id: id(&v["origin"]["edge_id"]).unwrap_or(0),
            op_id: str_of(&v["origin"], "op_id").unwrap_or_default(),
        })
    } else {
        None
    };
    let file = |v: &Value| -> Result<FileRow, Error> {
        let mut row = parse_file_row(&v["file"])?;
        // The envelope's entity_version is authoritative when present.
        if entity_version > 0 {
            row.entity_version = entity_version;
        }
        Ok(row)
    };
    let body = match ty {
        "file.upsert" => Body::FileUpsert(file(v)?),
        "file.rename" => Body::FileRename(file(v)?),
        "file.tombstone" => {
            let mut row = file(v)?;
            row.deleted = true;
            Body::FileTombstone(row)
        }
        "file.conflict" => {
            let c = &v["conflict"];
            Body::FileConflict {
                file_id: entity_id,
                op_id: str_of(c, "op_id").unwrap_or_default(),
                reason: str_of(c, "reason").unwrap_or_default(),
                current: parse_file_row(&c["current"]).ok(),
            }
        }
        "acl.upsert" => {
            let a = &v["acl"];
            Body::AclUpsert(AclRow {
                acl_id: req_id(a, "acl_id")?,
                dataset_id: opt_id(a, "dataset_id").unwrap_or(0),
                sha256: str_of(a, "sha256").unwrap_or_default(),
                entity_version: if entity_version > 0 {
                    entity_version
                } else {
                    opt_id(a, "entity_version").unwrap_or(0)
                },
                raw: a.clone(),
            })
        }
        "share.upsert" => {
            let mut s = parse_share(&v["share"])?;
            if entity_version > 0 {
                s.entity_version = entity_version;
            }
            Body::ShareUpsert(s)
        }
        "share.delete" => Body::ShareDelete {
            share_id: opt_id(v, "share_id").unwrap_or(entity_id),
            entity_version,
        },
        "dataset.upsert" => {
            let d = &v["dataset"];
            Body::DatasetUpsert(DatasetRow {
                dataset_id: req_id(d, "dataset_id")?,
                name: str_of(d, "name").unwrap_or_default(),
                backend_kind: str_of(&d["backend"], "kind").unwrap_or_default(),
                migration_state: str_of(d, "migration_state").unwrap_or_default(),
                entity_version: if entity_version > 0 {
                    entity_version
                } else {
                    opt_id(d, "entity_version").unwrap_or(0)
                },
                raw: d.clone(),
            })
        }
        "recall.state" => {
            let mut r = parse_recall(&v["recall"])?;
            if entity_version > 0 {
                r.entity_version = entity_version;
            }
            Body::RecallState(r)
        }
        other => Body::Unknown {
            ty: other.to_owned(),
        },
    };
    Ok(Event {
        entity_id,
        entity_version,
        origin,
        body,
    })
}
