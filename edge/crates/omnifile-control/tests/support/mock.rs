//! A small in-process mock of the control API, over real TLS:
//!
//! * an `enrol` listener (server TLS only) and an `api` listener that
//!   requires a client certificate from the mock's CA;
//! * a test PKI (CA, server certificate with the CA in its chain, CSR signing
//!   as `enrol` does: subject ignored, SAN `urn:omnifile:edge:<id>`);
//! * idempotency as in control API §8 (same key and body: replay; same key,
//!   other body: 422);
//! * **every request body is validated against `control-api.yaml`**, so each
//!   test also checks the client's requests against the contract.

#![allow(dead_code)]

use std::collections::{HashMap, HashSet};

use base64::Engine as _;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime};

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::{Request, Response};
use hyper_util::rt::TokioIo;
use rcgen::{
    BasicConstraints, CertificateParams, CertificateSigningRequestParams, DnType,
    ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair, KeyUsagePurpose, SanType,
};
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use serde_json::{Value, json};
use tokio::net::TcpListener;
use tokio_rustls::TlsAcceptor;

use omnifile_control::util::{format_time, sha256_hex};

// ---------------------------------------------------------------------------
// PKI
// ---------------------------------------------------------------------------

pub struct Pki {
    ca_params: CertificateParams,
    ca_key: KeyPair,
    pub ca_der: CertificateDer<'static>,
    pub ca_pem: String,
}

impl Pki {
    pub fn new(name: &str) -> Self {
        let ca_key = KeyPair::generate().unwrap();
        let mut p = CertificateParams::new(Vec::<String>::new()).unwrap();
        p.distinguished_name.push(DnType::CommonName, name);
        p.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        p.key_usages = vec![
            KeyUsagePurpose::KeyCertSign,
            KeyUsagePurpose::CrlSign,
            KeyUsagePurpose::DigitalSignature,
        ];
        let cert = p.self_signed(&ca_key).unwrap();
        Self {
            ca_der: cert.der().clone(),
            ca_pem: cert.pem(),
            ca_params: p,
            ca_key,
        }
    }

    pub fn ca_sha256(&self) -> String {
        sha256_hex(&self.ca_der)
    }

    fn issuer(&self) -> Issuer<'_, &KeyPair> {
        Issuer::from_params(&self.ca_params, &self.ca_key)
    }

    /// Server certificate for localhost / 127.0.0.1: (chain leaf+CA, key).
    pub fn server_cert(&self) -> (Vec<CertificateDer<'static>>, PrivateKeyDer<'static>) {
        let key = KeyPair::generate().unwrap();
        let mut p = CertificateParams::new(vec!["localhost".to_owned()]).unwrap();
        p.subject_alt_names
            .push(SanType::IpAddress(IpAddr::V4(Ipv4Addr::LOCALHOST)));
        p.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
        let cert = p.signed_by(&key, &self.issuer()).unwrap();
        (
            vec![cert.der().clone(), self.ca_der.clone()],
            PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(key.serialize_der())),
        )
    }

    /// Sign a CSR the way `enrol` does: the CSR's subject is ignored.
    pub fn sign_csr(&self, csr_pem: &str, edge_id: u64, lifetime: Duration) -> (String, String) {
        let csr = CertificateSigningRequestParams::from_pem(csr_pem).expect("CSR parses");
        let mut p = CertificateParams::new(Vec::<String>::new()).unwrap();
        p.distinguished_name
            .push(DnType::CommonName, format!("omnifile-edge-{edge_id}"));
        p.subject_alt_names = vec![SanType::URI(
            format!("urn:omnifile:edge:{edge_id}").try_into().unwrap(),
        )];
        p.extended_key_usages = vec![ExtendedKeyUsagePurpose::ClientAuth];
        let now = time::OffsetDateTime::now_utc();
        p.not_before = now - time::Duration::hours(1);
        let lifetime = time::Duration::try_from(lifetime).unwrap();
        p.not_after = now + lifetime;
        let cert = p.signed_by(&csr.public_key, &self.issuer()).unwrap();
        let not_after = format_time(SystemTime::from(now + lifetime));
        (cert.pem(), not_after)
    }
}

// ---------------------------------------------------------------------------
// Contract validation
// ---------------------------------------------------------------------------

pub fn spec() -> Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../service/openapi/control-api.yaml");
    let text = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    serde_yaml_ng::from_str(&text).expect("control-api.yaml parses")
}

