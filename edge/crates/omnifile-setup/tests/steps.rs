//! The setup steps against a scripted machine: what a first run does, that a
//! rerun changes nothing, and each refusal that protects the box.

use std::time::Duration;

use omnifile_setup::answers::{Answers, Aws};
use omnifile_setup::aws;
use omnifile_setup::host::Fake;
use omnifile_setup::render::*;
use omnifile_setup::steps::{self, Ctx, IDENTITY_JSON, Outcome};

const PASSWORD: &str = "Pa55-w0rd!";
const BUNDLE: &str = "ofe1.eyJ0b2tlbiI6InNlY3JldCJ9";

fn answers() -> Answers {
    Answers::parse(&format!(
        r#"
        version = 1
        hostname = "edge1"
        [network]
        mode = "static"
        address = "10.1.2.10/24"
        gateway = "10.1.2.1"
        dns = ["10.1.0.10"]
        [data]
        device = "/dev/vdb"
        [ad]
        domain = "corp.example"
        workgroup = "CORP"
        join_user = "svc-join"
        join_ou = "OU=Filers,DC=corp,DC=example"
        admin_group = "CORP\\filer admins"
        [service]
        bundle = "{BUNDLE}"
        [updates]
        feed = "https://updates.corp.example/omnifile-edge/"
        reboot_window = "Sat *-*-* 02:00"
        "#
    ))
    .unwrap()
}

/// A fresh appliance: one NIC, an empty data disk, not joined, not enrolled.
fn fresh() -> Fake {
    let mut h = Fake::default();
    h.dirs
        .insert("/sys/class/net".into(), vec!["ens3".into(), "lo".into()]);
    h.files
        .insert("/sys/class/net/ens3/device".into(), (String::new(), 0o644));
    h.files.insert("/dev/vdb".into(), (String::new(), 0o660));
    h.files
        .insert("/etc/hosts".into(), ("127.0.0.1 localhost\n".into(), 0o644));
    h.files.insert(
        "/etc/nsswitch.conf".into(),
        (
            "passwd: files systemd\ngroup: files systemd\nhosts: files dns\n".into(),
            0o644,
        ),
    );
    // The data root mounts only once the step has formatted and enabled it.
    h.answer(
        "findmnt -n -o FSTYPE,UUID /srv/omnifile/data",
        0,
        "xfs 7d2c-uuid\n",
    );
    h.answer_once("findmnt -n -o FSTYPE,UUID /srv/omnifile/data", 1, "");
    h.answer("blkid -p -o value -s UUID /dev/vdb", 0, "7d2c-uuid\n");
    h.answer("blkid -p -o value -s LABEL /dev/vdb", 2, "");
    h.answer("lsblk -dno TYPE", 0, "disk\n");
    // Not joined until the join runs.
    h.answer("net ads testjoin", 0, "Join is OK\n");
    h.answer_once("net ads testjoin", 1, "");
    h.creates.push((
        "systemd-creds setup".into(),
        "/var/lib/systemd/credential.secret".into(),
    ));
    h.creates.push((
        "omnifile-edge --config /etc/omnifile/edge.toml enrol -".into(),
        IDENTITY_JSON.into(),
    ));
    h.answer(
        "omnifile-edge --config",
        0,
        "enrolled: edge_id=12 api_url=https://omnifile.internal\n",
    );
    h.answer("systemctl is-enabled", 1, "");
    // The recovery account: made here (no sysusers in the fake), then locked.
    h.answer_once("getent passwd omnifile-recovery", 2, "");
    h.answer(
        "passwd -S omnifile-recovery",
        0,
        "omnifile-recovery L 2026-10-16 0 99999 7 -1\n",
    );
    h
}

fn ctx() -> Ctx {
    let mut c = Ctx::new(answers());
    c.join_password = Some(PASSWORD.into());
    c.wait = Duration::ZERO;
    c.can_reveal = true;
    c
}

fn run(h: &mut Fake, c: &mut Ctx) -> Result<Vec<(&'static str, Outcome)>, steps::Failure> {
    steps::run(h, c, None, |_, _| {})
}

