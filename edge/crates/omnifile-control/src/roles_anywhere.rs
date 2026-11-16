//! The edge's IAM Roles Anywhere certificate from OmniFile's edge CA
//! (decision 50; control API §2, `docs/design/setup-wizard.md` "Keys").
//!
//! The setup wizard's `aws` step seals the AWS key to the TPM and, in the
//! default mode, leaves its certificate request in [`DIR`]/`request.csr`.
//! The request is all the edge needs from then on: the service signs a
//! certificate for its key (`POST …/aws-certificate`, authenticated by the
//! mTLS certificate and a proof by its key), and the same request renews it,
//! so the AWS key is never opened outside `aws_signing_helper`'s unit.
//!
//! The directory is world-readable (certificates are public) and writable by
//! the daemon (`/var/lib/omnifile`), so `aws_signing_helper` (a dynamic user)
//! reads `cert.pem` and `chain.pem` there. A `request.csr` there means "OmniFile
//! issues this edge's certificate": the daemon requests one when there is
//! none, renews it with the mTLS certificate (the same heartbeat) or from its
//! `renew_after`, then restarts the helper. Without it (the client's own CA)
//! the daemon leaves Roles Anywhere alone.

use std::path::{Path, PathBuf};
use std::time::SystemTime;

use serde::{Deserialize, Serialize};

use crate::error::{ControlError, Result};
use crate::types::RolesAnywhereCertificate;
use crate::util::{parse_time, write_atomic};

/// Where the request, the certificate and its state live.
pub const DIR: &str = "/var/lib/omnifile/roles-anywhere";
pub const CSR_FILE: &str = "request.csr";
pub const CERT_FILE: &str = "cert.pem";
pub const CHAIN_FILE: &str = "chain.pem";
pub const STATE_FILE: &str = "issued.json";
/// Present from a certificate's install until the helper restarted onto it.
pub const RESTART_FILE: &str = "helper-restart-pending";

/// What was last installed, to know when to renew.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Installed {
    pub not_after: String,
    pub renew_after: String,
    /// The mTLS certificate's `not_after` when this one was issued: a new
    /// mTLS certificate renews this one too.
    pub mtls_not_after: String,
}

/// The directory of one edge's Roles Anywhere files.
#[derive(Clone, Debug)]
pub struct RolesAnywhereFiles {
    pub dir: PathBuf,
}

impl Default for RolesAnywhereFiles {
    fn default() -> Self {
        Self::new(DIR)
    }
}

impl RolesAnywhereFiles {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }

    pub fn path(&self, name: &str) -> PathBuf {
        self.dir.join(name)
    }

    /// OmniFile's edge CA issues this edge's certificate (the wizard's default).
    pub fn enabled(&self) -> bool {
        self.path(CSR_FILE).exists()
    }

    pub fn csr_pem(&self) -> Result<String> {
        Ok(std::fs::read_to_string(self.path(CSR_FILE))?)
    }

    pub fn installed(&self) -> Option<Installed> {
        let bytes = std::fs::read(self.path(STATE_FILE)).ok()?;
        if !self.path(CERT_FILE).exists() {
            return None;
        }
        serde_json::from_slice(&bytes).ok()
    }

    /// No certificate yet, past its `renew_after`, or the mTLS certificate
    /// was renewed since it was issued.
    pub fn due(&self, now: SystemTime, mtls_not_after: &str) -> bool {
        match self.installed() {
            None => true,
            Some(i) => {
                i.mtls_not_after != mtls_not_after
                    || parse_time(&i.renew_after).is_none_or(|t| now >= t)
            }
        }
    }

    /// A new certificate is installed and the signing helper hasn't been
    /// restarted onto it yet (review on #32): kept on disk, so a failed
    /// restart, or a daemon restart in between, is retried.
    pub fn restart_pending(&self) -> bool {
        self.path(RESTART_FILE).exists()
    }

    pub fn mark_restart_pending(&self) -> Result<()> {
        ensure_dir(&self.dir)?;
        write_atomic(&self.path(RESTART_FILE), b"", 0o644)
    }

    /// The helper restarted onto the installed certificate.
    pub fn restart_done(&self) -> Result<()> {
        match std::fs::remove_file(self.path(RESTART_FILE)) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e.into()),
            _ => Ok(()),
        }
    }

    /// Installs `cert` (checked against the request's key first): the
    /// helper-restart mark, then the chain, then the certificate, then the
    /// state, each written atomically, 0644. The mark comes first so that no
    /// installed certificate is ever left without one.
    pub fn install(&self, cert: &RolesAnywhereCertificate, mtls_not_after: &str) -> Result<()> {
        check_for_request(&self.csr_pem()?, &cert.cert_pem)?;
        self.mark_restart_pending()?;
        write_atomic(&self.path(CHAIN_FILE), cert.chain_pem.as_bytes(), 0o644)?;
        write_atomic(&self.path(CERT_FILE), cert.cert_pem.as_bytes(), 0o644)?;
        let state = Installed {
            not_after: cert.not_after.clone(),
            renew_after: cert.renew_after.clone(),
            mtls_not_after: mtls_not_after.to_owned(),
        };
        let json =
            serde_json::to_vec_pretty(&state).map_err(|e| ControlError::Invalid(e.to_string()))?;
        write_atomic(&self.path(STATE_FILE), &json, 0o644)
    }
}

