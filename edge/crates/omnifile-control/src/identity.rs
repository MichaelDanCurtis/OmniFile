//! The edge's identity: enrolment bundle, key and CSR, the issued certificate,
//! and the pinned service CA (control API §2).
//!
//! Files in the identity directory (all `0600`):
//!
//! | File | Content |
//! |---|---|
//! | `identity.pem` | the client certificate, its chain, and the private key, in one file so a renewal replaces them together |
//! | `server-ca.pem` | the CA that issued `api`'s TLS certificate, pinned at enrolment: the edge CA when the enrolment answer says so (`api_server_ca_sha256`, threat model T5), else the CA of `enrol`'s certificate |
//! | `client-ca.pem` | the OmniFile CA (`ca_pem` from the service) |
//! | `identity.json` | `{edge_id, api_url, not_after, api_server_ca_sha256?}` |
//! | `pending-*.json` | a CSR in flight (enrolment or renewal) with its key and idempotency key, so a lost response is retried with the same request |
//!
//! The two files holding a private key, `identity.pem` and `pending-*.json`,
//! are sealed with systemd credentials unless `[service] key_protection` is
//! `file` (M6, [`crate::seal`]): they are then `identity.cred` and
//! `pending-*.cred`, bound to the box's TPM.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime};

use base64::Engine as _;
use rustls::client::WebPkiServerVerifier;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::CryptoProvider;
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{DigitallySignedStruct, RootCertStore, SignatureScheme};
use serde::{Deserialize, Serialize};

use omnifile_blob::tls::{self, TlsVersions};

use crate::error::{ControlError, Result};
use crate::seal;
use crate::types::{EdgeFacts, EnrolRequest, IssuedCertificate};
use crate::util::{new_idempotency_key, parse_time, sha256_hex, write_atomic};

const IDENTITY_PEM: &str = "identity.pem";
const SERVER_CA_PEM: &str = "server-ca.pem";
const CLIENT_CA_PEM: &str = "client-ca.pem";
const IDENTITY_JSON: &str = "identity.json";
const PENDING_ENROL: &str = "pending-enrol.json";
pub(crate) const PENDING_RENEWAL: &str = "pending-renewal.json";
/// The names the sealed files are bound to: a sealed key opens only as what
/// it was sealed as.
const IDENTITY_CRED: &str = "omnifile-identity";
const PENDING_CRED: &str = "omnifile-pending";

/// Renew when less than this much lifetime is left (day 20 of 30).
pub const RENEW_BEFORE_EXPIRY: Duration = Duration::from_secs(10 * 24 * 3600);

// ---------------------------------------------------------------------------
// Enrolment bundle
// ---------------------------------------------------------------------------

/// `ofe1.<base64url(JSON{"url", "server_ca_sha256", "token"})>`, pasted by the
/// operator in the setup wizard.
#[derive(Clone, PartialEq, Eq, Deserialize)]
pub struct EnrolmentBundle {
    pub url: String,
    pub server_ca_sha256: String,
    pub token: String,
}

impl std::fmt::Debug for EnrolmentBundle {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("EnrolmentBundle")
            .field("url", &self.url)
            .field("server_ca_sha256", &self.server_ca_sha256)
            .field("token", &"<redacted>")
            .finish()
    }
}

impl EnrolmentBundle {
    pub fn parse(s: &str) -> Result<Self> {
        let body = s.trim().strip_prefix("ofe1.").ok_or_else(|| {
            ControlError::Invalid("enrolment bundle must start with ofe1.".into())
        })?;
        let json = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .decode(body.trim_end_matches('='))
            .map_err(|e| {
                ControlError::Invalid(format!("enrolment bundle is not base64url: {e}"))
            })?;
        let b: EnrolmentBundle = serde_json::from_slice(&json)
            .map_err(|e| ControlError::Invalid(format!("enrolment bundle JSON: {e}")))?;
        b.pin()?;
        Ok(b)
    }