#[test]
fn a_first_run_sets_up_everything_and_keeps_secrets_off_command_lines() {
    let mut h = fresh();
    let mut c = ctx();
    let done = run(&mut h, &mut c).unwrap();
    assert_eq!(done.len(), steps::STEPS.len());
    // Everything but Roles Anywhere, which these answers leave out.
    assert!(
        done.iter().all(|(s, o)| matches!(o, Outcome::Changed(_))
            || (*s == "aws" && matches!(o, Outcome::Skipped(w) if w.contains("presigned")))),
        "{done:?}"
    );

    // Network on the one NIC, the disk formatted and mounted by UUID.
    assert!(h.file(NETWORK_FILE).unwrap().contains("Name=ens3\n"));
    assert!(
        h.ran
            .contains(&"mkfs.xfs -f -L omni-data /dev/vdb".to_string())
    );
    assert!(!h.ran_any("wipefs --all"), "an empty disk needs no wiping");
    assert!(
        h.file(DATA_MOUNT_UNIT)
            .unwrap()
            .contains("What=/dev/disk/by-uuid/7d2c-uuid")
    );
    assert!(
        h.file(EDGE_FILE)
            .unwrap()
            .contains("fs_uuid = \"7d2c-uuid\"")
    );
    assert!(h.file(EDGE_FILE).unwrap().contains("private_only = true"));

    // The join: into the OU, the password only in the environment.
    let join = h
        .ran
        .iter()
        .position(|l| l.starts_with("net ads join"))
        .unwrap();
    assert_eq!(
        h.ran[join],
        "net ads join -U svc-join createcomputer=OU=Filers,DC=corp,DC=example"
    );
    assert_eq!(
        h.envs[join],
        vec![("PASSWD".to_string(), PASSWORD.to_string())]
    );
    assert!(
        h.file("/etc/nsswitch.conf")
            .unwrap()
            .contains("passwd: files systemd winbind")
    );
    assert!(h.ran_any("getent group filer admins"));

    // Admins: sudoers 0440, checked by visudo.
    assert_eq!(h.files[SUDOERS_FILE].1, 0o440);
    assert!(h.ran_any("visudo -c -q -f /etc/sudoers.d/omnifile-admins"));

    // The recovery account: made, sudo, sealed with a password shown once and
    // given to chpasswd on stdin only.
    assert!(h.ran_any("useradd --system --create-home --home-dir /var/lib/omnifile-recovery"));
    assert_eq!(h.files[RECOVERY_SUDOERS_FILE].1, 0o440);
    assert!(h.ran_any("pam-auth-update --enable omnifile-recovery"));
    let pw = c.revealed.clone().expect("a recovery password to show");
    let cp = h.ran.iter().position(|l| l == "chpasswd").unwrap();
    assert_eq!(
        h.stdins[cp].as_deref(),
        Some(format!("omnifile-recovery:{pw}\n").as_str())
    );
    assert!(
        h.ran.iter().all(|l| !l.contains(&pw)),
        "the recovery password on a command line"
    );

    // Enrolment through stdin; the spent code leaves the answers.
    let e = h
        .ran
        .iter()
        .position(|l| l.starts_with("omnifile-edge --config"))
        .unwrap();
    assert_eq!(h.stdins[e].as_deref(), Some(BUNDLE));
    // The key it makes is sealed: the host key for that exists first.
    let k = h
        .ran
        .iter()
        .position(|l| l == "systemd-creds setup")
        .unwrap();
    assert!(k < e, "the host key is made before enrolment");
    assert!(
        h.file(EDGE_FILE)
            .unwrap()
            .contains("key_protection = \"auto\"")
    );
    // The daemon renders the shares; the servers include what it renders,
    // which exists (empty) from the start.
    let edge = h.file(EDGE_FILE).unwrap();
    assert!(edge.contains("[shares]\n") && edge.contains("\nrender = true\n"));
    assert!(
        h.file(SMB_SHARES_FILE)
            .unwrap()
            .contains(&format!("include = {RENDERED_SMB}"))
    );
    assert!(h.file(GANESHA_FILE).unwrap().contains(GANESHA_EXPORTS_FILE));
    assert!(
        h.file(GANESHA_EXPORTS_FILE)
            .unwrap()
            .contains(RENDERED_GANESHA)
    );
    assert!(h.file(RENDERED_SMB).is_some() && h.file(RENDERED_GANESHA).is_some());
    assert!(c.answers.service.bundle.is_empty());
    assert!(!c.answers.data.erase);

    // Updates and servers.
    for t in TRANSFERS {
        assert!(
            h.file(&transfer_dropin_path(t))
                .unwrap()
                .contains("Path=https://updates.corp.example/omnifile-edge/")
        );
    }
    assert!(
        h.file(REBOOT_TIMER_DROPIN)
            .unwrap()
            .contains("OnCalendar=Sat *-*-* 02:00")
    );
    assert!(h.ran_any("systemctl enable --now nfs-ganesha-omni.service"));
    // The annex supervisor waits for setup at boot (its appliance conditions): setup starts it.
    assert!(h.ran_any("systemctl enable --now omnifile-annex.service"));

    for (i, l) in h.ran.iter().enumerate() {
        assert!(
            !l.contains(PASSWORD) && !l.contains(BUNDLE),
            "secret on a command line: {l}"
        );
        if !l.starts_with("net ads join") {
            assert!(
                h.envs[i].iter().all(|(_, v)| v != PASSWORD),
                "password leaked to {l}"
            );
        }
    }
}

#[test]
fn a_rerun_with_the_same_answers_changes_nothing() {
    let mut h = fresh();
    let mut c = ctx();
    run(&mut h, &mut c).unwrap();
    let before = h.ran.len();
    h.answer("systemctl is-enabled", 0, "");
    h.answer(
        "passwd -S omnifile-recovery",
        0,
        "omnifile-recovery P 2026-10-16 0 99999 7 -1\n",
    );
    c.revealed = None;
    let done = run(&mut h, &mut c).unwrap();
    assert!(
        done.iter()
            .all(|(s, o)| *o == Outcome::Unchanged
                || (*s == "aws" && matches!(o, Outcome::Skipped(_)))),
        "{done:?}"
    );
    let again = &h.ran[before..];
    assert!(
        !again.iter().any(|l| l.starts_with("mkfs")
            || l.starts_with("net ads join")
            || l.starts_with("omnifile-edge")
            || l.starts_with("chpasswd")),
        "{again:?}"
    );
    assert!(c.revealed.is_none(), "a sealed account keeps its password");
}

