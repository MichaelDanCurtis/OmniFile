//! The edge's S3 reader: ranged GETs of one object version, signed with
//! SigV4 from temporary credentials (M5, `docs/contracts/control-api.md` §3).
//!
//! * **Credentials.** The edge holds no AWS secret. Temporary credentials
//!   come from the local IAM Roles Anywhere helper (`aws_signing_helper
//!   serve`, an IMDSv2-compatible endpoint on loopback), and are refreshed
//!   before they expire. A refused session (`ExpiredToken`, `InvalidToken`)
//!   forces one refresh and one retry.
//! * **Private endpoints when private.** With private networking (the
//!   object's `private_network`, or the edge's `[network] private_only`,
//!   decision 22) the endpoint must be an S3 interface endpoint name
//!   (`*.vpce.amazonaws.com`); any other host is refused before a request
//!   is made, and the name resolves through [`crate::net::PrivateResolver`].
//!   Without it, any valid DNS name on 443 is accepted. Tests and the sim
//!   name their hosts in [`S3Options::test_endpoints`].
//! * **Addressing.** Virtual host (`<bucket>.<endpoint>`, the default for
//!   endpoint-specific names) or path (`<endpoint>/<bucket>/<key>`).
//! * **TLS 1.2.** Interface endpoints reject TLS 1.3, so an object whose
//!   credential says `tls_max_version = "1.2"` is read over a client that
//!   offers TLS 1.2 only.
//! * **Pinned version.** With a `version_id` the GET names it, and the
//!   response must carry the same `x-amz-version-id`. An object of an
//!   unversioned bucket is pinned by its ETag instead (`If-Match`): a `412`
//!   means the source changed ([`BlobError::is_source_changed`]). An object
//!   with neither is never read ([`BlobError::Unpinned`]).
//! * **No per-range checksum.** S3 returns full-object checksums on whole
//!   GETs only; a ranged read is protected by TLS alone (M5 decision). The
//!   whole-file CRC-64/NVME is checked when a file becomes fully local
//!   (`omnifile-stub`'s chunk fill). Every response is still checked for
//!   status, `Content-Range`, length and version: a short or mismatched
//!   body is an error, never zeros.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use aws_credential_types::Credentials;
use aws_sigv4::http_request::{
    PayloadChecksumKind, PercentEncodingMode, SignableBody, SignableRequest, SigningSettings,
    UriPathNormalizationMode, sign,
};
use aws_sigv4::sign::v4;
use aws_smithy_runtime_api::client::identity::Identity;
use bytes::Bytes;
use reqwest::{Method, Response, Url};
use serde::Deserialize;
use tokio::sync::Mutex;

use crate::client::RetryPolicy;
use crate::error::{BlobError, Result};
use crate::tls::{TlsOptions, TlsVersions};

/// The suffix every S3 interface endpoint name carries.
pub const PRIVATE_ENDPOINT_SUFFIXES: &[&str] = &[".vpce.amazonaws.com", ".vpce.amazonaws.com.cn"];

/// Default address of `aws_signing_helper serve` (its documented default port).
pub const DEFAULT_IMDS_URL: &str = "http://127.0.0.1:9911";

/// How an object's URL is formed.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Addressing {
    /// `https://<bucket>.<endpoint>/<key>` (default).
    #[default]
    VirtualHost,
    /// `https://<endpoint>/<bucket>/<key>`.
    Path,
}

impl Addressing {
    /// The wire word (`virtual_host`, `path`); `None` for anything else.
    pub fn parse(s: &str) -> Option<Addressing> {
        match s {
            "virtual_host" => Some(Addressing::VirtualHost),
            "path" => Some(Addressing::Path),
            _ => None,
        }
    }
}

/// One S3 object version, as the service names it (a credential pointer or
/// the feed's location plus the backend's `edge_access`). No secret.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct S3Object {
    pub bucket: String,
    pub key: String,
    /// The object version (`backend_version_id`). `None` only for
    /// unversioned buckets, which are pinned by `etag` instead.
    pub version_id: Option<String>,
    /// The object's ETag as the catalog recorded it. Sent as `If-Match`
    /// whenever known; required when there is no `version_id`.
    pub etag: Option<String>,
    pub region: String,
    /// The interface endpoint name, `vpce-….s3.<region>.vpce.amazonaws.com`
    /// (a leading `<bucket>.` is tolerated). Host, optionally `:port`.
    pub endpoint: String,
    pub addressing: Addressing,
    /// Offer TLS 1.2 only (interface endpoints reject 1.3).
    pub tls12_only: bool,
    /// Send `x-amz-request-payer: requester`.
    pub requester_pays: bool,
    /// Interface endpoints only (decision 22). The edge's own
    /// `[network] private_only` turns it on for every object too.
    pub private_network: bool,
}

/// How an [`S3Object`] travels where the daemon's ports carry a URL string
/// (a fill's `FetchTarget::url`): `s3ra:` then the object as JSON. It is a
/// pointer, never a secret, and never sent anywhere.
pub const POINTER_PREFIX: &str = "s3ra:";

#[derive(serde::Serialize, Deserialize)]
struct Pointer {
    bucket: String,
    key: String,
    #[serde(default)]
    version_id: Option<String>,
    #[serde(default)]
    etag: Option<String>,
    region: String,
    endpoint: String,
    #[serde(default)]
    path_style: bool,
    #[serde(default)]
    tls12_only: bool,
    #[serde(default)]
    requester_pays: bool,
    #[serde(default)]
    private_network: bool,
}

