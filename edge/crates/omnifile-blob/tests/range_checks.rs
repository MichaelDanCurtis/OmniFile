//! Range-read and URL checks against a scripted in-process HTTP server
//! (M1 review S9 and security items): https only, the range's CRC and the
//! named version are mandatory, and no error string carries a SAS.

use std::time::Duration;

use omnifile_blob::{BlobClient, BlobError, BlobOptions, RetryPolicy};
use omnifile_integrity::{azure_content_crc64, crc64_nvme};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

fn client(allow_http: bool) -> BlobClient {
    BlobClient::new(BlobOptions {
        allow_http,
        retry: RetryPolicy {
            max_attempts: 1,
            base_delay: Duration::from_millis(1),
            max_delay: Duration::from_millis(1),
        },
        ..BlobOptions::default()
    })
    .unwrap()
}

/// Serve one request with a 206 carrying `body` and `headers`.
async fn serve_once(headers: Vec<(&'static str, String)>, body: Vec<u8>) -> String {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
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
        let mut resp = format!(
            "HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nConnection: close\r\n",
            body.len()
        );
        for (k, v) in headers {
            resp.push_str(&format!("{k}: {v}\r\n"));
        }
        resp.push_str("\r\n");
        s.write_all(resp.as_bytes()).await.unwrap();
        s.write_all(&body).await.unwrap();
        let _ = s.shutdown().await;
    });
    format!("http://{addr}/c/blob?sv=2026-04-06&sig=SECRETSIG")
}

#[tokio::test]
async fn http_urls_are_refused_unless_allowed() {
    let e = client(false)
        .get_range("http://127.0.0.1:9/c/b?sig=SECRETSIG", 0, 10)
        .await
        .unwrap_err();
    assert!(matches!(e, BlobError::Invalid(_)), "{e}");
    assert!(!e.to_string().contains("SECRETSIG"));
}

#[tokio::test]
async fn a_range_without_its_crc_is_an_error() {
    let body = b"0123456789".to_vec();
    let url = serve_once(vec![], body).await;
    let e = client(true).get_range(&url, 0, 10).await.unwrap_err();
    assert!(e.to_string().contains("x-ms-content-crc64"), "{e}");
}

#[tokio::test]
async fn a_range_with_its_crc_passes() {
    let body = b"0123456789".to_vec();
    let crc = azure_content_crc64(crc64_nvme(&body));
    let url = serve_once(vec![("x-ms-content-crc64", crc)], body.clone()).await;
    let got = client(true).get_range(&url, 0, 10).await.unwrap();
    assert_eq!(&got[..], &body[..]);
}

#[tokio::test]
async fn a_range_of_another_version_is_refused() {
    let body = b"0123456789".to_vec();
    let crc = azure_content_crc64(crc64_nvme(&body));
    let url = serve_once(
        vec![
            ("x-ms-content-crc64", crc.clone()),
            ("x-ms-version-id", "2026-10-05T10:00:00.0000001Z".into()),
        ],
        body.clone(),
    )
    .await;
    let url = format!("{url}&versionid=2026-10-01T09%3A00%3A00.0000000Z");
    let e = client(true).get_range(&url, 0, 10).await.unwrap_err();
    assert!(matches!(e, BlobError::Integrity(_)), "{e}");
    // No version header at all is refused too.
    let url = serve_once(vec![("x-ms-content-crc64", crc.clone())], body.clone()).await;
    let url = format!("{url}&versionid=2026-10-01T09%3A00%3A00.0000000Z");
    assert!(client(true).get_range(&url, 0, 10).await.is_err());
    // The named version passes.
    let url = serve_once(
        vec![
            ("x-ms-content-crc64", crc),
            ("x-ms-version-id", "2026-10-01T09:00:00.0000000Z".into()),
        ],
        body.clone(),
    )
    .await;
    let url = format!("{url}&versionid=2026-10-01T09%3A00%3A00.0000000Z");
    assert_eq!(
        &client(true).get_range(&url, 0, 10).await.unwrap()[..],
        &body[..]
    );
}

