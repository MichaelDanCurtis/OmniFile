//! Every file the wizard writes, rendered from the answers. Pure functions:
//! the steps decide when to write them, the tests read them.

use crate::answers::{Answers, parse_cidr};

/// The banner on every file the wizard owns.
pub const OWNED: &str =
    "# Written by omnifile-setup from /etc/omnifile/setup.toml. Rerun it to change this.";

/// The mark on the one line the wizard owns in a file it shares (/etc/hosts).
const HOSTS_MARK: &str = "# omnifile-setup";

pub const NETWORK_FILE: &str = "/etc/systemd/network/10-omnifile.network";
pub const TIMESYNCD_FILE: &str = "/etc/systemd/timesyncd.conf.d/10-omnifile.conf";
pub const KRB5_FILE: &str = "/etc/krb5.conf";
pub const SMB_FILE: &str = "/etc/samba/smb.conf";
pub const SMB_SHARES_FILE: &str = "/etc/samba/omnifile-shares.conf";
pub const IDMAPD_FILE: &str = "/etc/idmapd.conf";
pub const SUDOERS_FILE: &str = "/etc/sudoers.d/omnifile-admins";
pub const SSHD_FILE: &str = "/etc/ssh/sshd_config.d/10-omnifile.conf";
pub const EDGE_FILE: &str = "/etc/omnifile/edge.toml";
pub const GANESHA_BASE_FILE: &str = "/etc/ganesha/omnifile-base.conf";
pub const GANESHA_FILE: &str = "/etc/ganesha/ganesha.conf";
pub const GANESHA_EXPORTS_FILE: &str = "/etc/ganesha/omnifile-exports.conf";
/// What the daemon renders from the catalog's shares (`[shares]` in
/// edge.toml), included by [`SMB_SHARES_FILE`] and [`GANESHA_EXPORTS_FILE`].
pub const RENDERED_SMB: &str = "/var/lib/omnifile/shares/smb.conf";
pub const RENDERED_GANESHA: &str = "/var/lib/omnifile/shares/ganesha.conf";
pub const DATA_MOUNT_UNIT: &str = "/etc/systemd/system/srv-omnifile-data.mount";
pub const EDGE_DATA_DROPIN: &str = "/etc/systemd/system/omnifile-edge.service.d/20-data.conf";
pub const REBOOT_TIMER_DROPIN: &str =
    "/etc/systemd/system/systemd-sysupdate-reboot.timer.d/10-omnifile.conf";
pub const RECOVERY_SUDOERS_FILE: &str = "/etc/sudoers.d/omnifile-recovery";
pub const PAM_PROFILE_FILE: &str = "/usr/share/pam-configs/omnifile-recovery";
/// The break-glass account (access-control.md §8).
pub const RECOVERY_USER: &str = "omnifile-recovery";
/// Present while the recovery account has been used since it was last sealed.
pub const RECOVERY_USED: &str = "/var/lib/omnifile/recovery/used";
pub const DATA_ROOT: &str = "/srv/omnifile/data";
pub const DATA_LABEL: &str = "omni-data";
/// The sysupdate transfers whose source is the update feed (appliance/mkosi.extra).
pub const TRANSFERS: [&str; 3] = ["10-uki", "20-usr", "21-usr-verity"];

const SMB_TEMPLATE: &str = include_str!("../../../config/samba/smb.conf.tmpl");
const GANESHA_BASE: &str = include_str!("../../../config/ganesha/omnifile-base.conf");
const PAM_PROFILE: &str = include_str!("../../../config/pam/omnifile-recovery");

pub fn hostname(a: &Answers) -> String {
    format!("{}\n", a.hostname)
}

/// `/etc/hosts` with the wizard's one line for this host replaced (or added).
pub fn hosts(existing: &str, a: &Answers) -> String {
    let mut out: Vec<String> = existing
        .lines()
        .filter(|l| !l.trim_end().ends_with(HOSTS_MARK))
        .map(str::to_string)
        .collect();
    out.push(format!(
        "127.0.1.1 {} {} {HOSTS_MARK}",
        a.fqdn(),
        a.hostname
    ));
    out.join("\n") + "\n"
}

