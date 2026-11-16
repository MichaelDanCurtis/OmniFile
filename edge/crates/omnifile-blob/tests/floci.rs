//! The `floci-aws-edge` lane (`cargo test -p omnifile-blob --features
//! floci`): a SigV4-signed ranged GET pinned to a version, against Floci's
//! S3 (`floci/floci`, an AWS emulator), over TLS 1.2 when Floci serves TLS.
//!
//! Environment (set by `.github/workflows/floci.yml`):
//! - `FLOCI_S3_ENDPOINT`: `host:port` (default `127.0.0.1:4566`);
//! - `FLOCI_S3_SCHEME`: `http` (default) or `https`;
//! - `FLOCI_CA_PEM`: path to the CA of Floci's TLS certificate (https only).
//!
//! VERIFY (M5.md): whether Floci checks SigV4 signatures. If it doesn't,
//! this proves the request shape only, as `floci-az` does for SAS.
#![cfg(feature = "floci")]

use std::time::{Duration, SystemTime};

use omnifile_blob::s3::sign_request_with_body;
use omnifile_blob::{
    Addressing, AwsCredentials, CredentialSource, S3Client, S3Object, S3Options, TlsOptions,
};

fn env(k: &str, d: &str) -> String {
    std::env::var(k).unwrap_or_else(|_| d.to_owned())
}

fn creds() -> AwsCredentials {
    AwsCredentials {
        access_key_id: "test".into(),
        secret_access_key: "test".into(),
        session_token: None,
        expiration: None,
    }
}

/// A signed request from the test itself (bucket and object setup).
async fn call(
    http: &reqwest::Client,
    method: reqwest::Method,
    url: &str,
    body: &[u8],
) -> reqwest::Response {
    let u = reqwest::Url::parse(url).unwrap();
    let host = match u.port() {
        Some(p) => format!("{}:{p}", u.host_str().unwrap()),
        None => u.host_str().unwrap().to_owned(),
    };
    let headers = vec![("host".to_owned(), host)];
    let signed = sign_request_with_body(
        method.as_str(),
        url,
        &headers,
        body,
        &creds(),
        "us-east-1",
        SystemTime::now(),
    )
    .unwrap();
    let mut req = http.request(method, u).body(body.to_vec());
    for (k, v) in signed {
        req = req.header(k, v);
    }
    req.send().await.expect("floci request")
}

#[tokio::test]
async fn sigv4_ranged_get_pinned_to_a_version() {
    let endpoint = env("FLOCI_S3_ENDPOINT", "127.0.0.1:4566");
    let scheme = env("FLOCI_S3_SCHEME", "http");
    let tls = match std::env::var("FLOCI_CA_PEM") {
        Ok(p) => TlsOptions::pinned(std::fs::read(p).expect("FLOCI_CA_PEM")),
        Err(_) => TlsOptions::public(),
    };
    let setup = omnifile_blob::tls::http_client(
        tls.clone().client_config().unwrap(),
        Duration::from_secs(10),
    )
    .unwrap();
    let base = format!("{scheme}://{endpoint}");
    let bucket = format!("omni-edge-{}", fastrand::u32(..));
    let r = call(
        &setup,
        reqwest::Method::PUT,
        &format!("{base}/{bucket}"),
        b"",
    )
    .await;
    assert!(r.status().is_success(), "create bucket: {}", r.status());
    let ver = b"<VersioningConfiguration xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\"><Status>Enabled</Status></VersioningConfiguration>";
    let r = call(
        &setup,
        reqwest::Method::PUT,
        &format!("{base}/{bucket}?versioning"),
        ver,
    )
    .await;
    assert!(r.status().is_success(), "versioning: {}", r.status());
    let v1: Vec<u8> = (0..100_000u32).map(|i| (i % 251) as u8).collect();
    let r = call(
        &setup,
        reqwest::Method::PUT,
        &format!("{base}/{bucket}/a/b%20c.bin"),
        &v1,
    )
    .await;
    assert!(r.status().is_success(), "put v1: {}", r.status());
    let version = r
        .headers()
        .get("x-amz-version-id")
        .expect("versioned put returns a version id")
        .to_str()
        .unwrap()
        .to_owned();
    let r = call(
        &setup,
        reqwest::Method::PUT,
        &format!("{base}/{bucket}/a/b%20c.bin"),
        &[9u8; 100_000],
    )
    .await;
    assert!(r.status().is_success(), "put v2: {}", r.status());

    let c = S3Client::new(S3Options {
        credentials: CredentialSource::Static(creds()),
        allow_http: scheme == "http",
        tls,
        test_endpoints: vec![endpoint.clone()],
        ..S3Options::default()
    })
    .unwrap();
    let o = S3Object {
        bucket,
        key: "a/b c.bin".into(),
        version_id: Some(version),
        region: "us-east-1".into(),
        endpoint,
        addressing: Addressing::Path,
        tls12_only: true,
        requester_pays: false,
        private_network: true,
        etag: None,
    };
    let got = c.get_range(&o, 4096, 50_000).await.unwrap();
    assert_eq!(&got[..], &v1[4096..54_096], "the pinned version's bytes");
}