    /// The pinned SHA-256 (hex; colons and case ignored).
    pub fn pin(&self) -> Result<[u8; 32]> {
        parse_sha256(&self.server_ca_sha256)
    }
}

pub fn parse_sha256(s: &str) -> Result<[u8; 32]> {
    let hex: String = s
        .chars()
        .filter(|c| *c != ':')
        .collect::<String>()
        .to_ascii_lowercase();
    if hex.len() != 64 {
        return Err(ControlError::Invalid(format!("not a SHA-256: {s:?}")));
    }
    let mut out = [0u8; 32];
    for (i, b) in out.iter_mut().enumerate() {
        *b = u8::from_str_radix(&hex[2 * i..2 * i + 2], 16)
            .map_err(|_| ControlError::Invalid(format!("not a SHA-256: {s:?}")))?;
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// Key and CSR
// ---------------------------------------------------------------------------

/// A key with a CSR and the idempotency key of the request carrying it.
/// Persisted before the request is sent.
#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct PendingCsr {
    /// SHA-256 of what the request is for (the enrolment token, or the
    /// current certificate), so a stale pending file is not reused.
    pub(crate) for_sha256: String,
    pub(crate) key_pem: String,
    pub(crate) csr_pem: String,
    pub(crate) idempotency_key: String,
    /// The renewal proof, once made (renewals only; enrolment has none).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) proof: Option<crate::types::RenewalProof>,
}

impl PendingCsr {
    pub(crate) fn load_or_new(path: &Path, for_sha256: &str) -> Result<Self> {
        if let Ok(Some(bytes)) = seal::read_secret(path, PENDING_CRED)
            && let Ok(p) = serde_json::from_slice::<PendingCsr>(&bytes)
            && p.for_sha256 == for_sha256
        {
            return Ok(p);
        }
        let key = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256)
            .map_err(|e| ControlError::Identity(format!("key generation: {e}")))?;
        // The service ignores the subject; this one only helps a human reading the CSR.
        let mut params = rcgen::CertificateParams::new(Vec::<String>::new())
            .map_err(|e| ControlError::Identity(e.to_string()))?;
        params
            .distinguished_name
            .push(rcgen::DnType::CommonName, "omnifile-edge-pending");
        let csr = params
            .serialize_request(&key)
            .and_then(|c| c.pem())
            .map_err(|e| ControlError::Identity(format!("CSR: {e}")))?;
        let p = PendingCsr {
            for_sha256: for_sha256.to_owned(),
            key_pem: key.serialize_pem(),
            csr_pem: csr,
            idempotency_key: new_idempotency_key(),
            proof: None,
        };
        p.save(path)?;
        Ok(p)
    }

    pub(crate) fn save(&self, path: &Path) -> Result<()> {
        let json = serde_json::to_vec(self).map_err(|e| ControlError::Identity(e.to_string()))?;
        seal::write_secret(path, PENDING_CRED, &json)
    }

    /// Removes a pending file, in whichever form it is.
    pub(crate) fn remove(path: &Path) {
        seal::remove_secret(path);
    }
}

/// Sign a renewal with the CURRENT identity's key (control API §2): proof of
/// possession, so a copied certificate alone can't renew.
pub(crate) fn renewal_proof(
    current: &Identity,
    csr_pem: &str,
) -> Result<crate::types::RenewalProof> {
    signed_proof(current, csr_pem, crate::types::RENEWAL_PROOF_CONTEXT)
}