impl S3Object {
    /// The object as a pointer string ([`POINTER_PREFIX`]).
    pub fn to_pointer(&self) -> String {
        let p = Pointer {
            bucket: self.bucket.clone(),
            key: self.key.clone(),
            version_id: self.version_id.clone(),
            etag: self.etag.clone(),
            region: self.region.clone(),
            endpoint: self.endpoint.clone(),
            path_style: self.addressing == Addressing::Path,
            tls12_only: self.tls12_only,
            requester_pays: self.requester_pays,
            private_network: self.private_network,
        };
        format!(
            "{POINTER_PREFIX}{}",
            serde_json::to_string(&p).expect("a pointer serializes")
        )
    }

    /// Parse [`S3Object::to_pointer`]'s output.
    pub fn from_pointer(s: &str) -> Result<S3Object> {
        let json = s
            .strip_prefix(POINTER_PREFIX)
            .ok_or_else(|| BlobError::Invalid("not an S3 pointer".into()))?;
        let p: Pointer = serde_json::from_str(json)
            .map_err(|e| BlobError::Invalid(format!("bad S3 pointer: {e}")))?;
        Ok(S3Object {
            bucket: p.bucket,
            key: p.key,
            version_id: p.version_id,
            etag: p.etag,
            region: p.region,
            endpoint: p.endpoint,
            addressing: if p.path_style {
                Addressing::Path
            } else {
                Addressing::VirtualHost
            },
            tls12_only: p.tls12_only,
            requester_pays: p.requester_pays,
            private_network: p.private_network,
        })
    }

    /// True when its bytes can be pinned: by version id, or by ETag.
    pub fn is_pinned(&self) -> bool {
        self.version_id.is_some() || self.etag.as_deref().is_some_and(|e| !e.trim().is_empty())
    }

    /// The error for an object that can't be pinned.
    pub fn unpinned_error(&self) -> BlobError {
        BlobError::Unpinned(format!(
            "s3://{}/{}: no version id and no ETag",
            self.bucket, self.key
        ))
    }
}

/// Temporary (or, in tests, static) AWS credentials.
#[derive(Clone, PartialEq, Eq)]
pub struct AwsCredentials {
    pub access_key_id: String,
    pub secret_access_key: String,
    pub session_token: Option<String>,
    pub expiration: Option<SystemTime>,
}

impl std::fmt::Debug for AwsCredentials {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Never print the secret or the session token.
        f.debug_struct("AwsCredentials")
            .field("access_key_id", &self.access_key_id)
            .field("session_token", &self.session_token.as_ref().map(|_| "…"))
            .field("expiration", &self.expiration)
            .finish()
    }
}

/// Where the reader's credentials come from.
#[derive(Clone, Debug)]
pub enum CredentialSource {
    /// The local `aws_signing_helper serve` endpoint (IMDSv2 protocol). Only
    /// a loopback `http://` URL is accepted: credentials never cross the
    /// network in clear text.
    Imds { url: String },
    /// Fixed credentials (tests, the sim).
    Static(AwsCredentials),
}

#[derive(Clone, Debug)]
pub struct S3Options {
    pub credentials: CredentialSource,
    pub retry: RetryPolicy,
    pub connect_timeout: Duration,
    pub request_timeout: Duration,
    /// Floor on the time a range transfer is allowed: `len / min_rate`.
    pub min_transfer_bytes_per_sec: u64,
    /// Refresh credentials this long before they expire (capped at half
    /// their lifetime).
    pub refresh_margin: Duration,
    /// Trusted roots (platform roots by default). The versions in it are
    /// ignored: they come per object.
    pub tls: TlsOptions,
    /// Accept `http://` (the sim). Off by default.
    pub allow_http: bool,
    /// Endpoint hosts (`host` or `host:port`) accepted besides interface
    /// endpoint names: the sim and test servers only. Empty in production.
    pub test_endpoints: Vec<String>,
    /// DNS overrides `(host, addr)`, for virtual-host names in tests.
    pub resolve: Vec<(String, SocketAddr)>,
}

impl Default for S3Options {
    fn default() -> Self {
        Self {
            credentials: CredentialSource::Imds {
                url: DEFAULT_IMDS_URL.to_owned(),
            },
            retry: RetryPolicy::default(),
            connect_timeout: Duration::from_secs(10),
            request_timeout: Duration::from_secs(60),
            min_transfer_bytes_per_sec: 1 << 20,
            refresh_margin: Duration::from_secs(300),
            tls: TlsOptions::public(),
            allow_http: false,
            test_endpoints: Vec::new(),
            resolve: Vec::new(),
        }
    }
}

#[derive(Clone)]
pub struct S3Client {
    /// TLS 1.2 and 1.3.
    http_any: reqwest::Client,
    /// TLS 1.2 only.
    http_12: reqwest::Client,
    opts: Arc<S3Options>,
    creds: Arc<CredentialProvider>,
}

