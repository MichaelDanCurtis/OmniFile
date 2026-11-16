//! The control client against the in-process mock (real TLS and mTLS). Each
//! test ends with `assert_contract`: every request the client sent matched
//! `control-api.yaml`.

mod support;

use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, SystemTime};

use omnifile_blob::{BlobClient, BlobError, BlobOptions, UploadOutcome};
use omnifile_control::types::*;
use omnifile_control::*;
use omnifile_integrity::{BlockPlan, crc64_nvme};
use support::mock::Mock;
use support::sim::Sim;

fn facts() -> EdgeFacts {
    EdgeFacts {
        hostname: "edge-test".into(),
        os: Some("Ubuntu 26.04".into()),
        kernel: Some("7.0".into()),
        version: "0.0.0".into(),
    }
}

async fn enrolled(mock: &Mock, dir: &Path) -> ControlClient {
    let bundle = EnrolmentBundle::parse(&mock.bundle()).unwrap();
    enrol(&bundle, dir, facts(), Default::default())
        .await
        .unwrap();
    ControlClient::open(dir, ControlConfig::default()).unwrap()
}

#[tokio::test]
async fn enrolment_then_mutual_tls() {
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    let client = enrolled(&mock, dir.path()).await;
    assert!(client.edge_id() > 0);

    use std::os::unix::fs::PermissionsExt;
    for f in ["identity.pem", "server-ca.pem", "identity.json"] {
        let mode = std::fs::metadata(dir.path().join(f))
            .unwrap()
            .permissions()
            .mode();
        assert_eq!(mode & 0o777, 0o600, "{f}");
    }
    assert!(!dir.path().join("pending-enrol.json").exists());
    // The pinned server CA is the mock's CA.
    let pinned = std::fs::read(dir.path().join("server-ca.pem")).unwrap();
    let pinned = omnifile_blob::tls::parse_certs_pem(&pinned).unwrap();
    assert_eq!(pinned[0], mock.pki.ca_der);

    let hb = client
        .heartbeat(&HeartbeatRequest {
            version: "0.0.0".into(),
            guard_state: Some(GuardState::Armed),
            feed_cursor: Some(FEED_START.into()),
            ..Default::default()
        })
        .await
        .unwrap();
    assert_eq!(hb.config_version, 1);
    let reqs = mock.requests("/heartbeat");
    assert_eq!(
        reqs[0].edge_id,
        Some(client.edge_id()),
        "mTLS identified the edge"
    );
    mock.assert_contract();
}

#[tokio::test]
async fn enrolment_refuses_a_server_with_another_ca() {
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    let mut bundle = EnrolmentBundle::parse(&mock.bundle()).unwrap();
    bundle.server_ca_sha256 = "00".repeat(32);
    let err = enrol(&bundle, dir.path(), facts(), Default::default())
        .await
        .unwrap_err();
    assert!(err.to_string().contains("enrol"), "{err}");
    assert!(!Identity::exists(dir.path()));
    assert!(
        mock.requests("/v0/enrol").is_empty(),
        "nothing was sent to the wrong server"
    );
}

#[tokio::test]
async fn a_used_token_is_refused() {
    let mock = Mock::start().await;
    let b = mock.bundle();
    let bundle = EnrolmentBundle::parse(&b).unwrap();
    let d1 = tempfile::tempdir().unwrap();
    enrol(&bundle, d1.path(), facts(), Default::default())
        .await
        .unwrap();
    let d2 = tempfile::tempdir().unwrap();
    let err = enrol(&bundle, d2.path(), facts(), Default::default())
        .await
        .unwrap_err();
    assert_eq!(err.code(), Some("enrolment_token_invalid"), "{err}");
    assert!(!err.is_retryable());
}

