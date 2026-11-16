//! The IAM Roles Anywhere key, its certificate request and its certificate
//! (decision 43; docs/design/setup-wizard.md, "Keys").
//!
//! The private key is made in memory and handed to `systemd-creds encrypt`
//! on stdin, which seals it to the TPM 2.0 (and the host key), exactly as the
//! edge's mTLS key is. It is never written in the clear, and never on a
//! command line. `omnifile-aws-signing-helper.service` opens it for itself
//! only (`LoadCredentialEncrypted=`).
//!
//! Who signs the request (decision 50):
//! - by default OmniFile's edge CA, which the client registered as the Roles
//!   Anywhere trust anchor: the request goes to [`RA_DIR`], and
//!   `omnifile-edge roles-anywhere-cert` gets the certificate from the
//!   service over mTLS; the daemon renews it with its mTLS certificate;
//! - with `issuer = "client"`, the CA the operator registered (AWS Private
//!   CA, or their own): the certificate it issues is checked against the
//!   key's public half before it is installed (`omnifile-setup aws-cert`).

use std::time::{SystemTime, UNIX_EPOCH};

use x509_parser::prelude::*;
use zeroize::Zeroizing;

use crate::answers::Aws;
use crate::render::OWNED;

/// The sealed private key (credential name [`KEY_NAME`]).
pub const KEY_CRED: &str = "/var/lib/omnifile/identity/roles-anywhere.cred";
/// The name bound into the credential: the helper's `LoadCredentialEncrypted=` ID.
pub const KEY_NAME: &str = "roles-anywhere-key";
/// The certificate request, for the trust anchor's CA.
pub const CSR_FILE: &str = "/etc/omnifile/roles-anywhere.csr";
/// The key's public half (SubjectPublicKeyInfo), to check certificates against.
pub const PUB_FILE: &str = "/etc/omnifile/roles-anywhere.pub";
/// The certificate the helper presents.
pub const CERT_FILE: &str = "/etc/omnifile/roles-anywhere.pem";
/// Intermediates between it and the trust anchor, if any.
pub const CHAIN_FILE: &str = "/etc/omnifile/roles-anywhere-chain.pem";
/// The helper's settings (no secrets).
pub const ENV_FILE: &str = "/etc/omnifile/aws-signing-helper.env";
pub const HELPER_UNIT: &str = "omnifile-aws-signing-helper.service";
/// Where the helper serves credentials (omnifile-blob's `[s3] imds_url`).
pub const HELPER_PORT: u16 = 9911;

/// Decision 50, the default: OmniFile's edge CA issues the certificate. The
/// daemon reads the request here and writes the certificate here
/// (`omnifile_control::roles_anywhere::DIR`, the daemon's `[s3]
/// roles_anywhere_dir`): a directory the daemon may write and the helper's
/// dynamic user may read. A request here is what tells the daemon to renew.
pub const RA_DIR: &str = "/var/lib/omnifile/roles-anywhere";
pub const RA_CSR: &str = "/var/lib/omnifile/roles-anywhere/request.csr";
pub const RA_CERT: &str = "/var/lib/omnifile/roles-anywhere/cert.pem";
pub const RA_CHAIN: &str = "/var/lib/omnifile/roles-anywhere/chain.pem";
/// The daemon's record of the installed certificate, and its helper-restart mark.
pub const RA_STATE: &str = "/var/lib/omnifile/roles-anywhere/issued.json";
pub const RA_RESTART: &str = "/var/lib/omnifile/roles-anywhere/helper-restart-pending";

/// A new key and its request.
pub struct NewKey {
    /// The private key (PKCS#8 PEM), wiped when dropped. Only for sealing.
    pub key_pem: Zeroizing<String>,
    pub csr_pem: String,
    pub public_pem: String,
}

