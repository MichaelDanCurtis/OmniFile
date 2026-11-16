//! Threat model T5: with TCP ingress, `api` presents a server certificate from
//! the edge CA, not the ingress certificate `enrol` presents. The enrolment
//! answer names that CA (`api_server_ca_sha256`), and the client pins it for
//! every later call.
//!
//! Two CAs, as in a deployment: the *ingress CA* issues `enrol`'s certificate
//! (the bundle pins it), and the *edge CA* issues the edges' client
//! certificates and `api`'s server certificate.

#[path = "support/mock.rs"]
mod mock;

use std::sync::Arc;

use bytes::Bytes;
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::{Request, Response};
use hyper_util::rt::TokioIo;
use mock::Pki;
use rustls::pki_types::CertificateDer;
use serde_json::{Value, json};
use tokio::net::TcpListener;
use tokio_rustls::TlsAcceptor;

use omnifile_control::types::{EdgeFacts, HeartbeatRequest};
use omnifile_control::*;

/// What the enrolment answer says about `api`'s CA.
#[derive(Clone)]
enum AnswerPin {
    /// HTTP ingress: no field; api presents the ingress certificate.
    None,
    /// TCP ingress: the edge CA (the CA in `ca_pem`).
    EdgeCa,
    /// A hash naming no CA the answer sent.
    Unknown,
}

struct Deployment {
    ingress: Arc<Pki>,
    edge_ca: Arc<Pki>,
    enrol_url: String,
    tasks: Vec<tokio::task::JoinHandle<()>>,
}

impl Drop for Deployment {
    fn drop(&mut self) {
        for t in &self.tasks {
            t.abort();
        }
    }
}

fn provider() -> Arc<rustls::crypto::CryptoProvider> {
    let _ = rustls::crypto::ring::default_provider().install_default();
    Arc::new(rustls::crypto::ring::default_provider())
}

/// `enrol` (ingress certificate) and `api`, whose server certificate is from
/// `api_cert_from`, requiring a client certificate from the edge CA.
async fn deploy(pin: AnswerPin, api_cert_from: &str) -> Deployment {
    let provider = provider();
    let ingress = Arc::new(Pki::new("Ingress CA"));
    let edge_ca = Arc::new(Pki::new("OmniFile edge CA"));
    let rogue = Pki::new("Some other CA");

    let (enrol_chain, enrol_key) = ingress.server_cert();
    let enrol_cfg = rustls::ServerConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_no_client_auth()
        .with_single_cert(enrol_chain, enrol_key)
        .unwrap();
    let (api_chain, api_key) = match api_cert_from {
        "edge" => edge_ca.server_cert(),
        "ingress" => ingress.server_cert(),
        _ => rogue.server_cert(),
    };
    // api sends only its leaf, as uvicorn does with a one-certificate chain file.
    let api_chain: Vec<CertificateDer<'static>> = api_chain.into_iter().take(1).collect();
    let mut roots = rustls::RootCertStore::empty();
    roots.add(edge_ca.ca_der.clone()).unwrap();
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
        .with_single_cert(api_chain, api_key)
        .unwrap();

    let enrol_l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let api_l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let enrol_url = format!("https://localhost:{}", enrol_l.local_addr().unwrap().port());
    let api_url = format!("https://localhost:{}", api_l.local_addr().unwrap().port());

    let answer_pin = match pin {
        AnswerPin::None => None,
        AnswerPin::EdgeCa => Some(edge_ca.ca_sha256()),
        AnswerPin::Unknown => Some("ab".repeat(32)),
    };
    let (ca, url) = (edge_ca.clone(), api_url.clone());
    let enrol_handler = move |body: Value| {
        let csr = body["csr_pem"].as_str().unwrap();
        let (cert_pem, not_after) = ca.sign_csr(csr, 7, std::time::Duration::from_secs(86400 * 30));
        let mut out = json!({
            "edge_id": "7", "cert_pem": cert_pem, "chain_pem": "", "ca_pem": ca.ca_pem,
            "not_after": not_after, "api_url": url,
        });
        if let Some(p) = &answer_pin {
            out["api_server_ca_sha256"] = json!(p);
        }
        (201u16, out)
    };
    let api_handler = |_body: Value| {
        (
            200u16,
            json!({"server_time": "2026-11-03T00:00:00Z", "min_edge_version": "0.0.0", "config_version": "1"}),
        )
    };
    let tasks = vec![
        tokio::spawn(serve(
            enrol_l,
            TlsAcceptor::from(Arc::new(enrol_cfg)),
            enrol_handler,
        )),
        tokio::spawn(serve(
            api_l,
            TlsAcceptor::from(Arc::new(api_cfg)),
            api_handler,
        )),
    ];
    Deployment {
        ingress,
        edge_ca,
        enrol_url,
        tasks,
    }
}