/// Decision 50: the Roles Anywhere certificate from the edge CA, requested
/// over mTLS with the wizard's request, renewed with the mTLS certificate,
/// and checked against the request's key before it is installed.
#[tokio::test]
async fn roles_anywhere_certificate_from_the_edge_ca() {
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    let client = enrolled(&mock, dir.path()).await;
    let ra = tempfile::tempdir().unwrap();
    let files = RolesAnywhereFiles::new(ra.path().join("roles-anywhere"));
    // Without the wizard's request (the client's own CA): nothing to do.
    assert!(
        !client
            .renew_roles_anywhere_if_due(&files, true)
            .await
            .unwrap()
    );
    assert!(mock.requests("/aws-certificate").is_empty());

    let key = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
    let csr = rcgen::CertificateParams::new(Vec::<String>::new())
        .unwrap()
        .serialize_request(&key)
        .unwrap()
        .pem()
        .unwrap();
    std::fs::create_dir_all(&files.dir).unwrap();
    std::fs::write(files.path(roles_anywhere::CSR_FILE), &csr).unwrap();

    // None yet: requested, with the request as written and a proof.
    assert!(
        client
            .renew_roles_anywhere_if_due(&files, false)
            .await
            .unwrap()
    );
    let sent = mock.requests("/aws-certificate");
    assert_eq!(sent.len(), 1);
    assert_eq!(sent[0].body["csr_pem"], csr.as_str());
    assert!(sent[0].body["proof"]["signature"].as_str().is_some());
    assert!(sent[0].idempotency_key.is_some());
    assert_eq!(sent[0].edge_id, Some(client.edge_id()));
    let cert = std::fs::read_to_string(files.path(roles_anywhere::CERT_FILE)).unwrap();
    roles_anywhere::check_for_request(&csr, &cert).unwrap();
    assert!(files.installed().is_some());

    // Not due again until the mTLS certificate renews, then with it.
    assert!(
        !client
            .renew_roles_anywhere_if_due(&files, false)
            .await
            .unwrap()
    );
    assert_eq!(mock.requests("/aws-certificate").len(), 1);
    client.renew_certificate().await.unwrap();
    assert!(
        client
            .renew_roles_anywhere_if_due(&files, false)
            .await
            .unwrap()
    );
    assert_eq!(mock.requests("/aws-certificate").len(), 2);
    assert!(
        !client
            .renew_roles_anywhere_if_due(&files, false)
            .await
            .unwrap()
    );

    // A certificate for another key is refused, and the installed one kept.
    let before = std::fs::read_to_string(files.path(roles_anywhere::CERT_FILE)).unwrap();
    mock.state.lock().unwrap().aws_wrong_key = true;
    let err = client
        .renew_roles_anywhere_if_due(&files, true)
        .await
        .unwrap_err();
    assert!(
        err.to_string().contains("not for this filer's AWS key"),
        "{err}"
    );
    assert_eq!(
        std::fs::read_to_string(files.path(roles_anywhere::CERT_FILE)).unwrap(),
        before
    );
    mock.assert_contract();
}

#[tokio::test]
async fn certificate_renewal_swaps_the_identity() {
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    let client = enrolled(&mock, dir.path()).await;
    let before = client.identity().cert_sha256().unwrap();
    assert!(!client.identity().renewal_due(SystemTime::now()));
    let renewed = client.renew_certificate().await.unwrap();
    assert_ne!(renewed.cert_sha256().unwrap(), before);
    assert!(!dir.path().join("pending-renewal.json").exists());
    // The new certificate is in use, and on disk.
    client
        .heartbeat(&HeartbeatRequest {
            version: "0".into(),
            ..Default::default()
        })
        .await
        .unwrap();
    let hb = mock.requests("/heartbeat");
    assert_eq!(hb.last().unwrap().edge_id, Some(client.edge_id()));
    let reopened = ControlClient::open(dir.path(), ControlConfig::default()).unwrap();
    assert_eq!(
        reopened.identity().cert_sha256().unwrap(),
        renewed.cert_sha256().unwrap()
    );
    reopened
        .heartbeat(&HeartbeatRequest {
            version: "0".into(),
            ..Default::default()
        })
        .await
        .unwrap();
    mock.assert_contract();
}