#[test]
fn a_disk_with_data_is_kept_unless_erase_is_asked() {
    let mut h = fresh();
    h.answer("wipefs --no-act", 0, "gpt\nPMBR\n");
    let mut c = ctx();
    let f = steps::run(&mut h, &mut c, Some(&["data".into()]), |_, _| {}).unwrap_err();
    assert_eq!(f.step, "data");
    assert!(
        f.message.contains("not empty") && f.message.contains("ERASE"),
        "{f}"
    );
    assert!(!h.ran_any("mkfs") && !h.ran_any("wipefs --all"));

    c.answers.data.erase = true;
    h.answer_once("findmnt -n -o FSTYPE,UUID /srv/omnifile/data", 1, "");
    steps::run(&mut h, &mut c, Some(&["data".into()]), |_, _| {}).unwrap();
    let w = h
        .ran
        .iter()
        .position(|l| l == "wipefs --all /dev/vdb")
        .unwrap();
    let m = h
        .ran
        .iter()
        .position(|l| l.starts_with("mkfs.xfs"))
        .unwrap();
    assert!(w < m);
    assert!(
        !c.answers.data.erase,
        "the erase is not repeated on a rerun"
    );
}

#[test]
fn a_disk_already_formatted_by_setup_is_reused() {
    let mut h = fresh();
    h.answer("wipefs --no-act", 0, "xfs\n");
    h.answer("blkid -p -o value -s LABEL /dev/vdb", 0, "omni-data\n");
    let mut c = ctx();
    steps::run(&mut h, &mut c, Some(&["data".into()]), |_, _| {}).unwrap();
    assert!(!h.ran_any("mkfs"), "an interrupted setup's volume is kept");
}

#[test]
fn an_unattended_rerun_after_a_failed_join_keeps_the_data_disk_and_joins() {
    // First boot: the credential's answers ask to erase the disk, the disk is
    // formatted and mounted, and the join fails.
    let mut h = fresh();
    h.answer_once("net ads join", 1, "");
    let mut c = ctx();
    c.answers.data.erase = true;
    let f = run(&mut h, &mut c).unwrap_err();
    assert_eq!(f.step, "ad", "{f}");
    assert!(h.ran_any("mkfs.xfs -f -L omni-data /dev/vdb"));
    let first = h.ran.len();

    // Next boot: the mount unit has mounted the volume, and the unit runs
    // again from the same credential (erase still asked: it is read again).
    h.answer("lsblk -nro MOUNTPOINTS /dev/vdb", 0, "/srv/omnifile/data\n");
    h.answer("wipefs --no-act", 0, "xfs\n");
    h.answer("blkid -p -o value -s LABEL /dev/vdb", 0, "omni-data\n");
    h.answer_once("net ads testjoin", 1, ""); // still not joined
    let mut c = ctx();
    c.answers.data.erase = true;
    let done = run(&mut h, &mut c).unwrap();
    assert_eq!(
        done.iter().find(|(s, _)| *s == "data").unwrap().1,
        Outcome::Unchanged,
        "{done:?}"
    );
    assert!(
        done.iter().any(|(s, o)| *s == "ad"
            && matches!(o, Outcome::Changed(w) if w.contains("joined the domain"))),
        "{done:?}"
    );
    let again = &h.ran[first..];
    assert!(
        !again
            .iter()
            .any(|l| l.starts_with("mkfs") || l.starts_with("wipefs --all")),
        "the volume is never formatted again: {again:?}"
    );
    assert!(!c.answers.data.erase, "the saved answers never ask again");

    // The same boot with the volume not mounted yet (setup ran first): it is
    // the volume setup made, so it is reused, not erased.
    h.answer_once("findmnt -n -o FSTYPE,UUID /srv/omnifile/data", 1, "");
    h.answer("lsblk -nro MOUNTPOINTS /dev/vdb", 0, "\n");
    let before = h.ran.len();
    let mut c = ctx();
    c.answers.data.erase = true;
    steps::run(&mut h, &mut c, Some(&["data".into()]), |_, _| {}).unwrap();
    assert!(
        !h.ran[before..]
            .iter()
            .any(|l| l.starts_with("mkfs") || l.starts_with("wipefs --all")),
        "{:?}",
        &h.ran[before..]
    );
}

#[test]
fn the_data_disk_is_a_whole_disk_never_a_partition_by_any_name() {
    // Review on #37: the system disk's inactive /usr slot is unformatted and
    // unmounted, so nothing else marks it, and an update would overwrite it.
    let by_path = "/dev/disk/by-path/virtio-pci-0000:00:04.0-part4";
    for device in ["/dev/vda4", by_path] {
        let mut h = fresh();
        h.files.insert(device.into(), (String::new(), 0o660));
        h.answer(&format!("realpath -e -- {device}"), 0, "/dev/vda4\n");
        h.answer("lsblk -dno TYPE /dev/vda4", 0, "part\n");
        h.answer("lsblk -nro MOUNTPOINTS /dev/vda4", 0, "\n");
        let mut c = ctx();
        c.answers.data.device = device.into();
        c.answers.data.erase = true;
        let f = steps::run(&mut h, &mut c, Some(&["data".into()]), |_, _| {}).unwrap_err();
        assert!(
            f.message.contains("is a partition, not a whole disk"),
            "{device}: {f}"
        );
        assert!(
            !h.ran_any("mkfs") && !h.ran_any("wipefs --all"),
            "{device}: {:?}",
            h.ran
        );
    }

    // An alias of a whole disk is fine, and the disk it names is what is formatted.
    let alias = "/dev/disk/by-path/virtio-pci-0000:00:06.0";
    let mut h = fresh();
    h.files.insert(alias.into(), (String::new(), 0o660));
    h.answer(&format!("realpath -e -- {alias}"), 0, "/dev/vdb\n");
    let mut c = ctx();
    c.answers.data.device = alias.into();
    steps::run(&mut h, &mut c, Some(&["data".into()]), |_, _| {}).unwrap();
    assert!(
        h.ran_any("mkfs.xfs -f -L omni-data /dev/vdb"),
        "{:?}",
        h.ran
    );
}

