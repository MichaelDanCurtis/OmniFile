//! Wire types for the edge side of the control API
//! (`service/openapi/control-api.yaml`; prose in `docs/contracts/control-api.md`).
//!
//! Identifiers are `u64` in Rust and decimal strings on the wire, so no client
//! loses precision. Times stay RFC 3339 strings here; [`crate::time`] parses
//! them where the edge acts on them. `tests/contract.rs` validates these
//! types against the OpenAPI schemas.

use serde::{Deserialize, Serialize};

/// `u64` as a decimal string (`Id` in the OpenAPI file).
pub mod id {
    use serde::{Deserialize, Deserializer, Serializer, de::Error};

    pub fn serialize<S: Serializer>(v: &u64, s: S) -> Result<S::Ok, S::Error> {
        s.collect_str(v)
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<u64, D::Error> {
        let s = String::deserialize(d)?;
        s.parse()
            .map_err(|_| D::Error::custom(format!("bad id {s:?}")))
    }

    pub mod opt {
        use super::*;

        pub fn serialize<S: Serializer>(v: &Option<u64>, s: S) -> Result<S::Ok, S::Error> {
            match v {
                Some(v) => s.collect_str(v),
                None => s.serialize_none(),
            }
        }

        pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Option<u64>, D::Error> {
            match Option::<String>::deserialize(d)? {
                None => Ok(None),
                Some(s) => s
                    .parse()
                    .map(Some)
                    .map_err(|_| D::Error::custom(format!("bad id {s:?}"))),
            }
        }
    }

    pub mod vec {
        use super::*;
        use serde::ser::SerializeSeq;

        pub fn serialize<S: Serializer>(v: &[u64], s: S) -> Result<S::Ok, S::Error> {
            let mut seq = s.serialize_seq(Some(v.len()))?;
            for x in v {
                seq.serialize_element(&x.to_string())?;
            }
            seq.end()
        }

        pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<u64>, D::Error> {
            Vec::<String>::deserialize(d)?
                .into_iter()
                .map(|s| {
                    s.parse()
                        .map_err(|_| D::Error::custom(format!("bad id {s:?}")))
                })
                .collect()
        }
    }
}

/// `i64` nanoseconds as a decimal string (`Nanos`).
pub mod nanos {
    use serde::{Deserialize, Deserializer, Serializer, de::Error};

    pub fn serialize<S: Serializer>(v: &i64, s: S) -> Result<S::Ok, S::Error> {
        s.collect_str(v)
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<i64, D::Error> {
        let s = String::deserialize(d)?;
        s.parse()
            .map_err(|_| D::Error::custom(format!("bad nanos {s:?}")))
    }
}

/// CRC-64/NVME as 16 lowercase hex digits, big-endian (`Crc64`).
pub mod crc_hex {
    use serde::{Deserialize, Deserializer, Serializer, de::Error};