#[tokio::test]
async fn feed_follows_next_even_on_empty_pages() {
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    let client = enrolled(&mock, dir.path()).await;
    {
        let mut st = mock.state.lock().unwrap();
        for i in 1..=3 {
            st.feed.push(
                serde_json::json!({"type": "file.upsert", "entity_id": i.to_string(),
                "entity_version": "1", "file": {"file_id": i.to_string(), "dataset_id": "1",
                "name": format!("f{i}"), "kind": "file", "entity_version": "1"}}),
            );
        }
        st.feed_horizon = 10;
    }
    let mut poller = FeedPoller::new(client.clone(), None);
    poller.limit = 2;
    poller.wait_s = 0;
    let p = poller.poll().await.unwrap();
    assert_eq!(p.events.len(), 2);
    assert_eq!(
        poller.cursor(),
        "0.0",
        "not advanced until the caller says so"
    );
    let again = poller.poll().await.unwrap();
    assert_eq!(again, p, "re-read until advanced");
    poller.advance(&p.next);
    let p = poller.poll().await.unwrap();
    assert_eq!(p.events.len(), 1);
    poller.advance(&p.next);
    let p = poller.poll().await.unwrap();
    assert!(p.events.is_empty());
    assert_eq!(
        p.next, "0.10",
        "next advanced past events this edge doesn't get"
    );
    poller.advance(&p.next);
    assert_eq!(poller.cursor(), "0.10");
    poller.ack().await.unwrap();
    assert_eq!(mock.state.lock().unwrap().acks, vec!["0.10".to_string()]);

    poller.reset_to("9.9");
    assert!(poller.poll().await.unwrap().reset_required);
    mock.assert_contract();
}

#[tokio::test]
async fn a_hung_service_times_out_and_is_retryable() {
    // A service that accepts and never answers (a stuck replica, or a flow a
    // stateful firewall forgot). Every call must give up on its own timeout:
    // the feed and the heartbeat are one request at a time, so a call left
    // waiting would cut the edge off for good.
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    enrolled(&mock, dir.path()).await;
    let t = Duration::from_millis(400);
    let cfg = ControlConfig {
        timeouts: Timeouts {
            connect: t,
            feed_slack: t,
            recall: t,
            credentials: t,
            default: t,
        },
        ..ControlConfig::default()
    };
    let client = ControlClient::open(dir.path(), cfg).unwrap();
    mock.state.lock().unwrap().hang = true;
    let bound = Duration::from_secs(5);
    let started = std::time::Instant::now();
    let feed = tokio::time::timeout(bound, client.feed(FEED_START, 10, 0))
        .await
        .expect("the feed call outlived its timeout");
    let hb = tokio::time::timeout(bound, client.heartbeat(&HeartbeatRequest::default()))
        .await
        .expect("the heartbeat outlived its timeout");
    let cred = tokio::time::timeout(bound, client.read_credential(1, 1))
        .await
        .expect("the read credential outlived its timeout");
    for err in [feed.unwrap_err(), hb.unwrap_err(), cred.unwrap_err()] {
        // A timeout reads as the service being unavailable, and is retried.
        assert!(err.is_unavailable() && err.is_retryable(), "{err}");
    }
    assert!(
        started.elapsed() < Duration::from_secs(4),
        "{:?}",
        started.elapsed()
    );

    // And once the service answers again, so does the client.
    mock.state.lock().unwrap().hang = false;
    client.feed(FEED_START, 10, 0).await.unwrap();
}

