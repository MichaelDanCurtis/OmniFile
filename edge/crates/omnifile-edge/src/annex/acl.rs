//! A NAS file's ACL as the NAS holds it (`annex.md` §5), for the `acl` of an
//! `arrived` / `changed` event. The service derives who may read it; the
//! edge only reads what is there and maps names it alone can map.
//!
//! - **SMB** (the cifs client): `system.cifs_ntsd`, the self-relative NT
//!   security descriptor with owner, group and DACL, sent whole (`ntfs_sd`).
//!   The service parses it.
//! - **NFSv4**: `system.nfs4_acl`, the ACL as XDR. Its principals are NFSv4
//!   names (`user@domain`, `OWNER@`, `GROUP@`, `EVERYONE@`), which only the
//!   edge's winbind can turn into SIDs, so the edge sends the raw XDR plus
//!   one `{type, sid, mask, flags}` per ACE (`nfs4`). A name it can't map
//!   has `sid: null`: harmless in an allow, a refusal of the whole ACL in a
//!   deny of read (the service decides).
//! - **POSIX** (NFSv3, or no ACL): mode bits with the owner's and group's
//!   SIDs, and the POSIX ACL's entries if the server gives them
//!   (`system.posix_acl_access`).
//!
//! Every xattr read is a read of a file opened beneath the annex root
//! (`nasfs`); nothing here writes to the NAS. A file whose ACL can't be read
//! is skipped (`acl_unreadable`), never given a guessed one.

use base64::Engine;
use omnifile_identity::{IdMap, IdMapError, Sid, SidKind};
use serde_json::{Value, json};

use super::nasfs::{Root, Stat, fgetxattr};

/// How the NAS is mounted, which decides where its ACL is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Flavour {
    Smb,
    Nfs4,
    Nfs3,
}

impl Flavour {
    pub fn from_protocol(p: &str) -> Flavour {
        match p {
            "smb" => Flavour::Smb,
            "nfs4" => Flavour::Nfs4,
            _ => Flavour::Nfs3,
        }
    }
}

pub const XATTR_NTSD: &str = "system.cifs_ntsd";
pub const XATTR_NFS4: &str = "system.nfs4_acl";
pub const XATTR_POSIX: &str = "system.posix_acl_access";

/// NFSv4 ACE (RFC 8881 §6.2.1).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Nfs4Ace {
    pub ace_type: u32,
    pub flag: u32,
    pub mask: u32,
    pub who: String,
}

pub const ACE4_ACCESS_ALLOWED: u32 = 0;
pub const ACE4_ACCESS_DENIED: u32 = 1;
pub const ACE4_IDENTIFIER_GROUP: u32 = 0x40;
pub const ACE4_INHERIT_ONLY: u32 = 0x08;

/// The XDR of `system.nfs4_acl`: a count, then per ACE type, flag, mask and
/// an opaque name padded to 4 bytes.
pub fn parse_nfs4(raw: &[u8]) -> Result<Vec<Nfs4Ace>, String> {
    let u32_at = |at: usize| -> Result<u32, String> {
        raw.get(at..at + 4)
            .map(|b| u32::from_be_bytes(b.try_into().unwrap()))
            .ok_or_else(|| "NFSv4 ACL is truncated".to_owned())
    };
    let count = u32_at(0)? as usize;
    if count > 1000 {
        return Err(format!("NFSv4 ACL has {count} entries"));
    }
    let mut at = 4;
    let mut out = Vec::with_capacity(count);
    for _ in 0..count {
        let ace_type = u32_at(at)?;
        let flag = u32_at(at + 4)?;
        let mask = u32_at(at + 8)?;
        let len = u32_at(at + 12)? as usize;
        let start = at + 16;
        let name = raw
            .get(start..start + len)
            .ok_or_else(|| "NFSv4 ACL name is truncated".to_owned())?;
        out.push(Nfs4Ace {
            ace_type,
            flag,
            mask,
            who: String::from_utf8_lossy(name).into_owned(),
        });
        at = start + len.div_ceil(4) * 4;
    }
    Ok(out)
}

/// POSIX ACL entries from `system.posix_acl_access` (version 2): tag, perm,
/// id. Named users and groups are mapped to SIDs.
pub fn parse_posix(raw: &[u8]) -> Result<Vec<(u16, u16, u32)>, String> {
    if raw.len() < 4 || u32::from_le_bytes(raw[0..4].try_into().unwrap()) != 2 {
        return Err("not a version-2 POSIX ACL".into());
    }
    let body = &raw[4..];
    if !body.len().is_multiple_of(8) {
        return Err("POSIX ACL is truncated".into());
    }
    Ok(body
        .chunks(8)
        .map(|c| {
            (
                u16::from_le_bytes([c[0], c[1]]),
                u16::from_le_bytes([c[2], c[3]]),
                u32::from_le_bytes(c[4..8].try_into().unwrap()),
            )
        })
        .collect())
}