async fn serve<F>(l: TcpListener, acceptor: TlsAcceptor, handler: F)
where
    F: Fn(Value) -> (u16, Value) + Clone + Send + Sync + 'static,
{
    loop {
        let Ok((tcp, _)) = l.accept().await else {
            continue;
        };
        let (acceptor, handler) = (acceptor.clone(), handler.clone());
        tokio::spawn(async move {
            let Ok(tls) = acceptor.accept(tcp).await else {
                return;
            };
            let svc = hyper::service::service_fn(move |req: Request<Incoming>| {
                let handler = handler.clone();
                async move {
                    let body = req.into_body().collect().await.unwrap().to_bytes();
                    let (status, out) = handler(serde_json::from_slice(&body).unwrap_or(json!({})));
                    Ok::<_, std::convert::Infallible>(
                        Response::builder()
                            .status(status)
                            .header("content-type", "application/json")
                            .body(Full::new(Bytes::from(out.to_string())))
                            .unwrap(),
                    )
                }
            });
            let _ = hyper::server::conn::http1::Builder::new()
                .serve_connection(TokioIo::new(tls), svc)
                .await;
        });
    }
}

fn bundle(d: &Deployment) -> EnrolmentBundle {
    use base64::Engine as _;
    let j = json!({"url": d.enrol_url, "server_ca_sha256": d.ingress.ca_sha256(), "token": "t".repeat(43)});
    EnrolmentBundle::parse(&format!(
        "ofe1.{}",
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(j.to_string())
    ))
    .unwrap()
}

fn facts() -> EdgeFacts {
    EdgeFacts {
        hostname: "edge-t5".into(),
        os: None,
        kernel: None,
        version: "0.0.0".into(),
    }
}

async fn heartbeat(client: &ControlClient) -> Result<types::HeartbeatResponse> {
    client.heartbeat(&HeartbeatRequest::default()).await
}

#[tokio::test]
async fn api_is_reached_with_the_edge_ca_the_enrolment_answer_names() {
    let d = deploy(AnswerPin::EdgeCa, "edge").await;
    let dir = tempfile::tempdir().unwrap();
    let id = enrol(&bundle(&d), dir.path(), facts(), Default::default())
        .await
        .unwrap();
    assert_eq!(
        id.info.api_server_ca_sha256.as_deref(),
        Some(d.edge_ca.ca_sha256().as_str())
    );
    // server-ca.pem is the edge CA now, not the ingress CA enrol was pinned with.
    let pinned = std::fs::read_to_string(dir.path().join("server-ca.pem")).unwrap();
    assert_eq!(pinned.trim(), d.edge_ca.ca_pem.trim());
    let client = ControlClient::open(dir.path(), ControlConfig::default()).unwrap();
    heartbeat(&client)
        .await
        .expect("api's edge-CA certificate is trusted");
}

#[tokio::test]
async fn an_api_certificate_from_another_ca_is_refused() {
    // The answer pins the edge CA; a server presenting the ingress CA's (or any
    // other CA's) certificate is not api.
    for from in ["ingress", "rogue"] {
        let d = deploy(AnswerPin::EdgeCa, from).await;
        let dir = tempfile::tempdir().unwrap();
        enrol(&bundle(&d), dir.path(), facts(), Default::default())
            .await
            .unwrap();
        let client = ControlClient::open(dir.path(), ControlConfig::default()).unwrap();
        let err = heartbeat(&client).await.unwrap_err();
        assert!(err.is_unavailable(), "{from}: {err}");
    }
}

#[tokio::test]
async fn without_a_pin_the_ingress_ca_is_kept_as_before() {
    // HTTP ingress: api presents the same ingress certificate as enrol.
    let d = deploy(AnswerPin::None, "ingress").await;
    let dir = tempfile::tempdir().unwrap();
    let id = enrol(&bundle(&d), dir.path(), facts(), Default::default())
        .await
        .unwrap();
    assert_eq!(id.info.api_server_ca_sha256, None);
    let client = ControlClient::open(dir.path(), ControlConfig::default()).unwrap();
    heartbeat(&client)
        .await
        .expect("the ingress CA still works");
    // ...and an edge-CA api certificate is then not trusted.
    let d = deploy(AnswerPin::None, "edge").await;
    let dir = tempfile::tempdir().unwrap();
    enrol(&bundle(&d), dir.path(), facts(), Default::default())
        .await
        .unwrap();
    let client = ControlClient::open(dir.path(), ControlConfig::default()).unwrap();
    assert!(heartbeat(&client).await.unwrap_err().is_unavailable());
}

#[tokio::test]
async fn a_pin_naming_no_ca_the_answer_sent_fails_enrolment() {
    let d = deploy(AnswerPin::Unknown, "edge").await;
    let dir = tempfile::tempdir().unwrap();
    let err = enrol(&bundle(&d), dir.path(), facts(), Default::default())
        .await
        .unwrap_err();
    assert!(err.to_string().contains("pins an api CA"), "{err}");
    assert!(!Identity::exists(dir.path()));
}

#[tokio::test]
async fn a_swapped_server_ca_file_is_refused() {
    let d = deploy(AnswerPin::EdgeCa, "edge").await;
    let dir = tempfile::tempdir().unwrap();
    enrol(&bundle(&d), dir.path(), facts(), Default::default())
        .await
        .unwrap();
    // Someone replaces server-ca.pem: the pin in identity.json no longer matches.
    std::fs::write(dir.path().join("server-ca.pem"), &d.ingress.ca_pem).unwrap();
    let err = ControlClient::open(dir.path(), ControlConfig::default()).unwrap_err();
    assert!(err.to_string().contains("pinned at enrolment"), "{err}");
}
