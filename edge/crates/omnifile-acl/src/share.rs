//! A share's Owners / Writers / Readers AD groups as the share root's POSIX
//! access and default ACLs (access-control.md §4). The root ACL is the real
//! enforcement point on both protocols: inheritance gives every new file and
//! folder the same rights, over NFS and SMB alike.

use std::io;
use std::path::Path;

use crate::posix::{Entry, Perms, PosixAcl, Tag};
use crate::sys::{AclFs, FileAcl};

/// The gids winbind gives the share's three AD groups.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct ShareGroups {
    /// Full control (POSIX `rwx`).
    pub owners: u32,
    /// Read, write, create, delete (`rwx`).
    pub writers: Option<u32>,
    /// Read and traverse (`r-x`).
    pub readers: Option<u32>,
}

impl ShareGroups {
    /// The named entries the share's groups need, strongest first so a gid
    /// listed twice keeps the stronger right.
    pub fn entries(&self) -> Vec<Entry> {
        let mut v = Vec::new();
        if let Some(r) = self.readers {
            v.push(Entry {
                tag: Tag::Group(r),
                perms: Perms::RX,
            });
        }
        if let Some(w) = self.writers {
            v.push(Entry {
                tag: Tag::Group(w),
                perms: Perms::RWX,
            });
        }
        v.push(Entry {
            tag: Tag::Group(self.owners),
            perms: Perms::RWX,
        });
        v
    }

    /// The share root's access ACL, and the default ACL every new object
    /// inherits:
    ///
    /// ```text
    /// user::rwx  group::---  group:<Owners>:rwx  group:<Writers>:rwx
    /// group:<Readers>:r-x  mask::rwx  other::---
    /// ```
    ///
    /// `group::---` matters: a new file's owning group is its creator's
    /// primary group (normally *Domain Users*), so any right on the owning
    /// group would go to every user in the domain. `other::---` keeps
    /// everyone else out. Access comes only from the named AD groups.
    pub fn template(&self) -> PosixAcl {
        let mut a = PosixAcl::new([
            Entry {
                tag: Tag::UserObj,
                perms: Perms::RWX,
            },
            Entry {
                tag: Tag::GroupObj,
                perms: Perms::NONE,
            },
            Entry {
                tag: Tag::Other,
                perms: Perms::NONE,
            },
        ]);
        for e in self.entries() {
            a.set(e.tag, e.perms);
        }
        a.recompute_mask();
        a
    }
}

/// How a share root differs from what its groups require. Differences are
/// reported, not silently reverted: changes made in Explorer stay valid
/// (access-control.md §4), and the catalog gets the real ACL.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RootDrift {
    /// Share-group entries that are gone (access ACL).
    pub missing: Vec<Entry>,
    /// Share-group entries whose effective rights are lower than required
    /// (expected entry, actual effective perms).
    pub weakened: Vec<(Entry, Perms)>,
    /// Named entries that are not one of the share's groups (e.g. a user or
    /// group added in Explorer).
    pub extra: Vec<Entry>,
    /// Share-group entries missing from the default ACL: new files will not
    /// inherit them.
    pub default_missing: Vec<Entry>,
    /// The root has no default ACL at all.
    pub no_default: bool,
    /// `other` or the owning group grants something: someone outside the
    /// share's groups can get in.
    pub opened: Vec<Entry>,
    /// `security.NTACL` is present (vfs_acl_xattr was loaded somewhere).
    pub ntacl: bool,
}

impl RootDrift {
    pub fn is_clean(&self) -> bool {
        *self == RootDrift::default()
    }

    /// Drift that weakens or bypasses the share's RBAC (as opposed to an
    /// added grant an Owner made on purpose).
    pub fn is_security_relevant(&self) -> bool {
        !self.missing.is_empty()
            || !self.weakened.is_empty()
            || !self.default_missing.is_empty()
            || self.no_default
            || !self.opened.is_empty()
            || self.ntacl
    }
}

/// Compares a share root's ACLs with what `groups` require.
pub fn check_root(actual: &FileAcl, groups: &ShareGroups) -> RootDrift {
    let access = actual.effective_access();
    let want = groups.entries();
    let mut d = RootDrift {
        ntacl: actual.has_ntacl,
        ..Default::default()
    };
    for e in &want {
        match access.effective(e.tag) {
            None => d.missing.push(*e),
            Some(p) if p.bits() & e.perms.bits() != e.perms.bits() => d.weakened.push((*e, p)),
            Some(_) => {}
        }
    }
    let wanted_tags: Vec<Tag> = want.iter().map(|e| e.tag).collect();
    for e in access.entries() {
        if matches!(e.tag, Tag::User(_) | Tag::Group(_)) && !wanted_tags.contains(&e.tag) {
            d.extra.push(*e);
        }
    }
    for t in [Tag::GroupObj, Tag::Other] {
        if let Some(p) = access.effective(t).filter(|p| p.bits() != 0) {
            // The owning group of the root is set to Owners by `apply_root`,
            // so a right on it is fine only when that is still the case.
            if t == Tag::GroupObj && actual.gid == groups.owners {
                continue;
            }
            d.opened.push(Entry { tag: t, perms: p });
        }
    }
    match &actual.default {
        None => d.no_default = true,
        Some(def) => {
            for e in &want {
                match def.get(e.tag) {
                    Some(p) if p.bits() & e.perms.bits() == e.perms.bits() => {}
                    _ => d.default_missing.push(*e),
                }
            }
            for t in [Tag::GroupObj, Tag::Other] {
                if let Some(p) = def.effective(t).filter(|p| p.bits() != 0) {
                    d.opened.push(Entry { tag: t, perms: p });
                }
            }
        }
    }
    d
}

