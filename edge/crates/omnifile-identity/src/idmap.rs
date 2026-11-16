//! Unix ids ↔ AD SIDs through winbind (`idmap_ad`, RFC2307 attributes).

use std::sync::Arc;
use std::time::Duration;

use crate::cache::TtlCache;
use crate::cmd::{CmdError, run_bounded};
use crate::sid::Sid;

#[derive(Debug, thiserror::Error, Clone, PartialEq, Eq)]
pub enum IdMapError {
    /// winbind is not running or cannot reach a DC. Never cached.
    #[error("winbind unavailable: {0}")]
    Unavailable(String),
    /// Output we could not parse. Never cached.
    #[error("unexpected winbind output: {0}")]
    BadOutput(String),
}

/// What kind of account a name or SID is (`wbinfo -n` type numbers,
/// `enum lsa_SidType`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SidKind {
    User,
    Group,
    Domain,
    Alias,
    WellKnownGroup,
    Other(u32),
}

impl SidKind {
    fn from_num(n: u32) -> SidKind {
        match n {
            1 => SidKind::User,
            2 => SidKind::Group,
            3 => SidKind::Domain,
            4 => SidKind::Alias,
            5 => SidKind::WellKnownGroup,
            n => SidKind::Other(n),
        }
    }
}

/// The lookups the edge needs. `Ok(None)` means "AD has no such mapping" (an
/// answer, cacheable); `Err` means "could not ask" (not cacheable).
pub trait IdMap: Send + Sync {
    fn uid_to_sid(&self, uid: u32) -> Result<Option<Sid>, IdMapError>;
    fn gid_to_sid(&self, gid: u32) -> Result<Option<Sid>, IdMapError>;
    fn sid_to_uid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError>;
    fn sid_to_gid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError>;
    /// `DOMAIN\name`, `name@realm` or a bare name (default domain).
    fn name_to_sid(&self, name: &str) -> Result<Option<(Sid, SidKind)>, IdMapError>;
    /// `DOMAIN\name` for a SID.
    fn sid_to_name(&self, sid: &Sid) -> Result<Option<String>, IdMapError>;
    /// A user's token SIDs: the user, its primary group and every group it is
    /// in, nested groups resolved (`wbinfo --user-sids`).
    fn user_token_sids(&self, user: &Sid) -> Result<Option<Vec<Sid>>, IdMapError>;
}

impl<T: IdMap + ?Sized> IdMap for Arc<T> {
    fn uid_to_sid(&self, uid: u32) -> Result<Option<Sid>, IdMapError> {
        (**self).uid_to_sid(uid)
    }
    fn gid_to_sid(&self, gid: u32) -> Result<Option<Sid>, IdMapError> {
        (**self).gid_to_sid(gid)
    }
    fn sid_to_uid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
        (**self).sid_to_uid(sid)
    }
    fn sid_to_gid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
        (**self).sid_to_gid(sid)
    }
    fn name_to_sid(&self, name: &str) -> Result<Option<(Sid, SidKind)>, IdMapError> {
        (**self).name_to_sid(name)
    }
    fn sid_to_name(&self, sid: &Sid) -> Result<Option<String>, IdMapError> {
        (**self).sid_to_name(sid)
    }
    fn user_token_sids(&self, user: &Sid) -> Result<Option<Vec<Sid>>, IdMapError> {
        (**self).user_token_sids(user)
    }
}

/// winbind through the `wbinfo` program (GPL-3.0, run as a separate process).
#[derive(Clone, Debug)]
pub struct Wbinfo {
    pub program: String,
    pub timeout: Duration,
}

impl Default for Wbinfo {
    fn default() -> Self {
        Wbinfo {
            program: "wbinfo".into(),
            timeout: Duration::from_secs(5),
        }
    }
}