#[test]
fn once_setup_made_the_data_volume_no_other_disk_is_formatted_in_its_place() {
    // Review on #37: the first boot formats /dev/vdb and the join fails. On a
    // later boot the volume isn't mounted, and /dev/vdb now names another
    // disk (sdX order changes on Hyper-V and vSphere), with data or empty.
    // The credential still asks to erase.
    for (holds, uuid) in [("ntfs\n", "9f9f-other\n"), ("", "")] {
        let mut h = fresh();
        h.answer_once("net ads join", 1, "");
        let mut c = ctx();
        c.answers.data.erase = true;
        assert_eq!(run(&mut h, &mut c).unwrap_err().step, "ad");
        let first = h.ran.len();

        h.answer_once("findmnt -n -o FSTYPE,UUID /srv/omnifile/data", 1, "");
        h.answer("wipefs --no-act", 0, holds);
        h.answer("blkid -p -o value -s UUID /dev/vdb", 0, uuid);
        let mut c = ctx();
        c.answers.data.erase = true;
        let f = steps::run(&mut h, &mut c, Some(&["data".into()]), |_, _| {}).unwrap_err();
        assert!(
            f.message
                .contains("is not the data volume an earlier run made (xfs 7d2c-uuid")
                && f.message.contains(DATA_MOUNT_UNIT),
            "{holds:?}: {f}"
        );
        assert!(
            !h.ran[first..]
                .iter()
                .any(|l| l.starts_with("mkfs") || l.starts_with("wipefs --all")),
            "{holds:?}: {:?}",
            &h.ran[first..]
        );
    }
}

#[test]
fn the_system_disk_is_never_formatted() {
    let mut h = fresh();
    h.answer("lsblk -nro MOUNTPOINTS /dev/vdb", 0, "/\n/usr\n");
    let mut c = ctx();
    c.answers.data.erase = true;
    let f = steps::run(&mut h, &mut c, Some(&["data".into()]), |_, _| {}).unwrap_err();
    assert!(f.message.contains("in use"), "{f}");
    assert!(!h.ran_any("mkfs") && !h.ran_any("wipefs --all"));
}

#[test]
fn joining_without_a_password_says_how_to_give_one() {
    let mut h = fresh();
    let mut c = ctx();
    c.join_password = None;
    let f = steps::run(&mut h, &mut c, Some(&["ad".into()]), |_, _| {}).unwrap_err();
    assert!(f.message.contains("omnifile.join-password"), "{f}");
    assert!(!h.ran_any("net ads join"));
}

#[test]
fn a_group_winbind_cannot_see_is_named() {
    let mut h = fresh();
    h.answer("getent group", 2, "");
    let mut c = ctx();
    let f = steps::run(&mut h, &mut c, Some(&["ad".into()]), |_, _| {}).unwrap_err();
    assert!(
        f.message.contains("filer admins") && f.message.contains("gidNumber"),
        "{f}"
    );
}

#[test]
fn a_sudoers_file_visudo_rejects_is_removed() {
    let mut h = fresh();
    h.answer("visudo", 1, "");
    let mut c = ctx();
    let f = steps::run(&mut h, &mut c, Some(&["admins".into()]), |_, _| {}).unwrap_err();
    assert_eq!(f.step, "admins");
    assert!(
        h.file(SUDOERS_FILE).is_none(),
        "a broken sudoers file would break sudo"
    );
}

#[test]
fn a_fixed_address_with_two_nics_needs_the_interface() {
    let mut h = fresh();
    h.dirs.insert(
        "/sys/class/net".into(),
        vec!["ens3".into(), "ens4".into(), "lo".into()],
    );
    h.files
        .insert("/sys/class/net/ens4/device".into(), (String::new(), 0o644));
    let mut c = ctx();
    let f = steps::run(&mut h, &mut c, Some(&["network".into()]), |_, _| {}).unwrap_err();
    assert!(
        f.message.contains("ens3, ens4") && f.message.contains("network.interface"),
        "{f}"
    );
    assert!(h.file(NETWORK_FILE).is_none());
}

#[test]
fn a_failed_enrolment_keeps_the_code_and_says_why() {
    let mut h = fresh();
    h.answer("omnifile-edge --config", 1, "");
    h.creates.clear();
    let mut c = ctx();
    let f = steps::run(&mut h, &mut c, Some(&["enrol".into()]), |_, _| {}).unwrap_err();
    assert!(f.message.contains("24 hours"), "{f}");
    assert_eq!(c.answers.service.bundle, BUNDLE);
    // The servers wait for enrolment.
    let done = steps::run(&mut h, &mut c, Some(&["services".into()]), |_, _| {}).unwrap();
    assert!(matches!(done[0].1, Outcome::Skipped(_)));
}