/// Makes an ECDSA P-256 key and a request naming `common_name` (and `uri`,
/// as a URI SAN, when given).
pub fn new_key(common_name: &str, uri: Option<&str>) -> Result<NewKey, String> {
    let key = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256)
        .map_err(|e| format!("key generation: {e}"))?;
    let mut params = rcgen::CertificateParams::new(Vec::<String>::new())
        .map_err(|e| format!("certificate request: {e}"))?;
    params
        .distinguished_name
        .push(rcgen::DnType::CommonName, common_name);
    if let Some(u) = uri {
        params.subject_alt_names.push(rcgen::SanType::URI(
            u.try_into().map_err(|e| format!("{u}: {e}"))?,
        ));
    }
    // Roles Anywhere signs with the key: digitalSignature, no CA.
    params.key_usages = vec![rcgen::KeyUsagePurpose::DigitalSignature];
    params.is_ca = rcgen::IsCa::ExplicitNoCa;
    let csr_pem = params
        .serialize_request(&key)
        .and_then(|c| c.pem())
        .map_err(|e| format!("certificate request: {e}"))?;
    Ok(NewKey {
        key_pem: Zeroizing::new(key.serialize_pem()),
        csr_pem,
        public_pem: key.public_key_pem(),
    })
}

/// The command that seals a key read from stdin into [`KEY_CRED`]. No PCRs
/// and no signed PCR policy, as for the mTLS key (`omnifile_control::seal`),
/// so a firmware update doesn't lock it out.
pub fn seal_argv() -> Vec<&'static str> {
    vec![
        "systemd-creds",
        "encrypt",
        "--name=roles-anywhere-key",
        "--with-key=auto",
        "--tpm2-pcrs=",
        "--tpm2-public-key=",
        "-",
        KEY_CRED,
    ]
}

/// A certificate that passed the checks.
#[derive(Debug)]
pub struct CheckedCert {
    pub leaf_pem: String,
    /// Intermediates, PEM, possibly empty.
    pub chain_pem: String,
    pub subject: String,
    pub not_after: String,
}

fn der_of(pem_text: &str, label: &str) -> Vec<Vec<u8>> {
    x509_parser::pem::Pem::iter_from_buffer(pem_text.as_bytes())
        .filter_map(Result::ok)
        .filter(|p| p.label == label)
        .map(|p| p.contents)
        .collect()
}

fn pem_of(label: &str, der: &[u8]) -> String {
    const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut b = String::new();
    for chunk in der.chunks(3) {
        let n = chunk.iter().fold(0u32, |a, &x| (a << 8) | x as u32) << (8 * (3 - chunk.len()));
        for i in 0..4 {
            if i <= chunk.len() {
                b.push(B64[((n >> (18 - 6 * i)) & 63) as usize] as char);
            } else {
                b.push('=');
            }
        }
    }
    let mut out = format!("-----BEGIN {label}-----\n");
    for line in b.as_bytes().chunks(64) {
        out.push_str(std::str::from_utf8(line).unwrap());
        out.push('\n');
    }
    out.push_str(&format!("-----END {label}-----\n"));
    out
}

/// Checks a certificate (PEM, the leaf first, then any intermediates) for
/// the key whose public half is `public_pem`: the same key, valid at `now`,
/// not a CA, usable for signatures, and signed with SHA-256 or better (what
/// Roles Anywhere accepts).
pub fn check_certificate(
    text: &str,
    public_pem: &str,
    now: SystemTime,
) -> Result<CheckedCert, String> {
    let certs = der_of(text, "CERTIFICATE");
    let Some(leaf_der) = certs.first() else {
        return Err("no certificate in it (PEM, -----BEGIN CERTIFICATE-----)".into());
    };
    let (_, leaf) = X509Certificate::from_der(leaf_der)
        .map_err(|e| format!("the certificate does not parse: {e}"))?;
    let ours = der_of(public_pem, "PUBLIC KEY");
    if ours.first().map(Vec::as_slice) != Some(leaf.public_key().raw) {
        return Err(
            "it is for another key: this filer's request is in /etc/omnifile/roles-anywhere.csr"
                .into(),
        );
    }
    let secs = now
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let at = ASN1Time::from_timestamp(secs).map_err(|e| e.to_string())?;
    let v = leaf.validity();
    if at < v.not_before {
        return Err(format!("it is not valid until {}", v.not_before));
    }
    if at > v.not_after {
        return Err(format!("it expired on {}", v.not_after));
    }
    if let Ok(Some(bc)) = leaf.basic_constraints()
        && bc.value.ca
    {
        return Err("it is a CA certificate, not this filer's".into());
    }
    if let Ok(Some(ku)) = leaf.key_usage()
        && !ku.value.digital_signature()
    {
        return Err("its key usage does not allow signatures (digitalSignature)".into());
    }
    if !signature_allowed(&leaf.signature_algorithm) {
        let alg = leaf.signature_algorithm.algorithm.to_id_string();
        return Err(format!(
            "it is signed with {alg}, which is not RSA (PKCS#1 v1.5 or PSS), ECDSA or Ed25519 with \
             SHA-256 or better; Roles Anywhere needs one of those"
        ));
    }
    for (i, d) in certs.iter().enumerate().skip(1) {
        X509Certificate::from_der(d)
            .map_err(|e| format!("certificate {} in the file does not parse: {e}", i + 1))?;
    }
    Ok(CheckedCert {
        leaf_pem: pem_of("CERTIFICATE", leaf_der),
        chain_pem: certs[1..]
            .iter()
            .map(|d| pem_of("CERTIFICATE", d))
            .collect(),
        subject: leaf.subject().to_string(),
        not_after: v.not_after.to_string(),
    })
}