impl S3Client {
    pub fn new(opts: S3Options) -> Result<Self> {
        let build = |versions: TlsVersions, private: bool| -> Result<reqwest::Client> {
            let mut tls = opts.tls.clone().with_versions(versions);
            tls.versions = versions;
            let mut cfg = tls
                .client_config()
                .map_err(|e| BlobError::Invalid(e.to_string()))?;
            cfg.alpn_protocols = vec![b"http/1.1".to_vec()];
            let mut b = reqwest::Client::builder()
                .use_preconfigured_tls(cfg)
                // A redirect could carry a signed request elsewhere.
                .redirect(reqwest::redirect::Policy::none())
                .connect_timeout(opts.connect_timeout)
                .pool_idle_timeout(Duration::from_secs(60));
            if private {
                // Interface endpoints resolve to private addresses only.
                b = b.dns_resolver(Arc::new(crate::net::PrivateResolver));
            }
            for (host, addr) in &opts.resolve {
                b = b.resolve(host, *addr);
            }
            b.build()
                .map_err(|e| BlobError::Invalid(format!("http client: {e}")))
        };
        let private = crate::net::private_only();
        let http_any = build(TlsVersions::Tls12AndTls13, private)?;
        let http_12 = build(TlsVersions::Tls12Only, private)?;
        // The credential helper is on loopback, over plain http: its own
        // client, which never resolves names through the private resolver.
        let imds = build(TlsVersions::Tls12AndTls13, false)?;
        let creds = CredentialProvider::new(&opts, imds)?;
        Ok(Self {
            http_any,
            http_12,
            opts: Arc::new(opts),
            creds: Arc::new(creds),
        })
    }

    pub fn options(&self) -> &S3Options {
        &self.opts
    }

    /// Current credentials (refreshed if due). For diagnostics and tests.
    pub async fn credentials(&self) -> Result<AwsCredentials> {
        self.creds.get(false).await
    }

    /// The object's URL, after checking its endpoint is allowed.
    ///
    /// Bucket and endpoint are checked character by character before any
    /// URL is built (only `[a-z0-9.-]`, plus `:port` for test endpoints),
    /// and the parsed URL's scheme, host, port and userinfo are checked
    /// again against what was intended: a signed request with the session
    /// token must never reach any other host.
    pub fn object_url(&self, o: &S3Object) -> Result<Url> {
        let scheme = if self.opts.allow_http {
            "http"
        } else {
            "https"
        };
        let (ep_host, ep_port) = self.checked_endpoint(o)?;
        if !valid_bucket(&o.bucket) {
            return Err(BlobError::Invalid(format!(
                "bad bucket name {:?}: only [a-z0-9.-], 3 to 63 characters",
                o.bucket
            )));
        }
        let key = encode_key(&o.key);
        let host = match o.addressing {
            Addressing::VirtualHost if ep_host.starts_with(&format!("{}.", o.bucket)) => {
                ep_host.to_owned()
            }
            Addressing::VirtualHost => format!("{}.{ep_host}", o.bucket),
            Addressing::Path => ep_host.to_owned(),
        };
        let authority = match ep_port {
            Some(p) => format!("{host}:{p}"),
            None => host.clone(),
        };
        let mut s = match o.addressing {
            Addressing::VirtualHost => format!("{scheme}://{authority}/{key}"),
            Addressing::Path => format!("{scheme}://{authority}/{}/{key}", o.bucket),
        };
        if let Some(v) = &o.version_id {
            s.push_str("?versionId=");
            s.push_str(&encode_query_value(v));
        }
        let url = Url::parse(&s).map_err(|e| BlobError::Invalid(format!("bad S3 URL: {e}")))?;
        // What the parser made of it must be exactly what was meant.
        if url.scheme() != scheme
            || url.host_str() != Some(host.as_str())
            || url.port() != ep_port
            || !url.username().is_empty()
            || url.password().is_some()
        {
            return Err(BlobError::Invalid(format!(
                "S3 URL for {:?} does not name the endpoint host {host:?}: refusing",
                o.bucket
            )));
        }
        // A literal IP never goes through the resolver.
        crate::net::check_host(url.host_str()).map_err(BlobError::Invalid)?;
        Ok(url)
    }

