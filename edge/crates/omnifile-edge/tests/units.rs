//! The systemd wiring of the IAM Roles Anywhere helper (review on #23): it
//! is enabled on the appliance and by `edge/systemd/install.sh`, stays idle
//! until provisioned, and the daemon starts after it.

use std::path::PathBuf;

fn repo(rel: &str) -> String {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .join(rel);
    std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()))
}

fn lines(s: &str) -> Vec<&str> {
    s.lines().map(str::trim).collect()
}

#[test]
fn the_appliance_preset_enables_the_signing_helper() {
    let preset = repo("appliance/mkosi.extra/usr/lib/systemd/system-preset/80-omnifile.preset");
    assert!(
        lines(&preset).contains(&"enable omnifile-aws-signing-helper.service"),
        "{preset}"
    );
}

#[test]
fn install_sh_installs_and_links_the_signing_helper() {
    let sh = repo("edge/systemd/install.sh");
    assert!(sh.contains(
        "omnifile-aws-signing-helper.service\" \"$U/omnifile-aws-signing-helper.service\""
    ));
    assert!(sh.contains(
        "ln -sfn ../omnifile-aws-signing-helper.service \"$U/multi-user.target.wants/omnifile-aws-signing-helper.service\""
    ));
}

#[test]
fn the_image_ships_the_pinned_helper_and_refuses_another() {
    let pin = repo("appliance/third-party/aws_signing_helper/PIN");
    let sha = pin
        .lines()
        .find_map(|l| l.strip_prefix("HELPER_SHA256="))
        .expect("a pinned SHA-256");
    assert_eq!(sha.len(), 64);
    assert!(sha.bytes().all(|b| b.is_ascii_hexdigit()));
    assert!(pin.lines().any(|l| l.starts_with("HELPER_VERSION=")));
    let build = repo("appliance/build.sh");
    assert!(build.contains("[ \"$got\" = \"$HELPER_SHA256\" ] ||"));
    assert!(build.contains("--signing-helper \"$SIGNING_HELPER\""));
    let sh = repo("edge/systemd/install.sh");
    assert!(sh.contains(
        "install -D -m 0755 \"$SIGNING_HELPER\" \"$ROOT/usr/libexec/omnifile/aws_signing_helper\""
    ));
    // Its licence goes with it, and LICENSES.md names the pinned version.
    repo("appliance/third-party/aws_signing_helper/LICENSE");
    repo("appliance/third-party/aws_signing_helper/NOTICE");
    let v = pin
        .lines()
        .find_map(|l| l.strip_prefix("HELPER_VERSION="))
        .unwrap();
    let licenses = repo("LICENSES.md");
    assert!(
        licenses.contains(&format!("aws_signing_helper {v}")) && licenses.contains(sha),
        "LICENSES.md names aws_signing_helper {v} and its SHA-256"
    );
}

#[test]
fn the_helper_waits_for_provisioning_and_the_daemon_starts_after_it() {
    let helper = repo("edge/systemd/omnifile-aws-signing-helper.service");
    let h = lines(&helper);
    for c in [
        "ConditionPathExists=/etc/omnifile/aws-signing-helper.env",
        "ConditionPathExists=/var/lib/omnifile/identity/roles-anywhere.cred",
        // Either certificate: the edge CA's (decision 50) or the client CA's.
        "ConditionPathExists=|/var/lib/omnifile/roles-anywhere/cert.pem",
        "ConditionPathExists=|/etc/omnifile/roles-anywhere.pem",
        // And then only the configured issuer's (review on #32).
        "ExecCondition=/bin/sh -c 'test -s \"$${OMNIFILE_RA_CERTIFICATE:-/etc/omnifile/roles-anywhere.pem}\"'",
        "ConditionFileIsExecutable=/usr/libexec/omnifile/aws_signing_helper",
        "WantedBy=multi-user.target",
    ] {
        assert!(h.contains(&c), "missing {c}");
    }
    // It runs the pinned binary from that read-only path, never one on $PATH,
    // and every $ is the shell's (systemd's own expansion would split words).
    let exec: String = helper
        .split("ExecStart=")
        .nth(1)
        .unwrap()
        .split("\nRestart=")
        .next()
        .unwrap()
        .into();
    assert!(exec.contains("exec /usr/libexec/omnifile/aws_signing_helper serve"));
    assert!(exec.contains("--private-key \"$$CREDENTIALS_DIRECTORY/roles-anywhere-key\""));
    assert!(
        exec.replace("$$", "").find('$').is_none(),
        "a single $ in ExecStart: {exec}"
    );
    assert!(
        h.iter()
            .any(|l| l.starts_with("LoadCredentialEncrypted=roles-anywhere-key:")),
        "the key is sealed (TPM), never a plain file"
    );
    let edge = repo("edge/crates/omnifile-edge/systemd/omnifile-edge.service");
    let unit_line = |k: &str| {
        lines(&edge)
            .into_iter()
            .filter(|l| l.starts_with(k))
            .flat_map(|l| l[k.len()..].split_whitespace())
            .map(str::to_owned)
            .collect::<Vec<_>>()
    };
    assert!(unit_line("After=").contains(&"omnifile-aws-signing-helper.service".to_owned()));
    assert!(unit_line("Wants=").contains(&"omnifile-aws-signing-helper.service".to_owned()));
    assert!(
        !unit_line("Requires=").contains(&"omnifile-aws-signing-helper.service".to_owned()),
        "an unprovisioned helper must not stop the daemon"
    );
}

/// An NFS annex mounts with sec=krb5, which needs rpc.gssd: a static unit
/// (no [Install]) that nothing else on the appliance starts. The per-annex
/// unit wants it, and orders itself after it (WP14 A2.3 review).
#[test]
fn an_annex_unit_brings_up_rpc_gssd() {
    let unit = repo("edge/crates/omnifile-edge/systemd/omnifile-annex@.service");
    let u = lines(&unit);
    let words = |key: &str| -> Vec<String> {
        u.iter()
            .filter_map(|l| l.strip_prefix(key))
            .flat_map(|v| v.split_whitespace().map(str::to_owned))
            .collect()
    };
    assert!(
        words("Wants=").contains(&"rpc-gssd.service".to_owned()),
        "{unit}"
    );
    assert!(
        words("After=").contains(&"rpc-gssd.service".to_owned()),
        "{unit}"
    );
}

/// On the appliance the annex supervisor waits for setup's config and the
/// enrolment instead of restarting every 10 s (WP14 A2.3's first boot).
#[test]
fn the_appliance_annex_supervisor_waits_for_setup() {
    let d = repo(
        "appliance/mkosi.extra/usr/lib/systemd/system/omnifile-annex.service.d/05-appliance.conf",
    );
    let l = lines(&d);
    for c in [
        "ConditionPathExists=/etc/omnifile/edge.toml",
        "ConditionPathExists=/var/lib/omnifile/identity/identity.json",
    ] {
        assert!(l.contains(&c), "missing {c}");
    }
}

/// The keytabs are sealed with systemd-creds and imported by the per-annex
/// unit, never read in the clear from /etc (WP14 A2.2, an A1 follow-up).
#[test]
fn an_annex_unit_imports_its_sealed_keytabs() {
    let unit = repo("edge/crates/omnifile-edge/systemd/omnifile-annex@.service");
    let l = lines(&unit);
    for c in [
        "ImportCredential=annex.keytab",
        "ImportCredential=annex-remove.keytab",
        "ImportCredential=annex-restore.keytab",
    ] {
        assert!(l.contains(&c), "missing {c}");
    }
}