const ACL_USER_OBJ: u16 = 0x01;
const ACL_USER: u16 = 0x02;
const ACL_GROUP_OBJ: u16 = 0x04;
const ACL_GROUP: u16 = 0x08;
const ACL_MASK: u16 = 0x10;
const ACL_OTHER: u16 = 0x20;

fn perms(bits: u16) -> String {
    format!(
        "{}{}{}",
        if bits & 4 != 0 { 'r' } else { '-' },
        if bits & 2 != 0 { 'w' } else { '-' },
        if bits & 1 != 0 { 'x' } else { '-' }
    )
}

/// SID lookups, cached by the caller's `CachedIdMap`. A failed lookup is a
/// missing SID (the service fails toward "can't read").
fn uid_sid(ids: &dyn IdMap, uid: u32) -> Option<String> {
    ids.uid_to_sid(uid).ok().flatten().map(|s| s.to_string())
}

fn gid_sid(ids: &dyn IdMap, gid: u32) -> Option<String> {
    ids.gid_to_sid(gid).ok().flatten().map(|s| s.to_string())
}

/// What says an ACL hasn't changed: the NAS's raw ACL with the numeric ids and
/// mode, hashed. Nothing goes through a lookup, so one that fails (winbind, a
/// moment) can't look like a change; only a read that fails is an error.
pub fn raw_print(f: &std::fs::File, flavour: Flavour, st: &Stat) -> Result<String, String> {
    let read = |name: &str| fgetxattr(f, name).map_err(|e| format!("{name}: {e}"));
    let raw = match flavour {
        Flavour::Smb => read(XATTR_NTSD)?,
        Flavour::Nfs4 => read(XATTR_NFS4)?,
        Flavour::Nfs3 => read(XATTR_POSIX)?,
    };
    let mut bytes = format!("{}:{}:{:o}:", st.uid, st.gid, st.mode & 0o7777).into_bytes();
    bytes.extend_from_slice(&raw.unwrap_or_default());
    let d = ring::digest::digest(&ring::digest::SHA256, &bytes);
    Ok(d.as_ref().iter().map(|b| format!("{b:02x}")).collect())
}

/// Whether a server shows POSIX ACLs on this file: it answers with one, or says
/// there is none. `fgetxattr` here reads both a server with no ACL support and a
/// file with no ACL as "none", which for a capture fails toward what the mode says.
pub fn posix_acls_shown(f: &std::fs::File) -> bool {
    super::nasfs::xattr_supported(f, XATTR_POSIX)
}

/// Decision 64: whether the server shows the ACLs of this flavour at all (the annex
/// root, at every mount). A server that says it has no ACL support answers as if
/// every file had only its mode, so a hidden mask can make its group bits look wider
/// than the NAS allows. It doesn't catch a server that answers but withholds
/// entries, or a mixed-style volume.
pub fn acls_shown(f: &std::fs::File, flavour: Flavour) -> bool {
    let name = match flavour {
        Flavour::Smb => XATTR_NTSD,
        Flavour::Nfs4 => XATTR_NFS4,
        Flavour::Nfs3 => XATTR_POSIX,
    };
    super::nasfs::xattr_supported(f, name)
}

/// An `IdMap` that notes any lookup that failed, for what can't be read as "no
/// such principal": the ACL `go` is asked about.
struct Noting<'a>(&'a dyn IdMap, std::sync::atomic::AtomicBool);

impl Noting<'_> {
    fn note<T>(&self, r: Result<T, IdMapError>) -> Result<T, IdMapError> {
        if r.is_err() {
            self.1.store(true, std::sync::atomic::Ordering::SeqCst);
        }
        r
    }
}