#[tokio::test]
async fn unavailable_service_is_reported_and_retried() {
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    let client = enrolled(&mock, dir.path()).await;
    mock.state.lock().unwrap().down = true;
    let err = client.feed(FEED_START, 10, 0).await.unwrap_err();
    assert!(err.is_unavailable() && err.is_retryable(), "{err}");
    mock.state.lock().unwrap().down = false;

    mock.state.lock().unwrap().fail_next = 2;
    let mut poller = FeedPoller::new(client.clone(), None).with_backoff(Backoff::new(
        Duration::from_millis(10),
        Duration::from_millis(50),
    ));
    poller.wait_s = 0;
    let seen = Mutex::new(0);
    poller
        .poll_until_ok(|e, _| {
            assert!(e.is_unavailable());
            *seen.lock().unwrap() += 1;
        })
        .await
        .unwrap();
    assert_eq!(*seen.lock().unwrap(), 2);

    // Nothing listening at all: also "unavailable", quickly.
    let cfg = ControlConfig {
        api_url_override: Some("https://localhost:1".into()),
        ..Default::default()
    };
    let offline = ControlClient::open(dir.path(), cfg).unwrap();
    let err = offline.recall_start(&recall_request()).await.unwrap_err();
    assert!(err.is_unavailable(), "{err}");
    mock.assert_contract();
}

fn recall_request() -> RecallRequest {
    RecallRequest {
        file_id: 42,
        version_id: 7,
        offset: Some(0),
        length: Some(1 << 20),
        requester: Requester {
            uid: Some(41023),
            sid: Some("S-1-5-21-1-2-3-1104".into()),
            via: Via::Nfs,
            sec: Some(Sec::Sys),
            proof: Proof::Claimed,
            client_addr: Some("10.1.2.3".into()),
        },
        on_open: OnOpen::Wait,
        pinned: false,
    }
}

#[tokio::test]
async fn read_credentials_are_cached_until_cache_until() {
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    let client = enrolled(&mock, dir.path()).await;
    let a = client.read_credential(1, 2).await.unwrap();
    let b = client.read_credential(1, 2).await.unwrap();
    assert_eq!(a, b);
    assert_eq!(
        mock.requests("/credentials").len(),
        1,
        "second read served from cache"
    );
    client.read_credential(1, 3).await.unwrap();
    assert_eq!(
        mock.requests("/credentials").len(),
        2,
        "keyed by content version"
    );

    // A SAS whose cache_until is already here is not reused.
    mock.state.lock().unwrap().sas_ttl = Duration::from_secs(200);
    client.read_credential(5, 5).await.unwrap();
    client.read_credential(5, 5).await.unwrap();
    assert_eq!(mock.requests("/credentials").len(), 4);

    // While the service is down, a cached credential still works.
    mock.state.lock().unwrap().down = true;
    assert_eq!(client.read_credential(1, 2).await.unwrap(), a);
    assert!(
        client
            .read_credential(9, 9)
            .await
            .unwrap_err()
            .is_unavailable()
    );
    mock.assert_contract();
}

#[tokio::test]
async fn stubs_and_recalls() {
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    let client = enrolled(&mock, dir.path()).await;

    let StubPrepare::Lease(lease) = client.stub_prepare(42, 7).await.unwrap() else {
        panic!("expected a lease")
    };
    assert_eq!(lease.resident_bytes, 4 << 20);
    client
        .stub_commit(42, &lease.lease_id, 7, lease.resident_bytes)
        .await
        .unwrap();
    // stub/commit is idempotent by lease: a resend carries the same key.
    client
        .stub_commit(42, &lease.lease_id, 7, lease.resident_bytes)
        .await
        .unwrap();
    let commits = mock.requests("/stub/commit");
    assert_eq!(commits[0].idempotency_key, commits[1].idempotency_key);

    mock.state.lock().unwrap().stub_refuse = true;
    match client.stub_prepare(42, 7).await.unwrap() {
        StubPrepare::Refused { code, .. } => assert_eq!(code, "stub_not_allowed"),
        other => panic!("{other:?}"),
    }
    client
        .stub_skip(
            42,
            7,
            StubSkipReason::LeaseRefused,
            Some("409 from prepare"),
        )
        .await
        .unwrap();

    match client.recall_start(&recall_request()).await.unwrap() {
        RecallResponse::Recall {
            recall_id,
            state,
            decision,
            eta,
            ..
        } => {
            assert_eq!(state, RecallState::Rehydrating);
            assert_eq!(decision, Decision::Wait);
            assert!(eta.is_none());
            client.recall_filled(recall_id, 7, 1 << 30).await.unwrap();
        }
        other => panic!("{other:?}"),
    }
    mock.state.lock().unwrap().recall_online = true;
    let RecallResponse::Online { credential } =
        client.recall_start(&recall_request()).await.unwrap()
    else {
        panic!("expected online")
    };
    assert_eq!(
        client.read_credential(42, 7).await.unwrap(),
        credential,
        "online credential cached"
    );
    assert!(mock.requests("/credentials").is_empty());
    mock.assert_contract();
}