/// Splits a failed `wbinfo` run into "no mapping" and "could not ask".
///
/// wbinfo exits 1 for both. Its `failed to call wbcXxx: WBC_ERR_…` line tells
/// them apart: `WBC_ERR_WINBIND_NOT_AVAILABLE` (daemon down) and
/// `WBC_ERR_*_NOT_AVAILABLE`/`NT_STATUS_*` connection errors are
/// [`IdMapError::Unavailable`]; `DOMAIN_NOT_FOUND`, `NONE_MAPPED`,
/// `NO_SUCH_*` and `INVALID_SID` are a negative answer.
fn classify(e: CmdError) -> Result<(), IdMapError> {
    match e {
        CmdError::Failed { stderr, code, .. } => {
            let s = stderr.to_ascii_uppercase();
            let negative = [
                "DOMAIN_NOT_FOUND",
                "NONE_MAPPED",
                "NO_SUCH_USER",
                "NO_SUCH_GROUP",
                "NO_SUCH",
                "INVALID_SID",
                "NOT_MAPPED",
                "COULD NOT CONVERT",
                "COULD NOT LOOKUP",
                "UNKNOWN_USER",
                "NOT_FOUND",
                "WBC_ERR_INVALID_PARAM",
            ];
            let unavailable = [
                "NOT_AVAILABLE",
                "NT_STATUS_IO_TIMEOUT",
                "NT_STATUS_NO_LOGON_SERVERS",
                "NT_STATUS_CONNECTION",
                "NT_STATUS_PIPE",
                "UNABLE TO CONNECT",
                "WBC_ERR_NO_MEMORY",
                "WBC_ERR_UNKNOWN_FAILURE",
            ];
            if unavailable.iter().any(|p| s.contains(p)) {
                Err(IdMapError::Unavailable(stderr))
            } else if code == Some(1) && negative.iter().any(|p| s.contains(p)) {
                Ok(())
            } else {
                Err(IdMapError::Unavailable(format!("exit {code:?}: {stderr}")))
            }
        }
        other => Err(IdMapError::Unavailable(other.to_string())),
    }
}

/// Parses one SID per line (`wbinfo -U`, `-G`, `--user-sids`).
pub(crate) fn parse_sid_lines(out: &str) -> Result<Vec<Sid>, IdMapError> {
    out.lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .map(|l| Sid::parse(l).map_err(|_| IdMapError::BadOutput(l.to_string())))
        .collect()
}

/// `wbinfo -n NAME`: `S-1-5-21-…-1113 SID_USER (1)`.
pub(crate) fn parse_name_to_sid(out: &str) -> Result<(Sid, SidKind), IdMapError> {
    let line = out.lines().next().unwrap_or("").trim();
    let bad = || IdMapError::BadOutput(line.to_string());
    let (sid, rest) = line.split_once(' ').ok_or_else(bad)?;
    let sid = Sid::parse(sid).map_err(|_| bad())?;
    let n = rest
        .rsplit_once('(')
        .and_then(|(_, n)| n.trim_end_matches(')').trim().parse::<u32>().ok())
        .ok_or_else(bad)?;
    Ok((sid, SidKind::from_num(n)))
}

/// `wbinfo -s SID`: `OMNI\geo-nested 1`.
pub(crate) fn parse_sid_to_name(out: &str) -> Result<String, IdMapError> {
    let line = out.lines().next().unwrap_or("").trim_end();
    match line.rsplit_once(' ') {
        Some((name, n)) if !name.is_empty() && n.parse::<u32>().is_ok() => Ok(name.to_string()),
        _ => Err(IdMapError::BadOutput(line.to_string())),
    }
}

fn parse_id(out: &str) -> Result<u32, IdMapError> {
    let l = out.trim();
    l.parse::<u32>()
        .map_err(|_| IdMapError::BadOutput(l.to_string()))
}

impl Wbinfo {
    fn run(&self, args: &[&str]) -> Result<Option<String>, IdMapError> {
        match run_bounded(&self.program, args, self.timeout) {
            Ok(s) => Ok(Some(s)),
            Err(e) => classify(e).map(|()| None),
        }
    }
}