/// systemd-networkd: `iface` is the interface to configure, or `None` for
/// every wired one (DHCP only).
pub fn network(a: &Answers, iface: Option<&str>) -> String {
    let n = &a.network;
    let mut s = format!("{OWNED}\n[Match]\n");
    match iface {
        Some(i) => s += &format!("Name={i}\n"),
        None => s += "Type=ether\nName=en* eth*\n",
    }
    s += "\n[Network]\n";
    if n.mode == "static" {
        let (ip, p) = parse_cidr(&n.address).expect("validated");
        s += &format!("Address={ip}/{p}\nGateway={}\n", n.gateway);
    } else {
        s += "DHCP=yes\n";
    }
    for d in &n.dns {
        s += &format!("DNS={d}\n");
    }
    let mut domains = vec![a.ad.domain.to_lowercase()];
    for d in &n.search {
        if !domains.contains(&d.to_lowercase()) {
            domains.push(d.to_lowercase());
        }
    }
    s += &format!("Domains={}\n", domains.join(" "));
    if n.mode == "dhcp" && !n.dns.is_empty() {
        // The given servers replace the DHCP ones: AD needs the domain's DNS.
        s += "\n[DHCPv4]\nUseDNS=no\n\n[IPv6AcceptRA]\nUseDNS=no\n";
    }
    s
}

/// systemd-timesyncd. Without servers, the AD domain's name: it resolves to
/// the domain controllers, which keep the domain's time (Kerberos allows 5
/// minutes of skew).
pub fn timesyncd(a: &Answers) -> String {
    let servers = if a.network.ntp.is_empty() {
        a.ad.domain.to_lowercase()
    } else {
        a.network.ntp.join(" ")
    };
    format!("{OWNED}\n[Time]\nNTP={servers}\n")
}

pub fn krb5(a: &Answers) -> String {
    let realm = a.realm();
    let domain = a.ad.domain.to_lowercase();
    format!(
        "{OWNED}\n\
         [libdefaults]\n    default_realm = {realm}\n    dns_lookup_realm = false\n    \
         dns_lookup_kdc = true\n    rdns = false\n    forwardable = true\n    ticket_lifetime = 24h\n\n\
         [domain_realm]\n    .{domain} = {realm}\n    {domain} = {realm}\n"
    )
}

/// The appliance's smb.conf (edge/config/samba/smb.conf.tmpl), checked the
/// way the daemon checks share stanzas.
pub fn smb_conf(a: &Answers) -> Result<String, String> {
    let s = SMB_TEMPLATE
        .replace("@WORKGROUP@", &a.ad.workgroup.to_uppercase())
        .replace("@REALM@", &a.realm())
        .replace("@NETBIOS_NAME@", &a.netbios_name())
        .replace("@IDMAP_RANGE@", a.ad.idmap_range.trim())
        .replace("@AUDIT_FACILITY@", "local5");
    if let Some(l) = s
        .lines()
        .find(|l| !l.trim_start().starts_with('#') && l.contains('@'))
    {
        return Err(format!("smb.conf template left a placeholder: {l}"));
    }
    let findings = omnifile_acl::config::check_smb_conf(&s);
    if !findings.is_empty() {
        return Err(format!("smb.conf fails the checks: {findings:?}"));
    }
    Ok(format!("{OWNED}\n{s}"))
}

/// The share stanzas file smb.conf includes. The daemon owns it; the wizard
/// only makes sure it exists.
/// smb.conf's share include: the stanzas the daemon renders
/// (`omnifile_edge::shares`, `ShareConfig::render_smb`).
pub fn smb_shares_include() -> String {
    format!("{OWNED}\ninclude = {RENDERED_SMB}\n")
}

/// Ganesha's export include: the blocks the daemon renders
/// (`ShareConfig::render_ganesha`).
pub fn ganesha_exports_include() -> String {
    format!("{OWNED}\n%include \"{RENDERED_GANESHA}\"\n")
}

