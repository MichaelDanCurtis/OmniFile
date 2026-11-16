//! The client's wire types against `service/openapi/control-api.yaml`.
//!
//! Requests are also validated live by the mock in `tests/control.rs`; this
//! file covers every endpoint the client knows, response shapes (the
//! client must read what the service sends), and request variants the
//! integration tests don't send.

mod support;

use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};

use omnifile_control::types::*;
use support::mock::{find_template, spec, validate};

fn request_schema<'a>(spec: &'a Value, method: &str, path: &str) -> &'a Value {
    let s = &spec["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"];
    assert!(!s.is_null(), "{method} {path}: no request schema");
    s
}

fn response_schema<'a>(spec: &'a Value, method: &str, path: &str, status: &str) -> &'a Value {
    let r = &spec["paths"][path][method]["responses"][status];
    assert!(!r.is_null(), "{method} {path}: no {status} response");
    let s = &r["content"]["application/json"]["schema"];
    assert!(!s.is_null(), "{method} {path} {status}: no JSON schema");
    s
}

/// `value` serialises to JSON valid against the request schema.
fn check_request<T: Serialize>(spec: &Value, method: &str, path: &str, value: &T) {
    let v = serde_json::to_value(value).unwrap();
    validate(spec, request_schema(spec, method, path), &v)
        .unwrap_or_else(|e| panic!("{method} {path} request {v}: {e}"));
}

/// A schema-valid response example parses into `T`, and `T` re-serialises
/// to something still valid.
fn check_response<T: Serialize + DeserializeOwned>(
    spec: &Value,
    method: &str,
    path: &str,
    status: &str,
    example: Value,
) {
    let schema = response_schema(spec, method, path, status);
    validate(spec, schema, &example)
        .unwrap_or_else(|e| panic!("{method} {path} example {example}: {e}"));
    let parsed: T = serde_json::from_value(example.clone())
        .unwrap_or_else(|e| panic!("{method} {path}: client can't read {example}: {e}"));
    let back = serde_json::to_value(&parsed).unwrap();
    validate(spec, schema, &back)
        .unwrap_or_else(|e| panic!("{method} {path} re-serialised {back}: {e}"));
}

#[test]
fn every_endpoint_the_client_calls_exists() {
    let spec = spec();
    for (method, path) in ENDPOINTS {
        assert!(
            !spec["paths"][*path][*method].is_null(),
            "{method} {path} is not in control-api.yaml"
        );
        assert_eq!(
            find_template(&spec, &path.replace("{edge_id}", "1")),
            Some(*path)
        );
    }
}

fn sas() -> Value {
    json!({"mode": "azure_user_delegation_sas", "url": "https://acct.privatelink.blob.core.windows.net/c/k?sig=x",
           "expires_at": "2026-10-05T13:00:00Z", "cache_until": "2026-10-05T12:55:00Z"})
}

