//! Per-share Samba and Ganesha configuration, rendered from the catalog's
//! `share.upsert` row (access-control.md §3–4). The global sections are the
//! templates in `edge/config/`.
//!
//! - **SMB:** `valid users` / `read list` / `write list` name the share's AD
//!   groups **by SID** (Samba matches a SID against the session token, so a
//!   renamed group still works), with access-based enumeration and
//!   `hide unreadable`. Never `vfs_acl_xattr`.
//! - **NFS:** `Manage_Gids = true` everywhere (the client's group list is
//!   replaced by a winbind lookup), `SecType` from the share, and for
//!   host-trusted shares a `CLIENT` block per named host or subnet with
//!   `Squash = Root_Squash`. Ganesha has no per-user allow-list; the share
//!   root's ACL does that job.

use std::fmt::Write as _;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use omnifile_identity::{NfsSec, Sid, TrustLevel};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Protocol {
    Nfs3,
    Nfs4,
    Smb,
}

/// Seconds Ganesha may serve cached attributes and ACLs before re-reading
/// them (`Attr_Expiration_Time`): the bound on how long an ACL edit made over
/// SMB takes to be enforced over NFS. Measured in tests/acl (explorer phase).
pub const ATTR_EXPIRATION_S: u32 = 10;

/// The FSAL_OMNI open policy for the export (`fsal/README.md`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FsalPolicy {
    Wait,
    FailFast,
    WaitQuick { ms: u64 },
}

impl FsalPolicy {
    fn render(&self) -> String {
        match self {
            FsalPolicy::Wait => "wait".into(),
            FsalPolicy::FailFast => "failfast".into(),
            FsalPolicy::WaitQuick { ms } => format!("waitquick:{ms}"),
        }
    }
}

/// One share, as the edge renders it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ShareConfig {
    pub name: String,
    pub path: PathBuf,
    pub protocols: Vec<Protocol>,
    pub nfs_sec: Vec<NfsSec>,
    /// Hosts, IPs or CIDR subnets (Ganesha `Clients` syntax).
    pub nfs_clients: Vec<String>,
    pub root_squash: bool,
    /// What the catalog says; must equal what the settings imply.
    pub trust_level: TrustLevel,
    pub owners_sid: Sid,
    pub writers_sid: Option<Sid>,
    pub readers_sid: Option<Sid>,
    /// Some folder carries a restricted AD group (§3 policy check).
    pub restricted: bool,
    /// An admin accepted a host-trusted share with restricted folders. Audited.
    pub host_trusted_override: bool,
    pub export_id: u16,
    pub fsal_policy: FsalPolicy,
    /// SMB3 encryption required (`smb encrypt = required`).
    pub smb_encrypt: bool,
    /// The share's rewind area (`<data root>/.omni/snap/<share>`), shown as
    /// Windows Previous Versions through `vfs_shadow_copy2` (decision 30;
    /// `fsal-namespace.md` §7). `None`: the share keeps no rewind depth.
    pub snapdir: Option<PathBuf>,
    /// `vfs_omnifile_prevver` (`samba-vfs/`) is installed in smbd's modules
    /// directory. It is stacked before `shadow_copy2` on shares with a rewind
    /// area, so a file opened directly in an unfilled label is filled first
    /// (decision 36). Never set it when the module isn't there: a share that
    /// names a module smbd can't load fails to connect at all. (smbd loads
    /// modules by name only; it refuses a path with a `/`.)
    pub smb_prevver: bool,
}

/// The module's name in `vfs objects`.
pub const PREVVER_MODULE: &str = "omnifile_prevver";

/// Is `vfs_omnifile_prevver` in the modules directory of the installed smbd
/// (`smbd -b`)? Only then may a stanza name it ([`ShareConfig::smb_prevver`]).
pub fn prevver_installed() -> bool {
    let Ok(out) = std::process::Command::new("smbd").arg("-b").output() else {
        return false;
    };
    let text = String::from_utf8_lossy(&out.stdout);
    text.lines()
        .find_map(|l| l.trim().strip_prefix("MODULESDIR:"))
        .is_some_and(|dir| {
            std::path::Path::new(dir.trim())
                .join("vfs")
                .join(format!("{PREVVER_MODULE}.so"))
                .is_file()
        })
}