/// The rendered files before the daemon's first pass: no shares yet.
/// Ganesha won't start on an `%include` of a missing file.
pub fn rendered_empty() -> String {
    "# No shares yet: the edge daemon renders them here from the catalog.\n".into()
}

/// libnfsidmap (Ganesha): NFSv4 names map in the AD domain, through winbind.
pub fn idmapd(a: &Answers) -> String {
    format!(
        "{OWNED}\n[General]\nDomain = {}\n\n[Mapping]\nNobody-User = nobody\nNobody-Group = nogroup\n",
        a.ad.domain.to_lowercase()
    )
}

/// `/etc/nsswitch.conf` with winbind on the passwd and group lines.
pub fn nsswitch(existing: &str) -> String {
    let mut out: Vec<String> = Vec::new();
    for l in existing.lines() {
        let db = l.split(':').next().unwrap_or("").trim();
        if (db == "passwd" || db == "group")
            && !l.split_whitespace().skip(1).any(|w| w == "winbind")
        {
            out.push(format!("{} winbind", l.trim_end()));
        } else {
            out.push(l.to_string());
        }
    }
    for db in ["passwd", "group"] {
        if !out
            .iter()
            .any(|l| l.split(':').next().unwrap_or("").trim() == db)
        {
            out.push(format!("{db}: files winbind"));
        }
    }
    out.join("\n") + "\n"
}

/// The admin group as winbind shows it (`winbind use default domain = yes`):
/// without a `DOMAIN\` prefix.
pub fn admin_group(a: &Answers) -> String {
    let g = a.ad.admin_group.trim();
    match g.split_once('\\') {
        Some((dom, name)) if dom.eq_ignore_ascii_case(&a.ad.workgroup) => name.to_string(),
        _ => g.to_string(),
    }
}

/// sudo for the appliance admins (access-control.md §8). Spaces in a group
/// name are escaped, as sudoers requires.
pub fn sudoers(a: &Answers) -> String {
    let g = admin_group(a).replace('\\', "\\\\").replace(' ', "\\ ");
    format!("{OWNED}\n%{g} ALL=(ALL:ALL) ALL\n")
}

/// sshd: only the appliance admins log in, never root, and never the
/// recovery account (it is for the console).
pub fn sshd(a: &Answers) -> String {
    format!(
        "{OWNED}\nPermitRootLogin no\nDenyUsers {RECOVERY_USER}\nAllowGroups \"{}\"\n",
        admin_group(a)
    )
}

/// sudo for the recovery account.
pub fn sudoers_recovery() -> String {
    format!("{OWNED}\n{RECOVERY_USER} ALL=(ALL:ALL) ALL\n")
}

/// The pam-auth-update profile that records every session of the recovery
/// account (edge/config/pam/omnifile-recovery).
pub fn pam_profile() -> &'static str {
    PAM_PROFILE
}

/// The daemon's configuration, with production defaults (config.rs).
pub fn edge_toml(a: &Answers, fs_uuid: &str) -> String {
    format!(
        "{OWNED}\n\
         data_root = \"{DATA_ROOT}\"\n\
         replica_path = \"/var/lib/omnifile/replica.db\"\n\
         socket_path = \"/run/omnifile/fsal.sock\"\n\
         # Fills and stubs go through the smb-guard's private fill mount.\n\
         fill_root = \"/run/omnifile-fill\"\n\
         backend = \"service\"\n\n\
         [service]\n\
         identity_dir = \"/var/lib/omnifile/identity\"\n\
         outbox_dir = \"/var/lib/omnifile/outbox\"\n\
         # The private keys are sealed with systemd credentials, bound to the TPM\n\
         # when the box has one (M6, \"TPM-held keys\").\n\
         key_protection = \"auto\"\n\n\
         [shares]\n\
         # Each share's Samba stanza and Ganesha EXPORT block, rendered from the\n\
         # catalog, with the share root's group ACL (docs/design/share-provisioning.md).\n\
         render = true\n\n\
         [smbguard]\n\
         # The guard refuses to mark any filesystem but the data volume.\n\
         fs_uuid = \"{fs_uuid}\"\n\n\
         [network]\n\
         # Decision 22: storage only through private endpoints, never a public path.\n\
         private_only = {}\n",
        a.storage.private_endpoints
    )
}

