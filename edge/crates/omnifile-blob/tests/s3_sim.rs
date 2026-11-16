//! The S3 reader against the sim's S3 layout (SigV4 checked by the sim,
//! credentials from its fake `aws_signing_helper serve`).
//!
//! Skipped until the S3 track's `--layout s3` is in the tree (see
//! `support/sim_s3.rs`); `OMNIFILE_REQUIRE_SIM=1` turns the skip into a
//! failure.

mod support;

use std::time::Duration;

use base64::Engine as _;
use omnifile_blob::client::RetryPolicy;
use omnifile_blob::{
    Addressing, AwsCredentials, CredentialSource, EdgeReader, RangeReader, ReadTarget, S3Client,
    S3Object, S3Options,
};
use omnifile_integrity::crc64_nvme;
use serde_json::json;
use support::sim_s3::{SimS3, synthetic};

const GIB: u64 = 1 << 30;

fn client(sim: &SimS3, creds: CredentialSource) -> S3Client {
    let addr = sim.host_port().parse().expect("sim addr");
    S3Client::new(S3Options {
        credentials: creds,
        allow_http: true,
        test_endpoints: vec![sim.host_port(), format!("s3.sim.test:{}", port(sim))],
        // Virtual-host names resolve to the sim.
        resolve: ["seis", "pays", "cold"]
            .iter()
            .map(|b| (format!("{b}.s3.sim.test"), addr))
            .collect(),
        retry: RetryPolicy {
            max_attempts: 2,
            base_delay: Duration::from_millis(5),
            max_delay: Duration::from_millis(20),
        },
        ..S3Options::default()
    })
    .unwrap()
}

fn port(sim: &SimS3) -> u16 {
    sim.host_port().rsplit(':').next().unwrap().parse().unwrap()
}

fn imds(sim: &SimS3) -> CredentialSource {
    CredentialSource::Imds {
        url: sim.imds_url.clone(),
    }
}

fn obj(sim: &SimS3, bucket: &str, key: &str, version: &str, addressing: Addressing) -> S3Object {
    let endpoint = match addressing {
        Addressing::VirtualHost => format!("s3.sim.test:{}", port(sim)),
        Addressing::Path => sim.host_port(),
    };
    S3Object {
        bucket: bucket.into(),
        key: key.into(),
        version_id: Some(version.into()),
        region: sim.region.clone(),
        endpoint,
        addressing,
        tls12_only: false,
        requester_pays: false,
        private_network: true,
        etag: None,
    }
}

async fn bucket(sim: &SimS3, name: &str, requester_pays: bool) {
    sim.admin(
        "/_sim/s3/buckets",
        json!({"bucket": name, "versioning": true, "requester_pays": requester_pays}),
    )
    .await;
}