#[test]
fn updates_need_a_valid_window_and_go_away_with_the_feed() {
    let mut h = fresh();
    let mut c = ctx();
    h.answer("systemd-analyze calendar", 1, "");
    let f = steps::run(&mut h, &mut c, Some(&["updates".into()]), |_, _| {}).unwrap_err();
    assert!(f.message.contains("restart window"), "{f}");

    h.answer("systemd-analyze calendar", 0, "");
    steps::run(&mut h, &mut c, Some(&["updates".into()]), |_, _| {}).unwrap();
    assert!(h.file(&transfer_dropin_path("20-usr")).is_some());
    c.answers.updates.feed.clear();
    let done = steps::run(&mut h, &mut c, Some(&["updates".into()]), |_, _| {}).unwrap();
    assert!(matches!(&done[0].1, Outcome::Changed(w) if w.contains("removed")));
    assert!(h.file(&transfer_dropin_path("20-usr")).is_none());
}

#[test]
fn unattended_recovery_takes_a_hash_or_stays_locked() {
    let mut h = fresh();
    let mut c = ctx();
    c.can_reveal = false;
    let done = steps::run(&mut h, &mut c, Some(&["recovery".into()]), |_, _| {}).unwrap();
    assert!(
        matches!(&done[0].1, Outcome::Skipped(w) if w.contains("recovery seal")),
        "{done:?}"
    );
    assert!(!h.ran_any("chpasswd"), "no password nobody would see");

    let hash = "$y$j9T$abcdefgh$0123456789abcdefghijklmnopqrstuvwxyzABCD";
    c.recovery_hash = Some(hash.into());
    steps::run(&mut h, &mut c, Some(&["recovery".into()]), |_, _| {}).unwrap();
    let cp = h.ran.iter().position(|l| l == "chpasswd -e").unwrap();
    assert_eq!(
        h.stdins[cp].as_deref(),
        Some(format!("omnifile-recovery:{hash}\n").as_str())
    );
    assert!(c.revealed.is_none());

    // Not one hash: refused, and nothing reaches chpasswd (M7 review). A
    // second line would set root's password.
    for bad in [
        format!("{hash}\nroot:$y$j9T$x$y"),
        "hunter2".to_string(),
        format!("{hash}:0"),
    ] {
        let mut h = fresh();
        c.recovery_hash = Some(bad.clone());
        assert!(
            steps::run(&mut h, &mut c, Some(&["recovery".into()]), |_, _| {}).is_err()
                || !h.ran_any("chpasswd"),
            "{bad}"
        );
        assert!(!h.ran_any("chpasswd"), "{bad}");
    }
}

const TA: &str = "arn:aws:rolesanywhere:eu-west-1:123456789012:trust-anchor/1f2e3d4c-5b6a-4978-8a1b-2c3d4e5f6a7b";
const PROFILE: &str =
    "arn:aws:rolesanywhere:eu-west-1:123456789012:profile/0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
const ROLE: &str = "arn:aws:iam::123456789012:role/omnifile-edge-s3-read";

/// An enrolled filer (edge 12) with Roles Anywhere asked for, its
/// certificate from the client's own CA (`issuer = "client"`).
fn with_roles_anywhere() -> (Fake, Ctx) {
    let (h, mut c) = with_roles_anywhere_from_the_edge_ca();
    c.answers.aws.issuer = "client".into();
    (h, c)
}

/// The same with the default issuer (decision 50): OmniFile's edge CA.
fn with_roles_anywhere_from_the_edge_ca() -> (Fake, Ctx) {
    let mut h = fresh();
    h.files.insert(
        IDENTITY_JSON.into(),
        (
            r#"{"edge_id": 12, "api_url": "https://omnifile.internal", "not_after": "x"}"#.into(),
            0o644,
        ),
    );
    h.creates
        .push(("systemd-creds encrypt".into(), aws::KEY_CRED.into()));
    let mut c = ctx();
    c.answers.aws = Aws {
        trust_anchor_arn: TA.into(),
        profile_arn: PROFILE.into(),
        role_arn: ROLE.into(),
        session_seconds: 3600,
        ..Aws::default()
    };
    (h, c)
}

fn only(h: &mut Fake, c: &mut Ctx, step: &str) -> Result<Outcome, steps::Failure> {
    steps::run(h, c, Some(&[step.to_string()]), |_, _| {}).map(|mut d| d.remove(0).1)
}

/// What the trust anchor's CA does with the filer's request.
fn sign(csr_pem: &str, key_of: Option<&rcgen::KeyPair>) -> String {
    let ca_key = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
    let mut ca = rcgen::CertificateParams::new(Vec::<String>::new()).unwrap();
    ca.distinguished_name
        .push(rcgen::DnType::CommonName, "Test trust anchor");
    ca.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    let ca_cert = ca.self_signed(&ca_key).unwrap();
    let issuer = rcgen::Issuer::from_params(&ca, &ca_key);
    let csr = rcgen::CertificateSigningRequestParams::from_pem(csr_pem).unwrap();
    let mut p = csr.params.clone();
    p.not_before = time::OffsetDateTime::now_utc() - time::Duration::hours(1);
    p.not_after = time::OffsetDateTime::now_utc() + time::Duration::days(30);
    let cert = match key_of {
        Some(k) => p.signed_by(k, &issuer).unwrap(),
        None => p.signed_by(&csr.public_key, &issuer).unwrap(),
    };
    format!("{}{}", cert.pem(), ca_cert.pem())
}