#[tokio::test]
async fn transport_errors_never_carry_the_sas() {
    // Nothing listens on this port: a connection error.
    let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = l.local_addr().unwrap().port();
    drop(l);
    let url = format!("http://127.0.0.1:{port}/c/b?sv=1&sig=SECRETSIG");
    let e = client(true).get_range(&url, 0, 10).await.unwrap_err();
    assert!(matches!(e, BlobError::Transport { .. }), "{e}");
    let shown = format!("{e} {e:?}");
    let shown = format!("{shown} {}", e.into_io());
    assert!(
        !shown.contains("SECRETSIG") && !shown.contains("sig="),
        "{shown}"
    );
}

#[tokio::test]
async fn redirects_are_never_followed() {
    // The redirect target: must never be contacted.
    let target = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let taddr = target.local_addr().unwrap();
    let hit = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let h2 = hit.clone();
    tokio::spawn(async move {
        if target.accept().await.is_ok() {
            h2.store(true, std::sync::atomic::Ordering::SeqCst);
        }
    });
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        let (mut s, _) = l.accept().await.unwrap();
        let mut b = [0u8; 4096];
        let _ = s.read(&mut b).await;
        let resp = format!(
            "HTTP/1.1 302 Found\r\nLocation: http://{taddr}/c/b?sig=SECRETSIG\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        );
        s.write_all(resp.as_bytes()).await.unwrap();
    });
    let url = format!("http://{addr}/c/b?sv=1&sig=SECRETSIG");
    let e = client(true).get_range(&url, 0, 10).await.unwrap_err();
    assert_eq!(e.status(), Some(302), "{e}");
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(
        !hit.load(std::sync::atomic::Ordering::SeqCst),
        "followed the redirect"
    );
}

// ---- S3 presigned GetObject ------------------------------------------------

/// Serve one request with `status` and `body`; returns the request's head.
async fn serve_status(
    status: &'static str,
    headers: Vec<(&'static str, String)>,
    body: Vec<u8>,
) -> (String, tokio::sync::oneshot::Receiver<String>) {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    let (tx, rx) = tokio::sync::oneshot::channel();
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
        let _ = tx.send(String::from_utf8_lossy(&buf).into_owned());
        let mut resp = format!(
            "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n",
            body.len()
        );
        for (k, v) in headers {
            resp.push_str(&format!("{k}: {v}\r\n"));
        }
        resp.push_str("\r\n");
        s.write_all(resp.as_bytes()).await.unwrap();
        s.write_all(&body).await.unwrap();
        let _ = s.shutdown().await;
    });
    (
        format!(
            "http://127.0.0.1:{}/bkt/k?versionId=v7&X-Amz-Credential=AKIA&X-Amz-Signature=SECRETSIG",
            addr.port()
        ),
        rx,
    )
}

#[tokio::test]
async fn an_s3_range_needs_no_crc_but_its_length_and_version() {
    let body = b"0123456789".to_vec();
    let (url, head) = serve_status(
        "206 Partial Content",
        vec![("x-amz-version-id", "v7".into())],
        body.clone(),
    )
    .await;
    let got = client(true).get_s3_range(&url, 100, 10).await.unwrap();
    assert_eq!(&got[..], &body[..]);
    let head = head.await.unwrap().to_ascii_lowercase();
    assert!(head.contains("range: bytes=100-109"), "{head}");
    assert!(!head.contains("x-ms-"), "no Azure headers to S3: {head}");
    // Another version, or none, is refused.
    let (url, _) = serve_status(
        "206 Partial Content",
        vec![("x-amz-version-id", "v8".into())],
        body.clone(),
    )
    .await;
    let e = client(true).get_s3_range(&url, 0, 10).await.unwrap_err();
    assert!(matches!(e, BlobError::Integrity(_)), "{e}");
    let (url, _) = serve_status("206 Partial Content", vec![], body.clone()).await;
    assert!(client(true).get_s3_range(&url, 0, 10).await.is_err());
    // A short body is refused.
    let (url, _) = serve_status(
        "206 Partial Content",
        vec![("x-amz-version-id", "v7".into())],
        body[..5].to_vec(),
    )
    .await;
    let e = client(true).get_s3_range(&url, 0, 10).await.unwrap_err();
    assert!(matches!(e, BlobError::BadResponse { .. }), "{e}");
}

