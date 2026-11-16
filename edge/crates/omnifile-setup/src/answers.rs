//! The answers file (`/etc/omnifile/setup.toml`): everything the wizard asks,
//! in one place, so a setup can be repeated, reviewed or automated.
//!
//! It never holds the domain join password: that is asked on the console, or
//! read from the systemd credential `omnifile.join-password` when setup runs
//! unattended. The enrolment bundle is a one-time code: it is removed from
//! the saved file once the edge has enrolled.

use std::net::IpAddr;

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Answers {
    /// The file's format. Only 1 exists.
    pub version: u32,
    /// This filer's name on the network (and its NetBIOS name, upper-cased).
    pub hostname: String,
    pub network: Network,
    pub data: Data,
    pub ad: Ad,
    #[serde(default)]
    pub storage: Storage,
    #[serde(default)]
    pub service: Service,
    #[serde(default)]
    pub updates: Updates,
    /// IAM Roles Anywhere, for reads of files still in Amazon S3 (decision
    /// 43). Left out: no S3 reads by Roles Anywhere (presigned URLs from the
    /// service are the fallback).
    #[serde(default, skip_serializing_if = "Aws::is_empty")]
    pub aws: Aws,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Network {
    /// `dhcp` or `static`.
    pub mode: String,
    /// The interface to configure (`static` with more than one, or to pin
    /// DHCP to one). Empty: every wired interface (`dhcp`), or the only one.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub interface: String,
    /// `static`: address with prefix length, e.g. `10.1.2.10/24`.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub address: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub gateway: String,
    /// DNS servers: with AD, normally the domain controllers.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub dns: Vec<String>,
    /// Search domains. The AD domain is always added.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub search: Vec<String>,
    /// Time servers. Empty: the AD domain's DCs (Kerberos needs the clocks
    /// within 5 minutes).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub ntp: Vec<String>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Data {
    /// The empty disk the shares live on, e.g. `/dev/sdb`. It is formatted
    /// (xfs) the first time, and only if it holds no filesystem or partition
    /// table, unless `erase` is set.
    pub device: String,
    #[serde(default, skip_serializing_if = "is_false")]
    pub erase: bool,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Ad {
    /// The AD DNS domain, e.g. `corp.example`. The Kerberos realm is its upper case.
    pub domain: String,
    /// The short (NetBIOS) domain name, e.g. `CORP`.
    pub workgroup: String,
    /// An account allowed to join computers to the domain.
    pub join_user: String,
    /// The OU the computer account is created in. Empty: the domain default.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub join_ou: String,
    /// The AD group whose members administer this filer (sudo, ssh).
    pub admin_group: String,
    /// uid/gid range for the domain's RFC2307 attributes (idmap_ad).
    #[serde(default = "d_idmap_range")]
    pub idmap_range: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Storage {
    /// Reach storage only through private endpoints (decision 22).
    pub private_endpoints: bool,
}

impl Default for Storage {
    fn default() -> Self {
        Storage {
            private_endpoints: true,
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Service {
    /// The one-time enrolment bundle from the portal (`ofe1.…`). Removed
    /// from the saved answers once the edge has enrolled.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub bundle: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Updates {
    /// The update feed (`appliance/README.md`, "Updates"). Empty: no updates
    /// until one is set.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub feed: String,
    /// When the filer may restart to apply a downloaded update: a systemd
    /// calendar expression, e.g. `Sat *-*-* 02:00`. Updates download at any
    /// time; only the restart waits for the window.
    #[serde(default = "d_window")]
    pub reboot_window: String,
}

impl Default for Updates {
    fn default() -> Self {
        Updates {
            feed: String::new(),
            reboot_window: d_window(),
        }
    }
}

/// IAM Roles Anywhere (decision 43; docs/design/setup-wizard.md, "Keys").
/// The wizard makes the key, sealed to the TPM, and a certificate request
/// for the trust anchor's CA; these say which trust anchor, profile and role
/// the helper (`omnifile-aws-signing-helper.service`) uses.
#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Aws {
    /// `arn:aws:rolesanywhere:<region>:<account>:trust-anchor/<id>`.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub trust_anchor_arn: String,
    /// `arn:aws:rolesanywhere:<region>:<account>:profile/<id>`.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub profile_arn: String,
    /// `arn:aws:iam::<account>:role/<name>`: read-only on the source prefixes.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub role_arn: String,
    /// The signing region. Empty: the trust anchor's.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub region: String,
    /// The Roles Anywhere interface endpoint (`https://…`), with private
    /// networking and no private DNS for it. Empty: the regional name.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub endpoint: String,
    /// How long a session lasts, 900 to 3600 seconds (threat-model.md T4: at
    /// most an hour, so a copied session ends soon). 0: the profile's.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub session_seconds: u32,
    /// A certificate the trust anchor's CA issued for this filer's request,
    /// to install (`omnifile-setup aws-cert FILE` does the same). With
    /// `issuer = "client"` only.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub certificate: String,
    /// Who issues the Roles Anywhere certificate (decision 50): empty or
    /// `omnifile` (the default) for OmniFile's edge CA, requested from the
    /// service over mTLS and renewed with the mTLS certificate; `client` for
    /// the client's own CA (AWS Private CA or theirs), signing the request by
    /// hand and installing it with `omnifile-setup aws-cert FILE`.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub issuer: String,
}

/// `[aws] issuer`: the client's own CA.
pub const ISSUER_CLIENT: &str = "client";
/// `[aws] issuer`: OmniFile's edge CA (also what empty means).
pub const ISSUER_OMNIFILE: &str = "omnifile";

impl Aws {
    pub fn is_empty(&self) -> bool {
        *self == Aws::default()
    }

    /// The client's own CA issues the certificate (`issuer = "client"`).
    pub fn client_ca(&self) -> bool {
        self.issuer == ISSUER_CLIENT
    }

    /// The region the helper signs for: the given one, or the trust anchor's.
    pub fn signing_region(&self) -> String {
        if !self.region.is_empty() {
            return self.region.clone();
        }
        parse_arn(&self.trust_anchor_arn)
            .map(|a| a.region.to_string())
            .unwrap_or_default()
    }

    /// Every problem with the Roles Anywhere answers (none when left out).
    fn problems(&self, bad: &mut impl FnMut(&'static str, String)) {
        if self.is_empty() {
            return;
        }
        if let Some(m) = rolesanywhere_arn_problem(&self.trust_anchor_arn, "trust-anchor") {
            bad("aws.trust_anchor_arn", m);
        }
        if let Some(m) = rolesanywhere_arn_problem(&self.profile_arn, "profile") {
            bad("aws.profile_arn", m);
        }
        if let Some(m) = role_arn_problem(&self.role_arn) {
            bad("aws.role_arn", m);
        }
        let ta = parse_arn(&self.trust_anchor_arn);
        if let (Some(t), Some(p)) = (&ta, parse_arn(&self.profile_arn))
            && (t.partition, t.region, t.account) != (p.partition, p.region, p.account)
        {
            bad(
                "aws.profile_arn",
                "the profile must be in the trust anchor's account and region".into(),
            );
        }
        if let (Some(t), Some(r)) = (&ta, parse_arn(&self.role_arn))
            && t.partition != r.partition
        {
            bad(
                "aws.role_arn",
                "the role must be in the trust anchor's partition".into(),
            );
        }
        if !self.region.is_empty() {
            if !is_region(&self.region) {
                bad(
                    "aws.region",
                    format!("{:?} is not a region, e.g. eu-west-1", self.region),
                );
            } else if ta.as_ref().is_some_and(|t| t.region != self.region) {
                bad(
                    "aws.region",
                    "Roles Anywhere signs in the trust anchor's region: leave it empty".into(),
                );
            }
        }
        let e = &self.endpoint;
        if !e.is_empty()
            && (!e.starts_with("https://")
                || e.len() <= "https://".len()
                || e.contains(|c: char| {
                    c.is_whitespace() || c.is_control() || "\"'\\$`".contains(c)
                }))
        {
            bad(
                "aws.endpoint",
                "give the Roles Anywhere endpoint's https:// address".into(),
            );
        }
        if !["", ISSUER_OMNIFILE, ISSUER_CLIENT].contains(&self.issuer.as_str()) {
            bad(
                "aws.issuer",
                "use \"omnifile\" (OmniFile's edge CA, the default) or \"client\" (your own CA)"
                    .into(),
            );
        } else if !self.certificate.is_empty() && !self.client_ca() {
            bad(
                "aws.certificate",
                "OmniFile's edge CA issues this filer's certificate: set issuer = \"client\" to \
                 install one from your own CA"
                    .into(),
            );
        }
        if self.session_seconds != 0 && !(900..=3600).contains(&self.session_seconds) {
            bad(
                "aws.session_seconds",
                "use 900 to 3600 seconds (a session lasts at most an hour)".into(),
            );
        }
        if !self.certificate.is_empty()
            && (!self.certificate.starts_with('/') || self.certificate.contains(".."))
        {
            bad(
                "aws.certificate",
                "give the certificate file's full path".into(),
            );
        }
    }
}

/// The parts of an ARN: `arn:partition:service:region:account:resource`.
#[derive(Debug, PartialEq, Eq)]
pub struct Arn<'a> {
    pub partition: &'a str,
    pub service: &'a str,
    pub region: &'a str,
    pub account: &'a str,
    pub resource: &'a str,
}

pub fn parse_arn(s: &str) -> Option<Arn<'_>> {
    let mut p = s.splitn(6, ':');
    let (arn, partition, service, region, account, resource) = (
        p.next()?,
        p.next()?,
        p.next()?,
        p.next()?,
        p.next()?,
        p.next()?,
    );
    (arn == "arn").then_some(Arn {
        partition,
        service,
        region,
        account,
        resource,
    })
}

fn is_partition(p: &str) -> bool {
    matches!(p, "aws" | "aws-cn" | "aws-us-gov" | "aws-eusc")
}

/// `eu-west-1`, `us-gov-west-1`, `cn-north-1`, `ap-southeast-5`, `eusc-de-east-1`.
pub fn is_region(r: &str) -> bool {
    let parts: Vec<&str> = r.split('-').collect();
    (3..=4).contains(&parts.len())
        && (2..=4).contains(&parts[0].len())
        && parts[..parts.len() - 1]
            .iter()
            .all(|w| !w.is_empty() && w.chars().all(|c| c.is_ascii_lowercase()))
        && parts
            .last()
            .is_some_and(|n| !n.is_empty() && n.len() <= 2 && n.chars().all(|c| c.is_ascii_digit()))
}

fn is_account(a: &str) -> bool {
    a.len() == 12 && a.bytes().all(|b| b.is_ascii_digit())
}

/// A Roles Anywhere resource id: a UUID (8-4-4-4-12 hex).
fn is_uuid(s: &str) -> bool {
    let groups: Vec<&str> = s.split('-').collect();
    groups.len() == 5
        && groups
            .iter()
            .zip([8, 4, 4, 4, 12])
            .all(|(g, n)| g.len() == n && g.bytes().all(|b| b.is_ascii_hexdigit()))
}

/// What is wrong with a Roles Anywhere ARN of `kind` (`trust-anchor` or
/// `profile`), if anything.
pub fn rolesanywhere_arn_problem(s: &str, kind: &str) -> Option<String> {
    let example = format!("arn:aws:rolesanywhere:eu-west-1:123456789012:{kind}/<id>");
    let Some(a) = parse_arn(s) else {
        return Some(format!("give the {kind} ARN, e.g. {example}"));
    };
    if !is_partition(a.partition) || a.service != "rolesanywhere" {
        return Some(format!("{s} is not a Roles Anywhere ARN ({example})"));
    }
    if !is_region(a.region) {
        return Some(format!("{s}: {:?} is not a region", a.region));
    }
    if !is_account(a.account) {
        return Some(format!("{s}: the account is 12 digits"));
    }
    match a.resource.split_once('/') {
        Some((k, id)) if k == kind && is_uuid(id) => None,
        _ => Some(format!("{s} is not a {kind} ARN ({example})")),
    }
}

/// What is wrong with an IAM role ARN, if anything.
pub fn role_arn_problem(s: &str) -> Option<String> {
    let example = "arn:aws:iam::123456789012:role/omnifile-edge-s3-read";
    let Some(a) = parse_arn(s) else {
        return Some(format!("give the role's ARN, e.g. {example}"));
    };
    if !is_partition(a.partition) || a.service != "iam" || !a.region.is_empty() {
        return Some(format!("{s} is not an IAM role ARN ({example})"));
    }
    if !is_account(a.account) {
        return Some(format!("{s}: the account is 12 digits"));
    }
    // role/[path/]name: a name is 1 to 64 of [A-Za-z0-9+=,.@_-]; a path is
    // /-separated segments of the same characters.
    let Some(rest) = a.resource.strip_prefix("role/") else {
        return Some(format!("{s} is not an IAM role ARN ({example})"));
    };
    let ok = |c: char| c.is_ascii_alphanumeric() || "+=,.@_-".contains(c);
    let segments: Vec<&str> = rest.split('/').collect();
    let name = segments.last().copied().unwrap_or("");
    if name.is_empty()
        || name.len() > 64
        || rest.len() > 512
        || segments.iter().any(|g| g.is_empty() || !g.chars().all(ok))
    {
        return Some(format!(
            "{s}: the role name is 1 to 64 of letters, digits and +=,.@_-"
        ));
    }
    None
}

fn is_zero(n: &u32) -> bool {
    *n == 0
}

fn is_false(b: &bool) -> bool {
    !*b
}

fn d_idmap_range() -> String {
    "10000-999999".into()
}

fn d_window() -> String {
    "Sun *-*-* 03:00".into()
}

/// A problem with one answer, in plain language.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Problem {
    pub field: &'static str,
    pub message: String,
}

impl std::fmt::Display for Problem {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.field, self.message)
    }
}

impl Answers {
    pub fn parse(text: &str) -> Result<Answers, String> {
        let a: Answers = toml::from_str(text).map_err(|e| e.to_string())?;
        if a.version != 1 {
            return Err(format!("version {} is not known (only 1)", a.version));
        }
        Ok(a)
    }

    pub fn to_toml(&self) -> String {
        toml::to_string(self).expect("answers serialize")
    }

    /// The Kerberos realm: the AD domain in upper case.
    pub fn realm(&self) -> String {
        self.ad.domain.to_uppercase()
    }

    /// The NetBIOS name: the hostname in upper case (at most 15 characters).
    pub fn netbios_name(&self) -> String {
        self.hostname.to_uppercase()
    }

    pub fn fqdn(&self) -> String {
        format!("{}.{}", self.hostname, self.ad.domain.to_lowercase())
    }

    /// Every problem, so the console can show them all at once.
    pub fn problems(&self) -> Vec<Problem> {
        let mut out = Vec::new();
        let mut bad = |field: &'static str, message: String| out.push(Problem { field, message });

        if !is_hostname(&self.hostname) {
            bad(
                "hostname",
                "use 1 to 15 letters, digits or hyphens, starting with a letter \
                 (it is also the Windows name, which allows 15)"
                    .into(),
            );
        }

        let n = &self.network;
        match n.mode.as_str() {
            "dhcp" => {}
            "static" => {
                if parse_cidr(&n.address).is_none() {
                    bad(
                        "network.address",
                        "give the address with its prefix, e.g. 10.1.2.10/24".into(),
                    );
                }
                if n.gateway.parse::<IpAddr>().is_err() {
                    bad("network.gateway", "give the gateway's IP address".into());
                }
                if n.dns.is_empty() {
                    bad(
                        "network.dns",
                        "give at least one DNS server (usually a domain controller)".into(),
                    );
                }
            }
            _ => bad("network.mode", "use dhcp or static".into()),
        }
        for d in &n.dns {
            if d.parse::<IpAddr>().is_err() {
                bad("network.dns", format!("{d} is not an IP address"));
            }
        }
        if !n.interface.is_empty() && !is_ifname(&n.interface) {
            bad(
                "network.interface",
                format!("{} is not an interface name", n.interface),
            );
        }
        for s in n.search.iter().chain(n.ntp.iter()) {
            if !is_dns_name(s) && s.parse::<IpAddr>().is_err() {
                bad("network", format!("{s} is not a host or domain name"));
            }
        }

        if !self.data.device.starts_with("/dev/") || self.data.device.contains("..") {
            bad(
                "data.device",
                "name the disk's device, e.g. /dev/sdb".into(),
            );
        }

        let ad = &self.ad;
        if !is_dns_name(&ad.domain) || !ad.domain.contains('.') {
            bad(
                "ad.domain",
                "give the domain's DNS name, e.g. corp.example".into(),
            );
        }
        if ad.workgroup.is_empty()
            || ad.workgroup.len() > 15
            || !ad
                .workgroup
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        {
            bad(
                "ad.workgroup",
                "give the short domain name, e.g. CORP (at most 15 characters)".into(),
            );
        }
        if ad.join_user.trim().is_empty() || ad.join_user.contains(['\n', '"']) {
            bad(
                "ad.join_user",
                "name the account that may join computers to the domain".into(),
            );
        }
        if ad.join_ou.contains(['\n', '"']) {
            bad(
                "ad.join_ou",
                "the OU may not contain quotes or line breaks".into(),
            );
        }
        for (field, v) in [
            ("ad.join_user", &ad.join_user),
            ("ad.admin_group", &ad.admin_group),
        ] {
            if v.chars().any(char::is_control) {
                bad(
                    field,
                    "holds a control character. In TOML a backslash needs doubling (\"CORP\\\\name\") \
                     or single quotes ('CORP\\name')"
                        .into(),
                );
            }
        }
        if ad.admin_group.trim().is_empty() || ad.admin_group.contains(['\n', '"', ',', '%', ':']) {
            bad(
                "ad.admin_group",
                "name the AD group whose members administer this filer".into(),
            );
        }
        if parse_range(&ad.idmap_range).is_none() {
            bad("ad.idmap_range", "use LOW-HIGH, e.g. 10000-999999".into());
        }

        if !self.service.bundle.is_empty() && !self.service.bundle.starts_with("ofe1.") {
            bad(
                "service.bundle",
                "paste the whole enrolment code; it starts with ofe1.".into(),
            );
        }

        let u = &self.updates;
        if !u.feed.is_empty()
            && (!u.feed.starts_with("https://") || u.feed.contains(char::is_whitespace))
        {
            bad(
                "updates.feed",
                "give the update server's https:// address".into(),
            );
        }
        if u.reboot_window.trim().is_empty() || u.reboot_window.contains('\n') {
            bad(
                "updates.reboot_window",
                "give a time, e.g. Sat *-*-* 02:00".into(),
            );
        }
        self.aws.problems(&mut bad);
        out
    }
}

pub fn is_hostname(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 15
        && b[0].is_ascii_alphabetic()
        && *b.last().unwrap() != b'-'
        && b.iter().all(|c| c.is_ascii_alphanumeric() || *c == b'-')
}

pub fn is_dns_name(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 253
        && s.split('.').all(|l| {
            !l.is_empty()
                && l.len() <= 63
                && !l.starts_with('-')
                && !l.ends_with('-')
                && l.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
        })
}

fn is_ifname(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 15
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c))
}

