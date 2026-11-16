//! The S3 reader against scripted in-process servers: request shape
//! (virtual host, pinned version, Range, requester pays, SigV4 headers with
//! the session token), every way a response can be wrong (each one an
//! error, never zeros), the archived answer, credentials from a fake
//! `aws_signing_helper serve` (refresh before expiry, refresh on
//! `ExpiredToken`), and TLS 1.2 only for interface endpoints.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime};

use omnifile_blob::client::RetryPolicy;
use omnifile_blob::{
    Addressing, AwsCredentials, CredentialSource, RangeReader, ReadTarget, S3Client, S3Object,
    S3Options, TlsOptions,
};
use rcgen::{
    BasicConstraints, CertificateParams, DnType, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair,
    KeyUsagePurpose,
};
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::TcpListener;

// ---------------------------------------------------------------------------
// A tiny scripted HTTP/1.1 server (one request per connection)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
struct Req {
    method: String,
    target: String,
    headers: HashMap<String, String>,
}

struct Resp {
    status: u16,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl Resp {
    fn new(status: u16, body: impl Into<Vec<u8>>) -> Resp {
        Resp {
            status,
            headers: vec![],
            body: body.into(),
        }
    }
    fn header(mut self, k: &str, v: impl Into<String>) -> Resp {
        self.headers.push((k.to_owned(), v.into()));
        self
    }
}

type Handler = Arc<dyn Fn(&Req) -> Resp + Send + Sync>;

async fn read_req<S: AsyncRead + Unpin>(s: &mut S) -> Option<Req> {
    let mut buf = Vec::new();
    let mut b = [0u8; 4096];
    while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
        let n = s.read(&mut b).await.ok()?;
        if n == 0 {
            return None;
        }
        buf.extend_from_slice(&b[..n]);
    }
    let text = String::from_utf8_lossy(&buf).into_owned();
    let mut lines = text.split("\r\n");
    let first = lines.next()?;
    let mut parts = first.split(' ');
    let method = parts.next()?.to_owned();
    let target = parts.next()?.to_owned();
    let mut headers = HashMap::new();
    for l in lines {
        if l.is_empty() {
            break;
        }
        if let Some((k, v)) = l.split_once(':') {
            headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_owned());
        }
    }
    Some(Req {
        method,
        target,
        headers,
    })
}

async fn write_resp<S: AsyncWrite + Unpin>(s: &mut S, r: Resp) {
    let mut out = format!(
        "HTTP/1.1 {} X\r\nContent-Length: {}\r\nConnection: close\r\n",
        r.status,
        r.body.len()
    );
    for (k, v) in &r.headers {
        out.push_str(&format!("{k}: {v}\r\n"));
    }
    out.push_str("\r\n");
    let _ = s.write_all(out.as_bytes()).await;
    let _ = s.write_all(&r.body).await;
    let _ = s.shutdown().await;
}

/// Serve `handler` over plain HTTP; returns the address.
async fn serve(handler: Handler) -> SocketAddr {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        loop {
            let Ok((mut s, _)) = l.accept().await else {
                return;
            };
            let h = handler.clone();
            tokio::spawn(async move {
                if let Some(req) = read_req(&mut s).await {
                    let r = h(&req);
                    write_resp(&mut s, r).await;
                }
            });
        }
    });
    addr
}

/// Serve `handler` over TLS with the given protocol versions.
async fn serve_tls(
    handler: Handler,
    chain: Vec<CertificateDer<'static>>,
    key: PrivateKeyDer<'static>,
    versions: &'static [&'static rustls::SupportedProtocolVersion],
) -> SocketAddr {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let cfg = rustls::ServerConfig::builder_with_provider(provider)
        .with_protocol_versions(versions)
        .unwrap()
        .with_no_client_auth()
        .with_single_cert(chain, key)
        .unwrap();
    let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(cfg));
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        loop {
            let Ok((s, _)) = l.accept().await else {
                return;
            };
            let (h, a) = (handler.clone(), acceptor.clone());
            tokio::spawn(async move {
                let Ok(mut s) = a.accept(s).await else {
                    return;
                };
                if let Some(req) = read_req(&mut s).await {
                    let r = h(&req);
                    write_resp(&mut s, r).await;
                }
            });
        }
    });
    addr
}