impl IdMap for Noting<'_> {
    fn uid_to_sid(&self, uid: u32) -> Result<Option<Sid>, IdMapError> {
        self.note(self.0.uid_to_sid(uid))
    }
    fn gid_to_sid(&self, gid: u32) -> Result<Option<Sid>, IdMapError> {
        self.note(self.0.gid_to_sid(gid))
    }
    fn sid_to_uid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
        self.note(self.0.sid_to_uid(sid))
    }
    fn sid_to_gid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
        self.note(self.0.sid_to_gid(sid))
    }
    fn name_to_sid(&self, name: &str) -> Result<Option<(Sid, SidKind)>, IdMapError> {
        self.note(self.0.name_to_sid(name))
    }
    fn sid_to_name(&self, sid: &Sid) -> Result<Option<String>, IdMapError> {
        self.note(self.0.sid_to_name(sid))
    }
    fn user_token_sids(&self, user: &Sid) -> Result<Option<Vec<Sid>>, IdMapError> {
        self.note(self.0.user_token_sids(user))
    }
}

/// [`capture_on`], where a lookup that fails is an error, not a missing SID. A
/// capture for the catalog fails toward "can't read"; one that decides `go`
/// would have the service refuse the file and the edge delete a whole copy.
pub fn capture_strict(
    f: &std::fs::File,
    flavour: Flavour,
    st: &Stat,
    ids: &dyn IdMap,
) -> Result<Value, String> {
    let noting = Noting(ids, std::sync::atomic::AtomicBool::new(false));
    let v = capture_on(f, flavour, st, &noting)?;
    if noting.1.load(std::sync::atomic::Ordering::SeqCst) {
        return Err("identity lookups fail".into());
    }
    Ok(v)
}

/// The `acl` of an event for `rel` (stat'ed as `st`), or why it can't be
/// read. **Fail closed**: over SMB the mount's owner, group and mode are the
/// client's, not the file's, and over NFSv4 the mode is only part of the
/// ACL, so without the NAS's own ACL the file is skipped (`acl_unreadable`),
/// never described by attributes that would grant the wrong people access.
pub fn capture(
    root: &Root,
    rel: &str,
    flavour: Flavour,
    st: &Stat,
    ids: &dyn IdMap,
) -> Result<Value, String> {
    let f = root.open_file(rel).map_err(|e| format!("open: {e}"))?;
    capture_on(&f, flavour, st, ids)
}

/// A folder's ACL, as a file's (phase A3: who may have a recalled file
/// placed there). The service derives who may add a file from it.
pub fn capture_dir(
    root: &Root,
    rel: &str,
    flavour: Flavour,
    ids: &dyn IdMap,
) -> Result<(Value, Stat), String> {
    let (f, st) = root.open_dir(rel).map_err(|e| format!("open: {e}"))?;
    Ok((capture_on(&f, flavour, &st, ids)?, st))
}

/// The ACL of an open file or folder.
pub fn capture_on(
    f: &std::fs::File,
    flavour: Flavour,
    st: &Stat,
    ids: &dyn IdMap,
) -> Result<Value, String> {
    let read = |name: &str| fgetxattr(f, name).map_err(|e| format!("{name}: {e}"));
    let mode = st.mode & 0o7777;
    match flavour {
        Flavour::Smb => match read(XATTR_NTSD)? {
            Some(raw) if !raw.is_empty() => {
                Ok(json!({"kind": "ntfs_sd", "raw": b64(&raw), "mode": mode}))
            }
            _ => Err(format!("no NT security descriptor ({XATTR_NTSD})")),
        },
        Flavour::Nfs4 => match read(XATTR_NFS4)? {
            Some(raw) => nfs4_acl(&raw, st, ids),
            None => Err(format!("no NFSv4 ACL ({XATTR_NFS4})")),
        },
        Flavour::Nfs3 => {
            let owner = uid_sid(ids, st.uid);
            let group = gid_sid(ids, st.gid);
            let mut entries: Vec<Value> = Vec::new();
            if let Some(raw) = read(XATTR_POSIX)? {
                for (tag, perm, id) in parse_posix(&raw)? {
                    let (t, sid) = match tag {
                        ACL_USER_OBJ => ("owner", None),
                        ACL_GROUP_OBJ => ("owning_group", None),
                        ACL_MASK => ("mask", None),
                        ACL_OTHER => ("other", None),
                        ACL_USER => ("user", Some(uid_sid(ids, id))),
                        ACL_GROUP => ("group", Some(gid_sid(ids, id))),
                        _ => continue,
                    };
                    let mut e = json!({"scope": "access", "tag": t, "perms": perms(perm)});
                    if let Some(sid) = sid {
                        e["sid"] = json!(sid);
                    }
                    entries.push(e);
                }
            }
            let mut v = json!({"kind": "posix", "mode": mode, "entries": entries});
            put_sids(&mut v, &owner, &group);
            Ok(v)
        }
    }
}

fn b64(b: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(b)
}