/// `10.1.2.10/24` → (address, prefix).
pub fn parse_cidr(s: &str) -> Option<(IpAddr, u8)> {
    let (a, p) = s.split_once('/')?;
    let ip: IpAddr = a.parse().ok()?;
    let p: u8 = p.parse().ok()?;
    let max = if ip.is_ipv4() { 32 } else { 128 };
    (p > 0 && p <= max).then_some((ip, p))
}

fn parse_range(s: &str) -> Option<(u32, u32)> {
    let (a, b) = s.split_once('-')?;
    let (a, b): (u32, u32) = (a.trim().parse().ok()?, b.trim().parse().ok()?);
    (a >= 1000 && a < b).then_some((a, b))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub(crate) fn sample() -> Answers {
        Answers::parse(
            r#"
            version = 1
            hostname = "edge1"
            [network]
            mode = "static"
            address = "10.1.2.10/24"
            gateway = "10.1.2.1"
            dns = ["10.1.0.10", "10.1.0.11"]
            [data]
            device = "/dev/sdb"
            [ad]
            domain = "corp.example"
            workgroup = "CORP"
            join_user = "svc-join"
            admin_group = "filer admins"
            [service]
            bundle = "ofe1.abc"
            [updates]
            feed = "https://updates.corp.example/omnifile-edge/"
            reboot_window = "Sat *-*-* 02:00"
            "#,
        )
        .unwrap()
    }

    #[test]
    fn a_good_file_has_no_problems_and_defaults_apply() {
        let a = sample();
        assert_eq!(a.problems(), vec![]);
        assert_eq!(a.realm(), "CORP.EXAMPLE");
        assert_eq!(a.netbios_name(), "EDGE1");
        assert_eq!(a.fqdn(), "edge1.corp.example");
        assert!(
            a.storage.private_endpoints,
            "private endpoints are the default"
        );
        assert_eq!(a.ad.idmap_range, "10000-999999");
        // Round trip.
        assert_eq!(Answers::parse(&a.to_toml()).unwrap(), a);
    }

    #[test]
    fn each_bad_answer_is_named() {
        let mut a = sample();
        a.hostname = "1-very-long-hostname".into();
        a.network.address = "10.1.2.10".into();
        a.network.dns = vec!["dc1".into()];
        a.data.device = "/tmp/../dev/sda".into();
        a.ad.domain = "corp".into();
        a.ad.workgroup = "A-WORKGROUP-NAME-TOO-LONG".into();
        a.ad.admin_group = "%wheel".into();
        a.ad.idmap_range = "5-1".into();
        a.service.bundle = "hello".into();
        a.updates.feed = "http://updates".into();
        a.ad.join_user = "CORP\x0cred".into();
        let fields: Vec<&str> = a.problems().iter().map(|p| p.field).collect();
        for f in [
            "hostname",
            "network.address",
            "network.dns",
            "data.device",
            "ad.domain",
            "ad.workgroup",
            "ad.admin_group",
            "ad.idmap_range",
            "service.bundle",
            "updates.feed",
            "ad.join_user",
        ] {
            assert!(fields.contains(&f), "{f} not flagged: {fields:?}");
        }
    }

    #[test]
    fn dhcp_needs_no_address_and_unknown_keys_fail() {
        let mut a = sample();
        a.network = Network {
            mode: "dhcp".into(),
            ..Network::default()
        };
        assert_eq!(a.problems(), vec![]);
        assert!(Answers::parse("version = 1\nhostname = \"x\"\nsurprise = 1").is_err());
        // Every optional section may be left out.
        let minimal = Answers::parse(
            "version = 1\nhostname = \"edge1\"\n[network]\nmode = \"dhcp\"\n[data]\ndevice = \"/dev/vdb\"\n\
             [ad]\ndomain = \"corp.example\"\nworkgroup = \"CORP\"\njoin_user = \"j\"\nadmin_group = \"g\"\n",
        )
        .unwrap();
        assert_eq!(minimal.problems(), vec![]);
        assert_eq!(minimal.updates.reboot_window, "Sun *-*-* 03:00");
        assert!(Answers::parse(&sample().to_toml().replace("version = 1", "version = 2")).is_err());
    }

    const TA: &str = "arn:aws:rolesanywhere:eu-west-1:123456789012:trust-anchor/1f2e3d4c-5b6a-4978-8a1b-2c3d4e5f6a7b";
    const PROFILE: &str =
        "arn:aws:rolesanywhere:eu-west-1:123456789012:profile/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
    const ROLE: &str = "arn:aws:iam::123456789012:role/omnifile/edge-s3-read";

    fn with_aws() -> Answers {
        let mut a = sample();
        a.aws = Aws {
            trust_anchor_arn: TA.into(),
            profile_arn: PROFILE.into(),
            role_arn: ROLE.into(),
            session_seconds: 3600,
            ..Aws::default()
        };
        a
    }

    #[test]
    fn roles_anywhere_answers_are_optional_and_round_trip() {
        let a = with_aws();
        assert_eq!(a.problems(), vec![]);
        assert_eq!(a.aws.signing_region(), "eu-west-1");
        assert_eq!(Answers::parse(&a.to_toml()).unwrap(), a);
        // Left out, it is not written at all.
        assert!(!sample().to_toml().contains("[aws]"));
        // GovCloud and China partitions, a path-less role.
        let mut g = with_aws();
        g.aws.trust_anchor_arn = TA
            .replace(":aws:", ":aws-us-gov:")
            .replace("eu-west-1", "us-gov-west-1");
        g.aws.profile_arn = PROFILE
            .replace(":aws:", ":aws-us-gov:")
            .replace("eu-west-1", "us-gov-west-1");
        g.aws.role_arn = "arn:aws-us-gov:iam::123456789012:role/r".into();
        assert_eq!(g.problems(), vec![]);
        assert_eq!(g.aws.signing_region(), "us-gov-west-1");
    }

    #[test]
    fn each_malformed_arn_is_named() {
        type Case = (&'static str, fn(&mut Aws));
        let cases: &[Case] = &[
            ("aws.trust_anchor_arn", |w| {
                w.trust_anchor_arn = "trust-anchor/abc".into()
            }),
            ("aws.trust_anchor_arn", |w| {
                w.trust_anchor_arn = TA.replace("trust-anchor/", "profile/")
            }),
            ("aws.trust_anchor_arn", |w| {
                w.trust_anchor_arn = TA.replace("1f2e3d4c-", "1f2e3d4-")
            }),
            ("aws.trust_anchor_arn", |w| {
                w.trust_anchor_arn = TA.replace("rolesanywhere", "iam")
            }),
            ("aws.trust_anchor_arn", |w| {
                w.trust_anchor_arn = TA.replace("eu-west-1", "Europe")
            }),
            ("aws.trust_anchor_arn", |w| {
                w.trust_anchor_arn = TA.replace(":aws:", ":azure:")
            }),
            ("aws.profile_arn", |w| {
                w.profile_arn = PROFILE.replace("123456789012", "12345")
            }),
            ("aws.profile_arn", |w| {
                w.profile_arn = PROFILE.replace("123456789012", "210987654321")
            }),
            ("aws.profile_arn", |w| {
                w.profile_arn = PROFILE.replace("eu-west-1", "eu-west-2")
            }),
            ("aws.role_arn", |w| {
                w.role_arn = "arn:aws:iam:eu-west-1:123456789012:role/r".into()
            }),
            ("aws.role_arn", |w| {
                w.role_arn = "arn:aws:iam::123456789012:user/r".into()
            }),
            ("aws.role_arn", |w| {
                w.role_arn = "arn:aws:iam::123456789012:role/".into()
            }),
            ("aws.role_arn", |w| {
                w.role_arn = "arn:aws:iam::123456789012:role/a b".into()
            }),
            ("aws.role_arn", |w| {
                w.role_arn = format!("arn:aws:iam::123456789012:role/{}", "r".repeat(65))
            }),
            ("aws.role_arn", |w| {
                w.role_arn = "arn:aws-cn:iam::123456789012:role/r".into()
            }),
            ("aws.region", |w| w.region = "eu-west-2".into()),
            ("aws.region", |w| w.region = "west".into()),
            ("aws.endpoint", |w| {
                w.endpoint = "http://rolesanywhere.eu-west-1.amazonaws.com".into()
            }),
            ("aws.endpoint", |w| {
                w.endpoint = "https://x\" --no-verify-ssl \"".into()
            }),
            ("aws.session_seconds", |w| w.session_seconds = 43200),
            ("aws.session_seconds", |w| w.session_seconds = 60),
            ("aws.certificate", |w| w.certificate = "cert.pem".into()),
        ];
        for (field, change) in cases {
            let mut a = with_aws();
            change(&mut a.aws);
            let fields: Vec<&str> = a.problems().iter().map(|p| p.field).collect();
            assert!(
                fields.contains(field),
                "{field} not flagged for {:?}: {fields:?}",
                a.aws
            );
        }
        // The region, when given, may be the trust anchor's own.
        let mut a = with_aws();
        a.aws.region = "eu-west-1".into();
        a.aws.endpoint = "https://vpce-0abc.rolesanywhere.eu-west-1.vpce.amazonaws.com".into();
        assert_eq!(a.problems(), vec![]);
    }
}
