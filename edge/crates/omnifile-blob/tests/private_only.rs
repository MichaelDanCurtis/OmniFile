//! Private networking (decision 22; `omnifile_blob::net`): with it on, the
//! edge never reaches a public address. Its own test binary, because the
//! policy is process-wide.

use std::str::FromStr;
use std::time::Duration;

use omnifile_blob::net::{self, PrivateResolver};
use omnifile_blob::{BlobClient, BlobError, BlobOptions, RetryPolicy};
use omnifile_integrity::{azure_content_crc64, crc64_nvme};
use reqwest::dns::{Name, Resolve};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

fn client() -> BlobClient {
    net::set_private_only(true);
    // The test servers listen on loopback, which only the sim may reach.
    net::set_loopback_ok(true);
    BlobClient::new(BlobOptions {
        allow_http: true,
        retry: RetryPolicy {
            max_attempts: 1,
            base_delay: Duration::from_millis(1),
            max_delay: Duration::from_millis(1),
        },
        ..BlobOptions::default()
    })
    .unwrap()
}

#[tokio::test]
async fn a_public_ip_literal_is_refused_before_any_connection() {
    let e = client()
        .get_range("http://20.60.1.2/c/b?sig=SECRETSIG", 0, 10)
        .await
        .unwrap_err();
    assert!(matches!(e, BlobError::Invalid(_)), "{e}");
    assert!(e.to_string().contains("private networking"), "{e}");
    assert!(!e.to_string().contains("SECRETSIG"));
    let e = client()
        .get_s3_range("http://[2603:1000::1]/b/k?X-Amz-Signature=SECRETSIG", 0, 10)
        .await
        .unwrap_err();
    assert!(e.to_string().contains("private networking"), "{e}");
}

#[tokio::test]
async fn a_name_with_only_public_addresses_does_not_resolve() {
    net::set_private_only(true);
    // An IP-shaped name resolves without DNS, so the test needs no network.
    let err = match PrivateResolver
        .resolve(Name::from_str("8.8.8.8").unwrap())
        .await
    {
        Ok(_) => panic!("a public address resolved"),
        Err(e) => e.to_string(),
    };
    assert!(
        err.contains("only to public addresses") && err.contains("8.8.8.8"),
        "{err}"
    );
    let ok: Vec<_> = match PrivateResolver
        .resolve(Name::from_str("10.1.2.3").unwrap())
        .await
    {
        Ok(a) => a.collect(),
        Err(e) => panic!("{e}"),
    };
    assert_eq!(ok.len(), 1);
}

#[tokio::test]
async fn a_private_endpoint_by_name_is_reached() {
    let body = b"0123456789".to_vec();
    let crc = azure_content_crc64(crc64_nvme(&body));
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = l.local_addr().unwrap().port();
    let served = body.clone();
    tokio::spawn(async move {
        let (mut s, _) = l.accept().await.unwrap();
        let mut buf = Vec::new();
        let mut b = [0u8; 4096];
        while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
            let n = s.read(&mut b).await.unwrap();
            if n == 0 {
                return;
            }
            buf.extend_from_slice(&b[..n]);
        }
        let head = format!(
            "HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nx-ms-content-crc64: {crc}\r\nConnection: close\r\n\r\n",
            served.len()
        );
        s.write_all(head.as_bytes()).await.unwrap();
        s.write_all(&served).await.unwrap();
        let _ = s.shutdown().await;
    });
    // "localhost" goes through the resolver, which keeps its loopback address.
    let got = client()
        .get_range(&format!("http://localhost:{port}/c/b?sig=x"), 0, 10)
        .await
        .unwrap();
    assert_eq!(&got[..], &body[..]);
}
