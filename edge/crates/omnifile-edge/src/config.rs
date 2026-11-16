//! Daemon configuration (`/etc/omnifile/edge.toml`).

use std::path::{Path, PathBuf};

use serde::Deserialize;

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// The xfs data volume. Shares are directories below it.
    #[serde(default = "d_data_root")]
    pub data_root: PathBuf,
    /// The private fill path (M2: a bind mount of `data_root` that the
    /// smb-guard ignores). Defaults to `data_root`.
    pub fill_root: Option<PathBuf>,
    #[serde(default = "d_replica")]
    pub replica_path: PathBuf,
    #[serde(default = "d_socket")]
    pub socket_path: PathBuf,
    /// This edge's id from enrolment (echo suppression). 0 = not enrolled.
    #[serde(default)]
    pub edge_id: u64,
    /// `service` (the control API through `omnifile-control`) or `fake`
    /// (in-memory, for development and tests).
    #[serde(default = "d_backend")]
    pub backend: String,
    #[serde(default)]
    pub fsal: FsalConfig,
    #[serde(default)]
    pub feed: FeedConfig,
    #[serde(default)]
    pub stub: StubConfig,
    #[serde(default)]
    pub fill: FillConfig,
    #[serde(default)]
    pub commit: CommitConfig,
    #[serde(default)]
    pub log: LogConfig,
    /// Settings handed to the control client unchanged (`omnifile-control`).
    #[serde(default)]
    pub service: toml::Table,
    /// The smb-guard (M2): fail-closed fanotify guard for SMB and local reads.
    #[serde(default)]
    pub smbguard: SmbGuardConfig,
    /// ACL scans reported to the service (control API §4.6).
    #[serde(default)]
    pub acl: AclScanConfig,
    /// Rewind trees (decision 30).
    #[serde(default)]
    pub rewind: RewindConfig,
    /// Private networking (decision 22), set by the setup wizard.
    #[serde(default)]
    pub network: NetworkConfig,
    /// Samba and Ganesha config for the shares (M6).
    #[serde(default)]
    pub shares: SharesConfig,
    /// Users' renames, deletes and owner changes reach the catalog
    /// (docs/design/namespace-sync.md).
    #[serde(default)]
    pub namespace: NamespaceConfig,
    /// M5: the SSD cache of ONLINE placeholders (chunk-range reads).
    #[serde(default)]
    pub cache: CacheConfig,
    /// M5: how the edge signs S3 reads (IAM Roles Anywhere).
    #[serde(default)]
    pub s3: S3Config,
    /// Annexes (`docs/design/annex.md`): read by `omnifile-annex` only.
    #[serde(default)]
    pub annex: crate::annex::AnnexConfig,
}

/// `[cache]` (M5): ONLINE ranges of placeholders fetched chunk by chunk
/// into the placeholder itself, for FSALs that read chunk maps (socket
/// v0.2) and the smb-guard. Off: line A's whole-file fills.
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct CacheConfig {
    pub enabled: bool,
    /// The SSD budget for cached bytes (chunks and header prefixes).
    pub capacity_gib: u64,
    /// The budget in MiB instead, when not 0 (small boxes and tests).
    pub capacity_mib: u64,
    /// Evict down to this share of the budget once it is exceeded.
    pub low_watermark_pct: u8,
    /// log2 of the chunk size for new maps (22 = 4 MiB). A file whose map
    /// would not fit an xattr gets a bigger one; a file keeps its own.
    pub chunk_shift: u8,
    /// Most chunks read ahead of a sequential reader (0: none).
    pub readahead_max_chunks: u32,
    /// The header prefix of an object the edge never held, fetched on
    /// first access and evicted last (MiB). Stubs keep their own prefix.
    pub header_mib: u64,
}

impl Default for CacheConfig {
    fn default() -> Self {
        CacheConfig {
            enabled: true,
            capacity_gib: 100,
            capacity_mib: 0,
            low_watermark_pct: 90,
            chunk_shift: 22,
            readahead_max_chunks: 16,
            header_mib: 4,
        }
    }
}