    /// The endpoint's host (and port, for test endpoints only), if it is an
    /// interface endpoint name or a configured test host.
    fn checked_endpoint<'a>(&self, o: &'a S3Object) -> Result<(&'a str, Option<u16>)> {
        let e = o.endpoint.as_str();
        let bad = || BlobError::Invalid(format!("bad S3 endpoint {e:?}"));
        if self.opts.test_endpoints.iter().any(|t| t == e) {
            let (host, port) = match e.rsplit_once(':') {
                Some((h, p)) => (h, Some(p.parse::<u16>().map_err(|_| bad())?)),
                None => (e, None),
            };
            if !valid_dns_name(host) {
                return Err(bad());
            }
            return Ok((host, port));
        }
        if e.contains(':') {
            // Interface endpoints listen on 443 only.
            return Err(BlobError::Invalid(format!(
                "S3 endpoint {e:?} names a port: refusing (interface endpoints only)"
            )));
        }
        if !valid_dns_name(e) {
            return Err(bad());
        }
        let interface = PRIVATE_ENDPOINT_SUFFIXES
            .iter()
            .any(|s| e.ends_with(s) && e.len() > s.len());
        if interface || !(o.private_network || crate::net::private_only()) {
            Ok((e, None))
        } else {
            Err(BlobError::Invalid(format!(
                "S3 endpoint {e:?} is not an interface endpoint name: refusing a public path"
            )))
        }
    }

    /// Read exactly `len` bytes at `offset` of the object version, with
    /// retries. Not checksummed per range (S3 can't); see the module docs.
    pub async fn get_range(&self, o: &S3Object, offset: u64, len: u64) -> Result<Bytes> {
        const OP: &str = "S3 GetObject";
        if len == 0 {
            return Ok(Bytes::new());
        }
        let end = offset
            .checked_add(len - 1)
            .ok_or_else(|| BlobError::Invalid("range overflows".into()))?;
        // Nothing to pin the bytes to: a read could mix two objects.
        if !o.is_pinned() {
            return Err(o.unpinned_error());
        }
        let url = self.object_url(o)?;
        let http = if o.tls12_only {
            &self.http_12
        } else {
            &self.http_any
        };
        let timeout = self.opts.request_timeout
            + Duration::from_secs(len / self.opts.min_transfer_bytes_per_sec.max(1));
        let mut attempt = 1;
        let mut refreshed = false;
        loop {
            let r = async {
                let creds = self.creds.get(false).await?;
                let mut headers = vec![
                    ("host".to_owned(), host_header(&url)),
                    ("range".to_owned(), format!("bytes={offset}-{end}")),
                ];
                if o.requester_pays {
                    headers.push(("x-amz-request-payer".to_owned(), "requester".to_owned()));
                }
                if let Some(etag) = o.etag.as_deref().filter(|e| !e.trim().is_empty()) {
                    headers.push(("if-match".to_owned(), quote_etag(etag)));
                }
                let signed = sign_request(
                    "GET",
                    url.as_str(),
                    &headers,
                    &creds,
                    &o.region,
                    SystemTime::now(),
                )?;
                let mut req = http.request(Method::GET, url.clone()).timeout(timeout);
                for (k, v) in headers.iter().filter(|(k, _)| k != "host") {
                    req = req.header(k, v);
                }
                for (k, v) in &signed {
                    req = req.header(k, v);
                }
                let resp = req.send().await.map_err(|e| transport(OP, e))?;
                let resp = check(OP, resp).await?;
                read_range_response(OP, resp, o, offset, end, len).await
            }
            .await;
            match r {
                Ok(b) => return Ok(b),
                Err(e) if !refreshed && is_session_refused(&e) => {
                    tracing::debug!(error = %e, "S3 refused the session; refreshing credentials");
                    self.creds.get(true).await?;
                    refreshed = true;
                }
                Err(e) if e.is_retryable() && attempt < self.opts.retry.max_attempts => {
                    let d = self.opts.retry.delay(attempt, e.retry_after());
                    tracing::debug!(op = OP, attempt, error = %e, "retrying");
                    tokio::time::sleep(d).await;
                    attempt += 1;
                }
                Err(e) => return Err(e),
            }
        }
    }
}

/// A DNS name as the edge accepts it: lowercase labels of `[a-z0-9-]`,
/// none empty or starting or ending with `-`, at most 253 characters. No
/// escapes, no percent-encoding, no Unicode, no IP literals in brackets.
fn valid_dns_name(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 253
        && s.split('.').all(|l| {
            !l.is_empty()
                && l.len() <= 63
                && !l.starts_with('-')
                && !l.ends_with('-')
                && l.bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        })
}

/// A bucket name usable in a host name: 3 to 63 of `[a-z0-9.-]`, starting
/// and ending with a letter or digit, no empty label. Legacy names with
/// capitals or `_` are refused (fail closed).
fn valid_bucket(s: &str) -> bool {
    (3..=63).contains(&s.len()) && valid_dns_name(s)
}

/// An ETag as `If-Match` wants it: quoted (S3 lists them quoted; the
/// catalog may hold either form).
fn quote_etag(e: &str) -> String {
    let e = e.trim();
    if e.starts_with('"') || e.starts_with("W/") {
        e.to_owned()
    } else {
        format!("\"{e}\"")
    }
}

/// `host[:port]` of a URL, as the Host header carries it.
fn host_header(url: &Url) -> String {
    let host = url.host_str().unwrap_or_default();
    match url.port() {
        Some(p) => format!("{host}:{p}"),
        None => host.to_owned(),
    }
}

fn transport(op: &'static str, e: reqwest::Error) -> BlobError {
    BlobError::Transport {
        op,
        source: e.without_url(),
    }
}

fn is_session_refused(e: &BlobError) -> bool {
    matches!(
        e.code(),
        Some("ExpiredToken" | "InvalidToken" | "TokenRefreshRequired" | "RequestExpired")
    )
}

async fn read_range_response(
    op: &'static str,
    resp: Response,
    o: &S3Object,
    offset: u64,
    end: u64,
    len: u64,
) -> Result<Bytes> {
    let status = resp.status().as_u16();
    let h = |n: &str| {
        resp.headers()
            .get(n)
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned)
    };
    let content_range = h("content-range");
    let version = h("x-amz-version-id");
    let body = resp.bytes().await.map_err(|e| transport(op, e))?;
    match status {
        206 => {
            let want = format!("bytes {offset}-{end}/");
            match &content_range {
                Some(cr) if cr.starts_with(&want) => {}
                other => {
                    return Err(BlobError::BadResponse {
                        op,
                        detail: format!("Content-Range {other:?} for bytes {offset}-{end}"),
                    });
                }
            }
        }
        // A server that ignores Range sends the whole object: acceptable
        // only when that is exactly the range asked for.
        200 if offset == 0 => {}
        _ => {
            return Err(BlobError::BadResponse {
                op,
                detail: format!("status {status} for a range read"),
            });
        }
    }
    if body.len() as u64 != len {
        return Err(BlobError::BadResponse {
            op,
            detail: format!("asked {len} bytes at {offset}, got {}", body.len()),
        });
    }
    if let Some(want) = &o.version_id
        && version.as_deref() != Some(want.as_str())
    {
        return Err(BlobError::Integrity(format!(
            "S3 range read returned version {version:?}, not {want}"
        )));
    }
    Ok(body)
}

