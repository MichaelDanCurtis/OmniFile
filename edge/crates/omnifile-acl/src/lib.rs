//! POSIX ACLs as the one enforced permission model for NFS and SMB
//! (decision 28, `docs/design/access-control.md`).
//!
//! - [`posix`]: the ACL model and the Linux `system.posix_acl_*` xattr codec,
//!   in pure Rust (no libacl).
//! - [`share`]: a share's Owners / Writers / Readers AD groups applied as the
//!   share root's access and default ACLs, and drift checks on the root.
//! - [`snapshot`]: ACLs in SIDs, with the canonical form and `acl_sha256` the
//!   control API uses (§4.6).
//! - [`scan`]: full and on-change scans, the `POST …/acls` payloads, drift
//!   tracking per inode, and re-targeting inherited entries after a group
//!   change.
//! - [`normalize`]: keeps `chmod` from opening `other` or narrowing the mask
//!   below the AD-group grants (one answer for NFS and SMB).
//! - [`config`]: per-share Samba and Ganesha config, trust-level rules, and the
//!   smb.conf check that keeps `vfs_acl_xattr` out.
//! - [`smb_module`]: proves smbd can load `vfs_omnifile_prevver` (its build info
//!   against the running Samba, then a real load in a throwaway smbd).
//!
//! Stubbing and recall never change ownership, mode or ACLs; the permission
//! matrix in `tests/acl/` proves it on the dev AD.

pub mod config;
pub mod normalize;
pub mod posix;
pub mod scan;
pub mod share;
pub mod smb_module;
pub mod snapshot;
pub mod sys;

pub use config::{
    ConfigError, FsalPolicy, PREVVER_MODULE, Protocol, ShareConfig, check_smb_conf,
    prevver_installed,
};
pub use normalize::{Normalized, normalize, normalize_inode, normalize_tree};
pub use posix::{AclError, Entry, Perms, PosixAcl, Tag};
pub use scan::{DriftTracker, Finding, InodeKey, ScanResult, retarget_group, scan};
pub use share::{RootDrift, ShareGroups, apply_root, apply_root_at, check_root};
pub use smb_module::{PrevverError, PrevverOk, check_prevver, prevver_usable};
pub use snapshot::{
    AclSnapshot, AclsRequest, LocalAcl, Scope, SnapEntry, SnapTag, SnapshotError, snapshot,
    to_local,
};
pub use sys::{AclFs, FileAcl, Kind, SysFs};