impl IdMap for Wbinfo {
    fn uid_to_sid(&self, uid: u32) -> Result<Option<Sid>, IdMapError> {
        let Some(out) = self.run(&["--uid-to-sid", &uid.to_string()])? else {
            return Ok(None);
        };
        Ok(parse_sid_lines(&out)?.into_iter().next())
    }

    fn gid_to_sid(&self, gid: u32) -> Result<Option<Sid>, IdMapError> {
        let Some(out) = self.run(&["--gid-to-sid", &gid.to_string()])? else {
            return Ok(None);
        };
        Ok(parse_sid_lines(&out)?.into_iter().next())
    }

    fn sid_to_uid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
        self.run(&["--sid-to-uid", sid.as_str()])?
            .map(|o| parse_id(&o))
            .transpose()
    }

    fn sid_to_gid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
        self.run(&["--sid-to-gid", sid.as_str()])?
            .map(|o| parse_id(&o))
            .transpose()
    }

    fn name_to_sid(&self, name: &str) -> Result<Option<(Sid, SidKind)>, IdMapError> {
        if name.is_empty() || name.starts_with('-') {
            return Ok(None);
        }
        self.run(&["--name-to-sid", name])?
            .map(|o| parse_name_to_sid(&o))
            .transpose()
    }

    fn sid_to_name(&self, sid: &Sid) -> Result<Option<String>, IdMapError> {
        self.run(&["--sid-to-name", sid.as_str()])?
            .map(|o| parse_sid_to_name(&o))
            .transpose()
    }

    fn user_token_sids(&self, user: &Sid) -> Result<Option<Vec<Sid>>, IdMapError> {
        let arg = format!("--user-sids={user}");
        self.run(&[&arg])?.map(|o| parse_sid_lines(&o)).transpose()
    }
}

/// Caches answers (including "no mapping") for `ttl`, and negative answers for
/// `negative_ttl`. Errors are never cached.
///
/// The TTL bounds how long a change in AD takes to reach the edge's *reports*
/// (audit, recall requester, ACL snapshots). It does not affect enforcement:
/// Samba and Ganesha resolve group membership themselves (winbind cache time,
/// `Manage_Gids_Expiration`; see `edge/config/README.md`).
pub struct CachedIdMap<M> {
    inner: M,
    uid: TtlCache<u32, Option<Sid>>,
    gid: TtlCache<u32, Option<Sid>>,
    sid_uid: TtlCache<Sid, Option<u32>>,
    sid_gid: TtlCache<Sid, Option<u32>>,
    names: TtlCache<String, Option<(Sid, SidKind)>>,
    sid_names: TtlCache<Sid, Option<String>>,
    tokens: TtlCache<Sid, Option<Vec<Sid>>>,
    neg: TtlCache<String, ()>,
}

impl<M: IdMap> CachedIdMap<M> {
    pub fn new(inner: M, ttl: Duration, negative_ttl: Duration) -> Self {
        const CAP: usize = 100_000;
        CachedIdMap {
            inner,
            uid: TtlCache::new(ttl, CAP),
            gid: TtlCache::new(ttl, CAP),
            sid_uid: TtlCache::new(ttl, CAP),
            sid_gid: TtlCache::new(ttl, CAP),
            names: TtlCache::new(ttl, CAP),
            sid_names: TtlCache::new(ttl, CAP),
            tokens: TtlCache::new(ttl, 10_000),
            neg: TtlCache::new(negative_ttl, CAP),
        }
    }

    pub fn inner(&self) -> &M {
        &self.inner
    }

    /// Forget everything (for example after an AD group change the admin
    /// wants reflected immediately).
    pub fn flush(&self) {
        self.uid.clear();
        self.gid.clear();
        self.sid_uid.clear();
        self.sid_gid.clear();
        self.names.clear();
        self.sid_names.clear();
        self.tokens.clear();
        self.neg.clear();
    }
}