#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum ConfigError {
    #[error("share name {0:?} is not a valid share name (letters, digits, '-', '_', '.', max 80)")]
    Name(String),
    #[error("path {0:?} must be absolute and printable, without quotes")]
    Path(String),
    #[error("NFS is enabled but no security flavour is set")]
    NoNfsSec,
    #[error("trust level is {declared:?} but the settings make it {actual:?}")]
    TrustMismatch {
        declared: TrustLevel,
        actual: TrustLevel,
    },
    #[error("host-trusted (sec=sys) shares need a list of named hosts or subnets")]
    NoClients,
    #[error(
        "client {0:?} is not allowed: no wildcards or catch-all subnets on host-trusted shares"
    )]
    WildClient(String),
    #[error("client {0:?} contains characters Ganesha config cannot carry")]
    BadClient(String),
    #[error("host-trusted shares must squash root (Root_Squash)")]
    NoRootSquash,
    #[error("a share with restricted folders cannot be host-trusted without an admin override")]
    RestrictedHostTrusted,
    #[error("no protocol enabled")]
    NoProtocol,
    #[error("export id 0 and 1 are reserved by Ganesha")]
    ExportId,
}

/// The shortest network a host-trusted share may name: anything wider is a
/// wildcard by another name (`0.0.0.0/1` and `128.0.0.0/1` are all of IPv4).
const MIN_PREFIX_V4: u32 = 8;
const MIN_PREFIX_V6: u32 = 32;

fn is_wild(c: &str) -> bool {
    let c = c.trim();
    let too_wide = c.rsplit_once('/').is_some_and(|(addr, len)| {
        let min = if addr.contains(':') {
            MIN_PREFIX_V6
        } else {
            MIN_PREFIX_V4
        };
        len.parse::<u32>().map_or(true, |l| l < min)
    });
    too_wide
        || c.contains('*')
        || c.contains('?')
        || c.contains('[')
        || c.eq_ignore_ascii_case("all")
        || c.eq_ignore_ascii_case("everyone")
        || c == "0.0.0.0"
        || c == "::"
}

impl ShareConfig {
    pub fn nfs_enabled(&self) -> bool {
        self.protocols
            .iter()
            .any(|p| matches!(p, Protocol::Nfs3 | Protocol::Nfs4))
    }

    pub fn smb_enabled(&self) -> bool {
        self.protocols.contains(&Protocol::Smb)
    }

    /// The trust level the settings imply (the weakest way in).
    pub fn actual_trust(&self) -> TrustLevel {
        TrustLevel::of(self.nfs_enabled(), &self.nfs_sec)
    }