/// A JSON Schema for `schema`, with the document's components attached so
/// `#/components/...` references resolve.
pub fn schema_with_components(spec: &Value, schema: &Value) -> Value {
    let mut s = schema.clone();
    if let Value::Object(m) = &mut s {
        m.insert("components".into(), spec["components"].clone());
    } else {
        s = json!({"allOf": [schema], "components": spec["components"]});
    }
    s
}

pub fn validate(spec: &Value, schema: &Value, instance: &Value) -> Result<(), String> {
    let full = schema_with_components(spec, schema);
    let v = jsonschema::validator_for(&full).map_err(|e| format!("schema: {e}"))?;
    let errors: Vec<String> = v
        .iter_errors(instance)
        .map(|e| format!("{} at {}", e, e.instance_path()))
        .collect();
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; "))
    }
}

/// Match a concrete path to a spec path template.
pub fn find_template<'a>(spec: &'a Value, path: &str) -> Option<&'a str> {
    let segs: Vec<&str> = path.trim_matches('/').split('/').collect();
    spec["paths"].as_object()?.keys().find_map(|t| {
        let ts: Vec<&str> = t.trim_matches('/').split('/').collect();
        (ts.len() == segs.len()
            && ts
                .iter()
                .zip(&segs)
                .all(|(a, b)| (a.starts_with('{') && a.ends_with('}')) || a == b))
        .then_some(t.as_str())
    })
}

fn request_schema<'a>(spec: &'a Value, method: &str, template: &str) -> Option<&'a Value> {
    let s =
        &spec["paths"][template][method]["requestBody"]["content"]["application/json"]["schema"];
    (!s.is_null()).then_some(s)
}

// ---------------------------------------------------------------------------
// Mock state
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
pub struct Recorded {
    pub method: String,
    pub path: String,
    pub query: String,
    pub idempotency_key: Option<String>,
    pub body: Value,
    pub edge_id: Option<u64>,
}

pub struct State {
    pub tokens: HashSet<String>,
    pub certs: HashMap<String, u64>,
    pub next_id: u64,
    pub requests: Vec<Recorded>,
    pub contract_violations: Vec<String>,
    idem: HashMap<(Option<u64>, String), (String, u16, String)>,
    /// Feed events; cursor "0.N" means N events consumed.
    pub feed: Vec<Value>,
    /// `next` jumps here on an empty page (events this edge doesn't receive).
    pub feed_horizon: u64,
    pub acks: Vec<String>,
    /// Respond 503 `unavailable` to the next N api requests.
    pub fail_next: u32,
    /// Accept requests and never answer them (a hung service behind a live socket).
    pub hang: bool,
    /// Respond 503 to everything while set.
    pub down: bool,
    /// URL returned in write credentials; `None`: a fake URL.
    pub upload_sas: Option<String>,
    /// Lifetime of minted SAS (`cache_until` = now + this − 5 min).
    pub sas_ttl: Duration,
    /// BLAKE3 values that dedupe.
    pub dedupe: HashSet<String>,
    pub conflict_uploads: bool,
    pub stub_refuse: bool,
    pub recall_online: bool,
    pub cert_lifetime: Duration,
    pub api_url: String,
    pub completes: Vec<(u64, Value)>,
    /// `aws-certificate` answers a certificate for another key (a broken or
    /// hostile service), which the edge must refuse.
    pub aws_wrong_key: bool,
}

pub struct Mock {
    pub state: Arc<Mutex<State>>,
    pub pki: Arc<Pki>,
    pub api_url: String,
    pub enrol_url: String,
    tasks: Vec<tokio::task::JoinHandle<()>>,
}

impl Drop for Mock {
    fn drop(&mut self) {
        for t in &self.tasks {
            t.abort();
        }
    }
}