#[test]
fn the_roles_anywhere_key_is_sealed_never_in_the_clear_and_waits_for_its_certificate() {
    let (mut h, mut c) = with_roles_anywhere();
    let o = only(&mut h, &mut c, "aws").unwrap();
    assert!(
        matches!(&o, Outcome::Changed(w) if w.contains("sealed to the TPM")
            && w.contains(aws::CSR_FILE) && w.contains("aws-cert")),
        "{o:?}"
    );
    // Sealed by systemd-creds, the key on its stdin only, bound to its name.
    let s = h
        .ran
        .iter()
        .position(|l| l.starts_with("systemd-creds encrypt"))
        .unwrap();
    assert_eq!(
        h.ran[s],
        "systemd-creds encrypt --name=roles-anywhere-key --with-key=auto --tpm2-pcrs= \
         --tpm2-public-key= - /var/lib/omnifile/identity/roles-anywhere.cred"
    );
    let key = h.stdins[s].clone().unwrap();
    assert!(key.contains("-----BEGIN PRIVATE KEY-----"));
    assert!(h.ran.iter().all(|l| !l.contains("PRIVATE")));
    assert!(
        h.files.values().all(|(f, _)| !f.contains("PRIVATE KEY")),
        "the key is never written in the clear"
    );
    assert!(h.ran_any("chmod 0600 /var/lib/omnifile/identity/roles-anywhere.cred"));
    // The request names the edge, as its mTLS certificate does, for this key.
    let csr = h.file(aws::CSR_FILE).unwrap().to_string();
    let parsed = rcgen::CertificateSigningRequestParams::from_pem(&csr).unwrap();
    let cn = parsed
        .params
        .distinguished_name
        .get(&rcgen::DnType::CommonName)
        .cloned();
    assert!(matches!(cn, Some(rcgen::DnValue::Utf8String(ref s)) if s == "omnifile-edge-12"));
    let key_pair = rcgen::KeyPair::from_pem(&key).unwrap();
    use rcgen::PublicKeyData;
    assert_eq!(parsed.public_key.der_bytes(), key_pair.der_bytes());
    // The helper's settings, no secret among them.
    let env = h.file(aws::ENV_FILE).unwrap();
    for l in [
        format!("OMNIFILE_RA_TRUST_ANCHOR_ARN={TA}"),
        format!("OMNIFILE_RA_PROFILE_ARN={PROFILE}"),
        format!("OMNIFILE_RA_ROLE_ARN={ROLE}"),
        "OMNIFILE_RA_REGION=eu-west-1".into(),
        "OMNIFILE_RA_PORT=9911".into(),
        "OMNIFILE_RA_SESSION_SECONDS=3600".into(),
    ] {
        assert!(env.lines().any(|x| x == l), "{l} in {env}");
    }
    assert!(!h.ran_any("systemctl restart"), "no certificate yet");

    // A rerun keeps the key and its request.
    let o = only(&mut h, &mut c, "aws").unwrap();
    assert!(matches!(&o, Outcome::Skipped(w) if w.contains("waiting for its certificate")));
    assert_eq!(
        h.ran
            .iter()
            .filter(|l| l.starts_with("systemd-creds encrypt"))
            .count(),
        1
    );
    assert_eq!(h.file(aws::CSR_FILE).unwrap(), csr);

    // The CA's certificate is checked against the key, installed with its
    // chain, and the helper started; the path is not kept in the answers.
    h.files
        .insert("/root/edge.pem".into(), (sign(&csr, None), 0o644));
    c.answers.aws.certificate = "/root/edge.pem".into();
    let o = only(&mut h, &mut c, "aws").unwrap();
    assert!(
        matches!(&o, Outcome::Changed(w) if w.contains("CN=omnifile-edge-12")),
        "{o:?}"
    );
    assert!(
        h.file(aws::CERT_FILE)
            .unwrap()
            .starts_with("-----BEGIN CERTIFICATE-----")
    );
    assert!(h.file(aws::CHAIN_FILE).is_some());
    assert!(
        h.file(aws::ENV_FILE)
            .unwrap()
            .contains("OMNIFILE_RA_INTERMEDIATES=/etc/omnifile/roles-anywhere-chain.pem")
    );
    assert!(h.ran_any("systemctl restart omnifile-aws-signing-helper.service"));
    assert!(c.answers.aws.certificate.is_empty());
    let restarts = h
        .ran
        .iter()
        .filter(|l| l.starts_with("systemctl restart"))
        .count();
    assert_eq!(only(&mut h, &mut c, "aws").unwrap(), Outcome::Unchanged);
    assert_eq!(
        h.ran
            .iter()
            .filter(|l| l.starts_with("systemctl restart"))
            .count(),
        restarts
    );
}