/// `[s3]` (M5): the edge reads S3 with SigV4 and temporary credentials from
/// IAM Roles Anywhere's `aws_signing_helper serve` (IMDSv2 on loopback; the
/// key in the TPM, `omnifile-aws-signing-helper.service`). No AWS secret is
/// ever configured here.
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct S3Config {
    /// The helper's endpoint: a loopback `http://` URL only.
    pub imds_url: String,
    /// Endpoint hosts (`host:port`) accepted besides interface endpoint
    /// names, over plain http: the sim only, honoured only with
    /// `OMNIFILE_BLOB_ALLOW_HTTP=1`.
    pub test_endpoints: Vec<String>,
    /// Decision 50: where the setup wizard leaves the Roles Anywhere
    /// certificate request when OmniFile's edge CA issues the certificate;
    /// the daemon requests and renews it there (`omnifile_control::roles_anywhere`).
    pub roles_anywhere_dir: PathBuf,
    /// Run after a new Roles Anywhere certificate is installed, so
    /// `aws_signing_helper` presents it.
    pub helper_restart: Vec<String>,
}

impl Default for S3Config {
    fn default() -> Self {
        S3Config {
            imds_url: omnifile_blob::s3::DEFAULT_IMDS_URL.into(),
            test_endpoints: Vec::new(),
            roles_anywhere_dir: omnifile_control::roles_anywhere::DIR.into(),
            helper_restart: [
                "systemctl",
                "restart",
                "omnifile-aws-signing-helper.service",
            ]
            .map(String::from)
            .to_vec(),
        }
    }
}

/// `[namespace]`.
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct NamespaceConfig {
    pub enabled: bool,
    /// Events are gathered this long before directories are reconciled.
    pub quiet_ms: u64,
    /// Reconcile every directory this often (0: never), and once at start.
    pub sweep_s: u64,
    pub sweep_at_start: bool,
    /// A create sent this long ago and never answered is sent again.
    pub create_resend_s: u64,
}

impl Default for NamespaceConfig {
    fn default() -> Self {
        NamespaceConfig {
            enabled: true,
            quiet_ms: 1000,
            sweep_s: 86_400,
            sweep_at_start: true,
            create_resend_s: 60,
        }
    }
}

/// `[shares]`: the daemon renders each share's Samba stanza and Ganesha
/// `EXPORT` block from its `share.upsert`, gives the share root its groups'
/// ACL, and reloads both servers (M6, docs/design/share-provisioning.md).
/// Off unless the setup wizard turns it on; the e2e lanes render by hand.
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct SharesConfig {
    pub render: bool,
    /// Included by `/etc/samba/omnifile-shares.conf`.
    pub smb_conf: PathBuf,
    /// Included by `/etc/ganesha/omnifile-exports.conf`.
    pub ganesha_conf: PathBuf,
    /// Run after either file changed. Both units reload on SIGHUP (Ganesha
    /// re-reads its exports); `try-reload-or-restart` leaves a stopped unit
    /// stopped.
    pub reload: Vec<String>,
    /// Run when an existing NFS export's rewind area (`Snap_Dir`) changed,
    /// which Ganesha takes only at a restart. NFS clients ride its grace
    /// period.
    pub restart: Vec<String>,
    /// `vfs_omnifile_prevver` in the SMB stanzas: `on`, `off`, or `auto`
    /// (when smbd's modules directory has it).
    pub smb_prevver: String,
    /// Give each share root its Owners/Writers/Readers ACL (access-control.md §4).
    pub root_acls: bool,
    /// Mark stubs offline for SMB clients (`FILE_ATTRIBUTE_OFFLINE` in Samba's
    /// `user.DOSATTRIB`), so Windows Explorer and the indexer don't open them
    /// and start recalls (`omnifile_marker::dosattr`).
    pub smb_offline: bool,
}