pub fn ganesha_base() -> String {
    format!("{OWNED}\n{GANESHA_BASE}")
}

/// Ganesha's main file: the base, and the exports the daemon renders.
pub fn ganesha_main() -> String {
    format!("{OWNED}\n%include \"{GANESHA_BASE_FILE}\"\n%include \"{GANESHA_EXPORTS_FILE}\"\n")
}

/// The data volume's mount, by filesystem UUID.
pub fn data_mount(fs_uuid: &str) -> String {
    format!(
        "{OWNED}\n[Unit]\nDescription=OmniFile data volume\nBefore=omnifile-edge.service\n\n\
         [Mount]\nWhat=/dev/disk/by-uuid/{fs_uuid}\nWhere={DATA_ROOT}\nType=xfs\nOptions=noatime\n\n\
         [Install]\nWantedBy=local-fs.target\n"
    )
}

/// The daemon never starts on the root filesystem by mistake.
pub fn edge_data_dropin() -> String {
    format!("{OWNED}\n[Unit]\nRequiresMountsFor={DATA_ROOT}\n")
}

/// The update feed, as a drop-in on one sysupdate transfer.
pub fn transfer_source(feed: &str) -> String {
    format!("{OWNED}\n[Source]\nPath={feed}\n")
}

pub fn transfer_dropin_path(transfer: &str) -> String {
    format!("/etc/sysupdate.d/{transfer}.transfer.d/10-omnifile-feed.conf")
}