/// Decision 50, the default: the request goes where the daemon reads it,
/// the certificate comes from the service over mTLS (`omnifile-edge
/// roles-anywhere-cert`, standing in for the service here), and the helper
/// starts on it; nothing is signed or installed by hand.
#[test]
fn by_default_the_edge_ca_issues_the_certificate_over_mtls() {
    const GET: &str = "omnifile-edge --config /etc/omnifile/edge.toml roles-anywhere-cert";
    let (mut h, mut c) = with_roles_anywhere_from_the_edge_ca();
    h.creates.push((GET.into(), aws::RA_CERT.into()));
    h.answer(
        GET,
        0,
        "Roles Anywhere certificate: /var/lib/omnifile/roles-anywhere/cert.pem (until 2026-11-21T00:00:00Z)\n",
    );
    let o = only(&mut h, &mut c, "aws").unwrap();
    assert!(
        matches!(&o, Outcome::Changed(w) if w.contains("sealed to the TPM")
            && w.contains("OmniFile's edge CA") && w.contains("until 2026-11-21")
            && !w.contains("aws-cert")),
        "{o:?}"
    );
    // The request the daemon reads is the sealed key's.
    assert_eq!(h.file(aws::RA_CSR), h.file(aws::CSR_FILE));
    assert_eq!(h.files[aws::RA_CSR].1, 0o644);
    // Sealed first, then the certificate, then the helper; no secret on a command line.
    let at = |p: &str| h.ran.iter().position(|l| l.starts_with(p)).unwrap();
    assert!(at("systemd-creds encrypt") < at(GET));
    assert!(at(GET) < at("systemctl restart omnifile-aws-signing-helper.service"));
    assert!(h.ran.iter().all(|l| !l.contains("PRIVATE")));
    let env = h.file(aws::ENV_FILE).unwrap();
    assert!(env.contains("\nOMNIFILE_RA_CERTIFICATE=/var/lib/omnifile/roles-anywhere/cert.pem\n"));
    assert!(
        env.contains("\nOMNIFILE_RA_INTERMEDIATES=/var/lib/omnifile/roles-anywhere/chain.pem\n")
    );
    assert!(
        h.file(aws::CERT_FILE).is_none(),
        "nothing installed by hand"
    );

    // A rerun asks for nothing: the daemon renews it with its mTLS certificate.
    assert_eq!(only(&mut h, &mut c, "aws").unwrap(), Outcome::Unchanged);
    assert_eq!(h.ran.iter().filter(|l| l.starts_with(GET)).count(), 1);
    // A certificate from elsewhere is for the client-CA option only.
    c.answers.aws.certificate = "/root/edge.pem".into();
    assert!(
        c.answers
            .problems()
            .iter()
            .any(|p| p.field == "aws.certificate")
    );

    // The client's own CA, explicitly: the daemon's request is withdrawn, and
    // the certificate is waited for from that CA.
    c.answers.aws.certificate.clear();
    c.answers.aws.issuer = "client".into();
    let o = only(&mut h, &mut c, "aws").unwrap();
    assert!(
        matches!(&o, Outcome::Changed(w) if w.contains("your own CA") && w.contains("aws-cert")),
        "{o:?}"
    );
    assert!(h.file(aws::RA_CSR).is_none());
    assert!(
        !h.file(aws::ENV_FILE)
            .unwrap()
            .contains("OMNIFILE_RA_CERTIFICATE")
    );
}

/// Review on #32 (P2): switching issuers removes the other issuer's
/// certificate and stops the helper until the new one's is installed, both
/// ways, so the helper never keeps (or restart-loops on) the wrong one.
#[test]
fn switching_issuers_removes_the_old_certificate_and_stops_the_helper() {
    const GET: &str = "omnifile-edge --config /etc/omnifile/edge.toml roles-anywhere-cert";
    let (mut h, mut c) = with_roles_anywhere_from_the_edge_ca();
    h.creates.push((GET.into(), aws::RA_CERT.into()));
    only(&mut h, &mut c, "aws").unwrap();
    // What the daemon keeps there once it has installed a certificate.
    for f in [aws::RA_CHAIN, aws::RA_STATE, aws::RA_RESTART] {
        h.files.insert(f.into(), (String::new(), 0o644));
    }
    let stops = |h: &Fake| {
        h.ran
            .iter()
            .filter(|l| l.starts_with("systemctl stop omnifile-aws-signing-helper"))
            .count()
    };

    // Edge CA -> client CA: everything of the edge CA's goes, the helper stops.
    c.answers.aws.issuer = "client".into();
    let o = only(&mut h, &mut c, "aws").unwrap();
    assert!(
        matches!(&o, Outcome::Changed(w) if w.contains("now your own CA")),
        "{o:?}"
    );
    for f in [
        aws::RA_CSR,
        aws::RA_CERT,
        aws::RA_CHAIN,
        aws::RA_STATE,
        aws::RA_RESTART,
    ] {
        assert!(h.file(f).is_none(), "{f} left behind");
    }
    assert_eq!(stops(&h), 1);
    let last_stop = h
        .ran
        .iter()
        .rposition(|l| l.starts_with("systemctl stop"))
        .unwrap();
    assert!(
        !h.ran[last_stop..]
            .iter()
            .any(|l| l.starts_with("systemctl restart")),
        "not started again without a certificate"
    );
    // The client CA's certificate starts it again.
    let csr = h.file(aws::CSR_FILE).unwrap().to_string();
    h.files
        .insert("/root/edge.pem".into(), (sign(&csr, None), 0o644));
    c.answers.aws.certificate = "/root/edge.pem".into();
    only(&mut h, &mut c, "aws").unwrap();
    assert!(h.file(aws::CERT_FILE).is_some());
    assert!(h.ran.last().unwrap().starts_with("systemctl restart"));

    // Client CA -> edge CA: the client CA's certificate goes, the helper
    // stops, and starts again on the edge CA's.
    c.answers.aws.issuer = String::new();
    let o = only(&mut h, &mut c, "aws").unwrap();
    assert!(
        matches!(&o, Outcome::Changed(w) if w.contains("now OmniFile's edge CA")),
        "{o:?}"
    );
    assert!(h.file(aws::CERT_FILE).is_none() && h.file(aws::CHAIN_FILE).is_none());
    assert_eq!(stops(&h), 2);
    let at = |p: &str| h.ran.iter().rposition(|l| l.starts_with(p)).unwrap();
    assert!(at("systemctl stop") < at(GET) && at(GET) < at("systemctl restart"));
    assert!(h.file(aws::RA_CERT).is_some() && h.file(aws::RA_CSR).is_some());
    // No change: nothing removed, nothing stopped.
    assert_eq!(only(&mut h, &mut c, "aws").unwrap(), Outcome::Unchanged);
    assert_eq!(stops(&h), 2);
}