impl Default for SharesConfig {
    fn default() -> Self {
        SharesConfig {
            render: false,
            smb_conf: "/var/lib/omnifile/shares/smb.conf".into(),
            ganesha_conf: "/var/lib/omnifile/shares/ganesha.conf".into(),
            reload: [
                "systemctl",
                "try-reload-or-restart",
                "smbd.service",
                "nfs-ganesha-omni.service",
            ]
            .map(String::from)
            .to_vec(),
            restart: ["systemctl", "try-restart", "nfs-ganesha-omni.service"]
                .map(String::from)
                .to_vec(),
            smb_prevver: "auto".into(),
            root_acls: true,
            smb_offline: true,
        }
    }
}

/// `[network]`.
#[derive(Clone, Debug, Default, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct NetworkConfig {
    /// Reach storage (and the service) only at private addresses, never
    /// falling back to a public path: every name must resolve to a private
    /// endpoint (`omnifile_blob::net`).
    pub private_only: bool,
}

/// `[rewind]`: the rewind trees under `.omni/snap/` are a cache.
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct RewindConfig {
    /// A populated rewind directory unused this long is emptied and marked
    /// unpopulated again (`fsal-namespace.md` §7, "Eviction").
    pub idle_evict_s: u64,
}

impl Default for RewindConfig {
    fn default() -> Self {
        RewindConfig {
            idle_evict_s: 86_400,
        }
    }
}

/// `[acl]`: drift scans of the share trees (Explorer edits, `setfacl`, chmod).
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct AclScanConfig {
    /// Report ACLs to the service at all.
    pub enabled: bool,
    /// Full scan of every share this often (the nightly scan); the first one
    /// runs `first_full_s` after start.
    pub full_scan_s: u64,
    pub first_full_s: u64,
    /// On-change batches (files the detector saw written) this often.
    pub on_change_s: u64,
}

impl Default for AclScanConfig {
    fn default() -> Self {
        AclScanConfig {
            enabled: true,
            full_scan_s: 86_400,
            first_full_s: 300,
            on_change_s: 30,
        }
    }
}

/// `[smbguard]`: the fanotify pre-content guard that runs inside the daemon
/// (`omnifile-smbguard`, `edge/systemd/omnifile-edge.service.d/10-smbguard.conf`).
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct SmbGuardConfig {
    /// Start the guard first thing in `main`. Turning it off leaves SMB and
    /// local reads of stubs unguarded (holes read as zeros): only for hosts
    /// that serve no SMB and run no local readers (tests, NFS-only CI).
    pub enabled: bool,
    /// The data volume's xfs UUID, recorded at install (`blkid`). The guard
    /// refuses to mark a filesystem with any other UUID.
    pub fs_uuid: String,
    /// The private fill mount (the unit's `BindPaths=`); `fill_root` must
    /// name it so fills and stubs raise no events.
    pub fill_mount: PathBuf,
    /// The event journal (tmpfs, kept across restarts).
    pub journal_path: PathBuf,
    pub workers: usize,
    /// Re-check held events this often without a wake.
    pub poll_ms: u64,
    /// Above this much still to fetch, an access is "huge" (denied at once,
    /// the recall still starts).
    pub max_wait_mib: u64,
    /// `close` (`FAN_CLOSE_WRITE`), `modify` (`FAN_MODIFY`) or `off`.
    pub trunc_watch: String,
    /// Initialise new groups with `FAN_REPORT_PIDFD` (pid-reuse safe identity).
    pub report_pidfd: bool,
    /// With `enabled = false`, the daemon refuses to start while the smbd or
    /// Ganesha guard drop-ins are installed (they would serve stubs
    /// unguarded). This overrides that refusal; it is logged as critical.
    pub allow_disabled_with_servers: bool,
}