/// Turn an S3 error response into [`BlobError::Http`] (code from the XML
/// body; S3 has no error-code header).
async fn check(op: &'static str, resp: Response) -> Result<Response> {
    let status = resp.status();
    if status.is_success() {
        return Ok(resp);
    }
    let request_id = resp
        .headers()
        .get("x-amz-request-id")
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned);
    let retry_after = resp
        .headers()
        .get("retry-after")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok())
        .map(Duration::from_secs);
    let body = resp.text().await.unwrap_or_default();
    let code = xml_tag(&body, "Code");
    let message =
        xml_tag(&body, "Message").unwrap_or_else(|| status.canonical_reason().unwrap_or("").into());
    Err(BlobError::Http {
        op,
        status: status.as_u16(),
        code,
        message,
        request_id,
        retry_after,
    })
}

fn xml_tag(body: &str, tag: &str) -> Option<String> {
    let open = format!("<{tag}>");
    let start = body.find(&open)? + open.len();
    let end = body[start..].find(&format!("</{tag}>"))? + start;
    Some(body[start..end].trim().to_owned())
}

/// SigV4 headers for an S3 request with an empty body: `authorization`,
/// `x-amz-date`, `x-amz-content-sha256` and, with a session,
/// `x-amz-security-token`. `headers` must include `host`.
pub fn sign_request(
    method: &str,
    url: &str,
    headers: &[(String, String)],
    creds: &AwsCredentials,
    region: &str,
    time: SystemTime,
) -> Result<Vec<(String, String)>> {
    sign_request_with_body(method, url, headers, &[], creds, region, time)
}

/// [`sign_request`] for a request carrying `body` (its SHA-256 is signed).
/// The edge itself only reads; tests use this to set up buckets.
pub fn sign_request_with_body(
    method: &str,
    url: &str,
    headers: &[(String, String)],
    body: &[u8],
    creds: &AwsCredentials,
    region: &str,
    time: SystemTime,
) -> Result<Vec<(String, String)>> {
    let identity: Identity = Credentials::new(
        creds.access_key_id.clone(),
        creds.secret_access_key.clone(),
        creds.session_token.clone(),
        None,
        "omnifile-edge",
    )
    .into();
    // S3's rules: the path is signed as sent (keys are encoded once and
    // never normalised), and the payload hash is a signed header.
    let mut settings = SigningSettings::default();
    settings.percent_encoding_mode = PercentEncodingMode::Single;
    settings.payload_checksum_kind = PayloadChecksumKind::XAmzSha256;
    settings.uri_path_normalization_mode = UriPathNormalizationMode::Disabled;
    let params = v4::SigningParams::builder()
        .identity(&identity)
        .region(region)
        .name("s3")
        .time(time)
        .settings(settings)
        .build()
        .map_err(|e| BlobError::Invalid(format!("SigV4 parameters: {e}")))?
        .into();
    let req = SignableRequest::new(
        method,
        url,
        headers.iter().map(|(k, v)| (k.as_str(), v.as_str())),
        SignableBody::Bytes(body),
    )
    .map_err(|e| BlobError::Invalid(format!("SigV4 request: {e}")))?;
    let (instructions, _sig) = sign(req, &params)
        .map_err(|e| BlobError::Invalid(format!("SigV4: {e}")))?
        .into_parts();
    Ok(instructions
        .headers()
        .map(|(k, v)| (k.to_owned(), v.to_owned()))
        .collect())
}

/// Percent-encode an object key for the URL path: every byte except the
/// unreserved set and `/`.
pub fn encode_key(key: &str) -> String {
    encode(key, true)
}

fn encode_query_value(v: &str) -> String {
    encode(v, false)
}