#[tokio::test]
async fn reads_are_batched_and_held_while_down() {
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    let client = enrolled(&mock, dir.path()).await;
    let now = SystemTime::now();
    let mut b = ReadsBatcher::new();
    b.record(1, now - Duration::from_secs(7200));
    b.record(1, now - Duration::from_secs(7100));
    b.record(2, now - Duration::from_secs(4000));
    b.record(3, now);

    mock.state.lock().unwrap().down = true;
    assert!(
        b.flush(&client, now, false)
            .await
            .unwrap_err()
            .is_unavailable()
    );
    assert_eq!(b.len(), 3, "held while the service is down");
    mock.state.lock().unwrap().down = false;

    assert_eq!(
        b.flush(&client, now, false).await.unwrap(),
        2,
        "past hours only"
    );
    let sent = mock.requests("/reads");
    let body = &sent.last().unwrap().body["reads"];
    assert_eq!(body.as_array().unwrap().len(), 2);
    assert_eq!(body[0]["file_id"], "1");
    assert_eq!(
        b.flush(&client, now, true).await.unwrap(),
        1,
        "forced: the current hour too"
    );
    mock.assert_contract();
}

#[tokio::test]
async fn changes_batch() {
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    let client = enrolled(&mock, dir.path()).await;
    let mut create = ChangeOp::new("e1:1", ChangeKind::Create);
    create.parent = Some(FileRef::Id { file_id: 1 });
    create.name = Some("line_1042.segy".into());
    create.kind = Some(NodeKind::File);
    create.attrs = Some(Attrs {
        size: Some(0),
        mtime_ns: Some(1_790_000_000_000_000_000),
        uid: Some(41023),
        gid: Some(513),
        mode: Some(0o644),
        ..Default::default()
    });
    let mut rename = ChangeOp::new("e1:2", ChangeKind::Rename);
    rename.file = Some(FileRef::Id { file_id: 7 });
    rename.new_parent = Some(FileRef::Id { file_id: 1 });
    rename.new_name = Some("renamed.segy".into());
    let results = client
        .changes(&[create, rename], &util::new_idempotency_key())
        .await
        .unwrap();
    assert_eq!(results.len(), 2);
    assert_eq!(results[0].status, ChangeStatus::Applied);
    mock.assert_contract();
}

// ---- outbox --------------------------------------------------------------

/// A sink that "uploads" by computing the outcome from the file.
struct FakeSink {
    calls: Mutex<Vec<UploadJob>>,
    fail_auth_once: Mutex<bool>,
}

impl FakeSink {
    fn new() -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
            fail_auth_once: Mutex::new(false),
        }
    }
}

impl UploadSink for FakeSink {
    async fn upload(&self, job: &UploadJob) -> Result<UploadOutcome, BlobError> {
        self.calls.lock().unwrap().push(job.clone());
        if std::mem::take(&mut *self.fail_auth_once.lock().unwrap()) {
            return Err(BlobError::Http {
                op: "Put Block",
                status: 403,
                code: Some("AuthenticationFailed".into()),
                message: "expired".into(),
                request_id: None,
                retry_after: None,
            });
        }
        let data = std::fs::read(&job.source).unwrap();
        Ok(UploadOutcome {
            etag: Some("\"etag\"".into()),
            version_id: Some("2026-10-05T00:00:00.0000000Z".into()),
            crc64nvme: crc64_nvme(&data),
            size: data.len() as u64,
            blocks_uploaded: job.plan.count(),
            blocks_resumed: 0,
            blocks: job
                .plan
                .blocks()
                .map(|b| omnifile_blob::CommittedBlock {
                    id: omnifile_integrity::block_id(7, b.index),
                    len: b.len,
                    crc64nvme: crc64_nvme(&data[b.offset as usize..(b.offset + b.len) as usize]),
                })
                .collect(),
        })
    }
}