impl Mock {
    pub async fn start() -> Mock {
        let _ = rustls::crypto::ring::default_provider().install_default();
        let pki = Arc::new(Pki::new("OmniFile test CA"));
        let spec = Arc::new(spec());
        let (chain, key) = pki.server_cert();
        let provider = Arc::new(rustls::crypto::ring::default_provider());

        // enrol: server auth only.
        let enrol_cfg = rustls::ServerConfig::builder_with_provider(provider.clone())
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(chain.clone(), key.clone_key())
            .unwrap();
        // api: client certificate from the CA required.
        let mut roots = rustls::RootCertStore::empty();
        roots.add(pki.ca_der.clone()).unwrap();
        let verifier = rustls::server::WebPkiClientVerifier::builder_with_provider(
            Arc::new(roots),
            provider.clone(),
        )
        .build()
        .unwrap();
        let api_cfg = rustls::ServerConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_client_cert_verifier(verifier)
            .with_single_cert(chain, key)
            .unwrap();

        let enrol_l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let api_l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let enrol_url = format!("https://localhost:{}", enrol_l.local_addr().unwrap().port());
        let api_url = format!("https://localhost:{}", api_l.local_addr().unwrap().port());

        let state = Arc::new(Mutex::new(State {
            tokens: HashSet::new(),
            certs: HashMap::new(),
            next_id: 100,
            requests: Vec::new(),
            contract_violations: Vec::new(),
            idem: HashMap::new(),
            feed: Vec::new(),
            feed_horizon: 0,
            acks: Vec::new(),
            fail_next: 0,
            hang: false,
            down: false,
            upload_sas: None,
            sas_ttl: Duration::from_secs(3600),
            dedupe: HashSet::new(),
            conflict_uploads: false,
            stub_refuse: false,
            recall_online: false,
            cert_lifetime: Duration::from_secs(30 * 86400),
            api_url: api_url.clone(),
            completes: Vec::new(),
            aws_wrong_key: false,
        }));
        let tasks = vec![
            tokio::spawn(serve(
                enrol_l,
                TlsAcceptor::from(Arc::new(enrol_cfg)),
                state.clone(),
                pki.clone(),
                spec.clone(),
                true,
            )),
            tokio::spawn(serve(
                api_l,
                TlsAcceptor::from(Arc::new(api_cfg)),
                state.clone(),
                pki.clone(),
                spec,
                false,
            )),
        ];
        Mock {
            state,
            pki,
            api_url,
            enrol_url,
            tasks,
        }
    }

    /// An enrolment bundle for a fresh one-time token.
    pub fn bundle(&self) -> String {
        use base64::Engine as _;
        let token = format!("tok-{}", "x".repeat(40)) + &fastrand::u64(..).to_string();
        self.state.lock().unwrap().tokens.insert(token.clone());
        let j = json!({"url": self.enrol_url, "server_ca_sha256": self.pki.ca_sha256(), "token": token});
        format!(
            "ofe1.{}",
            base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(j.to_string())
        )
    }

    pub fn requests(&self, path_suffix: &str) -> Vec<Recorded> {
        self.state
            .lock()
            .unwrap()
            .requests
            .iter()
            .filter(|r| r.path.ends_with(path_suffix))
            .cloned()
            .collect()
    }

    pub fn assert_contract(&self) {
        let v = self.state.lock().unwrap().contract_violations.clone();
        assert!(v.is_empty(), "requests violating control-api.yaml: {v:#?}");
    }
}

async fn serve(
    l: TcpListener,
    acceptor: TlsAcceptor,
    state: Arc<Mutex<State>>,
    pki: Arc<Pki>,
    spec: Arc<Value>,
    enrol: bool,
) {
    loop {
        let Ok((tcp, _peer)) = l.accept().await else {
            continue;
        };
        let acceptor = acceptor.clone();
        let (state, pki, spec) = (state.clone(), pki.clone(), spec.clone());
        tokio::spawn(async move {
            let Ok(tls) = acceptor.accept(tcp).await else {
                return;
            };
            let peer_sha = tls
                .get_ref()
                .1
                .peer_certificates()
                .and_then(|c| c.first())
                .map(|c| sha256_hex(c));
            let svc = hyper::service::service_fn(move |req: Request<Incoming>| {
                let (state, pki, spec, peer_sha) =
                    (state.clone(), pki.clone(), spec.clone(), peer_sha.clone());
                async move {
                    let method = req.method().as_str().to_ascii_lowercase();
                    let path = req.uri().path().to_owned();
                    let query = req.uri().query().unwrap_or("").to_owned();
                    let key = req
                        .headers()
                        .get("idempotency-key")
                        .and_then(|v| v.to_str().ok())
                        .map(str::to_owned);
                    let body = req
                        .into_body()
                        .collect()
                        .await
                        .map(|b| b.to_bytes())
                        .unwrap_or_default();
                    if !enrol && state.lock().unwrap().hang {
                        std::future::pending::<()>().await;
                    }
                    let (status, resp, replayed) = handle(
                        &state, &pki, &spec, enrol, peer_sha, &method, &path, &query, key, &body,
                    );
                    let mut r = Response::builder().status(status);
                    if !resp.is_empty() {
                        let ct = if status >= 400 {
                            "application/problem+json"
                        } else {
                            "application/json"
                        };
                        r = r.header("content-type", ct);
                    }
                    if replayed {
                        r = r.header("Idempotent-Replayed", "true");
                    }
                    Ok::<_, std::convert::Infallible>(r.body(Full::new(Bytes::from(resp))).unwrap())
                }
            });
            let _ = hyper::server::conn::http1::Builder::new()
                .serve_connection(TokioIo::new(tls), svc)
                .await;
        });
    }
}