struct Pki {
    ca_pem: String,
    ca_der: CertificateDer<'static>,
    chain: Vec<CertificateDer<'static>>,
    key: PrivateKeyDer<'static>,
}

/// A CA and a leaf for `*.s3.sim.test` / `s3.sim.test`.
fn pki() -> Pki {
    let ca_key = KeyPair::generate().unwrap();
    let mut p = CertificateParams::new(Vec::<String>::new()).unwrap();
    p.distinguished_name.push(DnType::CommonName, "S3 test CA");
    p.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    p.key_usages = vec![
        KeyUsagePurpose::KeyCertSign,
        KeyUsagePurpose::DigitalSignature,
    ];
    let ca = p.self_signed(&ca_key).unwrap();
    let issuer = Issuer::from_params(&p, &ca_key);
    let key = KeyPair::generate().unwrap();
    let mut lp =
        CertificateParams::new(vec!["s3.sim.test".to_owned(), "*.s3.sim.test".to_owned()]).unwrap();
    lp.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
    let leaf = lp.signed_by(&key, &issuer).unwrap();
    Pki {
        ca_pem: ca.pem(),
        ca_der: ca.der().clone(),
        chain: vec![leaf.der().clone(), ca.der().clone()],
        key: PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(key.serialize_der())),
    }
}

// ---------------------------------------------------------------------------
// A fake `aws_signing_helper serve` (IMDSv2)
// ---------------------------------------------------------------------------

struct Imds {
    addr: SocketAddr,
    fetches: Arc<AtomicUsize>,
}

/// Credentials valid for `ttl` from each fetch; tokens required.
async fn fake_imds(ttl: Duration) -> Imds {
    let fetches = Arc::new(AtomicUsize::new(0));
    let f = fetches.clone();
    let h: Handler = Arc::new(move |r: &Req| {
        if r.method == "PUT" && r.target == "/latest/api/token" {
            if !r
                .headers
                .contains_key("x-aws-ec2-metadata-token-ttl-seconds")
            {
                return Resp::new(400, "");
            }
            return Resp::new(200, "TOKEN123");
        }
        if r.headers
            .get("x-aws-ec2-metadata-token")
            .map(String::as_str)
            != Some("TOKEN123")
        {
            return Resp::new(401, "");
        }
        match r.target.as_str() {
            "/latest/meta-data/iam/security-credentials/" => Resp::new(200, "omnifile-edge\n"),
            "/latest/meta-data/iam/security-credentials/omnifile-edge" => {
                let n = f.fetch_add(1, Ordering::SeqCst) + 1;
                let exp = time::OffsetDateTime::from(SystemTime::now() + ttl)
                    .format(&time::format_description::well_known::Rfc3339)
                    .unwrap();
                Resp::new(
                    200,
                    serde_json::json!({
                        "Code": "Success", "Type": "AWS-HMAC",
                        "AccessKeyId": format!("ASIATEST{n}"),
                        "SecretAccessKey": "secret", "Token": format!("session-{n}"),
                        "Expiration": exp, "LastUpdated": exp,
                    })
                    .to_string(),
                )
            }
            _ => Resp::new(404, ""),
        }
    });
    let addr = serve(h).await;
    Imds { addr, fetches }
}

// ---------------------------------------------------------------------------

const BUCKET: &str = "seis";
const VERSION: &str = "v-0042";

fn data(n: usize) -> Vec<u8> {
    (0..n).map(|i| (i % 251) as u8).collect()
}

