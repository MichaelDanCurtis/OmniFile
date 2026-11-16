//! TPM-held keys (M6, `omnifile_control::seal`): enrolment and renewal with
//! the private key sealed by `systemd-creds`, against the in-process mock.
//!
//! The mode is process-wide, so this is its own test binary and one test.
//! It uses `host` (the host key, `/var/lib/systemd/credential.secret`),
//! which needs root and systemd ≥ 250; it is skipped, saying why, without
//! them. `tpm2` is tested on a box without a TPM: it must refuse, and
//! leave no key behind. The appliance test boots with a vTPM
//! (`appliance/tests/setup.sh`).

mod support;

use std::path::Path;

use omnifile_control::seal::{self, KeyProtection};
use omnifile_control::types::*;
use omnifile_control::*;
use support::mock::Mock;

fn facts() -> EdgeFacts {
    EdgeFacts {
        hostname: "edge-test".into(),
        os: None,
        kernel: None,
        version: "0.0.0".into(),
    }
}

fn names(dir: &Path) -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    v.sort();
    v
}

fn holds_a_key(dir: &Path) -> bool {
    std::fs::read_dir(dir).unwrap().any(|e| {
        let data = std::fs::read(e.unwrap().path()).unwrap();
        String::from_utf8_lossy(&data).contains("PRIVATE KEY")
    })
}

fn host_sealing_works() -> Result<(), String> {
    seal::set_key_protection(KeyProtection::Host);
    let r = seal::seal("omnifile-probe", b"x").map(|_| ());
    seal::set_key_protection(KeyProtection::File);
    r.map_err(|e| e.to_string())
}

async fn heartbeat(client: &ControlClient) {
    client
        .heartbeat(&HeartbeatRequest {
            version: "0".into(),
            ..Default::default()
        })
        .await
        .unwrap();
}

#[tokio::test]
async fn keys_sealed_with_systemd_credentials() {
    if let Err(e) = host_sealing_works() {
        eprintln!("SKIP: systemd-creds can't seal with the host key here: {e}");
        return;
    }
    let mock = Mock::start().await;

    // 1. Enrol sealed: the key is only ever on disk sealed.
    seal::set_key_protection(KeyProtection::Host);
    seal::probe().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let bundle = EnrolmentBundle::parse(&mock.bundle()).unwrap();
    enrol(&bundle, dir.path(), facts(), Default::default())
        .await
        .unwrap();
    assert_eq!(
        names(dir.path()),
        [
            "client-ca.pem",
            "identity.cred",
            "identity.json",
            "server-ca.pem"
        ]
    );
    assert!(!holds_a_key(dir.path()), "no plaintext key on disk");
    assert!(Identity::exists(dir.path()));
    use std::os::unix::fs::PermissionsExt;
    let mode = std::fs::metadata(dir.path().join("identity.cred"))
        .unwrap()
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600);

    // It opens, and mutual TLS works with it.
    let client = ControlClient::open(dir.path(), ControlConfig::default()).unwrap();
    heartbeat(&client).await;
    assert_eq!(
        mock.requests("/heartbeat").last().unwrap().edge_id,
        Some(client.edge_id())
    );

    // 2. The credential's name is bound in: it opens as nothing else.
    let sealed = std::fs::read(dir.path().join("identity.cred")).unwrap();
    let err = seal::unseal("omnifile-pending", &sealed).unwrap_err();
    assert!(err.to_string().contains("decrypt"), "{err}");
    assert!(seal::unseal("omnifile-identity", &sealed).is_ok());

    // 3. Renewal: the new key is sealed too, and the pending file goes.
    let before = client.identity().cert_sha256().unwrap();
    let renewed = client.renew_certificate().await.unwrap();
    assert_ne!(renewed.cert_sha256().unwrap(), before);
    assert_eq!(
        names(dir.path()),
        [
            "client-ca.pem",
            "identity.cred",
            "identity.json",
            "server-ca.pem"
        ]
    );
    assert!(!holds_a_key(dir.path()));
    heartbeat(&client).await;
    let reopened = ControlClient::open(dir.path(), ControlConfig::default()).unwrap();
    assert_eq!(
        reopened.identity().cert_sha256().unwrap(),
        renewed.cert_sha256().unwrap()
    );

    // 4. An edge enrolled before M6 (a 0600 file) is sealed in place the
    //    first time it is opened with sealing on.
    seal::set_key_protection(KeyProtection::File);
    let old = tempfile::tempdir().unwrap();
    let bundle = EnrolmentBundle::parse(&mock.bundle()).unwrap();
    enrol(&bundle, old.path(), facts(), Default::default())
        .await
        .unwrap();
    assert!(old.path().join("identity.pem").exists());
    assert!(holds_a_key(old.path()));
    seal::set_key_protection(KeyProtection::Host);
    let client = ControlClient::open(old.path(), ControlConfig::default()).unwrap();
    assert!(!old.path().join("identity.pem").exists());
    assert!(old.path().join("identity.cred").exists());
    assert!(!holds_a_key(old.path()));
    heartbeat(&client).await;
    // That key was on disk in the clear, so it is replaced at once: the
    // next renewal check renews, with a new key (review finding 5a).
    let was = client.identity().cert_sha256().unwrap();
    assert!(client.identity().key_was_plain);
    assert!(client.renew_if_due(None).await.unwrap(), "renewed at once");
    assert_ne!(client.identity().cert_sha256().unwrap(), was);
    assert!(!client.identity().key_was_plain);
    assert!(!client.renew_if_due(None).await.unwrap(), "and only once");

    // A plain copy left beside the sealed key (a crash between the two
    // writes) is removed when the key is read, and the key replaced
    // (review finding 5b).
    let key = seal::unseal(
        "omnifile-identity",
        &std::fs::read(old.path().join("identity.cred")).unwrap(),
    )
    .unwrap();
    std::fs::write(old.path().join("identity.pem"), &key).unwrap();
    let again = ControlClient::open(old.path(), ControlConfig::default()).unwrap();
    assert!(!old.path().join("identity.pem").exists());
    assert!(!holds_a_key(old.path()));
    assert!(again.identity().key_was_plain);

    // 5. `tpm2` on a box without one refuses before sending anything, and
    //    leaves no key behind.
    if !Path::new("/dev/tpmrm0").exists() && !Path::new("/dev/tpm0").exists() {
        seal::set_key_protection(KeyProtection::Tpm2);
        assert!(seal::probe().is_err(), "--check refuses tpm2 without a TPM");
        let none = tempfile::tempdir().unwrap();
        let sent = mock.requests("/v0/enrol").len();
        let bundle = EnrolmentBundle::parse(&mock.bundle()).unwrap();
        let err = enrol(&bundle, none.path(), facts(), Default::default())
            .await
            .unwrap_err();
        assert!(err.to_string().contains("systemd-creds"), "{err}");
        assert_eq!(mock.requests("/v0/enrol").len(), sent);
        assert!(names(none.path()).is_empty(), "{:?}", names(none.path()));
    }
    seal::set_key_protection(KeyProtection::File);
    mock.assert_contract();
}