/// The same proof under `context` ([`crate::types::proof_message`]): the
/// Roles Anywhere certificate's request signs it with
/// [`crate::types::ROLES_ANYWHERE_PROOF_CONTEXT`] (decision 50).
pub(crate) fn signed_proof(
    current: &Identity,
    csr_pem: &str,
    context: &str,
) -> Result<crate::types::RenewalProof> {
    use base64::Engine as _;
    use ring::signature::{ECDSA_P256_SHA256_ASN1_SIGNING, EcdsaKeyPair};
    let bad = |what: &str| ControlError::Identity(format!("renewal proof: {what}"));
    let key = tls::parse_key_pem(&current.identity_pem).map_err(|e| bad(&e.to_string()))?;
    let rng = ring::rand::SystemRandom::new();
    let kp = EcdsaKeyPair::from_pkcs8(&ECDSA_P256_SHA256_ASN1_SIGNING, key.secret_der(), &rng)
        .map_err(|e| bad(&format!("the current key is not a P-256 PKCS#8 key: {e}")))?;
    let csr_der = pem_body(csr_pem).ok_or_else(|| bad("unreadable CSR PEM"))?;
    let signed_at = time::OffsetDateTime::now_utc()
        .replace_nanosecond(0)
        .map_err(|e| bad(&e.to_string()))?
        .format(&time::format_description::well_known::Rfc3339)
        .map_err(|e| bad(&e.to_string()))?;
    let msg = crate::types::proof_message(context, current.info.edge_id, &signed_at, &csr_der);
    let sig = kp.sign(&rng, &msg).map_err(|_| bad("signing failed"))?;
    Ok(crate::types::RenewalProof {
        signed_at,
        signature: base64::engine::general_purpose::STANDARD.encode(sig.as_ref()),
    })
}

/// The DER inside a single-block PEM.
pub(crate) fn pem_body(pem: &str) -> Option<Vec<u8>> {
    use base64::Engine as _;
    let b64: String = pem
        .lines()
        .filter(|l| !l.starts_with("-----") && !l.trim().is_empty())
        .map(str::trim)
        .collect();
    base64::engine::general_purpose::STANDARD.decode(b64).ok()
}

// ---------------------------------------------------------------------------
// The stored identity
// ---------------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct IdentityInfo {
    pub edge_id: u64,
    pub api_url: String,
    pub not_after: String,
    /// The SHA-256 (hex) of the CA `api`'s server certificate must chain to, from
    /// the enrolment answer (threat model T5: with TCP ingress, the edge CA).
    /// `server-ca.pem` is that certificate; the client checks it still is.
    /// None: an enrolment that named none (HTTP ingress), which pins `enrol`'s CA.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub api_server_ca_sha256: Option<String>,
}

/// The enrolled identity, as stored on disk.
#[derive(Clone)]
pub struct Identity {
    pub dir: PathBuf,
    pub info: IdentityInfo,
    /// Client certificate chain, then the private key (PEM).
    pub identity_pem: Vec<u8>,
    /// The pinned service CA (PEM).
    pub server_ca_pem: Vec<u8>,
    /// The key was found on disk in the clear while sealing is on (an edge
    /// enrolled before M6, or a crash between two writes): it is sealed now,
    /// but was readable, so the certificate is renewed with a new key at
    /// once ([`Identity::renewal_due`]).
    pub key_was_plain: bool,
}

impl std::fmt::Debug for Identity {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Identity")
            .field("dir", &self.dir)
            .field("info", &self.info)
            .finish_non_exhaustive()
    }
}

impl Identity {
    /// Is there an enrolled identity in `dir`?
    pub fn exists(dir: &Path) -> bool {
        seal::secret_exists(&dir.join(IDENTITY_PEM)) && dir.join(IDENTITY_JSON).exists()
    }

    pub fn load(dir: &Path) -> Result<Self> {
        let info: IdentityInfo =
            serde_json::from_slice(&std::fs::read(dir.join(IDENTITY_JSON))?)
                .map_err(|e| ControlError::Identity(format!("{IDENTITY_JSON}: {e}")))?;
        let key =
            seal::read_secret_full(&dir.join(IDENTITY_PEM), IDENTITY_CRED)?.ok_or_else(|| {
                ControlError::Identity(format!("no {IDENTITY_PEM} in {}", dir.display()))
            })?;
        Ok(Self {
            dir: dir.to_owned(),
            info,
            identity_pem: key.data,
            server_ca_pem: std::fs::read(dir.join(SERVER_CA_PEM))?,
            key_was_plain: key.was_plain,
        })
    }