/// An NFSv4 ACL's event body: the raw XDR and each ACE, in order (the
/// service evaluates them deny-first), with the principals mapped to SIDs.
pub fn nfs4_acl(raw: &[u8], st: &Stat, ids: &dyn IdMap) -> Result<Value, String> {
    let aces = parse_nfs4(raw)?;
    let owner = uid_sid(ids, st.uid);
    let group = gid_sid(ids, st.gid);
    let mapped: Vec<Value> = aces
        .iter()
        .filter(|a| matches!(a.ace_type, ACE4_ACCESS_ALLOWED | ACE4_ACCESS_DENIED))
        .map(|a| {
            let sid = match a.who.as_str() {
                "OWNER@" => owner.clone(),
                "GROUP@" => group.clone(),
                "EVERYONE@" => Some("S-1-1-0".to_owned()),
                who => ids
                    .name_to_sid(who)
                    .ok()
                    .flatten()
                    .map(|(s, _)| s.to_string()),
            };
            json!({
                "type": if a.ace_type == ACE4_ACCESS_DENIED { "deny" } else { "allow" },
                "sid": sid,
                "mask": a.mask,
                "flags": a.flag,
            })
        })
        .collect();
    let mut v = json!({"kind": "nfs4", "raw": b64(raw), "mode": st.mode & 0o7777, "aces": mapped});
    put_sids(&mut v, &owner, &group);
    Ok(v)
}

/// The owner and group SIDs of a captured ACL (an `acl` event body): from
/// the NT descriptor's header, or the NFSv4/POSIX body's mapped SIDs.
pub fn owner_group(acl: &Value) -> (Option<String>, Option<String>) {
    let field = |k: &str| acl.get(k).and_then(Value::as_str).map(str::to_owned);
    if acl.get("kind").and_then(Value::as_str) != Some("ntfs_sd") {
        return (field("owner_sid"), field("group_sid"));
    }
    let Some(raw) = acl
        .get("raw")
        .and_then(Value::as_str)
        .and_then(|r| base64::engine::general_purpose::STANDARD.decode(r).ok())
    else {
        return (None, None);
    };
    let u32_at = |at: usize| {
        raw.get(at..at + 4)
            .map(|b| u32::from_le_bytes(b.try_into().unwrap()))
    };
    let sid_at = |off: Option<u32>| -> Option<String> {
        let o = off? as usize;
        if o == 0 {
            return None;
        }
        let rev = *raw.get(o)?;
        let n = *raw.get(o + 1)? as usize;
        let auth = raw.get(o + 2..o + 8)?;
        let auth = auth.iter().fold(0u64, |a, b| a << 8 | u64::from(*b));
        let mut s = format!("S-{rev}-{auth}");
        for i in 0..n {
            let at = o + 8 + 4 * i;
            let sub = u32::from_le_bytes(raw.get(at..at + 4)?.try_into().ok()?);
            s.push_str(&format!("-{sub}"));
        }
        Some(s)
    };
    (sid_at(u32_at(4)), sid_at(u32_at(8)))
}