fn problem(status: u16, code: &str, retryable: bool) -> (u16, String) {
    (
        status,
        json!({"type": format!("urn:omnifile:error:{code}"), "title": code, "status": status,
               "code": code, "retryable": retryable, "detail": format!("mock: {code}")})
        .to_string(),
    )
}

#[allow(clippy::too_many_arguments)]
fn handle(
    state: &Mutex<State>,
    pki: &Pki,
    spec: &Value,
    enrol: bool,
    peer_sha: Option<String>,
    method: &str,
    path: &str,
    query: &str,
    key: Option<String>,
    body: &Bytes,
) -> (u16, String, bool) {
    let mut st = state.lock().unwrap();
    let json_body: Value = if body.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(body).unwrap_or(Value::String("<not json>".into()))
    };
    let edge_id = peer_sha.as_ref().and_then(|s| st.certs.get(s).copied());
    st.requests.push(Recorded {
        method: method.into(),
        path: path.into(),
        query: query.into(),
        idempotency_key: key.clone(),
        body: json_body.clone(),
        edge_id,
    });

    // Contract: the path and method exist, the body matches, POSTs carry a key.
    match find_template(spec, path) {
        None => st
            .contract_violations
            .push(format!("{method} {path}: no such path")),
        Some(t) => {
            if spec["paths"][t][method].is_null() {
                st.contract_violations
                    .push(format!("{method} {t}: no such method"));
            }
            if let Some(schema) = request_schema(spec, method, t)
                && let Err(e) = validate(spec, schema, &json_body)
            {
                st.contract_violations.push(format!("{method} {t}: {e}"));
            }
        }
    }
    if method == "post" && !key.as_ref().is_some_and(|k| (16..=128).contains(&k.len())) {
        st.contract_violations.push(format!(
            "{method} {path}: missing or bad Idempotency-Key {key:?}"
        ));
    }

    if !enrol {
        if st.down {
            let (s, b) = problem(503, "unavailable", true);
            return (s, b, false);
        }
        if st.fail_next > 0 {
            st.fail_next -= 1;
            let (s, b) = problem(503, "unavailable", true);
            return (s, b, false);
        }
        // Authentication: the certificate maps to an edge, which must match the path.
        let Some(eid) = edge_id else {
            let (s, b) = problem(401, "certificate_invalid", false);
            return (s, b, false);
        };
        let path_edge = path.split('/').nth(3).and_then(|s| s.parse::<u64>().ok());
        if path_edge != Some(eid) {
            let (s, b) = problem(403, "edge_mismatch", false);
            return (s, b, false);
        }
    }

    // Idempotency (§8).
    let idem_key = key.clone().map(|k| (edge_id, k));
    if method == "post"
        && let Some(k) = &idem_key
        && let Some((stored_body, s, r)) = st.idem.get(k)
    {
        if stored_body.as_bytes() == body.as_ref() {
            return (*s, r.clone(), true);
        }
        let (s, b) = problem(422, "idempotency_key_reused", false);
        return (s, b, false);
    }

    let (status, resp) = route(
        &mut st, pki, enrol, edge_id, method, path, query, &json_body, peer_sha,
    );
    if method == "post"
        && status < 500
        && status != 409
        && let Some(k) = idem_key
    {
        let stored = (
            String::from_utf8_lossy(body).into_owned(),
            status,
            resp.clone(),
        );
        st.idem.insert(k, stored);
    }
    (status, resp, false)
}

fn sas_credential(url: String, ttl: Duration) -> Value {
    let now = SystemTime::now();
    let expires = now + ttl;
    let cache_until = expires - Duration::from_secs(300).min(ttl);
    json!({"mode": "azure_user_delegation_sas", "url": url,
           "expires_at": format_time(expires), "cache_until": format_time(cache_until)})
}