fn encode(s: &str, keep_slash: bool) -> String {
    let mut out = String::with_capacity(s.len() * 3);
    for b in s.bytes() {
        if b.is_ascii_alphanumeric()
            || matches!(b, b'-' | b'.' | b'_' | b'~')
            || (keep_slash && b == b'/')
        {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Credentials from `aws_signing_helper serve` (IMDSv2)
// ---------------------------------------------------------------------------

struct CredentialProvider {
    source: Source,
    margin: Duration,
    state: Mutex<Option<Held>>,
}

enum Source {
    Static(AwsCredentials),
    Imds { http: reqwest::Client, base: Url },
}

struct Held {
    creds: AwsCredentials,
    fetched: SystemTime,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct ImdsCreds {
    #[serde(default)]
    code: Option<String>,
    access_key_id: String,
    secret_access_key: String,
    #[serde(default)]
    token: Option<String>,
    expiration: String,
}

impl CredentialProvider {
    fn new(opts: &S3Options, http: reqwest::Client) -> Result<Self> {
        let source = match &opts.credentials {
            CredentialSource::Static(c) => Source::Static(c.clone()),
            CredentialSource::Imds { url } => {
                let base = Url::parse(url)
                    .map_err(|e| BlobError::Invalid(format!("bad IMDS URL: {e}")))?;
                let host = base.host_str().unwrap_or("");
                let loopback = host == "localhost"
                    || host
                        .trim_start_matches('[')
                        .trim_end_matches(']')
                        .parse::<std::net::IpAddr>()
                        .is_ok_and(|a| a.is_loopback());
                if base.scheme() != "http" || !loopback {
                    return Err(BlobError::Invalid(format!(
                        "the credential helper must be a loopback http:// URL, not {url}"
                    )));
                }
                Source::Imds { http, base }
            }
        };
        Ok(Self {
            source,
            margin: opts.refresh_margin,
            state: Mutex::new(None),
        })
    }

    async fn get(&self, force: bool) -> Result<AwsCredentials> {
        let (http, base) = match &self.source {
            Source::Static(c) => return Ok(c.clone()),
            Source::Imds { http, base } => (http, base),
        };
        let mut st = self.state.lock().await;
        let now = SystemTime::now();
        if !force
            && let Some(h) = st.as_ref()
            && !due(h, now, self.margin)
        {
            return Ok(h.creds.clone());
        }
        let creds = fetch_imds(http, base).await?;
        if creds.expiration.is_some_and(|e| e <= now) {
            return Err(BlobError::Credentials(
                "the credential helper returned expired credentials".into(),
            ));
        }
        *st = Some(Held {
            creds: creds.clone(),
            fetched: now,
        });
        Ok(creds)
    }
}

/// Refresh when less than the margin (at most half the lifetime) is left.
fn due(h: &Held, now: SystemTime, margin: Duration) -> bool {
    let Some(exp) = h.creds.expiration else {
        return false;
    };
    let life = exp.duration_since(h.fetched).unwrap_or_default();
    let margin = margin.min(life / 2);
    match exp.checked_sub(margin) {
        Some(t) => now >= t,
        None => true,
    }
}

async fn fetch_imds(http: &reqwest::Client, base: &Url) -> Result<AwsCredentials> {
    const PATH: &str = "latest/meta-data/iam/security-credentials/";
    let err = |what: &str, e: &dyn std::fmt::Display| {
        BlobError::Credentials(format!("credential helper {what}: {e}"))
    };
    let join = |p: &str| base.join(p).map_err(|e| err("URL", &e));
    let token = http
        .put(join("latest/api/token")?)
        .header("x-aws-ec2-metadata-token-ttl-seconds", "21600")
        .timeout(Duration::from_secs(5))
        .send()
        .await
        .map_err(|e| err("token", &e.without_url()))?;
    let token = ok_text("token", token).await?;
    let role = http
        .get(join(PATH)?)
        .header("x-aws-ec2-metadata-token", &token)
        .timeout(Duration::from_secs(5))
        .send()
        .await
        .map_err(|e| err("role", &e.without_url()))?;
    let role = ok_text("role", role).await?;
    let role = role
        .lines()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .ok_or_else(|| BlobError::Credentials("credential helper named no role".into()))?
        .to_owned();
    if role.contains(['/', '?', '#']) {
        return Err(BlobError::Credentials(format!("bad role name {role:?}")));
    }
    let resp = http
        .get(join(&format!("{PATH}{role}"))?)
        .header("x-aws-ec2-metadata-token", &token)
        .timeout(Duration::from_secs(5))
        .send()
        .await
        .map_err(|e| err("credentials", &e.without_url()))?;
    let body = ok_text("credentials", resp).await?;
    let c: ImdsCreds = serde_json::from_str(&body).map_err(|e| err("credentials JSON", &e))?;
    if let Some(code) = &c.code
        && code != "Success"
    {
        return Err(BlobError::Credentials(format!(
            "credential helper says {code:?}"
        )));
    }
    let exp = time::OffsetDateTime::parse(
        &c.expiration,
        &time::format_description::well_known::Rfc3339,
    )
    .map_err(|e| err("Expiration", &e))?;
    let exp = SystemTime::UNIX_EPOCH
        + Duration::from_secs(u64::try_from(exp.unix_timestamp()).unwrap_or(0));
    if c.access_key_id.is_empty() || c.secret_access_key.is_empty() {
        return Err(BlobError::Credentials("empty credentials".into()));
    }
    Ok(AwsCredentials {
        access_key_id: c.access_key_id,
        secret_access_key: c.secret_access_key,
        session_token: c.token.filter(|t| !t.is_empty()),
        expiration: Some(exp),
    })
}

async fn ok_text(what: &str, resp: Response) -> Result<String> {
    let status = resp.status();
    let text = resp.text().await.map_err(|e| {
        BlobError::Credentials(format!("credential helper {what}: {}", e.without_url()))
    })?;
    if !status.is_success() {
        return Err(BlobError::Credentials(format!(
            "credential helper {what}: HTTP {}",
            status.as_u16()
        )));
    }
    Ok(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn obj(endpoint: &str, addressing: Addressing) -> S3Object {
        S3Object {
            bucket: "seis-raw".into(),
            key: "lines/line 1042+x.segy".into(),
            version_id: Some("3HL4kqtJlcpXroDTDmJ+rmSpXd3dIbrHY".into()),
            region: "eu-west-1".into(),
            endpoint: endpoint.into(),
            addressing,
            tls12_only: true,
            requester_pays: false,
            etag: None,
            private_network: true,
        }
    }

    fn client(test_endpoints: Vec<String>) -> S3Client {
        S3Client::new(S3Options {
            credentials: CredentialSource::Static(AwsCredentials {
                access_key_id: "AKID".into(),
                secret_access_key: "secret".into(),
                session_token: None,
                expiration: None,
            }),
            test_endpoints,
            ..S3Options::default()
        })
        .unwrap()
    }

    /// AWS's published example ("Signature Calculations for the
    /// Authorization Header", Example: GET Object): a ranged GET of
    /// `/test.txt` from `examplebucket`.
    #[test]
    fn sigv4_matches_aws_get_object_example() {
        let creds = AwsCredentials {
            access_key_id: "AKIAIOSFODNN7EXAMPLE".into(),
            secret_access_key: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY".into(),
            session_token: None,
            expiration: None,
        };
        // 2013-05-24T00:00:00Z
        let t = SystemTime::UNIX_EPOCH + Duration::from_secs(1_369_353_600);
        let headers = vec![
            (
                "host".to_owned(),
                "examplebucket.s3.amazonaws.com".to_owned(),
            ),
            ("range".to_owned(), "bytes=0-9".to_owned()),
        ];
        let out = sign_request(
            "GET",
            "https://examplebucket.s3.amazonaws.com/test.txt",
            &headers,
            &creds,
            "us-east-1",
            t,
        )
        .unwrap();
        let get = |n: &str| {
            out.iter()
                .find(|(k, _)| k.eq_ignore_ascii_case(n))
                .map(|(_, v)| v.as_str())
        };
        assert_eq!(get("x-amz-date"), Some("20130524T000000Z"));
        assert_eq!(
            get("x-amz-content-sha256"),
            Some("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        );
        assert_eq!(
            get("authorization"),
            Some(
                "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
            )
        );
    }

    #[test]
    fn session_token_is_signed() {
        let creds = AwsCredentials {
            access_key_id: "ASIA".into(),
            secret_access_key: "s".into(),
            session_token: Some("tok".into()),
            expiration: None,
        };
        let headers = vec![("host".to_owned(), "b.e".to_owned())];
        let out = sign_request(
            "GET",
            "https://b.e/k",
            &headers,
            &creds,
            "eu-west-1",
            SystemTime::now(),
        )
        .unwrap();
        assert!(
            out.iter()
                .any(|(k, v)| k == "x-amz-security-token" && v == "tok")
        );
        let auth = &out.iter().find(|(k, _)| k == "authorization").unwrap().1;
        assert!(auth.contains("x-amz-security-token"), "{auth}");
    }

    #[test]
    fn virtual_host_on_interface_endpoint() {
        let c = client(vec![]);
        let u = c
            .object_url(&obj(
                "vpce-0a1b2c3d-4e5f.s3.eu-west-1.vpce.amazonaws.com",
                Addressing::VirtualHost,
            ))
            .unwrap();
        assert_eq!(
            u.as_str(),
            "https://seis-raw.vpce-0a1b2c3d-4e5f.s3.eu-west-1.vpce.amazonaws.com/lines/line%201042%2Bx.segy?versionId=3HL4kqtJlcpXroDTDmJ%2BrmSpXd3dIbrHY"
        );
        // A leading `<bucket>.` in the endpoint is not doubled.
        let u = c
            .object_url(&obj(
                "seis-raw.vpce-0a1b2c3d-4e5f.s3.eu-west-1.vpce.amazonaws.com",
                Addressing::VirtualHost,
            ))
            .unwrap();
        assert_eq!(
            u.host_str(),
            Some("seis-raw.vpce-0a1b2c3d-4e5f.s3.eu-west-1.vpce.amazonaws.com")
        );
        let u = c
            .object_url(&obj(
                "vpce-0a1b2c3d-4e5f.s3.eu-west-1.vpce.amazonaws.com",
                Addressing::Path,
            ))
            .unwrap();
        assert!(u.path().starts_with("/seis-raw/lines/"), "{u}");
    }

    #[test]
    fn public_hosts_are_refused() {
        let c = client(vec!["127.0.0.1:9000".into()]);
        for e in [
            "s3.amazonaws.com",
            "s3.eu-west-1.amazonaws.com",
            "seis-raw.s3.eu-west-1.amazonaws.com",
            "vpce.amazonaws.com.evil.example",
            "x.vpce.amazonaws.com:8443",
            "https://x.vpce.amazonaws.com",
            "x.vpce.amazonaws.com/path",
            "",
            "127.0.0.1:9001",
        ] {
            let r = c.object_url(&obj(e, Addressing::Path));
            assert!(r.is_err(), "{e:?} accepted: {r:?}");
        }
        assert!(
            c.object_url(&obj("127.0.0.1:9000", Addressing::Path))
                .is_ok()
        );
    }

    /// Without private networking a public S3 host is a valid endpoint
    /// (still 443 only, still a plain DNS name).
    #[test]
    fn public_hosts_only_without_private_networking() {
        let c = client(vec![]);
        let mut o = obj("s3.eu-west-1.amazonaws.com", Addressing::VirtualHost);
        assert!(c.object_url(&o).is_err(), "private by default");
        o.private_network = false;
        let u = c.object_url(&o).unwrap();
        assert_eq!(u.host_str(), Some("seis-raw.s3.eu-west-1.amazonaws.com"));
        for e in [
            "s3.eu-west-1.amazonaws.com:8443",
            "evil.example\\.s3.amazonaws.com",
            "user@s3.amazonaws.com",
        ] {
            o.endpoint = e.into();
            assert!(c.object_url(&o).is_err(), "{e:?} accepted");
        }
    }

    /// Review S2: the URL parser treats `\\` as `/`, so an endpoint (or
    /// bucket) carrying one named a public host while the suffix check
    /// passed. Every odd character is refused before a URL is built, and the
    /// parsed URL's host is checked again.
    #[test]
    fn no_endpoint_or_bucket_can_smuggle_another_host() {
        let c = client(vec!["127.0.0.1:9000".into(), "s3.sim.test:9000".into()]);
        let ep = "vpce-1.s3.eu-west-1.vpce.amazonaws.com";
        // The review's probe, as endpoint and as bucket.
        let probe = "evil.example\\.vpce-1.s3.eu-west-1.vpce.amazonaws.com";
        assert_eq!(
            Url::parse(&format!("https://{probe}/key"))
                .unwrap()
                .host_str(),
            Some("evil.example"),
            "the parser really does this"
        );
        let mut bad_endpoints: Vec<String> = vec![probe.into()];
        let mut bad_buckets: Vec<String> = vec!["evil.example\\".into()];
        for odd in [
            "\\", "%2e", "%40", "%2F", "@", "user@", "user:pw@", "#", "?", "/", "\t", " ", "\n",
            "\u{0}", "\u{2024}", "\u{FF0E}", "ü", "[::1]", "[", "]", "..", "-", ";", "&", "'",
            "\"", "Evil", "_",
        ] {
            bad_endpoints.push(format!(
                "evil.example{odd}.vpce-1.s3.eu-west-1.vpce.amazonaws.com"
            ));
            bad_endpoints.push(format!("{odd}{ep}"));
            if odd != "-" {
                bad_buckets.push(format!("seis{odd}raw"));
            }
            bad_buckets.push(format!("evil.example{odd}"));
        }
        bad_endpoints.extend(
            [
                "[::1]",
                "[::1]:9000",
                "vpce.amazonaws.com",
                ".vpce.amazonaws.com",
                "x..vpce.amazonaws.com",
                "-x.vpce.amazonaws.com",
                "x.vpce.amazonaws.com.",
                "127.0.0.1:9000/",
                "s3.sim.test:9000@evil.example",
            ]
            .map(String::from),
        );
        for e in &bad_endpoints {
            for a in [Addressing::VirtualHost, Addressing::Path] {
                let r = c.object_url(&obj(e, a));
                assert!(r.is_err(), "endpoint {e:?} accepted: {r:?}");
            }
        }
        for b in bad_buckets
            .iter()
            .chain(["ab".to_owned(), "a".repeat(64)].iter())
        {
            for (e, a) in [
                (ep, Addressing::VirtualHost),
                (ep, Addressing::Path),
                ("s3.sim.test:9000", Addressing::VirtualHost),
            ] {
                let mut o = obj(e, a);
                o.bucket = b.clone();
                let r = c.object_url(&o);
                assert!(r.is_err(), "bucket {b:?} accepted: {r:?}");
            }
        }
        // The good ones still work, and name exactly the intended host.
        let u = c.object_url(&obj(ep, Addressing::VirtualHost)).unwrap();
        assert_eq!(
            u.host_str(),
            Some("seis-raw.vpce-1.s3.eu-west-1.vpce.amazonaws.com")
        );
        assert_eq!((u.port(), u.username()), (None, ""));
        let u = c
            .object_url(&obj("s3.sim.test:9000", Addressing::VirtualHost))
            .unwrap();
        assert_eq!(
            (u.host_str(), u.port()),
            (Some("seis-raw.s3.sim.test"), Some(9000))
        );
        let mut o = obj(ep, Addressing::Path);
        o.bucket = "a.b-c.123".into();
        assert!(c.object_url(&o).is_ok());
    }

    #[test]
    fn imds_must_be_loopback_http() {
        for url in [
            "https://127.0.0.1:9911",
            "http://169.254.169.254",
            "http://helper.example:9911",
        ] {
            let r = S3Client::new(S3Options {
                credentials: CredentialSource::Imds { url: url.into() },
                ..S3Options::default()
            });
            assert!(r.is_err(), "{url} accepted");
        }
        for url in [
            "http://127.0.0.1:9911",
            "http://localhost:9911",
            "http://[::1]:9911",
        ] {
            S3Client::new(S3Options {
                credentials: CredentialSource::Imds { url: url.into() },
                ..S3Options::default()
            })
            .unwrap_or_else(|e| panic!("{url}: {e}"));
        }
    }

    #[test]
    fn refresh_is_due_before_expiry() {
        let t0 = SystemTime::UNIX_EPOCH + Duration::from_secs(1_800_000_000);
        let h = Held {
            creds: AwsCredentials {
                access_key_id: "a".into(),
                secret_access_key: "s".into(),
                session_token: None,
                expiration: Some(t0 + Duration::from_secs(3600)),
            },
            fetched: t0,
        };
        let m = Duration::from_secs(300);
        assert!(!due(&h, t0 + Duration::from_secs(3000), m));
        assert!(due(&h, t0 + Duration::from_secs(3300), m));
        // A short-lived credential refreshes at half its life.
        let short = Held {
            creds: AwsCredentials {
                expiration: Some(t0 + Duration::from_secs(10)),
                ..h.creds.clone()
            },
            fetched: t0,
        };
        assert!(!due(&short, t0 + Duration::from_secs(4), m));
        assert!(due(&short, t0 + Duration::from_secs(5), m));
    }

    #[test]
    fn pointers_round_trip() {
        let mut o = obj("vpce-1.s3.eu-west-1.vpce.amazonaws.com", Addressing::Path);
        o.etag = Some("\"e1\"".into());
        let p = o.to_pointer();
        assert!(p.starts_with(POINTER_PREFIX));
        assert_eq!(S3Object::from_pointer(&p).unwrap(), o);
        assert!(S3Object::from_pointer("https://x/y").is_err());
        assert!(S3Object::from_pointer("s3ra:{").is_err());
    }

    #[test]
    fn key_encoding() {
        assert_eq!(encode_key("a/b c/ü+=&"), "a/b%20c/%C3%BC%2B%3D%26");
        assert_eq!(encode_query_value("a/b+"), "a%2Fb%2B");
    }

    #[test]
    fn debug_hides_secrets() {
        let c = AwsCredentials {
            access_key_id: "AKID".into(),
            secret_access_key: "TOPSECRET".into(),
            session_token: Some("SESSIONTOKEN".into()),
            expiration: None,
        };
        let s = format!("{c:?}");
        assert!(
            !s.contains("TOPSECRET") && !s.contains("SESSIONTOKEN"),
            "{s}"
        );
    }
}