/// An S3 object handler over `body`, recording every request.
fn object_handler(body: Vec<u8>, log: Arc<Mutex<Vec<Req>>>) -> Handler {
    Arc::new(move |r: &Req| {
        log.lock().unwrap().push(r.clone());
        let Some(range) = r.headers.get("range") else {
            return Resp::new(400, "");
        };
        let (a, b) = range
            .strip_prefix("bytes=")
            .and_then(|s| s.split_once('-'))
            .unwrap();
        let (a, b): (usize, usize) = (a.parse().unwrap(), b.parse().unwrap());
        Resp::new(206, body[a..=b].to_vec())
            .header("Content-Range", format!("bytes {a}-{b}/{}", body.len()))
            .header("x-amz-version-id", VERSION)
    })
}

fn opts(
    creds: CredentialSource,
    endpoint: &str,
    resolve: Vec<(String, SocketAddr)>,
    tls: Option<&Pki>,
) -> S3Options {
    S3Options {
        credentials: creds,
        retry: RetryPolicy {
            max_attempts: 2,
            base_delay: Duration::from_millis(1),
            max_delay: Duration::from_millis(5),
        },
        allow_http: tls.is_none(),
        tls: match tls {
            Some(p) => TlsOptions::pinned(p.ca_pem.clone()),
            None => TlsOptions::public(),
        },
        test_endpoints: vec![endpoint.to_owned()],
        resolve,
        ..S3Options::default()
    }
}

fn static_creds() -> CredentialSource {
    CredentialSource::Static(AwsCredentials {
        access_key_id: "AKIASIMEDGE000000000".into(),
        secret_access_key: "simsecret".into(),
        session_token: None,
        expiration: None,
    })
}

fn object(endpoint: &str, addressing: Addressing) -> S3Object {
    S3Object {
        bucket: BUCKET.into(),
        key: "lines/line 1042.segy".into(),
        version_id: Some(VERSION.into()),
        region: "eu-west-1".into(),
        endpoint: endpoint.into(),
        addressing,
        tls12_only: false,
        requester_pays: true,
        private_network: true,
        etag: None,
    }
}

#[tokio::test]
async fn virtual_host_ranged_get_with_session_credentials() {
    let imds = fake_imds(Duration::from_secs(3600)).await;
    let log = Arc::new(Mutex::new(Vec::new()));
    let body = data(10 << 20);
    let addr = serve(object_handler(body.clone(), log.clone())).await;
    let endpoint = format!("s3.sim.test:{}", addr.port());
    let c = S3Client::new(opts(
        CredentialSource::Imds {
            url: format!("http://{}", imds.addr),
        },
        &endpoint,
        vec![(format!("{BUCKET}.s3.sim.test"), addr)],
        None,
    ))
    .unwrap();
    let o = object(&endpoint, Addressing::VirtualHost);
    let got = c.get_range(&o, 5 << 20, 4 << 20).await.unwrap();
    assert_eq!(&got[..], &body[5 << 20..9 << 20]);
    let got = c.get_range(&o, 0, 1).await.unwrap();
    assert_eq!(&got[..], &body[..1]);
    assert_eq!(imds.fetches.load(Ordering::SeqCst), 1, "credentials cached");

    let reqs = log.lock().unwrap().clone();
    let r = &reqs[0];
    assert_eq!(r.method, "GET");
    assert_eq!(r.target, "/lines/line%201042.segy?versionId=v-0042");
    assert_eq!(
        r.headers["host"],
        format!("{BUCKET}.s3.sim.test:{}", addr.port())
    );
    assert_eq!(
        r.headers["range"],
        format!("bytes={}-{}", 5 << 20, (9 << 20) - 1)
    );
    assert_eq!(r.headers["x-amz-request-payer"], "requester");
    assert_eq!(r.headers["x-amz-security-token"], "session-1");
    let auth = &r.headers["authorization"];
    assert!(
        auth.starts_with("AWS4-HMAC-SHA256 Credential=ASIATEST1/")
            && auth.contains("/eu-west-1/s3/aws4_request"),
        "{auth}"
    );
    for h in [
        "host",
        "range",
        "x-amz-request-payer",
        "x-amz-security-token",
        "x-amz-date",
        "x-amz-content-sha256",
    ] {
        assert!(auth.contains(h), "{h} not signed: {auth}");
    }
}