fn ensure_dir(dir: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::create_dir_all(dir)?;
    std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o755))?;
    Ok(())
}

/// Refuses a certificate that isn't for the request's key: the request's
/// SubjectPublicKeyInfo must be the certificate's, byte for byte.
pub fn check_for_request(csr_pem: &str, cert_pem: &str) -> Result<()> {
    let bad = |what: &str| ControlError::Identity(format!("Roles Anywhere certificate: {what}"));
    let csr = crate::identity::pem_body(csr_pem).ok_or_else(|| bad("unreadable request"))?;
    let cert = crate::identity::pem_body(cert_pem).ok_or_else(|| bad("unreadable certificate"))?;
    let spki = csr_spki(&csr).ok_or_else(|| bad("the request does not parse"))?;
    if cert_spki(&cert) != Some(spki) {
        return Err(bad("it is not for this filer's AWS key"));
    }
    Ok(())
}

/// (tag, value range, end) of the DER element at `at`.
fn tlv(der: &[u8], at: usize) -> Option<(u8, std::ops::Range<usize>, usize)> {
    let tag = *der.get(at)?;
    let first = *der.get(at + 1)? as usize;
    let (len, start) = if first < 0x80 {
        (first, at + 2)
    } else {
        let n = first & 0x7f;
        if n == 0 || n > 4 {
            return None;
        }
        let bytes = der.get(at + 2..at + 2 + n)?;
        (
            bytes.iter().fold(0usize, |a, &b| (a << 8) | b as usize),
            at + 2 + n,
        )
    };
    let end = start.checked_add(len)?;
    (end <= der.len()).then_some((tag, start..end, end))
}

/// The `n`th element (0-based) inside the SEQUENCE whose value is `inner`.
fn nth(der: &[u8], inner: std::ops::Range<usize>, n: usize) -> Option<std::ops::Range<usize>> {
    let mut at = inner.start;
    for i in 0..=n {
        let (_, _, end) = tlv(der, at)?;
        if i == n {
            return (end <= inner.end).then_some(at..end);
        }
        at = end;
    }
    None
}

/// CertificationRequest { info { version, subject, subjectPKInfo, … }, … }.
fn csr_spki(der: &[u8]) -> Option<&[u8]> {
    let (_, outer, _) = tlv(der, 0)?;
    let info = nth(der, outer, 0)?;
    let (_, info_inner, _) = tlv(der, info.start)?;
    nth(der, info_inner, 2).map(|r| &der[r])
}