#[test]
fn requests_match_the_schemas() {
    let spec = spec();
    check_request(
        &spec,
        "post",
        "/v0/enrol",
        &EnrolRequest {
            token: "t".repeat(43),
            csr_pem: "-----BEGIN CERTIFICATE REQUEST-----".into(),
            facts: EdgeFacts {
                hostname: "h".into(),
                os: None,
                kernel: None,
                version: "0.0.0".into(),
            },
        },
    );
    check_request(
        &spec,
        "post",
        "/v0/edges/{edge_id}/certificate",
        &RenewRequest {
            csr_pem: "x".into(),
            proof: RenewalProof {
                signed_at: "2026-10-05T12:00:00Z".into(),
                signature: "MEUCIQ==".into(),
            },
        },
    );
    // ACLs (§4.6): the bodies ServiceControl / ControlClient send.
    check_request(
        &spec,
        "post",
        "/v0/edges/{edge_id}/acls",
        &json!({"dataset_id": "7", "acls": [{
        "sha256": "a".repeat(64), "owner_sid": "S-1-5-21-1-2-3-1105",
        "group_sid": "S-1-5-21-1-2-3-513", "mode": 488,
        "entries": [
            {"scope": "access", "tag": "owner", "perms": "rwx"},
            {"scope": "access", "tag": "group", "sid": "S-1-5-21-1-2-3-1103", "perms": "r-x"}
        ]}]}),
    );
    check_request(
        &spec,
        "post",
        "/v0/edges/{edge_id}/acl-scans",
        &json!({"dataset_id": "7", "kind": "full"}),
    );
    check_request(
        &spec,
        "post",
        "/v0/edges/{edge_id}/acl-scans/{scan_id}/complete",
        &json!({"entries_seen": 10, "entries_changed": 2}),
    );
    check_request(
        &spec,
        "post",
        "/v0/edges/{edge_id}/heartbeat",
        &HeartbeatRequest {
            version: "0.1.0".into(),
            kernel: Some("7.0".into()),
            guard_state: Some(GuardState::Degraded),
            landing_used_bytes: Some(1),
            landing_capacity_bytes: Some(2),
            cache_used_bytes: Some(3),
            outbox_depth: Some(4),
            feed_cursor: Some("12.34".into()),
            fills_running: Some(5),
            recalls_waiting: Some(6),
            data_free_bytes: Some(7),
            data_capacity_bytes: Some(8),
            fsal_connections: Some(9),
            recovery_used: Some(1),
            quiet_default_s: Some(10),
            quiet_worm_default_s: Some(60),
            quiet_stale_s: Some(0),
            recall_superseded: Some(1),
        },
    );
    check_request(
        &spec,
        "post",
        "/v0/edges/{edge_id}/feed/ack",
        &FeedAck {
            cursor: "1.2".into(),
        },
    );
    let path = "/v0/edges/{edge_id}/uploads";
    for file in [
        // Catalog ids are Postgres bigints: at most i64::MAX (19 digits).
        FileRef::Id {
            file_id: i64::MAX as u64,
        },
        FileRef::Local {
            local: LocalRef {
                fsid: "f".into(),
                ino: "1".into(),
                ino_gen: "2".into(),
            },
        },
    ] {
        for base in [None, Some(5)] {
            check_request(
                &spec,
                "post",
                path,
                &UploadIntent {
                    op_id: "e1:1".into(),
                    file: file.clone(),
                    size: 197_568_495_616,
                    mtime_ns: -1,
                    blake3: "a".repeat(64),
                    crc64nvme: 0x0123_4567_89ab_cdef,
                    block_size: 268_435_456,
                    base_version_id: base,
                },
            );
        }
    }
    check_request(
        &spec,
        "post",
        "/v0/edges/{edge_id}/uploads/{upload_id}/complete",
        &UploadComplete {
            crc64nvme: 1,
            blake3: "b".repeat(64),
            size: 0,
            etag: Some("\"0x8D\"".into()),
            backend_version_id: None,
            blocks: vec![CompleteBlock {
                id: "AAAAAAAAAAEAAAAA".into(),
                length: 0,
                crc64nvme: 0,
            }],
        },
    );
    let path = "/v0/edges/{edge_id}/credentials";
    check_request(
        &spec,
        "post",
        path,
        &CredentialRequest::Read {
            file_id: 1,
            version_id: 2,
        },
    );
    check_request(
        &spec,
        "post",
        path,
        &CredentialRequest::Write { upload_id: 3 },
    );
    check_request(
        &spec,
        "post",
        "/v0/edges/{edge_id}/files/{file_id}/stub/prepare",
        &StubPrepareRequest { version_id: 1 },
    );
    check_request(
        &spec,
        "post",
        "/v0/edges/{edge_id}/files/{file_id}/stub/commit",
        &StubCommitRequest {
            lease_id: "176722560.0123456789abcdef0123456789abcdef".into(),
            version_id: 2,
            resident_bytes: 4 << 20,
        },
    );
    for reason in [
        StubSkipReason::VersionMismatch,
        StubSkipReason::OpenForWrite,
        StubSkipReason::LeaseRefused,
        StubSkipReason::LocalError,
    ] {
        check_request(
            &spec,
            "post",
            "/v0/edges/{edge_id}/files/{file_id}/stub/skip",
            &StubSkipRequest {
                version_id: 1,
                reason,
                detail: None,
            },
        );
    }
    for (via, sec, proof) in [
        (Via::Nfs, Sec::Sys, Proof::Claimed),
        (Via::Nfs, Sec::Krb5p, Proof::Proven),
        (Via::Smb, Sec::Kerberos, Proof::Proven),
        (Via::Smb, Sec::Ntlm, Proof::Proven),
    ] {
        for on_open in [OnOpen::Wait, OnOpen::Fail, OnOpen::Auto] {
            check_request(
                &spec,
                "post",
                "/v0/edges/{edge_id}/recalls",
                &RecallRequest {
                    file_id: 1,
                    version_id: 2,
                    offset: None,
                    length: None,
                    requester: Requester {
                        uid: Some(0),
                        sid: Some("S-1-5-21-1-2-3-500".into()),
                        via,
                        sec: Some(sec),
                        proof,
                        client_addr: None,
                    },
                    on_open,
                    // Sent only when true: both shapes are the contract's.
                    pinned: matches!(on_open, OnOpen::Auto),
                },
            );
        }
    }
    check_request(
        &spec,
        "post",
        "/v0/edges/{edge_id}/recalls/{recall_id}/filled",
        &RecallFilledRequest {
            version_id: 1,
            resident_bytes: 0,
        },
    );
    check_request(
        &spec,
        "post",
        "/v0/edges/{edge_id}/reads",
        &ReadsRequest {
            reads: vec![ReadEntry {
                file_id: 1,
                last_read_at: "2026-10-05T12:00:00Z".into(),
            }],
        },
    );
    let mut setattr = ChangeOp::new("e1:5", ChangeKind::Setattr);
    setattr.file = Some(FileRef::Id { file_id: 9 });
    setattr.attrs = Some(Attrs {
        mode: Some(0o4755),
        owner_sid: Some("S-1-5-21-1-2-3-1104".into()),
        acl_sha256: Some("c".repeat(64)),
        ..Default::default()
    });
    setattr.expected_entity_version = Some(3);
    let mut unlink = ChangeOp::new("e1:6", ChangeKind::Unlink);
    unlink.file = Some(FileRef::Id { file_id: 9 });
    check_request(
        &spec,
        "post",
        "/v0/edges/{edge_id}/changes",
        &ChangesRequest {
            ops: vec![setattr, unlink],
        },
    );
}