fn cached<K, V>(
    cache: &TtlCache<K, Option<V>>,
    neg: &TtlCache<String, ()>,
    neg_key: String,
    k: K,
    f: impl FnOnce() -> Result<Option<V>, IdMapError>,
) -> Result<Option<V>, IdMapError>
where
    K: Eq + std::hash::Hash + Clone,
    V: Clone,
{
    if let Some(v) = cache.get(&k) {
        return Ok(v);
    }
    if neg.get(&neg_key).is_some() {
        return Ok(None);
    }
    let v = f()?;
    match &v {
        Some(_) => cache.put(k, v.clone()),
        None => neg.put(neg_key, ()),
    }
    Ok(v)
}

impl<M: IdMap> IdMap for CachedIdMap<M> {
    fn uid_to_sid(&self, uid: u32) -> Result<Option<Sid>, IdMapError> {
        cached(&self.uid, &self.neg, format!("u{uid}"), uid, || {
            self.inner.uid_to_sid(uid)
        })
    }
    fn gid_to_sid(&self, gid: u32) -> Result<Option<Sid>, IdMapError> {
        cached(&self.gid, &self.neg, format!("g{gid}"), gid, || {
            self.inner.gid_to_sid(gid)
        })
    }
    fn sid_to_uid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
        cached(
            &self.sid_uid,
            &self.neg,
            format!("su{sid}"),
            sid.clone(),
            || self.inner.sid_to_uid(sid),
        )
    }
    fn sid_to_gid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
        cached(
            &self.sid_gid,
            &self.neg,
            format!("sg{sid}"),
            sid.clone(),
            || self.inner.sid_to_gid(sid),
        )
    }
    fn name_to_sid(&self, name: &str) -> Result<Option<(Sid, SidKind)>, IdMapError> {
        let k = name.to_lowercase();
        cached(&self.names, &self.neg, format!("n{k}"), k.clone(), || {
            self.inner.name_to_sid(name)
        })
    }
    fn sid_to_name(&self, sid: &Sid) -> Result<Option<String>, IdMapError> {
        cached(
            &self.sid_names,
            &self.neg,
            format!("sn{sid}"),
            sid.clone(),
            || self.inner.sid_to_name(sid),
        )
    }
    fn user_token_sids(&self, user: &Sid) -> Result<Option<Vec<Sid>>, IdMapError> {
        cached(
            &self.tokens,
            &self.neg,
            format!("t{user}"),
            user.clone(),
            || self.inner.user_token_sids(user),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fake::FakeIdMap;
    use std::sync::atomic::{AtomicUsize, Ordering};

    // Real output from the dev AD (omnifile-edge, Samba 4.23.6), 2026-10-05.
    const SID_READER: &str = "S-1-5-21-3190051465-787089565-3716134449-1108";

    #[test]
    fn parses_real_wbinfo_output() {
        assert_eq!(
            parse_sid_lines(&format!("{SID_READER}\n")).unwrap(),
            vec![Sid::parse(SID_READER).unwrap()]
        );
        let (sid, kind) =
            parse_name_to_sid("S-1-5-21-3190051465-787089565-3716134449-1113 SID_USER (1)\n")
                .unwrap();
        assert_eq!(
            sid.as_str(),
            "S-1-5-21-3190051465-787089565-3716134449-1113"
        );
        assert_eq!(kind, SidKind::User);
        let (_, kind) = parse_name_to_sid("S-1-5-21-1-2-3-1103 SID_DOM_GROUP (2)").unwrap();
        assert_eq!(kind, SidKind::Group);
        assert_eq!(
            parse_sid_to_name("OMNI\\geo-nested 1\n").unwrap(),
            "OMNI\\geo-nested"
        );
        assert_eq!(
            parse_sid_to_name("OMNI\\domain users 2\n").unwrap(),
            "OMNI\\domain users"
        );
        let token = "S-1-5-21-3190051465-787089565-3716134449-1113\n\
                     S-1-5-21-3190051465-787089565-3716134449-513\n\
                     S-1-5-21-3190051465-787089565-3716134449-1107\n\
                     S-1-5-21-3190051465-787089565-3716134449-1103\n";
        assert_eq!(parse_sid_lines(token).unwrap().len(), 4);
        assert_eq!(parse_id("20001\n").unwrap(), 20001);
        assert!(parse_id("x").is_err());
        assert!(parse_sid_lines("garbage").is_err());
        assert!(parse_name_to_sid("S-1-5-21-1-2 SID_USER").is_err());
    }

    #[test]
    fn classifies_failures() {
        let f = |stderr: &str, code| CmdError::Failed {
            prog: "wbinfo".into(),
            code,
            stderr: stderr.into(),
        };
        // Real: `wbinfo -U 99999` on the dev edge.
        assert_eq!(
            classify(f(
                "failed to call wbcUidToSid: WBC_ERR_DOMAIN_NOT_FOUND\nCould not convert uid 99999 to sid",
                Some(1)
            )),
            Ok(())
        );
        assert!(matches!(
            classify(f(
                "failed to call wbcPingDc2: WBC_ERR_WINBIND_NOT_AVAILABLE",
                Some(1)
            )),
            Err(IdMapError::Unavailable(_))
        ));
        assert!(matches!(
            classify(f("", None)),
            Err(IdMapError::Unavailable(_))
        ));
        assert!(matches!(
            classify(CmdError::Timeout {
                prog: "wbinfo".into(),
                timeout: Duration::from_secs(1)
            }),
            Err(IdMapError::Unavailable(_))
        ));
    }

    struct Counting {
        inner: FakeIdMap,
        calls: AtomicUsize,
        fail: std::sync::atomic::AtomicBool,
    }
    impl IdMap for Counting {
        fn uid_to_sid(&self, uid: u32) -> Result<Option<Sid>, IdMapError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if self.fail.load(Ordering::SeqCst) {
                return Err(IdMapError::Unavailable("down".into()));
            }
            self.inner.uid_to_sid(uid)
        }
        fn gid_to_sid(&self, gid: u32) -> Result<Option<Sid>, IdMapError> {
            self.inner.gid_to_sid(gid)
        }
        fn sid_to_uid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
            self.inner.sid_to_uid(sid)
        }
        fn sid_to_gid(&self, sid: &Sid) -> Result<Option<u32>, IdMapError> {
            self.inner.sid_to_gid(sid)
        }
        fn name_to_sid(&self, n: &str) -> Result<Option<(Sid, SidKind)>, IdMapError> {
            self.inner.name_to_sid(n)
        }
        fn sid_to_name(&self, sid: &Sid) -> Result<Option<String>, IdMapError> {
            self.inner.sid_to_name(sid)
        }
        fn user_token_sids(&self, u: &Sid) -> Result<Option<Vec<Sid>>, IdMapError> {
            self.inner.user_token_sids(u)
        }
    }

    #[test]
    fn caches_answers_not_errors() {
        let inner = Counting {
            inner: FakeIdMap::dev_ad(),
            calls: AtomicUsize::new(0),
            fail: false.into(),
        };
        let c = CachedIdMap::new(inner, Duration::from_secs(60), Duration::from_secs(60));
        assert_eq!(
            c.uid_to_sid(20001).unwrap().unwrap().as_str(),
            FakeIdMap::DEV_READER
        );
        assert!(c.uid_to_sid(20001).unwrap().is_some());
        assert_eq!(c.uid_to_sid(99999).unwrap(), None);
        assert_eq!(c.uid_to_sid(99999).unwrap(), None);
        assert_eq!(c.inner().calls.load(Ordering::SeqCst), 2);

        c.inner().fail.store(true, Ordering::SeqCst);
        assert!(c.uid_to_sid(20002).is_err());
        assert!(c.uid_to_sid(20002).is_err());
        assert_eq!(
            c.inner().calls.load(Ordering::SeqCst),
            4,
            "errors not cached"
        );
        assert!(c.uid_to_sid(20001).unwrap().is_some(), "served from cache");
        c.flush();
        assert!(c.uid_to_sid(20001).is_err());
    }
}
