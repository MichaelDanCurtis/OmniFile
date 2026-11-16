//! A backend that accepts and then goes quiet (a stuck front end, or a flow a
//! stateful firewall forgot during an outage) must not hold a request for
//! ever: fills, uploads and property reads each give up on their own timeout
//! and report a retryable error, before the headers and in the middle of a body.

use std::time::{Duration, Instant};

use omnifile_blob::{BlobClient, BlobOptions, RetryPolicy};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

const TIMEOUT: Duration = Duration::from_millis(300);
/// Far above TIMEOUT, far below "for ever".
const BOUND: Duration = Duration::from_secs(5);

fn client() -> BlobClient {
    BlobClient::new(BlobOptions {
        allow_http: true,
        connect_timeout: TIMEOUT,
        request_timeout: TIMEOUT,
        retry: RetryPolicy {
            max_attempts: 1,
            base_delay: Duration::from_millis(1),
            max_delay: Duration::from_millis(1),
        },
        ..BlobOptions::default()
    })
    .unwrap()
}

/// Accept every connection and read the request; then send `head` (perhaps
/// nothing) and go quiet, holding the socket open.
async fn quiet_server(head: &'static str) -> String {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        loop {
            let Ok((mut s, _)) = l.accept().await else {
                return;
            };
            tokio::spawn(async move {
                let mut b = [0u8; 4096];
                let mut seen = Vec::new();
                while !seen.windows(4).any(|w| w == b"\r\n\r\n") {
                    match s.read(&mut b).await {
                        Ok(0) | Err(_) => return,
                        Ok(n) => seen.extend_from_slice(&b[..n]),
                    }
                }
                let _ = s.write_all(head.as_bytes()).await;
                std::future::pending::<()>().await;
                drop(s);
            });
        }
    });
    format!("http://{addr}/c/blob?sv=2026-04-06&sig=SECRETSIG")
}

async fn bounded<T>(what: &str, f: impl std::future::Future<Output = T>) -> T {
    let t = Instant::now();
    let r = tokio::time::timeout(BOUND, f)
        .await
        .unwrap_or_else(|_| panic!("{what} outlived its timeout"));
    assert!(t.elapsed() < BOUND, "{what}: {:?}", t.elapsed());
    r
}

#[tokio::test]
async fn a_backend_that_never_answers_times_out() {
    let url = quiet_server("").await;
    let c = client();
    for (what, err) in [
        (
            "a fill's range read",
            bounded("get_range", c.get_range(&url, 0, 4096))
                .await
                .unwrap_err(),
        ),
        (
            "an S3 range read",
            bounded("get_s3_range", c.get_s3_range(&url, 0, 4096))
                .await
                .unwrap_err(),
        ),
        (
            "a property read",
            bounded("get_properties", c.get_properties(&url))
                .await
                .unwrap_err(),
        ),
    ] {
        assert!(err.is_retryable(), "{what}: {err}");
        assert!(!err.to_string().contains("SECRETSIG"), "{what}: {err}");
    }
}

#[tokio::test]
async fn a_body_that_stalls_halfway_times_out() {
    // Headers and part of the body, then nothing: the timeout covers the
    // whole transfer, not just the wait for headers.
    let url = quiet_server(
        "HTTP/1.1 206 Partial Content\r\nContent-Length: 4096\r\nContent-Range: bytes 0-4095/8192\r\n\r\nabcd",
    )
    .await;
    let err = bounded("get_range", client().get_range(&url, 0, 4096))
        .await
        .unwrap_err();
    assert!(err.is_retryable(), "{err}");
}