/// The restart window: a downloaded update is applied only then.
pub fn reboot_timer(a: &Answers) -> String {
    format!(
        "{OWNED}\n[Timer]\nOnCalendar=\nOnCalendar={}\nRandomizedDelaySec=0\n",
        a.updates.reboot_window.trim()
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::answers::tests::sample;

    #[test]
    fn hosts_line_is_replaced_not_repeated() {
        let a = sample();
        let once = hosts("127.0.0.1 localhost\n", &a);
        let twice = hosts(&once, &a);
        assert_eq!(once, twice);
        assert!(once.contains("127.0.1.1 edge1.corp.example edge1 # omnifile-setup"));
        assert!(once.starts_with("127.0.0.1 localhost\n"));
    }

    #[test]
    fn static_and_dhcp_networks() {
        let a = sample();
        let s = network(&a, Some("ens3"));
        assert!(s.contains("Name=ens3\n"));
        assert!(
            s.contains("Address=10.1.2.10/24\nGateway=10.1.2.1\nDNS=10.1.0.10\nDNS=10.1.0.11\n")
        );
        assert!(s.contains("Domains=corp.example\n"));
        let mut d = a.clone();
        d.network.mode = "dhcp".into();
        let s = network(&d, None);
        assert!(s.contains("Type=ether\nName=en* eth*\n") && s.contains("DHCP=yes"));
        assert!(s.contains("UseDNS=no"), "given DNS servers replace DHCP's");
        d.network.dns.clear();
        assert!(!network(&d, None).contains("UseDNS"));
    }

    #[test]
    fn time_defaults_to_the_domain() {
        let mut a = sample();
        assert!(timesyncd(&a).contains("NTP=corp.example\n"));
        a.network.ntp = vec!["10.1.0.10".into(), "ntp.corp.example".into()];
        assert!(timesyncd(&a).contains("NTP=10.1.0.10 ntp.corp.example\n"));
    }

    #[test]
    fn samba_and_kerberos() {
        let a = sample();
        let s = smb_conf(&a).unwrap();
        assert!(s.contains("workgroup = CORP\n"));
        assert!(s.contains("realm = CORP.EXAMPLE\n"));
        assert!(s.contains("netbios name = EDGE1\n"));
        assert!(s.contains("idmap config CORP : range = 10000-999999"));
        assert!(s.contains("include = /etc/samba/omnifile-shares.conf"));
        let k = krb5(&a);
        assert!(
            k.contains("default_realm = CORP.EXAMPLE")
                && k.contains(".corp.example = CORP.EXAMPLE")
        );
        assert!(idmapd(&a).contains("Domain = corp.example"));
    }

    #[test]
    fn nsswitch_gains_winbind_once() {
        let base =
            "passwd:         files systemd\ngroup:          files systemd\nhosts: files dns\n";
        let once = nsswitch(base);
        assert_eq!(once, nsswitch(&once));
        assert!(once.contains("passwd:         files systemd winbind\n"));
        assert!(once.contains("hosts: files dns\n"));
        assert!(nsswitch("hosts: files\n").contains("group: files winbind"));
    }

    #[test]
    fn admins_get_sudo_and_ssh_with_names_escaped() {
        let mut a = sample();
        a.ad.admin_group = "CORP\\filer admins".into();
        assert_eq!(admin_group(&a), "filer admins");
        assert!(sudoers(&a).contains("%filer\\ admins ALL=(ALL:ALL) ALL\n"));
        let s = sshd(&a);
        assert!(s.contains("AllowGroups \"filer admins\"") && s.contains("PermitRootLogin no"));
        assert!(
            s.contains("DenyUsers omnifile-recovery"),
            "the recovery account is console only"
        );
        assert!(sudoers_recovery().contains("omnifile-recovery ALL=(ALL:ALL) ALL"));
        assert!(
            pam_profile().contains("pam_exec.so quiet /usr/local/bin/omnifile-setup recovery-used")
        );
        a.ad.admin_group = "OTHER\\x".into();
        assert_eq!(admin_group(&a), "OTHER\\x", "another domain's prefix stays");
    }

    #[test]
    fn edge_config_parses_as_the_daemon_reads_it() {
        let a = sample();
        let t = edge_toml(&a, "0b1c-uuid");
        let v: toml::Table = toml::from_str(&t).unwrap();
        assert_eq!(v["data_root"].as_str(), Some(DATA_ROOT));
        assert_eq!(v["smbguard"]["fs_uuid"].as_str(), Some("0b1c-uuid"));
        assert_eq!(v["network"]["private_only"].as_bool(), Some(true));
        assert_eq!(v["backend"].as_str(), Some("service"));
        assert_eq!(v["service"]["key_protection"].as_str(), Some("auto"));
    }

    #[test]
    fn ganesha_keeps_its_grace_period() {
        let g = ganesha_base();
        assert!(
            g.contains("Graceless = false"),
            "NFS clients ride the grace period across restarts"
        );
        assert!(g.contains("Socket_Path = \"/run/omnifile/fsal.sock\""));
        assert!(ganesha_main().contains("%include \"/etc/ganesha/omnifile-base.conf\""));
        assert!(ganesha_main().contains("%include \"/etc/ganesha/omnifile-exports.conf\""));
        assert!(
            ganesha_exports_include()
                .contains("%include \"/var/lib/omnifile/shares/ganesha.conf\"")
        );
        assert!(smb_shares_include().contains("include = /var/lib/omnifile/shares/smb.conf"));
    }

    #[test]
    fn units_and_updates() {
        let a = sample();
        let m = data_mount("u-1");
        assert!(m.contains("What=/dev/disk/by-uuid/u-1\nWhere=/srv/omnifile/data\nType=xfs"));
        assert!(edge_data_dropin().contains("RequiresMountsFor=/srv/omnifile/data"));
        assert!(transfer_source("https://u/").contains("[Source]\nPath=https://u/\n"));
        assert_eq!(
            transfer_dropin_path("20-usr"),
            "/etc/sysupdate.d/20-usr.transfer.d/10-omnifile-feed.conf"
        );
        let t = reboot_timer(&a);
        assert!(
            t.contains("OnCalendar=\nOnCalendar=Sat *-*-* 02:00\n"),
            "{t}"
        );
    }
}