/// A 100 GiB synthetic object read at 1,000 random offsets through the chunk
/// sizes the cache uses: every byte is the sim's, with no whole-object GET.
#[tokio::test(flavor = "multi_thread")]
async fn random_ranges_of_a_100_gib_object() {
    let Some(sim) = SimS3::start() else { return };
    bucket(&sim, "seis", false).await;
    let size = 100 * GIB;
    let put = sim
        .admin(
            "/_sim/s3/objects",
            json!({"bucket": "seis", "key": "big/line 1042.segy", "storage_class": "STANDARD",
                   "synthetic_size": size, "seed": 1042}),
        )
        .await;
    let version = put["version_id"].as_str().unwrap().to_owned();
    sim.admin("/_sim/s3/stats/reset", json!({})).await;
    let c = client(&sim, imds(&sim));
    let r = EdgeReader::new(
        omnifile_blob::BlobClient::new(Default::default()).unwrap(),
        c.clone(),
    );
    let t = ReadTarget::S3(obj(
        &sim,
        "seis",
        "big/line 1042.segy",
        &version,
        Addressing::VirtualHost,
    ));
    let mut rng = fastrand::Rng::with_seed(7);
    let mut bytes = 0u64;
    for i in 0..1000 {
        let len = if i % 10 == 0 {
            4 << 20
        } else {
            rng.u64(1..=256 << 10)
        };
        let off = if i == 0 {
            size - len
        } else {
            rng.u64(0..size - len)
        };
        let got = r.read_range(&t, off, len).await.unwrap();
        assert_eq!(got.len() as u64, len);
        assert!(
            got[..] == synthetic(1042, off, len as usize)[..],
            "bytes differ at {off}+{len}"
        );
        bytes += len;
    }
    let stats = sim.get("/_sim/s3/stats").await;
    let b = &stats["buckets"]["seis"];
    assert_eq!(b["GET"].as_u64(), Some(1000));
    assert_eq!(b["LIST"].as_u64().unwrap_or(0), 0, "no LIST from the edge");
    assert_eq!(
        b["bytes_out"].as_u64(),
        Some(bytes),
        "no whole-object download"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn sigv4_is_checked_and_versions_are_pinned() {
    let Some(sim) = SimS3::start() else { return };
    bucket(&sim, "seis", false).await;
    let v1 = vec![1u8; 1000];
    let v2 = vec![2u8; 1000];
    let b64 = |d: &[u8]| base64::engine::general_purpose::STANDARD.encode(d);
    let put1 = sim
        .admin(
            "/_sim/s3/objects",
            json!({"bucket": "seis", "key": "a/f", "data_b64": b64(&v1)}),
        )
        .await;
    sim.admin(
        "/_sim/s3/objects",
        json!({"bucket": "seis", "key": "a/f", "data_b64": b64(&v2)}),
    )
    .await;
    let ver1 = put1["version_id"].as_str().unwrap();
    // Path style, the sim's static credentials (no session).
    let stat = CredentialSource::Static(AwsCredentials {
        access_key_id: sim.access_key_id.clone(),
        secret_access_key: sim.secret_access_key.clone(),
        session_token: None,
        expiration: None,
    });
    let c = client(&sim, stat);
    let got = c
        .get_range(&obj(&sim, "seis", "a/f", ver1, Addressing::Path), 100, 50)
        .await
        .unwrap();
    assert_eq!(
        &got[..],
        &v1[100..150],
        "the pinned version, not the latest"
    );
    // A wrong secret is refused by the sim's SigV4 check.
    let bad = client(
        &sim,
        CredentialSource::Static(AwsCredentials {
            access_key_id: sim.access_key_id.clone(),
            secret_access_key: "wrong".into(),
            session_token: None,
            expiration: None,
        }),
    );
    let e = bad
        .get_range(&obj(&sim, "seis", "a/f", ver1, Addressing::Path), 0, 10)
        .await
        .unwrap_err();
    assert!(e.is_auth(), "{e}");
    assert_eq!(e.code(), Some("SignatureDoesNotMatch"));
    // A version that doesn't exist.
    let e = c
        .get_range(&obj(&sim, "seis", "a/f", "nope", Addressing::Path), 0, 10)
        .await
        .unwrap_err();
    assert_eq!(e.status(), Some(404), "{e}");
    // Past EOF.
    assert!(
        c.get_range(&obj(&sim, "seis", "a/f", ver1, Addressing::Path), 990, 20)
            .await
            .is_err()
    );
    // The whole-file CRC the catalog would hold matches what the ranges give.
    let all = c
        .get_range(&obj(&sim, "seis", "a/f", ver1, Addressing::Path), 0, 1000)
        .await
        .unwrap();
    assert_eq!(crc64_nvme(&all), crc64_nvme(&v1));
}

#[tokio::test(flavor = "multi_thread")]
async fn archived_until_restored() {
    let Some(sim) = SimS3::start() else { return };
    bucket(&sim, "cold", false).await;
    let put = sim
        .admin(
            "/_sim/s3/objects",
            json!({"bucket": "cold", "key": "g", "storage_class": "GLACIER",
                   "synthetic_size": 10_000, "seed": 3}),
        )
        .await;
    let v = put["version_id"].as_str().unwrap();
    let c = client(&sim, imds(&sim));
    let o = obj(&sim, "cold", "g", v, Addressing::VirtualHost);
    let e = c.get_range(&o, 0, 100).await.unwrap_err();
    assert!(e.is_archived(), "{e}");
    sim.admin(
        "/_sim/s3/restore",
        json!({"bucket": "cold", "key": "g", "version_id": v, "days": 1}),
    )
    .await;
    // The restore completes on the sim clock.
    let mut ok = None;
    for _ in 0..50 {
        match c.get_range(&o, 0, 100).await {
            Ok(b) => {
                ok = Some(b);
                break;
            }
            Err(e) if e.is_archived() => {
                sim.admin("/_sim/advance", json!({"seconds": 3600})).await;
            }
            Err(e) => panic!("{e}"),
        }
    }
    assert_eq!(&ok.expect("restored")[..], &synthetic(3, 0, 100)[..]);
}

#[tokio::test(flavor = "multi_thread")]
async fn requester_pays() {
    let Some(sim) = SimS3::start() else { return };
    bucket(&sim, "pays", true).await;
    let put = sim
        .admin(
            "/_sim/s3/objects",
            json!({"bucket": "pays", "key": "k", "synthetic_size": 4096, "seed": 9}),
        )
        .await;
    let v = put["version_id"].as_str().unwrap();
    let c = client(&sim, imds(&sim));
    let mut o = obj(&sim, "pays", "k", v, Addressing::VirtualHost);
    let e = c.get_range(&o, 0, 10).await.unwrap_err();
    assert_eq!(e.status(), Some(403), "{e}");
    o.requester_pays = true;
    let got = c.get_range(&o, 0, 10).await.unwrap();
    assert_eq!(&got[..], &synthetic(9, 0, 10)[..]);
}

#[tokio::test(flavor = "multi_thread")]
async fn helper_credentials_refresh_before_expiry() {
    let Some(sim) = SimS3::start() else { return };
    bucket(&sim, "seis", false).await;
    let put = sim
        .admin(
            "/_sim/s3/objects",
            json!({"bucket": "seis", "key": "k", "synthetic_size": 4096, "seed": 1}),
        )
        .await;
    let v = put["version_id"].as_str().unwrap();
    sim.admin("/_sim/imds", json!({"ttl_seconds": 4})).await;
    let c = client(&sim, imds(&sim));
    let o = obj(&sim, "seis", "k", v, Addressing::Path);
    for _ in 0..4 {
        c.get_range(&o, 0, 10).await.unwrap();
        tokio::time::sleep(Duration::from_millis(1_500)).await;
    }
    let s = sim.get("/_sim/imds").await;
    let fetched = s["credential_fetches"].as_u64().unwrap();
    // 6 s of reads with 4 s credentials: refreshed (at half life), and
    // never used past expiry (that would have failed a read above).
    assert!((2..=4).contains(&fetched), "{s}");
}

/// An unversioned bucket: reads are pinned by the ETag; an overwrite makes
/// the next read `412` ("the source changed").
#[tokio::test(flavor = "multi_thread")]
async fn unversioned_reads_are_pinned_by_etag() {
    let Some(sim) = SimS3::start() else { return };
    sim.admin(
        "/_sim/s3/buckets",
        json!({"bucket": "seis", "versioning": false, "requester_pays": false}),
    )
    .await;
    let b64 = |d: &[u8]| base64::engine::general_purpose::STANDARD.encode(d);
    let put = sim
        .admin(
            "/_sim/s3/objects",
            json!({"bucket": "seis", "key": "u/f", "data_b64": b64(&[1u8; 1000])}),
        )
        .await;
    assert!(put["version_id"].is_null(), "{put}");
    let etag = put["etag"].as_str().unwrap().to_owned();
    let c = client(&sim, imds(&sim));
    let mut o = obj(&sim, "seis", "u/f", "x", Addressing::Path);
    o.version_id = None;
    o.etag = Some(etag.clone());
    let got = c.get_range(&o, 10, 20).await.unwrap();
    assert_eq!(&got[..], &[1u8; 20]);
    // The same key overwritten at the source.
    sim.admin(
        "/_sim/s3/objects",
        json!({"bucket": "seis", "key": "u/f", "data_b64": b64(&[2u8; 1000])}),
    )
    .await;
    let e = c.get_range(&o, 10, 20).await.unwrap_err();
    if e.status() != Some(412) && std::env::var("OMNIFILE_REQUIRE_SIM").as_deref() != Ok("1") {
        eprintln!("skipping: this sim doesn't answer If-Match with 412 ({e})");
        return;
    }
    assert!(e.is_source_changed(), "{e}");
    assert_eq!(e.code(), Some("PreconditionFailed"));
    o.etag = None;
    assert!(matches!(
        c.get_range(&o, 0, 1).await.unwrap_err(),
        omnifile_blob::BlobError::Unpinned(_)
    ));
}