fn put_sids(v: &mut Value, owner: &Option<String>, group: &Option<String>) {
    if let Some(o) = owner {
        v["owner_sid"] = json!(o);
    }
    if let Some(g) = group {
        v["group_sid"] = json!(g);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn xdr(aces: &[(u32, u32, u32, &str)]) -> Vec<u8> {
        let mut b = (aces.len() as u32).to_be_bytes().to_vec();
        for (t, f, m, who) in aces {
            for v in [*t, *f, *m, who.len() as u32] {
                b.extend(v.to_be_bytes());
            }
            b.extend(who.as_bytes());
            b.resize(b.len().div_ceil(4) * 4, 0);
        }
        b
    }

    #[test]
    fn nfs4_acls_parse_with_padded_names() {
        let raw = xdr(&[
            (1, ACE4_IDENTIFIER_GROUP, 1, "interns@omni.test"),
            (0, 0, 0x12_0089, "OWNER@"),
            (0, 0, 1, "EVERYONE@"),
        ]);
        let aces = parse_nfs4(&raw).unwrap();
        assert_eq!(aces.len(), 3);
        assert_eq!(aces[0].who, "interns@omni.test");
        assert_eq!(aces[0].flag, ACE4_IDENTIFIER_GROUP);
        assert_eq!(aces[2].who, "EVERYONE@");
        assert!(parse_nfs4(&raw[..raw.len() - 6]).is_err());
    }

    #[test]
    fn posix_acls_parse() {
        let mut raw = 2u32.to_le_bytes().to_vec();
        for (t, p, id) in [(1u16, 6u16, u32::MAX), (2, 4, 10001), (0x20, 0, u32::MAX)] {
            raw.extend(t.to_le_bytes());
            raw.extend(p.to_le_bytes());
            raw.extend(id.to_le_bytes());
        }
        let l = parse_posix(&raw).unwrap();
        assert_eq!(
            l,
            vec![(1, 6, u32::MAX), (2, 4, 10001), (0x20, 0, u32::MAX)]
        );
        assert_eq!(perms(5), "r-x");
        assert!(parse_posix(&raw[..10]).is_err());
    }

    fn stat_of(p: &std::path::Path) -> Stat {
        Root::open(p.parent().unwrap())
            .unwrap()
            .stat(p.file_name().unwrap().to_str().unwrap(), false)
            .unwrap()
    }

    #[test]
    fn no_readable_acl_fails_closed_never_the_mounts_mode() {
        let t = tempfile::tempdir().unwrap();
        std::fs::write(t.path().join("f"), b"x").unwrap();
        let root = Root::open(t.path()).unwrap();
        let st = stat_of(&t.path().join("f"));
        let ids = omnifile_identity::fake::FakeIdMap::default();
        // A tmpfs/APFS file has no NT descriptor and no NFSv4 ACL: over SMB
        // and NFSv4 that is a skip, not the client's uid/gid/mode.
        let smb = capture(&root, "f", Flavour::Smb, &st, &ids).unwrap_err();
        assert!(smb.contains("NT security descriptor"), "{smb}");
        let v4 = capture(&root, "f", Flavour::Nfs4, &st, &ids).unwrap_err();
        assert!(v4.contains("NFSv4 ACL"), "{v4}");
        // NFSv3: the mode is the server's own.
        assert_eq!(
            capture(&root, "f", Flavour::Nfs3, &st, &ids).unwrap()["kind"],
            "posix"
        );
        // An NFSv4 ACL that doesn't parse: refused, not dropped.
        assert!(nfs4_acl(&[0, 0, 0, 2, 0, 0], &st, &ids).is_err());
    }

    #[test]
    fn nfs4_aces_keep_their_order_and_their_denies() {
        let t = tempfile::tempdir().unwrap();
        std::fs::write(t.path().join("f"), b"x").unwrap();
        let st = stat_of(&t.path().join("f"));
        let ids = omnifile_identity::fake::FakeIdMap::default();
        let raw = xdr(&[
            (ACE4_ACCESS_DENIED, ACE4_IDENTIFIER_GROUP, 1, "GROUP@"),
            (ACE4_ACCESS_ALLOWED, 0, 1, "EVERYONE@"),
            (2, 0, 1, "EVERYONE@"), // audit: not an access ACE
        ]);
        let v = nfs4_acl(&raw, &st, &ids).unwrap();
        let aces = v["aces"].as_array().unwrap();
        assert_eq!(aces.len(), 2);
        assert_eq!(
            aces[0]["type"], "deny",
            "the deny comes first, as on the NAS"
        );
        assert_eq!(aces[1]["type"], "allow");
        assert_eq!(aces[1]["sid"], "S-1-1-0");
    }

    #[test]
    fn owner_and_group_come_from_the_nt_descriptor_header() {
        fn sid(auth: u8, subs: &[u32]) -> Vec<u8> {
            let mut b = vec![1, subs.len() as u8, 0, 0, 0, 0, 0, auth];
            for s in subs {
                b.extend(s.to_le_bytes());
            }
            b
        }
        let o = sid(5, &[21, 111, 222, 333, 1105]);
        let g = sid(5, &[21, 111, 222, 333, 2001]);
        let mut sd = vec![1, 0, 0, 0x80];
        sd.extend(20u32.to_le_bytes());
        sd.extend((20 + o.len() as u32).to_le_bytes());
        sd.extend(0u32.to_le_bytes());
        sd.extend(0u32.to_le_bytes());
        sd.extend(&o);
        sd.extend(&g);
        let v = json!({"kind": "ntfs_sd", "raw": b64(&sd)});
        assert_eq!(
            owner_group(&v),
            (
                Some("S-1-5-21-111-222-333-1105".into()),
                Some("S-1-5-21-111-222-333-2001".into())
            )
        );
        let p = json!({"kind": "posix", "owner_sid": "S-1-5-21-9-1", "group_sid": "S-1-5-21-9-2"});
        assert_eq!(
            owner_group(&p),
            (Some("S-1-5-21-9-1".into()), Some("S-1-5-21-9-2".into()))
        );
    }
}