    /// Store an issued certificate with its key. `identity.pem` is replaced
    /// atomically, so the key and certificate always match.
    pub(crate) fn store(
        dir: &Path,
        issued: &IssuedCertificate,
        key_pem: &str,
        api_url: &str,
        server_ca_pem: &[u8],
        api_server_ca_sha256: Option<&str>,
    ) -> Result<Self> {
        let mut pem = String::new();
        for part in [&issued.cert_pem, &issued.chain_pem] {
            pem.push_str(part.trim());
            pem.push('\n');
        }
        pem.push_str(key_pem.trim());
        pem.push('\n');
        // Validate before replacing anything.
        tls::parse_certs_pem(pem.as_bytes()).map_err(|e| ControlError::Identity(e.to_string()))?;
        tls::parse_key_pem(pem.as_bytes()).map_err(|e| ControlError::Identity(e.to_string()))?;
        let info = IdentityInfo {
            edge_id: issued.edge_id,
            api_url: api_url.to_owned(),
            not_after: issued.not_after.clone(),
            api_server_ca_sha256: api_server_ca_sha256.map(str::to_owned),
        };
        write_atomic(&dir.join(SERVER_CA_PEM), server_ca_pem, 0o600)?;
        write_atomic(&dir.join(CLIENT_CA_PEM), issued.ca_pem.as_bytes(), 0o600)?;
        seal::write_secret(&dir.join(IDENTITY_PEM), IDENTITY_CRED, pem.as_bytes())?;
        let json =
            serde_json::to_vec_pretty(&info).map_err(|e| ControlError::Identity(e.to_string()))?;
        write_atomic(&dir.join(IDENTITY_JSON), &json, 0o600)?;
        Ok(Self {
            dir: dir.to_owned(),
            info,
            identity_pem: pem.into_bytes(),
            server_ca_pem: server_ca_pem.to_vec(),
            key_was_plain: false,
        })
    }

    /// SHA-256 (hex) of the client certificate's DER, as the service records it.
    pub fn cert_sha256(&self) -> Result<String> {
        let certs = tls::parse_certs_pem(&self.identity_pem)
            .map_err(|e| ControlError::Identity(e.to_string()))?;
        Ok(sha256_hex(&certs[0]))
    }

    pub fn not_after(&self) -> Option<SystemTime> {
        parse_time(&self.info.not_after)
    }

    /// Renewal is due: from day 20 of a 30-day certificate.
    pub fn renewal_due(&self, now: SystemTime) -> bool {
        if self.key_was_plain {
            return true;
        }
        match self.not_after() {
            Some(na) => now + RENEW_BEFORE_EXPIRY >= na,
            None => true,
        }
    }

    pub fn expired(&self, now: SystemTime) -> bool {
        self.not_after().is_some_and(|na| now >= na)
    }

    /// The mutual-TLS client config for the `api` app: `server-ca.pem` is the
    /// only trusted root, and, when the enrolment answer named one, it must
    /// still be the pinned CA.
    pub fn client_config(&self, versions: TlsVersions) -> Result<rustls::ClientConfig> {
        if let Some(pin) = &self.info.api_server_ca_sha256 {
            let want = parse_sha256(pin)?;
            let roots = tls::parse_certs_pem(&self.server_ca_pem)
                .map_err(|e| ControlError::Identity(e.to_string()))?;
            if roots.len() != 1 || sha256(&roots[0]) != want {
                return Err(ControlError::Identity(format!(
                    "{SERVER_CA_PEM} is not the api CA pinned at enrolment ({pin})"
                )));
            }
        }
        tls::TlsOptions::pinned(self.server_ca_pem.clone())
            .with_versions(versions)
            .client_config_mtls(&self.identity_pem, &self.identity_pem)
            .map_err(|e| ControlError::Identity(e.to_string()))
    }
}

// ---------------------------------------------------------------------------
// Enrolment
// ---------------------------------------------------------------------------

