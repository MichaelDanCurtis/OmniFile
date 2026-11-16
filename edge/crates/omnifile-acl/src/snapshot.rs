//! ACL snapshots in SIDs, content-addressed for the control API (§4.6).
//!
//! The edge reports each distinct ACL once per dataset; files point at it by
//! `acl_sha256`. The hash is over a canonical JSON form, defined here and
//! nowhere else:
//!
//! - the object `{entries, group_sid, mode, owner_sid}` with keys in that
//!   (lexicographic) order, no whitespace, `owner_sid`/`group_sid` omitted
//!   when unknown;
//! - each entry `{perms, scope, sid, tag}` with keys in that order, `sid`
//!   omitted for `owner`, `owning_group`, `mask` and `other`;
//! - entries sorted by their own canonical JSON (byte order), duplicates removed;
//! - strings are SIDs, tag/scope words and `rwx` text, so no escaping arises.
//!
//! `sha256` is the lowercase hex SHA-256 of those UTF-8 bytes.
//!
//! Ids without an AD SID (root, local accounts) use Samba's Unix SIDs
//! (`S-1-22-1-<uid>`, `S-1-22-2-<gid>`), so every entry names someone.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use omnifile_identity::{IdMap, IdMapError, Sid};

use crate::posix::{Perms, PosixAcl, Tag};
use crate::sys::FileAcl;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Scope {
    Access,
    Default,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SnapTag {
    Owner,
    OwningGroup,
    User,
    Group,
    Mask,
    Other,
}

impl SnapTag {
    fn as_str(self) -> &'static str {
        match self {
            SnapTag::Owner => "owner",
            SnapTag::OwningGroup => "owning_group",
            SnapTag::User => "user",
            SnapTag::Group => "group",
            SnapTag::Mask => "mask",
            SnapTag::Other => "other",
        }
    }
}

impl Scope {
    fn as_str(self) -> &'static str {
        match self {
            Scope::Access => "access",
            Scope::Default => "default",
        }
    }
}

/// `AclEntry` in the OpenAPI spec.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct SnapEntry {
    pub scope: Scope,
    pub tag: SnapTag,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sid: Option<Sid>,
    /// `rwx` / `r-x` / `---`.
    pub perms: String,
}

impl SnapEntry {
    fn canonical(&self) -> String {
        match &self.sid {
            Some(sid) => format!(
                r#"{{"perms":"{}","scope":"{}","sid":"{}","tag":"{}"}}"#,
                self.perms,
                self.scope.as_str(),
                sid,
                self.tag.as_str()
            ),
            None => format!(
                r#"{{"perms":"{}","scope":"{}","tag":"{}"}}"#,
                self.perms,
                self.scope.as_str(),
                self.tag.as_str()
            ),
        }
    }
}

/// `AclBody` in the OpenAPI spec (`POST /v0/edges/{id}/acls`, `acls[]`).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AclSnapshot {
    pub sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub owner_sid: Option<Sid>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub group_sid: Option<Sid>,
    pub mode: u32,
    pub entries: Vec<SnapEntry>,
}

/// `POST /v0/edges/{id}/acls` body. `dataset_id` is a decimal string
/// (control API §1, identifiers).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AclsRequest {
    pub dataset_id: String,
    pub acls: Vec<AclSnapshot>,
}

