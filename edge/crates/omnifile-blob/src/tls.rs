//! TLS configuration shared by every HTTPS client on the edge (Blob data
//! plane, control API, later S3).
//!
//! * rustls with the `ring` provider: no OpenSSL, no C++ build.
//! * **TLS 1.2 is allowed by default.** S3 interface endpoints reject TLS 1.3
//!   (`AGENTS.md`, "Credentials without secrets"), so the edge must be able to
//!   negotiate 1.2; [`TlsVersions`] narrows it per endpoint when wanted.
//! * Roots: the platform store (Azure private endpoints present public-CA
//!   certificates for `*.privatelink.blob.core.windows.net`), plus any extra
//!   PEM roots, or only the extra roots (the control API pins the OmniFile
//!   service CA learnt at enrolment).

use std::sync::Arc;
use std::time::Duration;

use rustls::client::WantsClientCert;
use rustls::crypto::CryptoProvider;
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, PrivateKeyDer};
use rustls::{ClientConfig, ConfigBuilder, RootCertStore, SupportedProtocolVersion};

/// Which TLS protocol versions a client may negotiate.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum TlsVersions {
    /// TLS 1.2 and 1.3 (default). The server picks.
    #[default]
    Tls12AndTls13,
    /// TLS 1.2 only, for endpoints known to mishandle 1.3.
    Tls12Only,
    /// TLS 1.3 only.
    Tls13Only,
}

impl TlsVersions {
    pub fn protocol_versions(self) -> &'static [&'static SupportedProtocolVersion] {
        static BOTH: &[&SupportedProtocolVersion] =
            &[&rustls::version::TLS13, &rustls::version::TLS12];
        static V12: &[&SupportedProtocolVersion] = &[&rustls::version::TLS12];
        static V13: &[&SupportedProtocolVersion] = &[&rustls::version::TLS13];
        match self {
            TlsVersions::Tls12AndTls13 => BOTH,
            TlsVersions::Tls12Only => V12,
            TlsVersions::Tls13Only => V13,
        }
    }
}

/// Where trusted roots come from.
#[derive(Clone, Debug, Default)]
pub struct TlsOptions {
    pub versions: TlsVersions,
    /// Load the platform's trust store (`/etc/ssl/certs` on Ubuntu).
    pub native_roots: bool,
    /// Extra trusted roots, PEM (one or more certificates).
    pub extra_roots_pem: Vec<Vec<u8>>,
}

impl TlsOptions {
    /// Platform roots, TLS 1.2 and 1.3: right for Azure Blob endpoints.
    pub fn public() -> Self {
        Self {
            versions: TlsVersions::default(),
            native_roots: true,
            extra_roots_pem: Vec::new(),
        }
    }

    /// Only the given PEM roots: a pinned private CA.
    pub fn pinned(ca_pem: impl Into<Vec<u8>>) -> Self {
        Self {
            versions: TlsVersions::default(),
            native_roots: false,
            extra_roots_pem: vec![ca_pem.into()],
        }
    }

    pub fn with_versions(mut self, versions: TlsVersions) -> Self {
        self.versions = versions;
        self
    }

    pub fn root_store(&self) -> Result<RootCertStore, TlsError> {
        let mut roots = RootCertStore::empty();
        if self.native_roots {
            let loaded = rustls_native_certs::load_native_certs();
            for e in &loaded.errors {
                tracing::warn!(error = %e, "error loading a platform root certificate");
            }
            let (added, ignored) = roots.add_parsable_certificates(loaded.certs);
            tracing::debug!(added, ignored, "platform roots loaded");
        }
        for pem in &self.extra_roots_pem {
            for cert in parse_certs_pem(pem)? {
                roots
                    .add(cert)
                    .map_err(|e| TlsError::Config(format!("bad root certificate: {e}")))?;
            }
        }
        if roots.is_empty() {
            return Err(TlsError::Config("no trusted root certificates".into()));
        }
        Ok(roots)
    }