/// Enrol this edge with a one-time bundle, and store the identity in `dir`.
///
/// The server certificate is accepted only if its chain contains a CA whose
/// SHA-256 equals the bundle's `server_ca_sha256`; that CA is then pinned for
/// every later call. The service must therefore send its CA in the chain.
pub async fn enrol(
    bundle: &EnrolmentBundle,
    dir: &Path,
    facts: EdgeFacts,
    versions: TlsVersions,
) -> Result<Identity> {
    const OP: &str = "enrol";
    let pin = bundle.pin()?;
    let pending_path = dir.join(PENDING_ENROL);
    let pending = PendingCsr::load_or_new(&pending_path, &sha256_hex(bundle.token.as_bytes()))?;

    let verifier = Arc::new(PinnedCaVerifier::new(pin));
    let config = tls::TlsOptions::default()
        .with_versions(versions)
        .builder()
        .map_err(|e| ControlError::Identity(e.to_string()))?
        .dangerous()
        .with_custom_certificate_verifier(verifier.clone())
        .with_no_client_auth();
    let http = tls::http_client(config, Duration::from_secs(10))
        .map_err(|e| ControlError::Identity(e.to_string()))?;

    let url = format!("{}/v0/enrol", bundle.url.trim_end_matches('/'));
    let body = EnrolRequest {
        token: bundle.token.clone(),
        csr_pem: pending.csr_pem.clone(),
        facts,
    };
    let resp = http
        .post(&url)
        .header("Idempotency-Key", &pending.idempotency_key)
        .header("OmniFile-Edge-Version", &body.facts.version)
        .json_body(&body)?
        .timeout(Duration::from_secs(30))
        .send()
        .await
        .map_err(|e| crate::client::transport_error(OP, e))?;
    let answer: EnrolAnswer = crate::client::decode(OP, resp).await?;
    let issued = answer.issued;
    let ca_der = verifier.pinned_ca().ok_or_else(|| {
        ControlError::Identity("server CA was not seen during the handshake".into())
    })?;
    let api_ca = api_server_ca(answer.api_server_ca_sha256.as_deref(), &issued, &ca_der)?;
    let api_url = issued.api_url.clone().unwrap_or_else(|| bundle.url.clone());
    let identity = Identity::store(
        dir,
        &issued,
        &pending.key_pem,
        &api_url,
        pem_encode("CERTIFICATE", &api_ca).as_bytes(),
        answer.api_server_ca_sha256.as_deref(),
    )?;
    PendingCsr::remove(&pending_path);
    tracing::info!(edge_id = identity.info.edge_id, not_after = %identity.info.not_after, "enrolled");
    Ok(identity)
}

/// The enrolment answer: the certificate, and which CA `api`'s server
/// certificate chains to (control API §2; threat model T5).
#[derive(Deserialize)]
struct EnrolAnswer {
    #[serde(flatten)]
    issued: IssuedCertificate,
    #[serde(default)]
    api_server_ca_sha256: Option<String>,
}

fn sha256(der: &[u8]) -> [u8; 32] {
    let mut out = [0u8; 32];
    out.copy_from_slice(ring::digest::digest(&ring::digest::SHA256, der).as_ref());
    out
}

/// The CA to pin for `api` (DER). With no pin in the answer (HTTP ingress)
/// `api` presents the same ingress certificate as `enrol`, whose CA the
/// bundle pinned. With a pin (TCP ingress, T5) `api`'s certificate is the
/// edge CA's: the pin must name `ca_pem` (or, failing that, `enrol`'s CA),
/// and anything else is refused, so the answer can't choose a CA it didn't
/// send over the pinned channel.
pub(crate) fn api_server_ca(
    pin: Option<&str>,
    issued: &IssuedCertificate,
    enrol_ca_der: &[u8],
) -> Result<Vec<u8>> {
    let Some(pin) = pin else {
        return Ok(enrol_ca_der.to_vec());
    };
    let want = parse_sha256(pin)?;
    let sent = tls::parse_certs_pem(issued.ca_pem.as_bytes())
        .map_err(|e| ControlError::Identity(format!("ca_pem: {e}")))?;
    if let Some(ca) = sent.iter().find(|c| sha256(c) == want) {
        return Ok(ca.to_vec());
    }
    if sha256(enrol_ca_der) == want {
        return Ok(enrol_ca_der.to_vec());
    }
    Err(ControlError::Identity(
        "the enrolment answer pins an api CA that is neither ca_pem nor enrol's CA".into(),
    ))
}

