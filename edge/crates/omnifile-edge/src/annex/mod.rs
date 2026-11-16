//! Annexes on the edge (`docs/design/annex.md`, decision 51; phase A1, Copy
//! mode): a directory on someone else's NAS, read and copied to Azure as
//! plain blobs, never written.
//!
//! Each annex runs in its own unit, `omnifile-annex@<annex_id>.service`
//! (`omnifile-annex run --annex <id>`), with its own read-only NAS mount, so a
//! hung NAS can never stall the daemon that serves OmniFile's own shares.
//! `omnifile-annex supervise` (`omnifile-annex.service`) starts and stops the
//! units as annexes come and go in the catalog.
//!
//! - [`mount`]: the read-only NAS mount, with the edge's AD computer account
//!   (SMB Kerberos, NFS `sec=krb5`), or NFS `sec=sys` as the host-trusted
//!   fallback.
//! - [`db`]: `annex.db`, the unit's listing of the NAS and its outbox.
//! - [`scan`]: quick and full scans, renames by file id.
//! - [`segy`]: the SEG-Y structure check before a copy.
//! - [`acl`]: the NAS's ACL, read for the catalog.
//! - [`folders`]: folder ACLs and free space, for where a recalled file may
//!   be placed (A3.1).
//! - [`copy`]: the copy through the usual upload path.
//! - [`notify`]: SMB change notify, a hint that triggers a quick scan.
//! - [`runner`]: the loop: scans, the quiet period, events, copies and the
//!   weekly reconciliation.
//! - [`service`]: the control API and Azure, and a fake for tests.

pub mod acl;
pub mod copy;
pub mod db;
pub mod folders;
pub mod mount;
pub mod nasfs;
pub mod notify;
pub mod placer;
pub mod preview;
pub mod remover;
pub mod rules;
pub mod runner;
pub mod scan;
pub mod segy;
pub mod service;
pub mod watchdog;

use std::path::PathBuf;

use serde::Deserialize;

/// `[annex]` in `/etc/omnifile/edge.toml`.
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, default)]
pub struct AnnexConfig {
    /// One directory per annex: `annex.db` and upload journals.
    pub state_dir: PathBuf,
    /// One read-only mount per annex below it (inside the unit's own mount
    /// namespace: `PrivateMounts=yes`).
    pub mount_root: PathBuf,
    /// How often the loop runs (quiet checks, events, copies).
    pub tick_s: u64,
    /// How often the unit asks the service for its settings.
    pub poll_s: u64,
    /// How long a mount may not answer before the unit restarts (`watchdog`).
    pub hang_s: u64,
    /// Events per `POST annex-events`.
    pub batch: usize,
    /// Copies started per tick.
    pub copies_per_tick: usize,
    /// Extra mount options, comma-separated (e.g. `port=4450` for a test
    /// server), by protocol.
    pub smb_options: String,
    pub nfs_options: String,
    /// Test and lab overrides of the annex's own settings, in seconds (0:
    /// use the annex's). A pilot never sets these.
    pub quiet_s: u64,
    pub quick_scan_s: u64,
    pub full_scan_s: u64,
    pub reconcile_s: u64,
    /// How often age rules are looked at (0: daily).
    pub age_s: u64,
    /// The removal grace period, in seconds (0: the annex's `grace_minutes`).
    /// A test override, like the ones above.
    pub grace_s: u64,
    /// The remove-rights account (`OmniFile-Annex-Remove`) for the
    /// read-write mount, which exists only while removals are live. Empty:
    /// the read account's identity, for a lab; a pilot names its own.
    pub remove_keytab: PathBuf,
    pub remove_principal: String,
    /// The local uid whose ticket cache (`/tmp/krb5cc_<uid>`) holds the
    /// remove-rights ticket, for the SMB mount's `cruid` (0: root's, the
    /// read account's).
    pub remove_uid: u32,
    /// The restore-rights account (`OmniFile-Annex-Restore`) for the
    /// placement mount (A3.3), which exists only while a placement is in
    /// hand. Empty: the remove-rights account's, for a lab.
    pub restore_keytab: PathBuf,
    pub restore_principal: String,
    pub restore_uid: u32,
    /// The SIDs the staging folder's protected DACL names over SMB: the
    /// placement account (full) and the read account (read and traverse).
    /// Empty: looked up by the accounts' names through winbind.
    pub placement_sid: String,
    pub read_sid: String,
    /// Over NFSv4 with two accounts: the read account's NFSv4 name
    /// (`user@domain`), which the staging folder's ACL names.
    pub read_nfs4_who: String,
    /// `systemctl` for `supervise`.
    pub systemctl: String,
    /// The machine account's keytab (the setup wizard's AD join) and its
    /// principal (`""`: the host name, upper case, and `$`).
    pub machine_keytab: PathBuf,
    pub machine_principal: String,
    /// A dedicated service account (credential mode `service_keytab`).
    pub service_keytab: PathBuf,
    pub service_principal: String,
}

impl Default for AnnexConfig {
    fn default() -> Self {
        AnnexConfig {
            state_dir: "/var/lib/omnifile/annex".into(),
            mount_root: "/run/omnifile/annex".into(),
            tick_s: 10,
            poll_s: 60,
            hang_s: 300,
            batch: 500,
            copies_per_tick: 4,
            smb_options: String::new(),
            nfs_options: String::new(),
            quiet_s: 0,
            quick_scan_s: 0,
            full_scan_s: 0,
            reconcile_s: 0,
            age_s: 0,
            grace_s: 0,
            remove_keytab: PathBuf::new(),
            remove_principal: String::new(),
            remove_uid: 0,
            restore_keytab: PathBuf::new(),
            restore_principal: String::new(),
            restore_uid: 0,
            placement_sid: String::new(),
            read_sid: String::new(),
            read_nfs4_who: String::new(),
            systemctl: "systemctl".into(),
            machine_keytab: "/etc/krb5.keytab".into(),
            machine_principal: String::new(),
            service_keytab: "/etc/omnifile/annex.keytab".into(),
            service_principal: String::new(),
        }
    }
}

impl AnnexConfig {
    /// The keytabs from the unit's credentials (`ImportCredential=`, sealed
    /// with systemd-creds), when there: `annex.keytab` for the dedicated
    /// account, `annex-remove.keytab` for the remove-rights one.
    pub fn with_credentials(mut self, dir: Option<&std::path::Path>) -> AnnexConfig {
        if let Some(d) = dir {
            let k = d.join("annex.keytab");
            if k.is_file() {
                self.service_keytab = k;
            }
            let r = d.join("annex-remove.keytab");
            if r.is_file() {
                self.remove_keytab = r;
            }
            let p = d.join("annex-restore.keytab");
            if p.is_file() {
                self.restore_keytab = p;
            }
        }
        self
    }
}

/// Milliseconds since the Unix epoch.
pub fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

#[cfg(test)]
mod credential_tests {
    use super::*;

    #[test]
    fn sealed_keytabs_from_the_units_credentials_are_used() {
        let d = tempfile::tempdir().unwrap();
        let none = AnnexConfig::default().with_credentials(Some(d.path()));
        assert_eq!(none.service_keytab, AnnexConfig::default().service_keytab);
        std::fs::write(d.path().join("annex.keytab"), b"k").unwrap();
        std::fs::write(d.path().join("annex-remove.keytab"), b"r").unwrap();
        let c = AnnexConfig::default().with_credentials(Some(d.path()));
        assert_eq!(c.service_keytab, d.path().join("annex.keytab"));
        assert_eq!(c.remove_keytab, d.path().join("annex-remove.keytab"));
    }
}