    /// A config builder with this crate's provider and protocol versions, for
    /// callers that need their own verifier (for example a pinned-hash check).
    pub fn builder(&self) -> Result<ConfigBuilder<ClientConfig, rustls::WantsVerifier>, TlsError> {
        ClientConfig::builder_with_provider(provider())
            .with_protocol_versions(self.versions.protocol_versions())
            .map_err(|e| TlsError::Config(e.to_string()))
    }

    /// Builder with the root store already applied.
    pub fn builder_with_roots(
        &self,
    ) -> Result<ConfigBuilder<ClientConfig, WantsClientCert>, TlsError> {
        Ok(self.builder()?.with_root_certificates(self.root_store()?))
    }

    /// Server-authenticated client config, no client certificate.
    pub fn client_config(&self) -> Result<ClientConfig, TlsError> {
        Ok(self.builder_with_roots()?.with_no_client_auth())
    }

    /// Mutual TLS: present `cert_chain_pem` with `key_pem`.
    pub fn client_config_mtls(
        &self,
        cert_chain_pem: &[u8],
        key_pem: &[u8],
    ) -> Result<ClientConfig, TlsError> {
        let chain = parse_certs_pem(cert_chain_pem)?;
        let key = parse_key_pem(key_pem)?;
        self.builder_with_roots()?
            .with_client_auth_cert(chain, key)
            .map_err(|e| TlsError::Config(format!("client certificate: {e}")))
    }
}

/// The process-wide crypto provider: `ring`.
pub fn provider() -> Arc<CryptoProvider> {
    Arc::new(rustls::crypto::ring::default_provider())
}

/// A reqwest client over `config`. HTTP/1.1 and HTTP/2 (ALPN) are both offered.
pub fn http_client(
    mut config: ClientConfig,
    connect_timeout: Duration,
) -> Result<reqwest::Client, TlsError> {
    if config.alpn_protocols.is_empty() {
        config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    }
    let mut builder = reqwest::Client::builder();
    if crate::net::private_only() {
        builder = builder.dns_resolver(std::sync::Arc::new(crate::net::PrivateResolver));
    }
    builder
        .use_preconfigured_tls(config)
        // Never follow a redirect: a 3xx could send a SAS-bearing request
        // (or the client certificate's session) to another host or to
        // plain http. Azure and the service never redirect these calls.
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(connect_timeout)
        .pool_idle_timeout(Duration::from_secs(60))
        .build()
        .map_err(|e| TlsError::Config(format!("http client: {e}")))
}

pub fn parse_certs_pem(pem: &[u8]) -> Result<Vec<CertificateDer<'static>>, TlsError> {
    let certs = CertificateDer::pem_slice_iter(pem)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| TlsError::Config(format!("bad certificate PEM: {e}")))?;
    if certs.is_empty() {
        return Err(TlsError::Config("no certificate in PEM".into()));
    }
    Ok(certs)
}

pub fn parse_key_pem(pem: &[u8]) -> Result<PrivateKeyDer<'static>, TlsError> {
    PrivateKeyDer::from_pem_slice(pem)
        .map_err(|e| TlsError::Config(format!("bad private key PEM: {e}")))
}

#[derive(Debug, thiserror::Error)]
pub enum TlsError {
    #[error("TLS configuration: {0}")]
    Config(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tls12_is_allowed_by_default() {
        let v = TlsVersions::default().protocol_versions();
        assert!(
            v.iter()
                .any(|p| p.version == rustls::ProtocolVersion::TLSv1_2)
        );
        assert!(
            v.iter()
                .any(|p| p.version == rustls::ProtocolVersion::TLSv1_3)
        );
    }

    #[test]
    fn empty_roots_are_refused() {
        let opts = TlsOptions {
            native_roots: false,
            ..Default::default()
        };
        assert!(opts.root_store().is_err());
    }

    #[test]
    fn version_narrowing_builds() {
        for v in [
            TlsVersions::Tls12Only,
            TlsVersions::Tls13Only,
            TlsVersions::Tls12AndTls13,
        ] {
            TlsOptions::public()
                .with_versions(v)
                .builder()
                .expect("builder");
        }
    }
}