/// Signature algorithms a certificate may be signed with: an allowlist (Codex
/// on #28), so SHA-224, SHA-1, MD2/MD5, DSA and anything unknown are refused.
const SHA2_256_PLUS: [&str; 3] = [
    "2.16.840.1.101.3.4.2.1", // SHA-256
    "2.16.840.1.101.3.4.2.2", // SHA-384
    "2.16.840.1.101.3.4.2.3", // SHA-512
];
const ALLOWED_SIGNATURES: [&str; 7] = [
    "1.2.840.113549.1.1.11", // sha256WithRSAEncryption
    "1.2.840.113549.1.1.12", // sha384WithRSAEncryption
    "1.2.840.113549.1.1.13", // sha512WithRSAEncryption
    "1.2.840.10045.4.3.2",   // ecdsa-with-SHA256
    "1.2.840.10045.4.3.3",   // ecdsa-with-SHA384
    "1.2.840.10045.4.3.4",   // ecdsa-with-SHA512
    "1.3.101.112",           // Ed25519
];
const RSASSA_PSS: &str = "1.2.840.113549.1.1.10";

/// Whether `alg` is RSA (PKCS#1 v1.5, or PSS with a SHA-256-or-better hash and
/// MGF1 hash), ECDSA or Ed25519, with SHA-256 or better.
pub fn signature_allowed(alg: &AlgorithmIdentifier) -> bool {
    let oid = alg.algorithm.to_id_string();
    if ALLOWED_SIGNATURES.contains(&oid.as_str()) {
        return true;
    }
    if oid != RSASSA_PSS {
        return false;
    }
    // PSS: the hash defaults to SHA-1 when absent (RFC 4055), so it must be given.
    match x509_parser::signature_algorithm::SignatureAlgorithm::try_from(alg) {
        Ok(x509_parser::signature_algorithm::SignatureAlgorithm::RSASSA_PSS(p)) => {
            let hash = p.hash_algorithm_oid().to_id_string();
            let mgf_hash_ok = match p.mask_gen_algorithm() {
                Ok(m) => SHA2_256_PLUS.contains(&m.hash.to_id_string().as_str()),
                Err(_) => false,
            };
            SHA2_256_PLUS.contains(&hash.as_str()) && mgf_hash_ok
        }
        _ => false,
    }
}