    /// Every rule from access-control.md §3, plus what keeps the rendered
    /// config well-formed. All violations are returned, not just the first.
    pub fn validate(&self) -> Result<(), Vec<ConfigError>> {
        let mut e = Vec::new();
        let n = &self.name;
        // A leading letter or digit, as the service requires: never `.`,
        // `..` or a hidden name (the root folder is named after the share).
        if !n.as_bytes().first().is_some_and(u8::is_ascii_alphanumeric)
            || n.len() > 80
            || !n
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
            || n.eq_ignore_ascii_case("global")
            || n.eq_ignore_ascii_case("homes")
            || n.eq_ignore_ascii_case("printers")
        {
            e.push(ConfigError::Name(n.clone()));
        }
        for path in std::iter::once(&self.path).chain(self.snapdir.as_ref()) {
            let p = path.to_string_lossy();
            // `%` is a Samba substitution (`%U`, `%$(VAR)`) and smbd trims a
            // trailing space, so either would serve another directory than
            // the one the edge gave the share's ACL.
            if !path.is_absolute()
                || p.ends_with(char::is_whitespace)
                || p.chars()
                    .any(|c| c.is_control() || c == '"' || c == '\\' || c == ';' || c == '%')
            {
                e.push(ConfigError::Path(p.into_owned()));
            }
        }
        if self.protocols.is_empty() {
            e.push(ConfigError::NoProtocol);
        }
        if self.export_id < 2 {
            e.push(ConfigError::ExportId);
        }
        let actual = self.actual_trust();
        if actual != self.trust_level {
            e.push(ConfigError::TrustMismatch {
                declared: self.trust_level,
                actual,
            });
        }
        if self.nfs_enabled() {
            if self.nfs_sec.is_empty() {
                e.push(ConfigError::NoNfsSec);
            }
            for c in &self.nfs_clients {
                if c.is_empty()
                    || !c
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"-_.:/".contains(&b))
                {
                    e.push(ConfigError::BadClient(c.clone()));
                }
            }
            if actual == TrustLevel::HostTrusted {
                if self.nfs_clients.is_empty() {
                    e.push(ConfigError::NoClients);
                }
                for c in self.nfs_clients.iter().filter(|c| is_wild(c)) {
                    e.push(ConfigError::WildClient(c.clone()));
                }
                if !self.root_squash {
                    e.push(ConfigError::NoRootSquash);
                }
                if self.restricted && !self.host_trusted_override {
                    e.push(ConfigError::RestrictedHostTrusted);
                }
            }
        }
        if e.is_empty() { Ok(()) } else { Err(e) }
    }

    /// The share's `[name]` stanza for `/etc/samba/omnifile-shares.conf`.
    pub fn render_smb(&self) -> Option<String> {
        if !self.smb_enabled() {
            return None;
        }
        let mut all = vec![self.owners_sid.to_string()];
        let mut write = vec![self.owners_sid.to_string()];
        let mut read = Vec::new();
        if let Some(w) = &self.writers_sid {
            all.push(w.to_string());
            write.push(w.to_string());
        }
        if let Some(r) = &self.readers_sid {
            all.push(r.to_string());
            read.push(r.to_string());
        }
        let mut s = String::new();
        let _ = writeln!(s, "# share {} (export id {})", self.name, self.export_id);
        let _ = writeln!(s, "[{}]", self.name);
        let _ = writeln!(s, "    path = {}", self.path.display());
        let _ = writeln!(s, "    read only = no");
        let _ = writeln!(s, "    browseable = yes");
        let _ = writeln!(s, "    guest ok = no");
        let _ = writeln!(s, "    valid users = {}", all.join(" "));
        let _ = writeln!(s, "    write list = {}", write.join(" "));
        if !read.is_empty() {
            let _ = writeln!(s, "    read list = {}", read.join(" "));
        }
        let _ = writeln!(s, "    access based share enum = yes");
        let _ = writeln!(s, "    hide unreadable = yes");
        let _ = writeln!(s, "    nt acl support = yes");
        let _ = writeln!(s, "    inherit acls = yes");
        let _ = writeln!(s, "    inherit owner = no");
        // Stubs carry FILE_ATTRIBUTE_OFFLINE in Samba's DOS attributes
        // (omnifile_marker::dosattr), so Explorer and the indexer leave them
        // unopened; Samba's default, made explicit so a global setting can't
        // drop it.
        let _ = writeln!(s, "    store dos attributes = yes");
        // Windows Offline Files would cache the share, reading (and so
        // recalling) every file a user marks "always available offline".
        let _ = writeln!(s, "    csc policy = disable");
        match &self.snapdir {
            // Previous Versions: the labels under the rewind area, read-only
            // placeholders filled on access (fsal-namespace.md §7). Checked
            // under a real smbd by tests/shadow_copy2.sh. vfs_omnifile_prevver
            // goes first, so it sees a @GMT path before shadow_copy2 maps it.
            Some(snap) => {
                if self.smb_prevver {
                    let _ = writeln!(
                        s,
                        "    vfs objects = full_audit {PREVVER_MODULE} shadow_copy2"
                    );
                } else {
                    let _ = writeln!(s, "    vfs objects = full_audit shadow_copy2");
                }
                let _ = writeln!(s, "    shadow:snapdir = {}", snap.display());
                let _ = writeln!(s, "    shadow:basedir = {}", self.path.display());
                let _ = writeln!(s, "    shadow:format = %Y-%m-%dT%H%MZ");
                let _ = writeln!(s, "    shadow:localtime = no");
                let _ = writeln!(s, "    shadow:sort = desc");
            }
            None => {
                let _ = writeln!(s, "    vfs objects = full_audit");
            }
        }
        if self.smb_encrypt {
            let _ = writeln!(s, "    smb encrypt = required");
        }
        Some(s)
    }

    /// The share's `EXPORT { … }` block for Ganesha.
    pub fn render_ganesha(&self) -> Option<String> {
        if !self.nfs_enabled() {
            return None;
        }
        let mut vers = Vec::new();
        if self.protocols.contains(&Protocol::Nfs3) {
            vers.push("3");
        }
        if self.protocols.contains(&Protocol::Nfs4) {
            vers.push("4");
        }
        let mut secs = self.nfs_sec.clone();
        secs.sort();
        secs.dedup();
        let sec = secs
            .iter()
            .map(|s| s.ganesha_name())
            .collect::<Vec<_>>()
            .join(", ");
        let squash = if self.root_squash {
            "Root_Squash"
        } else {
            "No_Root_Squash"
        };
        let host_trusted = self.actual_trust() == TrustLevel::HostTrusted;
        let mut s = String::new();
        let _ = writeln!(s, "# share {} ({:?})", self.name, self.actual_trust());
        let _ = writeln!(s, "EXPORT {{");
        let _ = writeln!(s, "    Export_Id = {};", self.export_id);
        let _ = writeln!(s, "    Path = \"{}\";", self.path.display());
        let _ = writeln!(s, "    Pseudo = \"/{}\";", self.name);
        let _ = writeln!(s, "    Protocols = {};", vers.join(", "));
        let _ = writeln!(s, "    Transports = TCP;");
        let _ = writeln!(s, "    SecType = {sec};");
        let _ = writeln!(s, "    Squash = {squash};");
        let _ = writeln!(s, "    Manage_Gids = true;");
        // Host-trusted: only the listed hosts get in. Proven: any host with a
        // valid Kerberos ticket, unless a client list narrows it.
        let open = !host_trusted && self.nfs_clients.is_empty();
        let _ = writeln!(s, "    Access_Type = {};", if open { "RW" } else { "None" });
        let _ = writeln!(s, "    Disable_ACL = false;");
        // Ganesha caches attributes, ACLs included, for 60 s by default. ACLs
        // also change outside Ganesha (Explorer over SMB), so bound how long
        // NFS keeps enforcing a stale one.
        let _ = writeln!(s, "    Attr_Expiration_Time = {ATTR_EXPIRATION_S};");
        // The rewind trees, shown to NFS clients as `.snapshots` in the
        // share's root (`fsal-namespace.md` §7).
        let snap = match &self.snapdir {
            Some(d) => format!(" Snap_Dir = \"{}\";", d.display()),
            None => String::new(),
        };
        let _ = writeln!(
            s,
            "    FSAL {{ Name = OMNI; Policy = \"{}\";{snap} FSAL {{ Name = VFS; }} }}",
            self.fsal_policy.render()
        );
        if !self.nfs_clients.is_empty() {
            let _ = writeln!(s, "    CLIENT {{");
            let _ = writeln!(s, "        Clients = {};", self.nfs_clients.join(", "));
            let _ = writeln!(s, "        Access_Type = RW;");
            let _ = writeln!(s, "        SecType = {sec};");
            let _ = writeln!(s, "        Squash = {squash};");
            let _ = writeln!(s, "        Manage_Gids = true;");
            let _ = writeln!(s, "    }}");
        }
        let _ = writeln!(s, "}}");
        Some(s)
    }
}