#[tokio::test]
async fn an_archived_s3_object_reads_as_archived_and_hides_the_signature() {
    let xml = b"<?xml version=\"1.0\"?><Error><Code>InvalidObjectState</Code>\
        <Message>The operation is not valid for the object's storage class</Message></Error>"
        .to_vec();
    let (url, _) = serve_status("403 Forbidden", vec![], xml).await;
    let e = client(true).get_s3_range(&url, 0, 10).await.unwrap_err();
    assert!(e.is_archived(), "{e}");
    assert!(!format!("{e} {e:?}").contains("SECRETSIG"));
    // An http URL is refused unless allowed, without echoing the signature.
    let e = client(false).get_s3_range(&url, 0, 10).await.unwrap_err();
    assert!(matches!(e, BlobError::Invalid(_)) && !e.to_string().contains("SECRETSIG"));
}

/// Review on #23 (P1): a presigned URL signed with the catalog's ETag as
/// `If-Match` must be read with exactly that header, and a replaced object
/// (`412`) is a changed source, never bytes of another object.
#[tokio::test]
async fn a_signed_if_match_is_sent_and_a_replaced_object_is_a_changed_source() {
    let body = b"0123456789".to_vec();
    let (url, head) = serve_status(
        "206 Partial Content",
        vec![("x-amz-version-id", "v7".into())],
        body.clone(),
    )
    .await;
    let read = omnifile_blob::PresignedRead {
        url,
        if_match: Some("\"etag-1\"".into()),
    };
    let s = read.encode();
    assert!(s.starts_with(omnifile_blob::client::PRESIGNED_PREFIX));
    let got = client(true).get_s3_range(&s, 0, 10).await.unwrap();
    assert_eq!(&got[..], &body[..]);
    let head = head.await.unwrap().to_ascii_lowercase();
    assert!(head.contains("if-match: \"etag-1\""), "{head}");
    // The object was overwritten: S3 refuses the signed precondition.
    let xml = b"<?xml version=\"1.0\"?><Error><Code>PreconditionFailed</Code>\
        <Message>At least one of the pre-conditions you specified did not hold</Message></Error>"
        .to_vec();
    let (url, _) = serve_status("412 Precondition Failed", vec![], xml).await;
    let s = omnifile_blob::PresignedRead {
        url,
        if_match: Some("\"etag-1\"".into()),
    }
    .encode();
    let e = client(true).get_s3_range(&s, 0, 10).await.unwrap_err();
    assert!(e.is_source_changed(), "{e}");
    assert!(!format!("{e} {e:?}").contains("SECRETSIG"));
    // A plain URL is still a plain URL (no header).
    let (url, head) = serve_status(
        "206 Partial Content",
        vec![("x-amz-version-id", "v7".into())],
        body.clone(),
    )
    .await;
    client(true).get_s3_range(&url, 0, 10).await.unwrap();
    assert!(
        !head
            .await
            .unwrap()
            .to_ascii_lowercase()
            .contains("if-match")
    );
}

#[test]
fn a_presigned_read_is_pinned_by_version_or_signed_etag() {
    use omnifile_blob::PresignedRead;
    let p = |url: &str, e: Option<&str>| PresignedRead {
        url: url.into(),
        if_match: e.map(Into::into),
    };
    let base = "https://b.vpce-1.s3.eu-west-1.vpce.amazonaws.com/k";
    assert!(p(&format!("{base}?versionId=v1&X-Amz-Signature=s"), None).is_pinned());
    assert!(p(&format!("{base}?X-Amz-Signature=s"), Some("\"e\"")).is_pinned());
    assert!(!p(&format!("{base}?X-Amz-Signature=s"), None).is_pinned());
    assert!(!p(&format!("{base}?versionId=null&X-Amz-Signature=s"), None).is_pinned());
    let r = p(&format!("{base}?X-Amz-Signature=SECRET"), Some("\"e\""));
    assert_eq!(PresignedRead::decode(&r.encode()).unwrap(), r);
    assert!(!format!("{r:?}").contains("SECRET"));
    // A plain URL decodes as itself.
    assert_eq!(PresignedRead::decode(base).unwrap().url, base);
}