/// The helper's settings: the ARNs and region (not secrets), its port, and
/// what is optional only when given.
pub fn env_file(w: &Aws, has_chain: bool) -> String {
    let mut s = format!(
        "{OWNED}\nOMNIFILE_RA_TRUST_ANCHOR_ARN={}\nOMNIFILE_RA_PROFILE_ARN={}\nOMNIFILE_RA_ROLE_ARN={}\n\
         OMNIFILE_RA_REGION={}\nOMNIFILE_RA_PORT={HELPER_PORT}\n",
        w.trust_anchor_arn,
        w.profile_arn,
        w.role_arn,
        w.signing_region()
    );
    if !w.endpoint.is_empty() {
        s.push_str(&format!("OMNIFILE_RA_ENDPOINT={}\n", w.endpoint));
    }
    if w.session_seconds != 0 {
        s.push_str(&format!(
            "OMNIFILE_RA_SESSION_SECONDS={}\n",
            w.session_seconds
        ));
    }
    if !w.client_ca() {
        // The edge CA's certificate, which the daemon installs and renews; the
        // helper passes the chain only when it isn't empty.
        s.push_str(&format!(
            "OMNIFILE_RA_CERTIFICATE={RA_CERT}\nOMNIFILE_RA_INTERMEDIATES={RA_CHAIN}\n"
        ));
    } else if has_chain {
        s.push_str(&format!("OMNIFILE_RA_INTERMEDIATES={CHAIN_FILE}\n"));
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn a_new_key_has_a_request_for_itself_and_pem_round_trips() {
        let k = new_key("omnifile-edge-12", Some("urn:omnifile:edge:12")).unwrap();
        assert!(k.key_pem.contains("PRIVATE KEY"));
        assert!(k.csr_pem.starts_with("-----BEGIN CERTIFICATE REQUEST-----"));
        let der = &der_of(&k.csr_pem, "CERTIFICATE REQUEST")[0];
        let (_, csr) = X509CertificationRequest::from_der(der).unwrap();
        assert_eq!(
            csr.certification_request_info.subject.to_string(),
            "CN=omnifile-edge-12"
        );
        assert_eq!(
            csr.certification_request_info.subject_pki.raw,
            der_of(&k.public_pem, "PUBLIC KEY")[0].as_slice()
        );
        assert!(csr.verify_signature().is_ok());
        assert_eq!(
            pem_of("CERTIFICATE REQUEST", der),
            k.csr_pem.replace("\r\n", "\n")
        );
        // Two keys are two keys.
        let k2 = new_key("omnifile-edge-12", None).unwrap();
        assert_ne!(k.public_pem, k2.public_pem);
    }

    /// A test CA and a certificate it issues for `subject_key`.
    fn issue(
        subject_key: &rcgen::KeyPair,
        days: (i64, i64),
        ca_leaf: bool,
        usage: rcgen::KeyUsagePurpose,
    ) -> (String, String) {
        let ca_key = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
        let mut ca = rcgen::CertificateParams::new(Vec::<String>::new()).unwrap();
        ca.distinguished_name
            .push(rcgen::DnType::CommonName, "Test trust anchor");
        ca.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
        let ca_cert = ca.self_signed(&ca_key).unwrap();
        let issuer = rcgen::Issuer::from_params(&ca, &ca_key);
        let mut p = rcgen::CertificateParams::new(Vec::<String>::new()).unwrap();
        p.distinguished_name
            .push(rcgen::DnType::CommonName, "omnifile-edge-12");
        let now = ::time::OffsetDateTime::now_utc();
        p.not_before = now + ::time::Duration::days(days.0);
        p.not_after = now + ::time::Duration::days(days.1);
        p.key_usages = vec![usage];
        if ca_leaf {
            p.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
        }
        let leaf = p.signed_by(subject_key, &issuer).unwrap();
        (leaf.pem(), ca_cert.pem())
    }

    #[test]
    fn a_certificate_for_this_key_is_accepted_and_others_are_named() {
        let key = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
        let ours = key.public_key_pem();
        let now = SystemTime::now();
        let sig = rcgen::KeyUsagePurpose::DigitalSignature;
        let (leaf, ca) = issue(&key, (-1, 30), false, sig);
        let ok = check_certificate(&format!("{leaf}{ca}"), &ours, now).unwrap();
        assert_eq!(ok.subject, "CN=omnifile-edge-12");
        assert!(ok.leaf_pem.starts_with("-----BEGIN CERTIFICATE-----"));
        assert_eq!(
            der_of(&ok.chain_pem, "CERTIFICATE").len(),
            1,
            "the CA is kept as the chain"
        );
        assert!(
            check_certificate(&leaf, &ours, now)
                .unwrap()
                .chain_pem
                .is_empty()
        );

        let other = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
        let (theirs, _) = issue(&other, (-1, 30), false, sig);
        let e = check_certificate(&theirs, &ours, now).unwrap_err();
        assert!(e.contains("another key"), "{e}");
        let (old, _) = issue(&key, (-60, -1), false, sig);
        assert!(
            check_certificate(&old, &ours, now)
                .unwrap_err()
                .contains("expired")
        );
        let (early, _) = issue(&key, (2, 30), false, sig);
        assert!(
            check_certificate(&early, &ours, now)
                .unwrap_err()
                .contains("not valid until")
        );
        let (ca_like, _) = issue(&key, (-1, 30), true, sig);
        assert!(
            check_certificate(&ca_like, &ours, now)
                .unwrap_err()
                .contains("CA certificate")
        );
        let (enc, _) = issue(
            &key,
            (-1, 30),
            false,
            rcgen::KeyUsagePurpose::KeyEncipherment,
        );
        assert!(
            check_certificate(&enc, &ours, now)
                .unwrap_err()
                .contains("digitalSignature")
        );
        assert!(
            check_certificate("junk", &ours, now)
                .unwrap_err()
                .contains("no certificate")
        );
        // Valid now, not in a year.
        let later = now + Duration::from_secs(365 * 86400);
        let (leaf, _) = issue(
            &key,
            (-1, 30),
            false,
            rcgen::KeyUsagePurpose::DigitalSignature,
        );
        assert!(check_certificate(&leaf, &ours, later).is_err());
    }

    // A self-signed RSA-2048 leaf (sha256WithRSAEncryption, CA:FALSE, digitalSignature,
    // valid to 2126), made once with openssl for these tests, and its public key.
    const RSA_CERT: &str = "-----BEGIN CERTIFICATE-----
MIIC5zCCAc+gAwIBAgIJAImvb97JkRM5MA0GCSqGSIb3DQEBCwUAMCExHzAdBgNV
BAMMFm9tbmlmaWxlLWVkZ2UtcnNhLXRlc3QwIBcNMjYxMDIyMDIwMTIxWhgPMjEy
NjA5MjgwMjAxMjFaMCExHzAdBgNVBAMMFm9tbmlmaWxlLWVkZ2UtcnNhLXRlc3Qw
ggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQC/TJScd0IeQVXtq9WSEAZH
WcT/m1JFGXD7Z/6S5lsTd+SHHqkQ4gvpwOPG4UXcq4V40IqojsyfcwRLBN9JN1gq
mdK7P7tcfZLSM7sRqNUlyzUmzSxgTJAMDD4t2hOAuLiMhUvlUHtUVxbCP8fp56ur
mMq3DujPBmVJxivX4qdpzJNc/xJ+keiOQovec4ACl/FXr8qzVCSwRdH4MgZyP9ih
LDtzuUxZNBPH3p7xy3GGPJaoMxKmJ/4/GVzG3+XABEZwX0UCz/OLB3QMOBfk1dgz
CZ5RPwzKSSaRTAdgc2nA6aVp4eaq9F7DIYh08sBd3IXqhrK/NXP0Tztld2pLybKF
AgMBAAGjIDAeMAwGA1UdEwEB/wQCMAAwDgYDVR0PAQH/BAQDAgeAMA0GCSqGSIb3
DQEBCwUAA4IBAQAbyRtYxWrtQBUY7oaVcJqo1Peij071DJSjzcgv7raFJPdeZBX+
ZDEq6CbP93E0dMiM3BJdaSKbp389O5ngXTbjJglyDoSA499fpcQbekMt2aXzpJmM
xamX6RKqAmHr7WZHhDOQYEm0Cl1D0ALU7JQ3hWT+RQ3mHruV302i/E9ta1wCk4Fh
uTbwnOi8QOq3Z68+OBWkovl6wkyC/pnAJvnNqnsZ2Kff6ODAlFcP9BXxcMnrJxdU
4kFfOqXDMUtE765ftEIbCsgmfDgHnqtFsSoO9Dp7tNK9O5j6QrO40k+9ddcmZBXG
+pZLEKneg2G6bBCW48YsmDpemfGFmr/Ew2iq
-----END CERTIFICATE-----
";
    const RSA_PUB: &str = "-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAv0yUnHdCHkFV7avVkhAG
R1nE/5tSRRlw+2f+kuZbE3fkhx6pEOIL6cDjxuFF3KuFeNCKqI7Mn3MESwTfSTdY
KpnSuz+7XH2S0jO7EajVJcs1Js0sYEyQDAw+LdoTgLi4jIVL5VB7VFcWwj/H6eer
q5jKtw7ozwZlScYr1+KnacyTXP8SfpHojkKL3nOAApfxV6/Ks1QksEXR+DIGcj/Y
oSw7c7lMWTQTx96e8ctxhjyWqDMSpif+Pxlcxt/lwARGcF9FAs/ziwd0DDgX5NXY
MwmeUT8MykkmkUwHYHNpwOmlaeHmqvRewyGIdPLAXdyF6oayvzVz9E87ZXdqS8my
hQIDAQAB
-----END PUBLIC KEY-----
";
    const SHA256_RSA: [u8; 9] = [0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x0b];
    const ECDSA_SHA256: [u8; 8] = [0x2a, 0x86, 0x48, 0xce, 0x3d, 0x04, 0x03, 0x02];
    const SHA256: [u8; 9] = [0x60, 0x86, 0x48, 0x01, 0x65, 0x03, 0x04, 0x02, 0x01];
    const SHA1: [u8; 5] = [0x2b, 0x0e, 0x03, 0x02, 0x1a];

    fn tlv(tag: u8, body: &[u8]) -> Vec<u8> {
        assert!(body.len() < 128);
        let mut v = vec![tag, body.len() as u8];
        v.extend_from_slice(body);
        v
    }

    /// AlgorithmIdentifier DER: SEQUENCE { OID, params }.
    fn alg_der(oid: &[u8], params: &[u8]) -> Vec<u8> {
        let mut body = tlv(0x06, oid);
        body.extend_from_slice(params);
        tlv(0x30, &body)
    }

    fn allowed(der: &[u8]) -> bool {
        let (_, a) = AlgorithmIdentifier::from_der(der).unwrap();
        signature_allowed(&a)
    }

    /// RSASSA-PSS with `hash` for the digest and for MGF1.
    fn pss(hash: &[u8]) -> Vec<u8> {
        let null = [0x05, 0x00];
        let h = alg_der(hash, &null);
        let mgf1 = alg_der(&[0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x08], &h);
        let mut params = tlv(0xa0, &h);
        params.extend(tlv(0xa1, &mgf1));
        params.extend(tlv(0xa2, &[0x02, 0x01, 0x20]));
        alg_der(
            &[0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x0a],
            &tlv(0x30, &params),
        )
    }

    #[test]
    fn only_sha256_or_better_rsa_ecdsa_and_ed25519_signatures_are_allowed() {
        let null = [0x05, 0x00];
        let rsa = |last: u8| {
            let mut o = SHA256_RSA;
            o[8] = last;
            alg_der(&o, &null)
        };
        // Allowed: RSA PKCS#1 v1.5 with SHA-256/384/512, ECDSA with SHA-256/384/512, Ed25519.
        for last in [0x0b, 0x0c, 0x0d] {
            assert!(allowed(&rsa(last)), "{last:#x}");
        }
        for last in [0x02, 0x03, 0x04] {
            let mut o = ECDSA_SHA256;
            o[7] = last;
            assert!(allowed(&alg_der(&o, &[])), "ecdsa {last:#x}");
        }
        assert!(allowed(&alg_der(&[0x2b, 0x65, 0x70], &[])), "Ed25519");
        assert!(allowed(&pss(&SHA256)), "PSS with SHA-256");
        // Refused: SHA-224, SHA-1, MD5, MD2 (RSA); ECDSA with SHA-224 and SHA-1; DSA;
        // PSS with SHA-1 or with its SHA-1 default; anything unknown.
        for last in [0x0e, 0x05, 0x04, 0x02] {
            assert!(!allowed(&rsa(last)), "RSA {last:#x}");
        }
        let mut ec224 = ECDSA_SHA256;
        ec224[7] = 0x01;
        assert!(!allowed(&alg_der(&ec224, &[])), "ECDSA SHA-224");
        assert!(
            !allowed(&alg_der(&[0x2a, 0x86, 0x48, 0xce, 0x3d, 0x04, 0x01], &[])),
            "ECDSA SHA-1"
        );
        let dsa256 = [0x60, 0x86, 0x48, 0x01, 0x65, 0x03, 0x04, 0x03, 0x02];
        assert!(!allowed(&alg_der(&dsa256, &[])), "DSA with SHA-256");
        assert!(
            !allowed(&alg_der(&[0x2a, 0x86, 0x48, 0xce, 0x38, 0x04, 0x03], &[])),
            "DSA SHA-1"
        );
        assert!(!allowed(&pss(&SHA1)), "PSS with SHA-1");
        let pss_default = alg_der(
            &[0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x0a],
            &[0x30, 0x00],
        );
        assert!(!allowed(&pss_default), "PSS with the SHA-1 default");
        assert!(!allowed(&alg_der(&[0x2a, 0x03], &[])), "unknown");
    }

    /// `pem`'s DER with every `from` replaced by `to` (same length), back as PEM.
    fn patched(pem: &str, from: &[u8], to: &[u8]) -> String {
        let mut der = der_of(pem, "CERTIFICATE").remove(0);
        let mut n = 0;
        let mut i = 0;
        while i + from.len() <= der.len() {
            if der[i..i + from.len()] == *from {
                der[i..i + from.len()].copy_from_slice(to);
                n += 1;
            }
            i += 1;
        }
        assert_eq!(n, 2, "the signature algorithm, inside and outside the TBS");
        pem_of("CERTIFICATE", &der)
    }

    #[test]
    fn certificates_signed_with_weak_or_unknown_algorithms_are_refused() {
        let now = SystemTime::now();
        assert!(
            check_certificate(RSA_CERT, RSA_PUB, now).is_ok(),
            "RSA with SHA-256 is fine"
        );
        let mut to = SHA256_RSA;
        for (name, last) in [
            ("SHA-224", 0x0e),
            ("SHA-1", 0x05),
            ("MD5", 0x04),
            ("MD2", 0x02),
        ] {
            to[8] = last;
            let e =
                check_certificate(&patched(RSA_CERT, &SHA256_RSA, &to), RSA_PUB, now).unwrap_err();
            assert!(e.contains("is not RSA"), "{name}: {e}");
        }
        let dsa256 = [0x60, 0x86, 0x48, 0x01, 0x65, 0x03, 0x04, 0x03, 0x02];
        let e =
            check_certificate(&patched(RSA_CERT, &SHA256_RSA, &dsa256), RSA_PUB, now).unwrap_err();
        assert!(e.contains("2.16.840.1.101.3.4.3.2"), "DSA: {e}");
        // ECDSA with SHA-224, on a certificate for an ECDSA key.
        let key = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
        let (leaf, _) = issue(
            &key,
            (-1, 30),
            false,
            rcgen::KeyUsagePurpose::DigitalSignature,
        );
        let mut ec224 = ECDSA_SHA256;
        ec224[7] = 0x01;
        let e = check_certificate(
            &patched(&leaf, &ECDSA_SHA256, &ec224),
            &key.public_key_pem(),
            now,
        )
        .unwrap_err();
        assert!(e.contains("1.2.840.10045.4.3.1"), "ECDSA SHA-224: {e}");
    }

    #[test]
    fn the_env_file_has_the_arns_and_only_given_options() {
        let w = Aws {
            trust_anchor_arn: "arn:aws:rolesanywhere:eu-west-1:123456789012:trust-anchor/x".into(),
            profile_arn: "arn:aws:rolesanywhere:eu-west-1:123456789012:profile/y".into(),
            role_arn: "arn:aws:iam::123456789012:role/r".into(),
            issuer: "client".into(),
            ..Aws::default()
        };
        // Decision 50, the default: the edge CA's certificate, from the daemon.
        let ours = env_file(
            &Aws {
                issuer: String::new(),
                ..w.clone()
            },
            false,
        );
        assert!(
            ours.contains("\nOMNIFILE_RA_CERTIFICATE=/var/lib/omnifile/roles-anywhere/cert.pem\n")
        );
        assert!(
            ours.contains(
                "\nOMNIFILE_RA_INTERMEDIATES=/var/lib/omnifile/roles-anywhere/chain.pem\n"
            )
        );
        let e = env_file(&w, false);
        assert!(!e.contains("OMNIFILE_RA_CERTIFICATE"));
        assert!(e.contains("\nOMNIFILE_RA_REGION=eu-west-1\n"));
        assert!(e.contains("\nOMNIFILE_RA_PORT=9911\n"));
        assert!(!e.contains("ENDPOINT") && !e.contains("SESSION") && !e.contains("INTERMEDIATES"));
        let w2 = Aws {
            endpoint: "https://vpce.example".into(),
            session_seconds: 900,
            ..w
        };
        let e = env_file(&w2, true);
        assert!(e.contains("OMNIFILE_RA_ENDPOINT=https://vpce.example\n"));
        assert!(e.contains("OMNIFILE_RA_SESSION_SECONDS=900\n"));
        assert!(e.contains("OMNIFILE_RA_INTERMEDIATES=/etc/omnifile/roles-anywhere-chain.pem\n"));
        assert!(!e.contains("PRIVATE"));
    }
}