#[test]
fn before_enrolment_the_edge_ca_certificate_waits_for_the_daemon() {
    let (mut h, mut c) = with_roles_anywhere_from_the_edge_ca();
    h.files.remove(IDENTITY_JSON);
    let o = only(&mut h, &mut c, "aws").unwrap();
    assert!(
        matches!(&o, Outcome::Changed(w) if w.contains(aws::RA_CSR)
            && w.contains("once it is enrolled")),
        "{o:?}"
    );
    assert!(
        !h.ran_any("omnifile-edge"),
        "not enrolled: nothing to ask the service with"
    );
    assert!(!h.ran_any("systemctl restart"));
    // A failed request is the step's failure, named.
    h.files
        .insert(IDENTITY_JSON.into(), (r#"{"edge_id": 12}"#.into(), 0o644));
    h.answer("omnifile-edge --config", 1, "");
    let f = only(&mut h, &mut c, "aws").unwrap_err();
    assert!(f.message.contains("OmniFile's edge CA"), "{f}");
}

#[test]
fn a_certificate_for_another_key_is_refused() {
    let (mut h, mut c) = with_roles_anywhere();
    only(&mut h, &mut c, "aws").unwrap();
    let csr = h.file(aws::CSR_FILE).unwrap().to_string();
    let other = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256).unwrap();
    h.files
        .insert("/root/other.pem".into(), (sign(&csr, Some(&other)), 0o644));
    c.answers.aws.certificate = "/root/other.pem".into();
    let f = only(&mut h, &mut c, "aws").unwrap_err();
    assert!(f.message.contains("another key"), "{f}");
    assert!(h.file(aws::CERT_FILE).is_none());
    assert!(!h.ran_any("systemctl restart"));
}

#[test]
fn a_key_that_cannot_be_sealed_leaves_nothing_behind() {
    let (mut h, mut c) = with_roles_anywhere();
    h.creates.clear();
    h.answer("systemd-creds encrypt", 1, "");
    let f = only(&mut h, &mut c, "aws").unwrap_err();
    assert!(f.message.contains("sealing the Roles Anywhere key"), "{f}");
    assert!(h.file(aws::CSR_FILE).is_none() && h.file(aws::PUB_FILE).is_none());
    assert!(h.files.values().all(|(f, _)| !f.contains("PRIVATE KEY")));
}

#[test]
fn before_enrolment_the_request_names_the_host_and_off_stops_the_helper() {
    let (mut h, mut c) = with_roles_anywhere();
    h.files.remove(IDENTITY_JSON);
    only(&mut h, &mut c, "aws").unwrap();
    let csr =
        rcgen::CertificateSigningRequestParams::from_pem(h.file(aws::CSR_FILE).unwrap()).unwrap();
    assert!(matches!(
        csr.params.distinguished_name.get(&rcgen::DnType::CommonName),
        Some(rcgen::DnValue::Utf8String(s)) if s == "omnifile-edge-edge1"
    ));
    c.answers.aws = Aws::default();
    let o = only(&mut h, &mut c, "aws").unwrap();
    assert!(matches!(&o, Outcome::Changed(w) if w.contains("Roles Anywhere off")));
    assert!(h.file(aws::ENV_FILE).is_none());
    assert!(h.ran_any("systemctl stop omnifile-aws-signing-helper.service"));
    assert!(matches!(
        only(&mut h, &mut c, "aws").unwrap(),
        Outcome::Skipped(_)
    ));
}

#[test]
fn sealing_again_clears_the_used_mark() {
    let mut h = fresh();
    h.files
        .insert(RECOVERY_USED.into(), ("tty1 login\n".into(), 0o600));
    let pw = steps::seal_recovery(&mut h).unwrap();
    assert!(h.file(RECOVERY_USED).is_none());
    let groups: Vec<&str> = pw.split('-').collect();
    assert_eq!(groups.len(), 6);
    assert!(groups.iter().all(|g| {
        g.len() == 5
            && g.chars()
                .all(|c| "ABCDEFGHJKLMNPQRSTUVWXYZ23456789".contains(c))
    }));
    assert_ne!(pw, steps::new_recovery_password());
}