    pub fn serialize<S: Serializer>(v: &u64, s: S) -> Result<S::Ok, S::Error> {
        s.collect_str(&format_args!("{v:016x}"))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<u64, D::Error> {
        let s = String::deserialize(d)?;
        if s.len() != 16 {
            return Err(D::Error::custom(format!("bad crc64 {s:?}")));
        }
        u64::from_str_radix(&s, 16).map_err(|_| D::Error::custom(format!("bad crc64 {s:?}")))
    }

    pub mod opt {
        use super::*;

        pub fn serialize<S: Serializer>(v: &Option<u64>, s: S) -> Result<S::Ok, S::Error> {
            match v {
                Some(v) => super::serialize(v, s),
                None => s.serialize_none(),
            }
        }

        pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Option<u64>, D::Error> {
            #[derive(Deserialize)]
            struct Hex(#[serde(with = "super")] u64);
            Ok(Option::<Hex>::deserialize(d)?.map(|h| h.0))
        }
    }
}

// ---------------------------------------------------------------------------
// Errors (RFC 9457)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Problem {
    #[serde(rename = "type", default)]
    pub type_: String,
    #[serde(default)]
    pub title: String,
    pub status: u16,
    pub code: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(default)]
    pub retryable: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry_after_s: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub request_id: Option<String>,
}

// ---------------------------------------------------------------------------
// Enrolment and certificates (§2)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct EdgeFacts {
    pub hostname: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub os: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kernel: Option<String>,
    pub version: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct EnrolRequest {
    pub token: String,
    pub csr_pem: String,
    pub facts: EdgeFacts,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RenewRequest {
    pub csr_pem: String,
    /// Possession of the current key (control API §2): a signature by it over
    /// [`renewal_proof_message`].
    pub proof: RenewalProof,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RenewalProof {
    /// RFC 3339; within 5 minutes of the service clock.
    pub signed_at: String,
    /// Base64 of a DER ECDSA P-256/SHA-256 signature by the current client key.
    pub signature: String,
}

/// What the current key signs to renew:
/// `omnifile-renewal-v1\n<edge_id>\n<signed_at>\n<hex sha256 of the CSR DER>`.
pub fn renewal_proof_message(edge_id: u64, signed_at: &str, csr_der: &[u8]) -> Vec<u8> {
    proof_message(RENEWAL_PROOF_CONTEXT, edge_id, signed_at, csr_der)
}

pub const RENEWAL_PROOF_CONTEXT: &str = "omnifile-renewal-v1";
/// The Roles Anywhere certificate's proof (decision 50): the renewal's, under
/// its own context so neither can be replayed as the other.
pub const ROLES_ANYWHERE_PROOF_CONTEXT: &str = "omnifile-roles-anywhere-v1";

/// `<context>\n<edge_id>\n<signed_at>\n<hex sha256 of the CSR DER>`.
pub fn proof_message(context: &str, edge_id: u64, signed_at: &str, csr_der: &[u8]) -> Vec<u8> {
    format!(
        "{context}\n{edge_id}\n{signed_at}\n{}",
        crate::util::sha256_hex(csr_der)
    )
    .into_bytes()
}

/// `POST …/aws-certificate` (decision 50): a Roles Anywhere certificate for
/// the TPM-sealed AWS key in `csr_pem`, with a proof by the mTLS key over
/// [`proof_message`] with [`ROLES_ANYWHERE_PROOF_CONTEXT`].
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RolesAnywhereRequest {
    pub csr_pem: String,
    pub proof: RenewalProof,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RolesAnywhereCertificate {
    #[serde(with = "id", default)]
    pub edge_id: u64,
    pub cert_pem: String,
    pub chain_pem: String,
    pub ca_pem: String,
    pub not_after: String,
    /// Day 20 of 30.
    pub renew_after: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct IssuedCertificate {
    #[serde(with = "id", default)]
    pub edge_id: u64,
    pub cert_pem: String,
    pub chain_pem: String,
    pub ca_pem: String,
    pub not_after: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub api_url: Option<String>,
}

// ---------------------------------------------------------------------------
// Heartbeat (§4.7)
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GuardState {
    Armed,
    Degraded,
    Down,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct HeartbeatRequest {
    pub version: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kernel: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub guard_state: Option<GuardState>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub landing_used_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub landing_capacity_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cache_used_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outbox_depth: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub feed_cursor: Option<String>,
    /// Fills running now (recalls and online fetches).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fills_running: Option<u64>,
    /// Recalls some reader is waiting on, not yet filled.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recalls_waiting: Option<u64>,
    /// Free and total bytes on the data volume.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data_free_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data_capacity_bytes: Option<u64>,
    /// FSAL connections (Ganesha instances) ready on the daemon socket.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fsal_connections: Option<u64>,
    /// 1 while the break-glass account has been used since it was last sealed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recovery_used: Option<u64>,
    /// Decision 67: the quiet periods a share with none of its own gets here.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub quiet_default_s: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub quiet_worm_default_s: Option<u64>,
    /// How long the edge has failed to read its shares' quiet periods.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub quiet_stale_s: Option<u64>,
    /// 1: this edge knows the recall state `superseded` (§4.4). The service
    /// sends `cancelled` in its place to an edge that doesn't say so.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recall_superseded: Option<u64>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct HeartbeatResponse {
    pub server_time: String,
    pub min_edge_version: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub feed_lag_events: Option<i64>,
    #[serde(with = "id")]
    pub config_version: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub certificate_renew_after: Option<String>,
}

// ---------------------------------------------------------------------------
// Feed (§5)
// ---------------------------------------------------------------------------

/// One page of the feed. Events are raw JSON (`Event` in the OpenAPI file):
/// the replica parses them, and unknown types must survive untouched.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct FeedPage {
    pub events: Vec<serde_json::Value>,
    /// Continue from here, even when `events` is empty.
    pub next: String,
    pub reset_required: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct FeedAck {
    pub cursor: String,
}

/// The feed's first cursor.
pub const FEED_START: &str = "0.0";

// ---------------------------------------------------------------------------
// Namespace changes (§4.1)
// ---------------------------------------------------------------------------

/// A file reference: a catalog id, or an edge-local identity for objects the
/// service hasn't numbered yet.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum FileRef {
    Id {
        #[serde(with = "id")]
        file_id: u64,
    },
    Local {
        local: LocalRef,
    },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct LocalRef {
    pub fsid: String,
    pub ino: String,
    pub ino_gen: String,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Attrs {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
    #[serde(default, with = "nanos_opt", skip_serializing_if = "Option::is_none")]
    pub mtime_ns: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub uid: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gid: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mode: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub owner_sid: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub acl_sha256: Option<String>,
}

mod nanos_opt {
    use serde::{Deserialize, Deserializer, Serializer, de::Error};

    pub fn serialize<S: Serializer>(v: &Option<i64>, s: S) -> Result<S::Ok, S::Error> {
        match v {
            Some(v) => s.collect_str(v),
            None => s.serialize_none(),
        }
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Option<i64>, D::Error> {
        Option::<String>::deserialize(d)?
            .map(|s| {
                s.parse()
                    .map_err(|_| D::Error::custom(format!("bad nanos {s:?}")))
            })
            .transpose()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ChangeKind {
    Create,
    Setattr,
    Rename,
    Unlink,
    Acl,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NodeKind {
    File,
    Dir,
    Symlink,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChangeOp {
    pub op_id: String,
    pub op: ChangeKind,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub file: Option<FileRef>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent: Option<FileRef>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<NodeKind>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub symlink_target: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attrs: Option<Attrs>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub new_parent: Option<FileRef>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub new_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub replace: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub acl_sha256: Option<String>,
    #[serde(default, with = "id::opt", skip_serializing_if = "Option::is_none")]
    pub expected_entity_version: Option<u64>,
    /// Who made the change on the edge (§4.1 `actor`; optional, additive).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub actor: Option<Actor>,
}

/// How an [`Actor`] reached the edge.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActorVia {
    Nfs,
    Smb,
    Local,
    /// The reconcile pass found the change after the fact, or the session
    /// could not be resolved: no identity is sent.
    Unknown,
}

/// Who made a namespace change (§4.1), shaped like a recall's
/// [`Requester`]. The service derives `proof` itself; the edge's value can
/// only lower it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Actor {
    pub via: ActorVia,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub uid: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sid: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sec: Option<Sec>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub proof: Option<Proof>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_addr: Option<String>,
}

impl Actor {
    /// No identity: the reconcile pass, or an unresolved session.
    pub fn unknown() -> Actor {
        Actor {
            via: ActorVia::Unknown,
            uid: None,
            sid: None,
            sec: None,
            proof: None,
            client_addr: None,
        }
    }

    pub fn is_unknown(&self) -> bool {
        self.via == ActorVia::Unknown
    }
}

impl ChangeOp {
    pub fn new(op_id: impl Into<String>, op: ChangeKind) -> Self {
        Self {
            op_id: op_id.into(),
            op,
            file: None,
            parent: None,
            name: None,
            kind: None,
            symlink_target: None,
            attrs: None,
            new_parent: None,
            new_name: None,
            replace: None,
            acl_sha256: None,
            expected_entity_version: None,
            actor: None,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChangesRequest {
    pub ops: Vec<ChangeOp>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ChangeStatus {
    Applied,
    Duplicate,
    Conflict,
    Rejected,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChangeResult {
    pub op_id: String,
    pub status: ChangeStatus,
    #[serde(default, with = "id::opt", skip_serializing_if = "Option::is_none")]
    pub file_id: Option<u64>,
    #[serde(default, with = "id::opt", skip_serializing_if = "Option::is_none")]
    pub version_id: Option<u64>,
    #[serde(default, with = "id::opt", skip_serializing_if = "Option::is_none")]
    pub entity_version: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub code: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChangesResponse {
    pub results: Vec<ChangeResult>,
}

// ---------------------------------------------------------------------------
// Credentials (§3)
// ---------------------------------------------------------------------------

/// A data credential.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case")]
pub enum Credential {
    /// A blob URL with a user-delegation SAS.
    AzureUserDelegationSas {
        url: String,
        expires_at: String,
        /// Reuse until this time (expiry minus 5 minutes).
        cache_until: String,
    },
    /// A presigned S3 GetObject URL for one object version (reads only).
    S3PresignedUrl {
        url: String,
        expires_at: String,
        /// Reuse until this time (expiry minus 5 minutes).
        cache_until: String,
        /// The object's full CRC-64/NVME, as S3 stores it, when the service
        /// knows it: what a fill is verified against when the replica has
        /// no CRC for the file yet (a seeded file, read before the feed
        /// carries the CRC the service just learned).
        #[serde(
            default,
            skip_serializing_if = "Option::is_none",
            with = "crc_hex::opt"
        )]
        crc64nvme: Option<u64>,
        /// The catalog's ETag, signed into the URL as an `If-Match` header:
        /// every GET must send exactly it, and `412` means the object was
        /// replaced (the source changed). Absent: the URL pins only what
        /// its `versionId` names.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        if_match: Option<String>,
    },
    /// Only a pointer: the edge signs with SigV4, with temporary
    /// credentials from its IAM Roles Anywhere helper (the default for S3,
    /// control API §3). No secret.
    S3RolesAnywhere {
        bucket: String,
        key: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        version_id: Option<String>,
        /// The location's ETag as S3 gives it (quoted): sent as `If-Match`
        /// when there is no `version_id` (an unversioned bucket). A read
        /// with neither is refused.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        etag: Option<String>,
        /// The object's full CRC-64/NVME when the service knows it (the
        /// location's, or S3's own): what a whole-file check uses when the
        /// replica has none yet.
        #[serde(
            default,
            skip_serializing_if = "Option::is_none",
            with = "crc_hex::opt"
        )]
        crc64nvme: Option<u64>,
        region: String,
        /// Host only: the account's S3 interface endpoint.
        endpoint: String,
        /// `virtual_host` (default) or `path`.
        #[serde(default = "d_addressing")]
        addressing: String,
        /// `"1.2"` pins TLS 1.2 (interface endpoints reject 1.3). Absent
        /// means "1.2", the safe value for an older service.
        #[serde(default = "d_tls12")]
        tls_max_version: Option<String>,
        #[serde(default)]
        requester_pays: bool,
        /// Interface endpoints only (decision 22). Absent means private.
        #[serde(default = "d_true")]
        private_network: bool,
    },
    /// A `mode` this edge does not know (a newer service, a newer backend
    /// kind). It deserializes so the rest of the response stays readable;
    /// every use of it is an "unsupported backend: upgrade the edge" error
    /// for that one operation. It carries nothing and is never sent.
    #[serde(other)]
    Unsupported,
}

impl Credential {
    /// The SAS URL, for Azure credentials (the only ones uploads use).
    pub fn sas_url(&self) -> Option<&str> {
        match self {
            Credential::AzureUserDelegationSas { url, .. } => Some(url),
            Credential::S3PresignedUrl { .. }
            | Credential::S3RolesAnywhere { .. }
            | Credential::Unsupported => None,
        }
    }

    /// The object's CRC-64/NVME the credential vouches for (S3 only).
    pub fn crc64nvme(&self) -> Option<u64> {
        match self {
            Credential::S3PresignedUrl { crc64nvme, .. }
            | Credential::S3RolesAnywhere { crc64nvme, .. } => *crc64nvme,
            _ => None,
        }
    }

    /// A URL this edge can read with, and what kind it is.
    pub fn read_url(&self) -> Option<(&str, ReadKind)> {
        match self {
            Credential::AzureUserDelegationSas { url, .. } => Some((url, ReadKind::AzureSas)),
            Credential::S3PresignedUrl { url, .. } => Some((url, ReadKind::S3Presigned)),
            Credential::S3RolesAnywhere { .. } | Credential::Unsupported => None,
        }
    }

    /// The service sent a credential `mode` this edge does not know.
    pub fn is_unsupported(&self) -> bool {
        matches!(self, Credential::Unsupported)
    }

    /// When the edge must stop reusing this credential. `None`: no expiry
    /// (S3 pointers are not secrets).
    pub fn cache_until(&self) -> Option<&str> {
        match self {
            Credential::AzureUserDelegationSas { cache_until, .. }
            | Credential::S3PresignedUrl { cache_until, .. } => Some(cache_until),
            Credential::S3RolesAnywhere { .. } | Credential::Unsupported => None,
        }
    }
}

fn d_addressing() -> String {
    "virtual_host".into()
}

fn d_tls12() -> Option<String> {
    Some("1.2".into())
}

fn d_true() -> bool {
    true
}

/// What kind of read URL a credential carries.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReadKind {
    /// Azure blob URL with a user-delegation SAS; ranges come with a CRC.
    AzureSas,
    /// S3 presigned GetObject; ranges come unverified.
    S3Presigned,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "access", rename_all = "snake_case")]
pub enum CredentialRequest {
    Read {
        #[serde(with = "id")]
        file_id: u64,
        #[serde(with = "id")]
        version_id: u64,
    },
    Write {
        #[serde(with = "id")]
        upload_id: u64,
    },
}

// ---------------------------------------------------------------------------
// Uploads (§4.2)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct UploadIntent {
    pub op_id: String,
    pub file: FileRef,
    pub size: u64,
    #[serde(with = "nanos")]
    pub mtime_ns: i64,
    pub blake3: String,
    #[serde(with = "crc_hex")]
    pub crc64nvme: u64,
    pub block_size: u64,
    #[serde(default, with = "id::opt")]
    pub base_version_id: Option<u64>,
}

// The credential (with the S3 pointer's fields) makes `Upload` the larger
// variant; one short-lived response, not worth a box.
#[allow(clippy::large_enum_variant)]
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum UploadStart {
    Upload {
        #[serde(with = "id")]
        upload_id: u64,
        #[serde(with = "id")]
        version_id: u64,
        target: Credential,
        block_size: u64,
        /// False: do not send `x-ms-access-tier` (Smart accounts).
        set_tier: bool,
    },
    Dedupe {
        #[serde(with = "id")]
        version_id: u64,
    },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct UploadComplete {
    #[serde(with = "crc_hex")]
    pub crc64nvme: u64,
    pub blake3: String,
    pub size: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub etag: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub backend_version_id: Option<String>,
    /// The committed block list, in order (control API §4.2): the service
    /// checks it against the blob version's block list and the file's CRC.
    pub blocks: Vec<CompleteBlock>,
}

/// One entry of `complete`'s `blocks`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CompleteBlock {
    /// The base64 block id exactly as sent on the wire.
    pub id: String,
    pub length: u64,
    /// The block's CRC-64/NVME (as sent in `x-ms-content-crc64`), 16 hex digits.
    #[serde(with = "crc_hex")]
    pub crc64nvme: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct UploadCommitted {
    #[serde(with = "id")]
    pub version_id: u64,
    pub committed_at: String,
    #[serde(default)]
    pub retention_until: Option<String>,
}

// ---------------------------------------------------------------------------
// Stubs (§4.3)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StubPrepareRequest {
    #[serde(with = "id")]
    pub version_id: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StubLease {
    /// Opaque (`<expiry>.<MAC hex>`); sent back unchanged.
    pub lease_id: String,
    pub resident_bytes: u64,
    pub lease_expires_at: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StubCommitRequest {
    pub lease_id: String,
    #[serde(with = "id")]
    pub version_id: u64,
    pub resident_bytes: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StubSkipReason {
    VersionMismatch,
    OpenForWrite,
    LeaseRefused,
    LocalError,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StubSkipRequest {
    #[serde(with = "id")]
    pub version_id: u64,
    pub reason: StubSkipReason,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

// ---------------------------------------------------------------------------
// Recalls (§4.4)
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Via {
    Nfs,
    Smb,
    /// A process on the edge itself, caught by the smb-guard.
    Local,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Sec {
    Sys,
    Krb5,
    Krb5i,
    Krb5p,
    Ntlm,
    Kerberos,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Proof {
    Claimed,
    Proven,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Requester {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub uid: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sid: Option<String>,
    pub via: Via,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sec: Option<Sec>,
    pub proof: Proof,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_addr: Option<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OnOpen {
    Wait,
    Fail,
    Auto,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecallRequest {
    #[serde(with = "id")]
    pub file_id: u64,
    #[serde(with = "id")]
    pub version_id: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub offset: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub length: Option<u64>,
    pub requester: Requester,
    pub on_open: OnOpen,
    /// The reader asked for this version itself (a rewind placeholder): its
    /// recall is not ended when the file is replaced or deleted (§4.4).
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub pinned: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecallState {
    Requested,
    Rehydrating,
    Copying,
    Downloading,
    Available,
    Failed,
    Cancelled,
    /// The file was replaced or deleted while it was being recalled.
    Superseded,
    /// A state a newer service reports for a new backend kind. Display
    /// only: the `decision` still says wait or fail.
    #[serde(other)]
    Unknown,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Decision {
    Wait,
    Fail,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EtaConfidence {
    ProviderSla,
    Measured,
    None,
    /// A confidence a newer service reports; treated as no promise.
    #[serde(other)]
    Unknown,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum RecallResponse {
    /// Readable now: range-get it with the credential.
    Online { credential: Credential },
    /// A recall was started or joined.
    Recall {
        #[serde(with = "id")]
        recall_id: u64,
        state: RecallState,
        joined: bool,
        /// RFC 3339, or null when no time is promised (Azure, over 10 GB).
        #[serde(default)]
        eta: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        eta_confidence: Option<EtaConfidence>,
        decision: Decision,
        /// Text for `<file>.RECALLING.txt`.
        note: String,
    },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecallFilledRequest {
    #[serde(with = "id")]
    pub version_id: u64,
    pub resident_bytes: u64,
}

/// `POST …/recalls/{recall_id}/failed`: the edge could not fill it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RecallFailedRequest {
    #[serde(with = "id")]
    pub version_id: u64,
    pub reason: String,
}

// ---------------------------------------------------------------------------
// Reads (§4.7)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReadEntry {
    #[serde(with = "id")]
    pub file_id: u64,
    pub last_read_at: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReadsRequest {
    pub reads: Vec<ReadEntry>,
}

/// One page of a directory as it was at a time (`GET …/asof`). Entries are
/// raw `FileRow` JSON, parsed by the replica's parser.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AsofPage {
    pub entries: Vec<serde_json::Value>,
    pub more: bool,
}

/// One page of a dataset's snapshot (`control-api.md` §6). The first page
/// also carries `as_of` (read the feed from it afterwards), the dataset,
/// its shares and its ACLs. Rows are raw JSON, in the feed's shapes.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct SnapshotPage {
    #[serde(default)]
    pub as_of: Option<String>,
    #[serde(default)]
    pub dataset: Option<serde_json::Value>,
    #[serde(default)]
    pub shares: Vec<serde_json::Value>,
    #[serde(default)]
    pub acls: Vec<serde_json::Value>,
    pub files: Vec<serde_json::Value>,
    pub more: bool,
}

/// The reconciliation digest of a dataset or directory (`control-api.md`
/// §6), as of a feed cursor.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Digest {
    pub as_of: String,
    pub live_rows: u64,
    /// 16 hex digits.
    pub digest: String,
}

/// A bulk snapshot export (`control-api.md` §6): `parts` once `state` is
/// `ready`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SnapshotExport {
    #[serde(with = "id")]
    pub export_id: u64,
    /// `running`, `ready`, `failed` or `expired`.
    pub state: String,
    #[serde(default)]
    pub as_of: Option<String>,
    #[serde(default)]
    pub parts: Vec<ExportPart>,
}

/// One zstd-compressed NDJSON part of an export.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExportPart {
    /// A read SAS, valid 24 hours.
    pub url: String,
    /// NDJSON lines in the part.
    pub rows: u64,
    /// Size of the compressed part.
    pub bytes: u64,
    /// SHA-256 of the compressed part, hex.
    pub sha256: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReadsResponse {
    pub accepted: u64,
    #[serde(with = "id::vec")]
    pub unknown_file_ids: Vec<u64>,
}

/// Every endpoint this client calls, `(method, OpenAPI path)`. The contract
/// test checks each exists in `control-api.yaml`.
pub const ENDPOINTS: &[(&str, &str)] = &[
    ("post", "/v0/enrol"),
    ("post", "/v0/edges/{edge_id}/certificate"),
    ("post", "/v0/edges/{edge_id}/heartbeat"),
    ("get", "/v0/edges/{edge_id}/feed"),
    ("get", "/v0/edges/{edge_id}/asof"),
    ("post", "/v0/edges/{edge_id}/feed/ack"),
    ("post", "/v0/edges/{edge_id}/changes"),
    ("post", "/v0/edges/{edge_id}/uploads"),
    ("post", "/v0/edges/{edge_id}/uploads/{upload_id}/complete"),
    ("post", "/v0/edges/{edge_id}/uploads/{upload_id}/abort"),
    ("post", "/v0/edges/{edge_id}/credentials"),
    ("post", "/v0/edges/{edge_id}/files/{file_id}/stub/prepare"),
    ("post", "/v0/edges/{edge_id}/files/{file_id}/stub/commit"),
    ("post", "/v0/edges/{edge_id}/files/{file_id}/stub/skip"),
    ("post", "/v0/edges/{edge_id}/recalls"),
    ("post", "/v0/edges/{edge_id}/recalls/{recall_id}/filled"),
    ("post", "/v0/edges/{edge_id}/reads"),
];

#[cfg(test)]
mod unknown_variant_tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn unknown_credential_mode_deserializes_as_unsupported() {
        let c: Credential = serde_json::from_value(
            json!({"mode": "nfs_nas_mount", "host": "nas1", "export": "/x"}),
        )
        .unwrap();
        assert_eq!(c, Credential::Unsupported);
        assert!(c.is_unsupported());
        assert_eq!(c.sas_url(), None);
        assert_eq!(c.cache_until(), None);
    }

    #[test]
    fn a_presigned_url_may_carry_its_signed_if_match() {
        let c: Credential = serde_json::from_value(json!({
            "mode": "s3_presigned_url", "url": "https://b/k?X-Amz-Signature=s",
            "expires_at": "2099-01-01T00:00:00Z", "cache_until": "2099-01-01T00:00:00Z",
            "if_match": "\"e1\""
        }))
        .unwrap();
        let Credential::S3PresignedUrl { if_match, .. } = &c else {
            panic!("{c:?}")
        };
        assert_eq!(if_match.as_deref(), Some("\"e1\""));
    }

    #[test]
    fn an_s3_presigned_url_is_read_only_and_cached_like_a_sas() {
        let c: Credential = serde_json::from_value(json!({
            "mode": "s3_presigned_url",
            "url": "https://b.bucket.vpce-1.s3.eu-west-1.vpce.amazonaws.com/k?versionId=v&X-Amz-Signature=s",
            "expires_at": "2026-10-15T12:00:00Z", "cache_until": "2026-10-15T11:55:00Z"
        }))
        .unwrap();
        assert!(!c.is_unsupported());
        assert_eq!(c.sas_url(), None, "never an upload target");
        let (url, kind) = c.read_url().unwrap();
        assert!(url.starts_with("https://b.bucket.vpce-1."));
        assert_eq!(kind, ReadKind::S3Presigned);
        assert_eq!(c.cache_until(), Some("2026-10-15T11:55:00Z"));
        assert_eq!(c.crc64nvme(), None);
        // With the object's CRC (a file the replica has none for yet).
        let mut v = serde_json::to_value(&c).unwrap();
        v["crc64nvme"] = json!("00000000deadbeef");
        let with: Credential = serde_json::from_value(v).unwrap();
        assert_eq!(with.crc64nvme(), Some(0xdead_beef));
        assert_eq!(
            serde_json::to_value(&with).unwrap()["crc64nvme"],
            "00000000deadbeef"
        );
        for bad in ["deadbeef", "zz000000deadbeef"] {
            let mut v = serde_json::to_value(&c).unwrap();
            v["crc64nvme"] = json!(bad);
            assert!(serde_json::from_value::<Credential>(v).is_err(), "{bad}");
        }
        let p: Credential = serde_json::from_value(json!({
            "mode": "s3_roles_anywhere", "bucket": "b", "key": "k", "region": "r",
            "endpoint": "e"
        }))
        .unwrap();
        assert_eq!(
            p.read_url(),
            None,
            "a pointer, not a URL: the edge signs it"
        );
        // Defaults for an older service: private, TLS 1.2, virtual host.
        let Credential::S3RolesAnywhere {
            addressing,
            tls_max_version,
            private_network,
            requester_pays,
            etag,
            ..
        } = &p
        else {
            panic!("{p:?}")
        };
        assert_eq!(addressing, "virtual_host");
        assert_eq!(tls_max_version.as_deref(), Some("1.2"));
        assert!(*private_network && !*requester_pays && etag.is_none());
        let full: Credential = serde_json::from_value(json!({
            "mode": "s3_roles_anywhere", "bucket": "b", "key": "k", "etag": "\"e1\"",
            "crc64nvme": "00000000deadbeef", "region": "r", "endpoint": "e",
            "addressing": "path", "tls_max_version": null, "requester_pays": true,
            "private_network": false
        }))
        .unwrap();
        assert_eq!(full.crc64nvme(), Some(0xdead_beef));
        assert!(
            full.cache_until().is_none(),
            "a pointer is no secret: no expiry"
        );
    }

    #[test]
    fn unknown_mode_in_a_response_leaves_the_rest_readable() {
        let r: UploadStart = serde_json::from_value(json!({
            "action": "upload", "upload_id": "8", "version_id": "8",
            "target": {"mode": "nfs_nas_mount"}, "block_size": 1, "set_tier": false
        }))
        .unwrap();
        let UploadStart::Upload {
            target, version_id, ..
        } = r
        else {
            panic!("expected upload")
        };
        assert_eq!((version_id, target), (8, Credential::Unsupported));

        let r: RecallResponse = serde_json::from_value(json!({
            "status": "online", "credential": {"mode": "nfs_nas_mount"}
        }))
        .unwrap();
        assert_eq!(
            r,
            RecallResponse::Online {
                credential: Credential::Unsupported
            }
        );
    }

    #[test]
    fn known_modes_still_parse() {
        let c: Credential = serde_json::from_value(json!({
            "mode": "azure_user_delegation_sas", "url": "https://a/b?sig=x",
            "expires_at": "2099-01-01T00:00:00Z", "cache_until": "2099-01-01T00:00:00Z"
        }))
        .unwrap();
        assert_eq!(c.sas_url(), Some("https://a/b?sig=x"));
    }

    #[test]
    fn unknown_recall_state_and_eta_confidence_are_tolerated() {
        let s: RecallState = serde_json::from_value(json!("staging")).unwrap();
        assert_eq!(s, RecallState::Unknown);
        let s: RecallState = serde_json::from_value(json!("superseded")).unwrap();
        assert_eq!(s, RecallState::Superseded);
        let c: EtaConfidence = serde_json::from_value(json!("guess")).unwrap();
        assert_eq!(c, EtaConfidence::Unknown);
    }

    /// §4.1 `actor` (ported from line B): what the edge sends for an NFS
    /// user and for the reconcile pass (no identity at all), and that an op
    /// without one sends no `actor` key (older services ignore it either way).
    #[test]
    fn change_op_actor_shape() {
        let mut op = ChangeOp::new("7-12", ChangeKind::Unlink);
        op.file = Some(FileRef::Id { file_id: 9 });
        assert!(serde_json::to_value(&op).unwrap().get("actor").is_none());
        op.actor = Some(Actor {
            via: ActorVia::Nfs,
            uid: Some(41023),
            sid: Some("S-1-5-21-1-2-3-1107".into()),
            sec: Some(Sec::Sys),
            proof: Some(Proof::Claimed),
            client_addr: Some("10.1.2.3".into()),
        });
        assert_eq!(
            serde_json::to_value(&op).unwrap()["actor"],
            json!({"via": "nfs", "uid": 41023, "sid": "S-1-5-21-1-2-3-1107", "sec": "sys",
                   "proof": "claimed", "client_addr": "10.1.2.3"})
        );
        op.actor = Some(Actor::unknown());
        assert_eq!(
            serde_json::to_value(&op).unwrap()["actor"],
            json!({"via": "unknown"})
        );
        let back: ChangeOp = serde_json::from_value(serde_json::to_value(&op).unwrap()).unwrap();
        assert_eq!(back, op);
    }
}