/// A problem in an smb.conf that breaks the one-permission-model rule.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SmbConfFinding {
    pub section: String,
    pub line: usize,
    pub message: String,
}

/// Checks an smb.conf (globals and shares) for settings that would create a
/// second permission store or let unauthenticated users in:
/// `vfs objects` with `acl_xattr` or `acl_tdb`, `nt acl support = no`,
/// `guest ok = yes`, `map to guest` other than `never`.
pub fn check_smb_conf(text: &str) -> Vec<SmbConfFinding> {
    let mut out = Vec::new();
    let mut section = String::from("global");
    for (i, raw) in text.lines().enumerate() {
        let l = raw.trim();
        if l.is_empty() || l.starts_with('#') || l.starts_with(';') {
            continue;
        }
        if l.starts_with('[') && l.ends_with(']') {
            section = l[1..l.len() - 1].trim().to_lowercase();
            continue;
        }
        let Some((k, v)) = l.split_once('=') else {
            continue;
        };
        let k: String = k
            .to_lowercase()
            .chars()
            .filter(|c| !c.is_whitespace() && *c != '_')
            .collect();
        let v = v.trim().to_lowercase();
        let mut flag = |m: &str| {
            out.push(SmbConfFinding {
                section: section.clone(),
                line: i + 1,
                message: m.to_string(),
            })
        };
        match k.as_str() {
            "vfsobjects" | "vfsobject" => {
                for m in v.split(|c: char| c.is_whitespace() || c == ',') {
                    if m == "acl_xattr" || m == "acl_tdb" {
                        flag(&format!(
                            "vfs module {m} stores a second (Windows) ACL that NFS never enforces"
                        ));
                    }
                }
            }
            "ntaclsupport" if ["no", "false", "0"].contains(&v.as_str()) => {
                flag("nt acl support = no hides the POSIX ACLs from Explorer")
            }
            "guestok" | "public" if ["yes", "true", "1"].contains(&v.as_str()) => {
                flag("guest access bypasses AD")
            }
            "maptoguest" if v != "never" => flag("map to guest must be never"),
            _ => {}
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sid(rid: u32) -> Sid {
        Sid::parse(&format!("S-1-5-21-3190051465-787089565-3716134449-{rid}")).unwrap()
    }

    pub(crate) fn proven_share() -> ShareConfig {
        ShareConfig {
            name: "seismic".into(),
            path: "/srv/omnifile/data/seismic".into(),
            protocols: vec![Protocol::Nfs4, Protocol::Smb],
            nfs_sec: vec![NfsSec::Krb5p, NfsSec::Krb5],
            nfs_clients: vec![],
            root_squash: true,
            trust_level: TrustLevel::Proven,
            owners_sid: sid(1105),
            writers_sid: Some(sid(1104)),
            readers_sid: Some(sid(1103)),
            restricted: true,
            host_trusted_override: false,
            export_id: 10,
            fsal_policy: FsalPolicy::FailFast,
            smb_encrypt: false,
            snapdir: None,
            smb_prevver: false,
        }
    }

    fn host_trusted() -> ShareConfig {
        ShareConfig {
            name: "batch".into(),
            protocols: vec![Protocol::Nfs3, Protocol::Nfs4],
            nfs_sec: vec![NfsSec::Sys],
            nfs_clients: vec!["10.20.0.0/24".into(), "hpc01.omni.test".into()],
            trust_level: TrustLevel::HostTrusted,
            restricted: false,
            fsal_policy: FsalPolicy::Wait,
            export_id: 11,
            ..proven_share()
        }
    }

    #[test]
    fn a_share_with_rewind_shows_previous_versions() {
        let mut s = proven_share();
        s.snapdir = Some("/srv/omnifile/data/.omni/snap/seismic".into());
        s.validate().unwrap();
        let t = s.render_smb().unwrap();
        assert!(
            t.contains("    vfs objects = full_audit shadow_copy2\n"),
            "{t}"
        );
        assert!(t.contains("    shadow:snapdir = /srv/omnifile/data/.omni/snap/seismic\n"));
        assert!(t.contains("    shadow:basedir = /srv/omnifile/data/seismic\n"));
        // The label format the daemon names directories with.
        assert!(t.contains("    shadow:format = %Y-%m-%dT%H%MZ\n"));
        assert!(t.contains("    shadow:localtime = no\n"));
        // Without rewind: no shadow_copy2.
        assert!(!proven_share().render_smb().unwrap().contains("shadow"));
        // With vfs_omnifile_prevver installed: stacked before shadow_copy2,
        // and only on shares with a rewind area.
        s.smb_prevver = true;
        s.validate().unwrap();
        assert!(
            s.render_smb()
                .unwrap()
                .contains("    vfs objects = full_audit omnifile_prevver shadow_copy2\n")
        );
        let mut plain = proven_share();
        plain.smb_prevver = true;
        assert!(!plain.render_smb().unwrap().contains("omnifile_prevver"));
        s.smb_prevver = false;
        // NFS: the FSAL shows the same area as .snapshots.
        let g = s.render_ganesha().unwrap();
        assert!(
            g.contains(
                "FSAL { Name = OMNI; Policy = \"failfast\"; \
                 Snap_Dir = \"/srv/omnifile/data/.omni/snap/seismic\"; FSAL { Name = VFS; } }"
            ),
            "{g}"
        );
        assert!(
            !proven_share()
                .render_ganesha()
                .unwrap()
                .contains("Snap_Dir")
        );
        // The snapdir is checked like the path.
        s.snapdir = Some("relative/snap".into());
        assert!(s.validate().is_err());
        s.snapdir = Some("/srv/x;rm".into());
        assert!(s.validate().is_err());
        // A Samba substitution, or a trailing space smbd trims (M7 review).
        s.snapdir = None;
        for bad in ["/srv/data/%U", "/srv/data/proj ", "/srv/data/%$(HOME)"] {
            s.path = bad.into();
            assert!(s.validate().is_err(), "{bad}");
        }
    }

    #[test]
    fn valid_shares() {
        proven_share().validate().unwrap();
        host_trusted().validate().unwrap();
    }

    #[test]
    fn host_trusted_rules() {
        let mut s = host_trusted();
        s.nfs_clients = vec!["*".into(), "10.0.0.0/0".into()];
        s.root_squash = false;
        s.restricted = true;
        let e = s.validate().unwrap_err();
        assert!(e.contains(&ConfigError::WildClient("*".into())));
        assert!(e.contains(&ConfigError::WildClient("10.0.0.0/0".into())));
        assert!(e.contains(&ConfigError::NoRootSquash));
        assert!(e.contains(&ConfigError::RestrictedHostTrusted));
        s.nfs_clients.clear();
        assert!(s.validate().unwrap_err().contains(&ConfigError::NoClients));
        let mut o = host_trusted();
        o.restricted = true;
        o.host_trusted_override = true;
        o.validate().unwrap();
        // A network so wide it is a wildcard by another name (M7 review):
        // 0.0.0.0/1 and 128.0.0.0/1 are all of IPv4.
        for wide in [
            "0.0.0.0/1",
            "128.0.0.0/1",
            "10.0.0.0/7",
            "::/1",
            "2001:db8::/31",
            "10.0.0.0/x",
        ] {
            let mut w = host_trusted();
            w.nfs_clients = vec![wide.into()];
            assert!(
                w.validate()
                    .unwrap_err()
                    .contains(&ConfigError::WildClient(wide.into())),
                "{wide}"
            );
        }
        for ok in ["10.0.0.0/8", "192.168.10.0/24", "2001:db8::/32", "10.1.2.3"] {
            let mut w = host_trusted();
            w.nfs_clients = vec![ok.into()];
            w.validate().unwrap();
        }
    }

    #[test]
    fn trust_level_must_match_settings() {
        let mut s = proven_share();
        s.nfs_sec.push(NfsSec::Sys);
        let e = s.validate().unwrap_err();
        assert!(e.contains(&ConfigError::TrustMismatch {
            declared: TrustLevel::Proven,
            actual: TrustLevel::HostTrusted
        }));
    }

    #[test]
    fn injection_is_refused() {
        let mut s = host_trusted();
        s.name = "x]\n[global".into();
        s.path = "/srv/a\"; Path = \"/".into();
        s.nfs_clients = vec!["10.0.0.1; } EXPORT {".into()];
        let e = s.validate().unwrap_err();
        assert!(matches!(e[0], ConfigError::Name(_)));
        assert!(e.iter().any(|x| matches!(x, ConfigError::Path(_))));
        assert!(e.iter().any(|x| matches!(x, ConfigError::BadClient(_))));
    }

    #[test]
    fn a_share_name_starts_with_a_letter_or_digit() {
        for bad in [".", "..", ".omni", "-x", "_x"] {
            let mut s = host_trusted();
            s.name = bad.into();
            let e = s.validate().unwrap_err();
            assert!(matches!(e[0], ConfigError::Name(_)), "{bad}");
        }
        let mut s = host_trusted();
        s.name = "2026-projects".into();
        assert!(s.validate().is_ok());
    }

    #[test]
    fn smb_stanza() {
        let t = proven_share().render_smb().unwrap();
        let d = "S-1-5-21-3190051465-787089565-3716134449";
        assert!(t.contains("[seismic]\n"));
        assert!(t.contains(&format!("valid users = {d}-1105 {d}-1104 {d}-1103\n")));
        assert!(t.contains(&format!("write list = {d}-1105 {d}-1104\n")));
        assert!(t.contains(&format!("read list = {d}-1103\n")));
        assert!(t.contains("hide unreadable = yes"));
        assert!(t.contains("access based share enum = yes"));
        assert!(t.contains("vfs objects = full_audit\n"));
        // Stubs show as offline to Windows, and Offline Files never caches.
        assert!(t.contains("    store dos attributes = yes\n"));
        assert!(t.contains("    csc policy = disable\n"));
        assert!(check_smb_conf(&t).is_empty());
        assert!(host_trusted().render_smb().is_none());
    }

    #[test]
    fn ganesha_proven_export() {
        let t = proven_share().render_ganesha().unwrap();
        assert!(t.contains("SecType = krb5, krb5p;"), "{t}");
        assert!(t.contains("Manage_Gids = true;"));
        assert!(t.contains("Attr_Expiration_Time = 10;"));
        assert!(t.contains("Squash = Root_Squash;"));
        assert!(t.contains("Access_Type = RW;"));
        assert!(t.contains("Protocols = 4;"));
        assert!(t.contains("Policy = \"failfast\""));
        assert!(!t.contains("CLIENT"));
    }

    #[test]
    fn ganesha_host_trusted_export() {
        let t = host_trusted().render_ganesha().unwrap();
        assert!(t.contains("    Access_Type = None;"), "{t}");
        assert!(t.contains("Clients = 10.20.0.0/24, hpc01.omni.test;"));
        assert!(t.contains("        SecType = sys;"));
        assert!(t.contains("        Squash = Root_Squash;"));
        assert!(t.contains("        Manage_Gids = true;"));
        assert!(t.contains("Protocols = 3, 4;"));
        assert!(t.contains("Policy = \"wait\""));
        assert_eq!(t.matches('{').count(), t.matches('}').count());
    }

    #[test]
    fn smb_conf_checks() {
        let bad = "[global]\n  vfs objects = full_audit acl_xattr\n  map to guest = Bad User\n\
                   [x]\n  nt acl support = no\n  guest ok = yes\n  ; vfs objects = acl_xattr\n\
                   [y]\n  vfs_objects = acl_tdb\n";
        let f = check_smb_conf(bad);
        assert_eq!(f.len(), 5, "{f:?}");
        assert_eq!(f[0].section, "global");
        assert_eq!(f[0].line, 2);
        assert_eq!(f[4].section, "y");
        let tmpl = include_str!("../../../config/samba/smb.conf.tmpl");
        assert!(
            check_smb_conf(tmpl).is_empty(),
            "{:?}",
            check_smb_conf(tmpl)
        );
    }
}