#[test]
fn responses_parse_and_match_the_schemas() {
    let spec = spec();
    let cert = json!({"edge_id": "12", "cert_pem": "c", "chain_pem": "", "ca_pem": "a",
                      "not_after": "2026-11-04T00:00:00Z", "api_url": "https://omnifile.internal"});
    check_response::<IssuedCertificate>(&spec, "post", "/v0/enrol", "201", cert.clone());
    check_response::<IssuedCertificate>(
        &spec,
        "post",
        "/v0/edges/{edge_id}/certificate",
        "201",
        cert,
    );
    check_response::<HeartbeatResponse>(
        &spec,
        "post",
        "/v0/edges/{edge_id}/heartbeat",
        "200",
        json!({"server_time": "2026-10-05T12:00:00Z", "min_edge_version": "0.1.0", "feed_lag_events": 3,
               "config_version": "9", "certificate_renew_after": "2026-10-25T00:00:00Z"}),
    );
    check_response::<FeedPage>(
        &spec,
        "get",
        "/v0/edges/{edge_id}/feed",
        "200",
        json!({"events": [{"type": "recall.state", "entity_id": "5", "entity_version": "2",
                           "recall": {"recall_id": "5", "file_id": "1", "version_id": "2",
                                      "state": "downloading", "entity_version": "2", "read_credential": sas()}}],
               "next": "17.4", "reset_required": false}),
    );
    let path = "/v0/edges/{edge_id}/uploads";
    check_response::<UploadStart>(
        &spec,
        "post",
        path,
        "201",
        json!({"action": "upload", "upload_id": "8", "version_id": "8", "target": sas(),
               "block_size": 268435456, "set_tier": false}),
    );
    check_response::<UploadStart>(
        &spec,
        "post",
        path,
        "201",
        json!({"action": "dedupe", "version_id": "8"}),
    );
    check_response::<UploadCommitted>(
        &spec,
        "post",
        "/v0/edges/{edge_id}/uploads/{upload_id}/complete",
        "200",
        json!({"version_id": "8", "committed_at": "2026-10-05T12:00:00Z", "retention_until": null}),
    );
    let path = "/v0/edges/{edge_id}/credentials";
    check_response::<Credential>(&spec, "post", path, "200", sas());
    check_response::<Credential>(
        &spec,
        "post",
        path,
        "200",
        json!({"mode": "s3_roles_anywhere", "bucket": "b", "key": "k", "version_id": null,
               "region": "eu-west-1", "endpoint": "bucket.vpce-1.s3.eu-west-1.vpce.amazonaws.com"}),
    );
    check_response::<Credential>(
        &spec,
        "post",
        path,
        "200",
        json!({"mode": "s3_roles_anywhere", "bucket": "b", "key": "k",
               "etag": "\"9b2cf535f27731c974343645a3985328\"",
               "crc64nvme": "00000000deadbeef", "region": "eu-west-1",
               "endpoint": "bucket.vpce-1.s3.eu-west-1.vpce.amazonaws.com",
               "addressing": "virtual_host", "tls_max_version": "1.2",
               "requester_pays": false, "private_network": true}),
    );
    check_response::<StubLease>(
        &spec,
        "post",
        "/v0/edges/{edge_id}/files/{file_id}/stub/prepare",
        "200",
        json!({"lease_id": "176722560.0123456789abcdef0123456789abcdef", "resident_bytes": 4194304, "lease_expires_at": "2026-10-05T12:10:00Z"}),
    );
    let path = "/v0/edges/{edge_id}/recalls";
    check_response::<RecallResponse>(
        &spec,
        "post",
        path,
        "200",
        json!({"status": "online", "credential": sas()}),
    );
    check_response::<RecallResponse>(
        &spec,
        "post",
        path,
        "200",
        json!({"status": "recall", "recall_id": "4", "state": "requested", "joined": true,
               "eta": "2026-10-05T20:00:00Z", "eta_confidence": "provider_sla", "decision": "fail",
               "note": "Archived; recall started."}),
    );
    check_response::<ReadsResponse>(
        &spec,
        "post",
        "/v0/edges/{edge_id}/reads",
        "200",
        json!({"accepted": 2, "unknown_file_ids": ["99"]}),
    );
    check_response::<ChangesResponse>(
        &spec,
        "post",
        "/v0/edges/{edge_id}/changes",
        "200",
        json!({"results": [{"op_id": "e1:1", "status": "rejected", "code": "name_invalid", "detail": "x"},
                           {"op_id": "e1:2", "status": "applied", "file_id": "7", "version_id": "8",
                            "entity_version": "1"}]}),
    );
}

#[test]
fn problem_details_parse() {
    let spec = spec();
    let p = json!({"type": "urn:omnifile:error:stub_not_allowed", "title": "Stub not allowed", "status": 409,
                   "code": "stub_not_allowed", "detail": "keep_online hold 812", "retryable": false,
                   "retry_after_s": null, "request_id": "r"});
    validate(&spec, &json!({"$ref": "#/components/schemas/Problem"}), &p).unwrap();
    let parsed: Problem = serde_json::from_value(p).unwrap();
    assert_eq!(parsed.code, "stub_not_allowed");
}