#[derive(Debug, thiserror::Error)]
pub enum SnapshotError {
    #[error("id mapping: {0}")]
    IdMap(#[from] IdMapError),
    /// A SID in a catalog ACL has no uid/gid on this edge. Materialising
    /// without it would drop a grant or a restriction, so the caller must not
    /// guess (fail closed: keep the placeholder unreadable and retry).
    #[error("SID {0} has no {1} on this edge")]
    Unmapped(Sid, &'static str),
    #[error("ACL entry {0:?} is malformed")]
    BadEntry(SnapEntry),
}

/// A catalog ACL resolved to this edge's ids, ready to put on an inode.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LocalAcl {
    pub uid: u32,
    pub gid: u32,
    /// Permission bits (`mode & 0o7777` of the snapshot).
    pub mode: u32,
    pub access: PosixAcl,
    pub default: Option<PosixAcl>,
}

fn unix_id(sid: &Sid, prefix: &str) -> Option<u32> {
    sid.as_str().strip_prefix(prefix)?.parse().ok()
}

/// The reverse of [`snapshot`]: maps a catalog ACL (`acl.upsert`) back to
/// uids/gids through winbind, for placeholders of files that were never local
/// (control API §12 item 6: the edge uses the ACL the service supplies and never
/// invents one). A placeholder created in the staging directory and linked
/// into place does not inherit the share's default ACL, so this is the only
/// way it gets the right one.
pub fn to_local(s: &AclSnapshot, idmap: &dyn IdMap) -> Result<LocalAcl, SnapshotError> {
    let uid_of = |sid: &Sid| -> Result<u32, SnapshotError> {
        if let Some(u) = unix_id(sid, "S-1-22-1-") {
            return Ok(u);
        }
        idmap
            .sid_to_uid(sid)?
            .ok_or_else(|| SnapshotError::Unmapped(sid.clone(), "uid"))
    };
    let gid_of = |sid: &Sid| -> Result<u32, SnapshotError> {
        if let Some(g) = unix_id(sid, "S-1-22-2-") {
            return Ok(g);
        }
        idmap
            .sid_to_gid(sid)?
            .ok_or_else(|| SnapshotError::Unmapped(sid.clone(), "gid"))
    };
    let uid = match &s.owner_sid {
        Some(o) => uid_of(o)?,
        None => 0,
    };
    let gid = match &s.group_sid {
        Some(g) => gid_of(g)?,
        None => 0,
    };
    let mut access = Vec::new();
    let mut default = Vec::new();
    for e in &s.entries {
        let perms = Perms::parse(&e.perms).ok_or_else(|| SnapshotError::BadEntry(e.clone()))?;
        let need_sid = || {
            e.sid
                .as_ref()
                .ok_or_else(|| SnapshotError::BadEntry(e.clone()))
        };
        let tag = match e.tag {
            SnapTag::Owner => Tag::UserObj,
            SnapTag::OwningGroup => Tag::GroupObj,
            SnapTag::Mask => Tag::Mask,
            SnapTag::Other => Tag::Other,
            SnapTag::User => Tag::User(uid_of(need_sid()?)?),
            SnapTag::Group => Tag::Group(gid_of(need_sid()?)?),
        };
        let entry = crate::posix::Entry { tag, perms };
        match e.scope {
            Scope::Access => access.push(entry),
            Scope::Default => default.push(entry),
        }
    }
    let access = if access.is_empty() {
        PosixAcl::from_mode(s.mode)
    } else {
        PosixAcl::new(access)
    };
    let default = (!default.is_empty()).then(|| PosixAcl::new(default));
    Ok(LocalAcl {
        uid,
        gid,
        mode: s.mode & 0o7777,
        access,
        default,
    })
}

/// Builds the snapshot for SIDs already resolved: `sid_of(tag)` gives the SID
/// of a named entry (`Tag::User`/`Tag::Group`).
pub fn snapshot_with(
    owner_sid: Option<Sid>,
    group_sid: Option<Sid>,
    mode: u32,
    access: &PosixAcl,
    default: Option<&PosixAcl>,
    mut sid_of: impl FnMut(Tag) -> Option<Sid>,
) -> AclSnapshot {
    let mut entries = Vec::new();
    let mut push = |scope, acl: &PosixAcl| {
        for e in acl.entries() {
            let (tag, sid) = match e.tag {
                Tag::UserObj => (SnapTag::Owner, None),
                Tag::User(_) => (SnapTag::User, sid_of(e.tag)),
                Tag::GroupObj => (SnapTag::OwningGroup, None),
                Tag::Group(_) => (SnapTag::Group, sid_of(e.tag)),
                Tag::Mask => (SnapTag::Mask, None),
                Tag::Other => (SnapTag::Other, None),
            };
            entries.push(SnapEntry {
                scope,
                tag,
                sid,
                perms: e.perms.as_string(),
            });
        }
    };
    push(Scope::Access, access);
    if let Some(d) = default {
        push(Scope::Default, d);
    }
    let mut keyed: BTreeMap<String, SnapEntry> =
        entries.into_iter().map(|e| (e.canonical(), e)).collect();
    let entries: Vec<SnapEntry> = std::mem::take(&mut keyed).into_values().collect();
    let mut s = AclSnapshot {
        sha256: String::new(),
        owner_sid,
        group_sid,
        mode: mode & 0o7777,
        entries,
    };
    s.sha256 = sha256_hex(s.canonical_json().as_bytes());
    s
}

/// The snapshot of one inode, resolving uids and gids through `idmap`.
pub fn snapshot(f: &FileAcl, idmap: &dyn IdMap) -> Result<AclSnapshot, SnapshotError> {
    let user = |uid: u32| -> Result<Sid, IdMapError> {
        Ok(idmap
            .uid_to_sid(uid)?
            .unwrap_or_else(|| Sid::unix_user(uid)))
    };
    let group = |gid: u32| -> Result<Sid, IdMapError> {
        Ok(idmap
            .gid_to_sid(gid)?
            .unwrap_or_else(|| Sid::unix_group(gid)))
    };
    let access = f.effective_access();
    // Resolve every named id up front so an id-map error is not swallowed.
    let mut sids: BTreeMap<Tag, Sid> = BTreeMap::new();
    for acl in std::iter::once(&access).chain(f.default.as_ref()) {
        for e in acl.entries() {
            match e.tag {
                Tag::User(u) if !sids.contains_key(&e.tag) => {
                    sids.insert(e.tag, user(u)?);
                }
                Tag::Group(g) if !sids.contains_key(&e.tag) => {
                    sids.insert(e.tag, group(g)?);
                }
                _ => {}
            }
        }
    }
    Ok(snapshot_with(
        Some(user(f.uid)?),
        Some(group(f.gid)?),
        f.mode,
        &access,
        f.default.as_ref(),
        |t| sids.get(&t).cloned(),
    ))
}

impl AclSnapshot {
    /// The canonical JSON form the hash is computed over (module docs).
    pub fn canonical_json(&self) -> String {
        let entries: Vec<String> = self.entries.iter().map(SnapEntry::canonical).collect();
        let mut s = format!(r#"{{"entries":[{}]"#, entries.join(","));
        if let Some(g) = &self.group_sid {
            s.push_str(&format!(r#","group_sid":"{g}""#));
        }
        s.push_str(&format!(r#","mode":{}"#, self.mode));
        if let Some(o) = &self.owner_sid {
            s.push_str(&format!(r#","owner_sid":"{o}""#));
        }
        s.push('}');
        s
    }

    /// Recomputes the hash (after deserialising one, to check it).
    pub fn computed_sha256(&self) -> String {
        sha256_hex(self.canonical_json().as_bytes())
    }

    /// `(read_sids, deny_sids)` in POSIX.1e access-check order, exactly as
    /// the service derives them (control API §4.6, migration 0007,
    /// `omnifile_service.api.acls.access_sets`; shared vectors in
    /// `tests/acl/vectors/acl-access.json`):
    ///
    /// 1. the owner entry decides for the owner (unmasked);
    /// 2. a named-user entry decides for that user, even when it denies
    ///    (masked);
    /// 3. else any matching group entry (owning or named) granting `r`
    ///    (masked) grants, and a matching one without `r` denies;
    /// 4. else `other` (Everyone, `S-1-1-0`, unmasked).
    ///
    /// `read_sids` is the index prefilter; use [`can_read`] for an answer.
    pub fn access_sets(&self) -> (Vec<Sid>, Vec<Sid>) {
        use std::collections::{BTreeMap, BTreeSet};
        let access: Vec<&SnapEntry> = self
            .entries
            .iter()
            .filter(|e| e.scope == Scope::Access)
            .collect();
        let mask = access
            .iter()
            .find(|e| e.tag == SnapTag::Mask)
            .and_then(|e| Perms::parse(&e.perms));
        let reads = |e: &SnapEntry, masked: bool| -> bool {
            let mut b = Perms::parse(&e.perms).map(|p| p.bits()).unwrap_or(0);
            if masked && let Some(m) = mask {
                b &= m.bits();
            }
            b & 4 != 0
        };
        let mut users: BTreeMap<Sid, bool> = BTreeMap::new();
        if let Some(o) = &self.owner_sid {
            for e in access.iter().filter(|e| e.tag == SnapTag::Owner) {
                users.insert(o.clone(), reads(e, false));
            }
        }
        for e in access.iter().filter(|e| e.tag == SnapTag::User) {
            if let Some(sid) = &e.sid
                && !users.contains_key(sid)
            {
                users.insert(sid.clone(), reads(e, true));
            }
        }
        let mut grant = BTreeSet::new();
        let mut deny_groups = BTreeSet::new();
        for e in &access {
            let who = match e.tag {
                SnapTag::OwningGroup => self.group_sid.clone(),
                SnapTag::Group => e.sid.clone(),
                _ => None,
            };
            let Some(who) = who else { continue };
            if reads(e, true) {
                grant.insert(who);
            } else {
                deny_groups.insert(who);
            }
        }
        let other = access
            .iter()
            .any(|e| e.tag == SnapTag::Other && reads(e, false));
        let mut read: BTreeSet<Sid> = users
            .iter()
            .filter(|(_, ok)| **ok)
            .map(|(s, _)| s.clone())
            .collect();
        read.extend(grant.iter().cloned());
        if other {
            read.insert(everyone());
        }
        let mut deny: BTreeSet<Sid> = users
            .iter()
            .filter(|(_, ok)| !**ok)
            .map(|(s, _)| s.clone())
            .collect();
        deny.extend(deny_groups.difference(&grant).cloned());
        (read.into_iter().collect(), deny.into_iter().collect())
    }

    /// The `read_sids` prefilter (see [`access_sets`](Self::access_sets)).
    pub fn read_sids(&self) -> Vec<Sid> {
        self.access_sets().0
    }

    /// Can `user` (with its transitive `groups`) read through this ACL?
    pub fn can_read(&self, user: &Sid, groups: &[Sid]) -> bool {
        let (read, deny) = self.access_sets();
        can_read(&read, &deny, user, groups)
    }
}

fn everyone() -> Sid {
    Sid::parse("S-1-1-0").expect("Everyone")
}

/// `catalog.acl_can_read` (migration 0007) over the two sets.
pub fn can_read(read: &[Sid], deny: &[Sid], user: &Sid, groups: &[Sid]) -> bool {
    if deny.contains(user) {
        return false;
    }
    if read.contains(user) {
        return true;
    }
    let ev = everyone();
    if groups.iter().any(|g| *g != ev && read.contains(g)) {
        return true;
    }
    if groups.iter().any(|g| deny.contains(g)) {
        return false;
    }
    read.contains(&ev)
}

pub(crate) fn sha256_hex(b: &[u8]) -> String {
    let d = ring::digest::digest(&ring::digest::SHA256, b);
    d.as_ref().iter().map(|x| format!("{x:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::share::ShareGroups;
    use crate::sys::Kind;
    use omnifile_identity::fake::FakeIdMap;

    fn dir(access: Option<PosixAcl>, default: Option<PosixAcl>, uid: u32, gid: u32) -> FileAcl {
        FileAcl {
            dev: 1,
            ino: 2,
            kind: Kind::Dir,
            uid,
            gid,
            mode: access.as_ref().map(|a| a.mode_bits()).unwrap_or(0o755),
            access,
            default,
            has_ntacl: false,
        }
    }

    const G: ShareGroups = ShareGroups {
        owners: 30003,
        writers: Some(30002),
        readers: Some(30001),
    };

    #[test]
    fn share_root_snapshot_is_stable() {
        let m = FakeIdMap::dev_ad();
        let f = dir(Some(G.template()), Some(G.template()), 0, 30003);
        let s = snapshot(&f, &m).unwrap();
        let d = FakeIdMap::DOMAIN_SID;
        assert_eq!(s.owner_sid.as_ref().unwrap().as_str(), "S-1-22-1-0");
        assert_eq!(s.group_sid.as_ref().unwrap().as_str(), format!("{d}-1105"));
        assert_eq!(s.mode, 0o770);
        assert_eq!(s.entries.len(), 14);
        // Golden canonical form: any change here changes every acl_sha256 the
        // edge reports, so it must be coordinated with the service.
        let expect = format!(
            concat!(
                r#"{{"entries":["#,
                r#"{{"perms":"---","scope":"access","tag":"other"}},"#,
                r#"{{"perms":"---","scope":"access","tag":"owning_group"}},"#,
                r#"{{"perms":"---","scope":"default","tag":"other"}},"#,
                r#"{{"perms":"---","scope":"default","tag":"owning_group"}},"#,
                r#"{{"perms":"r-x","scope":"access","sid":"{d}-1103","tag":"group"}},"#,
                r#"{{"perms":"r-x","scope":"default","sid":"{d}-1103","tag":"group"}},"#,
                r#"{{"perms":"rwx","scope":"access","sid":"{d}-1104","tag":"group"}},"#,
                r#"{{"perms":"rwx","scope":"access","sid":"{d}-1105","tag":"group"}},"#,
                r#"{{"perms":"rwx","scope":"access","tag":"mask"}},"#,
                r#"{{"perms":"rwx","scope":"access","tag":"owner"}},"#,
                r#"{{"perms":"rwx","scope":"default","sid":"{d}-1104","tag":"group"}},"#,
                r#"{{"perms":"rwx","scope":"default","sid":"{d}-1105","tag":"group"}},"#,
                r#"{{"perms":"rwx","scope":"default","tag":"mask"}},"#,
                r#"{{"perms":"rwx","scope":"default","tag":"owner"}}"#,
                r#"],"group_sid":"{d}-1105","mode":504,"owner_sid":"S-1-22-1-0"}}"#
            ),
            d = d
        );
        assert_eq!(s.canonical_json(), expect);
        assert_eq!(s.sha256, sha256_hex(expect.as_bytes()));
        assert_eq!(s.sha256.len(), 64);
        // The same ACL built in another order hashes the same.
        let again = snapshot(&f, &m).unwrap();
        assert_eq!(again.sha256, s.sha256);
        // The canonical form is valid JSON with the same content.
        let v: serde_json::Value = serde_json::from_str(&s.canonical_json()).unwrap();
        assert_eq!(v["mode"], 504);
        assert_eq!(v["entries"].as_array().unwrap().len(), 14);
    }

    #[test]
    fn hash_changes_with_any_right() {
        let m = FakeIdMap::dev_ad();
        let base = snapshot(&dir(Some(G.template()), None, 0, 30003), &m).unwrap();
        let mut a = G.template();
        a.set(Tag::Group(30001), Perms::R);
        let changed = snapshot(&dir(Some(a), None, 0, 30003), &m).unwrap();
        assert_ne!(base.sha256, changed.sha256);
        let other_owner = snapshot(&dir(Some(G.template()), None, 20005, 30003), &m).unwrap();
        assert_ne!(base.sha256, other_owner.sha256);
    }

    #[test]
    fn minimal_acl_from_mode_and_read_sids() {
        let m = FakeIdMap::dev_ad();
        let f = FileAcl {
            kind: Kind::File,
            mode: 0o640,
            ..dir(None, None, 20001, 30001)
        };
        let s = snapshot(&f, &m).unwrap();
        assert_eq!(s.entries.len(), 3);
        let d = FakeIdMap::DOMAIN_SID;
        assert_eq!(
            s.read_sids()
                .iter()
                .map(|x| x.to_string())
                .collect::<Vec<_>>(),
            vec![format!("{d}-1103"), format!("{d}-1108")]
        );
        // Share template: Owners, Writers, Readers can read; owner root can.
        let t = snapshot(&dir(Some(G.template()), None, 0, 30003), &m).unwrap();
        assert_eq!(t.read_sids().len(), 4);
        assert!(!t.read_sids().iter().any(|x| x.as_str() == "S-1-1-0"));
    }

    #[test]
    fn mask_limits_read_sids() {
        let m = FakeIdMap::dev_ad();
        let mut a = G.template();
        a.set(Tag::Mask, Perms::NONE);
        let s = snapshot(&dir(Some(a), None, 0, 30003), &m).unwrap();
        assert_eq!(
            s.read_sids()
                .iter()
                .map(|x| x.to_string())
                .collect::<Vec<_>>(),
            vec!["S-1-22-1-0"]
        );
    }

    #[test]
    fn unmapped_ids_get_unix_sids() {
        let m = FakeIdMap::dev_ad();
        let mut a = PosixAcl::from_mode(0o750);
        a.set(Tag::User(4242), Perms::R);
        a.recompute_mask();
        let s = snapshot(&dir(Some(a), None, 1000, 1000), &m).unwrap();
        assert!(
            s.entries
                .iter()
                .any(|e| e.sid.as_ref().map(|x| x.as_str()) == Some("S-1-22-1-4242"))
        );
        assert_eq!(s.group_sid.unwrap().as_str(), "S-1-22-2-1000");
    }

    #[test]
    fn catalog_acl_maps_back_to_local_ids() {
        let m = FakeIdMap::dev_ad();
        let mut a = G.template();
        a.set(Tag::User(20003), Perms::R);
        a.set(Tag::User(4242), Perms::RX); // a local account: Unix SID
        a.recompute_mask();
        let f = dir(Some(a.clone()), Some(G.template()), 20005, 10000);
        let s = snapshot(&f, &m).unwrap();
        let l = to_local(&s, &m).unwrap();
        assert_eq!((l.uid, l.gid), (20005, 10000));
        assert_eq!(l.access, a);
        assert_eq!(l.default, Some(G.template()));
        assert_eq!(l.mode, f.mode);
        // Round trip: the same hash.
        let back = FileAcl {
            uid: l.uid,
            gid: l.gid,
            mode: l.mode,
            access: Some(l.access),
            default: l.default,
            ..f
        };
        assert_eq!(snapshot(&back, &m).unwrap().sha256, s.sha256);
    }

    #[test]
    fn unknown_sid_fails_closed() {
        let m = FakeIdMap::dev_ad();
        let mut s = snapshot(&dir(Some(G.template()), None, 0, 30003), &m).unwrap();
        s.entries.push(SnapEntry {
            scope: Scope::Access,
            tag: SnapTag::Group,
            sid: Some(Sid::parse("S-1-5-21-9-9-9-4242").unwrap()),
            perms: "r--".into(),
        });
        assert!(matches!(
            to_local(&s, &m),
            Err(SnapshotError::Unmapped(_, "gid"))
        ));
        s.entries.pop();
        s.entries.push(SnapEntry {
            scope: Scope::Access,
            tag: SnapTag::User,
            sid: None,
            perms: "r--".into(),
        });
        assert!(matches!(to_local(&s, &m), Err(SnapshotError::BadEntry(_))));
    }

    /// The vectors the service tests too (`service/tests/api/test_acl_vectors.py`):
    /// same read/deny sets and the same decisions, so edge and service agree.
    #[test]
    fn shared_access_vectors() {
        let v: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../tests/acl/vectors/acl-access.json"
        ))
        .unwrap();
        let sids = |x: &serde_json::Value| -> Vec<Sid> {
            x.as_array()
                .unwrap()
                .iter()
                .map(|s| Sid::parse(s.as_str().unwrap()).unwrap())
                .collect()
        };
        let cases = v["cases"].as_array().unwrap();
        assert!(cases.len() >= 10);
        for c in cases {
            let name = c["name"].as_str().unwrap();
            let snap = AclSnapshot {
                sha256: String::new(),
                owner_sid: c["owner_sid"].as_str().map(|s| Sid::parse(s).unwrap()),
                group_sid: c["group_sid"].as_str().map(|s| Sid::parse(s).unwrap()),
                mode: 0,
                entries: serde_json::from_value(c["entries"].clone()).unwrap(),
            };
            let (read, deny) = snap.access_sets();
            assert_eq!(read, sids(&c["read_sids"]), "{name}: read_sids");
            assert_eq!(deny, sids(&c["deny_sids"]), "{name}: deny_sids");
            for k in c["checks"].as_array().unwrap() {
                let user = Sid::parse(k["user"].as_str().unwrap()).unwrap();
                let groups = sids(&k["groups"]);
                assert_eq!(
                    snap.can_read(&user, &groups),
                    k["can_read"].as_bool().unwrap(),
                    "{name}: {user} {groups:?}"
                );
            }
        }
    }

    #[test]
    fn named_user_deny_and_mask_limited_groups() {
        let m = FakeIdMap::dev_ad();
        let d = FakeIdMap::DOMAIN_SID;
        let sid = |rid: u32| Sid::parse(&format!("{d}-{rid}")).unwrap();
        // Share file; Explorer adds `geo-nested: no access` as user:20006:---.
        let mut a = G.template();
        a.set(Tag::User(20006), Perms::NONE);
        let s = snapshot(&dir(Some(a.clone()), None, 0, 30003), &m).unwrap();
        let nested_groups = [sid(513), sid(1107), sid(1103)];
        assert!(
            !s.can_read(&sid(1113), &nested_groups),
            "named-user deny wins"
        );
        assert!(s.can_read(&sid(1108), &[sid(513), sid(1103)]));
        assert!(s.access_sets().1.contains(&sid(1113)));
        // chmod g-r on the file: mask -wx limits every group entry.
        a.set(Tag::Mask, Perms(3));
        let s = snapshot(&dir(Some(a), None, 0, 30003), &m).unwrap();
        assert!(!s.can_read(&sid(1108), &[sid(513), sid(1103)]));
        assert!(!s.can_read(&sid(1109), &[sid(513), sid(1104)]));
        assert!(s.can_read(&Sid::unix_user(0), &[]), "owner unmasked");
    }

    #[test]
    fn request_body_matches_openapi_shape() {
        let m = FakeIdMap::dev_ad();
        let s = snapshot(&dir(Some(G.template()), None, 0, 30003), &m).unwrap();
        let body = AclsRequest {
            dataset_id: "42".into(),
            acls: vec![s.clone()],
        };
        let v = serde_json::to_value(&body).unwrap();
        assert_eq!(v["dataset_id"], "42");
        let a = &v["acls"][0];
        for k in ["sha256", "owner_sid", "group_sid", "mode", "entries"] {
            assert!(a.get(k).is_some(), "{k}");
        }
        let e = &a["entries"][0];
        assert!(e["scope"].is_string() && e["tag"].is_string() && e["perms"].is_string());
        let back: AclSnapshot = serde_json::from_value(a.clone()).unwrap();
        assert_eq!(back.computed_sha256(), s.sha256);
        let tags: std::collections::BTreeSet<_> = a["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["tag"].as_str().unwrap().to_string())
            .collect();
        for t in &tags {
            assert!(
                ["owner", "owning_group", "user", "group", "mask", "other"].contains(&t.as_str())
            );
        }
    }
}