#[tokio::test]
async fn path_addressing() {
    let log = Arc::new(Mutex::new(Vec::new()));
    let addr = serve(object_handler(data(100), log.clone())).await;
    let endpoint = addr.to_string();
    let c = S3Client::new(opts(static_creds(), &endpoint, vec![], None)).unwrap();
    let mut o = object(&endpoint, Addressing::Path);
    o.requester_pays = false;
    c.get_range(&o, 10, 10).await.unwrap();
    let r = log.lock().unwrap()[0].clone();
    assert_eq!(r.target, "/seis/lines/line%201042.segy?versionId=v-0042");
    assert!(!r.headers.contains_key("x-amz-request-payer"));
    assert!(!r.headers.contains_key("x-amz-security-token"));
}

/// Every wrong answer is an error; nothing short, foreign or unversioned
/// is ever returned.
#[tokio::test]
async fn wrong_responses_are_errors() {
    let cases: Vec<(&str, Handler)> = vec![
        (
            "short body",
            Arc::new(|_: &Req| {
                Resp::new(206, vec![1u8; 5])
                    .header("Content-Range", "bytes 0-9/100")
                    .header("x-amz-version-id", VERSION)
            }),
        ),
        (
            "other version",
            Arc::new(|_: &Req| {
                Resp::new(206, vec![1u8; 10])
                    .header("Content-Range", "bytes 0-9/100")
                    .header("x-amz-version-id", "v-other")
            }),
        ),
        (
            "no version header",
            Arc::new(|_: &Req| {
                Resp::new(206, vec![1u8; 10]).header("Content-Range", "bytes 0-9/100")
            }),
        ),
        (
            "wrong content-range",
            Arc::new(|_: &Req| {
                Resp::new(206, vec![1u8; 10])
                    .header("Content-Range", "bytes 10-19/100")
                    .header("x-amz-version-id", VERSION)
            }),
        ),
        (
            "missing content-range",
            Arc::new(|_: &Req| Resp::new(206, vec![1u8; 10]).header("x-amz-version-id", VERSION)),
        ),
        (
            "200 with the whole object",
            Arc::new(|_: &Req| Resp::new(200, vec![1u8; 100]).header("x-amz-version-id", VERSION)),
        ),
        (
            "server error",
            Arc::new(|_: &Req| Resp::new(500, "<Error><Code>InternalError</Code></Error>")),
        ),
    ];
    for (name, h) in cases {
        let addr = serve(h).await;
        let endpoint = addr.to_string();
        let c = S3Client::new(opts(static_creds(), &endpoint, vec![], None)).unwrap();
        let r = c
            .get_range(&object(&endpoint, Addressing::Path), 0, 10)
            .await;
        assert!(r.is_err(), "{name}: accepted {r:?}");
    }
}

#[tokio::test]
async fn archived_object_is_reported_as_archived() {
    let h: Handler = Arc::new(|_: &Req| {
        Resp::new(
            403,
            "<?xml version=\"1.0\"?><Error><Code>InvalidObjectState</Code><Message>The operation is not valid for the object's storage class</Message></Error>",
        )
        .header("x-amz-request-id", "R1")
    });
    let addr = serve(h).await;
    let endpoint = addr.to_string();
    let c = S3Client::new(opts(static_creds(), &endpoint, vec![], None)).unwrap();
    let e = c
        .get_range(&object(&endpoint, Addressing::Path), 0, 10)
        .await
        .unwrap_err();
    assert!(e.is_archived(), "{e}");
    assert!(!e.is_auth());
    assert!(!e.is_retryable());
}