#[allow(clippy::too_many_arguments)]
fn route(
    st: &mut State,
    pki: &Pki,
    enrol: bool,
    edge_id: Option<u64>,
    method: &str,
    path: &str,
    query: &str,
    body: &Value,
    peer_sha: Option<String>,
) -> (u16, String) {
    let segs: Vec<&str> = path.trim_matches('/').split('/').collect();
    if enrol {
        if (method, path) != ("post", "/v0/enrol") {
            return problem(404, "not_found", false);
        }
        let token = body["token"].as_str().unwrap_or("");
        if !st.tokens.remove(token) {
            return problem(401, "enrolment_token_invalid", false);
        }
        st.next_id += 1;
        let id = st.next_id;
        let (cert_pem, not_after) =
            pki.sign_csr(body["csr_pem"].as_str().unwrap(), id, st.cert_lifetime);
        let der = omnifile_blob::tls::parse_certs_pem(cert_pem.as_bytes()).unwrap();
        st.certs.insert(sha256_hex(&der[0]), id);
        return (
            201,
            json!({"edge_id": id.to_string(), "cert_pem": cert_pem, "chain_pem": "",
                   "ca_pem": pki.ca_pem, "not_after": not_after, "api_url": st.api_url})
            .to_string(),
        );
    }
    let eid = edge_id.unwrap();
    let rest: Vec<&str> = segs[3..].to_vec();
    let new_id = |st: &mut State| {
        st.next_id += 1;
        st.next_id
    };
    match (method, rest.as_slice()) {
        ("post", ["certificate"]) => {
            // Proof of possession (control API §2): present and well formed
            // (the signature itself is checked in identity.rs's unit test).
            let proof = &body["proof"];
            if proof["signed_at"].as_str().is_none_or(str::is_empty)
                || proof["signature"]
                    .as_str()
                    .is_none_or(|s| base64::engine::general_purpose::STANDARD.decode(s).is_err())
            {
                return problem(400, "invalid_request", false);
            }
            let (cert_pem, not_after) =
                pki.sign_csr(body["csr_pem"].as_str().unwrap(), eid, st.cert_lifetime);
            let der = omnifile_blob::tls::parse_certs_pem(cert_pem.as_bytes()).unwrap();
            st.certs.insert(sha256_hex(&der[0]), eid);
            let _ = peer_sha; // the previous certificate stays valid until it expires
            (
                201,
                json!({"edge_id": eid.to_string(), "cert_pem": cert_pem, "chain_pem": "",
                       "ca_pem": pki.ca_pem, "not_after": not_after})
                .to_string(),
            )
        }
        ("post", ["aws-certificate"]) => {
            // Decision 50: a Roles Anywhere certificate for the CSR's key.
            let proof = &body["proof"];
            if proof["signed_at"].as_str().is_none_or(str::is_empty)
                || proof["signature"]
                    .as_str()
                    .is_none_or(|s| base64::engine::general_purpose::STANDARD.decode(s).is_err())
            {
                return problem(400, "invalid_request", false);
            }
            let csr = if st.aws_wrong_key {
                let k = KeyPair::generate().unwrap();
                CertificateParams::new(Vec::<String>::new())
                    .unwrap()
                    .serialize_request(&k)
                    .unwrap()
                    .pem()
                    .unwrap()
            } else {
                body["csr_pem"].as_str().unwrap().to_owned()
            };
            let (cert_pem, not_after) = pki.sign_csr(&csr, eid, st.cert_lifetime);
            let renew_after = format_time(SystemTime::now() + st.cert_lifetime * 2 / 3);
            (
                201,
                json!({"edge_id": eid.to_string(), "cert_pem": cert_pem, "chain_pem": "",
                       "ca_pem": pki.ca_pem, "not_after": not_after, "renew_after": renew_after})
                .to_string(),
            )
        }
        ("post", ["heartbeat"]) => (
            200,
            json!({"server_time": format_time(SystemTime::now()), "min_edge_version": "0.0.0",
                   "feed_lag_events": 0, "config_version": "1"})
            .to_string(),
        ),
        ("get", ["feed"]) => {
            let q: HashMap<String, String> = query
                .split('&')
                .filter_map(|kv| kv.split_once('='))
                .map(|(k, v)| (k.to_owned(), v.to_owned()))
                .collect();
            let after = q.get("after").cloned().unwrap_or_default();
            if after == "9.9" {
                return (
                    200,
                    json!({"events": [], "next": after, "reset_required": true}).to_string(),
                );
            }
            let n: u64 = after
                .split_once('.')
                .and_then(|(_, b)| b.parse().ok())
                .unwrap_or(0);
            let limit: u64 = q.get("limit").and_then(|l| l.parse().ok()).unwrap_or(500);
            let events: Vec<Value> = st
                .feed
                .iter()
                .skip(n as usize)
                .take(limit as usize)
                .cloned()
                .collect();
            let next = if events.is_empty() {
                n.max(st.feed_horizon)
            } else {
                n + events.len() as u64
            };
            (
                200,
                json!({"events": events, "next": format!("0.{next}"), "reset_required": false})
                    .to_string(),
            )
        }
        ("post", ["feed", "ack"]) => {
            st.acks
                .push(body["cursor"].as_str().unwrap_or("").to_owned());
            (204, String::new())
        }
        ("post", ["changes"]) => {
            let results: Vec<Value> = body["ops"]
                .as_array()
                .unwrap()
                .iter()
                .map(|op| json!({"op_id": op["op_id"], "status": "applied", "file_id": "7", "entity_version": "1"}))
                .collect();
            (200, json!({"results": results}).to_string())
        }
        ("post", ["uploads"]) => {
            if st.conflict_uploads {
                return problem(409, "version_conflict", false);
            }
            let blake3 = body["blake3"].as_str().unwrap_or("");
            if st.dedupe.contains(blake3) {
                return (
                    201,
                    json!({"action": "dedupe", "version_id": "555"}).to_string(),
                );
            }
            let id = new_id(st);
            let url = st
                .upload_sas
                .clone()
                .unwrap_or_else(|| format!("https://acct.blob.core.windows.net/c/obj-{id}?sig=w"));
            (
                201,
                json!({"action": "upload", "upload_id": id.to_string(), "version_id": id.to_string(),
                       "target": sas_credential(url, st.sas_ttl), "block_size": body["block_size"],
                       "set_tier": false})
                .to_string(),
            )
        }
        ("post", ["uploads", id, "complete"]) => {
            let id: u64 = id.parse().unwrap();
            st.completes.push((id, body.clone()));
            (
                200,
                json!({"version_id": id.to_string(), "committed_at": format_time(SystemTime::now()),
                       "retention_until": null})
                .to_string(),
            )
        }
        ("post", ["uploads", _id, "abort"]) => (204, String::new()),
        ("post", ["credentials"]) => {
            let n = new_id(st);
            let url = match body["access"].as_str() {
                Some("write") => st
                    .upload_sas
                    .clone()
                    .unwrap_or_else(|| format!("https://acct.blob.core.windows.net/c/w?sig={n}")),
                _ => format!(
                    "https://acct.blob.core.windows.net/c/{}-{}?sig={n}",
                    body["file_id"].as_str().unwrap_or("?"),
                    body["version_id"].as_str().unwrap_or("?")
                ),
            };
            (200, sas_credential(url, st.sas_ttl).to_string())
        }
        ("post", ["files", _f, "stub", "prepare"]) => {
            if st.stub_refuse {
                return problem(409, "stub_not_allowed", false);
            }
            let lease = new_id(st);
            (
                200,
                json!({"lease_id": format!("{lease}.0123456789abcdef0123456789abcdef"), "resident_bytes": 4194304,
                       "lease_expires_at": format_time(SystemTime::now() + Duration::from_secs(600))})
                .to_string(),
            )
        }
        ("post", ["files", _f, "stub", "commit" | "skip"]) => (204, String::new()),
        ("post", ["recalls"]) => {
            if st.recall_online {
                let url = format!(
                    "https://acct.blob.core.windows.net/c/online?sig={}",
                    new_id(st)
                );
                return (
                    200,
                    json!({"status": "online", "credential": sas_credential(url, st.sas_ttl)})
                        .to_string(),
                );
            }
            let rid = new_id(st);
            (
                200,
                json!({"status": "recall", "recall_id": rid.to_string(), "state": "rehydrating",
                       "joined": false, "eta": null, "eta_confidence": "none", "decision": "wait",
                       "note": "Recalling from Archive."})
                .to_string(),
            )
        }
        ("post", ["recalls", _r, "filled"]) => (204, String::new()),
        ("post", ["reads"]) => {
            let n = body["reads"].as_array().map(|a| a.len()).unwrap_or(0);
            (
                200,
                json!({"accepted": n, "unknown_file_ids": []}).to_string(),
            )
        }
        _ => problem(404, "not_found", false),
    }
}

pub fn addr_of(url: &str) -> SocketAddr {
    let port: u16 = url.rsplit(':').next().unwrap().parse().unwrap();
    SocketAddr::from((Ipv4Addr::LOCALHOST, port))
}