/// Encode DER as PEM.
pub fn pem_encode(label: &str, der: &[u8]) -> String {
    let b64 = base64::engine::general_purpose::STANDARD.encode(der);
    let mut out = format!("-----BEGIN {label}-----\n");
    for chunk in b64.as_bytes().chunks(64) {
        out.push_str(std::str::from_utf8(chunk).expect("base64 is ASCII"));
        out.push('\n');
    }
    out.push_str(&format!("-----END {label}-----\n"));
    out
}

/// Accept a server whose chain contains the CA with the pinned SHA-256, and
/// which validates (name, dates, signatures) against that CA alone.
#[derive(Debug)]
pub struct PinnedCaVerifier {
    pin: [u8; 32],
    provider: Arc<CryptoProvider>,
    seen: Mutex<Option<CertificateDer<'static>>>,
}

impl PinnedCaVerifier {
    pub fn new(pin: [u8; 32]) -> Self {
        Self {
            pin,
            provider: tls::provider(),
            seen: Mutex::new(None),
        }
    }

    /// The pinned CA certificate, once a handshake has verified against it.
    pub fn pinned_ca(&self) -> Option<CertificateDer<'static>> {
        self.seen.lock().expect("pinned CA lock").clone()
    }
}

impl ServerCertVerifier for PinnedCaVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        server_name: &ServerName<'_>,
        ocsp_response: &[u8],
        now: UnixTime,
    ) -> std::result::Result<ServerCertVerified, rustls::Error> {
        let ca = intermediates
            .iter()
            .find(|c| ring::digest::digest(&ring::digest::SHA256, c).as_ref() == self.pin)
            .ok_or_else(|| {
                rustls::Error::General(
                    "no certificate in the server chain matches the enrolment bundle's CA hash"
                        .into(),
                )
            })?;
        let mut roots = RootCertStore::empty();
        roots
            .add(ca.clone().into_owned())
            .map_err(|e| rustls::Error::General(format!("pinned CA: {e}")))?;
        let others: Vec<_> = intermediates.iter().filter(|c| *c != ca).cloned().collect();
        let inner =
            WebPkiServerVerifier::builder_with_provider(Arc::new(roots), self.provider.clone())
                .build()
                .map_err(|e| rustls::Error::General(e.to_string()))?;
        let ok = inner.verify_server_cert(end_entity, &others, server_name, ocsp_response, now)?;
        *self.seen.lock().expect("pinned CA lock") = Some(ca.clone().into_owned());
        Ok(ok)
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(
            message,
            cert,
            dss,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> std::result::Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(
            message,
            cert,
            dss,
            &self.provider.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.provider
            .signature_verification_algorithms
            .supported_schemes()
    }
}

/// `RequestBuilder::json` needs reqwest's `json` feature; this is the same
/// without it.
pub(crate) trait JsonBody: Sized {
    fn json_body<T: Serialize>(self, body: &T) -> Result<Self>;
}

impl JsonBody for reqwest::RequestBuilder {
    fn json_body<T: Serialize>(self, body: &T) -> Result<Self> {
        let bytes = serde_json::to_vec(body)
            .map_err(|e| ControlError::Invalid(format!("encode body: {e}")))?;
        Ok(self
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .body(bytes))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bundle_parse() {
        let json = serde_json::json!({
            "url": "https://enrol.internal", "server_ca_sha256": "AB".repeat(32), "token": "t".repeat(43)
        });
        let s = format!(
            "ofe1.{}",
            base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(json.to_string())
        );
        let b = EnrolmentBundle::parse(&s).unwrap();
        assert_eq!(b.url, "https://enrol.internal");
        assert_eq!(b.pin().unwrap(), [0xab; 32]);
        assert!(
            !format!("{b:?}").contains("ttt"),
            "token must not be logged"
        );
        assert!(EnrolmentBundle::parse("ofe2.xx").is_err());
    }

    #[test]
    fn sha256_with_colons() {
        let colon = (0..32).map(|_| "0A").collect::<Vec<_>>().join(":");
        assert_eq!(parse_sha256(&colon).unwrap(), [0x0a; 32]);
        assert!(parse_sha256("abc").is_err());
    }

    #[test]
    fn pending_csr_is_reused_for_the_same_request() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("pending.json");
        let a = PendingCsr::load_or_new(&p, "x").unwrap();
        let b = PendingCsr::load_or_new(&p, "x").unwrap();
        assert_eq!(a.csr_pem, b.csr_pem);
        assert_eq!(a.idempotency_key, b.idempotency_key);
        let c = PendingCsr::load_or_new(&p, "y").unwrap();
        assert_ne!(a.key_pem, c.key_pem);
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(&p).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
}

#[cfg(test)]
mod renewal_tests {
    use super::*;

    #[test]
    fn renewal_proof_verifies_with_the_current_key() {
        use ring::signature::{ECDSA_P256_SHA256_ASN1, KeyPair as _, UnparsedPublicKey};
        let dir = tempfile::tempdir().unwrap();
        // A "current" identity: a self-signed cert and its P-256 key.
        let key = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
        let cert = rcgen::CertificateParams::new(vec!["edge".into()])
            .unwrap()
            .self_signed(&key)
            .unwrap();
        let ident = Identity {
            dir: dir.path().to_owned(),
            info: IdentityInfo {
                edge_id: 42,
                api_url: "https://x".into(),
                not_after: "2026-11-01T00:00:00Z".into(),
                api_server_ca_sha256: None,
            },
            identity_pem: format!("{}{}", cert.pem(), key.serialize_pem()).into_bytes(),
            server_ca_pem: Vec::new(),
            key_was_plain: false,
        };
        let pending = PendingCsr::load_or_new(&dir.path().join("p.json"), "x").unwrap();
        let proof = renewal_proof(&ident, &pending.csr_pem).unwrap();
        let csr_der = pem_body(&pending.csr_pem).unwrap();
        let msg = crate::types::renewal_proof_message(42, &proof.signed_at, &csr_der);
        let sig = base64::engine::general_purpose::STANDARD
            .decode(&proof.signature)
            .unwrap();
        let rng = ring::rand::SystemRandom::new();
        let pkcs8 = key.serialize_der();
        let kp = ring::signature::EcdsaKeyPair::from_pkcs8(
            &ring::signature::ECDSA_P256_SHA256_ASN1_SIGNING,
            &pkcs8,
            &rng,
        )
        .unwrap();
        UnparsedPublicKey::new(&ECDSA_P256_SHA256_ASN1, kp.public_key().as_ref())
            .verify(&msg, &sig)
            .expect("the proof verifies with the current key");
        assert!(proof.signed_at.ends_with('Z'));
        assert!(
            String::from_utf8(msg)
                .unwrap()
                .starts_with("omnifile-renewal-v1\n42\n")
        );
        // The Roles Anywhere request (decision 50): the same key, its own
        // context, so a renewal proof can't stand in for it.
        let ra = signed_proof(
            &ident,
            &pending.csr_pem,
            crate::types::ROLES_ANYWHERE_PROOF_CONTEXT,
        )
        .unwrap();
        let ra_sig = base64::engine::general_purpose::STANDARD
            .decode(&ra.signature)
            .unwrap();
        let ra_msg =
            crate::types::proof_message("omnifile-roles-anywhere-v1", 42, &ra.signed_at, &csr_der);
        let public = UnparsedPublicKey::new(&ECDSA_P256_SHA256_ASN1, kp.public_key().as_ref());
        public.verify(&ra_msg, &ra_sig).expect("it verifies");
        let as_renewal = crate::types::renewal_proof_message(42, &ra.signed_at, &csr_der);
        assert!(public.verify(&as_renewal, &ra_sig).is_err());
    }
}