fn new_upload(op_id: &str, src: &Path, data: &[u8], block_size: u64) -> NewUpload {
    let plan = BlockPlan::with_block_size(data.len() as u64, block_size).unwrap();
    NewUpload {
        op_id: op_id.into(),
        file: FileRef::Id { file_id: 42 },
        source: src.to_owned(),
        size: data.len() as u64,
        mtime_ns: 1_790_000_000_123_456_789,
        blake3: blake3_hex(data),
        crc64nvme: crc64_nvme(data),
        block_size,
        block_crcs: plan
            .blocks()
            .map(|b| crc64_nvme(&data[b.offset as usize..(b.offset + b.len) as usize]))
            .collect(),
        base_version_id: None,
        uid: 41023,
        gid: 513,
        mode: 0o640,
    }
}

fn blake3_hex(data: &[u8]) -> String {
    // The contract only needs 64 hex digits here; a SHA-256 stands in.
    util::sha256_hex(data)
}

#[tokio::test]
async fn outbox_commits_and_survives_restart() {
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    let client = enrolled(&mock, &dir.path().join("id")).await;
    let src = dir.path().join("clone");
    let data = vec![7u8; 300_000];
    std::fs::write(&src, &data).unwrap();
    let obox_dir = dir.path().join("outbox");
    let sink = FakeSink::new();

    // The service is down at first: the op waits, nothing is lost.
    let mut outbox = UploadOutbox::open(&obox_dir).unwrap();
    outbox
        .enqueue(new_upload("e1:10", &src, &data, 100_000))
        .unwrap();
    outbox
        .enqueue(new_upload("e1:10", &src, &data, 100_000))
        .unwrap(); // idempotent
    assert_eq!(outbox.len(), 1);
    mock.state.lock().unwrap().fail_next = 1;
    let err = outbox.drain(&client, &sink).await.unwrap_err();
    assert!(err.is_unavailable());
    assert_eq!(outbox.get("e1:10").unwrap().state, UploadState::Queued);
    drop(outbox);

    // "Restart": reopen from disk and drain.
    let mut outbox = UploadOutbox::open(&obox_dir).unwrap();
    let report = outbox.drain(&client, &sink).await.unwrap();
    assert_eq!(report.finished, vec!["e1:10".to_string()]);
    let op = outbox.get("e1:10").unwrap();
    assert!(
        matches!(op.state, UploadState::Committed { .. }),
        "{:?}",
        op.state
    );
    let intents = mock.requests("/uploads");
    assert_eq!(intents.len(), 2);
    assert_eq!(
        intents[0].idempotency_key, intents[1].idempotency_key,
        "same op, same key"
    );
    let completes = mock.state.lock().unwrap().completes.clone();
    assert_eq!(completes.len(), 1);
    assert_eq!(
        completes[0].1["crc64nvme"],
        format!("{:016x}", crc64_nvme(&data))
    );
    assert_eq!(completes[0].1["etag"], "\"etag\"");
    let job = &sink.calls.lock().unwrap()[0];
    assert!(!job.set_tier);
    assert_eq!(job.plan.count(), 3);
    assert_eq!(job.block_crcs.as_ref().unwrap().len(), 3);

    // The daemon deletes the clone, then removes the op.
    outbox.remove("e1:10").unwrap();
    assert!(outbox.is_empty());
    assert!(UploadOutbox::open(&obox_dir).unwrap().is_empty());
    mock.assert_contract();
}