impl Default for SmbGuardConfig {
    fn default() -> Self {
        SmbGuardConfig {
            enabled: true,
            fs_uuid: String::new(),
            fill_mount: "/run/omnifile-fill".into(),
            journal_path: "/run/omnifile/smbguard/journal".into(),
            workers: 32,
            poll_ms: 2_000,
            // 10 GiB: never a silent wait for more (AGENTS.md, recall times).
            max_wait_mib: omnifile_smbguard::classify::DEFAULT_MAX_WAIT_BYTES >> 20,
            trunc_watch: "close".into(),
            report_pidfd: true,
            allow_disabled_with_servers: false,
        }
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct FsalConfig {
    pub allowed_uids: Vec<u32>,
    /// Full path `/proc/<pid>/exe` must equal for a peer; empty disables
    /// the check (tests only).
    pub required_exe: String,
    pub answer_ttl_ms: u32,
    pub park_ms_max: u32,
    pub handler_timeout_ms: u64,
    pub invalidate_timeout_ms: u64,
    /// `RECALL_STATE.valid_ms` for pushed recall states.
    pub recall_valid_ms: u32,
    /// This edge runs no Ganesha (SMB only). Only then may bytes be
    /// punched while no FSAL is connected; otherwise the stubber waits for
    /// a ready FSAL and every punch needs its acknowledgement.
    pub no_ganesha: bool,
    /// How long a fill's fence waits for the FSALs this edge expects (one
    /// per running `required_exe` process) to connect, after a daemon
    /// restart or a lost connection, before the fill fails closed. One bound
    /// per fill, however often it retries a broken fence. While it
    /// waits, readers keep waiting (the smb-guard holds SMB and local
    /// reads; a disconnected FSAL answers its policy default).
    pub reconnect_wait_ms: u64,
    /// After a Ganesha instance first connects (a Ganesha restart, or this
    /// daemon's), commits wait this long: NFSv4 clients reclaim their opens
    /// in grace, so the FSAL's open-state answer is not complete before
    /// then. Ganesha's lease (60 s) plus its grace period (90 s). It must
    /// stay at least Ganesha's `Grace_Period`, which can be set as high as
    /// 270 s: a shorter window believes an answer of 0 while clients may
    /// still be reclaiming.
    pub reclaim_window_s: u64,
}

impl Default for FsalConfig {
    fn default() -> Self {
        FsalConfig {
            allowed_uids: vec![0],
            required_exe: omnifile_fsal_proto::DEFAULT_GANESHA_EXE.into(),
            answer_ttl_ms: 1_000,
            park_ms_max: 10_000,
            handler_timeout_ms: 1_500,
            invalidate_timeout_ms: 1_000,
            recall_valid_ms: 30_000,
            no_ganesha: false,
            reconnect_wait_ms: 30_000,
            reclaim_window_s: 150,
        }
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct FeedConfig {
    pub limit: u32,
    pub wait_s: u32,
    pub retry_s: u64,
    /// A re-snapshot asks the service for a bulk export of each dataset
    /// (`control-api.md` §6) and waits this long for it to be ready before
    /// paging instead. 0: always page.
    pub export_wait_s: u64,
    /// How often each dataset is checked against the service's
    /// reconciliation digest (`control-api.md` §6); 0: never.
    pub reconcile_s: u64,
    /// The quiet interval between two service digests that must agree
    /// before a difference counts (writes racing the check change them).
    pub reconcile_quiet_s: u64,
    /// How often the edge reports health and checks whether its client
    /// certificate is due for renewal (`control-api.md` §4.7, §2); 0: never.
    pub heartbeat_s: u64,
}

impl Default for FeedConfig {
    fn default() -> Self {
        FeedConfig {
            limit: 1000,
            wait_s: 30,
            retry_s: 5,
            export_wait_s: 4 * 3600,
            reconcile_s: 24 * 3600,
            reconcile_quiet_s: 60,
            heartbeat_s: 60,
        }
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct StubConfig {
    pub batch: usize,
    pub interval_s: u64,
    /// Report `skip` after this many deferred attempts (unacknowledged
    /// invalidation).
    pub max_deferrals: u32,
}

impl Default for StubConfig {
    fn default() -> Self {
        StubConfig {
            batch: 64,
            interval_s: 10,
            max_deferrals: 5,
        }
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct FillConfig {
    pub chunk_mib: u64,
    pub widen_every_mib: u64,
}

impl Default for FillConfig {
    fn default() -> Self {
        FillConfig {
            chunk_mib: 8,
            widen_every_mib: 64,
        }
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct CommitConfig {
    /// Upload finished writes (detector / `WRITE_CLOSED`).
    pub enabled: bool,
    /// Run the fanotify detector on the data volume. Off in tests that feed
    /// `Daemon::commits` themselves.
    pub detect: bool,
    /// Quiet period after the last write before a file is committed, for a
    /// share with no period of its own (decision 67, `crate::quiet`).
    pub quiet_s: u64,
    /// The same, for a WORM share with no period of its own: a partial
    /// version there can't be removed until retention ends.
    pub worm_quiet_s: u64,
    /// A finished outbox op whose finish fails (the live file unreadable,
    /// the marker write refused) is tried again after this, doubling (WP9).
    pub outbox_finish_backoff_s: u64,
    /// After this many failed finishes the op is forgotten and its file
    /// committed again from the live copy. Its close stays pending in the
    /// pending-close log meanwhile, so nothing is lost.
    pub outbox_finish_attempts: u32,
}

impl Default for CommitConfig {
    fn default() -> Self {
        CommitConfig {
            enabled: true,
            detect: true,
            quiet_s: 10,
            worm_quiet_s: 60,
            outbox_finish_backoff_s: 10,
            outbox_finish_attempts: 8,
        }
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct LogConfig {
    /// `text` or `json`.
    pub format: String,
    /// `tracing_subscriber` filter; `RUST_LOG` overrides it.
    pub filter: String,
}

impl Default for LogConfig {
    fn default() -> Self {
        LogConfig {
            format: "json".into(),
            filter: "info".into(),
        }
    }
}

fn d_data_root() -> PathBuf {
    "/srv/omnifile/data".into()
}
fn d_replica() -> PathBuf {
    "/var/lib/omnifile/replica.db".into()
}
fn d_socket() -> PathBuf {
    omnifile_fsal_proto::DEFAULT_SOCKET_PATH.into()
}
fn d_backend() -> String {
    "service".into()
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("reading {path}: {err}")]
    Read { path: PathBuf, err: std::io::Error },
    #[error("parsing {path}: {err}")]
    Parse {
        path: PathBuf,
        err: Box<toml::de::Error>,
    },
    #[error("invalid config: {0}")]
    Invalid(String),
}

impl Config {
    /// `[service] key_protection`: how the edge's private keys are kept
    /// (`omnifile_control::seal`). `file` when unset, as before M6.
    pub fn key_protection(&self) -> Result<omnifile_control::seal::KeyProtection, ConfigError> {
        let v = match self.service.get("key_protection") {
            None => "file",
            Some(v) => v.as_str().ok_or_else(|| {
                ConfigError::Invalid("service.key_protection must be a string".into())
            })?,
        };
        omnifile_control::seal::KeyProtection::parse(v)
            .map_err(|e| ConfigError::Invalid(format!("service.{e}")))
    }

    pub fn from_toml(s: &str, path: &Path) -> Result<Config, ConfigError> {
        let c: Config = toml::from_str(s).map_err(|err| ConfigError::Parse {
            path: path.to_path_buf(),
            err: Box::new(err),
        })?;
        c.validate()?;
        Ok(c)
    }

    pub fn load(path: &Path) -> Result<Config, ConfigError> {
        let s = std::fs::read_to_string(path).map_err(|err| ConfigError::Read {
            path: path.to_path_buf(),
            err,
        })?;
        Self::from_toml(&s, path)
    }

    pub fn fill_root(&self) -> &Path {
        self.fill_root.as_deref().unwrap_or(&self.data_root)
    }

    fn validate(&self) -> Result<(), ConfigError> {
        for (name, p) in [
            ("data_root", &self.data_root),
            ("replica_path", &self.replica_path),
            ("socket_path", &self.socket_path),
        ] {
            if !p.is_absolute() {
                return Err(ConfigError::Invalid(format!(
                    "{name} must be absolute: {}",
                    p.display()
                )));
            }
        }
        if !matches!(self.backend.as_str(), "service" | "fake") {
            return Err(ConfigError::Invalid(format!(
                "backend must be `service` or `fake`, not `{}`",
                self.backend
            )));
        }
        self.key_protection()?;
        if !matches!(self.shares.smb_prevver.as_str(), "on" | "off" | "auto") {
            return Err(ConfigError::Invalid(
                "shares.smb_prevver must be `on`, `off` or `auto`".into(),
            ));
        }
        if self.shares.render
            && (!self.shares.smb_conf.is_absolute() || !self.shares.ganesha_conf.is_absolute())
        {
            return Err(ConfigError::Invalid(
                "shares.smb_conf and shares.ganesha_conf must be absolute".into(),
            ));
        }
        if !matches!(self.log.format.as_str(), "text" | "json") {
            return Err(ConfigError::Invalid(
                "log.format must be `text` or `json`".into(),
            ));
        }
        if self.fill.chunk_mib == 0 || self.fill.widen_every_mib == 0 {
            return Err(ConfigError::Invalid("sizes must be positive".into()));
        }
        if !(omnifile_marker::MIN_CHUNK_SHIFT..=omnifile_marker::MAX_CHUNK_SHIFT)
            .contains(&self.cache.chunk_shift)
        {
            return Err(ConfigError::Invalid(
                "cache.chunk_shift must be 12 (4 KiB) to 40 (1 TiB)".into(),
            ));
        }
        if self.cache.low_watermark_pct > 100 {
            return Err(ConfigError::Invalid(
                "cache.low_watermark_pct is at most 100".into(),
            ));
        }
        if !matches!(
            self.smbguard.trunc_watch.as_str(),
            "close" | "modify" | "off"
        ) {
            return Err(ConfigError::Invalid(
                "smbguard.trunc_watch must be `close`, `modify` or `off`".into(),
            ));
        }
        if self.fsal.allowed_uids.is_empty() {
            return Err(ConfigError::Invalid(
                "fsal.allowed_uids must not be empty".into(),
            ));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_from_empty_file() {
        let c = Config::from_toml("", Path::new("edge.toml")).unwrap();
        assert_eq!(c.data_root, PathBuf::from("/srv/omnifile/data"));
        assert_eq!(c.fill_root(), Path::new("/srv/omnifile/data"));
        assert_eq!(c.socket_path, PathBuf::from("/run/omnifile/fsal.sock"));
        assert_eq!(c.fsal.allowed_uids, vec![0]);
        assert_eq!(c.backend, "service");
        assert!(!c.network.private_only);
        let p = Config::from_toml("[network]\nprivate_only = true", Path::new("x")).unwrap();
        assert!(p.network.private_only);
        assert!(Config::from_toml("[network]\npublic = 1", Path::new("x")).is_err());
    }

    #[test]
    fn full_file_and_errors() {
        let c = Config::from_toml(
            r#"
            data_root = "/data"
            fill_root = "/run/omnifile/fill"
            edge_id = 7
            backend = "fake"
            [fsal]
            required_exe = ""
            [fill]
            chunk_mib = 4
            [log]
            format = "text"
            [service]
            api_url = "https://omnifile.internal"
            "#,
            Path::new("edge.toml"),
        )
        .unwrap();
        assert_eq!(c.fill_root(), Path::new("/run/omnifile/fill"));
        assert_eq!(c.fill.chunk_mib, 4);
        assert_eq!(
            c.service["api_url"].as_str(),
            Some("https://omnifile.internal")
        );
        assert!(Config::from_toml("bogus = 1", Path::new("x")).is_err());
        assert!(Config::from_toml("data_root = \"relative\"", Path::new("x")).is_err());
        assert!(Config::from_toml("backend = \"s3\"", Path::new("x")).is_err());
    }
}
