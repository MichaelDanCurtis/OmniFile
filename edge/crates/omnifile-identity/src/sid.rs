//! Security identifiers in their string form (`S-1-5-21-…`).

use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

/// A validated SID string, as the control API carries it
/// (`^S-1-[0-9]+(-[0-9]+){1,15}$`).
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct Sid(String);

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
#[error("not a SID: {0:?}")]
pub struct SidError(pub String);

impl Sid {
    pub fn parse(s: &str) -> Result<Sid, SidError> {
        let s = s.trim();
        let err = || SidError(s.to_string());
        let rest = s.strip_prefix("S-1-").ok_or_else(err)?;
        let parts: Vec<&str> = rest.split('-').collect();
        // Authority plus 1..=15 sub-authorities.
        if parts.len() < 2 || parts.len() > 16 {
            return Err(err());
        }
        for p in &parts {
            if p.is_empty() || p.len() > 20 || !p.bytes().all(|b| b.is_ascii_digit()) {
                return Err(err());
            }
        }
        Ok(Sid(s.to_string()))
    }

    /// Samba's SID for a Unix uid that has no AD SID (`S-1-22-1-<uid>`), used
    /// for local accounts such as root so that every ACL entry still has a SID.
    pub fn unix_user(uid: u32) -> Sid {
        Sid(format!("S-1-22-1-{uid}"))
    }

    /// Samba's SID for a Unix gid with no AD SID (`S-1-22-2-<gid>`).
    pub fn unix_group(gid: u32) -> Sid {
        Sid(format!("S-1-22-2-{gid}"))
    }

    /// Is this one of the `S-1-22-*` Unix fallbacks rather than an AD SID?
    pub fn is_unix_fallback(&self) -> bool {
        self.0.starts_with("S-1-22-")
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for Sid {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl FromStr for Sid {
    type Err = SidError;
    fn from_str(s: &str) -> Result<Sid, SidError> {
        Sid::parse(s)
    }
}

impl TryFrom<String> for Sid {
    type Error = SidError;
    fn try_from(s: String) -> Result<Sid, SidError> {
        Sid::parse(&s)
    }
}

impl From<Sid> for String {
    fn from(s: Sid) -> String {
        s.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_ad_and_wellknown_sids() {
        for s in [
            "S-1-5-21-3190051465-787089565-3716134449-1108",
            "S-1-5-32-544",
            "S-1-1-0",
            "S-1-22-1-0",
        ] {
            assert_eq!(Sid::parse(s).unwrap().as_str(), s);
        }
    }

    #[test]
    fn rejects_garbage() {
        for s in [
            "",
            "S-1",
            "S-1-",
            "S-1-5-",
            "S-2-5-21",
            "s-1-5-21",
            "S-1-5-21-x",
            "S-1-5-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16",
            "S-1-5-21 1",
        ] {
            assert!(Sid::parse(s).is_err(), "{s:?} accepted");
        }
    }

    #[test]
    fn unix_fallbacks() {
        assert_eq!(Sid::unix_user(0).as_str(), "S-1-22-1-0");
        assert_eq!(Sid::unix_group(30001).as_str(), "S-1-22-2-30001");
        assert!(Sid::unix_user(7).is_unix_fallback());
        assert!(!Sid::parse("S-1-5-32-544").unwrap().is_unix_fallback());
    }

    #[test]
    fn serde_round_trip_and_validation() {
        let s: Sid = serde_json::from_str("\"S-1-5-32-544\"").unwrap();
        assert_eq!(serde_json::to_string(&s).unwrap(), "\"S-1-5-32-544\"");
        assert!(serde_json::from_str::<Sid>("\"nope\"").is_err());
    }
}