#[tokio::test]
async fn outbox_dedupe_conflict_and_sas_renewal() {
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    let client = enrolled(&mock, &dir.path().join("id")).await;
    let src = dir.path().join("clone");
    let data = b"hello world".to_vec();
    std::fs::write(&src, &data).unwrap();
    let sink = FakeSink::new();
    let mut outbox = UploadOutbox::open(dir.path().join("outbox")).unwrap();

    mock.state.lock().unwrap().dedupe.insert(blake3_hex(&data));
    outbox
        .enqueue(new_upload("e1:1", &src, &data, 4 << 20))
        .unwrap();
    outbox.drain(&client, &sink).await.unwrap();
    assert_eq!(
        outbox.get("e1:1").unwrap().state,
        UploadState::Deduped { version_id: 555 }
    );
    assert!(
        sink.calls.lock().unwrap().is_empty(),
        "dedupe uploads nothing"
    );
    mock.state.lock().unwrap().dedupe.clear();

    mock.state.lock().unwrap().conflict_uploads = true;
    outbox
        .enqueue(new_upload("e1:2", &src, &data, 4 << 20))
        .unwrap();
    outbox.drain(&client, &sink).await.unwrap();
    assert!(matches!(
        outbox.get("e1:2").unwrap().state,
        UploadState::Conflict { .. }
    ));
    mock.state.lock().unwrap().conflict_uploads = false;

    // A write SAS already past cache_until is renewed before uploading, and a
    // 403 from Azure renews it once more.
    mock.state.lock().unwrap().sas_ttl = Duration::from_secs(100);
    *sink.fail_auth_once.lock().unwrap() = true;
    outbox
        .enqueue(new_upload("e1:3", &src, &data, 4 << 20))
        .unwrap();
    outbox.drain(&client, &sink).await.unwrap();
    assert!(matches!(
        outbox.get("e1:3").unwrap().state,
        UploadState::Committed { .. }
    ));
    let writes: Vec<_> = mock
        .requests("/credentials")
        .into_iter()
        .filter(|r| r.body["access"] == "write")
        .collect();
    assert!(writes.len() >= 2, "renewed: {}", writes.len());
    mock.assert_contract();
}

#[tokio::test(flavor = "multi_thread")]
async fn outbox_uploads_to_the_sim_blob_facade() {
    let Some(sim) = Sim::start() else { return };
    let mock = Mock::start().await;
    let dir = tempfile::tempdir().unwrap();
    let client = enrolled(&mock, &dir.path().join("id")).await;
    let sas = sim.sas("data", "edge/obj-1", "rcw", 3600).await;
    mock.state.lock().unwrap().upload_sas = Some(sas.clone());

    let mut rng = fastrand::Rng::with_seed(9);
    let data: Vec<u8> = (0..(3 << 20) + 17).map(|_| rng.u8(..)).collect();
    let src = dir.path().join("clone");
    std::fs::write(&src, &data).unwrap();
    // The sim facade is plain http (test only).
    let blob = BlobClient::new(BlobOptions {
        allow_http: true,
        ..BlobOptions::default()
    })
    .unwrap();
    let mut outbox = UploadOutbox::open(dir.path().join("outbox")).unwrap();
    outbox
        .enqueue(new_upload("e1:99", &src, &data, 1 << 20))
        .unwrap();
    outbox.drain(&client, &blob).await.unwrap();
    assert!(matches!(
        outbox.get("e1:99").unwrap().state,
        UploadState::Committed { .. }
    ));

    let props = blob.get_properties(&sas).await.unwrap();
    assert_eq!(props.size, data.len() as u64);
    assert_eq!(
        props.access_tier.as_deref(),
        Some("Smart"),
        "no explicit tier sent"
    );
    assert_eq!(
        props.metadata.get("file_permissions").map(String::as_str),
        Some("0640")
    );
    let back = blob.get_range(&sas, 0, data.len() as u64).await.unwrap();
    assert_eq!(&back[..], &data[..]);
    let completes = mock.state.lock().unwrap().completes.clone();
    assert_eq!(
        completes[0].1["crc64nvme"],
        format!("{:016x}", crc64_nvme(&data))
    );
    mock.assert_contract();
}
