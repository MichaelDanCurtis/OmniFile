//! POSIX ACLs and their Linux xattr encoding (`system.posix_acl_access`,
//! `system.posix_acl_default`), in pure Rust: no libacl.
//!
//! Wire format (`include/uapi/linux/posix_acl_xattr.h`), little-endian:
//! a `u32` version (2), then `{u16 tag, u16 perm, u32 id}` per entry. Named
//! entries carry a uid/gid, the others `ACL_UNDEFINED_ID` (`u32::MAX`).

use std::fmt;

pub const XATTR_ACCESS: &str = "system.posix_acl_access";
pub const XATTR_DEFAULT: &str = "system.posix_acl_default";
const VERSION: u32 = 2;
const UNDEFINED_ID: u32 = u32::MAX;

/// `rwx` bits (4 = r, 2 = w, 1 = x).
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Default)]
pub struct Perms(pub u8);

impl Perms {
    pub const NONE: Perms = Perms(0);
    pub const R: Perms = Perms(4);
    pub const RX: Perms = Perms(5);
    pub const RW: Perms = Perms(6);
    pub const RWX: Perms = Perms(7);

    pub fn bits(self) -> u8 {
        self.0 & 7
    }
    pub fn read(self) -> bool {
        self.0 & 4 != 0
    }
    pub fn write(self) -> bool {
        self.0 & 2 != 0
    }
    pub fn exec(self) -> bool {
        self.0 & 1 != 0
    }

    /// `rwx` / `r-x` / `---`.
    pub fn as_string(self) -> String {
        let b = self.bits();
        let c = |m, ch| if b & m != 0 { ch } else { '-' };
        [c(4, 'r'), c(2, 'w'), c(1, 'x')].iter().collect()
    }

    pub fn parse(s: &str) -> Option<Perms> {
        let b = s.as_bytes();
        if b.len() != 3 {
            return None;
        }
        let bit = |i: usize, ch: u8, v: u8| match b[i] {
            x if x == ch => Some(v),
            b'-' => Some(0),
            _ => None,
        };
        Some(Perms(
            bit(0, b'r', 4)? | bit(1, b'w', 2)? | bit(2, b'x', 1)?,
        ))
    }
}

impl fmt::Display for Perms {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.as_string())
    }
}

/// ACL entry tags, in the order the kernel requires them.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Tag {
    /// The file's owner (`user::`).
    UserObj,
    /// A named user (`user:<uid>:`).
    User(u32),
    /// The file's owning group (`group::`).
    GroupObj,
    /// A named group (`group:<gid>:`).
    Group(u32),
    Mask,
    Other,
}

impl Tag {
    fn wire(self) -> (u16, u32) {
        match self {
            Tag::UserObj => (0x01, UNDEFINED_ID),
            Tag::User(u) => (0x02, u),
            Tag::GroupObj => (0x04, UNDEFINED_ID),
            Tag::Group(g) => (0x08, g),
            Tag::Mask => (0x10, UNDEFINED_ID),
            Tag::Other => (0x20, UNDEFINED_ID),
        }
    }

