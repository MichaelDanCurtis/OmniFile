//! The edge filer's setup wizard (`omnifile-setup`; docs/design/setup-wizard.md).
//!
//! It asks, in plain language, what a filer needs before it can serve
//! shares: its name and network, the data disk, the Active Directory domain
//! and its administrators, the enrolment code from the portal, private
//! networking, and the update feed. Then it applies the answers in
//! idempotent steps. The same answers can come from a file or, unattended,
//! from a systemd credential.

pub mod answers;
pub mod aws;
pub mod host;
pub mod prompt;
pub mod render;
pub mod steps;

/// Where the answers are kept (no password; the enrolment code only until used).
pub const ANSWERS_FILE: &str = "/etc/omnifile/setup.toml";
/// The update feed the appliance image was built with (appliance/build.sh --feed-url).
pub const IMAGE_FEED_FILE: &str = "/usr/lib/omnifile/update-feed";
/// Present once a setup has run to the end: the first-boot units stop offering setup.
pub const DONE_FILE: &str = "/var/lib/omnifile/setup/done";
/// systemd credentials an unattended setup reads.
pub const CRED_ANSWERS: &str = "omnifile.setup";
pub const CRED_PASSWORD: &str = "omnifile.join-password";
pub const CRED_BUNDLE: &str = "omnifile.enrol-bundle";
/// A crypt(3) hash for the recovery account (unattended setup; never a password).
pub const CRED_RECOVERY_HASH: &str = "omnifile.recovery-hash";