/// Applies the share's groups to its root directory.
///
/// - First application (`previous = None`): the root gets the
///   [`ShareGroups::template`] as both access and default ACL, and its owning
///   group becomes Owners.
/// - A change of groups (`previous = Some(old)`): the old groups' entries are
///   removed and the new ones set, and every other entry (Explorer edits) is
///   kept. Descendants keep the entries they inherited; re-target them with
///   [`crate::scan::retarget_group`].
///
/// Returns the ACL that was written.
pub fn apply_root(
    fs: &dyn AclFs,
    root: &Path,
    groups: &ShareGroups,
    previous: Option<&ShareGroups>,
) -> io::Result<PosixAcl> {
    let cur = fs.read(root)?;
    apply_root_at(fs, root, (cur.dev, cur.ino), groups, previous)
}

/// [`apply_root`], only to the directory `pin` (`(st_dev, st_ino)`): a root
/// that is another inode by the time it is read or written (renamed away,
/// another directory or a symlink put in its place) gets nothing, and the
/// call fails with `ESTALE`. Every write goes to the pinned inode.
pub fn apply_root_at(
    fs: &dyn AclFs,
    root: &Path,
    pin: (u64, u64),
    groups: &ShareGroups,
    previous: Option<&ShareGroups>,
) -> io::Result<PosixAcl> {
    let cur = fs.read(root)?;
    if (cur.dev, cur.ino) != pin {
        return Err(io::Error::from_raw_os_error(libc::ESTALE));
    }
    if cur.kind != crate::sys::Kind::Dir {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "share root is not a directory",
        ));
    }
    let merge = |base: Option<PosixAcl>| -> PosixAcl {
        match (base, previous) {
            (Some(mut a), Some(old)) => {
                for e in old.entries() {
                    a.remove(e.tag);
                }
                for e in groups.entries() {
                    a.set(e.tag, e.perms);
                }
                a.set(Tag::GroupObj, Perms::NONE);
                a.recompute_mask();
                a
            }
            _ => groups.template(),
        }
    };
    let access = merge(Some(cur.effective_access()));
    let default = merge(cur.default.clone());
    fs.chown_at(root, pin, None, Some(groups.owners))?;
    fs.write_access_at(root, pin, &access)?;
    fs.write_default_at(root, pin, Some(&default))?;
    Ok(access)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sys::Kind;

    const G: ShareGroups = ShareGroups {
        owners: 30003,
        writers: Some(30002),
        readers: Some(30001),
    };

    fn root(access: PosixAcl, default: Option<PosixAcl>) -> FileAcl {
        FileAcl {
            dev: 1,
            ino: 128,
            kind: Kind::Dir,
            uid: 0,
            gid: 30003,
            mode: access.mode_bits(),
            access: Some(access),
            default,
            has_ntacl: false,
        }
    }

    #[test]
    fn template_is_closed_to_everyone_else() {
        let t = G.template();
        t.validate().unwrap();
        assert_eq!(
            t.to_text(),
            "user::rwx,group::---,group:30001:r-x,group:30002:rwx,group:30003:rwx,mask::rwx,other::---"
        );
        assert_eq!(t.mode_bits(), 0o770);
        let only_owners = ShareGroups {
            owners: 5,
            writers: None,
            readers: None,
        };
        assert_eq!(
            only_owners.template().to_text(),
            "user::rwx,group::---,group:5:rwx,mask::rwx,other::---"
        );
        // A group that is both Readers and Owners keeps rwx.
        let same = ShareGroups {
            owners: 7,
            writers: None,
            readers: Some(7),
        };
        assert_eq!(same.template().get(Tag::Group(7)), Some(Perms::RWX));
    }

    #[test]
    fn clean_root() {
        let d = check_root(&root(G.template(), Some(G.template())), &G);
        assert!(d.is_clean(), "{d:?}");
    }

    #[test]
    fn explorer_edits_are_classified() {
        // An Owner removed Readers, added a user, and opened it to Everyone.
        let mut a = G.template();
        a.remove(Tag::Group(30001));
        a.set(Tag::User(20003), Perms::R);
        a.set(Tag::Other, Perms::R);
        let mut def = G.template();
        def.remove(Tag::Group(30001));
        let d = check_root(&root(a, Some(def)), &G);
        assert_eq!(
            d.missing,
            vec![Entry {
                tag: Tag::Group(30001),
                perms: Perms::RX
            }]
        );
        assert_eq!(
            d.extra,
            vec![Entry {
                tag: Tag::User(20003),
                perms: Perms::R
            }]
        );
        assert_eq!(d.opened.len(), 1);
        assert_eq!(d.default_missing.len(), 1);
        assert!(d.is_security_relevant());
    }

    #[test]
    fn mask_weakens_and_no_default() {
        let mut a = G.template();
        a.set(Tag::Mask, Perms::RX); // chmod g-w on the root
        let d = check_root(&root(a, None), &G);
        assert!(d.no_default);
        assert_eq!(d.weakened.len(), 2, "{d:?}");
        assert!(d.missing.is_empty());
    }

    #[test]
    fn added_grant_alone_is_not_security_relevant() {
        let mut a = G.template();
        a.set(Tag::Group(40000), Perms::RX);
        let d = check_root(&root(a, Some(G.template())), &G);
        assert!(!d.is_clean());
        assert!(!d.is_security_relevant());
    }

    #[test]
    fn minimal_mode_root_is_all_missing() {
        let r = FileAcl {
            access: None,
            mode: 0o755,
            gid: 0,
            ..root(G.template(), None)
        };
        let d = check_root(&r, &G);
        assert_eq!(d.missing.len(), 3);
        assert_eq!(d.opened.len(), 2, "group r-x and other r-x");
    }
}