    fn from_wire(tag: u16, id: u32) -> Option<Tag> {
        Some(match tag {
            0x01 => Tag::UserObj,
            0x02 => Tag::User(id),
            0x04 => Tag::GroupObj,
            0x08 => Tag::Group(id),
            0x10 => Tag::Mask,
            0x20 => Tag::Other,
            _ => return None,
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Entry {
    pub tag: Tag,
    pub perms: Perms,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum AclError {
    #[error("ACL xattr too short or misaligned ({0} bytes)")]
    Length(usize),
    #[error("unsupported ACL xattr version {0}")]
    Version(u32),
    #[error("unknown ACL tag {0:#x}")]
    UnknownTag(u16),
    #[error("invalid ACL: {0}")]
    Invalid(&'static str),
    #[error("bad ACL text: {0:?}")]
    Text(String),
}

/// One POSIX ACL (access or default), kept sorted in kernel order.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Default)]
pub struct PosixAcl {
    entries: Vec<Entry>,
}

impl PosixAcl {
    /// Builds an ACL from entries in any order. Duplicate tags: the last wins.
    pub fn new(entries: impl IntoIterator<Item = Entry>) -> PosixAcl {
        let mut m = std::collections::BTreeMap::new();
        for e in entries {
            m.insert(e.tag, e.perms);
        }
        PosixAcl {
            entries: m
                .into_iter()
                .map(|(tag, perms)| Entry { tag, perms })
                .collect(),
        }
    }

    /// The three-entry ACL equivalent to a mode.
    pub fn from_mode(mode: u32) -> PosixAcl {
        PosixAcl::new([
            Entry {
                tag: Tag::UserObj,
                perms: Perms(((mode >> 6) & 7) as u8),
            },
            Entry {
                tag: Tag::GroupObj,
                perms: Perms(((mode >> 3) & 7) as u8),
            },
            Entry {
                tag: Tag::Other,
                perms: Perms((mode & 7) as u8),
            },
        ])
    }

    pub fn entries(&self) -> &[Entry] {
        &self.entries
    }

    pub fn get(&self, tag: Tag) -> Option<Perms> {
        self.entries.iter().find(|e| e.tag == tag).map(|e| e.perms)
    }

    pub fn set(&mut self, tag: Tag, perms: Perms) {
        let mut v = std::mem::take(&mut self.entries);
        v.push(Entry { tag, perms });
        *self = PosixAcl::new(v);
    }

    pub fn remove(&mut self, tag: Tag) {
        self.entries.retain(|e| e.tag != tag);
    }

    pub fn has_named(&self) -> bool {
        self.entries
            .iter()
            .any(|e| matches!(e.tag, Tag::User(_) | Tag::Group(_)))
    }

    /// Sets the mask to the union of the group class (named entries and the
    /// owning group), as `setfacl` does by default. Removes it when there are
    /// no named entries.
    /// The same ACL with no `w` in any entry, the mask included: setting it
    /// sets the mode from it, so nothing it covers is writable (rewind
    /// trees, decision 30).
    pub fn without_write(&self) -> PosixAcl {
        PosixAcl {
            entries: self
                .entries
                .iter()
                .map(|e| Entry {
                    tag: e.tag,
                    perms: Perms(e.perms.bits() & !2),
                })
                .collect(),
        }
    }

    pub fn recompute_mask(&mut self) {
        if !self.has_named() {
            self.remove(Tag::Mask);
            return;
        }
        let m = self
            .entries
            .iter()
            .filter(|e| matches!(e.tag, Tag::User(_) | Tag::Group(_) | Tag::GroupObj))
            .fold(0u8, |a, e| a | e.perms.bits());
        self.set(Tag::Mask, Perms(m));
    }

    /// The kernel's `posix_acl_valid`: exactly one owner, owning group and
    /// other entry; a mask iff there are named entries.
    pub fn validate(&self) -> Result<(), AclError> {
        let count = |f: fn(&Tag) -> bool| self.entries.iter().filter(|e| f(&e.tag)).count();
        if count(|t| *t == Tag::UserObj) != 1 {
            return Err(AclError::Invalid("needs exactly one user:: entry"));
        }
        if count(|t| *t == Tag::GroupObj) != 1 {
            return Err(AclError::Invalid("needs exactly one group:: entry"));
        }
        if count(|t| *t == Tag::Other) != 1 {
            return Err(AclError::Invalid("needs exactly one other:: entry"));
        }
        if self.has_named() && self.get(Tag::Mask).is_none() {
            return Err(AclError::Invalid("named entries need a mask"));
        }
        Ok(())
    }

    /// The mode bits this access ACL implies (owner, mask-or-group, other).
    pub fn mode_bits(&self) -> u32 {
        let p = |t| self.get(t).map(|p| p.bits() as u32).unwrap_or(0);
        let g = if self.get(Tag::Mask).is_some() {
            p(Tag::Mask)
        } else {
            p(Tag::GroupObj)
        };
        (p(Tag::UserObj) << 6) | (g << 3) | p(Tag::Other)
    }

    /// Is this ACL fully described by the mode (no named entries, no mask)?
    pub fn is_minimal(&self) -> bool {
        !self.has_named() && self.get(Tag::Mask).is_none()
    }

    /// Effective permissions of a named entry or the owning group: its perms
    /// limited by the mask.
    pub fn effective(&self, tag: Tag) -> Option<Perms> {
        let p = self.get(tag)?;
        Some(match (tag, self.get(Tag::Mask)) {
            (Tag::User(_) | Tag::Group(_) | Tag::GroupObj, Some(m)) => Perms(p.bits() & m.bits()),
            _ => p,
        })
    }

    pub fn to_xattr(&self) -> Vec<u8> {
        let mut v = Vec::with_capacity(4 + 8 * self.entries.len());
        v.extend_from_slice(&VERSION.to_le_bytes());
        for e in &self.entries {
            let (tag, id) = e.tag.wire();
            v.extend_from_slice(&tag.to_le_bytes());
            v.extend_from_slice(&(e.perms.bits() as u16).to_le_bytes());
            v.extend_from_slice(&id.to_le_bytes());
        }
        v
    }

    pub fn from_xattr(b: &[u8]) -> Result<PosixAcl, AclError> {
        if b.len() < 4 || !(b.len() - 4).is_multiple_of(8) {
            return Err(AclError::Length(b.len()));
        }
        let ver = u32::from_le_bytes(b[0..4].try_into().unwrap());
        if ver != VERSION {
            return Err(AclError::Version(ver));
        }
        let mut entries = Vec::with_capacity((b.len() - 4) / 8);
        for c in b[4..].as_chunks::<8>().0 {
            let tag = u16::from_le_bytes([c[0], c[1]]);
            let perm = u16::from_le_bytes([c[2], c[3]]);
            let id = u32::from_le_bytes([c[4], c[5], c[6], c[7]]);
            let tag = Tag::from_wire(tag, id).ok_or(AclError::UnknownTag(tag))?;
            entries.push(Entry {
                tag,
                perms: Perms((perm & 7) as u8),
            });
        }
        Ok(PosixAcl::new(entries))
    }

    /// `getfacl`-style short text: `user::rwx,group:30001:r-x,mask::rwx,other::---`.
    pub fn to_text(&self) -> String {
        self.entries
            .iter()
            .map(|e| {
                let (k, q) = match e.tag {
                    Tag::UserObj => ("user", String::new()),
                    Tag::User(u) => ("user", u.to_string()),
                    Tag::GroupObj => ("group", String::new()),
                    Tag::Group(g) => ("group", g.to_string()),
                    Tag::Mask => ("mask", String::new()),
                    Tag::Other => ("other", String::new()),
                };
                format!("{k}:{q}:{}", e.perms)
            })
            .collect::<Vec<_>>()
            .join(",")
    }

    /// Parses [`to_text`](Self::to_text) output (numeric ids only).
    pub fn from_text(s: &str) -> Result<PosixAcl, AclError> {
        let bad = || AclError::Text(s.to_string());
        let mut v = Vec::new();
        for part in s.split(',').map(str::trim).filter(|p| !p.is_empty()) {
            let f: Vec<&str> = part.split(':').collect();
            if f.len() != 3 {
                return Err(bad());
            }
            let perms = Perms::parse(f[2]).ok_or_else(bad)?;
            let id = || f[1].parse::<u32>().map_err(|_| bad());
            let tag = match (f[0], f[1].is_empty()) {
                ("user" | "u", true) => Tag::UserObj,
                ("user" | "u", false) => Tag::User(id()?),
                ("group" | "g", true) => Tag::GroupObj,
                ("group" | "g", false) => Tag::Group(id()?),
                ("mask" | "m", true) => Tag::Mask,
                ("other" | "o", true) => Tag::Other,
                _ => return Err(bad()),
            };
            v.push(Entry { tag, perms });
        }
        Ok(PosixAcl::new(v))
    }
}

impl fmt::Display for PosixAcl {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_text())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn perms_text() {
        assert_eq!(Perms::RX.as_string(), "r-x");
        assert_eq!(Perms::parse("rw-"), Some(Perms::RW));
        assert_eq!(Perms::parse("---"), Some(Perms::NONE));
        assert_eq!(Perms::parse("wrx"), None);
        assert_eq!(Perms::parse("rw"), None);
    }

    /// `setfacl -m g:30001:r-x,m::rwx` on a 0750 directory, as the kernel
    /// stores it (bytes captured from xfs on Linux 7.0).
    #[test]
    fn known_xattr_bytes() {
        let acl = PosixAcl::from_text("user::rwx,group::r-x,group:30001:r-x,mask::rwx,other::---")
            .unwrap();
        let b = acl.to_xattr();
        let expect: Vec<u8> = [
            &2u32.to_le_bytes()[..],
            &[1, 0, 7, 0, 0xff, 0xff, 0xff, 0xff],
            &[4, 0, 5, 0, 0xff, 0xff, 0xff, 0xff],
            &[8, 0, 5, 0],
            &30001u32.to_le_bytes(),
            &[0x10, 0, 7, 0, 0xff, 0xff, 0xff, 0xff],
            &[0x20, 0, 0, 0, 0xff, 0xff, 0xff, 0xff],
        ]
        .concat();
        assert_eq!(b, expect);
        assert_eq!(PosixAcl::from_xattr(&b).unwrap(), acl);
        acl.validate().unwrap();
        assert_eq!(acl.mode_bits(), 0o770);
    }

    #[test]
    fn rejects_bad_xattrs() {
        assert_eq!(PosixAcl::from_xattr(&[2, 0, 0]), Err(AclError::Length(3)));
        assert_eq!(
            PosixAcl::from_xattr(&[1, 0, 0, 0]),
            Err(AclError::Version(1))
        );
        let mut b = 2u32.to_le_bytes().to_vec();
        b.extend_from_slice(&[0x40, 0, 7, 0, 0, 0, 0, 0]);
        assert_eq!(PosixAcl::from_xattr(&b), Err(AclError::UnknownTag(0x40)));
    }

    #[test]
    fn validation_and_mask() {
        let mut a = PosixAcl::from_mode(0o750);
        a.validate().unwrap();
        assert!(a.is_minimal());
        assert_eq!(a.mode_bits(), 0o750);
        a.set(Tag::Group(30002), Perms::RWX);
        assert!(a.validate().is_err(), "named entry without mask");
        a.recompute_mask();
        a.validate().unwrap();
        assert_eq!(a.get(Tag::Mask), Some(Perms::RWX));
        a.set(Tag::Mask, Perms::R);
        assert_eq!(a.effective(Tag::Group(30002)), Some(Perms::R));
        assert_eq!(a.effective(Tag::UserObj), Some(Perms::RWX));
        a.remove(Tag::Group(30002));
        a.recompute_mask();
        assert!(a.is_minimal());
        assert!(PosixAcl::new([]).validate().is_err());
    }

    #[test]
    fn kernel_order() {
        let a = PosixAcl::from_text(
            "other::---,group:9:r--,user:5:rw-,mask::rw-,group::r--,user::rwx,user:2:r--",
        )
        .unwrap();
        assert_eq!(
            a.to_text(),
            "user::rwx,user:2:r--,user:5:rw-,group::r--,group:9:r--,mask::rw-,other::---"
        );
    }

    fn arb_acl() -> impl Strategy<Value = PosixAcl> {
        (
            0u8..8,
            0u8..8,
            0u8..8,
            proptest::collection::vec((any::<bool>(), 0u32..100_000, 0u8..8), 0..8),
        )
            .prop_map(|(u, g, o, named)| {
                let mut a = PosixAcl::new([
                    Entry {
                        tag: Tag::UserObj,
                        perms: Perms(u),
                    },
                    Entry {
                        tag: Tag::GroupObj,
                        perms: Perms(g),
                    },
                    Entry {
                        tag: Tag::Other,
                        perms: Perms(o),
                    },
                ]);
                for (user, id, p) in named {
                    a.set(if user { Tag::User(id) } else { Tag::Group(id) }, Perms(p));
                }
                a.recompute_mask();
                a
            })
    }

    proptest! {
        #[test]
        fn xattr_and_text_round_trip(a in arb_acl()) {
            prop_assert!(a.validate().is_ok());
            prop_assert_eq!(PosixAcl::from_xattr(&a.to_xattr()).unwrap(), a.clone());
            prop_assert_eq!(PosixAcl::from_text(&a.to_text()).unwrap(), a);
        }
    }
}