/// Certificate { tbs { [0] version, serial, sig, issuer, validity, subject, spki, … }, … }.
fn cert_spki(der: &[u8]) -> Option<&[u8]> {
    let (_, outer, _) = tlv(der, 0)?;
    let tbs = nth(der, outer, 0)?;
    let (_, tbs_inner, _) = tlv(der, tbs.start)?;
    // The version is explicit ([0]) in a v3 certificate.
    let skip = usize::from(der.get(tbs_inner.start) == Some(&0xa0));
    nth(der, tbs_inner, 5 + skip).map(|r| &der[r])
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn request(key: &rcgen::KeyPair) -> String {
        let mut p = rcgen::CertificateParams::new(Vec::<String>::new()).unwrap();
        p.distinguished_name
            .push(rcgen::DnType::CommonName, "omnifile-edge-host");
        p.serialize_request(key).unwrap().pem().unwrap()
    }

    fn certificate(key: &rcgen::KeyPair) -> String {
        let ca_key = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
        let mut ca = rcgen::CertificateParams::new(Vec::<String>::new()).unwrap();
        ca.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
        let issuer = rcgen::Issuer::from_params(&ca, &ca_key);
        let mut p = rcgen::CertificateParams::new(Vec::<String>::new()).unwrap();
        p.distinguished_name
            .push(rcgen::DnType::CommonName, "omnifile-edge-7");
        p.distinguished_name.push(
            rcgen::DnType::OrganizationalUnitName,
            "omnifile-roles-anywhere",
        );
        p.signed_by(key, &issuer).unwrap().pem()
    }

    #[test]
    fn a_certificate_must_be_for_the_requests_key() {
        let key = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
        let other = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
        let csr = request(&key);
        assert!(check_for_request(&csr, &certificate(&key)).is_ok());
        let e = check_for_request(&csr, &certificate(&other)).unwrap_err();
        assert!(
            e.to_string().contains("not for this filer's AWS key"),
            "{e}"
        );
        assert!(check_for_request(&csr, "junk").is_err());
        assert!(check_for_request("junk", &certificate(&key)).is_err());
    }

    #[test]
    fn installs_and_knows_when_to_renew() {
        let dir = tempfile::tempdir().unwrap();
        let files = RolesAnywhereFiles::new(dir.path().join("ra"));
        assert!(!files.enabled());
        let key = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
        std::fs::create_dir_all(&files.dir).unwrap();
        std::fs::write(files.path(CSR_FILE), request(&key)).unwrap();
        assert!(files.enabled());
        let now = SystemTime::now();
        assert!(
            files.due(now, "2026-11-20T00:00:00Z"),
            "nothing installed yet"
        );
        let renew = crate::util::format_time(now + Duration::from_secs(20 * 86400));
        let cert = RolesAnywhereCertificate {
            edge_id: 7,
            cert_pem: certificate(&key),
            chain_pem: String::new(),
            ca_pem: String::new(),
            not_after: crate::util::format_time(now + Duration::from_secs(30 * 86400)),
            renew_after: renew,
        };
        assert!(!files.restart_pending());
        files.install(&cert, "2026-11-20T00:00:00Z").unwrap();
        // The helper must restart onto it: marked until it has.
        assert!(files.restart_pending());
        files.restart_done().unwrap();
        assert!(!files.restart_pending());
        files.restart_done().unwrap();
        use std::os::unix::fs::PermissionsExt;
        for f in [CERT_FILE, CHAIN_FILE, STATE_FILE] {
            let mode = std::fs::metadata(files.path(f))
                .unwrap()
                .permissions()
                .mode();
            assert_eq!(mode & 0o777, 0o644, "{f}");
        }
        let mode = std::fs::metadata(&files.dir).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o755);
        assert!(!files.due(now, "2026-11-20T00:00:00Z"));
        assert!(files.due(
            now + Duration::from_secs(21 * 86400),
            "2026-11-20T00:00:00Z"
        ));
        assert!(
            files.due(now, "2026-12-10T00:00:00Z"),
            "the mTLS certificate was renewed"
        );
        // Another key's certificate is never installed.
        let other = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
        let wrong = RolesAnywhereCertificate {
            cert_pem: certificate(&other),
            ..cert.clone()
        };
        assert!(files.install(&wrong, "x").is_err());
        assert_eq!(
            std::fs::read_to_string(files.path(CERT_FILE)).unwrap(),
            cert.cert_pem
        );
    }
}