#[tokio::test]
async fn expired_session_forces_one_refresh() {
    let imds = fake_imds(Duration::from_secs(3600)).await;
    let calls = Arc::new(AtomicUsize::new(0));
    let c2 = calls.clone();
    let h: Handler = Arc::new(move |r: &Req| {
        c2.fetch_add(1, Ordering::SeqCst);
        // The first session is "expired" at S3.
        if r.headers.get("x-amz-security-token").map(String::as_str) == Some("session-1") {
            return Resp::new(
                400,
                "<Error><Code>ExpiredToken</Code><Message>x</Message></Error>",
            );
        }
        Resp::new(206, vec![7u8; 10])
            .header("Content-Range", "bytes 0-9/10")
            .header("x-amz-version-id", VERSION)
    });
    let addr = serve(h).await;
    let endpoint = addr.to_string();
    let c = S3Client::new(opts(
        CredentialSource::Imds {
            url: format!("http://{}", imds.addr),
        },
        &endpoint,
        vec![],
        None,
    ))
    .unwrap();
    let got = c
        .get_range(&object(&endpoint, Addressing::Path), 0, 10)
        .await
        .unwrap();
    assert_eq!(&got[..], &[7u8; 10]);
    assert_eq!(imds.fetches.load(Ordering::SeqCst), 2);
    assert_eq!(calls.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn credentials_refresh_before_expiry() {
    let imds = fake_imds(Duration::from_secs(4)).await;
    let c = S3Client::new(S3Options {
        credentials: CredentialSource::Imds {
            url: format!("http://{}", imds.addr),
        },
        ..S3Options::default()
    })
    .unwrap();
    let a = c.credentials().await.unwrap();
    let b = c.credentials().await.unwrap();
    assert_eq!(a, b);
    assert_eq!(imds.fetches.load(Ordering::SeqCst), 1);
    // Past half the lifetime: refreshed before it expires.
    tokio::time::sleep(Duration::from_millis(2_200)).await;
    let d = c.credentials().await.unwrap();
    assert_ne!(a.access_key_id, d.access_key_id);
    assert!(d.expiration.unwrap() > SystemTime::now());
    assert_eq!(imds.fetches.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn credential_helper_down_is_an_error() {
    let c = S3Client::new(S3Options {
        credentials: CredentialSource::Imds {
            url: "http://127.0.0.1:9".into(),
        },
        test_endpoints: vec!["127.0.0.1:9".into()],
        allow_http: true,
        ..S3Options::default()
    })
    .unwrap();
    let e = c
        .get_range(&object("127.0.0.1:9", Addressing::Path), 0, 10)
        .await
        .unwrap_err();
    assert!(e.to_string().contains("credential"), "{e}");
}

static TLS12_ONLY: &[&rustls::SupportedProtocolVersion] = &[&rustls::version::TLS12];
static TLS13_ONLY: &[&rustls::SupportedProtocolVersion] = &[&rustls::version::TLS13];

#[tokio::test]
async fn tls12_only_endpoint() {
    let p = pki();
    let log = Arc::new(Mutex::new(Vec::new()));
    let body = data(64 << 10);
    // A server that speaks TLS 1.2 only, like an S3 interface endpoint.
    let addr = serve_tls(
        object_handler(body.clone(), log.clone()),
        p.chain.clone(),
        p.key.clone_key(),
        TLS12_ONLY,
    )
    .await;
    let endpoint = format!("s3.sim.test:{}", addr.port());
    let resolve = vec![(format!("{BUCKET}.s3.sim.test"), addr)];
    let c = S3Client::new(opts(static_creds(), &endpoint, resolve.clone(), Some(&p))).unwrap();
    let mut o = object(&endpoint, Addressing::VirtualHost);
    o.tls12_only = true;
    let got = c.get_range(&o, 1000, 4000).await.unwrap();
    assert_eq!(&got[..], &body[1000..5000]);

    // A server that refuses 1.2: the 1.2-only client cannot connect.
    let addr13 = serve_tls(
        object_handler(body.clone(), log.clone()),
        p.chain.clone(),
        p.key.clone_key(),
        TLS13_ONLY,
    )
    .await;
    let endpoint13 = format!("s3.sim.test:{}", addr13.port());
    let c13 = S3Client::new(opts(
        static_creds(),
        &endpoint13,
        vec![(format!("{BUCKET}.s3.sim.test"), addr13)],
        Some(&p),
    ))
    .unwrap();
    let mut o13 = object(&endpoint13, Addressing::VirtualHost);
    o13.tls12_only = true;
    assert!(c13.get_range(&o13, 0, 10).await.is_err());
    // Without the 1.2 pin it negotiates 1.3.
    o13.tls12_only = false;
    c13.get_range(&o13, 0, 10).await.unwrap();
    let _ = p.ca_der;
}

#[tokio::test]
async fn range_reader_dispatches_by_target() {
    let log = Arc::new(Mutex::new(Vec::new()));
    let body = data(1 << 20);
    let addr = serve(object_handler(body.clone(), log)).await;
    let endpoint = addr.to_string();
    let r = omnifile_blob::EdgeReader::new(
        omnifile_blob::BlobClient::new(Default::default()).unwrap(),
        S3Client::new(opts(static_creds(), &endpoint, vec![], None)).unwrap(),
    );
    let t = ReadTarget::S3(object(&endpoint, Addressing::Path));
    assert!(!t.verifies_ranges());
    let got = r.read_range(&t, 4096, 8192).await.unwrap();
    assert_eq!(&got[..], &body[4096..4096 + 8192]);
    let e = r
        .read_range(&ReadTarget::Unsupported { mode: "nas".into() }, 0, 1)
        .await
        .unwrap_err();
    assert!(e.is_unsupported());
}

/// Unversioned objects are pinned by ETag: `If-Match` is sent (and signed),
/// a `412` is "the source changed" and is not retried, and an object with
/// neither a version id nor an ETag is never requested.
#[tokio::test]
async fn unversioned_objects_are_pinned_by_etag() {
    let calls = Arc::new(Mutex::new(Vec::<Req>::new()));
    let c2 = calls.clone();
    let h: Handler = Arc::new(move |r: &Req| {
        c2.lock().unwrap().push(r.clone());
        match r.headers.get("if-match").map(String::as_str) {
            Some("\"e1\"") => {
                Resp::new(206, vec![5u8; 10]).header("Content-Range", "bytes 0-9/100")
            }
            _ => Resp::new(
                412,
                "<Error><Code>PreconditionFailed</Code><Message>At least one of the pre-conditions you specified did not hold</Message><Condition>If-Match</Condition></Error>",
            ),
        }
    });
    let addr = serve(h).await;
    let endpoint = addr.to_string();
    let c = S3Client::new(opts(static_creds(), &endpoint, vec![], None)).unwrap();
    let mut o = object(&endpoint, Addressing::Path);
    o.version_id = None;
    o.requester_pays = false;
    // Unquoted in the catalog: sent quoted.
    o.etag = Some("e1".into());
    let got = c.get_range(&o, 0, 10).await.unwrap();
    assert_eq!(&got[..], &[5u8; 10]);
    let r = calls.lock().unwrap()[0].clone();
    assert!(!r.target.contains("versionId"), "{}", r.target);
    assert_eq!(r.headers["if-match"], "\"e1\"");
    assert!(
        r.headers["authorization"].contains("if-match"),
        "If-Match is signed"
    );
    // The source was overwritten: 412, once (no retry), reported as such.
    o.etag = Some("\"e0\"".into());
    let e = c.get_range(&o, 0, 10).await.unwrap_err();
    assert!(e.is_source_changed(), "{e}");
    assert!(!e.is_archived() && !e.is_auth() && !e.is_retryable());
    assert_eq!(calls.lock().unwrap().len(), 2);
    // Neither a version id nor an ETag: refused before any request.
    for etag in [None, Some(String::new()), Some("  ".into())] {
        o.etag = etag;
        assert!(!o.is_pinned());
        let e = c.get_range(&o, 0, 10).await.unwrap_err();
        assert!(matches!(e, omnifile_blob::BlobError::Unpinned(_)), "{e}");
    }
    assert_eq!(calls.lock().unwrap().len(), 2, "nothing requested");
    // With a version id the ETag is sent too (both must hold).
    let mut v = object(&endpoint, Addressing::Path);
    v.requester_pays = false;
    v.etag = Some("e1".into());
    let _ = c.get_range(&v, 0, 10).await;
    let r = calls.lock().unwrap()[2].clone();
    assert!(r.target.contains("versionId=v-0042") && r.headers["if-match"] == "\"e1\"");
}
