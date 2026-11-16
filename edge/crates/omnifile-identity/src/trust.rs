//! Claimed vs proven identities, and share trust levels (access-control.md §3).

use serde::{Deserialize, Serialize};

use crate::sid::Sid;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Via {
    Nfs,
    Smb,
    /// A process on the edge itself (an admin shell, a backup agent) touched a
    /// guarded file directly. Not in the control API's `via` enum yet: the
    /// daemon reports these in audit records only.
    Local,
}

/// `requester.sec` in the control API (§4.4).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Sec {
    Sys,
    Krb5,
    Krb5i,
    Krb5p,
    Ntlm,
    Kerberos,
}

/// An NFS export security flavour (`share.upsert.nfs_sec`, Ganesha `SecType`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NfsSec {
    Sys,
    Krb5,
    Krb5i,
    Krb5p,
}

impl NfsSec {
    /// From the FSAL's identity record (`fsal-socket.md` §4.2: 1 sys, 2 krb5,
    /// 3 krb5i, 4 krb5p; anything else unknown).
    pub fn from_wire(sec: u8) -> Option<NfsSec> {
        match sec {
            1 => Some(NfsSec::Sys),
            2 => Some(NfsSec::Krb5),
            3 => Some(NfsSec::Krb5i),
            4 => Some(NfsSec::Krb5p),
            _ => None,
        }
    }

    pub fn is_kerberos(self) -> bool {
        !matches!(self, NfsSec::Sys)
    }

    pub fn ganesha_name(self) -> &'static str {
        match self {
            NfsSec::Sys => "sys",
            NfsSec::Krb5 => "krb5",
            NfsSec::Krb5i => "krb5i",
            NfsSec::Krb5p => "krb5p",
        }
    }

    pub fn as_sec(self) -> Sec {
        match self {
            NfsSec::Sys => Sec::Sys,
            NfsSec::Krb5 => Sec::Krb5,
            NfsSec::Krb5i => Sec::Krb5i,
            NfsSec::Krb5p => Sec::Krb5p,
        }
    }
}

/// `proof` (decision 28): `claimed` for NFS `sec=sys` (the client host asserts
/// the uid), `proven` for Kerberos and for SMB sessions authenticated by AD.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Proof {
    Claimed,
    Proven,
}

/// A share's trust level, shown in the portal (`share.upsert.trust_level`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TrustLevel {
    /// Every way in proves the user: NFS `krb5*` only, and/or SMB.
    Proven,
    /// NFS `sec=sys` is allowed: the uid is whatever an allowed host says.
    HostTrusted,
}

impl TrustLevel {
    /// The trust level a share actually has, from what it allows. A share that
    /// exports NFS with `sys` among its flavours is host-trusted, whatever SMB
    /// does, because the weakest way in decides.
    pub fn of(nfs_enabled: bool, nfs_sec: &[NfsSec]) -> TrustLevel {
        if nfs_enabled && (nfs_sec.is_empty() || nfs_sec.contains(&NfsSec::Sys)) {
            TrustLevel::HostTrusted
        } else {
            TrustLevel::Proven
        }
    }
}

/// The control API's `requester` object (§4.4) and the identity half of an
/// audit record (§4.7).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Requester {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub uid: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sid: Option<Sid>,
    pub via: Via,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sec: Option<Sec>,
    pub proof: Proof,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_addr: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trust_level_is_the_weakest_way_in() {
        use NfsSec::*;
        assert_eq!(TrustLevel::of(true, &[Sys]), TrustLevel::HostTrusted);
        assert_eq!(TrustLevel::of(true, &[Krb5, Sys]), TrustLevel::HostTrusted);
        assert_eq!(TrustLevel::of(true, &[]), TrustLevel::HostTrusted);
        assert_eq!(TrustLevel::of(true, &[Krb5, Krb5p]), TrustLevel::Proven);
        assert_eq!(TrustLevel::of(false, &[Sys]), TrustLevel::Proven);
    }

    #[test]
    fn wire_sec() {
        assert_eq!(NfsSec::from_wire(1), Some(NfsSec::Sys));
        assert_eq!(NfsSec::from_wire(4), Some(NfsSec::Krb5p));
        assert_eq!(NfsSec::from_wire(0), None);
        assert!(NfsSec::Krb5i.is_kerberos());
        assert!(!NfsSec::Sys.is_kerberos());
    }

    #[test]
    fn requester_matches_the_contract_example() {
        let r = Requester {
            uid: Some(41023),
            sid: Some(Sid::parse("S-1-5-21-1-2-3-1108").unwrap()),
            via: Via::Nfs,
            sec: Some(Sec::Sys),
            proof: Proof::Claimed,
            client_addr: Some("10.1.2.3".into()),
        };
        assert_eq!(
            serde_json::to_string(&r).unwrap(),
            r#"{"uid":41023,"sid":"S-1-5-21-1-2-3-1108","via":"nfs","sec":"sys","proof":"claimed","client_addr":"10.1.2.3"}"#
        );
        assert_eq!(
            serde_json::to_string(&TrustLevel::HostTrusted).unwrap(),
            "\"host_trusted\""
        );
    }
}
